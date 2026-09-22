"""Step 9 — the evening reconciliation, pure parts only (no network).

guard_breaches gets every fact handed in; drift_lines compares digests.
"""
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import oversight as ov  # noqa: E402

ET = ZoneInfo("America/New_York")
CEIL = {"position_pct": 0.25, "gross": 1.0}
ENGINE_TODAY = {"position_pct": 1.0, "gross": 2.0}       # step 8 not applied
ENGINE_BOXED = {"position_pct": 0.25, "gross": 1.0}      # after step 8
CLOSE = dt.datetime(2026, 9, 22, 16, 0, tzinfo=ET).timestamp()
CHECK = CLOSE + 15 * 60


def _pos(sym, qty, mv):
    return {"symbol": sym, "qty": str(qty), "market_value": str(mv)}


def _guard(qty, trail, hwm=100.0):
    return {"qty": str(qty), "trail_percent": str(trail), "hwm": str(hwm)}


def _run(positions=None, guards=None, widths=None, ceiling=CEIL, engine=ENGINE_TODAY, equity=71_000,
         guards_on=True, hb=CLOSE - 20, close=CLOSE, now=CHECK):
    positions = positions if positions is not None else [_pos("NVDA", 92.3, 20_600), _pos("TSLA", 82.5, 30_500)]
    guards = guards if guards is not None else {"NVDA": [_guard(92, 6.64)], "TSLA": [_guard(82, 8.95)]}
    widths = widths if widths is not None else {"NVDA": 6.64, "TSLA": 8.95}
    return ov.guard_breaches(positions, guards, widths, ceiling, engine, equity, guards_on, hb, close, now)


def test_todays_book_is_clean():
    assert _run() == []


def test_not_a_boxed_wallet_says_nothing_whatever_the_book():
    assert _run(ceiling=None, guards={}, equity=1, hb=None) == []


def test_whole_shares_without_a_resting_stop():
    out = _run(guards={"NVDA": [_guard(92, 6.64)]})
    assert len(out) == 1 and out[0].startswith("TSLA: 82 whole shares with no resting stop")


def test_fractional_dust_needs_no_guard_but_must_not_have_one():
    assert _run(positions=[_pos("AMD", 0.7, 100)], guards={}, widths={}) == []
    out = _run(positions=[_pos("AMD", 0.7, 100)], guards={"AMD": [_guard(1, 10)]}, widths={})
    assert "under one share" in out[0]


def test_guard_quantity_and_width_drift():
    out = _run(guards={"NVDA": [_guard(90, 6.64)], "TSLA": [_guard(82, 7.5)]},
               widths={"NVDA": 6.64, "TSLA": 9.0})
    assert any("covers 90 of 92" in x for x in out)
    assert any("trails 7.5%, today's width is 9.0%" in x for x in out)
    # drift inside the tolerance is left alone
    assert _run(guards={"NVDA": [_guard(92, 6.64)], "TSLA": [_guard(82, 8.2)]}) == []


def test_duplicate_and_orphan_guards():
    out = _run(guards={"NVDA": [_guard(92, 6.64, hwm=200), _guard(92, 6.64, hwm=210)],
                       "TSLA": [_guard(82, 8.95)], "AMD": [_guard(5, 10)]})
    assert any("NVDA: 2 resting stops" in x for x in out)
    assert any("AMD: a resting stop with no position" in x for x in out)


def test_guards_switched_off_is_the_only_line_about_guards():
    out = _run(guards={}, guards_on=False)
    assert len(out) == 1 and "switched OFF" in out[0]


def test_over_ceiling_holding_is_silent_until_the_config_cap_is_applied():
    assert _run() == []                                        # TSLA 43%: Peter's choice today
    out = _run(engine=ENGINE_BOXED)                            # after step 8 the engine should have trimmed
    assert any("TSLA is 43% of equity, over the 25% cap" in x for x in out)
    assert any("NVDA is 29% of equity" in x for x in out)


def test_borrowing_is_always_a_breach():
    out = _run(positions=[_pos("TSLA", 82.5, 85_000)], guards={"TSLA": [_guard(82, 8.95)]}, widths={"TSLA": 8.95})
    assert any("Book is 1.20x equity" in x and "borrowing" in x for x in out)


def test_heartbeat_at_the_bell():
    assert _run(hb=CLOSE - 100) == []
    assert "was not sweeping at the bell" in _run(hb=CLOSE - 2 * 3600)[0]
    assert "No price-watcher heartbeat" in _run(hb=None)[0]
    # before the close nothing is said about the heartbeat yet
    assert _run(hb=None, now=CLOSE - 3600) == []


def test_early_close_uses_the_real_bell():
    early = dt.datetime(2026, 11, 27, 13, 0, tzinfo=ET).timestamp()
    assert _run(hb=early - 30, close=early, now=early + 900) == []


# ── drift ────────────────────────────────────────────────────────────────────

def test_json_changed_keys_two_levels():
    old = {"anchor": {"universe": ["A"], "position_pct": 1.0}, "risk": {"x": 1}}
    new = {"anchor": {"universe": ["A", "B"], "position_pct": 1.0}, "risk": {"x": 1}, "extra": 1}
    assert ov._json_changed_keys(old, new) == ["anchor.universe", "extra"]


def test_drift_lines_only_for_unlogged_changes():
    prev = {"strategy": {"digest": "aaa", "ts": "2026-09-21T16:15:00-04:00"},
            "soul": {"digest": "bbb", "ts": "2026-09-21T16:15:00-04:00"}}
    cur = {"strategy": {"digest": "ccc", "ts": "x", "name": "strategy_high_risk.json", "keys": ["anchor.universe"]},
           "soul": {"digest": "ddd", "ts": "x", "name": "SOUL.md"}}
    out = ov.drift_lines(prev, cur, logged={"soul"})
    assert len(out) == 1 and out[0].startswith("strategy_high_risk.json was rewritten since the 2026-09-21T16:15")
    assert "anchor.universe" in out[0]
    assert ov.drift_lines({}, cur, set()) == []                  # first run only records
