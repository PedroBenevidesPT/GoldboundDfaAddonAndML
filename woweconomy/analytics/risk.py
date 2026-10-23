"""Risk analysis module."""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from woweconomy.config import WowEconomyConfig
from woweconomy.models import IndicatorResult, MarketState, RiskProfile


class RiskAnalyzer:
    """Assess market risk for a single item."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()

    def assess(self, state: MarketState,
               indicators: Optional[List[IndicatorResult]] = None) -> RiskProfile:
        """Compute all risk dimensions from a MarketState."""
        profile = RiskProfile(item_id=state.item_id)

        # Liquidity risk (inverse of liquidity score)
        profile.liquidity_risk = 1.0 - state.liquidity_score

        # Price risk (how far from historical mean)
        trend = state.price_trend
        if trend > 2.0 or trend < 0.5:
            profile.price_risk = 0.8
        elif trend > 1.5 or trend < 0.75:
            profile.price_risk = 0.5
        else:
            profile.price_risk = 0.2

        # Saturation risk (declining demand)
        if state.demand_trend < 0.5:
            profile.saturation_risk = 0.8
        elif state.demand_trend < 0.8:
            profile.saturation_risk = 0.5
        else:
            profile.saturation_risk = 0.2

        # Volatility risk
        vol = state.volatility
        if vol > 0.15:
            profile.volatility_risk = 0.8
        elif vol > 0.05:
            profile.volatility_risk = 0.5
        else:
            profile.volatility_risk = 0.2

        # Overall (weighted average)
        profile.overall_risk = (
            profile.liquidity_risk * 0.35 +
            profile.price_risk * 0.25 +
            profile.saturation_risk * 0.20 +
            profile.volatility_risk * 0.20
        )

        # Risk level
        if profile.overall_risk > 0.7:
            profile.risk_level = "high"
        elif profile.overall_risk > 0.4:
            profile.risk_level = "medium"
        else:
            profile.risk_level = "low"
        profile.risk_level = state.risk_level = profile.risk_level

        # Collect risk factors
        factors = []
        if profile.liquidity_risk > 0.6:
            factors.append("Low liquidity")
        if profile.price_risk > 0.6:
            factors.append("Extreme price deviation")
        if profile.saturation_risk > 0.6:
            factors.append("Declining demand")
        if profile.volatility_risk > 0.6:
            factors.append("High volatility")
        if state.confidence_score < 0.3:
            factors.append("Low data confidence")
        profile.risk_factors = factors

        return profile


class RiskAnalyzerBatch:
    """Assess risk for multiple items."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.analyzer = RiskAnalyzer(config)

    def assess_all(self, states: List[MarketState]) -> List[RiskProfile]:
        return [self.analyzer.assess(s) for s in states]
