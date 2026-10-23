"""Coordination between pipeline workers.

Several processes now touch the same stores at different times: a fetcher,
a trainer, an exporter, a strategy builder.  Two rules make that safe:

* **one worker per task at a time**, so the same 92 books are not downloaded
  twice and two trainers cannot write the same artifact;
* **a dead worker must not block the pipeline**, so whatever it held has to
  expire on its own.

Both come from a lease: a row with an owner token and an expiry time.

* *Acquiring* is a single conditional write (``INSERT ... ON CONFLICT DO
  UPDATE ... WHERE expires_at <= now``).  SQLite runs it atomically, so two
  workers racing for the same name produce exactly one winner.
* *Renewing* and *releasing* are conditional on the token still being ours.
  A worker that stalled past its expiry, had the lease taken over by
  someone else, and then woke up cannot clobber the new owner's state -- it
  gets :class:`LeaseLost` instead.  That is the "safe ownership check": the
  token, not the process identity, decides who may write.
* Nothing here decides *when* a task should run.  Scheduling is the
  caller's business; this module only answers "may I, and only me?".

Durability
----------
The lease table, the run log and the published statistics all live in the
same SQLite database as the market data.  Redis, when present, is a fast
mirror for the *current* view only: losing it costs a little coordination
speed and never a fact, because every lease acquisition and every run
outcome is already written to SQLite before it is mirrored.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

#: Default lease lifetime.  A task that runs longer must renew; one that
#: dies is replaced after this many seconds rather than never.
DEFAULT_TTL = 900

#: A lease is renewed at a third of its life, so two renewals can be missed
#: before anyone else may take over.
RENEW_FRACTION = 3.0


class LeaseLost(RuntimeError):
    """The lease expired and someone else owns it now.

    Raised rather than swallowed: a worker that keeps writing after losing
    its lease is exactly the corruption the lease exists to prevent.
    """


@dataclass(frozen=True)
class Lease:
    """Proof that this process may do the named work until ``expires_at``."""

    name: str
    owner: str
    expires_at: float
    acquired_at: float = 0.0

    def expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def renew_at(self) -> float:
        """When this lease should be renewed, comfortably before it lapses."""
        span = max(1.0, self.expires_at - self.acquired_at)
        return self.acquired_at + span / RENEW_FRACTION


class LeaseStore(Protocol):
    """The operations a worker needs; both backends implement these."""

    def acquire(self, name: str, ttl: float = DEFAULT_TTL,
                owner: Optional[str] = None) -> Optional[Lease]: ...

    def renew(self, lease: Lease, ttl: float = DEFAULT_TTL) -> Lease: ...

    def release(self, lease: Lease) -> bool: ...

    def holder(self, name: str) -> Optional[str]: ...

    def held(self, name: str) -> bool: ...


# ---------------------------------------------------------------------------
# SQLite: the durable default
# ---------------------------------------------------------------------------

class SqliteLeaseStore:
    """Leases in the same database as the data.

    One connection per worker.  ``check_same_thread=False`` is not used:
    each worker owns its connection, which is what SQLite wants.
    """

    def __init__(self, con: sqlite3.Connection, *,
                 clock=time.time) -> None:
        self._con = con
        self._clock = clock

    # -- helpers ---------------------------------------------------------
    def _now(self) -> float:
        return float(self._clock())

    def _one(self, sql: str, params: tuple) -> Any:
        cur = self._con.execute(sql, params)
        return cur

    # -- LeaseStore ------------------------------------------------------
    def acquire(self, name: str, ttl: float = DEFAULT_TTL,
                owner: Optional[str] = None) -> Optional[Lease]:
        """Take ``name`` if it is free or its holder has lapsed.

        The upsert's ``WHERE`` is what makes this safe: the row is only
        replaced when it has already expired, so a live holder is never
        displaced.  ``rowcount`` then says whether we were the one who got
        it -- 0 means someone else still holds it and we must not proceed.
        """
        now = self._now()
        token = owner or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        cur = self._one(
            "INSERT INTO pipeline_lease"
            " (name, owner, acquired_at, expires_at, heartbeat_at, runs)"
            " VALUES (?,?,?,?,?,1)"
            " ON CONFLICT(name) DO UPDATE SET"
            "   owner = excluded.owner,"
            "   acquired_at = excluded.acquired_at,"
            "   expires_at = excluded.expires_at,"
            "   heartbeat_at = excluded.heartbeat_at,"
            "   runs = pipeline_lease.runs + 1"
            " WHERE pipeline_lease.expires_at <= ?",
            (name, token, now, now + ttl, now, now))
        if cur.rowcount != 1:
            self._con.rollback()
            return None
        self._con.commit()
        return Lease(name=name, owner=token, expires_at=now + ttl,
                     acquired_at=now)

    def renew(self, lease: Lease, ttl: float = DEFAULT_TTL) -> Lease:
        now = self._now()
        cur = self._one(
            "UPDATE pipeline_lease SET expires_at = ?, heartbeat_at = ?"
            " WHERE name = ? AND owner = ?",
            (now + ttl, now, lease.name, lease.owner))
        if cur.rowcount != 1:
            self._con.rollback()
            raise LeaseLost(
                f"lease {lease.name!r} is no longer held by {lease.owner!r}"
                " (it expired and was taken over)")
        self._con.commit()
        return Lease(name=lease.name, owner=lease.owner,
                     expires_at=now + ttl, acquired_at=lease.acquired_at)

    def release(self, lease: Lease) -> bool:
        """Give the lease up.  False means we had already lost it."""
        cur = self._one("DELETE FROM pipeline_lease WHERE name = ? AND owner = ?",
                        (lease.name, lease.owner))
        self._con.commit()
        return cur.rowcount == 1

    def holder(self, name: str) -> Optional[str]:
        row = self._con.execute(
            "SELECT owner FROM pipeline_lease WHERE name = ? AND expires_at > ?",
            (name, self._now())).fetchone()
        return str(row[0]) if row else None

    def held(self, name: str) -> bool:
        return self.holder(name) is not None

    # -- inspection ------------------------------------------------------
    def snapshot(self) -> List[Dict[str, Any]]:
        """Every lease, including lapsed ones, for `pipeline status`."""
        now = self._now()
        rows = self._con.execute(
            "SELECT name, owner, acquired_at, expires_at, heartbeat_at, runs"
            " FROM pipeline_lease ORDER BY name").fetchall()
        return [{"name": r[0], "owner": r[1], "acquired_at": int(r[2]),
                 "expires_at": int(r[3]), "heartbeat_at": int(r[4]),
                 "runs": int(r[5]), "live": float(r[3]) > now}
                for r in rows]

    def sweep(self) -> int:
        """Drop lapsed leases so the table does not grow without bound."""
        cur = self._con.execute(
            "DELETE FROM pipeline_lease WHERE expires_at <= ?", (self._now(),))
        self._con.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# Redis: an optional mirror, never the record
# ---------------------------------------------------------------------------

class RedisLeaseStore:
    """Leases in Redis, for workers that are not on the same filesystem.

    Correctness rests on ``SET key token NX PX ttl`` for acquisition and on
    a Lua compare-and-delete for release, so a worker can only ever remove
    its own token.  Redis is still not the record: callers keep the SQLite
    store as the durable log, and this class is used only when a server is
    actually reachable.
    """

    def __init__(self, client: Any, prefix: str = "wow:lease:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def acquire(self, name: str, ttl: float = DEFAULT_TTL,
                owner: Optional[str] = None) -> Optional[Lease]:
        token = owner or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        ok = self._client.set(self._key(name), token, nx=True,
                              px=int(ttl * 1000))
        if not ok:
            return None
        now = time.time()
        return Lease(name=name, owner=token, expires_at=now + ttl,
                     acquired_at=now)

    def renew(self, lease: Lease, ttl: float = DEFAULT_TTL) -> Lease:
        # Only the holder may extend: a plain EXPIRE would let a stale
        # worker keep someone else's lease alive.
        script = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                  "redis.call('pexpire', KEYS[1], ARGV[2]); return 1 "
                  "else return 0 end")
        ok = self._client.eval(script, 1, self._key(lease.name),
                               lease.owner, int(ttl * 1000))
        if not ok:
            raise LeaseLost(f"lease {lease.name!r} is not held by"
                            f" {lease.owner!r}")
        return Lease(name=lease.name, owner=lease.owner,
                     expires_at=time.time() + ttl,
                     acquired_at=lease.acquired_at)

    def release(self, lease: Lease) -> bool:
        script = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                  "return redis.call('del', KEYS[1]) else return 0 end")
        return bool(self._client.eval(script, 1, self._key(lease.name),
                                      lease.owner))

    def holder(self, name: str) -> Optional[str]:
        value = self._client.get(self._key(name))
        return value.decode() if isinstance(value, bytes) else value

    def held(self, name: str) -> bool:
        return self.holder(name) is not None

    def sweep(self) -> int:  # pragma: no cover - Redis expires on its own
        return 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def open_lease_store(con: sqlite3.Connection, *,
                     url: str = "") -> LeaseStore:
    """The durable store, with a Redis mirror only when it is reachable.

    ``url`` comes from ``WOW_PIPELINE_REDIS``.  An unreachable server is a
    warning, not a failure: the pipeline must still run on a machine where
    Redis is not installed, and the SQLite store is sufficient on its own.
    """
    url = url or os.environ.get("WOW_PIPELINE_REDIS", "")
    if not url:
        return SqliteLeaseStore(con)
    try:  # pragma: no cover - depends on an installed, running server
        import redis  # type: ignore

        client = redis.Redis.from_url(url, socket_connect_timeout=1.0,
                                      socket_timeout=1.0)
        client.ping()
        logger.info("pipeline leases mirrored to redis at %s", url)
        return _MirroredLeaseStore(SqliteLeaseStore(con),
                                   RedisLeaseStore(client))
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("redis unavailable (%s); using the sqlite lease store",
                       exc)
        return SqliteLeaseStore(con)


class _MirroredLeaseStore:
    """SQLite decides, Redis is told.

    Acquisition goes through SQLite first so the durable answer is the one
    that counts; the mirror is best-effort and its failure is logged, not
    raised, because a mirror that can veto real work is worse than no mirror.
    """

    def __init__(self, primary: LeaseStore, mirror: LeaseStore) -> None:
        self._primary = primary
        self._mirror = mirror

    def acquire(self, name: str, ttl: float = DEFAULT_TTL,
                owner: Optional[str] = None) -> Optional[Lease]:
        lease = self._primary.acquire(name, ttl, owner)
        if lease is not None:
            try:
                self._mirror.acquire(name, ttl, lease.owner)
            except Exception as exc:  # pragma: no cover - optional
                logger.warning("lease mirror acquire failed: %s", exc)
        return lease

    def renew(self, lease: Lease, ttl: float = DEFAULT_TTL) -> Lease:
        renewed = self._primary.renew(lease, ttl)
        try:
            self._mirror.renew(lease, ttl)
        except Exception as exc:  # pragma: no cover - optional
            logger.warning("lease mirror renew failed: %s", exc)
        return renewed

    def release(self, lease: Lease) -> bool:
        ok = self._primary.release(lease)
        try:
            self._mirror.release(lease)
        except Exception as exc:  # pragma: no cover - optional
            logger.warning("lease mirror release failed: %s", exc)
        return ok

    def holder(self, name: str) -> Optional[str]:
        return self._primary.holder(name)

    def held(self, name: str) -> bool:
        return self._primary.held(name)

    def sweep(self) -> int:
        return self._primary.sweep()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Runs and published statistics
# ---------------------------------------------------------------------------

def record_run(con: sqlite3.Connection, task: str, owner: str, *,
               started_at: float, finished_at: float, ok: bool,
               detail: Optional[Dict[str, Any]] = None) -> str:
    """Write the durable record of one attempt.

    Called for failures too: a run that vanished without a row is
    indistinguishable from one that never started, and the difference
    matters when the pipeline looks stalled.
    """
    run_id = uuid.uuid4().hex[:16]
    con.execute(
        "INSERT INTO pipeline_run (run_id, task, owner, started_at,"
        " finished_at, ok, detail) VALUES (?,?,?,?,?,?,?)",
        (run_id, task, owner, int(started_at), int(finished_at),
         1 if ok else 0, json.dumps(detail or {}, default=str)))
    con.commit()
    return run_id


#: Redis hash holding the hot copy of the shared statistics.  A UI can read
#: this instead of polling Postgres; the database row is still the truth.
STATS_HASH = "goldbound:stats"

_client_cache: Dict[str, Any] = {}


def _stats_client(url: str = ""):
    """A connected Redis client, or ``None``.  Never raises.

    Cached per URL, including the failure: a pipeline on a machine without
    Redis must not pay a connection timeout on every published statistic.
    """
    url = url or os.environ.get("WOW_PIPELINE_REDIS", "")
    if not url:
        return None
    if url in _client_cache:
        return _client_cache[url]
    client = None
    try:  # pragma: no cover - depends on a running server
        import redis  # type: ignore

        candidate = redis.Redis.from_url(url, socket_connect_timeout=1.0,
                                         socket_timeout=1.0)
        candidate.ping()
        client = candidate
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.info("no redis for shared stats (%s); the database is still"
                    " the authority", exc)
    _client_cache[url] = client
    return client


def publish_stat(con: sqlite3.Connection, key: str, value: Any,
                 owner: str = "", now: Optional[float] = None,
                 client=None) -> None:
    """Record the current value of one shared statistic.

    The history is ``pipeline_run``; this is only "what is true now", so it
    is a single upserted row per key rather than an append-only table.

    The row is written first and the Redis copy second, so a mirror failure
    cannot lose a fact -- and a reader that trusts Redis is trusting a cache
    that is always behind, never ahead of, the database.
    """
    payload = json.dumps(value, default=str)
    stamp = int(now if now is not None else time.time())
    con.execute(
        "INSERT INTO pipeline_stat (key, value, updated_at, owner)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at, owner = excluded.owner",
        (key, payload, stamp, owner))
    con.commit()

    mirror = client if client is not None else _stats_client()
    if mirror is not None:
        try:
            mirror.hset(STATS_HASH, key, payload)
            mirror.hset(STATS_HASH + ":meta", key, json.dumps(
                {"updated_at": stamp, "owner": owner}))
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("redis stat mirror failed for %s: %s", key, exc)


def hot_stats(con: sqlite3.Connection, client=None) -> Dict[str, Any]:
    """The shared statistics, read from Redis when it is available.

    Falls back to the database on any problem, so the caller always gets an
    answer.  Use :func:`all_stats` when the answer must be authoritative.
    """
    mirror = client if client is not None else _stats_client()
    if mirror is not None:
        try:
            raw = mirror.hgetall(STATS_HASH) or {}
            if raw:
                return {_text(k): _loads(_text(v)) for k, v in raw.items()}
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("redis stat read failed: %s", exc)
    return all_stats(con)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def read_stat(con: sqlite3.Connection, key: str,
              default: Any = None) -> Any:
    row = con.execute("SELECT value FROM pipeline_stat WHERE key = ?",
                      (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def all_stats(con: sqlite3.Connection) -> Dict[str, Any]:
    return {row[0]: _loads(row[1])
            for row in con.execute(
                "SELECT key, value FROM pipeline_stat ORDER BY key")}


def recent_runs(con: sqlite3.Connection, limit: int = 20) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT run_id, task, owner, started_at, finished_at, ok, detail"
        " FROM pipeline_run ORDER BY started_at DESC LIMIT ?", (int(limit),))
    return [{"run_id": r[0], "task": r[1], "owner": r[2],
             "started_at": int(r[3]), "finished_at": int(r[4] or 0),
             "ok": bool(r[5]), "detail": _loads(r[6])}
            for r in rows]


def _loads(text: Any) -> Any:
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return text
