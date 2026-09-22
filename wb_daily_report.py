#!/usr/bin/env python3
"""
wb_daily_report.py — Wanna Buffet's own daily report for the HIGH RISK wallet.

Deliberately separate from brief_day_report.py, which covers all three wallets.
This one is single-wallet and adds what the multi-wallet report cannot: realized
P&L per closed trade, the ISR signal scores behind each holding, an analyst
assessment tying signals to actual results, and a one-line projection derived
from ISR's own computed intent for the next session.

Target length is 100 words. The analyst model can fail or time out; when it
does the numbers still send.

  python3 wb_daily_report.py                    push today's session
  python3 wb_daily_report.py --no-push          print only
  python3 wb_daily_report.py --no-analyst       numbers only, no model call
  python3 wb_daily_report.py --session 2026-08-25
"""
import datetime as dt
import json
import os
import re
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

WALLET = "High Risk"   # this report is Wanna Buffet's own wallet only
WORD_BUDGET = 160   # numbers stay terse; the strategic take needs room
REVIEW_AFTER_SESSIONS = 7   # diary length that triggers the outlook-logic review

# Only the scheduled launchd job may push. Rule 6 in the agent's SOUL.md says
# the same thing in words, but an instruction is a request — this is the part
# that holds when an agent turn runs this script while doing something else
# (2026-08-27 14:08: the inverse_hedge cron agent did exactly that).
SCHEDULE_TOKEN_ENV = "WB_REPORT_SCHEDULED"
# Voice defaults track the Hermes profile so the report sounds like the agent
# you actually talk to. macOS `say` is only the fallback — this Mac has no
# Enhanced/Premium voices installed, so its output is the robotic compact set.
VOICE = os.environ.get("WB_VOICE", "")          # "" = read from Hermes config
VOICE_RATE = int(os.environ.get("WB_VOICE_RATE", "175"))   # `say` fallback only
SAY_FALLBACK_VOICE = os.environ.get("WB_SAY_VOICE", "Samantha")
HERMES_PROFILE = os.environ.get("ALPACA_ANALYST_PROFILE", "wanna_buffet")
# Wanna Buffet's trading diary. Separate from reports/, which holds 240+
# files from the multi-wallet report and its charts — the daily record is
# unreadable buried in there.
DIARY_DIR = _HERE / "diary"
STATE_FILE = DIARY_DIR / "state.json"
HISTORY_FILE = DIARY_DIR / "history.jsonl"


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


def realized_pl(trades: List[Dict]) -> Dict[str, float]:
    """FIFO-matched realized P&L per symbol for one session."""
    lots: Dict[str, List[List[float]]] = {}
    pl: Dict[str, float] = {}
    for t in trades:
        sym, qty, px = t["sym"], t["qty"], t["price"]
        q = lots.setdefault(sym, [])
        if t["side"] == "B":
            q.append([qty, px])
        else:
            left = qty
            while left > 1e-9 and q:
                lot = q[0]
                take = min(left, lot[0])
                pl[sym] = pl.get(sym, 0.0) + take * (px - lot[1])
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-9:
                    q.pop(0)
    return {k: v for k, v in pl.items() if abs(v) >= 0.005}


def isr_context(held: List[Dict], traded: List[str] | None = None,
                wallet: str = WALLET) -> Dict[str, Any]:
    """ISR scores for what we hold + what ISR intends to do next.

    Both halves are computed, never guessed: scores come from the ISR
    database ranking, the intent from the same rebalance maths the executor
    runs. Returns {} if ISR is unavailable so the report still sends.
    """
    try:
        import strategies
        import isr_alpha.executor as ex
        import isr_alpha.signals as sg
        import isr_alpha.portfolio as pf
        from isr_alpha.loader import load_isr_database

        cfg = strategies.load_merged(wallet)
        icfg = cfg.get("isr_alpha", {}) or {}
        ranked = sg.rank_universe(load_isr_database())
        by_tic = {c.ticker.upper(): c for c in ranked}

        # Score everything we HOLD and everything we TRADED. Without the
        # traded side the model has no basis to compare signal to outcome and
        # will invent one (observed 2026-08-26: it asserted scores for MRNA,
        # PSX, MRK and MPC that were never in its input).
        scored, unscored = [], []
        for sym in sorted({p_["sym"].upper() for p_ in held} | {t.upper() for t in (traded or [])}):
            c = by_tic.get(sym)
            if c:
                scored.append({"sym": c.ticker, "score": round(c.composite, 3),
                               "moat": round(c.moat_score, 1),
                               "sector": c.sub_sector})
            else:
                unscored.append(sym)

        eq = sum(p_["mv"] for p_ in held) or 0.0
        intent = {}
        try:
            equity = ex.get_account_equity() or 0
            tgt = pf.build_target_portfolio(
                ranked, equity=equity,
                max_positions=icfg.get("max_positions", 15),
                max_sector_pct=icfg.get("max_sector_pct", 0.30),
                max_gross_leverage=icfg.get("max_gross_leverage", 1.5))
            cur = ex.get_current_positions_usd()
            foreign = ex.other_engine_tickers()
            cur = {k: v for k, v in cur.items() if k not in foreign}
            tgt = [t for t in tgt if t.ticker.upper() not in foreign]
            orders = pf.rebalance_portfolio(
                cur, tgt, equity=equity,
                max_turnover_pct=icfg.get("max_turnover_pct", 0.50))
            intent = {"buys": sorted(t for t, o in orders.items() if o["side"] == "buy"),
                      "sells": sorted(t for t, o in orders.items() if o["side"] == "sell")}
        except Exception:
            pass
        return {"scored": scored, "unscored": unscored,
                "intent": intent, "universe": len(ranked)}
    except Exception:
        return {}


ANALYST_PROMPT = """You are the trader running this account. Below are today's
FACTS: the market picture, and what this account actually did.

Grounding rules, all mandatory:
- Never state a price, ticker, score, percentage, headline or number that is
  not printed in the FACTS below.
- Do NOT recommend buying or selling anything. Do not list tickers as picks.
  A ticker may only be named as evidence for a point about a sector or result.
- No price targets and no predicted percentages. You do not know where the
  market will go.
- Do not describe a score or move as high or low unless you contrast it with
  another figure printed below.
- If the facts do not support a conclusion, say the data is inconclusive.

Write two things, plain English, no markdown, no bullets:
1. ASSESSMENT: three sentences. Which sectors money moved into and out of,
   whether this book is positioned with or against that rotation given its
   sector weights, and what the day's realized results say about it.
2. OUTLOOK: two sentences. If a WHAT CHANGED block is present, lead with what
   actually moved since the prior session — a widening or narrowing gap, a
   leadership streak, a run of up or down days, a shift in book weights. State
   plainly when something did NOT change; "unchanged for a third session" is
   useful, a restatement of today's levels is not. Then name the specific
   condition that would break the current setup, using the figures given.
   Never repeat yesterday's outlook back verbatim.

Hard limit: 95 words total across both. Output exactly:
ASSESSMENT: <text>
OUTLOOK: <text>

--- FACTS ---
{facts}
--- END FACTS ---
"""


def analyst_take(facts: str,
                 timeout_s: int = int(os.environ.get("ALPACA_ANALYST_TIMEOUT", "150"))
                 ) -> tuple[str, str]:
    """(assessment, tomorrow). Empty strings on any failure — the numbers in
    this report must never be blocked by the commentary model."""
    try:
        import os
        os.environ.setdefault("ALPACA_ANALYST_TIMEOUT", str(timeout_s))
        import analyst_llm
        ok, out = analyst_llm.ask(ANALYST_PROMPT.format(facts=facts))
        if not ok or not out:
            return "", ""
        a = t_ = ""
        for line in out.splitlines():
            ls = line.strip()
            if ls.upper().startswith("ASSESSMENT:"):
                a = ls.split(":", 1)[1].strip()
            elif ls.upper().startswith(("OUTLOOK:", "TOMORROW:")):
                t_ = ls.split(":", 1)[1].strip()
        if not a and out.strip():
            a = " ".join(out.split())[:400]
        return a, t_
    except Exception:
        return "", ""


def gather_report_data(session: str | None = None, wallet: str = WALLET):
    """One wallet: session P&L, trades, realized P&L, holdings, ISR context."""
    creds = wallets.resolve(wallet)
    if creds is None:
        return {"ok": False, "name": wallet, "err": "wallet not configured"}, session
    key, secret, base = creds
    sess, prev = resolve_session(key, secret, base, session)

    eq = session_equity(key, secret, base, sess, prev)
    trades = fetch_session_trades(key, secret, base, sess)

    held = []
    try:
        r = requests.get(f"{base}/positions", headers=_hdrs(key, secret), timeout=20)
        r.raise_for_status()
        for p_ in r.json():
            held.append({"sym": p_["symbol"], "mv": _f(p_.get("market_value")),
                         "upl": _f(p_.get("unrealized_pl"))})
    except Exception:
        pass

    rl = realized_pl(trades)
    # Mark-to-market on open positions FOR THIS SESSION, derived so it
    # reconciles: day P&L = realized + mark-to-market, by definition.
    # The positions endpoint cannot supply this — unrealized_pl there is
    # lifetime-since-entry and marks live, so it drifts after the close and
    # answers a different question entirely.
    mtm = None
    if eq["pl"] is not None:
        mtm = eq["pl"] - sum(rl.values())

    return {
        "ok": True, "name": wallet, "session": sess,
        "equity": eq["close"], "day_pl": eq["pl"], "day_pct": eq["pct"],
        "equity_timeframe": eq.get("timeframe"),
        "trades": trades, "realized": rl,
        "held": sorted(held, key=lambda x: -x["mv"]),
        "unrealized": mtm,
        "isr": isr_context(held, [t["sym"] for t in trades], wallet),
    }, sess


# ---- Report building ----

def _review_due() -> str:
    """A note that rides the daily report once enough sessions have accrued.

    The outlook logic is deliberately provisional — it was tuned against four
    backfilled sessions whose deltas were tiny. Rather than rely on anyone
    remembering to revisit it, the diary raises its own hand on the report
    Peter already reads every morning.
    """
    rows = _history(limit=1000)
    n = len(rows)
    if n < REVIEW_AFTER_SESSIONS:
        return ""
    flag = DIARY_DIR / ".review_raised"
    try:
        if flag.exists() and int(flag.read_text().strip() or 0) >= n:
            return ""
    except Exception:
        pass
    try:
        DIARY_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text(str(n))
    except Exception:
        pass
    return (f"🔔 Diary now holds {n} sessions. The outlook logic was tuned on "
            f"backfilled data — worth reviewing which deltas actually carried "
            f"signal. Tell Claude: \"review the outlook logic\".")


def _holiday_notice(key: str, secret: str, base: str, sess: str) -> str:
    """Heads-up when the very next weekday after `sess` is a market holiday.

    Peter otherwise only finds out the day it happens, when no report shows
    up and nothing traded. An ordinary Friday-to-Saturday gap is not
    "special" and stays silent; only a weekday the exchange is unexpectedly
    shut gets flagged, one session ahead, via the same report he already
    reads every morning.
    """
    try:
        s_d = dt.date.fromisoformat(sess)
    except Exception:
        return ""
    nxt = s_d + dt.timedelta(days=1)
    if nxt.weekday() >= 5:  # Saturday/Sunday — routine, not a holiday
        return ""
    try:
        cal = requests.get(f"{base}/calendar", headers=_hdrs(key, secret),
                           params={"start": nxt.isoformat(),
                                   "end": (nxt + dt.timedelta(days=6)).isoformat()},
                           timeout=15).json()
        days = sorted({c["date"] for c in cal if c.get("date")})
    except Exception:
        return ""
    if not days or nxt.isoformat() in days:
        return ""  # calendar unavailable, or tomorrow trades normally
    when = dt.date.fromisoformat(days[0]).strftime("%A, %B %-d") if days else None
    return (f"📅 Market closed {nxt.strftime('%A, %B %-d')} for a holiday"
            + (f" — next session {when}." if when else "."))


def _earnings_notice(held: list, sess: str) -> str:
    """One line when a held name reports within anchor.earnings_warn_days
    sessions. Same posture as the holiday notice: silent unless it matters,
    and never a reason for the report not to go out."""
    try:
        import earnings_calendar as ec
        lead = int((strategies.load_merged("High Risk").get("anchor") or {}).get("earnings_warn_days", 2))
        return ec.notice([x["sym"] for x in held], sess, lead)
    except Exception:
        return ""


def build_brief_report(d: Dict[str, Any], assessment: str = "",
                       tomorrow: str = "", holiday: str = "", earnings: str = "") -> str:
    """High Risk only, target <=100 words."""
    L: List[str] = []
    L.append(f"📊 *High Risk* — Session {d.get('session','?')} (ET)")
    L.append("")
    if not d.get("ok"):
        L.append(f"_{d.get('err','unavailable')}_")
        return "\n".join(L) + "\n"

    day = d.get("day_pl")
    arrow = "🟢" if (day or 0) >= 0 else "🔴"
    if day is None:
        L.append(f"Equity {_m(d['equity'] or 0)} · _session P&L unavailable_")
    else:
        L.append(f"{arrow} Equity {_m(d['equity'])} · Day {_s(day)} ({_p(d['day_pct'])})")

    rl = d.get("realized") or {}
    if rl:
        tot = sum(rl.values())
        top = sorted(rl.items(), key=lambda kv: kv[1])
        best, worst = top[-1], top[0]
        L.append(f"Realized {_s(tot)} on {len(d['trades'])} orders "
                 f"(best {best[0]} {_s(best[1])}, worst {worst[0]} {_s(worst[1])}).")
    elif d.get("trades"):
        L.append(f"{len(d['trades'])} orders, nothing closed.")
    else:
        L.append("No trades.")

    held = d.get("held") or []
    if held:
        line = f"Holding {len(held)}: " + " ".join(x["sym"] for x in held[:12])
        mtm = d.get("unrealized")
        if mtm is not None:
            line += f" · mark-to-mkt {_s(mtm)}"
        L.append(line)

    if assessment:
        L.append("")
        L.append(assessment)
    if tomorrow:
        L.append(f"*Outlook:* {tomorrow}")
    if holiday:
        L.append("")
        L.append(holiday)
    if earnings:
        L.append("")
        L.append(earnings)
    note = _review_due()
    if note:
        L.append("")
        L.append(note)

    return "\n".join(L).rstrip() + "\n"


def _facts_block(d: Dict[str, Any]) -> str:
    """Compact, purely factual input for the analyst model."""
    isr = d.get("isr") or {}
    lines = [
        f"session: {d.get('session')}",
        f"day P&L: {d.get('day_pl'):+.2f} ({d.get('day_pct'):+.2f}%)"
        if d.get("day_pl") is not None else "day P&L: unavailable",
        f"realized by symbol: " + (", ".join(f"{k} {v:+.2f}"
                                             for k, v in sorted((d.get('realized') or {}).items(),
                                                                key=lambda kv: kv[1])) or "none"),
        (f"mark-to-market on open positions this session: {d['unrealized']:+.2f}"
         if d.get("unrealized") is not None else "mark-to-market: unavailable"),
        "holdings: " + (", ".join(f"{x['sym']} ${x['mv']:,.0f}" for x in (d.get('held') or [])) or "none"),
    ]
    sc = isr.get("scored") or []
    if sc:
        lines.append("ISR scores for held and traded symbols (composite 0-1, moat 0-10): " +
                     ", ".join(f"{x['sym']} {x['score']} moat {x['moat']}" for x in sc))
    un = isr.get("unscored") or []
    if un:
        lines.append("NOT in the ISR universe, so ISR has no score for them: " +
                     ", ".join(un))
    if isr.get("universe"):
        lines.append(f"ISR universe size: {isr['universe']} companies")
    # ISR intent is deliberately NOT passed to the analyst any more: handing it
    # a buy list is what made the old take read as a buy list.
    body = "\n".join(lines)
    ctx: Dict[str, Any] = {}
    try:
        import market_context as mc
        ctx = mc.build(d.get("held") or [], d.get("session"))
        market = mc.as_facts(ctx)
        if market:
            body = market + "\n\n--- THIS ACCOUNT ---\n" + body
    except Exception:
        pass

    hist = _history()
    delta = _deltas(_snapshot(d, ctx, "", ""), hist)
    if delta:
        body += "\n\n--- WHAT CHANGED ---\n" + delta
    return body, ctx


def _ffmpeg() -> "str | None":
    """Absolute paths first — launchd jobs do not inherit /opt/homebrew/bin,
    so shutil.which() alone silently loses the encoder under cron."""
    import shutil
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                 "/usr/bin/ffmpeg"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("ffmpeg")


def _spell(sym: str) -> str:
    """Tickers must be spelled, not pronounced: `say` renders BE as "be" and
    MRNA as noise. Spacing the letters makes it read them out."""
    return " ".join(sym.upper())


def speakable(d: Dict[str, Any], assessment: str = "", outlook: str = "",
              oversight: str = "", proposals: "list | None" = None,
              weekly: bool = False, holiday: str = "", earnings: str = "") -> str:
    """A spoken script built from the data, not from the markdown.

    Reading the rendered report aloud would say "asterisk", the bullet
    separator, and a run of tickers as one unpronounceable word.
    """
    parts: List[str] = []
    try:
        when = dt.date.fromisoformat(d["session"]).strftime("%A, %B %-d")
    except Exception:
        when = str(d.get("session", ""))
    parts.append(f"High Risk wallet. Session {when}.")

    day, eq = d.get("day_pl"), d.get("equity")
    if eq is not None and day is not None:
        way = "up" if day >= 0 else "down"
        parts.append(f"Equity {eq:,.0f} dollars, {way} {abs(day):,.0f} dollars, "
                     f"{abs(d.get('day_pct') or 0):.2f} percent.")
    elif eq is not None:
        parts.append(f"Equity {eq:,.0f} dollars. Session P and L unavailable.")

    rl = d.get("realized") or {}
    n = len(d.get("trades") or [])
    if rl:
        tot = sum(rl.values())
        ranked = sorted(rl.items(), key=lambda kv: kv[1])
        best, worst = ranked[-1], ranked[0]
        way = "gain" if tot >= 0 else "loss"
        parts.append(f"Realized {way} of {abs(tot):,.0f} dollars on {n} orders. "
                     f"Best, {_spell(best[0])}, {'up' if best[1] >= 0 else 'down'} "
                     f"{abs(best[1]):,.0f}. Worst, {_spell(worst[0])}, "
                     f"{'up' if worst[1] >= 0 else 'down'} {abs(worst[1]):,.0f}.")
    elif n:
        parts.append(f"{n} orders, nothing closed.")
    else:
        parts.append("No trades today.")

    held = d.get("held") or []
    if held:
        parts.append(f"Holding {len(held)} positions.")

    # Spell tickers inside the prose too, longest first so MRNA is not eaten
    # by a shorter symbol that happens to be a prefix.
    syms = sorted({x["sym"].upper() for x in held} |
                  set((d.get("realized") or {}).keys()), key=len, reverse=True)
    def despell(txt: str) -> str:
        for sym in syms:
            txt = re.sub(rf"\b{re.escape(sym)}\b", _spell(sym), txt)
        return txt

    if assessment:
        parts.append(despell(assessment))
    if outlook:
        parts.append("Outlook. " + despell(outlook))
    if holiday:
        parts.append(re.sub(r"^[^\w]+", "", holiday).strip())
    if earnings:
        parts.append(despell(re.sub(r"^[^\w]+", "", earnings).strip()))
    if oversight:
        parts.append(("Weekly assessment." if weekly else "Oversight.") + " " + despell(oversight))
        for p in (proposals or []):
            kind = "Lesson." if p.get("type") == "lesson" else "Proposal."
            parts.append(f"{kind} {despell(p.get('what',''))}. {despell(p.get('why',''))}.")
    return " ".join(parts)


def _hermes_voice() -> tuple[str, float]:
    """(voice, speed) from the Hermes profile's tts config, so the report is
    read in the same voice as normal interaction. Falls back to Aria."""
    if VOICE:
        return VOICE, 1.0
    for cfg in (Path.home() / ".hermes" / "profiles" / HERMES_PROFILE / "config.yaml",
                Path.home() / ".hermes" / "config.yaml"):
        try:
            import yaml
            tts = (yaml.safe_load(cfg.read_text()) or {}).get("tts") or {}
            edge = tts.get("edge") or {}
            if edge.get("voice"):
                return edge["voice"], float(edge.get("speed", 1.0) or 1.0)
        except Exception:
            continue
    return "en-US-AriaNeural", 1.0


def _edge_tts(script: str, mp3: "Path") -> bool:
    """Microsoft Edge neural voice. Free, no API key, but it is a NETWORK call
    — if it fails we fall back to local `say` rather than lose the note."""
    try:
        import asyncio
        import edge_tts
    except Exception:
        return False
    voice, speed = _hermes_voice()
    rate = f"{int(round((speed - 1.0) * 100)):+d}%"
    try:
        async def go():
            await edge_tts.Communicate(script, voice, rate=rate).save(str(mp3))
        asyncio.run(go())
        return mp3.exists() and mp3.stat().st_size > 0
    except Exception as e:
        print(f"[voice] edge-tts failed ({e}) — falling back to say")
        return False


def _say_tts(script: str, aiff: "Path") -> bool:
    import shutil
    import subprocess
    if not shutil.which("say"):
        return False
    try:
        subprocess.run(["say", "-v", SAY_FALLBACK_VOICE, "-r", str(VOICE_RATE),
                        "-o", str(aiff), script],
                       check=True, capture_output=True, timeout=180)
        return aiff.exists()
    except Exception as e:
        print(f"[voice] say failed: {e}")
        return False


def render_voice(script: str, session: str) -> "Path | None":
    """Neural voice -> OGG/Opus, which is what Telegram sendVoice accepts.

    Returns None on any failure; the voice note is a bonus and must never
    stop the text report from going out.
    """
    import subprocess
    import tempfile
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    out = DIARY_DIR / f"{session}.ogg"
    ff = _ffmpeg()
    if not ff:
        print("[voice] ffmpeg not found — cannot encode Opus for Telegram")
        return None
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "r.mp3"
        if not _edge_tts(script, src):
            src = Path(td) / "r.aiff"
            if not _say_tts(script, src):
                return None
        try:
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src),
                            "-c:a", "libopus", "-b:a", "32k", "-ac", "1",
                            "-ar", "48000", str(out)],
                           check=True, capture_output=True, timeout=180)
        except Exception as e:
            print(f"[voice] ffmpeg failed: {e}")
            return None
    return out if out.exists() and out.stat().st_size > 0 else None


def _history(limit: int = 10) -> List[Dict[str, Any]]:
    """Past sessions, oldest first. Append-only, so history is never rewritten."""
    try:
        rows = [json.loads(l) for l in HISTORY_FILE.read_text().splitlines() if l.strip()]
    except Exception:
        return []
    return rows[-limit:]


def _history_append(rec: Dict[str, Any]) -> None:
    try:
        DIARY_DIR.mkdir(parents=True, exist_ok=True)
        prior = [json.loads(l) for l in HISTORY_FILE.read_text().splitlines()
                 if l.strip()] if HISTORY_FILE.exists() else []
        prior = [r for r in prior if r.get("session") != rec.get("session")]
        prior.append(rec)
        prior.sort(key=lambda r: r.get("session", ""))
        HISTORY_FILE.write_text("\n".join(json.dumps(r) for r in prior) + "\n")
    except Exception as e:
        print(f"[diary] could not append history: {e}")


def _snapshot(d: Dict[str, Any], ctx: Dict[str, Any],
              assessment: str, outlook: str) -> Dict[str, Any]:
    rot = {r["label"]: round(r["d1"], 2) for r in (ctx.get("rotation") or [])}
    reg = ctx.get("regime") or {}
    bbs = ctx.get("book_by_sector") or {}
    tot = sum(bbs.values()) or 1.0
    return {
        "session": d.get("session"),
        "day_pl": d.get("day_pl"),
        "day_pct": d.get("day_pct"),
        "realized": round(sum((d.get("realized") or {}).values()), 2),
        "equity": d.get("equity"),
        "n_orders": len(d.get("trades") or []),
        "regime": {k: round(reg[k], 2) for k in ("vs50", "vs200") if k in reg},
        "rotation_d1": rot,
        "book_pct": {k: round(v / tot * 100, 1) for k, v in bbs.items()},
        "assessment": assessment,
        "outlook": outlook,
    }


def _deltas(cur: Dict[str, Any], hist: List[Dict[str, Any]]) -> str:
    """What CHANGED since prior sessions, framed as levels that mean something.

    Reporting the raw gap ("+1.73% vs the 50-day") gave the analyst nothing to
    reason about, so it echoed yesterday's figure back as a fake threshold
    ("a gap widening above +1.74% would break the trend" — which is backwards;
    a widening gap is strength). The same number stated as a distance to the
    crossover is an actual condition: SPY has to fall that far to lose the
    average.
    """
    hist = [h for h in hist if h.get("session", "") < (cur.get("session") or "")]
    if not hist:
        return ""
    prev = hist[-1]
    L: List[str] = [f"PRIOR SESSION ({prev.get('session')}) FOR COMPARISON:"]

    for k, lbl in (("vs50", "50-day"), ("vs200", "200-day")):
        a, b = (cur.get("regime") or {}).get(k), (prev.get("regime") or {}).get(k)
        if a is None:
            continue
        side = "above" if a >= 0 else "below"
        need = ("fall" if a >= 0 else "rise")
        L.append(f"  SPY is {abs(a):.2f}% {side} its {lbl} average — it must "
                 f"{need} {abs(a):.2f}% to cross it")
        if b is not None:
            drift = a - b
            if abs(drift) >= 0.005:
                L.append(f"    (that cushion {'grew' if abs(a) > abs(b) else 'shrank'} "
                         f"by {abs(drift):.2f} points since {prev.get('session')})")
            else:
                L.append("    (essentially unchanged from the prior session)")

    def leader(rec):
        r = {k: v for k, v in (rec.get("rotation_d1") or {}).items() if k != "S&P 500"}
        return max(r, key=r.get) if r else None
    lead = leader(cur)
    if lead:
        streak = 1
        for old_ in reversed(hist):
            if leader(old_) == lead:
                streak += 1
            else:
                break
        L.append(f"  Sector leading today: {lead}"
                 + (f", {streak} consecutive sessions" if streak > 1 else
                    " (did not lead the prior session)"))

    pls = [h["day_pl"] for h in hist if h.get("day_pl") is not None]
    cur_pl = cur.get("day_pl")
    if cur_pl is not None and pls:
        L.append(f"  Day P&L: {prev.get('day_pl'):+,.2f} -> {cur_pl:+,.2f}")
        typical = sum(abs(x) for x in pls) / len(pls)
        if typical > 0:
            ratio = abs(cur_pl) / typical
            size = ("about typical" if 0.6 <= ratio <= 1.6
                    else "unusually small" if ratio < 0.6 else "unusually large")
            L.append(f"    (typical daily swing over the last {len(pls)} sessions is "
                     f"{typical:,.0f}; today is {size} at {ratio:.1f}x)")
        run = 0
        for old_ in reversed(hist + [cur]):
            v = old_.get("day_pl")
            if v is None or (v >= 0) != (cur_pl >= 0):
                break
            run += 1
        if run > 1:
            L.append(f"    ({run} consecutive {'up' if cur_pl >= 0 else 'down'} sessions)")

    for sec, pct in sorted((cur.get("book_pct") or {}).items(),
                           key=lambda kv: -kv[1])[:3]:
        was = (prev.get("book_pct") or {}).get(sec)
        if was is not None and abs(pct - was) >= 1.0:
            L.append(f"  Book weight {sec}: {was:.0f}% -> {pct:.0f}%")

    if prev.get("outlook"):
        L.append(f"  You said this yesterday, do not repeat it: {prev['outlook'][:220]}")
    return "\n".join(L) if len(L) > 1 else ""


def _sent_sessions() -> set:
    try:
        return set(json.loads(STATE_FILE.read_text()).get("sent") or [])
    except Exception:
        return set()


def _mark_sent(session: str) -> None:
    """Remember which sessions already went out.

    The scheduler fires on Taipei weekdays, but Taipei Sunday and Monday map
    to ET weekend days with no session, so resolve_session() would hand back
    Friday again and the same report would be pushed three times. The stamp
    is what actually prevents that; the cron weekday list is only a hint.
    """
    keep = sorted(_sent_sessions() | {session})[-40:]
    try:
        STATE_FILE.write_text(json.dumps({"sent": keep}, indent=2))
    except Exception:
        pass


def send_brief_report(push: bool = True, channel: str | None = None, log=print,
                      session: str | None = None, wallet: str = WALLET,
                      analyst: bool = True, voice: bool = True,
                      once: bool = False, force_push: bool = False,
                      oversight: bool = True) -> Dict:
    """Build the High Risk session report and optionally push it."""
    # Keep the closed-trade ledger current. sync() dedupes on sell_order_id so
    # this is safe to repeat, and it is the only scheduled thing that runs after
    # the close — performance_log.json had otherwise gone weeks without an
    # update because sync() only ever ran by hand.
    try:
        import performance_tracker as _pt
        _before = len(_pt.load_log().get("trades") or [])
        _pt.sync(verbose=False)
        _added = len(_pt.load_log().get("trades") or []) - _before
        log(f"[0/3] Trade ledger synced (+{_added} closed trades)")
    except Exception as _e:
        log(f"[0/3] Trade ledger sync failed: {_e}")

    log("[1/3] Fetching wallet…")
    d, sess = gather_report_data(session, wallet)
    if once and push and sess in _sent_sessions():
        log(f"session {sess} already sent — skipping")
        return {"report": "", "md_path": None, "sent": False,
                "words": 0, "voice": None, "skipped": True}

    a = t_ = ""
    if analyst and d.get("ok"):
        log("[2/3] Analyst take…")
        facts, ctx = _facts_block(d)
        a, t_ = analyst_take(facts)
        if not a:
            log("[analyst] unavailable — sending numbers only")
    else:
        log("[2/3] Analyst skipped.")
        _, ctx = _facts_block(d)

    holiday = earnings = ""
    if d.get("ok"):
        creds = wallets.resolve(wallet)
        if creds:
            holiday = _holiday_notice(*creds, sess)
            if holiday:
                log(f"✓ {holiday}")
        earnings = _earnings_notice(d.get("held") or [], sess)
        if earnings:
            log(f"✓ {earnings}")

    report = build_brief_report(d, a, t_, holiday, earnings)

    # Claude's oversight read, appended so Peter gets ONE morning message rather
    # than two. Bounded and optional by design: if it is slow or fails, the
    # numbers still go out without it — same posture as the analyst take.
    ov_text, ov_proposals = "", []
    if oversight and d.get("ok"):
        # Saturday 09:00 Taipei is Friday 21:00 ET — after Friday's close, so it
        # carries the week's assessment instead of the daily read. One report,
        # never both.
        _is_weekly = dt.datetime.now(ET).weekday() == 5
        log("[2b/3] Weekly assessment…" if _is_weekly else "[2b/3] Oversight evaluation…")
        try:
            import oversight as ov
            res = ov.run_weekly(push=False) if _is_weekly else ov.run_evaluate(sess, push=False)
            if res.get("ok") and res.get("text"):
                ov_text = res["text"]
                ov_proposals = res.get("proposals") or []
                report = (report.rstrip("\n") + "\n\n— — —\n"
                          + ("*Weekly assessment*" if _is_weekly else "*Oversight*")
                          + "\n" + res["text"] + "\n")
                if res.get("proposals"):
                    report += "\n*Proposals* (approve with `oversight.py approve <id>`)\n"
                    for pr in res["proposals"]:
                        report += f"  `{pr['id']}` {pr['what']}\n"
                log(f"✓ Oversight added ({len(res.get('proposals') or [])} proposals)")
            else:
                log(f"⚠ oversight unavailable: {res.get('error','no output')}")
        except Exception as e:
            log(f"⚠ oversight failed: {e}")
    else:
        _is_weekly = False

    words = len(report.split())
    if words > WORD_BUDGET:
        log(f"⚠ {words} words (budget {WORD_BUDGET})")

    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    md_path = DIARY_DIR / f"{sess}.md"
    md_path.write_text(report, encoding="utf-8")
    log(f"✓ Report: {md_path} ({words} words)")

    ogg = None
    if voice and d.get("ok"):
        log("[3/4] Recording voice note…")
        ogg = render_voice(speakable(d, a, t_, oversight=ov_text,
                                     proposals=ov_proposals, weekly=_is_weekly,
                                     holiday=holiday, earnings=earnings), sess)
        log(f"✓ Voice: {ogg}" if ogg else "⚠ voice unavailable — text only")

    sent = False
    if push and not (os.environ.get(SCHEDULE_TOKEN_ENV) or force_push):
        log("push blocked: only the scheduled job delivers this report "
            f"(set {SCHEDULE_TOKEN_ENV}=1 or pass --force-push to override)")
        push = False
    if push:
        log("[4/4] Sending to Telegram…")
        parts = hr._split_for_telegram(report)
        n = sum(1 for part in parts
                if tn.send(part, parse_mode="Markdown", channel=channel))
        sent = n == len(parts) and n > 0
        # The voice note is a bonus: a failed send must not mark the report
        # undelivered, or the scheduler would keep retrying a report that
        # already landed.
        if ogg:
            if tn.send_voice(str(ogg), caption=f"High Risk · {sess}", channel=channel):
                log("✓ Voice sent.")
            else:
                log("⚠ voice note not delivered (text was).")
        if sent:
            _mark_sent(sess)
            _history_append(_snapshot(d, ctx, a, t_))
        log("✓ Done." if sent else "⚠ not (fully) delivered.")
    else:
        log("(push disabled — Telegram skipped)")

    return {"report": report, "md_path": md_path, "sent": sent,
            "words": words, "voice": str(ogg) if ogg else None}


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
    res = send_brief_report(push=not no_push, session=_cli_session(),
                            analyst="--no-analyst" not in sys.argv,
                            voice="--no-voice" not in sys.argv,
                            once="--once" in sys.argv,
                            force_push="--force-push" in sys.argv,
                            oversight="--no-oversight" not in sys.argv)
    if no_push:
        print("\n" + res["report"])
    # A skip is a correct outcome, not a failure: exiting non-zero here
    # makes launchd record every deduped no-op as a failed run.
    sys.exit(0 if (res["sent"] or no_push or res.get("skipped")) else 1)