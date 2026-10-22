"""Check pipeline with 2 real snapshots — verify indicators compute correctly."""
import sys
import numpy as np

from woweconomy import WowEconomyConfig
from woweconomy.services.pipeline import load_all_histories

db_path = r"C:\FarmAnalysis\tsm_markets.db"
histories, snap_count, warning = load_all_histories(db_path)

print(f"Snapshots: {snap_count}")
print(f"Items:     {len(histories)}")

# Check how many items have 2 records vs 1
counts = {}
for h in histories.values():
    c = h.count
    counts[c] = counts.get(c, 0) + 1
print(f"Record distribution: {dict(sorted(counts.items()))}")

# Find an item with 2 records and analyze it
from woweconomy.analytics.market_analyzer import MarketAnalyzer

cfg = WowEconomyConfig.default()
analyzer = MarketAnalyzer(cfg)

# First item with 2+ records
for item_id, h in histories.items():
    if h.count >= 2:
        state = analyzer.analyze(h)
        print(f"\n--- {state.name} (ID={item_id}, records={h.count}) ---")
        print(f"  Latest: MV={state.current_price:,.0f}g, hist={state.historical_mean:,.0f}g")
        print(f"  ASP={state.avg_sale_price:,.0f}g, SR={state.sale_rate:.4f}, SPD={state.sold_per_day:.1f}")
        print(f"  PriceStability={state.price_stability:.4f}")
        print(f"  LiquidityScore={state.liquidity_score:.4f}")
        print(f"  Momentum={state.momentum:+.6f}")
        print(f"  PriceTrend={state.price_trend:.4f}")
        print(f"  ZScore={state.z_score:.4f}")
        print(f"  Volatility={state.volatility:.6f}")
        print(f"  Classification={state.classification}")
        print(f"  RiskLevel={state.risk_level}")
        break
    if item_id > 100000:
        break

# Also run full pipeline with forecasting (now possible with 2 snapshots)
print("\n--- Checking forecast availability ---")
from woweconomy.forecasting import PriceForecaster
forecaster = PriceForecaster(cfg)

items_with_2 = [h for h in histories.values() if h.count >= 2]
forecastable = sum(1 for h in items_with_2 if h.count >= cfg.min_history_for_forecast)
print(f"Items with 2+ records: {len(items_with_2)}")
print(f"Items with >= {cfg.min_history_for_forecast} records (forecastable): {forecastable}")

# Run recommendations on just the items with 2+ records
print("\n--- Running full pipeline on items with 2+ records ---")
from woweconomy.signals import SignalDetector
from woweconomy.recommendations import RecommendationEngine

detector = SignalDetector(cfg)
engine = RecommendationEngine(cfg)

states = [analyzer.analyze(h) for h in items_with_2]
engine.fit(states)
signals_list = [detector.detect_all(h, s) for h, s in zip(items_with_2, states)]

# Only forecast where we have enough data
forecasts = []
for h in items_with_2:
    if h.count >= cfg.min_history_for_forecast:
        forecasts.append(forecaster.forecast(h))
    else:
        forecasts.append(None)

recs = engine.evaluate_batch(states, signals_list, forecasts)

print(f"\nTop 10 (items with 2+ records):")
hdr = f"{'ID':>8} {'Name':>28} {'Score':>6} | Demand  Liq   Value  Stab   Trend | SPD>8 SR>6"
print(hdr)
print('-' * len(hdr))
for rec in recs[:10]:
    s = rec.score_breakdown
    name = rec.item_name[:26] + ".." if len(rec.item_name) > 28 else rec.item_name
    print(
        f"{rec.item_id:>8} {name:>28} {rec.farm_score:>6.3f} | "
        f"{s.get('demand',0):.3f} {s.get('liquidity',0):.3f} {s.get('item_value',0):.3f} "
        f"{s.get('stability',0):.3f} {s.get('trend',0):.3f} | "
        f"{rec.key_indicators.get('spd',0):>8.1f} {rec.key_indicators.get('sr',0):>6.3f}"
    )
