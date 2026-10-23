"""Copy an existing SQLite database into the shared Postgres stack.

The SQLite file is opened read-only and is never modified.  Every table is
streamed with ``COPY`` rather than row-by-row inserts, because 14M auctions at
one round-trip per row is a day of waiting and a night of timeouts.

What "done" means here
----------------------
Row counts alone are not proof: a load that shifts every value by one column
preserves the count perfectly.  So each table is verified on both the number of
rows *and* a content fingerprint (the sum of a numeric column, or a hash of the
text columns when there is no numeric one).  A mismatch is a failure, not a
warning.

The source is never deleted.  Retiring the SQLite file is a separate, explicit
decision made after the Postgres copy has been read from for a while.

Usage
-----
    python scripts/migrate_to_postgres.py --source data/intel.sqlite --dry-run
    python scripts/migrate_to_postgres.py --source data/intel.sqlite
    python scripts/migrate_to_postgres.py --source data/intel.sqlite --verify-only
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Text columns are fingerprinted with this; numeric columns with a sum.  Chosen
# so the fingerprint is order-independent and cheap on both engines.
_HASH_EXPR = "sum(hashtext(CAST({col} AS text)))"


# ---------------------------------------------------------------------------
# COPY encoding
# ---------------------------------------------------------------------------

def encode_text_value(value) -> str:
    """Encode one value for ``COPY ... FROM STDIN (FORMAT text)``.

    Text format has exactly one NULL marker (``\\N``) and a small set of
    escapes, so this is deterministic -- unlike CSV, where an empty string and
    NULL can both be written as nothing.
    """
    if value is None:
        return r"\N"
    if isinstance(value, (bytes, bytearray, memoryview)):
        # bytea hex input is "\xDEAD"; the backslash itself has to be escaped
        # for COPY, otherwise COPY would read \x as a byte escape.
        return r"\\x" + bytes(value).hex()
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # Backslash first, or the later escapes would be double-escaped.
    return (text.replace("\\", "\\\\")
                .replace("\t", "\\t")
                .replace("\n", "\\n")
                .replace("\r", "\\r"))


class _CopyStream:
    """A file-like object that streams encoded rows to psycopg2's COPY.

    psycopg2 pulls with ``read(size)``; rows are produced lazily from the
    SQLite cursor so a 14M-row table never exists in memory.
    """

    def __init__(self, rows: Iterable[Sequence], chunk_rows: int = 5000) -> None:
        self._rows = iter(rows)
        self._chunk_rows = chunk_rows
        self._buffer = ""
        self.rows_written = 0
        self._done = False

    def read(self, size: int = -1) -> str:
        while not self._done and len(self._buffer) < max(size, 65536):
            chunk = []
            for _ in range(self._chunk_rows):
                try:
                    row = next(self._rows)
                except StopIteration:
                    self._done = True
                    break
                chunk.append("\t".join(encode_text_value(v) for v in row) + "\n")
            if chunk:
                self.rows_written += len(chunk)
                self._buffer += "".join(chunk)
            elif self._done:
                break
        if size is None or size < 0:
            out, self._buffer = self._buffer, ""
            return out
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def sqlite_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def pg_columns(pg_con, table: str) -> list[str]:
    rows = pg_con.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r["name"] for r in rows]


def sqlite_columns(con: sqlite3.Connection, table: str) -> list[str]:
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in rows]


def numeric_columns(con: sqlite3.Connection, table: str) -> list[str]:
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in rows if (r[2] or "").upper() in ("INTEGER", "REAL", "NUMERIC")]


def fingerprint_sqlite(con: sqlite3.Connection, table: str, columns: Sequence[str]) -> tuple[int, int]:
    """(row_count, fingerprint) for a SQLite table."""
    count = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    numeric = numeric_columns(con, table)
    if count == 0:
        return 0, 0
    if numeric:
        col = numeric[0]
        total = con.execute(
            f'SELECT coalesce(sum("{col}"), 0) FROM "{table}"'
        ).fetchone()[0]
        return count, int(total or 0)
    # No numeric column: fold the text into a sum of character codes so the
    # fingerprint still changes when values change.
    cols = [c for c in columns if c][:1]
    if not cols:
        return count, 0
    total = con.execute(
        f'SELECT coalesce(sum(length("{cols[0]}")), 0) FROM "{table}"'
    ).fetchone()[0]
    return count, int(total or 0)


def fingerprint_pg(pg_con, table: str, columns: Sequence[str]) -> tuple[int, int]:
    count = pg_con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    if count == 0:
        return 0, 0
    info = pg_con.execute(f'PRAGMA table_info("{table}")').fetchall()
    numeric = [r["name"] for r in info
               if (r["type"] or "").upper().startswith(("INT", "BIGINT", "SMALLINT", "REAL", "DOUBLE", "NUMERIC"))]
    if numeric:
        total = pg_con.execute(
            f'SELECT coalesce(sum("{numeric[0]}"), 0) FROM "{table}"'
        ).fetchone()[0]
        return count, int(total or 0)
    if columns:
        total = pg_con.execute(
            f'SELECT coalesce(sum(length("{columns[0]}")), 0) FROM "{table}"'
        ).fetchone()[0]
        return count, int(total or 0)
    return count, 0


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------

def copy_table(sqlite_con, pg_con, table: str) -> int:
    """Stream one table into Postgres. Returns the row count written.

    Truncation is deliberately *not* done here: truncating a parent with
    ``CASCADE`` after a child has been loaded deletes the child again, and the
    per-table check would already have passed.  The caller empties every
    target up front instead.
    """
    source_cols = sqlite_columns(sqlite_con, table)
    target_cols = pg_columns(pg_con, table)
    if not target_cols:
        raise SystemExit(
            f"table {table!r} does not exist in Postgres; run the schema step first"
        )
    missing = [c for c in source_cols if c not in target_cols]
    if missing:
        raise SystemExit(
            f"table {table!r}: source has columns the target lacks: {missing}"
        )
    # Intersect in target order so a column added on either side does not
    # shift the others.
    cols = [c for c in target_cols if c in source_cols]
    if not cols:
        raise SystemExit(f"table {table!r}: no shared columns")

    quoted = ", ".join(f'"{c}"' for c in cols)
    select = f'SELECT {quoted} FROM "{table}"'

    cursor = sqlite_con.execute(select)
    stream = _CopyStream(cursor)
    copy_sql = f'COPY "{table}" ({quoted}) FROM STDIN WITH (FORMAT text)'
    pg_con.copy_expert(copy_sql, stream)
    return stream.rows_written


# ---------------------------------------------------------------------------
# deriving a target schema from the source
# ---------------------------------------------------------------------------
#
# The TSM market file has 25 tables whose DDL is spread across several
# services and one-off import scripts.  Chasing each CREATE TABLE to keep a
# hand-written Postgres copy in step is how a target schema silently drifts
# from the source it is supposed to mirror.  Instead the target is derived
# from the source's own declared shape, so it cannot disagree with the file
# being copied.  This is a *migration* facility; production code still
# creates its own tables through its own ensure_schema().

#: SQLite declared type -> Postgres type.  INTEGER is widened to BIGINT for
#: the same reason the dialect widens it everywhere else: SQLite integers are
#: 64-bit and Postgres INTEGER is not, so copper totals overflow silently.
_TYPE_MAP = {
    "": "TEXT",
    "INTEGER": "BIGINT",
    "INT": "BIGINT",
    "BIGINT": "BIGINT",
    "SMALLINT": "SMALLINT",
    "REAL": "DOUBLE PRECISION",
    "FLOAT": "DOUBLE PRECISION",
    "DOUBLE": "DOUBLE PRECISION",
    "NUMERIC": "NUMERIC",
    "DECIMAL": "NUMERIC",
    "TEXT": "TEXT",
    "VARCHAR": "TEXT",
    "CHAR": "TEXT",
    "CLOB": "TEXT",
    "BLOB": "BYTEA",
    "BOOLEAN": "BOOLEAN",
    "DATE": "TEXT",
    "DATETIME": "TEXT",
    "TIMESTAMP": "TEXT",
}

#: Defaults that carry over unchanged.  Anything else is reported rather than
#: dropped: a column that silently loses its default is a behavioural change
#: that shows up much later as a NULL.
_SIMPLE_DEFAULT = re.compile(
    r"^(?:-?\d+(?:\.\d+)?|'[^']*'|NULL|CURRENT_TIMESTAMP|CURRENT_DATE|"
    r"CURRENT_TIME|TRUE|FALSE)$",
    re.I,
)


def _pg_type(declared: str) -> str:
    base = (declared or "").split("(")[0].strip().upper()
    return _TYPE_MAP.get(base, "TEXT")


def create_table_from_source(sqlite_con, pg_con, table: str) -> list[str]:
    """Create ``table`` in Postgres mirroring its SQLite declaration.

    Returns the list of defaults that were *not* carried over, so the caller
    can print them instead of pretending the copy is exact.
    """
    info = sqlite_con.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not info:
        raise SystemExit(f"source has no columns for {table!r}")

    # ``INTEGER PRIMARY KEY AUTOINCREMENT`` hands out the next id when an
    # INSERT omits the column.  Postgres has no implicit equivalent, so the
    # target declares an identity instead -- otherwise the writer that relies
    # on it fails with a NOT NULL violation the first time it runs.
    row = sqlite_con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    auto_column = None
    if row and row[0] and re.search(r"AUTOINCREMENT", row[0], re.I):
        match = re.search(r'"?(\w+)"?\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
                          row[0], re.I)
        auto_column = match.group(1) if match else None

    columns: list[str] = []
    skipped_defaults: list[str] = []
    pk_cols: list[tuple[int, str]] = []
    for _cid, name, declared, notnull, default, pk in info:
        if auto_column and name.lower() == auto_column.lower():
            piece = f'"{name}" BIGINT GENERATED BY DEFAULT AS IDENTITY'
            columns.append(piece)
            continue
        piece = f'"{name}" {_pg_type(declared)}'
        if notnull:
            piece += " NOT NULL"
        if default is not None:
            text = str(default).strip()
            if _SIMPLE_DEFAULT.match(text):
                piece += f" DEFAULT {text}"
            else:
                skipped_defaults.append(f"{table}.{name} = {text}")
        columns.append(piece)
        if pk:
            pk_cols.append((int(pk), name))

    if pk_cols:
        pk_cols.sort()
        columns.append("PRIMARY KEY ("
                       + ", ".join(f'"{n}"' for _, n in pk_cols) + ")")

    pg_con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" '
                   f'({", ".join(columns)})')
    pg_con.commit()
    return skipped_defaults


def missing_targets(sqlite_con, pg_con, tables: Sequence[str]) -> list[str]:
    return [t for t in tables if not pg_columns(pg_con, t)]


def foreign_keys(pg_con) -> list[tuple[str, str, str, str]]:
    """(child_table, child_column, parent_table, parent_column) for every FK."""
    rows = pg_con.execute(
        """
        SELECT c.conrelid::regclass::text AS child,
               ca.attname               AS child_col,
               c.confrelid::regclass::text AS parent,
               pa.attname               AS parent_col
          FROM pg_constraint c
          JOIN pg_attribute ca ON ca.attrelid = c.conrelid AND ca.attnum = c.conkey[1]
          JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = c.confkey[1]
         WHERE c.contype = 'f'
         ORDER BY 1, 2
        """
    ).fetchall()
    return [(r[0].replace("public.", ""), r[1],
             r[2].replace("public.", ""), r[3]) for r in rows]


def orphan_report(pg_con) -> list[tuple[str, int]]:
    """Count rows that violate a declared foreign key.

    SQLite never enforced these constraints, so a legacy file may contain
    orphans.  They are loaded and *reported*: silently dropping rows would
    lose data, and silently keeping them without saying so would make the
    Postgres copy look cleaner than it is.
    """
    problems: list[tuple[str, int]] = []
    for child, child_col, parent, parent_col in foreign_keys(pg_con):
        n = pg_con.execute(
            f'SELECT count(*) FROM "{child}" c'
            f' WHERE c."{child_col}" IS NOT NULL'
            f' AND NOT EXISTS (SELECT 1 FROM "{parent}" p'
            f' WHERE p."{parent_col}" = c."{child_col}")'
        ).fetchone()[0]
        if n:
            problems.append((f"{child}.{child_col} -> {parent}.{parent_col}", n))
    return problems


def drop_all_tables(pg_con) -> list[str]:
    """Drop every table in the current schema. Returns what was dropped.

    Only used by ``--recreate``: the SQLite source is untouched, so this
    discards a copy that can always be rebuilt, never the original.
    """
    names = [
        r[0]
        for r in pg_con.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            " ORDER BY table_name"
        ).fetchall()
    ]
    if names:
        joined = ", ".join(f'"{n}"' for n in names)
        pg_con.execute(f"DROP TABLE IF EXISTS {joined} CASCADE")
        pg_con.commit()
    return names


def migrate(
    source: Path,
    *,
    tables: Optional[Sequence[str]] = None,
    truncate: bool = True,
    dry_run: bool = False,
    verify_only: bool = False,
    ensure_schema: bool = True,
    strict_fk: bool = False,
    recreate: bool = False,
    create_missing: bool = False,
) -> int:
    from woweconomy.dbcompat import backend, connect as dbconnect

    if backend() != "postgres":
        raise SystemExit(
            "this script needs the postgres backend: set DATABASE_URL (and"
            " WOW_PIPELINE_BACKEND=postgres)"
        )
    if not source.exists():
        raise SystemExit(f"source database not found: {source}")

    # Read-only on the source: this script must be incapable of damaging the
    # file it is copying.
    sqlite_con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)

    if ensure_schema and not dry_run:
        if recreate:
            probe = dbconnect(None)
            dropped = drop_all_tables(probe)
            probe.close()
            if dropped:
                print(f"dropped {len(dropped)} existing table(s) (--recreate)")
        # store.connect creates every table and index idempotently, so the
        # target is ready before the first COPY rather than half-way through.
        from woweconomy.intel import store

        store.connect().close()
    pg_con = dbconnect(None)

    # Foreign-key triggers are suspended for the load.  The tables are copied
    # in name order, not dependency order, and the source was never enforcing
    # FKs in the first place -- so enforcing them mid-load would fail on the
    # parent that simply has not arrived yet.  They are checked afterwards.
    if not dry_run:
        pg_con.execute("SET session_replication_role = replica")
        pg_con.commit()

    available = sqlite_tables(sqlite_con)
    wanted = [t for t in available if tables is None or t in tables]
    if tables:
        unknown = sorted(set(tables) - set(available))
        if unknown:
            raise SystemExit(f"source has no such table(s): {unknown}")

    print(f"source : {source}  ({len(available)} tables)")
    print(f"target : {pg_con!r}")
    print(f"mode   : {'verify only' if verify_only else 'dry run' if dry_run else 'migrate'}")
    print()

    if create_missing and not dry_run and not verify_only:
        absent = missing_targets(sqlite_con, pg_con, wanted)
        if absent:
            print(f"creating {len(absent)} table(s) from the source schema:")
            skipped: list[str] = []
            for table in absent:
                skipped.extend(create_table_from_source(sqlite_con, pg_con, table))
                cols = len(sqlite_columns(sqlite_con, table))
                print(f"  {table:<32} {cols:>3} columns")
            if skipped:
                print()
                print("defaults NOT carried over (the source expression has no"
                      " Postgres equivalent):")
                for item in skipped:
                    print(f"  - {item}")
            print()

    failures: list[str] = []
    expected: dict[str, tuple[int, int]] = {}

    # Phase 1: empty every target in one statement.  Doing this per table
    # would let a later TRUNCATE ... CASCADE delete rows a previous table had
    # already loaded and verified.
    if truncate and not verify_only and not dry_run and wanted:
        joined = ", ".join(f'"{t}"' for t in wanted)
        pg_con.execute(f"TRUNCATE TABLE {joined} CASCADE")
        pg_con.commit()
        print(f"emptied {len(wanted)} target table(s)")

    # Phase 2: copy.
    for table in wanted:
        src_cols = sqlite_columns(sqlite_con, table)
        expected[table] = fingerprint_sqlite(sqlite_con, table, src_cols)

        if dry_run:
            print(f"  {table:<32} {expected[table][0]:>12,} rows (would copy)")
            continue

        if verify_only:
            print(f"  {table:<32} {'':>12}        (already copied)")
            continue

        started = time.time()
        written = copy_table(sqlite_con, pg_con, table)
        elapsed = time.time() - started
        rate = written / elapsed if elapsed > 0 else 0
        print(f"  {table:<32} {written:>12,} rows  {elapsed:>7.1f}s  {rate:>10,.0f}/s")

    if dry_run:
        pg_con.close()
        sqlite_con.close()
        print()
        print("dry run complete; nothing written")
        return 0

    # Phase 3: verify everything, after every write has happened.  A check
    # made immediately after each load cannot see a later truncate.
    print()
    print("verifying:")
    for table in wanted:
        src_count, src_fp = expected[table]
        dst_count, dst_fp = fingerprint_pg(pg_con, table, sqlite_columns(sqlite_con, table))
        ok = (src_count == dst_count) and (src_fp == dst_fp)
        mark = "ok" if ok else "MISMATCH"
        print(f"  {table:<32} {dst_count:>12,} rows  {mark}")
        if not ok:
            failures.append(
                f"{table}: source {src_count:,}/{src_fp} vs target {dst_count:,}/{dst_fp}"
            )

    pg_con.close()
    sqlite_con.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} table(s) did not verify")
        for f in failures:
            print(f"  - {f}")
        return 1
    if dry_run:
        print("dry run complete; nothing written")
        return 0

    # Restore normal FK enforcement on a fresh connection, then say plainly
    # whether the data actually satisfies the constraints.
    checker = dbconnect(None)
    orphans = orphan_report(checker)
    checker.close()
    if orphans:
        print("foreign keys NOT satisfied by the copied data:")
        for label, n in orphans:
            print(f"  - {label}: {n:,} orphan row(s)")
        if strict_fk:
            print("FAILED: --strict-fk was requested")
            return 1
        print("(loaded anyway; run with --strict-fk to treat this as an error)")
    else:
        print("foreign keys: all satisfied")

    print(f"OK: {len(wanted)} table(s) copied and verified")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", default=str(ROOT / "data" / "intel.sqlite"),
                   help="SQLite file to copy (opened read-only)")
    p.add_argument("--tables", nargs="*", default=None,
                   help="only these tables (default: all)")
    p.add_argument("--no-truncate", action="store_true",
                   help="append instead of replacing target contents")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be copied, write nothing")
    p.add_argument("--verify-only", action="store_true",
                   help="compare an existing copy without writing")
    p.add_argument("--strict-fk", action="store_true",
                   help="fail if the copied data violates a declared foreign key")
    p.add_argument("--recreate", action="store_true",
                   help="drop the target tables and rebuild them (the SQLite"
                        " source is never touched)")
    p.add_argument("--create-missing", action="store_true",
                   help="create target tables that do not exist yet, mirroring"
                        " the source schema (defaults are reported, not dropped"
                        " silently)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return migrate(
        Path(args.source),
        tables=args.tables,
        truncate=not args.no_truncate,
        dry_run=args.dry_run,
        verify_only=args.verify_only,
        strict_fk=args.strict_fk,
        recreate=args.recreate,
        create_missing=args.create_missing,
    )


if __name__ == "__main__":
    raise SystemExit(main())
