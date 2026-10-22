"""Refresh csv/items.csv from today's TSM region data.

Fetches region/items.csv, joins item names from tsm_markets.db, and
rewrites csv/items.csv (backup first) with columns:
    itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt

    python scripts/fetch_csv.py
"""

from __future__ import annotations

import csv
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

from woweconomy.config import WowEconomyConfig
from woweconomy.services.fetcher import TSMFetcher

COLUMNS = ["itemId", "name", "marketValue", "historical", "avgSalePrice",
           "saleRate", "soldPerDay", "updatedAt"]


def main() -> None:
    cfg = WowEconomyConfig.default()
    print("Downloading region/items.csv from TSM...")
    checksum, rows = TSMFetcher(cfg).fetch_region_items_csv()
    print(f"  {len(rows)} rows, SHA256: {checksum[:16]}...")
    if not rows:
        print("Empty response, aborting (CSV untouched).")
        raise SystemExit(1)

    conn = sqlite3.connect("tsm_markets.db")
    try:
        names = dict(conn.execute("SELECT itemId, name FROM items").fetchall())
    finally:
        conn.close()
    print(f"  {len(names)} names from DB")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path("csv/items.csv")
    bak = Path(f"csv/items.{now[:10]}.bak.csv")
    shutil.copy2(out, bak)
    print(f"  backup -> {bak}")
    n_named = 0
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            try:
                iid = int(r.get("itemId", r.get("itemid", 0)))
            except (TypeError, ValueError):
                continue
            name = names.get(iid, "")
            n_named += bool(name)
            w.writerow({
                "itemId": iid,
                "name": name,
                "marketValue": r.get("marketValue", ""),
                "historical": r.get("historical", ""),
                "avgSalePrice": r.get("avgSalePrice", ""),
                "saleRate": r.get("saleRate", ""),
                "soldPerDay": r.get("soldPerDay", ""),
                "updatedAt": now,
            })
    print(f"Wrote {out} ({len(rows)} rows, {n_named} named) as of {now}")


if __name__ == "__main__":
    main()
