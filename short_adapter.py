"""
short_adapter.py — Live Paper Trading Adapter for Short Orders.

Bridges the offline-only short_selling.py domain logic to Alpaca's paper trading REST API.
Enforces:
  - Exact paper HTTPS endpoint verification
  - Easy-To-Borrow (ETB) verification via Alpaca Assets API
  - Whole-share sizing calculations via short_selling.size_short_position
  - Bracket orders with server-side buy-to-cover stop-loss and take-profit
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os
from typing import Any, Dict, Optional
import requests

from short_selling import (
    AccountInfo,
    AssetInfo,
    ExposureLimits,
    normalize_paper_base_url,
    preflight_short,
    size_short_position,
)


@dataclass(frozen=True)
class ShortOrderResult:
    submitted: bool
    order_id: Optional[str]
    symbol: str
    qty: int
    entry_price: float
    stop_price: float
    take_profit_price: float
    reasons: list[str]


class AlpacaShortAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        self.base_url = normalize_paper_base_url(base_url)
        self.headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Content-Type": "application/json",
        }

    def fetch_asset_info(self, symbol: str) -> AssetInfo:
        """Query Alpaca for symbol shortability and ETB status."""
        resp = requests.get(f"{self.base_url}/v2/assets/{symbol.upper()}", headers=self.headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return AssetInfo(
            tradable=bool(data.get("tradable", False)),
            shortable=bool(data.get("shortable", False)),
            easy_to_borrow=bool(data.get("easy_to_borrow", False)),
        )

    def fetch_account_info(self) -> AccountInfo:
        """Query Alpaca account for buying power and equity."""
        resp = requests.get(f"{self.base_url}/v2/account", headers=self.headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return AccountInfo(
            shorting_enabled=bool(data.get("shorting_enabled", False)),
            buying_power=float(data.get("buying_power", 0.0)),
            equity=float(data.get("equity", 0.0)),
        )

    def fetch_gross_short_exposure(self) -> float:
        """Calculate total current market value of all short positions."""
        resp = requests.get(f"{self.base_url}/v2/positions", headers=self.headers, timeout=10)
        resp.raise_for_status()
        positions = resp.json()
        gross_short = 0.0
        for p in positions:
            side = p.get("side", "")
            if side == "short" or float(p.get("qty", 0)) < 0:
                gross_short += abs(float(p.get("market_value", 0.0)))
        return gross_short

    def submit_bracket_short(
        self,
        symbol: str,
        entry_price: float,
        stop_price: float,
        take_profit_price: float,
        risk_per_trade_pct: float = 0.015,
        max_trade_notional_pct: float = 0.08,
        max_gross_short_pct: float = 0.25,
        dry_run: bool = False,
    ) -> ShortOrderResult:
        """
        Calculates whole-share size and submits a bracketed short entry order.
        """
        reasons = []
        sym = symbol.upper()

        # 1. Fetch live broker facts
        asset = self.fetch_asset_info(sym)
        account = self.fetch_account_info()
        current_short = self.fetch_gross_short_exposure()

        limits = ExposureLimits(
            per_trade_notional=account.equity * max_trade_notional_pct,
            gross_short_exposure=account.equity * max_gross_short_pct,
            current_gross_short_exposure=current_short,
        )

        # 2. Risk sizing (integer shares)
        risk_shares = size_short_position(
            equity=account.equity,
            risk_per_trade_pct=risk_per_trade_pct,
            entry_price=entry_price,
            stop_price=stop_price,
        )

        # Cap by per-trade notional limit
        max_by_notional = int((account.equity * max_trade_notional_pct) // entry_price)
        final_qty = max(0, min(risk_shares, max_by_notional))

        if final_qty <= 0:
            return ShortOrderResult(
                submitted=False,
                order_id=None,
                symbol=sym,
                qty=0,
                entry_price=entry_price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                reasons=[f"Risk sizing produced 0 shares (per-share risk: ${stop_price - entry_price:.2f})"],
            )

        # 3. Domain preflight check
        decision = preflight_short(
            base_url=self.base_url,
            symbol=sym,
            qty=final_qty,
            entry_price=entry_price,
            stop_price=stop_price,
            risk_per_trade_pct=risk_per_trade_pct,
            asset=asset,
            account=account,
            limits=limits,
        )

        if not decision.approved:
            return ShortOrderResult(
                submitted=False,
                order_id=None,
                symbol=sym,
                qty=final_qty,
                entry_price=entry_price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                reasons=list(decision.reasons),
            )

        if dry_run:
            return ShortOrderResult(
                submitted=True,
                order_id="DRY-RUN-SIMULATED",
                symbol=sym,
                qty=final_qty,
                entry_price=entry_price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                reasons=["Dry run approved: order not dispatched to broker"],
            )

        # 4. Dispatch bracket order to Alpaca REST API
        order_payload = {
            "symbol": sym,
            "qty": str(final_qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {
                "limit_price": str(round(take_profit_price, 2))
            },
            "stop_loss": {
                "stop_price": str(round(stop_price, 2))
            },
        }

        resp = requests.post(f"{self.base_url}/v2/orders", json=order_payload, headers=self.headers, timeout=12)
        resp.raise_for_status()
        order_data = resp.json()

        return ShortOrderResult(
            submitted=True,
            order_id=order_data.get("id"),
            symbol=sym,
            qty=final_qty,
            entry_price=entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            reasons=["Bracket short order successfully placed at Alpaca"],
        )
