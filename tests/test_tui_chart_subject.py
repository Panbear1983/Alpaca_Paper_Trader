"""Chart subject rules — what the candlestick chart shows and when it changes.

Pure-rule tests need no Textual. The headless test drives the real app with
every network fetch replaced, and pins the two behaviours Peter asked for:
a searched ticker gets charted and SURVIVES the 8s refresh, and Escape on the
holdings table charts the whole wallet.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tui  # noqa: E402

P = tui.PORTFOLIO_SUBJECT


# ── the pure rule ───────────────────────────────────────────────────────────

def test_portfolio_mode_wins_over_everything():
    assert tui._resolve_chart_subject(True, "NVDA", "AAPL") == P
    assert tui._resolve_chart_subject(True, "", "") == P


def test_searched_ticker_beats_the_highlighted_holding():
    assert tui._resolve_chart_subject(False, "NVDA", "AAPL") == "NVDA"


def test_no_search_falls_through_to_the_holding():
    assert tui._resolve_chart_subject(False, "", "AAPL") == "AAPL"


def test_nothing_at_all_is_empty_so_the_chart_shows_the_hint():
    assert tui._resolve_chart_subject(False, "", "") == ""


# ── the real app, headless, no network ──────────────────────────────────────

POS = [{"symbol": "AAPL", "qty": 10, "avg_entry_price": 100.0, "current_price": 110.0,
        "market_value": 1100.0, "cost_basis": 1000.0, "unrealized_pl": 100.0,
        "unrealized_plpc": 0.1, "unrealized_intraday_pl": 5.0, "unrealized_intraday_plpc": 0.005},
       {"symbol": "TSLA", "qty": 1, "avg_entry_price": 300.0, "current_price": 290.0,
        "market_value": 290.0, "cost_basis": 300.0, "unrealized_pl": -10.0,
        "unrealized_plpc": -0.033, "unrealized_intraday_pl": -1.0, "unrealized_intraday_plpc": -0.003}]


@pytest.fixture
def offline(monkeypatch):
    """Every fetch the dashboard makes, answered locally. Records chart fetches."""
    fetched: list[str] = []
    monkeypatch.setattr(tui.hr, "fetch_account", lambda: {"equity": "1390", "last_equity": "1380",
                                                           "cash": "0", "buying_power": "0"})
    monkeypatch.setattr(tui.hr, "fetch_positions", lambda: list(POS))
    monkeypatch.setattr(tui.hr, "fetch_open_orders", lambda: [])
    monkeypatch.setattr(tui.im, "get_clock", lambda: {"is_open": False, "next_open": ""})
    monkeypatch.setattr(tui.hr, "fetch_bars_ranged",
                        lambda sym, rng: fetched.append(sym) or
                        [{"t": "2026-10-01T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2}])
    monkeypatch.setattr(tui.hr, "fetch_portfolio_history_ranged",
                        lambda rng: fetched.append(P) or
                        [{"t": "2026-10-01T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2}])
    monkeypatch.setattr(tui, "fetch_asset_name", lambda sym: "")
    monkeypatch.setattr(tui, "fetch_company_bio", lambda sym: ("NVIDIA", "chips"))
    monkeypatch.setattr(tui, "fetch_ticker_perf", lambda sym: {})
    monkeypatch.setattr(tui, "latest_price", lambda sym: 0.0)
    monkeypatch.setattr(tui.ps, "get_streak_for_symbol", lambda *a, **k: (None, None))
    return fetched


@pytest.mark.anyio
async def test_search_charts_the_ticker_and_survives_a_refresh(offline):
    from textual.widgets import Input
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._positions_cache = list(POS)
        app._repopulate_table()
        await pilot.pause()
        assert app._sel_sym == "AAPL"            # boot default: first row

        box = app.query_one("#ticker_search", Input)
        box.value = "nvda"
        await box.action_submit()
        await pilot.pause()
        assert app._search_sym == "NVDA"
        assert app._chart_subject() == "NVDA"

        # the 8-second refresh re-fills the table; the searched ticker must stay
        app._repopulate_table()
        await pilot.pause()
        assert app._chart_subject() == "NVDA"
        assert app._sel_sym == "AAPL"            # cursor row untouched too


@pytest.mark.anyio
async def test_escape_on_holdings_charts_the_whole_wallet(offline):
    from textual.widgets import DataTable, Input, Static
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._positions_cache = list(POS)
        app._repopulate_table()
        box = app.query_one("#ticker_search", Input)
        box.value = "NVDA"
        await box.action_submit()
        await pilot.pause()
        app._bio_sym = "NVDA"                    # as _lookup_bio would have set

        app.query_one("#holdings", DataTable).focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app._chart_subject() == P
        assert app._portfolio_mode is True
        assert app._search_sym == "" and app._bio_sym == ""
        bio = app.query_one("#bio", Static)
        assert str(getattr(bio, "content", getattr(bio, "renderable", ""))) == ""


@pytest.mark.anyio
async def test_moving_the_cursor_picks_that_holding_back(offline):
    from textual.widgets import DataTable
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._positions_cache = list(POS)
        app._repopulate_table()
        app._portfolio_mode = True
        app._search_sym = "NVDA"
        t = app.query_one("#holdings", DataTable)
        t.focus()
        await pilot.pause()
        await pilot.press("down")                # a REAL cursor move
        await pilot.pause()
        assert app._portfolio_mode is False
        assert app._search_sym == ""
        assert app._chart_subject() == app._sel_sym == "TSLA"


@pytest.mark.anyio
async def test_escape_inside_the_search_box_only_moves_focus(offline):
    from textual.widgets import DataTable, Input
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._positions_cache = list(POS)
        app._repopulate_table()
        app._search_sym = "NVDA"
        app.query_one("#ticker_search", Input).focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is app.query_one("#holdings", DataTable)
        assert app._search_sym == "NVDA"         # first Esc is just "leave the box"
        assert app._portfolio_mode is False
