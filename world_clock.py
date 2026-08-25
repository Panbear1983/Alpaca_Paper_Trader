"""
World Clock — Single source of truth for market-relevant timezones.
==================================================================
Call get_world_clock() to get current time in all relevant zones.
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass


@dataclass
class WorldClock:
    utc: datetime
    et: datetime          # America/New_York (market hours)
    cst: datetime         # Asia/Taipei (user local)
    utc_str: str
    et_str: str
    cst_str: str
    market_open: bool     # 9:30-16:00 ET, Mon-Fri
    market_state: str     # "PRE", "OPEN", "POST", "CLOSED"
    session_label: str    # e.g., "Mon 09:45 ET"


def get_world_clock() -> WorldClock:
    """Get current time in all relevant timezones + market state."""
    now_utc = datetime.now(timezone.utc)
    et = now_utc.astimezone(ZoneInfo("America/New_York"))
    cst = now_utc.astimezone(ZoneInfo("Asia/Taipei"))

    # Market hours: 9:30-16:00 ET, Monday-Friday
    et_hour = et.hour
    et_minute = et.minute
    et_weekday = et.weekday()  # 0=Mon, 6=Sun

    is_weekday = et_weekday < 5
    minutes_from_midnight = et_hour * 60 + et_minute
    market_open_min = 9 * 60 + 30   # 9:30
    market_close_min = 16 * 60      # 16:00

    if is_weekday and market_open_min <= minutes_from_midnight < market_close_min:
        market_open = True
        market_state = "OPEN"
    elif is_weekday and minutes_from_midnight < market_open_min:
        market_open = False
        market_state = "PRE"
    elif is_weekday and minutes_from_midnight >= market_close_min:
        market_open = False
        market_state = "POST"
    else:
        market_open = False
        market_state = "CLOSED"

    return WorldClock(
        utc=now_utc,
        et=et,
        cst=cst,
        utc_str=now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        et_str=et.strftime("%Y-%m-%d %H:%M:%S %Z"),
        cst_str=cst.strftime("%Y-%m-%d %H:%M:%S %Z"),
        market_open=market_open,
        market_state=market_state,
        session_label=et.strftime("%a %H:%M ET"),
    )


def is_market_open() -> bool:
    """Quick boolean check."""
    return get_world_clock().market_open


def get_market_state() -> str:
    """Return PRE/OPEN/POST/CLOSED."""
    return get_world_clock().market_state


if __name__ == "__main__":
    clock = get_world_clock()
    print(f"UTC:  {clock.utc_str}")
    print(f"ET:   {clock.et_str}  ← MARKET TIME")
    print(f"CST:  {clock.cst_str}  ← USER LOCAL")
    print(f"Market: {clock.market_state} ({clock.session_label})")