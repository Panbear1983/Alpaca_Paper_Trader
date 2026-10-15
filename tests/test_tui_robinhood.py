"""The Robinhood wallet inside the real dashboard, headless and offline:
it shows the main account's holdings, and every trading key is refused."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tui  # noqa: E402
import robinhood_view as rhv  # noqa: E402
from test_robinhood_view import _good_calls, _stream  # noqa: E402


@pytest.fixture
def offline(monkeypatch):
    snap = rhv.build_snapshot(rhv.parse_stream(_stream(_good_calls())), 1.0)
    reads = []
    monkeypatch.setattr(tui.hr, "fetch_account", lambda: {"equity": "1", "last_equity": "1",
                                                           "cash": "0", "buying_power": "0"})
    monkeypatch.setattr(tui.hr, "fetch_positions", lambda: [])
    monkeypatch.setattr(tui.hr, "fetch_open_orders", lambda: [])
    monkeypatch.setattr(tui.im, "get_clock", lambda: {"is_open": False, "next_open": ""})
    monkeypatch.setattr(tui.hr, "fetch_bars_ranged", lambda s, r: [])
    monkeypatch.setattr(tui.hr, "fetch_portfolio_history_ranged", lambda r: [])
    monkeypatch.setattr(tui, "fetch_asset_name", lambda s: "")
    monkeypatch.setattr(tui.ps, "get_streak_for_symbol", lambda *a, **k: (None, None))
    monkeypatch.setattr(tui.ps, "update_bars_cache", lambda *a, **k: None)
    monkeypatch.setattr(tui.wl, "_probe_account", lambda *a: None)
    monkeypatch.setattr(rhv, "load_snapshot", lambda: snap)
    monkeypatch.setattr(rhv, "load_status", lambda: {})
    monkeypatch.setattr(rhv, "fetch_live_prices", lambda *a: {})
    monkeypatch.setattr(rhv, "history_bars", lambda *a, **k: [])
    monkeypatch.setattr(rhv, "run_reader",
                        lambda market_open=None: reads.append(market_open) or (True, "2 holdings"))

    def no_orders(*a, **k):
        raise AssertionError("an order was attempted from the Robinhood view")
    monkeypatch.setattr(tui.cc, "place_market_order", no_orders)
    return reads


async def _settle(pilot, app, cond, tries=40):
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return


@pytest.mark.anyio
async def test_robinhood_view_shows_the_main_account(offline):
    from textual.widgets import Static
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._do_switch_wallet(tui.ROBINHOOD_KEY)
        await _settle(pilot, app, lambda: app._syms)
        assert app._rh_mode is True
        assert app._syms == ["AAA", "BBB"]              # biggest gain first, like the others
        text = str(app.query_one("#summary", Static).content)
        assert "VIEW ONLY" in text and "0001" in text
        assert app._equity == pytest.approx(261.0)
        assert offline, "an out-of-date snapshot triggers a read on switching"


@pytest.mark.anyio
async def test_every_trading_key_is_refused_on_robinhood(offline):
    from textual.widgets import Input
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._do_switch_wallet(tui.ROBINHOOD_KEY)
        await _settle(pilot, app, lambda: app._syms)
        app.query_one("#holdings").focus()
        await pilot.press("a")                           # arm
        await pilot.pause()
        assert app.armed is False
        app.armed = True                                 # even if armed somehow…
        assert app._require_armed() is False             # …orders stay refused
        for key in ("b", "s", "f", "t", "c", "e", "x", "X", "C", "n", "v"):
            await pilot.press(key)
            await pilot.pause()
            assert len(app.screen_stack) == 1, f"'{key}' opened a dialog on Robinhood"
        assert not isinstance(app.focused, Input)


@pytest.mark.anyio
async def test_switching_back_returns_to_the_alpaca_wallet(offline):
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        before = tui.wl.current()
        app._do_switch_wallet(tui.ROBINHOOD_KEY)
        await _settle(pilot, app, lambda: app._syms)
        app._do_switch_wallet(before)
        await pilot.pause()
        assert app._rh_mode is False
        assert tui.wl.current() == before
        assert "Robinhood" not in app._wallet_badge()


@pytest.mark.anyio
async def test_wallet_picker_lists_robinhood_as_view_only(offline):
    from textual.widgets import DataTable
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app.push_screen(tui.WalletModal())
        await pilot.pause()
        t = app.screen.query_one("#wallettable", DataTable)
        keys = [str(r.value) for r in t.rows]
        assert keys[-1] == tui.ROBINHOOD_KEY
        row = t.get_row(tui.ROBINHOOD_KEY)
        assert row[2] == "••0001" and "view only" in str(row[3])
