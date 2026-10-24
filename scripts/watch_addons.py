"""Keep the game's files and the project in step, automatically.

The pipeline runs in a Linux container.  It can reach the project through
bind mounts and nothing else: `C:\\Program Files (x86)\\...` does not exist
there.  Two things therefore have to happen on the host, and this script
does both:

* **AddOns** -- when a file the `.toc` names changes, run the same deploy
  `scripts/deploy_addons.py` does (not a second implementation of it), so
  the game copy always carries the dataset the export just wrote.
* **SavedVariables** -- publish the journal into ``data/game/``, which *is*
  mounted, so the container can train from the account's own transactions.
  Without this the export inside the container cannot fit a model at all:
  its only copy of the journal was a Windows file.

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
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy import addons                            # noqa: E402

DEFAULT_ADDONS_DIR = Path(
    r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns")

#: The game's account folder is named after a numeric account id, which is
#: not stable across installs, so it is discovered rather than configured.
DEFAULT_WTF = Path(
    r"C:\Program Files (x86)\World of Warcraft\_retail_\WTF\Account")

#: The saved variables the project reads.  Published into ``data/game``,
#: which is inside the bind mount the worker gets.
GAME_DATA = ("Journalator.lua",)

#: The Lua libraries the journal is compressed with.  They live inside the
#: Journalator addon; the container cannot see Program Files, so they are
#: published beside the journal.  Static files, ~190 KB.
GAME_LIBS = (
    "Journalator/Embeds/Archivist/libs/LibDeflate/LibDeflate.lua",
    "Journalator/Embeds/Archivist/libs/LibSerialize/LibSerialize.lua",
)

#: Where the published copies land, relative to the project.
GAME_DATA_DIR = Path("data") / "game"


def find_saved_variables(wtf_root: Path, name: str) -> Optional[Path]:
    """The newest copy of one saved-variables file, across every account."""
    newest: Optional[Path] = None
    for path in sorted(Path(wtf_root).glob(f"*/SavedVariables/{name}")):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest.stat().st_mtime:
            newest = path
    return newest


def publish_game_data(project: Path, wtf_root: Path,
                      names: Tuple[str, ...] = GAME_DATA,
                      addons_dir: Optional[Path] = None) -> List[str]:
    """Copy the saved variables into the project.  Returns what changed.

    The copy is atomic (a temporary file, then ``os.replace``): the worker
    reads this file to train, and a reader must never see half a Lua table.
    """
    changed: List[str] = []
    target_dir = project / GAME_DATA_DIR
    for name in names:
        source = find_saved_variables(wtf_root, name)
        if source is None:
            continue
        target = target_dir / name
        try:
            if target.exists() and target.stat().st_size == source.stat().st_size \
                    and target.stat().st_mtime >= source.stat().st_mtime:
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            changed.append(f"{name} <- {source}")
        except OSError as exc:
            print(f"could not publish {name}: {exc}", file=sys.stderr)

    if addons_dir is not None:
        for relative in GAME_LIBS:
            source = Path(addons_dir) / relative
            if not source.exists():
                continue
            target = target_dir / relative
            if target.exists() and target.stat().st_size == source.stat().st_size:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                changed.append(f"{relative} <- {source}")
            except OSError as exc:
                print(f"could not publish {relative}: {exc}", file=sys.stderr)
    return changed


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
    parser.add_argument("--wtf-dir", default=str(DEFAULT_WTF),
                        help="the game's WTF/Account folder, for the journal")
    parser.add_argument("--project", default=str(ROOT))
    parser.add_argument("--only", action="append", default=[],
                        help="watch only this addon (repeatable)")
    parser.add_argument("--no-game-data", dest="game_data",
                        action="store_false",
                        help="do not publish the saved variables")
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
    seen_data = ""
    while True:
        if args.game_data:
            # The journal is what the container trains from.  Publishing it
            # is cheap (600 KB) and it is the difference between an export
            # that can fit a model and one that exits saying it cannot.
            for line in publish_game_data(project, Path(args.wtf_dir),
                                          addons_dir=target):
                print(f"published {line}")
            current_data = hashlib.sha256(
                b"".join((project / GAME_DATA_DIR / name).read_bytes()
                         for name in GAME_DATA
                         if (project / GAME_DATA_DIR / name).exists())
            ).hexdigest() if (project / GAME_DATA_DIR).exists() else ""
            if current_data != seen_data:
                seen_data = current_data
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
