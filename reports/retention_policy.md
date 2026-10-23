# Retention policy — approval required before any deletion

**Status: nothing has been deleted.** `--apply` has never been run against a real
database. The CLI is dry-run by default; deletion requires an explicit flag, and
`apply()` refuses tables marked `needed_for_replay` unless an archive directory or
`--accept-replay-loss` is supplied.

This document is the approval artefact. It is meant to be read before the sixth
step, not after it.

## The order that was approved

| # | step | state |
|---|---|---|
| 1 | Aggregates validated | **building** — 163 of ~379 partitions; validation runs on completion |
| 2 | Reproduction test | not yet run against the live databases |
| 3 | External archive, verified | **done** — 2,000 rows, verification PASS, nothing deleted |
| 4 | Comparative dry run | **done** — see below |
| 5 | Limited deletion | implemented and tested, **not run** |
| 6 | Space verification | baseline recorded (`reports/space_baseline.json`), **not run** |

Deletion of `needed_for_replay` tables is withheld until all six are implemented and
approved. Steps 5 and 6 exist as tested code paths; they have not been executed.

1. **Aggregates validated** — a day-grain statistical record, checked against raw.
2. **Reproduction test** — a past evaluation recomputed from the aggregate alone.
3. **External archive, verified** — written and read back, with nothing deleted.
4. **Comparative dry run** — what goes, what survives, what it costs.
5. **Limited deletion** — bounded by table and row count.
6. **Space verification** — measured against a recorded baseline.

### Step 3, as evidence rather than a claim

```
python scripts/retention.py --days 1 --archive-only \
    --archive-dir archives/step3-rehearsal --only-table ah_auction --max-rows 2000
```

```
retention 1d   cutoff 2026-10-21T22:03:07Z
archived (nothing deleted): 38,395 rows (9.33MB)
  intel.sqlite   ah_auction   38,395   9.33MB   0
  limited: 2,000 of 38,395 eligible rows in 1 table(s); rerun to continue
  wrote 2,000 rows to archives\step3-rehearsal
  archive verification: PASS
```

The file is 0.113 MB, its header names `ah_auction`, `intel.sqlite`, the cutoff and all
20 columns. Read back independently: 2,000 data rows, and **all 2,000 distinct
`auction_key` values were still present in `ah_auction` afterwards**. 38,395 rows were
eligible; none was removed.

## Declared policy

`woweconomy/retention.py` declares **41 tables** across both databases: **16 expire**,
**25 are kept regardless of age**. Every expiring table names its authoritative
timestamp, the rule that computes expiry, and why. The declaration is the source of
truth; `python scripts/retention.py --show-policy` prints it.

### Tables that expire

| database | table | timestamp | expires when | protection | replay |
|---|---|---|---|---|---|
| intel.sqlite | `ah_snapshot` | `captured_at` | captured_at < cutoff | the newest scan of each realm is always kept | yes |
| intel.sqlite | `ah_auction` | `ah_snapshot.captured_at` | its scan is expired | rows of a kept scan are kept | yes |
| intel.sqlite | `inventory_snapshot` | `captured_at` | captured_at < cutoff | the newest snapshot of each character is always kept | no |
| intel.sqlite | `inventory_row` | `inventory_snapshot.captured_at` | its snapshot is expired | rows of a kept snapshot are kept | no |
| intel.sqlite | `inventory_posted_listing` | `inventory_snapshot.captured_at` | its snapshot is expired | listings with time_left > 0 are kept: the auction is live | no |
| intel.sqlite | `reconciliation` | `inventory_snapshot.captured_at` | its snapshot is expired | rows of a kept snapshot are kept | no |
| intel.sqlite | `seller_observation` | `observed_at` | observed_at < cutoff | — | yes |
| intel.sqlite | `undercut_observation` | `observed_at` | observed_at < cutoff | — | yes |
| intel.sqlite | `mail_invoice` | `seen_at` | seen_at < cutoff | days_left > 0 is kept: the mail is still in the mailbox | yes |
| intel.sqlite | `sale_buyer_link` | `linked_at` | linked_at < cutoff | kept while the invoice it points at still exists | no |
| tsm_markets.db | `tsm_snapshot` | `updated_at` | updated_at < cutoff | the newest snapshot of each scope and kind is always kept | yes |
| tsm_markets.db | `tsm_price_change` | `tsm_snapshot.updated_at` | its snapshot is expired | rows of a kept snapshot are kept | yes |
| tsm_markets.db | `market_snapshots` | `snapshots.snapshotTimestamp` | its legacy snapshot is expired | rows of a kept legacy snapshot are kept | yes |
| tsm_markets.db | `snapshots` | `snapshotTimestamp` | snapshotTimestamp < cutoff | — | no |
| tsm_markets.db | `auction_events` | `ts` | ts < cutoff | — | yes |
| tsm_markets.db | `auction_intel_events` | `ts` | ts < cutoff | — | yes |

### A correction worth recording

`tsm_realm_price` was in the expiring set above. It is not history and it does not
expire. Measured:

* 1,193,857 rows for 1,193,857 distinct `(scope, item_id)` pairs, enforced by a
  unique index — one row per item, upserted, not appended.
* Its `snapshot_id` records the last *write*, not the last *change*. Traced item
  (`garona`, 30282): `tsm_realm_price.snapshot_id = 1177`, but its only
  `tsm_price_change` row is `snapshot_id = 1`.
* `tsm_price_change` row counts equal `tsm_snapshot.changed` exactly, per snapshot —
  that table is the price history, this one is a mirror of the present.

The old policy would have deleted **29,315 current prices** at `--days 2`. It is now
classified `CURRENT`, excluded from deletion, and absent from the dry run below. This
is why the aggregate is built from `tsm_price_change` and not from the mirror.

### Tables kept regardless of age

Dimensions and current-state tables. Deleting these would not reclaim history, it
would destroy the reference data that makes the history readable.

| database | table | kind | why kept |
|---|---|---|---|
| intel.sqlite | `ah_realm` | dimension | realm identity and connected-realm groups |
| intel.sqlite | `import_log` | dimension | import bookkeeping |
| intel.sqlite | `realm_model_run` | dimension | model provenance; small and auditable |
| intel.sqlite | `mob_observation` | dimension | empty today; not an auction observation |
| intel.sqlite | `mob_loot_observation` | dimension | empty today |
| intel.sqlite | `mob_valuation` | dimension | empty today |
| tsm_markets.db | `tsm_region_price` | current | single current region state |
| tsm_markets.db | `tsm_commodity_price` | current | current commodity prices |
| tsm_markets.db | `tsm_realm_pet_price` | current | current pet prices per realm |
| tsm_markets.db | `tsm_region_pet_price` | current | current pet prices per region |
| tsm_markets.db | **`tsm_realm_price`** | current | **one upserted row per item; the present, not a history** |
| tsm_markets.db | `journal_stats` | dimension | aggregated per-item journal counters, not events |
| tsm_markets.db | `tsm_market` | dimension | item market metadata |
| tsm_markets.db | `tsm_region` | dimension | region metadata |
| tsm_markets.db | `tsm_realm_group` | dimension | realm to connected-group mapping |
| tsm_markets.db | `tsm_meta` | dimension | import metadata |
| tsm_markets.db | `items` | dimension | item catalogue |
| tsm_markets.db | `item_analytics` | dimension | derived analytics, not raw observations |
| tsm_markets.db | `db2_items` | dimension | Blizzard DB2 reference data |
| tsm_markets.db | `db2_item_appearance` | dimension | Blizzard DB2 reference data |
| tsm_markets.db | `db2_meta` | dimension | Blizzard DB2 import metadata |
| tsm_markets.db | `blizzard_items` | dimension | Blizzard item API cache |
| tsm_markets.db | `blizzard_missing` | dimension | items the API could not resolve |
| tsm_markets.db | `journal` | dimension | empty today |
| tsm_markets.db | `sqlite_sequence` | dimension | SQLite internal |

## What is deliberately never deleted

These are the integrity rules that matter more than the window:

- **`mail_invoice` while `days_left > 0`** — the mail is still in the mailbox. An open
  invoice is an open transaction, not old data.
- **`inventory_posted_listing` while `time_left > 0`** — the auction is still live.
- **`sale_buyer_link` while its `mail_invoice` survives** — deleting the link first would
  orphan the attribution.
- **The newest `ah_snapshot` per realm, and the newest `tsm_snapshot` per kind/scope** —
  even when expired. An empty dashboard is not a retention win.
- **Any row whose timestamp will not parse** — `strftime` returns NULL, the comparison is
  false, the row is kept. The failure mode points at keeping data, not losing it.
- **A disappeared auction is never reclassified.** Rows may be deleted; labels are not
  rewritten. A vanished auction stays `CENSORED` and never becomes a confirmed sale or a
  fabricated negative. Labels come from the Journalator archive, not from these tables.

## Dry run — 30 days

Cutoff `2026-09-22T22:04:54Z`. `python scripts/retention.py --days 30`

```
would delete: 62,051 rows (4.50MB)

  database       table                              rows       size  protected
  tsm_markets.db market_snapshots                 62,049     4.50MB          0
  tsm_markets.db snapshots                             2    400.00B          0
```

**Nothing from `intel.sqlite`.** That database holds ~36 hours of scans, so no row in
it is 30 days old. The 62 k rows are legacy whole-region imports from July and October.

This is the uncomfortable finding: at the intended window, the policy deletes almost
nothing. The 30-day window is not what reclaims space today — growth is.

## Dry run — 2 days, to show the policy under load

Cutoff `2026-10-20T22:04:47Z`. `python scripts/retention.py --days 2`

```
would delete: 9,865,833 rows (940.29MB)

  database       table                              rows       size  protected
  tsm_markets.db tsm_price_change              9,724,814   919.08MB          0
  tsm_markets.db market_snapshots                124,336     9.01MB          0
  tsm_markets.db auction_events                    7,665     6.58MB          0
  tsm_markets.db auction_intel_events              7,971     5.32MB          0
  tsm_markets.db tsm_snapshot                      1,043   305.57KB         95
  tsm_markets.db snapshots                             4    800.00B          0
  note tsm_price_change: 187 current snapshots kept
```

The **95 protected `tsm_snapshot` headers** are the protection rule working: older than
two days, but the newest of their scope and kind, so they survive and their price rows
survive with them.

`tsm_realm_price` does not appear. Under the previous policy it would have contributed
29,315 rows to this deletion.

## Why 30 days is not a comfortable number here

Measured on the live databases, not estimated:

| file | size | rows | note |
|---|---|---|---|
| `data/intel.sqlite` | 3.19 GB | 8.3 M auctions / 106 scans | no free pages; all live |
| `tsm_markets.db` | 1.42 GB | 11.1 M price changes | 16 KB free |
| `./dfa.db` | 2.84 GB | — | stray duplicate; no code references it |

`intel.sqlite` gains roughly 70 scans per day at ~74 k auctions each — about 5.2 M rows
and ~2 GB per day. Thirty days of that is ~60 GB. The window is not cosmetic; it is the
only thing standing between this machine and a full disk.

## The exception being asked about

Eleven expiring tables are marked `needed_for_replay`. Because of that, `apply()` raises
`PolicyRefusal` rather than deleting them. The options are not equivalent:

1. **`--archive-dir <path>`** — each expired batch is written as gzipped JSONL with a
   header line recording table, database, cutoff epoch/ISO and columns, *before* the rows
   are deleted. History stays reproducible; disk does not fall unless the archive lives
   elsewhere.
2. **`--accept-replay-loss`** — the rows are deleted outright. Disk genuinely falls. Past
   evaluations can no longer be recomputed from raw data, only cited.
3. **Validated aggregates** — the option that is now implemented. Raw rows may go; the
   day-grain record keeps the statistics, and `price_as_of()` reconstructs a price level
   by carrying the last observation forward, because a day with no row means the price
   held.

Option 3 is what makes deletion defensible. It is also a cost: the aggregate is
~276 MB today and grows with the window.

## Safety properties that are tested, not asserted

**103 tests** across `tests/test_retention.py`, `test_archive.py`, `test_archive_only.py`,
`test_aggregates.py`, `test_rehearsal.py`.

Retention:

- recent data retained; expired data removed; a row exactly on the cutoff is kept
- a `+02:00` timestamp does not shift the boundary
- unparseable and NULL timestamps are kept
- the newest snapshot per realm survives expiry
- an open invoice and a live listing survive
- a buyer link survives exactly as long as its invoice does
- no orphan children are left behind
- `plan()` changes nothing at all
- running cleanup twice is a no-op the second time
- a failure injected halfway rolls the transaction back
- `apply()` refuses replay loss without the flag
- `max_rows` caps a run and repeats until done; `--only-table` stages one table
- the archive holds exactly the rows that were deleted

Archive:

- a flipped byte, a truncated file, a missing file, an orphan file, a surviving `.part`
  and a rewritten row count are each detected
- a foreign format and a newer version are refused
- a second apply is idempotent; two runs merge into one manifest without duplicating

Aggregates:

- hand-computed values match; the closing observation is not the maximum
- NULL is not zero; the UTC day boundary is where it should be
- the current-state table has no aggregate and is not a history source
- a price level is reconstructed by carrying the last observation
- reconstruction matches raw, and still works after the raw rows are gone
- validation catches a tampered row and a missing row; a schema version mismatch is refused

Archive-only:

- deletes nothing at all, checked across every table
- writes the same archive an `apply` would have written, per table
- verifies what it wrote, and fails when an older file in the directory is damaged

## Rollback

The work is on `fix/sentinel-probability`, commits `cb3a2b2` and `2f962f9`, **not
merged**. No database has been modified by any retention work, so there is nothing to
restore — which is why the dry run is the deliverable and not the cleanup.
