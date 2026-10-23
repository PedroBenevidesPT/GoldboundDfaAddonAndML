"""Apply, verify and revert the DfA fork of TradeSkillMaster.

The fork is a *patch*, not a copy: it adds one file to the TSM folder
and one line to the TOC.  Nothing of TSM's own is edited, so a TSM
update cannot silently break the fork and the fork can be removed by
deleting one file.

    python scripts/dfa_tsm_patch.py status     # is the fork applied?
    python scripts/dfa_tsm_patch.py apply      # install it
    python scripts/dfa_tsm_patch.py verify     # check the TSM version and the API
    python scripts/dfa_tsm_patch.py revert     # remove it

The patch refuses to apply when the TSM in front of it is not the one
it was written against.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
FORK_DIR = REPO / "DFA_TSM"
SOURCE_FILE = FORK_DIR / "DFAEstimate.lua"

#: the TSM versions the fork was written against.  The patch refuses to
#: apply to anything else, because the API it uses is internal.
SUPPORTED_TSM_VERSIONS = ("v4.14.78",)

#: the line the fork adds to the TOC
TOC_MARKER = "# DfA fork: the DfAEstimate price source"
TOC_LINE = "DFAEstimate.lua"

#: the API the fork uses, and where it must exist for the fork to work
REQUIRED_API = (
    ("LibTSMTypes/Source/CustomString/CustomString.lua",
     r"function CustomString\.RegisterSource"),
    ("LibTSMTypes/Source/CustomString/Classes/Sources.lua",
     r"function Sources\.Register"),
    ("LibTSMTypes/Source/CustomString/Classes/Types.lua",
     r"SOURCE_TYPE"),
    ("Core/Service/AuctionDB/Core.lua",
     r"function AuctionDB\.GetRegionItemData"),
)

#: the file the fork adds to the TSM folder
FORK_FILE_NAME = "DFAEstimate.lua"


@dataclass
class PatchStatus:
    tsm_dir: Path
    tsm_version: str = ""
    toc_path: Path = field(default_factory=Path)
    backup_path: Path = field(default_factory=Path)
    installed: bool = False
    toc_patched: bool = False
    file_present: bool = False
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"TSM folder : {self.tsm_dir}",
                 f"version   : {self.tsm_version or 'unknown'}",
                 f"fork file : {'present' if self.file_present else 'absent'}",
                 f"TOC line  : {'present' if self.toc_patched else 'absent'}"]
        if self.backup_path.exists():
            lines.append(f"backup    : {self.backup_path}")
        for note in self.notes:
            lines.append(f"note      : {note}")
        for problem in self.problems:
            lines.append(f"PROBLEM   : {problem}")
        return "\n".join(lines)


def default_tsm_dir() -> Path:
    return Path(
        r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\TradeSkillMaster")


def read_tsm_version(tsm_dir: Path) -> str:
    toc = tsm_dir / "TradeSkillMaster.toc"
    if not toc.exists():
        return ""
    for line in toc.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## Version:"):
            return line.split(":", 1)[1].strip()
    return ""


def check_api(tsm_dir: Path) -> List[str]:
    """Whether the APIs the fork uses are really there."""
    problems: List[str] = []
    for relative, pattern in REQUIRED_API:
        path = tsm_dir / relative
        if not path.exists():
            problems.append(f"the file {relative} is not in this TSM")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(pattern, text):
            problems.append(f"{relative} does not define {pattern}")
    return problems


def status(tsm_dir: Path) -> PatchStatus:
    result = PatchStatus(tsm_dir=tsm_dir)
    toc = tsm_dir / "TradeSkillMaster.toc"
    result.toc_path = toc
    result.backup_path = tsm_dir / "TradeSkillMaster.toc.dfa-backup"
    result.tsm_version = read_tsm_version(tsm_dir)
    if not toc.exists():
        result.problems.append(f"there is no TradeSkillMaster.toc in {tsm_dir}")
        return result
    text = toc.read_text(encoding="utf-8", errors="replace")
    result.toc_patched = TOC_MARKER in text
    result.file_present = (tsm_dir / FORK_FILE_NAME).exists()
    result.installed = result.toc_patched and result.file_present
    if result.tsm_version and result.tsm_version not in SUPPORTED_TSM_VERSIONS:
        result.problems.append(
            f"this fork was written for TSM {', '.join(SUPPORTED_TSM_VERSIONS)} "
            f"and this is {result.tsm_version}")
    result.problems.extend(check_api(tsm_dir))
    if result.file_present and not result.toc_patched:
        result.problems.append(
            f"{FORK_FILE_NAME} is in the TSM folder but the TOC does not load it")
    if result.toc_patched and not result.file_present:
        result.problems.append(
            "the TOC loads the fork but the file is missing")
    return result


def apply(tsm_dir: Path, force: bool = False) -> PatchStatus:
    """Install the fork."""
    result = status(tsm_dir)
    if result.installed and not force:
        # already there: refresh the file anyway, so that an updated fork
        # reaches the install without anyone having to revert first
        shutil.copy2(SOURCE_FILE, tsm_dir / FORK_FILE_NAME)
        result.notes.append("the fork was already applied; its file was refreshed")
        return result
    if result.problems and not force:
        return result
    toc = result.toc_path
    if not result.backup_path.exists():
        shutil.copy2(toc, result.backup_path)
        result.notes.append(f"the original TOC was kept at {result.backup_path.name}")
    text = toc.read_text(encoding="utf-8", errors="replace")
    # remove a previous marker block, so applying twice is safe
    text = re.sub(r"\n?" + re.escape(TOC_MARKER) + r"\n" + re.escape(TOC_LINE) + r"\n",
                  "\n", text)
    if not text.endswith("\n"):
        text += "\n"
    text += f"\n{TOC_MARKER}\n{TOC_LINE}\n"
    toc.write_text(text, encoding="utf-8")
    shutil.copy2(SOURCE_FILE, tsm_dir / FORK_FILE_NAME)
    result.notes.append("the fork was applied")
    return status(tsm_dir)


def revert(tsm_dir: Path) -> PatchStatus:
    """Remove the fork."""
    result = status(tsm_dir)
    if result.backup_path.exists():
        shutil.copy2(result.backup_path, result.toc_path)
        result.backup_path.unlink()
        result.notes.append("the original TOC was restored")
    else:
        text = result.toc_path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"\n?" + re.escape(TOC_MARKER) + r"\n" + re.escape(TOC_LINE) + r"\n",
                      "\n", text)
        result.toc_path.write_text(text, encoding="utf-8")
        result.notes.append("the fork's TOC lines were removed")
    fork_file = tsm_dir / FORK_FILE_NAME
    if fork_file.exists():
        fork_file.unlink()
        result.notes.append("the fork's file was removed")
    return status(tsm_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "apply", "verify", "revert"])
    parser.add_argument("--tsm-dir", default=None)
    parser.add_argument("--force", action="store_true",
                        help="apply even when the version or the API does not match")
    args = parser.parse_args()

    tsm_dir = Path(args.tsm_dir) if args.tsm_dir else default_tsm_dir()
    if args.command == "status":
        result = status(tsm_dir)
        print(result.describe())
        return 0 if not result.problems else 1
    if args.command == "verify":
        result = status(tsm_dir)
        print(result.describe())
        print()
        print("the APIs the fork needs:")
        for relative, pattern in REQUIRED_API:
            path = tsm_dir / relative
            ok = path.exists() and re.search(
                pattern, path.read_text(encoding="utf-8", errors="replace"))
            print(f"   {'ok  ' if ok else 'MISS'} {relative}  ({pattern})")
        return 0 if not result.problems else 1
    if args.command == "apply":
        result = apply(tsm_dir, force=args.force)
        print(result.describe())
        if result.installed:
            print()
            print("the fork is installed.  **Restart the WoW client** (or /reload):")
            print("the TOC is read when the addon loads, so a client that is already")
            print("running will not see the new source.")
        return 0 if result.installed else 1
    result = revert(tsm_dir)
    print(result.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
