"""Scoreboard Multiples (scoreboard.py): the maths behind Shift+S, offline.
Closes and stamps below are the real wallets' (read 2026-10-14) unless noted;
Robinhood values are made up."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scoreboard as sb  # noqa: E402


def _sessions(*days, close="16:00"):
    return sb.parse_calendar([{"date": d, "open": "09:30", "close": close} for d in days])


def _stamp(day: str) -> float:
    """Alpaca's stamp for a session's close: 00:00 UTC the next calendar day."""
    d = dt.date.fromisoformat(day) + dt.timedelta(days=1)
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()


def _et(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=sb.ET)


def _hist(closes: dict, pls: dict | None = None) -> dict:
    out = {"timestamp": [], "equity": [], "profit_loss": []}
    prev = None
    for d in sorted(closes):
        out["timestamp"].append(_stamp(d))
        out["equity"].append(closes[d])
        out["profit_loss"].append((pls or {}).get(d, 0.0 if prev is None else closes[d] - prev))
        prev = closes[d]
    return out


def _wallet(name, closes, equity=0.0, transfers=(), pls=None):
    return {"name": name, "ok": True, "err": "", "hist": _hist(closes, pls),
            "equity": equity, "transfers": [dt.date.fromisoformat(t) for t in transfers]}


def _rh(*reads):
    return [(_et(t).timestamp(), v) for t, v in reads]


def _pcts(table, col):
    return {r["day"].isoformat(): (None if r["cells"][col]["pct"] is None
                                   else round(r["cells"][col]["pct"], 2))
            for r in table["rows"]}


OCT = _sessions("2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12",
                "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")
WB = {"2026-10-07": 92975.0, "2026-10-08": 90546.0, "2026-10-09": 91012.0,
      "2026-10-12": 89224.0}
HR = {"2026-10-07": 72642.47, "2026-10-08": 70844.27, "2026-10-09": 72919.97,
      "2026-10-12": 71206.91}


# ── which session a history point belongs to ─────────────────────────────────

def test_history_stamps_map_to_the_session_they_close():
    utc = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
    assert sb.session_for_stamp(utc("2026-10-09T00:00"), OCT) == dt.date(2026, 10, 8)
    assert sb.session_for_stamp(utc("2026-10-10T00:00"), OCT) == dt.date(2026, 10, 9)   # Saturday stamp
    assert sb.session_for_stamp(utc("2026-10-13T00:00"), OCT) == dt.date(2026, 10, 12)


def test_mapping_survives_the_end_of_daylight_saving():
    nov = _sessions("2026-11-02", "2026-11-03")
    assert sb.session_for_stamp(_stamp("2026-11-02"), nov) == dt.date(2026, 11, 2)   # 19:00 EST


def test_a_stamp_the_calendar_does_not_cover_is_dropped():
    assert sb.session_for_stamp(_stamp("2026-10-20"), OCT) is None
    assert sb.session_for_stamp(_stamp("2026-01-05"), OCT) is None


def test_early_close_days_use_their_real_bell():
    s = sb.parse_calendar([{"date": "2026-11-27", "open": "09:30", "close": "13:00"}])
    assert s[0].close == _et("2026-11-27T13:00")


# ── reference values (the plan's check) ─────────────────────────────────────

def test_reference_values_match_the_real_closes():
    t = sb.assemble(30, OCT, [_wallet("High Risk", HR), _wallet("WB", WB)], [],
                    _et("2026-10-12T20:00"))
    hr, wb = _pcts(t, 0), _pcts(t, 1)
    assert [wb[d] for d in ("2026-10-08", "2026-10-09", "2026-10-12")] == [-2.61, 0.51, -1.96]
    assert [hr[d] for d in ("2026-10-08", "2026-10-09", "2026-10-12")] == [-2.48, 2.93, -2.35]
    assert t["rows"][0]["day"] == dt.date(2026, 10, 12)          # newest at the top


def test_zero_equity_days_before_funding_are_skipped():
    closes, _ = sb.alpaca_closes(_hist({"2026-10-07": 0.0, "2026-10-08": 100.0}), OCT)
    assert closes == {dt.date(2026, 10, 8): 100.0}


# ── unchanged days: real flat vs Alpaca data gap ────────────────────────────

SEP = _sessions("2026-09-14", "2026-09-15", "2026-09-16")
JUL = _sessions("2026-07-28", "2026-07-29", "2026-07-30")


def test_one_wallet_unchanged_is_a_real_flat_day():
    """High Risk held only cash on 2026-09-15; Low Risk moved."""
    hr = _wallet("High Risk", {"2026-09-14": 69351.07, "2026-09-15": 69351.07, "2026-09-16": 68770.0})
    lr = _wallet("Low Risk", {"2026-09-14": 100666.0, "2026-09-15": 100471.0, "2026-09-16": 100009.0})
    t = sb.assemble(30, SEP, [hr, lr], [], _et("2026-09-16T20:00"))
    row = {r["day"].isoformat(): r for r in t["rows"]}["2026-09-15"]
    assert row["cells"][0]["state"] == "flat" and row["cells"][1]["state"] == "down"
    assert t["totals"][0]["flat"] == 1 and t["totals"][0]["missing"] == 0
    assert not any(k == "sb.note.gap" for k, _ in t["notes"])


def test_every_wallet_unchanged_is_a_data_gap_and_the_next_day_covers_two():
    """2026-07-29: all wallets exactly unchanged although they moved intraday."""
    a = _wallet("A", {"2026-07-28": 96076.0, "2026-07-29": 96076.0, "2026-07-30": 96710.0})
    b = _wallet("B", {"2026-07-28": 89919.0, "2026-07-29": 89919.0, "2026-07-30": 88741.0})
    t = sb.assemble(30, JUL, [a, b], [], _et("2026-07-30T20:00"))
    rows = {r["day"].isoformat(): r for r in t["rows"]}
    assert [c["state"] for c in rows["2026-07-29"]["cells"][:2]] == ["missing", "missing"]
    c = rows["2026-07-30"]["cells"][0]
    assert c["span"] == 2 and "2d" in c["marks"]
    assert c["pct"] == pytest.approx((96710 / 96076 - 1) * 100)
    assert ("sb.note.gap", {"days": "07-29"}) in t["notes"]
    assert t["totals"][0]["missing"] == 1


def test_a_lone_wallet_unchanged_is_never_called_a_gap():
    assert sb.gap_sessions([{dt.date(2026, 7, 28): 1.0, dt.date(2026, 7, 29): 1.0}]) == set()


# ── deposits and withdrawals ────────────────────────────────────────────────

def test_transfer_day_uses_alpacas_own_pnl_and_is_marked():
    """A $100k journal landed on a Saturday: it belongs to Monday's change."""
    w = _wallet("A", {"2026-10-09": 100000.0, "2026-10-12": 200500.0},
                transfers=["2026-10-10"], pls={"2026-10-12": 500.0})
    t = sb.assemble(30, OCT, [w], [], _et("2026-10-12T20:00"))
    c = t["rows"][0]["cells"][0]
    assert c["dollars"] == 500.0 and c["pct"] == pytest.approx(0.5) and "*" in c["marks"]
    assert ("sb.note.transfer", {}) in t["notes"]


# ── Robinhood day closes ────────────────────────────────────────────────────

def test_robinhood_close_is_the_first_read_after_the_bell():
    closes, est = sb.robinhood_closes(_rh(("2026-10-14T15:55", 100.0), ("2026-10-14T16:02", 101.0),
                                          ("2026-10-14T21:03", 110.0)), OCT)
    assert closes[dt.date(2026, 10, 14)] == 101.0 and not est


def test_a_late_or_missing_close_read_is_an_estimate():
    closes, est = sb.robinhood_closes(_rh(("2026-10-12T22:04", 90.0), ("2026-10-12T22:10", 91.0),
                                          ("2026-10-13T11:00", 95.0), ("2026-10-13T14:00", 96.0)), OCT)
    assert closes[dt.date(2026, 10, 12)] == 90.0       # first after the bell, but 6 h late
    assert closes[dt.date(2026, 10, 13)] == 96.0       # none after the bell: last read
    assert est == {dt.date(2026, 10, 12), dt.date(2026, 10, 13)}


def test_robinhood_weekend_reads_are_ignored():
    closes, _ = sb.robinhood_closes(_rh(("2026-10-10T12:00", 1.0)), OCT)
    assert closes == {}


def test_robinhood_change_inherits_an_estimate_and_marks_skipped_days():
    pts = _rh(("2026-10-07T22:04", 100.0),                  # first day, late: estimate
              ("2026-10-08T16:02", 102.0),                  # on time, but measured from an estimate
              ("2026-10-12T16:05", 99.96))                  # 10-09 had no reads
    t = sb.assemble(30, OCT, [], pts, _et("2026-10-12T20:00"))
    rows = {r["day"].isoformat(): r["cells"][0] for r in t["rows"]}
    assert rows["2026-10-07"]["state"] == "none"           # nothing to compare with yet
    assert rows["2026-10-08"]["marks"] == ["~"]
    assert rows["2026-10-09"]["state"] == "missing"
    assert rows["2026-10-12"]["marks"] == ["2d"] and rows["2026-10-12"]["pct"] == pytest.approx(-2.0)


# ── today's row ─────────────────────────────────────────────────────────────

def test_today_is_live_while_the_session_is_open():
    rh = _rh(("2026-10-12T16:02", 100.0), ("2026-10-13T11:00", 98.0))
    t = sb.assemble(30, OCT, [_wallet("WB", WB, equity=90116.24)], rh, _et("2026-10-13T11:05"))
    top = t["rows"][0]
    assert top["day"] == dt.date(2026, 10, 13) and top["label"] == "live"
    assert round(top["cells"][0]["pct"], 2) == 1.0
    assert top["cells"][1]["marks"] == [] and round(top["cells"][1]["pct"], 2) == -2.0
    assert ("sb.note.live", {}) in t["notes"]


def test_today_after_the_close_is_after_hours_until_history_has_it():
    t = sb.assemble(30, OCT, [_wallet("WB", WB, equity=90116.24)], [], _et("2026-10-13T21:00"))
    assert t["rows"][0]["label"] == "after" and ("sb.note.after", {}) in t["notes"]
    done = dict(WB, **{"2026-10-13": 90116.24})
    t = sb.assemble(30, OCT, [_wallet("WB", done, equity=90200.0)], [], _et("2026-10-13T21:00"))
    assert t["rows"][0]["label"] == "" and round(t["rows"][0]["cells"][0]["pct"], 2) == 1.0


def test_no_live_row_before_the_open_or_at_the_weekend():
    done = dict(WB, **{"2026-10-13": 90116.24})
    pre = sb.assemble(30, OCT, [_wallet("WB", done, equity=1.0)], [], _et("2026-10-14T08:00"))
    assert pre["rows"][0]["day"] == dt.date(2026, 10, 13) and pre["rows"][0]["label"] == ""
    wknd = sb.assemble(30, OCT, [_wallet("WB", done, equity=1.0)], [], _et("2026-10-17T12:00"))
    assert wknd["rows"][0]["day"] == dt.date(2026, 10, 16)


# ── bars, colours, footers ──────────────────────────────────────────────────

def test_bars_share_one_scale_and_cap():
    assert [sb.bar_blocks(p) for p in (0, None, 0.1, 0.5, 0.51, -1.0, 2.93, 12.0)] == \
        [0, 0, 1, 1, 2, 2, 6, 6]


def test_up_is_green_down_is_red_rest_grey():
    assert [sb.cell_colour(s) for s in ("up", "down", "flat", "missing", "none")] == \
        ["green", "red", "grey50", "grey50", "grey50"]


def test_footers_count_sum_and_compound():
    cells = [sb._cell("up", 10.0, 1000.0), sb._cell("down", -10.0, -1100.0),
             sb._cell("up", 15.0, 1485.0, span=2, marks=["2d"]), sb._cell("flat", 0.0, 0.0),
             sb._cell("missing")]
    f = sb.totals(cells)
    assert (f["up"], f["down"], f["flat"], f["missing"]) == (2, 1, 1, 1)
    assert (f["won"], f["lost"], f["diff"]) == (2485.0, -1100.0, 1385.0)
    assert f["net_pct"] == pytest.approx((1.1 * 0.9 * 1.15 - 1) * 100)   # not the sum of %
    assert (f["best"], f["worst"]) == (10.0, -10.0)                       # the 2d +15% is not a "day"


# ── failure handling and safety ─────────────────────────────────────────────

def test_a_failing_wallet_becomes_a_note_not_a_blank_panel():
    bad = {"name": "Low Risk", "ok": False, "err": "401 Unauthorized"}
    t = sb.assemble(30, OCT, [bad, _wallet("WB", WB)], [], _et("2026-10-12T20:00"))
    assert t["columns"][0]["ok"] is False
    assert ("sb.note.err", {"name": "Low Risk", "err": "401 Unauthorized"}) in t["notes"]
    assert _pcts(t, 1)["2026-10-12"] == -1.96


def test_build_never_starts_a_robinhood_read(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the scoreboard started a Robinhood read")
    monkeypatch.setattr(sb.rhv, "run_reader", boom)
    monkeypatch.setattr(sb.wallets, "list_wallets", lambda: [{"name": "WB"}])
    t = sb.build(30, now=_et("2026-10-12T20:00"),
                 fetch=lambda name, since: _wallet(name, WB),
                 calendar=lambda a, b: OCT,
                 load_rh=lambda: _rh(("2026-10-12T16:02", 1.0)))
    assert _pcts(t, 0)["2026-10-12"] == -1.96
    assert [c["kind"] for c in t["columns"]] == ["alpaca", "robinhood"]


def test_calendar_failure_is_reported_not_raised():
    def down(a, b):
        raise RuntimeError("calendar down")
    t = sb.build(30, now=_et("2026-10-12T20:00"), calendar=down, load_rh=lambda: [])
    assert t["rows"] == [] and t["notes"][0][0] == "sb.note.err"


def test_text_mode_renders():
    t = sb.assemble(30, OCT, [_wallet("WB", WB)], [], _et("2026-10-12T20:00"))
    out = sb.render_text(t)
    assert "Scoreboard Multiples" in out and "-2.61% ██████" in out
