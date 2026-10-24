# Large data: what it costs, what was measured, what changed

This is the record of one pass over the pipeline's data lifecycle: where the
bytes are, what reading them costs, what was optimised because a measurement
justified it, and what was deliberately left alone.  Every number here was
taken on this machine against the live store, not estimated.

The short version:

| | before | after |
|---|---|---|
| a 6-hourly aggregate run over an unchanged window | 404.8 s, 50,152,511 rows read, 0 rows written | 1.1 s, 0 rows read, 0 rows written |
| a 6-hourly run where one day did change | whole window re-read | 319.9 s, 38,937,330 rows (only the changed day) |
| peak worker memory, largest aggregate | — | 228 MB, against 5.14 GB in Postgres |
| cost of the mechanism | — | 4 rows / 32 kB |

Nothing was deleted, no database was reset, no volume was touched, and the
retention policy is unchanged.  The changes are additive and reversible; the
[rollback](#rollback) is one environment variable and a `git revert`.

---

## 1. The audit

### Where the bytes are

`public` schema of the operational Postgres database, measured with
`pg_total_relation_size` (table + indexes + TOAST):

| table | rows | size |
|---|---:|---:|
| `ah_auction` | 49,978,920 | **23 GB** |
| `tsm_price_change` | 13,789,729 | 2267 MB |
| `tsm_realm_price` | 1,205,753 | 324 MB |
| `auction_events` | 7,861 | 8032 kB |
| `auction_intel_events` | 9,726 | 3728 kB |
| `tsm_snapshot` | 1,758 | 736 kB |
| `ah_snapshot` | 1,339 | 624 kB |

`ah_auction` alone is two thirds of the database.  Everything else in the
store is smaller than its own index overhead would suggest, which is the first
useful fact: **there is exactly one large table**, and any optimisation that
does not reduce what is read from `ah_auction` is bookkeeping.

The aggregate side, built from those tables:

| table | rows | size |
|---|---:|---:|
| `daily_item_price` | 5,192,551 | 2159 MB |
| `daily_ah_auction` | 1,649,555 | 394 MB |
| `daily_event` | 243 | 96 kB |
| `aggregate_partition` | 673 | 272 kB |
| `aggregate_source_state` | 4 | **32 kB** |
| `aggregate_meta` | 5 | 64 kB |

### The window does not cover what it looks like it covers

The data spans three days: 2026-10-21 (38,395 rows), 10-22 (14.3M) and 10-23
(35.8M).  So `--last-days 2`, the daily window, covers **~100% of the fact
table**.  A windowed rebuild was not buying a small fraction of the work; it
was buying a reprieve only on the very oldest day, and only once there were
more than two days of history.

That is why the window alone was never going to be the answer, and why the
work below is about *skipping days whose source did not move* rather than about
narrowing a range.

### What was being recomputed for nothing

Measured with `pipeline_run` and a direct run of the aggregates task:

| task | measured |
|---|---|
| `tsm_csv` | 1864 s (sleeping: 187 HTTP fetches at 10 s each) |
| `fetch` | max 737 s |
| `aggregates` | max 533 s |
| `export` | max 312 s |
| `strategy` | 124 s |
| `sentinel` | 24 s |

Inside the aggregates task, the three aggregates wrote **zero rows** while
scanning 50.2M rows:

| aggregate | time | source rows read | rows written |
|---|---:|---:|---:|
| `daily_item_price` | 29.7 s | 2,893,814 | 0 |
| `daily_ah_auction` | 404.8 s | 50,152,511 | 0 |
| `daily_event` | 0.9 s | 17,587 | 0 |

435 s of a 6-hourly schedule spent to produce nothing.  The per-partition
checksum that already existed could not help: it is computed *after* the
source has been aggregated, so it only avoids the write, never the read.

### Nothing reads the aggregate tables

The only references to `daily_item_price`, `daily_ah_auction` and
`daily_event` outside `woweconomy/aggregates.py` and its tests are the
migration DDL in `db/migrations/versions/002_baseline.py`.  They are not a
cache in front of a hot path; they are the statistical record that outlives
the raw rows once retention deletes them.  That is a reason to keep them
correct and cheap, not a reason to optimise their read path -- and it is the
reason the retention policy must not be touched.

### Is Postgres the right source?

Yes, for the raw store.  The alternatives do not survive contact with the
facts:

* **A separate analytics database** would be a second copy of 23 GB and a
  second thing to keep in sync.  The requirement that a snapshot must never
  replace authoritative data rules it out as the primary source.
* **Reading from SQLite replicas** would need a copy step that is exactly the
  kind of "publish an incomplete result" hazard the rest of this work avoids.
* **Parquet/Arrow exports** were benchmarked, not assumed -- see
  [Storage format](#storage-format-benchmarked-not-assumed).

What *was* wrong was the query shape, not the engine: the build re-read a
table it had already summarised.  Postgres is also where the contention was,
and the diagnosis there is in `README.md` ("Keeping the desktop usable"):
`shared_buffers` of 16 MB and `work_mem` of 4 MB -- the defaults sized for a
64 MB machine -- made every aggregate re-read from disk and spill its sorts.
Tuning those moved the load from the database to the client and cut the
aggregate build from 533 s to 451 s before any of the work below.

---

## 2. What was implemented: per-day source watermarks

### The contract it rests on

`ah_auction` and `tsm_price_change` are append-only:

* `woweconomy.intel.ah.ingest` returns early for a `snapshot_id` it already
  has, writes its rows once with `INSERT OR IGNORE`, and commits -- so an
  existing snapshot's rows and price status never change again.
* The TSM side upserts, but the upsert rewrites `updated_at` and `checksum`
  on the same row.

Either way **the set of snapshot header rows for a day changes exactly when
that day's facts could have changed**.  Hashing the header rows -- hundreds of
them -- is therefore a sound stand-in for re-grouping tens of millions of fact
rows.  The fact tables are append-only, so this is not an approximation of a
full scan; it is a smaller object that carries the same information.

### What is stored

```sql
CREATE TABLE aggregate_source_state (
    aggregate     TEXT    NOT NULL,
    day           TEXT    NOT NULL,
    source_table  TEXT    NOT NULL,   -- the facts that were summarised
    version       INTEGER NOT NULL,   -- the fingerprint's definition
    watermark     TEXT    NOT NULL,   -- sha256 of the day's header rows
    built_at      TEXT    NOT NULL,   -- creation timestamp
    PRIMARY KEY (aggregate, day)
);
```

* **Explicit source identity.** `source_table` names the facts; the header
  table the fingerprint is read from is declared by `AggregateSpec.days_from`
  and changing it requires a `version` bump.  The source databases are
  recorded in `aggregate_meta`.
* **Versioned.** `_WATERMARK_VERSION` is compared, not just stored.  A state
  written by a different fingerprint definition is treated as absent, so new
  code can never compare against a hash it did not produce.  This was not
  theoretical: v1 was written by a fingerprint that hashed a fixed column
  list, v2 hashes the whole row, and the first v2 run correctly invalidated
  every v1 state and rebuilt 361,800 rows from 51.3M source rows.
* **Atomic and crash-safe.** State is written **only after** the day's rows
  are committed.  A state row that survived a crash mid-build would tell the
  next run to skip a day it never wrote.  The reverse (rows written, state
  lost) is harmless: the next run rebuilds and gets the same numbers.
* **Interrupted writes cannot publish a hole.** A partition is written as
  delete-then-insert; both statements and the tracking row are now one
  transaction.  Autocommitted one at a time, a crash between them left the
  rows gone and `aggregate_partition` still holding the *old* checksum -- the
  next run would compute the same digest, find it recorded, skip, and the
  partition would stay empty for ever, with its own provenance saying it was
  fine.  Covered by
  `test_an_interrupted_partition_write_cannot_leave_an_empty_partition`.
* **Never replaces authoritative data.** The aggregate database is separate
  and additive.  Nothing in this change reads, writes or deletes a fact row.
* **Controlled rebuild and fallback.** `--force` rebuilds every day in the
  window and ignores the watermark; omitting the window rebuilds everything.
  If the fingerprint cannot be computed (`{}` from `_source_watermarks`, e.g.
  the header table is missing), the build falls back to the previous
  full-window rebuild rather than skipping anything.  Silence is never read
  as "unchanged".
* **Incremental refresh, demonstrably correct.** The watermark only ever
  removes *days*, never rows: a day that is skipped was compared by checksum
  of the exact header rows that decide it.  `test_skipping_produces_the_same_
  numbers_as_a_full_rebuild` asserts the fast path and a forced rebuild
  produce byte-identical aggregate tables.

### What it cannot see, and what covers that

A fact row edited behind the pipeline's back does not announce itself: the
header is untouched, so the day is skipped.  That is stated as a test
(`test_an_edit_to_a_fact_row_needs_a_forced_rebuild`) rather than left
implicit, and the recovery is `--force`.  `validate` is the check that would
catch it in practice -- it rebuilds a sample from raw and compares, so a
divergence shows up as a mismatch rather than as a wrong number nobody
noticed.

`daily_event` has no window and no watermark: it reads 17,587 rows in 0.9 s
from `auction_events`, which is not worth a mechanism.  That is a measured
decision, not an oversight.

---

## 3. Bounding concurrent large-data operations

Measured: an `export` that takes 312 s alone took **826 s** while two
aggregate builds were running, and Postgres reached **2.68 GB RSS at 89.8%
CPU** on the host -- on a machine someone was using.

`TaskDef.heavy` now marks the tasks whose peak memory is proportional to a
multi-gigabyte table (`aggregates`, `export`, `sentinel`), and
`_concurrent_cycle` serialises them through a semaphore independently of
`--workers`.  Two big scans do not go faster together; they contend for the
same buffers.

* `--heavy-lanes N` (or `PIPELINE_HEAVY_LANES`) widens it when the machine
  allows.
* Light tasks are unaffected: `test_a_light_task_still_runs_while_a_heavy_one_
  is_working` asserts a small task still overlaps a scan.
* The bound is per process.  Two pipeline processes would each get a lane;
  the lease system already prevents them from running the *same* task, and a
  cross-process bound would need a database lock for a case that does not
  occur in this deployment.

Resource limits that already existed and are unchanged: the worker is capped
at 6 CPUs / 8 GB with `nice 10` and BLAS threads at 4; Postgres at 8 CPUs /
6 GB with `shared_buffers=2GB`.  `docker inspect` confirms they are applied
(`NanoCpus=6000000000 Memory=8589934592 Shares=512`).

---

## 4. Measurements

### The reuse path, on the live store

`scripts/aggregates.py build --last-days 2`, source quiet, one run straight
after the other:

```
RUN 1 -- one day had changed (the watermark skipped the other)
aggregates built in 319.9s
  daily_item_price    0 rows written  from            0 source rows  (94 partitions unchanged)
  daily_ah_auction    0 rows written  from   38,937,330 source rows  (92 partitions unchanged)
  daily_event         0 rows written  from       17,587 source rows  (13 partitions unchanged)

RUN 2 -- nothing had changed
aggregates built in 1.1s
  daily_item_price    0 rows written  from            0 source rows  (94 partitions unchanged)
  daily_ah_auction    0 rows written  from            0 source rows  (186 partitions unchanged)
  daily_event         0 rows written  from       17,587 source rows  (13 partitions unchanged)
```

`source_rows` is the evidence: it counts what the stream actually read, so
"skipped" cannot be confused with "read and found equal".  Run 2 read **no
fact rows at all** and finished in 1.1 s against 404.8 s before the change --
a 368× reduction in wall time for the same output.

Note what run 1 shows about the window: `daily_ah_auction` was restricted to
`2026-10-23` alone, because 10-22's header rows had not moved.  The watermark
narrows *within* the window, per day.

### Peak memory and CPU

A forced rebuild of the largest aggregate for one day (38,937,330 source rows
into 900,812 rows), sampled every 4 s with `docker stats`:

| | value |
|---|---|
| elapsed | 376.4 s |
| worker peak CPU | 91.7% **of one core** |
| worker peak RSS | 227.7 MiB |
| Postgres peak | 5.14 GB of its 6 GB cap |
| WSL2 VM | 15.5 GB total, 12.4 GB available, 24 threads visible |

Two things worth naming:

* **The read path is single-threaded, by construction.**  One ordered cursor
  streaming 38.9M rows into a hash-per-partition cannot be split without
  splitting the checksum.  This is the shape the original complaint
  ("it's only using one thread") was about, and the answer is not to
  parallelise it but to stop doing it when nothing changed -- which is what
  the watermark does.  A run that reads nothing uses no core at all.
* **Postgres, not the worker, is the memory.**  5.14 GB against 228 MB: the
  scan reads a 23 GB table and the database's page cache is what fills the
  cap.  That is why the container limits are on Postgres as well, and why
  two of these scans at once (the old `--workers 3` behaviour) is the thing
  the heavy lane prevents.

### Storage overhead

The watermark costs one row per (aggregate, day) *inside the window*: 4 rows,
**32 kB** total, against 2553 MB of aggregate tables and 25 GB of source
tables.  That is 0.001% overhead for the mechanism that removes 404.8 s of
work per run.  Days outside the window keep no state at all, because a
windowed build never claimed anything about them.

### Numerical and semantic equivalence

* Every partition stores `source_rows` and `source_checksum` -- a sha256 of
  the exact fact rows it summarised, computed before any deletion.
* `validate` rebuilds a sample from raw with the same code and compares column
  by column; on the live store it reports PASS with no mismatches and no
  missing rows (see [the run below](#validation-on-the-live-store)).
* Regression tests compare the fast path against a forced rebuild
  (`test_skipping_produces_the_same_numbers_as_a_full_rebuild`) and against
  hand-computed fixtures, so a wrong definition cannot agree with itself.

### Storage format: benchmarked, not assumed

The request was explicit that an encoding must not be assumed to help.  So
`scripts/bench_large_data.py` measures the read shapes on the largest day
(38,937,330 rows over 1,182 snapshots), one process per shape because
`ru_maxrss` is a per-process high-water mark:

| shape | elapsed | peak RSS | note |
|---|---:|---:|---|
| `count` | 14.0 s | 190 MB | the engine merely touching the day |
| `scan_plain` — the 7 columns the aggregate uses, unordered | 131.9 s | 313 MB | |
| `scan` — **the query the aggregate actually runs** | 213.7 s | 313 MB | the `ORDER BY` costs 81.8 s |
| `star` — `SELECT *` in the same order | stopped after >4 min | — | Postgres reached **5.16 GB of its 6 GB limit** |
| `parquet` — write the 7 columns, read 3 back | 176.9 s write, **1.32 s read-back** | **3239 MB** | 335.6 MB on disk, zstd |

What this says, in the order it matters:

* **Column projection is already in place and is not a micro-optimisation.**
  `SELECT *` with the same sort did not finish in four minutes and pushed
  Postgres to 86% of its memory limit; the projection finishes in 213.7 s
  inside 313 MB.  The stream was already projecting, so there was nothing to
  change -- but the difference is now measured rather than assumed.
* **The sort is 38% of the scan** (81.8 s of 213.7 s).  It is not removable:
  the aggregate hashes the *raw* rows into a per-partition checksum, and that
  needs each (day, market) partition to arrive contiguous.  Aggregating in SQL
  would be faster and would destroy the provenance, which is the whole reason
  the aggregates exist.  Recorded as a measured trade, not an oversight.
* **Parquet is not adopted, and the reason is memory, not speed.**  Reading
  three columns of 38.9M rows back from a freshly written local file takes
  1.32 s -- but producing that file took 176.9 s and **3.2 GB of peak RSS**,
  ten times the streaming scan, and it would need a second 22 GB copy of the
  store kept in sync.  The read-back is also from the page cache immediately
  after the write.  It would be a faster way to read a copy whose production
  is exactly the cost being avoided, so the answer is: not worth it here.
  The one thing it does prove is that the per-column compression is real
  (335.6 MB for 38.9M rows x 7 columns); if a future job needed repeated
  columnar scans of closed days, that measurement is the starting point.
* **Chunked reads are already the shape.**  The stream fetches 50,000 rows at
  a time and the 313 MB peak is flat across `scan`, `scan_plain` and the
  production build -- memory does not grow with the day.

---

## 5. Rollback

Everything here is additive.  In order of increasing effort:

1. **Skip the mechanism for one run.**  `scripts/aggregates.py build
   --last-days 2 --force` ignores every watermark and rebuilds the window
   exactly as before.  No state needs clearing: `--force` writes fresh state
   as it goes.
2. **Turn it off for the pipeline.**  Nothing reads the state except the skip
   decision; setting `_WATERMARK_VERSION` to a value that no stored row has
   makes every day look new, and the build falls back to the pre-change
   behaviour without a code change.  (This is the same lever the version bump
   uses, so it is exercised by tests.)
3. **Revert the code.**  `git revert <commit>` restores the previous
   `woweconomy/aggregates.py`.  The `aggregate_source_state` table is then
   ignored, not dropped -- leaving it costs 32 kB and dropping it is not
   necessary.
4. **Revert the resource changes.**  Remove `WORKER_*` / `POSTGRES_*` from
   `.env`; `docker-compose.yml` falls back to its defaults.  `--heavy-lanes`
   defaults to 1; pass `--heavy-lanes 3` to remove the bound without a revert.

The aggregate tables themselves are never rebuilt or discarded by any of
these, and no fact row is touched by any of them.

---

## 6. What was deliberately not changed

* **Retention.**  Untouched.  The aggregates exist so retention can delete
  raw rows; changing the policy was out of scope and would invalidate the
  comparison this document is built on.
* **The raw tables.**  No row was deleted, no table was rewritten, no
  migration was added.
* **Volumes and databases.**  Nothing was reset.
* **`daily_event`.**  17,587 rows and 0.9 s: not worth a window or a
  watermark.
* **The SQLite aggregate file.**  `data/aggregates.sqlite` is a leftover from
  before the Postgres switch; with `DATABASE_URL` set, `_connect` returns the
  Postgres connection and the file is not read.  It is left in place rather
  than deleted, because deleting someone's data file to tidy up is not a
  measurement-driven change.
