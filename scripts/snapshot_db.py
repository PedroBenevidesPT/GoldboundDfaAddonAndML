"""A consistent, checksummed copy of intel.sqlite before the schema change.

``con.backup()`` is the SQLite online-backup API: it reads through the WAL
and produces a single consistent file, which a plain copy would not (the
2 GB main file plus its -wal is not a restorable pair on its own).

    python scripts/snapshot_db.py --db data/intel.sqlite --out data/_phase1/intel.sqlite.premigration
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    source = Path(args.db)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    started = time.time()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    info = {
        "source": str(source),
        "target": str(target),
        "bytes": target.stat().st_size,
        "sha256": digest(target),
        "seconds": round(time.time() - started, 1),
    }
    if not args.quiet:
        print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
