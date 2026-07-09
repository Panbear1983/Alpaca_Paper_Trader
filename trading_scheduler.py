"""
trading_scheduler.py — single heartbeat dispatcher for all autonomous trading.

A launchd job runs this every 10 minutes (same pattern as report_scheduler.py).
It self-gates in ET and dispatches, per tick:

  EXIT ENGINE   capitol_copier.manage_only()   every `manage_every_minutes`
                during market hours (stop-loss / trail / take-profit / pyramid)
  SWING BUYER   swing_buyer.run()              once per day in the
                swing_time_et window (regime-filtered RS entries + dip-adds)
  COPY LOOP     capitol_copier.run()           once per day in the
                copy_time_et window (politician disclosure copying)

Master switches (nothing trades until YOU flip them in strategy_config.json):
  swing.enabled                 → gates the swing buyer
  capitol_copier.autorun_enabled → gates BOTH the exit engine and the copy loop

Idempotency state in .trading_schedule_state.json (last manage tick, last
swing/copy dates) so restarts never double-fire.

Run manually to test:  python3 trading_scheduler.py
"""
import os
import json
import sys
import datetime as dt
from zoneinfo import ZoneInfo

HERE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "strategy_config.json")
STATE_FILE  = os.path.join(HERE, ".trading_schedule_state.json")
ET = ZoneInfo("America/New_York")


def _load_cfg() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _in_window(now: dt.datetime, hhmm: str, minutes: int) -> bool:
    try:
        h, m = map(int, hhmm.split(":"))
    except Exception:
        return False
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return start <= now < start + dt.timedelta(minutes=minutes)


def main() -> int:
    cfg   = _load_cfg()
    ts    = cfg.get("trading_schedule", {}) or {}
    state = _load_state()
    now   = dt.datetime.now(ET)
    today = now.date().isoformat()

    if ts.get("weekdays_only", True) and now.weekday() >= 5:
        print(f"[trade-sched] {now:%a %H:%M ET} weekend — skip")
        return 0

    # Regular session only (scheduler-level guard; each engine re-checks too)
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    if not (open_t <= now < close_t):
        print(f"[trade-sched] {now:%H:%M ET} outside market hours — skip")
        return 0

    capitol_on = cfg.get("capitol_copier", {}).get("autorun_enabled", False)
    swing_on   = cfg.get("swing", {}).get("enabled", False)
    if not capitol_on and not swing_on:
        print("[trade-sched] all engines disabled in config — skip "
              "(flip swing.enabled / capitol_copier.autorun_enabled to go live)")
        return 0

    ran = []

    # ── 1. Exit engine (high frequency) ────────────────────────────────────
    if capitol_on:
        every = int(ts.get("manage_every_minutes", 20))
        last  = state.get("last_manage_at")
        due   = True
        if last:
            try:
                last_dt = dt.datetime.fromisoformat(last)
                due = (now - last_dt).total_seconds() >= every * 60 - 30
            except Exception:
                pass
        if due:
            print(f"[trade-sched] {now:%H:%M ET} → exit engine (manage-only)")
            import capitol_copier as cc
            try:
                cc.manage_only(dry_run=False, require_market_open=True)
                state["last_manage_at"] = now.isoformat()
                ran.append("manage")
            except Exception as e:
                print(f"[trade-sched] manage error: {e}", file=sys.stderr)

    # ── 2. Swing buyer (daily) ──────────────────────────────────────────────
    win = int(ts.get("window_minutes", 20))
    if swing_on and state.get("last_swing_date") != today and \
            _in_window(now, ts.get("swing_time_et", "10:00"), win):
        print(f"[trade-sched] {now:%H:%M ET} → swing buyer (daily)")
        import swing_buyer
        try:
            swing_buyer.run(dry_run=False)
            state["last_swing_date"] = today
            ran.append("swing")
        except Exception as e:
            print(f"[trade-sched] swing error: {e}", file=sys.stderr)

    # ── 3. Disclosure copy loop (daily) ─────────────────────────────────────
    if capitol_on and state.get("last_copy_date") != today and \
            _in_window(now, ts.get("copy_time_et", "10:30"), win):
        print(f"[trade-sched] {now:%H:%M ET} → capitol copy loop (daily)")
        import capitol_copier as cc
        try:
            cc.run(dry_run=False)
            state["last_copy_date"] = today
            ran.append("copy")
        except Exception as e:
            print(f"[trade-sched] copy error: {e}", file=sys.stderr)

    if ran:
        _save_state(state)
        print(f"[trade-sched] done: {', '.join(ran)}")
    else:
        print(f"[trade-sched] {now:%H:%M ET} nothing due this tick")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
