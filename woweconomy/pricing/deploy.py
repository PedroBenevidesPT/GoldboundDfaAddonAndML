"""Ship a validated dataset to the project copy and to the addon the game reads.

The export builds one canonical file; the game reads another.  Copying by
hand is how the two drift, so ``scripts/watch.py`` deploys after every
export:

* both destinations come from configuration -- nothing is guessed, and a
  directory that is not there is reported, never created;
* only a dataset that validates is written, and only when its bytes differ
  from what the destination already holds;
* every write is atomic: a temporary file in the same directory, flushed,
  then ``os.replace``, so a reader never sees half a Lua table;
* the copy being replaced is kept beside it as ``*.lua.bak`` -- only when
  that copy is itself valid, so a broken file never overwrites the last
  good one;
* a locked destination is retried, and a destination that never accepts the
  write fails the whole deployment: one copy updated and the other not is
  not a deployment;
* the source is re-read after the writes.  If a newer export landed in the
  middle, the pass is repeated with the new bytes; if it never settles, the
  result says ``superseded`` and the caller queues another export.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import PricingConfig
from .dataset import validate_dataset

#: the addon folder and the file its loader reads
PACKAGE = "DFA_Estimate"
DATA_FILE = "DFA_Estimate_Data.lua"
BACKUP_SUFFIX = ".bak"

READ = Callable[[Path], bytes]
REPLACE = Callable[[Path, Path], None]
SLEEP = Callable[[float], None]


@dataclass(frozen=True)
class Target:
    """One destination.  ``name`` is what the report calls it."""

    name: str
    path: Path


@dataclass
class TargetResult:
    name: str
    path: str
    #: unchanged | written | failed | missing
    status: str
    error: str = ""
    backup: str = ""
    attempts: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "path": self.path, "status": self.status,
                "error": self.error, "backup": self.backup,
                "attempts": self.attempts}


@dataclass
class DeployResult:
    ok: bool
    source: str
    checksum: str = ""
    targets: List[TargetResult] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    #: a newer export landed while the copies were being written
    superseded: bool = False
    passes: int = 1

    def as_dict(self) -> Dict[str, object]:
        return {"ok": self.ok, "source": self.source,
                "checksum": self.checksum, "passes": self.passes,
                "superseded": self.superseded,
                "problems": list(self.problems),
                "targets": [target.as_dict() for target in self.targets]}

    def summary(self) -> str:
        """One line per destination, for the log and the alert."""
        parts = [f"{target.name}={target.status}"
                 + (f" ({target.error})" if target.error else "")
                 for target in self.targets]
        if self.problems:
            parts.append("; ".join(self.problems))
        return ", ".join(parts) if parts else "no targets"


def read_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def discover_targets(config: PricingConfig,
                     addons_dir: Optional[Path] = None) -> List[Target]:
    """The two copies, from the configuration the project already uses.

    ``dataset_path`` is the canonical artefact the engine writes;
    ``addons_dir`` is the game's ``Interface/AddOns`` that the learning
    engine and ``--addons-dir`` already name.  No glob, no candidate list:
    a path that is wrong is a path to fix, not one to search around.
    """
    addons = Path(addons_dir) if addons_dir else Path(config.addons_dir)
    return [Target("project", Path(config.dataset_path)),
            Target("addon", addons / PACKAGE / "Data" / DATA_FILE)]


def write_atomic(path: Path, data: bytes, *, replace: REPLACE = os.replace,
                 sleep: SLEEP = time.sleep, attempts: int = 3,
                 delay: float = 0.5) -> int:
    """Write ``data`` to ``path`` atomically, retrying a locked file.

    Returns the number of attempts used and raises the last ``OSError``:
    a temporary file is written and flushed first, so an interrupted write
    leaves the old copy intact instead of a truncated Lua table.
    """
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    last: Optional[OSError] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            replace(temporary, path)
            return attempt
        except OSError as exc:
            last = exc
            try:
                temporary.unlink()
            except OSError:
                pass
            if attempt < attempts:
                sleep(delay * attempt)
    raise last if last is not None else OSError(f"could not write {path}")


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_valid(data: bytes) -> bool:
    return not validate_dataset(data.decode("utf-8", errors="replace"))["problems"]


def _keep_backup(path: Path, current: bytes, *, replace: REPLACE) -> str:
    """Keep the copy being replaced, but never over a good one with a bad one."""
    if not _is_valid(current):
        return ""
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    try:
        write_atomic(backup, current, replace=replace, attempts=1)
    except OSError:
        return ""
    return str(backup)


def _deploy_one(target: Target, data: bytes, checksum: str, *,
                attempts: int, delay: float, sleep: SLEEP, replace: REPLACE,
                read: READ) -> TargetResult:
    path = Path(target.path)
    if not path.parent.is_dir():
        return TargetResult(target.name, str(path), "missing",
                            error=f"directory does not exist: {path.parent}")
    current: Optional[bytes] = None
    if path.is_file():
        try:
            current = read(path)
        except OSError as exc:
            return TargetResult(target.name, str(path), "failed",
                                error=f"cannot read it: {exc}")
    if current is not None and _checksum(current) == checksum:
        # The bytes are already right: an unchanged dataset is not rewritten,
        # which is what keeps a 700 KB file from being touched every tick.
        return TargetResult(target.name, str(path), "unchanged")

    backup = _keep_backup(path, current, replace=replace) if current is not None else ""
    try:
        used = write_atomic(path, data, replace=replace, sleep=sleep,
                            delay=delay, attempts=attempts)
    except OSError as exc:
        return TargetResult(target.name, str(path), "failed",
                            error=f"{type(exc).__name__}: {exc}",
                            backup=backup, attempts=attempts)
    try:
        written = read(path)
    except OSError as exc:
        return TargetResult(target.name, str(path), "failed",
                            error=f"cannot read it back: {exc}",
                            backup=backup, attempts=used)
    if _checksum(written) != checksum:
        return TargetResult(target.name, str(path), "failed",
                            error="the file does not match what was written",
                            backup=backup, attempts=used)
    return TargetResult(target.name, str(path), "written",
                        backup=backup, attempts=used)


def deploy(config: PricingConfig, *, addons_dir: Optional[Path] = None,
           passes: int = 3, attempts: int = 3, delay: float = 0.5,
           sleep: SLEEP = time.sleep, replace: REPLACE = os.replace,
           read: READ = read_bytes) -> DeployResult:
    """Copy the canonical dataset to both destinations, or say why not.

    ``passes`` bounds the retry when a new export lands mid-copy: the
    second pass reads the newer source, so both copies still end equal.
    """
    targets = discover_targets(config, addons_dir)
    source = Path(targets[0].path)
    if not source.is_file():
        return DeployResult(ok=False, source=str(source),
                            problems=[f"the canonical dataset is missing: {source}"])
    result = DeployResult(ok=False, source=str(source))
    for pass_no in range(1, max(1, passes) + 1):
        try:
            raw = read(source)
        except OSError as exc:
            return DeployResult(ok=False, source=str(source),
                                problems=[f"cannot read the canonical dataset: {exc}"])
        checksum = _checksum(raw)
        problems = list(validate_dataset(
            raw.decode("utf-8", errors="replace"))["problems"])
        if problems:
            # A dataset the reader would refuse is never shipped, and the
            # copies that exist stay as they are.
            return DeployResult(ok=False, source=str(source), checksum=checksum,
                                problems=problems,
                                targets=[TargetResult(t.name, str(t.path),
                                                      "skipped")
                                         for t in targets])
        results = [_deploy_one(target, raw, checksum, attempts=attempts,
                               delay=delay, sleep=sleep, replace=replace,
                               read=read)
                   for target in targets]
        try:
            after = _checksum(read(source))
        except OSError:
            after = checksum
        result = DeployResult(
            ok=all(entry.status in ("unchanged", "written") for entry in results),
            source=str(source), checksum=checksum, targets=results,
            problems=[], superseded=after != checksum, passes=pass_no)
        result.ok = result.ok and not result.superseded
        if not result.superseded:
            break
    return result


def verify(config: PricingConfig, *, addons_dir: Optional[Path] = None,
           read: READ = read_bytes) -> Dict[str, object]:
    """Are both copies present, valid and identical?  Read-only."""
    targets = discover_targets(config, addons_dir)
    entries: List[Dict[str, object]] = []
    for target in targets:
        entry: Dict[str, object] = {"name": target.name, "path": str(target.path),
                                    "exists": target.path.is_file()}
        if entry["exists"]:
            raw = read(target.path)
            entry["checksum"] = _checksum(raw)
            entry["bytes"] = len(raw)
            entry["valid"] = _is_valid(raw)
        entries.append(entry)
    checksums = {str(entry.get("checksum")) for entry in entries
                 if entry["exists"]}
    problems: List[str] = []
    for entry in entries:
        if not entry["exists"]:
            problems.append(f"{entry['name']} copy is missing: {entry['path']}")
        elif not entry["valid"]:
            problems.append(f"{entry['name']} copy does not validate: {entry['path']}")
    if len(checksums) > 1:
        problems.append("the copies differ")
    return {"in_sync": not problems and len(checksums) == 1,
            "checksum": next(iter(checksums)) if len(checksums) == 1 else "",
            "targets": entries, "problems": problems}
