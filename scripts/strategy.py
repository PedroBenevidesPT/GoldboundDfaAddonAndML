"""Build and read the mog posting plan.

    python scripts/strategy.py build
    python scripts/strategy.py build --limit 5000
    python scripts/strategy.py show --action post --limit 20
    python scripts/strategy.py show --json

``build`` computes the plan and replaces the stored one; ``show`` reads it back.
The plan is what the addon consumes, so it is written to the store rather than
kept in a file: one database, one place to look.
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

from woweconomy.analytics.strategy_plan import (  # noqa: E402
    DEFAULT_LIMIT,
    PLANNED_CATEGORIES,
    build_plans,
    read_plans,
    store_plans,
)
from woweconomy.dbcompat import store_exists  # noqa: E402

DEFAULT_STORE = ROOT / "tsm_markets.db"
DEFAULT_INTEL = ROOT / "data" / "intel.sqlite"

#: sysexits EX_CONFIG.  The plan needs the Data for Azeroth demand export,
#: which is an input this machine may simply not have; `run_pipeline` reports
#: that as a skip rather than a broken job that keeps failing every six hours.
EX_CONFIG = 78


def _fmt_gold(copper: Optional[float]) -> str:
    if copper is None:
        return "-"
    return f"{copper / 10000:,.0f}g"


def main(argv: Optional[List[str]] = None) -> int:
    from woweconomy.dbcompat import load_env

    load_env()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "show"])
    ap.add_argument("--store", default=str(DEFAULT_STORE),
                    help="the market store (ignored on the Postgres backend)")
    ap.add_argument("--intel", default=str(DEFAULT_INTEL),
                    help="the Blizzard snapshot store, for live competition")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="plan at most this many items")
    ap.add_argument("--action", default=None,
                    help="for 'show': only this action")
    ap.add_argument("--category", action="append", default=None,
                    help="for 'build': only these categories (repeatable)")
    ap.add_argument("--json", default=None,
                    help="write the result as JSON here ('-' for stdout)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    store = Path(args.store)
    if not store_exists(store):
        print(f"no market store at {store}", file=sys.stderr)
        return 1

    if args.command == "show":
        rows = read_plans(store, action=args.action, limit=args.limit)
        if args.json:
            text = json.dumps(rows, indent=2)
            if args.json == "-":
                print(text)
            else:
                Path(args.json).write_text(text, encoding="utf-8")
                print(f"wrote {args.json}")
            return 0
        if not rows:
            print("no plan stored; run 'build' first")
            return 1
        print(f"{'item':>8}  {'action':16s} {'price':>10s} {'estimate':>10s} "
              f"{'E[net]':>10s} {'p':>6s} {'conf':7s} name")
        for row in rows:
            p = row["p_sale"]
            print(f"{row['item_id']:>8}  {row['action']:16s} "
                  f"{_fmt_gold(row['price_unit_copper']):>10s} "
                  f"{_fmt_gold(row['estimate_copper']):>10s} "
                  f"{_fmt_gold(row['expected_net_copper']):>10s} "
                  f"{('-' if p is None else f'{p:.2f}'):>6s} "
                  f"{row['confidence']:7s} {row['name']}")
        print(f"\n{len(rows)} row(s), built {rows[0]['built_at']}, "
              f"snapshot {rows[0]['as_of_snapshot'] or 'unknown'}")
        return 0

    report = build_plans(
        store,
        intel_path=Path(args.intel) if args.intel else None,
        limit=args.limit,
        categories=tuple(args.category) if args.category else PLANNED_CATEGORIES)
    print(report.summary())
    if report.errors:
        for error in report.errors:
            print(error, file=sys.stderr)
        missing_input = any("DfA demand" in error or "no market store" in error
                            for error in report.errors)
        return EX_CONFIG if missing_input else 1
    written = store_plans(store, report)
    print(f"stored {written} plan(s)")
    if args.json:
        payload = {
            "considered": report.considered,
            "as_of_snapshot": report.as_of_snapshot,
            "categories": report.categories,
            "actions": report.actions(),
            "elapsed_s": round(report.elapsed_s, 2),
            "errors": report.errors,
        }
        text = json.dumps(payload, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
            print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
