"""
compare.py — read-only side-by-side snapshots of EVERY configured wallet.

Fetches account / positions / portfolio-history for each wallet declared in
strategy_config.json's "wallets" block, passing credentials PER REQUEST.
Deliberately does NOT go through wallets.apply(): that rebinds process-global
credential constants (the single-active-wallet mechanism), and a mid-refresh
rebind would cross-wire the main screen's data with the compare fetch. Here
every call carries its own headers, so the active wallet is never disturbed.

Read-only: only GET endpoints (/account, /positions, /account/portfolio/history)
are used — nothing in this module can place, change, or cancel an order.
"""
from __future__ import annotations

import datetime as dt
import zoneinfo
from typing import Any

import requests

import wallets

# Portfolio-history period/timeframe per TUI range key. Reused from
# hermes_report (single source of truth — its "30Min 422s here" fix included).
from hermes_report import _PH_PERIOD, _PH_TF


ACCOUNT_BASELINE_USD = 100_000.0

_ET = zoneinfo.ZoneInfo("America/New_York")


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def snapshot(name: str, key: str, secret: str, base: str,
             range_key: str = "1M") -> dict[str, Any]:
    """One wallet's account + positions + equity history, with derived metrics.
    Never raises: failures come back as {"ok": False, "err": …} so one broken
    wallet can't blank the whole compare page."""
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
               "Accept": "application/json"}
    session = requests.Session()
    session.headers.update(headers)

    def get(path: str, **params) -> Any:
        import time
        for attempt in range(3):
            try:
                r = session.get(f"{base}{path}", params=params, timeout=10)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(1)

    try:
        acct = get("/account")
        positions = get("/positions")
        if not isinstance(positions, list):
            positions = []
    except Exception as e:
        return {"name": name, "ok": False, "err": str(e)}

    equity = _f(acct.get("equity"))
    last = _f(acct.get("last_equity"), equity)
    cash = _f(acct.get("cash"))
    lmv = _f(acct.get("long_market_value"))
    day_pl = equity - last
    day_pct = (day_pl / last * 100) if last else 0.0
    # Same convention as the main summary line: paper accounts start at $100k.
    # Do not use the first Alpaca history point as the baseline; for a 1M view
    # that can be a mid-run equity like $102.4k and makes total figures drift.
    total_pl = equity - ACCOUNT_BASELINE_USD
    total_pct = ((total_pl / ACCOUNT_BASELINE_USD * 100)
                 if ACCOUNT_BASELINE_USD else 0.0)

    # Equity history over the compare window. Fetched separately so a history
    # hiccup degrades to "no chart" instead of killing the leaderboard row.
    hist: list[tuple[str, float]] = []      # (iso_ts_utc, equity)
    try:
        h = get("/account/portfolio/history",
                period=_PH_PERIOD.get(range_key, "1M"),
                timeframe=_PH_TF.get(range_key, "1D"))
        for ts, eq in zip(h.get("timestamp", []), h.get("equity", [])):
            # Skip gaps AND zero-equity points: Alpaca reports equity=0 for
            # days before an account was funded, which would spike a
            # normalized overlay down to 0 (same family as the chart_equity
            # None-gap bug fixed 2026-07-03).
            if not eq:
                continue
            iso = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()
            hist.append((iso, float(eq)))
    except Exception:
        pass

    # Append the LIVE equity as the final point. Daily history ends at
    # YESTERDAY'S close, so a wallet that rallied today still charted
    # downward while the (live) header said it was up — two clocks in one
    # section. With this, every curve ends at NOW.
    #
    # Exception — 1D outside the last session's day: pre/post-market Alpaca's
    # 1D history is the PREVIOUS session, and the 1D chart labels are
    # time-only (%H:%M), so a "now" point from a later ET day parses as
    # earlier than the session bars and the plotted line doubles back across
    # the chart. Skip the append then; the chart shows the last full session.
    if hist and equity:
        now = dt.datetime.now(dt.timezone.utc)
        if range_key == "1D":
            last = dt.datetime.fromisoformat(hist[-1][0])
            if last.astimezone(_ET).date() != now.astimezone(_ET).date():
                now = None
        if now is not None:
            hist.append((now.isoformat(), equity))

    win_ret = ((equity / ACCOUNT_BASELINE_USD - 1) * 100) if equity else None

    return {
        "name": name, "ok": True, "err": "",
        "equity": equity, "cash": cash, "lmv": lmv,
        "day_pl": day_pl, "day_pct": day_pct,
        "total_pl": total_pl, "total_pct": total_pct,
        "exposure": (lmv / equity) if equity else 0.0,
        "npos": len(positions),
        "positions": positions,
        "hist": hist,
        "win_ret": win_ret,                 # % return vs $100k baseline, or None
        "baseline_usd": ACCOUNT_BASELINE_USD,
    }


def gather(range_key: str = "1M") -> list[dict[str, Any]]:
    """Snapshot every declared wallet (config order). Unconfigured wallets
    (missing .env creds) come back as ok=False rows so the leaderboard can
    still show they exist."""
    import concurrent.futures
    wallet_list = wallets.list_wallets()
    
    def process_wallet(info):
        name = info["name"]
        creds = wallets.resolve(name)
        if creds is None:
            return {"name": name, "ok": False,
                    "err": "not configured — set " +
                           " / ".join(info["missing"]) + " in .env"}
        key, secret, base = creds
        return snapshot(name, key, secret, base, range_key)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wallet_list) or 1) as executor:
        out = list(executor.map(process_wallet, wallet_list))
        
    return out
