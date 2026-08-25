"""Offline-only short-selling validation, sizing, approval, and intent models."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
import hashlib
import hmac
import math
import re
from typing import Optional, Tuple
from urllib.parse import urlsplit


PAPER_HOSTNAME = "paper-api.alpaca.markets"
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?$")
_MAX_SYMBOL_LENGTH = 10


@dataclass(frozen=True)
class AssetInfo:
    tradable: bool
    shortable: bool
    easy_to_borrow: bool


@dataclass(frozen=True)
class AccountInfo:
    shorting_enabled: bool
    buying_power: float
    equity: float


@dataclass(frozen=True)
class ExposureLimits:
    per_trade_notional: float
    gross_short_exposure: float
    current_gross_short_exposure: float


@dataclass(frozen=True)
class PreflightDecision:
    approved: bool
    reasons: Tuple[str, ...]
    normalized_symbol: Optional[str]
    normalized_qty: Optional[int]
    proposed_notional: Optional[float]
    risk_sized_max_qty: Optional[int]


class PreflightRejected(ValueError):
    def __init__(self, decision: PreflightDecision):
        self.decision = decision
        super().__init__("short preflight rejected: " + "; ".join(decision.reasons))


@dataclass(frozen=True)
class OrderIntent:
    action: str
    side: str
    symbol: str
    qty: int
    order_type: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    execute: bool = False


@dataclass(frozen=True)
class CloseAction:
    action: str
    symbol: str
    qty: int
    execute: bool = False


@dataclass(frozen=True)
class AuditEvent:
    decision: str
    reasons: Tuple[str, ...]
    symbol: str
    qty: int
    paper_only: bool


@dataclass(frozen=True)
class ShortPlan:
    base_url: str
    paper_only: bool
    symbol: str
    qty: int
    entry: OrderIntent
    protective_stop: OrderIntent
    close_action: CloseAction
    audit_event: AuditEvent


class ApprovalError(ValueError):
    pass


@dataclass(frozen=True)
class ApprovalReceipt:
    approved: bool
    symbol: str
    qty: int
    paper_only: bool
    expires_at: datetime


def _valid_symbol(symbol: object) -> bool:
    return (
        isinstance(symbol, str)
        and len(symbol) <= _MAX_SYMBOL_LENGTH
        and _SYMBOL_PATTERN.fullmatch(symbol) is not None
    )


def _valid_qty(qty: object) -> bool:
    return isinstance(qty, int) and not isinstance(qty, bool) and qty > 0


def _positive_finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _nonnegative_finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def normalize_paper_base_url(base_url: object) -> str:
    """Return a normalized paper URL or reject anything outside the paper host."""
    if not isinstance(base_url, str):
        raise ValueError("paper endpoint must be an HTTPS URL on the exact paper hostname")

    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError(
            "paper endpoint must be an HTTPS URL on the exact paper hostname"
        ) from error

    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != PAPER_HOSTNAME
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("paper endpoint must be HTTPS on exactly paper-api.alpaca.markets")

    path = parsed.path.rstrip("/")
    port_suffix = ":443" if port == 443 else ""
    return f"https://{PAPER_HOSTNAME}{port_suffix}{path}"


def size_short_position(
    *,
    equity: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_price: float,
) -> int:
    """Calculate the maximum whole-share size allowed by the risk budget."""
    if not _positive_finite(equity):
        raise ValueError("equity must be positive and finite")
    if not _positive_finite(risk_per_trade_pct) or risk_per_trade_pct > 1:
        raise ValueError("risk_per_trade_pct must be positive, finite, and no greater than 1")
    if not _positive_finite(entry_price):
        raise ValueError("entry_price must be positive and finite")
    if not _positive_finite(stop_price):
        raise ValueError("stop_price must be positive and finite")
    if stop_price <= entry_price:
        raise ValueError("a protective short stop must be above entry_price")

    equity_decimal = Decimal(str(equity))
    risk_decimal = Decimal(str(risk_per_trade_pct))
    per_share_risk = Decimal(str(stop_price)) - Decimal(str(entry_price))
    maximum = (equity_decimal * risk_decimal / per_share_risk).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return int(maximum)


def preflight_short(
    *,
    base_url: str,
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    risk_per_trade_pct: float,
    asset: AssetInfo,
    account: AccountInfo,
    limits: ExposureLimits,
    broker_locate_evidence: Optional[str] = None,
) -> PreflightDecision:
    """Evaluate a proposed short without contacting a broker or executing an order."""
    reasons = []
    normalized_symbol = symbol if _valid_symbol(symbol) else None
    normalized_qty = qty if _valid_qty(qty) else None
    proposed_notional = None
    risk_sized_max_qty = None

    try:
        normalize_paper_base_url(base_url)
    except ValueError as error:
        reasons.append(str(error))

    if normalized_symbol is None:
        reasons.append("symbol must be a valid uppercase ticker of at most 10 characters")
    if normalized_qty is None:
        reasons.append("qty must be a whole positive share quantity")
    if not _positive_finite(entry_price):
        reasons.append("entry_price must be positive and finite")
    if not _positive_finite(stop_price):
        reasons.append("stop_price must be positive and finite")
    elif _positive_finite(entry_price) and stop_price <= entry_price:
        reasons.append("a protective short stop must be above entry_price")
    if not _positive_finite(risk_per_trade_pct) or (
        _positive_finite(risk_per_trade_pct) and risk_per_trade_pct > 1
    ):
        reasons.append(
            "risk_per_trade_pct must be positive, finite, and no greater than 1"
        )

    if not isinstance(asset, AssetInfo):
        reasons.append("asset facts are required")
    else:
        if asset.tradable is not True:
            reasons.append("asset is not tradable")
        if asset.shortable is not True:
            reasons.append("asset is not shortable")
        if asset.easy_to_borrow is not True:
            if broker_locate_evidence:
                reasons.append(
                    "asset is not easy-to-borrow; this offline module cannot validate "
                    "explicit broker locate evidence and locate availability is unavailable, "
                    "so non-ETB shorts are rejected"
                )
            else:
                reasons.append(
                    "asset is not easy-to-borrow; explicit broker locate evidence is required, "
                    "but locate availability is unavailable in this offline module, so non-ETB "
                    "shorts are rejected"
                )

    account_is_valid = isinstance(account, AccountInfo)
    if not account_is_valid:
        reasons.append("account facts are required")
    else:
        if account.shorting_enabled is not True:
            reasons.append("account shorting is not enabled")
        if not _nonnegative_finite(account.buying_power):
            reasons.append("account buying_power must be nonnegative and finite")
        if not _positive_finite(account.equity):
            reasons.append("account equity must be positive and finite")

    limits_are_valid = isinstance(limits, ExposureLimits)
    if not limits_are_valid:
        reasons.append("short-exposure limits are required")
    else:
        if not _positive_finite(limits.per_trade_notional):
            reasons.append("per-trade short-exposure limit must be positive and finite")
        if not _positive_finite(limits.gross_short_exposure):
            reasons.append("gross short-exposure limit must be positive and finite")
        if not _nonnegative_finite(limits.current_gross_short_exposure):
            reasons.append("current gross short exposure must be nonnegative and finite")

    if normalized_qty is not None and _positive_finite(entry_price):
        proposed_notional = float(Decimal(normalized_qty) * Decimal(str(entry_price)))

        if account_is_valid and _nonnegative_finite(account.buying_power):
            if proposed_notional > account.buying_power:
                reasons.append("account buying power is insufficient for the proposed short")

        if limits_are_valid:
            if (
                _positive_finite(limits.per_trade_notional)
                and proposed_notional > limits.per_trade_notional
            ):
                reasons.append("proposed short exceeds the per-trade short-exposure limit")
            if (
                _positive_finite(limits.gross_short_exposure)
                and _nonnegative_finite(limits.current_gross_short_exposure)
                and proposed_notional + limits.current_gross_short_exposure
                > limits.gross_short_exposure
            ):
                reasons.append("proposed short exceeds the gross short-exposure limit")

    if (
        account_is_valid
        and _positive_finite(account.equity)
        and _positive_finite(risk_per_trade_pct)
        and risk_per_trade_pct <= 1
        and _positive_finite(entry_price)
        and _positive_finite(stop_price)
        and stop_price > entry_price
    ):
        risk_sized_max_qty = size_short_position(
            equity=account.equity,
            risk_per_trade_pct=risk_per_trade_pct,
            entry_price=entry_price,
            stop_price=stop_price,
        )
        if normalized_qty is not None and normalized_qty > risk_sized_max_qty:
            reasons.append("qty exceeds the risk-sized maximum whole-share quantity")

    if reasons:
        return PreflightDecision(
            approved=False,
            reasons=tuple(reasons),
            normalized_symbol=normalized_symbol,
            normalized_qty=normalized_qty,
            proposed_notional=proposed_notional,
            risk_sized_max_qty=risk_sized_max_qty,
        )

    return PreflightDecision(
        approved=True,
        reasons=(
            "paper endpoint verified",
            "all normalized short preflight checks passed",
        ),
        normalized_symbol=normalized_symbol,
        normalized_qty=normalized_qty,
        proposed_notional=proposed_notional,
        risk_sized_max_qty=risk_sized_max_qty,
    )


def build_short_plan(
    *,
    base_url: str,
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    risk_per_trade_pct: float,
    asset: AssetInfo,
    account: AccountInfo,
    limits: ExposureLimits,
    broker_locate_evidence: Optional[str] = None,
) -> ShortPlan:
    """Build non-executable entry, protective-stop, and close intents."""
    decision = preflight_short(
        base_url=base_url,
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        stop_price=stop_price,
        risk_per_trade_pct=risk_per_trade_pct,
        asset=asset,
        account=account,
        limits=limits,
        broker_locate_evidence=broker_locate_evidence,
    )
    if not decision.approved:
        raise PreflightRejected(decision)

    normalized_base_url = normalize_paper_base_url(base_url)
    assert decision.normalized_symbol is not None
    assert decision.normalized_qty is not None

    entry = OrderIntent(
        action="sell_short",
        side="sell",
        symbol=decision.normalized_symbol,
        qty=decision.normalized_qty,
        order_type="limit",
        limit_price=float(entry_price),
    )
    protective_stop = OrderIntent(
        action="buy_to_cover",
        side="buy",
        symbol=decision.normalized_symbol,
        qty=decision.normalized_qty,
        order_type="stop",
        stop_price=float(stop_price),
    )
    close_action = CloseAction(
        action="buy_to_cover",
        symbol=decision.normalized_symbol,
        qty=decision.normalized_qty,
    )
    audit_event = AuditEvent(
        decision="approved",
        reasons=decision.reasons,
        symbol=decision.normalized_symbol,
        qty=decision.normalized_qty,
        paper_only=True,
    )
    return ShortPlan(
        base_url=normalized_base_url,
        paper_only=True,
        symbol=decision.normalized_symbol,
        qty=decision.normalized_qty,
        entry=entry,
        protective_stop=protective_stop,
        close_action=close_action,
        audit_event=audit_event,
    )


class ApprovalGate:
    """Deterministic, expiring, single-use approval for a paper short intent."""

    def __init__(
        self,
        *,
        symbol: str,
        qty: int,
        token: str,
        expires_at: datetime,
    ) -> None:
        if not _valid_symbol(symbol):
            raise ValueError("symbol must be a valid uppercase ticker")
        if not _valid_qty(qty):
            raise ValueError("qty must be a whole positive share quantity")
        if not isinstance(token, str) or not token:
            raise ValueError("approval token must be a nonempty string")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("expires_at must be a timezone-aware datetime")

        self._symbol = symbol
        self._qty = qty
        self._token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        self._expires_at = expires_at
        self._used = False

    def approve(
        self,
        *,
        phrase: str,
        token: str,
        now: datetime,
        paper_only: bool,
    ) -> ApprovalReceipt:
        if self._used:
            raise ApprovalError("approval token is already used")

        expected_phrase = f"PAPER-SHORT {self._symbol} {self._qty}"
        if phrase != expected_phrase:
            raise ApprovalError(f"approval requires exact phrase: {expected_phrase}")
        if paper_only is not True:
            raise ApprovalError("approval gate is paper-only")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ApprovalError("now must be a timezone-aware datetime")
        if now >= self._expires_at:
            raise ApprovalError("approval token has expired")
        if not isinstance(token, str):
            raise ApprovalError("approval token is invalid")

        candidate_digest = hashlib.sha256(token.encode("utf-8")).digest()
        if not hmac.compare_digest(candidate_digest, self._token_digest):
            raise ApprovalError("approval token is invalid")

        self._used = True
        self._token_digest = b""
        return ApprovalReceipt(
            approved=True,
            symbol=self._symbol,
            qty=self._qty,
            paper_only=True,
            expires_at=self._expires_at,
        )
