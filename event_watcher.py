"""
event_watcher.py — watches trading state files for changes, fires Telegram alerts.

Runs every 5 minutes via launchd. Tracks the last-seen state of:
  - performance_log.json   (new closed trades)
  - pool_state.json        (pool composition / weight changes)
  - .copied_trades.json    (new copied trades)
  - Alpaca positions       (stop-loss fills, ladder fills)

Stores its own "last seen" state in .event_watcher_state.json to avoid
re-notifying on already-seen events.

Notifications go via telegram_notifier.py — silent fail if Telegram not configured.
"""

import os, json, requests
from datetime import datetime, timezone
from dotenv import load_dotenv

import telegram_notifier as tg

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
H_ALPACA   = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}

ROOT       = os.path.dirname(__file__)
STATE_FILE = os.path.join(ROOT, ".event_watcher_state.json")

PERF_LOG   = os.path.join(ROOT, "performance_log.json")
POOL_STATE = os.path.join(ROOT, "pool_state.json")
COPIED     = os.path.join(ROOT, ".copied_trades.json")


def load_watcher_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try:    return json.load(f)
            except: pass
    return {
        "last_seen_trade_count": 0,
        "last_pool_hash":        "",
        "last_copied_count":     0,
        "last_filled_orders":    [],
        "first_run":             True,
    }


def save_watcher_state(s):
    s["last_run_at"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def safe_load_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def pool_hash(pool_data):
    """Hash the meaningful pool state (member list + weights)."""
    pool = pool_data.get("pool", [])
    keys = [(p["politician_id"], round(p["weight"], 3), p.get("rank")) for p in pool]
    return json.dumps(sorted(keys, key=lambda x: x[2] or 99))


# ── Event detectors ──────────────────────────────────────────────────────────

def check_new_closed_trades(state):
    """Detect new closed trades, notify on each."""
    log = safe_load_json(PERF_LOG, {"trades": []})
    trades = log.get("trades", [])
    n = len(trades)
    last_n = state.get("last_seen_trade_count", 0)

    if state.get("first_run"):
        state["last_seen_trade_count"] = n
        return 0

    if n > last_n:
        new = trades[last_n:]
        for t in new:
            sym  = t["symbol"]
            ret  = t.get("return_pct", 0) * 100
            pnl  = t.get("pnl_usd", 0)
            arrow = "✅" if ret > 0 else "❌"
            tg.send(f"{arrow} *Closed: {sym}*  {ret:+.1f}%  (${pnl:+.2f})\n"
                    f"Entry ${t.get('entry_price'):.2f} → Exit ${t.get('exit_price'):.2f} "
                    f"after {t.get('hold_days')}d ({t.get('strategy')})")
        state["last_seen_trade_count"] = n
        return len(new)
    return 0


def check_pool_changes(state):
    """Detect pool composition or weight changes."""
    pool_data = safe_load_json(POOL_STATE)
    if not pool_data.get("pool"):
        return 0

    h = pool_hash(pool_data)
    last_h = state.get("last_pool_hash", "")

    if state.get("first_run"):
        state["last_pool_hash"] = h
        return 0

    if h != last_h:
        pool = pool_data["pool"]
        rebal_reason = pool_data.get("rebalanced_by", "update")
        grads = pool_data.get("graduations", [])

        # Compose a single message describing the new state
        lines = [f"🔄 *Pool updated* ({rebal_reason})"]
        for p in pool:
            status = "🥉" if p.get("is_probationary") else "🏆"
            lines.append(f"{status} #{p['rank']} `{p['politician_id']}`  "
                         f"{p['weight']*100:.0f}%  score {p['score']:.3f}")
        if grads:
            for g in grads:
                lines.append(f"🎓 Graduation: `{g['promoted']}` ↔ `{g['demoted']}`")

        tg.send("\n".join(lines))
        state["last_pool_hash"] = h
        return 1
    return 0


def check_copied_trades(state):
    """Detect new trades copied by capitol_copier."""
    copied = safe_load_json(COPIED, {"copied": []})
    n = len(copied.get("copied", []))
    last_n = state.get("last_copied_count", 0)

    if state.get("first_run"):
        state["last_copied_count"] = n
        return 0

    if n > last_n:
        # Don't spam — just note the count
        tg.send(f"📋 {n - last_n} new trade(s) copied (total: {n})")
        state["last_copied_count"] = n
        return n - last_n
    return 0


def check_alpaca_fills(state):
    """Detect fresh order fills from Alpaca — especially stop-loss triggers."""
    if not API_KEY:
        return 0
    try:
        r = requests.get(
            f"{BASE_URL}/orders",
            headers=H_ALPACA,
            params={"status": "filled", "limit": 50, "direction": "desc"},
            timeout=10,
        )
        orders = r.json() if r.status_code == 200 else []
    except Exception:
        return 0

    if not isinstance(orders, list):
        return 0

    last_seen = set(state.get("last_filled_orders", []))
    notified = 0

    if state.get("first_run"):
        state["last_filled_orders"] = [o["id"] for o in orders[:50]]
        return 0

    # Collect all fresh fills and push ONE consolidated message instead of a
    # separate notification per fill.
    fill_lines = []
    for o in orders:
        oid = o["id"]
        if oid in last_seen:
            continue
        sym  = o["symbol"]
        side = o["side"]
        otype = o["type"]
        qty   = o.get("filled_qty", o.get("qty", "?"))
        price = o.get("filled_avg_price", "?")

        if otype == "stop" and side == "sell":
            fill_lines.append(f"🛑 STOP SELL `{sym}` {qty} @ ${price}")
        else:
            emoji = "🟢" if side == "buy" else "🔴"
            fill_lines.append(f"{emoji} {side.upper()} `{sym}` {qty} @ ${price}")
        notified += 1

    if fill_lines:
        tg.notify_batch("Order fills", fill_lines, emoji="💰")

    state["last_filled_orders"] = [o["id"] for o in orders[:50]]
    return notified


# ── Main runner ─────────────────────────────────────────────────────────────

def run():
    state = load_watcher_state()
    first = state.get("first_run", True)

    closed = check_new_closed_trades(state)
    pool   = check_pool_changes(state)
    copied = check_copied_trades(state)
    fills  = check_alpaca_fills(state)

    if first:
        # First run: silently seed state, then exit
        state["first_run"] = False
        save_watcher_state(state)
        print(f"[event_watcher] first run — state seeded (silent)")
        return

    save_watcher_state(state)
    total = closed + pool + copied + fills
    if total > 0:
        print(f"[event_watcher] notified: {closed} closed, {pool} pool, "
              f"{copied} copied, {fills} fills")
    else:
        print(f"[event_watcher] no new events")


if __name__ == "__main__":
    run()
