"""
Performance Tracker
===================
Pulls filled orders and closed positions from Alpaca, pairs buys with sells,
computes per-trade P&L, and appends to performance_log.json.

Called by:
  - sunday_review.py (weekly)
  - Any script that wants an up-to-date performance snapshot
"""

import os, json, requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = "https://data.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}

LOG_FILE = os.path.join(os.path.dirname(__file__), "performance_log.json")

# Politician attribution — which tickers came from whom
CAPITOL_COPIER_TICKERS = {
    "MU", "GOOGL", "DIS", "NDAQ", "TGT", "PINS",
    "CAT", "SBUX", "MS", "BAC", "JPM", "AMZN",
    "META", "NVDA", "MSFT", "HD", "JNJ", "TMO",
}


def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return {"trades": [], "last_synced": None}


def save_log(log):
    log["last_synced"] = datetime.utcnow().isoformat()
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def fetch_filled_orders(since_days=90):
    after = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    r = requests.get(
        f"{BASE_URL}/orders",
        headers=HEADERS,
        params={"status": "filled", "limit": 500, "after": after, "direction": "asc"}
    )
    return r.json() if r.status_code == 200 else []


def fetch_spy_return(start_date, end_date):
    """Get SPY return between two dates for benchmark comparison."""
    try:
        r = requests.get(
            f"{DATA_URL}/stocks/SPY/bars",
            headers=HEADERS,
            params={
                "timeframe": "1Day",
                "start": start_date,
                "end": end_date,
                "limit": 2,
            }
        )
        bars = r.json().get("bars", [])
        if len(bars) >= 2:
            return (bars[-1]["c"] - bars[0]["o"]) / bars[0]["o"]
    except:
        pass
    return None


def pair_trades(orders):
    """
    Match buy orders with subsequent sell orders per symbol.
    Returns list of closed trade dicts.
    """
    from collections import defaultdict
    buy_queue = defaultdict(list)
    closed = []

    for o in orders:
        if o.get("status") != "filled":
            continue
        symbol    = o["symbol"]
        side      = o["side"]
        qty       = float(o.get("filled_qty") or o.get("qty") or 0)
        avg_price = float(o.get("filled_avg_price") or 0)
        filled_at = o.get("filled_at") or o.get("updated_at") or ""
        order_id  = o["id"]

        if side == "buy" and avg_price > 0:
            buy_queue[symbol].append({
                "order_id": order_id,
                "qty": qty,
                "entry_price": avg_price,
                "entry_date": filled_at[:10],
                "remaining_qty": qty,
            })

        elif side == "sell" and avg_price > 0:
            sell_qty  = qty
            sell_price = avg_price
            sell_date  = filled_at[:10]

            while sell_qty > 0 and buy_queue[symbol]:
                buy = buy_queue[symbol][0]
                matched_qty = min(sell_qty, buy["remaining_qty"])

                return_pct  = (sell_price - buy["entry_price"]) / buy["entry_price"]
                hold_days   = (
                    datetime.fromisoformat(sell_date) -
                    datetime.fromisoformat(buy["entry_date"])
                ).days if buy["entry_date"] and sell_date else 0
                pnl_usd = (sell_price - buy["entry_price"]) * matched_qty

                strategy = "tsla_strategy" if symbol == "TSLA" else \
                           "capitol_copier" if symbol in CAPITOL_COPIER_TICKERS else "other"

                closed.append({
                    "symbol":       symbol,
                    "strategy":     strategy,
                    "entry_price":  buy["entry_price"],
                    "exit_price":   sell_price,
                    "qty":          matched_qty,
                    "entry_date":   buy["entry_date"],
                    "exit_date":    sell_date,
                    "hold_days":    hold_days,
                    "return_pct":   round(return_pct, 6),
                    "pnl_usd":      round(pnl_usd, 2),
                    "buy_order_id": buy["order_id"],
                    "sell_order_id": order_id,
                    "winner":       return_pct > 0,
                })

                buy["remaining_qty"] -= matched_qty
                sell_qty -= matched_qty
                if buy["remaining_qty"] <= 0:
                    buy_queue[symbol].pop(0)

    return closed


def compute_trade_score(trade):
    """
    Composite score per closed trade (0–1 scale).
      return_pct        × 0.40  — raw profit
      risk_adj_return   × 0.30  — return per unit of risk (simplified as return/abs(return) clipped)
      capture_ratio     × 0.20  — we approximate with return magnitude vs. benchmark
      speed_score       × 0.10  — faster = better capital efficiency
    """
    r = trade["return_pct"]
    hold = max(trade["hold_days"], 1)

    # Normalise return to [-1, 1] using tanh
    import math
    norm_return = math.tanh(r * 10)                           # 10% gain → ~0.76

    # Risk-adjusted: positive trades score higher, losses penalised
    risk_adj = norm_return / math.sqrt(hold)

    # Speed: annualised return proxy
    annualised = r * (252 / hold)
    speed = math.tanh(annualised * 2)

    score = (
        norm_return  * 0.40 +
        risk_adj     * 0.30 +
        norm_return  * 0.20 +   # capture_ratio: use return as proxy until we have OHLC data
        speed        * 0.10
    )
    return round(score, 4)


def sync(verbose=True):
    """Pull latest fills, pair trades, update log. Returns updated log."""
    log    = load_log()
    known  = {t["sell_order_id"] for t in log["trades"]}
    orders = fetch_filled_orders(since_days=180)

    if not orders:
        if verbose:
            print("  No filled orders found.")
        return log

    new_closed = pair_trades(orders)
    added = 0
    for t in new_closed:
        if t["sell_order_id"] not in known:
            t["trade_score"] = compute_trade_score(t)
            log["trades"].append(t)
            known.add(t["sell_order_id"])
            added += 1

    if verbose:
        print(f"  Trades synced: {len(new_closed)} pairs found, {added} new")

    save_log(log)
    return log


def summarise(log, strategy=None, last_days=None):
    """Return dict of key metrics for a strategy (or all)."""
    import math, statistics

    trades = log["trades"]
    if strategy:
        trades = [t for t in trades if t["strategy"] == strategy]
    if last_days:
        cutoff = (datetime.utcnow() - timedelta(days=last_days)).strftime("%Y-%m-%d")
        trades = [t for t in trades if t["exit_date"] >= cutoff]

    if not trades:
        return {"n_trades": 0}

    returns     = [t["return_pct"] for t in trades]
    pnls        = [t["pnl_usd"] for t in trades]
    winners     = [t for t in trades if t["winner"]]
    losers      = [t for t in trades if not t["winner"]]

    win_rate    = len(winners) / len(trades)
    avg_return  = sum(returns) / len(returns)
    total_gain  = sum(p for p in pnls if p > 0) or 0
    total_loss  = abs(sum(p for p in pnls if p < 0)) or 1
    profit_factor = total_gain / total_loss

    std_dev = statistics.stdev(returns) if len(returns) > 1 else 0
    sharpe  = (avg_return / std_dev * math.sqrt(252)) if std_dev > 0 else 0

    avg_hold    = sum(t["hold_days"] for t in trades) / len(trades)
    avg_score   = sum(t.get("trade_score", 0) for t in trades) / len(trades)

    return {
        "n_trades":      len(trades),
        "win_rate":      round(win_rate, 4),
        "avg_return_pct": round(avg_return * 100, 3),
        "total_pnl_usd": round(sum(pnls), 2),
        "profit_factor": round(profit_factor, 3),
        "sharpe":        round(sharpe, 3),
        "avg_hold_days": round(avg_hold, 1),
        "avg_trade_score": round(avg_score, 4),
        "best_trade":    max(trades, key=lambda t: t["return_pct"])["symbol"] if trades else None,
        "worst_trade":   min(trades, key=lambda t: t["return_pct"])["symbol"] if trades else None,
    }


if __name__ == "__main__":
    print("Syncing performance log...")
    log = sync()
    print()

    for strat in ["all", "capitol_copier", "tsla_strategy"]:
        s = summarise(log, strategy=None if strat == "all" else strat, last_days=90)
        label = strat.replace("_", " ").title()
        if s["n_trades"] == 0:
            print(f"{label}: no closed trades yet")
        else:
            print(f"{label}:")
            print(f"  Trades: {s['n_trades']}  |  Win rate: {s['win_rate']*100:.1f}%  |  Avg return: {s['avg_return_pct']:+.2f}%")
            print(f"  P&L: ${s['total_pnl_usd']:+,.2f}  |  Profit factor: {s['profit_factor']:.2f}  |  Sharpe: {s['sharpe']:.2f}")
            print(f"  Avg hold: {s['avg_hold_days']:.0f}d  |  Score: {s['avg_trade_score']:.3f}")
        print()
