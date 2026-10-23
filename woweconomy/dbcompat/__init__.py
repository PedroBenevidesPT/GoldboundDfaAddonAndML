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

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Union

from .dialect import UnsupportedSQL
from .pg import PgConnection, PgCursor, Row

__all__ = [
    "connect",
    "connect_store",
    "insert_returning_id",
    "backend",
    "database_url",
    "is_postgres",
    "store_exists",
    "load_env",
    "read_env_file",
    "env_file_path",
    "env_file_disagrees",
    "redact_dsn",
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


#: the repository root, used to find the ``.env`` an entry point should read
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_warned_about_env = False


def env_file_path(path: Optional[Union[str, Path]] = None) -> Path:
    """The ``.env`` to read: the one given, or the project's."""
    return Path(path) if path else PROJECT_ROOT / ".env"


def read_env_file(path: Optional[Union[str, Path]] = None) -> dict:
    """Parse a ``.env`` file without touching ``os.environ``.

    Deliberately dependency-free and forgiving: a line without ``=``, a
    comment, a blank line -- all skipped.  Values may be quoted.
    """
    target = env_file_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_env(path: Optional[Union[str, Path]] = None,
             *, override: bool = False) -> dict:
    """Read ``.env`` into ``os.environ``.  Entry points call this.

    **Not** done on import, on purpose.  A library that changed the backend
    when it was imported would flip the test suite to Postgres, where every
    test that hands over a temporary SQLite path would write to the shared
    database instead -- the same split-brain this module exists to prevent,
    arriving from the other side.

    Existing variables win unless ``override`` is set, so
    ``WOW_PIPELINE_BACKEND=sqlite python ...`` still means what it says.
    """
    values = read_env_file(path)
    applied = {}
    for name, value in values.items():
        if override or name not in os.environ:
            os.environ[name] = value
            applied[name] = value
    return applied


def env_file_disagrees(path: Optional[Union[str, Path]] = None) -> str:
    """The DSN the project's ``.env`` names, when this process is on SQLite.

    Empty when there is nothing to say.  This is the check that stops a
    hand-run script from looking like it used the shared database: the
    fallback to SQLite is legitimate, but it must not be silent when a
    ``.env`` right next to the code says the data lives in Postgres.
    """
    if backend() != "sqlite":
        return ""
    return read_env_file(path).get(_ENV_URL, "").strip()


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


def insert_returning_id(con, sql: str, params=(), column: str = "id") -> int:
    """Run an INSERT and return the key the database generated.

    SQLite exposes it as ``cursor.lastrowid``; Postgres has no such thing, so
    the statement is rewritten to ``INSERT ... RETURNING`` rather than the id
    being guessed or the row being looked up afterwards (which would be racy
    on a table two writers can touch).  ``column`` is the key's name because
    ``RETURNING`` has to name it.
    """
    if is_postgres():
        row = con.execute(f"{sql} RETURNING {column}", params).fetchone()
        if row is None:
            raise RuntimeError(f"INSERT returned no {column}: {sql[:120]}")
        return int(row[0])
    return int(con.execute(sql, params).lastrowid or 0)


def store_exists(target: Union[str, Path, None]) -> bool:
    """Whether the store ``target`` names is available.

    On Postgres the answer is always yes: the tables are in the shared
    database, and a ``.exists()`` check on the SQLite path would report a
    healthy store as missing and make callers skip work silently.
    """
    if backend() == "postgres":
        return True
    _warn_if_env_disagrees()
    return target is not None and Path(target).exists()


def _warn_if_env_disagrees() -> None:
    """Say so, once, when a ``.env`` names a database this run is not using.

    A hand-run script that quietly reads a local SQLite file while the
    pipeline writes to Postgres answers a question nobody asked.  The
    fallback is still allowed -- that is how the tests and a fresh
    workstation work -- but it must not be silent.
    """
    global _warned_about_env
    if _warned_about_env:
        return
    dsn = env_file_disagrees()
    if not dsn:
        return
    _warned_about_env = True
    logging.getLogger(__name__).warning(
        "this run is using sqlite, but %s names %s -- export %s=postgres "
        "(or call load_env()) to use the shared database",
        env_file_path(), redact_dsn(dsn), _ENV_BACKEND)


def redact_dsn(dsn: str) -> str:
    """A DSN with the password replaced, for a log line."""
    text = str(dsn)
    if "@" not in text or "://" not in text:
        return text
    scheme, _, rest = text.partition("://")
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


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
