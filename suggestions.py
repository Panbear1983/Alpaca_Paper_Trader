"""
suggestions.py — the engines as advisors, not traders (2026-09-23)
===================================================================
Peter's decision: the swing buyer and the capitol copier keep thinking but stop
pressing the buy button on the High Risk wallet. In "notify" mode each engine
runs its full logic and, where it would have placed an order, records an idea
here instead and texts it to Peter with the fence's verdict on it. Nothing is
bought unless he buys it.

  mode_for(block, "mode", "enabled")   → "trade" | "notify" | "off"
      The mode key wins; without it the old boolean decides (trade/off), so a
      wallet whose file never heard of modes behaves exactly as before.
  record(...)                            one row per engine+symbol+side per session
  fence_check(sym, side, usd)            what entry_gate would say to that buy
  notice(session)                        the morning-report line
  taken(rows, fills)                     which ideas Peter acted on (raw material
                                         for the playbook)

Log: diary/suggestions.jsonl. Pure functions take data; the two fetchers are
the only network.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "diary", "suggestions.jsonl")
MODES = ("trade", "notify", "off")
TAKEN_WITHIN_DAYS = 3          # a manual buy this soon after an idea counts as "taken"


def mode_for(block: dict, mode_key: str, enabled_key: str) -> str:
    m = str((block or {}).get(mode_key) or "").strip().lower()
    if m in MODES:
        return m
    return "trade" if (block or {}).get(enabled_key) else "off"


def session_today() -> str:
    return dt.datetime.now(ET).date().isoformat()


def _rows(limit: int = 2000) -> list[dict]:
    try:
        with open(LOG) as f:
            lines = f.readlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]
    except Exception:
        return []


def fence_check(symbol: str, side: str, usd: float) -> tuple[bool, str]:
    """(allowed, reason) from the same gate every real buy passes through."""
    try:
        import entry_gate as eg
        ok, why, _ = eg.check_entry(symbol, side, notional=float(usd or 1.0))
        return bool(ok), ("" if ok else short_reason(why))
    except Exception as e:
        return False, f"fence unavailable ({type(e).__name__})"


def short_reason(why: str) -> str:
    """'universe: AMD is not an anchor name — only …' → 'not on your list'."""
    w = (why or "").strip()
    head = w.split(":", 1)[0].strip().lower()
    names = {"universe": "not on your list", "window": "outside the entry window",
             "market": "market closed", "daily_loss": "daily-loss halt", "cooling_off": "cooling-off pause",
             "concurrent": "already five names", "cash_floor": "would break the cash floor",
             "entries": "entry limit for today", "loss_streak": "loss-streak halt", "gate": "fence error"}
    return names.get(head, w[:60])


def build_row(engine: str, symbol: str, side: str, usd: float, reason: str,
              fence_ok: bool, fence_why: str = "", session: str | None = None) -> dict:
    """One idea as a dict. Pure — nothing is written."""
    return {"ts": dt.datetime.now(ET).isoformat(timespec="seconds"), "session": session or session_today(),
            "engine": engine, "symbol": str(symbol).upper(), "side": side, "usd": round(float(usd or 0), 2),
            "reason": (reason or "")[:160], "fence_ok": bool(fence_ok), "fence_why": fence_why or ""}


def record(engine: str, symbol: str, side: str, usd: float, reason: str,
           fence_ok: bool, fence_why: str = "", session: str | None = None) -> dict | None:
    """Append one idea; None when the same engine already logged that idea today."""
    row = build_row(engine, symbol, side, usd, reason, fence_ok, fence_why, session)
    for r in _rows():
        if (r.get("session") == row["session"] and r.get("engine") == engine
                and r.get("symbol") == row["symbol"] and r.get("side") == side):
            return None
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def preview_fence(symbol: str, side: str, usd: float) -> tuple[bool, str]:
    """The fence's verdict as it would stand at 10:00 New York on the next
    trading weekday with the market open — for previews run outside the window.
    The time-of-day rules are answered 'yes' by construction; the list, the
    name count, the cash floor, the cooling-off pause and the daily halts are
    judged on today's real account."""
    try:
        import time
        import entry_gate as eg
        now = dt.datetime.now(ET)
        day = now.date() if now.weekday() < 5 else now.date() + dt.timedelta(days=7 - now.weekday())
        at = dt.datetime.combine(day, dt.time(10, 0), tzinfo=ET)
        eg._CACHE.update({"t": time.time(), "clock": {"is_open": True}})
        ok, why, _ = eg.check_entry(symbol, side, notional=float(usd or 1.0), now=at)
        return bool(ok), ("" if ok else short_reason(why))
    except Exception as e:
        return False, f"fence unavailable ({type(e).__name__})"


def format_line(r: dict) -> str:
    verdict = "allowed" if r.get("fence_ok") else f"blocked: {r.get('fence_why') or 'fence'}"
    amt = f" ${float(r.get('usd') or 0):,.0f}" if r.get("usd") else ""
    dot = "🟢" if r.get("fence_ok") else "⚪"
    return f"{dot} {str(r.get('side', '')).upper()} `{r.get('symbol')}`{amt} — {r.get('reason', '')} · {verdict}"


def for_session(session: str) -> list[dict]:
    return [r for r in _rows() if r.get("session") == session]


def taken(rows: list[dict], fills: list[dict]) -> list[dict]:
    """Mark each buy idea taken=True when a manual buy of that name filled on the
    idea's day or within TAKEN_WITHIN_DAYS after it. fills: {sym, side, date}."""
    out = []
    for r in rows:
        t = False
        if r.get("side") == "buy":
            d0 = dt.date.fromisoformat(str(r.get("session"))[:10])
            for f in fills:
                if str(f.get("sym", "")).upper() != r.get("symbol") or f.get("side") != "buy":
                    continue
                try:
                    fd = dt.date.fromisoformat(str(f.get("date"))[:10])
                except Exception:
                    continue
                if 0 <= (fd - d0).days <= TAKEN_WITHIN_DAYS:
                    t = True
                    break
        out.append(dict(r, taken=t))
    return out


def fetch_manual_fills(since: str) -> list[dict]:
    """Peter's own filled buys since `since` (YYYY-MM-DD), by order tag."""
    try:
        import requests
        import capitol_copier as cc
        r = requests.get(f"{cc.BASE_URL}/orders", headers=cc.ALPACA_HEADERS, timeout=15,
                         params={"status": "closed", "after": f"{since}T00:00:00Z", "limit": 300, "direction": "desc"})
        out = []
        for o in r.json() or []:
            cid = str(o.get("client_order_id") or "")
            if o.get("status") == "filled" and cid.startswith("manual"):
                out.append({"sym": o["symbol"], "side": o["side"], "date": str(o.get("filled_at") or "")[:10]})
        return out
    except Exception:
        return []


def notice(session: str, fills: list[dict] | None = None) -> str:
    """One line for the morning report; '' when the engines had no ideas."""
    rows = for_session(session)
    if not rows:
        return ""
    if fills is None:
        fills = fetch_manual_fills(session)
    return notice_from_rows(rows, fills)


def notice_from_rows(rows: list[dict], fills: list[dict]) -> str:
    """The report line from rows already in hand (pure)."""
    if not rows:
        return ""
    rows = taken(rows, fills)
    by = {}
    for r in rows:
        by.setdefault(r["engine"], []).append(r)
    parts = []
    for eng, rs in sorted(by.items()):
        items = []
        for r in rs:
            v = "allowed" if r["fence_ok"] else f"blocked, {r['fence_why']}"
            items.append(f"{r['side']} {r['symbol']}{' $' + format(r['usd'], ',.0f') if r.get('usd') else ''} ({v})"
                         + (" — taken" if r.get("taken") else ""))
        parts.append(f"{eng} → " + ", ".join(items))
    n_buy = sum(1 for r in rows if r["side"] == "buy")
    n_taken = sum(1 for r in rows if r.get("taken"))
    tail = f" · taken {n_taken}/{n_buy}" if n_buy else ""
    return "💡 Ideas from the engines (advice only): " + " · ".join(parts) + tail


if __name__ == "__main__":
    import sys
    sess = sys.argv[1] if len(sys.argv) > 1 else session_today()
    print(notice(sess) or f"(no ideas logged for {sess})")
