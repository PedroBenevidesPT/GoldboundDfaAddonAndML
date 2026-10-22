"""Configuration for the woweconomy engine."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WowEconomyConfig:
    """Global configuration for the economy engine."""

    # TSM API
    game_type: str = "retail"
    region_slug: str = "eu"
    realm_slug: str = "twisting-nether"

    # Database
    db_url: str = "sqlite:///tsm_markets.db"
    metadata_csv_path: str = ""  # optional CSV with item metadata

    # Indicator defaults
    sma_short_window: int = 7
    sma_long_window: int = 30
    ema_alpha: float = 0.3
    momentum_window: int = 7
    volatility_window: int = 14
    z_score_window: int = 30

    # Forecasting
    forecast_horizon: int = 7
    min_history_for_forecast: int = 14
    arima_order: tuple = (1, 1, 1)
    seasonal_period: int = 7

    # Signals
    shock_z_threshold: float = 2.0
    crash_pct_threshold: float = -0.15
    spike_pct_threshold: float = 0.25
    saturation_spd_decline: float = -0.20
    dead_market_spd_threshold: float = 0.1
    dead_market_sr_threshold: float = 0.005

    # ========================================================
    # Vendor items
    # ========================================================
    exclude_vendor_items: bool = True

    # ========================================================
    # FarmScore — hard gates
    # ========================================================
    min_spd_for_farm: float = 5.0
    min_sr_for_farm: float = 0.01
    gate_penalty: float = 0.1

    farm_score_weights: dict = field(default_factory=lambda: {
        "demand": 0.35,
        "liquidity": 0.25,
        "item_value": 0.20,
        "stability": 0.10,
        "trend": 0.10,
    })

    # InvestmentScore — disabled until enough history
    min_history_for_investment: int = 30
    investment_score_weights: dict = field(default_factory=lambda: {
        "trend": 0.30,
        "momentum": 0.25,
        "mean_reversion": 0.20,
        "volatility": 0.15,
        "historical_ratio": 0.10,
    })

    # FlipScore — redesign around expected profit
    ah_cut_pct: float = 0.05  # 5% AH cut
    flip_score_weights: dict = field(default_factory=lambda: {
        "expected_roi": 0.35,
        "demand": 0.20,
        "liquidity": 0.20,
        "time_to_sell": 0.15,
        "price_discount": 0.10,
    })
    flip_min_roi: float = 0.05  # 5% minimum ROI for a flip to score > 0
    flip_max_time_to_sell_days: float = 30.0

    # Percentiles
    demand_percentile_cap: float = 95.0
    liquidity_percentile_cap: float = 99.0
    value_percentile_cap: float = 95.0
    trend_dev_percentile_cap: float = 99.0

    # Star mapping
    star_thresholds: dict = field(default_factory=lambda: {
        5: 0.80,
        4: 0.60,
        3: 0.40,
        2: 0.20,
        1: 0.00,
    })

    # Risk
    risk_free_gold_rate: float = 0.02

    # Data fetching
    tsm_base_url: str = "https://public-data.tradeskillmaster.com"
    fetch_timeout_seconds: int = 60
    max_retries: int = 3
    retry_delay_seconds: int = 5

    @classmethod
    def default(cls) -> "WowEconomyConfig":
        return cls()

    @classmethod
    def for_realm(cls, realm_slug: str, region_slug: str = "eu",
                  game_type: str = "retail") -> "WowEconomyConfig":
        return cls(realm_slug=realm_slug, region_slug=region_slug, game_type=game_type)
