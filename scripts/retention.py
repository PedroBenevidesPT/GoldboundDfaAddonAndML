"""Apply the 30-day retention window to the operational databases.

Dry-run is the default: the command reports what it would remove and changes
nothing.  Deleting requires ``--apply``.

Usage:
    python scripts/retention.py --show-policy
    python scripts/retention.py                      # dry-run, 30 days
    python scripts/retention.py --days 14 --json out.json
    python scripts/retention.py --archive-only --archive-dir D:/dfa-archive
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
    fmt_bytes,
    render_policy,
    vacuum,
)
from woweconomy.rehearsal import compare_space, space_payload, space_report  # noqa: E402

DEFAULT_INTEL = ROOT / "data" / "intel.sqlite"
DEFAULT_TSM = ROOT / "tsm_markets.db"
DEFAULT_LOG = ROOT / "logs" / "retention.log"


def space_summary(intel_db: str, tsm_db: str) -> str:
    """The physical position, so a reclaim can be measured rather than assumed."""
    lines = ["space position (auto_vacuum is OFF: the file only shrinks on VACUUM)",
             f"  {'file':20s} {'file':>10s}  {'live':>14s}  {'free':>13s}"]
    for path in (intel_db, tsm_db):
        report = space_report(path)
        if not report.file_bytes:
            lines.append(f"  {path:20s} {'missing':>10s}")
            continue
        lines.append(
            f"  {Path(path).name:20s} {fmt_bytes(report.file_bytes):>10s}  "
            f"live {fmt_bytes(report.live_bytes):>9s}  "
            f"free {fmt_bytes(report.freelist_bytes):>9s}"
        )
    lines.append("  'free' is what a VACUUM would return to the filesystem")
    return "\n".join(lines)


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
    ap.add_argument("--archive-only", action="store_true",
                    help="write and verify the archive, then stop: nothing is "
                         "deleted. Requires --archive-dir")
    ap.add_argument("--accept-replay-loss", action="store_true",
                    help="allow deleting data that past evaluations could no "
                         "longer be recomputed from")
    ap.add_argument("--only-table", action="append", default=None,
                    help="restrict this run to one table (repeatable); use it "
                         "to stage a first real deletion")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="stop after this many rows; the run can be repeated")
    ap.add_argument("--space", action="store_true",
                    help="print the physical space position and exit; run it "
                         "before and after a deletion to measure the reclaim")
    ap.add_argument("--compare-space", default=None, metavar="BASELINE.json",
                    help="with --space: diff this position against an earlier "
                         "one written by --space --json")
    ap.add_argument("--vacuum", action="store_true",
                    help="rebuild the databases afterwards to reclaim space")
    ap.add_argument("--json", default=None, help="write the report as JSON here")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.show_policy:
        print(render_policy())
        return 0

    if args.space:
        print(space_summary(args.intel_db, args.tsm_db))
        payload = space_payload(args.intel_db, args.tsm_db)
        if args.compare_space:
            baseline = json.loads(Path(args.compare_space).read_text(
                encoding="utf-8"))
            print()
            print(compare_space(baseline, payload))
        if args.json:
            Path(args.json).write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
            print(f"\nwrote {args.json} (pass it back with --compare-space)")
        return 0

    if args.archive_only and not args.archive_dir:
        ap.error("--archive-only needs --archive-dir")
    if args.archive_only and args.apply:
        ap.error("--archive-only and --apply are mutually exclusive")

    _configure_logging(Path(args.log), args.verbose)

    runner = Retention(
        intel_db=args.intel_db,
        tsm_db=args.tsm_db,
        days=args.days,
        archive_dir=args.archive_dir,
        allow_replay_loss=args.accept_replay_loss,
        only_tables=args.only_table,
        max_rows=args.max_rows,
        archive_only=args.archive_only,
    )
    if args.archive_only:
        report = runner.archive()
    elif args.apply:
        report = runner.apply()
    else:
        report = runner.plan()

    print(report.summary())

    if args.archive_only and report.archive_verification:
        print()
        print(f"  archive verification: "
              f"{'PASS' if report.archive_verification.get('ok') else 'FAIL'} "
              f"({report.archive_verification.get('files_ok')}/"
              f"{report.archive_verification.get('files_checked')} files, "
              f"{report.archive_verification.get('rows'):,} rows)")

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
