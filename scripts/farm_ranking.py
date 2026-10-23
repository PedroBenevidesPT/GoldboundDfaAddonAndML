"""The farm ranking, as one command.

There was one implementation of the ranking -- `woweconomy.analytics.
farm_ranking` -- and a second entry point at the repo root that imported
`woweconomy.analytics.analytics`, a module that has never existed:

    ModuleNotFoundError: No module named 'woweconomy.analytics.analytics'

It caught that into a text file, so running it produced no error and no
ranking, but left `fazenda_report.txt` behind containing

    1. Test Item
    2. Test Item 2
    3. Test Item 3

which is what a duplicate reaches: it looks like a second tool, it never ran,
and its output looked like a result.

This script ranks nothing and parses nothing of its own for the zone report.
It dispatches:

* with zone files (or no subcommand), it hands the whole command line to
  :func:`woweconomy.analytics.farm_ranking.main`, which owns every flag --
  `--price-source`, `--blizz-db`, `--no-era-filter`, `--sort`, `--stops`,
  `--kpm`, `--session`, `--travel-sec`, `--mog-kills`, `--price-base`,
  `--price-rate`, `--price-floor`, `--out`.  One parser, one meaning.
* with `mats`, `expensive`, `mogs` or `jackpots`, it prints one of the
  narrower leaderboards the same module already computes.
* with `realms`, it prints the per-realm view, which answers the same
  question for each auction house separately instead of once for the region.

Every mode prices from the same source: the trained model, with the market
table only as the per-item fallback.  The leaderboards used to read the TSM
asking prices directly, so the same item had an asking price in one screen and
a realizable price in the other.

    python scripts/farm_ranking.py zone_331.json
    python scripts/farm_ranking.py zone_331.json zone_719.json --top 20
    python scripts/farm_ranking.py zone_331.json --out FarmReport.txt
    python scripts/farm_ranking.py mats --top 20
    python scripts/farm_ranking.py mogs --top 10
    python scripts/farm_ranking.py realms --all-realms --top 10
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

#: The narrower leaderboards.  Anything else is a zone file, handled by the
#: module's own parser.
LEADERBOARDS = ("mats", "expensive", "mogs", "jackpots")


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


def leaderboard(command: str, argv) -> int:
    parser = argparse.ArgumentParser(
        prog=f"farm_ranking {command}",
        description=f"Print the {command} leaderboard.")
    parser.add_argument("--zone", action="append", default=[], type=int,
                        help="zone id (repeatable); default is every zone on disk")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--db", default=str(DEFAULT_TSM),
                        help="the market database, used as the model's fallback")
    parser.add_argument("--price-source", default="auto",
                        choices=("auto", "model", "region", "legacy", "csv"),
                        help="where the prices come from; auto prefers the "
                             "trained model, exactly as the zone report does")
    parser.add_argument("--market-csv", default="csv/items.csv")
    parser.add_argument("--json", default="", metavar="PATH",
                        help="also write the result as JSON")
    args = parser.parse_args(argv)

    paths = zone_paths(args.zone)
    if not paths:
        print(f"no zone files under {ZONES_DIR}", file=sys.stderr)
        return 2
    drops = fr.load_zone_drops(paths)

    # The leaderboards used to read the TSM asking prices directly, which made
    # them disagree with the zone report about the same item: one said what a
    # listing asks, the other what it fetches.  Both now go through the single
    # price-source decision.
    try:
        source = fr.resolve_market(
            {d.item_id for d in drops},
            names={d.item_id: d.item_name for d in drops if d.item_name},
            source=args.price_source,
            db_path=args.db if Path(args.db).exists() else "",
            csv_path=args.market_csv)
    except SystemExit as exc:
        print(str(exc) or "no usable price source", file=sys.stderr)
        return 2
    market = source.market
    if not market:
        # Not a warning.  Every EV below would be zero and the ranking would
        # look like "nothing is worth farming".
        print(f"no prices for these zones ({source.label}); refusing to rank "
              f"against nothing", file=sys.stderr)
        return 2
    print(f"Market: {len(market)} priced items ({source.label})", file=sys.stderr)

    if command == "mats":
        rows = fr.top_mats(drops=drops, market=market, top_n=args.top)
        keys = ("item_id", "item_name", "npc_name", "ev", "units_per_kill",
                "drop_rate")
    elif command == "expensive":
        rows = fr.top_expensive(drops=drops, market=market, top_n=args.top)
        keys = ("item_id", "item_name", "price", "sale_rate")
    elif command == "mogs":
        rows = fr.top_mogs(drops=drops, market=market, top_n=args.top)
        keys = ("item_id", "item_name", "price", "drop_rate", "sold_per_day")
    else:
        rows = fr.top_jackpots(drops=drops, market=market, top_n=args.top)
        keys = ("item_id", "item_name", "price", "drop_rate")

    for index, row in enumerate(rows, 1):
        print(f"{index:>3}. {row}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(_rows(rows, keys), indent=2, default=str),
            encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "realms":
        # The per-realm view: the same question asked of each auction house
        # instead of once for the region.  It lives in its own module because
        # it batches the estimate over thousands of items, but it is reached
        # through this command so there is one entry point, not two.
        from scripts import rank_farms

        return rank_farms.main(argv[1:])
    if argv and argv[0] in LEADERBOARDS:
        return leaderboard(argv[0], argv[1:])
    # Everything else is a zone report, and the module owns those flags.
    return fr.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
