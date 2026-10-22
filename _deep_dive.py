"""Deep dive on a specific item — real data only, no synthetic records."""
import sys

from woweconomy import WowEconomyConfig
from woweconomy.services.pipeline import load_all_histories
from woweconomy.recommendations import RecommendationEngine

ITEM_ID = 765  # Silverleaf

cfg = WowEconomyConfig.default()
cfg.min_history_for_forecast = 3

db_path = r"C:\FarmAnalysis\tsm_markets.db"
histories, snap_count, warning = load_all_histories(db_path)

if ITEM_ID not in histories:
    print(f"Item {ITEM_ID} not found"); sys.exit(1)

history = histories[ITEM_ID]
print(f"Records: {history.count} (real snapshots)")
if warning:
    print(f"NOTE: {warning}")

# Analyze
from woweconomy.analytics.market_analyzer import MarketAnalyzer
from woweconomy.signals import SignalDetector
from woweconomy.forecasting import PriceForecaster

state = MarketAnalyzer(cfg).analyze(history)
signals = SignalDetector(cfg).detect_all(history, state)

if history.count >= cfg.min_history_for_forecast:
    forecast = PriceForecaster(cfg).forecast(history)
else:
    forecast = None

# Build a reference population from all items
ref_histories = list(histories.values())
ref_states = [MarketAnalyzer(cfg).analyze(h) for h in ref_histories]

engine = RecommendationEngine(cfg)
engine.fit(ref_states)
rec = engine.evaluate(state, signals, forecast)

print(f"\n{'='*70}")
print(f"DEEP DIVE: {state.name} (ID={state.item_id})")
print(f"{'='*70}")
print(f"\n--- Raw Data (from snapshot #{history.records[0].snapshot_id}) ---")
print(f"  Market Value:       {state.current_price:>14,.2f} g")
print(f"  Historical Mean:    {state.historical_mean:>14,.2f} g")
print(f"  Avg Sale Price:     {state.avg_sale_price:>14,.2f} g")
print(f"  Sale Rate:          {state.sale_rate:>14.4f}")
print(f"  Sold Per Day:       {state.sold_per_day:>14,.1f}")
print(f"  Market Size (info): {state.market_size:>14,.0f} g/day")
print(f"  Item Value:         {state.item_value:>14,.0f} g")

print(f"\n--- Indicators ---")
print(f"  Price Stability:    {state.price_stability:>14.4f}")
print(f"  Liquidity Score:    {state.liquidity_score:>14.4f}")
print(f"  Momentum:           {state.momentum:>+14.4f}")
print(f"  Price Trend:        {state.price_trend:>14.4f}")
print(f"  Z-Score:            {state.z_score:>14.4f}")
print(f"  Volatility:         {state.volatility:>14.4f}")

if forecast and forecast.forecast_available:
    print(f"\n--- Forecast ---")
    print(f"  Trend:       {forecast.trend_direction}")
    print(f"  Next 7d Avg: {forecast.next_7d_avg:,.0f} g")
else:
    print(f"\n--- Forecast: skipped (insufficient history) ---")

print(f"\n--- Signals ({len(signals)}) ---")
for s in signals:
    print(f"  [{s.severity}] {s.signal_type}: {s.description}")

print(f"\n--- Score Breakdown ---")
w = cfg.farm_score_weights
sd = rec.score_breakdown
print(f"  Weights: demand={w['demand']:.2f}, liq={w['liquidity']:.2f}, "
      f"value={w['item_value']:.2f}, stab={w['stability']:.2f}, trend={w['trend']:.2f}")
print(f"  {'Dimension':<14} {'Score':>8} {'Weight':>8} {'Contrib':>10}")
print(f"  {'-'*14} {'-'*8} {'-'*8} {'-'*10}")
for dim in ["demand", "liquidity", "item_value", "stability", "trend"]:
    s = sd.get(dim, 0)
    print(f"  {dim:<14} {s:>8.3f} {w[dim]:>8.2f} {s * w[dim]:>10.3f}")
print(f"  {'Gate':<14} {sd.get('farm_gate',''):>30}")
print(f"  {'Final Score':<14} {rec.farm_score:>8.3f}")
print(f"\n  InvestmentScore: {rec.investment_score:.3f}")
print(f"  FlipScore:        {rec.flip_score:.3f}")
print(f"  Stars: {rec.stars}/5  Category: {rec.category}")

print(f"\n--- Explanation ---")
for e in rec.explanation:
    print(f"  {e}")
