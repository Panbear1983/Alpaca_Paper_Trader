"""
daily_briefing.py — morning AI briefing pushed to Telegram.

Reads all trading state files, feeds the data to gemma4:26b via hermes_client,
gets a conversational summary, posts it to your Telegram (iOS push notification).

Runs at 8 AM ET weekdays via launchd.

Cost: $0 (local LLM only). Latency: 30–90s on gemma4:26b cold, 5–15s warm.
"""

import os, json, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

import hermes_client as llm
import telegram_notifier as tg

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = "https://data.alpaca.markets/v2"
H = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}

ROOT = os.path.dirname(__file__)


def _load_json(name, default=None):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def alpaca_account():
    r = requests.get(f"{BASE_URL}/account", headers=H, timeout=10)
    return r.json() if r.status_code == 200 else {}


def alpaca_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=H, timeout=10)
    return r.json() if r.status_code == 200 else []


def alpaca_clock():
    r = requests.get(f"{BASE_URL}/clock", headers=H, timeout=10)
    return r.json() if r.status_code == 200 else {}


def spy_yesterday():
    try:
        end = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=4)).strftime("%Y-%m-%d")
        r = requests.get(f"{DATA_URL}/stocks/SPY/bars",
                         headers=H,
                         params={"timeframe":"1Day","start":start,"end":end,
                                 "limit":5,"feed":"iex"}, timeout=10)
        bars = r.json().get("bars", [])
        if len(bars) >= 2:
            ystr = bars[-1]
            return (ystr["c"] - ystr["o"]) / ystr["o"] * 100
    except Exception:
        pass
    return None


def gather_state():
    """Snapshot of everything the LLM needs to write a good briefing."""
    account   = alpaca_account()
    positions = alpaca_positions()
    if not isinstance(positions, list):
        positions = []

    pool      = _load_json("pool_state.json", {"pool": []}).get("pool", [])
    perf      = _load_json("performance_log.json", {"trades": []}).get("trades", [])
    history   = _load_json("politician_history.json", {"politicians": {}})

    # Yesterday's closed trades
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_trades = [t for t in perf if t.get("exit_date", "") >= yesterday]

    return {
        "account":  account,
        "positions": positions,
        "pool":     pool,
        "yesterday_trades": yesterday_trades,
        "total_closed_trades": len(perf),
        "history_size": len(history.get("politicians", {})),
        "clock":    alpaca_clock(),
        "spy_y":    spy_yesterday(),
    }


def build_prompt(state):
    """Construct the prompt for gemma4:26b."""
    account = state["account"]
    positions = state["positions"]
    pool = state["pool"]
    yt = state["yesterday_trades"]

    # Account summary
    equity = float(account.get("equity", 0))
    last_eq = float(account.get("last_equity", equity))
    daily_pnl = equity - last_eq
    daily_pct = (daily_pnl / last_eq * 100) if last_eq else 0

    # Positions summary
    pos_lines = []
    total_unreal = 0
    for p in positions:
        sym  = p["symbol"]
        upl  = float(p.get("unrealized_pl", 0))
        uplp = float(p.get("unrealized_plpc", 0)) * 100
        total_unreal += upl
        pos_lines.append(f"  - {sym}: ${upl:+.2f} ({uplp:+.1f}%)")

    # Pool summary
    pool_lines = []
    for p in pool:
        win = (p.get("metrics") or {}).get("win_rate", 0) * 100
        al  = (p.get("metrics") or {}).get("avg_alpha", 0) * 100
        prob = " [probation]" if p.get("is_probationary") else ""
        pool_lines.append(
            f"  - #{p['rank']} {p['politician_id']} ({p.get('party','?')}): "
            f"weight {p['weight']*100:.0f}%, score {p['score']:.3f}, "
            f"win {win:.0f}%, α {al:+.1f}%{prob}"
        )

    # Yesterday's closed trades
    yt_lines = []
    for t in yt:
        yt_lines.append(f"  - {t['symbol']}: {t['return_pct']*100:+.1f}% "
                       f"(${t['pnl_usd']:+.2f}) {t.get('strategy','?')}")

    market_status = "OPEN" if state["clock"].get("is_open") else "CLOSED"
    spy_str = f"{state['spy_y']:+.2f}%" if state["spy_y"] is not None else "n/a"

    prompt = f"""You are the Claude Trader morning briefing assistant. Generate a concise, conversational
trading briefing (4-6 short paragraphs, no markdown headers, plain English).

CONTEXT (today is {datetime.utcnow().strftime('%Y-%m-%d')}):

Account:
  Equity: ${equity:,.2f}
  Daily P&L: ${daily_pnl:+,.2f} ({daily_pct:+.2f}%)
  SPY yesterday: {spy_str}
  Market: {market_status}

Open positions ({len(positions)}, unrealized: ${total_unreal:+,.2f}):
{chr(10).join(pos_lines) if pos_lines else '  (none)'}

Active politician pool ({len(pool)} members):
{chr(10).join(pool_lines) if pool_lines else '  (empty)'}

Closed trades yesterday ({len(yt)}):
{chr(10).join(yt_lines) if yt_lines else '  (none)'}

Historical context: {state['total_closed_trades']} total closed trades, tracking {state['history_size']} politicians.

YOUR JOB:
Write a 4-6 paragraph briefing covering:
1. How yesterday went (P&L, vs SPY)
2. Notable positions (biggest winners/losers right now)
3. Pool status (any concerns? Anyone underperforming?)
4. One thing to watch today
5. (Optional) a short closing thought

Keep it under 200 words. Conversational tone, like a smart trading buddy briefing you over coffee.
Do NOT use markdown headers or bullet lists. Use short paragraphs.
"""
    return prompt


def main():
    if not llm.is_alive():
        tg.notify_error("daily_briefing", "Ollama is not running — start it first")
        print("[daily_briefing] Ollama not running, cannot generate briefing")
        return 1

    state = gather_state()
    prompt = build_prompt(state)

    # Use hermes3:8b by default — faster, fits launchd timeout. gemma4:26b
    # can be enabled by setting BRIEFING_MODEL env var if you want depth over speed.
    model = os.getenv("BRIEFING_MODEL", "hermes3:8b")
    print(f"[daily_briefing] Asking {model} for briefing...")
    briefing = llm.ask(prompt, model=model, max_tokens=600, timeout=180)

    if briefing.startswith("[hermes_client error]"):
        tg.notify_error("daily_briefing", briefing)
        print(f"[daily_briefing] LLM error: {briefing}")
        return 1

    print("[daily_briefing] Briefing generated:")
    print("─" * 60)
    print(briefing)
    print("─" * 60)

    ok = tg.notify_daily_briefing(briefing)
    if ok:
        print("[daily_briefing] sent to Telegram ✓")
    else:
        print("[daily_briefing] Telegram not configured — printed locally only")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
