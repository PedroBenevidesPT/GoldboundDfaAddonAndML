"""Fetch today's TSM snapshot and import into SQLite, then compare."""
import os
import sys
import tempfile
from datetime import datetime, timezone

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from woweconomy import WowEconomyConfig
from woweconomy.services.fetcher import TSMFetcher
from db.import_snapshot import import_csv


def main():
    cfg = WowEconomyConfig.default()
    fetcher = TSMFetcher(cfg)

    print("=" * 60)
    print(f"  TSM Snapshot Fetcher — {datetime.now(timezone.utc).isoformat()}")
    print(f"  Region: {cfg.region_slug}  Realm: {cfg.realm_slug}")
    print("=" * 60)

    # ── 1. Fetch region items.csv ──
    print("\nDownloading region/items.csv from TSM...")
    checksum, rows = fetcher.fetch_region_items_csv()
    print(f"  Received {len(rows)} rows, SHA256: {checksum[:16]}...")

    # ── 2. Save to temp file for the importer ──
    fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="tsm_region_items_")
    import csv as csv_writer_mod
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        if rows:
            writer = csv_writer_mod.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    print(f"  Saved to {tmp_path}")

    # ── 3. Check if checksum matches the last imported snapshot ──
    import sqlite3
    db_path = cfg.db_url.replace("sqlite:///", "")
    if db_path == cfg.db_url:
        db_path = "tsm_markets.db"

    conn = sqlite3.connect(db_path)
    last_checksum = conn.execute(
        "SELECT checksum FROM snapshots ORDER BY snapshotId DESC LIMIT 1"
    ).fetchone()
    old_snapshot_id = conn.execute(
        "SELECT MAX(snapshotId) FROM snapshots"
    ).fetchone()[0]
    conn.close()

    if last_checksum and last_checksum[0] == checksum:
        print("\n  Checksum identical to last imported snapshot — no changes detected.")
        print("  (TSM data hasn't updated since your last import)")
        os.unlink(tmp_path)
        return

    # ── 4. Import ──
    print("\nImporting into database...")
    stats = import_csv(tmp_path, f"sqlite:///{db_path}")

    print()
    print("=" * 60)
    print("  IMPORT SUMMARY")
    print("=" * 60)
    if stats["skipped"]:
        print(f"  SKIPPED: {stats['reason']}")
    else:
        print(f"  New Snapshot ID: #{stats['snapshot_id']}")
        print(f"  Items imported:  {stats['rows_imported']}")
        print(f"  New items:       {stats['items_new']}")
        print(f"  Timestamp:       {rows[0].get('updatedAt', 'N/A') if rows else 'N/A'}")

    # ── 5. Compare old vs new ──
    if not stats["skipped"] and stats["snapshot_id"]:
        new_sid = stats["snapshot_id"]
        compare_snapshots(db_path, old_snapshot_id, new_sid)

    # Cleanup
    os.unlink(tmp_path)
    print("\nDone.")


def compare_snapshots(db_path: str, old_sid: int, new_sid: int):
    """Compare two snapshots and report changes of interest."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    old_rows = conn.execute(
        "SELECT itemId, marketValue, historical, avgSalePrice, saleRate, soldPerDay "
        "FROM market_snapshots WHERE snapshotId = ?", (old_sid,)
    ).fetchall()
    new_rows = conn.execute(
        "SELECT itemId, marketValue, historical, avgSalePrice, saleRate, soldPerDay "
        "FROM market_snapshots WHERE snapshotId = ?", (new_sid,)
    ).fetchall()

    old = {r[0]: r[1:] for r in old_rows}
    new = {r[0]: r[1:] for r in new_rows}

    old_items = set(old.keys())
    new_items = set(new.keys())

    added = new_items - old_items
    removed = old_items - new_items
    common = old_items & new_items

    print()
    print("=" * 60)
    print("  SNAPSHOT COMPARISON")
    print("=" * 60)
    print(f"  Old #{old_sid}: {len(old_items)} items")
    print(f"  New #{new_sid}: {len(new_items)} items")
    print(f"  Added:   {len(added)} items")
    print(f"  Removed: {len(removed)} items")
    print(f"  Common:  {len(common)} items")

    # Detect changes in top farm items
    if common:
        # Compute change in soldPerDay and marketValue for common items
        changes = []
        for iid in common:
            o = old[iid]
            n = new[iid]
            spd_change = n[4] - o[4]   # index 4 = soldPerDay
            mv_change = n[0] - o[0]     # index 0 = marketValue
            sr_change = n[3] - o[3]     # index 3 = saleRate
            changes.append((iid, spd_change, mv_change, sr_change))

        # Top 10 biggest demand increases
        changes.sort(key=lambda x: -x[1])
        print(f"\n  Top 10 SPD increases:")
        for iid, spd_chg, mv_chg, sr_chg in changes[:10]:
            name = conn.execute(
                "SELECT name FROM items WHERE itemId=?", (iid,)
            ).fetchone()
            n = name[0] if name else f"Item-{iid}"
            print(f"    +{spd_chg:>8.1f} SPD  {mv_chg:>+10.0f}g  "
                  f"{sr_chg:>+.3f} SR  {n}")

        # Top 10 biggest price jumps
        changes.sort(key=lambda x: -x[2])
        print(f"\n  Top 10 price increases:")
        for iid, spd_chg, mv_chg, sr_chg in changes[:10]:
            name = conn.execute(
                "SELECT name FROM items WHERE itemId=?", (iid,)
            ).fetchone()
            n = name[0] if name else f"Item-{iid}"
            print(f"    {mv_chg:>+12,.0f}g  ({spd_chg:>+.1f} SPD)  {n}")

    conn.close()


if __name__ == "__main__":
    main()
