"""A frozen feature frame, so two runs see the same listings.

The market watcher appends snapshots continuously.  A Sentinel run that
rebuilds its frame from the live database therefore does not see the same
rows as the run before it: ``observation_end`` moves forward, the
chronological test block shifts, and two models end up compared on
different data while both reports claim to be "out of sample".

Freezing the frame removes the ambiguity.  The cache is a joblib dump of
the frame with a sidecar ``.sha256``; a cache whose bytes no longer match
its checksum is refused rather than silently re-read, because a silent
change would invalidate every comparison built on it.

joblib rather than parquet: this installation has neither ``pyarrow`` nor
``fastparquet``, and the frame is a few thousand rows wide rather than
billions of rows tall, so the columnar format buys nothing.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Optional

import joblib
import pandas as pd

CHECKSUM_SUFFIX = ".sha256"


class FrameCacheMismatch(RuntimeError):
    """The cached frame is not the frame the checksum describes."""


def file_digest(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def checksum_path(cache: Path) -> Path:
    cache = Path(cache)
    return cache.with_suffix(cache.suffix + CHECKSUM_SUFFIX)


def write_frame(frame: pd.DataFrame, cache: Path) -> str:
    cache = Path(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(frame, cache, compress=0)
    digest = file_digest(cache)
    checksum_path(cache).write_text(digest + "\n", encoding="utf-8")
    return digest


def read_frame(cache: Path, *, verify: bool = True) -> pd.DataFrame:
    cache = Path(cache)
    sidecar = checksum_path(cache)
    if verify:
        if not sidecar.exists():
            raise FrameCacheMismatch(
                f"{cache} has no checksum sidecar; refusing to trust it")
        expected = sidecar.read_text(encoding="utf-8").strip()
        actual = file_digest(cache)
        if expected and expected != actual:
            raise FrameCacheMismatch(
                f"{cache} changed since it was frozen "
                f"(expected {expected[:12]}, found {actual[:12]})")
    return joblib.load(cache)


def load_or_build_frame(cache: Path,
                        build: Optional[Callable[[], pd.DataFrame]] = None,
                        *, rebuild: bool = False) -> pd.DataFrame:
    """Read the frozen frame, or build and freeze it once.

    ``build`` is called only when the cache is missing or ``rebuild`` is
    set, so a caller that passes a freshly built frame does not pay for it
    twice.
    """
    cache = Path(cache)
    if cache.exists() and not rebuild:
        return read_frame(cache)
    if build is None:
        raise FileNotFoundError(f"no frame cache at {cache} and nothing to build one from")
    frame = build() if callable(build) else build
    write_frame(frame, cache)
    return frame
