"""
Intraday Momentum Day-Trader — 4x, flat-to-cash daily
======================================================
A leveraged intraday strategy that REPLACES Capitol Copier on the account.
It ranks a liquid large-cap universe by relative strength vs SPY, holds the top
N, rotates as ranks change, and — critically — liquidates the ENTIRE account to
cash before each market close so the 4x intraday buying power never becomes an
overnight (Reg-T 2x) margin call.

Signal:
  rs = (price - today_open)/today_open  -  (SPY_price - SPY_open)/SPY_open
  Long the top `top_n`; per-name notional = equity * gross_mult / top_n.

Risk:
  - Per-trade hard stop at -stop_pct from entry (checked each tick).
  - NO daily kill-switch (by design).
  - MANDATORY end-of-day flatten — this is what makes 4x safe, not optional.

The account is intraday-only (Capitol Copier paused), so "flatten" = close ALL
positions. No position tagging needed.

Modes:
  python3 intraday_momentum.py               one scheduler tick (gated on market clock)
  python3 intraday_momentum.py --dry-run     tick logic, but place NO orders
  python3 intraday_momentum.py --rank-only    print RS ranking from latest data, no trading, ignores clock
  python3 intraday_momentum.py --once        alias for a single manual tick
  python3 intraday_momentum.py --flatten-now  force a full flatten right now (panic button)

Env reads from .env in this folder: ALPACA_API_KEY/SECRET/BASE_URL, TELEGRAM_*.
"""

import os, json, argparse
import datetime as dt
import requests

# Reuse the Alpaca plumbing already proven in capitol_copier.
from capitol_copier import (
    BASE_URL, DATA_URL, ALPACA_HEADERS,
    place_market_order, get_positions, get_account_equity,
)

try:
    import telegram_notifier as tg
except ImportError:
    tg = None

CONFIG_FILE     = os.path.join(os.path.dirname(__file__), "strategy_config.json")
STATE_FILE      = os.path.join(os.path.dirname(__file__), ".intraday_state.json")


# ── Config / state ──────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"entries": {}, "done_date": None, "last_rank": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Alpaca helpers (intraday-specific) ──────────────────────────────────────

def get_clock():
    r = requests.get(f"{BASE_URL}/clock", headers=ALPACA_HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_snapshots(symbols):
    """Return {sym: snapshot} for a list of symbols from the data API."""
    if not symbols:
        return {}
    r = requests.get(f"{DATA_URL}/stocks/snapshots", headers=ALPACA_HEADERS,
                     params={"symbols": ",".join(symbols)}, timeout=15)
    r.raise_for_status()
    return r.json() or {}


def cancel_all_orders():
    r = requests.delete(f"{BASE_URL}/orders", headers=ALPACA_HEADERS, timeout=15)
    return r.status_code in (200, 207)


def close_all_positions():
    """Liquidate every open position (account is intraday-only)."""
    r = requests.delete(f"{BASE_URL}/positions", headers=ALPACA_HEADERS,
                        params={"cancel_orders": "true"}, timeout=30)
    return r.status_code in (200, 207)


# ── Signal ──────────────────────────────────────────────────────────────────

def _intraday_return(snap):
    """(last - today_open)/today_open from a snapshot, or None if unavailable."""
    daily = snap.get("dailyBar") or {}
    trade = snap.get("latestTrade") or {}
    o = daily.get("o")
    p = trade.get("p")
    if not o or not p:
        return None, p
    return (p - o) / o, p


def rank_universe(cfg):
    """Return a list of (sym, rs, price) sorted by relative strength desc."""
    universe = cfg["intraday"]["universe"]
    snaps = get_snapshots(universe + ["SPY"])

    spy_ret, _ = _intraday_return(snaps.get("SPY", {}))
    spy_ret = spy_ret or 0.0

    ranked = []
    for sym in universe:
        ret, px = _intraday_return(snaps.get(sym, {}))
        if ret is None or not px:
            continue
        ranked.append((sym, ret - spy_ret, px))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked, spy_ret


# ── Core tick ───────────────────────────────────────────────────────────────

def _mins_to_close(clk):
    """Minutes from now until next_close, using the clock's own ET timestamps."""
    nxt = dt.datetime.fromisoformat(clk["next_close"])
    now = dt.datetime.fromisoformat(clk["timestamp"])
    return (nxt - now).total_seconds() / 60.0


def _today_str():
    return dt.date.today().isoformat()


def flatten(state, dry_run=False, reason="EOD flatten"):
    held = get_positions()
    syms = [p["symbol"] for p in held]
    print(f"  {'[DRY] ' if dry_run else ''}{reason}: closing {len(syms)} positions {syms}")
    if not dry_run:
        cancel_all_orders()
        close_all_positions()
        state["entries"] = {}
        state["done_date"] = _today_str()
        if tg:
            tg.send(f"🌙 *Intraday {reason}* — flat to cash ({len(syms)} closed)")
    return len(syms)


def run_tick(cfg, dry_run=False):
    if not cfg.get("intraday", {}).get("enabled", False):
        print("  intraday disabled in config — exiting.")
        return

    state = load_state()
    ic    = cfg["intraday"]
    top_n     = ic["top_n"]
    gross     = ic["gross_mult"]
    stop_pct  = ic["stop_pct"]
    flat_mins = ic["flatten_minutes"]

    clk = get_clock()
    now_s = clk["timestamp"][:19]

    if not clk.get("is_open"):
        print(f"[{now_s}] market CLOSED — no-op.")
        return

    mins = _mins_to_close(clk)
    print(f"[{now_s}] market OPEN — {mins:.0f} min to close")

    # ── EOD flatten gate (mandatory) ─────────────────────────────────────────
    if mins <= flat_mins:
        if state.get("done_date") == _today_str() and not dry_run:
            print("  already flattened for today — no-op.")
            return
        flatten(state, dry_run=dry_run)
        if not dry_run:
            save_state(state)
        return

    # fresh trading day — clear yesterday's done flag
    if state.get("done_date") and state["done_date"] != _today_str():
        state["done_date"] = None

    equity = get_account_equity() or 0
    if equity <= 0:
        print("  could not read equity — skipping tick.")
        return
    per_name = round(equity * gross / top_n, 2)

    ranked, spy_ret = rank_universe(cfg)
    if not ranked:
        print("  no ranked names (data unavailable) — skipping.")
        return
    desired = {sym for sym, _, _ in ranked[:top_n]}

    held = {p["symbol"]: p for p in get_positions()}
    entries = state.setdefault("entries", {})
    tag = "[DRY] " if dry_run else ""

    print(f"  SPY intraday {spy_ret*100:+.2f}%  |  target ${per_name:,.0f}/name × {top_n} "
          f"= {gross:.0f}x ${equity*gross:,.0f}")
    print(f"  Top {top_n}: " + ", ".join(f"{s}({rs*100:+.1f}%)" for s, rs, _ in ranked[:top_n]))

    # ── 1. Per-trade stops (override desired — don't rebuy a stopped name) ────
    for sym, p in list(held.items()):
        entry = float(entries.get(sym) or p.get("avg_entry_price") or 0)
        cur   = float(p.get("current_price") or 0)
        if entry > 0 and cur > 0 and (cur - entry) / entry <= -stop_pct:
            print(f"  {tag}🛑 STOP {sym}  {(cur/entry-1)*100:+.1f}% <= -{stop_pct*100:.0f}%  → sell")
            if not dry_run:
                place_market_order(sym, "sell", qty=abs(float(p.get("qty", 0))))
                entries.pop(sym, None)
                if tg:
                    tg.notify_stop_hit(sym, round(cur, 2), abs(float(p.get("qty", 0))))
            held.pop(sym, None)
            desired.discard(sym)

    # ── 2. Sell names that dropped out of the desired set ────────────────────
    for sym, p in list(held.items()):
        if sym not in desired:
            print(f"  {tag}↓ ROTATE-OUT {sym}  → sell")
            if not dry_run:
                place_market_order(sym, "sell", qty=abs(float(p.get("qty", 0))))
                entries.pop(sym, None)
            held.pop(sym, None)

    # ── 3. Buy desired names we don't yet hold ───────────────────────────────
    price_by_sym = {s: px for s, _, px in ranked}
    for sym in desired:
        if sym in held:
            continue
        print(f"  {tag}↑ BUY {sym}  ${per_name:,.0f}")
        if not dry_run:
            res = place_market_order(sym, "buy", notional=per_name)
            if res.get("id"):
                entries[sym] = price_by_sym.get(sym)
                if tg:
                    tg.send(f"🟢 *Intraday BUY* `{sym}` ${per_name:,.0f}")

    state["last_rank"] = [s for s, _, _ in ranked[:top_n]]
    if not dry_run:
        save_state(state)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings()

    ap = argparse.ArgumentParser(description="Intraday momentum day-trader (4x, flat daily)")
    ap.add_argument("--dry-run", action="store_true", help="Run tick logic but place NO orders")
    ap.add_argument("--rank-only", action="store_true",
                    help="Print RS ranking from latest data and exit (ignores market clock)")
    ap.add_argument("--once", action="store_true", help="Run a single tick (same as default)")
    ap.add_argument("--flatten-now", action="store_true", help="Force a full flatten immediately")
    args = ap.parse_args()

    cfg = load_config()

    if args.rank_only:
        ranked, spy_ret = rank_universe(cfg)
        top_n = cfg["intraday"]["top_n"]
        print(f"SPY intraday {spy_ret*100:+.2f}%   (rank by relative strength)")
        print(f"{'#':>3} {'SYM':<6} {'RS vs SPY':>10} {'PRICE':>10}")
        for i, (sym, rs, px) in enumerate(ranked, 1):
            marker = " ◀ LONG" if i <= top_n else ""
            print(f"{i:>3} {sym:<6} {rs*100:>+9.2f}% ${px:>8.2f}{marker}")
        return 0

    if args.flatten_now:
        state = load_state()
        flatten(state, dry_run=args.dry_run, reason="manual flatten")
        if not args.dry_run:
            save_state(state)
        return 0

    run_tick(cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
