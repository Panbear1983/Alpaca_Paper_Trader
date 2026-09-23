"""scorecard — pure parts. No network."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scorecard as sc  # noqa: E402


def _hist(start: dt.date, equities):
    ts = [int(dt.datetime(start.year, start.month, start.day + k, 20, 0, tzinfo=dt.timezone.utc).timestamp())
          for k in range(len(equities))]
    return {"timestamp": ts, "equity": list(equities)}


def test_equity_since_picks_the_first_close_on_or_after_the_date():
    h = _hist(dt.date(2026, 8, 27), [70_000, 70_500, 71_000, 71_400])
    assert sc.equity_since(h, "2026-08-29") == ("2026-08-29", 71_000.0)
    assert sc.equity_since(h, "2026-08-30") == ("2026-08-30", 71_400.0)
    assert sc.equity_since(h, "2026-09-30") is None


def test_by_source_credits_the_opener_and_honours_since():
    trades = [{"strategy": "manual", "pnl_usd": 500, "exit_date": "2026-09-01"},
              {"strategy": "manual", "pnl_usd": -100, "exit_date": "2026-09-10"},
              {"strategy": "swing", "pnl_usd": 40, "exit_date": "2026-08-20"},      # before since
              {"strategy": None, "pnl_usd": 10, "exit_date": "2026-09-12"}]
    out = sc.by_source(trades, "2026-08-29")
    assert out["manual"] == {"n": 2, "pnl": 400.0, "wins": 1}
    assert "swing" not in out and out["unattributed"]["n"] == 1


def test_lines_english_and_gap():
    out = sc.lines("2026-08-29", 70_000, 71_000, 500.0, 517.5,
                   {"manual": {"n": 7, "pnl": 812.0, "wins": 4}, "swing": {"n": 1, "pnl": -40.0, "wins": 0}})
    assert out[0] == "🎯 Since 2026-08-29: wallet +1.4% · QQQ +3.5% · gap -2.1 pts"
    assert out[1].endswith("manual +812 (7)  ·  swing -40 (1)")
    assert sc.lines("2026-08-29", None, 71_000, 500.0, 517.5, {}) == []


def test_translator_is_used_when_given():
    calls = []
    def t(key, **kw):
        calls.append(key); return key
    assert sc.lines("2026-08-29", 1, 1, 1, 1, {"manual": {"n": 1, "pnl": 1.0, "wins": 1}}, t) == ["rpt.score", "rpt.score_src"]
