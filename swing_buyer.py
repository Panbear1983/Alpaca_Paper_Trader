"""
Swing Buyer — smart daily BUY engine (the missing half of the strategy)
========================================================================
The exit engine (capitol_copier.manage_open_positions) sells five ways but the
account had only one slow buy signal (politician disclosures). This module adds
a systematic, price-driven buy side that runs once per trading day:

  1. REGIME FILTER  — no new buying while SPY < its 50-day SMA (risk-off).
  2. DIP-ADDS       — proven winners (peak gain >= dip_min_peak_gain) that have
                      pulled back >= dip_trigger_off_peak from their peak but
                      remain above entry get an add. Buys weakness in strength.
  3. NEW ENTRIES    — rank the liquid universe by 20-day relative strength vs
                      SPY; buy the top names not already held (RS must be
                      positive). Buys strength itself.
  4. CASH DISCIPLINE— budget = min(cash - reserve, exposure headroom); never
                      breach the per-name cap, the holdings cap, or rebuy a
                      name the exit engine stopped out of within the cooldown.

Cadence: designed for once daily during market hours (trading_scheduler.py).
Disclosure copying (capitol_copier) and exits (manage_open_positions) run on
their own schedules — this module ONLY buys.

Modes:
  python3 swing_buyer.py               live run (requires swing.enabled=true)
  python3 swing_buyer.py --dry-run     full logic, no orders
  python3 swing_buyer.py --rank        print the RS ranking and regime, exit
  python3 swing_buyer.py --force       run even if swing.enabled=false (manual)
"""

import os, json, argparse
import datetime as dt
import requests

from capitol_copier import (
    BASE_URL, DATA_URL, ALPACA_HEADERS,
    place_market_order, get_positions, get_account_equity,
    load_config, load_pos_state,
)
from intraday_momentum import get_snapshots, get_clock

try:
    import telegram_notifier as tg
except ImportError:
    tg = None

import strategies


def _state_file() -> str:              # per-wallet cooldown/dip state
    return strategies.state_path(".swing_state.json")


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(_state_file()):
        with open(_state_file()) as f:
            return json.load(f)
    return {"last_run_date": None, "dip_adds": {}, "entries": {}}


def save_state(state):
    with open(_state_file(), "w") as f:
        json.dump(state, f, indent=2)


# ── Market data ──────────────────────────────────────────────────────────────

def fetch_daily_bars(symbols, days=120):
    """Multi-symbol daily bars (iex feed), {sym: [bar, ...]} oldest→newest."""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out, page_token = {}, None
    while True:
        params = {
            "symbols":    ",".join(symbols),
            "timeframe":  "1Day",
            "start":      start,
            "adjustment": "split",
            "feed":       "iex",
            "limit":      10000,
        }
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{DATA_URL}/stocks/bars", headers=ALPACA_HEADERS,
                         params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for sym, bars in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bars)
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return out


def spy_regime(bars_by_sym, sma_days):
    """(risk_on: bool, spy_close, sma) — SPY close vs its N-day SMA."""
    spy = bars_by_sym.get("SPY") or []
    closes = [b["c"] for b in spy]
    if len(closes) < sma_days:
        return True, closes[-1] if closes else None, None   # not enough data → don't block
    sma = sum(closes[-sma_days:]) / sma_days
    return closes[-1] >= sma, closes[-1], sma


def rs_rank(bars_by_sym, universe, lookback):
    """[(sym, rs)] sorted desc — trailing `lookback`-bar return minus SPY's."""
    def ret(sym):
        closes = [b["c"] for b in (bars_by_sym.get(sym) or [])]
        if len(closes) < lookback + 1:
            return None
        return (closes[-1] - closes[-lookback - 1]) / closes[-lookback - 1]

    spy_ret = ret("SPY") or 0.0
    ranked = []
    for sym in universe:
        r = ret(sym)
        if r is not None:
            ranked.append((sym, r - spy_ret))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked, spy_ret


# ── Core run ─────────────────────────────────────────────────────────────────

def run(dry_run=False, force=False):
    import urllib3
    urllib3.disable_warnings()

    cfg = load_config()
    sw  = cfg.get("swing", {})
    if not sw.get("enabled", False) and not (dry_run or force):
        print("  swing buyer disabled in config (swing.enabled=false) — exiting.")
        return

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tag = "[DRY] " if dry_run else ""
    today = dt.date.today().isoformat()
    print(f"[{now}] Swing Buyer{' (DRY RUN)' if dry_run else ''}")

    state  = load_state()
    pstate = load_pos_state()

    # ── Account picture ──────────────────────────────────────────────────────
    equity    = get_account_equity() or 0
    positions = get_positions()
    held      = {p["symbol"]: p for p in positions}
    exposure  = sum(abs(float(p.get("market_value", 0))) for p in positions)
    r = requests.get(f"{BASE_URL}/account", headers=ALPACA_HEADERS, timeout=15)
    cash = float(r.json().get("cash", 0)) if r.status_code == 200 else 0.0

    reserve   = sw.get("min_cash_reserve_usd", 2000)
    max_exp   = equity * cfg["pool"]["max_total_exposure_pct"]
    headroom  = max(0.0, max_exp - exposure)
    budget    = max(0.0, min(cash - reserve, headroom))
    max_pos   = cfg["pool"]["max_position_usd"]
    max_hold  = sw.get("max_holdings", 20)

    print(f"  equity ${equity:,.0f} · cash ${cash:,.0f} (reserve ${reserve:,.0f}) · "
          f"exposure ${exposure:,.0f}/{max_exp:,.0f} → budget ${budget:,.0f}")

    # ── Data: universe = intraday universe ∪ current holdings ────────────────
    universe = list(dict.fromkeys(cfg["intraday"]["universe"] + list(held.keys())))
    universe = [s for s in universe if s != "TSLA"]          # permanently retired
    bars = fetch_daily_bars(universe + ["SPY"], days=120)

    # ── 1. Regime filter ─────────────────────────────────────────────────────
    risk_on, spy_c, sma = spy_regime(bars, sw.get("regime_sma_days", 50))
    sma_s = f"{sma:,.2f}" if sma else "?"
    print(f"  regime: SPY {spy_c:,.2f} vs {sw.get('regime_sma_days',50)}d SMA {sma_s} "
          f"→ {'RISK-ON' if risk_on else 'RISK-OFF (no new buys)'}")
    if not risk_on:
        state["last_run_date"] = today
        if not dry_run:
            save_state(state)
        return

    if budget < 500:
        print("  budget < $500 — nothing to deploy this run.")
        state["last_run_date"] = today
        if not dry_run:
            save_state(state)
        return

    ranked, spy20 = rs_rank(bars, universe, sw.get("rs_lookback_days", 20))
    rs_by_sym = dict(ranked)
    snaps = get_snapshots(list(held.keys()))     # live prices for dip detection
    acts, spent = [], 0.0

    stopped   = pstate.get("_stopped", {})
    cooldown  = sw.get("stop_cooldown_days", 5)
    def in_stop_cooldown(sym):
        d = stopped.get(sym)
        if not d:
            return False
        return (dt.date.today() - dt.date.fromisoformat(d)).days < cooldown

    # ── 2. Dip-adds on proven winners ────────────────────────────────────────
    dip_min_peak = sw.get("dip_min_peak_gain", 0.08)
    dip_trigger  = sw.get("dip_trigger_off_peak", 0.04)
    dip_usd      = sw.get("dip_add_usd", 1500)
    dip_cd_days  = sw.get("dip_cooldown_days", 7)

    for sym, p in held.items():
        if spent + dip_usd > budget:
            break
        st = pstate.get(sym) or {}
        entry = float(st.get("entry_price") or p.get("avg_entry_price") or 0)
        peak  = float(st.get("peak_price") or 0)
        # live price from snapshot; fall back to last daily close
        snap  = snaps.get(sym) or {}
        cur   = (snap.get("latestTrade") or {}).get("p") or \
                ((bars.get(sym) or [{}])[-1].get("c"))
        if not (entry and peak and cur):
            continue
        # fall back to 20d-high if the manage engine hasn't tracked a peak yet
        if peak <= entry:
            highs = [b["h"] for b in (bars.get(sym) or [])[-20:]]
            peak = max(highs) if highs else peak
        peak_gain = (peak - entry) / entry
        off_peak  = (peak - cur) / peak if peak else 0.0
        last_add  = state["dip_adds"].get(sym)
        cd_ok = (not last_add or
                 (dt.date.today() - dt.date.fromisoformat(last_add)).days >= dip_cd_days)
        held_mv = abs(float(p.get("market_value", 0)))

        if (peak_gain >= dip_min_peak and off_peak >= dip_trigger and cur > entry
                and cd_ok and held_mv + dip_usd <= max_pos):
            print(f"  {tag}↘︎ DIP-ADD {sym}  peak +{peak_gain*100:.0f}%, "
                  f"{off_peak*100:.1f}% off peak, still +{(cur/entry-1)*100:.1f}% "
                  f"→ add ${dip_usd:,.0f}")
            acts.append(f"🔵 DIP-ADD `{sym}` ${dip_usd:,.0f} ({off_peak*100:.1f}% off peak)")
            if not dry_run:
                res = place_market_order(sym, "buy", notional=dip_usd)
                if res.get("id"):
                    state["dip_adds"][sym] = today
            spent += dip_usd

    # ── 3. New entries: top relative strength, not held ──────────────────────
    entry_usd = sw.get("entry_size_usd", 3000)
    max_new   = sw.get("max_new_positions_per_run", 2)
    n_new     = 0

    print(f"  top RS: " + ", ".join(f"{s}({r*100:+.1f}%)" for s, r in ranked[:8]))
    for sym, rs in ranked:
        if n_new >= max_new or spent + entry_usd > budget:
            break
        if rs <= 0:                      # only names actually beating SPY
            break
        if sym in held or in_stop_cooldown(sym):
            continue
        if len(held) + n_new >= max_hold:
            print(f"  holdings cap ({max_hold}) reached — no more new entries.")
            break
        print(f"  {tag}↑ NEW ENTRY {sym}  RS {rs*100:+.1f}% → buy ${entry_usd:,.0f}")
        acts.append(f"🟢 NEW `{sym}` ${entry_usd:,.0f} (RS {rs*100:+.1f}%)")
        if not dry_run:
            res = place_market_order(sym, "buy", notional=entry_usd)
            if res.get("id"):
                state["entries"][sym] = today
        spent += entry_usd
        n_new += 1

    if not acts:
        print("  no buy triggers this run (dips shallow / RS flat / caps binding).")
    else:
        print(f"  deployed ${spent:,.0f} across {len(acts)} orders.")
    if acts and tg and not dry_run:
        tg.notify_batch("Swing Buyer · daily run", acts, emoji="🛒")

    state["last_run_date"] = today
    if not dry_run:
        save_state(state)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Swing Buyer — smart daily buy engine")
    ap.add_argument("--dry-run", action="store_true", help="full logic, no orders")
    ap.add_argument("--force",   action="store_true",
                    help="run even if swing.enabled=false in config")
    ap.add_argument("--rank",    action="store_true",
                    help="print regime + RS ranking and exit")
    args = ap.parse_args()

    if args.rank:
        import urllib3
        urllib3.disable_warnings()
        cfg = load_config()
        sw  = cfg.get("swing", {})
        held = [p["symbol"] for p in get_positions()]
        universe = list(dict.fromkeys(cfg["intraday"]["universe"] + held))
        universe = [s for s in universe if s != "TSLA"]
        bars = fetch_daily_bars(universe + ["SPY"], days=120)
        risk_on, spy_c, sma = spy_regime(bars, sw.get("regime_sma_days", 50))
        ranked, _ = rs_rank(bars, universe, sw.get("rs_lookback_days", 20))
        sma_s = f"{sma:,.2f}" if sma else "?"
        print(f"SPY {spy_c:,.2f} vs SMA {sma_s} → {'RISK-ON' if risk_on else 'RISK-OFF'}")
        print(f"{'#':>3} {'SYM':<6} {'RS 20d':>9}  held")
        for i, (sym, rs) in enumerate(ranked, 1):
            print(f"{i:>3} {sym:<6} {rs*100:>+8.2f}%  {'◀' if sym in held else ''}")
        return 0

    run(dry_run=args.dry_run, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
