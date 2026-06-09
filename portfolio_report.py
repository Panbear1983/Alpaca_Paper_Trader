"""
Portfolio Report — run anytime for a full snapshot.
Usage: python3 portfolio_report.py
"""

import os, json, requests, math
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = "https://data.alpaca.markets/v2"

H = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}

CAPITOL_TICKERS = {
    "MU","GOOGL","DIS","NDAQ","TGT","PINS","CAT","SBUX","MS",
    "BAC","JPM","AMZN","META","NVDA","MSFT","HD","JNJ","TMO",
}

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "strategy_config.json")
LOG_FILE    = os.path.join(os.path.dirname(__file__), "performance_log.json")

W = 58  # report width


def bar(char="─"): return char * W
def header(title): print(f"\n{'━'*W}\n  {title}\n{'━'*W}")
def section(title): print(f"\n  {title}\n  {'─'*(W-2)}")


def get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=H, params=params)
    return r.json() if r.status_code == 200 else {}


def spy_change_today():
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = requests.get(f"{DATA_URL}/stocks/SPY/bars",
                         headers=H,
                         params={"timeframe":"1Day","start":today,"limit":1})
        bars = r.json().get("bars",[])
        if bars:
            b = bars[0]
            return (b["c"] - b["o"]) / b["o"] * 100
    except: pass
    return None


def load_perf_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f).get("trades", [])
    return []


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def run():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Fetch data ────────────────────────────────────────────────────────────
    account   = get("/account")
    positions = get("/positions") or []
    if not isinstance(positions, list): positions = []

    open_orders = requests.get(f"{BASE_URL}/orders",
                               headers=H,
                               params={"status":"open","limit":50}).json()
    if not isinstance(open_orders, list): open_orders = []

    clock  = get("/clock")
    trades = load_perf_log()
    cfg    = load_config()
    spy    = spy_change_today()

    # ── Header ────────────────────────────────────────────────────────────────
    header(f"PORTFOLIO REPORT  —  {now}")

    mkt_status = "OPEN" if clock.get("is_open") else f"CLOSED  (opens {clock.get('next_open','?')[:16]})"
    print(f"  Market: {mkt_status}")
    if spy is not None:
        print(f"  SPY today: {spy:+.2f}%")

    # ── Account summary ───────────────────────────────────────────────────────
    section("ACCOUNT")
    equity       = float(account.get("equity", 0))
    last_equity  = float(account.get("last_equity", equity))
    buying_power = float(account.get("buying_power", 0))
    daily_pnl    = equity - last_equity
    daily_pct    = daily_pnl / last_equity * 100 if last_equity else 0

    print(f"  Portfolio value : ${equity:>12,.2f}")
    print(f"  Buying power    : ${buying_power:>12,.2f}")
    print(f"  Daily P&L       : ${daily_pnl:>+12,.2f}  ({daily_pct:+.2f}%)")
    if spy is not None:
        alpha = daily_pct - spy
        print(f"  vs SPY alpha    : {alpha:>+12.2f}%")

    # ── Open positions ────────────────────────────────────────────────────────
    if positions:
        section("OPEN POSITIONS")
        capitol, tsla_pos, other = [], [], []
        for p in positions:
            sym = p["symbol"]
            if sym == "TSLA":          tsla_pos.append(p)
            elif sym in CAPITOL_TICKERS: capitol.append(p)
            else:                        other.append(p)

        total_unreal = 0

        def print_positions(label, pos_list):
            nonlocal total_unreal
            if not pos_list: return
            print(f"\n  {label}")
            print(f"  {'SYM':<7} {'QTY':>6} {'AVG ENTRY':>10} {'CUR PRICE':>10} {'UNREAL P&L':>12} {'%':>7}")
            print(f"  {'─'*56}")
            for p in sorted(pos_list, key=lambda x: float(x.get('unrealized_pl',0)), reverse=True):
                sym  = p['symbol']
                qty  = float(p.get('qty', 0))
                avg  = float(p.get('avg_entry_price', 0))
                cur  = float(p.get('current_price', 0))
                upl  = float(p.get('unrealized_pl', 0))
                uplp = float(p.get('unrealized_plpc', 0)) * 100
                total_unreal += upl
                arrow = "▲" if upl >= 0 else "▼"
                print(f"  {sym:<7} {qty:>6.1f} ${avg:>9.2f} ${cur:>9.2f} ${upl:>+10.2f} {arrow}{abs(uplp):>5.1f}%")

        print_positions("Capitol Copier (Smart Money Pool)", capitol)
        print_positions("TSLA Strategy", tsla_pos)
        print_positions("Other", other)

        print(f"\n  {'─'*56}")
        print(f"  Total unrealized P&L: ${total_unreal:>+,.2f}")

    else:
        section("OPEN POSITIONS")
        print("  No open positions yet (market may be closed / orders pending)")

    # ── Pending orders summary ────────────────────────────────────────────────
    market_pending = [o for o in open_orders if o.get("type") == "market"]
    limit_pending  = [o for o in open_orders if o.get("type") == "limit"]
    stop_pending   = [o for o in open_orders if o.get("type") == "stop"]

    section("PENDING ORDERS")
    print(f"  Market orders (fill at open)   : {len(market_pending)}")
    for o in market_pending:
        qty = o.get('qty') or o.get('notional') or '?'
        print(f"    {o['symbol']:<7} {o['side']:4s}  notional/qty={qty}")
    if limit_pending:
        print(f"\n  GTC Limit orders (TSLA ladder) : {len(limit_pending)}")
        for o in limit_pending:
            print(f"    {o['symbol']:<7} {o['side']:4s}  qty={o.get('qty','?')}  @${o.get('limit_price','?')}")
    if stop_pending:
        print(f"\n  Stop orders (protection)       : {len(stop_pending)}")
        for o in stop_pending:
            print(f"    {o['symbol']:<7} {o['side']:4s}  qty={o.get('qty','?')}  stop @${o.get('stop_price','?')}  [{o['status']}]")

    # ── Closed trade performance ──────────────────────────────────────────────
    section("CLOSED TRADE PERFORMANCE")
    if not trades:
        print("  No closed trades yet — positions are still open.")
        print("  Performance data will populate as trades are closed.")
    else:
        def stats(tlist):
            if not tlist: return None
            returns = [t["return_pct"] for t in tlist]
            pnls    = [t["pnl_usd"] for t in tlist]
            winners = [t for t in tlist if t["winner"]]
            gains   = sum(p for p in pnls if p > 0) or 0
            losses  = abs(sum(p for p in pnls if p < 0)) or 1
            avg_r   = sum(returns)/len(returns)
            std_r   = (sum((r-avg_r)**2 for r in returns)/max(len(returns)-1,1))**0.5
            sharpe  = avg_r/std_r*math.sqrt(252) if std_r > 0 else 0
            return {
                "n": len(tlist),
                "win_rate": len(winners)/len(tlist)*100,
                "avg_ret": avg_r*100,
                "total_pnl": sum(pnls),
                "pf": gains/losses,
                "sharpe": sharpe,
            }

        cc_trades   = [t for t in trades if t["strategy"]=="capitol_copier"]
        tsla_trades = [t for t in trades if t["strategy"]=="tsla_strategy"]
        cc_s   = stats(cc_trades)
        tsla_s = stats(tsla_trades)
        all_s  = stats(trades)

        def print_stats(label, s):
            if not s: return
            print(f"  {label}")
            print(f"    Trades: {s['n']}  |  Win rate: {s['win_rate']:.1f}%  |  Avg return: {s['avg_ret']:+.2f}%")
            print(f"    P&L: ${s['total_pnl']:+,.2f}  |  Profit factor: {s['pf']:.2f}  |  Sharpe: {s['sharpe']:.2f}\n")

        print_stats("Capitol Copier", cc_s)
        print_stats("TSLA Strategy",  tsla_s)
        print_stats("Combined",       all_s)

        # Best and worst
        if trades:
            best  = max(trades, key=lambda t: t["return_pct"])
            worst = min(trades, key=lambda t: t["return_pct"])
            print(f"  Best trade : {best['symbol']} {best['return_pct']*100:+.1f}% (${best['pnl_usd']:+.2f})")
            print(f"  Worst trade: {worst['symbol']} {worst['return_pct']*100:+.1f}% (${worst['pnl_usd']:+.2f})")

    # ── Active Pool ───────────────────────────────────────────────────────────
    section("ACTIVE SMART-MONEY POOL")
    pool_state_file = os.path.join(os.path.dirname(__file__), "pool_state.json")
    if os.path.exists(pool_state_file):
        with open(pool_state_file) as f:
            pool_data = json.load(f)
        pool = pool_data.get("pool", [])
        if pool:
            updated = pool_data.get("updated_at", "?")[:19].replace("T", " ")
            budget = cfg.get("pool", {}).get("daily_budget_usd", "?")
            print(f"  Pool size: {len(pool)} members  |  Daily budget: ${budget}  |  Last vetted: {updated}")
            print(f"  {'Rank':<5} {'Politician':<12} {'Party':<12} {'Weight':>7} {'Score':>6} {'WR':>5} {'α':>6} {'Status'}")
            print(f"  {'─'*60}")
            for p in pool:
                m = p.get("metrics") or {}
                wr = (m.get("win_rate") or 0) * 100
                al = (m.get("avg_alpha") or 0) * 100
                status = "PROBATION" if p.get("is_probationary") else "FULL"
                print(f"  #{p['rank']:<4} {p['politician_id']:<12} {p.get('party','?'):<12} "
                      f"{p['weight']*100:>6.0f}% {p['score']:>6.3f} {wr:>4.0f}% {al:>+5.1f}% {status}")
        else:
            print("  Pool is empty. Run politician_vetter.py to populate.")
    else:
        print("  No pool state yet. Run politician_vetter.py first.")

    # ── Strategy config ───────────────────────────────────────────────────────
    section("CURRENT STRATEGY PARAMS")
    if cfg:
        cc  = cfg.get("capitol_copier", {})
        pl  = cfg.get("pool", {})
        tsl = cfg.get("tsla", {})
        print(f"  Capitol Copier : pool budget ${pl.get('daily_budget_usd','?')}/day  |  exposure cap {pl.get('max_total_exposure_pct',0)*100:.0f}%")
        print(f"  TSLA           : stop {tsl.get('stop_loss_pct',0)*100:.0f}%  |  trail trigger +{tsl.get('trailing_trigger_pct',0)*100:.0f}%  |  trail {tsl.get('trail_pct',0)*100:.0f}% below")
        print(f"  Config version : v{cfg.get('version','?')}  last updated {cfg.get('last_updated','?')} by {cfg.get('updated_by','?')}")

    print(f"\n{'━'*W}\n")


if __name__ == "__main__":
    run()
