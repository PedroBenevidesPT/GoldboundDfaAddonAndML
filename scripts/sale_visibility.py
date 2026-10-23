"""Are our sales visible in the Blizzard auction house snapshots?

    python scripts/sale_visibility.py
    python scripts/sale_visibility.py --realm "Dragonmaw" --limit 200
    python scripts/sale_visibility.py --by-realm --json report.json

Prints the coverage of the check as well as its result: a run where the
snapshots and the sales do not overlap cannot answer the question, and saying
"0 seen" would read like a finding about the market instead of a gap in the
data.
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

from woweconomy.analytics.sale_visibility import (  # noqa: E402
    DEFAULT_LOOKBACK_S,
    by_realm,
    check_sale_visibility,
)
from woweconomy.dbcompat import backend, load_env, redact_dsn, database_url  # noqa: E402

# A CLI is an entry point: read the project's `.env` so the question is
# asked of the database the pipeline writes to.  Without this the run
# silently fell back to a local SQLite file that does not have the
# `ah_snapshot` table, and failed with "no such table" instead of saying
# it was looking in the wrong place.
load_env()

DEFAULT_STORE = ROOT / "tsm_markets.db"


def _gold(copper: Optional[int]) -> str:
    if copper is None:
        return "-"
    return f"{copper / 10_000:,.0f}g"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=str(DEFAULT_STORE),
                    help="the store holding sales and snapshots "
                         "(ignored on the Postgres backend)")
    ap.add_argument("--realm", action="append", default=None,
                    help="only these realms (repeatable)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only the most recent N sales")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_S,
                    help="seconds before a sale to look for its listing")
    ap.add_argument("--by-realm", action="store_true",
                    help="also print the per-realm breakdown")
    ap.add_argument("--json", default=None,
                    help="write the result as JSON here ('-' for stdout)")
    ap.add_argument("--strict", action="store_true",
                    help="exit 3 when the snapshots and the sales do not "
                         "overlap, so an unanswered question is visible to a "
                         "caller that only reads the exit code")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    report = check_sale_visibility(
        Path(args.store), lookback_s=args.lookback,
        realms=tuple(args.realm) if args.realm else None,
        limit=args.limit)
    print(report.summary())

    if args.by_realm:
        rows = by_realm(report)
        if rows:
            print(f"\n  {'realm':22s} {'covered':>8s} {'seen':>8s} "
                  f"{'same price':>10s}")
            for realm, counts in sorted(rows.items(),
                                        key=lambda kv: -kv[1]["covered"]):
                print(f"  {realm:22s} {counts['covered']:>8,} "
                      f"{counts['seen']:>8,} {counts['same_price']:>10,}")

    if report.matched:
        print(f"\n  {'sale':10s} {'item':>8s} {'realm':18s} {'we sold':>9s} "
              f"{'wall':>9s} {'listings':>8s}")
        for match in report.matched[:25]:
            print(f"  {match.event_id[:10]:10s} {match.item_id:>8} "
                  f"{match.realm:18s} "
                  f"{_gold(match.unit_price_copper):>9s} "
                  f"{_gold(match.wall_low_copper):>9s} "
                  f"{match.listings:>8,}")

    if args.json:
        payload = {
            "sales_total": report.sales_total,
            "sales_covered": report.sales_covered,
            "before_snapshots": report.before_snapshots,
            "overlap": report.overlap,
            "matched": len(report.matched),
            "unmatched": len(report.unmatched),
            "same_price": len(report.same_price_matches()),
            "first_sale_at": report.first_sale_at,
            "last_sale_at": report.last_sale_at,
            "first_snapshot_at": report.first_snapshot_at,
            "last_snapshot_at": report.last_snapshot_at,
            "by_realm": by_realm(report),
            "notes": report.notes,
            "elapsed_s": round(report.elapsed_s, 2),
        }
        text = json.dumps(payload, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
            print(f"\nwrote {args.json}")
    # The check ran; that is the success.  "No overlap yet" is a statement
    # about the data, not a failure of the command, so it exits 0 unless the
    # caller asked to be told -- a pipeline task that failed every run because
    # nobody had sold anything yet would be noise nobody reads.
    return 3 if (args.strict and not report.overlap) else 0


if __name__ == "__main__":
    sys.exit(main())
