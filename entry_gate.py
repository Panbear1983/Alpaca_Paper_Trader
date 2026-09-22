#!/usr/bin/env python3
"""
entry_gate.py — the anchor-plan fence that every BUY on the High Risk wallet passes through.

Wired into capitol_copier.place_market_order() as the third check after the per-name and
gross caps, so it binds on every engine, on the price watcher's buy-back, on the TUI's manual
buy, and on WB's anchor_trade.py — nothing that goes through the order path can step around it.
Sells always pass: the fence limits what gets opened, never what gets closed.

What it enforces (all read from the `anchor` block of strategy_high_risk.json; when
`anchor.enabled` is false the gate passes everything):

  universe     only the anchor names may be bought
  window       new entries only between entry_window_et[0] and [1], New York time
  market       the Alpaca clock says the market is open (holidays are not weekends)
  daily_loss   equity down daily_loss_limit_pct or more from last close -> no new buys today
  entries      at most max_entries_per_name_per_day filled buys per name per day, counted from
               Alpaca fills (not from our own journal) so a buy placed around the fence still
               counts; max_entries_per_day > 0 adds a total cap
  loss_streak  consecutive_loss_halt closed losers in a row today (from diary/anchor_trades.jsonl)
               -> no new buys for the rest of the session

Every refusal returns a plain-English reason that names the rule, so the caller can print it
verbatim and the daily oversight run can count refusals by category.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
_HERE = Path(__file__).resolve().parent
JOURNAL = _HERE / "diary" / "anchor_trades.jsonl"

_CACHE: dict = {"t": 0.0, "account": None, "orders": None, "clock": None}
_TTL = 30  # seconds — same idea as capitol_copier._CAP_CACHE: keep the fence off the rate limiter

DEFAULTS = {
    "enabled": False,
    "universe": [],
    "entry_window_et": ["09:30", "11:30"],
    "max_entries_per_name_per_day": 2,
    "max_entries_per_day": 0,
    "consecutive_loss_halt": 2,
    "daily_loss_limit_pct": 3.0,
}


# ── config / data access (module-level so tests can replace them) ───────────

def load_cfg() -> dict:
    import strategies
    cfg = dict(DEFAULTS)
    cfg.update((strategies.load_merged().get("anchor") or {}))
    return cfg


def _api():
    import capitol_copier as cc
    return cc.BASE_URL, cc.ALPACA_HEADERS


def _fresh(key: str, fn):
    now = time.time()
    if now - _CACHE["t"] > _TTL:
        _CACHE.update({"t": now, "account": None, "orders": None, "clock": None})
    if _CACHE.get(key) is None:
        _CACHE[key] = fn()
    return _CACHE[key]


def fetch_account() -> dict:
    base, headers = _api()
    r = requests.get(f"{base}/account", headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_clock() -> dict:
    base, headers = _api()
    r = requests.get(f"{base}/clock", headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_todays_orders(now: dt.datetime) -> list[dict]:
    """Every closed order submitted today (ET), oldest first. Mirrors the request
    wb_daily_report.fetch_session_trades makes, without importing that module."""
    base, headers = _api()
    day = now.astimezone(ET).date()
    after = dt.datetime.combine(day, dt.time(0, 0), tzinfo=ET).isoformat()
    until = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=ET).isoformat()
    out, seen = [], set()
    for _ in range(5):
        r = requests.get(f"{base}/orders", headers=headers, timeout=15,
                         params={"status": "closed", "limit": 500, "direction": "asc",
                                 "after": after, "until": until})
        r.raise_for_status()
        page = r.json() or []
        fresh = [o for o in page if o.get("id") not in seen]
        for o in fresh:
            seen.add(o.get("id"))
        out.extend(fresh)
        if len(page) < 500 or not fresh:
            break
        after = page[-1].get("submitted_at") or after
    return out


def read_journal() -> list[dict]:
    try:
        return [json.loads(l) for l in JOURNAL.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []
    except Exception:
        return []


# ── derived facts ───────────────────────────────────────────────────────────

def _now(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(ET)).astimezone(ET)


def _parse_hhmm(s: str) -> dt.time:
    h, m = str(s).split(":")
    return dt.time(int(h), int(m))


def in_window(cfg: dict, now: dt.datetime | None = None) -> bool:
    n = _now(now)
    if n.weekday() >= 5:
        return False
    lo, hi = cfg.get("entry_window_et") or DEFAULTS["entry_window_et"]
    return _parse_hhmm(lo) <= n.time() <= _parse_hhmm(hi)


def day_change_pct(account: dict) -> float | None:
    try:
        eq, last = float(account.get("equity")), float(account.get("last_equity"))
        if last > 0:
            return (eq - last) / last * 100.0
    except Exception:
        pass
    return None


def entries_today(orders: list[dict]) -> dict[str, int]:
    """Filled BUY orders per symbol today. Counts fills, so an order placed directly
    against the API (around this fence) still uses up the budget."""
    counts: dict[str, int] = {}
    for o in orders:
        if str(o.get("side", "")).lower() != "buy" or o.get("status") != "filled":
            continue
        sym = str(o.get("symbol", "")).upper()
        counts[sym] = counts.get(sym, 0) + 1
    return counts


def loss_streak_today(rows: list[dict], now: dt.datetime | None = None) -> int:
    """Trailing run of losing closes in today's journal (0 if the last close was a win)."""
    day = _now(now).date().isoformat()
    closed = [r for r in rows
              if r.get("status") == "closed" and str(r.get("closed_at", ""))[:10] == day]
    closed.sort(key=lambda r: r.get("closed_at", ""))
    streak = 0
    for r in reversed(closed):
        try:
            pnl = float(r.get("pnl", 0))
        except Exception:
            pnl = 0.0
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


# ── the gate ────────────────────────────────────────────────────────────────

def check_entry(ticker: str, side: str, notional=None, qty=None, now: dt.datetime | None = None,
                cfg: dict | None = None) -> tuple[bool, str, str]:
    """(ok, reason, category). Buy-only; sells always pass with category ''."""
    if str(side).lower() != "buy":
        return True, "", ""
    cfg = cfg if cfg is not None else load_cfg()
    if not cfg.get("enabled"):
        return True, "", ""
    sym = str(ticker).upper()
    n = _now(now)

    universe = [str(u).upper() for u in (cfg.get("universe") or [])]
    if sym not in universe:
        return False, (f"universe: {sym} is not an anchor name — only "
                       f"{', '.join(universe)} may be bought; everything else is exit-only"), "universe"

    if not in_window(cfg, n):
        lo, hi = cfg.get("entry_window_et") or DEFAULTS["entry_window_et"]
        return False, (f"window: it is {n:%H:%M} New York time — new entries are allowed only "
                       f"{lo}–{hi} ET on weekdays"), "window"

    try:
        clock = _fresh("clock", fetch_clock)
        if clock and not clock.get("is_open", True):
            return False, "market: the Alpaca clock says the market is closed (holiday?)", "market"
    except Exception as e:
        return False, f"market: cannot read the Alpaca clock ({e}) — refusing to open blind", "market"

    try:
        account = _fresh("account", fetch_account)
    except Exception as e:
        return False, f"daily_loss: cannot read the account ({e}) — refusing to open blind", "daily_loss"
    chg = day_change_pct(account)
    limit = float(cfg.get("daily_loss_limit_pct") or DEFAULTS["daily_loss_limit_pct"])
    if chg is not None and chg <= -limit:
        return False, (f"daily_loss: equity is {chg:+.2f}% on the day, past the -{limit:.1f}% "
                       f"limit — no new positions for the rest of the session"), "daily_loss"

    try:
        orders = _fresh("orders", lambda: fetch_todays_orders(n))
    except Exception as e:
        return False, f"entries: cannot read today's orders ({e}) — refusing to open blind", "entries"
    counts = entries_today(orders)
    per_name = int(cfg.get("max_entries_per_name_per_day") or 0)
    if per_name and counts.get(sym, 0) >= per_name:
        return False, (f"entries: {sym} already has {counts[sym]} filled buy(s) today — "
                       f"the limit is {per_name} per name per day"), "entries"
    total_cap = int(cfg.get("max_entries_per_day") or 0)
    if total_cap and sum(counts.values()) >= total_cap:
        return False, (f"entries: {sum(counts.values())} buys already filled today — "
                       f"the daily limit is {total_cap}"), "entries"

    halt_n = int(cfg.get("consecutive_loss_halt") or 0)
    if halt_n:
        streak = loss_streak_today(read_journal(), n)
        if streak >= halt_n:
            return False, (f"loss_streak: {streak} losing anchor trades in a row today — "
                           f"trading is halted for the rest of the session"), "loss_streak"

    return True, "", ""


def status(now: dt.datetime | None = None) -> dict:
    """Snapshot for the scanner and anchor_trade status: what the fence would say right now."""
    cfg = load_cfg()
    n = _now(now)
    out = {"enabled": bool(cfg.get("enabled")), "universe": cfg.get("universe") or [],
           "window": cfg.get("entry_window_et"), "in_window": in_window(cfg, n),
           "et_time": n.strftime("%Y-%m-%d %H:%M ET"), "day_change_pct": None,
           "daily_loss_halt": False, "entries_today": {}, "loss_streak": 0,
           "loss_halt": False, "market_open": None, "errors": []}
    try:
        account = _fresh("account", fetch_account)
        out["day_change_pct"] = day_change_pct(account)
        lim = float(cfg.get("daily_loss_limit_pct") or 3.0)
        out["daily_loss_halt"] = out["day_change_pct"] is not None and out["day_change_pct"] <= -lim
        out["equity"] = float(account.get("equity") or 0)
    except Exception as e:
        out["errors"].append(f"account: {e}")
    try:
        out["market_open"] = bool(_fresh("clock", fetch_clock).get("is_open"))
    except Exception as e:
        out["errors"].append(f"clock: {e}")
    try:
        out["entries_today"] = entries_today(_fresh("orders", lambda: fetch_todays_orders(n)))
    except Exception as e:
        out["errors"].append(f"orders: {e}")
    out["loss_streak"] = loss_streak_today(read_journal(), n)
    out["loss_halt"] = out["loss_streak"] >= int(cfg.get("consecutive_loss_halt") or 99)
    return out


if __name__ == "__main__":
    print(json.dumps(status(), indent=2, default=str))
