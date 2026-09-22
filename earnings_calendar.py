"""
earnings_calendar.py — "you hold X and it reports in two days."

An earnings gap jumps straight through any stop, broker-side or software: the
sell happens at whatever the open is. The only defence is knowing it is
coming. This pulls the calendar from Financial Modeling Prep (the key already
on file for company bios), caches it for twelve hours, and never raises —
without a key or a network it simply says nothing.

    upcoming(["TSLA", "NVDA"], "2026-10-26", lead_days=2)
        -> [("TSLA", "2026-10-28")]
    notice([...], session, lead_days)  -> one line for the morning report, or ""
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

FMP_URL = "https://financialmodelingprep.com/stable/earnings-calendar"
CACHE = os.path.join(HERE, ".earnings_cache.json")
CACHE_TTL_S = 12 * 3600
LOOKAHEAD_DAYS = 45


def _load_cache() -> dict:
    try:
        with open(CACHE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cache(d: dict) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".earn.", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def fetch(start: dt.date, end: dt.date) -> list[dict]:
    """Rows {symbol, date, ...} for the window; [] without a key or on failure.
    Cached on disk for CACHE_TTL_S so the morning report and the evening check
    share one request a day."""
    key = os.getenv("FMP_API_KEY", "")
    if not key:
        return []
    c = _load_cache()
    if (time.time() - float(c.get("fetched_at") or 0) < CACHE_TTL_S
            and c.get("start") == start.isoformat() and c.get("end") == end.isoformat()):
        return c.get("rows") or []
    try:
        r = requests.get(FMP_URL, params={"from": start.isoformat(), "to": end.isoformat(),
                                          "apikey": key}, timeout=20)
        rows = r.json() if r.status_code == 200 else []
        rows = [x for x in rows if isinstance(x, dict) and x.get("symbol") and x.get("date")]
    except Exception:
        return c.get("rows") or []          # stale beats silent when the network is down
    _save_cache({"fetched_at": time.time(), "start": start.isoformat(),
                 "end": end.isoformat(), "rows": rows})
    return rows


def _trading_days_between(a: dt.date, b: dt.date, trading_days=None) -> int:
    """Sessions strictly after `a` up to and including `b`. With no calendar
    given, weekdays — one day off around a holiday is fine for a warning."""
    if b <= a:
        return 0
    if trading_days:
        return sum(1 for d in trading_days if a < d <= b)
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def upcoming(symbols, session: str, lead_days: int = 2, rows=None, trading_days=None) -> list[tuple[str, str]]:
    """(symbol, YYYY-MM-DD) for held names that report within `lead_days`
    sessions AFTER `session` (inclusive of the day itself). Sorted by date."""
    try:
        s = dt.date.fromisoformat(str(session))
    except Exception:
        return []
    want = {str(x).upper() for x in (symbols or [])}
    if not want or lead_days is None or lead_days < 0:
        return []
    if rows is None:
        rows = fetch(s, s + dt.timedelta(days=LOOKAHEAD_DAYS))
    out = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        if sym not in want:
            continue
        try:
            d = dt.date.fromisoformat(str(r.get("date"))[:10])
        except Exception:
            continue
        if d < s:
            continue
        if _trading_days_between(s, d, trading_days) <= lead_days:
            out.append((sym, d.isoformat()))
    return sorted(set(out), key=lambda t: (t[1], t[0]))


def notice(symbols, session: str, lead_days: int = 2, rows=None, trading_days=None) -> str:
    hits = upcoming(symbols, session, lead_days, rows, trading_days)
    if not hits:
        return ""
    parts = []
    for sym, d in hits:
        when = dt.date.fromisoformat(d).strftime("%a %b %-d")
        parts.append(f"{sym} {when}")
    return ("📣 Earnings ahead for what you hold: " + ", ".join(parts)
            + ". A gap can jump straight through a stop — decide before the report.")


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] or ["TSLA", "NVDA", "AAPL"]
    print(notice(syms, dt.date.today().isoformat(), 5) or "nothing within 5 sessions")
