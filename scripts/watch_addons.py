"""Keep the game's AddOns folder in step with the project, automatically.

The pipeline runs in a Linux container and writes the dataset through a
bind mount.  It cannot reach `C:\\Program Files (x86)\\...`, so the copy
into the game has to happen on the host.  This is that half: it watches
the files the `.toc` names, and when one changes it runs the same deploy
`scripts/deploy_addons.py` does -- not a second implementation of it.

    python scripts/watch_addons.py             # watch until interrupted
    python scripts/watch_addons.py --once      # sync now, then exit
    python scripts/watch_addons.py --interval 5

Why a watcher and not a cron job: the interesting event is "the export just
wrote a new dataset", which happens every six hours and at no predictable
second.  A poll every few seconds notices it within one cycle; a schedule
would either lag or run when nothing changed.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy import addons                            # noqa: E402

DEFAULT_ADDONS_DIR = Path(
    r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns")


def fingerprint(project: Path, names: Tuple[str, ...]) -> str:
    """A digest of every file the addons ship, by name, size and mtime.

    Content hashing would be more precise and much more expensive: the
    dataset is 1.4 MB and the check runs every few seconds.  A touched file
    that did not change costs one redundant deploy, which is cheap and
    atomic; a missed change costs a seller a stale price, which is not.
    """
    digest = hashlib.sha256()
    for name in names:
        toc = addons.ADDONS.get(name)
        if not toc:
            continue
        directory = project / name
        if not (directory / toc).exists():
            continue
        for relative in [toc, *addons.toc_files(directory / toc)]:
            path = directory / relative
            try:
                stat = path.stat()
            except OSError:
                digest.update(f"{name}/{relative}:missing".encode())
                continue
            digest.update(f"{name}/{relative}:{stat.st_size}:{stat.st_mtime_ns}"
                          .encode("utf-8"))
    return digest.hexdigest()


def sync(project: Path, target: Path, names: Tuple[str, ...],
         quiet: bool = False) -> int:
    """Run the real deploy.  Returns its exit code."""
    from scripts import deploy_addons  # the one implementation

    argv: List[str] = ["--addons-dir", str(target), "--project", str(project)]
    for name in names:
        argv += ["--only", name]
    if quiet:
        argv.append("--dry-run")
    return int(deploy_addons.main(argv))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--addons-dir", default=str(DEFAULT_ADDONS_DIR))
    parser.add_argument("--project", default=str(ROOT))
    parser.add_argument("--only", action="append", default=[],
                        help="watch only this addon (repeatable)")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="seconds between checks (default 10)")
    parser.add_argument("--once", action="store_true",
                        help="sync once and exit, without watching")
    args = parser.parse_args(argv)

    project = Path(args.project)
    target = Path(args.addons_dir)
    names = tuple(args.only) or tuple(addons.ADDONS)

    if not target.exists():
        print(f"the game's AddOns folder is not on this machine: {target}",
              file=sys.stderr)
        return 2

    seen = ""
    while True:
        current = fingerprint(project, names)
        if current != seen:
            # A change in the project that never reached the game is not a
            # fix: this is the step that closes that gap.
            code = sync(project, target, names)
            if code != 0:
                print(f"deploy failed with exit {code}", file=sys.stderr)
            seen = current
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
