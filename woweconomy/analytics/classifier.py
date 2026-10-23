"""Market classifier — tags every item into a market archetype."""
from __future__ import annotations

from typing import Optional

from woweconomy.config import WowEconomyConfig
from woweconomy.models import MarketState


class MarketClassifier:
    """Classify an item's market into one of several archetypes."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()

    def classify(self, state: MarketState) -> str:
        """Classify the market for a single item."""
        if self._is_dead(state):
            return "Dead Market"
        if self._is_excellent_farm(state):
            return "Excellent Farming Market"
        if self._is_stable_farm(state):
            return "Stable Farming Market"
        if self._is_flip(state):
            return "Flipping Market"
        if self._is_investment(state):
            return "Investment Market"
        if self._is_speculative(state):
            return "Speculative Market"
        return "Unclassified"

    def _is_dead(self, s: MarketState) -> bool:
        return (s.sold_per_day < self.config.dead_market_spd_threshold or
                s.sale_rate < self.config.dead_market_sr_threshold)

    def _is_excellent_farm(self, s: MarketState) -> bool:
        return (s.liquidity_score > 0.6 and
                s.price_stability > 0.6 and
                s.market_size > 100000 and
                s.sold_per_day > 50 and
                s.sale_rate > 0.02 and
                s.demand_trend >= 0.9)

    def _is_stable_farm(self, s: MarketState) -> bool:
        return (s.liquidity_score > 0.4 and
                s.price_stability > 0.4 and
                s.market_size > 10000 and
                s.sold_per_day > 5 and
                s.sale_rate > 0.01)

    def _is_flip(self, s: MarketState) -> bool:
        """Flipping opp: high volatility, decent liquidity, price oscillates."""
        return (s.liquidity_score > 0.3 and
                s.volatility > 0.05 and
                s.volatility < 0.5 and
                abs(s.z_score) > 0.5 and
                s.market_size > 10000)

    def _is_investment(self, s: MarketState) -> bool:
        """Investment: low liquidity but good long-term trend and value."""
        return (s.price_trend > 1.05 and
                s.price_stability > 0.5 and
                s.current_price > 50000 and
                s.confidence_score > 0.4)

    def _is_speculative(self, s: MarketState) -> bool:
        """Speculative: high volatility, low liquidity, extreme price moves."""
        return (s.volatility > 0.15 and
                s.liquidity_score < 0.3 and
                (s.z_score > 1.5 or s.z_score < -1.5))
