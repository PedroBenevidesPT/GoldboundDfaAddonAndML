"""Production pipeline — loads real historical data from SQLite, no synthetic records."""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from woweconomy.analytics.market_analyzer import MarketAnalyzer
from woweconomy.config import WowEconomyConfig
from woweconomy.forecasting import PriceForecaster
from woweconomy.models import (
    Forecast,
    ItemIdentity,
    MarketSignal,
    MarketState,
    PriceHistory,
    Recommendation,
    SnapshotRecord,
)

from woweconomy.signals import SignalDetector

logger = logging.getLogger(__name__)


def load_all_histories(db_path: str) -> Tuple[Dict[int, PriceHistory], int, str]:
    """Load all market snapshots from SQLite in one query.

    Returns
    -------
    histories : dict of item_id -> PriceHistory
        One PriceHistory per item containing every real SnapshotRecord.
    snapshot_count : int
        Total distinct snapshots in the database.
    warning : str
        Empty string if multiple snapshots exist, else a warning message.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Count distinct snapshots
    snap_count = cur.execute(
        "SELECT COUNT(*) FROM snapshots"
    ).fetchone()[0]

    warning = ""
    if snap_count <= 1:
        warning = (
            f"Only {snap_count} snapshot(s) available. "
            "Historical analytics and forecasting are disabled "
            "until more snapshots are imported."
        )

    # Single ordered query — no N+1
    rows = cur.execute("""
        SELECT
            ms.snapshotId,
            s.snapshotTimestamp,
            ms.itemId,
            ms.marketValue,
            ms.historical,
            ms.avgSalePrice,
            ms.saleRate,
            ms.soldPerDay,
            COALESCE(i.name, 'Item-' || ms.itemId)
        FROM market_snapshots ms
        JOIN snapshots s ON ms.snapshotId = s.snapshotId
        LEFT JOIN items i ON ms.itemId = i.itemId
        ORDER BY ms.itemId, s.snapshotTimestamp
    """).fetchall()

    conn.close()

    # Group rows by itemId
    items: Dict[int, List] = {}
    for r in rows:
        sid, ts, iid, mv, hist, asp, sr, spd, name = r
        items.setdefault(iid, []).append(r)

    # Build PriceHistory objects
    histories: Dict[int, PriceHistory] = {}
    for iid, item_rows in items.items():
        name = item_rows[0][8]
        item = ItemIdentity(item_id=iid, name=name)
        records = [
            SnapshotRecord(
                snapshot_id=r[0],
                item_id=r[2],
                market_value=float(r[3] or 0),
                historical=float(r[4] or 0),
                avg_sale_price=float(r[5] or 0),
                sale_rate=float(r[6] or 0),
                sold_per_day=float(r[7] or 0),
                snapshot_timestamp=r[1] or "",
            )
            for r in item_rows
        ]
        histories[iid] = PriceHistory(item=item, records=records)

    logger.info(
        "Loaded %d items, %d total records from %d snapshot(s)",
        len(histories), sum(len(h.records) for h in histories.values()), snap_count,
    )
    return histories, snap_count, warning


class DailyPipeline:
    """End-to-end market analysis pipeline using ONLY real data.

    Flow:
        SQLite → load_all_histories → group by item → build PriceHistory
        → MarketAnalyzer → SignalDetector → PriceForecaster (if enough data)
        → RecommendationEngine
    """

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()
        self.analyzer = MarketAnalyzer(self.config)
        self.signal_detector = SignalDetector(self.config)
        self.forecaster = PriceForecaster(self.config)
        from woweconomy.recommendations import RecommendationEngine
        self.rec_engine = RecommendationEngine(self.config)

    def run(self, db_path: str,
            max_items: Optional[int] = None,
            top_n: int = 50) -> Tuple[List[Recommendation], str]:
        """Run the full pipeline.

        Parameters
        ----------
        db_path : str
            Path to the SQLite database.
        max_items : int or None
            Limit to this many items (for testing).
        top_n : int
            Return top N recommendations.

        Returns
        -------
        recommendations : list of Recommendation
            Sorted by FarmScore descending.
        warning : str
            Warning message if insufficient snapshots.
        """
        histories, snap_count, warning = load_all_histories(db_path)

        if max_items is not None:
            # Take a subset (first N item_ids)
            ids = list(histories.keys())[:max_items]
            histories = {iid: histories[iid] for iid in ids}

        histories_list = list(histories.values())

        # ----- Analyze -----
        states = [self.analyzer.analyze(h) for h in histories_list]

        # ----- Signals -----
        signals_list = [
            self.signal_detector.detect_all(h, s)
            for h, s in zip(histories_list, states)
        ]

        # ----- Forecast (only if enough data) -----
        forecasts: List[Optional[Forecast]] = []
        for h in histories_list:
            if len(h.records) >= self.config.min_history_for_forecast:
                forecasts.append(self.forecaster.forecast(h))
            else:
                forecasts.append(None)

        # ----- Score -----
        record_counts = [len(h.records) for h in histories_list]
        recs = self.rec_engine.evaluate_batch(
            states, record_counts=record_counts,
            signals_list=signals_list, forecasts=forecasts,
        )

        return recs[:top_n], warning
