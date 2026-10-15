"""Scoreboard Multiples (Shift+S) inside the real dashboard, headless and
offline: it opens, shows one column per wallet plus Robinhood, w changes the
window, trading keys do nothing while it is open, Escape closes it, and it
never starts a Robinhood read."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import i18n  # noqa: E402
import tui  # noqa: E402
import scoreboard as sb  # noqa: E402
from test_scoreboard import OCT, WB, _rh, _wallet  # noqa: E402


@pytest.fixture
def offline(monkeypatch):
    calls = {"fetch": 0, "reads": 0}
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

    def fetch(name, since):
        calls["fetch"] += 1
        time.sleep(calls.get("delay", 0))
        return _wallet(name, WB)
    monkeypatch.setattr(sb, "fetch_wallet", fetch)
    # Sessions end at the last fake close, so the top row is the same whatever today is.
    monkeypatch.setattr(sb, "fetch_calendar", lambda a, b: [s for s in OCT if s.day.day <= 12])
    monkeypatch.setattr(sb.rhv, "load_history", lambda: _rh(("2026-10-12T16:02", 100.0)))

    def no_read(*a, **k):
        calls["reads"] += 1
        return (False, "the scoreboard must not read Robinhood")
    monkeypatch.setattr(tui.rhv, "run_reader", no_read)

    def no_orders(*a, **k):
        raise AssertionError("an order was attempted from the scoreboard")
    monkeypatch.setattr(tui.cc, "place_market_order", no_orders)
    return calls


async def _settle(pilot, cond, tries=60):
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return


def _loaded(app):
    return isinstance(app.screen, tui.ScoreboardScreen) and app.screen._raw is not None


@pytest.mark.anyio
async def test_shift_s_shows_every_account_side_by_side(offline):
    from textual.widgets import DataTable
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True, size=(180, 50)) as pilot:
        await pilot.pause()
        await pilot.press("S")
        await _settle(pilot, lambda: _loaded(app))
        assert _loaded(app)
        tbl = app.screen.query_one("#sb_table", DataTable)
        labels = [str(c.label) for c in tbl.columns.values()]
        names = [w["name"] for w in tui.wl.list_wallets()]
        assert labels == [i18n.t("sb.date"), *names, i18n.t("wal.rh_name")]
        top = [str(c) for c in tbl.get_row_at(0)]
        assert top[0].startswith("10-12") and "-1.96%" in top[1]     # newest at the top
        assert offline["fetch"] == len(names) and offline["reads"] == 0


@pytest.mark.anyio
async def test_w_changes_the_window_without_refetching(offline):
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True, size=(180, 50)) as pilot:
        await pilot.pause()
        await pilot.press("S")
        await _settle(pilot, lambda: _loaded(app))
        before = offline["fetch"]
        await pilot.press("w")
        assert app.screen._window == 60
        await pilot.press("w")
        assert app.screen._window == 10
        assert offline["fetch"] == before


@pytest.mark.anyio
async def test_trading_keys_do_nothing_while_it_is_open(offline):
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True, size=(180, 50)) as pilot:
        await pilot.pause()
        await pilot.press("S")
        await _settle(pilot, lambda: _loaded(app))
        scr, lang = app.screen, i18n._lang
        for key in ("a", "b", "s", "f", "t", "c", "e", "x", "X", "C", "k", "W", "l", "n", "q"):
            await pilot.press(key)
            await pilot.pause()
            assert app.screen is scr and len(app.screen_stack) == 2, f"'{key}' got through"
        assert app.armed is False and i18n._lang == lang
        # Armed, a leaked key would open a dialog or flip a setting. Only keys
        # that are harmless if they DO leak are pressed here (no tick/run keys).
        app.armed = True
        for key in ("a", "b", "f", "W", "k", "l"):
            await pilot.press(key)
            await pilot.pause()
            assert app.screen is scr and len(app.screen_stack) == 2, f"armed '{key}' got through"
        assert app.armed is True and i18n._lang == lang
        app.armed = False
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert offline["reads"] == 0


@pytest.mark.anyio
async def test_closing_while_it_is_still_fetching_is_harmless(offline):
    offline["delay"] = 0.2
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True, size=(180, 50)) as pilot:
        await pilot.pause()
        await pilot.press("S")
        scr = app.screen
        assert isinstance(scr, tui.ScoreboardScreen)
        await pilot.press("escape")
        await pilot.pause(1.0)                 # the fetch finishes after the close
        assert len(app.screen_stack) == 1
        assert scr._raw is None                # a closed screen ignores the late result


@pytest.mark.anyio
async def test_it_follows_the_language_setting(offline, monkeypatch):
    app = tui.AlpacaTUI()
    async with app.run_test(headless=True, size=(180, 50)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(i18n, "_lang", "zh_TW")   # after mount: it loads the saved language
        await pilot.press("S")
        await _settle(pilot, lambda: _loaded(app))
        from textual.widgets import Label
        hint = str(app.screen.query_one("#sb_hint", Label).content)
        assert "多帳戶每日記分板" in hint
