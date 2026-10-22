"""Show every drop of a given mob (NPC).

Usage:
    python scripts/find_mob.py "Ashenvale Bear"
    python scripts/find_mob.py 3809
    python scripts/find_mob.py "fangferror" --db tsm_markets.db --limit 30

Searches every woweconomy/wowhead_zones/zone_*.json by NPC id or
case-insensitive name substring.  Prices (u=) from csv/items.csv unless
--db is given.  Drops with no avgSalePrice are listed as NO PRICE.
BoP / grey-trash rows are tagged from Blizzard data when available.
Sorted by sale-weighted EV desc.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, ".")

from woweconomy.analytics.farm_ranking import (
    _g, _p, load_csv_market, load_latest_market,
    realization_weight,
)
from woweconomy.services.db2_items import load_facts as load_item_facts
from woweconomy.config import WowEconomyConfig

logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="Show one mob's drops.")
    ap.add_argument("query", help="npc id or name substring")
    ap.add_argument("--zones", nargs="*", default=[],
                    help="zone JSONs (default: all zone_*.json)")
    ap.add_argument("--market-csv", default="csv/items.csv")
    ap.add_argument("--db", default="")
    ap.add_argument("--blizz-db", default="tsm_markets.db")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    zone_paths = [Path(z) for z in args.zones] or \
        [Path(p) for p in sorted(glob.glob("woweconomy/wowhead_zones/zone_*.json"))]
    if not zone_paths:
        print("No zone files. Scrape first:")
        print('  python -m woweconomy.services.wowhead "zone=331/ashenvale"')
        raise SystemExit(2)
    market = load_latest_market(args.db) if args.db else load_csv_market(args.market_csv)
    blizz = load_item_facts(args.blizz_db)
    cfg = WowEconomyConfig.default()

    q = args.query.strip().lower()
    rows = []
    for zp in zone_paths:
        for e in json.loads(zp.read_text(encoding="utf-8")):
            nid = int(e.get("npc_id", 0) or 0)
            name = e.get("npc_name", "")
            if str(nid) != q and q not in name.lower():
                continue
            for it in e.get("items", []):
                iid = it.get("item_id")
                rate = float(it.get("drop_rate", 0) or 0)
                qty = float(it.get("qty", 1) or 1)
                m = market.get(int(iid) if iid else -1)
                unit = m.price if m and m.price > 0 else 0.0
                net = unit * (1.0 - cfg.ah_cut_pct)
                ev = rate * qty * net * (realization_weight(m) if m else 0.0)
                b = blizz.get(int(iid) if iid else -1)
                tag = ""
                if b is not None and (b.is_soulbound or b.is_junk_quality):
                    tag = " [BoP]" if b.is_soulbound else " [JUNK]"
                rows.append((ev, {
                    "npc": name or f"npc={nid}", "zone": e.get("name", ""),
                    "item": it.get("item_name", ""), "iid": iid,
                    "rate": rate, "qty": qty,
                    "cat": it.get("category", ""),
                    "unit": unit, "ev": ev,
                    "spd": m.sold_per_day if m else 0.0,
                    "sr": m.sale_rate if m else 0.0,
                    "tag": tag,
                }))
    rows.sort(key=lambda r: r[0], reverse=True)

    print(f"{len(rows)} drop(s) for {args.query!r}")
    for _, r in rows[:args.limit]:
        price = f"u={_g(r['unit']):>9s} EV={_g(r['ev'])}" if r["unit"] > 0 else "NO PRICE"
        print(f"  {_p(r['rate']):>7s} x{r['qty']:4.1f} [{r['cat'][:12]:12s}] "
              f"{r['item'][:36]:36s} {price} spd={r['spd']:.1f} "
              f"sr={r['sr']:.1%}{r['tag']} <= {r['npc']} ({r['zone']})")


if __name__ == "__main__":
    main()
