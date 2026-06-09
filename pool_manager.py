"""
Pool Manager — allocation logic for the active politician pool
================================================================
Reads pool_state.json and provides per-politician trade sizing.

Used by capitol_copier.py to size each copied trade according to the politician's
pool rank, with consensus boost when multiple pool members buy the same ticker.

Functions:
  get_pool()                         → list of {politician_id, weight, rank, ...}
  get_trade_size(politician_id, ...) → USD amount for a copied trade
  detect_consensus(ticker, recent_trades) → bool, did 2+ pool members buy same ticker recently?
  is_in_pool(politician_id)          → bool
  pool_member_ids()                  → set of pool member IDs
"""

import os, json
from datetime import datetime, timedelta, timezone

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "strategy_config.json")
POOL_FILE   = os.path.join(os.path.dirname(__file__), "pool_state.json")


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_pool():
    if not os.path.exists(POOL_FILE):
        return []
    with open(POOL_FILE) as f:
        data = json.load(f)
    return data.get("pool", [])


def pool_member_ids():
    return {p["politician_id"] for p in get_pool()}


def is_in_pool(politician_id):
    return politician_id in pool_member_ids()


def get_pool_member(politician_id):
    for p in get_pool():
        if p["politician_id"] == politician_id:
            return p
    return None


def get_trade_size(politician_id, base_budget_usd=None, sector_multiplier=1.0):
    """Compute trade size in USD for a politician.

    size = base_budget * pool_weight * sector_multiplier, clamped to [min, max].
    Returns 0 if politician not in pool.
    """
    cfg = load_config()
    pool_cfg = cfg["pool"]

    if base_budget_usd is None:
        base_budget_usd = pool_cfg["daily_budget_usd"]

    member = get_pool_member(politician_id)
    if not member:
        return 0

    weight = member.get("weight", 0)
    raw_size = base_budget_usd * weight * sector_multiplier
    return max(pool_cfg["min_position_usd"],
               min(pool_cfg["max_position_usd"], raw_size))


def detect_consensus(ticker, all_pool_buys, window_days=None):
    """
    Check if 2+ pool members have bought the same ticker within the consensus window.

    Args:
      ticker: the ticker we're about to buy
      all_pool_buys: list of dicts [{politician_id, ticker, pub_date, ...}] from current scan
      window_days: override config (default uses config consensus_window_days)

    Returns dict with {is_consensus: bool, members: [politician_ids], multiplier: float}
    """
    cfg = load_config()
    if window_days is None:
        window_days = cfg["pool"]["consensus_window_days"]

    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    members_buying = set()
    for t in all_pool_buys:
        if t["ticker"] == ticker and t.get("pub_date", "") >= cutoff:
            members_buying.add(t["politician_id"])

    is_cons = len(members_buying) >= 2
    return {
        "is_consensus": is_cons,
        "members":      list(members_buying),
        "n_members":    len(members_buying),
        "multiplier":   cfg["pool"]["consensus_boost_multiplier"] if is_cons else 1.0,
    }


def get_consensus_boost_multiplier():
    return load_config()["pool"]["consensus_boost_multiplier"]


def summarize_pool():
    """Pretty-print the current pool for CLI use."""
    pool = get_pool()
    if not pool:
        print("Pool is empty. Run politician_vetter.py to populate it.")
        return
    cfg = load_config()
    budget = cfg["pool"]["daily_budget_usd"]
    print(f"\n  ACTIVE POOL  (daily budget ${budget})")
    print(f"  {'Rank':<5} {'ID':<10} {'Weight':>7} {'Trade $':>9} {'Score':>6} {'Status'}")
    print(f"  {'─'*55}")
    for p in pool:
        size = get_trade_size(p["politician_id"])
        status = "PROBATION" if p.get("is_probationary") else "FULL"
        print(f"  #{p['rank']:<4} {p['politician_id']:<10} {p['weight']*100:>6.0f}% ${size:>7.0f} {p['score']:>6.3f}  {status}")
    print()


if __name__ == "__main__":
    summarize_pool()
