"""The farm ranking, as one command.

There was already one implementation of this -- `woweconomy.analytics.
farm_ranking` -- and one broken copy of it at the repo root that imported a
module which does not exist (`woweconomy.analytics.analytics`), swallowed the
error into a text file, and left that file behind containing the literal
strings "Test Item", "Test Item 2", "Test Item 3".  That is the state a
duplicate reaches: it looks like a second tool, it never ran, and its output
looked like a result.

This is the only entry point.  It does no ranking of its own: it loads the
zone drop tables and the market, calls the module, and prints what comes
back.  Every number here is produced by `farm_ranking`, so the CLI and the
tests cannot disagree about what the ranking means.

    python scripts/farm_ranking.py zone                 # full zone ranking
    python scripts/farm_ranking.py zone --zone 331 --zone 16
    python scripts/farm_ranking.py mats --top 20
    python scripts/farm_ranking.py expensive --top 10
    python scripts/farm_ranking.py mogs --top 10
    python scripts/farm_ranking.py jackpots --top 10
    python scripts/farm_ranking.py zone --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.analytics import farm_ranking as fr            # noqa: E402

DEFAULT_TSM = ROOT / "tsm_markets.db"
ZONES_DIR = ROOT / "woweconomy" / "wowhead_zones"


def zone_paths(zone_ids):
    """The wowhead zone files to read.

    ``--zone`` takes ids because the filenames are `zone_<id>.json`; with no
    ids, every zone on disk is read.  A missing id is an error rather than a
    silent skip: a ranking over "the zones that happened to exist" is not the
    ranking anybody asked for.
    """
    if not zone_ids:
        return sorted(ZONES_DIR.glob("zone_*.json"))
    paths = []
    for zone_id in zone_ids:
        path = ZONES_DIR / f"zone_{int(zone_id)}.json"
        if not path.exists():
            raise SystemExit(f"no zone file for id {zone_id}: {path}")
        paths.append(path)
    return paths


def _rows(items, keys):
    """A list of dicts, keeping only the fields that exist on each row."""
    out = []
    for item in items:
        row = {}
        for key in keys:
            value = getattr(item, key, None)
            if value is not None:
                row[key] = value
        out.append(row)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command",
                        choices=["zone", "mats", "expensive", "mogs", "jackpots"])
    parser.add_argument("--zone", action="append", default=[], type=int,
                        help="zone id (repeatable); default is every zone on disk")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--db", default=str(DEFAULT_TSM),
                        help="the TSM market database")
    parser.add_argument("--kills-per-minute", type=float, default=20.0,
                        help="used by the zone report to convert EV/kill to EV/hour")
    parser.add_argument("--json", default="", metavar="PATH",
                        help="also write the result as JSON")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"market database not found: {db}", file=sys.stderr)
        return 2
    market = fr.load_latest_market(str(db))
    if not market:
        # Not a warning.  Every EV below would be zero and the ranking would
        # look like "nothing is worth farming".
        print(f"no market prices in {db}; refusing to rank against nothing",
              file=sys.stderr)
        return 2

    paths = zone_paths(args.zone)
    if not paths:
        print(f"no zone files under {ZONES_DIR}", file=sys.stderr)
        return 2
    drops = fr.load_zone_drops(paths)

    payload = None
    if args.command == "zone":
        ranking = fr.rank_zone(paths, market)
        fr.print_report(ranking, kills_per_minute=args.kills_per_minute)
        payload = {"zones": [str(p) for p in paths],
                   "farms": _rows(ranking.farms, ("npc_id", "npc_name",
                                                  "zone", "ev_per_kill",
                                                  "ev_per_hour", "drops"))}
    elif args.command == "mats":
        rows = fr.top_mats(drops=drops, market=market, top_n=args.top)
        for index, row in enumerate(rows, 1):
            print(f"{index:>3}. {row}")
        payload = _rows(rows, ("item_id", "item_name", "npc_name", "ev",
                               "units_per_kill", "drop_rate"))
    elif args.command == "expensive":
        rows = fr.top_expensive(drops=drops, market=market, top_n=args.top)
        for index, row in enumerate(rows, 1):
            print(f"{index:>3}. {row}")
        payload = _rows(rows, ("item_id", "item_name", "price", "sale_rate"))
    elif args.command == "mogs":
        rows = fr.top_mogs(drops=drops, market=market, top_n=args.top)
        for index, row in enumerate(rows, 1):
            print(f"{index:>3}. {row}")
        payload = _rows(rows, ("item_id", "item_name", "price", "drop_rate",
                               "sold_per_day"))
    else:
        rows = fr.top_jackpots(drops=drops, market=market, top_n=args.top)
        for index, row in enumerate(rows, 1):
            print(f"{index:>3}. {row}")
        payload = _rows(rows, ("item_id", "item_name", "price", "drop_rate"))

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
