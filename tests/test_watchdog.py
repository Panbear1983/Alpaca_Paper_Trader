"""Watchdog — no network, no files. Every input is injected.

What must hold: silence when all is well; one alert per problem per episode,
repeated only after 30 minutes; soft failures (blind pricing, missing guards)
need three consecutive checks; nothing at all when the market is shut; one
"recovered" line when a problem clears.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import event_watcher as ew  # noqa: E402

NOW = 1_800_000_000.0
FRESH = {"epoch": NOW - 20, "ts": "now", "n_positions": 2, "n_priced": 2}
SCHED_OK = {"high_risk": {"last_manage_at": "2027-01-01T10:00:00-04:00"}}


def _sched(age_s):
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(NOW - age_s, tz=timezone.utc).isoformat()
    return {"high_risk": {"last_manage_at": ts}}


def _run(state, **kw):
    sent = []
    kw.setdefault("now", NOW)
    kw.setdefault("hb", FRESH)
    kw.setdefault("sched", _sched(300))
    kw.setdefault("market_open", True)
    kw.setdefault("guards", ({"NVDA", "TSLA"}, {"NVDA", "TSLA"}))
    kw.setdefault("exits_on", True)
    kw.setdefault("since_open", 3600)               # mid-session unless a test says otherwise
    n = ew.check_heartbeats(state, notify=lambda k, m: sent.append((k, m)), **kw)
    return n, sent


def test_all_well_is_silent():
    n, sent = _run({})
    assert n == 0 and sent == []


def test_market_closed_never_alerts_even_when_stale():
    n, sent = _run({}, hb={"epoch": 0}, market_open=False)
    assert n == 0 and sent == []


def test_first_run_seeds_silently():
    n, _ = _run({"first_run": True}, hb={"epoch": 0})
    assert n == 0


def test_stale_heartbeat_alerts_once_then_again_after_30_min():
    state = {}
    n1, s1 = _run(state, hb={"epoch": NOW - 400})
    n2, s2 = _run(state, hb={"epoch": NOW - 460}, now=NOW + 60)
    n3, s3 = _run(state, hb={"epoch": NOW - 2000}, now=NOW + 1900)
    assert n1 == 1 and "silent" in s1[0][1]
    assert n2 == 0
    assert n3 == 1


def test_recovery_sends_one_line_and_clears():
    state = {}
    _run(state, hb={"epoch": NOW - 400})
    n, sent = _run(state, now=NOW + 120, hb={"epoch": NOW + 100, "n_positions": 2, "n_priced": 2})
    assert n == 1 and sent[0][1].startswith("recovered")
    assert state["_hb_alerted"] == {}


def test_blind_pricing_needs_three_consecutive_checks():
    state = {}
    blind = {"epoch": NOW - 5, "n_positions": 2, "n_priced": 0}
    assert _run(state, hb=blind)[0] == 0
    assert _run(state, hb=blind, now=NOW + 60)[0] == 0
    n, sent = _run(state, hb=blind, now=NOW + 120)
    assert n == 1 and "blind" in sent[0][1]


def test_one_good_sweep_resets_the_blind_streak():
    state = {}
    blind = {"epoch": NOW - 5, "n_positions": 2, "n_priced": 0}
    _run(state, hb=blind); _run(state, hb=blind, now=NOW + 60)
    _run(state, now=NOW + 120)                      # fresh, fully priced
    assert state["_hb_streaks"]["unpriced"] == 0


def test_missing_guard_alerts_after_three_checks():
    state = {}
    g = ({"NVDA", "TSLA"}, {"TSLA"})                # NVDA has no resting stop
    _run(state, guards=g); _run(state, guards=g, now=NOW + 60)
    n, sent = _run(state, guards=g, now=NOW + 120)
    assert n == 1 and "NVDA" in sent[0][1]


def test_stalled_exit_engine_alerts_only_when_it_is_switched_on():
    n_on, s_on = _run({}, sched=_sched(45 * 60), exits_on=True)
    n_off, _ = _run({}, sched=_sched(45 * 60), exits_on=False)
    assert n_on == 1 and "exit engine" in s_on[0][1]
    assert n_off == 0


def test_no_false_alarm_in_the_first_minutes_of_a_session():
    """At 09:31 both stamps still show yesterday — that is normal."""
    n, _ = _run({}, hb={"epoch": NOW - 60_000}, sched=_sched(60_000), since_open=60)
    assert n == 0


def test_alarms_resume_once_the_session_is_old_enough():
    n, sent = _run({}, hb={"epoch": NOW - 60_000}, sched=_sched(60_000), since_open=41 * 60)
    kinds = {k for k, _ in sent}
    assert n == 2 and kinds == {"watcher", "scheduler"}


def test_notify_failure_does_not_crash_the_check():
    def boom(k, m):
        raise RuntimeError("telegram down")
    n = ew.check_heartbeats({}, now=NOW, hb={"epoch": 0}, sched=_sched(60), market_open=True,
                            guards=(set(), set()), exits_on=False, notify=boom)
    assert n == 0
