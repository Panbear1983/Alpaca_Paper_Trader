"""
tests/test_price_streaks.py — Unit tests for streak win/loss counter and accumulated % indicator.
"""
import datetime as dt
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.text import Text
import price_streaks as ps
import i18n

ET = ZoneInfo("America/New_York")


def test_compute_streak_reversal_and_continuation():
    """
    Closes 100, 99, 98, 97: streak -3.
    Reversal at 98: current +1, base 97.
    Next higher close 99: current +2, base 97.
    """
    # 3 consecutive down sessions
    s, pct = ps.compute_streak([100.0, 99.0, 98.0, 97.0])
    assert s == -3
    assert pytest.approx(pct, 0.01) == ((97.0 - 100.0) / 100.0) * 100  # -3.0%

    # Reversal up to 98: breaks down streak -> +1
    s, pct = ps.compute_streak([100.0, 99.0, 98.0, 97.0, 98.0])
    assert s == 1
    assert pytest.approx(pct, 0.01) == ((98.0 - 97.0) / 97.0) * 100  # +1.03%

    # Continuation up to 99: extends up streak -> +2
    s, pct = ps.compute_streak([100.0, 99.0, 98.0, 97.0, 98.0, 99.0])
    assert s == 2
    assert pytest.approx(pct, 0.01) == ((99.0 - 97.0) / 97.0) * 100  # +2.06%


def test_compute_streak_flat_days():
    """Flat days reset the current streak to 0 and 0.0%."""
    s, pct = ps.compute_streak([100.0, 102.0, 102.0])
    assert s == 0
    assert pct == 0.0

    # Next up day after a flat day starts a new streak of +1
    s, pct = ps.compute_streak([100.0, 102.0, 102.0, 105.0])
    assert s == 1
    assert pytest.approx(pct, 0.01) == ((105.0 - 102.0) / 102.0) * 100


def test_compute_streak_edge_cases():
    """Empty, single element, or non-moving series."""
    assert ps.compute_streak([]) == (0, 0.0)
    assert ps.compute_streak([150.0]) == (0, 0.0)
    assert ps.compute_streak([100.0, 100.0, 100.0]) == (0, 0.0)


def test_two_snapshots_market_open_vs_closed():
    """
    Snapshot 1 (Market Open):
      Evaluates live current_price against yesterday's completed close.
    Snapshot 2 (Market Closed):
      Uses finalized daily bars.
    """
    bars = [
        {"t": "2026-09-28T04:00:00Z", "c": 100.0},
        {"t": "2026-09-29T04:00:00Z", "c": 105.0},  # yesterday's close = 105.0 (streak was +1)
    ]
    # Reference date: 2026-09-30 at 10:00 ET (Market Open)
    now_open = dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET)

    # 1. Market Open with current_price = 108.0 (Up today -> streak becomes +2)
    s, pct = ps.calculate_streak(bars, current_price=108.0, is_market_open=True, now_et=now_open)
    assert s == 2
    assert pytest.approx(pct, 0.01) == ((108.0 - 100.0) / 100.0) * 100  # +8.0%

    # 2. Market Open with current_price = 102.0 (Down today -> reverses streak to -1)
    s, pct = ps.calculate_streak(bars, current_price=102.0, is_market_open=True, now_et=now_open)
    assert s == -1
    assert pytest.approx(pct, 0.01) == ((102.0 - 105.0) / 105.0) * 100  # -2.86%

    # 3. Market Open with current_price = 105.0 (Flat today -> streak is 0)
    s, pct = ps.calculate_streak(bars, current_price=105.0, is_market_open=True, now_et=now_open)
    assert s == 0
    assert pct == 0.0

    # 4. Market Closed at 16:30 ET with today's bar finalized at 110.0
    bars_with_today = bars + [{"t": "2026-09-30T04:00:00Z", "c": 110.0}]
    now_closed = dt.datetime(2026, 9, 30, 16, 30, tzinfo=ET)
    s, pct = ps.calculate_streak(bars_with_today, current_price=None, is_market_open=False, now_et=now_closed)
    assert s == 2
    assert pytest.approx(pct, 0.01) == ((110.0 - 100.0) / 100.0) * 100  # +10.0%

    # 5. Pre-market on 2026-09-30 at 08:00 ET (Market Closed, session not started)
    now_premarket = dt.datetime(2026, 9, 30, 8, 0, tzinfo=ET)
    s, pct = ps.calculate_streak(bars, current_price=107.0, is_market_open=False, now_et=now_premarket)
    # Uses yesterday's completed close: 100 -> 105 is +1
    assert s == 1
    assert pytest.approx(pct, 0.01) == 5.0


def test_formatting():
    """Green for positive, Red for negative, Gray for zero."""
    t_win = ps.format_streak(3)
    assert t_win.plain == "+3"
    assert t_win.style == "green"

    t_loss = ps.format_streak(-2)
    assert t_loss.plain == "-2"
    assert t_loss.style == "red"

    t_zero = ps.format_streak(0)
    assert t_zero.plain == "0"
    assert t_zero.style == "gray"

    t_pct_win = ps.format_streak_pct(5.23, streak=3)
    assert t_pct_win.plain == "+5.2%"
    assert t_pct_win.style == "green"

    t_pct_loss = ps.format_streak_pct(-3.41, streak=-2)
    assert t_pct_loss.plain == "-3.4%"
    assert t_pct_loss.style == "red"

    t_pct_zero = ps.format_streak_pct(0.0, streak=0)
    assert t_pct_zero.plain == "0.0%"
    assert t_pct_zero.style == "gray"


def test_i18n_columns():
    """Verify English and Traditional Chinese column headers."""
    assert i18n.t("col.streak") == "STREAK"
    assert i18n.t("col.streak_pct") == "STREAK %"

    i18n.set_lang("zh_TW")
    assert i18n.t("col.streak") == "連漲跌"
    assert i18n.t("col.streak_pct") == "連累計 %"
    i18n.set_lang("en")


def test_streak_cache_and_symbol_lookup():
    """Verify cache storage and lookup behavior."""
    # Unknown symbol
    assert ps.get_streak_for_symbol("UNKNOWN_SYM") == (0, 0.0)

    # Inject mock bars into cache
    mock_bars = [
        {"t": "2026-09-25T04:00:00Z", "c": 50.0},
        {"t": "2026-09-28T04:00:00Z", "c": 52.0},
        {"t": "2026-09-29T04:00:00Z", "c": 55.0},
    ]
    with ps._CACHE_LOCK:
        ps._BARS_CACHE["MOCK"] = mock_bars

    # Pre-market on Sep 30: +2 streak from Sep 25 (50 -> 52 -> 55)
    now_pre = dt.datetime(2026, 9, 30, 8, 30, tzinfo=ET)
    s, pct = ps.get_streak_for_symbol("MOCK", is_market_open=False, now_et=now_pre)
    assert s == 2
    assert pytest.approx(pct, 0.01) == 10.0

    # Market open on Sep 30 with current price = 57.0 -> +3 streak
    now_open = dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET)
    s, pct = ps.get_streak_for_symbol("MOCK", current_price=57.0, is_market_open=True, now_et=now_open)
    assert s == 3
    assert pytest.approx(pct, 0.01) == 14.0


def test_formatting_none_handling():
    """Ensure None values format as a neutral dash."""
    t_none = ps.format_streak(None)
    assert t_none.plain == "—"
    assert t_none.style == "gray"

    t_pct_none = ps.format_streak_pct(None)
    assert t_pct_none.plain == "—"
    assert t_pct_none.style == "gray"


@pytest.mark.anyio
async def test_tui_holdings_columns_and_row_rendering():
    """Verify DataTable columns and row formatting in AlpacaTUI."""
    from textual.widgets import DataTable
    import tui

    mock_bars = [
        {"t": "2026-09-25T04:00:00Z", "c": 100.0},
        {"t": "2026-09-28T04:00:00Z", "c": 102.0},
        {"t": "2026-09-29T04:00:00Z", "c": 105.0},
    ]
    with ps._CACHE_LOCK:
        ps._BARS_CACHE["AAPL"] = mock_bars

    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        table = app.query_one("#holdings", DataTable)
        col_labels = [str(c.label) for c in table.columns.values()]
        assert "STREAK" in col_labels
        assert "STREAK %" in col_labels

        app.market_open = True
        app._positions_cache = [
            {
                "symbol": "AAPL",
                "qty": 10,
                "avg_entry_price": 100.0,
                "current_price": 110.0,
                "market_value": 1100.0,
                "cost_basis": 1000.0,
                "unrealized_pl": 100.0,
                "unrealized_plpc": 0.1,
                "unrealized_intraday_pl": 50.0,
                "unrealized_intraday_plpc": 0.0476,
            }
        ]
        app._repopulate_table()
        row = table.get_row("AAPL")
        streak_cell = row[10]
        streak_pct_cell = row[11]
        assert streak_cell.plain == "+3"
        assert streak_cell.style == "green"
        assert streak_pct_cell.plain == "+10.0%"
        assert streak_pct_cell.style == "green"


