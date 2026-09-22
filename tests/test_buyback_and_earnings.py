"""Buy-back mode and the earnings warning — no network.

The buy-back used to be an automatic BUY of a falling stock. Now its default
is a once-a-day text. The earnings warning pins trading-day counting and the
"silent without a key" posture.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_fields as cf  # noqa: E402
import earnings_calendar as ec  # noqa: E402
import price_watcher as pw  # noqa: E402


# ── buy-back decision ────────────────────────────────────────────────────────

def test_default_is_notify_once_per_day():
    assert pw.buyback_decision("notify", None, "2026-09-22") == "notify"
    assert pw.buyback_decision("notify", "2026-09-22", "2026-09-22") is None
    assert pw.buyback_decision("notify", "2026-09-21", "2026-09-22") == "notify"
    assert pw.buyback_decision(None, None, "2026-09-22") == "notify"


def test_trade_mode_still_trades_and_off_does_nothing():
    assert pw.buyback_decision("trade", "2026-09-22", "2026-09-22") == "trade"
    assert pw.buyback_decision("off", None, "2026-09-22") is None


def test_buyback_mode_field_only_accepts_the_three_words():
    f = cf.by_path("price_watcher.buyback_mode")
    assert f is not None and f.section == "WATCHER"
    assert cf.validate(f, "Notify") == (True, "notify")
    assert cf.validate(f, "maybe")[0] is False
    assert cf.fmt_range(f) == "notify/trade/off"


# ── earnings warning ─────────────────────────────────────────────────────────

ROWS = [{"symbol": "TSLA", "date": "2026-10-28"}, {"symbol": "MSFT", "date": "2026-10-28"},
        {"symbol": "AAPL", "date": "2026-10-29"}, {"symbol": "NVDA", "date": "2026-11-19"}]


def test_only_held_names_within_the_lead_are_flagged():
    hits = ec.upcoming(["TSLA", "NVDA"], "2026-10-26", lead_days=2, rows=ROWS)
    assert hits == [("TSLA", "2026-10-28")]                # Mon → Wed is 2 sessions; NVDA far off


def test_weekend_does_not_count_as_sessions():
    # Friday Oct 23 → Wednesday Oct 28 is 3 sessions (Mon, Tue, Wed): outside a 2-day lead
    assert ec.upcoming(["TSLA"], "2026-10-23", lead_days=2, rows=ROWS) == []
    assert ec.upcoming(["TSLA"], "2026-10-23", lead_days=3, rows=ROWS) == [("TSLA", "2026-10-28")]


def test_a_real_calendar_overrides_weekday_counting():
    import datetime as dt
    cal = [dt.date(2026, 10, 27), dt.date(2026, 10, 28)]        # Oct 26 a holiday, say
    assert ec.upcoming(["TSLA"], "2026-10-23", lead_days=2, rows=ROWS, trading_days=cal) == [("TSLA", "2026-10-28")]


def test_lead_zero_flags_only_the_day_itself():
    assert ec.upcoming(["TSLA"], "2026-10-28", lead_days=0, rows=ROWS) == [("TSLA", "2026-10-28")]
    assert ec.upcoming(["TSLA"], "2026-10-27", lead_days=0, rows=ROWS) == []


def test_notice_text_names_the_day_and_the_risk():
    s = ec.notice(["TSLA", "MSFT"], "2026-10-26", 2, rows=ROWS)
    assert "TSLA Wed Oct 28" in s and "MSFT Wed Oct 28" in s and "stop" in s
    assert ec.notice(["NVDA"], "2026-10-26", 2, rows=ROWS) == ""


def test_no_key_means_silence_not_an_error(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    import datetime as dt
    assert ec.fetch(dt.date(2026, 10, 1), dt.date(2026, 11, 1)) == []
