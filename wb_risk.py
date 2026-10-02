"""Offline-only, deterministic safety domain for the authorized paper pilot.

No credentials, broker clients, file I/O, clock reads, or order submission.
All observations are injected. A successful domain check is NOT authorization
for a live adapter; regular-session/freshness/single-writer checks belong there.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re

ACCOUNT_NUMBER = "PA3BZK0WN833"
WALLET_LABEL = "Photonic CPO ETF"
PAPER_BASE_URL = "https://paper-api.alpaca.markets/v2"


@dataclass(frozen=True)
class AccountFacts:
    account_number: str
    equity: Decimal
    cash: Decimal


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{label} must be a finite number")
    text = str(value)
    if len(text) > 100 or not re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {label}") from exc
    if (not result.is_finite() or result < 0 or result > Decimal("1e18")
            or (positive and result == 0)):
        raise ValueError(f"{label} must be {'positive' if positive else 'nonnegative'} and finite")
    return result


def validate_account(base_url, account):
    """Pin exact account/HTTPS paper v2 URL and required trading flags.

    Only a single trailing slash and explicit :443 are permitted. Transfer and
    short-eligibility flags are irrelevant; all three trading-block flags must
    be explicitly False. Numeric broker strings are supported, never NaN/bools.
    Raises ValueError on any missing/malformed fact. Does not contact Alpaca.
    """
    allowed = (PAPER_BASE_URL, PAPER_BASE_URL + "/",
               "https://paper-api.alpaca.markets:443/v2",
               "https://paper-api.alpaca.markets:443/v2/")
    if not isinstance(base_url, str) or base_url not in allowed:
        raise ValueError("exact HTTPS paper v2 endpoint required")
    if not isinstance(account, Mapping):
        raise ValueError("account facts are required")
    if account.get("account_number") != ACCOUNT_NUMBER:
        raise ValueError("unauthorized account_number")
    if account.get("status") != "ACTIVE":
        raise ValueError("account must be ACTIVE")
    for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
        if account.get(flag) is not False:
            raise ValueError(f"{flag} must be explicitly False")
    return AccountFacts(ACCOUNT_NUMBER,
                        _number(account.get("equity"), "equity", positive=True),
                        _number(account.get("cash"), "cash"))


_STATE_FIELDS = {"version", "account_number", "starting_equity", "high_water_nav",
                 "nav", "units", "last_equity", "paused", "recovery_count",
                 "last_asof", "last_completed_session", "recovery_session",
                 "risk_allowed", "valuation_asof"}


def _aware_asof(asof):
    if not isinstance(asof, datetime) or asof.utcoffset() is None:
        raise ValueError("asof must be a timezone-aware datetime")
    return asof.astimezone(timezone.utc)


def _session_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("session must be an ISO trading-session date")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("invalid session date") from exc


def _state_values(state):
    if not isinstance(state, Mapping) or set(state) != _STATE_FIELDS:
        raise ValueError("complete persisted drawdown state required; never reset it")
    if type(state["version"]) is not int or state["version"] != 1:
        raise ValueError("unsupported drawdown version")
    if state["account_number"] != ACCOUNT_NUMBER:
        raise ValueError("drawdown account mismatch")
    if type(state["paused"]) is not bool:
        raise ValueError("paused must be boolean")
    if type(state["recovery_count"]) is not int or not 0 <= state["recovery_count"] <= 1:
        raise ValueError("invalid recovery count")
    values = {key: _number(state[key], key, positive=True) for key in
              ("starting_equity", "high_water_nav", "nav", "units", "last_equity")}
    if values["high_water_nav"] < max(Decimal(1), values["nav"]):
        raise ValueError("inconsistent high-water NAV")
    with localcontext() as ctx:
        ctx.prec = 50
        error = abs(values["nav"] * values["units"] - values["last_equity"])
        if error > values["last_equity"] * Decimal("1e-40"):
            raise ValueError("inconsistent persisted unit NAV")
    try:
        timestamp = _aware_asof(datetime.fromisoformat(state["last_asof"]))
        valuation_asof = _aware_asof(datetime.fromisoformat(state["valuation_asof"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid persisted asof") from exc
    if (valuation_asof > timestamp or type(state["risk_allowed"]) is not bool
            or state["risk_allowed"] != (not state["paused"])
            or (valuation_asof < timestamp and not state["paused"])):
        raise ValueError("inconsistent risk/valuation state")
    for key in ("last_completed_session", "recovery_session"):
        if state[key] is not None and _session_date(state[key]) > timestamp.date():
            raise ValueError("persisted session is in the future")
    count = state["recovery_count"]
    if count:
        if (not state["paused"] or state["recovery_session"] is None
                or state["recovery_session"] != state["last_completed_session"]
                or values["nav"] <= values["high_water_nav"] * Decimal("0.97")):
            raise ValueError("inconsistent recovery streak")
    elif state["recovery_session"] is not None:
        raise ValueError("recovery session without a streak")
    if not state["paused"] and values["nav"] <= values["high_water_nav"] * Decimal("0.95"):
        raise ValueError("drawdown breach cannot be unpaused")
    return values, timestamp


def update_drawdown(state, *, equity, asof, net_external_flow=None,
                    flow_authenticated=False, session_complete=False,
                    session=None, previous_session=None, healthy=False,
                    flow_at_observation=False):
    """Return JSON-compatible state; never clear a pause on ticks/day rollover.

    Recovery requires two unique completed sessions strictly below 3% from the
    preserved peak. Supply the calendar's immediately previous trading-session
    date to prove consecutiveness (weekends/holidays are not guessed). Missing
    predecessor starts a new one-session streak. Any observed >=3% drawdown or
    unhealthy observation interrupts recovery. Persist every returned state.

    External flows are caller-authenticated NET deposits/withdrawals since
    valuation_asof (not last_asof when a flow observation was unavailable).
    Nonzero flows require flow_at_observation=True: all flows occurred at this
    valuation boundary, after market P&L. Issue/redeem units at current preflow
    NAV; deposits cannot mask a loss. Arbitrarily timed/netted flows require
    separate properly timed observations; this domain never guesses timing.
    Dividends/costs are P&L, not external flow. Unknown flows latch a pause,
    preserve valuation/peak, and reset recovery; unknown initial flow rejects.
    Boolean evidence is an attestation by a trusted adapter, not authentication
    performed here. Errors must block risk, never trigger state reinitialization.
    """
    equity = _number(equity, "equity", positive=True)
    asof = _aware_asof(asof)
    if type(flow_authenticated) is not bool or type(flow_at_observation) is not bool:
        raise ValueError("flow evidence must be boolean")
    known_flow = flow_authenticated and net_external_flow is not None
    flow = Decimal(0)
    if net_external_flow is not None:
        # Signed quantities are accepted only for external cash flows.
        if isinstance(net_external_flow, bool):
            raise ValueError("flow must be a finite signed number")
        text = str(net_external_flow)
        negative = text.startswith("-")
        flow = _number(text[1:] if negative else net_external_flow, "net_external_flow")
        if negative:
            flow = -flow
    if known_flow and flow != 0 and not flow_at_observation:
        raise ValueError("nonzero flows require authenticated valuation-boundary timing")
    if type(session_complete) is not bool or type(healthy) is not bool:
        raise ValueError("completion and health evidence must be boolean")
    if session_complete:
        current_date = _session_date(session)
        if current_date > asof.date():
            raise ValueError("completed session cannot be in the future")
        if previous_session is not None and _session_date(previous_session) >= current_date:
            raise ValueError("previous session must precede the completed session")
    elif session is not None or previous_session is not None:
        raise ValueError("session evidence requires a completed session")
    if state is None:
        if not known_flow or flow != 0:
            raise ValueError("activation baseline requires authenticated zero external flow")
        return dict(version=1, account_number=ACCOUNT_NUMBER,
                    starting_equity=str(equity), high_water_nav="1", nav="1",
                    units=str(equity), last_equity=str(equity), paused=not healthy,
                    recovery_count=0, last_asof=asof.isoformat(),
                    last_completed_session=session, recovery_session=None,
                    risk_allowed=healthy, valuation_asof=asof.isoformat())
    values, previous_asof = _state_values(state)
    if asof <= previous_asof:
        raise ValueError("asof must strictly advance")
    last_session = state["last_completed_session"]
    if session_complete and last_session is not None and session < last_session:
        raise ValueError("completed sessions must not go backwards")
    if not known_flow:
        return dict(state, paused=True, risk_allowed=False, recovery_count=0,
                    recovery_session=None, last_asof=asof.isoformat(),
                    last_completed_session=session if session_complete else last_session)
    with localcontext() as ctx:
        ctx.prec = 50
        before_flow = equity - flow
        if before_flow <= 0:
            raise ValueError("preflow equity must be positive")
        nav = values["nav"] * before_flow / values["last_equity"]
        units = values["units"] if flow == 0 else equity / nav
        peak = max(nav, values["high_water_nav"])
        paused = state["paused"] or not healthy or nav <= peak * Decimal("0.95")
        eligible = nav > peak * Decimal("0.97") and healthy
    count, recovery_session = state["recovery_count"], state["recovery_session"]
    if not eligible or not paused:
        count, recovery_session = 0, None
    elif session_complete and session != last_session:
        consecutive = (count == 1 and previous_session == last_session
                       and recovery_session == last_session)
        count, recovery_session = (2 if consecutive else 1), session
        if count == 2:
            paused, count, recovery_session = False, 0, None
    return dict(state, nav=str(nav), high_water_nav=str(peak), last_equity=str(equity),
                paused=paused, last_asof=asof.isoformat(), recovery_count=count,
                recovery_session=recovery_session, units=str(units),
                risk_allowed=not paused, valuation_asof=asof.isoformat(),
                last_completed_session=session if session_complete else last_session)


@dataclass(frozen=True)
class OrderDecision:
    approved: bool
    reasons: tuple
    proposed_notional: Decimal | None = None
    reserved_cash: Decimal | None = None
    projected_gross: Decimal | None = None
    projected_name: Decimal | None = None


_OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held",
                  "pending_cancel", "pending_replace", "accepted_for_bidding", "stopped"}


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]+)?", value) or len(value) > 10:
        raise ValueError("valid uppercase symbol required")
    return value


def _sequence(value, label):
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"complete {label} list required")
    return value


def _order_fields(order, *, whole=False):
    if not isinstance(order, Mapping):
        raise ValueError("order facts required")
    symbol = _symbol(order.get("symbol"))
    side = order.get("side")
    if side not in ("buy", "sell"):
        raise ValueError("only long buy or reducing sell permitted")
    qty = _number(order.get("qty"), "order qty", positive=True)
    if whole and qty != qty.to_integral_value():
        raise ValueError("pilot orders require whole shares")
    if order.get("notional") is not None:
        raise ValueError("notional orders unsupported")
    kind = order.get("type")
    if kind not in ("market", "limit", "stop", "stop_limit"):
        raise ValueError("unsupported order type")
    prices = []
    if kind in ("limit", "stop_limit"):
        prices.append(_number(order.get("limit_price"), "limit_price", positive=True))
    if kind in ("stop", "stop_limit"):
        prices.append(_number(order.get("stop_price"), "stop_price", positive=True))
    return symbol, side, qty, prices


def _quote(quotes, symbol):
    if not isinstance(quotes, Mapping) or not isinstance(quotes.get(symbol), Mapping):
        raise ValueError(f"quote required for {symbol}")
    quote = quotes[symbol]
    ask = _number(quote.get("ask"), "ask", positive=True)
    bid = _number(quote.get("bid"), "bid", positive=True)
    if bid > ask:
        raise ValueError("crossed quote rejected")
    return ask


def _positions(positions):
    book = {}
    for item in _sequence(positions, "positions"):
        if not isinstance(item, Mapping):
            raise ValueError("position facts required")
        symbol = _symbol(item.get("symbol"))
        if symbol in book:
            raise ValueError("duplicate position symbol")
        side = item.get("side")
        raw_qty = item.get("qty")
        if side == "short":
            if not isinstance(raw_qty, (str, int, float, Decimal)) or not str(raw_qty).startswith("-"):
                raise ValueError("short position requires signed negative qty")
            qty = _number(str(raw_qty)[1:], "short qty", positive=True)
        elif side == "long":
            qty = _number(raw_qty, "position qty", positive=True)
        else:
            raise ValueError("position side must be long or short")
        available = qty
        if "qty_available" in item:
            available = _number(item["qty_available"], "qty_available")
            if available > qty:
                raise ValueError("qty_available exceeds actual shares")
        reported = None
        if "market_value" in item:
            raw_value = item["market_value"]
            if side == "short" and str(raw_value).startswith("-"):
                raw_value = str(raw_value)[1:]
            reported = _number(raw_value, "market_value")
        current_price = None
        if "current_price" in item:
            current_price = _number(item["current_price"], "current_price", positive=True)
        book[symbol] = (side, qty, available, reported, current_price)
    return book


def _pending_row(item, seen):
    symbol, side, qty, prices = _order_fields(item)
    identity = item.get("id")
    if not isinstance(identity, str) or not identity or identity in seen:
        raise ValueError("unique pending order id required (no flattened/nested duplicates)")
    seen.add(identity)
    filled = _number(item.get("filled_qty"), "filled_qty")
    if filled > qty:
        raise ValueError("filled_qty exceeds order qty")
    status = item.get("status")
    if (not isinstance(status, str) or
            (status not in _OPEN_STATUSES and not (status == "filled" and filled == qty))):
        raise ValueError("unknown/inconsistent pending status")
    return symbol, side, qty, prices, qty - filled


def _pending_totals(pending_orders, quotes, *, buying):
    buys, sells, unconditional_sells, seen = {}, {}, {}, set()
    for item in _sequence(pending_orders, "pending orders"):
        symbol, side, qty, prices, remaining = _pending_row(item, seen)
        kind = item.get("order_class", "simple")
        legs = item.get("legs")
        if kind in ("simple", ""):
            if legs not in (None, []):
                raise ValueError("simple order has ambiguous legs")
            if side == "sell":
                sells[symbol] = sells.get(symbol, Decimal(0)) + remaining
                unconditional_sells[symbol] = unconditional_sells.get(symbol, Decimal(0)) + remaining
        elif kind in ("bracket", "oco"):
            legs = _sequence(legs, "contingent legs")
            expected = 2 if kind == "bracket" else 1
            if (len(legs) != expected or (kind == "bracket" and side != "buy")
                    or (kind == "oco" and (side != "sell" or item["type"] != "limit"))):
                raise ValueError("ambiguous bracket/OCO parent")
            exit_rows = []
            types = []
            for leg in legs:
                leg_symbol, leg_side, leg_qty, _, leg_remaining = _pending_row(leg, seen)
                if (leg_symbol != symbol or leg_side != "sell" or leg_qty != qty
                        or leg.get("legs") not in (None, [])
                        or leg.get("order_class", "simple") not in ("simple", "", kind)):
                    raise ValueError("ambiguous bracket/OCO child")
                types.append(leg["type"])
                exit_rows.append(leg_remaining)
            if kind == "bracket":
                if sorted(types) not in (["limit", "stop"], ["limit", "stop_limit"]):
                    raise ValueError("bracket needs one take-profit and one protective stop")
            elif types[0] not in ("stop", "stop_limit"):
                raise ValueError("OCO needs a protective stop child")
            reserved = max(exit_rows + ([remaining] if kind == "oco" else []))
            sells[symbol] = sells.get(symbol, Decimal(0)) + reserved
            if kind == "oco":
                unconditional_sells[symbol] = unconditional_sells.get(symbol, Decimal(0)) + reserved
        else:
            raise ValueError("unsupported/ambiguous pending order class")
        if side == "buy" and buying and remaining:
            price = max([_quote(quotes, symbol)] + prices)
            buys[symbol] = buys.get(symbol, Decimal(0)) + remaining * price
    return buys, sells, unconditional_sells


def check_order(*, base_url, account, order, positions, pending_orders, quotes,
                drawdown_state=None, asof=None):
    """Evaluate injected facts and return OrderDecision; rejection never executes.

    Broker-shaped mappings, complete position/open-order lists, and conservative
    bid/ask quotes are required. Proposed orders must be simple whole-share
    buy/sell intents. Buys require a current healthy drawdown observation matching
    asof and account equity. Cash, name <=20%, and long gross <=equity include
    remaining pending buys. Pending sells never free buying capacity.
    """
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            facts = validate_account(base_url, account)
            symbol, side, qty, prices = _order_fields(order, whole=True)
            if order.get("order_class", "simple") not in ("simple", "") or order.get("legs") not in (None, []):
                raise ValueError("proposed contingent orders unsupported")
            book = _positions(positions)
            buys, sells, unconditional_sells = _pending_totals(pending_orders, quotes, buying=side == "buy")
            if side == "sell":
                holding = book.get(symbol)
                if holding is None or holding[0] != "long":
                    raise ValueError("sell requires actual current long shares; never open shorts")
                available = min(holding[2], holding[1] - sells.get(symbol, Decimal(0)))
                if qty > available:
                    raise ValueError("sell exceeds actual long shares minus pending sells")
                return OrderDecision(True, ("safe long reduction",))
            if any(item[0] == "short" for item in book.values()):
                raise ValueError("existing short positions block new purchases")
            values, observed_asof = _state_values(drawdown_state)
            if drawdown_state["paused"] or not drawdown_state["risk_allowed"]:
                raise ValueError("drawdown/uncertainty paused new purchases")
            if _aware_asof(asof) != observed_asof:
                raise ValueError("drawdown asof must match current observation")
            if values["last_equity"] != facts.equity:
                raise ValueError("drawdown equity must match account equity")
            for name, reserved in unconditional_sells.items():
                if reserved > book.get(name, (None, Decimal(0)))[1]:
                    raise ValueError("pending sells could open a short")
            exposures = dict(buys)
            for name, (_, shares, _, reported, current_price) in book.items():
                value = shares * _quote(quotes, name)
                if reported is not None:
                    value = max(value, reported)
                if current_price is not None:
                    value = max(value, shares * current_price)
                exposures[name] = exposures.get(name, Decimal(0)) + value
            notional = qty * max([_quote(quotes, symbol)] + prices)
            reserved_cash = sum(buys.values(), Decimal(0))
            gross = sum(exposures.values(), Decimal(0)) + notional
            name = exposures.get(symbol, Decimal(0)) + notional
            reasons = []
            if reserved_cash + notional > facts.cash:
                reasons.append("available cash after pending buys is insufficient")
            if gross > facts.equity:
                reasons.append("projected long gross exceeds equity")
            if name > facts.equity * Decimal("0.20"):
                reasons.append("projected name exceeds 20% of equity")
            return OrderDecision(not reasons, tuple(reasons), notional, reserved_cash, gross, name)
    except ValueError as exc:
        return OrderDecision(False, (str(exc),))
