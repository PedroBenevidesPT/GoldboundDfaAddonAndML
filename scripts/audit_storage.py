"""Measure what the operational databases actually hold.

Read-only.  Never writes to a database it opens.

Reports, per database: file size, page accounting, and per table the row count,
the bytes the table occupies (via ``dbstat`` when available) and the range of
every column that looks like a timestamp.

Usage:
    python scripts/audit_storage.py                # all known databases
    python scripts/audit_storage.py --db dfa.db    # one database
    python scripts/audit_storage.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DBS = [
    ROOT / "dfa.db",
    ROOT / "data" / "intel.sqlite",
    ROOT / "tsm_markets.db",
    ROOT / "data" / "_phase3" / "intel_scratch.sqlite",
]

# Columns whose values are epoch seconds.  Anything else that merely mentions
# "time" or "date" is inspected but reported as a plain range.
TS_HINTS = ("ts", "time", "date", "at", "seen", "created", "updated", "expires")


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _epoch_to_iso(value: Any) -> Optional[str]:
    """Render an epoch second as UTC.  Returns None if it is not one."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # 2001-09-09 .. 2033-05-18 covers every plausible value in this project.
    if not (1_000_000_000 <= number <= 2_000_000_000):
        return None
    return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _columns(con: sqlite3.Connection, table: str) -> List[sqlite3.Row]:
    return con.execute(f'PRAGMA table_info("{table}")').fetchall()


def _dbstat(con: sqlite3.Connection) -> Dict[str, int]:
    """Bytes per table, or {} when the build has no dbstat module."""
    try:
        rows = con.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: int(row[1] or 0) for row in rows}


def _remove_scratch(scratch: Path) -> None:
    """Delete a scratch database and its sidecars, ignoring locks."""
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(scratch) + suffix).unlink()
        except (FileNotFoundError, PermissionError, OSError):
            pass


def estimate_table_bytes(path: Path, table: str, rows: Optional[int],
                         sample: int = 200_000) -> Optional[int]:
    """Approximate the bytes a table occupies, when ``dbstat`` is missing.

    Copies up to ``sample`` rows into a scratch database, measures the pages it
    needs, and scales by ``rows / sampled``.  This is an estimate: B-tree pages
    are not perfectly packed, and secondary indexes are counted separately by
    the caller.  Good enough to rank contributors, which is what it is for.
    """
    if not rows:
        return None
    scratch = Path(tempfile.gettempdir()) / f"_measure_{os.getpid()}_{table}.db"
    _remove_scratch(scratch)
    dst = None
    try:
        dst = sqlite3.connect(scratch, uri=True)
        dst.execute("PRAGMA page_size=4096")
        dst.execute(
            "ATTACH DATABASE ? AS src", (f"file:{path.as_posix()}?mode=ro",)
        )
        dst.execute(
            f'CREATE TABLE main.sample AS SELECT * FROM src."{table}" LIMIT ?',
            (sample,),
        )
        copied = dst.execute("SELECT COUNT(*) FROM main.sample").fetchone()[0]
        dst.commit()
        pages = dst.execute("PRAGMA page_count").fetchone()[0]
        page_size = dst.execute("PRAGMA page_size").fetchone()[0]
        if not copied:
            return None
        return int(pages * page_size * (rows / copied))
    except sqlite3.Error:
        return None
    finally:
        if dst is not None:
            try:
                dst.close()
            except sqlite3.Error:
                pass
        _remove_scratch(scratch)


def estimate_index_bytes(path: Path, table: str, rows: Optional[int],
                         sample: int = 200_000) -> Optional[int]:
    """Approximate the bytes the table's indexes occupy.

    Copies a sample, measures it, creates the same indexes on the sample and
    measures again.  The difference, scaled to the full row count, is the
    index cost.  Only expression-free indexes are reproduced.
    """
    if not rows:
        return None
    try:
        con = _connect(path)
    except sqlite3.Error:
        return None
    try:
        # Rebuild each index from its column list rather than by rewriting the
        # original DDL: that survives quoting styles and partial indexes.
        specs: List[List[str]] = []
        for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall():
            cols = [
                r[2] for r in con.execute(f'PRAGMA index_info("{name}")').fetchall()
                if r[2] is not None
            ]
            if cols:
                specs.append(cols)
    finally:
        con.close()
    if not specs:
        return 0

    scratch = Path(tempfile.gettempdir()) / f"_idx_{os.getpid()}_{table}.db"
    _remove_scratch(scratch)
    dst = None
    try:
        dst = sqlite3.connect(scratch, uri=True)
        dst.execute("PRAGMA page_size=4096")
        dst.execute(
            "ATTACH DATABASE ? AS src", (f"file:{path.as_posix()}?mode=ro",)
        )
        dst.execute(
            f'CREATE TABLE main.sample AS SELECT * FROM src."{table}" LIMIT ?',
            (sample,),
        )
        dst.commit()
        before = dst.execute("PRAGMA page_count").fetchone()[0]
        made = 0
        for i, cols in enumerate(specs):
            col_sql = ", ".join(f'"{c}"' for c in cols)
            try:
                dst.execute(f"CREATE INDEX main.sample_ix{i} ON sample ({col_sql})")
                made += 1
            except sqlite3.Error:
                continue
        dst.commit()
        if not made:
            return 0
        after = dst.execute("PRAGMA page_count").fetchone()[0]
        page_size = dst.execute("PRAGMA page_size").fetchone()[0]
        copied = dst.execute("SELECT COUNT(*) FROM main.sample").fetchone()[0]
        if not copied:
            return None
        return int(max(0, after - before) * page_size * (rows / copied))
    except sqlite3.Error:
        return None
    finally:
        if dst is not None:
            try:
                dst.close()
            except sqlite3.Error:
                pass
        _remove_scratch(scratch)


def audit_db(path: Path, measure: bool = False) -> Dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "missing": True}

    started = time.perf_counter()
    size = path.stat().st_size
    # A -wal file holds committed pages that are not in the main file yet.
    wal = Path(str(path) + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0

    con = _connect(path)
    try:
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        dbstat = _dbstat(con)

        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]

        out_tables = []
        for name in tables:
            entry: Dict[str, Any] = {"table": name}
            try:
                entry["rows"] = con.execute(
                    f'SELECT COUNT(*) FROM "{name}"'
                ).fetchone()[0]
            except sqlite3.OperationalError as exc:
                entry["rows"] = None
                entry["error"] = str(exc)
            entry["bytes"] = dbstat.get(name)
            entry["bytes_index"] = dbstat.get(f"idx_{name}")
            if measure and not dbstat and (entry.get("rows") or 0) > 10_000:
                entry["bytes_est"] = estimate_table_bytes(path, name, entry["rows"])
                entry["bytes_index_est"] = estimate_index_bytes(
                    path, name, entry["rows"]
                )

            ranges = {}
            for col in _columns(con, name):
                col_name = col["name"]
                if not any(h in col_name.lower() for h in TS_HINTS):
                    continue
                try:
                    row = con.execute(
                        f'SELECT MIN("{col_name}"), MAX("{col_name}") FROM "{name}"'
                    ).fetchone()
                except sqlite3.OperationalError:
                    continue
                lo, hi = row[0], row[1]
                if lo is None:
                    continue
                ranges[col_name] = {
                    "min": lo,
                    "max": hi,
                    "min_iso": _epoch_to_iso(lo),
                    "max_iso": _epoch_to_iso(hi),
                }
            entry["time_columns"] = ranges
            out_tables.append(entry)

        return {
            "path": str(path),
            "file_bytes": size,
            "wal_bytes": wal_size,
            "total_bytes": size + wal_size,
            "journal_mode": journal,
            "page_size": page_size,
            "page_count": page_count,
            "pages_used": page_count - freelist,
            "freelist_pages": freelist,
            "free_bytes": freelist * page_size,
            "dbstat_available": bool(dbstat),
            "tables": out_tables,
            "audit_seconds": round(time.perf_counter() - started, 2),
        }
    finally:
        con.close()


def _fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def render(results: List[Dict[str, Any]]) -> None:
    grand = 0
    for db in results:
        if db.get("missing"):
            print(f"\n{db['path']}: MISSING")
            continue
        grand += db["total_bytes"]
        print(f"\n{'=' * 78}")
        print(f"{db['path']}")
        print(
            f"  file {_fmt_bytes(db['file_bytes'])}  wal {_fmt_bytes(db['wal_bytes'])}"
            f"  journal={db['journal_mode']}  page={db['page_size']}B"
        )
        print(
            f"  pages {db['page_count']}  used {db['pages_used']}"
            f"  free {db['freelist_pages']} ({_fmt_bytes(db['free_bytes'])})"
            f"  audit {db['audit_seconds']}s"
        )
        print(f"  {'table':34s} {'rows':>10s} {'bytes':>10s}  time range")
        rows_sorted = sorted(
            db["tables"],
            key=lambda t: -((t.get("bytes") or t.get("bytes_est") or 0))
        )
        for t in rows_sorted:
            rng = ""
            for col, r in t.get("time_columns", {}).items():
                if r.get("min_iso") and r.get("max_iso"):
                    rng = f"{col}: {r['min_iso']} .. {r['max_iso']}"
                    break
                rng = f"{col}: {r['min']} .. {r['max']}"
                break
            rows = t.get("rows")
            size = t.get("bytes") if t.get("bytes") is not None else t.get("bytes_est")
            label = _fmt_bytes(size) + ("~" if t.get("bytes") is None and size else "")
            print(
                f"  {t['table']:34s} {rows if rows is not None else '?':>10}"
                f" {label:>10s}  {rng}"
            )
    print(f"\n{'=' * 78}")
    print(f"TOTAL operational database bytes: {_fmt_bytes(grand)}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", action="append", help="database to audit (repeatable)")
    ap.add_argument("--json", help="write the full inventory as JSON here")
    ap.add_argument(
        "--measure", action="store_true",
        help="estimate per-table bytes when dbstat is unavailable (slow)",
    )
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.db] if args.db else DEFAULT_DBS
    results = [audit_db(p, measure=args.measure) for p in paths]
    render(results)

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
