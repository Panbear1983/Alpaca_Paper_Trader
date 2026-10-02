"""Offline contract tests for the bounded Photonic CPO ETF pilot."""
import copy
import importlib
import importlib.util
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
URL = "https://paper-api.alpaca.markets/v2"
ASOF = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)


def risk():
    assert importlib.util.find_spec("wb_risk") is not None, "safety domain is missing"
    return importlib.import_module("wb_risk")


def account(**overrides):
    return dict(account_number="PA3BZK0WN833", status="ACTIVE",
                trading_blocked=False, account_blocked=False,
                trade_suspended_by_user=False, equity="1000", cash="1000") | overrides


def test_validate_authorized_paper_account():
    facts = risk().validate_account(URL, account())
    assert facts.equity == Decimal("1000")
    assert facts.cash == Decimal("1000")
    assert facts.account_number == "PA3BZK0WN833"


@pytest.mark.parametrize("url", [URL + "/", URL.replace("/v2", ":443/v2")])
def test_explicit_https_port_and_one_trailing_slash_allowed(url):
    assert risk().validate_account(url, account()).cash == 1000


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets/v2", "http://paper-api.alpaca.markets/v2",
    URL.replace("/v2", ":444/v2"), URL.replace("/v2", ":0443/v2"),
    URL.replace("paper-api", "user:pass@paper-api"), URL + "?x=1", URL + "?",
    URL + "#x", URL + "#", URL + "//", URL + "/orders", URL[:-3],
    URL.replace("/v2", "/v1"), URL.replace(".markets", ".markets.evil"),
    URL.replace("/v2", "/%76%32"), " " + URL, URL + "\n", None,
])
def test_endpoint_fails_closed(url):
    with pytest.raises(ValueError):
        risk().validate_account(url, account())


@pytest.mark.parametrize("field,value", [
    ("account_number", "other"), ("account_number", "PA3BZK0WN833 "),
    ("account_number", None), ("status", "INACTIVE"),
    ("trading_blocked", True), ("account_blocked", True),
    ("trade_suspended_by_user", True), ("trading_blocked", "false"),
    ("equity", "0"), ("equity", "-1"), ("equity", "NaN"),
    ("equity", float("inf")), ("equity", True), ("equity", None),
    ("cash", "-0.01"), ("cash", "NaN"), ("cash", "Infinity"),
    ("cash", False), ("cash", "oops"),
])
def test_account_facts_fail_closed(field, value):
    with pytest.raises(ValueError):
        risk().validate_account(URL, account(**{field: value}))


@pytest.mark.parametrize("field", ["account_number", "status", "trading_blocked",
                                   "account_blocked", "trade_suspended_by_user",
                                   "equity", "cash"])
def test_missing_required_account_facts_rejected(field):
    payload = account()
    del payload[field]
    with pytest.raises(ValueError):
        risk().validate_account(URL, payload)


def test_transfer_and_short_eligibility_flags_are_not_trading_authority():
    payload = account(cash="0", transfers_blocked=True, shorting_enabled=True)
    assert risk().validate_account(URL, payload).cash == 0


def observe(state=None, equity=1000, day=1, **kwargs):
    return risk().update_drawdown(
        state, equity=equity,
        asof=datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc),
        net_external_flow=0, flow_authenticated=True, **(dict(healthy=True) | kwargs))


def test_drawdown_initializes_durable_baseline():
    state = observe()
    assert state["starting_equity"] == "1000"
    assert state["high_water_nav"] == "1"
    assert state["paused"] is False
    assert state["recovery_count"] == 0
    assert state["account_number"] == "PA3BZK0WN833"
    assert state["last_asof"] == ASOF.isoformat()


@pytest.mark.parametrize("equity,paused", [("950.01", False), ("950", True),
                                          ("949.99", True)])
def test_pause_at_five_percent_boundary(equity, paused):
    initial = observe()
    snapshot = copy.deepcopy(initial)
    result = observe(initial, equity, day=2)
    assert result["paused"] is paused
    assert result["high_water_nav"] == "1"
    assert initial == snapshot
    assert result == observe(initial, equity, day=2)


def test_day_rollover_never_resets_baseline_or_peak():
    peak = observe(observe(), 1200, day=2)
    paused = observe(peak, 1140, day=3)
    next_day = observe(paused, 1150, day=4)
    assert peak["high_water_nav"] == "1.2"
    assert next_day["starting_equity"] == "1000"
    assert next_day["high_water_nav"] == "1.2"
    assert next_day["paused"] is True


@pytest.mark.parametrize("equity", [0, -1, None, True, "NaN", "inf"])
def test_invalid_equity_cannot_initialize_or_advance_drawdown(equity):
    for state in (None, observe()):
        with pytest.raises(ValueError):
            observe(state, equity, day=2)


@pytest.mark.parametrize("field,value", [
    ("high_water_nav", "0"), ("high_water_nav", "NaN"),
    ("high_water_nav", "0.9"), ("starting_equity", "0"),
    ("units", "0"), ("nav", "0"), ("nav", "1.1"),
    ("last_equity", "999"), ("paused", "false"),
    ("recovery_count", True), ("recovery_count", -1),
    ("account_number", "other"), ("version", 2),
    ("last_asof", "not a timestamp"),
])
def test_malformed_persisted_drawdown_state_is_rejected(field, value):
    state = observe()
    state[field] = value
    with pytest.raises(ValueError):
        observe(state, day=2)


@pytest.mark.parametrize("field", ["units", "paused", "high_water_nav",
                                   "last_completed_session", "recovery_session"])
def test_incomplete_persisted_state_is_not_reinitialized(field):
    state = observe()
    del state[field]
    with pytest.raises(ValueError):
        observe(state, day=2)


def test_drawdown_requires_aware_increasing_asof():
    for asof in (None, ASOF.replace(tzinfo=None), ASOF, "2026-10-01"):
        with pytest.raises(ValueError):
            risk().update_drawdown(observe(), equity=1000, asof=asof,
                                   net_external_flow=0, flow_authenticated=True)


def paused_state():
    return observe(observe(), 950, day=2)


def test_recovery_requires_two_completed_distinct_sessions():
    paused = paused_state()
    tick = observe(paused, 980, day=3)
    assert tick["paused"] and tick["recovery_count"] == 0
    first = observe(tick, 980, day=4, session_complete=True, session="2026-10-02")
    assert first["paused"] and first["recovery_count"] == 1
    repeated = observe(first, 980, day=5, session_complete=True, session="2026-10-02")
    assert repeated["paused"] and repeated["recovery_count"] == 1
    second = observe(repeated, 980, day=6, session_complete=True, session="2026-10-05",
                     previous_session="2026-10-02")
    assert second["paused"] is False
    assert second["recovery_count"] == 0
    assert second["high_water_nav"] == "1"
    assert second["starting_equity"] == "1000"


@pytest.mark.parametrize("equity,count", [("970", 0), ("969.99", 0), ("970.01", 1)])
def test_recovery_is_strictly_below_three_percent(equity, count):
    result = observe(paused_state(), equity, day=3, session_complete=True,
                     session="2026-10-03")
    assert result["paused"]
    assert result["recovery_count"] == count


@pytest.mark.parametrize("interrupt", [dict(equity=970), dict(healthy=False)])
def test_intraday_interruption_resets_recovery(interrupt):
    first = observe(paused_state(), 980, day=3, session_complete=True,
                    session="2026-10-02")
    bad = observe(first, day=4, **(dict(equity=980) | interrupt))
    assert bad["recovery_count"] == 0
    again = observe(bad, 980, day=5, session_complete=True, session="2026-10-05",
                    previous_session="2026-10-02")
    assert again["paused"] and again["recovery_count"] == 1


@pytest.mark.parametrize("predecessor", [None, "2026-10-01"])
def test_unproven_or_skipped_session_cannot_complete_recovery(predecessor):
    first = observe(paused_state(), 980, day=3, session_complete=True,
                    session="2026-10-02")
    result = observe(first, 980, day=6, session_complete=True, session="2026-10-05",
                     previous_session=predecessor)
    assert result["paused"] and result["recovery_count"] == 1


def test_same_session_cannot_be_recounted_after_interruption():
    first = observe(paused_state(), 980, day=3, session_complete=True,
                    session="2026-10-02")
    bad = observe(first, 970, day=4)
    repeated = observe(bad, 980, day=5, session_complete=True, session="2026-10-02")
    assert repeated["paused"] and repeated["recovery_count"] == 0


@pytest.mark.parametrize("kwargs", [
    dict(session_complete="true"), dict(healthy="true"),
    dict(session_complete=True), dict(session="2026-10-02"),
    dict(session_complete=True, session="2026-10-99"),
    dict(session_complete=True, session="2026-10-20"),
    dict(session_complete=True, session="2026-10-03", previous_session="2026-10-03"),
])
def test_malformed_session_evidence_rejected(kwargs):
    with pytest.raises(ValueError):
        observe(paused_state(), 980, day=3, **kwargs)


def test_out_of_order_completed_session_rejected():
    first = observe(paused_state(), 980, day=3, session_complete=True,
                    session="2026-10-03")
    with pytest.raises(ValueError):
        observe(first, 980, day=4, session_complete=True, session="2026-10-02")


@pytest.mark.parametrize("changes", [
    dict(recovery_count=1), dict(recovery_session="2026-10-02"),
    dict(last_completed_session="nonsense"),
    dict(paused=False, recovery_count=1, recovery_session="2026-10-02",
         last_completed_session="2026-10-02"),
    dict(paused=False, nav="0.95", last_equity="950"),
])
def test_inconsistent_recovery_state_fails_closed(changes):
    with pytest.raises(ValueError):
        observe(observe() | changes, day=3)


def flow_observe(state, equity, day=3, **kwargs):
    return risk().update_drawdown(state, equity=equity,
        asof=datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc), healthy=True, **kwargs)


def test_unknown_flow_blocks_risk_and_interrupts_existing_recovery():
    first = observe(paused_state(), 980, day=3, session_complete=True,
                    session="2026-10-02")
    blocked = flow_observe(first, 1100, day=4)
    assert blocked["paused"] and not blocked["risk_allowed"]
    assert blocked["recovery_count"] == 0
    assert blocked["high_water_nav"] == "1"
    assert blocked["valuation_asof"] == first["valuation_asof"]
    again = flow_observe(blocked, 980, day=5, net_external_flow=0,
                         flow_authenticated=True, session_complete=True,
                         session="2026-10-05", previous_session="2026-10-02")
    assert again["paused"] and again["recovery_count"] == 1


@pytest.mark.parametrize("kwargs", [dict(), dict(net_external_flow=0),
    dict(net_external_flow=None, flow_authenticated=True),
    dict(net_external_flow=0, flow_authenticated="yes"),
    dict(net_external_flow="NaN", flow_authenticated=True),
    dict(net_external_flow=True, flow_authenticated=True),
    dict(net_external_flow=100, flow_authenticated=True)])
def test_baseline_requires_authenticated_zero_flow(kwargs):
    with pytest.raises(ValueError):
        flow_observe(None, 1000, **kwargs)


def test_nonzero_flow_requires_authenticated_boundary_timing():
    with pytest.raises(ValueError):
        flow_observe(observe(), 1100, net_external_flow=100, flow_authenticated=True)


def test_deposit_cannot_mask_loss_or_reset_peak():
    initial = observe()
    result = flow_observe(initial, 1950, net_external_flow=1000,
                          flow_authenticated=True, flow_at_observation=True)
    assert result["nav"] == "0.95"
    assert result["high_water_nav"] == "1"
    assert result["starting_equity"] == "1000"
    assert result["paused"] and not result["risk_allowed"]
    assert observe(result, 1950, day=4)["nav"] == "0.95"


def test_withdrawal_does_not_invent_drawdown():
    result = flow_observe(observe(), 500, net_external_flow=-500,
                          flow_authenticated=True, flow_at_observation=True)
    assert result["nav"] == "1"
    assert result["units"] == "500"
    assert result["high_water_nav"] == "1"
    assert not result["paused"] and result["risk_allowed"]
    assert observe(result, 475, day=4)["paused"]


def test_deposit_units_are_issued_at_current_not_previous_nav():
    result = flow_observe(observe(), 1600, net_external_flow=500,
                          flow_authenticated=True, flow_at_observation=True)
    assert result["nav"] == "1.1"
    assert result["high_water_nav"] == "1.1"
    assert not observe(result, 1521, day=4)["paused"]
    assert observe(result, 1520, day=4)["paused"]


def test_nonpositive_preflow_equity_fails_closed():
    with pytest.raises(ValueError):
        flow_observe(observe(), 100, net_external_flow=100,
                     flow_authenticated=True, flow_at_observation=True)


def order(symbol="AAA", side="buy", qty=1, **kwargs):
    return dict(symbol=symbol, side=side, qty=qty, type="market", order_class="simple") | kwargs


def position(symbol="AAA", qty="1", **kwargs):
    return dict(symbol=symbol, qty=qty, side="long") | kwargs


def pending(symbol="AAA", side="buy", qty="1", **kwargs):
    return order(symbol, side, qty, id="order-1", status="new", filled_qty="0") | kwargs


def check(**kwargs):
    defaults = dict(base_url=URL, account=account(), order=order(qty=2),
                    positions=[], pending_orders=[], quotes={"AAA": {"bid": "99", "ask": "100"}},
                    drawdown_state=observe(), asof=ASOF)
    return risk().check_order(**(defaults | kwargs))


def test_buy_cash_funded_whole_shares_at_exact_name_boundary():
    result = check(account=account(cash="200"))
    assert result.approved
    assert result.proposed_notional == 200
    assert result.projected_gross == 200
    assert result.projected_name == 200
    assert result.reserved_cash == 0


@pytest.mark.parametrize("kwargs,reason", [
    (dict(account=account(cash="199.99")), "cash"),
    (dict(order=order(qty=3)), "20%"),
    (dict(positions=[position(qty="0.01")]), "20%"),
    (dict(positions=[position("BBB", "8.01")],
          quotes={"AAA": {"ask": 100, "bid": 99}, "BBB": {"ask": 100, "bid": 99}}), "gross"),
    (dict(pending_orders=[pending(qty="1")]), "20%"),
    (dict(account=account(cash="299.99"), pending_orders=[pending("BBB")],
          quotes={"AAA": {"ask": 100, "bid": 99}, "BBB": {"ask": 100, "bid": 99}}), "cash"),
    (dict(positions=[position("BBB", "7")], pending_orders=[pending("CCC", qty="2")],
          quotes={s: {"ask": 100, "bid": 99} for s in ("AAA", "BBB", "CCC")}), "gross"),
    (dict(positions=[position("BBB", "-1", side="short")],
          quotes={s: {"ask": 100, "bid": 99} for s in ("AAA", "BBB")}), "short"),
    (dict(drawdown_state=None), "state"),
    (dict(drawdown_state=paused_state(), account=account(equity="950")), "paused"),
    (dict(asof=ASOF.replace(hour=21)), "asof"),
    (dict(account=account(equity="1001")), "equity"),
])
def test_buy_rejects_independent_risk_limit_breaches(kwargs, reason):
    result = check(**kwargs)
    assert not result.approved
    assert reason in " ".join(result.reasons)


def test_gross_boundary_includes_pending_buys_but_never_nets_pending_sells():
    quotes = {s: {"ask": 100, "bid": 99} for s in ("AAA", "BBB", "CCC")}
    positions = [position("BBB", "7")]
    pending_orders = [pending("CCC"), pending("BBB", "sell", "7", id="sell-1")]
    result = check(positions=positions, pending_orders=pending_orders, quotes=quotes)
    assert result.approved and result.projected_gross == 1000
    assert result.reserved_cash == 100
    assert not check(positions=positions + [position("CCC", "0.01")],
                     pending_orders=pending_orders, quotes=quotes).approved


def test_remaining_pending_buy_qty_and_conservative_price_reserved():
    result = check(order=order(qty=1), account=account(cash="200"),
        pending_orders=[pending(qty="5", filled_qty="4", type="limit", limit_price="100")])
    assert result.approved and result.reserved_cash == 100
    assert result.projected_name == 200
    result = check(order=order(qty=1),
        pending_orders=[pending(qty="5", filled_qty="4", type="limit", limit_price="101")])
    assert not result.approved


def test_positions_use_conservative_quote_or_reported_market_value():
    assert not check(order=order(qty=1), positions=[position(market_value="101")]).approved
    assert not check(order=order(qty=1), positions=[position(current_price="101")]).approved
    assert check(order=order(qty=1), positions=[position(market_value="99")]).approved


@pytest.mark.parametrize("intent", [None, {}, order(qty=0), order(qty=-1), order(qty=True),
    order(qty="NaN"), order(qty="1.5"), order(side="sell_short"), order(side="BUY"),
    order(symbol="aaa"), order(symbol="AAA "), order(type="unknown"),
    order(type="limit"), order(type="stop", stop_price="NaN"),
    order(notional="100"), order(order_class="bracket"), order(legs=[{}])])
def test_malformed_proposed_order_rejected(intent):
    assert not check(order=intent).approved


@pytest.mark.parametrize("quotes", [None, {}, {"AAA": 100}, {"AAA": {"ask": 100}},
    {"AAA": {"ask": "NaN", "bid": 99}}, {"AAA": {"ask": 0, "bid": 0}},
    {"AAA": {"ask": 98, "bid": 99}}])
def test_bad_quotes_cannot_approve_buy(quotes):
    assert not check(quotes=quotes).approved


@pytest.mark.parametrize("positions", [None, {}, [{}], [position(qty="NaN")],
    [position(qty="0")], [position(qty="-1")], [position(side="unknown")],
    [position(), position()], [position(market_value="NaN")],
    [position(qty_available="2")]])
def test_uncertain_positions_are_not_empty_portfolio(positions):
    assert not check(positions=positions).approved


@pytest.mark.parametrize("orders", [None, {}, [{}], [pending(qty="NaN")],
    [pending(filled_qty="2")], [pending(filled_qty=None)],
    [pending(status="unknown")], [pending(id=None)], [pending(), pending()],
    [pending(type="stop")], [pending(type="limit", limit_price="NaN")],
    [pending("BBB", "sell", "1")]])
def test_uncertain_pending_orders_cannot_approve_buy(orders):
    assert not check(pending_orders=orders).approved


def test_check_order_returns_rejection_for_wrong_account_or_endpoint():
    assert not check(base_url="https://api.alpaca.markets/v2").approved
    assert not check(account=account(account_number="other")).approved


def test_safe_reductions_ignore_caps_pause_other_shorts_and_missing_quotes():
    positions = [position(qty="10"), position("BBB", "-1", side="short")]
    result = check(order=order(side="sell", qty=10), positions=positions,
                   account=account(cash="0"), drawdown_state=None, asof=None, quotes=None)
    assert result.approved
    assert check(order=order(side="sell", qty=1), positions=positions,
                 drawdown_state=paused_state()).approved


@pytest.mark.parametrize("held,pending_qty,filled,proposed,approved", [
    ("10", "4", "0", 6, True), ("10", "4", "0", 7, False),
    ("10", "7", "4", 7, True), ("10", "7", "4", 8, False),
    ("2.5", "1.5", "0", 1, True), ("2.5", "1.5", "0", 2, False),
])
def test_oversells_use_actual_long_shares_minus_remaining_pending_sells(
        held, pending_qty, filled, proposed, approved):
    result = check(order=order(side="sell", qty=proposed), positions=[position(qty=held)],
        pending_orders=[pending(side="sell", qty=pending_qty, filled_qty=filled)])
    assert result.approved is approved


def test_all_same_symbol_pending_sells_count():
    orders = [pending(side="sell", qty="2"), pending(side="sell", qty="3", id="sell-2")]
    assert check(order=order(side="sell", qty=5), positions=[position(qty="10")],
                 pending_orders=orders).approved
    assert not check(order=order(side="sell", qty=6), positions=[position(qty="10")],
                     pending_orders=orders).approved


def test_broker_qty_available_is_upper_bound_not_double_subtracted():
    positions = [position(qty="10", qty_available="6")]
    orders = [pending(side="sell", qty="4")]
    assert check(order=order(side="sell", qty=6), positions=positions, pending_orders=orders).approved
    assert not check(order=order(side="sell", qty=7), positions=positions, pending_orders=orders).approved


@pytest.mark.parametrize("positions", [[], [position("BBB", "5")],
    [position(qty="-5", side="short")]])
def test_sell_cannot_open_or_increase_short(positions):
    assert not check(order=order(side="sell"), positions=positions).approved


def test_pending_buys_cannot_supply_shares_for_sell():
    assert not check(order=order(side="sell"), pending_orders=[pending(qty="10")]).approved


def test_pending_sells_of_other_symbol_do_not_reduce_sellable_shares():
    assert check(order=order(side="sell"), positions=[position()],
                 pending_orders=[pending("BBB", side="sell", qty="10")]).approved


def test_stop_limit_prices_are_conservative_for_buy():
    proposed = order(qty=2, type="stop_limit", limit_price="100", stop_price="101")
    assert not check(order=proposed).approved
    assert check(order=order(qty=1, type="stop_limit", limit_price="100", stop_price="101")).approved


def oco(qty="4"):
    child = pending(side="sell", qty=qty, id="stop", type="stop", stop_price="90")
    return pending(side="sell", qty=qty, id="take", type="limit", limit_price="110",
                   order_class="oco", legs=[child])


def bracket():
    take = pending(side="sell", qty="2", id="take", type="limit", limit_price="110")
    stop = pending(side="sell", qty="2", id="stop", type="stop", stop_price="90")
    return pending(qty="2", filled_qty="1", status="partially_filled",
                   order_class="bracket", legs=[take, stop])


def test_explicit_oco_reserves_max_of_mutually_exclusive_sells_not_sum():
    assert check(order=order(side="sell", qty=6), positions=[position(qty="10")],
                 pending_orders=[oco()]).approved
    assert not check(order=order(side="sell", qty=7), positions=[position(qty="10")],
                     pending_orders=[oco()]).approved


def test_bracket_reserves_remaining_parent_buy_and_contingent_exit_shares():
    result = check(order=order(qty=1), pending_orders=[bracket()])
    assert result.approved and result.reserved_cash == 100
    assert result.projected_name == 200
    assert not check(order=order(side="sell"), positions=[position(qty="2")],
                     pending_orders=[bracket()]).approved
    assert check(order=order(side="sell"), positions=[position(qty="3")],
                 pending_orders=[bracket()]).approved


def test_completed_bracket_parent_still_reserves_open_exit_legs():
    done = bracket() | dict(filled_qty="2", status="filled")
    assert not check(order=order(side="sell"), positions=[position(qty="2")],
                     pending_orders=[done]).approved
    assert check(order=order(side="sell"), positions=[position(qty="3")],
                 pending_orders=[done]).approved


@pytest.mark.parametrize("changes", [dict(legs=[]), dict(legs=[{}]),
    dict(legs=[pending(side="buy", id="stop")]), dict(order_class="oto"),
    dict(legs=[pending("BBB", "sell", id="stop", type="stop", stop_price="90")]),
    dict(legs=[pending(side="sell", qty="5", id="stop", type="stop", stop_price="90")])])
def test_ambiguous_oco_never_assumes_mutual_exclusivity(changes):
    result = check(order=order(side="sell"), positions=[position(qty="10")],
                   pending_orders=[oco() | changes])
    assert not result.approved


def test_duplicate_flattened_and_nested_leg_rejected():
    root = oco()
    assert not check(order=order(side="sell"), positions=[position(qty="10")],
                     pending_orders=[root, root["legs"][0]]).approved


def test_deterministic_order_checks_do_not_mutate_injected_snapshots():
    inputs = dict(order=order(side="sell"), positions=[position(qty="10")],
                  pending_orders=[oco()], quotes={"AAA": {"bid": "99", "ask": "100"}},
                  drawdown_state=observe())
    snapshot = copy.deepcopy(inputs)
    first = check(**inputs)
    assert first.approved and first == check(**inputs)
    assert inputs == snapshot
