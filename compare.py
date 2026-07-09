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
from typing import Any

import requests

import wallets

# Portfolio-history period/timeframe per TUI range key. Reused from
# hermes_report (single source of truth — its "30Min 422s here" fix included).
from hermes_report import _PH_PERIOD, _PH_TF


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

    def get(path: str, **params) -> Any:
        r = requests.get(f"{base}{path}", headers=headers, params=params,
                         timeout=10)
        r.raise_for_status()
        return r.json()

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
    total_pl = equity - 100000.0
    total_pct = total_pl / 1000.0

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

    first_eq = next((eq for _, eq in hist if eq), 0.0)
    win_ret = ((hist[-1][1] / first_eq - 1) * 100) if (hist and first_eq) else None

    return {
        "name": name, "ok": True, "err": "",
        "equity": equity, "cash": cash, "lmv": lmv,
        "day_pl": day_pl, "day_pct": day_pct,
        "total_pl": total_pl, "total_pct": total_pct,
        "exposure": (lmv / equity) if equity else 0.0,
        "npos": len(positions),
        "positions": positions,
        "hist": hist,
        "win_ret": win_ret,                 # % return over range_key, or None
    }


def gather(range_key: str = "1M") -> list[dict[str, Any]]:
    """Snapshot every declared wallet (config order). Unconfigured wallets
    (missing .env creds) come back as ok=False rows so the leaderboard can
    still show they exist."""
    out = []
    for info in wallets.list_wallets():
        name = info["name"]
        creds = wallets.resolve(name)
        if creds is None:
            out.append({"name": name, "ok": False,
                        "err": "not configured — set " +
                               " / ".join(info["missing"]) + " in .env"})
            continue
        key, secret, base = creds
        out.append(snapshot(name, key, secret, base, range_key))
    return out
