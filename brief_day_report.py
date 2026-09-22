#!/usr/bin/env python3
"""
brief_day_report.py — brief daily trading activity and P&L report.
Generates a concise report showing today's trades and day P&L for each wallet.
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List

import requests

import i18n
import wallets
import compare
import hermes_report as hr  # for _split_for_telegram and REPORTS_DIR
import telegram_notifier as tn

# Real ET, not hermes_report.ET — that one is a fixed dt.timezone(-4) with the
# comment "no DST correction needed for gate logic". It is correct only while
# EDT is in force and goes an hour wrong from 2026-11-01, which would shift
# every session bound and close-time check in this file.
ET = ZoneInfo("America/New_York")
_HERE = Path(__file__).resolve().parent

# ---- Helper functions (copied from wallet_report.py for independence) ----

def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def _m(v: float) -> str:
    return f"${v:,.0f}"

def _s(v: float) -> str:
    return f"{v:+,.0f}"

def _p(v: float) -> str:
    return f"{v:+.2f}%"

TRADE_LIST_MAX = 12   # above this, roll up per symbol


def _qty(q: float) -> str:
    """Trim trailing zeros so whole-share fills read '5', fractional '0.37'."""
    return f"{q:g}"

def _et_bound(session: str, t: "dt.time") -> str:
    """ISO timestamp for `session` at ET wall-clock time `t`, DST-correct."""
    return dt.datetime.combine(dt.date.fromisoformat(session), t,
                               tzinfo=ET).isoformat()


def _hdrs(key: str, secret: str) -> Dict[str, str]:
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json"}


def resolve_session(key: str, secret: str, base: str,
                    explicit: str | None = None) -> tuple[str, str | None]:
    """(session, previous_session) as ET YYYY-MM-DD, from Alpaca's calendar.

    A daily report must be anchored to a TRADING SESSION, not to the local
    clock. Taipei is 12h ahead of New York, so 'today' locally is a different
    day than the session being reported. If the session has not closed yet,
    we report the previous completed one.
    """
    now_et = dt.datetime.now(ET)
    start = (now_et.date() - dt.timedelta(days=14)).isoformat()
    end = (now_et.date() + dt.timedelta(days=1)).isoformat()
    try:
        cal = requests.get(f"{base}/calendar", headers=_hdrs(key, secret),
                           params={"start": start, "end": end}, timeout=15).json()
        days = [d["date"] for d in cal if d.get("date")]
    except Exception:
        days = []
    if not days:
        d = now_et.date().isoformat()
        return (explicit or d), None

    today = now_et.date().isoformat()
    closed = [d for d in days if d < today]
    if today in days and now_et.hour >= 16:
        closed.append(today)
    closed.sort()

    if explicit:
        session = explicit
    else:
        session = closed[-1] if closed else days[0]
    prior = [d for d in days if d < session]
    return session, (prior[-1] if prior else None)


def session_equity(key: str, secret: str, base: str,
                   session: str, prev: str | None) -> Dict[str, Any]:
    """Equity at the 16:00 ET close of `session` and of `prev`.

    Granularity matters more than it looks. The 1H portfolio-history series
    stops at 15:30, so taking its last point as "the close" silently drops the
    final half hour of trading (2026-08-26: 15:30 = 72,940.57 but the real
    16:00 close was 72,989.34 — a $49 error, and it flipped the sign of the
    derived unrealized figure). 15Min lands exactly on 16:00; fall back to
    coarser frames only if Alpaca has aged the intraday data out.

    account.last_equity is not usable here either: it still points at an older
    close once after-hours begins.
    """
    out: Dict[str, Any] = {"close": None, "prev_close": None,
                           "pl": None, "pct": None, "timeframe": None}
    s_d = dt.date.fromisoformat(session)
    start = (dt.date.fromisoformat(prev) if prev else s_d - dt.timedelta(days=5))
    for tf in ("15Min", "1H", "1D"):
        try:
            r = requests.get(f"{base}/account/portfolio/history", headers=_hdrs(key, secret),
                             params={"start": start.isoformat(),
                                     "end": (s_d + dt.timedelta(days=1)).isoformat(),
                                     "timeframe": tf, "extended_hours": "false"},
                             timeout=20)
            r.raise_for_status()
            j = r.json()
            pts = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(ET), e)
                   for t, e in zip(j.get("timestamp") or [], j.get("equity") or []) if e]

            def close_on(day: dt.date):
                # At or before the 16:00 bell, never after.
                vals = [e for d, e in pts
                        if d.date() == day and (d.hour, d.minute) <= (16, 0)]
                return vals[-1] if vals else None

            c = close_on(s_d)
            pc = close_on(dt.date.fromisoformat(prev)) if prev else None
            if c is not None and pc:
                out.update({"close": c, "prev_close": pc, "pl": c - pc,
                            "pct": (c - pc) / pc * 100.0, "timeframe": tf})
                return out
        except Exception:
            continue
    return out


def fetch_session_trades(key: str, secret: str, base: str,
                         session: str) -> List[Dict]:
    """Filled ORDERS for one wallet during `session`.

    Uses /orders, not /account/activities: activities returns raw partial
    fills and is page-capped at 100, so a busy session silently loses trades
    (2026-08-26: 44 orders arrived as 100 truncated fill fragments). /orders
    is already aggregated per order and takes limit=500.
    """
    try:
        r = requests.get(
            f"{base}/orders", headers=_hdrs(key, secret),
            params={"status": "all", "limit": 500, "direction": "asc",
                    # Build the bounds from the ET zone itself. A literal
                    # "-04:00" is EDT and silently shifts the window by an
                    # hour once DST ends (2026-11-01), pulling the wrong
                    # session's trades.
                    "after": _et_bound(session, dt.time(0, 0, 0)),
                    "until": _et_bound(session, dt.time(23, 59, 59))},
            timeout=20)
        r.raise_for_status()
        orders = r.json()
        if not isinstance(orders, list):
            return []
    except Exception:
        return []

    out = []
    for o in orders:
        if not o.get("filled_at"):
            continue
        try:
            qty = _f(o.get("filled_qty"))
            px = _f(o.get("filled_avg_price"))
            if qty <= 0:
                continue
            ts = dt.datetime.fromisoformat(
                str(o["filled_at"]).replace("Z", "+00:00")).astimezone(ET)
            out.append({
                "time_et": ts.strftime("%H:%M"),
                "side": "S" if str(o.get("side", "")).startswith("sell") else "B",
                "sym": o.get("symbol", "?"),
                "qty": qty,
                "price": px,
                "usd": qty * px,
                "_ts": ts,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["_ts"])
    for x in out:
        x.pop("_ts", None)
    return out


def gather_report_data(session: str | None = None):
    """Per-wallet snapshot + session P&L + session trades.

    Returns (results, trades_by_wallet, session_date).
    """
    results: List[Dict] = []
    trades: Dict[str, List[Dict]] = {}
    resolved = session
    for info in wallets.list_wallets():
        name = info["name"]
        creds = wallets.resolve(name)
        if creds is None:
            results.append({"name": name, "ok": False,
                            "err": "not configured — set " +
                                   " / ".join(info["missing"]) + " in .env"})
            continue
        key, secret, base = creds
        sess, prev = resolve_session(key, secret, base, session)
        resolved = resolved or sess
        try:
            r = compare.snapshot(name, key, secret, base, "1D")
        except Exception as e:
            r = {"name": name, "ok": False, "err": f"compare failed: {e}"}
        if r.get("ok"):
            eq = session_equity(key, secret, base, sess, prev)
            # Session marks replace compare's clock-based day_pl.
            if eq["pl"] is not None:
                r["equity"] = eq["close"]
                r["day_pl"] = eq["pl"]
                r["day_pct"] = eq["pct"]
                r["session_ok"] = True
            else:
                r["session_ok"] = False
            trades[name] = fetch_session_trades(key, secret, base, sess)
        results.append(r)
    return results, trades, (resolved or dt.datetime.now(ET).date().isoformat())


# ---- Report building ----

def build_brief_report(results: List[Dict], trades_by_wallet: Dict[str, List[Dict]],
                       session: str) -> str:
    """Brief report for one trading session: P&L and the trades behind it."""
    ok = [r for r in results if r.get("ok")]
    broken = [r for r in results if not r.get("ok")]

    L: List[str] = []
    L.append("📊 *Daily Report*")
    L.append(f"_Session {session} (ET)_")
    L.append("")

    if ok:
        tot_eq = sum(r["equity"] for r in ok if r.get("equity") is not None)
        priced = [r for r in ok if r.get("day_pl") is not None]
        tot_day = sum(r["day_pl"] for r in priced)
        base = tot_eq - tot_day
        L.append(f"*Combined*: Equity {_m(tot_eq)} · Day {_s(tot_day)} "
                 f"({_p(tot_day / base * 100 if base else 0.0)})")
        L.append("")

        for r in ok:
            name = r["name"]
            day_pl = r.get("day_pl")
            arrow = "🟢" if (day_pl or 0) >= 0 else "🔴"
            if day_pl is None:
                L.append(f"*{name}*: Equity {_m(r['equity'])} · "
                         f"_session P&L unavailable_")
            else:
                L.append(f"*{name}*: {arrow} Equity {_m(r['equity'])} · "
                         f"Day {_s(day_pl)} ({_p(r['day_pct'])})")
            tl = trades_by_wallet.get(name, [])
            if not tl:
                L.append("  _No trades_")
            elif len(tl) <= TRADE_LIST_MAX:
                L.append(f"  Trades ({len(tl)}):")
                for x in tl:
                    L.append(f"    {x['time_et']} {x['side']} {x['sym']:<6}"
                             f"{_qty(x['qty']):>10} @{x['price']:,.2f}")
            else:
                # Busy session: roll up per symbol so the report stays brief
                # and Telegram-sized instead of listing every order.
                agg: Dict[str, Dict[str, float]] = {}
                for x in tl:
                    a = agg.setdefault(x["sym"], {"n": 0, "b": 0, "s": 0, "net": 0.0})
                    a["n"] += 1
                    if x["side"] == "B":
                        a["b"] += 1
                        a["net"] += x["qty"]
                    else:
                        a["s"] += 1
                        a["net"] -= x["qty"]
                L.append(f"  Trades ({len(tl)} orders, {len(agg)} symbols):")
                for sym, a in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
                    L.append(f"    {sym:<6}{int(a['n']):>3}  "
                             f"{int(a['b'])}B/{int(a['s'])}S  net {a['net']:+,.2f}")
            L.append("")
    else:
        L.append("_No wallets with valid configuration_")
        L.append("")

    if broken:
        L.append("*Errors*")
        for r in broken:
            L.append(f"  {r['name']}: {r.get('err', '?')}")
        L.append("")

    return "\n".join(L).rstrip() + "\n"


def send_brief_report(push: bool = True, channel: str | None = None, log=print,
                      session: str | None = None) -> Dict:
    """Gather all wallets, build the session report, optionally push."""
    log("[1/2] Fetching all wallets…")
    results, trades, sess = gather_report_data(session)
    n_ok = sum(1 for r in results if r.get("ok"))
    log(f"[2/2] {n_ok}/{len(results)} wallets ok — session {sess} — building…")

    # Name the file by the SESSION it covers, not the wall clock, so reruns
    # overwrite instead of littering reports/ with near-identical copies.
    md_path = hr.REPORTS_DIR / f"brief_day_{sess}.md"
    report = build_brief_report(results, trades, sess)
    md_path.write_text(report, encoding="utf-8")
    log(f"✓ Report: {md_path}")

    sent = False
    if push:
        log("[Telegram] Sending to Telegram…")
        parts = hr._split_for_telegram(report)
        sent_parts = 0
        for i_, part in enumerate(parts, 1):
            if tn.send(part, parse_mode="Markdown", channel=channel):
                sent_parts += 1
            else:
                log(f"[tg] send failed (part {i_}/{len(parts)}) — check channel env vars")
        sent = sent_parts == len(parts) and sent_parts > 0
        log("✓ Done." if sent else "⚠ not (fully) delivered.")
    else:
        log("(push disabled — Telegram skipped)")

    return {"report": report, "md_path": md_path, "sent": sent}

def _cli_lang() -> None:
    """CLI runs outside the TUI — pick up the cockpit's language setting."""
    try:
        with open(_HERE / "strategy_config.json") as f:
            lang = (json.load(f).get("tui", {}) or {}).get("language", "en")
        i18n.set_lang(lang)
    except Exception:
        pass

def _cli_session() -> str | None:
    """--session YYYY-MM-DD regenerates any past trading day."""
    for i, a in enumerate(sys.argv):
        if a == "--session" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--session="):
            return a.split("=", 1)[1]
    return None


if __name__ == "__main__":
    _cli_lang()
    no_push = "--no-push" in sys.argv
    res = send_brief_report(push=not no_push, session=_cli_session())
    if no_push:
        print("\n" + res["report"])
    sys.exit(0 if (res["sent"] or no_push) else 1)