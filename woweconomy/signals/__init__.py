"""Market signal detection — watches for events in market data."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np

from woweconomy.config import WowEconomyConfig
from woweconomy.models import MarketSignal, MarketState, PriceHistory

logger = logging.getLogger(__name__)


class SignalDetector:
    """Detect market signals/events from price histories."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()

    def detect_all(self, history: PriceHistory,
                   state: MarketState) -> List[MarketSignal]:
        """Run all signal detectors on an item."""
        signals = []
        detectors = [
            self._detect_supply_shock,
            self._detect_demand_shock,
            self._detect_price_crash,
            self._detect_price_spike,
            self._detect_trend_reversal,
            self._detect_saturation,
            self._detect_dead_market,
        ]
        for detect in detectors:
            try:
                signal = detect(history, state)
                if signal is not None:
                    signals.append(signal)
            except Exception as e:
                logger.debug(f"Signal detection error for {history.item.name}: {e}")
        return signals

    def _detect_supply_shock(self, history: PriceHistory,
                             state: MarketState) -> Optional[MarketSignal]:
        """Supply shock: sudden price drop with stable or rising demand."""
        if len(history.prices) < 3:
            return None
        prices = history.prices
        recent_drop = (prices[-3] - prices[-1]) / max(prices[-3], 1)
        if recent_drop > 0.20 and state.demand_trend >= 0.9:
            return MarketSignal(
                signal_type="supply_shock",
                item_id=state.item_id,
                item_name=state.name,
                confidence=min(1.0, recent_drop / 0.5),
                severity="high" if recent_drop > 0.35 else "medium",
                description=f"Price dropped {recent_drop:.1%} in 3 snapshots — oversupply",
                current_value=prices[-1],
                previous_value=prices[-3],
                change_pct=-recent_drop * 100,
            )
        return None

    def _detect_demand_shock(self, history: PriceHistory,
                             state: MarketState) -> Optional[MarketSignal]:
        """Demand shock: sudden SPD spike."""
        spd = history.spd_values
        if len(spd) < 3:
            return None
        recent_spike = (spd[-1] - spd[-3]) / max(spd[-3], 1)
        if recent_spike > 0.50:
            return MarketSignal(
                signal_type="demand_shock",
                item_id=state.item_id,
                item_name=state.name,
                confidence=min(1.0, recent_spike / 2.0),
                severity="high" if recent_spike > 1.0 else "medium",
                description=f"SPD surged {recent_spike:.1%} in 3 snapshots",
                current_value=spd[-1],
                previous_value=spd[-3],
                change_pct=recent_spike * 100,
            )
        return None

    def _detect_price_crash(self, history: PriceHistory,
                            state: MarketState) -> Optional[MarketSignal]:
        """Price crash: sustained drop exceeding threshold."""
        if len(history.prices) < 7:
            return None
        prices = history.prices
        crash = (prices[-7] - prices[-1]) / max(prices[-7], 1)
        if crash > abs(self.config.crash_pct_threshold):
            return MarketSignal(
                signal_type="price_crash",
                item_id=state.item_id,
                item_name=state.name,
                confidence=min(1.0, crash / 0.4),
                severity="critical" if crash > 0.4 else "high",
                description=f"Price crashed {crash:.1%} over 7 snapshots",
                current_value=prices[-1],
                previous_value=prices[-7],
                change_pct=-crash * 100,
            )
        return None

    def _detect_price_spike(self, history: PriceHistory,
                            state: MarketState) -> Optional[MarketSignal]:
        """Price spike: sudden large increase."""
        if len(history.prices) < 3:
            return None
        prices = history.prices
        spike = (prices[-1] - prices[-3]) / max(prices[-3], 1)
        if spike > self.config.spike_pct_threshold:
            return MarketSignal(
                signal_type="price_spike",
                item_id=state.item_id,
                item_name=state.name,
                confidence=min(1.0, spike / 0.5),
                severity="high" if spike > 0.5 else "medium",
                description=f"Price spiked {spike:.1%} in 3 snapshots",
                current_value=prices[-1],
                previous_value=prices[-3],
                change_pct=spike * 100,
            )
        return None

    def _detect_trend_reversal(self, history: PriceHistory,
                               state: MarketState) -> Optional[MarketSignal]:
        """Trend reversal: momentum changed sign recently."""
        if len(history.prices) < 14:
            return None
        prices = history.prices
        mom7 = (prices[-1] - prices[-7]) / max(prices[-7], 1)
        mom14 = (prices[-7] - prices[-14]) / max(prices[-14], 1)
        if mom14 < -0.05 and mom7 > 0.03:
            return MarketSignal(
                signal_type="trend_reversal_up",
                item_id=state.item_id,
                item_name=state.name,
                confidence=min(1.0, abs(mom7 - mom14) / 0.2),
                severity="medium",
                description="Trend reversal detected: downtrend → uptrend",
                current_value=mom7,
                previous_value=mom14,
                change_pct=(mom7 - mom14) * 100,
            )
        if mom14 > 0.05 and mom7 < -0.03:
            return MarketSignal(
                signal_type="trend_reversal_down",
                item_id=state.item_id,
                item_name=state.name,
                confidence=min(1.0, abs(mom7 - mom14) / 0.2),
                severity="medium",
                description="Trend reversal detected: uptrend → downtrend",
                current_value=mom7,
                previous_value=mom14,
                change_pct=(mom7 - mom14) * 100,
            )
        return None

    def _detect_saturation(self, history: PriceHistory,
                           state: MarketState) -> Optional[MarketSignal]:
        """Market saturation: declining SPD + stable/increasing supply."""
        spd = history.spd_values
        if len(spd) < 14:
            return None
        old_spd_avg = float(np.mean(spd[-14:-7])) if len(spd) >= 14 else spd[0]
        new_spd_avg = float(np.mean(spd[-7:]))
        if old_spd_avg > 0:
            decline = (new_spd_avg - old_spd_avg) / old_spd_avg
            if decline < self.config.saturation_spd_decline:
                return MarketSignal(
                    signal_type="market_saturation",
                    item_id=state.item_id,
                    item_name=state.name,
                    confidence=min(1.0, abs(decline) / 0.5),
                    severity="medium",
                    description=f"Demand declining: SPD down {decline:.1%} over 14 snapshots",
                    current_value=new_spd_avg,
                    previous_value=old_spd_avg,
                    change_pct=decline * 100,
                )
        return None

    def _detect_dead_market(self, history: PriceHistory,
                            state: MarketState) -> Optional[MarketSignal]:
        """Dead market: item barely trades."""
        if (state.sold_per_day < self.config.dead_market_spd_threshold and
                state.sale_rate < self.config.dead_market_sr_threshold):
            return MarketSignal(
                signal_type="dead_market",
                item_id=state.item_id,
                item_name=state.name,
                confidence=0.9,
                severity="high",
                description=f"Market dead: SPD={state.sold_per_day:.2f}, SR={state.sale_rate:.4f}",
                current_value=state.sold_per_day,
                previous_value=0,
                change_pct=0,
            )
        return None
