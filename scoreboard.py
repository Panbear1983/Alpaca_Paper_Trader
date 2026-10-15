#!/usr/bin/env python3
"""
scoreboard.py — "Scoreboard Multiples": every account's daily change side
by side, one row per trading session, newest at the top (Shift+S).
====================================================================
Read-only end to end. The Alpaca paper wallets are read with per-request
credentials, like compare.py — never wallets.apply(), which rewires the
dashboard's active wallet — and GET requests only. Robinhood (main) comes
only from the history the view-only reader has already saved; nothing here
starts a Robinhood read (each read is a Claude run with its own scheduler).

How a day's number is made
  Alpaca  each daily history point carries one session's CLOSE but is
          stamped 00:00 UTC the NEXT day (20:00 ET, 19:00 in winter), so a
          point belongs to the latest session that closed at or before its
          stamp. Change = close / previous close - 1. On a deposit or
          withdrawal day the dollars are Alpaca's own profit_loss instead.
  Robinhood  the session's close is the FIRST read at or after the bell;
          later evening reads carry after-hours moves. A read more than 30
          minutes late, or none after the bell, makes the close an estimate.
  Unchanged  one wallet exactly unchanged is real (it sat in cash — High
          Risk on 2026-09-15). Every wallet exactly unchanged on the same
          session is an Alpaca data gap (2026-07-29): shown as missing, and
          the next change is marked as covering two sessions.

Maths is kept apart from fetching so it can be tested offline.

    venv/bin/python scoreboard.py [10|30|60]     print the table (default 30)
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

import i18n
import robinhood_view as rhv
import wallets

ET = ZoneInfo("America/New_York")
WINDOWS = (10, 30, 60)
DEFAULT_WINDOW = 30
BAR_STEP_PCT = 0.5          # one block per started 0.5% move …
BAR_CAP = 6                 # … capped at 6 blocks (3%+), the same in every column
RH_LATE_MIN = 30            # a Robinhood close read later than this = estimate
HISTORY_PERIOD = "6M"       # 3M gave only 65 closes — too tight for 60 sessions
CALENDAR_DAYS = 200         # calendar span fetched (covers HISTORY_PERIOD)
TRANSFER_TYPES = "CSD,CSW,JNLC,JNLS"
_MAX_STAMP_LAG = dt.timedelta(hours=24)


# ── trading sessions ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Session:
    day: dt.date
    open: dt.datetime       # New York time
    close: dt.datetime      # New York time (13:00 on early-close days)


def parse_calendar(rows: Any) -> list[Session]:
    """Alpaca GET /calendar rows → sessions, oldest first."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            day = dt.date.fromisoformat(r["date"])
            out.append(Session(day,
                               dt.datetime.combine(day, dt.time.fromisoformat(r["open"]), ET),
                               dt.datetime.combine(day, dt.time.fromisoformat(r["close"]), ET)))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda s: s.day)


def session_for_stamp(ts: float, sessions: list[Session]) -> dt.date | None:
    """The session whose close a daily history point carries: the latest one
    that closed at or before the stamp. None when no session closed in the
    24 hours before it (the calendar does not cover the stamp)."""
    when = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    i = bisect.bisect_right([s.close for s in sessions], when) - 1
    if i < 0 or when - sessions[i].close > _MAX_STAMP_LAG:
        return None
    return sessions[i].day


def session_on_or_after(day: dt.date, sessions: list[Session]) -> dt.date | None:
    """A weekend or holiday transfer lands in the next session's change."""
    i = bisect.bisect_left([s.day for s in sessions], day)
    return sessions[i].day if i < len(sessions) else None


# ── Alpaca closes ───────────────────────────────────────────────────────────

def alpaca_closes(hist: dict, sessions: list[Session]
                  ) -> tuple[dict[dt.date, float], dict[dt.date, float]]:
    """({session: equity}, {session: profit_loss}) from a daily
    portfolio-history reply. Zero/None equity (days before the wallet was
    funded) is skipped, as compare.py does."""
    ts = hist.get("timestamp") or []
    eq = hist.get("equity") or []
    pl = hist.get("profit_loss") or [None] * len(ts)
    closes, pls = {}, {}
    for t, e, p in zip(ts, eq, pl):
        if not e:
            continue
        day = session_for_stamp(t, sessions)
        if day is None:
            continue
        closes[day] = float(e)
        pls[day] = float(p or 0.0)
    return closes, pls


def gap_sessions(all_closes: list[dict[dt.date, float]]) -> set[dt.date]:
    """Sessions Alpaca filled with a stale copy of the day before: at least
    two wallets have a close and a previous close, and every one of them is
    exactly unchanged. One wallet unchanged alone is real (it held cash)."""
    seen: dict[dt.date, list[bool]] = {}
    for closes in all_closes:
        days = sorted(closes)
        for prev, day in zip(days, days[1:]):
            seen.setdefault(day, []).append(abs(closes[day] - closes[prev]) < 0.005)
    return {d for d, flags in seen.items() if len(flags) >= 2 and all(flags)}


# ── Robinhood closes ────────────────────────────────────────────────────────

def robinhood_closes(points: list[tuple[float, float]], sessions: list[Session]
                     ) -> tuple[dict[dt.date, float], set[dt.date]]:
    """({session: value}, {estimated sessions}) from the reader's history.
    Close = the first read at or after the bell; an estimate when that read
    is more than RH_LATE_MIN late, or when there is none after the bell (the
    last read of the day stands in). Weekend and holiday reads are ignored."""
    by_day = {s.day: s for s in sessions}
    reads: dict[dt.date, list[tuple[dt.datetime, float]]] = {}
    for ts, val in points:
        t = dt.datetime.fromtimestamp(ts, ET)
        if t.date() in by_day:
            reads.setdefault(t.date(), []).append((t, float(val)))
    closes, est = {}, set()
    for day, rs in reads.items():
        bell = by_day[day].close
        after = [r for r in rs if r[0] >= bell]
        t, v = min(after) if after else max(rs)
        closes[day] = v
        if not after or t - bell > dt.timedelta(minutes=RH_LATE_MIN):
            est.add(day)
    return closes, est


# ── per-session change cells ────────────────────────────────────────────────

def changes(closes: dict[dt.date, float], order: list[dt.date], *,
            skip: set[dt.date] = frozenset(),
            pls: dict[dt.date, float] | None = None,
            transfer_days: set[dt.date] = frozenset(),
            estimates: set[dt.date] = frozenset()) -> dict[dt.date, dict]:
    """One account's cell for every session in `order` (oldest first).

    state  up / down / flat, "missing" (no usable close after the account's
           first one), or "none" (not tracked yet, or its first close)
    pct, dollars  against the latest EARLIER session with a usable close
    span   sessions covered (> 1 when the ones before were missing)
    marks  "*" transfer in the span, "~" either end an estimate, "2d"…"""
    pls = pls or {}
    pos = {d: i for i, d in enumerate(order)}
    out: dict[dt.date, dict] = {}
    prev_day: dt.date | None = None
    for day in order:
        val = None if day in skip else closes.get(day)
        if val is None:
            out[day] = _cell("missing" if prev_day else "none")
            continue
        if prev_day is None:
            out[day] = _cell("none")
            prev_day = day
            continue
        prev = closes[prev_day]
        covered = order[pos[prev_day] + 1: pos[day] + 1]
        moved = any(d in transfer_days for d in covered)
        dollars = sum(pls.get(d, 0.0) for d in covered) if moved else val - prev
        pct = dollars / prev * 100 if prev else 0.0
        marks = []
        if moved:
            marks.append("*")
        if day in estimates or prev_day in estimates:
            marks.append("~")
        if len(covered) > 1:
            marks.append(f"{len(covered)}d")
        state = "flat" if abs(dollars) < 0.005 else ("up" if dollars > 0 else "down")
        out[day] = _cell(state, pct, dollars, len(covered), marks)
        prev_day = day
    return out


def _cell(state: str, pct: float | None = None, dollars: float | None = None,
          span: int = 1, marks: list[str] | None = None) -> dict:
    return {"state": state, "pct": pct, "dollars": dollars, "span": span,
            "marks": marks or []}


def bar_blocks(pct: float | None) -> int:
    """Shared scale: one block per started BAR_STEP_PCT, capped at BAR_CAP."""
    if not pct:
        return 0
    return min(BAR_CAP, max(1, math.ceil(abs(pct) / BAR_STEP_PCT - 1e-9)))


def cell_colour(state: str) -> str:
    return {"up": "green", "down": "red"}.get(state, "grey50")


def totals(cells: list[dict]) -> dict:
    """Footer for one column over the window's cells."""
    by = {k: [c for c in cells if c["state"] == k]
          for k in ("up", "down", "flat", "missing")}
    won = sum(c["dollars"] for c in by["up"])
    lost = sum(c["dollars"] for c in by["down"])
    priced = [c for c in cells if c["pct"] is not None]
    net = None
    if priced:
        growth = 1.0
        for c in priced:
            growth *= 1 + c["pct"] / 100
        net = (growth - 1) * 100
    single = [c for c in priced if c["span"] == 1 and c["state"] != "flat"]
    return {"up": len(by["up"]), "down": len(by["down"]),
            "flat": len(by["flat"]), "missing": len(by["missing"]),
            "won": won, "lost": lost, "diff": won + lost, "net_pct": net,
            "best": max((c["pct"] for c in single), default=None),
            "worst": min((c["pct"] for c in single), default=None)}


# ── putting the table together (pure) ───────────────────────────────────────

def assemble(window: int, sessions: list[Session], wallet_data: list[dict],
             rh_points: list[tuple[float, float]], now: dt.datetime,
             rh_name: str = "Robinhood (main)") -> dict:
    """The table the screen draws. wallet_data items come from fetch_wallet
    (ok/err/hist/equity/transfers); rh_points from robinhood_view.load_history.
    Rows are the last `window` sessions that have opened, newest first."""
    now_et = now.astimezone(ET)
    started = [s for s in sessions if s.open <= now_et]
    out = {"as_of": now_et, "window": window, "columns": [], "rows": [],
           "totals": [], "notes": [], "label": "", "today": None}
    if not started:
        out["notes"].append(("sb.note.no_sessions", {}))
        return out
    today = started[-1]
    order = [s.day for s in started]
    session_open = now_et < today.close
    out["today"] = today.day

    parsed = []
    for w in wallet_data:
        if w.get("ok"):
            closes, pls = alpaca_closes(w.get("hist") or {}, started)
            parsed.append((w, closes, pls))
        else:
            parsed.append((w, None, None))
            out["notes"].append(("sb.note.err", {"name": w.get("name", "?"),
                                                 "err": w.get("err") or "error"}))
    gaps = gap_sessions([c for _, c, _ in parsed if c is not None])

    cols: list[dict[dt.date, dict]] = []
    live_used = False
    for w, closes, pls in parsed:
        out["columns"].append({"name": w.get("name", "?"), "kind": "alpaca",
                               "ok": closes is not None, "err": w.get("err", "")})
        if closes is None:
            cols.append({d: _cell("none") for d in order})
            continue
        if today.day not in closes and w.get("equity"):
            closes[today.day] = float(w["equity"])     # not in history yet: live
            live_used = True
        moved = {session_on_or_after(d, started) for d in w.get("transfers") or []}
        cols.append(changes(closes, order, skip=gaps - {today.day}, pls=pls,
                            transfer_days=moved - {None}))

    rh_closes, rh_est = robinhood_closes(rh_points, started)
    if session_open and today.day in rh_closes:
        rh_est.discard(today.day)                      # live, not an estimate
        live_used = True
    out["columns"].append({"name": rh_name, "kind": "robinhood", "ok": True, "err": ""})
    cols.append(changes(rh_closes, order, estimates=rh_est))

    if live_used:
        out["label"] = "live" if session_open else "after"
        out["notes"].append((f"sb.note.{out['label']}", {}))
    shown = order[-window:]
    for day in reversed(shown):
        out["rows"].append({"day": day,
                            "label": out["label"] if day == today.day else "",
                            "cells": [c[day] for c in cols]})
    out["totals"] = [totals([c[d] for d in shown]) for c in cols]

    marks = {m for row in out["rows"] for c in row["cells"] for m in c["marks"]}
    if "*" in marks:
        out["notes"].append(("sb.note.transfer", {}))
    if "~" in marks:
        out["notes"].append(("sb.note.estimate", {}))
    if any(m.endswith("d") for m in marks):
        out["notes"].append(("sb.note.span", {}))
    in_gap = sorted(d for d in gaps if d in shown)
    if in_gap:
        out["notes"].append(("sb.note.gap", {"days": ", ".join(f"{d:%m-%d}" for d in in_gap)}))
    out["notes"].append(("sb.note.dot", {}))
    first = min(rh_closes) if rh_closes else None
    out["notes"].append(("sb.note.rh", {"first": f"{first:%m-%d}" if first else "—"}))
    return out


# ── fetching (GET only, errors returned as data) ────────────────────────────

def _client(key: str, secret: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                      "Accept": "application/json"})
    return s


def _get(client: requests.Session, url: str, **params) -> Any:
    for attempt in range(3):
        try:
            r = client.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)


def fetch_wallet(name: str, since: dt.date) -> dict:
    """One wallet's daily history, live equity and transfers. Never raises."""
    creds = wallets.resolve(name)
    if not creds:
        return {"name": name, "ok": False, "err": "not configured"}
    key, secret, base = creds
    c = _client(key, secret)
    try:
        hist = _get(c, f"{base}/account/portfolio/history",
                    period=HISTORY_PERIOD, timeframe="1D")
        acct = _get(c, f"{base}/account")
    except Exception as e:
        return {"name": name, "ok": False, "err": _short(e)}
    transfers = []
    try:
        acts = _get(c, f"{base}/account/activities",
                    activity_types=TRANSFER_TYPES, after=since.isoformat())
        for a in acts if isinstance(acts, list) else []:
            try:
                transfers.append(dt.date.fromisoformat(str(a.get("date"))[:10]))
            except ValueError:
                continue
    except Exception:
        pass                    # no transfer data degrades to no markers
    try:
        equity = float(acct.get("equity") or 0.0)
    except (TypeError, ValueError):
        equity = 0.0
    return {"name": name, "ok": True, "err": "", "hist": hist,
            "equity": equity, "transfers": transfers}


def fetch_calendar(start: dt.date, end: dt.date) -> list[Session]:
    """NYSE sessions from Alpaca, using the first configured wallet's keys."""
    for w in wallets.list_wallets():
        creds = wallets.resolve(w["name"])
        if creds:
            key, secret, base = creds
            return parse_calendar(_get(_client(key, secret), f"{base}/calendar",
                                       start=start.isoformat(), end=end.isoformat()))
    raise RuntimeError("no configured Alpaca wallet")


def _short(e: Exception) -> str:
    msg = str(e).split("\n")[0]
    return msg[:80] + ("…" if len(msg) > 80 else "")


def gather(now: dt.datetime | None = None,
           fetch: Callable[[str, dt.date], dict] | None = None,
           calendar: Callable[[dt.date, dt.date], list[Session]] | None = None,
           load_rh: Callable[[], list] | None = None) -> dict:
    """Everything the table needs, fetched once (the window can then change
    without refetching). Never raises: a calendar failure comes back as
    {"error": …}, a wallet failure inside its own entry."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ET).date()
    start = today - dt.timedelta(days=CALENDAR_DAYS)
    try:
        sessions = (calendar or fetch_calendar)(start, today)
    except Exception as e:
        return {"now": now, "error": _short(e)}
    data = [(fetch or fetch_wallet)(w["name"], start) for w in wallets.list_wallets()]
    try:
        points = (load_rh or rhv.load_history)()
    except Exception:
        points = []
    return {"now": now, "sessions": sessions, "wallets": data, "rh": points, "error": ""}


def table_from(raw: dict, window: int = DEFAULT_WINDOW) -> dict:
    """Assemble gathered data for one window."""
    if raw.get("error"):
        out = assemble(window, [], [], [], raw["now"])
        out["notes"] = [("sb.note.err", {"name": "calendar", "err": raw["error"]})]
        return out
    return assemble(window, raw["sessions"], raw["wallets"], raw["rh"], raw["now"],
                    rh_name=i18n.t("wal.rh_name"))


def build(window: int = DEFAULT_WINDOW, now: dt.datetime | None = None,
          fetch: Callable[[str, dt.date], dict] | None = None,
          calendar: Callable[[dt.date, dt.date], list[Session]] | None = None,
          load_rh: Callable[[], list] | None = None) -> dict:
    """Fetch everything and assemble the table. Never raises."""
    return table_from(gather(now, fetch, calendar, load_rh), window)


def day_text(day: dt.date) -> str:
    """'10-14 Wed' / '10-14 三' in the current language."""
    return f"{day:%m-%d} " + i18n.t("sb.weekdays").split()[day.weekday()]


# ── text mode ───────────────────────────────────────────────────────────────

_ANSI = {"green": "\033[32m", "red": "\033[31m", "grey50": "\033[90m"}


def cell_text(c: dict) -> str:
    """'-2.61% ██████ 2d' style text for one cell (no colour)."""
    if c["state"] in ("missing", "none") or c["pct"] is None or c["state"] == "flat":
        core = "·"
    else:
        core = f"{c['pct']:+.2f}% " + "█" * bar_blocks(c["pct"])
    return (core + (" " + "".join(c["marks"]) if c["marks"] else "")).rstrip()


def render_text(table: dict, colour: bool = False) -> str:
    t = i18n.t
    w, lw = 22, 22
    paint = (lambda s, st: f"{_ANSI[st]}{s}\033[0m") if colour else (lambda s, st: s)
    as_of = table["as_of"].strftime("%H:%M:%S ET") if table.get("as_of") else ""
    lines = [t("sb.title") + " — " + t("sb.window", n=table["window"]) + " · "
             + t("sb.scale", step=f"{BAR_STEP_PCT:g}", cap=BAR_CAP,
                 top=f"{BAR_STEP_PCT * BAR_CAP:g}")
             + " · " + t("sb.read_only") + " · " + as_of, ""]
    names = [c["name"][:w - 1] for c in table["columns"]]
    lines.append(f"{t('sb.date'):<{lw}}" + "".join(f"{n:<{w}}" for n in names))
    for row in table["rows"]:
        lab = {"live": t("sb.live"), "after": t("sb.after")}.get(row["label"], "")
        head = day_text(row["day"]) + (f" {lab}" if lab else "")
        cells = "".join(paint(f"{cell_text(c):<{w}}", cell_colour(c["state"]))
                        for c in row["cells"])
        lines.append(f"{head:<{lw}}" + cells)
    lines.append("")

    def money(x: float) -> str:
        return f"{'-' if x < 0 else '+'}${abs(x):,.0f}"

    def pct(x: float | None) -> str:
        return "—" if x is None else f"{x:+.2f}%"

    foot = [
        (t("sb.f_days"), lambda s: f"{s['up']}↑ {s['down']}↓ {s['flat']}·"
                                   + (f" {s['missing']}?" if s["missing"] else "")),
        (t("sb.f_won"), lambda s: money(s["won"])),
        (t("sb.f_lost"), lambda s: money(s["lost"])),
        (t("sb.f_diff"), lambda s: money(s["diff"])),
        (t("sb.f_net"), lambda s: pct(s["net_pct"])),
        (t("sb.f_best"), lambda s: pct(s["best"])),
        (t("sb.f_worst"), lambda s: pct(s["worst"])),
    ]
    for label, fn in foot:
        lines.append(f"{label:<{lw}}" + "".join(f"{fn(s):<{w}}" for s in table["totals"]))
    lines.append("")
    for key, params in table["notes"]:
        lines.append(t(key, **params))
    return "\n".join(lines)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else DEFAULT_WINDOW
    print(render_text(build(n), colour=sys.stdout.isatty()))
