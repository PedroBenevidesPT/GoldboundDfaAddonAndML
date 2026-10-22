"""
Idempotent TSM CSV snapshot importer.

Usage:
    python -m db.import_snapshot csv/items.csv

Idempotency: uses SHA256 checksum of CSV content.
If the same content was already imported, the import is skipped.
"""
import csv
import hashlib
import math
import sys
import os
from datetime import datetime, timezone
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import init_db, Item, Snapshot, MarketSnapshot, ItemAnalytic
from sqlalchemy.orm import Session
from sqlalchemy import text


def sha256_file(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_timestamp(ts_str: str) -> str:
    """Normalise timestamps to ISO-8601."""
    if not ts_str:
        return datetime.now(timezone.utc).isoformat()
    # TSM timestamps are already ISO-8601
    return ts_str


def compute_farm_score_liquidity(spd: float, sr: float) -> float:
    """Log-based liquidity (core FarmScore component)."""
    if spd <= 0 or sr <= 0:
        return 0.0
    return math.log(1 + spd) * math.log(2 + sr * 100)


def compute_price_trend(mv: float, hist: float) -> float:
    if hist <= 0:
        return 1.0
    return max(0.5, min(5.0, mv / hist))


def read_csv(filepath: str) -> Tuple[List[dict], str, str]:
    """Read CSV, return (rows, snapshot_timestamp, checksum)."""
    rows = []
    ts = None
    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                "itemId": int(r["itemId"]),
                "name": r["name"],
                "marketValue": float(r["marketValue"] or 0),
                "historical": float(r["historical"] or 0),
                "avgSalePrice": float(r["avgSalePrice"] or 0),
                "saleRate": float(r["saleRate"] or 0),
                "soldPerDay": float(r["soldPerDay"] or 0),
                "updatedAt": parse_timestamp(r.get("updatedAt", "")),
            })
            if ts is None and rows[-1]["updatedAt"]:
                ts = rows[-1]["updatedAt"]

    if not ts:
        ts = datetime.now(timezone.utc).isoformat()

    checksum = sha256_file(filepath)
    return rows, ts, checksum


def import_csv(filepath: str, db_url: str = "sqlite:///tsm_markets.db") -> dict:
    """
    Import a TSM CSV snapshot into the database.

    Returns a dict with import statistics.
    """
    engine, Session = init_db(db_url)
    stats = {"skipped": False, "reason": "", "items_seen": 0, "items_new": 0,
             "rows_imported": 0, "snapshot_id": None}

    # Read CSV
    rows, snapshot_ts, checksum = read_csv(filepath)
    stats["items_seen"] = len(rows)

    if not rows:
        stats["skipped"] = True
        stats["reason"] = "CSV is empty"
        return stats

    session = Session()
    try:
        # ── Idempotency check ──
        existing = session.query(Snapshot).filter(
            Snapshot.checksum == checksum
        ).first()
        if existing:
            stats["skipped"] = True
            stats["reason"] = f"Duplicate — already imported as snapshot #{existing.snapshotId}"
            stats["snapshot_id"] = existing.snapshotId
            session.close()
            return stats

        # ── Create snapshot record ──
        snapshot = Snapshot(
            snapshotTimestamp=snapshot_ts,
            importFileName=os.path.basename(filepath),
            rowCount=len(rows),
            checksum=checksum,
        )
        session.add(snapshot)
        session.flush()  # get snapshotId

        # ── Upsert new items ──
        existing_item_ids = {
            r[0] for r in session.query(Item.itemId).all()
        }
        new_items = []
        seen_ids = set()
        for r in rows:
            iid = r["itemId"]
            if iid not in existing_item_ids and iid not in seen_ids:
                new_items.append(Item(itemId=iid, name=r["name"]))
                seen_ids.add(iid)

        if new_items:
            session.bulk_save_objects(new_items)
            stats["items_new"] = len(new_items)

        # ── Bulk insert market data ──
        market_rows = []
        analytic_rows = []
        for r in rows:
            spd = r["soldPerDay"]
            sr = r["saleRate"]
            mv = r["marketValue"]
            asp = r["avgSalePrice"]
            hist = r["historical"]

            market_rows.append({
                "snapshotId": snapshot.snapshotId,
                "itemId": r["itemId"],
                "marketValue": mv,
                "historical": hist,
                "avgSalePrice": asp,
                "saleRate": sr,
                "soldPerDay": spd,
            })

            liq = compute_farm_score_liquidity(spd, sr)
            throughput = spd * asp
            trend = compute_price_trend(mv, hist)

            analytic_rows.append({
                "snapshotId": snapshot.snapshotId,
                "itemId": r["itemId"],
                "farmScore": liq,
                "goldThroughput": throughput,
                "priceTrend": trend,
                "liquidity": liq,
            })

        session.bulk_insert_mappings(MarketSnapshot, market_rows)
        session.bulk_insert_mappings(ItemAnalytic, analytic_rows)

        session.commit()
        stats["rows_imported"] = len(market_rows)
        stats["snapshot_id"] = snapshot.snapshotId

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return stats


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m db.import_snapshot <csv_path> [db_url]")
        sys.exit(1)

    filepath = sys.argv[1]
    db_url = sys.argv[2] if len(sys.argv) > 2 else "sqlite:///tsm_markets.db"

    if not os.path.exists(filepath):
        print(f"Error: file not found — {filepath}")
        sys.exit(1)

    stats = import_csv(filepath, db_url)

    print("=" * 50)
    print("  IMPORT SUMMARY")
    print("=" * 50)
    if stats["skipped"]:
        print(f"  SKIPPED: {stats['reason']}")
    else:
        print(f"  Snapshot ID:   {stats['snapshot_id']}")
        print(f"  Items seen:    {stats['items_seen']}")
        print(f"  New items:     {stats['items_new']}")
        print(f"  Rows imported: {stats['rows_imported']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
