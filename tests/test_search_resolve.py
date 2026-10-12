"""Search-bar resolution: ticker OR company name → symbol. No network.

The directory is a small fake shaped like Alpaca's /assets rows, chosen to
contain the traps that matter: a leveraged fund whose name contains the
company ("NVIDIA"), a REIT whose name starts the same way as the company
("Apple"), and an ADR whose ticker shares nothing with its name ("TSM").
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tui  # noqa: E402

RAW = [
    ("NVDA", "NVIDIA Corporation Common Stock"),
    ("NVDX", "T-Rex 2X Long NVIDIA Daily Target ETF"),
    ("NVDQ", "T-Rex 2X Inverse NVIDIA Daily Target ETF"),
    ("AAPL", "Apple Inc. Common Stock"),
    ("APLE", "Apple Hospitality REIT, Inc. Common Shares"),
    ("TSM", "Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares"),
    ("TSLA", "Tesla, Inc. Common Stock"),
    ("HOOD", "Robinhood Markets, Inc. Class A Common Stock"),
    ("MSFT", "Microsoft Corporation Common Stock"),
]
ROWS = [(s, tui._clean_asset_name(n), n) for s, n in RAW]


@pytest.fixture
def directory(monkeypatch):
    monkeypatch.setattr(tui, "_asset_directory", lambda force=False: ROWS)
    monkeypatch.setitem(tui._ASSET_DIR, "rows", ROWS)
    monkeypatch.setitem(tui._ASSET_DIR, "t", 10**12)


@pytest.fixture
def no_directory(monkeypatch):
    monkeypatch.setattr(tui, "_asset_directory", lambda force=False: [])
    monkeypatch.setitem(tui._ASSET_DIR, "rows", [])


# ── ranking ─────────────────────────────────────────────────────────────────

def test_company_beats_its_own_leveraged_funds():
    assert tui.rank_name_matches("nvidia", ROWS)[0][0] == "NVDA"


def test_shorter_exact_prefix_beats_longer_prefix():
    """'Apple' → Apple Inc, not Apple Hospitality REIT."""
    assert tui.rank_name_matches("apple", ROWS)[0][0] == "AAPL"


def test_multi_word_query_matches_all_words():
    assert tui.rank_name_matches("taiwan semiconductor", ROWS)[0][0] == "TSM"


def test_word_order_and_case_do_not_matter():
    assert tui.rank_name_matches("SEMICONDUCTOR taiwan", ROWS)[0][0] == "TSM"


def test_nonsense_matches_nothing():
    assert tui.rank_name_matches("zzqx corporation of mars", ROWS) == []


def test_alternatives_follow_the_winner():
    syms = [s for s, _ in tui.rank_name_matches("nvidia", ROWS)]
    assert syms[0] == "NVDA" and set(syms[1:]) == {"NVDX", "NVDQ"}


# ── resolution ──────────────────────────────────────────────────────────────

def test_plain_ticker_resolves_without_the_directory(no_directory):
    """First search of a session must not wait 2s for the asset list."""
    assert tui.resolve_search("nvda") == ("NVDA", "", [])


def test_plain_ticker_is_verified_when_the_directory_is_loaded(directory):
    sym, name, alts = tui.resolve_search("NVDA")
    assert (sym, name, alts) == ("NVDA", "NVIDIA Corporation", [])


def test_company_name_resolves_to_its_ticker(directory):
    sym, name, alts = tui.resolve_search("nvidia")
    assert sym == "NVDA" and name == "NVIDIA Corporation"
    assert [a for a, _ in alts] == ["NVDX", "NVDQ"]


def test_two_word_company_name(directory):
    assert tui.resolve_search("Taiwan Semiconductor")[0] == "TSM"


def test_ticker_shaped_word_that_is_a_company_name(directory):
    """'TESLA' looks like a ticker but isn't one — falls through to names."""
    assert tui.resolve_search("TESLA")[0] == "TSLA"


def test_unknown_name_is_empty(directory):
    assert tui.resolve_search("Acme Rocket Skates") == ("", "", [])


def test_blank_is_empty(directory):
    assert tui.resolve_search("   ") == ("", "", [])


# ── brand nicknames ─────────────────────────────────────────────────────────

def test_brand_nickname_beats_the_name_search(directory):
    """'spacex' is not in any legal name; the alias table knows it is SPCX."""
    assert tui.resolve_search("SpaceX")[0] == "SPCX"
    assert tui.resolve_search("space x")[0] == "SPCX"


def test_alias_punctuation_and_case_are_ignored(directory):
    assert tui.resolve_search("Coca-Cola")[0] == "KO"
    assert tui.resolve_search("coca cola")[0] == "KO"


def test_a_real_ticker_still_wins_over_an_alias(directory):
    """META is both a brand nickname and a ticker; the ticker is the answer."""
    assert tui.resolve_search("HOOD") == ("HOOD", "Robinhood Markets, Inc.", [])


def test_brand_alias_works_before_the_directory_has_loaded(no_directory):
    """The live bug: 'SpaceX' is ticker-shaped and was trusted as 'SPACEX'."""
    assert tui.resolve_search("SpaceX")[0] == "SPCX"
    assert tui.resolve_search("google")[0] == "GOOGL"
    assert tui.resolve_search("nvda") == ("NVDA", "", [])   # real ticker still instant


def test_known_unlisted_brand_is_a_clean_no_match(directory):
    assert tui.resolve_search("OpenAI") == ("", "", [])


# ── through the real app, headless ──────────────────────────────────────────

@pytest.mark.anyio
async def test_typing_a_company_name_charts_its_ticker(monkeypatch, directory):
    from textual.widgets import Input
    monkeypatch.setattr(tui.hr, "fetch_account", lambda: {"equity": "1", "last_equity": "1",
                                                           "cash": "0", "buying_power": "0"})
    monkeypatch.setattr(tui.hr, "fetch_positions", lambda: [])
    monkeypatch.setattr(tui.hr, "fetch_open_orders", lambda: [])
    monkeypatch.setattr(tui.im, "get_clock", lambda: {"is_open": False, "next_open": ""})
    monkeypatch.setattr(tui.hr, "fetch_bars_ranged", lambda s, r: [])
    monkeypatch.setattr(tui.hr, "fetch_portfolio_history_ranged", lambda r: [])
    monkeypatch.setattr(tui, "fetch_asset_name", lambda s: "")
    monkeypatch.setattr(tui, "fetch_company_bio", lambda s: ("NVIDIA", "chips"))
    monkeypatch.setattr(tui, "fetch_ticker_perf", lambda s: {})
    monkeypatch.setattr(tui, "latest_price", lambda s: 0.0)

    app = tui.AlpacaTUI()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        box = app.query_one("#ticker_search", Input)
        box.value = "nvidia"
        await box.action_submit()
        for _ in range(20):                      # resolver + bio run in threads
            await pilot.pause(0.05)
            if app._search_sym:
                break
        assert app._search_sym == "NVDA"
        assert app._chart_subject() == "NVDA"
        assert [a for a, _ in app._search_alts] == ["NVDX", "NVDQ"]
