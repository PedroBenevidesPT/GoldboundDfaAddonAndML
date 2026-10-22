"""A disk cache for Blizzard's JSON responses.

The scan asks the same questions again and again: the catalogue endpoints on
every run, and one profile answer per character that does not change between
runs.  This keeps the exact bytes Blizzard sent, keyed by the URL and the query,
together with the validators (``ETag`` / ``Last-Modified``) that let a later run
ask *"has this changed?"* instead of *"give me this again?"* -- a 304 costs no
body at all.

It also makes a run resumable.  What was fetched is on disk, so an interrupted
scan does not pay for it twice, and :meth:`ResponseCache.summary` answers *"where
did we stop"* without a single request.

The store is one SQLite file rather than a file per response: a scan of the
~900k discovered characters is more than two million answers, and two million
small files is a slow, unhappy thing on Windows.  The bodies are compressed,
because a profile answer is mostly repeated key names.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS response(
    key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    params TEXT NOT NULL,
    status INTEGER NOT NULL,
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    stored_at REAL NOT NULL,
    validated_at REAL NOT NULL,
    body BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS response_stored ON response(stored_at);
"""


@dataclass(frozen=True)
class CacheEntry:
    """One stored answer, as it is written down."""

    key: str
    url: str
    params: Mapping[str, Any]
    status: int
    etag: str
    last_modified: str
    stored_at: float
    validated_at: float
    raw_text: str

    def payload(self) -> Mapping[str, Any]:
        return json.loads(self.raw_text)


def cache_key(url: str, params: Mapping[str, Any] | None) -> str:
    """A stable key: the URL and the query, in a fixed order."""
    normalised = json.dumps(
        {"url": url, "params": dict(sorted((params or {}).items()))},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


class ResponseCache:
    """The answers, on disk, with the validators that make them reusable."""

    def __init__(self, path: Path | str, *, clock=time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._con = sqlite3.connect(self.path, check_same_thread=False)
        self._con.executescript(SCHEMA)
        self._con.commit()

    # -- reads ---------------------------------------------------------
    def get(self, url: str, params: Mapping[str, Any] | None = None) -> Optional[CacheEntry]:
        key = cache_key(url, params)
        row = self._con.execute(
            "SELECT key, url, params, status, etag, last_modified, stored_at,"
            " validated_at, body FROM response WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        body = zlib.decompress(row[8]).decode("utf-8")
        return CacheEntry(
            key=row[0], url=row[1], params=json.loads(row[2]), status=int(row[3]),
            etag=row[4] or "", last_modified=row[5] or "",
            stored_at=float(row[6]), validated_at=float(row[7]), raw_text=body)

    def is_fresh(self, entry: CacheEntry, ttl_seconds: float) -> bool:
        """Whether the entry may be served without asking Blizzard.

        ``ttl_seconds <= 0`` means "always revalidate": the 304 is cheap and
        the answer is then known to be current rather than assumed to be.
        """
        if ttl_seconds <= 0:
            return False
        return (self._clock() - entry.validated_at) < ttl_seconds

    def age_seconds(self, entry: CacheEntry) -> float:
        return max(0.0, self._clock() - entry.validated_at)

    # -- writes --------------------------------------------------------
    def put(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        *,
        status: int,
        raw_text: str,
        etag: str = "",
        last_modified: str = "",
    ) -> None:
        now = self._clock()
        key = cache_key(url, params)
        self._con.execute(
            "INSERT INTO response(key,url,params,status,etag,last_modified,"
            "stored_at,validated_at,body) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET status=excluded.status,"
            " etag=excluded.etag, last_modified=excluded.last_modified,"
            " stored_at=excluded.stored_at, validated_at=excluded.validated_at,"
            " body=excluded.body",
            (key, url, json.dumps(dict(params or {}), sort_keys=True), int(status),
             etag or "", last_modified or "", now, now,
             zlib.compress(raw_text.encode("utf-8"))))
        self._con.commit()

    def touch(self, entry: CacheEntry) -> None:
        """A 304: the stored answer is confirmed current as of now."""
        self._con.execute("UPDATE response SET validated_at=? WHERE key=?",
                          (self._clock(), entry.key))
        self._con.commit()

    def purge(self, *, older_than_seconds: float = 0.0) -> int:
        """Delete entries last validated before the cutoff.  0 = everything."""
        if older_than_seconds <= 0:
            cursor = self._con.execute("DELETE FROM response")
        else:
            cutoff = self._clock() - older_than_seconds
            cursor = self._con.execute("DELETE FROM response WHERE validated_at<?",
                                       (cutoff,))
        self._con.commit()
        return cursor.rowcount or 0

    # -- reporting -----------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """What is cached and how old it is -- no request, no guess."""
        count, raw_bytes = self._con.execute(
            "SELECT COUNT(1), COALESCE(SUM(LENGTH(body)),0) FROM response").fetchone()
        oldest, newest = self._con.execute(
            "SELECT MIN(validated_at), MAX(validated_at) FROM response").fetchone()
        by_host = self._con.execute(
            "SELECT SUBSTR(url, 1, INSTR(SUBSTR(url, 9), '/') + 7) AS host,"
            " COUNT(1) FROM response GROUP BY host ORDER BY 2 DESC LIMIT 5").fetchall()
        return {
            "entries": int(count or 0),
            "bytes": int(raw_bytes or 0),
            "oldest": oldest,
            "newest": newest,
            "path": str(self.path),
            "by_host": {host: int(n) for host, n in by_host},
        }

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "ResponseCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
