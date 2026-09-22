"""
World Clock Utility — Single Source of Truth for Time & Timezone
================================================================
Provides timezone-aware datetime objects for consistent time handling
across all trading logic. Uses IANA time zone database via pytz.
"""
from datetime import datetime, time, timedelta
import pytz
from typing import Optional
import time as _time_mod

_CLOCK_CACHE: dict = {"t": 0.0, "data": None}
_CLOCK_TTL = 20  # seconds — real enough for a UI badge/scheduler gate, gentle on Alpaca


def _alpaca_clock() -> Optional[dict]:
    """The real market clock (holiday-aware) from Alpaca's own /clock endpoint.

    Returns None on any failure so callers can fall back rather than crash —
    this runs inside tight polling loops (price_watcher) that must not die
    over a transient network hiccup.
    """
    now = _time_mod.time()
    if now - _CLOCK_CACHE["t"] < _CLOCK_TTL and _CLOCK_CACHE["data"] is not None:
        return _CLOCK_CACHE["data"]
    try:
        import capitol_copier as cc
        import requests
        r = requests.get(f"{cc.BASE_URL}/clock", headers=cc.ALPACA_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        _CLOCK_CACHE["t"] = now
        _CLOCK_CACHE["data"] = data
        return data
    except Exception:
        return None


class WorldClock:
    """Singleton-like clock for consistent time references."""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        # Initialize timezones
        self.utc = pytz.UTC
        self.ny = pytz.timezone('America/New_York')
        self.taipei = pytz.timezone('Asia/Taipei')
    
    def now(self, tz: str = 'UTC') -> datetime:
        """Get current time in specified timezone."""
        utc_now = datetime.now(self.utc)
        if tz == 'UTC':
            return utc_now
        elif tz == 'NY':
            return utc_now.astimezone(self.ny)
        elif tz == 'Taipei':
            return utc_now.astimezone(self.taipei)
        else:
            # Allow any IANA timezone
            try:
                target_tz = pytz.timezone(tz)
                return utc_now.astimezone(target_tz)
            except pytz.exceptions.UnknownTimeZoneError:
                raise ValueError(f"Unknown timezone: {tz}")
    
    def ny_time(self) -> datetime:
        """Get current New York time (market timezone)."""
        return self.now('NY')
    
    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        """
        Check if market is open at given time (defaults to now).

        Every caller in this codebase asks about "now", so the real Alpaca
        clock (holiday-aware) is checked first and used whenever available.
        The Mon-Fri/9:30-16:00 rule below only covers a transient API failure
        — it does NOT know about market holidays, so it will say "open" on a
        day like Labor Day even though the exchange is shut.
        """
        live = _alpaca_clock()
        if live is not None:
            return bool(live.get("is_open"))

        if dt is None:
            dt = self.ny_time()

        # Convert to NY time if needed
        if dt.tzinfo != self.ny:
            dt = dt.astimezone(self.ny)

        # Check weekday (Monday=0, Friday=4)
        if dt.weekday() > 4:  # Saturday=5, Sunday=6
            return False

        # Check time (9:30-16:00)
        market_open = time(9, 30)
        market_close = time(16, 0)
        current_time = dt.time()

        return market_open <= current_time < market_close
    
    def time_to_open(self) -> Optional[float]:
        """Hours until market opens (returns None if already open)."""
        now = self.ny_time()
        if self.is_market_open(now):
            return 0.0
        
        # If weekend, calculate to next Monday
        if now.weekday() >= 5:  # Saturday or Sunday
            days_ahead = 7 - now.weekday()  # Days to next Monday
            target = now.replace(hour=9, minute=30, second=0, microsecond=0) + \
                     timedelta(days=days_ahead)
        else:
            # Weekday but before open or after close
            if now.time() < time(9, 30):
                # Today at 9:30
                target = now.replace(hour=9, minute=30, second=0, microsecond=0)
            else:
                # After close, next day at 9:30
                target = now.replace(hour=9, minute=30, second=0, microsecond=0) + \
                         timedelta(days=1)
        
        delta = target - now
        return delta.total_seconds() / 3600
    
    def time_to_close(self) -> Optional[float]:
        """Hours until market closes (returns None if already closed)."""
        now = self.ny_time()
        if not self.is_market_open(now):
            return 0.0
        
        # Today at 16:00
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        delta = target - now
        return delta.total_seconds() / 3600


# Convenience functions for backward compatibility
def get_ny_time() -> datetime:
    """Get current New York time."""
    return WorldClock().ny_time()


def is_market_open(dt: Optional[datetime] = None) -> bool:
    """Check if market is open."""
    return WorldClock().is_market_open(dt)


def time_to_market_open() -> Optional[float]:
    """Hours until market opens."""
    return WorldClock().time_to_open()


def time_to_market_close() -> Optional[float]:
    """Hours until market closes."""
    return WorldClock().time_to_close()


if __name__ == "__main__":
    # Quick test
    clock = WorldClock()
    print("=== World Clock Test ===")
    print(f"UTC:     {clock.now('UTC')}")
    print(f"NY:      {clock.now('NY')}")
    print(f"Taipei:  {clock.now('Taipei')}")
    print(f"Market Open: {clock.is_market_open()}")
    print(f"Time to open: {clock.time_to_open():.2f} hours")
    print(f"Time to close: {clock.time_to_close():.2f} hours")