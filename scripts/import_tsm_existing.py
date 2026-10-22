"""Seed the TSM snapshot tables from data already on disk -- no network.

The public CSV endpoint rate-limits bursts (it returns a 429 with a long
``Retry-After``), so this is the fallback that makes the analysis usable now:

* ``csv/items.csv``        -> region_items
* ``csv/commodities.csv``  -> commodities
* ``csv/realm_items.csv``  -> realm_items for the default realm
* ``tsm_market`` (AppData) -> realm_items for the 12 realms TSM scanned

Every import goes through the same snapshot store as the live fetch, so running
it twice is free and a later CSV download only writes the rows that changed.

    python scripts/import_tsm_existing.py
    python scripts/import_tsm_existing.py --no-appdata
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.config import WowEconomyConfig  # noqa: E402
from woweconomy.services import tsm_snapshots as ts  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "tsm_markets.db"))
    ap.add_argument("--csv-dir", default=str(ROOT / "csv"))
    ap.add_argument("--out", default=str(ROOT / "data" / "tsm" / "snapshots"))
    ap.add_argument("--no-appdata", action="store_true")
    args = ap.parse_args()

    cfg = WowEconomyConfig.default()
    csv_dir = Path(args.csv_dir)
    con = ts.connect(args.db)
    print(f"base: {args.db}")

    jobs = [
        ("items.csv", "region", "region_items"),
        ("commodities.csv", "region", "commodities"),
        ("realm_items.csv", cfg.realm_slug, "realm_items"),
    ]
    for filename, scope, kind in jobs:
        path = csv_dir / filename
        if not path.exists():
            print(f"  - {filename:18s} ausente")
            continue
        result = ts.import_csv_file(con, path, scope, kind, args.out)
        status = "inalterado" if result.skipped else f"{result.rows:,} linhas"
        print(f"  + {filename:18s} -> {scope}/{kind:12s} {status} "
              f"({result.updated_at or '—'})")

    if not args.no_appdata:
        print("  + AppData tsm_market -> realm_items")
        for result in ts.import_appdata(con):
            print(f"      {result.task.scope:24s} {result.rows:>7,} linhas | "
                  f"{result.changed:>7,} mudanças | {result.updated_at}")

    con.close()
    print(f"snapshots -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
