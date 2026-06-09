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
"""

import os, json, re, requests
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv

import pool_manager
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

STATE_FILE   = os.path.join(os.path.dirname(__file__), ".copied_trades.json")
CONFIG_FILE  = os.path.join(os.path.dirname(__file__), "strategy_config.json")


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


def get_capitol_exposure():
    """Sum of market value across positions tagged as Capitol Copier."""
    r = requests.get(f"{BASE_URL}/positions", headers=ALPACA_HEADERS)
    if r.status_code != 200:
        return 0
    positions = r.json() or []
    cfg = load_config()
    legacy_id = cfg["capitol_copier"].get("legacy_politician_id")  # for TSLA exclusion
    # We treat all positions except TSLA + AAPL test as Capitol Copier exposure
    total = 0
    for p in positions:
        if p["symbol"] in ("TSLA", "AAPL"):
            continue
        total += abs(float(p.get("market_value", 0)))
    return total


# ── Copy logic ───────────────────────────────────────────────────────────────

def is_eligible_ticker(ticker):
    if not ticker or "/" in ticker or len(ticker) > 5:
        return False
    if ticker in ("XSP", "SPX", "VIX", "NDX"):  # known index symbols
        return False
    return True


def copy_trade(trade, all_pool_buys, state, cfg):
    ticker        = trade["ticker"]
    tx_type       = trade["tx_type"]
    tx_id         = trade["tx_id"]
    politician_id = trade["politician_id"]

    if tx_id in state["copied"]:
        return False, "already copied", 0

    if not is_eligible_ticker(ticker):
        return False, f"non-standard ticker {ticker}", 0

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
            # Fire Telegram notification
            if tg:
                try:
                    sell_value_est = float(pos.get("market_value", 0))
                except (TypeError, ValueError):
                    sell_value_est = 0
                tg.notify_trade(politician_id, ticker, "sell", sell_value_est)
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

    # Hard skip if sentiment is very bearish (score 1)
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
        # Fire Telegram notification if configured
        if tg:
            tg.notify_trade(politician_id, ticker, "buy", size,
                            sentiment=sentiment, consensus=consensus)
        return True, f"buy {order_id[:8]} ${size:.0f}{consensus_note}{sentiment_note}", size
    return False, f"buy failed: {status}", 0


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    import urllib3
    urllib3.disable_warnings()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()
    cfg   = load_config()

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
            else:
                new_sells += 1
            print(f"  ✓ COPIED  [{pid}] {symbol}  disclosed={disclosed}  lag={lag}d  → {reason}")
        else:
            skipped += 1
            print(f"  ✗ SKIP    [{pid}] {symbol}  disclosed={disclosed}  lag={lag}d  → {reason}")

    if new_buys == 0 and new_sells == 0:
        print("\n  No new trades to copy since last run.")

    print(f"\n  Session: +{new_buys} buys, +{new_sells} sells, {skipped} skips, "
          f"${total_deployed:.0f} deployed")
    print(f"  Lifetime totals: buys={state['stats']['total_buys']} sells={state['stats']['total_sells']}")

    save_state(state)


if __name__ == "__main__":
    run()
