#!/usr/bin/env python3
"""
anchor_journal.py — the trade journal for the anchor plan, diary/anchor_trades.jsonl.

One JSON row per entry. anchor_trade.py appends when WB opens a position; price_watcher.py
rewrites rows as it moves stops, takes partials and closes them; entry_gate.py reads it for
the consecutive-loss halt; oversight.py reads it to document the day. Two processes write it,
so every write holds an exclusive lock and full rewrites go through a temp file + rename.

Row shape (all times ISO-8601 with an ET offset):
  id, ts, symbol, side, notional, est_entry, entry, qty, stop, stop_kind, r, note, source,
  status ("open" | "closed" | "failed"), breakeven, partial_done, partial_at,
  exit_price, pnl, r_multiple, reason, closed_at
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).resolve().parent
JOURNAL = _HERE / "diary" / "anchor_trades.jsonl"
LOCK = JOURNAL.with_suffix(".lock")


@contextmanager
def locked():
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def read() -> list[dict]:
    try:
        return [json.loads(l) for l in JOURNAL.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def append(row: dict) -> None:
    with locked():
        with open(JOURNAL, "a") as f:
            f.write(json.dumps(row) + "\n")


def update(mutator) -> bool:
    """mutator(rows) edits in place and returns True if anything changed."""
    with locked():
        rows = read()
        if not mutator(rows):
            return False
        tmp = JOURNAL.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        os.replace(tmp, JOURNAL)
        return True


def open_rows(rows: list[dict] | None = None) -> list[dict]:
    return [r for r in (rows if rows is not None else read()) if r.get("status") == "open"]
