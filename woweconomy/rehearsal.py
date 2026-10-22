"""The comparative dry run: what each deletion would cost and what would remain.

``Retention.plan()`` answers "how many rows".  That is not enough to authorise a
deletion.  The questions that matter are:

* For each table, does an aggregate exist that preserves its statistics, and
  does that aggregate actually cover the days being deleted?
* If the raw rows go and are not archived, what can no longer be recomputed?
* What will the archive cost, measured rather than guessed?
* What is the net change in disk, once the aggregate and the archive are
  counted?
* What is the space position now, so the reclaim can be measured later?

This module answers those and deletes nothing.  It is deliberately separate
from ``retention.py``: the thing that decides is not the thing that destroys.
"""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .aggregates import (
    AGGREGATE_FOR_TABLE,
    COVERED_BY_CHILD,
    DAY_SQL,
    coverage as aggregate_coverage,
)
from .retention import (
    POLICY,
    Retention,
    RetentionReport,
    SourceResult,
    _iso,
    fmt_bytes,
    policy_for,
)

#: How many rows are sampled to measure the JSONL gzip ratio.  A measured
#: ratio on a sample beats a guessed constant, and 2000 rows is cheap.
SAMPLE_ROWS = 2_000

#: Where a table's statistics survive, if anywhere.
COVERED = "aggregate"        # an aggregate summarises this table
DERIVED = "derived"          # a header whose meaning survives through a child
UNCOVERED = "none"           # no aggregate: deleting loses the history


@dataclass
class TablePlan:
    database: str
    table: str
    category: str
    eligible: int
    protected: int
    bytes_est: int
    coverage: str = UNCOVERED
    aggregate: str = ""
    aggregate_covers: str = ""
    expired_range: str = ""
    reproducible: bool = False
    archive_rows: int = 0
    archive_bytes_est: int = 0
    note: str = ""

    @property
    def needs_replay_decision(self) -> bool:
        """True when deleting this table destroys something not summarised."""
        return self.eligible > 0 and self.coverage == UNCOVERED

    def to_dict(self) -> Dict[str, object]:
        return {
            "database": self.database, "table": self.table,
            "category": self.category, "eligible": self.eligible,
            "protected": self.protected, "bytes_est": self.bytes_est,
            "coverage": self.coverage, "aggregate": self.aggregate,
            "aggregate_covers": self.aggregate_covers,
            "expired_range": self.expired_range,
            "reproducible": self.reproducible,
            "archive_rows": self.archive_rows,
            "archive_bytes_est": self.archive_bytes_est,
            "note": self.note,
        }


@dataclass
class SpaceReport:
    """The physical position of one database file."""

    path: str
    file_bytes: int = 0
    page_size: int = 0
    page_count: int = 0
    freelist_count: int = 0

    @property
    def freelist_bytes(self) -> int:
        return self.freelist_count * self.page_size

    @property
    def live_bytes(self) -> int:
        return (self.page_count - self.freelist_count) * self.page_size

    @property
    def vacuum_would_reclaim(self) -> int:
        """What a full VACUUM would return to the filesystem.

        ``auto_vacuum`` is OFF, so deleted rows move pages to the freelist and
        the file never shrinks on its own.  This is the number to compare
        against after a VACUUM, and it is zero until something is deleted.
        """
        return self.freelist_bytes

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "file_bytes": self.file_bytes,
            "page_size": self.page_size,
            "page_count": self.page_count,
            "freelist_count": self.freelist_count,
            "freelist_bytes": self.freelist_bytes,
            "live_bytes": self.live_bytes,
            "vacuum_would_reclaim": self.freelist_bytes,
        }

    def summary(self) -> str:
        return (f"{Path(self.path).name:20s} {fmt_bytes(self.file_bytes):>10s}  "
                f"live {fmt_bytes(self.live_bytes):>10s}  "
                f"free {fmt_bytes(self.freelist_bytes):>9s}")


def space_report(path: Path | str) -> SpaceReport:
    """Measure one database file: size, pages, and what VACUUM would return."""
    p = Path(path)
    out = SpaceReport(path=str(p))
    if not p.exists():
        return out
    out.file_bytes = p.stat().st_size
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        out.page_size = int(con.execute("PRAGMA page_size").fetchone()[0])
        out.page_count = int(con.execute("PRAGMA page_count").fetchone()[0])
        out.freelist_count = int(con.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        con.close()
    return out


def space_payload(*paths: Path | str) -> Dict[str, object]:
    """A position that can be written to disk and compared later.

    ``--space`` before a deletion is only useful if the "after" measurement can
    be diffed against it, so the numbers are recorded rather than printed.
    """
    return {
        "measured_at": _iso(int(time.time())),
        "databases": {
            Path(p).name: space_report(p).to_dict() for p in paths
        },
    }


def _signed(n: int) -> str:
    """A delta with its sign kept, so a growth is never read as a reclaim.

    Zero has no sign.  The previous form fell through to the ``-`` branch
    and printed ``-0.00B``, which reads as a small reclaim that never
    happened.
    """
    if n == 0:
        return fmt_bytes(0)
    return ("+" if n > 0 else "-") + fmt_bytes(abs(n))


def _net_note(net_bytes: int) -> str:
    """Say plainly which way the disk moves, since the sign invites a misread.

    ``net_bytes`` is the *reclaim*: raw freed minus what the aggregate and
    the archive still cost.  The report prints the disk **delta**, which is
    its negation, so a shrink shows as a negative change.  Printing the
    reclaim under a label that says "change" is how a 2 GB growth gets read
    as a 2 GB win.
    """
    if net_bytes < 0:
        return "  (grows: the aggregate costs more than the deletion frees)"
    if net_bytes > 0:
        return "  (shrinks)"
    return "  (no change)"


def compare_space(baseline: Dict[str, object],
                  current: Dict[str, object]) -> str:
    """What actually changed between two measured positions.

    A deletion moves pages to the freelist; it does not shrink the file, because
    ``auto_vacuum`` is OFF.  This separates the two: the *live* delta is what
    the deletion freed, and the *file* delta is what the filesystem got back.
    Reporting them as one number would let a run claim a reclaim it never made.
    """
    lines = [
        f"space change   {baseline.get('measured_at')} -> "
        f"{current.get('measured_at')}",
        f"  {'file':20s} {'before':>10s} {'after':>10s} {'live delta':>12s} "
        f"{'file delta':>12s} {'free after':>11s}",
    ]
    base = baseline.get("databases") or {}
    now = current.get("databases") or {}
    if not isinstance(base, dict) or not isinstance(now, dict):
        return "space change: unreadable baseline"

    total_live = 0
    total_file = 0
    for name, after in now.items():
        before = base.get(name) or {}
        live_before = int(before.get("live_bytes", 0))
        live_after = int(after.get("live_bytes", 0))
        file_before = int(before.get("file_bytes", 0))
        file_after = int(after.get("file_bytes", 0))
        live_delta = live_after - live_before
        file_delta = file_after - file_before
        total_live += live_delta
        total_file += file_delta
        lines.append(
            f"  {name:20s} {fmt_bytes(file_before):>10s} "
            f"{fmt_bytes(file_after):>10s} {_signed(live_delta):>12s} "
            f"{_signed(file_delta):>12s} "
            f"{fmt_bytes(int(after.get('freelist_bytes', 0))):>11s}"
        )
    lines.append(
        f"  {'total':20s} {'':>10s} {'':>10s} {_signed(total_live):>12s} "
        f"{_signed(total_file):>12s}"
    )
    pending = sum(int(d.get("freelist_bytes", 0)) for d in now.values()
                  if isinstance(d, dict))
    if total_live < 0 and total_file >= 0:
        lines.append(
            f"  {fmt_bytes(-total_live)} was freed but the files did not "
            f"shrink: auto_vacuum is OFF, so VACUUM is what returns it"
        )
    elif total_file < 0:
        lines.append(f"  the filesystem received {fmt_bytes(-total_file)} back")
    elif total_live > 0:
        lines.append(f"  the databases grew by {fmt_bytes(total_live)}; "
                     f"nothing was freed")
    else:
        lines.append("  nothing was freed")
    if pending:
        lines.append(f"  {fmt_bytes(pending)} is still on the freelist")
    return "\n".join(lines)


@dataclass
class Rehearsal:
    """The full comparative picture before a deletion is authorised."""

    days: int
    cutoff_iso: str
    tables: List[TablePlan] = field(default_factory=list)
    spaces: List[SpaceReport] = field(default_factory=list)
    aggregate_bytes: int = 0
    aggregate_coverage: Dict[str, object] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    validation_ok: Optional[bool] = None
    validation_note: str = ""

    @property
    def raw_bytes(self) -> int:
        return sum(t.bytes_est for t in self.tables)

    @property
    def archive_bytes_est(self) -> int:
        return sum(t.archive_bytes_est for t in self.tables)

    @property
    def total_rows(self) -> int:
        return sum(t.eligible for t in self.tables)

    @property
    def unreproducible(self) -> List[TablePlan]:
        return [t for t in self.tables if t.needs_replay_decision]

    @property
    def net_bytes(self) -> int:
        """Disk change: raw freed, minus what the aggregate and archive cost."""
        return self.raw_bytes - self.aggregate_bytes - self.archive_bytes_est

    def to_dict(self) -> Dict[str, object]:
        return {
            "days": self.days,
            "cutoff_iso": self.cutoff_iso,
            "total_rows": self.total_rows,
            "raw_bytes": self.raw_bytes,
            "archive_bytes_est": self.archive_bytes_est,
            "aggregate_bytes": self.aggregate_bytes,
            "net_bytes": self.net_bytes,
            "aggregate_coverage": self.aggregate_coverage,
            "validation_ok": self.validation_ok,
            "validation_note": self.validation_note,
            "tables": [t.to_dict() for t in self.tables],
            "spaces": [s.to_dict() for s in self.spaces],
            "unreproducible": [t.table for t in self.unreproducible],
            "errors": self.errors,
        }

    def summary(self) -> str:
        lines = [
            f"comparative dry run: {self.days} days, cutoff {self.cutoff_iso}",
            f"  rows eligible      : {self.total_rows:,}",
            f"  raw bytes freed    : {fmt_bytes(self.raw_bytes)}",
            f"  aggregate on disk  : {fmt_bytes(self.aggregate_bytes)} (kept)",
            f"  archive estimate   : {fmt_bytes(self.archive_bytes_est)}",
            f"  net disk change    : {_signed(-self.net_bytes)}"
            f"{_net_note(self.net_bytes)}",
            "",
            f"  {'database':14s} {'table':24s} {'rows':>10s} {'freed':>9s} "
            f"{'survives as':>16s} {'archive':>9s}",
        ]
        for t in sorted(self.tables, key=lambda x: -x.bytes_est):
            if not t.eligible:
                continue
            survives = {
                COVERED: f"~{t.aggregate}",
                DERIVED: f"via {t.aggregate}",
                UNCOVERED: "NOTHING",
            }[t.coverage]
            lines.append(
                f"  {t.database:14s} {t.table:24s} {t.eligible:>10,} "
                f"{fmt_bytes(t.bytes_est):>9s} {survives:>16s} "
                f"{fmt_bytes(t.archive_bytes_est):>9s}"
            )
        if self.total_rows == 0:
            lines.append("  nothing is older than the cutoff")
        if self.unreproducible:
            lines += ["", "  Tables whose history would not survive:"]
            for t in self.unreproducible:
                lines.append(f"    {t.table} ({t.eligible:,} rows, "
                             f"{fmt_bytes(t.bytes_est)})")
            lines.append("  For these, the archive is the only copy.")
        lines += ["", "  Current space position:"]
        for s in self.spaces:
            lines.append("    " + s.summary())
        if self.validation_note:
            lines += ["", f"  aggregates: {self.validation_note}"]
        for err in self.errors:
            lines.append(f"  ERROR {err}")
        return "\n".join(lines)


def _sample_archive_cost(con: sqlite3.Connection, table: str, where: str,
                         params: Sequence[object],
                         sample: int = SAMPLE_ROWS) -> Tuple[float, int]:
    """Measure the JSONL gzip ratio on a sample of the rows to be archived.

    Returns (compressed_bytes_per_row, rows_sampled).  A measured ratio is used
    instead of a constant because the tables differ by an order of magnitude in
    how compressible they are: integer-heavy price rows pack far better than
    event rows carrying names and JSON blobs.
    """
    cur = con.execute(f'SELECT * FROM "{table}" WHERE {where} LIMIT ?',
                      tuple(params) + (sample,))
    columns = [d[0] for d in cur.description]
    raw = io.BytesIO()
    uncompressed = 0
    rows = 0
    for row in cur:
        line = (json.dumps(dict(zip(columns, row)), default=str) + "\n")
        data = line.encode("utf-8")
        raw.write(data)
        uncompressed += len(data)
        rows += 1
    if rows == 0:
        return 0.0, 0
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(raw.getvalue())
    return len(buf.getvalue()) / rows, rows


def rehearse(intel_db: Path | str, tsm_db: Path | str,
             aggregate_db: Path | str,
             days: int, now: Optional[int] = None,
             archive_dir: Optional[Path | str] = None,
             validation_ok: Optional[bool] = None,
             validation_note: str = "") -> Rehearsal:
    """Compare what would be deleted against what would remain."""
    runner = Retention(intel_db=intel_db, tsm_db=tsm_db, days=days, now=now,
                       archive_dir=archive_dir)
    plan: RetentionReport = runner.plan()
    out = Rehearsal(days=days, cutoff_iso=plan.cutoff_iso,
                    errors=list(plan.errors))
    out.validation_ok = validation_ok
    out.validation_note = validation_note

    agg_cov = aggregate_coverage(aggregate_db)
    out.aggregate_coverage = agg_cov
    if agg_cov.get("exists"):
        out.aggregate_bytes = int(agg_cov.get("bytes", 0))  # type: ignore[arg-type]

    by_table = {(s.database, s.table): s for s in plan.sources}
    conns: Dict[str, sqlite3.Connection] = {}

    def conn_for(database: str) -> Optional[sqlite3.Connection]:
        path = Path(intel_db if database == "intel.sqlite" else tsm_db)
        if not path.exists():
            return None
        if database not in conns:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60.0)
            conns[database] = c
        return conns[database]

    try:
        # every declared table appears, so the absence of a row is meaningful
        for policy in POLICY:
            result = by_table.get((policy.database, policy.table))
            if result is None:
                continue
            tp = TablePlan(
                database=policy.database, table=policy.table,
                category=policy.category, eligible=result.eligible,
                protected=result.protected,
                bytes_est=result.estimated_bytes, note=result.note,
            )
            if policy.table in AGGREGATE_FOR_TABLE:
                tp.coverage = COVERED
                tp.aggregate = AGGREGATE_FOR_TABLE[policy.table]
            elif policy.table in COVERED_BY_CHILD:
                tp.coverage = DERIVED
                tp.aggregate = COVERED_BY_CHILD[policy.table]
            else:
                tp.coverage = UNCOVERED

            agg_entry = (agg_cov.get("aggregates", {}) or {}).get(tp.aggregate)
            if agg_entry:
                tp.aggregate_covers = (f"{agg_entry['first_day']}.."
                                       f"{agg_entry['last_day']} "
                                       f"({agg_entry['days']}d)")

            con = conn_for(policy.database)
            if result.eligible and con is not None:
                task = next((t for t in runner.tasks(con, policy.database)
                             if t.table == policy.table), None)
                if task is not None:
                    day_sql = DAY_SQL.get((policy.database, policy.table))
                    if day_sql:
                        try:
                            row = con.execute(
                                f'SELECT MIN(d), MAX(d), COUNT(*) FROM '
                                f'(SELECT ({day_sql}) AS d FROM "{policy.table}" t '
                                f'WHERE {task.where})',
                                task.params,
                            ).fetchone()
                            if row and row[0]:
                                tp.expired_range = f"{row[0]}..{row[1]}"
                        except sqlite3.Error as exc:
                            out.errors.append(
                                f"{policy.table}: cannot read day range: {exc}")
                    ratio, _n = _sample_archive_cost(
                        con, policy.table, task.where, task.params)
                    tp.archive_rows = result.eligible
                    tp.archive_bytes_est = int(ratio * result.eligible)

                # reproducible only when an aggregate exists AND it covers the
                # days being deleted
                if tp.coverage == UNCOVERED:
                    tp.reproducible = False
                elif not agg_entry:
                    tp.reproducible = False
                elif tp.expired_range and agg_entry["first_day"]:
                    lo = tp.expired_range.split("..")[0]
                    hi = tp.expired_range.split("..")[-1]
                    tp.reproducible = (agg_entry["first_day"] <= lo
                                       and agg_entry["last_day"] >= hi)
                else:
                    tp.reproducible = False
            out.tables.append(tp)

        for path in (intel_db, tsm_db):
            out.spaces.append(space_report(path))
    finally:
        for c in conns.values():
            c.close()
    return out
