"""Run the full pipeline — top farms, flips, and investments."""
import sys
import numpy as np

from woweconomy import WowEconomyConfig
from woweconomy.services.pipeline import load_all_histories
from woweconomy.analytics.market_analyzer import MarketAnalyzer
from woweconomy.signals import SignalDetector
from woweconomy.forecasting import PriceForecaster
from woweconomy.recommendations import RecommendationEngine, validate_recommendations
from woweconomy.posthog import posthog_client
from woweconomy.posthog_logs import posthog_logger

cfg = WowEconomyConfig.default()
db_path = r"C:\FarmAnalysis\tsm_markets.db"

# ── Load ──
print("Loading data...", file=sys.stderr)
histories, snap_count, warning = load_all_histories(db_path)
if warning:
    print(f"  {warning}", file=sys.stderr)
histories_list = list(histories.values())
print(f"  {len(histories_list)} items, {snap_count} snapshot(s)", file=sys.stderr)
posthog_logger.info(
    "pipeline_input_loaded",
    extra={"item_count": len(histories_list), "snapshot_count": snap_count},
)

# ── Analyze ──
print("Analyzing...", file=sys.stderr)
analyzer = MarketAnalyzer(cfg)
states = [analyzer.analyze(h) for h in histories_list]

# ── Signals ──
detector = SignalDetector(cfg)
signals_list = [detector.detect_all(h, s) for h, s in zip(histories_list, states)]

# ── Forecast ──
forecaster = PriceForecaster(cfg)
forecasts = []
for h in histories_list:
    if h.count >= cfg.min_history_for_forecast:
        forecasts.append(forecaster.forecast(h))
    else:
        forecasts.append(None)

# ── Score ──
print("Scoring...", file=sys.stderr)
engine = RecommendationEngine(cfg)
record_counts = [len(h.records) for h in histories_list]
recs = engine.evaluate_batch(states, record_counts=record_counts,
                             signals_list=signals_list, forecasts=forecasts)

# ── Validate ──
issues = validate_recommendations(recs, engine.metadata, snap_count)
for iss in issues:
    print(f"  [{iss.severity.upper()}] {iss.message}", file=sys.stderr)

by_farm = sorted(recs, key=lambda r: r.farm_score, reverse=True)
by_flip = sorted(recs, key=lambda r: r.flip_score, reverse=True)
by_inv  = sorted(recs, key=lambda r: r.investment_score, reverse=True)

# ────────────────────────────────────────
# TOP 50 FARM
# ────────────────────────────────────────
sep = "-" * 120
print(f"\n{'='*120}")
print("TOP 50  FARMSCORE")
print(f"{'='*120}")
print(f"{'#':>3} {'ID':>8} {'Nome':<30} {'Score':>6} {'SPD':>8} {'SR':>6} {'Preco':>12} {'Conf':>6} {'Inv':>6} {'Flip':>6} {'Gate':<20}")
print(sep)
for i, rec in enumerate(by_farm[:50], 1):
    spd = rec.key_indicators.get("spd", 0)
    sr = rec.key_indicators.get("sr", 0)
    price = rec.key_indicators.get("price", 0)
    nome = rec.item_name[:28] + ".." if len(rec.item_name) > 30 else rec.item_name
    conf = rec.score_breakdown.get("confidence_label", "")
    gate = rec.score_breakdown.get("farm_gate", "")[:18]
    print(
        f"{i:>3} {rec.item_id:>8} {nome:<30} "
        f"{rec.farm_score:>6.3f} "
        f"{spd:>8.1f} {sr:>6.3f} {price:>12,.0f} "
        f"{conf:>6} "
        f"{rec.investment_score:>6.3f} {rec.flip_score:>6.3f} "
        f"{gate:<20}"
    )

# ────────────────────────────────────────
# TOP 10 FLIP
# ────────────────────────────────────────
print(f"\n{'='*120}")
print("TOP 10  FLIPSCORE")
print(f"{'='*120}")
print(f"{'#':>3} {'ID':>8} {'Nome':<30} {'Flip':>6} {'Custo':>10} {'Venda':>10} {'ROI':>8} {'Lucro':>10} {'Dias':>6} {'Farm':>6}")
print(sep)
for i, rec in enumerate(by_flip[:10], 1):
    nome = rec.item_name[:28] + ".." if len(rec.item_name) > 30 else rec.item_name
    sb = rec.score_breakdown
    cost_price = int(sb.get("flip_cost_price", 0))
    exp_sale = int(sb.get("flip_expected_sale", 0))
    roi = sb.get("flip_roi", 0) * 100
    profit = sb.get("flip_profit", 0)
    days = sb.get("flip_days", 0)
    print(
        f"{i:>3} {rec.item_id:>8} {nome:<30} "
        f"{rec.flip_score:>6.3f} "
        f"{cost_price:>10,} {exp_sale:>10,} "
        f"{roi:>7.1f}% {profit:>10,.0f}g {days:>6.1f} "
        f"{rec.farm_score:>6.3f}"
    )

# ────────────────────────────────────────
# TOP 10 INVESTMENT
# ────────────────────────────────────────
print(f"\n{'='*120}")
print("TOP 10  INVESTMENTSCORE")
print(f"{'='*120}")
print(f"{'#':>3} {'ID':>8} {'Nome':<30} {'Inv':>6} {'Preco':>12} {'Conf':>6} {'Farm':>6}")
print(sep)
for i, rec in enumerate(by_inv[:10], 1):
    nome = rec.item_name[:28] + ".." if len(rec.item_name) > 30 else rec.item_name
    price = rec.key_indicators.get("price", 0)
    conf = rec.score_breakdown.get("confidence_label", "")
    print(
        f"{i:>3} {rec.item_id:>8} {nome:<30} "
        f"{rec.investment_score:>6.3f} "
        f"{price:>12,.0f} "
        f"{conf:>6} "
        f"{rec.farm_score:>6.3f}"
    )
print()
posthog_logger.info(
    "pipeline_scoring_completed",
    extra={
        "item_count": len(histories_list),
        "recommendation_count": len(recs),
        "validation_issue_count": len(issues),
        "forecast_count": sum(forecast is not None for forecast in forecasts),
    },
)

if posthog_client:
    posthog_client.capture(
        event="pipeline_scored",
        properties={
            "item_count": len(histories_list),
            "snapshot_count": snap_count,
            "recommendation_count": len(recs),
            "validation_issue_count": len(issues),
            "forecast_count": sum(forecast is not None for forecast in forecasts),
        },
    )
