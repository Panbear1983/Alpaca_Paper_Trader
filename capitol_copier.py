"""
Capitol Trades Copier — pool-aware version
==============================================
Iterates over all politicians in the active pool (managed by pool_manager.py and
selected by politician_vetter.py), scrapes each one's latest disclosed trades,
and copies new buys to Alpaca with per-politician position sizing.

Sizing formula:
  trade_size = daily_budget × pool_weight × consensus_multiplier
  (clamped to [min_position_usd, max_position_usd])

State tracking:
  .copied_trades.json   stores tx_ids that have been copied (dedup across all pool members)

Modes:
  python3 capitol_copier.py              normal trickle-copy run
  python3 capitol_copier.py --rebalance  one-time reallocation toward --target-pct
                                          of equity, spread across each pool member's
                                          most recent distinct disclosed buys
"""

import os, json, re, requests
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv

import pool_manager
from sectors import sector_of
try:
    import sentiment_check
    import telegram_notifier as tg
    SENTIMENT_AVAILABLE = True
except ImportError:
    SENTIMENT_AVAILABLE = False
    tg = None

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = "https://data.alpaca.markets/v2"

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type":        "application/json",
}

CT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept":     "text/x-component",
    "RSC":        "1",
}

STATE_FILE     = os.path.join(os.path.dirname(__file__), ".copied_trades.json")
CONFIG_FILE    = os.path.join(os.path.dirname(__file__), "strategy_config.json")
POS_STATE_FILE = os.path.join(os.path.dirname(__file__), ".position_state.json")

# Symbols never managed by Capitol Copier (legacy / test holdings)
NON_CC_SYMBOLS = ("TSLA", "AAPL")


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "copied":  [],
        "last_check": None,
        "stats":   {"total_buys": 0, "total_sells": 0},
        "by_politician": {},
    }


def save_state(state):
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Capitol Trades scraper ───────────────────────────────────────────────────

def fetch_politician_trades(pol_id, per_page=96):
    """Scrape latest trades for a politician from capitoltrades.com RSC stream."""
    url = (f"https://www.capitoltrades.com/politicians/{pol_id}"
           f"?per_page={per_page}&sort=-reportedAt")
    try:
        r = requests.get(url, headers={**CT_HEADERS, "Next-Url": f"/politicians/{pol_id}"},
                         verify=False, timeout=20)
    except Exception as e:
        print(f"  [SCRAPER {pol_id}] Fetch error: {e}")
        return []

    c = r.text
    ticker_list  = re.findall(r'"issuerTicker":"([^"]+)"', c)
    issuer_list  = re.findall(r'"issuerName":"([^"]+)"',   c)
    txid_list    = re.findall(r'"_txId":(\d+)',            c)
    txtype_list  = re.findall(r'"txType":"(buy|sell)"',    c)
    txdate_list  = re.findall(r'"txDate":"(\d{4}-\d{2}-\d{2})"', c)
    pubdate_list = re.findall(r'"pubDate":"(\d{4}-\d{2}-\d{2})', c)
    value_list   = re.findall(r'"value":(\d+)',            c)
    owner_list   = re.findall(r'"owner":"([^"]*)"',        c)
    gap_list     = re.findall(r'"reportingGap":(\d+)',     c)

    n = min(len(ticker_list), len(txtype_list), len(txdate_list))
    trades = []
    for i in range(n):
        raw = ticker_list[i]
        ticker = raw.split(":")[0] if ":" in raw else raw
        if not ticker or ticker in ("null", ""):
            continue
        trades.append({
            "tx_id":         txid_list[i] if i < len(txid_list) else "",
            "ticker":        ticker,
            "issuer":        issuer_list[i] if i < len(issuer_list) else "",
            "tx_type":       txtype_list[i],
            "tx_date":       txdate_list[i],
            "pub_date":      pubdate_list[i] if i < len(pubdate_list) else "",
            "value":         int(value_list[i]) if i < len(value_list) else 0,
            "owner":         owner_list[i] if i < len(owner_list) else "",
            "gap_days":      int(gap_list[i]) if i < len(gap_list) else 0,
            "politician_id": pol_id,  # track source
        })

    return trades


# ── Alpaca helpers ───────────────────────────────────────────────────────────

def place_market_order(ticker, side, notional=None, qty=None):
    payload = {
        "symbol":        ticker,
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    }
    if notional:
        payload["notional"] = str(round(notional, 2))
    elif qty:
        payload["qty"] = str(qty)
    r = requests.post(f"{BASE_URL}/orders", headers=ALPACA_HEADERS, json=payload)
    return r.json()


def get_position(ticker):
    r = requests.get(f"{BASE_URL}/positions/{ticker}", headers=ALPACA_HEADERS)
    return r.json() if r.status_code == 200 else None


def get_account_equity():
    r = requests.get(f"{BASE_URL}/account", headers=ALPACA_HEADERS)
    if r.status_code != 200:
        return None
    return float(r.json().get("equity", 0))


def get_positions():
    """All open Alpaca positions (raw dicts). Empty list on error."""
    r = requests.get(f"{BASE_URL}/positions", headers=ALPACA_HEADERS)
    if r.status_code != 200:
        return []
    return r.json() or []


def get_capitol_exposure():
    """Sum of market value across positions tagged as Capitol Copier
    (everything except the legacy/test holdings in NON_CC_SYMBOLS)."""
    total = 0
    for p in get_positions():
        if p["symbol"] in NON_CC_SYMBOLS:
            continue
        total += abs(float(p.get("market_value", 0)))
    return total


# ── Dynamic position management (stop-loss / trail / take-profit / pyramid) ──

def load_pos_state():
    if os.path.exists(POS_STATE_FILE):
        with open(POS_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_pos_state(pstate):
    with open(POS_STATE_FILE, "w") as f:
        json.dump(pstate, f, indent=2)


def manage_open_positions(cfg, dry_run=False):
    """Active management for every Capitol Copier position, evaluated each run.

    Per position, in priority order (at most one action per position per run):
      1. Stop-loss   — unrealized loss <= -stop_loss_pct  → sell all.
      2. Trail stop  — once peak gain >= trail_trigger_pct, if price falls
                       trail_giveback_pct below the peak → sell all.
      3. Take-profit — at each gain tier in take_profit_levels, sell that
                       fraction of the remaining qty (one tier per run).
      4. Pyramid     — at each gain tier in pyramid_levels, add
                       pyramid_add_frac x original size, if the exposure cap allows.

    Peak price, pyramid count and take-profit stage are persisted in
    .position_state.json so tiers fire only once.
    """
    dx = cfg.get("dynamic_exits", {})
    if not dx.get("enabled", False):
        return

    stop_loss      = dx.get("stop_loss_pct", 0.08)
    trail_trigger  = dx.get("trail_trigger_pct", 0.15)
    trail_giveback = dx.get("trail_giveback_pct", 0.08)
    tp_levels      = dx.get("take_profit_levels", [])
    pyr_levels     = dx.get("pyramid_levels", [])
    pyr_frac       = dx.get("pyramid_add_frac", 0.5)
    prune_off      = dx.get("prune_off_target", False)
    max_hold       = dx.get("max_holdings", 0)

    positions = [p for p in get_positions() if p["symbol"] not in NON_CC_SYMBOLS]
    if not positions:
        print("  [manage] no Capitol Copier positions to manage.")
        return

    pstate   = load_pos_state()
    equity   = get_account_equity() or 100000
    exposure = get_capitol_exposure()
    max_exp  = equity * cfg["pool"]["max_total_exposure_pct"]
    tag      = "[DRY] " if dry_run else ""
    actions  = 0
    acts     = []   # collect all actions, push ONE consolidated message at end

    # prune state for positions we no longer hold
    held = {p["symbol"] for p in positions}
    for sym in list(pstate.keys()):
        if sym not in held:
            pstate.pop(sym, None)

    def _liquidate(p, reason):
        nonlocal actions
        sym = p["symbol"]
        qty = abs(float(p.get("qty", 0)))
        mv  = abs(float(p.get("market_value", 0)))
        print(f"  {tag}✗ {reason} {sym}  → sell all {qty:g} (${mv:,.0f})")
        acts.append(f"🔴 SELL `{sym}` ${mv:,.0f} ({reason})")
        if not dry_run and qty > 0:
            place_market_order(sym, "sell", qty=qty)
        pstate.pop(sym, None)
        actions += 1

    # ── Consolidation pass 1: sweep off-target-sector holdings ──────────────
    # The whitelist blocks new off-target buys; this sheds the legacy ones so
    # the book collapses to the target sectors instead of letting them ride.
    if prune_off:
        survivors = []
        for p in positions:
            if is_eligible_ticker(p["symbol"], cfg):
                survivors.append(p)
            else:
                _liquidate(p, "PRUNE-OFFTARGET")
        positions = survivors

    # ── Consolidation pass 2: hard cap on number of holdings ────────────────
    # Keep the largest `max_hold` on-target positions; sell the long tail.
    if max_hold and len(positions) > max_hold:
        positions.sort(key=lambda p: abs(float(p.get("market_value", 0))), reverse=True)
        for p in positions[max_hold:]:
            _liquidate(p, f"CAP-TAIL(>{max_hold})")
        positions = positions[:max_hold]

    for p in positions:
        sym   = p["symbol"]
        qty   = abs(float(p.get("qty", 0)))
        cur   = float(p.get("current_price", 0) or 0)
        entry = float(p.get("avg_entry_price", 0) or 0)
        plpc  = float(p.get("unrealized_plpc", 0) or 0)   # decimal, +0.12 = +12%
        cost  = abs(float(p.get("cost_basis", 0) or 0))
        if cur <= 0 or entry <= 0 or qty <= 0:
            continue

        st = pstate.setdefault(sym, {
            "entry_price": entry,
            "peak_price":  cur,
            "adds_done":   0,
            "tp_stage":    0,
            "orig_size":   cost or abs(float(p.get("market_value", 0))),
        })
        st["peak_price"] = max(float(st.get("peak_price", cur)), cur)
        peak      = st["peak_price"]
        peak_gain = (peak - entry) / entry if entry else 0.0

        # 1. Stop-loss
        if plpc <= -stop_loss:
            print(f"  {tag}✗ STOP-LOSS {sym}  {plpc*100:+.1f}% <= -{stop_loss*100:.0f}%  → sell all {qty:g}")
            acts.append(f"🛑 STOP `{sym}` {plpc*100:+.1f}%")
            if not dry_run:
                place_market_order(sym, "sell", qty=qty)
            pstate.pop(sym, None)
            actions += 1
            continue

        # 2. Trailing stop
        if peak_gain >= trail_trigger and cur <= peak * (1 - trail_giveback):
            print(f"  {tag}✗ TRAIL-STOP {sym}  peak +{peak_gain*100:.0f}%, now {plpc*100:+.1f}% "
                  f"({(cur/peak-1)*100:+.1f}% off peak)  → sell all {qty:g}")
            acts.append(f"📉 TRAIL `{sym}` +{peak_gain*100:.0f}%→{plpc*100:+.1f}%")
            if not dry_run:
                place_market_order(sym, "sell", qty=qty)
            pstate.pop(sym, None)
            actions += 1
            continue

        # 3. Take-profit (one tier per run)
        sold_tp = False
        for idx, level in enumerate(tp_levels):
            thr, frac = level[0], level[1]
            if st["tp_stage"] <= idx and plpc >= thr:
                sell_qty = round(qty * frac, 4)
                if sell_qty <= 0:
                    continue
                print(f"  {tag}↓ TAKE-PROFIT {sym}  +{plpc*100:.0f}% >= +{thr*100:.0f}%  "
                      f"→ trim {frac*100:.0f}% ({sell_qty:g} sh)")
                acts.append(f"💰 TAKE-PROFIT `{sym}` +{plpc*100:.0f}% trim {frac*100:.0f}%")
                if not dry_run:
                    place_market_order(sym, "sell", qty=sell_qty)
                st["tp_stage"] = idx + 1
                actions += 1
                sold_tp = True
                break
        if sold_tp:
            continue

        # 4. Pyramid into winners (one tier per run, respect exposure cap).
        #    Only compound ON-TARGET names — never add to off-sector legacy
        #    holdings the new regime wouldn't buy (let those bleed down via
        #    take-profits / stops instead).
        if not is_eligible_ticker(sym, cfg):
            continue
        for idx, thr in enumerate(pyr_levels):
            if st["adds_done"] <= idx and plpc >= thr:
                add_size = float(st["orig_size"]) * pyr_frac
                if exposure + add_size > max_exp:
                    print(f"  {tag}• PYRAMID {sym} skipped — would breach exposure cap "
                          f"(${exposure:.0f}+${add_size:.0f} > ${max_exp:.0f})")
                    st["adds_done"] = idx + 1   # don't retry this tier forever
                    break
                print(f"  {tag}↑ PYRAMID {sym}  +{plpc*100:.0f}% >= +{thr*100:.0f}%  "
                      f"→ add ${add_size:.0f}")
                acts.append(f"🟢 PYRAMID `{sym}` +${add_size:,.0f}")
                if not dry_run:
                    place_market_order(sym, "buy", notional=add_size)
                st["adds_done"] = idx + 1
                exposure += add_size
                actions += 1
                break

    if actions == 0:
        print("  [manage] no exit/pyramid triggers fired this run.")
    # ── ONE consolidated push for the whole management cycle ─────────────────
    if acts and tg and not dry_run:
        tg.notify_batch("Capitol position management", acts, emoji="🏛")
    if not dry_run:
        save_pos_state(pstate)


# ── Copy logic ───────────────────────────────────────────────────────────────

def is_eligible_ticker(ticker, cfg=None):
    if not ticker or "/" in ticker or len(ticker) > 5:
        return False
    if ticker in ("XSP", "SPX", "VIX", "NDX"):  # known index symbols
        return False
    if ticker == "TSLA":  # TSLA Ladder strategy retired; never re-enter via Capitol Copier
        return False
    if any(c.isdigit() for c in ticker):  # foreign listings, e.g. Bovespa "AZZA3"
        return False
    if len(ticker) == 5 and ticker.endswith("X"):  # 5-letter mutual/money-market funds, e.g. "VMFXX"
        return False
    # Sector-concentration whitelist: only copy names in the target sectors.
    # Tickers with an unknown sector are skipped (keeps the book concentrated).
    if cfg is None:
        cfg = load_config()
    target = cfg["capitol_copier"].get("target_sectors")
    if target:
        if sector_of(ticker) not in target:
            return False
    return True


def copy_trade(trade, all_pool_buys, state, cfg):
    ticker        = trade["ticker"]
    tx_type       = trade["tx_type"]
    tx_id         = trade["tx_id"]
    politician_id = trade["politician_id"]

    if tx_id in state["copied"]:
        return False, "already copied", 0

    if not is_eligible_ticker(ticker, cfg):
        return False, f"off-target/non-standard ticker {ticker}", 0

    # SELL logic: only sell if we hold a position
    if tx_type == "sell":
        pos = get_position(ticker)
        if not pos or "symbol" not in pos:
            return False, f"no position in {ticker} to sell", 0
        qty = pos.get("qty", "0")
        result = place_market_order(ticker, "sell", qty=qty)
        order_id = result.get("id", "")
        status   = result.get("status", result.get("message", "unknown"))
        if order_id:
            state["copied"].append(tx_id)
            state["stats"]["total_sells"] += 1
            state.setdefault("by_politician", {}).setdefault(politician_id, {"buys":0,"sells":0})["sells"] += 1
            return True, f"sell {order_id[:8]} qty={qty}", 0
        return False, f"sell failed: {status}", 0

    # BUY logic: size by pool weight × consensus boost × sentiment multiplier
    consensus = pool_manager.detect_consensus(ticker, all_pool_buys)
    boost     = consensus["multiplier"]

    # Sentiment overlay (Hermes/gemma local LLM)
    sentiment = None
    sentiment_mult = 1.0
    if SENTIMENT_AVAILABLE:
        try:
            sentiment = sentiment_check.get_sentiment(ticker)
            sentiment_mult = sentiment.get("multiplier", 1.0)
        except Exception as e:
            print(f"  [sentiment] {ticker} check failed: {e}")

    combined_mult = boost * sentiment_mult
    size = pool_manager.get_trade_size(politician_id, sector_multiplier=combined_mult)

    if size <= 0:
        return False, "not in active pool", 0

    # Hard skip if sentiment is very bearish (score 1) — only when the veto is
    # enabled. The aggressive regime disables it (sentiment still scales size).
    if cfg["capitol_copier"].get("sentiment_veto_enabled", True):
        if sentiment and sentiment.get("score") == 1:
            return False, f"sentiment veto (1/5: {sentiment.get('flag','')})", 0

    # Exposure cap check
    equity = get_account_equity() or 100000
    exposure = get_capitol_exposure()
    max_exposure = equity * cfg["pool"]["max_total_exposure_pct"]
    if exposure + size > max_exposure:
        return False, f"would breach {cfg['pool']['max_total_exposure_pct']*100:.0f}% exposure cap (${exposure:.0f}/${max_exposure:.0f})", 0

    result = place_market_order(ticker, "buy", notional=size)
    order_id = result.get("id", "")
    status   = result.get("status", result.get("message", "unknown"))

    if order_id:
        state["copied"].append(tx_id)
        state["stats"]["total_buys"] += 1
        state.setdefault("by_politician", {}).setdefault(politician_id, {"buys":0,"sells":0})["buys"] += 1
        consensus_note = f" [CONSENSUS x{boost} from {consensus['n_members']} members]" if consensus["is_consensus"] else ""
        sentiment_note = ""
        if sentiment:
            sentiment_note = f" [sentiment {sentiment['score']}/5 x{sentiment_mult:.2f}]"
            if sentiment.get("flag"):
                sentiment_note += f" ⚠ {sentiment['flag']}"
        return True, f"buy {order_id[:8]} ${size:.0f}{consensus_note}{sentiment_note}", size
    return False, f"buy failed: {status}", 0


# ── Main ─────────────────────────────────────────────────────────────────────

def run(dry_run=False):
    import urllib3
    urllib3.disable_warnings()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()
    cfg   = load_config()

    # 1. Active management of existing positions runs every tick, before any new
    #    copying — and regardless of pool state. In dry-run we ONLY preview this
    #    (no orders, no new copies).
    print(f"[{now}] Capitol Copier — dynamic position management"
          f"{' (DRY RUN)' if dry_run else ''}")
    manage_open_positions(cfg, dry_run=dry_run)
    print()

    if dry_run:
        print("  DRY RUN — skipping new-trade copy loop.")
        return

    pool = pool_manager.get_pool()
    if not pool:
        print(f"[{now}] Capitol Copier — POOL IS EMPTY")
        print("  Run `python3 politician_vetter.py` to populate the pool first.")
        return

    print(f"[{now}] Capitol Copier — pool-aware scan")
    print(f"  Active pool: {len(pool)} members  |  daily budget: ${cfg['pool']['daily_budget_usd']}")
    for p in pool:
        size = pool_manager.get_trade_size(p["politician_id"])
        flag = " [PROBATION]" if p.get("is_probationary") else ""
        print(f"    #{p['rank']} {p['politician_id']}  weight={p['weight']*100:.0f}%  size=${size:.0f}{flag}")
    print(f"  Previously copied: {len(state['copied'])} trades")
    print()

    # Fetch all pool members' trades first (needed for consensus detection)
    all_trades = []
    for member in pool:
        pid = member["politician_id"]
        member_trades = fetch_politician_trades(pid)
        all_trades.extend(member_trades)
        print(f"  Fetched {len(member_trades)} trades for {pid}")

    # Build pool_buys list for consensus detection (only buys, within window)
    all_pool_buys = [t for t in all_trades if t["tx_type"] == "buy"]

    print(f"\n  Processing {len(all_trades)} total trades from pool...\n")

    new_buys, new_sells, skipped = 0, 0, 0
    total_deployed = 0
    acts = []   # collect all copied trades, push ONE consolidated message at end

    for t in all_trades:
        if t["tx_id"] in state["copied"]:
            continue

        copied, reason, size = copy_trade(t, all_pool_buys, state, cfg)
        pid = t["politician_id"]
        lag = t.get("gap_days", "?")
        disclosed = t.get("pub_date", "?")
        symbol = f"{t['tx_type'].upper():4s} {t['ticker']:6s}"

        if copied:
            if t["tx_type"] == "buy":
                new_buys += 1
                total_deployed += size
                acts.append(f"🟢 BUY `{t['ticker']}` ${size:,.0f}")
            else:
                new_sells += 1
                acts.append(f"🔴 SELL `{t['ticker']}`")
            print(f"  ✓ COPIED  [{pid}] {symbol}  disclosed={disclosed}  lag={lag}d  → {reason}")
        else:
            skipped += 1
            print(f"  ✗ SKIP    [{pid}] {symbol}  disclosed={disclosed}  lag={lag}d  → {reason}")

    if new_buys == 0 and new_sells == 0:
        print("\n  No new trades to copy since last run.")

    # ── ONE consolidated push for all newly-copied trades ────────────────────
    if acts and tg:
        tg.notify_batch("Capitol Copier · new trades", acts, emoji="🏛")

    print(f"\n  Session: +{new_buys} buys, +{new_sells} sells, {skipped} skips, "
          f"${total_deployed:.0f} deployed")
    print(f"  Lifetime totals: buys={state['stats']['total_buys']} sells={state['stats']['total_sells']}")

    save_state(state)


def rebalance(target_pct=0.60, max_tickers_per_member=5):
    """One-time reallocation toward `target_pct` of equity invested via Capitol
    Copier picks, spread across each pool member's most recent distinct buys."""
    import urllib3
    urllib3.disable_warnings()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()
    cfg   = load_config()
    pool  = pool_manager.get_pool()

    if not pool:
        print(f"[{now}] Capitol Copier REBALANCE — POOL IS EMPTY")
        return

    equity   = get_account_equity() or 0
    exposure = get_capitol_exposure()
    target   = equity * target_pct
    gap      = target - exposure

    print(f"[{now}] Capitol Copier — REBALANCE (target {target_pct*100:.0f}% of equity)")
    print(f"  Equity:           ${equity:,.2f}")
    print(f"  Current exposure: ${exposure:,.2f}")
    print(f"  Target:           ${target:,.2f}")
    print(f"  Gap to deploy:    ${gap:,.2f}")
    print()

    if gap <= 0:
        print("  Already at or above target — nothing to deploy.")
        return

    min_pos    = cfg["pool"]["min_position_usd"]
    max_pos    = cfg["pool"]["max_position_usd"]
    weight_sum = sum(m["weight"] for m in pool) or 1.0

    total_deployed = 0
    n_orders = 0

    for member in pool:
        pid    = member["politician_id"]
        weight = member["weight"] / weight_sum
        bucket = gap * weight
        print(f"  [{pid}] weight={weight*100:.0f}% (normalized)  bucket=${bucket:,.2f}")

        if bucket < min_pos:
            print(f"    -> below ${min_pos} minimum, skipping")
            continue

        trades = fetch_politician_trades(pid)
        buys = [t for t in trades
                if t["tx_type"] == "buy" and is_eligible_ticker(t["ticker"], cfg)]
        buys.sort(key=lambda t: (t.get("pub_date", ""), t.get("tx_date", "")), reverse=True)

        seen, picks = set(), []
        for t in buys:
            if t["ticker"] in seen:
                continue
            seen.add(t["ticker"])
            picks.append(t)
            if len(picks) >= max_tickers_per_member:
                break

        if not picks:
            print("    -> no eligible recent buys found, bucket undeployed")
            continue

        per_ticker = max(min_pos, min(max_pos, bucket / len(picks)))
        print(f"    -> {len(picks)} ticker(s) @ ${per_ticker:,.2f} each: "
              f"{', '.join(t['ticker'] for t in picks)}")

        for t in picks:
            ticker = t["ticker"]
            result = place_market_order(ticker, "buy", notional=per_ticker)
            order_id = result.get("id", "")
            status   = result.get("status", result.get("message", "unknown"))
            if order_id:
                if t["tx_id"] not in state["copied"]:
                    state["copied"].append(t["tx_id"])
                state["stats"]["total_buys"] += 1
                state.setdefault("by_politician", {}).setdefault(pid, {"buys": 0, "sells": 0})["buys"] += 1
                total_deployed += per_ticker
                n_orders += 1
                print(f"      ✓ BUY {ticker:<6} ${per_ticker:,.2f}  order {order_id[:8]}")
            else:
                print(f"      ✗ BUY {ticker:<6} failed: {status}")

    print()
    print(f"  Deployed ${total_deployed:,.2f} across {n_orders} orders")
    new_exposure = exposure + total_deployed
    if equity:
        print(f"  New exposure (est): ${new_exposure:,.2f}  ({new_exposure/equity*100:.1f}% of equity)")

    save_state(state)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebalance", action="store_true",
                         help="One-time reallocation toward target equity allocation via Capitol Copier picks")
    parser.add_argument("--target-pct", type=float, default=0.60,
                         help="Target fraction of equity actively invested (default 0.60)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Preview dynamic-exit/pyramid decisions only — no orders, no new copies")
    args = parser.parse_args()

    if args.rebalance:
        rebalance(target_pct=args.target_pct)
    else:
        run(dry_run=args.dry_run)
