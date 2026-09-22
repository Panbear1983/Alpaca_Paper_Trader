"""broker_stops planner — pure, no network.

The planner decides what the reconciler does; these pin the decisions that
matter: whole shares only, one guard per name, no thrashing on small width
drift, cooldown after a mutation, orphans cancelled, and the tightened trail
that stops a re-armed guard from sitting lower than the one it replaced.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import broker_stops as bs  # noqa: E402

NOW = 1_000_000.0
W = {"NVDA": 6.6, "TSLA": 8.9, "ALM": 14.0}


def _pos(sym, qty, px=100.0):
    return {"symbol": sym, "qty": str(qty), "current_price": str(px)}


def _guard(sym, qty, trail, oid="g1", hwm=None):
    return {"id": oid, "symbol": sym, "type": "trailing_stop", "side": "sell",
            "client_order_id": f"pstop-20260922-{oid}", "qty": str(qty),
            "trail_percent": str(trail), "hwm": hwm}


def _acts(positions, guards, state=None):
    return {(a["sym"], a["action"]): a for a in bs.plan(positions, guards, W, state or {}, NOW)}


# ── arming ───────────────────────────────────────────────────────────────────

def test_arms_a_whole_share_guard_when_none_rests():
    a = _acts([_pos("NVDA", 92.87)], {})
    arm = a[("NVDA", "arm")]
    assert arm["qty"] == 92 and arm["trail"] == 6.6


def test_position_under_one_share_gets_no_guard():
    a = _acts([_pos("ALM", 0.7)], {})
    assert ("ALM", "arm") not in a


def test_existing_guard_on_a_sub_share_position_is_cancelled():
    a = _acts([_pos("ALM", 0.7)], {"ALM": [_guard("ALM", 1, 14.0)]})
    assert ("ALM", "cancel_extra") in a


# ── keeping it in place ──────────────────────────────────────────────────────

def test_matching_guard_is_left_alone():
    a = _acts([_pos("NVDA", 92.87)], {"NVDA": [_guard("NVDA", 92, 6.6)]})
    assert a[("NVDA", "none")]["reason"] == "in place"


def test_small_width_drift_does_not_thrash():
    a = _acts([_pos("NVDA", 92.87)], {"NVDA": [_guard("NVDA", 92, 6.3)]})   # 0.3 pt off
    assert ("NVDA", "patch_trail") not in a and ("NVDA", "none") in a


def test_large_width_drift_patches_the_trail():
    a = _acts([_pos("NVDA", 92.87)], {"NVDA": [_guard("NVDA", 92, 5.0)]})   # 1.6 pt off
    assert a[("NVDA", "patch_trail")]["trail"] == 6.6


def test_qty_change_patches_the_guard():
    a = _acts([_pos("NVDA", 46.5)], {"NVDA": [_guard("NVDA", 92, 6.6)]})     # sold half
    assert a[("NVDA", "patch_qty")]["qty"] == 46


def test_duplicate_guards_keep_the_highest_water_mark():
    guards = {"NVDA": [_guard("NVDA", 92, 6.6, "old", hwm="220"),
                       _guard("NVDA", 92, 6.6, "new", hwm="230")]}
    a = _acts([_pos("NVDA", 92.87)], guards)
    assert a[("NVDA", "cancel_extra")]["order_id"] == "old"


def test_orphan_guard_with_no_position_is_cancelled():
    a = _acts([], {"TSLA": [_guard("TSLA", 82, 8.9)]})
    assert a[("TSLA", "cancel_extra")]["reason"].startswith("no position")


# ── cooldown ─────────────────────────────────────────────────────────────────

def test_symbol_touched_seconds_ago_is_skipped():
    state = {"last_mutation": {"NVDA": NOW - 10}}
    a = _acts([_pos("NVDA", 92.87)], {}, state)
    assert ("NVDA", "arm") not in a and a[("NVDA", "none")]["reason"] == "cooldown"


def test_cooldown_expires():
    state = {"last_mutation": {"NVDA": NOW - 61}}
    a = _acts([_pos("NVDA", 92.87)], {}, state)
    assert ("NVDA", "arm") in a


# ── the tightened trail ──────────────────────────────────────────────────────

def test_rearm_after_a_pullback_never_lowers_the_stop():
    # previous guard had its stop at 210; price is now 215; a fresh 6.6% trail
    # would put the stop at 200.8 — below where protection already was.
    assert bs.tighten(6.6, 215.0, 210.0) == round((215 - 210) / 215 * 100, 2)


def test_tighten_is_a_no_op_when_the_old_stop_is_not_higher():
    assert bs.tighten(6.6, 215.0, 190.0) == 6.6
    assert bs.tighten(6.6, 215.0, None) == 6.6


def test_arm_uses_the_tightened_trail_from_state():
    state = {"last_stop": {"NVDA": 210.0}}
    a = _acts([_pos("NVDA", 92.87, px=215.0)], {}, state)
    assert a[("NVDA", "arm")]["trail"] < 6.6


# ── shorts and junk ──────────────────────────────────────────────────────────

def test_short_positions_are_ignored():
    a = _acts([_pos("SH", -50)], {})
    assert not any(k[0] == "SH" for k in a)


def test_is_guard_recognises_only_our_trailing_stops():
    assert bs.is_guard(_guard("NVDA", 1, 6.6))
    assert not bs.is_guard({"type": "trailing_stop", "side": "sell", "client_order_id": "manual-x"})
    assert not bs.is_guard({"type": "market", "side": "sell", "client_order_id": "pstop-x"})


def test_an_edited_guard_that_lost_its_tag_is_still_ours_via_the_replaces_link():
    """Live finding 2026-09-22: PATCH returns a replacement with a random client
    id. Without this, the reconciler would arm a duplicate and release() would
    not cancel the real guard — every sell would then be refused."""
    replacement = {"id": "new1", "type": "trailing_stop", "side": "sell",
                   "client_order_id": "55f3ba52-uuid", "replaces": "old1"}
    assert bs.is_guard(replacement, known_ids={"old1"})
    assert not bs.is_guard(replacement, known_ids={"other"})
    assert not bs.is_guard(replacement)


def test_patch_hands_the_replacement_our_tag(monkeypatch):
    sent = {}

    class R:
        def json(self):
            return {"id": "new1"}
    monkeypatch.setattr(bs.requests, "patch", lambda url, headers=None, json=None, timeout=None: sent.update(json) or R())
    bs.patch("old1", trail=7.1)
    assert sent["client_order_id"].startswith("pstop-") and sent["trail"] == "7.10"
