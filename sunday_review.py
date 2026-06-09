"""
Sunday Review — Self-Evolving Strategy Optimizer
=================================================
Runs weekly (Sunday 6 PM ET via scheduler, or manually).

What it does:
  1. Syncs performance log from Alpaca fills
  2. Scores both strategies (TSLA + Capitol Copier)
  3. Re-ranks Capitol Trades politicians by recent activity
  4. Applies parameter adjustments based on outcomes
  5. Writes updated strategy_config.json
  6. Prints a full weekly report

Adjustment rules:
  TSLA:
    - Win rate < 40%       → reduce position size 20%
    - Stop triggered & price recovered in 5d → widen stop by +1%
    - Trailing never active in 3w → lower trigger by -1%
    - Win rate > 65% for 4w → tighten trail by -0.5% (capture more)

  Capitol Copier:
    - Win rate > 65%       → increase trade size by $100 (max $3,000)
    - Win rate < 40%       → decrease trade size by $100 (min $500)
    - Avg lag > 35d        → reduce trade size 10% (signals too stale)
    - Politician score < 0.45 → rescan for better politician
"""

import os, json, re, requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = "https://data.alpaca.markets/v2"
HEADERS    = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "strategy_config.json")
REVIEW_LOG  = os.path.join(os.path.dirname(__file__), "review_log.json")

CT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/x-component", "RSC": "1",
}


# ── Config I/O ────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(cfg, reason):
    cfg["version"]    += 1
    cfg["last_updated"] = datetime.utcnow().strftime("%Y-%m-%d")
    cfg["updated_by"]   = f"sunday_review: {reason}"
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def load_review_log():
    if os.path.exists(REVIEW_LOG):
        with open(REVIEW_LOG) as f:
            return json.load(f)
    return {"reviews": []}


def save_review_log(log):
    with open(REVIEW_LOG, "w") as f:
        json.dump(log, f, indent=2)


# ── Performance data ──────────────────────────────────────────────────────────

def get_metrics():
    """Import and run performance_tracker to get current metrics."""
    import performance_tracker as pt
    log = pt.sync(verbose=False)
    return {
        "all":            pt.summarise(log, last_days=30),
        "capitol_copier": pt.summarise(log, strategy="capitol_copier", last_days=30),
        "tsla_strategy":  pt.summarise(log, strategy="tsla_strategy", last_days=30),
        "capitol_90d":    pt.summarise(log, strategy="capitol_copier", last_days=90),
        "raw_trades":     log["trades"],
    }


def get_spy_weekly_return():
    """SPY return over the last 7 days as benchmark."""
    try:
        end   = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=8)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{DATA_URL}/stocks/SPY/bars",
            headers=HEADERS,
            params={"timeframe": "1Day", "start": start, "end": end, "limit": 10}
        )
        bars = r.json().get("bars", [])
        if len(bars) >= 2:
            return round((bars[-1]["c"] - bars[0]["o"]) / bars[0]["o"] * 100, 3)
    except:
        pass
    return None


# ── Capitol Trades politician re-ranking ──────────────────────────────────────

def rescan_politicians():
    """
    Re-scrape Capitol Trades, rank politicians by:
      score = win_rate×0.35 + avg_return×0.35 + recency×0.15 + activity×0.15
    Since we don't have win_rate per politician yet, use trade count + recency as proxy.
    Returns top 5 politicians with their IDs.
    """
    try:
        r = requests.get(
            "https://www.capitoltrades.com/politicians?per_page=96&sort=-stats.countTrades&page=1",
            headers={**CT_HEADERS, "Next-Url": "/politicians"},
            verify=False, timeout=20
        )
        c = r.text
        ids      = re.findall(r'entity--politician id--([A-Z]\d+)', c)
        parties  = re.findall(r'party--(\w+) flavour--compact', c)
        trades   = re.findall(r'cell--count-trades.*?q-value.*?"children":"(\d+)"', c)
        vols     = re.findall(r'cell--volume.*?q-value.*?"children":"([^"]+)"', c)
        last_t   = re.findall(r'Last Traded.*?"children":"(\d{4}-\d{2}-\d{2})"', c)

        politicians = []
        today = datetime.utcnow().date()
        for i in range(min(len(ids), 20)):
            # Skip if no equity trades (volume but no trade count)
            tc  = int(trades[i]) if i < len(trades) else 0
            vol = vols[i] if i < len(vols) else "0"
            lt  = last_t[i] if i < len(last_t) else "2020-01-01"

            # Recency score: traded within last 60 days scores 1.0, older decays
            days_ago = (today - datetime.strptime(lt, "%Y-%m-%d").date()).days
            recency  = max(0, 1 - days_ago / 60)

            # Activity score: log-normalised trade count
            import math
            activity = math.log10(max(tc, 1)) / 3.0  # 1000 trades → 1.0

            # Combined score (no P&L data yet for politicians, use activity+recency)
            score = recency * 0.50 + activity * 0.50

            politicians.append({
                "id":       ids[i],
                "party":    parties[i] if i < len(parties) else "?",
                "trades":   tc,
                "volume":   vol,
                "last_trade": lt,
                "recency":  round(recency, 3),
                "activity": round(activity, 3),
                "score":    round(score, 3),
            })

        # Sort by score, filter to those who traded recently (< 60 days)
        active = [p for p in politicians if p["recency"] > 0]
        active.sort(key=lambda x: x["score"], reverse=True)
        return active[:5]

    except Exception as e:
        print(f"  [RESCAN] Error: {e}")
        return []


# ── Parameter adjustment engine ───────────────────────────────────────────────

def adjust_capitol_copier(cfg, metrics, changes):
    """Adjust pool daily budget based on aggregate Capitol Copier performance."""
    pool_cfg = cfg["pool"]
    rev  = cfg["review"]
    m    = metrics["capitol_copier"]
    m90  = metrics["capitol_90d"]

    if m["n_trades"] == 0:
        changes.append("Capitol Copier: no closed trades yet — holding pool budget")
        return False

    wr   = m["win_rate"]
    budget = pool_cfg["daily_budget_usd"]
    step = rev["trade_size_step_usd"]
    max_b = 5000
    min_b = 500

    if wr > rev["target_win_rate"]:
        new_budget = min(budget + step * 2, max_b)
        if new_budget != budget:
            pool_cfg["daily_budget_usd"] = new_budget
            changes.append(f"Pool: win rate {wr*100:.1f}% > {rev['target_win_rate']*100:.0f}% → daily budget ${budget} → ${new_budget}")

    elif wr < rev["min_win_rate_threshold"]:
        new_budget = max(budget - step * 2, min_b)
        if new_budget != budget:
            pool_cfg["daily_budget_usd"] = new_budget
            changes.append(f"Pool: win rate {wr*100:.1f}% < {rev['min_win_rate_threshold']*100:.0f}% → daily budget ${budget} → ${new_budget}")

    # Trigger emergency re-vet if pool win rate cratering
    if m90["n_trades"] >= 5 and m90["win_rate"] < pool_cfg["auto_pause_win_rate"]:
        changes.append(f"⚠ Pool: 90d win rate {m90['win_rate']*100:.1f}% < auto-pause threshold → emergency re-vet")
        return True
    return False


def should_revet_monthly():
    """True if this Sunday is the 1st Sunday of the month."""
    today = datetime.utcnow().date()
    if today.weekday() != 6:  # 6 = Sunday
        return False
    return today.day <= 7


def trigger_revet():
    """Run politician_vetter as a subprocess (or import-and-call). Returns log entry."""
    try:
        import politician_vetter as pv
        result = pv.run_vetting(limit=30, dry_run=False, verbose=False)
        return {"status": "ok", "pool_size": len(result) if result else 0}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def rebalance_pool_weights(cfg):
    """Re-rank existing pool members by 14d momentum with smoothing + graduation.

    Improvements over naive rebalance:
      1. Smoothing — weight changes capped at ±15% per week
      2. Graduation — a probation slot that beats a full slot for 2 consecutive weeks
                       gets promoted (swaps positions with the weakest full slot)
      3. Memory     — calls politician_history.tick_week() to track tenure
    """
    try:
        import pool_manager
        import backtest_engine as be
        import politician_history as ph

        pool = pool_manager.get_pool()
        if not pool:
            return None

        max_weight_change = cfg["pool"].get("max_weight_change_per_week", 0.15)
        graduation_weeks  = ph.GRADUATION_WEEKS

        # Quick 14d backtest for each pool member
        scored = []
        for member in pool:
            pid = member["politician_id"]
            try:
                bt = be.backtest_politician(pid, window_days=14, use_cache=True, verbose=False)
                m = bt.get("metrics") or {}
                recent_score = (m.get("avg_alpha") or 0) + (m.get("win_rate") or 0) * 0.5
            except Exception:
                recent_score = -999
            scored.append({"member": member, "recent_score": recent_score})

        # Sort by recent momentum
        scored.sort(key=lambda x: x["recent_score"], reverse=True)

        # ── Graduation check ───────────────────────────────────────────────
        # A probationary member who outscores a full-slot member AND has
        # served ≥ graduation_weeks consecutive weeks on probation can swap.
        rank_weights = cfg["pool"]["rank_weights"]
        full_slot_count = 3

        graduations = []
        for i in range(full_slot_count, len(scored)):  # probation slots 4..n
            probation_item = scored[i]
            pid = probation_item["member"]["politician_id"]
            weeks = ph.get_weeks_on_probation(pid)
            if weeks < graduation_weeks:
                continue
            # Check if they outscore any full-slot member
            for j in range(full_slot_count):
                full_item = scored[j]
                if probation_item["recent_score"] > full_item["recent_score"]:
                    # Swap them in the sorted list
                    scored[i], scored[j] = scored[j], scored[i]
                    graduations.append({
                        "promoted": pid,
                        "demoted":  full_item["member"]["politician_id"],
                        "weeks_served": weeks,
                    })
                    break

        # ── Smoothing: cap weight changes ──────────────────────────────────
        new_pool = []
        for rank, item in enumerate(scored, start=1):
            m = item["member"]
            current_weight = m.get("weight", 0.05)
            target_weight  = rank_weights.get(str(rank), 0.05)
            # Clamp change
            delta = target_weight - current_weight
            if delta > max_weight_change:
                smoothed = current_weight + max_weight_change
            elif delta < -max_weight_change:
                smoothed = current_weight - max_weight_change
            else:
                smoothed = target_weight
            m["rank"] = rank
            m["weight"] = round(smoothed, 4)
            m["target_weight"] = target_weight     # informational
            m["is_probationary"] = rank > full_slot_count
            new_pool.append(m)

        # ── Persist new pool ───────────────────────────────────────────────
        pool_file = os.path.join(os.path.dirname(__file__), "pool_state.json")
        with open(pool_file, "w") as f:
            json.dump({
                "pool": new_pool,
                "updated_at": datetime.utcnow().isoformat(),
                "rebalanced_by": "weekly_momentum",
                "graduations": graduations,
                "smoothing_cap": max_weight_change,
            }, f, indent=2)

        # ── Update history (tenure tracking) ───────────────────────────────
        ph.tick_week(new_pool)

        return {"pool": new_pool, "graduations": graduations}
    except Exception as e:
        return {"error": str(e)}


def adjust_tsla(cfg, metrics, changes):
    tsla = cfg["tsla"]
    rev  = cfg["review"]
    m    = metrics["tsla_strategy"]

    if m["n_trades"] == 0:
        changes.append("TSLA: no closed trades yet — holding all params")
        return

    wr = m["win_rate"]

    # Win rate too low → tighten, protect capital
    if wr < rev["min_win_rate_threshold"] and m["n_trades"] >= 3:
        new_stop = round(min(tsla["stop_loss_pct"] - rev["stop_loss_step_pct"], 0.15), 3)
        changes.append(f"TSLA: low win rate {wr*100:.1f}% → stop loss {tsla['stop_loss_pct']*100:.0f}% → {new_stop*100:.0f}% (tighter protection)")
        tsla["stop_loss_pct"] = new_stop

    # Win rate strong → give trades more room to breathe
    if wr > 0.65 and m["n_trades"] >= 5:
        new_trail = round(max(tsla["trail_pct"] - 0.005, 0.03), 3)
        if new_trail != tsla["trail_pct"]:
            changes.append(f"TSLA: strong win rate {wr*100:.1f}% → tighten trail {tsla['trail_pct']*100:.1f}% → {new_trail*100:.1f}% (capture more)")
            tsla["trail_pct"] = new_trail

    # Trailing trigger: if Sharpe is poor, lower trigger so trailing activates sooner
    if m["sharpe"] < rev["min_sharpe"] and m["n_trades"] >= 3:
        new_trigger = round(max(tsla["trailing_trigger_pct"] - rev["trailing_trigger_step_pct"], 0.05), 3)
        if new_trigger != tsla["trailing_trigger_pct"]:
            changes.append(f"TSLA: low Sharpe {m['sharpe']:.2f} → lower trailing trigger {tsla['trailing_trigger_pct']*100:.0f}% → {new_trigger*100:.0f}%")
            tsla["trailing_trigger_pct"] = new_trigger


# ── Weekly report ─────────────────────────────────────────────────────────────

def print_report(metrics, spy_return, changes, new_politician, politicians):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    sep = "═" * 58

    print(f"\n{sep}")
    print(f"  SUNDAY STRATEGY REVIEW  —  {now}")
    print(sep)

    # SPY benchmark
    if spy_return is not None:
        print(f"  SPY this week: {spy_return:+.2f}%")

    # Capitol Copier
    m = metrics["capitol_copier"]
    print(f"\n  CAPITOL COPIER (30d)")
    if m["n_trades"] == 0:
        print("    No closed trades yet — positions still open")
    else:
        print(f"    Trades: {m['n_trades']}  |  Win rate: {m['win_rate']*100:.1f}%  |  Avg: {m['avg_return_pct']:+.2f}%")
        print(f"    P&L: ${m['total_pnl_usd']:+,.2f}  |  Profit factor: {m['profit_factor']:.2f}  |  Sharpe: {m['sharpe']:.2f}")
        if spy_return and m["n_trades"] > 0:
            alpha = m["avg_return_pct"] - spy_return / 4  # approx weekly
            print(f"    vs SPY alpha: {alpha:+.2f}%")

    # TSLA
    t = metrics["tsla_strategy"]
    print(f"\n  TSLA STRATEGY (30d)")
    if t["n_trades"] == 0:
        print("    No closed trades yet")
    else:
        print(f"    Trades: {t['n_trades']}  |  Win rate: {t['win_rate']*100:.1f}%  |  Avg: {t['avg_return_pct']:+.2f}%")
        print(f"    P&L: ${t['total_pnl_usd']:+,.2f}  |  Profit factor: {t['profit_factor']:.2f}  |  Sharpe: {t['sharpe']:.2f}")

    # Politician ranking
    if politicians:
        print(f"\n  TOP POLITICIANS (rescan)")
        print(f"    {'ID':<12} {'Party':<12} {'Trades':>7} {'Last Trade':<12} {'Score'}")
        print(f"    {'-'*55}")
        for p in politicians[:5]:
            marker = " ← CURRENT" if p["id"] == "K000389" else ""
            print(f"    {p['id']:<12} {p['party']:<12} {p['trades']:>7} {p['last_trade']:<12} {p['score']:.3f}{marker}")

    if new_politician:
        print(f"\n  ⚡ POLITICIAN SWITCH: → {new_politician['id']} (score {new_politician['score']:.3f})")

    # Changes
    print(f"\n  PARAMETER CHANGES THIS WEEK")
    if changes:
        for c in changes:
            print(f"    • {c}")
    else:
        print("    No changes — all params holding")

    print(f"\n{sep}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print("Loading config and performance data...")
    cfg     = load_config()
    metrics = get_metrics()
    spy     = get_spy_weekly_return()
    changes = []

    # Rescan politicians (for awareness — pool managed separately)
    politicians = rescan_politicians()

    # Adjust pool daily budget based on aggregate Capitol Copier P&L
    needs_emergency_revet = adjust_capitol_copier(cfg, metrics, changes)

    # Monthly full re-vetting (1st Sunday of month) OR emergency re-vet
    new_politician = None
    if should_revet_monthly() or needs_emergency_revet:
        reason = "monthly cycle" if should_revet_monthly() else "emergency (cratering win rate)"
        changes.append(f"Full re-vetting triggered: {reason}")
        revet_result = trigger_revet()
        changes.append(f"Re-vet result: {revet_result}")
    else:
        # Weekly: rebalance weights of existing pool members based on 14d momentum
        rebal_result = rebalance_pool_weights(cfg)
        if rebal_result and isinstance(rebal_result, dict) and rebal_result.get("error"):
            changes.append(f"Pool rebalance error: {rebal_result['error']}")
        elif rebal_result and isinstance(rebal_result, dict):
            pool_out = rebal_result.get("pool") or []
            grads = rebal_result.get("graduations") or []
            changes.append(f"Pool weights smoothed ({len(pool_out)} members, 15% max change per week)")
            for g in grads:
                changes.append(f"⬆ Graduation: {g['promoted']} (after {g['weeks_served']}w probation) → swaps in for {g['demoted']}")

    # Adjust TSLA params
    adjust_tsla(cfg, metrics, changes)

    # Save updated config
    if changes and any("→" in c for c in changes):
        reason = "; ".join(changes[:2])
        save_config(cfg, reason)
        print(f"  Config updated (v{cfg['version']})")
    else:
        print("  Config unchanged")

    # Save review log
    log = load_review_log()
    log["reviews"].append({
        "date":        datetime.utcnow().strftime("%Y-%m-%d"),
        "spy_return":  spy,
        "metrics":     {k: v for k, v in metrics.items() if k != "raw_trades"},
        "changes":     changes,
        "new_politician": new_politician["id"] if new_politician else None,
    })
    save_review_log(log)

    # Print full report
    print_report(metrics, spy, changes, new_politician, politicians)


if __name__ == "__main__":
    run()
