"""Technical indicators for market analysis."""
from __future__ import annotations

import math
import statistics
from typing import List, Optional

import numpy as np
from scipy import stats as scipy_stats

from woweconomy.indicators.base import BaseIndicator
from woweconomy.models import IndicatorResult, PriceHistory


class SMA(BaseIndicator):
    """Simple Moving Average over a window."""

    name = "sma"

    def __init__(self, window: int = 7):
        self.window = window

    def compute(self, history: PriceHistory) -> IndicatorResult:
        prices = history.prices
        if len(prices) < self.window:
            return IndicatorResult(
                name=f"SMA({self.window})",
                value=0.0, signal="neutral",
                explanation=f"Insufficient data (need {self.window}, have {len(prices)})",
            )
        recent = prices[-self.window:]
        value = float(np.mean(recent))

        # Signal: price vs SMA
        latest = prices[-1]
        signal = "bullish" if latest > value else "bearish" if latest < value else "neutral"
        return IndicatorResult(
            name=f"SMA({self.window})",
            value=value,
            normalized=self.normalize(value, min(prices), max(prices)),
            signal=signal,
            explanation=f"Price {'above' if latest > value else 'below'} {self.window}-SMA ({value:,.0f})",
        )


class EMA(BaseIndicator):
    """Exponential Moving Average."""

    name = "ema"

    def __init__(self, alpha: float = 0.3):
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha

    def compute(self, history: PriceHistory) -> IndicatorResult:
        prices = history.prices
        if not prices:
            return IndicatorResult(name="EMA", value=0.0, signal="neutral",
                                   explanation="No data")
        ema_val = float(prices[0])
        for p in prices[1:]:
            ema_val = self.alpha * p + (1 - self.alpha) * ema_val

        latest = prices[-1]
        signal = "bullish" if latest > ema_val else "bearish" if latest < ema_val else "neutral"
        return IndicatorResult(
            name="EMA",
            value=ema_val,
            normalized=self.normalize(ema_val, min(prices), max(prices)),
            signal=signal,
            explanation=f"EMA={ema_val:,.0f}, price {'above' if latest > ema_val else 'below'}",
        )


class Momentum(BaseIndicator):
    """Price momentum (rate of change) over a window."""

    name = "momentum"

    def __init__(self, window: int = 7):
        self.window = window

    def compute(self, history: PriceHistory) -> IndicatorResult:
        prices = history.prices
        if len(prices) < self.window + 1:
            return IndicatorResult(
                name=f"Momentum({self.window})",
                value=0.0, signal="neutral",
                explanation=f"Insufficient data (need {self.window + 1})",
            )
        old = prices[-self.window - 1]
        new = prices[-1]
        if old == 0:
            return IndicatorResult(name="Momentum", value=0.0, signal="neutral")
        roc = (new - old) / old
        signal = "bullish" if roc > 0.02 else "bearish" if roc < -0.02 else "neutral"
        return IndicatorResult(
            name=f"Momentum({self.window})",
            value=float(roc),
            normalized=self.normalize(roc, -0.5, 0.5),
            signal=signal,
            explanation=f"{roc:+.2%} over {self.window} snapshots",
        )


class RateOfChange(BaseIndicator):
    """Rate of Change (identical to momentum but separate for clarity)."""

    name = "rate_of_change"

    def __init__(self, window: int = 7):
        self.window = window
        self._mom = Momentum(window)

    def compute(self, history: PriceHistory) -> IndicatorResult:
        result = self._mom.compute(history)
        result.name = f"RoC({self.window})"
        return result


class Volatility(BaseIndicator):
    """Price volatility (std dev of log returns)."""

    name = "volatility"

    def __init__(self, window: int = 14):
        self.window = window

    def compute(self, history: PriceHistory) -> IndicatorResult:
        prices = history.prices
        if len(prices) < self.window:
            return IndicatorResult(
                name=f"Volatility({self.window})",
                value=0.0, signal="neutral",
                explanation=f"Insufficient data",
            )
        recent = prices[-self.window:]
        log_returns = []
        for i in range(1, len(recent)):
            if recent[i - 1] > 0:
                log_returns.append(math.log(recent[i] / recent[i - 1]))
        if len(log_returns) < 2:
            return IndicatorResult(name="Volatility", value=0.0, signal="neutral")
        std = float(np.std(log_returns, ddof=1))
        # Annualize (but WoW data is irregular, just report per-snapshot)
        signal = "high" if std > 0.1 else "low" if std < 0.02 else "moderate"
        return IndicatorResult(
            name=f"Volatility({self.window})",
            value=std,
            normalized=self.normalize(std, 0, 0.5),
            signal=signal,
            explanation=f"σ={std:.4f} ({signal})",
        )


class ZScore(BaseIndicator):
    """Z-Score of latest price relative to history."""

    name = "z_score"

    def __init__(self, window: int = 30):
        self.window = window

    def compute(self, history: PriceHistory) -> IndicatorResult:
        prices = history.prices
        if len(prices) < 2:
            return IndicatorResult(name="Z-Score", value=0.0, signal="neutral")
        recent = prices[-min(self.window, len(prices)):]
        mean = float(np.mean(recent))
        std = float(np.std(recent, ddof=1))
        if std == 0:
            return IndicatorResult(name="Z-Score", value=0.0, signal="neutral")
        z = (prices[-1] - mean) / std
        return IndicatorResult(
            name="Z-Score",
            value=z,
            normalized=self.normalize(z, -3, 3),
            signal=self.signal_from_zscore(z),
            explanation=f"z={z:.2f} ({self.signal_from_zscore(z)})",
        )


class PercentileRank(BaseIndicator):
    """Percentile rank of latest price in historical distribution."""

    name = "percentile_rank"

    def __init__(self, window: int = 30):
        self.window = window

    def compute(self, history: PriceHistory) -> IndicatorResult:
        prices = history.prices
        if len(prices) < 2:
            return IndicatorResult(name="Percentile", value=50.0, signal="neutral")
        recent = prices[-min(self.window, len(prices)):]
        latest = prices[-1]
        pct = scipy_stats.percentileofscore(recent, latest, kind="rank")
        signal = "overvalued" if pct > 90 else "undervalued" if pct < 10 else "fair"
        return IndicatorResult(
            name="Percentile Rank",
            value=pct,
            normalized=pct / 100.0,
            signal=signal,
            explanation=f"Price at {pct:.0f}th percentile ({signal})",
        )


class PriceStability(BaseIndicator):
    """Price stability score: 0 (wild) to 1 (rock solid)."""

    name = "price_stability"

    def compute(self, history: PriceHistory) -> IndicatorResult:
        prices = history.prices
        if len(prices) < 2:
            return IndicatorResult(name="Price Stability", value=1.0, signal="stable")
        mean = float(np.mean(prices))
        std = float(np.std(prices, ddof=1))
        if mean == 0:
            return IndicatorResult(name="Price Stability", value=0.5, signal="neutral")
        cv = std / mean  # coefficient of variation
        stability = 1.0 / (1.0 + cv * 10)  # map to 0-1
        signal = "stable" if stability > 0.7 else "volatile" if stability < 0.3 else "moderate"
        return IndicatorResult(
            name="Price Stability",
            value=stability,
            normalized=stability,
            signal=signal,
            explanation=f"CV={cv:.4f} → stability={stability:.2f}",
        )


class LiquidityScore(BaseIndicator):
    """Liquidity from sale rate and sold per day."""

    name = "liquidity"

    def compute(self, history: PriceHistory) -> IndicatorResult:
        latest = history.latest
        if latest is None:
            return IndicatorResult(name="Liquidity", value=0.0, signal="illiquid")
        spd = latest.sold_per_day
        sr = latest.sale_rate
        if spd <= 0 or sr <= 0:
            return IndicatorResult(name="Liquidity", value=0.0, signal="illiquid")
        liq = math.log(1 + spd) * math.log(2 + sr * 100)

        # Normalise using typical ranges
        norm = self.normalize(liq, 0, 50)
        signal = "liquid" if norm > 0.5 else "illiquid" if norm < 0.15 else "moderate"
        return IndicatorResult(
            name="Liquidity Score",
            value=liq,
            normalized=norm,
            signal=signal,
            explanation=f"SPD={spd:.1f}, SR={sr:.3f} → liq={liq:.2f}",
        )


class ItemValue(BaseIndicator):
    """Unit value of an item — avg sale price (no demand multiplier)."""

    name = "item_value"

    def compute(self, history: PriceHistory) -> IndicatorResult:
        latest = history.latest
        if latest is None:
            return IndicatorResult(name="Item Value", value=0.0, signal="cheap")
        price = latest.avg_sale_price if latest.avg_sale_price else latest.market_value
        norm = self.normalize(math.log(1 + price), 0, 25)
        signal = "expensive" if norm > 0.7 else "cheap" if norm < 0.2 else "moderate"
        return IndicatorResult(
            name="Item Value",
            value=price,
            normalized=norm,
            signal=signal,
            explanation=f"{price:,.0f} g",
        )


class MeanReversion(BaseIndicator):
    """How far price is from its mean (potential reversion signal)."""

    name = "mean_reversion"

    def __init__(self, window: int = 30):
        self.window = window
        self._z = ZScore(window)

    def compute(self, history: PriceHistory) -> IndicatorResult:
        z_result = self._z.compute(history)
        z = z_result.value
        reversion_potential = abs(z) / 3.0  # 0 to ~1
        reversion_potential = min(reversion_potential, 1.0)

        if z > 1.5:
            signal = "price_high_reversion_risk"
            explanation = f"Price {z:.1f}σ above mean — may drop"
        elif z < -1.5:
            signal = "price_low_reversion_opportunity"
            explanation = f"Price {abs(z):.1f}σ below mean — may rise"
        elif abs(z) < 0.5:
            signal = "fair_value"
            explanation = "Price near mean — fair value"
        else:
            signal = "neutral"
            explanation = f"Price {z:.1f}σ from mean — moderate"

        return IndicatorResult(
            name="Mean Reversion",
            value=z,
            normalized=reversion_potential,
            signal=signal,
            explanation=explanation,
        )


class RelativeDemand(BaseIndicator):
    """Demand relative to historical average: current SPD / avg SPD."""

    name = "relative_demand"

    def __init__(self, window: int = 30):
        self.window = window

    def compute(self, history: PriceHistory) -> IndicatorResult:
        spd = history.spd_values
        if len(spd) < 2:
            return IndicatorResult(name="Relative Demand", value=1.0, signal="neutral")
        latest = spd[-1]
        avg = float(np.mean(spd[-min(self.window, len(spd)):]))
        if avg == 0:
            return IndicatorResult(name="Relative Demand", value=1.0, signal="neutral")
        ratio = latest / avg
        signal = "growing" if ratio > 1.1 else "declining" if ratio < 0.9 else "stable"
        return IndicatorResult(
            name="Relative Demand",
            value=ratio,
            normalized=self.normalize(ratio, 0, 3),
            signal=signal,
            explanation=f"SPD {ratio:.2f}x historical average ({signal})",
        )


class AllIndicators:
    """Compute all indicators at once for convenience."""

    def __init__(self, config=None):
        if config is None:
            from woweconomy.config import WowEconomyConfig
            config = WowEconomyConfig.default()
        self.indicators = [
            SMA(window=config.sma_short_window),
            SMA(window=config.sma_long_window),
            EMA(alpha=config.ema_alpha),
            Momentum(window=config.momentum_window),
            Volatility(window=config.volatility_window),
            ZScore(window=config.z_score_window),
            PercentileRank(window=config.z_score_window),
            PriceStability(),
            LiquidityScore(),
            ItemValue(),
            MeanReversion(window=config.z_score_window),
            RelativeDemand(window=config.momentum_window),
        ]

    def compute_all(self, history: PriceHistory) -> List[IndicatorResult]:
        return [ind(history) for ind in self.indicators]
