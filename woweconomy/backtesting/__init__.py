"""Backtesting — validate recommendation logic against historical data."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from woweconomy.config import WowEconomyConfig
from woweconomy.models import MarketState, PriceHistory, Recommendation
from woweconomy.recommendations import RecommendationEngine

logger = logging.getLogger(__name__)


class BacktestEngine:
    """Simulate holding periods to validate recommendations."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()
        self.engine = RecommendationEngine(self.config)

    def simulate_hold_period(self, history: PriceHistory,
                             buy_index: int = -1,
                             hold_snapshots: int = 7) -> Dict[str, Any]:
        """Simulate buying at buy_index, holding for hold_snapshots."""
        prices = history.prices
        if len(prices) < buy_index + hold_snapshots + 1:
            return {"error": "insufficient_history"}

        buy_price = prices[buy_index]
        sell_price = prices[buy_index + hold_snapshots]
        profit_pct = (sell_price - buy_price) / max(buy_price, 1)

        # Create state at buy time
        truncated = PriceHistory(
            item=history.item,
            dates=history.dates[:buy_index + 1],
            prices=history.prices[:buy_index + 1],
            market_values=history.market_values[:buy_index + 1],
            sale_rates=history.sale_rates[:buy_index + 1],
            spd_values=history.spd_values[:buy_index + 1],
            snapshot_ids=history.snapshot_ids[:buy_index + 1],
        )
        state = self._make_state_at_index(history, buy_index)
        rec = self.engine.evaluate(state)
        rec_stars = rec.stars

        return {
            "item_id": history.item.item_id,
            "item_name": history.item.name,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "profit_pct": round(profit_pct * 100, 2),
            "rec_stars": rec_stars,
            "hold_snapshots": hold_snapshots,
            "recommendation": rec.explanation,
        }

    def batch_backtest(self, histories: List[PriceHistory],
                       hold_snapshots: int = 7) -> pd.DataFrame:
        """Backtest across all histories."""
        results = []
        for h in histories:
            r = self.simulate_hold_period(h, hold_snapshots=hold_snapshots)
            results.append(r)
        return pd.DataFrame(results)

    @staticmethod
    def _make_state_at_index(history: PriceHistory, index: int) -> MarketState:
        """Build a MarketState as it looked at a given snapshot index."""
        prices = history.prices[:index + 1]
        spds = history.spd_values[:index + 1]
        rates = history.sale_rates[:index + 1]

        latest = prices[-1]
        avg_day = float(np.mean(spds)) if spds else 0

        return MarketState(
            item_id=history.item.item_id,
            name=history.item.name,
            timestamp="",
            current_price=latest,
            historical_mean=0,
            avg_sale_price=latest,
            sale_rate=float(np.mean(rates)) if rates else 0,
            sold_per_day=avg_day,
            price_stability=0.5,
            momentum=0,
            demand_trend=1.0,
            item_value=latest,
            market_size=latest * avg_day,
            liquidity_score=0.3,
            classification="",
        )


class PerformanceReport:
    """Generate performance stats for a backtest."""

    @staticmethod
    def generate(df: pd.DataFrame) -> Dict[str, Any]:
        if df.empty:
            return {"error": "no_results"}

        accurate = df[df["rec_stars"] >= 4]
        good_investments = accurate[accurate["profit_pct"] > 0]

        avg_profit = float(df["profit_pct"].mean())
        hit_rate = float(len(good_investments) / max(len(accurate), 1))

        return {
            "total_opportunities": len(df),
            "recommended_count": len(accurate),
            "profitable_count": len(good_investments),
            "avg_profit_pct": round(avg_profit, 2),
            "hit_rate": round(hit_rate, 4),
            "best_profit_pct": float(df["profit_pct"].max()),
            "worst_profit_pct": float(df["profit_pct"].min()),
        }
