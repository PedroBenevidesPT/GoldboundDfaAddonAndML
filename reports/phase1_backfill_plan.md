# Phase 1 — deterministic backfill and rebuild plan

Scope: `data/intel.sqlite` only. Nothing here writes `tsm_markets.db`,
`models/`, `data/_phase3/` or the shipped addon dataset.

## What the plan has to guarantee

| property | how it is achieved |
|---|---|
| raw observations are never modified | `unit_price_copper` and every other API field stay as received; validation writes two *new* columns (`price_status`, `price_reason`) |
| the same input produces the same output | snapshot ids are `sha256(region\|realm\|captured_at\|payload_sha256)`; `variant_key` is a pure function of five stored columns; `mark_outliers` is a pure function of the snapshot's statically valid rows |
| a second run changes nothing | measured: 7,704,472 rows filled, then 0, then 0 (see *Measured run*) |
| a bad run can be undone | the database is copied with the SQLite backup API and the copy is checksummed before the migration |
| the active model is not touched | the backfill never opens `models/` and never promotes an artifact |

## Order of operations

```bash
# 0. a consistent, checksummed copy (SQLite online backup, reads through WAL)
python scripts/snapshot_db.py --db data/intel.sqlite \
    --out data/_phase1/intel.sqlite.premigration

# 1. schema migration + counts only (writes nothing but the schema)
python scripts/backfill_ah_identity.py --dry-run

# 2. fill variant_key, label price_status, flag outliers (idempotent)
python scripts/backfill_ah_identity.py

# 3. prove the fixed point (must report 0 / 0 / 0)
python scripts/backfill_ah_identity.py

# 4. the audit view
python scripts/backfill_ah_identity.py --verify

# 5. the commodity book, through the watcher's own code path
python scripts/watch.py --once          # or wait for the hourly ah task
```

## Why step 2 is a fixed point and not a single pass

`mark_outliers` computes a reference median per **variant** in one
snapshot, then flags rows beyond `max(1000 × median, 10,000 gold)`.

The reference must be computed over every *statically valid* row, not over
the rows that are currently `ok`. The two differ on the second run — the
first run moved some rows to `outlier` — and a median over the survivors is
a different number, so a rule written the naive way keeps finding "a few
more" outliers on every pass. Measured on this database: 1,987 rows on pass
one, 36 more on pass two, and 32,508 rows changed when the rule was fixed.
The final rule reads the full valid pool and can also move a row *back* to
`ok`, so the state is a function of the raw data rather than of the run
order.

A second, related defect was fixed at the same time: the write was keyed on
`(snapshot_id, item_id, price)`, which is not unique across variants. One
variant's verdict could therefore flag another variant's row at the same
price, and that row then left its own reference pool. The write is now keyed
on `auction_key`, which is unique per row.

## Rollback

```bash
# stop the watcher first; it writes intel.sqlite every hour
python -c "import sqlite3;s=sqlite3.connect('data/_phase1/intel.sqlite.premigration');\
d=sqlite3.connect('data/intel.sqlite');s.backup(d)"
```

The pre-migration copy is 2,053,287,936 bytes, sha256
`3834c55b01aa2e6c41207fe660138e8c34bfda9cf5a4bc9eb2755d28232f2a68`.
Restoring it also removes the two derived columns, which the code tolerates:
`connect()` re-adds them, and every consumer reads `price_status` through
`prices.usable_price_sql()`.

## Rebuild (only after the backfill verifies)

The market data and the learning frame are separate stores. The Sentinel
frame is built from `tsm_markets.db` and `dfa.db`; it does **not** read
`ah_auction`. Verified by inspection: no module under `woweconomy/ml/`
imports `woweconomy.intel` or opens `data/intel.sqlite`. A Phase 1
correction therefore does not invalidate a frozen frame, and a Phase 3
retrain is not "rebuilding the dataset" — it is a refit on unchanged
inputs. The only frame-affecting change in this branch is the linking
change (`price_exact`, `price_ambiguous`), which the frame cache from
21:37 already contains.

## Measured run (2026-10-22)

| step | duration | result |
|---|---|---|
| snapshot_db (2.0 GB) | 7.6 s | sha256 above |
| dry run | 26 s | 7,704,472 variant fills, 7,704,255 price labels |
| pass 1 (original rule) | 4 min 31 s | +1,987 outliers |
| pass 2 (same rule) | 41 s | **+36 more** — not a fixed point |
| pass 3 (variant-grouped, keyed write) | 4 min 31 s | 32,508 rows corrected |
| pass 4 (ceiling rule + small-group restore) | 4 min 31 s | 3,705 rows corrected |
| pass 5 | 41 s | 0 changes |
| pass 6 | 41 s | 0 changes |
| verify (realm rows only) | — | ok 7,699,464 · outlier 1,974 · sentinel 2,817 · missing 217 |
| verify (with the commodity snapshot) | — | ok 7,991,162 · outlier 6,511 · sentinel 2,834 · missing 217 |

The database grew from 2.05 GB to 3.22 GB: two indexes on 8.0 M rows plus
the two derived text columns.
