"""Measure the read shapes the aggregates could use, one process per shape.

Why a script and not an opinion: the request that produced the snapshot work
said not to assume that an encoding is faster, and to benchmark it.  This is
that benchmark.  It reads one day of ``ah_auction`` -- the largest table in the
store, 50.2M rows over 22 GB at the time of writing -- in the four shapes that
were actually candidates, and reports wall time, peak RSS and bytes for each.

Each shape runs in its own process because ``ru_maxrss`` is a high-water mark
for the whole process: measuring two shapes in one process would report the
larger one twice.

    python scripts/bench_large_data.py --day 2026-10-22 --shape scan
    python scripts/bench_large_data.py --day 2026-10-22 --all

Shapes
------
``count``     how fast the engine can merely touch the day.
``scan``      the exact query ``_stream_ah_auction`` runs: seven columns, the
              join to the snapshot header, ordered by the group key.
``scan_plain`` the same seven columns without the ORDER BY, which isolates what
              the sort costs.
``star``      ``SELECT *`` in the same order -- what a naive implementation
              would pay, and the reason the stream projects its columns.
``parquet``   the same seven columns written to Parquet and read back
              columnar.  Needs ``pyarrow``; skipped, with a reason, when it is
              not installed.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.aggregates import _as_day, _ro                    # noqa: E402

DEFAULT_INTEL = ROOT / "data" / "intel.sqlite"
DEFAULT_TSM = ROOT / "data" / "tsm_markets.db"

SHAPES = ("count", "scan", "scan_plain", "star", "parquet")

#: The columns the auction aggregate actually consumes.  Kept here as a literal
#: on purpose: this script measures the query, it does not import it, so a
#: change to the stream shows up as a difference rather than as agreement.
COLUMNS = ("a.item_id, a.quantity, a.unit_price_copper,"
           " a.total_price_copper, a.is_commodity, a.snapshot_id")

MARKET = "COALESCE(NULLIF(s.realm, ''), s.realm_slug, '?')"


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def day_snapshots(con, day: str) -> list:
    rows = con.execute(
        "SELECT snapshot_id, captured_at FROM ah_snapshot"
        " WHERE captured_at IS NOT NULL").fetchall()
    return [str(sid) for sid, captured_at in rows if _as_day(captured_at) == day]


def run(shape: str, intel: Path, day: str, batch: int) -> dict:
    con = _ro(intel)
    started = time.perf_counter()
    result: dict = {"shape": shape, "day": day}
    try:
        ids = day_snapshots(con, day)
        result["snapshots"] = len(ids)
        if not ids:
            result["error"] = "no snapshots for that day"
            return result
        marks = ",".join("?" * len(ids))
        params = tuple(ids)
        join = (f"FROM ah_auction a JOIN ah_snapshot s"
                f" ON s.snapshot_id = a.snapshot_id"
                f" WHERE a.snapshot_id IN ({marks})")

        if shape == "count":
            n = con.execute(f"SELECT COUNT(*) {join}", params).fetchone()[0]
            result["rows"] = int(n)
        elif shape in ("scan", "scan_plain"):
            sql = (f"SELECT {MARKET} AS market, {COLUMNS} {join}")
            if shape == "scan":
                sql += " ORDER BY market, a.item_id, s.captured_at, a.auction_key"
            result["rows"] = _drain(con.execute(sql, params), batch)
        elif shape == "star":
            sql = (f"SELECT {MARKET} AS market, a.* {join}"
                   " ORDER BY market, a.item_id, s.captured_at, a.auction_key")
            result["rows"] = _drain(con.execute(sql, params), batch)
        elif shape == "parquet":
            result.update(_parquet(con, join, params, day, batch))
        else:
            result["error"] = f"unknown shape {shape}"
    finally:
        con.close()
        result["elapsed_s"] = round(time.perf_counter() - started, 2)
        result["peak_rss_mb"] = round(peak_rss_mb(), 1)
    return result


def _drain(cur, batch: int) -> int:
    """Consume the cursor in batches, touching every row as an aggregate would."""
    n = 0
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return n
        n += len(rows)


def _parquet(con, join: str, params: tuple, day: str, batch: int) -> dict:
    """Write the projection to Parquet, then read it back and sum it."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:                          # pragma: no cover
        return {"skipped": f"pyarrow not installed ({exc})"}

    out = Path(os.environ.get("BENCH_DIR", "/tmp")) / f"ah_auction_{day}.parquet"
    sql = (f"SELECT {MARKET} AS market, {COLUMNS} {join}")
    cur = con.execute(sql, params)

    schema = pa.schema([("market", pa.string()), ("item_id", pa.int64()),
                        ("quantity", pa.int64()),
                        ("unit_price_copper", pa.int64()),
                        ("total_price_copper", pa.int64()),
                        ("is_commodity", pa.int64()),
                        ("snapshot_id", pa.string())])
    write_started = time.perf_counter()
    written = 0
    with pq.ParquetWriter(out, schema, compression="zstd") as writer:
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            cols = list(zip(*rows))
            writer.write_table(pa.table(
                {name: list(col) for name, col in zip(schema.names, cols)},
                schema=schema))
            written += len(rows)
    write_s = time.perf_counter() - write_started

    read_started = time.perf_counter()
    table = pq.read_table(out, columns=["item_id", "quantity",
                                        "unit_price_copper"])
    read_s = time.perf_counter() - read_started
    scanned = table.num_rows
    total = sum(v or 0 for v in table.column("unit_price_copper").to_pylist())
    del table, total
    return {"rows": written, "parquet_rows_read": scanned,
            "write_s": round(write_s, 2), "read_s": round(read_s, 2),
            "bytes": out.stat().st_size}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=None,
                        help="the day to read (default: the largest day)")
    parser.add_argument("--intel-db", default=str(DEFAULT_INTEL))
    parser.add_argument("--batch", type=int, default=50_000)
    parser.add_argument("--shape", default=None,
                        choices=SHAPES,
                        help="measure one shape in this process")
    args = parser.parse_args(argv)

    intel = Path(args.intel_db)
    day = args.day or largest_day(intel)

    if args.shape:
        # One shape per process: `ru_maxrss` is a high-water mark for the
        # whole process, so two shapes in one process would report the
        # larger one twice.
        print(json.dumps(run(args.shape, intel, day, args.batch)), flush=True)
        return 0

    print(f"day {day}, one process per shape", flush=True)
    for shape in SHAPES:
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--shape", shape,
             "--day", day, "--intel-db", str(intel),
             "--batch", str(args.batch)],
            capture_output=True, text=True)
        line = (child.stdout or "").strip().splitlines()
        print(line[-1] if line else json.dumps(
            {"shape": shape, "error": (child.stderr or "").strip()[-300:]}),
            flush=True)
    return 0


def largest_day(intel: Path) -> str:
    con = _ro(intel)
    try:
        rows = con.execute(
            "SELECT captured_at, COUNT(*) FROM ah_snapshot"
            " WHERE captured_at IS NOT NULL GROUP BY captured_at").fetchall()
    finally:
        con.close()
    buckets: dict = {}
    for captured_at, n in rows:
        buckets[_as_day(captured_at)] = buckets.get(_as_day(captured_at), 0) + n
    return max(buckets, key=buckets.get)


if __name__ == "__main__":
    raise SystemExit(main())
