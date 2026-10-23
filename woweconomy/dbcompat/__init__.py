"""One connection entry point, two backends.

``connect()`` is what the rest of the codebase should call instead of
``sqlite3.connect``.  With no configuration it returns a real
``sqlite3.Connection`` -- byte-for-byte the old behaviour, which is what keeps
the test suite and a workstation run working.  With ``DATABASE_URL`` (or
``WOW_PIPELINE_BACKEND=postgres``) it returns a :class:`PgConnection` that
presents the same surface, so the *same* code runs against the shared stack.

Resolution order
----------------
1. ``WOW_PIPELINE_BACKEND`` if it names a known backend;
2. otherwise ``postgres`` when ``DATABASE_URL`` is set;
3. otherwise ``sqlite``.

Choosing ``postgres`` without a ``DATABASE_URL`` is an error, not a silent
fallback: a worker that quietly writes to a local file while its siblings
write to Postgres is exactly the split-brain this layer exists to prevent.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, Union

from .dialect import UnsupportedSQL
from .pg import PgConnection, PgCursor, Row

__all__ = [
    "connect",
    "connect_store",
    "backend",
    "database_url",
    "is_postgres",
    "store_exists",
    "Row",
    "PgConnection",
    "PgCursor",
    "UnsupportedSQL",
    "BACKENDS",
]

BACKENDS = ("sqlite", "postgres")
_ENV_BACKEND = "WOW_PIPELINE_BACKEND"
_ENV_URL = "DATABASE_URL"


def database_url() -> str:
    """The configured Postgres DSN, or an empty string."""
    return os.environ.get(_ENV_URL, "").strip()


def backend() -> str:
    """Which backend this process should use. See the module docstring."""
    explicit = os.environ.get(_ENV_BACKEND, "").strip().lower()
    if explicit:
        if explicit not in BACKENDS:
            raise ValueError(
                f"{_ENV_BACKEND}={explicit!r} is not one of {BACKENDS}"
            )
        if explicit == "postgres" and not database_url():
            raise ValueError(
                f"{_ENV_BACKEND}=postgres requires {_ENV_URL} to be set"
            )
        return explicit
    return "postgres" if database_url() else "sqlite"


def is_postgres() -> bool:
    return backend() == "postgres"


def connect(
    target: Union[str, Path, None] = None,
    *,
    url: Optional[str] = None,
    row_factory: bool = True,
    autocommit: Optional[bool] = None,
) -> Union[sqlite3.Connection, PgConnection]:
    """Open a connection to whichever backend is configured.

    ``target`` is the SQLite path and is ignored on Postgres.  ``row_factory``
    applies to Postgres only; SQLite callers keep setting ``con.row_factory``
    themselves exactly as before.
    """
    if backend() == "sqlite":
        if target is None:
            raise ValueError("a database path is required for the sqlite backend")
        return sqlite3.connect(str(target))

    import psycopg2  # imported lazily: sqlite-only runs need not have it

    dsn = url or database_url()
    if not dsn:
        raise ValueError(f"the postgres backend needs {_ENV_URL} or an explicit url")
    raw = psycopg2.connect(dsn)
    con = PgConnection(raw)
    if autocommit is not None:
        con.isolation_level = None if autocommit else ""
    if row_factory:
        con.row_factory = Row
    return con


def store_exists(target: Union[str, Path, None]) -> bool:
    """Whether the store ``target`` names is available.

    On Postgres the answer is always yes: the tables are in the shared
    database, and a ``.exists()`` check on the SQLite path would report a
    healthy store as missing and make callers skip work silently.
    """
    if backend() == "postgres":
        return True
    return target is not None and Path(target).exists()


def connect_store(target: Union[str, Path, None] = None, *,
                  readonly: bool = False):
    """Open a store that may be a SQLite file or the shared Postgres.

    This is the call site the rest of the codebase should use for a *data*
    store (the TSM market file, the Blizzard snapshot file).  It exists so
    that replacing ``sqlite3.connect(path)`` with one function is enough to
    make a reader backend-agnostic, and so that ``row_factory`` is set to the
    right class -- ``sqlite3.Row`` on SQLite, :class:`Row` on Postgres.
    Setting ``sqlite3.Row`` on a ``PgConnection`` silently disables
    name-based access, which is the kind of failure that shows up as a
    ``KeyError`` in a report rather than at the boundary.

    ``readonly`` opens a SQLite file with ``mode=ro``.  Postgres has no
    equivalent per-connection flag here; the guarantee comes from the caller
    issuing only reads.
    """
    if backend() == "sqlite":
        if target is None:
            raise ValueError("a database path is required for the sqlite backend")
        if readonly:
            con = sqlite3.connect(f"file:{target}?mode=ro", uri=True,
                                  timeout=60.0)
        else:
            con = sqlite3.connect(str(target), timeout=60.0)
        con.row_factory = sqlite3.Row
        return con
    return connect(None)
