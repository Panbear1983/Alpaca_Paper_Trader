#!/bin/bash
# Hermes daily-report cron wrapper.
# Fires hourly via launchd. Self-gates on ET clock: only runs at 4:05 PM ET
# on weekdays. DST-aware via zoneinfo.

set -euo pipefail
SCRIPT_DIR="/Users/peter/Desktop/Old_Projects/GitHub/Alpaca_Paper_Trader"
LOG_FILE="$SCRIPT_DIR/reports/cron.log"
mkdir -p "$SCRIPT_DIR/reports"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] cron tick" >> "$LOG_FILE"

# Gate: only run between 16:00 and 16:59 ET on weekdays (Mon=0..Fri=4)
SHOULD_RUN=$(/usr/bin/python3 <<'PY'
import datetime, sys
try:
    from zoneinfo import ZoneInfo
except ImportError:
    import pytz
    et = datetime.datetime.now(pytz.timezone("America/New_York"))
else:
    et = datetime.datetime.now(ZoneInfo("America/New_York"))
ok = (et.weekday() < 5) and (et.hour == 16)
print("YES" if ok else "NO")
PY
)

if [[ "$SHOULD_RUN" != "YES" ]]; then
    echo "  → gate: skip (not weekday 4PM ET)" >> "$LOG_FILE"
    exit 0
fi

echo "  → gate: PASS — running report" >> "$LOG_FILE"
cd "$SCRIPT_DIR"
/usr/bin/python3 hermes_report.py >> "$LOG_FILE" 2>&1
echo "  → exit=$?" >> "$LOG_FILE"
