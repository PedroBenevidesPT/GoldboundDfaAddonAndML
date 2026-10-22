"""
Analytical SQL queries for the TSM Market Snapshot database.

Usage:
    from db.queries import MarketQueries
    mq = MarketQueries()
    print(mq.price_history(152506))
"""
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import init_db


class MarketQueries:
    def __init__(self, db_url: str = "sqlite:///tsm_markets.db"):
        self.engine, self.Session = init_db(db_url)

    def session(self) -> Session:
        return self.Session()

    # ── 1. Price history of an item ──
    def price_history(self, item_id: int, limit: int = 100):
        sql = """
        SELECT s.snapshotTimestamp, ms.marketValue, ms.historical,
               ms.avgSalePrice, ms.soldPerDay, ms.saleRate
        FROM market_snapshots ms
        JOIN snapshots s ON s.snapshotId = ms.snapshotId
        WHERE ms.itemId = :item_id
        ORDER BY s.snapshotTimestamp DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(text(sql),
                     {"item_id": item_id, "lim": limit})]

    # ── 2. SaleRate history ──
    def sale_rate_history(self, item_id: int, limit: int = 100):
        sql = """
        SELECT s.snapshotTimestamp, ms.saleRate
        FROM market_snapshots ms
        JOIN snapshots s ON s.snapshotId = ms.snapshotId
        WHERE ms.itemId = :item_id
        ORDER BY s.snapshotTimestamp DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(text(sql),
                     {"item_id": item_id, "lim": limit})]

    # ── 3. SoldPerDay history ──
    def sold_per_day_history(self, item_id: int, limit: int = 100):
        sql = """
        SELECT s.snapshotTimestamp, ms.soldPerDay
        FROM market_snapshots ms
        JOIN snapshots s ON s.snapshotId = ms.snapshotId
        WHERE ms.itemId = :item_id
        ORDER BY s.snapshotTimestamp DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(text(sql),
                     {"item_id": item_id, "lim": limit})]

    # ── 4. Latest snapshot ID helper ──
    def latest_snapshot_id(self) -> Optional[int]:
        sql = "SELECT MAX(snapshotId) FROM snapshots"
        with self.session() as sess:
            r = sess.execute(text(sql)).scalar()
            return r

    # ── 5. Top FarmScore today ──
    # Full FarmScore computed at query time using window functions
    # to normalise across all items in the latest snapshot.
    def top_farmscore(self, top_n: int = 100):
        """
        Compute true FarmScore (log + geometric mean of 5 normalised dims)
        using percentile-based normalisation via window functions.
        """
        sid = self.latest_snapshot_id()
        if sid is None:
            return []

        sql = """
        WITH base AS (
            SELECT
                ms.itemId,
                i.name,
                ms.marketValue,
                ms.historical,
                ms.avgSalePrice,
                ms.saleRate,
                ms.soldPerDay,
                ms.marketValue / MAX(ms.historical, 0.01) AS raw_trend,
                LOG(1 + ms.soldPerDay) * LOG(2 + ms.saleRate * 100) AS liquidity,
                ms.soldPerDay * ms.avgSalePrice AS goldThroughput,
                1.0 / (1.0 + ABS(LOG(MAX(ms.marketValue, 0.01) / MAX(ms.historical, 0.01)))) AS stability,
                MIN(ms.avgSalePrice / MAX(ms.marketValue, 0.01), 1.0) AS marginHealth,
                LOG(1 + ms.soldPerDay * ms.avgSalePrice) AS logThroughput
            FROM market_snapshots ms
            JOIN items i ON i.itemId = ms.itemId
            WHERE ms.snapshotId = :sid
              AND ms.soldPerDay > 0 AND ms.saleRate > 0
        ),
        percentiles AS (
            SELECT
                *,
                (liquidity - MIN(liquidity) OVER ()) / NULLIF(MAX(liquidity) OVER () - MIN(liquidity) OVER (), 0) AS n_liq,
                (logThroughput - MIN(logThroughput) OVER ()) / NULLIF(MAX(logThroughput) OVER () - MIN(logThroughput) OVER (), 0) AS n_thr,
                (stability - MIN(stability) OVER ()) / NULLIF(MAX(stability) OVER () - MIN(stability) OVER (), 0) AS n_stab,
                (raw_trend - MIN(raw_trend) OVER ()) / NULLIF(MAX(raw_trend) OVER () - MIN(raw_trend) OVER (), 0) AS n_trend,
                (marginHealth - MIN(marginHealth) OVER ()) / NULLIF(MAX(marginHealth) OVER () - MIN(marginHealth) OVER (), 0) AS n_margin
            FROM base
        ),
        scored AS (
            SELECT
                *,
                ROUND(
                    EXP(
                        (LOG(MAX(n_liq, 0.001)) + LOG(MAX(n_thr, 0.001)) +
                         LOG(MAX(n_stab, 0.001)) + LOG(MAX(n_trend, 0.001)) +
                         LOG(MAX(n_margin, 0.001))) / 5.0
                    ) * 100, 2
                ) AS farmScore
            FROM percentiles
        )
        SELECT name, farmScore, marketValue, avgSalePrice, saleRate, soldPerDay,
               ROUND(goldThroughput, 0) AS goldThroughput, ROUND(raw_trend, 4) AS priceTrend,
               ROUND(liquidity, 2) AS liquidity, ROUND(stability, 4) AS stability
        FROM scored
        ORDER BY farmScore DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"sid": sid, "lim": top_n})]

    # ── 6. Biggest price increases over N snapshots ──
    def biggest_price_increases(self, window: int = 1, top_n: int = 50):
        """Items with largest marketValue % increase between latest and window-th snapshot."""
        sid = self.latest_snapshot_id()
        if sid is None or sid <= window:
            return []
        prev_sid = sid - window

        sql = """
        SELECT i.name,
               curr.marketValue AS currentValue,
               prev.marketValue AS previousValue,
               ROUND((curr.marketValue - prev.marketValue) / NULLIF(prev.marketValue, 0) * 100, 2) AS pctChange,
               curr.soldPerDay, curr.saleRate, curr.avgSalePrice
        FROM market_snapshots curr
        JOIN market_snapshots prev ON prev.itemId = curr.itemId AND prev.snapshotId = :prev_sid
        JOIN items i ON i.itemId = curr.itemId
        WHERE curr.snapshotId = :sid
          AND prev.marketValue > 0
          AND curr.marketValue > prev.marketValue
        ORDER BY pctChange DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"sid": sid, "prev_sid": prev_sid, "lim": top_n})]

    # ── 7. Biggest price decreases ──
    def biggest_price_decreases(self, window: int = 1, top_n: int = 50):
        sid = self.latest_snapshot_id()
        if sid is None or sid <= window:
            return []
        prev_sid = sid - window

        sql = """
        SELECT i.name,
               curr.marketValue AS currentValue,
               prev.marketValue AS previousValue,
               ROUND((curr.marketValue - prev.marketValue) / NULLIF(prev.marketValue, 0) * 100, 2) AS pctChange,
               curr.soldPerDay, curr.saleRate, curr.avgSalePrice
        FROM market_snapshots curr
        JOIN market_snapshots prev ON prev.itemId = curr.itemId AND prev.snapshotId = :prev_sid
        JOIN items i ON i.itemId = curr.itemId
        WHERE curr.snapshotId = :sid
          AND prev.marketValue > 0
          AND curr.marketValue < prev.marketValue
        ORDER BY pctChange ASC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"sid": sid, "prev_sid": prev_sid, "lim": top_n})]

    # ── 8. Fastest growing demand (soldPerDay increase) ──
    def fastest_growing_demand(self, window: int = 1, top_n: int = 50):
        sid = self.latest_snapshot_id()
        if sid is None or sid <= window:
            return []
        prev_sid = sid - window

        sql = """
        SELECT i.name,
               curr.soldPerDay AS currentSPD,
               prev.soldPerDay AS previousSPD,
               ROUND((curr.soldPerDay - prev.soldPerDay) / NULLIF(prev.soldPerDay, 0) * 100, 2) AS pctChange,
               curr.marketValue, curr.avgSalePrice, curr.saleRate
        FROM market_snapshots curr
        JOIN market_snapshots prev ON prev.itemId = curr.itemId AND prev.snapshotId = :prev_sid
        JOIN items i ON i.itemId = curr.itemId
        WHERE curr.snapshotId = :sid
          AND prev.soldPerDay > 0
          AND curr.soldPerDay > prev.soldPerDay
        ORDER BY pctChange DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"sid": sid, "prev_sid": prev_sid, "lim": top_n})]

    # ── 9. Highest Gold Throughput ──
    def highest_gold_throughput(self, top_n: int = 100):
        sid = self.latest_snapshot_id()
        if sid is None:
            return []

        sql = """
        SELECT i.name,
               ROUND(ia.goldThroughput, 0) AS goldThroughput,
               ms.marketValue, ms.soldPerDay, ms.avgSalePrice, ms.saleRate,
               ROUND(ia.farmScore, 2) AS farmScore
        FROM item_analytics ia
        JOIN items i ON i.itemId = ia.itemId
        JOIN market_snapshots ms ON ms.itemId = ia.itemId AND ms.snapshotId = ia.snapshotId
        WHERE ia.snapshotId = :sid
        ORDER BY ia.goldThroughput DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"sid": sid, "lim": top_n})]

    # ── 10. Moving averages (7 and 30 snapshots) ──
    def moving_averages(self, item_id: int, short_window: int = 7, long_window: int = 30):
        """
        Compute short and long moving average of marketValue for an item.
        Uses the most recent N snapshots.
        """
        sql = """
        WITH ordered AS (
            SELECT s.snapshotId, s.snapshotTimestamp, ms.marketValue
            FROM market_snapshots ms
            JOIN snapshots s ON s.snapshotId = ms.snapshotId
            WHERE ms.itemId = :item_id
            ORDER BY s.snapshotId DESC
        ),
        with_ma AS (
            SELECT *,
                AVG(marketValue) OVER (ORDER BY snapshotId DESC ROWS BETWEEN :short-1 PRECEDING AND CURRENT ROW) AS ma_short,
                AVG(marketValue) OVER (ORDER BY snapshotId DESC ROWS BETWEEN :long-1 PRECEDING AND CURRENT ROW) AS ma_long
            FROM ordered
        )
        SELECT snapshotTimestamp, marketValue,
               ROUND(ma_short, 0) AS ma_7,
               ROUND(ma_long, 0) AS ma_30
        FROM with_ma
        ORDER BY snapshotId DESC
        LIMIT :short
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"item_id": item_id, "short": short_window, "long": long_window})]

    # ── 11. Volatility (standard deviation of marketValue) ──
    def volatility(self, item_id: int, window: int = 30):
        sql = """
        WITH recent AS (
            SELECT ms.marketValue
            FROM market_snapshots ms
            WHERE ms.itemId = :item_id
            ORDER BY ms.snapshotId DESC
            LIMIT :win
        )
        SELECT ROUND(AVG(marketValue), 0) AS meanValue,
               ROUND(SQRT(AVG(marketValue * marketValue) - AVG(marketValue) * AVG(marketValue)), 0) AS stdDev,
               ROUND(SQRT(AVG(marketValue * marketValue) - AVG(marketValue) * AVG(marketValue))
                     / NULLIF(AVG(marketValue), 0) * 100, 2) AS cvPct
        FROM recent
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"item_id": item_id, "win": window})]

    # ── 12. Best farming opportunities ──
    def best_farming_opportunities(self, top_n: int = 100):
        """
        Score items using the full log + geometric mean FarmScore
        from the latest snapshot, returning the top opportunities.
        """
        return self.top_farmscore(top_n)

    # ── 13. Item by category ──
    def items_by_category(self, category: str, top_n: int = 50):
        sid = self.latest_snapshot_id()
        if sid is None:
            return []

        sql = """
        SELECT i.name, ROUND(ia.farmScore, 2) AS farmScore,
               ROUND(ia.goldThroughput, 0) AS goldThroughput,
               ms.marketValue, ms.avgSalePrice, ms.saleRate, ms.soldPerDay
        FROM item_analytics ia
        JOIN items i ON i.itemId = ia.itemId
        JOIN market_snapshots ms ON ms.itemId = ia.itemId AND ms.snapshotId = ia.snapshotId
        JOIN item_categories ic ON ic.itemId = ia.itemId
        WHERE ia.snapshotId = :sid AND ic.category = :cat
        ORDER BY ia.farmScore DESC
        LIMIT :lim
        """
        with self.session() as sess:
            return [dict(r._mapping) for r in sess.execute(
                text(sql), {"sid": sid, "cat": category, "lim": top_n})]

    # ── 14. Market summary ──
    def market_summary(self):
        sid = self.latest_snapshot_id()
        if sid is None:
            return {}

        sql = """
        SELECT
            COUNT(*) AS totalItems,
            ROUND(AVG(ms.marketValue), 0) AS avgMarketValue,
            ROUND(AVG(ms.soldPerDay), 2) AS avgSoldPerDay,
            ROUND(AVG(ms.saleRate), 4) AS avgSaleRate,
            ROUND(SUM(ms.soldPerDay * ms.avgSalePrice), 0) AS totalGoldThroughput,
            ROUND(AVG(ms.marketValue / MAX(ms.historical, 0.01)), 4) AS avgPriceTrend
        FROM market_snapshots ms
        WHERE ms.snapshotId = :sid
          AND ms.soldPerDay > 0
        """
        with self.session() as sess:
            return dict(sess.execute(text(sql), {"sid": sid}).mappings().first() or {})


def run_all_queries(db_url: str = "sqlite:///tsm_markets.db"):
    """Run all analytical queries and print results."""
    mq = MarketQueries(db_url)

    print("=" * 70)
    print("  MARKET SUMMARY (Latest Snapshot)")
    print("=" * 70)
    summary = mq.market_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 70)
    print("  TOP 20 FARMSCORE")
    print("=" * 70)
    for r in mq.top_farmscore(20):
        print(f"  {r['name']:<45} Score={r['farmScore']:<7.2f}  "
              f"MV={r['marketValue']:<12,.0f}  SPD={r['soldPerDay']:<10.1f}  "
              f"Throughput={r['goldThroughput']:<15,.0f}")

    print("\n" + "=" * 70)
    print("  TOP 10 GOLD THROUGHPUT")
    print("=" * 70)
    for r in mq.highest_gold_throughput(10):
        print(f"  {r['name']:<45} {r['goldThroughput']:<18,.0f} g/d  "
              f"SPD={r['soldPerDay']:<10.1f}  Score={r['farmScore']:<7.2f}")

    # Price history for a known item
    print("\n" + "=" * 70)
    print("  PRICE HISTORY: Star Moss (itemId=152506)")
    print("=" * 70)
    for r in mq.price_history(152506, limit=5):
        print(f"  {r['snapshotTimestamp']}  MV={r['marketValue']:<10,.0f}  "
              f"Hist={r['historical']:<10,.0f}  SPD={r['soldPerDay']:<8.1f}")

    # Volatility
    print("\n" + "=" * 70)
    print("  VOLATILITY: Star Moss (itemId=152506)")
    print("=" * 70)
    for r in mq.volatility(152506):
        print(f"  Mean={r['meanValue']:<10,.0f}  StdDev={r['stdDev']:<10,.0f}  CV={r['cvPct']:<6.2f}%")


if __name__ == "__main__":
    run_all_queries()
