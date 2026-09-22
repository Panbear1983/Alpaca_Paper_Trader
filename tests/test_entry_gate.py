"""entry_gate tests — no network. Every data source is replaced with a fake."""
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entry_gate as eg  # noqa: E402

ET = ZoneInfo("America/New_York")
CFG = {"enabled": True, "universe": ["AAPL", "MSFT", "NVDA", "JPM", "CAT"],
       "entry_window_et": ["09:30", "11:30"], "max_entries_per_name_per_day": 2,
       "max_entries_per_day": 0, "consecutive_loss_halt": 2, "daily_loss_limit_pct": 3.0}
MON_10 = dt.datetime(2026, 8, 31, 10, 0, tzinfo=ET)


def _order(sym, side="buy", status="filled"):
    return {"symbol": sym, "side": side, "status": status}


def _setup(monkeypatch, account=None, orders=None, clock=None, journal=None):
    eg._CACHE.update({"t": 0.0, "account": None, "orders": None, "clock": None})
    monkeypatch.setattr(eg, "fetch_account", lambda: account or {"equity": "70000", "last_equity": "70000"})
    monkeypatch.setattr(eg, "fetch_todays_orders", lambda now: orders or [])
    monkeypatch.setattr(eg, "fetch_clock", lambda: clock or {"is_open": True})
    monkeypatch.setattr(eg, "read_journal", lambda: journal or [])


def test_sell_always_passes(monkeypatch):
    _setup(monkeypatch)
    assert eg.check_entry("TTAN", "sell", qty=5, now=MON_10, cfg=CFG)[0] is True


def test_disabled_passes_everything(monkeypatch):
    _setup(monkeypatch)
    off = dict(CFG, enabled=False)
    assert eg.check_entry("TTAN", "buy", notional=100, now=MON_10, cfg=off)[0] is True


def test_non_anchor_blocked(monkeypatch):
    _setup(monkeypatch)
    ok, why, cat = eg.check_entry("TTAN", "buy", notional=100, now=MON_10, cfg=CFG)
    assert not ok and cat == "universe" and "TTAN" in why


def test_anchor_in_window_passes(monkeypatch):
    _setup(monkeypatch)
    assert eg.check_entry("aapl", "buy", notional=100, now=MON_10, cfg=CFG) == (True, "", "")


def test_outside_window_blocked(monkeypatch):
    _setup(monkeypatch)
    late = dt.datetime(2026, 8, 31, 11, 31, tzinfo=ET)
    ok, why, cat = eg.check_entry("AAPL", "buy", notional=100, now=late, cfg=CFG)
    assert not ok and cat == "window"
    early = dt.datetime(2026, 8, 31, 9, 29, tzinfo=ET)
    assert eg.check_entry("AAPL", "buy", notional=100, now=early, cfg=CFG)[2] == "window"


def test_weekend_blocked(monkeypatch):
    _setup(monkeypatch)
    sun = dt.datetime(2026, 8, 30, 10, 0, tzinfo=ET)
    assert eg.check_entry("AAPL", "buy", notional=100, now=sun, cfg=CFG)[2] == "window"


def test_holiday_blocked_by_clock(monkeypatch):
    _setup(monkeypatch, clock={"is_open": False})
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)[2] == "market"


def test_daily_loss_halt(monkeypatch):
    _setup(monkeypatch, account={"equity": "67800", "last_equity": "70000"})   # -3.14%
    ok, why, cat = eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)
    assert not ok and cat == "daily_loss"
    _setup(monkeypatch, account={"equity": "68000", "last_equity": "70000"})   # -2.86%
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)[0]


def test_entries_per_name(monkeypatch):
    _setup(monkeypatch, orders=[_order("AAPL"), _order("AAPL"), _order("MSFT")])
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)[2] == "entries"
    assert eg.check_entry("MSFT", "buy", notional=100, now=MON_10, cfg=CFG)[0]
    # unfilled and sells do not count
    _setup(monkeypatch, orders=[_order("AAPL", status="canceled"), _order("AAPL", side="sell")])
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)[0]


def test_total_entries_cap(monkeypatch):
    _setup(monkeypatch, orders=[_order("AAPL"), _order("MSFT")])
    capped = dict(CFG, max_entries_per_day=2)
    assert eg.check_entry("NVDA", "buy", notional=100, now=MON_10, cfg=capped)[2] == "entries"


def test_loss_streak_halt(monkeypatch):
    today = "2026-08-31"
    journal = [
        {"status": "closed", "closed_at": f"{today}T10:05:00-04:00", "pnl": -120},
        {"status": "closed", "closed_at": f"{today}T10:40:00-04:00", "pnl": -80},
    ]
    _setup(monkeypatch, journal=journal)
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)[2] == "loss_streak"
    # a win after the losses resets the streak
    journal.append({"status": "closed", "closed_at": f"{today}T10:50:00-04:00", "pnl": 30})
    _setup(monkeypatch, journal=journal)
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)[0]
    # yesterday's losses do not count
    old = [dict(r, closed_at=r["closed_at"].replace(today, "2026-08-28")) for r in journal[:2]]
    _setup(monkeypatch, journal=old)
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)[0]


def test_data_failure_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("api down")
    _setup(monkeypatch)
    monkeypatch.setattr(eg, "fetch_account", boom)
    ok, why, cat = eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=CFG)
    assert not ok and cat == "daily_loss" and "refusing" in why
