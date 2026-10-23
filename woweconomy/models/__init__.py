"""Data models for the WoW economy engine."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence


@dataclass
class ItemIdentity:
    """Immutable identity of a WoW item."""
    item_id: int
    name: str


@dataclass
class SnapshotRecord:
    """A single row from a TSM market snapshot."""
    snapshot_id: int
    item_id: int
    market_value: float
    historical: float
    avg_sale_price: float
    sale_rate: float
    sold_per_day: float
    snapshot_timestamp: str


@dataclass
class PriceHistory:
    """Time-ordered price observations for a single item."""
    item: ItemIdentity
    records: List[SnapshotRecord] = field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.records) == 0

    def sort(self) -> None:
        self.records.sort(key=lambda r: r.snapshot_timestamp)

    @property
    def latest(self) -> Optional[SnapshotRecord]:
        return self.records[-1] if self.records else None

    @property
    def prices(self) -> List[float]:
        return [r.market_value for r in self.records]

    @property
    def spd_values(self) -> List[float]:
        return [r.sold_per_day for r in self.records]

    @property
    def sale_rates(self) -> List[float]:
        return [r.sale_rate for r in self.records]

    @property
    def count(self) -> int:
        return len(self.records)


@dataclass
class MarketState:
    """Computed market state for a single item at a point in time.

    This is the core data structure — all indicators feed into this.
    """
    item_id: int
    name: str
    timestamp: str

    # Raw data
    current_price: float = 0.0
    historical_mean: float = 0.0
    avg_sale_price: float = 0.0
    sale_rate: float = 0.0
    sold_per_day: float = 0.0

    # Moving averages
    sma_7: Optional[float] = None
    sma_30: Optional[float] = None
    ema: Optional[float] = None

    # Volatility
    volatility: float = 0.0
    z_score: float = 0.0
    percentile_rank: float = 0.0

    # Liquidity & value
    liquidity_score: float = 0.0
    item_value: float = 0.0        # AvgSalePrice (no SPD multiplier)
    market_size: float = 0.0       # SPD × AvgSalePrice — informational only

    # Trends
    momentum: float = 0.0
    price_trend: float = 1.0
    demand_trend: float = 0.0
    supply_trend: float = 0.0  # estimated

    # Stability
    price_stability: float = 0.0

    # Confidence
    confidence_score: float = 0.0

    # Classification
    classification: str = "Unknown"
    risk_level: str = "Unknown"

    def compute_liquidity(self) -> float:
        """Log-based liquidity score."""
        if self.sold_per_day <= 0 or self.sale_rate <= 0:
            return 0.0
        return math.log(1 + self.sold_per_day) * math.log(2 + self.sale_rate * 100)

    def compute_market_size(self) -> float:
        """Total gold circulating per day — informational only, NOT used in scoring."""
        return self.sold_per_day * self.avg_sale_price

    def compute_price_trend(self) -> float:
        if self.historical_mean > 0:
            return self.current_price / self.historical_mean
        return 1.0

    def compute_stability(self) -> float:
        """Price stability: 1.0 = perfectly stable, approaches 0 as volatility increases."""
        if self.historical_mean <= 0:
            return 1.0
        ratio = self.current_price / max(self.historical_mean, 0.01)
        return 1.0 / (1.0 + abs(math.log(max(ratio, 0.0001))))


@dataclass
class IndicatorResult:
    """Result of computing a single indicator."""
    name: str
    value: float
    normalized: float = 0.0  # 0-1
    signal: str = "neutral"  # bullish, bearish, neutral, extreme
    explanation: str = ""


@dataclass
class Forecast:
    """Price forecast result."""
    item_id: int
    next_day_price: Optional[float] = None
    next_7d_avg: Optional[float] = None
    next_7d_high: Optional[float] = None
    next_7d_low: Optional[float] = None
    confidence_interval: float = 0.0
    trend_direction: str = "unknown"  # up, down, stable
    seasonality_detected: bool = False
    forecast_available: bool = False
    error_message: str = ""


@dataclass
class MarketSignal:
    """A detected market event."""
    signal_type: str  # supply_shock, demand_shock, price_crash, etc.
    item_id: int
    item_name: str
    confidence: float  # 0-1
    severity: str  # low, medium, high, critical
    description: str = ""
    detected_at: str = ""
    current_value: float = 0.0
    previous_value: float = 0.0
    change_pct: float = 0.0


@dataclass
class Recommendation:
    """Farming/market recommendation for an item."""
    item_id: int
    item_name: str
    stars: int  # 1-5
    category: str  # farm, flip, invest, speculative, avoid
    title: str = ""
    explanation: List[str] = field(default_factory=list)
    farm_score: float = 0.0
    investment_score: float = 0.0
    flip_score: float = 0.0
    risk: str = "medium"
    market_classification: str = ""
    key_indicators: Dict[str, float] = field(default_factory=dict)
    score_breakdown: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self.title:
            self.title = _recommendation_title(self.stars)

    @property
    def star_display(self) -> str:
        filled = "★" * self.stars
        empty = "☆" * (5 - self.stars)
        return filled + empty


def _recommendation_title(stars: int) -> str:
    titles = {
        5: "Strong Farm",
        4: "Good Farm",
        3: "Average",
        2: "Avoid",
        1: "Dead Market",
    }
    return titles.get(stars, "Unknown")


@dataclass
class RiskProfile:
    """Risk assessment for a single item."""
    item_id: int
    liquidity_risk: float = 0.0
    price_risk: float = 0.0
    saturation_risk: float = 0.0
    volatility_risk: float = 0.0
    overall_risk: float = 0.0
    risk_level: str = "medium"
    risk_factors: List[str] = field(default_factory=list)
