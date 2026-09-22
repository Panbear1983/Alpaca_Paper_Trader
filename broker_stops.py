"""
broker_stops.py — a resting trailing-stop SELL order at Alpaca for every position
in a boxed wallet (today: High Risk only).

Why this exists (2026-09-22): every stop in this system was software on the Mac.
If the laptop slept, the watcher died, or the network dropped, nothing protected
a position — and the watcher had in fact been crashing every loop for weeks
without anyone knowing. A resting order lives on Alpaca's servers and fires
whether this machine is awake or not.

Design, in one breath: per name the stop trails the high-water mark by a width
set from how much that name normally moves (2.5 x 14-day ATR, clamped 6%–14%);
Alpaca only takes WHOLE shares on GTC stop orders, so the guard covers
floor(qty) and the existing software stop stays as the fallback for the
fractional remainder and for any name with no guard; every software sell first
releases the guard for that symbol (see capitol_copier.place_market_order) and
the reconciler re-arms whatever is left within a minute.

Scope: gated on TWO things — the wallet must have a row in
capitol_copier.HARD_LIMITS AND its strategy file must carry
anchor.broker_stops: true. Low Risk and Photonic have neither.

Everything here goes through capitol_copier's bare BASE_URL / ALPACA_HEADERS
at call time, so wallets.apply() redirects it exactly like an order.

CLI:  python3 broker_stops.py status | widths | plan | arm | disarm [--symbol X]
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import statistics
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

import requests

import capitol_copier as cc
import strategies
import wallets

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "diary", "stops_log.jsonl")

TAG = "pstop"                 # client_order_id head — registered in performance_tracker
ATR_DAYS = 14
DEFAULTS = {"stop_atr_mult": 2.5, "stop_min_pct": 0.06, "stop_max_pct": 0.14}
TRAIL_TOL_PT = 0.5            # ignore width drift smaller than this (percentage points)
MUTATION_COOLDOWN_S = 60      # never touch the same symbol's guard twice inside this
RUN_MIN_INTERVAL_S = 20       # reconcile() is called from a 30 s loop; keep it gentle
RELEASE_WAIT_S = 5.0


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ── gate ─────────────────────────────────────────────────────────────────────

def enabled(wallet: str | None = None) -> bool:
    """True only for a boxed wallet whose strategy file switches guards on."""
    w = wallet or wallets.current()
    if cc.hard_limits_for(w) is None:
        return False
    try:
        a = strategies.load_merged(w).get("anchor") or {}
    except Exception:
        return False
    return bool(a.get("broker_stops", False))


def _cfg(wallet: str | None = None) -> dict:
    try:
        dx = strategies.load_merged(wallet or wallets.current()).get("dynamic_exits") or {}
    except Exception:
        dx = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = _f(dx.get(k), 0)
        if v > 0:
            out[k] = v
    return out


# ── state ────────────────────────────────────────────────────────────────────

def _state_path() -> str:
    return strategies.state_path(".stop_widths.json", wallets.current())


def _load_state() -> dict:
    try:
        with open(_state_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    path = _state_path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".stopw.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(tmp)


def _log(row: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        row = dict(row, ts=dt.datetime.now(ET).isoformat(timespec="seconds"),
                   wallet=wallets.current())
        with open(LOG, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


# ── width ────────────────────────────────────────────────────────────────────

def _session() -> str:
    return dt.datetime.now(ET).date().isoformat()


def _atr_pct(sym: str) -> float | None:
    """14-day ATR as a fraction of the last close, from daily IEX bars."""
    start = (dt.datetime.now(ET) - dt.timedelta(days=45)).date().isoformat()
    try:
        r = requests.get(f"{cc.DATA_URL}/stocks/{sym}/bars", headers=cc.ALPACA_HEADERS,
                         params={"timeframe": "1Day", "start": start, "limit": 60,
                                 "feed": "iex", "adjustment": "split"}, timeout=15)
        bars = r.json().get("bars") or []
    except Exception:
        return None
    if len(bars) < ATR_DAYS + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    close = bars[-1]["c"]
    if not close:
        return None
    return statistics.mean(trs[-ATR_DAYS:]) / close


def width_pct(sym: str, cfg: dict | None = None) -> float:
    """Trailing width for `sym` in PERCENT POINTS (Alpaca's trail_percent unit),
    cached per ET session. No bars → the widest allowed width (fewest false
    stops; the software fallback still guards)."""
    sym = sym.upper()
    cfg = cfg or _cfg()
    st = _load_state()
    if st.get("session") != _session() or st.get("widths_cfg") != cfg:
        # new session, or the width settings were edited in the strategy tab
        st = {"session": _session(), "widths": {}, "widths_cfg": dict(cfg),
              "guards": st.get("guards", {}), "last_mutation": st.get("last_mutation", {})}
    w = st.setdefault("widths", {}).get(sym)
    if w:
        return float(w)
    atr = _atr_pct(sym)
    lo, hi = cfg["stop_min_pct"], cfg["stop_max_pct"]
    raw = hi if atr is None else min(hi, max(lo, cfg["stop_atr_mult"] * atr))
    w = round(raw * 100, 2)
    st["widths"][sym] = w
    _save_state(st)
    return w


# ── orders ───────────────────────────────────────────────────────────────────

def is_guard(o: dict, known_ids: set | None = None) -> bool:
    """A resting guard of ours: a trailing-stop SELL carrying our tag, or one
    that Alpaca created by editing (PATCH) a guard we know — a replacement
    gets a fresh Alpaca-generated client id unless we hand it one, and the
    first live test showed exactly that."""
    if o.get("type") != "trailing_stop" or o.get("side") != "sell":
        return False
    if str(o.get("client_order_id") or "").startswith(TAG + "-"):
        return True
    return bool(known_ids) and (o.get("replaces") in known_ids or o.get("id") in known_ids)


def _known_ids() -> set:
    st = _load_state()
    ids = set()
    for g in (st.get("guards") or {}).values():
        ids.add(g.get("id"))
        ids.update(g.get("history") or [])
    return {i for i in ids if i}


def guard_orders() -> dict[str, list[dict]]:
    """All resting guards, grouped by symbol. ONE request, never per symbol."""
    r = requests.get(f"{cc.BASE_URL}/orders", headers=cc.ALPACA_HEADERS,
                     params={"status": "open", "limit": 200, "direction": "desc"}, timeout=15)
    r.raise_for_status()
    known = _known_ids()
    out: dict[str, list[dict]] = {}
    for o in r.json() or []:
        if is_guard(o, known):
            out.setdefault(o["symbol"].upper(), []).append(o)
    return out


def covered(sym: str, guards: dict | None = None) -> bool:
    try:
        guards = guards if guards is not None else guard_orders()
    except Exception:
        return False
    return bool(guards.get(sym.upper()))


def _client_id() -> str:
    return f"{TAG}-{dt.datetime.now(ET):%Y%m%d}-{uuid.uuid4().hex[:8]}"


def arm(sym: str, qty_whole: int, width: float) -> dict:
    payload = {"symbol": sym.upper(), "side": "sell", "type": "trailing_stop",
               "trail_percent": f"{width:.2f}", "qty": str(int(qty_whole)),
               "time_in_force": "gtc", "client_order_id": _client_id()}
    r = requests.post(f"{cc.BASE_URL}/orders", headers=cc.ALPACA_HEADERS, json=payload, timeout=15)
    return r.json()


def patch(order_id: str, qty: int | None = None, trail: float | None = None) -> dict:
    """Edit a resting guard in place. Alpaca answers with a REPLACEMENT order
    (new id, old one marked 'replaced') and — verified live 2026-09-22 — keeps
    the high-water mark, which is why editing beats cancel-and-re-arm. We hand
    the replacement our tag, or it would come back with a random client id."""
    body: dict = {"client_order_id": _client_id()}
    if qty is not None:
        body["qty"] = str(int(qty))
    if trail is not None:
        body["trail"] = f"{trail:.2f}"
    r = requests.patch(f"{cc.BASE_URL}/orders/{order_id}", headers=cc.ALPACA_HEADERS,
                       json=body, timeout=15)
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text[:200]}


def cancel(order_id: str) -> bool:
    r = requests.delete(f"{cc.BASE_URL}/orders/{order_id}", headers=cc.ALPACA_HEADERS, timeout=10)
    return r.status_code in (200, 204)


def _order_status(order_id: str) -> str:
    try:
        r = requests.get(f"{cc.BASE_URL}/orders/{order_id}", headers=cc.ALPACA_HEADERS, timeout=10)
        return str((r.json() or {}).get("status") or "")
    except Exception:
        return ""


def release(sym: str, wait_s: float = RELEASE_WAIT_S) -> bool:
    """Cancel every guard on `sym` and wait until the shares are free.

    Called by capitol_copier.place_market_order before ANY sell on a boxed
    wallet. Returns True when nothing is resting any more. On timeout the
    caller still attempts its sell — Alpaca then rejects it exactly as it would
    have before, and the guard stays in place, so protection never lapses.
    """
    sym = sym.upper()
    try:
        gs = guard_orders().get(sym, [])
    except Exception as e:
        _log({"sym": sym, "action": "release_failed", "reason": str(e)})
        return False
    if not gs:
        return True
    ids = [g["id"] for g in gs]
    for oid in ids:
        with contextlib.suppress(Exception):
            cancel(oid)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        left = [oid for oid in ids if _order_status(oid) not in
                ("canceled", "filled", "expired", "replaced", "rejected", "")]
        if not left:
            _log({"sym": sym, "action": "released", "orders": ids})
            st = _load_state()
            st.setdefault("last_mutation", {})[sym] = time.time()
            _save_state(st)
            return True
        time.sleep(0.4)
    _log({"sym": sym, "action": "release_timeout", "orders": ids})
    return False


# ── planner (pure) ───────────────────────────────────────────────────────────

def tighten(width: float, price: float, last_stop: float | None) -> float:
    """When re-arming after a pullback, never let the stop price fall below
    where the previous guard already had it. Alpaca starts a new trailing
    stop's high-water mark at the current price; this closes that gap."""
    if not last_stop or not price or last_stop >= price:
        return width
    return round(min(width, (price - last_stop) / price * 100), 2)


def plan(positions: list[dict], guards: dict[str, list[dict]], widths: dict[str, float],
         state: dict, now_ts: float) -> list[dict]:
    """Decide what to do. Pure: no I/O, fully unit-tested.

    One guard per symbol, qty = floor(position qty), trail = width. Small
    width drift (< TRAIL_TOL_PT) is left alone so the reconciler never
    thrashes; a symbol touched in the last MUTATION_COOLDOWN_S is skipped.
    """
    acts: list[dict] = []
    last_mut = state.get("last_mutation") or {}
    last_stop = state.get("last_stop") or {}
    held: set[str] = set()

    for p in positions:
        sym = str(p.get("symbol", "")).upper()
        qty = abs(_f(p.get("qty")))
        if not sym or qty <= 0 or _f(p.get("qty")) < 0:      # shorts are not ours
            continue
        held.add(sym)
        want_qty = int(math.floor(qty))
        gs = list(guards.get(sym, []))
        cooling = (now_ts - _f(last_mut.get(sym))) < MUTATION_COOLDOWN_S

        if want_qty < 1:
            for g in gs:
                acts.append({"sym": sym, "action": "cancel_extra", "order_id": g["id"],
                             "reason": "position under one whole share — software fallback only"})
            continue
        width = float(widths.get(sym) or DEFAULTS["stop_max_pct"] * 100)

        if not gs:
            if cooling:
                acts.append({"sym": sym, "action": "none", "reason": "cooldown"})
                continue
            px = _f(p.get("current_price"))
            acts.append({"sym": sym, "action": "arm", "qty": want_qty,
                         "trail": tighten(width, px, _f(last_stop.get(sym)) or None),
                         "reason": "no guard resting"})
            continue

        gs.sort(key=lambda g: -_f(g.get("hwm")))            # keep the highest-water one
        keep, extras = gs[0], gs[1:]
        for g in extras:
            acts.append({"sym": sym, "action": "cancel_extra", "order_id": g["id"],
                         "reason": "duplicate guard"})
        if cooling:
            acts.append({"sym": sym, "action": "none", "reason": "cooldown"})
            continue
        g_qty = int(_f(keep.get("qty")))
        g_trail = _f(keep.get("trail_percent"))
        if g_qty != want_qty:
            acts.append({"sym": sym, "action": "patch_qty", "order_id": keep["id"],
                         "qty": want_qty, "reason": f"guard {g_qty} sh, position {want_qty} whole"})
        elif abs(g_trail - width) >= TRAIL_TOL_PT:
            acts.append({"sym": sym, "action": "patch_trail", "order_id": keep["id"],
                         "trail": width, "reason": f"width {g_trail:.2f} → {width:.2f}"})
        else:
            acts.append({"sym": sym, "action": "none", "reason": "in place"})

    for sym, gs in guards.items():
        if sym not in held:
            for g in gs:
                acts.append({"sym": sym, "action": "cancel_extra", "order_id": g["id"],
                             "reason": "no position — orphan guard"})
    return acts


# ── apply ────────────────────────────────────────────────────────────────────

def apply(actions: list[dict], dry_run: bool = False) -> list[dict]:
    st = _load_state()
    st.setdefault("last_mutation", {}); st.setdefault("guards", {}); st.setdefault("last_stop", {})
    done = []
    for a in actions:
        act, sym = a["action"], a["sym"]
        if act == "none":
            continue
        row = dict(a, dry_run=dry_run)
        if dry_run:
            print(f"  [DRY] {sym:<6} {act:<12} {a.get('reason','')}")
            done.append(row)
            continue
        try:
            if act == "arm":
                res = arm(sym, a["qty"], a["trail"])
                row["result"] = {k: res.get(k) for k in ("id", "status", "qty", "trail_percent", "hwm")}
                if res.get("id"):
                    st["guards"][sym] = {"id": res["id"], "qty": a["qty"], "trail": a["trail"],
                                         "history": [res["id"]]}
            elif act in ("patch_qty", "patch_trail"):
                res = (patch(a["order_id"], qty=a["qty"]) if act == "patch_qty"
                       else patch(a["order_id"], trail=a["trail"]))
                row["result"] = {k: res.get(k) for k in ("id", "status", "qty", "trail_percent", "hwm", "message")}
                if res.get("id"):
                    g = st["guards"].setdefault(sym, {"history": []})
                    g.setdefault("history", []).append(res["id"])
                    g.update({"id": res["id"], "qty": res.get("qty"), "trail": res.get("trail_percent")})
                else:                                       # PATCH refused → cancel + re-arm
                    cancel(a["order_id"])
                    trail = _f(a.get("trail")) or width_pct(sym)
                    qty_new = int(_f(a.get("qty"))) or int(_f((st["guards"].get(sym) or {}).get("qty")))
                    res2 = arm(sym, qty_new, trail) if qty_new else {}
                    row["fallback"] = {k: res2.get(k) for k in ("id", "status", "hwm")}
                    if res2.get("id"):
                        st["guards"][sym] = {"id": res2["id"], "qty": qty_new, "trail": trail,
                                             "history": [res2["id"]]}
            elif act == "cancel_extra":
                row["result"] = {"cancelled": cancel(a["order_id"])}
            st["last_mutation"][sym] = time.time()
        except Exception as e:
            row["error"] = str(e)
        _log(row)
        done.append(row)
    _save_state(st)
    return done


def _remember_stop_prices(guards: dict[str, list[dict]]) -> None:
    """Keep the current stop price per symbol so a later re-arm can be
    tightened (see tighten())."""
    st = _load_state()
    ls = st.setdefault("last_stop", {})
    for sym, gs in guards.items():
        sp = max((_f(g.get("stop_price")) for g in gs), default=0.0)
        if sp > 0:
            ls[sym] = sp
    _save_state(st)


def reconcile(dry_run: bool = False, force: bool = False) -> list[dict]:
    """Make the broker's resting guards match the positions. Safe to call
    every 30 s; it rate-limits itself and never raises."""
    try:
        if not enabled():
            return []
        st = _load_state()
        if not force and not dry_run and time.time() - _f(st.get("last_run")) < RUN_MIN_INTERVAL_S:
            return []
        positions = [p for p in cc.get_positions() if _f(p.get("qty")) > 0]
        guards = guard_orders()
        _remember_stop_prices(guards)
        cfg = _cfg()
        widths = {str(p["symbol"]).upper(): width_pct(p["symbol"], cfg) for p in positions}
        actions = plan(positions, guards, widths, _load_state(), time.time())
        out = apply(actions, dry_run=dry_run)
        st = _load_state(); st["last_run"] = time.time(); _save_state(st)
        return out
    except Exception as e:
        _log({"action": "reconcile_error", "reason": str(e)})
        print(f"[stops] reconcile error: {e}")
        return []


def disarm(symbol: str | None = None) -> int:
    """Cancel guards — all of them, or one symbol's. The rollback path, and
    the way to run a controlled test. The reconciler re-arms on its next pass
    unless anchor.broker_stops is switched off first."""
    n = 0
    for sym, gs in guard_orders().items():
        if symbol and sym != symbol.upper():
            continue
        for g in gs:
            if cancel(g["id"]):
                n += 1
                _log({"sym": sym, "action": "disarmed", "order_id": g["id"]})
    return n


def status() -> dict:
    positions = [p for p in cc.get_positions() if _f(p.get("qty")) > 0]
    guards = guard_orders()
    rows = []
    for p in positions:
        sym = p["symbol"].upper()
        gs = guards.get(sym, [])
        rows.append({"sym": sym, "qty": _f(p["qty"]), "whole": int(math.floor(_f(p["qty"]))),
                     "guards": [{"id": g["id"][:8], "qty": g.get("qty"), "trail": g.get("trail_percent"),
                                 "hwm": g.get("hwm"), "stop": g.get("stop_price"), "status": g.get("status")}
                                for g in gs],
                     "width": width_pct(sym)})
    return {"enabled": enabled(), "wallet": wallets.current(), "positions": rows,
            "orphans": [s for s in guards if s not in {r["sym"] for r in rows}]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    sym_arg = sys.argv[sys.argv.index("--symbol") + 1] if "--symbol" in sys.argv else None
    if cmd == "status":
        print(json.dumps(status(), indent=1, default=str))
    elif cmd == "widths":
        for p in cc.get_positions():
            print(f"  {p['symbol']:<6} {width_pct(p['symbol']):5.2f}%")
    elif cmd == "plan":
        reconcile(dry_run=True, force=True)
    elif cmd == "arm":
        for r in reconcile(force=True):
            print(json.dumps(r, default=str))
    elif cmd == "disarm":
        print(f"cancelled {disarm(sym_arg)} guard(s)")
    else:
        print(__doc__)
