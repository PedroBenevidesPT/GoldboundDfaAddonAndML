"""Where to farm each item: best (zone, NPC) across ALL scraped zones.

For every valuable drop, compares every NPC in every zone file and shows
the best spot plus runner-ups — so you know when an Ashenvale drop is
more easily farmed elsewhere (or vice versa).

Usage:
    python scripts/relocate.py --top 20
    python scripts/relocate.py --top 30 --min-ev 50
    python scripts/relocate.py --db tsm_markets.db

Only compares zones already scraped (woweconomy/wowhead_zones/zone_*.json).
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

sys.path.insert(0, ".")

from woweconomy.analytics.farm_ranking import (
    _g, _p, blizz_excluded, load_csv_market,
    load_latest_market, load_zone_drops, realization_weight,
)
from woweconomy.analytics.farm_ranking import FarmRankingConfig
from woweconomy.services.db2_items import load_facts as load_item_facts
from woweconomy.config import WowEconomyConfig

logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="Best farm spot per item.")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-ev", type=float, default=0.0,
                    help="min best-spot EV in gold to list an item")
    ap.add_argument("--zones", nargs="*", default=[],
                    help="zone JSONs (default: all zone_*.json)")
    ap.add_argument("--market-csv", default="csv/items.csv")
    ap.add_argument("--db", default="")
    ap.add_argument("--blizz-db", default="tsm_markets.db")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    zone_paths = [Path(z) for z in args.zones] or \
        [Path(p) for p in sorted(glob.glob("woweconomy/wowhead_zones/zone_*.json"))]
    market = load_latest_market(args.db) if args.db else load_csv_market(args.market_csv)
    blizz = load_item_facts(args.blizz_db)
    cfg, r_cfg = WowEconomyConfig.default(), FarmRankingConfig()

    # per (item) -> list of (ev, npc, zone, rate, qty, u)
    per_item: dict[int, list] = {}
    names: dict[int, str] = {}
    for d in load_zone_drops(zone_paths):
        if d.drop_rate <= 0 or blizz_excluded(d, blizz, r_cfg):
            continue
        m = market.get(d.item_id)
        if m is None or m.price <= 0:
            continue
        net = m.price * (1.0 - cfg.ah_cut_pct)
        ev = d.drop_rate * d.qty * net * realization_weight(m)
        names[d.item_id] = d.item_name or m.name
        per_item.setdefault(d.item_id, []).append(
            (ev, d.npc_name or f"npc={d.npc_id}", d.zone_name,
             d.drop_rate, d.qty, m.price))

    ranked = sorted(per_item.items(),
                    key=lambda kv: max(r[0] for r in kv[1]), reverse=True)
    print(f"\n=== BEST SPOT PER ITEM ({len(ranked)} items, "
          f"{len(zone_paths)} zones) ===")
    n = 0
    for iid, spots in ranked:
        spots.sort(key=lambda r: r[0], reverse=True)
        best = spots[0]
        if best[0] / 10_000 < args.min_ev:
            continue
        n += 1
        if n > args.top:
            break
        print(f"{n:2d}. {names[iid][:38]:38s} u={_g(best[5]):>9s} "
              f"BEST: {best[1][:28]:28s} ({best[2]}) "
              f"{_p(best[3]):>7s} EV={_g(best[0])}")
        for ev, npc, zone, rate, qty, u in spots[1:4]:
            if ev <= 0:
                continue
            print(f"     alt: {npc[:28]:28s} ({zone}) "
                  f"{_p(rate):>7s} EV={_g(ev)}")
    if n == 0:
        print("(nothing above --min-ev)")


if __name__ == "__main__":
    main()
