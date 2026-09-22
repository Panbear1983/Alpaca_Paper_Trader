#!/usr/bin/env python3
"""
manual_trade_tracker.py — Peter's own buying pattern on the High Risk wallet.

Every order placed by hand through the TUI is tagged client_order_id
"manual-YYYYMMDD-xxxxxxxx" (see capitol_copier._caller_source /
_log_manual_trade) and, from 2026-08-31 on, logged live to
diary/manual_trades.jsonl the moment it's placed. This script:

  sync     pulls Alpaca's full order history for this wallet, finds every
           "manual-" order (including ones from before the live logging
           started), and merges any missing ones into the journal
  summary  prints a plain-English read of the journal: what Peter buys,
           how often, how much, and when

Usage:
  python3 manual_trade_tracker.py            sync, then print the summary
  python3 manual_trade_tracker.py --sync-only
  python3 manual_trade_tracker.py --summary-only
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests

import capitol_copier as cc

HERE = Path(__file__).resolve().parent
JOURNAL = HERE / "diary" / "manual_trades.jsonl"


def _read_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    rows = []
    for line in JOURNAL.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _write_journal(rows: list[dict]) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: r.get("ts") or r.get("submitted_at") or "")
    with open(JOURNAL, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def sync() -> int:
    """Pull every order Alpaca has ever recorded for this account, keep the
    ones tagged as manual (placed by hand, not by an engine), and merge any
    that aren't already in the journal. Returns how many were newly added."""
    existing = _read_journal()
    known_ids = {r.get("order_id") for r in existing if r.get("order_id")}

    added = []
    after = None
    while True:
        params = {"status": "all", "limit": 500, "direction": "asc"}
        if after:
            params["after"] = after
        r = requests.get(f"{cc.BASE_URL}/orders", headers=cc.ALPACA_HEADERS,
                          params=params, timeout=20)
        r.raise_for_status()
        page = r.json() or []
        if not page:
            break
        for o in page:
            coid = str(o.get("client_order_id") or "")
            if not coid.startswith("manual-"):
                continue
            if o.get("id") in known_ids:
                continue
            known_ids.add(o.get("id"))
            added.append({
                "ts": o.get("submitted_at"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "notional": float(o["notional"]) if o.get("notional") else None,
                "qty": float(o["qty"]) if o.get("qty") else None,
                "order_id": o.get("id"),
                "client_order_id": coid,
                "status": o.get("status"),
                "filled_avg_price": float(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
                "filled_qty": float(o["filled_qty"]) if o.get("filled_qty") else None,
            })
        if len(page) < 500:
            break
        after = page[-1].get("submitted_at")

    if added:
        _write_journal(existing + added)
    return len(added)


def summary() -> None:
    rows = [r for r in _read_journal() if str(r.get("side", "")).lower() == "buy"]
    if not rows:
        print("No manual buys on record yet.")
        return

    total = len(rows)
    by_symbol = Counter(r["symbol"] for r in rows)
    spend_by_symbol: dict[str, float] = defaultdict(float)
    total_spend = 0.0
    by_hour = Counter()
    by_weekday = Counter()

    for r in rows:
        amt = r.get("notional")
        if amt is None and r.get("filled_qty") and r.get("filled_avg_price"):
            amt = r["filled_qty"] * r["filled_avg_price"]
        if amt:
            spend_by_symbol[r["symbol"]] += amt
            total_spend += amt
        ts = r.get("ts")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                by_hour[dt.hour] += 1
                by_weekday[dt.strftime("%A")] += 1
            except Exception:
                pass

    print(f"Manual buys on record: {total}")
    print(f"Total spent: ${total_spend:,.0f}" if total_spend else "Total spent: unknown (no notional/fill data)")
    print(f"Average per buy: ${total_spend/total:,.0f}" if total_spend else "")
    print()
    print("By stock:")
    for sym, n in by_symbol.most_common():
        spend = spend_by_symbol.get(sym, 0.0)
        spend_str = f", ${spend:,.0f} spent" if spend else ""
        print(f"  {sym}: {n} buy(s){spend_str}")
    if by_weekday:
        print()
        print("By day of week (UTC):", ", ".join(f"{d} {n}" for d, n in by_weekday.most_common()))
    if by_hour:
        busiest = by_hour.most_common(1)[0]
        print(f"Busiest hour (UTC): {busiest[0]:02d}:00 ({busiest[1]} buys)")
    print()
    print("Most recent 5:")
    for r in rows[-5:]:
        amt = r.get("notional") or (r.get("filled_qty", 0) * (r.get("filled_avg_price") or 0)) or 0
        print(f"  {r.get('ts', '?')}  {r['symbol']}  ${amt:,.0f}  ({r.get('status')})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync-only", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    if not args.summary_only:
        n = sync()
        print(f"Synced {n} new manual order(s) from Alpaca.\n")
    if not args.sync_only:
        summary()
