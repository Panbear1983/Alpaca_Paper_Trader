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


def _trim_plan(keep, raise_amt, trim_rule):
    """Notional sells drawn from the KEPT holdings summing to ~raise_amt, capped
    at each holding's market value. rule: prorata | losers | winners."""
    raise_amt = min(_f(raise_amt), sum(_f(p.get("market_value")) for p in keep))
    if raise_amt <= 0 or not keep:
        return []
    mv = {p["symbol"]: _f(p.get("market_value")) for p in keep}
    plpc = {p["symbol"]: _f(p.get("unrealized_plpc")) * 100 for p in keep}
    total = sum(mv.values()) or 1.0
    alloc = {}
    if trim_rule == "prorata":
        for s, v in mv.items():
            alloc[s] = raise_amt * (v / total)
    else:  # losers-first (asc P&L%) or winners-first (desc) — fill until met
        order = sorted(keep, key=lambda p: _f(p.get("unrealized_plpc")),
                       reverse=(trim_rule == "winners"))
        remaining = raise_amt
        for p in order:
            take = min(mv[p["symbol"]], remaining)
            if take > 0:
                alloc[p["symbol"]] = take
                remaining -= take
            if remaining <= 0:
                break
    return [{"symbol": s, "side": "sell", "notional": a, "plpc": plpc[s]}
            for s, a in alloc.items() if a > 0]


def build_plan(positions, top_n, by, floor_frac,
               deploy_cash=0.0, withdraw_cash=0.0, trim_rule="prorata"):
    """Sell everything outside the top-N, then route a single signed pool through
    the kept names:  net = proceeds + deploy_cash - withdraw_cash.
      net >= 0 -> BUY net into the kept names (weighted by performance)
      net <  0 -> TRIM -net from the kept names (by trim_rule)
    deploy=withdraw=0 reproduces the original recycle-only behavior."""
    longs = [p for p in positions if _f(p.get("qty")) > 0]
    skipped = [p for p in positions if _f(p.get("qty")) <= 0]  # shorts: not handled
    ranked = rank_positions(longs, by)
    keep, sell = ranked[:top_n], ranked[top_n:]
    freed = sum(_f(p.get("market_value")) for p in sell)
    net = freed + max(0.0, _f(deploy_cash)) - max(0.0, _f(withdraw_cash))
    if net >= 0:
        weights = performance_weights(keep, floor_frac) if keep else {}
        buys = [{"symbol": p["symbol"], "notional": net * weights[p["symbol"]],
                 "plpc": _f(p.get("unrealized_plpc")) * 100} for p in keep]
        trims = []
    else:
        buys = []
        trims = _trim_plan(keep, -net, trim_rule)
    return keep, sell, buys, trims, freed, skipped


def fmt_pct(x):  # x already a fraction
    return f"{x * 100:+.1f}%"


def print_plan(keep, sell, buys, trims, freed, skipped, by,
               deploy_cash=0.0, withdraw_cash=0.0):
    print(f"\n{'='*64}\nREBALANCE PLAN  (rank by {by})\n{'='*64}")

    print(f"\nSELL {len(sell)} positions  →  frees ~${freed:,.2f} cash")
    if deploy_cash > 0:
        print(f"DEPLOY idle cash  →  +${deploy_cash:,.2f}  "
              f"(total to invest: ${freed + deploy_cash:,.2f})")
    if withdraw_cash > 0:
        print(f"WITHDRAW (raise cash) →  -${withdraw_cash:,.2f}")
    print(f"  {'SYM':<6} {'QTY':>10} {'MKT VAL':>12} {'P&L %':>9}")
    for p in sell:
        print(f"  {p['symbol']:<6} {_f(p.get('qty')):>10g} "
              f"{_f(p.get('market_value')):>12,.2f} "
              f"{fmt_pct(_f(p.get('unrealized_plpc'))):>9}")

    if buys:
        print(f"\nKEEP + ADD to {len(keep)} positions  (weighted by P&L %)")
        print(f"  {'SYM':<6} {'P&L %':>9} {'ADD $':>12}")
        for b in buys:
            print(f"  {b['symbol']:<6} {b['plpc']:>+8.1f}% {b['notional']:>12,.2f}")
        print(f"  {'':6} {'TOTAL':>9} {sum(b['notional'] for b in buys):>12,.2f}")
    if trims:
        print(f"\nTRIM {len(trims)} kept positions to raise cash")
        print(f"  {'SYM':<6} {'P&L %':>9} {'SELL $':>12}")
        for t in trims:
            print(f"  {t['symbol']:<6} {t['plpc']:>+8.1f}% {t['notional']:>12,.2f}")
        print(f"  {'':6} {'TOTAL':>9} {sum(t['notional'] for t in trims):>12,.2f}")

    if skipped:
        syms = ", ".join(p["symbol"] for p in skipped)
        print(f"\n⚠ SKIPPED {len(skipped)} non-long position(s): {syms}")
        print("  (short positions are not handled — close them manually if needed)")


def execute(sell, buys, trims=None, log=print):
    """Place full-position sells (bottom-N), then either buys into the kept names
    or partial trims of them (raise-cash). `log` lets callers (e.g. the TUI)
    capture output instead of printing to stdout."""
    log("Executing SELLs…")
    for p in sell:
        qty = abs(_f(p.get("qty")))
        res = cc.place_market_order(p["symbol"], "sell", qty=qty)
        oid = res.get("id") or res.get("message") or res
        log(f"  SELL {p['symbol']:<6} qty {qty:g}  →  {str(oid)[:18]}")

    for t in (trims or []):
        if t["notional"] < 1:
            log(f"  skip {t['symbol']} (trim < $1)")
            continue
        # notional sell = partial share. Non-fractionable names will reject; the
        # response is logged so it's visible rather than silently dropped.
        res = cc.place_market_order(t["symbol"], "sell", notional=t["notional"])
        oid = res.get("id") or res.get("message") or res
        log(f"  TRIM {t['symbol']:<6} -${t['notional']:>9,.2f}  →  {str(oid)[:18]}")

    if buys:
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
    ap.add_argument("--withdraw", type=float, default=0.0,
                    help="raise this much cash by trimming the kept names")
    ap.add_argument("--trim-rule", choices=("prorata", "losers", "winners"),
                    default="prorata", help="how to trim for --withdraw (default prorata)")
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
    withdraw = max(0.0, args.withdraw)
    if deploy > 0 and withdraw > 0:
        print("⚠ pick ONE of --deploy-cash/--deploy-all or --withdraw, not both.")
        sys.exit(1)
    if deploy > 0:
        avail = available_cash()
        if deploy > avail:
            print(f"⚠ requested ${deploy:,.2f} > available cash ${avail:,.2f} — "
                  f"capping at cash (no leverage). Use the leveraged mode for >1x.")
            deploy = avail

    n_long = len([p for p in positions if _f(p.get("qty")) > 0])
    if n_long <= args.top and deploy <= 0 and withdraw <= 0:
        print(f"Only {n_long} long positions — nothing to trim at top={args.top} "
              f"(and no cash to deploy/withdraw).")
        sys.exit(0)

    keep, sell, buys, trims, freed, skipped = build_plan(
        positions, args.top, args.by, args.floor,
        deploy_cash=deploy, withdraw_cash=withdraw, trim_rule=args.trim_rule)
    print_plan(keep, sell, buys, trims, freed, skipped, args.by,
               deploy_cash=deploy, withdraw_cash=withdraw)

    if not args.live:
        print("\n[DRY-RUN] No orders placed. Re-run with --live to execute.")
        return

    action = (f"buy into {len(buys)}" if buys else f"trim {len(trims)}")
    print(f"\n⚠ LIVE MODE — sell {len(sell)}, {action} on {cc.BASE_URL}")
    if input("Type EXECUTE to proceed: ").strip() != "EXECUTE":
        print("Aborted — nothing placed.")
        return
    execute(sell, buys, trims)


if __name__ == "__main__":
    main()
