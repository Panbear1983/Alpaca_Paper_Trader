"""suggestions — the engines as advisors. Pure parts + the log in a temp dir. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suggestions as sg  # noqa: E402


def test_mode_key_wins_and_the_old_boolean_is_the_fallback():
    assert sg.mode_for({"enabled": True, "mode": "notify"}, "mode", "enabled") == "notify"
    assert sg.mode_for({"enabled": True}, "mode", "enabled") == "trade"          # a wallet without the key: as before
    assert sg.mode_for({"enabled": False}, "mode", "enabled") == "off"
    assert sg.mode_for({"copy_on": True, "copy_mode": "OFF"}, "copy_mode", "copy_on") == "off"
    assert sg.mode_for({"copy_on": True, "copy_mode": "banana"}, "copy_mode", "copy_on") == "trade"
    assert sg.mode_for({}, "mode", "enabled") == "off"


def test_short_reason_translates_the_fence():
    assert sg.short_reason("universe: AMD is not an anchor name — only AAPL…") == "not on your list"
    assert sg.short_reason("cash_floor: this buy ($5,000) would leave …") == "would break the cash floor"
    assert sg.short_reason("something odd happened") == "something odd happened"


def test_record_dedupes_per_engine_symbol_side_and_session(monkeypatch, tmp_path):
    monkeypatch.setattr(sg, "LOG", str(tmp_path / "s.jsonl"))
    a = sg.record("swing", "amd", "buy", 5000, "momentum #2", True, "", session="2026-09-23")
    assert a["symbol"] == "AMD" and a["fence_ok"] is True
    assert sg.record("swing", "AMD", "buy", 5000, "again", True, "", session="2026-09-23") is None
    assert sg.record("copier", "AMD", "buy", 1500, "K000389 bought", False, "not on your list", session="2026-09-23")
    assert sg.record("swing", "AMD", "buy", 5000, "next day", True, "", session="2026-09-24")
    assert len(sg.for_session("2026-09-23")) == 2


def test_format_line():
    ok = {"side": "buy", "symbol": "AMD", "usd": 5000, "reason": "momentum #2 (RS +29.5%)", "fence_ok": True}
    no = {"side": "buy", "symbol": "INTC", "usd": 5000, "reason": "momentum #1", "fence_ok": False, "fence_why": "not on your list"}
    assert sg.format_line(ok) == "🟢 BUY `AMD` $5,000 — momentum #2 (RS +29.5%) · allowed"
    assert sg.format_line(no) == "⚪ BUY `INTC` $5,000 — momentum #1 · blocked: not on your list"


def test_taken_matches_a_manual_buy_within_three_days():
    rows = [{"session": "2026-09-22", "engine": "swing", "symbol": "AMD", "side": "buy", "usd": 5000, "fence_ok": True, "fence_why": ""},
            {"session": "2026-09-22", "engine": "copier", "symbol": "HOOD", "side": "buy", "usd": 1500, "fence_ok": True, "fence_why": ""},
            {"session": "2026-09-22", "engine": "copier", "symbol": "AAPL", "side": "sell", "usd": 0, "fence_ok": True, "fence_why": ""}]
    fills = [{"sym": "AMD", "side": "buy", "date": "2026-09-24"},      # 2 days later → taken
             {"sym": "HOOD", "side": "buy", "date": "2026-09-30"},     # too late
             {"sym": "AAPL", "side": "sell", "date": "2026-09-22"}]
    out = sg.taken(rows, fills)
    assert [r["taken"] for r in out] == [True, False, False]


def test_notice_line(monkeypatch, tmp_path):
    monkeypatch.setattr(sg, "LOG", str(tmp_path / "s.jsonl"))
    assert sg.notice("2026-09-23", fills=[]) == ""
    sg.record("swing", "AMD", "buy", 5000, "momentum #2", True, "", session="2026-09-23")
    sg.record("swing", "INTC", "buy", 5000, "momentum #1", False, "not on your list", session="2026-09-23")
    line = sg.notice("2026-09-23", fills=[{"sym": "AMD", "side": "buy", "date": "2026-09-23"}])
    assert line.startswith("💡 Ideas from the engines (advice only): swing → ")
    assert "buy AMD $5,000 (allowed) — taken" in line and "buy INTC $5,000 (blocked, not on your list)" in line
    assert line.endswith("· taken 1/2")
