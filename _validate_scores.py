"""Full validation — real data only, distribution, gate enforcement, Top 20 breakdown."""
import sys
import numpy as np

from woweconomy import WowEconomyConfig
from woweconomy.analytics.market_analyzer import MarketAnalyzer
from woweconomy.services.pipeline import load_all_histories
from woweconomy.recommendations import RecommendationEngine

cfg = WowEconomyConfig.default()
cfg.min_history_for_forecast = 3

db_path = r"C:\FarmAnalysis\tsm_markets.db"

# Load real data — no synthetic records
histories, snap_count, warning = load_all_histories(db_path)
print(f"Loaded {snap_count} snapshot(s), {len(histories)} items", file=sys.stderr)
if warning:
    print(f"NOTE: {warning}", file=sys.stderr)

histories_list = list(histories.values())

# Analyze all items
analyzer = MarketAnalyzer(cfg)
states = [analyzer.analyze(h) for h in histories_list]
print(f"Analyzed {len(states)} states", file=sys.stderr)

# Score all items
engine = RecommendationEngine(cfg)
engine.fit(states)

# Evaluate each with signals (but NOT forecast — insufficient history)
from woweconomy.signals import SignalDetector
detector = SignalDetector(cfg)

signals_list = [detector.detect_all(h, s) for h, s in zip(histories_list, states)]

recs = engine.evaluate_batch(states, signals_list, forecasts=None)
scores = np.array([r.farm_score for r in recs])

# ================================================================
# REPORT 1: Score Distribution
# ================================================================
print()
print("=" * 70)
print("FARMSCORE DISTRIBUTION (real data only)")
print("=" * 70)
print(f"  Count:           {len(scores)}")
print(f"  Min:             {scores.min():.4f}")
print(f"  Max:             {scores.max():.4f}")
print(f"  Mean:            {scores.mean():.4f}")
print(f"  Median:          {np.median(scores):.4f}")
print(f"  Std Dev:         {scores.std():.4f}")
print(f"  p90:             {np.percentile(scores, 90):.4f}")
print(f"  p95:             {np.percentile(scores, 95):.4f}")
print(f"  p99:             {np.percentile(scores, 99):.4f}")

# ================================================================
# REPORT 2: Gate Violation Check
# ================================================================
print()
print("=" * 70)
print("GATE VIOLATION CHECK")
print("=" * 70)
top1000 = recs[:1000]
violations_spd = sum(1 for r in top1000 if r.key_indicators.get("spd", 0) == 0)
violations_sr = sum(1 for r in recs[:500] if r.key_indicators.get("sr", 0) < 0.01)
print(f"  SPD=0 in Top 1000: {violations_spd}")
print(f"  SR<0.01 in Top 500: {violations_sr}")
if violations_spd == 0 and violations_sr == 0:
    print("  PASS: No gate violations")

# ================================================================
# REPORT 3: Saturation
# ================================================================
above_85 = (scores >= 0.85).sum()
pct = above_85 / len(scores) * 100
print()
print("=" * 70)
print("SATURATION CHECK")
print("=" * 70)
print(f"  Items >= 0.85: {above_85} ({pct:.2f}%)")
if pct > 5:
    print("  WARNING: Saturated (>5%)")
else:
    print("  OK: Below 5% threshold")

# ================================================================
# REPORT 4: Dimension Dominance (Top 100)
# ================================================================
print()
print("=" * 70)
print("DIMENSION CONTRIBUTION (Top 100)")
print("=" * 70)
dim_names = ["demand", "liquidity", "item_value", "stability", "trend"]
w = cfg.farm_score_weights
totals = {d: 0.0 for d in dim_names}
for r in recs[:100]:
    for d in dim_names:
        totals[d] += r.score_breakdown.get(d, 0)
for d in dim_names:
    avg = totals[d] / 100
    contrib = avg * w[d]
    pct_of_final = contrib / (sum(totals[dd] / 100 * w[dd] for dd in dim_names)) * 100
    print(f"  {d:<14} avg_score={avg:.4f}  w={w[d]:.2f}  "
          f"avg_contrib={contrib:.4f}  ({pct_of_final:.1f}% of final)")

# ================================================================
# REPORT 5: Top 20
# ================================================================
print()
print("=" * 70)
print("TOP 20 — Full Breakdown (Real Data)")
print("=" * 70)
hdr = (f"{'ID':>8} {'Name':>28} {'Score':>6} | "
       f"{'Demand':>7} {'Liq':>7} {'Value':>7} {'Stab':>7} {'Trend':>7} | "
       f"{'SPD':>8} {'SR':>6}")
print(hdr)
print('-' * len(hdr))
for rec in recs[:20]:
    s = rec.score_breakdown
    name = rec.item_name[:26] + ".." if len(rec.item_name) > 28 else rec.item_name
    spd = rec.key_indicators.get("spd", 0)
    sr = rec.key_indicators.get("sr", 0)
    print(
        f"{rec.item_id:>8} {name:>28} {rec.farm_score:>6.3f} | "
        f"{s.get('demand',0):>7.3f} {s.get('liquidity',0):>7.3f} "
        f"{s.get('item_value',0):>7.3f} {s.get('stability',0):>7.3f} "
        f"{s.get('trend',0):>7.3f} | "
        f"{spd:>8.1f} {sr:>6.3f}"
    )

# ================================================================
# REPORT 6: Histogram
# ================================================================
print()
print("=" * 70)
print("HISTOGRAM (20 buckets)")
print("=" * 70)
buckets = 20
hist, edges = np.histogram(scores, bins=buckets, range=(0, 1))
max_bar = max(hist) if max(hist) > 0 else 1
for i in range(buckets):
    lo = edges[i]; hi = edges[i + 1]
    count = hist[i]
    bar = "#" * int(count / max_bar * 40)
    print(f"  [{lo:.2f}-{hi:.2f}) {count/len(scores)*100:5.1f}% |{bar} {count}")
