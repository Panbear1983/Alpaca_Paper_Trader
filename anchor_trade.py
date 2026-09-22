#!/usr/bin/env python3
"""
anchor_trade.py — the only way Wanna Buffet opens or closes an anchor position.

  python3 anchor_trade.py buy  SYM --stop PRICE [--note "why"] [--dry-run]
  python3 anchor_trade.py sell SYM [--frac 1.0]
  python3 anchor_trade.py status

buy   sizes the name to anchor.position_pct of equity (topping up whatever is already held),
      clamps the stop so it is never more than anchor.max_stop_pct below the price, sends the
      order through capitol_copier.place_market_order — where the per-name cap, gross cap and
      entry_gate all apply — and, on success, writes a journal row that price_watcher.py then
      manages (stop, break-even at 1R, partial at 2R, trail).
      If the fence refuses, the reason is printed verbatim and the exit code is 2. Report it
      as a refusal; do not retry and do not place the order another way.
sell  closes that fraction of the live position. The journal row is closed by the watcher
      once the position is gone.
status what the fence says right now, plus every open journal row with its live numbers.

Plain text output, no markdown — it is read by an agent and quoted to Peter.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(_HERE)

import requests  # noqa: E402

import anchor_journal as aj  # noqa: E402
import capitol_copier as cc  # noqa: E402
import entry_gate as eg  # noqa: E402

ET = ZoneInfo("America/New_York")


def acfg() -> dict:
    return eg.load_cfg()


def latest_price(sym: str) -> float:
    r = requests.get(f"{cc.DATA_URL}/stocks/{sym}/trades/latest", headers=cc.ALPACA_HEADERS, timeout=10)
    if r.status_code != 200:
        return 0.0
    return float((r.json().get("trade") or {}).get("p") or 0)


def _now_iso() -> str:
    return dt.datetime.now(ET).isoformat(timespec="seconds")


def cmd_buy(sym: str, stop: float, note: str, dry_run: bool) -> int:
    sym = sym.upper()
    a = acfg()
    price = latest_price(sym)
    if price <= 0:
        print(f"ERROR: no price for {sym}; not trading.")
        return 3

    max_stop_pct = float(a.get("max_stop_pct") or 0.05)
    floor = round(price * (1 - max_stop_pct), 2)
    if stop >= price:
        print(f"ERROR: stop {stop:.2f} is not below the price {price:.2f}; not trading.")
        return 3
    if stop < floor:
        print(f"note: stop {stop:.2f} is more than {max_stop_pct*100:.0f}% below {price:.2f}; "
              f"tightened to {floor:.2f}.")
        stop = floor
    r_per_share = price - stop

    # The fence speaks first, so a refusal names the real rule (universe, window, ...)
    # rather than a sizing detail.
    ok, why, cat = eg.check_entry(sym, "buy", notional=1.0)
    if not ok:
        print(f"BLOCKED: {why}")
        return 2

    equity = cc.get_account_equity() or 0.0
    if equity <= 0:
        print("ERROR: cannot read equity; not trading.")
        return 3
    held = 0.0
    for p in cc.get_positions():
        if p.get("symbol", "").upper() == sym:
            held = abs(float(p.get("market_value") or 0))
    target = equity * float(a.get("position_pct") or 0.20)
    notional = round(target - held, 2)
    if notional < 100:
        print(f"BLOCKED: {sym} already holds ${held:,.0f}, which is the full "
              f"{float(a.get('position_pct') or 0.20)*100:.0f}% size (${target:,.0f}). Nothing to add.")
        return 2

    line = (f"{'DRY RUN — would ' if dry_run else ''}BUY {sym} ${notional:,.0f} at about {price:.2f}, "
            f"stop {stop:.2f} ({(price-stop)/price*100:.1f}% below, R = {r_per_share:.2f}/share, "
            f"about ${notional/price*r_per_share:,.0f} at risk = {notional/price*r_per_share/equity*100:.2f}% of equity)")
    if dry_run:
        print(line)
        return 0

    res = cc.place_market_order(sym, "buy", notional=notional)
    if res.get("blocked_by_cap"):
        print(f"BLOCKED: {res.get('reason')}")
        return 2
    oid = res.get("id")
    if not oid:
        print(f"ERROR: order not accepted: {res}")
        return 3
    aj.append({
        "id": oid, "ts": _now_iso(), "symbol": sym, "side": "buy", "notional": notional,
        "est_entry": price, "entry": None, "qty": None, "stop": round(stop, 2), "stop_kind": "initial",
        "r": round(r_per_share, 4), "note": (note or "")[:300], "source": "anchor_trade",
        "status": "open", "breakeven": False, "partial_done": False,
    })
    print(line)
    print(f"order {oid} sent; journal row written. The watcher manages the stop from here.")
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
    print(f"SELL {sym} {qty:g} shares ({frac*100:.0f}% of the position); order {res['id']} sent.")
    if frac >= 1.0:
        aj.update(lambda rows: _mark_manual_close(rows, sym))
    return 0


def _mark_manual_close(rows, sym):
    changed = False
    for r in rows:
        if r.get("symbol") == sym and r.get("status") == "open":
            r["reason"] = "sold_by_wb"
            changed = True
    return changed


def cmd_status() -> int:
    st = eg.status()
    print(f"{st['et_time']}  fence {'ON' if st['enabled'] else 'OFF'}  anchors: {', '.join(st['universe'])}")
    print(f"entry window {st['window'][0]}-{st['window'][1]} ET: {'OPEN' if st['in_window'] else 'closed'}"
          f"   market: {'open' if st['market_open'] else 'closed' if st['market_open'] is not None else '?'}")
    chg = st.get("day_change_pct")
    print(f"day so far: {chg:+.2f}%  {'HALTED (daily loss limit)' if st['daily_loss_halt'] else ''}"
          if chg is not None else "day so far: unknown")
    used = st.get("entries_today") or {}
    print("entries used today: " + (", ".join(f"{k} {v}" for k, v in sorted(used.items())) or "none"))
    print(f"loss streak today: {st['loss_streak']}  {'HALTED (consecutive losses)' if st['loss_halt'] else ''}")
    for e in st.get("errors") or []:
        print(f"warning: {e}")
    rows = aj.open_rows()
    if not rows:
        print("open anchor trades: none")
        return 0
    print("open anchor trades:")
    for r in rows:
        entry = r.get("entry") or r.get("est_entry") or 0
        cur = latest_price(r["symbol"]) or 0
        rr = float(r.get("r") or 0)
        prog = (cur - entry) / rr if rr and entry else 0.0
        print(f"  {r['symbol']}: entry {entry:.2f}, now {cur:.2f} ({(cur/entry-1)*100:+.2f}%), "
              f"stop {float(r['stop']):.2f} [{r.get('stop_kind')}], progress {prog:+.2f}R"
              f"{', partial taken' if r.get('partial_done') else ''}  — {r.get('note','')[:80]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("buy"); b.add_argument("symbol"); b.add_argument("--stop", type=float, required=True)
    b.add_argument("--note", default=""); b.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("sell"); s.add_argument("symbol"); s.add_argument("--frac", type=float, default=1.0)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "buy":
        return cmd_buy(a.symbol, a.stop, a.note, a.dry_run)
    if a.cmd == "sell":
        return cmd_sell(a.symbol, a.frac)
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
