"""A ``sqlite3``-shaped DBAPI over psycopg2.

The rest of the codebase was written against ``sqlite3``: it calls
``con.execute(...)``, sets ``con.row_factory = sqlite3.Row``, iterates cursors,
reads ``cursor.rowcount`` to detect an upsert race, and runs ``executescript``
on a schema.  This module reproduces that surface on top of psycopg2 so those
call sites do not have to change -- and, more importantly, so there is exactly
one code path whether the process is talking to SQLite or Postgres.

Deliberately *not* supported, because a wrong answer is worse than an error:
``create_function``, ``create_aggregate``, ``iterdump``, ``backup`` and
``text_factory``.  Requesting them raises :class:`UnsupportedSQL`.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Iterator, Optional, Sequence

from .dialect import (
    UnsupportedSQL,
    is_ddl,
    is_pragma,
    known_pragma,
    parse_pragma,
    returns_rows,
    split_statements,
    to_pyformat,
    translate,
    unsupported_pragma,
)

__all__ = ["Row", "PgCursor", "PgConnection"]

#: Rows a server-side cursor pulls per round trip when the caller iterates
#: without asking for a size.  ``fetchmany(n)`` uses ``n``, not this.
DEFAULT_STREAM_BATCH = 5_000

# A single-tuple INSERT, optionally followed by ON CONFLICT.  This is the shape
# psycopg2's execute_values can batch; anything else falls back to executemany.
_BATCHABLE_INSERT = re.compile(
    r"^\s*(?P<head>INSERT\s+INTO\s+(?:[A-Za-z_][\w.]*|\"[^\"]+\")\s*\([^)]*\)\s*VALUES)"
    r"\s*\((?P<tuple>[^()]*)\)"
    r"(?P<tail>\s*(?:ON\s+CONFLICT[\s\S]*)?)\s*$",
    re.I,
)


class Row:
    """A row addressable by position *and* by column name, like ``sqlite3.Row``."""

    __slots__ = ("_values", "_keys", "_index")

    def __init__(self, keys: Sequence[str], values: Sequence[Any]) -> None:
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._index = {k: i for i, k in enumerate(self._keys)}

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._index[key]]
            except KeyError:
                raise IndexError(f"no such column: {key}") from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._keys)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: object) -> bool:
        return key in self._index

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._keys == other._keys and self._values == other._values
        if isinstance(other, (tuple, list)):
            return self._values == tuple(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        pairs = ", ".join(f"{k}={v!r}" for k, v in zip(self._keys, self._values))
        return f"Row({pairs})"


class PgCursor:
    """Wraps a psycopg2 cursor to look like ``sqlite3.Cursor``.

    Two execution modes, chosen by the statement, not by the caller's guess:

    * a **row-returning** statement runs on a *named* (server-side) cursor, so
      rows are fetched in pages and the client never holds the result set.
      Measured on the real 17.4M-row auction join: 85 MB peak instead of
      3,495 MB, for 17.2 s instead of 13.9 s;
    * anything else -- and any statement run with ``buffered=True`` -- runs on
      a plain cursor, which is one round trip and keeps ``rowcount``.

    The plain path is what made the first aggregates build peak at 12.6 GB:
    psycopg2 buffers the *entire* result set client-side inside ``execute()``,
    so a plain cursor over the auction join allocated 3.5 GB before the first
    ``fetchmany`` was ever called.  Streaming is therefore the default for
    reads, and the extra round trip is paid only where the result is small
    enough that it does not matter.
    """

    def __init__(self, connection: "PgConnection",
                 *, buffered: bool = False) -> None:
        self.connection = connection
        self._buffered_mode = buffered
        self._cur: Any = None
        self._server_side = False
        self._rows: Optional[list[Any]] = None
        self._pos = 0
        self._keys: Optional[list[str]] = None
        self._rowcount = -1
        self._done = False
        self._closed = False

    # -- result shaping ----------------------------------------------------
    def _shape(self, rows: Iterable[Sequence[Any]]) -> list[Any]:
        if self.connection.row_factory is Row and self._keys:
            keys = self._keys
            return [Row(keys, r) for r in rows]
        return list(rows)

    def _ensure_keys(self) -> None:
        """Learn the column names from the first page of a streamed result.

        A server-side cursor has no ``description`` until a ``FETCH`` has run,
        so ``row_factory = Row`` cannot be honoured until then.  Capturing it
        here keeps ``Row`` working without forcing the whole result to be
        buffered first.
        """
        if self._keys is None and self._cur is not None:
            desc = self._cur.description
            if desc:
                self._keys = [d[0] for d in desc]

    # -- execution ---------------------------------------------------------
    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        if self._closed:
            raise UnsupportedSQL("cursor is closed")

        # PRAGMA never reaches the server: it is answered from the catalog.
        if is_pragma(sql):
            return self._execute_pragma(sql)

        statement = translate(sql, pk_lookup=self.connection.primary_key)
        statement, placeholders = to_pyformat(statement)

        args = params
        if args is not None and len(args) == 0:
            # sqlite3 tolerates execute(sql, ()); psycopg2 treats an empty
            # sequence as "interpolate nothing" and then chokes on '%%'.
            args = None
            if placeholders:
                raise UnsupportedSQL(
                    f"statement has {placeholders} placeholder(s) but no"
                    f" parameters: {sql.strip()[:200]}"
                )

        self._server_side = (not self._buffered_mode) and returns_rows(statement)
        if self._server_side:
            self._cur = self.connection._raw.cursor(
                name=self.connection.next_cursor_name())
            if self.connection._raw.autocommit:
                # psycopg2 refuses a named cursor outside a transaction.  On
                # an autocommit connection the DECLARE therefore runs WITH
                # HOLD, which Postgres satisfies by materialising the result
                # when the implicit transaction commits.  Client memory stays
                # bounded either way; the materialisation is on the server,
                # which is the side that has the rows to begin with.
                self._cur.withhold = True
            self._cur.itersize = self.connection.stream_batch
            self._cur.execute(statement, args)
            self._keys = None
            self._rowcount = -1
        else:
            self._cur = self.connection._raw.cursor()
            self._cur.execute(statement, args)
            self._rowcount = self._cur.rowcount
            self._capture_description()
        return self

    def _execute_pragma(self, sql: str):
        parsed = parse_pragma(sql)
        if parsed is None:
            raise UnsupportedSQL(f"unparseable PRAGMA: {sql.strip()[:200]}")
        name, arg, value = parsed

        if name in ("table_info", "index_info", "index_list", "foreign_key_list"):
            rows = self.connection.pragma_rows(name, arg or "")
            self._keys = list(self.connection.PRAGMA_COLUMNS[name])
            self._rows = self._shape(rows)
            self._rowcount = len(self._rows)
            return self

        if name in ("page_size", "page_count", "freelist_count", "integrity_check"):
            rows = self.connection.pragma_rows(name, arg or "")
            self._keys = list(self.connection.PRAGMA_COLUMNS[name])
            self._rows = self._shape(rows)
            self._rowcount = len(self._rows)
            return self

        if known_pragma(name):
            # Tuning pragmas: accepted, ignored, and reported as such.  The
            # journal_mode read is the one callers look at, so it answers with
            # the honest equivalent rather than the SQLite string.
            self._keys = [name]
            answer = "postgres" if name == "journal_mode" else None
            self._rows = self._shape([(answer,)]) if answer else []
            self._rowcount = -1
            return self

        raise unsupported_pragma(name, sql)

    def _capture_description(self) -> None:
        desc = self._cur.description
        self._keys = [d[0] for d in desc] if desc else []
        self._rows = None

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        statement = translate(sql, pk_lookup=self.connection.primary_key)
        statement, _ = to_pyformat(statement)
        params = list(seq_of_params)
        if not params:
            self._rowcount = 0
            self._keys = []
            self._rows = []
            return self
        self._cur = self.connection._raw.cursor()

        # psycopg2's executemany sends one statement per row, which is a
        # round-trip per row -- the auction ingest alone is ~3M rows.  For the
        # common single-tuple INSERT, execute_values sends them in pages.
        batch = _BATCHABLE_INSERT.match(statement)
        if batch:
            from psycopg2.extras import execute_values

            template = f"{batch.group('head')} %s{batch.group('tail')}"
            execute_values(self._cur, template, params, page_size=1000)
            self._rowcount = self._cur.rowcount
            self._capture_description()
            return self

        self._cur.executemany(statement, params)
        self._rowcount = self._cur.rowcount
        self._capture_description()
        return self

    # -- fetching ----------------------------------------------------------
    #
    # A streamed result is pulled in pages, so peak memory is one page.  A
    # buffered result (PRAGMA emulation, or ``buffered=True``) is a list with
    # a cursor position, because sqlite3's ``fetchone`` advances and the old
    # implementation here did not -- two calls returned the same row.
    def _page(self, size: int) -> list[Any]:
        """Next ``size`` rows from a server-side cursor."""
        if self._done or self._cur is None:
            return []
        batch = self._cur.fetchmany(size)
        if not batch:
            self._done = True
            # psycopg2 reports the total once a named cursor is exhausted.
            if self._rowcount < 0:
                self._rowcount = self._cur.rowcount
            return []
        self._ensure_keys()
        return self._shape(batch)

    def _buffered_rest(self) -> list[Any]:
        rows = self._rows or []
        rest = rows[self._pos:]
        self._pos = len(rows)
        return rest

    def fetchone(self):
        if self._server_side:
            page = self._page(1)
            return page[0] if page else None
        if self._rows is None:
            self._rows = self._shape(self._cur.fetchall())
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int = 1) -> list[Any]:
        if self._server_side:
            return self._page(size)
        if self._rows is None:
            self._rows = self._shape(self._cur.fetchall())
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self) -> list[Any]:
        if self._server_side:
            out: list[Any] = []
            while True:
                page = self._page(self.connection.stream_batch)
                if not page:
                    return out
                out.extend(page)
        if self._rows is None:
            self._rows = self._shape(self._cur.fetchall())
        return self._buffered_rest()

    def __iter__(self) -> Iterator[Any]:
        if self._server_side:
            def pages() -> Iterator[Any]:
                while True:
                    page = self._page(self.connection.stream_batch)
                    if not page:
                        return
                    yield from page
            return pages()
        if self._rows is None:
            self._rows = self._shape(self._cur.fetchall())
        return iter(self._buffered_rest())

    def __next__(self):
        raise TypeError("cursor is not an iterator; iterate over the cursor instead")

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def lastrowid(self):
        # SQLite-only concept.  Postgres returns ids with RETURNING, which this
        # project does not use (0 call sites), so this is an explicit refusal
        # rather than a None that would be written to a column.
        raise UnsupportedSQL(
            "lastrowid does not exist on Postgres; use INSERT ... RETURNING"
        )

    @property
    def description(self):
        if self._keys is None:
            self._ensure_keys()
        if self._keys is None:
            return None
        return [(k, None, None, None, None, None, None) for k in self._keys]

    def close(self) -> None:
        if not self._closed:
            if self._cur is not None:
                self._cur.close()
            self._closed = True

    def __del__(self) -> None:
        # A named cursor is a server-side resource; leaving one open because
        # the caller used con.execute(...).fetchone() would accumulate them on
        # an autocommit connection, where they are held WITH HOLD.
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "PgCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class PgConnection:
    """Wraps a psycopg2 connection to look like ``sqlite3.Connection``."""

    #: Column names each emulated PRAGMA returns, in sqlite3's order.
    PRAGMA_COLUMNS = {
        "table_info": ("cid", "name", "type", "notnull", "dflt_value", "pk"),
        "index_list": ("seq", "name", "unique", "origin", "partial"),
        "index_info": ("seqno", "cid", "name"),
        "foreign_key_list": ("id", "seq", "table", "from", "to",
                             "on_update", "on_delete", "match"),
        "page_size": ("page_size",),
        "page_count": ("page_count",),
        "freelist_count": ("freelist_count",),
        "integrity_check": ("integrity_check",),
    }

    def __init__(self, raw, *, is_postgres: bool = True,
                 stream_batch: int = DEFAULT_STREAM_BATCH) -> None:
        self._raw = raw
        self.row_factory: Any = None
        self._pk_cache: dict[str, tuple[str, ...]] = {}
        self._closed = False
        #: Rows pulled per round trip when a result is iterated without an
        #: explicit size.  Large enough to amortise the round trip, small
        #: enough that peak memory does not depend on the result size.
        self.stream_batch = max(1, int(stream_batch))
        self._cursor_seq = 0

    def next_cursor_name(self) -> str:
        """A unique server-side cursor name for this connection."""
        self._cursor_seq += 1
        return f"pgc_{id(self) & 0xffffff:x}_{self._cursor_seq}"

    # -- catalog -----------------------------------------------------------
    def primary_key(self, table: str) -> tuple[str, ...]:
        """Primary-key column names for ``table``, in key order.

        Cached per connection: it is consulted for every ``INSERT OR REPLACE``
        and the answer cannot change inside one connection's lifetime.
        """
        key = table.lower()
        if key in self._pk_cache:
            return self._pk_cache[key]
        with self._raw.cursor() as cur:
            cur.execute(
                """
                SELECT a.attname
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                       ON TRUE
                  JOIN pg_attribute a
                    ON a.attrelid = c.oid AND a.attnum = k.attnum
                 WHERE i.indisprimary
                   AND c.relname = %s
                   AND n.nspname = current_schema()
                 ORDER BY k.ord
                """,
                (table,),
            )
            cols = tuple(r[0] for r in cur.fetchall())
        self._pk_cache[key] = cols
        return cols

    def pragma_rows(self, name: str, arg: str) -> list[tuple]:
        """Answer an emulated PRAGMA from the Postgres catalog."""
        with self._raw.cursor() as cur:
            if name == "table_info":
                cur.execute(
                    """
                    SELECT c.ordinal_position - 1 AS cid,
                           c.column_name,
                           upper(c.data_type) AS type,
                           CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
                           c.column_default AS dflt_value,
                           CASE WHEN pk.attname IS NULL THEN 0 ELSE 1 END AS pk
                      FROM information_schema.columns c
                      LEFT JOIN (
                            SELECT a.attname
                              FROM pg_index i
                              JOIN pg_class cl ON cl.oid = i.indrelid
                              JOIN pg_namespace n ON n.oid = cl.relnamespace
                              JOIN pg_attribute a
                                ON a.attrelid = cl.oid AND a.attnum = ANY(i.indkey)
                             WHERE i.indisprimary
                               AND cl.relname = %s
                               AND n.nspname = current_schema()
                      ) pk ON pk.attname = c.column_name
                     WHERE c.table_schema = current_schema() AND c.table_name = %s
                     ORDER BY c.ordinal_position
                    """,
                    (arg, arg),
                )
                return [tuple(r) for r in cur.fetchall()]

            if name == "index_list":
                cur.execute(
                    """
                    SELECT row_number() OVER (ORDER BY indexname) - 1 AS seq,
                           indexname AS name,
                           CASE WHEN indexdef LIKE '%%UNIQUE%%' THEN 1 ELSE 0 END AS unique,
                           'c' AS origin,
                           0 AS partial
                      FROM pg_indexes
                     WHERE schemaname = current_schema() AND tablename = %s
                    """,
                    (arg,),
                )
                return [tuple(r) for r in cur.fetchall()]

            if name == "index_info":
                cur.execute(
                    """
                    SELECT k.ord - 1 AS seqno,
                           a.attnum - 1 AS cid,
                           a.attname AS name
                      FROM pg_index i
                      JOIN pg_class c ON c.oid = i.indrelid
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                      JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
                      JOIN pg_attribute a
                        ON a.attrelid = c.oid AND a.attnum = k.attnum
                     WHERE c.relname = %s AND n.nspname = current_schema()
                    """,
                    (arg,),
                )
                return [tuple(r) for r in cur.fetchall()]

            if name == "foreign_key_list":
                # No foreign keys are declared in this schema; an empty result
                # is the truthful answer, not a stub.
                return []

            if name == "page_size":
                cur.execute("SELECT current_setting('block_size')::bigint")
                return [tuple(r) for r in cur.fetchall()]

            if name == "page_count":
                # Reported so that page_size * page_count equals the database
                # size, which is what the storage audit is actually asking.
                cur.execute(
                    "SELECT ceil(pg_database_size(current_database())"
                    "::numeric / current_setting('block_size')::numeric)::bigint"
                )
                return [tuple(r) for r in cur.fetchall()]

            if name == "freelist_count":
                return [(0,)]

            if name == "integrity_check":
                # Postgres has no equivalent; the honest check available to a
                # client is that the catalog answers.
                cur.execute("SELECT 'ok'")
                return [tuple(r) for r in cur.fetchall()]

        raise UnsupportedSQL(f"PRAGMA {name} is not emulated")

    # -- execution ---------------------------------------------------------
    def cursor(self, *, buffered: bool = False) -> PgCursor:
        """A cursor.  Reads stream by default; ``buffered=True`` forces the
        one-round-trip path for a lookup whose result is known to be tiny."""
        return PgCursor(self, buffered=buffered)

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None, *,
                buffered: bool = False) -> PgCursor:
        return self.cursor(buffered=buffered).execute(sql, params)

    def executemany(self, sql: str, seq_of_params) -> PgCursor:
        return self.cursor().executemany(sql, seq_of_params)

    def executescript(self, script: str) -> None:
        """Run a ``;``-separated script, translating DDL as it goes."""
        for statement in split_statements(script):
            if is_pragma(statement):
                self.execute(statement)
                continue
            prepared = translate(statement, pk_lookup=self.primary_key,
                                 for_ddl=is_ddl(statement))
            prepared, _ = to_pyformat(prepared)
            cur = self._raw.cursor()
            try:
                cur.execute(prepared)
            finally:
                cur.close()
        # sqlite3's executescript commits the pending transaction first and
        # leaves the connection outside one; match that so callers that rely
        # on it (store.connect does) behave identically.
        self._raw.commit()
        self._pk_cache.clear()

    # -- bulk load ---------------------------------------------------------
    def copy_expert(self, sql: str, file) -> None:
        """Run a ``COPY`` statement against a file-like object.

        Exposed because streaming a large table in is the one operation with
        no SQLite equivalent worth emulating: SQLite callers use ``executemany``
        and never need this.
        """
        with self._raw.cursor() as cur:
            cur.copy_expert(sql, file)
        self._raw.commit()
        self._pk_cache.clear()

    # -- transactions ------------------------------------------------------
    def commit(self) -> None:
        self._raw.commit()
        self._pk_cache.clear()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        if not self._closed:
            self._raw.close()
            self._closed = True

    @property
    def in_transaction(self) -> bool:
        import psycopg2.extensions as ext

        return self._raw.info.transaction_status in (
            ext.TRANSACTION_STATUS_INTRANS,
            ext.TRANSACTION_STATUS_INERROR,
        )

    @property
    def isolation_level(self):
        return None if self._raw.autocommit else ""

    @isolation_level.setter
    def isolation_level(self, value) -> None:
        self._raw.autocommit = value is None

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()

    def __repr__(self) -> str:
        return f"<PgConnection {self._raw.dsn}>"
