"""
Rebalance to Top-N — keep the best performers, recycle the rest
================================================================
Keeps the top-N holdings (ranked by unrealized P&L %), SELLS every other
position, and redeploys the freed cash into the kept names, weighted by
performance (better performers get a larger share).

This acts on whatever account ALPACA_BASE_URL points at — the shipped value is
the PAPER endpoint. It reuses the Alpaca helpers in capitol_copier.py and places
no orders on import.

SAFETY:
  • Default mode is DRY-RUN: prints the full plan, places nothing.
  • --live actually trades, and STILL requires you to type EXECUTE at the prompt.

Usage:
  python3 rebalance_top_n.py                 # dry-run, keep top 20
  python3 rebalance_top_n.py --top 15        # dry-run, keep top 15
  python3 rebalance_top_n.py --by pl         # rank by P&L $ instead of P&L %
  python3 rebalance_top_n.py --live          # execute (asks for typed confirm)
"""
from __future__ import annotations

import argparse
import sys

import capitol_copier as cc


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# Ranking metric → key into the Alpaca position dict
RANK_KEYS = {
    "plpc": "unrealized_plpc",   # P&L %  (default)
    "pl":   "unrealized_pl",     # P&L $
    "mv":   "market_value",      # position size
}


def rank_positions(positions: list[dict], by: str) -> list[dict]:
    key = RANK_KEYS[by]
    return sorted(positions, key=lambda p: _f(p.get(key)), reverse=True)


def performance_weights(keep: list[dict], floor_frac: float) -> dict[str, float]:
    """Normalised weights over the kept names, larger for better P&L %.

    P&L % can be negative, so we shift by the worst kept performer and add a
    floor (a fraction of the spread) so even the weakest kept name still gets a
    slice instead of zero.
    """
    plpc = {p["symbol"]: _f(p.get("unrealized_plpc")) for p in keep}
    lo, hi = min(plpc.values()), max(plpc.values())
    spread = (hi - lo) or 1.0
    floor = spread * floor_frac
    raw = {s: (v - lo) + floor for s, v in plpc.items()}
    total = sum(raw.values()) or 1.0
    return {s: w / total for s, w in raw.items()}


def available_cash() -> float:
    """Idle (uninvested) cash on the account, for deploy-to-1x."""
    import requests
    try:
        r = requests.get(f"{cc.BASE_URL}/account", headers=cc.ALPACA_HEADERS, timeout=15)
        return float(r.json().get("cash", 0)) if r.status_code == 200 else 0.0
    except Exception:
        return 0.0


def build_plan(positions, top_n, by, floor_frac, deploy_cash=0.0):
    """Sell everything outside the top-N and redeploy the proceeds PLUS any
    `deploy_cash` (idle cash to put to work) into the kept names, weighted by
    performance. deploy_cash=0 reproduces the original recycle-only behavior."""
    longs = [p for p in positions if _f(p.get("qty")) > 0]
    skipped = [p for p in positions if _f(p.get("qty")) <= 0]  # shorts: not handled
    ranked = rank_positions(longs, by)
    keep, sell = ranked[:top_n], ranked[top_n:]
    freed = sum(_f(p.get("market_value")) for p in sell)
    pool = freed + max(0.0, _f(deploy_cash))          # proceeds + idle cash
    weights = performance_weights(keep, floor_frac) if keep else {}
    buys = [
        {"symbol": p["symbol"], "notional": pool * weights[p["symbol"]],
         "plpc": _f(p.get("unrealized_plpc")) * 100}
        for p in keep
    ]
    return keep, sell, buys, freed, skipped


def fmt_pct(x):  # x already a fraction
    return f"{x * 100:+.1f}%"


def print_plan(keep, sell, buys, freed, skipped, by, deploy_cash=0.0):
    print(f"\n{'='*64}\nREBALANCE PLAN  (rank by {by})\n{'='*64}")

    print(f"\nSELL {len(sell)} positions  →  frees ~${freed:,.2f} cash")
    if deploy_cash > 0:
        print(f"DEPLOY idle cash  →  +${deploy_cash:,.2f}  "
              f"(total to invest: ${freed + deploy_cash:,.2f})")
    print(f"  {'SYM':<6} {'QTY':>10} {'MKT VAL':>12} {'P&L %':>9}")
    for p in sell:
        print(f"  {p['symbol']:<6} {_f(p.get('qty')):>10g} "
              f"{_f(p.get('market_value')):>12,.2f} "
              f"{fmt_pct(_f(p.get('unrealized_plpc'))):>9}")

    print(f"\nKEEP + ADD to {len(keep)} positions  (weighted by P&L %)")
    print(f"  {'SYM':<6} {'P&L %':>9} {'ADD $':>12}")
    for b in buys:
        print(f"  {b['symbol']:<6} {b['plpc']:>+8.1f}% {b['notional']:>12,.2f}")
    print(f"  {'':6} {'TOTAL':>9} {sum(b['notional'] for b in buys):>12,.2f}")

    if skipped:
        syms = ", ".join(p["symbol"] for p in skipped)
        print(f"\n⚠ SKIPPED {len(skipped)} non-long position(s): {syms}")
        print("  (short positions are not handled — close them manually if needed)")


def execute(sell, buys, log=print):
    """Place the sells then the buys. `log` lets callers (e.g. the TUI) capture
    output instead of printing to stdout."""
    log("Executing SELLs…")
    for p in sell:
        qty = abs(_f(p.get("qty")))
        res = cc.place_market_order(p["symbol"], "sell", qty=qty)
        oid = res.get("id") or res.get("message") or res
        log(f"  SELL {p['symbol']:<6} qty {qty:g}  →  {str(oid)[:18]}")

    log("Executing BUYs…")
    for b in buys:
        if b["notional"] < 1:
            log(f"  skip {b['symbol']} (allocation < $1)")
            continue
        res = cc.place_market_order(b["symbol"], "buy", notional=b["notional"])
        oid = res.get("id") or res.get("message") or res
        log(f"  BUY  {b['symbol']:<6} ${b['notional']:>9,.2f}  →  {str(oid)[:18]}")
    log("Done. Positions will refresh shortly.")


def main():
    ap = argparse.ArgumentParser(description="Rebalance to top-N performers.")
    ap.add_argument("--top", type=int, default=20, help="how many to keep (default 20)")
    ap.add_argument("--by", choices=list(RANK_KEYS), default="plpc",
                    help="ranking metric: plpc=P&L%% (default), pl=P&L$, mv=size")
    ap.add_argument("--floor", type=float, default=0.10,
                    help="min weight floor as fraction of P&L spread (default 0.10)")
    ap.add_argument("--deploy-cash", type=float, default=0.0,
                    help="also deploy this much idle cash into the kept names")
    ap.add_argument("--deploy-all", action="store_true",
                    help="deploy ALL available idle cash (to ~1x, no leverage)")
    ap.add_argument("--live", action="store_true",
                    help="actually place orders (otherwise dry-run)")
    args = ap.parse_args()

    positions = cc.get_positions()
    if not positions:
        print("No open positions (or fetch failed). Check ALPACA creds / endpoint.")
        sys.exit(1)

    # Resolve idle cash to deploy, capped at what's actually available (1x — no
    # leverage; borrowing beyond cash is a separate, gated mode).
    deploy = available_cash() if args.deploy_all else max(0.0, args.deploy_cash)
    if deploy > 0:
        avail = available_cash()
        if deploy > avail:
            print(f"⚠ requested ${deploy:,.2f} > available cash ${avail:,.2f} — "
                  f"capping at cash (no leverage). Use the leveraged mode for >1x.")
            deploy = avail

    n_long = len([p for p in positions if _f(p.get("qty")) > 0])
    if n_long <= args.top and deploy <= 0:
        print(f"Only {n_long} long positions — nothing to trim at top={args.top} "
              f"(and no cash to deploy).")
        sys.exit(0)

    keep, sell, buys, freed, skipped = build_plan(
        positions, args.top, args.by, args.floor, deploy_cash=deploy)
    print_plan(keep, sell, buys, freed, skipped, args.by, deploy_cash=deploy)

    if not args.live:
        print("\n[DRY-RUN] No orders placed. Re-run with --live to execute.")
        return

    print(f"\n⚠ LIVE MODE — sell {len(sell)}, buy into {len(buys)} "
          f"(${freed + deploy:,.0f} total) on {cc.BASE_URL}")
    if input("Type EXECUTE to proceed: ").strip() != "EXECUTE":
        print("Aborted — nothing placed.")
        return
    execute(sell, buys)


if __name__ == "__main__":
    main()
