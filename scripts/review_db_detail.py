"""Detailed non-destructive inspection of tsm_markets.db and intel.sqlite."""
import sqlite3
from pathlib import Path


def connect_ro(path):
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10.0)


def inspect_tsm():
    db = r"C:\FarmAnalysis\tsm_markets.db"
    con = connect_ro(db)
    tables = [
        "auction_events", "auction_intel_events", "blizzard_items",
        "blizzard_missing", "db2_item_appearance", "db2_items", "db2_meta",
        "item_analytics", "items", "journal", "journal_stats",
        "market_snapshots", "snapshots", "tsm_commodity_price",
        "tsm_market", "tsm_meta", "tsm_price_change", "tsm_realm_group",
        "tsm_realm_pet_price", "tsm_realm_price", "tsm_region",
        "tsm_region_pet_price", "tsm_region_price", "tsm_snapshot",
    ]
    print("=== tsm_markets.db table counts ===")
    for t in tables:
        try:
            c = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {c}")
        except Exception as e:
            print(f"  {t}: ERROR {e}")

    print("\n=== snapshots (latest 5) ===")
    for row in con.execute(
        "SELECT snapshotId, snapshotTimestamp, importFileName, rowCount, checksum "
        "FROM snapshots ORDER BY snapshotId DESC LIMIT 5"
    ):
        chk = row[4][:16] if row[4] else None
        print(f"  id={row[0]}, ts={row[1]}, file={row[2]}, rows={row[3]}, chk={chk}")

    print("\n=== snapshots (earliest 3) ===")
    for row in con.execute(
        "SELECT snapshotId, snapshotTimestamp, importFileName, rowCount "
        "FROM snapshots ORDER BY snapshotId ASC LIMIT 3"
    ):
        print(f"  id={row[0]}, ts={row[1]}, file={row[2]}, rows={row[3]}")

    print("\n=== market_snapshots summary ===")
    total = con.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    distinct = con.execute("SELECT COUNT(DISTINCT itemId) FROM market_snapshots").fetchone()[0]
    print(f"  rows={total}, distinct_item_ids={distinct}")

    print("\n=== tsm_market sample ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(tsm_market)").fetchall()]
    print(f"  columns: {cols}")
    for row in con.execute("SELECT * FROM tsm_market LIMIT 1"):
        print(f"  sample: {dict(zip(cols, row))}")
    c = con.execute("SELECT COUNT(*) FROM tsm_market").fetchone()[0]
    print(f"  rows: {c}")

    print("\n=== tsm_region_price sample ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(tsm_region_price)").fetchall()]
    print(f"  columns: {cols}")
    for row in con.execute("SELECT * FROM tsm_region_price LIMIT 1"):
        print(f"  sample: {dict(zip(cols, row))}")
    c = con.execute("SELECT COUNT(*) FROM tsm_region_price").fetchone()[0]
    print(f"  rows: {c}")

    print("\n=== tsm_snapshot sample ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(tsm_snapshot)").fetchall()]
    print(f"  columns: {cols}")
    for row in con.execute("SELECT * FROM tsm_snapshot LIMIT 1"):
        print(f"  sample: {dict(zip(cols, row))}")
    c = con.execute("SELECT COUNT(*) FROM tsm_snapshot").fetchone()[0]
    print(f"  rows: {c}")

    print("\n=== tsm_meta ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(tsm_meta)").fetchall()]
    print(f"  columns: {cols}")
    for row in con.execute("SELECT * FROM tsm_meta"):
        print(f"  row: {dict(zip(cols, row))}")

    print("\n=== items sample ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(items)").fetchall()]
    print(f"  columns: {cols}")
    for row in con.execute("SELECT * FROM items LIMIT 3"):
        print(f"  sample: {dict(zip(cols, row))}")
    c = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"  rows: {c}")

    print("\n=== db2_items sample ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(db2_items)").fetchall()]
    print(f"  columns: {cols}")
    for row in con.execute("SELECT * FROM db2_items LIMIT 3"):
        print(f"  sample: {dict(zip(cols, row))}")
    c = con.execute("SELECT COUNT(*) FROM db2_items").fetchone()[0]
    print(f"  rows: {c}")

    print("\n=== item_analytics sample ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(item_analytics)").fetchall()]
    print(f"  columns: {cols}")
    for row in con.execute("SELECT * FROM item_analytics LIMIT 1"):
        print(f"  sample: {dict(zip(cols, row))}")
    c = con.execute("SELECT COUNT(*) FROM item_analytics").fetchone()[0]
    print(f"  rows: {c}")

    print("\n=== journal_stats ===")
    cols = [d[0] for d in con.execute("PRAGMA table_info(journal_stats)").fetchall()]
    for row in con.execute("SELECT * FROM journal_stats"):
        print(f"  row: {dict(zip(cols, row))}")

    print("\n=== Integrity check (READ ONLY) ===")
    print(f"  (Skipped in this pass - will run separately)")

    print("\n=== WAL checkpoint status ===")
    try:
        for row in con.execute("PRAGMA wal_checkpoint"):
            print(f"  wal_checkpoint: {row}")
    except Exception as e:
        print(f"  wal_checkpoint ERROR (ro connection): {e}")
    con.close()


def inspect_intel():
    db = r"C:\FarmAnalysis\data\intel.sqlite"
    con = connect_ro(db)
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    print("\n=== data/intel.sqlite table counts ===")
    for t in tables:
        name = t[0]
        try:
            c = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            print(f"  {name}: {c}")
        except Exception as e:
            print(f"  {name}: ERROR {e}")
    print("\n=== intel.sqlite Integrity check ===")
    res = con.execute("PRAGMA integrity_check").fetchone()
    print(f"  integrity_check: {res[0]}")
    con.close()


if __name__ == "__main__":
    inspect_tsm()
    inspect_intel()


