"""wbauto — the agent's toolbox, pure parts. No network."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wbauto as wb  # noqa: E402


def test_coerce_reads_what_the_agent_types():
    assert wb.coerce("true") is True and wb.coerce("No") is False and wb.coerce("none") is None
    assert wb.coerce("off") == "off" and wb.coerce("on") == "on"
    assert wb.coerce("12") == 12 and wb.coerce("-3") == -3 and wb.coerce("0.08") == 0.08
    assert wb.coerce('["09:30", "11:30"]') == ["09:30", "11:30"]
    assert wb.coerce("notify") == "notify"


def test_target_progress_straight_line():
    p = wb.target_progress(100_000, 0.10, dt.date(2026, 10, 2), dt.date(2026, 10, 2), 100_000)
    assert p["day"] == 0 and abs(p["goal"] - 110_000) < 1e-6 and abs(p["vs_pace_pct"]) < 1e-9 and p["days_left"] == 91
    half = dt.date(2026, 10, 2) + dt.timedelta(days=45)
    p = wb.target_progress(100_000, 0.10, dt.date(2026, 10, 2), half, 103_000)
    assert abs(p["pace_equity"] - 104_945) < 1 and p["vs_pace_pct"] < 0 and abs(p["needed_pct"] - 6.8) < 0.1
    late = dt.date(2026, 10, 2) + dt.timedelta(days=120)
    assert wb.target_progress(100_000, 0.10, dt.date(2026, 10, 2), late, 111_000)["days_left"] == 0


def test_rules_summary_reads_the_agents_own_file():
    cfg = {"anchor": {"enabled": True, "entry_window_et": ["09:30", "11:30"], "universe": ["A", "B"], "max_open_names": 25,
                      "min_cash_pct": 0.05, "daily_loss_limit_pct": 2.5, "cooling_off_pct": 5.0, "cooling_off_days": 2,
                      "regime_filter_enabled": True, "max_entries_per_name_per_day": 2, "max_entries_per_day": 4},
           "risk": {"max_position_pct": 0.2, "max_gross_exposure": 1.0, "gross_by_regime": {"bull": 1.0}},
           "dynamic_exits": {"stop_loss_pct": 0.08, "trail_trigger_pct": 0.5, "trail_giveback_pct": 0.25, "take_profit_levels": [], "pyramid_levels": [], "max_holdings": 20},
           "capitol_copier": {"exits_on": True, "copy_on": False}, "swing": {"enabled": False}, "trading_schedule": {"manage_every_minutes": 20}}
    out = wb.rules_summary(cfg)
    assert out[0].startswith("fence ON: window ['09:30', '11:30'] ET, 2 names on the list")
    assert "20% per name, gross 1.0x" in out[1] and "stop 8%" in out[2] and "swing buyer off" in out[3]


def test_stats_from_bars():
    bars = [{"t": f"2026-09-{d:02d}", "o": 100 + d * 0.5, "h": 102 + d * 0.5, "l": 98 + d * 0.5, "c": 100 + d * 0.5} for d in range(1, 31)]
    st = wb.stats_from_bars(bars)
    assert abs(st["close"] - 115.0) < 1e-9 and st["d5"] > 0 and st["d20"] > 0 and st["atr"] > 0 and st["from_hi20"] <= 0


def test_journal_and_logs_live_in_the_agents_diary(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "DIARY", str(tmp_path)); monkeypatch.setattr(wb, "JOURNAL", str(tmp_path / "journal.md"))
    monkeypatch.setattr(wb, "CHANGES", str(tmp_path / "changes.jsonl"))
    wb.journal_add("First entry.")
    assert "First entry." in wb.journal_tail()
    wb._append(wb.CHANGES, {"ts": dt.datetime.now(wb.ET).isoformat(), "key": "anchor.min_cash_pct", "old": 0.05, "new": 0.0, "why": "test"})
    assert "anchor.min_cash_pct: 0.05 → 0.0" in wb.changes_lines()[1]
