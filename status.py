#!/usr/bin/env python3
"""
status.py — READ-ONLY account + strategy snapshot for Hermes/Telegram.

Stage 1 command: Hermes runs this when you ask "status", "portfolio", "how are
we doing". It ONLY reads — GET calls to Alpaca + local state files. It places
no orders, edits no files, changes no config. Safe to expose over Telegram.

Usage:
    python3 status.py            # print clean summary to stdout (Hermes relays)
    python3 status.py --push     # also push directly to Telegram

Guaranteed read-only: the only Alpaca calls are GET /account and GET /positions.
No POST, no DELETE, no order placement, no file writes.
"""
from __future__ import annotations
import os, json, sys
from pathlib import Path
import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv(Path.home() / ".hermes" / ".env", override=False)

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
H = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}

# Bioguide → name (for the pool display)
NAMES = {
    "B001277": ("Richard Blumenthal", "CT-D"),
    "G000583": ("Josh Gottheimer", "NJ-D"),
    "M001157": ("Michael McCaul", "TX-R"),
    "S000168": ("Maria Elvira Salazar", "FL-R"),
    "T000490": ("David Taylor", "OH-R"),
    "K000389": ("Ro Khanna", "CA-D"),
}


def _get(path):
    """READ-ONLY GET helper."""
    r = requests.get(f"{BASE_URL}{path}", headers=H, timeout=20)
    r.raise_for_status()
    return r.json()


def build_status() -> str:
    L = []
    # --- Account ---
    acct = _get("/account")
    eq   = float(acct["equity"]); last = float(acct["last_equity"])
    day  = eq - last; daypct = (day / last * 100) if last else 0
    cash = float(acct["cash"])
    L.append("📟 STATUS · read-only snapshot")
    L.append(f"Equity   ${eq:,.0f}   Day {day:+,.0f} ({daypct:+.2f}%)")
    L.append(f"Cash     ${cash:,.0f}   ({cash/eq*100:.0f}% idle)")

    # --- Positions ---
    pos = _get("/positions")
    if not isinstance(pos, list):
        pos = []
    pos.sort(key=lambda p: float(p["unrealized_pl"]), reverse=True)
    unreal = sum(float(p["unrealized_pl"]) for p in pos)
    L.append(f"Positions {len(pos)}   Unrealized {unreal:+,.0f}")
    if pos:
        top = pos[0]; bot = pos[-1]
        L.append(f"  Best  {top['symbol']} {float(top['unrealized_plpc'])*100:+.1f}%"
                 f" (${float(top['unrealized_pl']):+.0f})")
        L.append(f"  Worst {bot['symbol']} {float(bot['unrealized_plpc'])*100:+.1f}%"
                 f" (${float(bot['unrealized_pl']):+.0f})")

    # --- Pool (read local state) ---
    pool_file = HERE / "pool_state.json"
    if pool_file.exists():
        try:
            pool = json.load(open(pool_file)).get("pool", [])
            if pool:
                L.append("Smart-money pool (who we follow):")
                for p in pool:
                    pid = p["politician_id"]
                    name, st = NAMES.get(pid, (pid, "?"))
                    flag = " ⚠prob" if p.get("is_probationary") else ""
                    L.append(f"  {name} ({st}) {p['weight']*100:.0f}%{flag}")
        except Exception:
            pass

    L.append("")
    L.append("(read-only · no trades or changes made)")
    return "\n".join(L)


def push_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_HOME_CHANNEL") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=15)
    return r.status_code == 200


if __name__ == "__main__":
    out = build_status()
    print(out)
    if "--push" in sys.argv:
        ok = push_telegram(out)
        print("\n[push] " + ("sent ✓" if ok else "skipped (no telegram creds)"), file=sys.stderr)
