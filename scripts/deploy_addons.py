"""Copy the toc-listed addon files into the game's AddOns folder.

A change in this repository that never reached `Interface/AddOns` is not a
fix: the client keeps loading the old copy and every test that reads the
project files keeps passing.  This is the step that closes that gap, and it
is deliberately dumb -- it copies exactly what the `.toc` names, writes each
file atomically, and reports what it skipped.

    python scripts/deploy_addons.py            # deploy what changed
    python scripts/deploy_addons.py --check    # exit 1 if the game is stale
    python scripts/deploy_addons.py --dry-run  # list the files it would write
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy import addons                            # noqa: E402

DEFAULT_ADDONS_DIR = Path(
    r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addons-dir", default=str(DEFAULT_ADDONS_DIR),
                        help="the game's Interface/AddOns directory")
    parser.add_argument("--project", default=str(ROOT),
                        help="the repository holding the addon folders")
    parser.add_argument("--only", action="append", default=[],
                        help="deploy only this addon (repeatable)")
    parser.add_argument("--check", action="store_true",
                        help="report staleness and exit 1 instead of writing")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change, write nothing")
    args = parser.parse_args(argv)

    project = Path(args.project)
    target = Path(args.addons_dir)
    names = args.only or tuple(addons.ADDONS)

    if not target.exists():
        print(f"addons directory not found: {target}", file=sys.stderr)
        return 2

    if args.dry_run or args.check:
        stale = 0
        for name in names:
            toc = addons.ADDONS.get(name)
            if not toc or not (project / name / toc).exists():
                print(f"{name}: missing in the project", file=sys.stderr)
                return 2
            missing = addons.missing_files(project / name)
            if missing:
                print(f"{name}: toc names files that do not exist:"
                      f" {', '.join(missing)}", file=sys.stderr)
                return 2
            for relative in [toc, *addons.toc_files(project / name / toc)]:
                origin = (project / name / relative).read_bytes()
                destination = target / name / relative
                if not destination.exists() or destination.read_bytes() != origin:
                    stale += 1
                    print(f"{'would write' if args.dry_run else 'stale'}:"
                          f" {name}/{relative}")
        if args.check and stale:
            print(f"{stale} file(s) in the game copy are out of date",
                  file=sys.stderr)
            return 1
        if not stale:
            print("the game copy is current")
        return 0

    results = addons.deploy(project, target, names)
    failed = 0
    for result in results:
        print(result.summary())
        if not result.ok:
            failed += 1
            for name in result.missing:
                print(f"  missing: {name}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
