"""Robinhood main-account view: read-only reader + dashboard wallet. No network,
no Claude, no real account numbers — the shapes below copy Robinhood's real
answers (2026-10-13) with made-up numbers.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import robinhood_view as rhv  # noqa: E402

MAIN, ROTH, AGENTIC = "111110001", "222220002", "333330003"


def _accounts():
    return {"data": {"accounts": [
        {"account_number": MAIN, "is_default": True, "agentic_allowed": False,
         "brokerage_account_type": "individual", "type": "margin"},
        {"account_number": ROTH, "is_default": False, "agentic_allowed": False,
         "brokerage_account_type": "ira_roth"},
        {"account_number": AGENTIC, "is_default": False, "agentic_allowed": True,
         "nickname": "Agentic", "brokerage_account_type": "individual"}]}}


def _portfolio(total="1000.00", equity="990.00", cash="10.00"):
    return {"data": {"total_value": total, "equity_value": equity, "cash": cash,
                     "buying_power": {"buying_power": "10.0000"}}}


def _positions(rows):
    return {"data": {"positions": rows}}


def _quote(sym, last, last_t, close, close_date, ext=None, ext_t=None):
    return {"quote": {"symbol": sym, "last_trade_price": str(last),
                      "venue_last_trade_time": last_t,
                      "last_non_reg_trade_price": None if ext is None else str(ext),
                      "venue_last_non_reg_trade_time": ext_t,
                      "previous_close": str(close), "previous_close_date": close_date,
                      "adjusted_previous_close": str(close), "state": "active"},
            "close": {"symbol": sym, "date": close_date, "price": str(close)}}


def _bars(sym, rows):
    return {"symbol": sym, "bars": [
        {"begins_at": f"{d}T00:00:00Z", "close_price": str(c), **({"interpolated": True} if i else {})}
        for d, c, i in rows]}


def _stream(calls):
    """calls = [(tool, input, answer_dict)] → stream-json lines, like Claude Code."""
    lines = [json.dumps({"type": "system", "subtype": "init"})]
    for i, (tool, inp, ans) in enumerate(calls):
        tid = f"toolu_{i}"
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": rhv.PFX + tool, "input": inp}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": json.dumps(ans)}]}}))
    lines.append(json.dumps({"type": "result", "is_error": False, "result": "done"}))
    return lines


def _good_calls():
    pos = [{"symbol": "AAA", "quantity": "2", "intraday_quantity": "0", "average_buy_price": "90"},
           {"symbol": "BBB", "quantity": "1", "intraday_quantity": "1", "average_buy_price": "50"}]
    quotes = {"data": {"results": [
        # evening: last regular trade at the 10-12 close, after-hours trade later
        _quote("AAA", 100, "2026-10-12T19:59:59.123456789Z", 100, "2026-10-12",
               ext=98, ext_t="2026-10-13T00:30:00.5Z"),
        _quote("BBB", 55, "2026-10-12T19:59:59Z", 55, "2026-10-12")]}}
    hist = {"data": {"results": [
        _bars("AAA", [("2026-10-08", 94, False), ("2026-10-09", 95, False), ("2026-10-12", 95, True)]),
        _bars("BBB", [("2026-10-09", 60, False), ("2026-10-12", 60, True)])]}}
    return [("get_accounts", {}, _accounts()),
            ("get_portfolio", {"account_number": AGENTIC}, _portfolio("0", "0", "0")),
            ("get_portfolio", {"account_number": MAIN}, _portfolio("261.00", "251.00", "10.00")),
            ("get_equity_positions", {"account_number": MAIN}, _positions(pos)),
            ("get_equity_quotes", {"symbols": ["AAA", "BBB"]}, quotes),
            ("get_equity_historicals", {"symbols": ["AAA", "BBB"]}, hist)]


# ── safety of the headless command ─────────────────────────────────────────

def test_command_allows_only_read_tools_and_blocks_every_money_tool():
    cmd = rhv.build_command("/bin/claude", "x", "haiku")
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert set(allowed) == {"ToolSearch", *rhv.READ_TOOLS}
    for name in ("place_equity_order", "place_option_order", "place_crypto_order",
                 "cancel_equity_order", "review_equity_order", "exercise_option"):
        assert rhv.PFX + name in blocked
    assert not set(allowed) & set(blocked)
    assert cmd[cmd.index("--tools") + 1] == "ToolSearch"          # no shell, no files
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--setting-sources") + 1] == "project"  # no voice / memory hooks
    assert "--no-session-persistence" in cmd


def test_child_env_never_carries_a_paid_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://elsewhere")
    env = rhv._child_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env


# ── parsing + snapshot ──────────────────────────────────────────────────────

def test_snapshot_uses_only_the_main_account():
    raw = rhv.parse_stream(_stream(_good_calls()))
    snap = rhv.build_snapshot(raw, now=1_000_000.0, market_open=False)
    assert snap["account_last4"] == "0001"
    assert snap["total_value"] == 261.0            # the MAIN portfolio, not the Agentic one
    assert [p["symbol"] for p in snap["positions"]] == ["AAA", "BBB"]
    assert MAIN not in json.dumps(snap)            # only the last four digits are kept


def test_after_hours_trade_wins_when_newer():
    snap = rhv.build_snapshot(rhv.parse_stream(_stream(_good_calls())), 1.0)
    aaa = snap["positions"][0]
    assert aaa["last_price"] == 98 and aaa["extended_hours"] is True


def test_placeholder_daily_bar_is_skipped_for_the_prior_close():
    snap = rhv.build_snapshot(rhv.parse_stream(_stream(_good_calls())), 1.0)
    aaa = snap["positions"][0]
    assert aaa["latest_close"] == 100 and aaa["latest_close_date"] == "2026-10-12"
    assert aaa["prior_close"] == 95 and aaa["prior_close_date"] == "2026-10-09"


def test_missing_accounts_is_a_failed_read_with_a_hint():
    with pytest.raises(rhv.ReaderError, match="approving again"):
        rhv.build_snapshot(rhv.parse_stream(_stream([])), 1.0)


def test_missing_price_fails_rather_than_showing_zero():
    calls = [c for c in _good_calls() if c[0] != "get_equity_quotes"]
    with pytest.raises(rhv.ReaderError, match="no Robinhood price"):
        rhv.build_snapshot(rhv.parse_stream(_stream(calls)), 1.0)


def test_holdings_from_another_account_are_ignored():
    calls = _good_calls()
    calls[3] = ("get_equity_positions", {"account_number": AGENTIC}, calls[3][2])
    with pytest.raises(rhv.ReaderError, match="holdings"):
        rhv.build_snapshot(rhv.parse_stream(_stream(calls)), 1.0)


# ── the numbers the dashboard shows ─────────────────────────────────────────

def test_evening_day_change_is_the_latest_session_vs_the_close_before():
    snap = rhv.build_snapshot(rhv.parse_stream(_stream(_good_calls())), 1.0)
    acct, rows, info = rhv.view(snap)
    a, b = rows
    assert a["current_price"] == 98
    assert a["unrealized_intraday_pl"] == pytest.approx(2 * (98 - 95))   # vs Friday's 95
    assert b["unrealized_intraday_pl"] == pytest.approx(1 * (55 - 50))   # bought that day: vs cost
    assert a["unrealized_pl"] == pytest.approx(2 * (98 - 90))
    assert acct["equity"] == pytest.approx(10 + 196 + 55)
    assert info["day_pl"] == pytest.approx(6 + 5)


def test_live_price_from_today_measures_against_the_latest_close():
    snap = rhv.build_snapshot(rhv.parse_stream(_stream(_good_calls())), 1.0)
    for p in snap["positions"]:
        p["intraday_qty"] = 0                        # a new session: nothing bought today yet
    live = {"AAA": (103.0, "2026-10-13T14:00:00Z")}
    _, rows, info = rhv.view(snap, live)
    assert rows[0]["current_price"] == 103
    assert rows[0]["unrealized_intraday_pl"] == pytest.approx(2 * (103 - 100))
    assert info["live_prices"] == 1


def test_an_older_live_price_does_not_replace_robinhoods():
    snap = rhv.build_snapshot(rhv.parse_stream(_stream(_good_calls())), 1.0)
    _, rows, _ = rhv.view(snap, {"AAA": (70.0, "2026-10-12T15:00:00Z")})
    assert rows[0]["current_price"] == 98


# ── when to read ────────────────────────────────────────────────────────────

ST = dict(rhv.DEFAULTS)


def test_reads_every_few_minutes_while_open():
    snap = {"read_ts": 1000.0, "market_open": True, "positions": []}
    assert not rhv.read_is_due(snap, {}, True, now=1000 + 4 * 60, st=ST)
    assert rhv.read_is_due(snap, {}, True, now=1000 + 5 * 60, st=ST)


def test_one_read_after_the_close_then_quiet():
    snap = {"read_ts": 1000.0, "market_open": True, "positions": []}
    assert rhv.read_is_due(snap, {}, False, now=1000 + 6 * 60, st=ST)
    snap2 = {"read_ts": 2000.0, "market_open": False, "positions": []}
    assert not rhv.read_is_due(snap2, {}, False, now=2000 + 3600, st=ST)
    assert rhv.read_is_due(snap2, {}, False, now=2000 + 12 * 3600, st=ST)


def test_a_failed_attempt_waits_a_full_interval():
    snap = {"read_ts": 0.0, "market_open": True, "positions": []}
    assert not rhv.read_is_due(snap, {"last_attempt_ts": 10_000.0}, True, now=10_060, st=ST)


def test_switched_off_never_reads():
    assert not rhv.read_is_due(None, {}, True, now=1.0, st={**ST, "enabled": False})


# ── the full read against a fake Claude ─────────────────────────────────────

@pytest.fixture
def tmpdir_view(tmp_path, monkeypatch):
    monkeypatch.setattr(rhv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rhv, "claude_binary", lambda: "/bin/claude")
    return tmp_path


def test_good_read_writes_private_snapshot_and_history(tmpdir_view):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "\n".join(_stream(_good_calls())), "")
    ok, msg = rhv.run_reader(market_open=False, runner=fake_run, now_fn=lambda: 5000.0)
    assert ok is True and "2 holdings" in msg
    snap = rhv.load_snapshot()
    assert snap["read_ts"] == 5000.0
    assert (tmpdir_view / "snapshot.json").stat().st_mode & 0o777 == 0o600
    assert rhv.load_history() == [(5000.0, 261.0)]
    assert rhv.load_status()["consecutive_failures"] == 0


def test_failed_read_keeps_the_last_good_snapshot(tmpdir_view):
    good = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "\n".join(_stream(_good_calls())), "")
    rhv.run_reader(runner=good, now_fn=lambda: 5000.0)
    bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "login expired")
    ok, msg = rhv.run_reader(runner=bad, now_fn=lambda: 6000.0)
    assert ok is False and "login expired" in msg
    assert rhv.load_snapshot()["read_ts"] == 5000.0
    st = rhv.load_status()
    assert st["consecutive_failures"] == 1 and st["last_attempt_ts"] == 6000.0


def test_timeout_is_a_failed_read(tmpdir_view):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    ok, msg = rhv.run_reader(runner=slow)
    assert ok is False and "timed out" in msg


def test_history_candles_follow_successive_reads():
    pts = [(1_760_000_000.0, 100.0), (1_760_000_300.0, 103.0), (1_760_000_600.0, 101.0)]
    bars = rhv.history_bars("1D", points=pts)
    assert [(b["o"], b["c"]) for b in bars] == [(100, 100), (100, 103), (103, 101)]
