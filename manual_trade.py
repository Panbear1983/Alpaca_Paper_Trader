#!/usr/bin/env python3
"""
manual_trade.py — the only way Peter's own direct orders (typed by hand, or relayed
verbatim by Wanna Buffet from a Telegram "TRADE ..." message) reach the High Risk
wallet.

  python3 manual_trade.py buy  SYM AMOUNT_USD
  python3 manual_trade.py sell SYM [--frac 0.5]
  python3 manual_trade.py status

buy    sends a dollar-amount order through capitol_copier.place_market_order — the same
       function the trading dashboard's manual buy uses — so the per-name cap, the gross
       cap and the anchor fence (entry_gate.py: approved list, entry window, daily-loss
       halt) all apply exactly as they do everywhere else on this wallet. A refusal
       prints the fence's own plain-English reason and the exit code is 2.
       Whole-share-only assets (no fractional trading) are converted from the dollar
       amount to whole shares at the live price, same as the dashboard does.
sell   closes that fraction of the live position (default: all of it).
status what the fence says right now, plus every open position with today's move.

The order is tagged source "manual_trade" (capitol_copier.SOURCE_BY_MODULE), so it lands
in diary/manual_trades.jsonl and manual_trade_tracker.py's summary alongside buys placed
from the dashboard — one record of Peter's own trading pattern, regardless of which
screen he used.

Plain text output, no markdown — read by a human or relayed verbatim by an agent.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import capitol_copier as cc  # noqa: E402
import entry_gate as eg  # noqa: E402


def fetch_asset_info(sym: str) -> dict | None:
    try:
        r = requests.get(f"{cc.BASE_URL}/assets/{sym}", headers=cc.ALPACA_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def latest_price(sym: str) -> float:
    try:
        r = requests.get(f"{cc.DATA_URL}/stocks/{sym}/trades/latest",
                         headers=cc.ALPACA_HEADERS, timeout=10)
        if r.status_code == 200:
            return float((r.json().get("trade") or {}).get("p") or 0)
    except Exception:
        pass
    return 0.0


def cmd_buy(sym: str, amount: float) -> int:
    sym = sym.upper()
    if amount <= 0:
        print(f"ERROR: amount must be a positive number of dollars, got {amount!r}.")
        return 3
    asset = fetch_asset_info(sym)
    if asset is None or not asset.get("tradable"):
        print(f"ERROR: {sym} is not a known tradable symbol on this account.")
        return 3

    if asset.get("fractionable"):
        res = cc.place_market_order(sym, "buy", notional=amount)
    else:
        price = latest_price(sym)
        if price <= 0:
            print(f"ERROR: no live price for {sym}; not trading.")
            return 3
        qty = int(amount // price)
        if qty < 1:
            print(f"BLOCKED: ${amount:,.0f} doesn't cover one whole share of {sym} "
                  f"(price ${price:,.2f}) — it's a whole-share-only asset.")
            return 2
        res = cc.place_market_order(sym, "buy", qty=qty)

    if res.get("blocked_by_cap"):
        print(f"BLOCKED: {res.get('reason')}")
        return 2
    oid = res.get("id")
    if not oid:
        print(f"ERROR: order not accepted: {res}")
        return 3
    if asset.get("fractionable"):
        print(f"BOUGHT ${amount:,.0f} of {sym}; order {oid} sent.")
    else:
        print(f"BOUGHT {qty} share(s) of {sym} (~${qty*price:,.0f}); order {oid} sent.")
    return 0


def cmd_sell(sym: str, frac: float) -> int:
    sym = sym.upper()
    pos = None
    for p in cc.get_positions():
        if p.get("symbol", "").upper() == sym:
            pos = p
    if not pos:
        print(f"nothing to sell: no open position in {sym}.")
        return 2
    avail = abs(float(pos.get("qty_available", pos.get("qty", 0)) or 0))
    qty = round(avail * max(0.0, min(1.0, frac)), 4)
    if qty <= 0:
        print(f"nothing to sell: {sym} has no available shares (open order pending?).")
        return 2
    res = cc.place_market_order(sym, "sell", qty=qty)
    if not res.get("id"):
        print(f"ERROR: sell not accepted: {res}")
        return 3
    print(f"SOLD {sym} {qty:g} shares ({frac*100:.0f}% of the position); order {res['id']} sent.")
    return 0


def cmd_status() -> int:
    st = eg.status()
    print(f"{st['et_time']}  approved list: {', '.join(st['universe']) or '(none — fence off)'}")
    print(f"entry window {st['window'][0]}-{st['window'][1]} ET: "
          f"{'OPEN' if st['in_window'] else 'closed'}   "
          f"market: {'open' if st['market_open'] else 'closed' if st['market_open'] is not None else '?'}")
    equity = cc.get_account_equity()
    if equity:
        print(f"equity: ${equity:,.0f}")
    positions = cc.get_positions()
    if not positions:
        print("positions: none")
        return 0
    print("positions:")
    for p in sorted(positions, key=lambda x: -abs(float(x.get("unrealized_pl") or 0))):
        sym = p.get("symbol")
        mv = float(p.get("market_value") or 0)
        pl = float(p.get("unrealized_pl") or 0)
        day_pl = float(p.get("unrealized_intraday_pl") or 0)
        print(f"  {sym}: ${mv:,.0f}  total {pl:+,.0f}  today {day_pl:+,.0f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("buy")
    b.add_argument("symbol")
    b.add_argument("amount", type=float)
    s = sub.add_parser("sell")
    s.add_argument("symbol")
    s.add_argument("--frac", type=float, default=1.0)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "buy":
        return cmd_buy(a.symbol, a.amount)
    if a.cmd == "sell":
        return cmd_sell(a.symbol, a.frac)
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
