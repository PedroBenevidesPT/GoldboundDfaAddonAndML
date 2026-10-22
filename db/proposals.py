"""
Proposed Additional Tables for Long-Term Market Analytics
========================================================

Goal: extend the schema for deeper trend analysis, pattern detection,
and goblin-grade decision support, while keeping SQLite lightweight
(< 5 GB for millions of rows).

Current DB size: ~1.3 GB for 31k items × 1 snapshot.
At 365 snapshots/year × 31k items = ~11.3M rows, we expect ~4-5 GB.

------------------------------------------------------------------------
TABLE 1: snapshot_daily_aggregates
------------------------------------------------------------------------
Purpose: Pre-compute daily market snapshots so queries against
         "what happened last week" don't scan millions of rows.

Columns:
  date          TEXT  PK  -- YYYY-MM-DD
  snapshotId    INT   FK  -- last snapshot of that day
  itemCount     INT       -- items with active trades
  totalGold     REAL      -- SUM(marketValue * soldPerDay)
  avgPriceTrend REAL      -- AVG(marketValue / historical)
  categories    TEXT      -- JSON aggregator for per-category stats

Why: Avoids repeated window-function scans over market_snapshots
     for day-over-day comparisons. One row per day (365/year).

------------------------------------------------------------------------
TABLE 2: item_trend_signals
------------------------------------------------------------------------
Purpose: Store computed trend signals per item per snapshot so
         "crossed above 7-day moving average" queries are instant.

Columns:
  itemId            INT  PK
  snapshotId        INT  PK
  ma_7              REAL  -- 7-snapshot moving average of marketValue
  ma_30             REAL  -- 30-snapshot moving average
  volatility_7      REAL  -- std dev over last 7 snapshots
  volatility_30     REAL  -- std dev over last 30 snapshots
  momentum          REAL  -- (ma_7 - ma_30) / ma_30  -- bullish if > 0.05
  demand_trend      REAL  -- slope of soldPerDay over last 7 snapshots
  z_score           REAL  -- (marketValue - ma_30) / volatility_30

Why: Moving averages and z-scores are O(n) to compute and rarely
     change once computed. Materialising them makes trend screens
     (e.g. "items with positive momentum and low volatility") instant.

Size: ~1 KB per row. 11M rows → ~11 GB. This is the largest table.
      Consider partitioning: keep only last 90 snapshots for speed.
      OR move to a separate "trends" database that can be regenerated.

------------------------------------------------------------------------
TABLE 3: category_trends
------------------------------------------------------------------------
Purpose: Aggregate analytics by category per snapshot for market
         rotation analysis ("herbs up 5%, ore down 2%").

Columns:
  snapshotId    INT  PK
  category      TEXT PK
  medianScore   REAL
  medianSPD     REAL
  medianSR      REAL
  medianMV      REAL
  totalThroughput REAL
  itemCount     INT

Why: Enables category-level heatmaps and rotation alerts. Much faster
     than GROUP BY on the full market_snapshots table.

Size: ~15 categories × 365 snapshots/year → ~5,500 rows. Trivial.

------------------------------------------------------------------------
TABLE 4: flip_opportunities
------------------------------------------------------------------------
Purpose: Track items where minBuyout << marketValue, indicating
         arbitrage/resell opportunities (from commodities.csv).

Columns:
  snapshotId    INT  PK
  itemId        INT  PK
  minBuyout     REAL
  marketValue   REAL
  spread        REAL  -- (marketValue - minBuyout) / marketValue
  recent        REAL
  historical    REAL
  flipScore     REAL  -- spread × liquidity proxy

Why: Dedicated flip table means you don't need to JOIN commodities
     every time you scan for deals.

Size: Same as items (31k × snapshots). ~11M rows.

------------------------------------------------------------------------
TABLE 5: alerts_config (user-facing, not auto-populated)
------------------------------------------------------------------------
Purpose: Let the user define alert conditions for email/discord notifications.

Columns:
  alertId     INT PK
  itemId      INT    -- NULL = any item
  metric      TEXT   -- 'price_drop_7d', 'spike_volume', 'score_threshold'
  operator    TEXT   -- '>', '<', 'crosses_above'
  threshold   REAL
  active      INT DEFAULT 1
  last_triggered TEXT

Why: Separates alert configuration from alert evaluation logic.
     Allows automated monitoring: "tell me when any herb drops 20%".

------------------------------------------------------------------------
TABLE 6: import_log_metrics
------------------------------------------------------------------------
Purpose: Track performance and data quality of each CSV import.

Columns:
  snapshotId       INT  PK
  importDurationMs INT
  rowsParsed       INT
  rowsRejected     INT          -- items with zero values
  newItems         INT
  memoryMb         REAL
  pythonVersion    TEXT
  warnings         TEXT         -- JSON list of anomalies

Why: Helps detect data pipeline problems (e.g. corrupted CSVs,
     missing columns) before they pollute analytics.

------------------------------------------------------------------------
INDEX RECOMMENDATIONS for proposed tables
------------------------------------------------------------------------
- item_trend_signals:  INDEX on (momentum, volatility_7) for "low vol, high momentum" screens
- category_trends:     INDEX on (snapshotId, totalThroughput DESC)
- flip_opportunities:  INDEX on (spread DESC) for best deals first
- import_log_metrics:  INDEX on (importDurationMs DESC) for outlier detection

------------------------------------------------------------------------
PERFORMANCE NOTES FOR MILLIONS OF RECORDS
------------------------------------------------------------------------
1. WAL mode is essential for concurrent reads during import.
2. Set `PRAGMA cache_size = -200000` (200 MB) for production use.
3. For > 10M rows, consider `PRAGMA mmap_size = 268435456` (256 MB).
4. Run `ANALYZE` after each import to update query planner stats.
5. Consider monthly VACUUM if deletes ever occur.
6. For 1B+ row scale, migrate to PostgreSQL or DuckDB (columnar).
   SQLite is excellent up to ~100M rows on decent hardware.
"""

# This file is documentation only — run `python -m db.proposals` to print it.

if __name__ == "__main__":
    print(__doc__)
