#!/usr/bin/env python3
"""
wbauto.py — Wanna Buffet's own toolbox for the wallet it trades by itself
=========================================================================
Peter's instruction, 2026-10-02: the Hermes agent "Wanna Buffet" trades the
wallet "Wanna_Buffet Auto Trading" (Alpaca paper account PA3BZK0WN833) on its
own, aims at +10% of starting equity every three months, iterates its own
rules over time and documents everything. No guardrails are imposed from
outside: every rule the code enforces on this wallet is read from the
agent's OWN strategy file, which the agent edits through `tune` below.

Everything the agent does goes through this one file, always run as:

    cd /Users/peter/GitHub/Alpaca_Paper_Trader && venv/bin/python wbauto.py <command> ...

Commands
  scan                 market read, your book, your rules, the target — the
                       scheduled session tick (silent outside market hours)
  status               positions, open orders, cash, rules in force
  buy  SYM --usd N [--stop S] --why "..."   [--dry-run]
  sell SYM [--frac F | --usd N] --why "..." [--dry-run]
  tune show | get KEY | set KEY VALUE --why "..."
       | add-name SYM --why "..." | remove-name SYM --why "..."
  journal "text"       append a dated entry to your journal
  review [--weekly]    the facts for your end-of-day (or Saturday) review
  target [--reset]     progress against the quarterly target

Files it keeps (diary/wbauto/): journal.md, trades.jsonl, changes.jsonl.
Orders are tagged "wbauto" so every fill is attributable to the agent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
WALLET = "Wanna_Buffet Auto Trading"
ALIASES = {"photonic cpo etf", "wanna_buffet auto trading", "wanna buffet auto trading"}
DIARY = os.path.join(HERE, "diary", "wbauto")
JOURNAL = os.path.join(DIARY, "journal.md")
TRADES = os.path.join(DIARY, "trades.jsonl")
CHANGES = os.path.join(DIARY, "changes.jsonl")
QUARTER_DAYS = 91
DEFAULT_TARGET = {"target_pct_per_quarter": 0.10, "quarter_start": None, "baseline_equity": None}


# ── pure helpers (unit-tested) ───────────────────────────────────────────────

def coerce(value: str):
    """'true' → True, '12' → 12, '0.08' → 0.08, '[...]' → list, else the string."""
    v = str(value).strip()
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    # "on"/"off" stay words: several rules use them as mode names (swing.mode = off)
    if low in ("null", "none"):
        return None
    if v.startswith("[") or v.startswith("{"):
        try:
            return json.loads(v)
        except Exception:
            pass
    try:
        if v.lstrip("-").isdigit():
            return int(v)
        return float(v)
    except ValueError:
        return v


def target_progress(baseline: float, target_pct: float, start: dt.date, today: dt.date, equity: float) -> dict:
    """Where the wallet stands against a straight-line path to +target_pct in QUARTER_DAYS."""
    days = max(0, (today - start).days)
    frac = min(1.0, days / QUARTER_DAYS)
    goal = baseline * (1 + target_pct)
    pace = baseline * (1 + target_pct * frac)
    return {"start": start.isoformat(), "day": days, "of": QUARTER_DAYS, "baseline": baseline, "goal": goal,
            "equity": equity, "gain_pct": (equity / baseline - 1) * 100 if baseline else 0.0,
            "needed_pct": (goal / equity - 1) * 100 if equity else 0.0,
            "pace_equity": pace, "vs_pace_pct": (equity / pace - 1) * 100 if pace else 0.0,
            "days_left": max(0, QUARTER_DAYS - days)}


def rules_summary(cfg: dict) -> list[str]:
    a = cfg.get("anchor") or {}; r = cfg.get("risk") or {}; x = cfg.get("dynamic_exits") or {}
    sw = cfg.get("swing") or {}; cp = cfg.get("capitol_copier") or {}
    out = []
    out.append(f"fence {'ON' if a.get('enabled') else 'OFF'}: window {a.get('entry_window_et')} ET, "
               f"{len(a.get('universe') or [])} names on the list, max {a.get('max_open_names') or '∞'} held, "
               f"cash floor {float(a.get('min_cash_pct') or 0)*100:.0f}%, daily-loss halt {a.get('daily_loss_limit_pct')}%, "
               f"cooling-off {a.get('cooling_off_pct')}%/{a.get('cooling_off_days')}d, "
               f"regime filter {'on' if a.get('regime_filter_enabled') else 'off'}, "
               f"{a.get('max_entries_per_name_per_day')} entries/name/day, {a.get('max_entries_per_day') or '∞'}/day")
    out.append(f"caps: {float(r.get('max_position_pct') or 1)*100:.0f}% per name, gross {r.get('max_gross_exposure')}x "
               f"(by regime {r.get('gross_by_regime')})")
    out.append(f"exit engine {'ON' if cp.get('exits_on') else 'OFF'} every {((cfg.get('trading_schedule') or {}).get('manage_every_minutes'))} min: "
               f"stop {float(x.get('stop_loss_pct') or 0)*100:.0f}%, trail after +{float(x.get('trail_trigger_pct') or 0)*100:.0f}% "
               f"giving back {float(x.get('trail_giveback_pct') or 0)*100:.0f}%, profit tiers {x.get('take_profit_levels')}, "
               f"pyramid adds {x.get('pyramid_levels')}, max holdings {x.get('max_holdings')}")
    out.append(f"other engines: swing buyer {'ON' if sw.get('enabled') and (sw.get('mode') or 'trade') == 'trade' else 'off'}, "
               f"copier {'ON' if cp.get('copy_on') else 'off'}")
    return out


def pct(a, b):
    return (a / b - 1) * 100 if a and b else 0.0


# ── wallet binding and data ──────────────────────────────────────────────────

def bind():
    import wallets, strategies
    ok, msg = wallets.apply(WALLET)
    if not ok:
        raise SystemExit(f"cannot bind wallet: {msg}")
    strategies.set_active(WALLET)
    return strategies.load_strategy(WALLET)


def account() -> dict:
    import requests, capitol_copier as cc
    return requests.get(f"{cc.BASE_URL}/account", headers=cc.ALPACA_HEADERS, timeout=10).json()


def clock_open() -> bool | None:
    try:
        import entry_gate as eg
        return bool(eg.fetch_clock().get("is_open"))
    except Exception:
        return None


def positions() -> list[dict]:
    import capitol_copier as cc
    return [p for p in cc.get_positions() if float(p.get("qty") or 0) != 0]


def open_orders() -> list[dict]:
    import requests, capitol_copier as cc
    return requests.get(f"{cc.BASE_URL}/orders", headers=cc.ALPACA_HEADERS, timeout=10,
                        params={"status": "open", "limit": 100}).json() or []


def orders_since(since: str) -> list[dict]:
    import requests, capitol_copier as cc
    out, after = [], f"{since}T00:00:00Z"
    while True:
        r = requests.get(f"{cc.BASE_URL}/orders", headers=cc.ALPACA_HEADERS, timeout=20,
                         params={"status": "closed", "after": after, "limit": 500, "direction": "asc"}).json() or []
        out += [o for o in r if o.get("status") == "filled"]
        if len(r) < 500:
            return out
        after = r[-1]["submitted_at"]


def equity_history(period="3M") -> list[tuple[str, float]]:
    import requests, capitol_copier as cc
    h = requests.get(f"{cc.BASE_URL}/account/portfolio/history", headers=cc.ALPACA_HEADERS, timeout=15,
                     params={"period": period, "timeframe": "1D"}).json() or {}
    return [(dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(ET).date().isoformat(), float(e))
            for t, e in zip(h.get("timestamp") or [], h.get("equity") or []) if e]


def bars_for(symbols: list[str], days: int = 60) -> dict:
    from swing_buyer import fetch_daily_bars
    return fetch_daily_bars(sorted(set(symbols)), days=days)


def stats_from_bars(b: list[dict]) -> dict:
    if len(b) < 2:
        return {}
    c = [x["c"] for x in b]
    hi20 = max(x["h"] for x in b[-20:])
    trs = [max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i-1]["c"]), abs(b[i]["l"] - b[i-1]["c"])) for i in range(1, len(b))]
    atr = statistics.mean(trs[-14:]) / c[-1] * 100 if len(trs) >= 14 and c[-1] else None
    return {"close": c[-1], "d5": pct(c[-1], c[-6]) if len(c) > 6 else 0.0, "d20": pct(c[-1], c[-21]) if len(c) > 21 else 0.0,
            "from_hi20": pct(c[-1], hi20), "atr": atr}


# ── diary ────────────────────────────────────────────────────────────────────

def _append(path: str, row: dict):
    os.makedirs(DIARY, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def _rows(path: str) -> list[dict]:
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except Exception:
        return []


def journal_add(text: str) -> str:
    os.makedirs(DIARY, exist_ok=True)
    stamp = dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    with open(JOURNAL, "a") as f:
        f.write(f"## {stamp}\n{text.strip()}\n\n")
    return stamp


def journal_tail(n_chars: int = 1500) -> str:
    try:
        return open(JOURNAL).read()[-n_chars:]
    except Exception:
        return "(empty)"


# ── target ───────────────────────────────────────────────────────────────────

def target_block(cfg: dict) -> dict:
    t = dict(DEFAULT_TARGET); t.update(cfg.get("wbauto") or {})
    return t


def target_line(cfg: dict, equity: float) -> str:
    t = target_block(cfg)
    if not t.get("quarter_start") or not t.get("baseline_equity"):
        return "TARGET: not started — run `wbauto.py target --reset` to set today as day 1"
    p = target_progress(float(t["baseline_equity"]), float(t["target_pct_per_quarter"]),
                        dt.date.fromisoformat(t["quarter_start"]), dt.datetime.now(ET).date(), equity)
    return (f"TARGET: +{float(t['target_pct_per_quarter'])*100:.0f}% by day {p['of']} (from ${p['baseline']:,.0f} on {p['start']}) — "
            f"day {p['day']}, equity ${p['equity']:,.0f} = {p['gain_pct']:+.1f}%, pace says ${p['pace_equity']:,.0f} "
            f"({p['vs_pace_pct']:+.1f}% vs pace), still need {p['needed_pct']:+.1f}% in {p['days_left']} days")


def cmd_target(reset: bool) -> int:
    cfg = bind()
    if reset:
        eq = float(account().get("equity") or 0)
        import strategies
        def mut(c):
            c["wbauto"] = dict(target_block(c), quarter_start=dt.datetime.now(ET).date().isoformat(), baseline_equity=round(eq, 2))
            return c
        strategies.update_strategy(mut, WALLET)
        _append(CHANGES, {"ts": dt.datetime.now(ET).isoformat(timespec="seconds"), "key": "wbauto.quarter",
                          "old": target_block(cfg), "new": {"quarter_start": dt.datetime.now(ET).date().isoformat(), "baseline_equity": round(eq, 2)},
                          "why": "quarter (re)started", "by": "wbauto"})
        cfg = bind()
    print(target_line(cfg, float(account().get("equity") or 0)))
    return 0


# ── scan / status ────────────────────────────────────────────────────────────

def book_lines(cfg: dict, acct: dict, pos: list[dict], bars: dict | None = None, hist: list | None = None) -> list[str]:
    eq = float(acct.get("equity") or 0); cash = float(acct.get("cash") or 0); last = float(acct.get("last_equity") or 0)
    inv = sum(abs(float(p.get("market_value") or 0)) for p in pos)
    L = [f"BOOK: equity ${eq:,.0f} | today {pct(eq, last):+.2f}% | cash ${cash:,.0f} ({cash/eq*100:.0f}%) | invested {inv/eq*100:.0f}% | {len(pos)} names"]
    if hist:
        peak_d, peak = max(hist, key=lambda x: x[1])
        L[0] += f" | drawdown {(1 - eq/peak)*100:.1f}% from ${peak:,.0f} ({peak_d})"
    trades = _rows(TRADES)
    first_buy = {}
    for t in trades:
        if t.get("side") == "buy" and t.get("order_id"):
            first_buy.setdefault(t["symbol"], t["ts"][:10])
    L.append(f"{'SYM':6s}{'$':>9s}{'%eq':>6s}{'entry':>9s}{'P&L%':>7s}{'5d%':>7s}{'off20h%':>8s}{'ATR%':>6s}  since   stop(you)")
    stops = {t["symbol"]: t.get("stop") for t in trades if t.get("side") == "buy" and t.get("stop")}
    for p in sorted(pos, key=lambda p: -abs(float(p.get("market_value") or 0))):
        s = p["symbol"]; mv = abs(float(p.get("market_value") or 0)); st = stats_from_bars((bars or {}).get(s) or [])
        L.append(f"{s:6s}{mv:>9,.0f}{mv/eq*100:>6.1f}{float(p.get('avg_entry_price') or 0):>9.2f}"
                 f"{float(p.get('unrealized_plpc') or 0)*100:>7.1f}{st.get('d5', 0):>7.1f}{st.get('from_hi20', 0):>8.1f}"
                 f"{(st.get('atr') or 0):>6.1f}  {first_buy.get(s, '?'):10s} {stops.get(s, '-')}")
    return L


def universe_lines(cfg: dict, pos: list[dict], bars: dict) -> list[str]:
    held = {p["symbol"] for p in pos}
    uni = [u.upper() for u in ((cfg.get("anchor") or {}).get("universe") or [])]
    spy = stats_from_bars(bars.get("SPY") or [])
    rows = []
    for s in uni:
        if s in held:
            continue
        st = stats_from_bars(bars.get(s) or [])
        if not st:
            continue
        rows.append((s, st, st["d20"] - spy.get("d20", 0)))
    rows.sort(key=lambda r: -r[2])
    L = [f"NOT HELD, ranked by 20-day strength vs SPY (SPY 20d {spy.get('d20', 0):+.1f}%):",
         f"{'SYM':6s}{'close':>9s}{'5d%':>7s}{'20d%':>7s}{'RS20':>7s}{'off20h%':>8s}{'ATR%':>6s}"]
    for s, st, rs in rows:
        L.append(f"{s:6s}{st['close']:>9.2f}{st['d5']:>7.1f}{st['d20']:>7.1f}{rs:>7.1f}{st['from_hi20']:>8.1f}{(st['atr'] or 0):>6.1f}")
    return L


def fills_today_lines(session: str) -> list[str]:
    import performance_tracker as pt
    rows = [o for o in orders_since(session) if str(o.get("filled_at", ""))[:10] == session]
    if not rows:
        return ["FILLS TODAY: none"]
    return ["FILLS TODAY:"] + [f"  {str(o['filled_at'])[11:16]}Z {o['symbol']:5s} {o['side']:4s} {float(o.get('filled_qty') or 0):.4g} @ "
                               f"{float(o.get('filled_avg_price') or 0):.2f}  by {pt.source_of(o)}" for o in rows]


def changes_lines(days: int = 7) -> list[str]:
    since = (dt.datetime.now(ET) - dt.timedelta(days=days)).isoformat()
    rows = [r for r in _rows(CHANGES) if r.get("ts", "") >= since]
    if not rows:
        return [f"RULE CHANGES (last {days} days): none"]
    return [f"RULE CHANGES (last {days} days):"] + [f"  {r['ts'][:16]} {r['key']}: {r.get('old')} → {r.get('new')} — {r.get('why', '')[:100]}" for r in rows[-8:]]


def cmd_scan(force: bool) -> int:
    now = dt.datetime.now(ET)
    is_open = clock_open()
    if not force and not is_open:
        return 0                                         # silent: the scheduler does not wake the agent
    cfg = bind()
    acct = account(); pos = positions()
    uni = [u.upper() for u in ((cfg.get("anchor") or {}).get("universe") or [])]
    bars = bars_for(uni + [p["symbol"] for p in pos] + ["SPY"], days=60)
    try:
        import market_context as mc
        r = mc.regime_state(); regime = f"{r.get('state')} (SPY {r.get('vs50', 0):+.1f}% vs 50d, {r.get('vs200', 0):+.1f}% vs 200d)"
    except Exception:
        regime = "unknown"
    L = [f"WANNA BUFFET SCAN  {now:%a %Y-%m-%d %H:%M} ET  market {'OPEN' if is_open else 'closed'}  regime {regime}",
         target_line(cfg, float(acct.get("equity") or 0)), ""]
    L += book_lines(cfg, acct, pos, bars, equity_history()) + [""]
    L += universe_lines(cfg, pos, bars) + [""]
    L += fills_today_lines(now.date().isoformat()) + [""]
    L += ["YOUR RULES IN FORCE:"] + [f"  {x}" for x in rules_summary(cfg)] + [""]
    L += changes_lines() + [""]
    oo = open_orders()
    if oo:
        L += ["OPEN ORDERS: " + ", ".join(f"{o['symbol']} {o['side']} {o.get('type')} {o.get('qty') or o.get('notional')}" for o in oo), ""]
    L.append("Decide: buy, sell, or hold. Your tools: wbauto.py buy/sell/tune/journal. One line of reasoning per action.")
    print("\n".join(L))
    return 0


def cmd_status() -> int:
    cfg = bind(); acct = account(); pos = positions()
    L = [target_line(cfg, float(acct.get("equity") or 0))]
    L += book_lines(cfg, acct, pos, bars_for([p["symbol"] for p in pos], days=40), equity_history())
    oo = open_orders()
    L.append("OPEN ORDERS: " + (", ".join(f"{o['symbol']} {o['side']} {o.get('type')} {o.get('qty') or o.get('notional')}" for o in oo) or "none"))
    L += ["RULES:"] + [f"  {x}" for x in rules_summary(cfg)]
    print("\n".join(L))
    return 0


# ── trading ──────────────────────────────────────────────────────────────────

def cmd_buy(sym: str, usd: float, stop: float | None, why: str, dry_run: bool) -> int:
    import capitol_copier as cc
    bind(); sym = sym.upper()
    if usd <= 0:
        print("ERROR: --usd must be positive"); return 3
    plan = f"BUY {sym} ${usd:,.0f}" + (f" with your stop at {stop:.2f}" if stop else " (no stop given)") + f" — {why}"
    if dry_run:
        print("DRY RUN — would " + plan); return 0
    res = cc.place_market_order(sym, "buy", notional=round(usd, 2))
    if isinstance(res, dict) and res.get("blocked_by_cap"):
        print(f"BLOCKED by your own rule — {res.get('reason')}"); return 2
    oid = (res or {}).get("id") if isinstance(res, dict) else None
    if not oid:
        print(f"ERROR: order not accepted: {res}"); return 3
    _append(TRADES, {"ts": dt.datetime.now(ET).isoformat(timespec="seconds"), "side": "buy", "symbol": sym, "usd": round(usd, 2),
                     "stop": stop, "why": why[:300], "order_id": oid})
    print(f"{plan}\norder {oid} sent through the fence.")
    return 0


def cmd_sell(sym: str, frac: float | None, usd: float | None, why: str, dry_run: bool) -> int:
    import capitol_copier as cc
    bind(); sym = sym.upper()
    pos = next((p for p in positions() if p["symbol"] == sym), None)
    if not pos:
        print(f"nothing to sell: no position in {sym}"); return 2
    avail = abs(float(pos.get("qty_available") or pos.get("qty") or 0))
    px = float(pos.get("current_price") or 0)
    if usd:
        qty = min(avail, round(usd / px, 4)) if px else 0
    else:
        qty = round(avail * max(0.0, min(1.0, frac if frac is not None else 1.0)), 4)
    if qty <= 0:
        print(f"nothing to sell: {sym} has no available shares (open order pending?)"); return 2
    plan = f"SELL {sym} {qty:g} sh (~${qty*px:,.0f}) — {why}"
    if dry_run:
        print("DRY RUN — would " + plan); return 0
    res = cc.place_market_order(sym, "sell", qty=qty)
    oid = (res or {}).get("id") if isinstance(res, dict) else None
    if not oid:
        print(f"ERROR: sell not accepted: {res}"); return 3
    _append(TRADES, {"ts": dt.datetime.now(ET).isoformat(timespec="seconds"), "side": "sell", "symbol": sym, "qty": qty,
                     "usd": round(qty * px, 2), "why": why[:300], "order_id": oid})
    print(f"{plan}\norder {oid} sent.")
    return 0


# ── tuning your own rules ────────────────────────────────────────────────────

def cmd_tune(args) -> int:
    import strategies, config_fields as cf
    cfg = bind()
    if args.op == "show":
        for k in ("anchor", "risk", "dynamic_exits", "swing", "capitol_copier", "trading_schedule", "wbauto"):
            print(f"{k} = {json.dumps(cfg.get(k), default=str)}")
        return 0
    if args.op == "get":
        print(f"{args.key} = {json.dumps(cf.get_path(cfg, args.key), default=str)}"); return 0
    why = (args.why or "").strip()
    if not why:
        print("ERROR: --why is required — write down the reason before you change a rule"); return 3
    stamp = dt.datetime.now(ET).isoformat(timespec="seconds")
    if args.op in ("add-name", "remove-name"):
        sym = args.key.upper()
        old = list((cfg.get("anchor") or {}).get("universe") or [])
        new = [s for s in old if s != sym] if args.op == "remove-name" else (old + [sym] if sym not in old else old)
        key, old_v, new_v = "anchor.universe", old, new
    else:
        key = args.key; old_v = cf.get_path(cfg, key); new_v = coerce(args.value)
    def mut(c):
        cf.set_path(c, key, new_v)
        c["last_updated"] = stamp[:10]; c["updated_by"] = f"wanna_buffet: {why[:120]}"
        return c
    strategies.update_strategy(mut, WALLET)
    _append(CHANGES, {"ts": stamp, "key": key, "old": old_v, "new": new_v, "why": why[:300], "by": "wbauto"})
    print(f"{key}: {json.dumps(old_v, default=str)} → {json.dumps(new_v, default=str)}  (logged: {why[:80]})")
    return 0


# ── review ───────────────────────────────────────────────────────────────────

def cmd_review(weekly: bool) -> int:
    import performance_tracker as pt
    cfg = bind(); acct = account(); pos = positions()
    now = dt.datetime.now(ET); session = now.date().isoformat()
    eq = float(acct.get("equity") or 0)
    hist = equity_history("3M")
    span_days = 7 if weekly else 1
    since = (now - dt.timedelta(days=90)).date().isoformat()
    orders = orders_since(since)
    closed = pt.pair_trades(orders)
    def in_span(t):
        d = str(t.get("exit_date") or "")[:10]
        return d >= (now - dt.timedelta(days=span_days)).date().isoformat()
    span = [t for t in closed if in_span(t)]
    gp = sum(t["pnl_usd"] for t in span if t["pnl_usd"] > 0); gl = -sum(t["pnl_usd"] for t in span if t["pnl_usd"] <= 0)
    L = [f"WANNA BUFFET {'WEEKLY' if weekly else 'DAILY'} REVIEW — {session} (facts; the judgement is yours)", target_line(cfg, eq)]
    L += book_lines(cfg, acct, pos, bars_for([p["symbol"] for p in pos], days=40), hist)
    if hist and len(hist) >= 2:
        d1 = pct(hist[-1][1], hist[-2][1]); L.append(f"SESSION P&L: {d1:+.2f}%" + (f" | week {pct(hist[-1][1], hist[-6][1]):+.2f}%" if weekly and len(hist) >= 6 else ""))
    t = target_block(cfg)
    if t.get("quarter_start"):
        try:
            from swing_buyer import fetch_daily_bars
            b = fetch_daily_bars(["QQQ", "SPY"], days=(now.date() - dt.date.fromisoformat(t["quarter_start"])).days + 10)
            def bench(sym):
                x = [z for z in b.get(sym) or [] if z["t"][:10] >= t["quarter_start"]]; return pct(x[-1]["c"], x[0]["c"]) if len(x) > 1 else 0.0
            L.append(f"SINCE QUARTER START: you {pct(eq, float(t['baseline_equity'])):+.1f}% | QQQ {bench('QQQ'):+.1f}% | SPY {bench('SPY'):+.1f}%")
        except Exception:
            pass
    L.append(f"ROUND TRIPS CLOSED ({'this week' if weekly else 'today'}): {len(span)} | wins {sum(1 for x in span if x['pnl_usd'] > 0)} | "
             f"P&L {sum(x['pnl_usd'] for x in span):+,.0f} | profit factor {(gp/gl) if gl else (float('inf') if gp else 0):.2f}")
    for x in sorted(span, key=lambda x: x["pnl_usd"])[:3] + sorted(span, key=lambda x: -x["pnl_usd"])[:3]:
        L.append(f"  {x['symbol']:5s} opened by {x['strategy']:7s} {x['entry_date'][:10]} → {x['exit_date'][:10]}  {x['return_pct']*100:+.1f}%  {x['pnl_usd']:+,.0f}")
    L += fills_today_lines(session)
    refusals = [r for r in _rows(os.path.join(HERE, "diary", "guardrail_log.jsonl"))
                if r.get("wallet") == WALLET and r.get("ts", "")[:10] >= (now - dt.timedelta(days=span_days)).date().isoformat()]
    L.append(f"YOUR OWN RULES REFUSED {len(refusals)} order(s)" + (": " + "; ".join(f"{r['symbol']} {r['cap']}" for r in refusals[-6:]) if refusals else ""))
    L += changes_lines(7 if not weekly else 30)
    mine = [x for x in _rows(TRADES) if x.get("ts", "") >= (now - dt.timedelta(days=span_days)).isoformat()]
    L.append(f"YOUR TRADES LOGGED ({len(mine)}):" + ("" if mine else " none"))
    for x in mine[-10:]:
        L.append(f"  {x['ts'][5:16]} {x['side']} {x['symbol']} ${float(x.get('usd') or 0):,.0f} — {x.get('why', '')[:90]}")
    L += ["YOUR LAST JOURNAL ENTRY:", journal_tail(900), "",
          "Now: (1) what worked and what did not, with the numbers above; (2) are you on pace for the target, and what would change that;",
          "(3) at most ONE rule change, as a hypothesis with the metric and the horizon that will judge it, applied with `wbauto.py tune set ... --why`;",
          "(4) write the entry with `wbauto.py journal \"...\"`; (5) end with a 100-word summary for Peter."]
    print("\n".join(L))
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--force", action="store_true", help="print even when the market is closed")
    sub.add_parser("status")
    b = sub.add_parser("buy"); b.add_argument("symbol"); b.add_argument("--usd", type=float, required=True)
    b.add_argument("--stop", type=float, default=None); b.add_argument("--why", required=True); b.add_argument("--dry-run", action="store_true")
    se = sub.add_parser("sell"); se.add_argument("symbol"); se.add_argument("--frac", type=float, default=None)
    se.add_argument("--usd", type=float, default=None); se.add_argument("--why", required=True); se.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("tune"); t.add_argument("op", choices=["show", "get", "set", "add-name", "remove-name"])
    t.add_argument("key", nargs="?"); t.add_argument("value", nargs="?"); t.add_argument("--why", default="")
    j = sub.add_parser("journal"); j.add_argument("text")
    r = sub.add_parser("review"); r.add_argument("--weekly", action="store_true")
    g = sub.add_parser("target"); g.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    if a.cmd == "scan":
        return cmd_scan(a.force)
    if a.cmd == "status":
        return cmd_status()
    if a.cmd == "buy":
        return cmd_buy(a.symbol, a.usd, a.stop, a.why, a.dry_run)
    if a.cmd == "sell":
        return cmd_sell(a.symbol, a.frac, a.usd, a.why, a.dry_run)
    if a.cmd == "tune":
        if a.op in ("get", "set", "add-name", "remove-name") and not a.key:
            print("ERROR: a key (or symbol) is required"); return 3
        if a.op == "set" and a.value is None:
            print("ERROR: a value is required"); return 3
        return cmd_tune(a)
    if a.cmd == "journal":
        print("journal entry written at " + journal_add(a.text)); return 0
    if a.cmd == "review":
        return cmd_review(a.weekly)
    if a.cmd == "target":
        return cmd_target(a.reset)
    return 1


if __name__ == "__main__":
    sys.exit(main())
