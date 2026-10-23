"""Validated daily aggregates: the statistical record that outlives the raw rows.

The retention window deletes raw observations.  If that were the whole story,
a past evaluation could never be recomputed, only quoted.  This module builds a
second, much smaller record at **day granularity** that preserves the
statistics an evaluation actually consumes, and proves that record matches the
raw data it came from before anything is deleted.

What this can and cannot do
---------------------------
The grain is one day.  Aggregates reproduce *day-level* statistics exactly --
counts, sums, minima, maxima, and the last observation of the day.  They do
not reproduce intra-day ordering: if an evaluation looked at the sequence of
snapshots within a day, only the day's totals and its closing observation
survive.

Where the price history actually is
-----------------------------------
This was measured, and the first guess was wrong, so it is written down here.

``tsm_realm_price`` looks like a per-snapshot dump -- it has a ``snapshot_id``
and an ``updated_at`` -- but it is not.  It holds **1,193,857 rows for
1,193,857 distinct (scope, item_id) pairs**, enforced by a unique index: one
row per item, upserted in place.  Its ``snapshot_id`` is the last time the row
was *written*, not the last time the price *changed*; a traced item carried
``snapshot_id`` 1177 while its only ``tsm_price_change`` row was snapshot 1.
Its ``updated_at`` is therefore a write timestamp and is not a retention
boundary.  It is current state, it is kept forever, and there is no price
history in it to aggregate.

``tsm_price_change`` is the history.  It is keyed ``(snapshot_id, key_id)`` and
holds one row per item per import, written **only when the price changed** --
verified exactly: per snapshot, its row count equals ``tsm_snapshot.changed``
for every snapshot sampled.  So:

* aggregating it per (day, scope, item) yields the price *observations* of that
  day, and the last of them is the price as of that day;
* an item that did not change on a day has no row that day, which is correct:
  its price was whatever it last was.  :func:`price_as_of` encodes that rule --
  take the most recent observation at or before the day asked for.

That is the reconstruction an evaluation needs, and it is why the aggregate is
named ``daily_item_price`` and is built from the change log rather than from
the current-state table.

Every aggregate partition carries a checksum of the exact source rows it
summarised.  After the raw rows are gone, that checksum is the only remaining
evidence of what was deleted -- so it is computed *before* deletion and stored
beside the aggregate, never derived afterwards.

Provenance
----------
``aggregate_meta`` records the schema version, the build time and the source
databases.  ``aggregate_partition`` records, per (aggregate, day, scope), how
many source rows went in and their checksum.  A partition whose checksum is
unchanged is not rewritten, so re-running a build over unchanged data costs one
read pass and no writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Bumped when the aggregate schema or a definition changes.  A database built
#: under a different version is refused rather than read optimistically.
#:
#: v2: the price aggregate moved from tsm_realm_price to tsm_price_change after
#: measuring that tsm_realm_price is an upserted current-state mirror (one row
#: per item) and holds no price history.  A v1 database has a different table
#: set and must not be read.
AGGREGATE_SCHEMA_VERSION = 2

#: The aggregate database.  Deliberately a separate file: it must survive the
#: deletion of rows from the operational databases, be backupable on its own,
#: and never be a reason to keep the raw tables.
DEFAULT_AGGREGATE_DB = "data/aggregates.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS aggregate_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per (aggregate, day, scope): what went in, and a checksum of exactly those
-- source rows.  This is the evidence that survives the deletion.
CREATE TABLE IF NOT EXISTS aggregate_partition (
    aggregate       TEXT    NOT NULL,
    day             TEXT    NOT NULL,
    scope           TEXT    NOT NULL,
    source_table    TEXT    NOT NULL,
    source_rows     INTEGER NOT NULL,
    source_checksum TEXT    NOT NULL,
    built_at        TEXT    NOT NULL,
    PRIMARY KEY (aggregate, day, scope)
);

-- Price observations per item per day, from tsm_price_change -- the only
-- record of how a price moved.  An item that did not change on a day has no
-- row for that day, and that is correct: its price was whatever it last was.
-- price_as_of() applies that rule when reading.
CREATE TABLE IF NOT EXISTS daily_item_price (
    day                TEXT    NOT NULL,
    scope              TEXT    NOT NULL,
    kind               TEXT    NOT NULL,
    item_id            INTEGER NOT NULL,
    n_observations     INTEGER NOT NULL,
    n_snapshots        INTEGER NOT NULL,
    min_market_value   INTEGER,
    max_market_value   INTEGER,
    sum_market_value   INTEGER,
    min_buyout_min     INTEGER,
    min_buyout_max     INTEGER,
    sum_min_buyout     INTEGER,
    sum_avg_sale_price INTEGER,
    sum_sale_rate      REAL,
    sum_sold_per_day   REAL,
    last_market_value  INTEGER,
    last_min_buyout    INTEGER,
    last_sale_rate     REAL,
    last_sold_per_day  REAL,
    last_snapshot_id   INTEGER,
    PRIMARY KEY (day, scope, kind, item_id)
);

-- Auction observations per item per realm per day, from intel.sqlite.
CREATE TABLE IF NOT EXISTS daily_ah_auction (
    day             TEXT    NOT NULL,
    realm           TEXT    NOT NULL,
    item_id         INTEGER NOT NULL,
    n_auctions      INTEGER NOT NULL,
    n_snapshots     INTEGER NOT NULL,
    n_commodity     INTEGER NOT NULL,
    sum_quantity    INTEGER,
    min_unit_price  INTEGER,
    max_unit_price  INTEGER,
    sum_unit_price  INTEGER,
    sum_total_price INTEGER,
    PRIMARY KEY (day, realm, item_id)
);

-- Event counts per day, from the journal event tables.
CREATE TABLE IF NOT EXISTS daily_event (
    day          TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    realm        TEXT    NOT NULL,
    n_events     INTEGER NOT NULL,
    sum_quantity INTEGER,
    sum_total    INTEGER,
    sum_net      INTEGER,
    PRIMARY KEY (day, source, event_type, realm)
);

CREATE INDEX IF NOT EXISTS daily_item_price_item
    ON daily_item_price (item_id, day);
CREATE INDEX IF NOT EXISTS daily_ah_auction_item
    ON daily_ah_auction (item_id, day);
"""

#: The aggregate tables, in the order they are built.
AGGREGATES: Tuple[str, ...] = (
    "daily_item_price",
    "daily_ah_auction",
    "daily_event",
)

#: Which raw table each aggregate summarises.  A raw table that is not a value
#: here and not in :data:`COVERED_BY_CHILD` has no aggregate: deleting its rows
#: loses that history, and the rehearsal must say so rather than assume.
AGGREGATE_FOR_TABLE: Dict[str, str] = {
    "tsm_price_change": "daily_item_price",
    "ah_auction": "daily_ah_auction",
    "auction_events": "daily_event",
    "auction_intel_events": "daily_event",
}

#: Header tables whose meaning survives through an aggregated child.  A
#: tsm_snapshot row is a timestamp and a checksum; the prices that hang off it
#: are what an evaluation reads, and those are aggregated.
COVERED_BY_CHILD: Dict[str, str] = {
    "tsm_snapshot": "daily_item_price",
    "ah_snapshot": "daily_ah_auction",
}

#: SQL that yields the UTC day of a row of ``t``, used to compare the range
#: being deleted against the range the aggregates cover.
DAY_SQL: Dict[Tuple[str, str], str] = {
    ("tsm_markets.db", "tsm_price_change"):
        "SELECT date(s.updated_at) FROM tsm_snapshot s "
        "WHERE s.snapshot_id = t.snapshot_id",
    ("tsm_markets.db", "tsm_snapshot"): "SELECT date(t.updated_at)",
    ("intel.sqlite", "ah_auction"):
        "SELECT date(s.captured_at, 'unixepoch') FROM ah_snapshot s "
        "WHERE s.snapshot_id = t.snapshot_id",
    ("intel.sqlite", "ah_snapshot"): "SELECT date(t.captured_at, 'unixepoch')",
    ("tsm_markets.db", "auction_events"):
        "SELECT date(t.timestamp_unix, 'unixepoch')",
    ("tsm_markets.db", "auction_intel_events"):
        "SELECT date(t.ts, 'unixepoch')",
}


class SchemaMismatch(RuntimeError):
    """The aggregate database was built by a different version of this code."""


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_postgres() -> bool:
    from woweconomy.dbcompat import backend

    return backend() == "postgres"


def _needs_file(path: Path | str) -> bool:
    """Whether this store is a file the caller should check for.

    With the Postgres backend the intel tables and the aggregate tables live
    in the shared database, so a ``.exists()`` check would wrongly report
    them missing and the build would silently do nothing.  ``tsm_markets.db``
    is TSM's own import and stays a file either way.
    """
    if not _is_postgres():
        return True
    return Path(path).name.lower() not in ("intel.sqlite", "aggregates.sqlite")


def _connect(path: Path | str) -> sqlite3.Connection:
    if _is_postgres():
        from woweconomy.dbcompat import connect as _dbconnect

        con = _dbconnect(None)
        con.isolation_level = None  # the builders manage their own commits
        return con
    con = sqlite3.connect(str(path), timeout=60.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _ro(path: Path | str, *, backend_aware: bool = False) -> sqlite3.Connection:
    if backend_aware and _is_postgres():
        from woweconomy.dbcompat import connect as _dbconnect

        return _dbconnect(None)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60.0)
    con.execute("PRAGMA busy_timeout=60000")
    return con


def open_aggregates(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the aggregate database and check its version."""
    con = _connect(path)
    con.executescript(SCHEMA)
    row = con.execute(
        "SELECT value FROM aggregate_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO aggregate_meta(key, value) VALUES('schema_version', ?)",
            (str(AGGREGATE_SCHEMA_VERSION),),
        )
    elif int(row[0]) != AGGREGATE_SCHEMA_VERSION:
        con.close()
        raise SchemaMismatch(
            f"{path} was built with aggregate schema {row[0]}, this code "
            f"writes {AGGREGATE_SCHEMA_VERSION}; rebuild into a new file"
        )
    return con


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# checksums
# ---------------------------------------------------------------------------

def _feed(h: "hashlib._Hash", values: Iterable[object]) -> None:
    """Hash one source row, with separators that cannot collide.

    0x1f between fields, 0x1e for NULL, 0x1d between rows.  A NULL and the
    string "None" therefore hash differently, which matters because a NULL
    price and a price are not the same observation.
    """
    for v in values:
        h.update(b"\x1f" if v is not None else b"\x1e")
        if v is not None:
            h.update(str(v).encode("utf-8", "replace"))
    h.update(b"\x1d")


# ---------------------------------------------------------------------------
# row streams
# ---------------------------------------------------------------------------
#
# Each stream yields (key_tuple, accumulator, source_row_count, checksum),
# ordered by key_tuple, so a caller can emit a partition the moment the key
# changes and never hold the whole result in memory.  key_tuple is always in
# the order given by KEY_COLUMNS[name].

def _snapshot_days(con: sqlite3.Connection) -> Dict[int, str]:
    """snapshot_id -> UTC day.  One small read instead of a join per row."""
    return {
        int(sid): str(ts)[:10]
        for sid, ts in con.execute(
            "SELECT snapshot_id, updated_at FROM tsm_snapshot "
            "WHERE updated_at IS NOT NULL"
        )
    }


def _stream_item_price(con: sqlite3.Connection) -> Iterable[tuple]:
    """Daily price observations per item, from tsm_price_change.

    This is the only record of how a price moved.  A row exists only when the
    price changed, so a day with no row for an item means the price held: that
    is what :func:`price_as_of` relies on.

    The work is done **one day at a time**.  A single ``ORDER BY date(...)``
    over the whole table makes SQLite sort eleven million rows through a temp
    b-tree, which measured at well over a quarter of an hour.  Grouping the
    snapshot ids by day first keeps each sort to one day's rows, and the result
    is still emitted in an order where every (day, scope) partition is
    contiguous, which is what :func:`build` needs.
    """
    days = _snapshot_days(con)
    by_day: Dict[str, List[int]] = {}
    for sid, day in days.items():
        by_day.setdefault(day, []).append(sid)

    for day in sorted(by_day):
        ids = sorted(by_day[day])
        marks = ",".join("?" * len(ids))
        sql = f"""
            SELECT p.scope, p.kind, p.key_id,
                   p.market_value, p.min_buyout, p.avg_sale_price,
                   p.sale_rate, p.sold_per_day, p.snapshot_id
              FROM tsm_price_change p
             WHERE p.snapshot_id IN ({marks})
             ORDER BY p.scope, p.kind, p.key_id, p.snapshot_id
        """
        cur = con.execute(sql, tuple(ids))
        key = None
        acc: Optional[Dict[str, object]] = None
        h: Optional["hashlib._Hash"] = None
        n = 0
        snapshots: set = set()
        while True:
            batch = cur.fetchmany(50_000)
            if not batch:
                break
            for (scope, kind, item_id, mv, mb, asp, sale_rate, sold_per_day,
                 snapshot_id) in batch:
                k = (day, scope, kind, item_id)
                if k != key:
                    if key is not None:
                        acc["n_snapshots"] = len(snapshots)  # type: ignore[index]
                        yield key, acc, n, h.hexdigest()  # type: ignore[union-attr]
                    key = k
                    acc = {
                        "n_observations": 0, "n_snapshots": 0,
                        "min_market_value": None, "max_market_value": None,
                        "sum_market_value": None,
                        "min_buyout_min": None, "min_buyout_max": None,
                        "sum_min_buyout": None,
                        "sum_avg_sale_price": None,
                        "sum_sale_rate": 0.0, "sum_sold_per_day": 0.0,
                        "last_market_value": None, "last_min_buyout": None,
                        "last_sale_rate": None, "last_sold_per_day": None,
                        "last_snapshot_id": None,
                    }
                    h = hashlib.sha256()
                    n = 0
                    snapshots = set()
                _feed(h, (day, scope, kind, item_id, mv, mb, asp, sale_rate,
                          sold_per_day, snapshot_id))  # type: ignore[arg-type]
                n += 1
                acc["n_observations"] += 1
                snapshots.add(snapshot_id)
                if mv is not None:
                    acc["min_market_value"] = _min(acc["min_market_value"], mv)
                    acc["max_market_value"] = _max(acc["max_market_value"], mv)
                    acc["sum_market_value"] = (acc["sum_market_value"] or 0) + mv
                if mb is not None:
                    acc["min_buyout_min"] = _min(acc["min_buyout_min"], mb)
                    acc["min_buyout_max"] = _max(acc["min_buyout_max"], mb)
                    acc["sum_min_buyout"] = (acc["sum_min_buyout"] or 0) + mb
                if asp is not None:
                    acc["sum_avg_sale_price"] = (
                        acc["sum_avg_sale_price"] or 0) + asp
                if sale_rate is not None:
                    acc["sum_sale_rate"] = float(
                        acc["sum_sale_rate"]) + float(sale_rate)
                    acc["last_sale_rate"] = sale_rate
                if sold_per_day is not None:
                    acc["sum_sold_per_day"] = float(
                        acc["sum_sold_per_day"]) + float(sold_per_day)
                    acc["last_sold_per_day"] = sold_per_day
                # ordered by snapshot_id within the item, so the last row seen
                # is the day's closing observation
                acc["last_market_value"] = mv
                acc["last_min_buyout"] = mb
                acc["last_snapshot_id"] = snapshot_id
        if key is not None:
            acc["n_snapshots"] = len(snapshots)  # type: ignore[index]
            yield key, acc, n, h.hexdigest()  # type: ignore[union-attr]


def _stream_ah_auction(con: sqlite3.Connection) -> Iterable[tuple]:
    """Daily auction observations per item per realm from intel.sqlite."""
    sql = """
        SELECT date(s.captured_at, 'unixepoch') AS day,
               s.realm                          AS realm,
               a.item_id                        AS item_id,
               a.quantity, a.unit_price_copper, a.total_price_copper,
               a.is_commodity, a.snapshot_id
          FROM ah_auction  a
          JOIN ah_snapshot s ON s.snapshot_id = a.snapshot_id
         WHERE s.captured_at IS NOT NULL
         ORDER BY day, realm, item_id, s.captured_at, a.auction_key
    """
    cur = con.execute(sql)
    key = None
    acc: Optional[Dict[str, object]] = None
    h: Optional["hashlib._Hash"] = None
    n = 0
    snapshots: set = set()
    while True:
        batch = cur.fetchmany(50_000)
        if not batch:
            break
        for (day, realm, item_id, qty, unit, total,
             is_commodity, snapshot_id) in batch:
            if day is None:
                continue
            k = (day, realm, item_id)
            if k != key:
                if key is not None:
                    acc["n_snapshots"] = len(snapshots)  # type: ignore[index]
                    yield key, acc, n, h.hexdigest()  # type: ignore[union-attr]
                key = k
                acc = {
                    "n_auctions": 0, "n_snapshots": 0, "n_commodity": 0,
                    "sum_quantity": None, "min_unit_price": None,
                    "max_unit_price": None, "sum_unit_price": None,
                    "sum_total_price": None,
                }
                h = hashlib.sha256()
                n = 0
                snapshots = set()
            _feed(h, (day, realm, item_id, qty, unit, total,
                      is_commodity, snapshot_id))  # type: ignore[arg-type]
            n += 1
            acc["n_auctions"] += 1
            snapshots.add(snapshot_id)
            if is_commodity:
                acc["n_commodity"] += 1
            if qty is not None:
                acc["sum_quantity"] = (acc["sum_quantity"] or 0) + qty
            if unit is not None:
                acc["min_unit_price"] = _min(acc["min_unit_price"], unit)
                acc["max_unit_price"] = _max(acc["max_unit_price"], unit)
                acc["sum_unit_price"] = (acc["sum_unit_price"] or 0) + unit
            if total is not None:
                acc["sum_total_price"] = (acc["sum_total_price"] or 0) + total
    if key is not None:
        acc["n_snapshots"] = len(snapshots)  # type: ignore[index]
        yield key, acc, n, h.hexdigest()  # type: ignore[union-attr]


def _stream_events(con: sqlite3.Connection) -> Iterable[tuple]:
    """Daily event counts from both journal event tables."""
    parts = []
    if _has_table(con, "auction_events"):
        parts.append("""
            SELECT date(COALESCE(timestamp_unix,
                                 CAST(strftime('%s', ts) AS INTEGER)),
                        'unixepoch')         AS day,
                   'auction_events'          AS source,
                   COALESCE(event_type, '?') AS event_type,
                   COALESCE(realm, '?')      AS realm,
                   qty, total_price_copper, net_proceeds_copper,
                   event_id
              FROM auction_events
        """)
    if _has_table(con, "auction_intel_events"):
        parts.append("""
            SELECT date(ts, 'unixepoch')     AS day,
                   'auction_intel_events'    AS source,
                   COALESCE(event_type, '?') AS event_type,
                   COALESCE(realm, '?')      AS realm,
                   quantity, total_price_copper, net_proceeds_copper,
                   event_id
              FROM auction_intel_events
        """)
    if not parts:
        return
    sql = " UNION ALL ".join(parts)
    cur = con.execute(
        f"SELECT * FROM ({sql}) ORDER BY day, source, event_type, realm, event_id"
    )
    key = None
    acc: Optional[Dict[str, object]] = None
    h: Optional["hashlib._Hash"] = None
    n = 0
    while True:
        batch = cur.fetchmany(50_000)
        if not batch:
            break
        for day, source, event_type, realm, qty, total, net, event_id in batch:
            if day is None:
                continue
            k = (day, source, event_type, realm)
            if k != key:
                if key is not None:
                    yield key, acc, n, h.hexdigest()  # type: ignore[union-attr]
                key = k
                acc = {"n_events": 0, "sum_quantity": None,
                       "sum_total": None, "sum_net": None}
                h = hashlib.sha256()
                n = 0
            _feed(h, (day, source, event_type, realm, qty, total, net, event_id))  # type: ignore[arg-type]
            n += 1
            acc["n_events"] += 1
            if qty is not None:
                acc["sum_quantity"] = (acc["sum_quantity"] or 0) + qty
            if total is not None:
                acc["sum_total"] = (acc["sum_total"] or 0) + total
            if net is not None:
                acc["sum_net"] = (acc["sum_net"] or 0) + net
    if key is not None:
        yield key, acc, n, h.hexdigest()  # type: ignore[union-attr]


def _min(a: object, b: object) -> object:
    return b if a is None else min(a, b)  # type: ignore[type-var]


def _max(a: object, b: object) -> object:
    return b if a is None else max(a, b)  # type: ignore[type-var]


# ---------------------------------------------------------------------------
# the specification of each aggregate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AggregateSpec:
    """One aggregate: where it comes from, how it is keyed, what it holds."""

    name: str
    database: str
    source_table: str
    stream: object
    key_columns: Tuple[str, ...]
    value_columns: Tuple[str, ...]

    @property
    def columns(self) -> Tuple[str, ...]:
        return self.key_columns + self.value_columns

    @property
    def insert_sql(self) -> str:
        cols = self.columns
        return (f'INSERT OR REPLACE INTO "{self.name}" ({",".join(cols)}) '
                f'VALUES ({",".join("?" * len(cols))})')

    @property
    def select_sql(self) -> str:
        where = " AND ".join(f"{c}=?" for c in self.key_columns)
        return f'SELECT {",".join(self.columns)} FROM "{self.name}" WHERE {where}'


SPECS: Dict[str, AggregateSpec] = {
    "daily_item_price": AggregateSpec(
        "daily_item_price", "tsm_markets.db", "tsm_price_change",
        _stream_item_price,
        ("day", "scope", "kind", "item_id"),
        ("n_observations", "n_snapshots",
         "min_market_value", "max_market_value", "sum_market_value",
         "min_buyout_min", "min_buyout_max", "sum_min_buyout",
         "sum_avg_sale_price", "sum_sale_rate", "sum_sold_per_day",
         "last_market_value", "last_min_buyout", "last_sale_rate",
         "last_sold_per_day", "last_snapshot_id"),
    ),
    "daily_ah_auction": AggregateSpec(
        "daily_ah_auction", "intel.sqlite", "ah_auction",
        _stream_ah_auction,
        ("day", "realm", "item_id"),
        ("n_auctions", "n_snapshots", "n_commodity",
         "sum_quantity", "min_unit_price", "max_unit_price",
         "sum_unit_price", "sum_total_price"),
    ),
    "daily_event": AggregateSpec(
        "daily_event", "tsm_markets.db", "auction_events",
        _stream_events,
        ("day", "source", "event_type", "realm"),
        ("n_events", "sum_quantity", "sum_total", "sum_net"),
    ),
}


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

@dataclass
class BuildReport:
    """What a build did, per aggregate."""

    rows_written: Dict[str, int] = field(default_factory=dict)
    partitions_skipped: Dict[str, int] = field(default_factory=dict)
    source_rows: Dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "rows_written": self.rows_written,
            "partitions_skipped": self.partitions_skipped,
            "source_rows": self.source_rows,
            "elapsed_s": round(self.elapsed_s, 2),
            "errors": self.errors,
        }

    def summary(self) -> str:
        lines = [f"aggregates built in {self.elapsed_s:.1f}s"]
        for name in AGGREGATES:
            if name not in self.rows_written:
                continue
            lines.append(
                f"  {name:22s} {self.rows_written[name]:>9,} rows written  "
                f"from {self.source_rows.get(name, 0):>12,} source rows  "
                f"({self.partitions_skipped.get(name, 0)} partitions unchanged)"
            )
        for err in self.errors:
            lines.append(f"  ERROR {err}")
        return "\n".join(lines)


def build(intel_db: Path | str, tsm_db: Path | str,
          aggregate_db: Path | str,
          only: Optional[Sequence[str]] = None,
          force: bool = False) -> BuildReport:
    """Build the daily aggregates from the raw databases.

    Idempotent and incremental: a partition whose source checksum is unchanged
    is not rewritten, so an unchanged day costs a read pass and no writes.
    """
    started = time.perf_counter()
    report = BuildReport()
    out = open_aggregates(aggregate_db)
    sources = {"tsm_markets.db": Path(tsm_db), "intel.sqlite": Path(intel_db)}
    conns: Dict[str, sqlite3.Connection] = {}
    try:
        for name in (only or AGGREGATES):
            spec = SPECS[name]
            path = sources[spec.database]
            if _needs_file(path) and not path.exists():
                report.errors.append(f"{name}: missing {path}")
                continue
            if spec.database not in conns:
                conns[spec.database] = _ro(
                        path, backend_aware=spec.database == "intel.sqlite")
            src = conns[spec.database]
            if not _has_table(src, spec.source_table):
                report.errors.append(f"{name}: {path} has no {spec.source_table}")
                continue

            written = skipped = source_rows = 0
            pending: List[tuple] = []
            # A partition is (day, scope) and holds every item of that scope on
            # that day.  It is the unit of rebuild: one item changing rewrites
            # the partition, because the checksum covers the whole partition.
            # The stream is ordered by key, so a partition's rows are
            # contiguous and can be buffered until the partition ends.
            part_key: Optional[Tuple[str, str]] = None
            part_h = hashlib.sha256()
            part_rows = 0

            def flush_partition(key: Optional[Tuple[str, str]], rows: List[tuple],
                                digest: str, n_rows: int) -> None:
                nonlocal written, skipped
                if key is None or not rows:
                    return
                partition = (name, key[0], key[1])
                if not force:
                    prev = out.execute(
                        "SELECT source_checksum FROM aggregate_partition "
                        "WHERE aggregate=? AND day=? AND scope=?",
                        partition,
                    ).fetchone()
                    if prev is not None and prev[0] == digest:
                        skipped += 1
                        return
                out.executemany(spec.insert_sql, rows)
                out.execute(
                    "INSERT OR REPLACE INTO aggregate_partition "
                    "(aggregate, day, scope, source_table, source_rows, "
                    " source_checksum, built_at) VALUES (?,?,?,?,?,?,?)",
                    partition + (spec.source_table, n_rows, digest, _utc_now()),
                )
                written += len(rows)

            for key, acc, n_source, checksum in spec.stream(src):  # type: ignore[operator]
                source_rows += n_source
                pk = (str(key[0]), str(key[1]))
                if pk != part_key:
                    flush_partition(part_key, pending, part_h.hexdigest(),
                                    part_rows)
                    part_key = pk
                    pending = []
                    part_h = hashlib.sha256()
                    part_rows = 0
                part_h.update(checksum.encode("ascii"))
                part_rows += n_source
                pending.append(tuple(key)
                               + tuple(acc.get(c) for c in spec.value_columns))
            flush_partition(part_key, pending, part_h.hexdigest(), part_rows)

            report.rows_written[name] = written
            report.partitions_skipped[name] = skipped
            report.source_rows[name] = source_rows

        for key, value in (
            ("built_at", _utc_now()),
            ("source_intel_db", str(Path(intel_db))),
            ("source_tsm_db", str(Path(tsm_db))),
        ):
            out.execute(
                "INSERT OR REPLACE INTO aggregate_meta(key, value) VALUES(?,?)",
                (key, value),
            )
        covered = out.execute(
            "SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM daily_item_price"
        ).fetchone()
        out.execute(
            "INSERT OR REPLACE INTO aggregate_meta(key, value) VALUES(?,?)",
            ("covered", json.dumps({"first_day": covered[0],
                                    "last_day": covered[1],
                                    "days": covered[2]})),
        )
    finally:
        for con in conns.values():
            con.close()
        out.close()
    report.elapsed_s = time.perf_counter() - started
    return report


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

@dataclass
class Mismatch:
    aggregate: str
    key: str
    column: str
    stored: object
    recomputed: object

    def __str__(self) -> str:
        return (f"{self.aggregate} {self.key}: {self.column} "
                f"stored={self.stored!r} recomputed={self.recomputed!r}")


@dataclass
class ValidationReport:
    """The result of rebuilding a sample from raw and comparing it."""

    days_checked: List[str] = field(default_factory=list)
    rows_compared: Dict[str, int] = field(default_factory=dict)
    missing: Dict[str, int] = field(default_factory=dict)
    mismatches: List[Mismatch] = field(default_factory=list)
    elapsed_s: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (not self.mismatches and not self.errors
                and not any(self.missing.values()))

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "days_checked": self.days_checked,
            "rows_compared": self.rows_compared,
            "missing": self.missing,
            "mismatch_count": len(self.mismatches),
            "mismatches": [
                {"aggregate": m.aggregate, "key": m.key, "column": m.column,
                 "stored": m.stored, "recomputed": m.recomputed}
                for m in self.mismatches[:200]
            ],
            "elapsed_s": round(self.elapsed_s, 2),
            "errors": self.errors,
        }

    def summary(self) -> str:
        lines = [f"aggregate validation: {'PASS' if self.ok else 'FAIL'}",
                 f"  days checked : {', '.join(self.days_checked) or '-'}"]
        for name in AGGREGATES:
            if name in self.rows_compared:
                lines.append(
                    f"  {name:22s} {self.rows_compared[name]:>9,} rows compared, "
                    f"{self.missing.get(name, 0)} missing"
                )
        for m in self.mismatches[:20]:
            lines.append(f"  MISMATCH {m}")
        if len(self.mismatches) > 20:
            lines.append(f"  ... and {len(self.mismatches) - 20} more")
        for err in self.errors:
            lines.append(f"  ERROR {err}")
        return "\n".join(lines)


def validate(aggregate_db: Path | str, intel_db: Path | str,
             tsm_db: Path | str,
             days: Optional[Sequence[str]] = None,
             limit_days: int = 3) -> ValidationReport:
    """Rebuild a sample of days from raw and compare against what is stored.

    This is a drift and corruption check, not a proof of correctness: it uses
    the same stream functions, so a wrong definition would be wrong twice and
    agree with itself.  Correctness of the definitions is established
    separately, in ``tests/test_aggregates.py``, against hand-computed
    fixtures.  What this catches is the thing a test cannot: a partial build, a
    build against a different raw state, or a tampered aggregate file.
    """
    started = time.perf_counter()
    report = ValidationReport()
    if _needs_file(aggregate_db) and not Path(aggregate_db).exists():
        report.errors.append(f"missing {aggregate_db}")
        report.elapsed_s = time.perf_counter() - started
        return report

    con = _ro(aggregate_db, backend_aware=True)
    try:
        available = [r[0] for r in con.execute(
            "SELECT DISTINCT day FROM daily_item_price ORDER BY day DESC"
        ).fetchall()]
        if not available:
            report.errors.append("no aggregates to validate")
            return report
        chosen = list(days) if days else available[:limit_days]
        report.days_checked = chosen
        wanted = set(chosen)

        sources = {"tsm_markets.db": Path(tsm_db), "intel.sqlite": Path(intel_db)}
        conns: Dict[str, sqlite3.Connection] = {}
        try:
            for name in AGGREGATES:
                spec = SPECS[name]
                path = sources[spec.database]
                if _needs_file(path) and not path.exists():
                    report.errors.append(f"{name}: missing {path}")
                    continue
                if spec.database not in conns:
                    conns[spec.database] = _ro(
                        path, backend_aware=spec.database == "intel.sqlite")
                src = conns[spec.database]
                if not _has_table(src, spec.source_table):
                    report.errors.append(f"{name}: no {spec.source_table}")
                    continue
                compared = missing = 0
                for key, acc, _n, _checksum in spec.stream(src):  # type: ignore[operator]
                    if key[0] not in wanted:
                        continue
                    row = con.execute(spec.select_sql, tuple(key)).fetchone()
                    if row is None:
                        missing += 1
                        continue
                    stored = dict(zip(spec.columns, row))
                    for column in spec.value_columns:
                        if _differs(stored[column], acc.get(column)):
                            report.mismatches.append(Mismatch(
                                name, "/".join(str(k) for k in key),
                                column, stored[column], acc.get(column)))
                    compared += 1
                report.rows_compared[name] = compared
                report.missing[name] = missing
        finally:
            for c in conns.values():
                c.close()
    finally:
        con.close()
    report.elapsed_s = time.perf_counter() - started
    return report


def _differs(a: object, b: object) -> bool:
    """Exact comparison, with a tolerance only for accumulated floats."""
    if a is None and b is None:
        return False
    if a is None or b is None:
        # a sum that stayed at 0.0 and a stored NULL are the same fact: no
        # non-null observation contributed
        other = b if a is None else a
        return not (other == 0 or other == 0.0)
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) > 1e-6 * max(1.0, abs(float(a)))
    return a != b


# ---------------------------------------------------------------------------
# reading the aggregates back
# ---------------------------------------------------------------------------

def day_price(aggregate_db: Path | str, day: str, scope: str, kind: str,
              item_id: int) -> Optional[Dict[str, object]]:
    """That day's price observations for one item, from the aggregate alone.

    Returns None when the item did not change that day.  That is not missing
    data -- it means the price held -- and :func:`price_as_of` is the function
    that turns a series of such days into a level.
    """
    con = _ro(aggregate_db, backend_aware=True)
    try:
        row = con.execute(
            "SELECT n_observations, n_snapshots, min_market_value, "
            "       max_market_value, sum_market_value, min_buyout_min, "
            "       min_buyout_max, sum_min_buyout, last_market_value, "
            "       last_min_buyout, last_sale_rate, last_sold_per_day "
            "  FROM daily_item_price "
            " WHERE day=? AND scope=? AND kind=? AND item_id=?",
            (day, scope, kind, int(item_id)),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    (n_obs, n_snap, mv_min, mv_max, mv_sum, mb_min, mb_max, mb_sum,
     last_mv, last_mb, last_sr, last_spd) = row
    return {
        "day": day, "scope": scope, "kind": kind, "item_id": int(item_id),
        "n_observations": n_obs, "n_snapshots": n_snap,
        "market_value": {
            "min": mv_min, "max": mv_max,
            "mean": (mv_sum / n_obs) if (mv_sum is not None and n_obs) else None,
            "last": last_mv,
        },
        "min_buyout": {
            "min": mb_min, "max": mb_max,
            "mean": (mb_sum / n_obs) if (mb_sum is not None and n_obs) else None,
            "last": last_mb,
        },
        "sale_rate_last": last_sr,
        "sold_per_day_last": last_spd,
    }


def price_as_of(aggregate_db: Path | str, day: str, scope: str, kind: str,
                item_id: int) -> Optional[Dict[str, object]]:
    """The item's price as of the end of ``day``, reconstructed from aggregates.

    ``tsm_price_change`` records a row only when a price moved, so the price on
    a day with no row is the most recent earlier observation.  This function
    applies that rule: take the latest observation at or before the day asked
    for.  It is the reconstruction an evaluation needs, and it is the reason
    the aggregates are worth building before the raw rows go.

    Returns None only when the item has no observation at or before that day.
    """
    con = _ro(aggregate_db, backend_aware=True)
    try:
        row = con.execute(
            "SELECT day, last_market_value, last_min_buyout, last_sale_rate, "
            "       last_sold_per_day, n_observations "
            "  FROM daily_item_price "
            " WHERE scope=? AND kind=? AND item_id=? AND day<=? "
            " ORDER BY day DESC LIMIT 1",
            (scope, kind, int(item_id), day),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {
        "as_of": day, "observed_on": row[0], "scope": scope, "kind": kind,
        "item_id": int(item_id),
        "market_value": row[1], "min_buyout": row[2],
        "sale_rate": row[3], "sold_per_day": row[4],
        "observations_that_day": row[5],
    }


def coverage(aggregate_db: Path | str) -> Dict[str, object]:
    """What the aggregate database currently covers."""
    path = Path(aggregate_db)
    if _needs_file(path) and not path.exists():
        return {"exists": False}
    con = _ro(path, backend_aware=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM aggregate_meta").fetchall())
        out: Dict[str, object] = {
            "exists": True,
            "path": str(path),
            "bytes": path.stat().st_size,
            "schema_version": int(meta.get("schema_version", 0)),
            "built_at": meta.get("built_at"),
            "aggregates": {},
        }
        for name in AGGREGATES:
            if not _has_table(con, name):
                continue
            first, last, days = con.execute(
                f'SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM "{name}"'
            ).fetchone()
            rows = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out["aggregates"][name] = {
                "rows": rows, "first_day": first, "last_day": last, "days": days,
            }
        return out
    finally:
        con.close()


def render_coverage(aggregate_db: Path | str) -> str:
    info = coverage(aggregate_db)
    if not info.get("exists"):
        return f"no aggregate database at {aggregate_db}"
    lines = [
        f"aggregates: {info['path']} ({info['bytes'] / 1e6:.1f} MB, "  # type: ignore[operator]
        f"schema {info['schema_version']}, built {info['built_at']})",
        f"  {'aggregate':24s} {'rows':>10s} {'days':>6s}  range",
    ]
    for name, a in info["aggregates"].items():  # type: ignore[union-attr]
        lines.append(
            f"  {name:24s} {a['rows']:>10,} {a['days']:>6}  "
            f"{a['first_day']} .. {a['last_day']}"
        )
    return "\n".join(lines)
