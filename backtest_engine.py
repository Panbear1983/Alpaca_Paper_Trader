"""
Backtest Engine — measure a politician's edge by replaying their disclosed trades
==================================================================================
For each disclosed trade, we ask: "if we had copied at the disclosure date and held
30/60 days, what would our return have been vs SPY?"

Outputs per-politician metrics: realized return, alpha vs SPY, win rate, sample size.

CLI usage:
  python3 backtest_engine.py --politician K000389
  python3 backtest_engine.py --politician K000389 --days 90 --no-cache
"""

import os, json, re, sys, time, argparse
from datetime import datetime, timedelta, timezone
from typing import Optional
import requests
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = "https://data.alpaca.markets/v2"

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}

CT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept":     "text/x-component",
    "RSC":        "1",
}

CONFIG_FILE        = os.path.join(os.path.dirname(__file__), "strategy_config.json")
CACHE_FILE         = os.path.join(os.path.dirname(__file__), "backtest_cache.json")
BACKTEST_RESULTS   = os.path.join(os.path.dirname(__file__), "backtest_results.json")


# ── Cache (price history per ticker) ──────────────────────────────────────────

def _load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            try:    return json.load(f)
            except: return {}
    return {}


def _save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _cache_key(ticker, start, end):
    return f"{ticker}|{start}|{end}"


# ── Price lookup ──────────────────────────────────────────────────────────────

def get_bars(ticker, start, end, cache=None):
    """Fetch daily bars for ticker between start..end (YYYY-MM-DD). Returns list of bars."""
    if cache is not None:
        key = _cache_key(ticker, start, end)
        if key in cache:
            return cache[key]

    try:
        r = requests.get(
            f"{DATA_URL}/stocks/{ticker}/bars",
            headers=ALPACA_HEADERS,
            params={
                "timeframe":  "1Day",
                "start":      start,
                "end":        end,
                "limit":      200,
                "adjustment": "split",
                "feed":       "iex",   # free tier feed
            },
            timeout=15,
        )
        bars = r.json().get("bars", []) if r.status_code == 200 else []
    except Exception as e:
        bars = []

    if cache is not None and bars:
        cache[_cache_key(ticker, start, end)] = bars

    return bars


def price_on_or_after(ticker, target_date, cache=None, lookback_days=7):
    """Get the first available close price on or after target_date. Returns None if unavailable."""
    start = target_date
    end   = (datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    bars  = get_bars(ticker, start, end, cache)
    if not bars:
        return None
    return float(bars[0]["c"])


# ── Politician trade fetch (reused/adapted from capitol_copier) ───────────────

def _parse_trades_from_rsc(c):
    """Extract trade tuples from a Capitol Trades RSC response body."""
    tickers     = re.findall(r'"issuerTicker":"([^"]+)"',  c)
    tx_types    = re.findall(r'"txType":"(buy|sell)"',     c)
    tx_dates    = re.findall(r'"txDate":"(\d{4}-\d{2}-\d{2})"', c)
    pub_dates   = re.findall(r'"pubDate":"(\d{4}-\d{2}-\d{2})', c)
    values      = re.findall(r'"value":(\d+)',             c)
    owners      = re.findall(r'"owner":"([^"]*)"',         c)
    gaps        = re.findall(r'"reportingGap":(\d+)',      c)
    issuers     = re.findall(r'"issuerName":"([^"]+)"',    c)

    n = min(len(tickers), len(tx_types), len(tx_dates))
    trades = []
    for i in range(n):
        raw = tickers[i]
        ticker = raw.split(":")[0] if ":" in raw else raw
        if not ticker or "/" in ticker or len(ticker) > 5:
            continue
        trades.append({
            "ticker":   ticker,
            "issuer":   issuers[i] if i < len(issuers) else "",
            "tx_type":  tx_types[i],
            "tx_date":  tx_dates[i],
            "pub_date": pub_dates[i] if i < len(pub_dates) else "",
            "value":    int(values[i]) if i < len(values) else 0,
            "owner":    owners[i] if i < len(owners) else "",
            "gap_days": int(gaps[i]) if i < len(gaps) else 0,
        })
    return trades


def fetch_politician_trades(pol_id, per_page=96, max_pages=5):
    """Scrape trades for a politician across multiple pages of disclosures."""
    all_trades = []
    seen = set()
    for page in range(1, max_pages + 1):
        url = (f"https://www.capitoltrades.com/politicians/{pol_id}"
               f"?per_page={per_page}&sort=-reportedAt&page={page}")
        try:
            r = requests.get(
                url,
                headers={**CT_HEADERS, "Next-Url": f"/politicians/{pol_id}"},
                verify=False,
                timeout=20,
            )
            page_trades = _parse_trades_from_rsc(r.text)
        except Exception:
            page_trades = []

        if not page_trades:
            break

        # Dedup across pages using (ticker, tx_date, tx_type)
        added = 0
        for t in page_trades:
            key = (t["ticker"], t["tx_date"], t["tx_type"], t.get("owner",""))
            if key not in seen:
                seen.add(key)
                all_trades.append(t)
                added += 1
        if added == 0:
            break  # No new trades on this page → stop paginating

    return all_trades


# ── SPY benchmark cache ───────────────────────────────────────────────────────

def spy_return(start, end, cache=None):
    """SPY total return from start to end."""
    bars = get_bars("SPY", start, end, cache)
    if len(bars) < 2:
        return None
    return (bars[-1]["c"] - bars[0]["c"]) / bars[0]["c"]


# ── Per-trade backtest ────────────────────────────────────────────────────────

def backtest_trade(trade, cache=None, hold_days_list=(30, 60, 15, 10, 5)):
    """
    For a single politician trade, compute hypothetical return if we had copied
    at disclosure (pub_date) and held N days. Falls back to shorter hold periods
    when 30d hasn't elapsed yet (recently-disclosed trades).
    Returns dict with: ticker, pub_date, returns for each available hold period, alpha vs SPY.
    """
    ticker   = trade["ticker"]
    pub_date = trade.get("pub_date") or trade["tx_date"]
    today    = datetime.now(timezone.utc).date()
    pub_dt   = datetime.strptime(pub_date, "%Y-%m-%d").date()

    entry_price = price_on_or_after(ticker, pub_date, cache)
    if entry_price is None:
        return None

    result = {
        "ticker":      ticker,
        "tx_type":     trade["tx_type"],
        "pub_date":    pub_date,
        "entry_price": entry_price,
    }

    # Always compute return "as of today" — partial-window measure
    today_str = today.strftime("%Y-%m-%d")
    days_held = (today - pub_dt).days
    if days_held > 0:
        cur_price = price_on_or_after(ticker, (today - timedelta(days=3)).strftime("%Y-%m-%d"), cache)
        if cur_price is not None:
            ret_today = (cur_price - entry_price) / entry_price
            if trade["tx_type"] == "sell":
                ret_today = -ret_today
            spy_r = spy_return(pub_date, today_str, cache)
            result["return_to_date"]   = round(ret_today, 6)
            result["alpha_to_date"]    = round(ret_today - spy_r, 6) if spy_r is not None else None
            result["days_held_to_date"] = days_held

    for hd in hold_days_list:
        exit_date_dt = pub_dt + timedelta(days=hd)
        # Skip if exit date hasn't happened yet
        if exit_date_dt > today:
            result[f"return_{hd}d"] = None
            result[f"alpha_{hd}d"]  = None
            continue

        exit_date_str = exit_date_dt.strftime("%Y-%m-%d")
        exit_price = price_on_or_after(ticker, exit_date_str, cache)
        if exit_price is None:
            result[f"return_{hd}d"] = None
            result[f"alpha_{hd}d"]  = None
            continue

        ret = (exit_price - entry_price) / entry_price
        if trade["tx_type"] == "sell":
            ret = -ret  # for sells, positive score if price dropped after they sold

        spy_ret = spy_return(pub_date, exit_date_str, cache)
        alpha = ret - spy_ret if spy_ret is not None else None

        result[f"return_{hd}d"] = round(ret, 6)
        result[f"alpha_{hd}d"]  = round(alpha, 6) if alpha is not None else None

    return result


# ── Politician-level backtest ─────────────────────────────────────────────────

def backtest_politician(pol_id, window_days=90, use_cache=True, verbose=False):
    """Full backtest for a single politician over last `window_days` of disclosures."""
    cache = _load_cache() if use_cache else {}
    trades = fetch_politician_trades(pol_id)

    if not trades:
        return {
            "politician_id":  pol_id,
            "n_trades_total": 0,
            "n_trades_scored": 0,
            "scored": [],
            "metrics": None,
            "error": "no_trades_fetched",
        }

    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    in_window = [t for t in trades if t.get("pub_date", "") >= cutoff]

    scored = []
    for t in in_window:
        if t["tx_type"] != "buy":
            # We score buys (the actionable signal); sells are noted but not scored
            continue
        bt = backtest_trade(t, cache=cache)
        if bt is None:
            continue
        scored.append(bt)
        if verbose:
            print(f"  {t['ticker']:6s}  {t['pub_date']}  entry=${bt['entry_price']:.2f}  "
                  f"r30={bt.get('return_30d')}  α30={bt.get('alpha_30d')}")

    if use_cache:
        _save_cache(cache)

    # Compute aggregate metrics from scored trades
    metrics = compute_politician_metrics(scored, trades_total=len(trades), in_window=len(in_window))
    return {
        "politician_id":   pol_id,
        "window_days":     window_days,
        "n_trades_total":  len(trades),
        "n_trades_in_window": len(in_window),
        "n_trades_scored": len(scored),
        "scored":          scored,
        "metrics":         metrics,
        "computed_at":     datetime.now(timezone.utc).isoformat(),
    }


def compute_politician_metrics(scored, trades_total, in_window):
    """Aggregate per-trade results into politician-level metrics.

    Picks the longest available hold window with data, in priority:
      30d → 60d → 15d → 10d → 5d → to_date (partial)
    """
    import statistics, math

    def collect(key_pattern):
        rets = [s.get(f"return_{key_pattern}") for s in scored]
        rets = [r for r in rets if r is not None]
        alphas = [s.get(f"alpha_{key_pattern}") for s in scored]
        alphas = [a for a in alphas if a is not None]
        return rets, alphas

    # Try each window in priority order
    chosen_window = None
    primary_returns, primary_alphas = [], []
    for window in ("30d", "60d", "15d", "10d", "5d", "to_date"):
        rets, alphas = collect(window)
        if len(rets) >= max(3, len(scored) // 2):  # require at least 3 or half the trades
            chosen_window = window
            primary_returns, primary_alphas = rets, alphas
            break

    n = len(primary_returns)
    if n == 0:
        return {
            "n_scored":          0,
            "chosen_window":     None,
            "win_rate":          None,
            "avg_return":        None,
            "avg_alpha":         None,
            "median_return":     None,
            "stdev_return":      None,
            "sample_confidence": 0,
            "n_trades_total":    trades_total,
            "n_trades_in_window": in_window,
        }

    winners = sum(1 for r in primary_returns if r > 0)
    win_rate = winners / n
    avg_ret  = sum(primary_returns) / n
    avg_alpha = (sum(primary_alphas) / len(primary_alphas)) if primary_alphas else None
    median_r = statistics.median(primary_returns)
    stdev_r  = statistics.stdev(primary_returns) if n > 1 else 0
    sample_conf = min(1.0, math.log(max(n, 1) + 1) / math.log(20))

    # Window penalty: shorter windows = less confidence in the alpha signal
    window_confidence = {
        "30d": 1.0, "60d": 1.0, "15d": 0.75, "10d": 0.6, "5d": 0.4, "to_date": 0.5,
    }.get(chosen_window, 0.5)

    return {
        "n_scored":          n,
        "chosen_window":     chosen_window,
        "win_rate":          round(win_rate, 4),
        "avg_return":        round(avg_ret, 6),
        "avg_alpha":         round(avg_alpha, 6) if avg_alpha is not None else None,
        "median_return":     round(median_r, 6),
        "stdev_return":      round(stdev_r, 6),
        "sample_confidence": round(sample_conf * window_confidence, 4),
        "n_trades_total":    trades_total,
        "n_trades_in_window": in_window,
    }


# ── Save results ──────────────────────────────────────────────────────────────

def save_result(result):
    all_results = {}
    if os.path.exists(BACKTEST_RESULTS):
        with open(BACKTEST_RESULTS) as f:
            try:
                all_results = json.load(f)
            except:
                pass
    all_results[result["politician_id"]] = result
    with open(BACKTEST_RESULTS, "w") as f:
        json.dump(all_results, f, indent=2)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest a politician's trades")
    parser.add_argument("--politician", required=True, help="Politician bioguide ID (e.g. K000389)")
    parser.add_argument("--days", type=int, default=90, help="Backtest window in days")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--save", action="store_true", help="Save result to backtest_results.json")
    args = parser.parse_args()

    print(f"Backtesting {args.politician} over last {args.days} days...")
    result = backtest_politician(
        args.politician,
        window_days=args.days,
        use_cache=not args.no_cache,
        verbose=args.verbose,
    )

    m = result.get("metrics") or {}
    print(f"\n{'='*60}")
    print(f"  {args.politician}  —  last {args.days} days")
    print(f"{'='*60}")
    print(f"  Trades total              : {result['n_trades_total']}")
    print(f"  Trades in window (90d)    : {result.get('n_trades_in_window', 0)}")
    print(f"  Trades scored (buys w/ price data): {result['n_trades_scored']}")
    if m and m.get("n_scored"):
        print(f"  Hold window used          : {m.get('chosen_window')}")
        print(f"  Win rate                  : {m['win_rate']*100:.1f}%")
        print(f"  Avg return                : {m['avg_return']*100:+.2f}%")
        if m.get('avg_alpha') is not None:
            print(f"  Avg alpha vs SPY          : {m['avg_alpha']*100:+.2f}%")
        print(f"  Median return             : {m['median_return']*100:+.2f}%")
        print(f"  Stdev return              : {m['stdev_return']*100:.2f}%")
        print(f"  Sample confidence         : {m['sample_confidence']:.2f}")
    print(f"{'='*60}\n")

    if args.save:
        save_result(result)
        print(f"Saved to {BACKTEST_RESULTS}")


if __name__ == "__main__":
    main()
