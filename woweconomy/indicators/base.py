"""Abstract base for all market indicators."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from woweconomy.models import IndicatorResult, PriceHistory


class BaseIndicator(ABC):
    """Every indicator inherits from this and implements compute()."""

    name: str = "base_indicator"

    @abstractmethod
    def compute(self, history: PriceHistory) -> IndicatorResult:
        ...

    def __call__(self, history: PriceHistory) -> IndicatorResult:
        return self.compute(history)

    def normalize(self, value: float, min_val: float, max_val: float) -> float:
        """Min-max normalize to 0-1, clamping extremes."""
        if max_val <= min_val:
            return 0.5
        clipped = max(min_val, min(value, max_val))
        return (clipped - min_val) / (max_val - min_val)

    def signal_from_zscore(self, z: float) -> str:
        if z > 2.0:
            return "extreme_bullish"
        elif z > 1.0:
            return "bullish"
        elif z < -2.0:
            return "extreme_bearish"
        elif z < -1.0:
            return "bearish"
        return "neutral"
