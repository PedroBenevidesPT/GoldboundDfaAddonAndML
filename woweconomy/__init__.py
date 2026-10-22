"""woweconomy - World of Warcraft economy analysis engine.

A modular Python package inspired by quantitative finance,
adapted for the WoW Auction House.
"""

__version__ = "0.1.0"

from woweconomy.config import WowEconomyConfig
from woweconomy.models import (
    Forecast,
    ItemIdentity,
    MarketSignal,
    MarketState,
    PriceHistory,
    Recommendation,
    RiskProfile,
    SnapshotRecord,
)
from woweconomy.indicators import (
    EMA,
    SMA,
    AllIndicators,
    ItemValue,
    LiquidityScore,
    MeanReversion,
    Momentum,
    PercentileRank,
    PriceStability,
    RateOfChange,
    RelativeDemand,
    Volatility,
    ZScore,
)
from woweconomy.analytics import MarketAnalyzer, MarketAnalyzerBatch, MarketClassifier, RiskAnalyzer
from woweconomy.forecasting import PriceForecaster, TrendAnalyzer
from woweconomy.signals import SignalDetector
from woweconomy.recommendations import FarmAdvisor, RecommendationEngine
from woweconomy.services import DailyPipeline, TSMFetcher
from woweconomy.backtesting import BacktestEngine, PerformanceReport

__all__ = [
    "__version__",
    "WowEconomyConfig",
    # Models
    "ItemIdentity", "SnapshotRecord", "PriceHistory", "MarketState",
    "MarketSignal", "Forecast", "Recommendation", "RiskProfile",
    # Indicators
    "SMA", "EMA", "Momentum", "RateOfChange", "Volatility", "ZScore",
    "PercentileRank", "PriceStability", "LiquidityScore", "ItemValue",
    "MeanReversion", "RelativeDemand", "AllIndicators",
    # Analytics
    "MarketAnalyzer", "MarketAnalyzerBatch", "MarketClassifier", "RiskAnalyzer",
    # Forecasting
    "PriceForecaster", "TrendAnalyzer",
    # Signals
    "SignalDetector",
    # Recommendations
    "RecommendationEngine", "FarmAdvisor",
    # Services
    "TSMFetcher", "DailyPipeline",
    # Backtesting
    "BacktestEngine", "PerformanceReport",
]
