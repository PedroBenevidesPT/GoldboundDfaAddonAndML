"""Translate the SQL this project writes into SQL Postgres understands.

Why this exists
---------------
The codebase talks to SQLite directly in ~130 places.  Rewriting all of them
onto an ORM would be a large, risky change that would also invalidate the
existing test suite (which builds throwaway SQLite databases).  Instead the
*statements* are translated at the boundary, so one code path serves both
backends: SQLite in tests and on a workstation, Postgres in the shared stack.

What is translated
------------------
* placeholders: ``?`` -> ``%s``, with ``%`` escaped for psycopg2's pyformat;
* ``INSERT OR REPLACE`` -> ``INSERT ... ON CONFLICT (pk) DO UPDATE``;
* ``INSERT OR IGNORE``  -> ``INSERT ... ON CONFLICT DO NOTHING``;
* ``sqlite_master``     -> a catalog-backed view with the same columns;
* ``strftime('%s', x)`` -> ``EXTRACT(EPOCH FROM x)``;
* SQLite type names in DDL -> Postgres equivalents.

What is *not* translated
------------------------
Anything not listed above is passed through untouched.  That is deliberate:
a translator that quietly rewrites SQL it does not understand is how a
database silently ends up with the wrong rows.  If a statement needs a
construct that has no faithful equivalent, the caller gets
:class:`UnsupportedSQL` naming the construct and the statement, rather than a
plausible-looking result.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

__all__ = [
    "UnsupportedSQL",
    "to_pyformat",
    "split_statements",
    "translate_ddl",
    "translate_insert_or",
    "translate_sqlite_master",
    "translate_strftime_epoch",
    "translate",
    "is_pragma",
    "parse_pragma",
]


class UnsupportedSQL(RuntimeError):
    """A statement uses a SQLite feature with no faithful Postgres equivalent.

    Raised instead of guessing.  The message always names the construct so a
    caller can decide between rewriting the statement and keeping that path on
    SQLite.
    """


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

def to_pyformat(sql: str) -> tuple[str, int]:
    """Convert ``?`` placeholders to ``%s`` and escape literal ``%``.

    Returns ``(sql, placeholder_count)``.

    psycopg2 scans the *whole* statement for ``%``, including inside string
    literals, so a ``LIKE '%foo%'`` has to become ``LIKE '%%foo%%'`` whenever
    parameters are supplied.  Escaping is therefore unconditional here; the
    caller only uses the result when it is actually passing arguments.
    """
    out: list[str] = []
    quote: Optional[str] = None
    count = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
            elif ch == "%":
                out.append("%%")
            elif ch == "?":
                out.append("%s")
                count += 1
            else:
                out.append(ch)
        else:
            if ch == quote:
                # A doubled quote is an escaped quote, not the end of the literal.
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(ch)
                    out.append(ch)
                    i += 2
                    continue
                quote = None
                out.append(ch)
            elif ch == "%":
                out.append("%%")
            else:
                out.append(ch)
        i += 1
    return "".join(out), count


# ---------------------------------------------------------------------------
# Statement splitting (for executescript)
# ---------------------------------------------------------------------------

def split_statements(script: str) -> list[str]:
    """Split a ``;``-separated script, respecting literals and comments.

    SQLite's ``executescript`` accepts several statements at once.  psycopg2
    does too, but only when no parameters are involved, and it gives no useful
    error position -- so they are split here and run one at a time.
    """
    statements: list[str] = []
    buf: list[str] = []
    quote: Optional[str] = None
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif ch == "-" and i + 1 < n and script[i + 1] == "-":
                # line comment: copy through to newline
                j = script.find("\n", i)
                if j == -1:
                    buf.append(script[i:])
                    i = n
                    continue
                buf.append(script[i:j])
                i = j
                continue
            elif ch == "/" and i + 1 < n and script[i + 1] == "*":
                j = script.find("*/", i + 2)
                if j == -1:
                    buf.append(script[i:])
                    i = n
                    continue
                buf.append(script[i:j + 2])
                i = j + 2
                continue
            elif ch == ";":
                text = "".join(buf).strip()
                if text:
                    statements.append(text)
                buf = []
            else:
                buf.append(ch)
        else:
            if ch == quote:
                if i + 1 < n and script[i + 1] == quote:
                    buf.append(ch)
                    buf.append(ch)
                    i += 2
                    continue
                quote = None
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # SQLite's "INTEGER PRIMARY KEY AUTOINCREMENT" is an identity column in
    # Postgres.  BIGINT rather than BIGSERIAL so the sequence is owned and
    # dropped with the column.
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I),
     "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\bAUTOINCREMENT\b", re.I), ""),
    # SQLite INTEGER is a signed 64-bit value and REAL is a 64-bit double.
    # Postgres INTEGER is 32-bit and REAL is 32-bit float, so keeping the
    # SQLite names would silently narrow every price, quantity and timestamp
    # -- 2.5 billion copper does not fit in an INTEGER, and a REAL would lose
    # precision on prices.  Widen to the faithful equivalents.
    (re.compile(r"\bINTEGER\b", re.I), "BIGINT"),
    (re.compile(r"\bREAL\b", re.I), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b", re.I), "BYTEA"),
    (re.compile(r"\bDATETIME\b", re.I), "TIMESTAMPTZ"),
)


def translate_ddl(sql: str) -> str:
    """Rewrite SQLite type names in a DDL statement for Postgres."""
    for pattern, replacement in _DDL_RULES:
        sql = pattern.sub(replacement, sql)
    return sql


# ---------------------------------------------------------------------------
# INSERT OR REPLACE / INSERT OR IGNORE
# ---------------------------------------------------------------------------

_INSERT_OR = re.compile(
    r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+"
    r"(?P<table>[A-Za-z_][\w.]*|\"[^\"]+\"|\[[^\]]+\])"
    r"\s*(?:\((?P<cols>[^)]*)\))?",
    re.I | re.S,
)

_HAS_CONFLICT = re.compile(r"\bON\s+CONFLICT\b", re.I)


def _unquote_ident(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"[]`":
        return name[1:-1]
    return name


def translate_insert_or(
    sql: str,
    pk_columns: Sequence[str],
) -> str:
    """Rewrite ``INSERT OR REPLACE/IGNORE`` into an ``ON CONFLICT`` clause.

    ``pk_columns`` is the target table's primary key, in key order, as read
    from the catalog.  It is required for ``REPLACE``: without a key there is
    no conflict target, so "replace the existing row" has no meaning and the
    statement is rejected rather than degraded to an append.
    """
    if _HAS_CONFLICT.search(sql):
        # Already explicit; the author said what they meant.
        return sql

    m = _INSERT_OR.match(sql)
    if not m:
        return sql

    mode = m.group(1).upper()
    head = sql[: m.start()]
    rest = sql[m.end():]
    # The matched text still contains "OR REPLACE"/"OR IGNORE", which Postgres
    # rejects outright.  Rebuild it as a plain INSERT and express the conflict
    # behaviour in the ON CONFLICT clause instead.
    matched = re.sub(
        r"\bINSERT\s+OR\s+(?:REPLACE|IGNORE)\s+INTO\b",
        "INSERT INTO",
        sql[m.start():m.end()],
        count=1,
        flags=re.I,
    )

    if mode == "IGNORE":
        return f"{head}{matched}{rest} ON CONFLICT DO NOTHING"

    # REPLACE
    if not pk_columns:
        raise UnsupportedSQL(
            "INSERT OR REPLACE on a table with no primary key has no faithful"
            f" Postgres equivalent (no conflict target): {sql.strip()[:200]}"
        )

    cols_raw = m.group("cols")
    if not cols_raw:
        raise UnsupportedSQL(
            "INSERT OR REPLACE without an explicit column list cannot be"
            f" translated (needed to build the DO UPDATE clause): {sql.strip()[:200]}"
        )
    cols = [_unquote_ident(c) for c in cols_raw.split(",")]
    pk = [_unquote_ident(c) for c in pk_columns]
    updates = [c for c in cols if c not in pk]

    target = ", ".join(f'"{c}"' for c in pk)
    if updates:
        assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updates)
        action = f"DO UPDATE SET {assignments}"
    else:
        # Every supplied column is part of the key: the row is identical, so
        # there is nothing to update.
        action = "DO NOTHING"

    return f"{head}{matched}{rest} ON CONFLICT ({target}) {action}"


# ---------------------------------------------------------------------------
# sqlite_master
# ---------------------------------------------------------------------------

# The columns SQLite exposes on sqlite_master, reproduced from the Postgres
# catalog so `SELECT name FROM sqlite_master WHERE type='table'` keeps working.
_SQLITE_MASTER_VIEW = (
    "(SELECT table_name::text AS name, 'table' AS type,"
    " table_name::text AS tbl_name, '' AS sql FROM information_schema.tables"
    " WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    " UNION ALL"
    " SELECT indexname::text AS name, 'index' AS type, tablename::text AS tbl_name,"
    " indexdef AS sql FROM pg_indexes WHERE schemaname = current_schema()"
    " UNION ALL"
    " SELECT table_name::text AS name, 'view' AS type, table_name::text AS tbl_name,"
    " '' AS sql FROM information_schema.views"
    " WHERE table_schema = current_schema()) AS sqlite_master"
)

_SQLITE_MASTER = re.compile(r"\bsqlite_(?:master|schema)\b", re.I)


def translate_sqlite_master(sql: str) -> str:
    """Replace ``sqlite_master`` with a catalog-backed equivalent."""
    return _SQLITE_MASTER.sub(_SQLITE_MASTER_VIEW, sql)


# ---------------------------------------------------------------------------
# strftime
# ---------------------------------------------------------------------------

# `CAST(strftime('%s', ts) AS INTEGER)` is the only SQL-level strftime form
# this project uses; it means "seconds since the epoch".
_STRFTIME_EPOCH_CAST = re.compile(
    r"CAST\s*\(\s*strftime\s*\(\s*'%s'\s*,\s*(?P<arg>.+?)\s*\)\s*AS\s+INTEGER\s*\)",
    re.I | re.S,
)
_STRFTIME_EPOCH = re.compile(
    r"strftime\s*\(\s*'%s'\s*,\s*(?P<arg>.+?)\s*\)",
    re.I | re.S,
)
_STRFTIME_OTHER = re.compile(r"\bstrftime\s*\(", re.I)


def translate_strftime_epoch(sql: str) -> str:
    """Convert ``strftime('%s', x)`` to ``EXTRACT(EPOCH FROM x)``.

    Any other strftime format has no direct Postgres equivalent (``to_char``
    is close but its format codes differ, and a wrong format silently yields
    the wrong string), so it is reported instead of approximated.
    """
    sql = _STRFTIME_EPOCH_CAST.sub(
        lambda m: f"CAST(EXTRACT(EPOCH FROM {m.group('arg')}) AS BIGINT)", sql
    )
    sql = _STRFTIME_EPOCH.sub(
        lambda m: f"EXTRACT(EPOCH FROM {m.group('arg')})", sql
    )
    if _STRFTIME_OTHER.search(sql):
        raise UnsupportedSQL(
            "strftime() with a format other than '%s' has no faithful"
            f" Postgres equivalent: {sql.strip()[:200]}"
        )
    return sql


# ---------------------------------------------------------------------------
# CAST target types
# ---------------------------------------------------------------------------

#: Postgres type names whose SQLite names have a different width.  A CAST is
#: not DDL, so the DDL rules above never see it -- and `CAST(x AS REAL)` in
#: Postgres is a 4-byte float, which turns 893312600 into 893312576.  That is
#: a silent change to every derived price, so it is corrected explicitly.
_CAST_TYPES = {
    "REAL": "DOUBLE PRECISION",
    "INTEGER": "BIGINT",
    "INT": "BIGINT",
    "DOUBLE": "DOUBLE PRECISION",
    "NUMERIC": "NUMERIC",
}

_CAST_START = re.compile(r"\bCAST\s*\(", re.I)


def _matching_paren(sql: str, open_index: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_index``, or -1."""
    depth = 0
    quote: Optional[str] = None
    i = open_index
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        else:
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        i += 1
    return -1


def _top_level_as(inner: str) -> int:
    """Index of the last top-level `` AS `` inside a CAST body, or -1."""
    depth = 0
    quote: Optional[str] = None
    i = 0
    n = len(inner)
    last = -1
    while i < n:
        ch = inner[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and ch in " \t\n\r":
                m = re.match(r"\s+AS\s+", inner[i:], re.I)
                if m:
                    last = i
                    i += m.end()
                    continue
        else:
            if ch == quote:
                if i + 1 < n and inner[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        i += 1
    return last


def translate_cast_types(sql: str) -> str:
    """Widen ``CAST(... AS REAL)`` and ``CAST(... AS INTEGER)`` for Postgres.

    Walks the statement rather than using a regex, because the cast body can
    itself contain parentheses (``CAST(SUM(a * b) AS REAL)``).
    """
    out = []
    pos = 0
    while True:
        m = _CAST_START.search(sql, pos)
        if not m:
            out.append(sql[pos:])
            break
        open_index = m.end() - 1
        close_index = _matching_paren(sql, open_index)
        if close_index == -1:
            out.append(sql[pos:])
            break
        inner = sql[open_index + 1:close_index]
        as_index = _top_level_as(inner)
        if as_index == -1:
            out.append(sql[pos:close_index + 1])
            pos = close_index + 1
            continue
        target = inner[as_index:]
        type_match = re.match(r"\s+AS\s+(?P<type>[A-Za-z_][\w ]*?)\s*$", target, re.I)
        if not type_match:
            out.append(sql[pos:close_index + 1])
            pos = close_index + 1
            continue
        type_name = type_match.group("type").strip()
        widened = _CAST_TYPES.get(type_name.upper())
        if widened and widened.upper() != type_name.upper():
            # Recurse into the body so a nested CAST is widened too.
            new_inner = translate_cast_types(inner[:as_index]) + f" AS {widened}"
            out.append(sql[pos:open_index + 1])
            out.append(new_inner)
            out.append(")")
        else:
            out.append(sql[pos:close_index + 1])
        pos = close_index + 1
    return "".join(out)


# ---------------------------------------------------------------------------
# date() / datetime()
# ---------------------------------------------------------------------------

_DATE_CALL = re.compile(r"\bdate\s*\(", re.I)


def _split_args(inner: str) -> list[str]:
    """Split a function's arguments on top-level commas."""
    parts: list[str] = []
    depth = 0
    quote: Optional[str] = None
    current: list[str] = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                current.append(ch)
            elif ch == "(":
                depth += 1
                current.append(ch)
            elif ch == ")":
                depth -= 1
                current.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        else:
            if ch == quote:
                if i + 1 < n and inner[i + 1] == quote:
                    current.append(ch)
                    i += 1
                else:
                    quote = None
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def translate_date_functions(sql: str) -> str:
    """Rewrite SQLite's ``date()`` into a UTC ``to_char``.

    SQLite has no date type: ``date(x, 'unixepoch')`` turns a Unix timestamp
    into ``'YYYY-MM-DD'`` in UTC, and ``date(text)`` parses an ISO string.
    Postgres has no such function, so both forms become ``to_char`` with an
    explicit UTC zone -- without the zone the answer depends on the server's
    timezone, which is exactly the kind of difference that moves a row into
    the wrong day.
    """
    out: list[str] = []
    pos = 0
    while True:
        m = _DATE_CALL.search(sql, pos)
        if not m:
            out.append(sql[pos:])
            break
        # Skip if this is part of a longer identifier such as "update(".
        if m.start() > 0 and (sql[m.start() - 1].isalnum() or sql[m.start() - 1] == "_"):
            out.append(sql[pos:m.end()])
            pos = m.end()
            continue
        open_index = m.end() - 1
        close_index = _matching_paren(sql, open_index)
        if close_index == -1:
            out.append(sql[pos:])
            break
        args = _split_args(sql[open_index + 1:close_index])
        replacement: Optional[str] = None
        if len(args) == 2 and args[1].strip().lower() in ("'unixepoch'", '"unixepoch"'):
            replacement = (
                f"to_char(to_timestamp({args[0]}) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
            )
        elif len(args) == 1:
            replacement = (
                f"to_char(CAST({args[0]} AS timestamptz) AT TIME ZONE 'UTC',"
                " 'YYYY-MM-DD')"
            )
        if replacement is None:
            out.append(sql[pos:close_index + 1])
        else:
            out.append(sql[pos:m.start()])
            out.append(replacement)
        pos = close_index + 1
    return "".join(out)


# ---------------------------------------------------------------------------
# PRAGMA
# ---------------------------------------------------------------------------

_PRAGMA = re.compile(
    r"^\s*PRAGMA\s+(?P<name>[A-Za-z_][\w]*)"
    r"\s*(?:\(\s*(?P<arg>[^)]*?)\s*\)|=\s*(?P<val>[^;]*?))?\s*;?\s*$",
    re.I | re.S,
)

# Pragmas that only tune the SQLite engine.  On Postgres they are meaningless
# but harmless, so they are accepted and ignored rather than crashing a run.
_PRAGMA_TUNING = {
    "journal_mode", "synchronous", "cache_size", "temp_store", "busy_timeout",
    "mmap_size", "foreign_keys", "query_only", "wal_checkpoint", "locking_mode",
    "read_uncommitted", "recursive_triggers", "secure_delete", "wal_autocheckpoint",
}

# Pragmas that report on the file.  They are answered from the catalog so a
# storage report does not silently divide by zero on Postgres.
_PRAGMA_DIAGNOSTIC = {
    "table_info", "index_list", "index_info", "page_size", "page_count",
    "freelist_count", "integrity_check", "foreign_key_list", "database_list",
}


def is_pragma(sql: str) -> bool:
    return bool(_PRAGMA.match(sql))


def parse_pragma(sql: str) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """Return ``(name, argument, assigned_value)`` or ``None``.

    ``PRAGMA table_info(t)`` -> ``("table_info", "t", None)``
    ``PRAGMA journal_mode=WAL`` -> ``("journal_mode", None, "WAL")``
    """
    m = _PRAGMA.match(sql)
    if not m:
        return None
    return (
        m.group("name").lower(),
        _unquote_ident(m.group("arg")) if m.group("arg") is not None else None,
        m.group("val").strip() if m.group("val") is not None else None,
    )


def strip_leading_comments(sql: str) -> str:
    """Drop leading ``--`` and ``/* */`` comments and whitespace.

    Needed because a schema is written with a comment above most tables, and a
    statement that *starts* with a comment does not look like DDL -- which
    silently skipped the type widening for every table after the first.
    """
    text = sql.lstrip()
    while True:
        if text.startswith("--"):
            newline = text.find("\n")
            if newline == -1:
                return ""
            text = text[newline + 1:].lstrip()
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            if end == -1:
                return ""
            text = text[end + 2:].lstrip()
        else:
            return text


def is_ddl(sql: str) -> bool:
    """Whether a statement is a DDL statement, ignoring leading comments."""
    return bool(re.match(r"^\s*(CREATE|ALTER|DROP|COMMENT|GRANT|REVOKE)\b",
                         strip_leading_comments(sql), re.I))


def known_pragma(name: str) -> bool:
    return name in _PRAGMA_TUNING or name in _PRAGMA_DIAGNOSTIC


def unsupported_pragma(name: str, sql: str) -> UnsupportedSQL:
    return UnsupportedSQL(
        f"PRAGMA {name} has no Postgres equivalent and is not in the known"
        f" tuning/diagnostic set: {sql.strip()[:200]}"
    )


# ---------------------------------------------------------------------------
# Composite translation
# ---------------------------------------------------------------------------

def translate(
    sql: str,
    *,
    pk_lookup=None,
    for_ddl: bool = False,
) -> str:
    """Apply every applicable rewrite to one statement.

    ``pk_lookup(table) -> Sequence[str]`` supplies primary-key columns for
    ``INSERT OR REPLACE``.  It is only consulted for that construct, so a
    caller without a connection can still translate everything else.
    """
    if for_ddl:
        return translate_ddl(sql)

    if _INSERT_OR.match(sql):
        if pk_lookup is None:
            raise UnsupportedSQL(
                "INSERT OR REPLACE/IGNORE needs a primary-key lookup to be"
                f" translated: {sql.strip()[:200]}"
            )
        m = _INSERT_OR.match(sql)
        table = _unquote_ident(m.group("table"))
        sql = translate_insert_or(sql, pk_lookup(table))

    sql = translate_sqlite_master(sql)
    sql = translate_strftime_epoch(sql)
    sql = translate_cast_types(sql)
    sql = translate_date_functions(sql)
    return sql
