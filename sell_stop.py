#!/usr/bin/env python3
"""
Sell Stop — consecutive-uptrend exit rule for the High Risk wallet.

Rule (per Peter):
- Track 5 consecutive trading days UP (close > previous close) for each open position.
- Weekends/holidays don't break the count; only trading days count.
- On the 4th consecutive up day: send Telegram warning (heads-up).
- On the 5th consecutive up day:
    * Gain from streak start > 15% → sell 100% of position
    * Gain from streak start > 5%  → sell 50% of position
    * Gain ≤ 5%                   → hold (streak continues, re-evaluate next up day)
- Down/flat days: streak resets to 0, no action.
- Only exits; never enters.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from capitol_copier import BASE_URL, ALPACA_HEADERS, DATA_URL, core_holds
import telegram_notifier as tn

STATE_FILE = Path(__file__).with_name("diary") / "sell_stop_state.json"
ET = __import__("zoneinfo").ZoneInfo("America/New_York")


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _fetch_daily_bars(symbol: str, start: date, end: date) -> list[dict]:
    """Fetch daily bars from Alpaca (IEX feed)."""
    url = f"{DATA_URL}/stocks/{symbol}/bars"
    params = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 1000,
        "adjustment": "split",
        "feed": "iex",
    }
    r = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("bars", [])


def _get_positions() -> list[dict]:
    r = requests.get(f"{BASE_URL}/positions", headers=ALPACA_HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def _sell(symbol: str, frac: float = 1.0) -> dict:
    """Sell via anchor_trade.py (sell path)."""
    import subprocess
    cmd = ["python3", "anchor_trade.py", "sell", symbol]
    if frac < 1.0:
        cmd += ["--frac", str(frac)]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parent)
    return {"symbol": symbol, "frac": frac, "stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}


def _warn_fourth_day(symbol: str, streak: int, current_price: float, streak_start_price: float,
                     is_core: bool = False) -> bool:
    """Send Telegram warning on 4th consecutive up day."""
    if streak != 4 or not streak_start_price:
        return False
    gain_pct = (current_price - streak_start_price) / streak_start_price * 100.0
    tail = ("★ CORE hold — this rule will NOT sell it (unmark with C in the dashboard to change that)"
            if is_core else
            "Tomorrow (5th up day) triggers: >15% = sell all, >5% = sell half")
    msg = (
        f"⚠️ *Sell Stop Warning — 4th Consecutive Up Day*\n"
        f"*{symbol}* — {streak} days up, gain from streak start: {gain_pct:+.2f}%\n"
        f"Current: ${current_price:.2f} | Streak start: ${streak_start_price:.2f}\n"
        f"{tail}"
    )
    return tn.send(msg)


def decide(streak: int, gain_pct: float, is_core: bool) -> str:
    """The rule as a pure function — 'sell_all' | 'sell_half' | 'hold' |
    'core_hold' | 'none'. CORE holds are never sold by this rule; they still
    get the day-4 warning so Peter knows the streak is there."""
    if streak < 5:
        return "none"
    if is_core:
        return "core_hold"
    if gain_pct > 15:
        return "sell_all"
    if gain_pct > 5:
        return "sell_half"
    return "hold"


def run_check(now: datetime | None = None, dry_run: bool = False) -> dict:
    """
    Main entry point. Call once per trading day after the close (or before next open).
    Returns a summary of actions taken. dry_run reports what it WOULD do and sells nothing.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    today = now.date()
    core = core_holds()

    # Only run on trading weekdays
    if today.weekday() >= 5:
        return {"skipped": "weekend", "date": today.isoformat()}

    positions = _get_positions()
    if not positions:
        return {"skipped": "no positions", "date": today.isoformat()}

    state = _load_state()
    actions = []

    for pos in positions:
        sym = pos["symbol"]
        qty = float(pos["qty"])
        avg_entry = float(pos["avg_entry_price"])
        current_price = float(pos["current_price"])

        # Fetch last ~10 trading days to compute streak
        # Go back 14 calendar days to be safe
        start = today - timedelta(days=14)
        bars = _fetch_daily_bars(sym, start, today)
        if len(bars) < 2:
            continue

        # Build list of (date, close) for trading days only, most recent last
        daily = []
        for b in bars:
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
            daily.append((ts.date(), float(b["c"])))
        daily.sort(key=lambda x: x[0])

        # Compute consecutive up streak ending at most recent bar
        streak = 0
        streak_start_price = None
        for i in range(1, len(daily)):
            prev_close = daily[i - 1][1]
            curr_close = daily[i][1]
            if curr_close > prev_close:
                if streak == 0:
                    streak_start_price = prev_close
                streak += 1
            else:
                streak = 0
                streak_start_price = None

        # Persist streak in state
        sym_state = state.get(sym, {"streak": 0, "streak_start_price": None, "last_action_date": None, "warned_day4": False})
        sym_state["streak"] = streak
        sym_state["streak_start_price"] = streak_start_price
        state[sym] = sym_state

        is_core = sym in core

        # 4th day warning (once per streak)
        if streak == 4 and not sym_state.get("warned_day4") and streak_start_price:
            warned = True if dry_run else _warn_fourth_day(sym, streak, current_price, streak_start_price, is_core)
            if warned:
                sym_state["warned_day4"] = True
                actions.append({"symbol": sym, "action": "warn_day4", "core": is_core,
                                "gain_pct": (current_price - streak_start_price) / streak_start_price * 100.0})

        # Trigger on 5th consecutive up day
        if streak == 5 and streak_start_price:
            gain_pct = (current_price - streak_start_price) / streak_start_price * 100.0
            last_action = sym_state.get("last_action_date")
            if last_action != today.isoformat():  # avoid double-fire same day
                verdict = decide(streak, gain_pct, is_core)
                frac = {"sell_all": 1.0, "sell_half": 0.5}.get(verdict)
                if frac is not None:
                    res = {"dry_run": True} if dry_run else _sell(sym, frac=frac)
                    actions.append({"symbol": sym, "action": verdict, "gain_pct": gain_pct, "result": res})
                    if not dry_run:
                        sym_state["last_action_date"] = today.isoformat()
                else:
                    actions.append({"symbol": sym, "action": verdict, "gain_pct": gain_pct, "streak": streak})

        # Reset streak tracking on down/flat day
        if streak == 0:
            sym_state["streak_start_price"] = None
            sym_state["warned_day4"] = False

    if not dry_run:                      # a dry run must leave no trace
        _save_state(state)
    return {"date": today.isoformat(), "actions": actions, "state": state}


if __name__ == "__main__":
    import sys
    out = run_check(dry_run="--dry-run" in sys.argv)   # --dry-run: report, never sell
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0)