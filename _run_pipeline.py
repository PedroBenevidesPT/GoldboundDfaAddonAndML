"""Run the pipeline on real SQLite data — no synthetic records."""
import sys
import numpy as np

from woweconomy import WowEconomyConfig
from woweconomy.services.pipeline import DailyPipeline

cfg = WowEconomyConfig.default()
cfg.min_history_for_forecast = 3  # for demo, real default is 14

db_path = r"C:\FarmAnalysis\tsm_markets.db"
pipeline = DailyPipeline(cfg)

print("Loading real historical data from SQLite...", file=sys.stderr)
recs, warning = pipeline.run(db_path, max_items=None, top_n=31067)

if warning:
    print(f"\nWARNING: {warning}")
    print()

# ================================================================
# Distribution
# ================================================================
scores = np.array([r.farm_score for r in recs])
print("=" * 70)
print("FARMSCORE DISTRIBUTION (all items, real data only)")
print("=" * 70)
print(f"  Count:   {len(scores)}")
print(f"  Min:     {scores.min():.4f}")
print(f"  Max:     {scores.max():.4f}")
print(f"  Mean:    {scores.mean():.4f}")
print(f"  Median:  {np.median(scores):.4f}")
print(f"  Std:     {scores.std():.4f}")
print(f"  p90:     {np.percentile(scores, 90):.4f}")
print(f"  p95:     {np.percentile(scores, 95):.4f}")
print(f"  p99:     {np.percentile(scores, 99):.4f}")

# ================================================================
# Gate Violations
# ================================================================
print()
print("=" * 70)
print("GATE VIOLATION CHECK")
print("=" * 70)
top1000 = recs[:1000]
violations_spd = sum(1 for r in top1000
                     if r.key_indicators.get("spd", 0) == 0)
violations_sr = sum(1 for r in recs[:500]
                    if r.key_indicators.get("sr", 0) < 0.01)
print(f"  SPD=0 in Top 1000: {violations_spd}")
print(f"  SR<0.01 in Top 500: {violations_sr}")
if violations_spd == 0 and violations_sr == 0:
    print("  PASS: No gate violations")
else:
    # Show the violators
    for r in recs[:500]:
        if r.key_indicators.get("sr", 0) < 0.01:
            print(f"    VIOLATION: ID={r.item_id} {r.item_name} "
                  f"SR={r.key_indicators['sr']:.4f} score={r.farm_score:.4f}")

# ================================================================
# Top 20
# ================================================================
print()
print("=" * 70)
print("TOP 50 — REAL DATA ONLY")
print("=" * 70)
hdr = (f"{'ID':>8} {'Name':>28} {'Score':>6} {'Stars':>5} | "
       f"{'Demand':>7} {'Liq':>7} {'Value':>7} {'Stab':>7} {'Trend':>7} | "
       f"{'SPD':>8} {'SR':>6} {'Price':>12} {'Inv':>6} {'Flip':>6}")
print(hdr)
print('-' * len(hdr))
for rec in recs[:50]:
    s = rec.score_breakdown
    spd = rec.key_indicators.get("spd", 0)
    sr = rec.key_indicators.get("sr", 0)
    price = rec.key_indicators.get("price", 0)
    name = rec.item_name[:26] + ".." if len(rec.item_name) > 28 else rec.item_name
    print(
        f"{rec.item_id:>8} {name:>28} "
        f"{rec.farm_score:>6.3f} {rec.stars:>4}/5 | "
        f"{s.get('demand',0):>7.3f} {s.get('liquidity',0):>7.3f} "
        f"{s.get('item_value',0):>7.3f} {s.get('stability',0):>7.3f} "
        f"{s.get('trend',0):>7.3f} | "
        f"{spd:>8.1f} {sr:>6.3f} {price:>12,.0f} "
        f"{rec.investment_score:>6.3f} {rec.flip_score:>6.3f}"
    )

# ================================================================
# Histogram
# ================================================================
print()
print("=" * 70)
print("HISTOGRAM (20 buckets)")
print("=" * 70)
buckets = 20
hist, edges = np.histogram(scores, bins=buckets, range=(0, 1))
max_bar = max(hist) if max(hist) > 0 else 1
for i in range(buckets):
    lo = edges[i]
    hi = edges[i + 1]
    count = hist[i]
    bar = "#" * int(count / max_bar * 40)
    pct = count / len(scores) * 100
    print(f"  [{lo:.2f}-{hi:.2f}) {pct:5.1f}% |{bar} {count}")
