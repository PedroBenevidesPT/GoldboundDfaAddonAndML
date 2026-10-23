"""Build, validate and inspect the daily aggregates.

The aggregates are the statistical record that outlives the raw rows, so they
are built and validated *before* any deletion, never after.

Usage:
    python scripts/aggregates.py build
    python scripts/aggregates.py build --force
    python scripts/aggregates.py validate
    python scripts/aggregates.py validate --days 2026-10-20 --days 2026-10-21
    python scripts/aggregates.py coverage
    python scripts/aggregates.py price --day 2026-10-20 --scope draenor --item 12345
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.aggregates import (  # noqa: E402
    AGGREGATES,
    DEFAULT_AGGREGATE_DB,
    build,
    day_price,
    price_as_of,
    render_coverage,
    validate,
)

DEFAULT_INTEL = ROOT / "data" / "intel.sqlite"
DEFAULT_TSM = ROOT / "tsm_markets.db"
DEFAULT_LOG = ROOT / "logs" / "aggregates.log"


def main(argv: Optional[List[str]] = None) -> int:
    from woweconomy.dbcompat import load_env

    load_env()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command",
                    choices=["build", "validate", "coverage", "price"])
    ap.add_argument("--intel-db", default=str(DEFAULT_INTEL))
    ap.add_argument("--tsm-db", default=str(DEFAULT_TSM))
    ap.add_argument("--aggregate-db", default=str(ROOT / DEFAULT_AGGREGATE_DB))
    ap.add_argument("--only", action="append", choices=list(AGGREGATES),
                    help="build only this aggregate (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="rebuild partitions even when unchanged")
    ap.add_argument("--last-days", type=int, default=None,
                    help="build only the N most recent days (a daily run uses 2)")
    ap.add_argument("--since", default=None,
                    help="build only days on or after this YYYY-MM-DD")
    ap.add_argument("--days", action="append",
                    help="validate only these days (YYYY-MM-DD, repeatable)")
    ap.add_argument("--limit-days", type=int, default=3,
                    help="how many recent days to validate by default")
    ap.add_argument("--day", help="for 'price': the day to look up")
    ap.add_argument("--scope", help="for 'price': the realm scope")
    ap.add_argument("--kind", default="realm_items",
                    help="for 'price': the TSM kind (default realm_items)")
    ap.add_argument("--item", type=int, help="for 'price': the item id")
    ap.add_argument("--as-of", action="store_true",
                    help="for 'price': reconstruct the level as of --day, "
                         "carrying forward the last observation")
    ap.add_argument("--json", default=None, help="write the report as JSON here")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.FileHandler(args.log, encoding="utf-8")],
    )

    if args.command == "coverage":
        print(render_coverage(args.aggregate_db))
        return 0

    if args.command == "price":
        if not (args.day and args.scope and args.item):
            ap.error("price needs --day, --scope and --item")
        if args.as_of:
            level = price_as_of(args.aggregate_db, args.day, args.scope,
                                args.kind, args.item)
        else:
            level = day_price(args.aggregate_db, args.day, args.scope,
                              args.kind, args.item)
        if level is None:
            print(f"no observation for {args.day} {args.scope} "
                  f"{args.kind} item {args.item}")
            return 1
        print(json.dumps(level, indent=2))
        return 0

    if args.command == "build":
        report = build(args.intel_db, args.tsm_db, args.aggregate_db,
                       only=args.only, force=args.force,
                       days=args.last_days, since=args.since)
        print(report.summary())
        print()
        print(render_coverage(args.aggregate_db))
        payload = report.to_dict()
        if args.json:
            Path(args.json).write_text(json.dumps(payload, indent=2),
                                       encoding="utf-8")
            print(f"\nwrote {args.json}")
        return 1 if report.errors else 0

    report = validate(args.aggregate_db, args.intel_db, args.tsm_db,
                      days=args.days, limit_days=args.limit_days)
    print(report.summary())
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=2),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
