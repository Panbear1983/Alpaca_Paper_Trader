import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daily_metrics import compute_daily_metrics


def test_empty_or_single_point():
    res = compute_daily_metrics([], [])
    assert res["total_days"] == 0
    assert res["win_days"] == 0
    assert res["loss_days"] == 0

    res_single = compute_daily_metrics([1700000000], [10000.0])
    assert res_single["total_days"] == 0


def test_perfect_win_streak():
    ts = [1700000000 + i * 86400 for i in range(4)]
    eq = [1000.0, 1100.0, 1200.0, 1300.0]
    res = compute_daily_metrics(ts, eq)

    assert res["total_days"] == 3
    assert res["win_days"] == 3
    assert res["loss_days"] == 0
    assert res["win_rate"] == 100.0
    assert res["win_loss_ratio"] == float("inf")
    assert res["daily_profit_factor"] == float("inf")
    assert res["max_win_streak"] == 3
    assert res["max_loss_streak"] == 0
    assert res["current_streak"]["type"] == "win"
    assert res["current_streak"]["length"] == 3


def test_mixed_win_loss_metrics():
    ts = [1700000000 + i * 86400 for i in range(5)]
    # Day 1: +100 (+10%), Day 2: -50 (-4.55%), Day 3: +150 (+14.29%), Day 4: -20 (-1.67%)
    eq = [1000.0, 1100.0, 1050.0, 1200.0, 1180.0]
    res = compute_daily_metrics(ts, eq)

    assert res["total_days"] == 4
    assert res["win_days"] == 2
    assert res["loss_days"] == 2
    assert res["win_rate"] == 50.0
    assert res["loss_rate"] == 50.0
    assert res["win_loss_ratio"] == 1.0
    assert res["total_gains"] == 250.0
    assert res["total_losses"] == 70.0
    assert res["net_pnl"] == 180.0
    assert res["daily_profit_factor"] == round(250.0 / 70.0, 2)
    assert res["avg_win_dollar"] == 125.0
    assert res["avg_loss_dollar"] == 35.0
    assert res["payoff_ratio"] == round(125.0 / 35.0, 2)
    assert res["max_win_streak"] == 1
    assert res["max_loss_streak"] == 1
    assert res["current_streak"]["type"] == "loss"
    assert res["current_streak"]["length"] == 1


def test_filters_unfunded_zero_days():
    ts = [1700000000 + i * 86400 for i in range(5)]
    eq = [0.0, 0.0, 5000.0, 5100.0, 4950.0]
    res = compute_daily_metrics(ts, eq)

    # Only 5000 -> 5100 -> 4950 are valid days (2 returns)
    assert res["total_days"] == 2
    assert res["win_days"] == 1
    assert res["loss_days"] == 1
    assert res["start_equity"] == 5000.0
    assert res["end_equity"] == 4950.0
