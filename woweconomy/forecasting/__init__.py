"""Price forecasting for WoW auction house items.

Uses statsmodels ARIMA with graceful fallback to simple methods
when insufficient data exists.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np

from woweconomy.config import WowEconomyConfig
from woweconomy.models import Forecast, PriceHistory

logger = logging.getLogger(__name__)


class PriceForecaster:
    """Forecast future prices for a single item.

    Uses ARIMA when sufficient data exists, falls back to
    exponential smoothing or naive methods.
    """

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()
        self._arima_cache = {}

    def forecast(self, history: PriceHistory) -> Forecast:
        """Generate a full forecast for an item."""
        prices = history.prices
        item_id = history.item.item_id
        forecast = Forecast(item_id=item_id)

        if len(prices) < self.config.min_history_for_forecast:
            forecast.error_message = (
                f"Insufficient history: {len(prices)} snapshots "
                f"(need {self.config.min_history_for_forecast})"
            )
            return forecast

        try:
            return self._forecast_arima(history)
        except Exception as e:
            logger.warning(f"ARIMA failed for item {item_id}, falling back: {e}")
            try:
                return self._forecast_ema(history)
            except Exception as e2:
                forecast.error_message = f"All forecast methods failed: {e2}"
                return forecast

    def _forecast_arima(self, history: PriceHistory) -> Forecast:
        """ARIMA-based forecast using statsmodels."""
        from statsmodels.tsa.arima.model import ARIMA

        prices = np.array(history.prices, dtype=float)
        item_id = history.item.item_id

        # Ensure stationarity by differencing
        order = self.config.arima_order

        trend_param = "n" if order[1] > 0 else "c"
        model = ARIMA(prices, order=order, trend=trend_param)
        fitted = model.fit()

        # Forecast
        forecast_arr = fitted.forecast(steps=self.config.forecast_horizon)
        pred = fitted.get_prediction(
            start=len(prices),
            end=len(prices) + self.config.forecast_horizon - 1,
        )
        ci = pred.conf_int()

        next_day = float(forecast_arr[0])
        next_7d = float(np.mean(forecast_arr))

        if isinstance(ci, np.ndarray) and ci.ndim == 2:
            high = float(np.max(ci[:, 1]))
            low = float(np.min(ci[:, 0]))
        else:
            high = next_7d * 1.1
            low = next_7d * 0.9

        # Trend direction
        latest = prices[-1]
        if next_7d > latest * 1.05:
            trend = "up"
        elif next_7d < latest * 0.95:
            trend = "down"
        else:
            trend = "stable"

        # Seasonality check (simple: compare today vs 7 days ago)
        seasonality = False
        if len(prices) >= 14:
            week_ago = prices[-8] if len(prices) >= 8 else prices[0]
            today = prices[-1]
            # Rough ACF at lag 7
            diffs = []
            for i in range(7, len(prices)):
                diffs.append(prices[i] - prices[i - 7])
            if diffs and abs(np.mean(diffs)) > np.std(prices) * 0.3:
                seasonality = True

        return Forecast(
            item_id=item_id,
            next_day_price=round(next_day, 2),
            next_7d_avg=round(next_7d, 2),
            next_7d_high=round(high, 2),
            next_7d_low=round(low, 2),
            confidence_interval=round((high - low) / next_7d, 4),
            trend_direction=trend,
            seasonality_detected=seasonality,
            forecast_available=True,
        )

    def _forecast_ema(self, history: PriceHistory) -> Forecast:
        """Simple EMA-based forecast fallback."""
        prices = history.prices
        item_id = history.item.item_id
        alpha = 0.3
        ema = float(prices[0])
        for p in prices[1:]:
            ema = alpha * p + (1 - alpha) * ema

        # Project forward using EMA + recent drift
        drift = (prices[-1] - prices[-min(7, len(prices))]) / min(7, len(prices))
        next_day = ema + drift
        next_7d = next_day + drift * 3.5
        std = float(np.std(prices[-min(14, len(prices)):], ddof=1)) if len(prices) >= 2 else next_7d * 0.05

        trend = "up" if drift > 0 else "down" if drift < 0 else "stable"

        return Forecast(
            item_id=item_id,
            next_day_price=round(next_day, 2),
            next_7d_avg=round(next_7d, 2),
            next_7d_high=round(next_7d + 2 * std, 2),
            next_7d_low=round(max(0, next_7d - 2 * std), 2),
            confidence_interval=round(4 * std / max(next_7d, 1), 4),
            trend_direction=trend,
            forecast_available=True,
            seasonality_detected=False,
        )


class TrendAnalyzer:
    """Analyze price trends for an item."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()

    def classify_trend(self, history: PriceHistory) -> str:
        prices = history.prices
        if len(prices) < 5:
            return "insufficient_data"

        # Linear regression for slope
        x = np.arange(len(prices))
        slope, _ = np.polyfit(x, prices, 1)

        if slope > 0.02 * np.mean(prices):
            return "strongly_uptrending"
        elif slope > 0.005 * np.mean(prices):
            return "uptrending"
        elif slope < -0.02 * np.mean(prices):
            return "strongly_downtrending"
        elif slope < -0.005 * np.mean(prices):
            return "downtrending"
        else:
            return "sideways"

    def detect_seasonality(self, history: PriceHistory, period: int = 7) -> bool:
        prices = history.prices
        if len(prices) < period * 2:
            return False
        # Simple autocorrelation at lag
        lagged = prices[:-period]
        shifted = prices[period:]
        if len(lagged) < 3:
            return False
        corr = np.corrcoef(lagged, shifted)[0, 1]
        return abs(corr) > 0.5
