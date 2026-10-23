"""Plan a hyperspawn-proof route through ONE zone.

Groups top NPCs by income profile (JACKPOT vs STEADY vs MIXED) and orders
camps alternating profiles, so you keep moving sitio em sitio instead of
sitting on one spot until loot throttles.

NOTE: order is logical (profile-alternating), NOT geographic — the scrape
does not store mob coordinates (Wowhead has them; future work).  Use the
camp list below and trace it on the map yourself.

Usage:
    python scripts/plan_route.py --zone 331 --stops 5 --minutes 60
    python scripts/plan_route.py --zone 16 --stops 4 --kpm 25 --travel-sec 90
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

sys.path.insert(0, ".")

from woweconomy.analytics.farm_ranking import (
    _g, _p, farm_profile, load_csv_market, load_latest_market,
    load_zone_drops, score_npcs,
)
from woweconomy.services.db2_items import load_facts as load_item_facts
from woweconomy.config import WowEconomyConfig

logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="Plan a zone farm route.")
    ap.add_argument("--zone", type=int, default=331)
    ap.add_argument("--zones", nargs="*", default=[],
                    help="zone JSONs (default: auto-find zone_<id>.json)")
    ap.add_argument("--stops", type=int, default=5)
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--kpm", type=float, default=20.0)
    ap.add_argument("--travel-sec", type=float, default=60.0)
    ap.add_argument("--market-csv", default="csv/items.csv")
    ap.add_argument("--db", default="")
    ap.add_argument("--blizz-db", default="tsm_markets.db")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    if args.zones:
        paths = [Path(z) for z in args.zones]
    else:
        found = sorted(glob.glob(f"woweconomy/wowhead_zones/zone_{args.zone}.json"))
        if not found:
            print(f"No file for zone {args.zone}. Scrape first:")
            print(f'  python -m woweconomy.services.wowhead "zone={args.zone}/..."')
            raise SystemExit(2)
        paths = [Path(found[0])]
    market = load_latest_market(args.db) if args.db else load_csv_market(args.market_csv)
    blizz = load_item_facts(args.blizz_db)
    cfg = WowEconomyConfig.default()

    drops = [d for d in load_zone_drops(paths) if d.zone == args.zone or not args.zones]
    farms = [f for f in score_npcs(drops, market, cfg, blizz) if f.realizable_ev > 0]
    if not farms:
        print("No farms with realizable EV. Nothing to route.")
        raise SystemExit(1)

    # Two ordered pools: regular income vs lottery hits.  Alternate them
    # so no two lottery camps (or two steady grinds) sit back-to-back.
    steady = sorted([f for f in farms if f.realizable_ev > 0],
                    key=lambda f: f.realizable_ev, reverse=True)
    lottery = sorted(
        [f for f in farms
         if 0 < f.jackpot_rate < 0.10 and f.jackpot_value > 0],
        key=lambda f: f.jackpot_value, reverse=True)
    order: list = []
    seen: set[int] = set()
    si = li = 0
    while len(order) < args.stops and (si < len(steady) or li < len(lottery)):
        if len(order) % 2 == 0 and si < len(steady):
            f = steady[si]; si += 1
        elif li < len(lottery):
            f = lottery[li]; li += 1
        elif si < len(steady):
            f = steady[si]; si += 1
        else:
            break
        if f.npc_id in seen:
            continue
        seen.add(f.npc_id)
        order.append((farm_profile(f), f))

    travel_min = len(order) * args.travel_sec / 60.0
    kill_min = max(0.0, args.minutes - travel_min)
    per_stop_min = kill_min / max(len(order), 1)
    kills = per_stop_min * args.kpm

    print(f"\n=== ROUTE zone {args.zone}: {len(order)} camps, "
          f"{args.minutes:.0f}min (~{per_stop_min:.1f}min/camp, "
          f"{args.travel_sec:.0f}s travel between) ===")
    total_vend = total_real = 0.0
    for i, (prof, f) in enumerate(order, 1):
        jp = (f"lottery {f.jackpot_item[:30]} {_p(f.jackpot_rate)} "
              f"E={_g(f.jackpot_value)}" if prof == "JACKPOT" else
              f"steady {_g(f.realizable_ev)}/kill")
        print(f"Camp {i} [{prof:7s}] {f.npc_name or f.npc_id} [{f.npc_id}] "
              f"{f.spawns} spawns {kills:.0f} kills -> vend~{_g(kills * f.vendor_per_kill)} "
              f"real~{_g(kills * f.realizable_ev)} | {jp}")
        total_vend += kills * f.vendor_per_kill
        total_real += kills * f.realizable_ev
    print(f"Route total ~{kills * len(order):.0f} kills: "
          f"vendor={_g(total_vend)} real~={_g(total_real)}")
    print("(order is logical, not geographic: no coordinates scraped yet)")


if __name__ == "__main__":
    main()
