"""Cooling-off, concurrent-name cap and cash floor — no network.

Same faking contract as test_entry_gate.py: every data source is a module-level
function on entry_gate replaced with a lambda; cfg is injected; the cooling
state file lives in a temp dir.
"""
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entry_gate as eg  # noqa: E402

ET = ZoneInfo("America/New_York")
MON_10 = dt.datetime(2026, 8, 31, 10, 0, tzinfo=ET)
CFG = {"enabled": True, "universe": ["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "CAT", "V"],
       "entry_window_et": ["09:30", "11:30"], "max_entries_per_name_per_day": 2,
       "max_entries_per_day": 0, "consecutive_loss_halt": 2, "daily_loss_limit_pct": 3.0,
       "cooling_off_pct": 8.0, "cooling_off_days": 5, "max_open_names": 3, "min_cash_pct": 0.20}


def _hist(*equities, start=dt.date(2026, 8, 3)):
    """Daily closes, one per calendar day from `start` (default: early August,
    i.e. before MON_10)."""
    base = dt.datetime(start.year, start.month, start.day, 21, 0, tzinfo=dt.timezone.utc)
    ts = [int((base + dt.timedelta(days=i)).timestamp()) for i in range(len(equities))]
    return {"timestamp": ts, "equity": list(equities)}


def _setup(monkeypatch, tmp_path, equity=100_000, cash=40_000, hist=None, positions=None,
           order_value=1_000.0, calendar=None):
    eg._CACHE.update({"t": 0.0, "account": None, "orders": None, "clock": None,
                      "history": None, "positions": None})
    monkeypatch.setattr(eg, "_cooling_path", lambda: str(tmp_path / "cool.json"))
    monkeypatch.setattr(eg, "fetch_account",
                        lambda: {"equity": str(equity), "last_equity": str(equity), "cash": str(cash)})
    monkeypatch.setattr(eg, "fetch_todays_orders", lambda now: [])
    monkeypatch.setattr(eg, "fetch_clock", lambda: {"is_open": True})
    monkeypatch.setattr(eg, "read_journal", lambda: [])
    monkeypatch.setattr(eg, "fetch_portfolio_history", lambda now: hist if hist is not None else _hist(100_000))
    monkeypatch.setattr(eg, "fetch_positions", lambda: positions or [])
    monkeypatch.setattr(eg, "estimate_order_value", lambda t, n, q: order_value)
    monkeypatch.setattr(eg, "fetch_calendar", lambda a, b: calendar or [])


def _buy(sym="AAPL", now=MON_10, notional=1000):
    return eg.check_entry(sym, "buy", notional=notional, now=now, cfg=CFG)


# ── cooling-off ──────────────────────────────────────────────────────────────

def test_seven_point_nine_percent_off_the_high_still_buys(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=92_100, hist=_hist(100_000, 98_000))
    assert _buy()[0] is True


def test_eight_percent_off_the_high_pauses_and_writes_state(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=92_000, hist=_hist(100_000, 98_000))
    ok, why, cat = _buy()
    assert ok is False and cat == "cooling_off" and "20-day high" in why
    st = eg._cooling_read()
    assert st["triggered_on"] == "2026-08-31" and st["peak_equity"] == 100_000.0
    # 5 weekdays after Mon Aug 31 = Mon Sep 7 (without a calendar, Labor Day is not skipped)
    assert st["resume_on"] == "2026-09-07"


def test_a_real_calendar_sets_the_resume_day(monkeypatch, tmp_path):
    cal = [dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 3), dt.date(2026, 9, 4),
           dt.date(2026, 9, 8), dt.date(2026, 9, 9)]                    # Sep 7 holiday
    _setup(monkeypatch, tmp_path, equity=92_000, hist=_hist(100_000), calendar=cal)
    _buy()
    assert eg._cooling_read()["resume_on"] == "2026-09-08"


def test_still_paused_next_day_even_if_equity_recovers(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=92_000, hist=_hist(100_000))
    _buy()
    _setup(monkeypatch, tmp_path, equity=99_000, hist=_hist(100_000))
    ok, why, cat = _buy(now=MON_10 + dt.timedelta(days=1))
    assert ok is False and cat == "cooling_off" and "until 2026-09-07" in why


def test_buys_again_on_the_resume_day(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=92_000, hist=_hist(100_000))
    _buy()
    # Tue Sep 8, still 8% under the OLD high but the pause has expired and no new high was made
    _setup(monkeypatch, tmp_path, equity=92_000, hist=_hist(100_000, 92_000, 92_000))
    ok, _, _ = _buy(now=dt.datetime(2026, 9, 8, 10, 0, tzinfo=ET))
    assert ok is True


def test_re_arms_only_after_a_new_high(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=92_000, hist=_hist(100_000))
    _buy()                                                        # triggered Aug 31
    later = dt.datetime(2026, 9, 15, 10, 0, tzinfo=ET)
    # a new high dated AFTER the trigger, then an 8.6% drop → pauses again
    _setup(monkeypatch, tmp_path, equity=96_000,
           hist=_hist(100_000, 101_000, 105_000, start=dt.date(2026, 9, 9)))
    ok, _, cat = _buy(now=later)
    assert ok is False and cat == "cooling_off"


def test_an_old_high_does_not_re_arm(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=92_000, hist=_hist(100_000))
    _buy()                                                        # triggered Aug 31
    later = dt.datetime(2026, 9, 15, 10, 0, tzinfo=ET)
    _setup(monkeypatch, tmp_path, equity=85_000, hist=_hist(100_000, 92_000, 90_000))   # highs all pre-trigger
    assert _buy(now=later)[0] is True


def test_history_failure_fails_closed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(eg, "fetch_portfolio_history", lambda now: (_ for _ in ()).throw(RuntimeError("api")))
    ok, why, cat = _buy()
    assert ok is False and cat == "cooling_off" and "refusing" in why


def test_rule_off_when_the_config_never_heard_of_it(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=50_000, hist=_hist(100_000))
    cfg = {k: v for k, v in CFG.items() if not k.startswith("cooling_off")}
    assert eg.check_entry("AAPL", "buy", notional=100, now=MON_10, cfg=cfg)[0] is True


# ── concurrent-name cap ──────────────────────────────────────────────────────

def _pos(*syms):
    return [{"symbol": s, "qty": "1"} for s in syms]


def test_new_name_refused_at_the_cap_but_adding_to_a_held_one_is_fine(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, positions=_pos("NVDA", "TSLA", "MSFT"))
    ok, why, cat = _buy("AAPL")
    assert ok is False and cat == "concurrent" and "3 names" in why
    assert _buy("NVDA")[0] is True


def test_under_the_cap_buys(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, positions=_pos("NVDA", "TSLA"))
    assert _buy("AAPL")[0] is True


# ── cash floor ───────────────────────────────────────────────────────────────

def test_buy_that_breaks_the_cash_floor_is_refused_at_the_boundary(monkeypatch, tmp_path):
    # equity 100k, floor 20% = 20k; cash 25k; a 5,000 buy leaves exactly 20k → allowed
    _setup(monkeypatch, tmp_path, cash=25_000, order_value=5_000)
    assert _buy(notional=5_000)[0] is True
    _setup(monkeypatch, tmp_path, cash=25_000, order_value=5_001)
    ok, why, cat = _buy(notional=5_001)
    assert ok is False and cat == "cash_floor" and "floor" in why


def test_sells_are_never_touched_by_any_of_this(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, equity=50_000, hist=_hist(100_000), cash=0,
           positions=_pos("A", "B", "C", "D"))
    assert eg.check_entry("NVDA", "sell", qty=5, now=MON_10, cfg=CFG) == (True, "", "")
