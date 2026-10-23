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
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy import addons                            # noqa: E402
from woweconomy.dbcompat import load_env                 # noqa: E402

DEFAULT_ADDONS_DIR = Path(
    r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns")


def default_addons_dir() -> Path:
    """Where the game keeps its addons.

    ``WOW_ADDONS_DIR`` is how the container reaches the folder: the host's
    path is bind-mounted at a fixed place and the variable is re-pointed at
    the mount (see ``docker-compose.yml``), so the same copy runs in both
    places.  Without it, the Windows default is the one
    ``PricingConfig.addons_dir`` already assumes.
    """
    return Path(os.environ.get("WOW_ADDONS_DIR") or DEFAULT_ADDONS_DIR)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # A run from the source tree reads `.env`, where WOW_ADDONS_DIR lives; a
    # container gets the same variable from its environment instead.
    load_env()
    parser.add_argument("--addons-dir", default=str(default_addons_dir()),
                        help="the game's Interface/AddOns directory")
    parser.add_argument("--project", default=str(ROOT),
                        help="the repository holding the addon folders")
    parser.add_argument("--only", action="append", default=[],
                        help="deploy only this addon (repeatable)")
    parser.add_argument("--check", action="store_true",
                        help="report staleness and exit 1 instead of writing")
    parser.add_argument("--require", action="store_true",
                        help="treat a missing AddOns folder as an error"
                             " (default: exit 78, 'not this machine')")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change, write nothing")
    args = parser.parse_args(argv)

    project = Path(args.project)
    target = Path(args.addons_dir)
    names = args.only or tuple(addons.ADDONS)

    if not target.exists():
        # The pipeline runs in a Linux container, which cannot see the
        # game's Program Files directory.  That is not a failure and it is
        # not a success: exit EX_CONFIG so the pipeline reports "skipped"
        # and does not advance `last_ok` -- claiming a deploy that never
        # happened is worse than saying nothing.  The host-side watcher
        # (`scripts/watch_addons.py`) is what actually copies the files.
        message = (f"the game's AddOns folder is not on this machine: {target}")
        print(message, file=sys.stderr)
        return 78 if not args.require else 2

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
