"""Market analytics — builds MarketState from PriceHistory."""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from woweconomy.config import WowEconomyConfig
from woweconomy.indicators import AllIndicators
from woweconomy.models import (
    IndicatorResult,
    MarketState,
    PriceHistory,
    Recommendation,
    RiskProfile,
    SnapshotRecord,
)


class MarketAnalyzer:
    """Transforms raw PriceHistory into computed MarketState."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()
        self.indicators = AllIndicators(self.config)

    def analyze(self, history: PriceHistory) -> MarketState:
        """Run full analysis pipeline on a price history."""
        latest = history.latest
        if latest is None or history.is_empty():
            return self._empty_state(history)

        indicator_results = self.indicators.compute_all(history)
        result_map = {r.name: r for r in indicator_results}

        state = MarketState(
            item_id=history.item.item_id,
            name=history.item.name,
            timestamp=latest.snapshot_timestamp,
            current_price=latest.market_value,
            historical_mean=latest.historical,
            avg_sale_price=latest.avg_sale_price,
            sale_rate=latest.sale_rate,
            sold_per_day=latest.sold_per_day,
        )

        # Moving averages
        sma7 = result_map.get("SMA(7)")
        sma30 = result_map.get("SMA(30)")
        ema = result_map.get("EMA")
        if sma7 and sma7.value:
            state.sma_7 = sma7.value
        if sma30 and sma30.value:
            state.sma_30 = sma30.value
        if ema and ema.value:
            state.ema = ema.value

        # Volatility
        vol = result_map.get("Volatility(14)")
        if vol:
            state.volatility = vol.value

        # Z-Score
        zs = result_map.get("Z-Score")
        if zs:
            state.z_score = zs.value

        # Percentile
        pct = result_map.get("Percentile Rank")
        if pct:
            state.percentile_rank = pct.value

        # Liquidity
        liq = result_map.get("Liquidity Score")
        if liq:
            state.liquidity_score = liq.normalized

        # Item value (no demand multiplier — just unit price)
        val = result_map.get("Item Value")
        if val:
            state.item_value = val.value

        # Market size (informational only — SPD × AvgSalePrice)
        state.market_size = state.compute_market_size()

        # Momentum
        mom = result_map.get("Momentum(7)")
        if mom:
            state.momentum = mom.value

        # Price trend
        state.price_trend = state.compute_price_trend()

        # Demand trend (from relative demand)
        rd = result_map.get("Relative Demand")
        if rd:
            state.demand_trend = rd.value

        # Price stability
        stab = result_map.get("Price Stability")
        if stab:
            state.price_stability = stab.value

        # Confidence score (composite)
        state.confidence_score = self._compute_confidence(state, indicator_results)

        # Classification
        from woweconomy.analytics.classifier import MarketClassifier
        classifier = MarketClassifier(self.config)
        state.classification = classifier.classify(state)

        # Risk
        from woweconomy.analytics.risk import RiskAnalyzer
        risk_analyzer = RiskAnalyzer(self.config)
        risk_profile = risk_analyzer.assess(state, indicator_results)
        state.risk_level = risk_profile.risk_level

        return state

    def _compute_confidence(self, state: MarketState,
                            indicators: List[IndicatorResult]) -> float:
        """Confidence in the market state: 0 (unreliable) to 1 (high confidence)."""
        factors = []
        # More history = more confidence
        factors.append(0.5)  # base
        # Stable price boosts confidence
        factors.append(state.price_stability * 0.2)
        # Good liquidity
        factors.append(state.liquidity_score * 0.2)
        # Reasonable volatility
        factors.append(max(0, 1 - state.volatility * 5) * 0.1)
        return min(1.0, sum(factors))

    def _empty_state(self, history: PriceHistory) -> MarketState:
        if history.latest:
            return MarketState(
                item_id=history.item.item_id,
                name=history.item.name,
                timestamp=history.latest.snapshot_timestamp,
                current_price=history.latest.market_value,
                historical_mean=history.latest.historical,
                confidence_score=0.0,
                classification="No Data",
            )
        return MarketState(
            item_id=history.item.item_id,
            name=history.item.name,
            timestamp="",
            confidence_score=0.0,
            classification="No Data",
        )


class MarketAnalyzerBatch:
    """Analyze multiple items at once with shared state."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.analyzer = MarketAnalyzer(config)

    def analyze_all(self, histories: List[PriceHistory]) -> List[MarketState]:
        return [self.analyzer.analyze(h) for h in histories]

    def to_dataframe(self, states: List[MarketState]):
        """Convert states to a pandas DataFrame for further analysis."""
        import pandas as pd
        rows = []
        for s in states:
            rows.append({
                "item_id": s.item_id,
                "name": s.name,
                "current_price": s.current_price,
                "historical_mean": s.historical_mean,
                "sma_7": s.sma_7,
                "sma_30": s.sma_30,
                "volatility": s.volatility,
                "z_score": s.z_score,
                "liquidity_score": s.liquidity_score,
                "item_value": s.item_value,
                "market_size": s.market_size,
                "momentum": s.momentum,
                "price_trend": s.price_trend,
                "demand_trend": s.demand_trend,
                "price_stability": s.price_stability,
                "confidence_score": s.confidence_score,
                "classification": s.classification,
                "risk_level": s.risk_level,
                "sale_rate": s.sale_rate,
                "sold_per_day": s.sold_per_day,
            })
        return pd.DataFrame(rows)
