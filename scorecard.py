"""
scorecard.py — who is actually earning the High Risk wallet's return (2026-09-23)
=================================================================================
Phase 2 needs an honest answer to one question before the wallet is handed
back to the agent: since the day Peter took over by hand, how has the wallet
done against QQQ, and which door did the money come through — his manual
orders, the swing engine, the anchor engine, the stops?

Two lines in the daily wallets report, for the boxed wallet only:

  🎯 Since 2026-08-29: wallet +1.4% · QQQ +3.5% · gap -2.1 pts
     realized by who opened the trade: manual +$812 (7) · swing -$40 (1)

Pure functions here take data; `scorecard(wallet)` gathers it. Nothing in
this file places orders or writes state.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_SINCE = "2026-08-29"          # the day Peter started trading the wallet by hand


def since_for(cfg: dict) -> str:
    return str((cfg.get("anchor") or {}).get("scorecard_since") or DEFAULT_SINCE)


def equity_since(history: dict, since: str) -> tuple[str, float] | None:
    """(date, equity) of the first daily close on/after `since` in Alpaca's
    portfolio history payload {"timestamp": [...], "equity": [...]}."""
    pts = []
    for ts, eq in zip(history.get("timestamp") or [], history.get("equity") or []):
        if eq:
            pts.append((dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone(ET).date().isoformat(), float(eq)))
    for d, eq in sorted(pts):
        if d >= since:
            return d, eq
    return None


def close_since(closes: dict[str, float], since: str) -> tuple[str, float] | None:
    for d in sorted(closes):
        if d >= since and closes[d]:
            return d, float(closes[d])
    return None


def by_source(trades: list[dict], since: str) -> dict[str, dict]:
    """Realized P&L and count per opening source for trades closed on/after
    `since`. performance_tracker credits a round trip to the engine that
    OPENED it, which is the right attribution here."""
    out: dict[str, dict] = {}
    for t in trades:
        if str(t.get("exit_date") or "")[:10] < since:
            continue
        src = str(t.get("strategy") or "unattributed")
        row = out.setdefault(src, {"n": 0, "pnl": 0.0, "wins": 0})
        row["n"] += 1
        row["pnl"] += float(t.get("pnl_usd") or 0)
        row["wins"] += 1 if float(t.get("pnl_usd") or 0) > 0 else 0
    return out


def lines(since: str, w_then: float | None, w_now: float | None, b_then: float | None, b_now: float | None,
          sources: dict[str, dict], t=None) -> list[str]:
    """The report lines. `t` is the translator (i18n.t); English without it."""
    if t is None:
        t = _english
    if not (w_then and w_now and b_then and b_now):
        return []
    w = (w_now / w_then - 1) * 100
    b = (b_now / b_then - 1) * 100
    out = [t("rpt.score", since=since, w=f"{w:+.1f}%", b=f"{b:+.1f}%", gap=f"{w - b:+.1f}")]
    if sources:
        label = {"unattributed": "untagged/legacy"}          # opened before order tagging existed
        parts = "  ·  ".join(f"{label.get(src, src)} {row['pnl']:+,.0f} ({row['n']})"
                             for src, row in sorted(sources.items(), key=lambda kv: -kv[1]["pnl"]))
        out.append(t("rpt.score_src", parts=parts))
    return out


def _english(key: str, **kw) -> str:
    return {"rpt.score": "🎯 Since {since}: wallet {w} · QQQ {b} · gap {gap} pts",
            "rpt.score_src": "   realized by who opened the trade: {parts}"}[key].format(**kw)


def scorecard(wallet: str = "High Risk", t=None) -> list[str]:
    """Live gather for the boxed wallet; [] for any other wallet or on any error."""
    try:
        import requests
        import capitol_copier as cc
        import strategies
        import wallets
        if cc.hard_limits_for(wallet) is None:
            return []
        creds = wallets.resolve(wallet)
        if creds is None:
            return []
        key, secret, base = creds
        if not base.rstrip("/").endswith("/v2"):
            base = base.rstrip("/") + "/v2"
        hdrs = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        since = since_for(strategies.load_merged(wallet))
        days = (dt.date.today() - dt.date.fromisoformat(since)).days + 40
        period = "1M" if days <= 31 else "3M" if days <= 93 else "1A"
        hist = requests.get(f"{base}/account/portfolio/history", headers=hdrs, timeout=15,
                            params={"period": period, "timeframe": "1D", "extended_hours": "false"}).json() or {}
        acct = requests.get(f"{base}/account", headers=hdrs, timeout=10).json() or {}
        from swing_buyer import fetch_daily_bars
        qqq = {b["t"][:10]: b["c"] for b in (fetch_daily_bars(["QQQ"], days=days).get("QQQ") or [])}
        w0 = equity_since(hist, since)
        b0 = close_since(qqq, since)
        b_now = qqq[max(qqq)] if qqq else None
        try:
            import performance_tracker as pt
            trades = pt.load_log().get("trades") or []
        except Exception:
            trades = []
        return lines(since, w0[1] if w0 else None, float(acct.get("equity") or 0) or None,
                     b0[1] if b0 else None, b_now, by_source(trades, since), t)
    except Exception:
        return []


if __name__ == "__main__":
    for line in scorecard():
        print(line)
