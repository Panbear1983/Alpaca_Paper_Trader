"""
Politician History — long-term memory of pool membership decisions.

Tracks for every politician who has ever been in or considered for the pool:
  - times_in_pool       — how many times they've been selected
  - last_in_pool        — date of most recent inclusion
  - last_removal_date   — date of most recent removal
  - last_removal_reason — why we removed them (poor score, low alpha, etc.)
  - last_score          — score at last vetting (for comparison)
  - weeks_in_pool       — running count of consecutive weeks currently in pool
  - weeks_on_probation  — running count of consecutive weeks at rank 4-5

Used by:
  - politician_vetter.py — apply cool-off penalty if recently removed
  - sunday_review.py    — graduation logic for probationary slots

State lives at politician_history.json.
"""

import os, json
from datetime import datetime, timezone, timedelta

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "politician_history.json")

COOL_OFF_DAYS         = 60     # apply penalty if removed within this window
COOL_OFF_PENALTY      = 0.10   # 10% score reduction during cool-off
GRADUATION_WEEKS      = 2      # weeks-on-probation before graduation eligible


def _load():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            try:    return json.load(f)
            except: pass
    return {"politicians": {}}


def _save(data):
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_history(politician_id):
    """Return history dict for a politician, or empty if never seen."""
    return _load()["politicians"].get(politician_id, {})


def get_cool_off_penalty(politician_id):
    """Return 0..1 score multiplier penalty if politician was recently removed.

    Returns 1.0 (no penalty) if never removed or last removal > COOL_OFF_DAYS ago.
    Returns (1.0 - COOL_OFF_PENALTY) if within cool-off window.
    """
    h = get_history(politician_id)
    last_removal = h.get("last_removal_date")
    if not last_removal:
        return 1.0
    try:
        removed_dt = datetime.fromisoformat(last_removal.replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    days_since = (datetime.now(timezone.utc) - removed_dt).days
    if days_since < COOL_OFF_DAYS:
        return 1.0 - COOL_OFF_PENALTY
    return 1.0


def record_added(politician_id, score, party=None):
    """Mark a politician as added to the pool."""
    data = _load()
    now = datetime.now(timezone.utc).isoformat()
    p = data["politicians"].setdefault(politician_id, {
        "politician_id": politician_id,
        "times_in_pool": 0,
        "weeks_in_pool": 0,
        "weeks_on_probation": 0,
    })
    p["times_in_pool"] = p.get("times_in_pool", 0) + 1
    p["last_in_pool"] = now
    p["last_score"] = score
    if party:
        p["party"] = party
    p["weeks_in_pool"] = 0           # reset on (re)entry
    p["weeks_on_probation"] = 0
    _save(data)


def record_removed(politician_id, reason, score=None):
    """Mark a politician as removed from the pool."""
    data = _load()
    p = data["politicians"].setdefault(politician_id, {
        "politician_id": politician_id,
        "times_in_pool": 0,
    })
    p["last_removal_date"] = datetime.now(timezone.utc).isoformat()
    p["last_removal_reason"] = reason
    if score is not None:
        p["last_removal_score"] = score
    p["weeks_in_pool"] = 0
    p["weeks_on_probation"] = 0
    _save(data)


def tick_week(active_pool):
    """Increment weeks_in_pool / weeks_on_probation for current pool members."""
    data = _load()
    for member in active_pool:
        pid = member["politician_id"]
        p = data["politicians"].setdefault(pid, {
            "politician_id": pid,
            "times_in_pool": 1,
            "weeks_in_pool": 0,
            "weeks_on_probation": 0,
        })
        p["weeks_in_pool"] = p.get("weeks_in_pool", 0) + 1
        if member.get("is_probationary"):
            p["weeks_on_probation"] = p.get("weeks_on_probation", 0) + 1
        else:
            p["weeks_on_probation"] = 0  # reset if promoted to full
    _save(data)


def get_weeks_on_probation(politician_id):
    return get_history(politician_id).get("weeks_on_probation", 0)


def is_graduation_eligible(politician_id):
    """True if politician has served enough weeks on probation to graduate."""
    return get_weeks_on_probation(politician_id) >= GRADUATION_WEEKS


def reconcile(current_pool):
    """Reconcile in-memory history with current pool state.
    Detects new additions and removals by diffing against the last known pool.
    Call this AFTER a vetting run rewrote pool_state.json.
    """
    data = _load()
    known_in_pool = {pid for pid, p in data["politicians"].items()
                     if p.get("last_in_pool") and not p.get("last_removal_date")
                        or (p.get("last_in_pool", "") > p.get("last_removal_date", ""))}
    new_ids = {m["politician_id"] for m in current_pool}

    # New additions
    for pid in new_ids - known_in_pool:
        member = next(m for m in current_pool if m["politician_id"] == pid)
        record_added(pid, member.get("score", 0), member.get("party"))

    # Removals
    for pid in known_in_pool - new_ids:
        h = data["politicians"].get(pid, {})
        last_score = h.get("last_score", 0)
        reason = f"dropped from top-5 in re-vet (last score {last_score:.3f})"
        record_removed(pid, reason, last_score)


if __name__ == "__main__":
    """CLI summary for debugging."""
    data = _load()
    print(f"Politicians tracked: {len(data['politicians'])}")
    print(f"\n  {'ID':<10} {'Times in':>9} {'Weeks':>6} {'Prob':>5} {'Last in pool':<22} {'Last removal'}")
    print(f"  {'─'*80}")
    for pid, p in sorted(data["politicians"].items(),
                         key=lambda x: x[1].get("last_in_pool", ""),
                         reverse=True):
        last_in   = (p.get("last_in_pool") or "")[:16]
        last_rem  = (p.get("last_removal_date") or "—")[:16]
        print(f"  {pid:<10} {p.get('times_in_pool',0):>9} "
              f"{p.get('weeks_in_pool',0):>6} {p.get('weeks_on_probation',0):>5} "
              f"{last_in:<22} {last_rem}")
