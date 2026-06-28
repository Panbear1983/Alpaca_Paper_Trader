"""
report_scheduler.py — config-driven trigger for the daily portfolio report.

Replaces the rigid launchd schedule (hardcoded 16:08/17:08 + --gate-close). A
periodic launchd heartbeat runs this every few minutes; it reads the
`report_schedule` block from strategy_config.json and fires
`hermes_report.run_report` once per day when ET-now falls inside the configured
window. The schedule (time, on/off, channel) is now editable from config / the
TUI — not baked into the plist.

Idempotency: a last-fired-date in .report_schedule_state.json ensures exactly one
report per day even though the heartbeat ticks several times inside the window.

Run manually to test:  python3 report_scheduler.py
"""
import os
import json
import sys
import datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "strategy_config.json")
STATE_FILE = os.path.join(HERE, ".report_schedule_state.json")
ET = ZoneInfo("America/New_York")


def _load_schedule() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f).get("report_schedule", {}) or {}
    except Exception as e:
        print(f"[sched] cannot read config: {e}", file=sys.stderr)
        return {}


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main() -> int:
    cfg = _load_schedule()
    if not cfg.get("enabled", False):
        print("[sched] report_schedule disabled — skip")
        return 0

    now = dt.datetime.now(ET)
    if cfg.get("weekdays_only", True) and now.weekday() >= 5:
        print(f"[sched] {now:%a %H:%M ET} weekend — skip")
        return 0

    try:
        hh, mm = (int(x) for x in str(cfg.get("time_et", "16:00")).split(":"))
    except ValueError:
        print(f"[sched] bad time_et {cfg.get('time_et')!r}", file=sys.stderr)
        return 1

    window = int(cfg.get("window_minutes", 20))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    minutes_since = (now - target).total_seconds() / 60.0
    if not (0 <= minutes_since < window):
        print(f"[sched] {now:%H:%M ET} outside window "
              f"[{hh:02d}:{mm:02d}, +{window}m) — skip")
        return 0

    today = now.strftime("%Y-%m-%d")
    state = _load_state()
    if state.get("last_fired_date") == today:
        print(f"[sched] already fired today ({today}) — skip")
        return 0

    print(f"[sched] {now:%H:%M ET} in window — firing report")
    import hermes_report as hr
    hr.run_report(push=True, channel=cfg.get("channel"),
                  log=lambda m: print(m, flush=True))
    state["last_fired_date"] = today
    _save_state(state)
    print(f"[sched] done; marked fired for {today}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
