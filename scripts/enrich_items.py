"""Enrich farmed drops with Blizzard quality/binding (BoP + junk filter).

Reads distinct item_ids from zone JSONs, fetches the missing ones from
/data/wow/item/{id}, stores them in tsm_markets.db :: blizzard_items.
Resumable: cached ids are skipped (re-fetch after 90 days).

Needs `.env` at repo root with BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET.

Run (background-friendly, ~5 items/s):
    python scripts/enrich_items.py
    python scripts/enrich_items.py woweconomy/wowhead_zones/zone_16.json --db tsm_markets.db
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from woweconomy.services.blizzard_items import (
    BlizzardItemsClient, mark_missing, missing_ids, open_db, upsert,
)

logger = logging.getLogger(__name__)


def distinct_ids(paths: list[Path]) -> list[int]:
    import json
    ids: list[int] = []
    seen: set[int] = set()
    for p in paths:
        for e in json.loads(p.read_text(encoding="utf-8")):
            for it in e.get("items", []):
                i = it.get("item_id")
                if i and i not in seen:
                    seen.add(i)
                    ids.append(int(i))
    return ids


def main() -> None:
    from woweconomy.dbcompat import load_env

    load_env()
    ap = argparse.ArgumentParser(description="Enrich drop items via Blizzard API.")
    ap.add_argument("zones", nargs="*", default=[],
                    help="zone JSONs (default: all zone_*.json)")
    ap.add_argument("--db", default="tsm_markets.db")
    ap.add_argument("--limit", type=int, default=0, help="max items (0 = all)")
    ap.add_argument("--rps", type=float, default=8.0,
                    help="requests/sec (burst cap 100, hourly cap 36000)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    paths = [Path(z) for z in args.zones] or \
        [Path(p) for p in sorted(glob.glob("woweconomy/wowhead_zones/zone_*.json"))]
    ids = distinct_ids(paths)
    print(f"{len(ids)} distinct items in {len(paths)} zone file(s)")

    conn = open_db(args.db)
    todo = missing_ids(conn, ids)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} to fetch")

    client = BlizzardItemsClient(requests_per_second=args.rps)
    done = 0
    for iid in todo:
        try:
            upsert(conn, client.fetch_item(iid))
            done += 1
        except Exception as exc:
            logger.warning("item %s failed: %r", iid, exc)
            if "404" in str(exc):
                mark_missing(conn, iid)  # ghost id: never retry
        if done % 25 == 0:
            conn.commit()
            print(f"  {done}/{len(todo)}")
    conn.commit()
    conn.close()
    print(f"Done: {done} fetched, {len(todo) - done} failed")


if __name__ == "__main__":
    main()
