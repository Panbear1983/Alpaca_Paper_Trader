"""
tests/test_short_adapter.py — Tests for the live paper trading adapter bridging short_selling.py.
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from short_adapter import AlpacaShortAdapter
from short_selling import AccountInfo, AssetInfo


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_dry_run_bracket_short(monkeypatch):
    adapter = AlpacaShortAdapter(
        base_url="https://paper-api.alpaca.markets/v2",
        api_key="TEST_KEY",
        api_secret="TEST_SECRET",
    )

    # Mock asset, account, and positions
    monkeypatch.setattr(
        adapter,
        "fetch_asset_info",
        lambda sym: AssetInfo(tradable=True, shortable=True, easy_to_borrow=True),
    )
    monkeypatch.setattr(
        adapter,
        "fetch_account_info",
        lambda: AccountInfo(shorting_enabled=True, buying_power=50000.0, equity=25000.0),
    )
    monkeypatch.setattr(adapter, "fetch_gross_short_exposure", lambda: 0.0)

    # Short AAPL at $150 with stop at $160 (risk = $10/sh) and take profit at $130
    res = adapter.submit_bracket_short(
        symbol="AAPL",
        entry_price=150.0,
        stop_price=160.0,
        take_profit_price=130.0,
        risk_per_trade_pct=0.01,  # 1% of $25,000 = $250 max loss -> 25 shares
        dry_run=True,
    )

    assert res.submitted is True
    assert res.order_id == "DRY-RUN-SIMULATED"
    assert res.qty == 13  # Capped by 8% max notional ($2,000 / $150 = 13 shares)
    assert res.symbol == "AAPL"


def test_non_etb_rejected_by_adapter(monkeypatch):
    adapter = AlpacaShortAdapter(
        base_url="https://paper-api.alpaca.markets/v2",
        api_key="TEST_KEY",
        api_secret="TEST_SECRET",
    )

    # Asset is not easy-to-borrow
    monkeypatch.setattr(
        adapter,
        "fetch_asset_info",
        lambda sym: AssetInfo(tradable=True, shortable=True, easy_to_borrow=False),
    )
    monkeypatch.setattr(
        adapter,
        "fetch_account_info",
        lambda: AccountInfo(shorting_enabled=True, buying_power=50000.0, equity=25000.0),
    )
    monkeypatch.setattr(adapter, "fetch_gross_short_exposure", lambda: 0.0)

    res = adapter.submit_bracket_short(
        symbol="GME",
        entry_price=25.0,
        stop_price=30.0,
        take_profit_price=20.0,
        dry_run=True,
    )

    assert res.submitted is False
    assert any("not easy-to-borrow" in r for r in res.reasons)
