"""
tests/test_regime_engine.py — Unit tests for the regime detection and adaptation engine.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from regime_engine import ExecutionBias, MarketRegime, RegimeDetector


def _generate_bars(
    num_bars: int = 250,
    start_price: float = 100.0,
    daily_drift: float = 0.001,
    volatility: float = 0.01,
    seed: int = 42,
) -> pd.DataFrame:
    np.random.seed(seed)
    prices = [start_price]
    for _ in range(num_bars - 1):
        ret = np.random.normal(daily_drift, volatility)
        prices.append(prices[-1] * (1.0 + ret))

    closes = np.array(prices)
    highs = closes * (1.0 + np.abs(np.random.normal(0.005, 0.002, num_bars)))
    lows = closes * (1.0 - np.abs(np.random.normal(0.005, 0.002, num_bars)))
    volumes = np.random.randint(1_000_000, 5_000_000, num_bars)

    dates = [
        (datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(num_bars)
    ]
    return pd.DataFrame({
        "t": dates,
        "c": closes,
        "h": highs,
        "l": lows,
        "v": volumes,
    })


def test_insufficient_bars_returns_fallback():
    engine = RegimeDetector(min_bars_required=60)
    short_df = _generate_bars(num_bars=30)
    res = engine.evaluate(short_df)

    assert res.regime == MarketRegime.UNKNOWN
    assert res.bias == ExecutionBias.PRESERVE_CAPITAL
    assert any("Insufficient" in r for r in res.reasons)


def test_empty_bars_returns_fallback():
    engine = RegimeDetector()
    res = engine.evaluate([])
    assert res.regime == MarketRegime.UNKNOWN
    assert res.bias == ExecutionBias.PRESERVE_CAPITAL


def test_supercycle_bull_regime():
    # Strong upward trend with low noise produces high ADX, price > 50 > 200 SMA
    df = _generate_bars(num_bars=250, start_price=100.0, daily_drift=0.003, volatility=0.004)
    engine = RegimeDetector()
    res = engine.evaluate(df)

    assert res.regime == MarketRegime.SUPERCYCLE_BULL
    assert res.bias == ExecutionBias.HOLD_AND_ACCUMULATE
    assert res.spy_close > res.sma_50 > res.sma_200
    assert res.slope_200 > 0


def test_cyclical_bear_regime():
    # Strong downward drift produces death cross: price < 50 < 200 SMA
    df = _generate_bars(num_bars=250, start_price=200.0, daily_drift=-0.003, volatility=0.008)
    engine = RegimeDetector()
    res = engine.evaluate(df)

    assert res.regime == MarketRegime.CYCLICAL_BEAR
    assert res.bias == ExecutionBias.ACTIVE_SHORT
    assert res.spy_close < res.sma_200
    assert res.sma_50 < res.sma_200


def test_panic_liquidation_regime():
    # Steep downward trend with extreme volatility (> 0.35 annualized)
    df = _generate_bars(num_bars=250, start_price=200.0, daily_drift=-0.008, volatility=0.035)
    engine = RegimeDetector()
    res = engine.evaluate(df)

    assert res.regime == MarketRegime.PANIC_LIQUIDATION
    assert res.bias == ExecutionBias.PRESERVE_CAPITAL
    assert res.realized_vol_pct >= 0.35


def test_neutral_chop_regime():
    # Run an initial uptrend then pull back below 50 SMA while staying above 200 SMA
    df = _generate_bars(num_bars=250, start_price=100.0, daily_drift=0.001, volatility=0.006)
    # Artificially pull down recent 10 bars below 50 SMA but keep above 200 SMA
    sma_50 = df["c"].rolling(50).mean().iloc[-1]
    sma_200 = df["c"].rolling(200).mean().iloc[-1]
    if sma_50 > sma_200:
        # Pull last bar between 200 SMA and 50 SMA
        mid = (sma_50 + sma_200) / 2.0
        df.loc[df.index[-5:], "c"] = mid
        df.loc[df.index[-5:], "h"] = mid * 1.002
        df.loc[df.index[-5:], "l"] = mid * 0.998

        engine = RegimeDetector()
        res = engine.evaluate(df)
        assert res.regime == MarketRegime.NEUTRAL_CHOP
        assert res.bias == ExecutionBias.NEUTRAL_CASH


def test_anti_churn_throttling_logic():
    df = _generate_bars(num_bars=250, daily_drift=0.001)
    engine = RegimeDetector(churn_penalty_threshold=0.8, max_churn_multiplier=3.0)

    # Case A: Active trading outperforming benchmark -> no penalty (throttle == 1.0)
    res_good = engine.evaluate(df, active_returns_30d=0.10, benchmark_returns_30d=0.05)
    assert res_good.churn_throttle_multiplier == 1.0

    # Case B: Active trading underperforming benchmark -> throttle scales up
    res_bad = engine.evaluate(df, active_returns_30d=-0.04, benchmark_returns_30d=0.08)
    assert res_bad.churn_throttle_multiplier > 1.0
    assert res_bad.churn_throttle_multiplier <= 3.0
    assert any("turnover throttle" in r for r in res_bad.reasons)


def test_metrics_serialization():
    df = _generate_bars(num_bars=250)
    engine = RegimeDetector()
    res = engine.evaluate(df)
    d = res.to_dict()

    assert isinstance(d["regime"], str)
    assert isinstance(d["bias"], str)
    assert isinstance(d["spy_close"], float)
    assert isinstance(d["reasons"], list)
