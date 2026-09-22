#!/usr/bin/env python3
"""
market_context.py — the outside-world half of Wanna Buffet's daily report.

wb_daily_report.py on its own can only see the account, so the analyst had
nothing to say beyond "these scores went up and those went down". This module
supplies the market picture that makes a strategic read possible: which sectors
money is rotating into, where SPY sits against its own trend, how the book is
distributed across sectors, and what the newswire is saying about the names we
actually hold.

Everything here is fetched, never guessed. Sources, all verified reachable:
  - sector rotation : Alpaca daily bars for the 11 SPDR sector ETFs
  - regime          : Alpaca daily bars for SPY vs its 50/200 day averages
  - book sectors    : FMP /stable/profile (cached; sectors rarely change)
  - headlines       : Alpaca /v1beta1/news (Benzinga wire)

Bloomberg has no public API and its content is paywalled, so it is not a
source here and nothing in this file should pretend otherwise.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

ET = ZoneInfo("America/New_York")
_HERE = Path(__file__).resolve().parent
SECTOR_CACHE = _HERE / ".sector_cache.json"
REGIME_CACHE = _HERE / ".regime_cache.json"
DATA_URL = "https://data.alpaca.markets"

# SPDR sector ETFs. The map from FMP's sector names to these tickers is a
# taxonomy, not a strategy setting — it is here so the book can be compared
# to rotation data on the same axis.
SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE",
    "Healthcare": "XLV", "Industrials": "XLI", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Utilities": "XLU", "Basic Materials": "XLB",
    "Communication Services": "XLC", "Real Estate": "XLRE",
}
ETF_LABEL = {v: k for k, v in SECTOR_ETF.items()}


def _keys() -> Dict[str, str]:
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "")}


def _bars(symbols: List[str], start: str, limit: int = 10000) -> Dict[str, List[dict]]:
    try:
        r = requests.get(f"{DATA_URL}/v2/stocks/bars", headers=_keys(),
                         params={"symbols": ",".join(symbols), "timeframe": "1Day",
                                 "start": start, "feed": "iex", "limit": limit},
                         timeout=30)
        r.raise_for_status()
        return r.json().get("bars") or {}
    except Exception:
        return {}


def sector_rotation(session: str, lookback_days: int = 7) -> List[Dict[str, Any]]:
    """Per-sector 1-day and lookback-window change, most recent session last."""
    start = (dt.date.fromisoformat(session) - dt.timedelta(days=lookback_days + 12)).isoformat()
    syms = sorted(SECTOR_ETF.values()) + ["SPY"]
    bars = _bars(syms, start)
    out = []
    for sym, b in bars.items():
        b = [x for x in b if x["t"][:10] <= session]
        if len(b) < 2:
            continue
        window = b[-(lookback_days + 1):] if len(b) > lookback_days else b
        out.append({
            "sym": sym,
            "label": "S&P 500" if sym == "SPY" else ETF_LABEL.get(sym, sym),
            "d1": (b[-1]["c"] - b[-2]["c"]) / b[-2]["c"] * 100,
            "dn": (b[-1]["c"] - window[0]["c"]) / window[0]["c"] * 100,
            "days": len(window) - 1,
        })
    return sorted(out, key=lambda x: -x["d1"])


def spy_regime(session: str) -> Dict[str, Any]:
    """SPY against its own 50 and 200 day averages. Regime, not prediction."""
    start = (dt.date.fromisoformat(session) - dt.timedelta(days=420)).isoformat()
    b = [x for x in (_bars(["SPY"], start).get("SPY") or []) if x["t"][:10] <= session]
    if len(b) < 60:
        return {}
    closes = [x["c"] for x in b]
    out: Dict[str, Any] = {"close": closes[-1], "bars": len(closes)}
    for n in (50, 200):
        if len(closes) >= n:
            sma = sum(closes[-n:]) / n
            out[f"sma{n}"] = sma
            out[f"vs{n}"] = (closes[-1] - sma) / sma * 100
    return out


def regime_state(session: str | None = None, force: bool = False) -> Dict[str, Any]:
    """{'state': 'bull'|'neutral'|'bear', ...} from SPY vs its 50/200 day averages.

        bull    above both the 50 and the 200 day average
        neutral above the 200 but below the 50
        bear    below the 200 day average

    Cached per session date. spy_regime() fetches ~290 daily bars and takes the
    better part of a second, which is far too heavy for the order path — and
    that is exactly where this gets called, on every trade from every engine.
    The 50 and 200 day averages barely move intraday, so once per session is
    the right granularity.

    Returns state 'unknown' if SPY cannot be read. Callers must treat that as
    "no regime opinion" and fall back to their unconditional limits rather than
    guessing a direction.
    """
    if session is None:
        session = dt.datetime.now(ET).date().isoformat()
    if not force:
        try:
            cached = json.loads(REGIME_CACHE.read_text())
            if cached.get("session") == session:
                return cached
        except Exception:
            pass

    r = spy_regime(session)
    out: Dict[str, Any] = {"session": session, "state": "unknown"}
    if r.get("close") and "vs200" in r:
        above200 = r["vs200"] >= 0
        above50 = r.get("vs50", 0) >= 0
        out["state"] = "bull" if (above200 and above50) else ("neutral" if above200 else "bear")
        out.update({k: r[k] for k in ("close", "sma50", "sma200", "vs50", "vs200") if k in r})
    try:
        REGIME_CACHE.write_text(json.dumps(out, indent=2))
    except Exception:
        pass
    return out


def book_sectors(symbols: List[str]) -> Dict[str, str]:
    """symbol -> sector, via FMP profile, cached to disk (sectors are static)."""
    cache: Dict[str, str] = {}
    if SECTOR_CACHE.exists():
        try:
            cache = json.loads(SECTOR_CACHE.read_text())
        except Exception:
            cache = {}
    key = os.getenv("FMP_API_KEY", "")
    missing = [s for s in symbols if s.upper() not in cache]
    for sym in missing:
        if not key:
            break
        try:
            r = requests.get("https://financialmodelingprep.com/stable/profile",
                             params={"symbol": sym, "apikey": key}, timeout=15)
            j = r.json()
            if isinstance(j, list) and j and j[0].get("sector"):
                cache[sym.upper()] = j[0]["sector"]
        except Exception:
            continue
    if missing:
        try:
            SECTOR_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))
        except Exception:
            pass
    return {s.upper(): cache.get(s.upper(), "Unknown") for s in symbols}


def market_news(symbols: List[str], session: str, limit: int = 8) -> List[Dict[str, str]]:
    """Headlines about the names we hold, topped up with broad-market ones.

    The unfiltered wire is mostly crypto and politics; asking for our symbols
    explicitly is what makes this worth feeding to the analyst at all.
    """
    def _get(params: dict) -> List[dict]:
        try:
            r = requests.get(f"{DATA_URL}/v1beta1/news", headers=_keys(),
                             params=params, timeout=25)
            r.raise_for_status()
            return r.json().get("news") or []
        except Exception:
            return []

    since = (dt.date.fromisoformat(session) - dt.timedelta(days=2)).isoformat()
    rows: List[Dict[str, str]] = []
    seen = set()

    def _add(arts, scope):
        for a in arts:
            h = (a.get("headline") or "").strip()
            if not h or h in seen:
                continue
            seen.add(h)
            rows.append({"headline": h, "source": a.get("source", ""),
                         "symbols": ",".join(a.get("symbols") or []),
                         "scope": scope})

    if symbols:
        _add(_get({"symbols": ",".join(sorted({s.upper() for s in symbols})),
                   "start": since, "limit": limit * 2, "sort": "desc"}), "holding")
    # A little broad-market context, but never more than a third of the list.
    _add(_get({"symbols": "SPY,QQQ,DIA", "start": since,
               "limit": max(3, limit // 3), "sort": "desc"}), "market")
    return rows[:limit]


def build(held: List[Dict[str, Any]], session: str) -> Dict[str, Any]:
    """Everything above, assembled. Any piece may come back empty."""
    syms = [h["sym"] for h in held]
    sectors = book_sectors(syms)
    by_sector: Dict[str, float] = {}
    for h in held:
        by_sector[sectors.get(h["sym"].upper(), "Unknown")] = \
            by_sector.get(sectors.get(h["sym"].upper(), "Unknown"), 0.0) + h.get("mv", 0.0)
    return {
        "rotation": sector_rotation(session),
        "regime": spy_regime(session),
        "book_sectors": sectors,
        "book_by_sector": dict(sorted(by_sector.items(), key=lambda kv: -kv[1])),
        "news": market_news(syms, session),
    }


def as_facts(ctx: Dict[str, Any]) -> str:
    """Compact plain-text block for the analyst prompt."""
    L: List[str] = []
    rot = ctx.get("rotation") or []
    if rot:
        n = rot[0]["days"]
        L.append(f"SECTOR ROTATION (1-day %, then {n}-session %):")
        for r in rot:
            L.append(f"  {r['label']:<24} {r['d1']:+6.2f}   {r['dn']:+6.2f}")
    reg = ctx.get("regime") or {}
    if reg.get("close"):
        bits = [f"SPY {reg['close']:,.2f}"]
        for n in (50, 200):
            if f"vs{n}" in reg:
                bits.append(f"{reg[f'vs{n}']:+.2f}% vs its {n}-day average")
        L.append("REGIME: " + ", ".join(bits))
    bbs = ctx.get("book_by_sector") or {}
    if bbs:
        tot = sum(bbs.values()) or 1.0
        L.append("BOOK BY SECTOR (market value, share of book):")
        for s, v in bbs.items():
            L.append(f"  {s:<24} ${v:,.0f}  {v / tot * 100:.0f}%")
    news = ctx.get("news") or []
    if news:
        L.append("HEADLINES (newswire, most recent first):")
        for a in news:
            tag = f" [{a['symbols']}]" if a["symbols"] else ""
            L.append(f"  - {a['headline'][:120]}{tag}")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    sess = sys.argv[1] if len(sys.argv) > 1 else dt.datetime.now(ET).date().isoformat()
    demo = [{"sym": s, "mv": 1000.0} for s in ("BE", "META", "ISRG", "F", "INTC")]
    print(as_facts(build(demo, sess)))
