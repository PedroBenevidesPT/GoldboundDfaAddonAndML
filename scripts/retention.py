"""Apply the 30-day retention window to the operational databases.

Dry-run is the default: the command reports what it would remove and changes
nothing.  Deleting requires ``--apply``.

Usage:
    python scripts/retention.py --show-policy
    python scripts/retention.py                      # dry-run, 30 days
    python scripts/retention.py --days 14 --json out.json
    python scripts/retention.py --apply --archive-dir archives/
    python scripts/retention.py --apply --accept-replay-loss
    python scripts/retention.py --vacuum             # reclaim freed pages
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

from woweconomy.retention import (  # noqa: E402
    RETENTION_DAYS,
    Retention,
    render_policy,
    vacuum,
)

DEFAULT_INTEL = ROOT / "data" / "intel.sqlite"
DEFAULT_TSM = ROOT / "tsm_markets.db"
DEFAULT_LOG = ROOT / "logs" / "retention.log"


def _configure_logging(log_path: Path, verbose: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=RETENTION_DAYS,
                    help=f"rolling window in days (default {RETENTION_DAYS})")
    ap.add_argument("--intel-db", default=str(DEFAULT_INTEL))
    ap.add_argument("--tsm-db", default=str(DEFAULT_TSM))
    ap.add_argument("--show-policy", action="store_true",
                    help="print the declared policy and exit")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without this nothing is removed")
    ap.add_argument("--archive-dir", default=None,
                    help="write expired rows to gzipped JSONL here before "
                         "deleting them")
    ap.add_argument("--accept-replay-loss", action="store_true",
                    help="allow deleting data that past evaluations could no "
                         "longer be recomputed from")
    ap.add_argument("--vacuum", action="store_true",
                    help="rebuild the databases afterwards to reclaim space")
    ap.add_argument("--json", default=None, help="write the report as JSON here")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.show_policy:
        print(render_policy())
        return 0

    _configure_logging(Path(args.log), args.verbose)

    runner = Retention(
        intel_db=args.intel_db,
        tsm_db=args.tsm_db,
        days=args.days,
        archive_dir=args.archive_dir,
        allow_replay_loss=args.accept_replay_loss,
    )
    report = runner.apply() if args.apply else runner.plan()

    print(report.summary())

    if args.apply and not report.errors:
        reclaimed = None
        if args.vacuum:
            reclaimed = vacuum(args.intel_db, args.tsm_db)
            for path, info in reclaimed.items():
                if "error" in info:
                    print(f"  vacuum {path}: {info['error']}")
                else:
                    print(f"  vacuum {path}: reclaimed "
                          f"{info['reclaimed'] / 1e6:.1f}MB in {info['seconds']}s")
        else:
            freed = sum(report.free_bytes_after.values()) - sum(
                report.free_bytes_before.values()
            )
            if freed > 0:
                print(f"  {freed / 1e6:.1f}MB moved to the freelist; "
                      f"run with --vacuum to return it to the filesystem")

    payload = report.to_dict()
    if args.vacuum and args.apply:
        payload["vacuum"] = reclaimed
    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nwrote {args.json}")

    if report.errors:
        print("\ncompleted with errors; nothing was deleted in the affected "
              "database", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
