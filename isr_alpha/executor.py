"""
ISR Alpha Executor — Alpaca Order Execution with ISR Context
=============================================================
Executes rebalance orders for the High Risk wallet using Alpaca API.
Integrates with existing capitol_copier infrastructure.
"""
import os
import json
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional

from capitol_copier import (
    BASE_URL, DATA_URL, ALPACA_HEADERS,
    place_market_order, get_positions, get_account_equity,
)
from intraday_momentum import get_clock

import strategies
import isr_alpha.signals as signals
import isr_alpha.portfolio as portfolio


# State file for ISR Alpha (per-wallet)
def _state_file() -> str:
    return strategies.state_path(".isr_alpha_state.json")


def load_state() -> dict:
    if os.path.exists(_state_file()):
        with open(_state_file()) as f:
            return json.load(f)
    return {
        "last_rebalance": None,
        "last_catalyst_check": None,
        "positions": {},  # ticker -> entry info
    }


def save_state(state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(_state_file(), "w") as f:
        json.dump(state, f, indent=2)


def load_merged_config() -> dict:
    """Load global config �� active wallet strategy."""
    return strategies.load_merged()


def get_isr_config(cfg: dict) -> dict:
    """Get ISR Alpha config section from merged config."""
    return cfg.get("isr_alpha", {
        "enabled": True,
        "rebalance_every_minutes": 15,
        "catalyst_check_every_minutes": 5,
        "max_positions": 15,
        "max_sector_pct": 0.30,
        "max_gross_leverage": 1.5,
        "base_position_usd": 5000,
        "max_position_pct": 0.16,
        "max_turnover_pct": 0.50,
        "catalyst_threshold": 0.6,
        "moat_threshold": 7.0,
        "capex_threshold": 0.7,
    })


def cancel_all_orders() -> bool:
    """Cancel all open orders."""
    r = requests.delete(f"{BASE_URL}/orders", headers=ALPACA_HEADERS, timeout=15)
    return r.status_code in (200, 207)


def get_current_positions_usd() -> Dict[str, float]:
    """Get current positions as ticker -> market value USD."""
    positions = get_positions()
    result = {}
    for p in positions:
        try:
            ticker = p["symbol"]
            qty = float(p.get("qty", 0))
            price = float(p.get("market_value", 0)) / abs(qty) if qty != 0 else 0
            market_value = float(p.get("market_value", 0))
            if market_value != 0:
                result[ticker.upper()] = market_value
        except Exception:
            continue
    return result


def other_engine_tickers() -> set:
    """Tickers owned by OTHER engines — ISR must never liquidate these.

    ISR sizes a target book from its own ranking and rebalance_portfolio()
    assigns target 0 to anything not in that book. Without this guard it sells
    whatever swing_buyer, intraday_momentum or the inverse hedge just bought
    (observed 2026-08-24 and 2026-08-26: swing bought MPC/MRK/MRNA/PSX at
    09:35, ISR sold all four at 09:47).
    """
    owned = set()
    for fname, key in ((".swing_state.json", "entries"),
                       (".intraday_state.json", "entries")):
        try:
            with open(strategies.state_path(fname)) as f:
                owned.update(k.upper() for k in (json.load(f).get(key) or {}))
        except Exception:
            pass
    try:
        import inverse_hedge
        owned.add(str(inverse_hedge.INVERSE_ETF).upper())
    except Exception:
        pass
    return owned


def _regime_blocks_entries(cfg_section):
    """True when the market is in a confirmed downtrend and this engine is gated.

    A brake on ENTRY only — managing, trimming and exiting existing positions
    continues normally. On 2026-08-17/18 every engine except swing_buyer traded
    straight through the drawdown as if nothing had changed.
    """
    if not (cfg_section or {}).get("regime_gate", True):
        return False, "unknown"
    try:
        import market_context
        state = market_context.regime_state().get("state", "unknown")
    except Exception:
        return False, "unknown"
    return state == "bear", state


def run_rebalance(cfg: dict, dry_run: bool = False) -> List[dict]:
    """Run portfolio rebalance based on ISR signals."""
    isr_cfg = get_isr_config(cfg)
    
    if not isr_cfg.get("enabled", True):
        return []
    
    # Load ISR universe and compute signals
    from isr_alpha.loader import load_isr_database
    companies = load_isr_database()
    # Universe is the full ISR database. The photonic/semiconductor whitelist that
    # used to sit here was revoked by Peter on 2026-08-19 ("feel free to let go of
    # the list guardrail"). China-domiciled companies are still excluded — that
    # filter lives in isr_alpha/loader.py and is unrelated to this one.
    ranked = signals.rank_universe(companies)
    
    # Get current equity
    equity = get_account_equity() or 0
    if equity <= 0:
        print("  Could not read equity — skipping rebalance")
        return []
    
    # Build target portfolio
    target_positions = portfolio.build_target_portfolio(
        ranked,
        equity=equity,
        max_positions=isr_cfg.get("max_positions", 15),
        max_sector_pct=isr_cfg.get("max_sector_pct", 0.30),
        max_gross_leverage=isr_cfg.get("max_gross_leverage", 1.5),
    )
    
    # Get current positions, minus anything another engine owns. ISR only
    # manages its own sleeve — see other_engine_tickers().
    current = get_current_positions_usd()
    _foreign = other_engine_tickers()
    if _foreign:
        _skip = sorted(set(current) & _foreign)
        if _skip:
            print(f"  \u270b Not ISR's book, leaving alone: {', '.join(_skip)}")
        # Drop from BOTH sides. Removing only from `current` would make a
        # foreign-owned ticker look unheld and ISR would buy a second lot.
        current = {k: v for k, v in current.items() if k not in _foreign}
        target_positions = [p for p in target_positions
                            if p.ticker.upper() not in _foreign]
    
    # PROTECTION: Don't sell moat compounders (moat_score >= 7) unless stop loss hit
    protected = {p.ticker for p in target_positions if p.moat_score >= 7.0}
    protected.update({p.ticker for p in target_positions if p.engine == "compounder" and p.moat_score >= 6.0})
    
    # Check stops on protected positions
    for ticker in list(protected):
        pos = next((p for p in get_positions() if p["symbol"].upper() == ticker), None)
        if pos:
            entry = float(pos.get("avg_entry_price") or 0)
            cur = float(pos.get("current_price") or 0)
            if entry > 0 and cur > 0 and (cur - entry) / entry <= -0.08:  # 8% stop
                protected.discard(ticker)
                print(f"  ⚠️  Stop hit on protected {ticker}: {(cur/entry-1)*100:.1f}%")
    
    # Rank hysteresis — enter on top_n, exit only when a name falls out of
    # exit_rank. Without it a holding that slips from rank 20 to 21 is sold in
    # full and often bought back minutes later. Same pattern the swing
    # backtester documents (backtest_swing.py:12).
    _max_pos = isr_cfg.get("max_positions", 15)
    _exit_rank = int(isr_cfg.get("exit_rank", _max_pos * 2))
    _still_ok = {c.ticker.upper() for c in ranked[:_exit_rank]}
    _tgt_syms = {p.ticker.upper() for p in target_positions}
    for _sym, _val in current.items():
        if _sym not in _tgt_syms and _sym in _still_ok and _val > 0:
            # Hold at its current size: target == current means no order.
            target_positions = target_positions + [
                portfolio.Position(ticker=_sym, company_name=_sym, target_usd=_val,
                                   signal_score=0.0, catalyst_density=0.0,
                                   moat_score=0.0, sub_sector="", tier="",
                                   engine="hold")]
            print(f"  \u23f8  Holding {_sym} — still inside top {_exit_rank}")

    # Generate rebalance orders
    orders = portfolio.rebalance_portfolio(
        current,
        target_positions,
        equity=equity,
        max_turnover_pct=isr_cfg.get("max_turnover_pct", 0.50),
        min_order_usd=isr_cfg.get("min_order_usd", 100.0),
        min_drift_pct=isr_cfg.get("min_drift_pct", 0.25),
    )
    
    # REGIME: in a confirmed downtrend, open nothing new. Adds to existing
    # holdings and every sell still go through — this brakes entry, not exit.
    _blocked, _regime = _regime_blocks_entries(isr_cfg)
    if _blocked:
        _dropped = [t_ for t_, o in orders.items()
                    if o["side"] == "buy" and current.get(t_, 0) <= 0]
        for t_ in _dropped:
            orders.pop(t_, None)
        print(f"  \U0001f6d1 regime {_regime.upper()} — blocked {len(_dropped)} new entries"
              + (f": {', '.join(sorted(_dropped))}" if _dropped else ""))

    # FILTER: Remove sell orders for protected positions
    filtered_orders = {}
    for ticker, order in orders.items():
        if order["side"] == "sell" and ticker in protected:
            print(f"  🛡️  PROTECTED: Skipping sell of {ticker} (moat compounder)")
            continue
        filtered_orders[ticker] = order
    
    # ENFORCE catalyst position cap (max 6)
    catalyst_count = sum(1 for p in target_positions if p.engine == "catalyst")
    if catalyst_count > 6:
        # Remove lowest-conviction catalyst buys
        catalyst_buys = [(t, o) for t, o in filtered_orders.items() 
                        if o["side"] == "buy" and any(p.ticker == t and p.engine == "catalyst" for p in target_positions)]
        catalyst_buys.sort(key=lambda x: x[1].get("signal_score", 0))
        to_remove = catalyst_count - 6
        for ticker, _ in catalyst_buys[:to_remove]:
            print(f"  🚫  CAP: Blocking catalyst buy {ticker} (limit 6)")
            del filtered_orders[ticker]
    
    # MINIMUM CONVICTION for new entries
    min_conviction = isr_cfg.get("min_composite_for_entry", 0.45)
    for ticker, order in list(filtered_orders.items()):
        if order["side"] == "buy" and order.get("signal_score", 0) < min_conviction:
            print(f"  🚫  CONVICTION: Blocking buy {ticker} (score {order.get('signal_score', 0):.3f} < {min_conviction})")
            del filtered_orders[ticker]
    
    # Run risk checks
    warnings = portfolio.risk_checks(target_positions, equity)
    for w in warnings:
        print(f"  ⚠️  RISK: {w}")
    
    # Execute orders
    executed = []
    for ticker, order in filtered_orders.items():
        side = order["side"]
        notional = order["notional"]
        
        print(f"  {'[DRY] ' if dry_run else ''}{side.upper()} {ticker} \${notional:,.0f} ({order['reason']})")
        
        if not dry_run:
            try:
                if side == "buy":
                    res = place_market_order(ticker, "buy", notional=notional)
                else:
                    pos = next((p for p in get_positions() if p["symbol"].upper() == ticker), None)
                    if pos:
                        qty = abs(float(pos.get("qty", 0)))
                        res = place_market_order(ticker, "sell", qty=qty)
                    else:
                        print(f"    No position to sell for {ticker}")
                        continue
                
                if res.get("id"):
                    executed.append({
                        "ticker": ticker,
                        "side": side,
                        "notional": notional,
                        "order_id": res["id"],
                        "engine": order.get("engine", "unknown"),
                    })
                else:
                    print(f"    Order failed: {res}")
            except Exception as e:
                print(f"    Order error for {ticker}: {e}")
    
    return executed


def check_catalyst_triggers(cfg: dict, dry_run: bool = False) -> List[dict]:
    """Check for catalyst events that require immediate action."""
    isr_cfg = get_isr_config(cfg)
    
    from isr_alpha.loader import load_isr_database
    companies = load_isr_database()
    ranked = signals.rank_universe(companies)
    
    # Find high-catalyst-density names we don't hold
    current = get_current_positions_usd()
    held = set(current.keys())
    
    catalyst_threshold = isr_cfg.get("catalyst_threshold", 0.6)
    triggers = []
    
    for c in ranked:
        if c.catalyst_density >= catalyst_threshold and c.ticker not in held:
            # Check if catalyst is imminent (within ~5 days)
            # This is a simplified check - in production, parse actual dates
            triggers.append({
                "ticker": c.ticker,
                "catalyst_density": c.catalyst_density,
                "signal_score": c.composite,
                "action": "consider_entry",
            })
    
    return triggers


def run_tick(cfg: dict, dry_run: bool = False) -> None:
    """Main tick function called by trading_scheduler."""
    isr_cfg = get_isr_config(cfg)
    
    if not isr_cfg.get("enabled", True):
        print("  ISR Alpha disabled in config — exiting.")
        return
    
    state = load_state()
    now = datetime.now(timezone.utc)
    
    # Check if market is open
    clk = get_clock()
    if not clk.get("is_open"):
        print("  Market closed — no-op.")
        return
    
    # Rebalance check
    rebalance_every = isr_cfg.get("rebalance_every_minutes", 15)
    last_rebal = state.get("last_rebalance")
    due_rebalance = True
    if last_rebal:
        try:
            last_dt = datetime.fromisoformat(last_rebal.replace('Z', '+00:00'))
            due_rebalance = (now - last_dt).total_seconds() >= rebalance_every * 60 - 30
        except Exception:
            pass
    
    if due_rebalance:
        print(f"  ISR Alpha rebalance tick")
        executed = run_rebalance(cfg, dry_run=dry_run)
        if executed:
            state["last_rebalance"] = now.isoformat()
            state["positions"] = {e["ticker"]: e for e in executed}
            save_state(state)
    
    # Catalyst trigger check (more frequent)
    catalyst_every = isr_cfg.get("catalyst_check_every_minutes", 5)
    last_catalyst = state.get("last_catalyst_check")
    due_catalyst = True
    if last_catalyst:
        try:
            last_dt = datetime.fromisoformat(last_catalyst.replace('Z', '+00:00'))
            due_catalyst = (now - last_dt).total_seconds() >= catalyst_every * 60 - 30
        except Exception:
            pass
    
    if due_catalyst:
        triggers = check_catalyst_triggers(cfg, dry_run)
        if triggers:
            print(f"  Catalyst triggers: {len(triggers)} names flagged")
            for t in triggers[:5]:
                print(f"    {t['ticker']}: cat={t['catalyst_density']:.2f} score={t['signal_score']:.3f}")
        state["last_catalyst_check"] = now.isoformat()
        save_state(state)


# CLI entry point
def main():
    import argparse
    import urllib3
    urllib3.disable_warnings()
    
    ap = argparse.ArgumentParser(description="ISR Alpha executor")
    ap.add_argument("--dry-run", action="store_true", help="Run logic but place NO orders")
    ap.add_argument("--once", action="store_true", help="Run a single tick")
    args = ap.parse_args()
    
    cfg = load_merged_config()
    
    if args.once or True:  # Default behavior is single tick
        run_tick(cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())