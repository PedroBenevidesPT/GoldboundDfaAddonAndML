"""The comparative dry run: what would be deleted, and what would survive it.

This is the step that has to be read and approved before anything is removed.
It deletes nothing.

Usage:
    python scripts/retention_rehearsal.py
    python scripts/retention_rehearsal.py --days 7
    python scripts/retention_rehearsal.py --json reports/rehearsal.json
    python scripts/retention_rehearsal.py --verify-archive D:/dfa-archive
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

from woweconomy.aggregates import DEFAULT_AGGREGATE_DB, validate  # noqa: E402
from woweconomy.rehearsal import rehearse, space_report  # noqa: E402
from woweconomy.retention import (  # noqa: E402
    RETENTION_DAYS,
    fmt_bytes,
    verify_archive,
)

DEFAULT_INTEL = ROOT / "data" / "intel.sqlite"
DEFAULT_TSM = ROOT / "tsm_markets.db"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=RETENTION_DAYS)
    ap.add_argument("--intel-db", default=str(DEFAULT_INTEL))
    ap.add_argument("--tsm-db", default=str(DEFAULT_TSM))
    ap.add_argument("--aggregate-db", default=str(ROOT / DEFAULT_AGGREGATE_DB))
    ap.add_argument("--archive-dir", default=None,
                    help="where an archive would be written, for the estimate")
    ap.add_argument("--verify-archive", default=None,
                    help="verify an existing archive against its manifest")
    ap.add_argument("--skip-validation", action="store_true",
                    help="do not re-validate the aggregates first")
    ap.add_argument("--json", default=None)
    ap.add_argument("--log", default=str(ROOT / "logs" / "retention.log"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.FileHandler(args.log, encoding="utf-8")],
    )

    payload: dict = {}

    if args.verify_archive:
        verification = verify_archive(args.verify_archive)
        print(verification.summary())
        payload["archive_verification"] = verification.to_dict()
        print()

    validation_ok: Optional[bool] = None
    validation_note = ""
    if not args.skip_validation:
        vr = validate(args.aggregate_db, args.intel_db, args.tsm_db)
        validation_ok = vr.ok
        if vr.ok:
            validation_note = (
                f"validated {len(vr.days_checked)} day(s) against raw: PASS")
        else:
            validation_note = (
                f"validation FAILED ({len(vr.mismatches)} mismatches, "
                f"{sum(vr.missing.values())} missing rows); "
                f"do not delete anything until this passes")
        payload["validation"] = vr.to_dict()

    report = rehearse(args.intel_db, args.tsm_db, args.aggregate_db,
                      days=args.days, archive_dir=args.archive_dir,
                      validation_ok=validation_ok,
                      validation_note=validation_note)
    print(report.summary())

    if not args.skip_validation and validation_ok is False:
        print("\nThe aggregates do not match the raw data. Deleting now would "
              "destroy the only copy of anything they got wrong.",
              file=sys.stderr)

    payload["rehearsal"] = report.to_dict()
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")

    if report.errors:
        return 1
    if validation_ok is False:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
