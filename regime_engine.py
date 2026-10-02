"""
regime_engine.py — Hierarchical Market Regime Detection and Dynamic Strategy Selector.

Part of the Wanna Buffet autonomous trading architecture.
Evaluates multi-timeframe trends, momentum, volatility, and historical alpha efficiency
to classify market state and modulate execution frequency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any, Dict, List, Optional, Sequence, Union
import numpy as np
import pandas as pd


class MarketRegime(str, Enum):
    SUPERCYCLE_BULL = "supercycle_bull"  # Long-term secular uptrend; passive hold dominant
    CYCLICAL_BULL = "cyclical_bull"      # Active trend following, buying dip pullbacks
    NEUTRAL_CHOP = "neutral_chop"        # Range-bound, mean reversion, elevated cash
    CYCLICAL_BEAR = "cyclical_bear"      # Active shorting / inverse hedging, tight stops
    PANIC_LIQUIDATION = "liquidation"    # High vol cascade; maximum cash or tail hedge
    UNKNOWN = "unknown"                  # Insufficient data; fail-safe defaults


class ExecutionBias(str, Enum):
    HOLD_AND_ACCUMULATE = "hold_and_accumulate"
    ACTIVE_LONG = "active_long"
    NEUTRAL_CASH = "neutral_cash"
    ACTIVE_SHORT = "active_short"
    PRESERVE_CAPITAL = "preserve_capital"


@dataclass(frozen=True)
class RegimeMetrics:
    regime: MarketRegime
    bias: ExecutionBias
    spy_close: float
    sma_50: float
    sma_200: float
    slope_200: float
    adx_14: float
    rsi_14: float
    realized_vol_pct: float
    active_alpha_ratio: float
    churn_throttle_multiplier: float
    reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["regime"] = self.regime.value
        d["bias"] = self.bias.value
        return d


class RegimeDetector:
    """
    Deterministic regime detection engine.
    Analyzes price series (typically SPY or broad market proxy) and attribution feedback.
    """

    def __init__(
        self,
        min_bars_required: int = 60,
        vol_window: int = 20,
        churn_penalty_threshold: float = 0.8,
        max_churn_multiplier: float = 3.0,
    ) -> None:
        self.min_bars_required = min_bars_required
        self.vol_window = vol_window
        self.churn_penalty_threshold = churn_penalty_threshold
        self.max_churn_multiplier = max_churn_multiplier

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
        """Welles Wilder's Directional Movement Index (ADX)."""
        if len(df) < period + 2:
            return 20.0

        high = df["h"].astype(float)
        low = df["l"].astype(float)
        close = df["c"].astype(float)

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = np.maximum(tr1, np.maximum(tr2, tr3))

        tr_series = pd.Series(tr, index=df.index).replace(0, 1e-6)
        atr = tr_series.rolling(period, min_periods=period).mean()

        p_dm_series = pd.Series(plus_dm, index=df.index)
        m_dm_series = pd.Series(minus_dm, index=df.index)

        plus_di = 100 * (p_dm_series.rolling(period, min_periods=period).mean() / atr)
        minus_di = 100 * (m_dm_series.rolling(period, min_periods=period).mean() / atr)

        denom = (plus_di + minus_di).replace(0, 1e-6)
        dx = (abs(plus_di - minus_di) / denom) * 100
        adx_series = dx.rolling(period, min_periods=period).mean()

        val = adx_series.iloc[-1]
        return float(val) if not math.isnan(val) else 20.0

    @staticmethod
    def calculate_rsi(series: pd.Series, period: int = 14) -> float:
        """Standard 14-period Relative Strength Index."""
        if len(series) < period + 1:
            return 50.0

        delta = series.diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(period, min_periods=period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(period, min_periods=period).mean()

        rs = gain / (loss.replace(0, 1e-6))
        rsi = 100.0 - (100.0 / (1.0 + rs))
        val = rsi.iloc[-1]
        return float(val) if not math.isnan(val) else 50.0

    def evaluate(
        self,
        bars: Union[pd.DataFrame, Sequence[Dict[str, Any]]],
        active_returns_30d: Optional[float] = None,
        benchmark_returns_30d: Optional[float] = None,
    ) -> RegimeMetrics:
        """
        Evaluate market regime and dynamic anti-churn throttle.
        Accepts DataFrame or list of dicts with keys ['c', 'h', 'l', 'v'].
        """
        reasons: List[str] = []

        if isinstance(bars, pd.DataFrame):
            df = bars.copy()
        elif isinstance(bars, Sequence):
            if not bars:
                return self._fallback_metrics(["No bar data provided."])
            df = pd.DataFrame(bars)
        else:
            return self._fallback_metrics(["Invalid bar data format."])

        # Validate required columns
        for col in ("c", "h", "l"):
            if col not in df.columns:
                return self._fallback_metrics([f"Missing required price column: {col}"])

        if len(df) < self.min_bars_required:
            return self._fallback_metrics([
                f"Insufficient historical bars: {len(df)} < {self.min_bars_required} required"
            ])

        closes = df["c"].astype(float)
        curr_price = float(closes.iloc[-1])

        # 1. Moving Averages & Slopes
        sma_50 = float(closes.rolling(50, min_periods=30).mean().iloc[-1])
        if len(closes) >= 200:
            sma_200 = float(closes.rolling(200, min_periods=100).mean().iloc[-1])
            sma_200_prior = float(closes.rolling(200, min_periods=100).mean().iloc[-20])
            slope_200 = (sma_200 - sma_200_prior) / (sma_200_prior + 1e-6)
        else:
            # Approximate with available depth if between 60 and 200 bars
            sma_200 = float(closes.mean())
            slope_200 = 0.0
            reasons.append(f"Fewer than 200 bars ({len(closes)}); 200 SMA is approximated.")

        # 2. Oscillators & Realized Volatility
        adx = self.calculate_adx(df)
        rsi = self.calculate_rsi(closes)

        log_rets = np.log(closes / closes.shift(1).replace(0, 1e-6)).dropna()
        if len(log_rets) >= self.vol_window:
            realized_vol = float(log_rets.rolling(self.vol_window).std().iloc[-1] * np.sqrt(252))
        else:
            realized_vol = 0.16

        # 3. Dynamic Adaptation (Anti-Churn Metric)
        # Compares active trading performance against the buy-and-hold benchmark
        if active_returns_30d is not None and benchmark_returns_30d is not None:
            # Baseline offset to avoid div zero on small returns
            active_alpha = (active_returns_30d + 0.05) / (benchmark_returns_30d + 0.05)
            if active_alpha < self.churn_penalty_threshold:
                throttle = min(self.max_churn_multiplier, 1.0 / max(0.25, active_alpha))
                reasons.append(
                    f"Active strategy alpha underperforming baseline (ratio {active_alpha:.2f} < "
                    f"{self.churn_penalty_threshold:.2f}). Applying {throttle:.2f}x turnover throttle."
                )
            else:
                throttle = 1.0
        else:
            active_alpha = 1.0
            throttle = 1.0

        # 4. Regime Classification Decision Matrix
        is_above_50 = curr_price >= sma_50
        is_above_200 = curr_price >= sma_200
        is_golden_cross = sma_50 >= sma_200
        is_strong_trend = adx >= 25.0

        if is_above_50 and is_above_200 and is_golden_cross and slope_200 >= 0:
            if is_strong_trend and rsi >= 48.0:
                regime = MarketRegime.SUPERCYCLE_BULL
                bias = ExecutionBias.HOLD_AND_ACCUMULATE
                reasons.append("Price > 50 SMA > 200 SMA with positive 200-day slope and strong ADX trend.")
            else:
                regime = MarketRegime.CYCLICAL_BULL
                bias = ExecutionBias.ACTIVE_LONG
                reasons.append("Bullish moving average stack with consolidating trend momentum.")

        elif (not is_above_200) and (not is_golden_cross):
            # Death cross confirmed (50 SMA < 200 SMA and Price < 200 SMA)
            if realized_vol >= 0.35:
                regime = MarketRegime.PANIC_LIQUIDATION
                bias = ExecutionBias.PRESERVE_CAPITAL
                reasons.append(
                    f"High realized volatility spike ({realized_vol * 100:.1f}%) in bear regime. "
                    "Halting active shorting; enforcing cash preservation."
                )
            else:
                regime = MarketRegime.CYCLICAL_BEAR
                bias = ExecutionBias.ACTIVE_SHORT
                reasons.append(
                    "Price and 50 SMA below 200 SMA. Systematic shorting and inverse hedging permitted."
                )

        elif is_above_200 and not is_above_50:
            regime = MarketRegime.NEUTRAL_CHOP
            bias = ExecutionBias.NEUTRAL_CASH
            reasons.append("Correction regime: Price below 50 SMA but supported by 200 SMA.")

        elif not is_above_200 and is_above_50:
            regime = MarketRegime.NEUTRAL_CHOP
            bias = ExecutionBias.NEUTRAL_CASH
            reasons.append("Bear market rally: Price above 50 SMA but lagging under 200 SMA.")

        else:
            regime = MarketRegime.NEUTRAL_CHOP
            bias = ExecutionBias.NEUTRAL_CASH
            reasons.append("Indecisive moving average compression.")

        return RegimeMetrics(
            regime=regime,
            bias=bias,
            spy_close=curr_price,
            sma_50=sma_50,
            sma_200=sma_200,
            slope_200=slope_200,
            adx_14=adx,
            rsi_14=rsi,
            realized_vol_pct=realized_vol,
            active_alpha_ratio=active_alpha,
            churn_throttle_multiplier=throttle,
            reasons=reasons,
        )

    def _fallback_metrics(self, reasons: List[str]) -> RegimeMetrics:
        return RegimeMetrics(
            regime=MarketRegime.UNKNOWN,
            bias=ExecutionBias.PRESERVE_CAPITAL,
            spy_close=0.0,
            sma_50=0.0,
            sma_200=0.0,
            slope_200=0.0,
            adx_14=20.0,
            rsi_14=50.0,
            realized_vol_pct=0.16,
            active_alpha_ratio=1.0,
            churn_throttle_multiplier=1.0,
            reasons=reasons,
        )
