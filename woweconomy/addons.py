"""The addon folders, their manifests, and getting them into the game.

Two things live here, both driven by the ``.toc`` rather than by a hand-kept
list:

* **what an addon loads.**  WoW reads the ``.toc`` and loads each file it
  names.  A list in a test or a script that does not follow the toc tests the
  list, not the addon -- and it silently drifts, which is exactly what
  happened here: the tests asserted a ``Localization.lua`` this addon never
  had, while the toc named seven files nothing checked.
* **how it reaches the client.**  A change in the project that never got
  copied into ``Interface/AddOns`` is not a fix.  ``deploy`` writes each file
  atomically (via :func:`woweconomy.pricing.deploy.write_atomic`, so a reader
  never sees half a Lua file) and reports what it skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .pricing.deploy import write_atomic

#: ``## Key: value`` directives -- metadata, not files.
_DIRECTIVE = re.compile(r"^##")
#: ``#`` comment lines.
_COMMENT = re.compile(r"^#")
#: Anything else ending in .lua or .xml is a file the client loads.
_ASSET = re.compile(r"\.(lua|xml)$", re.IGNORECASE)

#: The addons this repository owns, as ``(project folder, toc name)``.
ADDONS: Dict[str, str] = {
    "DFA_Estimate": "DFA_Estimate.toc",
    "DFA_AuctionIntelligence": "DFA_AuctionIntelligence.toc",
}


def toc_files(toc: Path) -> List[str]:
    """The files a ``.toc`` asks the client to load, in order.

    Paths are normalised to ``/`` so they can be joined to a directory on any
    platform (tocs authored on Windows use backslashes).
    """
    out: List[str] = []
    for raw in Path(toc).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or _DIRECTIVE.match(line) or _COMMENT.match(line):
            continue
        if _ASSET.search(line):
            out.append(line.replace("\\", "/"))
    return out


def missing_files(addon_dir: Path) -> List[str]:
    """Files a toc names that the folder does not contain.

    This is the failure that actually breaks a client load: WoW skips the
    line silently, the addon loads with a chunk missing, and the error
    surfaces much later as a nil global.
    """
    addon_dir = Path(addon_dir)
    missing: List[str] = []
    for toc in sorted(addon_dir.glob("*.toc")):
        for name in toc_files(toc):
            if not (addon_dir / name).exists():
                missing.append(f"{toc.name}: {name}")
    return missing


@dataclass
class DeployResult:
    addon: str
    written: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    target: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        if self.target is None:
            return f"{self.addon}: no install target"
        return (f"{self.addon}: {len(self.written)} written, "
                f"{len(self.unchanged)} already current"
                + (f", {len(self.missing)} missing" if self.missing else ""))


def deploy(project: Path, addons_dir: Path,
           names: Sequence[str] = tuple(ADDONS)) -> List[DeployResult]:
    """Copy each addon's toc-listed files into ``addons_dir``.

    Only files the toc names are copied, so a stray editor backup or a
    scratch file in the project folder never reaches the client.  A file that
    is already byte-identical is left alone: rewriting it would touch its
    timestamp for no reason and make every deploy look like a change.
    """
    project = Path(project)
    addons_dir = Path(addons_dir)
    results: List[DeployResult] = []
    for name in names:
        toc_name = ADDONS.get(name)
        source = project / name
        result = DeployResult(addon=name)
        if not toc_name or not (source / toc_name).exists():
            result.missing.append(toc_name or name)
            results.append(result)
            continue
        target = addons_dir / name
        result.target = target
        target.mkdir(parents=True, exist_ok=True)
        for relative in [toc_name, *toc_files(source / toc_name)]:
            origin = source / relative
            if not origin.exists():
                result.missing.append(relative)
                continue
            destination = target / relative
            data = origin.read_bytes()
            if destination.exists() and destination.read_bytes() == data:
                result.unchanged.append(relative)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(destination, data)
            result.written.append(relative)
        results.append(result)
    return results
