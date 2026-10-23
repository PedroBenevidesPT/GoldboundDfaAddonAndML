"""Backfill the price status and the product key on existing snapshots.

Nothing here changes a raw observation.  It fills two derived columns that
were introduced after the snapshots were captured:

* ``variant_key`` -- the product identity (see
  :mod:`woweconomy.intel.identity`);
* ``price_status`` / ``price_reason`` -- whether the stored price may be
  used as a price (see :mod:`woweconomy.intel.prices`).

The script is deterministic and idempotent: it processes snapshots in
``captured_at`` order, only touches rows whose derived value is still
unset, and a second run reports zero changes.

    python scripts/backfill_ah_identity.py --dry-run
    python scripts/backfill_ah_identity.py
    python scripts/backfill_ah_identity.py --verify

It never opens ``tsm_markets.db`` and never writes under ``models/``.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.intel import identity, prices, store  # noqa: E402

BATCH = 20_000


def snapshots(con: sqlite3.Connection):
    return con.execute(
        "SELECT snapshot_id, realm, captured_at FROM ah_snapshot"
        " ORDER BY captured_at, snapshot_id").fetchall()


def fill_variants(con: sqlite3.Connection, snapshot_id: str,
                  dry_run: bool) -> int:
    rows = con.execute(
        "SELECT auction_key, item_id, item_context, bonus_lists, modifiers,"
        " pet_species_id FROM ah_auction"
        " WHERE snapshot_id = ? AND variant_key IS NULL", (snapshot_id,)).fetchall()
    if not rows or dry_run:
        return len(rows)
    con.executemany(
        "UPDATE ah_auction SET variant_key = ?"
        " WHERE snapshot_id = ? AND auction_key = ? AND variant_key IS NULL",
        [(identity.variant_key(r["item_id"], r["item_context"], r["bonus_lists"],
                               r["modifiers"], r["pet_species_id"]),
          snapshot_id, r["auction_key"]) for r in rows])
    con.commit()
    return len(rows)


def fill_price_status(con: sqlite3.Connection, snapshot_id: str,
                      dry_run: bool) -> int:
    """Label every row that still carries the column default.

    A legacy row has ``price_status = 'missing'`` even when it has a
    price, so the value -- not the status -- decides.
    """
    rows = con.execute(
        "SELECT auction_key, unit_price_copper FROM ah_auction"
        " WHERE snapshot_id = ? AND price_status = ? AND unit_price_copper IS NOT NULL",
        (snapshot_id, prices.MISSING)).fetchall()
    if not rows or dry_run:
        return len(rows)
    updates = []
    for r in rows:
        verdict = prices.classify_static(r["unit_price_copper"])
        updates.append((verdict.status, verdict.reason, snapshot_id,
                        r["auction_key"]))
    con.executemany(
        "UPDATE ah_auction SET price_status = ?, price_reason = ?"
        " WHERE snapshot_id = ? AND auction_key = ?", updates)
    con.commit()
    return len(rows)


def run(db: Path, dry_run: bool) -> dict:
    con = store.connect(db)
    report = {"snapshots": 0, "variants_filled": 0, "prices_labelled": 0,
              "outliers_flagged": 0, "dry_run": dry_run}
    for snap in snapshots(con):
        report["snapshots"] += 1
        report["variants_filled"] += fill_variants(con, snap["snapshot_id"], dry_run)
        report["prices_labelled"] += fill_price_status(con, snap["snapshot_id"],
                                                       dry_run)
        if not dry_run:
            report["outliers_flagged"] += prices.mark_outliers(con, snap["snapshot_id"])
    return report


def verify(db: Path) -> dict:
    con = store.connect(db)
    total = con.execute("SELECT COUNT(*) FROM ah_auction").fetchone()[0]
    unkeyed = con.execute(
        "SELECT COUNT(*) FROM ah_auction WHERE variant_key IS NULL").fetchone()[0]
    status = prices.status_counts(con)
    unpriced = con.execute(
        "SELECT COUNT(*) FROM ah_auction WHERE unit_price_copper IS NULL").fetchone()[0]
    return {
        "rows": total,
        "rows_without_variant_key": unkeyed,
        "rows_without_price": unpriced,
        "price_status": status,
        "usable_prices": status.get(prices.OK, 0),
        "usable_share": round(status.get(prices.OK, 0) / total, 4) if total else 0.0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(store.DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    parser.add_argument("--verify", action="store_true",
                        help="print the current derived-column state and exit")
    args = parser.parse_args(argv)
    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    if args.verify:
        print(json.dumps(verify(db), indent=2))
        return 0
    print(json.dumps(run(db, args.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
