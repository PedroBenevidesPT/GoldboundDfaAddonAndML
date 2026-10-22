"""Bounded retention for the operational databases.

The operational stores grow without limit: ``data/intel.sqlite`` records every
auction in every scan and ``tsm_markets.db`` records every price change.  At
the measured ingest rate that is more than a gigabyte a day, so a rolling
window is a requirement, not a preference.

This module deletes raw observations older than a configurable window
(30 days by default) and nothing else.  The rules it follows:

* **The policy is declared, not inferred.**  Every table in both databases
  appears once in :data:`POLICY`, including the ones that never expire.  A
  table with no entry fails the test suite, so a new table cannot be silently
  ignored or silently deleted.
* **Only raw observations and events expire.**  Reference data, current-state
  mirrors and model provenance are kept regardless of age.
* **Expiry is driven by the parent snapshot**, so a child row can never
  outlive the record that gives it meaning.  An auction expires when the scan
  that captured it expires.
* **The newest snapshot per realm, character, scope or kind is never
  deleted**, even when older than the window: it is the current state, and
  without it the system cannot answer "what is on the auction house now".
* **In-flight records are never deleted.**  An invoice still in the mailbox
  (``days_left > 0``) or a posting whose auction has not ended
  (``time_left > 0``) is protected, because its outcome is not yet known.
* **Nothing is deleted unless asked.**  ``plan()`` only counts; ``apply()``
  deletes.  ``apply()`` refuses to drop data that a past evaluation could no
  longer be recomputed from unless an archive directory is given.

Time is handled in UTC everywhere.  Stored timestamps are a mixture of epoch
integers and ISO-8601 text with either ``Z`` or ``+00:00``; the cutoff is
compared as an integer epoch, so no timezone can shift the boundary.  A
timestamp that cannot be parsed compares as NULL, which is false, so it is
never treated as expired: the failure mode is to keep a row, not to delete a
live one.
"""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Default rolling window.  The operational stores keep at most this much raw
#: history.  See ``docs/retention.md`` for the measurements behind the choice.
RETENTION_DAYS = 30

#: Rows deleted per transaction.  A single transaction over millions of rows
#: would build a multi-gigabyte write-ahead log, and the watcher writes to
#: these databases while cleanup runs.
BATCH_ROWS = 20_000

#: category meanings
RAW = "raw"                  # observation or event; expires
HISTORY = "history"          # derived history keyed by a parent snapshot; expires
DIMENSION = "dimension"      # reference data; never expires
CURRENT = "current"          # current-state mirror; never expires


@dataclass(frozen=True)
class SourcePolicy:
    """What happens to one table, stated once and checked by the tests."""

    table: str
    database: str
    category: str
    #: the authoritative timestamp column, or the parent that carries it
    timestamp: str
    #: how a row is judged expired, in words a human can check
    expires_when: str = ""
    #: what is kept even when expired
    protection: str = ""
    #: measured with ``scripts/audit_storage.py --measure``; includes indexes
    bytes_per_row: float = 0.0
    #: True when the rows are needed to recompute a past evaluation
    needed_for_replay: bool = False
    rationale: str = ""

    @property
    def expires(self) -> bool:
        return self.category in (RAW, HISTORY)


POLICY: Tuple[SourcePolicy, ...] = (
    # -- data/intel.sqlite ------------------------------------------------
    SourcePolicy(
        "ah_snapshot", "intel.sqlite", RAW, "captured_at",
        expires_when="captured_at < cutoff",
        protection="the newest scan of each realm is always kept",
        bytes_per_row=120.0, needed_for_replay=True,
        rationale="the scan header; an auction is only meaningful with it",
    ),
    SourcePolicy(
        "ah_auction", "intel.sqlite", HISTORY, "ah_snapshot.captured_at",
        expires_when="its scan is expired",
        protection="rows of a kept scan are kept",
        bytes_per_row=254.8, needed_for_replay=True,
        rationale="the bulk of the database: every auction in every scan",
    ),
    SourcePolicy(
        "inventory_snapshot", "intel.sqlite", RAW, "captured_at",
        expires_when="captured_at < cutoff",
        protection="the newest snapshot of each character is always kept",
        bytes_per_row=400.0,
        rationale="bag and warband contents at a moment",
    ),
    SourcePolicy(
        "inventory_row", "intel.sqlite", HISTORY, "inventory_snapshot.captured_at",
        expires_when="its snapshot is expired",
        protection="rows of a kept snapshot are kept",
        bytes_per_row=80.0,
        rationale="one item stack inside an inventory snapshot",
    ),
    SourcePolicy(
        "inventory_posted_listing", "intel.sqlite", HISTORY,
        "inventory_snapshot.captured_at",
        expires_when="its snapshot is expired",
        protection="listings with time_left > 0 are kept: the auction is live",
        bytes_per_row=90.0,
        rationale="the postings a snapshot saw on the auction house",
    ),
    SourcePolicy(
        "reconciliation", "intel.sqlite", HISTORY, "inventory_snapshot.captured_at",
        expires_when="its snapshot is expired",
        protection="rows of a kept snapshot are kept",
        bytes_per_row=140.0,
        rationale="stock deltas between two inventory snapshots",
    ),
    SourcePolicy(
        "seller_observation", "intel.sqlite", RAW, "observed_at",
        expires_when="observed_at < cutoff",
        bytes_per_row=320.0, needed_for_replay=True,
        rationale="who was selling what; feeds seller analysis",
    ),
    SourcePolicy(
        "undercut_observation", "intel.sqlite", RAW, "observed_at",
        expires_when="observed_at < cutoff",
        bytes_per_row=700.0, needed_for_replay=True,
        rationale="undercut events recorded from the addon",
    ),
    SourcePolicy(
        "mail_invoice", "intel.sqlite", RAW, "seen_at",
        expires_when="seen_at < cutoff",
        protection="days_left > 0 is kept: the mail is still in the mailbox",
        bytes_per_row=500.0, needed_for_replay=True,
        rationale="the only record of a sale; an open invoice is an open "
                  "transaction",
    ),
    SourcePolicy(
        "sale_buyer_link", "intel.sqlite", RAW, "linked_at",
        expires_when="linked_at < cutoff",
        protection="kept while the invoice it points at still exists",
        bytes_per_row=120.0,
        rationale="invoice to buyer attribution",
    ),
    SourcePolicy("ah_realm", "intel.sqlite", DIMENSION, "refreshed_at",
                 rationale="realm identity and connected-realm groups"),
    SourcePolicy("import_log", "intel.sqlite", DIMENSION, "",
                 rationale="import bookkeeping"),
    SourcePolicy("realm_model_run", "intel.sqlite", DIMENSION, "created_at",
                 rationale="model provenance; small and auditable"),
    SourcePolicy("mob_observation", "intel.sqlite", DIMENSION, "",
                 rationale="empty today; not an auction observation"),
    SourcePolicy("mob_loot_observation", "intel.sqlite", DIMENSION, "",
                 rationale="empty today"),
    SourcePolicy("mob_valuation", "intel.sqlite", DIMENSION, "",
                 rationale="empty today"),

    # -- tsm_markets.db ---------------------------------------------------
    SourcePolicy(
        "tsm_snapshot", "tsm_markets.db", RAW, "updated_at",
        expires_when="updated_at < cutoff",
        protection="the newest snapshot of each scope and kind is always kept",
        bytes_per_row=300.0, needed_for_replay=True,
        rationale="a TSM import header; price changes hang off it",
    ),
    SourcePolicy(
        "tsm_price_change", "tsm_markets.db", HISTORY, "tsm_snapshot.updated_at",
        expires_when="its snapshot is expired",
        protection="rows of a kept snapshot are kept",
        bytes_per_row=99.1, needed_for_replay=True,
        rationale="the second largest table: every changed price per import",
    ),
    SourcePolicy(
        "tsm_realm_price", "tsm_markets.db", HISTORY, "tsm_snapshot.updated_at",
        expires_when="its snapshot is expired",
        protection="rows of a kept snapshot are kept, so names stay resolvable",
        bytes_per_row=136.3, needed_for_replay=True,
        rationale="per-realm item rows, including the names the ML path reads",
    ),
    SourcePolicy("tsm_region_price", "tsm_markets.db", CURRENT, "updated_at",
                 rationale="single current region state (one snapshot only)"),
    SourcePolicy("tsm_commodity_price", "tsm_markets.db", CURRENT, "updated_at",
                 rationale="current commodity prices"),
    SourcePolicy("tsm_realm_pet_price", "tsm_markets.db", CURRENT, "updated_at",
                 rationale="current pet prices per realm"),
    SourcePolicy("tsm_region_pet_price", "tsm_markets.db", CURRENT, "updated_at",
                 rationale="current pet prices per region"),
    SourcePolicy(
        "market_snapshots", "tsm_markets.db", HISTORY,
        "snapshots.snapshotTimestamp",
        expires_when="its legacy snapshot is expired",
        protection="rows of a kept legacy snapshot are kept",
        bytes_per_row=76.0, needed_for_replay=True,
        rationale="legacy whole-region imports from July and October",
    ),
    SourcePolicy(
        "snapshots", "tsm_markets.db", RAW, "snapshotTimestamp",
        expires_when="snapshotTimestamp < cutoff",
        bytes_per_row=200.0,
        rationale="headers of the legacy imports",
    ),
    SourcePolicy(
        "auction_events", "tsm_markets.db", RAW, "ts",
        expires_when="ts < cutoff",
        bytes_per_row=900.0, needed_for_replay=True,
        rationale="journal sale and expiry events",
    ),
    SourcePolicy(
        "auction_intel_events", "tsm_markets.db", RAW, "ts",
        expires_when="ts < cutoff",
        bytes_per_row=700.0, needed_for_replay=True,
        rationale="addon-recorded auction events",
    ),
    SourcePolicy("journal_stats", "tsm_markets.db", DIMENSION, "imported_at",
                 rationale="aggregated per-item journal counters, not events"),
    SourcePolicy("tsm_market", "tsm_markets.db", DIMENSION, "",
                 rationale="item market metadata"),
    SourcePolicy("tsm_region", "tsm_markets.db", DIMENSION, "",
                 rationale="region metadata"),
    SourcePolicy("tsm_realm_group", "tsm_markets.db", DIMENSION, "",
                 rationale="realm to connected-group mapping"),
    SourcePolicy("tsm_meta", "tsm_markets.db", DIMENSION, "",
                 rationale="import metadata"),
    SourcePolicy("items", "tsm_markets.db", DIMENSION, "createdAt",
                 rationale="item catalogue"),
    SourcePolicy("item_analytics", "tsm_markets.db", DIMENSION, "",
                 rationale="derived analytics, not raw observations"),
    SourcePolicy("db2_items", "tsm_markets.db", DIMENSION, "",
                 rationale="Blizzard DB2 reference data"),
    SourcePolicy("db2_item_appearance", "tsm_markets.db", DIMENSION, "",
                 rationale="Blizzard DB2 reference data"),
    SourcePolicy("db2_meta", "tsm_markets.db", DIMENSION, "",
                 rationale="Blizzard DB2 import metadata"),
    SourcePolicy("blizzard_items", "tsm_markets.db", DIMENSION, "fetchedAt",
                 rationale="Blizzard item API cache"),
    SourcePolicy("blizzard_missing", "tsm_markets.db", DIMENSION, "failedAt",
                 rationale="items the API could not resolve"),
    SourcePolicy("journal", "tsm_markets.db", DIMENSION, "",
                 rationale="empty today"),
    SourcePolicy("sqlite_sequence", "tsm_markets.db", DIMENSION, "",
                 rationale="SQLite internal"),
)


class PolicyRefusal(RuntimeError):
    """Raised when ``apply()`` would destroy data that cannot be replayed."""


def policy_for(table: str, database: str) -> Optional[SourcePolicy]:
    for p in POLICY:
        if p.table == table and p.database == database:
            return p
    return None


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f}{unit}"
        n /= 1024.0
    return f"{n:.2f}TB"


def epoch_expr(con: sqlite3.Connection, table: str, column: str) -> str:
    """The SQL that yields ``column`` as an integer epoch, whatever it stores.

    The databases mix epoch integers (``ah_snapshot.captured_at``) with
    ISO-8601 text (``tsm_snapshot.updated_at``).  Reading the declared type
    keeps the two apart without hardcoding it per call site.
    """
    declared = {
        row[1]: (row[2] or "").upper()
        for row in con.execute(f'PRAGMA table_info("{table}")')
    }.get(column, "")
    quoted = f'"{column}"'
    if "INT" in declared or "REAL" in declared or "NUM" in declared:
        return f"CAST({quoted} AS INTEGER)"
    return f"CAST(strftime('%s', {quoted}) AS INTEGER)"


@dataclass
class DeleteTask:
    """One table's worth of expired rows, with the predicates that find them."""

    database: str
    table: str
    #: full predicate: the age test *and* every protection rule
    where: str
    params: Tuple[object, ...] = ()
    #: age test alone, used only to report how many rows protection saved
    age_where: str = ""
    age_params: Tuple[object, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if not self.age_where:
            self.age_where = self.where
            self.age_params = self.params

    @property
    def policy(self) -> SourcePolicy:
        found = policy_for(self.table, self.database)
        assert found is not None, f"{self.table} is not in POLICY"
        return found


@dataclass
class SourceResult:
    """What happened, or would happen, to one table."""

    database: str
    table: str
    eligible: int = 0
    deleted: int = 0
    protected: int = 0
    estimated_bytes: int = 0
    archived: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "database": self.database,
            "table": self.table,
            "eligible": self.eligible,
            "deleted": self.deleted,
            "protected": self.protected,
            "estimated_bytes": self.estimated_bytes,
            "archived": self.archived,
            "note": self.note,
        }


@dataclass
class RetentionReport:
    """The outcome of one plan or apply run."""

    days: int
    cutoff: int
    dry_run: bool
    sources: List[SourceResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    free_bytes_before: Dict[str, int] = field(default_factory=dict)
    free_bytes_after: Dict[str, int] = field(default_factory=dict)

    @property
    def cutoff_iso(self) -> str:
        return _iso(self.cutoff)

    @property
    def total_eligible(self) -> int:
        return sum(s.eligible for s in self.sources)

    @property
    def total_deleted(self) -> int:
        return sum(s.deleted for s in self.sources)

    @property
    def total_bytes(self) -> int:
        return sum(s.estimated_bytes for s in self.sources)

    def to_dict(self) -> Dict[str, object]:
        return {
            "retention_days": self.days,
            "cutoff_epoch": self.cutoff,
            "cutoff_iso": self.cutoff_iso,
            "dry_run": self.dry_run,
            "started_iso": _iso(int(self.started)),
            "finished_iso": _iso(int(self.finished or time.time())),
            "elapsed_s": round((self.finished or time.time()) - self.started, 2),
            "total_rows": self.total_eligible,
            "total_bytes": self.total_bytes,
            "total_bytes_human": fmt_bytes(self.total_bytes),
            "free_bytes_before": self.free_bytes_before,
            "free_bytes_after": self.free_bytes_after,
            "sources": [s.to_dict() for s in self.sources],
            "errors": self.errors,
        }

    def summary(self) -> str:
        verb = "would delete" if self.dry_run else "deleted"
        lines = [
            f"retention {self.days}d   cutoff {self.cutoff_iso}",
            f"{verb}: {self.total_eligible:,} rows ({fmt_bytes(self.total_bytes)})",
            "",
            f"  {'database':14s} {'table':26s} {'rows':>12s} {'size':>10s} "
            f"{'protected':>10s}",
        ]
        for s in sorted(self.sources, key=lambda x: -x.estimated_bytes):
            if not s.eligible:
                continue
            lines.append(
                f"  {s.database:14s} {s.table:26s} {s.eligible:>12,} "
                f"{fmt_bytes(s.estimated_bytes):>10s} {s.protected:>10,}"
            )
        if self.total_eligible == 0:
            lines.append("  nothing is older than the cutoff")
        for s in self.sources:
            if s.note and s.eligible:
                lines.append(f"  note {s.table}: {s.note}")
        for err in self.errors:
            lines.append(f"  ERROR {err}")
        return "\n".join(lines)


class Retention:
    """Apply a rolling window to the operational databases."""

    def __init__(self, intel_db: Path | str, tsm_db: Path | str,
                 days: int = RETENTION_DAYS,
                 now: Optional[int] = None,
                 batch: int = BATCH_ROWS,
                 archive_dir: Optional[Path | str] = None,
                 allow_replay_loss: bool = False) -> None:
        if days <= 0:
            raise ValueError("retention days must be positive")
        self.intel_db = Path(intel_db)
        self.tsm_db = Path(tsm_db)
        self.days = int(days)
        self.now = int(now if now is not None else time.time())
        self.batch = int(batch)
        self.archive_dir = Path(archive_dir) if archive_dir else None
        self.allow_replay_loss = bool(allow_replay_loss)
        self.cutoff = self.now - self.days * 86400

    # -- connection ------------------------------------------------------
    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(path, timeout=60.0, isolation_level=None)
        con.execute("PRAGMA busy_timeout=60000")
        return con

    @staticmethod
    def _free_bytes(con: sqlite3.Connection) -> int:
        free = int(con.execute("PRAGMA freelist_count").fetchone()[0])
        page = int(con.execute("PRAGMA page_size").fetchone()[0])
        return free * page

    @staticmethod
    def _count(con: sqlite3.Connection, table: str, where: str,
               params: Sequence[object] = ()) -> int:
        return int(con.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where}', tuple(params)
        ).fetchone()[0])

    @staticmethod
    def _in_list(values: Sequence[object]) -> Tuple[str, Tuple[object, ...]]:
        if not values:
            return "0", ()
        return ",".join("?" * len(values)), tuple(values)

    # -- which snapshots are expired --------------------------------------
    def _expired_ah_snapshots(self, con: sqlite3.Connection) -> Tuple[List[str], int]:
        rows = con.execute(
            "SELECT snapshot_id, realm, captured_at FROM ah_snapshot "
            "ORDER BY realm, captured_at DESC"
        ).fetchall()
        newest: Dict[str, str] = {}
        for sid, realm, _ts in rows:
            newest.setdefault(str(realm), str(sid))
        keep = set(newest.values())
        expired = [str(sid) for sid, _r, ts in rows
                   if ts is not None and int(ts) < self.cutoff
                   and str(sid) not in keep]
        return expired, len(keep)

    def _expired_inventory(self, con: sqlite3.Connection) -> Tuple[List[str], int]:
        rows = con.execute(
            "SELECT snapshot_id, character, captured_at FROM inventory_snapshot "
            "ORDER BY character, captured_at DESC"
        ).fetchall()
        newest: Dict[str, str] = {}
        for sid, ch, _ts in rows:
            newest.setdefault(str(ch), str(sid))
        keep = set(newest.values())
        expired = [str(sid) for sid, _c, ts in rows
                   if ts is not None and int(ts) < self.cutoff
                   and str(sid) not in keep]
        return expired, len(keep)

    def _expired_tsm_snapshots(self, con: sqlite3.Connection) -> Tuple[List[int], int]:
        epoch = epoch_expr(con, "tsm_snapshot", "updated_at")
        rows = con.execute(
            f"SELECT snapshot_id, kind, scope, {epoch} AS epoch "
            "FROM tsm_snapshot ORDER BY kind, scope, epoch DESC"
        ).fetchall()
        newest: Dict[Tuple[str, str], int] = {}
        for sid, kind, scope, _ts in rows:
            newest.setdefault((str(kind), str(scope)), int(sid))
        keep = set(newest.values())
        expired = [int(sid) for sid, _k, _s, ts in rows
                   if ts is not None and int(ts) < self.cutoff
                   and int(sid) not in keep]
        return expired, len(keep)

    # -- the tasks -------------------------------------------------------
    def tasks(self, con: sqlite3.Connection, database: str) -> List[DeleteTask]:
        """Every deletion this run would perform, in dependency order."""
        if database == "intel.sqlite":
            return self._intel_tasks(con)
        return self._tsm_tasks(con)

    def _intel_tasks(self, con: sqlite3.Connection) -> List[DeleteTask]:
        expired_ah, kept_ah = self._expired_ah_snapshots(con)
        expired_inv, kept_inv = self._expired_inventory(con)
        out: List[DeleteTask] = []

        if expired_ah:
            marks, params = self._in_list(expired_ah)
            out.append(DeleteTask(
                "intel.sqlite", "ah_auction", f"snapshot_id IN ({marks})", params,
                note=f"{len(expired_ah)} expired scans; {kept_ah} current kept",
            ))
            out.append(DeleteTask(
                "intel.sqlite", "ah_snapshot", f"snapshot_id IN ({marks})", params,
                age_where="captured_at < ?", age_params=(self.cutoff,),
                note=f"{kept_ah} current scans kept",
            ))

        if expired_inv:
            marks, params = self._in_list(expired_inv)
            out.append(DeleteTask("intel.sqlite", "inventory_row",
                                  f"snapshot_id IN ({marks})", params))
            out.append(DeleteTask(
                "intel.sqlite", "inventory_posted_listing",
                f"snapshot_id IN ({marks}) AND COALESCE(time_left, 0) <= 0", params,
                age_where=f"snapshot_id IN ({marks})", age_params=params,
                note="postings whose auction has not ended are kept",
            ))
            out.append(DeleteTask("intel.sqlite", "reconciliation",
                                  f"snapshot_id IN ({marks})", params))
            out.append(DeleteTask("intel.sqlite", "inventory_snapshot",
                                  f"snapshot_id IN ({marks})", params,
                                  age_where="captured_at < ?",
                                  age_params=(self.cutoff,),
                                  note=f"{kept_inv} current snapshots kept"))

        for table, column in (("seller_observation", "observed_at"),
                              ("undercut_observation", "observed_at")):
            out.append(DeleteTask("intel.sqlite", table,
                                  f"{column} < ?", (self.cutoff,)))

        # Invoices first: a buyer link is only released once the invoice it
        # points at is gone, so the link delete has to run after this one.
        out.append(DeleteTask(
            "intel.sqlite", "mail_invoice",
            "seen_at < ? AND COALESCE(days_left, 0) <= 0", (self.cutoff,),
            age_where="seen_at < ?", age_params=(self.cutoff,),
            note="mail still in flight (days_left > 0) is kept",
        ))
        out.append(DeleteTask(
            "intel.sqlite", "sale_buyer_link",
            f"{epoch_expr(con, 'sale_buyer_link', 'linked_at')} < ? AND "
            "mail_id NOT IN (SELECT mail_id FROM mail_invoice)", (self.cutoff,),
            age_where=f"{epoch_expr(con, 'sale_buyer_link', 'linked_at')} < ?",
            age_params=(self.cutoff,),
            note="kept while the invoice it links still exists",
        ))
        return out

    def _tsm_tasks(self, con: sqlite3.Connection) -> List[DeleteTask]:
        expired, kept = self._expired_tsm_snapshots(con)
        out: List[DeleteTask] = []
        if expired:
            marks, params = self._in_list(expired)
            for table in ("tsm_price_change", "tsm_realm_price"):
                out.append(DeleteTask("tsm_markets.db", table,
                                      f"snapshot_id IN ({marks})", params,
                                      note=f"{kept} current snapshots kept"))
            out.append(DeleteTask("tsm_markets.db", "tsm_snapshot",
                                  f"snapshot_id IN ({marks})", params,
                                  age_where=f"{epoch_expr(con, 'tsm_snapshot', 'updated_at')} < ?",
                                  age_params=(self.cutoff,),
                                  note=f"{kept} current snapshots kept"))

        stamp = epoch_expr(con, "snapshots", "snapshotTimestamp")
        legacy = [int(r[0]) for r in con.execute(
            f"SELECT snapshotId FROM snapshots WHERE {stamp} < ?", (self.cutoff,)
        ).fetchall()]
        if legacy:
            marks, params = self._in_list(legacy)
            out.append(DeleteTask("tsm_markets.db", "market_snapshots",
                                  f"snapshotId IN ({marks})", params,
                                  note="legacy July/October region imports"))
            out.append(DeleteTask("tsm_markets.db", "snapshots",
                                  f"snapshotId IN ({marks})", params,
                                  age_where=f"{stamp} < ?",
                                  age_params=(self.cutoff,)))

        for table in ("auction_events", "auction_intel_events"):
            out.append(DeleteTask(
                "tsm_markets.db", table,
                f"{epoch_expr(con, table, 'ts')} < ?", (self.cutoff,),
            ))
        return out

    # -- archive ---------------------------------------------------------
    def _archive(self, con: sqlite3.Connection, task: DeleteTask) -> str:
        """Write the expired rows to gzipped JSONL before they are deleted."""
        assert self.archive_dir is not None
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = _iso(self.cutoff).replace(":", "")
        path = self.archive_dir / f"{task.database}.{task.table}.{stamp}.jsonl.gz"
        cur = con.execute(
            f'SELECT * FROM "{task.table}" WHERE {task.where}', task.params
        )
        columns = [d[0] for d in cur.description]
        written = 0
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as fh:
            fh.write(json.dumps({
                "table": task.table,
                "database": task.database,
                "cutoff_epoch": self.cutoff,
                "cutoff_iso": _iso(self.cutoff),
                "columns": columns,
            }) + "\n")
            for row in cur:
                fh.write(json.dumps(dict(zip(columns, row)), default=str) + "\n")
                written += 1
        logger.info("archived %d rows from %s to %s", written, task.table, path)
        return str(path)

    # -- execution -------------------------------------------------------
    def _delete(self, con: sqlite3.Connection, task: DeleteTask) -> int:
        total = 0
        while True:
            cur = con.execute(
                f'DELETE FROM "{task.table}" WHERE rowid IN '
                f'(SELECT rowid FROM "{task.table}" WHERE {task.where} LIMIT ?)',
                tuple(task.params) + (self.batch,),
            )
            removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            total += removed
            if removed < self.batch:
                break
        return total

    def _run(self, dry_run: bool) -> RetentionReport:
        report = RetentionReport(days=self.days, cutoff=self.cutoff, dry_run=dry_run)
        for path, database in ((self.intel_db, "intel.sqlite"),
                               (self.tsm_db, "tsm_markets.db")):
            if not path.exists():
                report.errors.append(f"missing database {path}")
                continue
            try:
                self._run_one(path, database, report, dry_run)
            except PolicyRefusal as exc:
                report.errors.append(f"{database}: {exc}")
                logger.error("retention refused on %s: %s", database, exc)
            except sqlite3.Error as exc:
                report.errors.append(f"{database}: {exc}")
                logger.exception("retention failed on %s", database)
        report.finished = time.time()
        return report

    def _run_one(self, path: Path, database: str,
                 report: RetentionReport, dry_run: bool) -> None:
        con = self._open(path)
        try:
            report.free_bytes_before[str(path)] = self._free_bytes(con)
            tasks = self.tasks(con, database)

            # Refuse to destroy anything a past evaluation could not be
            # recomputed from, unless an archive was asked for or the caller
            # accepted the loss explicitly.
            if not dry_run and not self.archive_dir and not self.allow_replay_loss:
                blocking = [
                    t.table for t in tasks
                    if t.policy.needed_for_replay
                    and self._count(con, t.table, t.where, t.params) > 0
                ]
                if blocking:
                    raise PolicyRefusal(
                        "refusing to delete data needed to recompute past "
                        "evaluations: " + ", ".join(sorted(set(blocking)))
                        + ". Pass an archive directory, or set "
                        "allow_replay_loss=True to accept that earlier "
                        "evaluations can no longer be reproduced from raw data."
                    )

            if dry_run:
                # A dry run must not take a write lock: the watcher writes to
                # these databases continuously, and BEGIN IMMEDIATE would
                # block it for the whole scan.
                for task in tasks:
                    self._record(report, con, task, dry_run=True)
            else:
                con.execute("BEGIN IMMEDIATE")
                try:
                    for task in tasks:
                        self._record(report, con, task, dry_run=False)
                    con.execute("COMMIT")
                except (sqlite3.Error, OSError):
                    con.execute("ROLLBACK")
                    raise
            report.free_bytes_after[str(path)] = self._free_bytes(con)
        finally:
            con.close()

    def _record(self, report: RetentionReport, con: sqlite3.Connection,
                task: DeleteTask, dry_run: bool) -> None:
        """Count, and if applying, archive then delete one table."""
        res = SourceResult(database=task.database, table=task.table,
                           note=task.note)
        res.eligible = self._count(con, task.table, task.where, task.params)
        res.estimated_bytes = int(res.eligible * task.policy.bytes_per_row)
        aged = self._count(con, task.table, task.age_where, task.age_params)
        res.protected = max(0, aged - res.eligible)
        if dry_run:
            res.deleted = res.eligible
        else:
            if self.archive_dir and res.eligible:
                res.archived = self._archive(con, task)
            res.deleted = self._delete(con, task)
            logger.info("retention deleted %d rows from %s.%s",
                        res.deleted, task.database, task.table)
        report.sources.append(res)

    def plan(self) -> RetentionReport:
        """Count what ``apply()`` would delete.  Touches nothing."""
        return self._run(dry_run=True)

    def apply(self) -> RetentionReport:
        """Delete expired rows.  Safe to run repeatedly."""
        return self._run(dry_run=False)


def render_policy() -> str:
    """The policy as a table a human can read and approve."""
    expiring = [p for p in POLICY if p.expires]
    lines = [
        f"Retention policy: {RETENTION_DAYS} days, UTC.",
        f"{len(expiring)} of {len(POLICY)} declared tables expire; "
        f"{len(POLICY) - len(expiring)} are kept regardless of age.",
        "",
        f"{'database':14s} {'table':26s} {'kind':10s} {'timestamp':28s} expires",
        "-" * 92,
    ]
    for p in sorted(POLICY, key=lambda x: (x.database, not x.expires, x.table)):
        lines.append(
            f"{p.database:14s} {p.table:26s} {p.category:10s} "
            f"{p.timestamp or '-':28s} {'yes' if p.expires else 'no'}"
        )
    lines += ["", "Expiration and protection rules:"]
    for p in expiring:
        lines.append(f"  {p.table}")
        lines.append(f"    expires when : {p.expires_when}")
        if p.protection:
            lines.append(f"    protected    : {p.protection}")
        if p.rationale:
            lines.append(f"    why          : {p.rationale}")
        if p.needed_for_replay:
            lines.append("    replay       : needed to recompute a past "
                         "evaluation from raw data")
    return "\n".join(lines)


def vacuum(intel_db: Path | str, tsm_db: Path | str) -> Dict[str, object]:
    """Rebuild both databases so the pages freed by retention are returned.

    ``auto_vacuum`` is OFF on both databases, so deleting rows moves pages to
    the freelist but never shrinks the file.  A full ``VACUUM`` is the only
    way to reclaim the space, and it needs roughly the database size again in
    free disk while it runs.
    """
    out: Dict[str, object] = {}
    for path in (Path(intel_db), Path(tsm_db)):
        if not path.exists():
            out[str(path)] = {"error": "missing"}
            continue
        before = path.stat().st_size
        started = time.perf_counter()
        con = sqlite3.connect(path, timeout=120.0, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=120000")
            con.execute("VACUUM")
        finally:
            con.close()
        out[str(path)] = {
            "bytes_before": before,
            "bytes_after": path.stat().st_size,
            "reclaimed": before - path.stat().st_size,
            "seconds": round(time.perf_counter() - started, 1),
        }
    return out
