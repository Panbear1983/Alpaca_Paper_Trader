"""
price_streaks.py — Daily price streak indicator and consecutive accumulated % calculator.
========================================================================================
Tracks consecutive sessions of win (gain) or loss, plus cumulative percentage growth
over that streak.

Operates with two snapshots per session:
  - Snapshot 1 (Market Open / Intraday):
      Evaluates the active trading session's price (or live current_price) against yesterday's
      official completed close. If today is up, continues an up streak or starts +1. If down,
      continues a down streak or starts -1. If flat ($0.00 / 0.0%), resets to 0.
  - Snapshot 2 (Market Closed / EOD Finalized):
      Locks in today's official closing bar price against yesterday's close once the session ends
      (after 16:00 ET). Retains this finalized state outside market hours.

Display conventions:
  - Streak: +N (green), -N (red), 0 (gray).
  - Accumulated %: +X.X% (green), -X.X% (red), 0.0% (gray).

Pure and display-only: places no orders and mutates no trading configurations.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from rich.text import Text

import wallets as wl

ET = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets/v2"

logger = logging.getLogger(__name__)

# In-memory thread-safe cache for historical bars and streak results
_CACHE_LOCK = threading.Lock()
_BARS_CACHE: dict[str, list[dict]] = {}
_LAST_FETCH_TIME: float = 0.0
_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes TTL for historical bars


def _bar_date(bar: dict) -> str:
    """Extract session date YYYY-MM-DD from an Alpaca bar timestamp in ET."""
    t_val = str(bar.get("t", ""))
    if not t_val:
        return ""
    try:
        dt_obj = dt.datetime.fromisoformat(t_val.replace("Z", "+00:00"))
        return dt_obj.astimezone(ET).date().isoformat()
    except Exception:
        # Fallback to prefix if parsing fails
        return t_val[:10]


def compute_streak(closes: list[float]) -> tuple[int, float]:
    """
    Compute signed streak days and accumulated % from a sequence of prices.
    `closes` is ordered chronologically from oldest to newest.

    Returns:
        (streak_days, accumulated_pct)
        - streak_days: +N (win streak), -N (loss streak), 0 (flat / unknown)
        - accumulated_pct: cumulative % change from the close before streak began
    """
    if not closes or len(closes) < 2:
        return 0, 0.0

    k = len(closes) - 1
    diff = closes[k] - closes[k - 1]

    # Floating point tolerance
    if abs(diff) < 1e-6:
        return 0, 0.0

    if diff > 0:
        # Winning / positive streak
        count = 0
        idx = k
        while idx > 0 and (closes[idx] - closes[idx - 1]) > 1e-6:
            count += 1
            idx -= 1
        base_price = closes[idx]
        pct = ((closes[k] - base_price) / base_price * 100.0) if base_price > 0 else 0.0
        return count, pct

    else:
        # Losing / negative streak
        count = 0
        idx = k
        while idx > 0 and (closes[idx - 1] - closes[idx]) > 1e-6:
            count += 1
            idx -= 1
        base_price = closes[idx]
        pct = ((closes[k] - base_price) / base_price * 100.0) if base_price > 0 else 0.0
        return -count, pct


def calculate_streak(
    bars: list[dict],
    current_price: float | None = None,
    is_market_open: bool = False,
    now_et: dt.datetime | None = None,
) -> tuple[int, float]:
    """
    Calculate the streak for a symbol using daily bars, optional live current_price,
    and market status (Snapshot 1 vs Snapshot 2).
    """
    if not bars:
        return 0, 0.0

    if now_et is None:
        now_et = dt.datetime.now(ET)

    today_str = now_et.date().isoformat()

    # Sort bars chronologically
    sorted_bars = sorted(bars, key=lambda b: str(b.get("t", "")))

    if is_market_open:
        # Snapshot 1: Market is actively open today.
        # Historical completed closes are sessions strictly prior to today.
        completed_closes = [float(b["c"]) for b in sorted_bars if _bar_date(b) < today_str]

        # Determine today's active session price
        today_price: float | None = None
        if current_price is not None and float(current_price) > 0:
            today_price = float(current_price)
        else:
            # Check if there is an intraday bar for today in the list
            today_bars = [b for b in sorted_bars if _bar_date(b) == today_str]
            if today_bars:
                today_price = float(today_bars[-1]["c"])

        if today_price is not None:
            return compute_streak(completed_closes + [today_price])
        else:
            return compute_streak(completed_closes)

    else:
        # Snapshot 2: Market is closed (after hours, pre-market, weekend/holiday).
        # If it is after market close today (>= 16:00 ET), today's bar is completed.
        # If before market open (< 09:30 ET), today's session has not taken place yet.
        time_et = now_et.time()
        if time_et >= dt.time(16, 0):
            completed_closes = [float(b["c"]) for b in sorted_bars if _bar_date(b) <= today_str]
        else:
            completed_closes = [float(b["c"]) for b in sorted_bars if _bar_date(b) < today_str]

        return compute_streak(completed_closes)


def format_streak(streak: int | None) -> Text:
    """Format streak integer as Rich Text."""
    if streak is None:
        return Text("—", style="gray")
    if streak > 0:
        return Text(f"+{streak}", style="green")
    if streak < 0:
        return Text(f"{streak}", style="red")
    return Text("0", style="gray")


def format_streak_pct(pct: float | None, streak: int | None = None) -> Text:
    """Format accumulated percentage as Rich Text."""
    if pct is None or streak is None:
        return Text("—", style="gray")
    if streak == 0 or abs(pct) < 1e-6:
        return Text("0.0%", style="gray")
    if streak > 0:
        return Text(f"{pct:+.1f}%", style="green")
    return Text(f"{pct:+.1f}%", style="red")


def fetch_daily_bars(
    symbols: list[str],
    days: int = 30,
    creds: tuple[str, str, str] | None = None,
) -> dict[str, list[dict]]:
    """
    Fetch multi-symbol daily bars from Alpaca /stocks/bars.
    Safe read-only call; returns {symbol: [bar, ...]}.
    """
    if not symbols:
        return {}

    # Resolve credentials
    if creds:
        key, secret, _ = creds
    else:
        wallet_creds = wl.resolve(wl.current())
        if wallet_creds:
            key, secret, _ = wallet_creds
        else:
            key = os.getenv("ALPACA_API_KEY", "")
            secret = os.getenv("ALPACA_SECRET_KEY", "")

    if not key or not secret:
        return {}

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out: dict[str, list[dict]] = {}
    page_token = None

    try:
        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start,
                "adjustment": "split",
                "feed": "iex",
                "limit": 1000,
            }
            if page_token:
                params["page_token"] = page_token

            r = requests.get(f"{DATA_URL}/stocks/bars", headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            for sym, bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            page_token = data.get("next_page_token")
            if not page_token:
                break
    except Exception as e:
        logger.debug("Failed to fetch daily bars for streaks: %s", e)
        return out

    return out


def update_bars_cache(symbols: list[str], days: int = 30, creds: tuple[str, str, str] | None = None) -> None:
    """Fetch daily bars and store them in the global cache."""
    bars_by_sym = fetch_daily_bars(symbols, days=days, creds=creds)
    if bars_by_sym:
        with _CACHE_LOCK:
            _BARS_CACHE.update(bars_by_sym)


def get_cached_bars(symbol: str) -> list[dict]:
    """Retrieve cached bars for a symbol."""
    with _CACHE_LOCK:
        return list(_BARS_CACHE.get(symbol.upper(), []))


def get_streak_for_symbol(
    symbol: str,
    current_price: float | None = None,
    is_market_open: bool = False,
    now_et: dt.datetime | None = None,
) -> tuple[int, float]:
    """
    Get (streak, accumulated_pct) for a given symbol using cached daily bars.
    Returns (0, 0.0) if no bars are cached yet.
    """
    bars = get_cached_bars(symbol)
    if not bars:
        return 0, 0.0
    return calculate_streak(bars, current_price=current_price, is_market_open=is_market_open, now_et=now_et)
