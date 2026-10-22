"""Find which NPCs drop a given item.

Usage:
    python scripts/find_drop.py "Ruffled Chaplet"
    python scripts/find_drop.py 14047
    python scripts/find_drop.py "bear bone" --zones woweconomy/wowhead_zones/zone_331.json
    python scripts/find_drop.py "robe" --db tsm_markets.db

Searches every woweconomy/wowhead_zones/zone_*.json by default, matching
item id or case-insensitive name substring.  Prices (u=) come from
csv/items.csv unless --db is given.  Sorted by EV per kill desc.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

sys.path.insert(0, ".")
from woweconomy.analytics.farm_ranking import (
    _g, _p, load_csv_market, load_latest_market, load_zone_drops,
)
from woweconomy.config import WowEconomyConfig


def main() -> None:
    ap = argparse.ArgumentParser(description="Find NPCs that drop an item.")
    ap.add_argument("query", help="item id or name substring")
    ap.add_argument("--zones", nargs="*", default=[],
                    help="zone JSONs (default: all zone_*.json)")
    ap.add_argument("--market-csv", default="csv/items.csv")
    ap.add_argument("--db", default="")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    zone_paths = [Path(z) for z in args.zones] or \
        [Path(p) for p in sorted(glob.glob("woweconomy/wowhead_zones/zone_*.json"))]
    if not zone_paths:
        print("No zone files. Scrape first:")
        print('  python -m woweconomy.services.wowhead "zone=331/ashenvale"')
        raise SystemExit(2)
    market = load_latest_market(args.db) if args.db else load_csv_market(args.market_csv)
    cfg = WowEconomyConfig.default()

    q = args.query.strip().lower()
    hits = []
    for d in load_zone_drops(zone_paths):
        if d.item_id == (int(q) if q.isdigit() else -1) or q in d.item_name.lower():
            m = market.get(d.item_id)
            unit = m.price if m and m.price > 0 else 0.0
            ev = d.drop_rate * d.qty * unit * (1.0 - cfg.ah_cut_pct)
            hits.append((ev, d, unit,
                         m.sold_per_day if m else 0.0,
                         m.sale_rate if m else 0.0))
    hits.sort(key=lambda h: h[0], reverse=True)

    print(f"{len(hits)} drop(s) matching {args.query!r}")
    for ev, d, unit, spd, sr in hits[:args.limit]:
        npc = d.npc_name or f"npc={d.npc_id}"
        price = f"u={_g(unit):>9s} EV={_g(ev)}" if unit > 0 else "NO PRICE"
        print(f"  {d.item_name[:38]:38s} {_p(d.drop_rate):>7s} x{d.qty:4.1f} "
              f"[{d.category[:12]:12s}] {price} <= {npc} [{d.npc_id}] "
              f"({d.zone_name}) spd={spd} sr={sr}")


if __name__ == "__main__":
    main()
