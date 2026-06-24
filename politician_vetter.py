"""
Politician Vetter — monthly scoring & pool selection
======================================================
Scrapes ALL active politicians from Capitol Trades, backtests each candidate via
backtest_engine, scores with multi-factor formula, and writes the top 5 to
pool_state.json. Logs every decision to vetting_log.json.

CLI usage:
  python3 politician_vetter.py                 # full run, updates pool_state.json
  python3 politician_vetter.py --dry-run       # scores everyone, doesn't write pool
  python3 politician_vetter.py --limit 20      # only vet top-20 most active (faster)
"""

import os, json, re, math, time, argparse, statistics
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings()

import backtest_engine as be
import politician_history as ph

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

CT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept":     "text/x-component",
    "RSC":        "1",
}

CONFIG_FILE       = os.path.join(os.path.dirname(__file__), "strategy_config.json")
UNIVERSE_FILE     = os.path.join(os.path.dirname(__file__), "politician_universe.json")
POOL_FILE         = os.path.join(os.path.dirname(__file__), "pool_state.json")
VETTING_LOG       = os.path.join(os.path.dirname(__file__), "vetting_log.json")
BACKTEST_RESULTS  = os.path.join(os.path.dirname(__file__), "backtest_results.json")

# Sector mapping for diversification scoring — single source of truth in sectors.py
from sectors import TICKER_SECTOR


# ── Config & state I/O ───────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_universe():
    if os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE) as f:
            return json.load(f)
    return {"politicians": [], "scanned_at": None}


def save_universe(universe):
    universe["scanned_at"] = datetime.now(timezone.utc).isoformat()
    with open(UNIVERSE_FILE, "w") as f:
        json.dump(universe, f, indent=2)


def load_pool():
    if os.path.exists(POOL_FILE):
        with open(POOL_FILE) as f:
            return json.load(f)
    return {"pool": [], "updated_at": None}


def save_pool(pool):
    pool["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(POOL_FILE, "w") as f:
        json.dump(pool, f, indent=2)


def append_vetting_log(entry):
    log = {"runs": []}
    if os.path.exists(VETTING_LOG):
        with open(VETTING_LOG) as f:
            try:    log = json.load(f)
            except: pass
    log["runs"].append(entry)
    with open(VETTING_LOG, "w") as f:
        json.dump(log, f, indent=2)


# ── Universe scan ─────────────────────────────────────────────────────────────

def scan_universe(min_recency_days=60, min_trades=3, max_politicians=50):
    """
    Scrape Capitol Trades politicians sorted by trade count, filter to
    actively-trading equity traders. Returns list of dicts: {id, party, trades, last_trade}
    """
    print(f"Scanning Capitol Trades politicians (max {max_politicians})...")
    universe = []
    today = datetime.now(timezone.utc).date()

    # Capitol Trades caps per_page at 96, so paginate to get more
    for page in range(1, 4):  # up to ~300 politicians across 3 pages
        url = (f"https://www.capitoltrades.com/politicians"
               f"?per_page=96&sort=-stats.countTrades&page={page}")
        try:
            r = requests.get(url, headers={**CT_HEADERS, "Next-Url": "/politicians"},
                             verify=False, timeout=20)
            c = r.text
        except Exception as e:
            print(f"  Page {page} fetch error: {e}")
            continue

        ids       = re.findall(r'entity--politician id--([A-Z]\d+)',           c)
        parties   = re.findall(r'party--(\w+) flavour--compact',               c)
        trades    = re.findall(r'cell--count-trades.*?q-value.*?"children":"(\d+)"', c)
        vols      = re.findall(r'cell--volume.*?q-value.*?"children":"([^"]+)"', c)
        last_t    = re.findall(r'Last Traded.*?"children":"(\d{4}-\d{2}-\d{2})"', c)

        if not ids:
            break

        for i in range(len(ids)):
            try:
                pid       = ids[i]
                party     = parties[i] if i < len(parties) else "?"
                trade_ct  = int(trades[i]) if i < len(trades) else 0
                volume    = vols[i] if i < len(vols) else "?"
                lt_str    = last_t[i] if i < len(last_t) else None
                if not lt_str:
                    continue
                lt        = datetime.strptime(lt_str, "%Y-%m-%d").date()
                days_ago  = (today - lt).days

                if days_ago > min_recency_days:    continue
                if trade_ct < min_trades:           continue

                universe.append({
                    "id":         pid,
                    "party":      party,
                    "trades":     trade_ct,
                    "volume":     volume,
                    "last_trade": lt_str,
                    "days_since_last": days_ago,
                })
            except (IndexError, ValueError):
                continue

        if len(universe) >= max_politicians:
            break

    # Dedup by id
    seen = set()
    deduped = []
    for p in universe:
        if p["id"] not in seen:
            seen.add(p["id"])
            deduped.append(p)

    print(f"  Found {len(deduped)} actively-trading politicians")
    return deduped[:max_politicians]


# ── Scoring ──────────────────────────────────────────────────────────────────

def sector_diversification_score(scored_trades):
    """0..1 score: higher when buys are spread across multiple sectors."""
    sectors = []
    for t in scored_trades:
        if t["tx_type"] != "buy":
            continue
        sec = TICKER_SECTOR.get(t["ticker"], "unknown")
        sectors.append(sec)
    if not sectors:
        return 0.0
    unique = len(set(sectors))
    # Penalize "unknown" tickers
    unknown_ratio = sectors.count("unknown") / len(sectors)
    return min(1.0, unique / 6.0) * (1.0 - unknown_ratio * 0.5)


def activity_consistency_score(politician, metrics):
    """Higher when politician trades regularly (not feast-or-famine)."""
    if not metrics or not metrics.get("n_trades_in_window"):
        return 0.0
    # 90-day window, ideal is 1+ trade per week
    expected = 13  # ~90/7
    actual = metrics["n_trades_in_window"]
    # Cap at expected, then sigmoid
    ratio = min(actual / expected, 1.5)
    return min(1.0, ratio)


def disclosure_speed_score(scored_trades):
    """Higher when politician files trades quickly (smaller gap)."""
    gaps = []
    for t in scored_trades:
        # Estimate gap by computing pub_date - tx_date if we have both
        # We didn't carry tx_date into scored, but reporting_gap is in raw trades
        pass
    # Fallback: use average reporting_gap from universe data if avail
    # For now, return 0.5 (neutral) — TODO: thread reporting_gap into backtest output
    return 0.5


def compute_politician_score(metrics, scored_trades, politician_meta, weights):
    """Compute composite 0..1 score using the 6-factor weighted formula."""
    if not metrics or not metrics.get("n_scored"):
        return 0.0, {"reason": "no_scoreable_trades"}

    # Each component in [0, 1] (or near)
    alpha = metrics.get("avg_alpha") or 0
    # alpha may be slightly negative; normalize with tanh
    alpha_score = (math.tanh(alpha * 20) + 1) / 2  # tanh(0.05*20)≈0.76 → 0.88

    win_rate_score = metrics.get("win_rate") or 0

    disc_speed_score = disclosure_speed_score(scored_trades)
    sector_div_score = sector_diversification_score(scored_trades)
    activity_score   = activity_consistency_score(politician_meta, metrics)
    sample_conf      = metrics.get("sample_confidence") or 0

    components = {
        "realized_return_alpha":   alpha_score,
        "win_rate":                win_rate_score,
        "disclosure_speed":        disc_speed_score,
        "sector_diversification":  sector_div_score,
        "activity_consistency":    activity_score,
        "sample_size_confidence":  sample_conf,
    }

    total = sum(components[k] * weights[k] for k in weights)

    # Apply cool-off penalty if recently removed from pool
    pid = politician_meta.get("id") if politician_meta else None
    if pid:
        penalty_mult = ph.get_cool_off_penalty(pid)
        if penalty_mult < 1.0:
            total *= penalty_mult
            components["cool_off_penalty"] = penalty_mult

    return round(total, 4), components


# ── Pool selection ────────────────────────────────────────────────────────────

def select_pool(scored_politicians, pool_size, rank_weights):
    """
    Pick top N politicians by score, assign weight per rank, return pool list.
    """
    # Filter out zero-score (no data) politicians
    eligible = [p for p in scored_politicians if p["score"] > 0]
    eligible.sort(key=lambda p: p["score"], reverse=True)
    selected = eligible[:pool_size]

    pool = []
    for rank, p in enumerate(selected, start=1):
        weight = rank_weights.get(str(rank), 0.05)
        pool.append({
            "rank":              rank,
            "politician_id":     p["id"],
            "score":             p["score"],
            "weight":            weight,
            "components":        p.get("components"),
            "metrics":           p.get("metrics"),
            "party":             p.get("party"),
            "is_probationary":   rank > 3,
        })
    return pool


# ── Main flow ────────────────────────────────────────────────────────────────

def run_vetting(limit=50, dry_run=False, verbose=False):
    cfg = load_config()
    pool_cfg = cfg["pool"]
    weights  = cfg["scoring_weights"]
    window   = pool_cfg["backtest_window_days"]

    print(f"\n{'='*60}")
    print(f"  POLITICIAN VETTING RUN  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")
    print(f"  Backtest window      : {window}d")
    print(f"  Pool size            : {pool_cfg['max_size']}")
    print(f"  Scoring weights      : {weights}")
    print()

    # Phase A — universe
    universe = scan_universe(max_politicians=limit)
    save_universe({"politicians": universe})

    # Phase B — backtest each
    scored_pols = []
    for i, pol in enumerate(universe, 1):
        pid = pol["id"]
        print(f"  [{i}/{len(universe)}] Backtesting {pid} ({pol['party']}, {pol['trades']} trades total)...", end=" ", flush=True)
        try:
            bt = be.backtest_politician(pid, window_days=window, use_cache=True, verbose=False)
            metrics = bt.get("metrics") or {}
            scored_trades = bt.get("scored") or []
            score, components = compute_politician_score(metrics, scored_trades, pol, weights)
            scored_pols.append({
                "id":         pid,
                "party":      pol["party"],
                "trades":     pol["trades"],
                "last_trade": pol["last_trade"],
                "metrics":    metrics,
                "components": components,
                "score":      score,
            })
            n_scored = metrics.get("n_scored", 0)
            win = metrics.get("win_rate")
            alpha = metrics.get("avg_alpha")
            print(f"score={score:.3f}  n={n_scored}  wr={win*100 if win else 0:.0f}%  α={alpha*100 if alpha else 0:+.1f}%")
        except Exception as e:
            print(f"ERROR: {e}")
            scored_pols.append({
                "id": pid, "party": pol["party"], "trades": pol["trades"],
                "last_trade": pol["last_trade"], "metrics": None,
                "components": None, "score": 0,
                "error": str(e),
            })

    # Phase C — rank
    scored_pols.sort(key=lambda p: p["score"], reverse=True)

    print(f"\n  Top 10 by score:")
    print(f"  {'#':<3} {'ID':<10} {'Party':<12} {'Score':>6} {'N':>4} {'WR':>6} {'α':>7}")
    print(f"  {'─'*55}")
    for i, p in enumerate(scored_pols[:10], 1):
        m = p["metrics"] or {}
        wr = (m.get("win_rate") or 0) * 100
        al = (m.get("avg_alpha") or 0) * 100
        n = m.get("n_scored", 0)
        print(f"  {i:<3} {p['id']:<10} {p['party']:<12} {p['score']:>6.3f} {n:>4} {wr:>5.0f}% {al:>+6.1f}%")

    # Phase D — pool selection
    pool = select_pool(scored_pols, pool_cfg["max_size"], pool_cfg["rank_weights"])

    print(f"\n  SELECTED POOL ({len(pool)} members):")
    print(f"  {'Rank':<5} {'ID':<10} {'Weight':>7} {'Score':>6} {'Status'}")
    print(f"  {'─'*50}")
    for p in pool:
        status = "PROBATION" if p["is_probationary"] else "FULL"
        print(f"  #{p['rank']:<4} {p['politician_id']:<10} {p['weight']*100:>6.0f}% {p['score']:>6.3f}  {status}")

    # Phase E — write pool & log
    if not dry_run:
        old_pool = load_pool().get("pool", [])
        old_ids = {p["politician_id"] for p in old_pool}
        new_ids = {p["politician_id"] for p in pool}
        added   = new_ids - old_ids
        removed = old_ids - new_ids

        save_pool({"pool": pool})
        append_vetting_log({
            "run_at":            datetime.now(timezone.utc).isoformat(),
            "universe_size":     len(universe),
            "pool_size":         len(pool),
            "added":             list(added),
            "removed":           list(removed),
            "top_5":             [{"id": p["politician_id"], "score": p["score"], "rank": p["rank"]} for p in pool],
        })

        if added:    print(f"\n  + Added to pool:   {', '.join(sorted(added))}")
        if removed:  print(f"  - Removed from pool: {', '.join(sorted(removed))}")
        if not added and not removed:
            print(f"\n  Pool unchanged.")

        # Reconcile politician_history.json with the freshly-written pool
        ph.reconcile(pool)
    else:
        print(f"\n  [DRY RUN] No state written")

    print(f"\n{'='*60}\n")
    return pool


def main():
    parser = argparse.ArgumentParser(description="Score and rank politicians, update active pool")
    parser.add_argument("--limit", type=int, default=30, help="Max politicians to vet")
    parser.add_argument("--dry-run", action="store_true", help="Don't write state files")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    run_vetting(limit=args.limit, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
