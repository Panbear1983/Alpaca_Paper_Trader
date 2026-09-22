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

import fcntl
import os, json, requests
from datetime import datetime, timezone
from dotenv import load_dotenv

import telegram_notifier as tg

load_dotenv()

# Single-instance guard: this launchd job fires every 60s (StartInterval),
# but has no minimum runtime guarantee. If a run is still mid-flight when the
# next one fires (slow Alpaca API, a DNS retry, etc.) two processes race on
# reading/writing .event_watcher_state.json with no coordination — each one
# sees the same "new" fills against a stale last_filled_orders list, both
# notify, and the loser's save is clobbered, so the same fills get reported
# again next cycle. Confirmed happening in .logs/event_watcher.out.log
# (identical "50 fills" notifications repeating for 40000+ log lines).
_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".event_watcher.lock")


def _acquire_singleton_lock():
    lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[event_watcher] another instance is already running "
              f"(lock held on {_LOCK_PATH}) — exiting instead of racing it.")
        raise SystemExit(0)   # not an error condition — just skip this tick
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()
    return lock_fh   # caller must keep this open for the process lifetime

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
# Alpaca trading endpoints all live under /v2. ALPACA_BASE_URL has been
# written both ways; a base missing the suffix 404s on every request and
# the error handling here reads that as "nothing found" rather than
# "broken". Normalise through the one shared helper.
from wallets import norm_base as _norm_base
BASE_URL   = _norm_base(os.getenv("ALPACA_BASE_URL", ""))
H_ALPACA   = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}

ROOT       = os.path.dirname(__file__)
STATE_FILE = os.path.join(ROOT, ".event_watcher_state.json")

PERF_LOG   = os.path.join(ROOT, "performance_log.json")
POOL_STATE = os.path.join(ROOT, "pool_state.json")
try:        # per-wallet split: watch the DEFAULT wallet's dedup state
    import strategies
    COPIED = strategies.state_path(".copied_trades.json")
except Exception:
    COPIED = os.path.join(ROOT, ".copied_trades.json")


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
        if r.status_code != 200:
            return 0
        orders = r.json()
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

        if otype in ("stop", "trailing_stop") and side == "sell":
            fill_lines.append(f"🛑 STOP SELL `{sym}` {qty} @ ${price}")
        else:
            emoji = "🟢" if side == "buy" else "🔴"
            fill_lines.append(f"{emoji} {side.upper()} `{sym}` {qty} @ ${price}")
        notified += 1

    if fill_lines:
        tg.notify_batch("Order fills", fill_lines, emoji="💰")

    state["last_filled_orders"] = [o["id"] for o in orders[:50]]
    return notified


# ── Watchdog: are the protective processes actually WORKING? ────────────────
# (2026-09-22) The price watcher has KeepAlive=true, so it is always "running";
# it had also been crashing every loop for weeks without a single alert. This
# check reads the heartbeat it writes only after a complete sweep, the
# scheduler's last exit-engine stamp, and whether every whole-share High Risk
# position has its broker guard. Silent when all is well; one Telegram per
# episode, repeated every 30 minutes while it persists, one line on recovery.

import time as _time
import math as _math

HEARTBEAT_FILE = os.path.join(ROOT, "diary", "watcher_heartbeat.json")
SCHED_STATE    = os.path.join(ROOT, ".trading_schedule_state.json")
HR_STRATEGY    = os.path.join(ROOT, "strategy_high_risk.json")
HB_STALE_S     = 180        # watcher sweeps every 30 s; 3 min silent = dead
SCHED_STALE_S  = 40 * 60    # exit engine runs every 15 min; 40 min = stalled
REALERT_S      = 30 * 60
STREAK_N       = 3          # consecutive checks before a "soft" failure alerts


def _market_open_now():
    """Alpaca's own clock (holiday-aware). Unsure → False → no alerts."""
    try:
        r = requests.get(f"{BASE_URL}/clock", headers=H_ALPACA, timeout=10)
        return bool(r.json().get("is_open"))
    except Exception:
        return False


def _hr_positions_and_guards():
    """(whole-share positions, symbols with a resting guard) for the default
    (High Risk) wallet. Two requests."""
    pos = requests.get(f"{BASE_URL}/positions", headers=H_ALPACA, timeout=10).json()
    orders = requests.get(f"{BASE_URL}/orders", headers=H_ALPACA,
                          params={"status": "open", "limit": 200}, timeout=10).json()
    whole = {p["symbol"].upper() for p in pos
             if isinstance(p, dict) and _math.floor(abs(float(p.get("qty") or 0))) >= 1}
    guarded = {o["symbol"].upper() for o in orders
               if isinstance(o, dict) and o.get("type") == "trailing_stop" and o.get("side") == "sell"
               and str(o.get("client_order_id") or "").startswith("pstop-")}
    return whole, guarded


def _guards_enabled():
    try:
        a = safe_load_json(HR_STRATEGY, {}).get("anchor") or {}
        return bool(a.get("broker_stops"))
    except Exception:
        return False


def _exits_on():
    try:
        cc = safe_load_json(HR_STRATEGY, {}).get("capitol_copier") or {}
        return bool(cc.get("exits_on", cc.get("autorun_enabled", False)))
    except Exception:
        return False


def _parse_iso_epoch(s):
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return 0.0


def _since_open(now):
    """Seconds since today's 09:30 New York. Both stamps we watch are only
    written while the market is open, so for the first minutes of a session
    they legitimately still show yesterday — alerting then would be a false
    alarm at every open."""
    try:
        from zoneinfo import ZoneInfo
        et = datetime.fromtimestamp(now, tz=ZoneInfo("America/New_York"))
        opened = et.replace(hour=9, minute=30, second=0, microsecond=0)
        return max(0.0, (et - opened).total_seconds())
    except Exception:
        return 10 ** 9


def check_heartbeats(state, now=None, hb=None, sched=None, market_open=None,
                     guards=None, exits_on=None, notify=None, since_open=None) -> int:
    """Return the number of alerts sent. Every input can be injected for
    tests; the defaults read the live files and the broker."""
    if state.get("first_run"):
        return 0
    now = now if now is not None else _time.time()
    if market_open is None:
        market_open = _market_open_now()
    if not market_open:
        return 0                                    # nothing is expected to run
    since_open = _since_open(now) if since_open is None else since_open

    hb = hb if hb is not None else safe_load_json(HEARTBEAT_FILE, {})
    sched = sched if sched is not None else safe_load_json(SCHED_STATE, {})
    exits_on = _exits_on() if exits_on is None else exits_on
    notify = notify or (lambda kind, msg: tg.notify_position_alert("WATCHDOG", msg, "critical"))

    alerted = state.setdefault("_hb_alerted", {})
    streaks = state.setdefault("_hb_streaks", {})
    problems = {}

    # 1. the price watcher's heartbeat
    age = now - float(hb.get("epoch") or 0)
    if age > HB_STALE_S and since_open > HB_STALE_S:
        problems["watcher"] = (f"price watcher silent for {age/60:.0f} min — stops and "
                               f"guards are NOT being maintained (last sweep {hb.get('ts') or 'never'})")
    n_pos, n_priced = int(hb.get("n_positions") or 0), int(hb.get("n_priced") or 0)
    streaks["unpriced"] = streaks.get("unpriced", 0) + 1 if n_pos and n_priced < n_pos else 0
    if streaks["unpriced"] >= STREAK_N:
        problems["unpriced"] = (f"price watcher is alive but could only price {n_priced} of "
                                f"{n_pos} positions for {streaks['unpriced']} sweeps — its stops are blind")

    # 2. the exit engine's last run (only meaningful when it is switched on)
    if exits_on and since_open > SCHED_STALE_S:
        last = _parse_iso_epoch((sched.get("high_risk") or {}).get("last_manage_at"))
        if last and now - last > SCHED_STALE_S:
            problems["scheduler"] = (f"exit engine has not run for {(now-last)/60:.0f} min "
                                     f"(software stops / trims / profit tiers are stalled)")

    # 3. every whole-share position must have its broker guard
    if _guards_enabled() or guards is not None:
        try:
            whole, guarded = guards if guards is not None else _hr_positions_and_guards()
            missing = sorted(whole - guarded)
        except Exception as e:
            missing, whole = [], set()
            problems.setdefault("guards_unknown", f"could not check broker guards: {e}")
        streaks["unguarded"] = streaks.get("unguarded", 0) + 1 if missing else 0
        if streaks["unguarded"] >= STREAK_N:
            problems["guards"] = (f"no broker stop resting for {', '.join(missing)} for "
                                  f"{streaks['unguarded']} checks — reconciler is not re-arming")

    sent = 0
    for kind, msg in problems.items():
        last = float(alerted.get(kind) or 0)
        if now - last >= REALERT_S:
            try:
                notify(kind, msg)
                sent += 1
            except Exception as e:
                print(f"[watchdog] notify failed: {e}")
            alerted[kind] = now
    for kind in [k for k in alerted if k not in problems]:
        try:
            notify(kind, f"recovered: {kind} is back to normal")
            sent += 1
        except Exception:
            pass
        alerted.pop(kind, None)
    return sent


# ── Main runner ─────────────────────────────────────────────────────────────

def run():
    _lock = _acquire_singleton_lock()   # kept open for the process lifetime
    state = load_watcher_state()
    first = state.get("first_run", True)

    closed = check_new_closed_trades(state)
    if closed > 0: save_watcher_state(state)
    
    pool   = check_pool_changes(state)
    if pool > 0: save_watcher_state(state)
    
    copied = check_copied_trades(state)
    if copied > 0: save_watcher_state(state)
    
    fills  = check_alpaca_fills(state)
    if fills > 0: save_watcher_state(state)

    dog    = check_heartbeats(state)
    save_watcher_state(state)          # streak counters change every tick

    if first:
        # First run: silently seed state, then exit
        state["first_run"] = False
        save_watcher_state(state)
        print(f"[event_watcher] first run — state seeded (silent)")
        return

    save_watcher_state(state) # Final save to update last_run_at timestamp
    total = closed + pool + copied + fills + dog
    if total > 0:
        print(f"[event_watcher] notified: {closed} closed, {pool} pool, "
              f"{copied} copied, {fills} fills, {dog} watchdog")
    else:
        print(f"[event_watcher] no new events")


if __name__ == "__main__":
    run()
