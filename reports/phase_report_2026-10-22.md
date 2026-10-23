# Phased report — market data integrity, labels, controlled comparison

Branch `fix/sentinel-probability`, 2026-10-22. Not merged to `main`.
Everything below was executed against the live stores; every number is
reproducible from the script named beside it.

---

## 0. Where the work resumed from

The working tree already contained Phase 1–3 code from an earlier session
(commodity parser, `prices.py`, `identity.py`, `label_audit.py`,
`select_candidate`, the CI-gated demand-curve claim, and 105 tests covering
them). What had **not** happened was any of it touching the data: the live
`data/intel.sqlite` still had `commodity_count = 0` in all 104 snapshots,
no `price_status` column, no `variant_key`, and `mark_outliers` had never
run. So the phases were resumed at the point where code meets data.

Running it that way found six defects that unit tests on synthetic
fixtures could not: three in the price rules, one in the schema migration,
one in the watcher's command line, and one in the feature layer's read of
a price (a verdict is only worth having if the consumer honours it). A
seventh, in the interval arithmetic, surfaced during Phase 2 and is
recorded in §2.4.

## 1. Verdict

| phase | outcome |
|---|---|
| 1 — market data | **Done.** Commodities are fetched by the watcher and stored; 99.5 % of the items the account sells now have market coverage (was 17 %); every stored price carries a validation status, and the feature layer now reads that status instead of the raw number; the product key is computed for all 8.0 M rows |
| 2 — labels | **Done.** The five horizons are measured, not assumed: 48 h and 168 h are one target (1 disagreement in 3,474) and 168 h is now refused promotion. Matching ambiguity is 3.0 %, not the 78 % previously reported, and the definition behind each number is stated |
| 3 — comparison | **Done, and it does not support a wholesale promotion.** The new pipeline wins clearly on 24 h and on the economics of every horizon, loses on 48 h log loss, and collapses 168 h. The new artifact stays isolated |
| tests | 1143 passed, 8 failed — the same 8 pre-existing addon failures (missing `DFA_Estimate/Localization.lua`), zero regressions |

---

# Phase 1 — market data integrity

## 1.1 Endpoint semantics, and enabling it in the watcher

Inspected live, not from documentation (`_probe_commodities_endpoint.py`):

```
GET /data/wow/auctions/commodities?namespace=dynamic-eu
top-level keys       : ['_links', 'auctions']      <- not 'commodities'
rows                 : 296,252
row keys             : id, item{id}, quantity, time_left, unit_price
buyout present       : no (0 of the sample)        <- unit_price is the price
distinct time_left   : LONG, MEDIUM, SHORT, VERY_LONG
connected realm      : none — one book per region
Last-Modified        : Thu, 22 Oct 2026 20:31:40 GMT
```

The three facts that matter for the implementation: the rows are under
`auctions`, there is no `buyout` so `unit_price` is the only price, and
there is no realm in the path. A commodity auction is one stack at one
unit price, so `quantity` is the stack size and must not be read as a
listing count.

Wiring: `Watch.fetch_commodities()` runs once per tick **after** the realm
loop (one 28 MB download, not ten), under its own state key so a commodity
failure cannot mark the realm snapshots as failed, with `--commodities-since`
for a 304, and `intel.py` refuses to write an empty book (exit 4) because
a 200 with no rows is a failed fetch, not a market with nothing for sale.

**Defect found on the first production run.** The watcher built

```python
[PY, "scripts/intel.py", "ah-fetch", "--region", "eu", ...]
```

but `--region` is a *global* flag of `intel.py` and must precede the
subcommand. The subprocess exited 2 with `unrecognized arguments` and
nothing was fetched. The unit test passed because it used a fake runner,
which accepts any argv. Fixed by moving `--region` before `ah-fetch`, and
by a regression test that parses every recorded argv through the real
parser (`intel.build_parser()`, extracted for exactly this purpose).

Verified end-to-end afterwards (`_fetch_commodities_once.py`, the watcher's
own method):

```
{"returncode": 0,
 "snapshot": {"snapshot_id": "568f84a7bb6ddc0733eacf7a", "inserted": true,
              "auctions": 0, "commodities": 296252, "rejected": 0,
              "unknown_time_left": 0},
 "seconds": 15.4}
```

Stored region-wide (`realm = ''`, `realm_slug = 'commodities'`) so every
existing `WHERE realm = ?` query keeps returning realm rows only — checked,
not assumed: a realm query for the same snapshot returns 0 rows.

Re-verified after the `market_context` change, by running the watcher's
argv verbatim in the foreground rather than waiting for the daemon (which
had died — see §6.9):

```
$ python scripts/intel.py --region eu ah-fetch --realm "" --commodities-only
{"snapshot_id": "2dea68e15953f97d7457a974", "inserted": true, "auctions": 0,
 "commodities": 296252, "rejected": 0, "already_present": false,
 "unknown_time_left": 0}                                               exit 0

$ ... --commodities-since "Thu, 22 Oct 2026 20:31:40 GMT"
commodities: 304 unchanged                                             exit 0
```

The 304 path is the one the watcher uses from its second tick onward, so it
matters that it neither writes a duplicate snapshot nor advances
`last_modified` (verified: still 2 commodity snapshots afterwards). Both
snapshots hold 296,252 rows with `commodity_count > 0` and
`auction_count = 0`, which is the correct shape for a book with no
connected realm.

## 1.2 Regression tests for it

`tests/test_commodity_pipeline.py` (26 tests). The ones that pin the
requirements:

| test | what it proves |
|---|---|
| `test_the_commodity_payload_is_wrapped_in_auctions_and_reads_item_id` | the payload shape, from a verbatim live excerpt |
| `test_every_commodity_row_is_marked_commodity_and_keeps_its_unit_price` | rows are commodities and the unit price is preserved |
| `test_the_commodity_book_is_stored_region_wide_not_against_a_realm` | `commodity_count > 0` and no connected realm |
| `test_a_commodity_snapshot_never_appears_in_a_realm_query` | realm scoping holds |
| `test_the_watcher_requests_the_commodity_book_once_per_tick` | the watcher asks for it, once, with an empty realm |
| `test_every_argv_the_watcher_builds_parses` | the argv is valid for the real parser |
| `test_an_empty_commodity_book_is_rejected_by_the_cli` | an empty book is a failure, not a market |

## 1.3 Safe price validation

`woweconomy/intel/prices.py`. The raw observation is never modified: the
API's value stays in `unit_price_copper`, and two new columns
(`price_status`, `price_reason`) say whether it may be *used*.

| status | rule | rows (all) | of which realm snapshots |
|---|---|---|---|
| `ok` | positive, below the ceiling | 8,282,860 | 7,699,464 |
| `sentinel` | `>= 99,999,999,000` | 2,851 | 2,817 |
| `impossible` | `<= 0`, or above the gold cap | 0 | 0 |
| `outlier` | `> max(1000 × variant median, 10,000 g)` in the same snapshot | 11,048 | 1,974 |
| `missing` | the API sent no price — stored NULL, never zero | 217 | 217 |

Measured with two commodity snapshots in the book. The realm column is the
stable part — it moves only when a realm is scanned — while the commodity
column grows by one book per hourly fetch, so the left-hand totals are a
snapshot of a growing number and the right-hand ones are the ones to
compare against a future run.

Two rules were wrong as written and are now measured rather than assumed:

**The sentinel was an exact match.** No row equals `99,999,999,000` —
not one, in 8.3 M rows — but **2,749 rows equal `99,999,999,900`**, which
is 9,999,999 g 99 s, the maximum buyout the client accepts. Those are items
parked at the ceiling. An `==` rule therefore classified *nothing* as a
sentinel while letting every parked row through as an ordinary price. The
rule is now `>=`, which is also what
`woweconomy/analytics/reference_price.py` has always used. This corrects
the earlier audit's claim that 2,749 rows *equalled* the sentinel: they
equal the ceiling above it.

**Outliers were grouped by `item_id`.** Item 82800 (the caged-pet
container) holds 1,014 distinct variants in one snapshot; a median over
that mixture describes no good that exists. Grouping is now by
`variant_key`, with `item_id` only as a fallback for un-migrated rows.

**The rule was not idempotent.** Measured: pass 1 flagged 1,987 rows, pass
2 flagged **36 more**, pass 3 more again. Two causes, both fixed:

1. the reference median was computed over the rows still `ok`, so flagging
   a row changed the median that judged the rest. It is now computed over
   every statically valid row, which makes the verdict a function of the
   raw data — and lets a row return to `ok` if the pool says it is ordinary;
2. the write was keyed on `(snapshot_id, item_id, price)`, which is not
   unique across variants, so one variant's verdict flagged another
   variant's row at the same price. That row then left its own reference
   pool — which is how a single pass started finding "a few more" on every
   run. The write is now keyed on `auction_key`.

Confirmed on live data after the fix, three consecutive passes over the
newest commodity snapshot (296,252 rows, already marked at ingest time):

```
snapshot 2dea68e15953f97d7457a974
before    {'ok': 291698, 'outlier': 4537, 'sentinel': 17}
pass 1: changed=0   pass 2: changed=0   pass 3: changed=0
```

A fixed point on the first re-run, which is what "deterministic" has to
mean here: the verdict is a function of the raw rows, not of how many times
the job has run.

Measured effect on the aggregates (the reason this matters):

| item | mean of all rows | mean of `ok` rows | inflation removed |
|---|---|---|---|
| 20748 | 99,999,999,900 | *(no usable price)* | infinite — every listing is parked |
| 20749 | 99,999,999,900 | *(no usable price)* | infinite |
| 173144 | 29,403,240,992 | 24,465,546 | 1,202× |

The last consumer that still trusted a raw minimum was
`woweconomy/ml/market_context.py`, which is what the model actually reads;
it now takes the cheapest `ok` row and reports `None` when there is none
(§6.4). A verdict nobody acts on would not have removed the inflation above
— the 1,202× case was reachable through that function until it was changed.

## 1.4 Product identity

`auction_key` was inspected before being relied on: it is
`str(entry["id"])`, the Blizzard auction id. It is unique per auction and
stable across snapshots, which makes it right for "did this listing
disappear" and wrong for "which good is this" — it changes on every
relist, and a single product has hundreds of live keys at once (296,252
commodity rows for 11,711 items).

`item_id` is not a product id either. The measured pooling, one realm
snapshot (`_measure_phase1.py`):

```
rows 50,291 · items 13,077 · variants 32,111
items with more than one variant: 7,071
worst: 82800 → 1,014 variants in one snapshot
       271438 / 271440 / 271638 → 45–46 variants
```

`variant_key` = item + context + bonus lists + modifiers + pet species,
hashed to a fixed width so it can be indexed. Backfilled on all 8.0 M rows
(0 rows without it). The commodity book is 11,711 items and 11,711
variants — commodities have no modifiers, so pooling there is zero, as it
should be.

## 1.5 Deterministic backfill, and what it must not touch

Full plan in `reports/phase1_backfill_plan.md`. Summary:

```bash
python scripts/snapshot_db.py --db data/intel.sqlite \
    --out data/_phase1/intel.sqlite.premigration     # 7.6 s, sha256 recorded
python scripts/backfill_ah_identity.py --dry-run      # 26 s
python scripts/backfill_ah_identity.py                # 4 min 31 s
python scripts/backfill_ah_identity.py                # must report 0 / 0 / 0
python scripts/backfill_ah_identity.py --verify
```

Pre-migration copy: 2,053,287,936 bytes,
sha256 `3834c55b01aa2e6c41207fe660138e8c34bfda9cf5a4bc9eb2755d28232f2a68`.
The database grew 2.05 → 3.22 GB (two indexes on 8.0 M rows).

**Migration defect found here.** `store.SCHEMA` created
`idx_ah_auction_variant` immediately after `CREATE TABLE IF NOT EXISTS
ah_auction`. On a fresh database that works; on the live 2 GB database the
table already exists without the new column, so the index raised
`no such column: variant_key` and the migration could never run. Fixed by
moving both indexes into `_ensure_ah_indexes()`, called after the additive
migration, and pinned by
`test_a_legacy_database_is_migrated_without_losing_a_row`, which builds a
pre-migration schema, migrates it, and asserts the raw price survives, the
status defaults to `missing` (not `ok`) and the variant key is NULL (not
invented).

Coverage of what the account actually sells (`_measure_phase1.py`):

| measure | before | after |
|---|---|---|
| items sold with market coverage | 37 of 216 (17.1 %) | 215 of 216 (99.5 %) |
| sale events with market coverage | 43 of 557 (7.7 %) | 557 of 557 (100 %) |

Every top seller is a commodity and was invisible before: Light Leather
(14 sales), Medium Leather (13), Small Spider Leg (13), Wool Cloth (12),
Linen Cloth (11), Briarthorn (10) — all now present. The single uncovered
item is the account's top seller by count (20 sales, "Inlaid Greatsword of
the Harmonious") and it has **`item_id = NULL` in the journal archive**, so
it cannot be joined to any market. That is a journal defect, not a market
gap, and it is left as a known issue rather than patched by guessing an id.

---

# Phase 2 — label and evaluation validity

## 2.1 All sale horizons, measured exactly

From the isolated Sentinel report (`_measure_phase2.py`), exact row-by-row
agreement on the 3,474 rows every horizon can label:

| pair | agreement | disagreements |
|---|---|---|
| 24 h ↔ 48 h | 0.996834 | 11 |
| 24 h ↔ 168 h | 0.996546 | 12 |
| 48 h ↔ 168 h | **0.999712** | **1** |

- identical group: `[[48, 168]]`
- degenerate map: `{168: 48}` → `sale_168h` is reported `degenerate`, with
  the reason "the labels are identical to the 48h target; this is the same
  question, not an independent model"
- closure: observation window 157.1 h, maximum auction 48 h → 72, 168 and
  336 h are beyond the auction; 168 and 336 h are beyond the window

So the honest position is narrower than the earlier audit's: 24 h and 48 h
are *nearly* the same question (11 rows differ in 3,474), and 48 h and
168 h are *the same* question to within one row. Only 24 h carries
information the others do not, and 168 h must not ship as an independent
model — which it now does not.

## 2.2 Ambiguous matching and censoring

527 matched sales in the frame:

| measure | value | definition |
|---|---|---|
| price identified exactly | 524 (99.4 %) | exactly one outstanding posting had that total |
| price ambiguous | 2 | two or more postings shared the total |
| >1 candidate listing | 16 (3.0 %) | more than one posting by the same character, same item and same stack size existed at the sale time |
| median elapsed, unique candidate | 5.7 h | |
| median elapsed, several candidates | 6.4 h | |
| elapsed < 1 h | 0.4 % | |

**This corrects the earlier audit's "381 of 489 matches (78 %) are
ambiguous".** That figure counted candidates at item level; the matcher's
own unit is `(realm, character, item, quantity)`, and on that definition
ambiguity is 3.0 %. Both numbers can be true of different questions, but
only the second describes what the matcher actually resolves, so it is the
one reported here. The audit's directional worry — that `candidates[-1]`
compresses elapsed time — is **not** supported by the current data: the
ambiguous group is slower (6.4 h vs 5.7 h), not faster.

The audit's `ambiguous=1` flag is now split into `price_exact` and
`price_ambiguous`, and both are blocklisted from features
(`tests/test_feature_leakage.py`) because `price_exact == 1` is
single-feature AUC 0.997 against the sale label — the label wearing a
feature's clothes.

Censoring, at row level: expired 2,947 · censored 1,185 · sale 527 ·
cancelled 276. `label_known` is 3,474 (70.4 %) for every horizon. A
censored listing is excluded from both classes; it is never counted as a
non-sale, and in the economic table (§3.3) `realised_gold()` **raises** on
a censored row rather than valuing it at zero.

## 2.3 Deterministic selection and promotion

`select_candidate` (in `sentinel.py`) has explicit eligibility and
tie-breaking:

1. a candidate whose primary metric is not finite is ineligible;
2. a candidate must beat the baseline by `min_gain` to be eligible at all;
3. lowest primary metric wins;
4. ties within `1e-9` break on the secondary metric, then on name, so the
   answer never depends on dict order or on which thread finished first;
5. the choice is made on the **calibration block**, never the test block,
   and the held-out block is used only to confirm.

Every probability target records the audit trail — `selection_block =
calibration`, `selection_metric = logloss`, `selection_value`,
`selection_baseline_value`, `selection_margin`, the full per-candidate
`selection_scores`, and `test_confirmation` — so a promotion can be
re-derived without refitting. Measured for all seven probability targets:

```
sale_24h    validated   catboost  0.1515 vs baseline 0.2193  margin 0.0678  confirmed True
sale_48h    validated   catboost  0.1593 vs baseline 0.2250  margin 0.0657  confirmed True
sale_168h   degenerate  catboost  0.1472 vs baseline 0.2250  margin 0.0778  (not promotable)
undercut_pressure_24h  validated 0.5440 vs 0.5693  margin 0.0253  confirmed True
undercut_pressure_48h  validated 0.6270 vs 0.6696  margin 0.0426  confirmed True
price_decline_24h      validated 0.2958 vs 0.6563  margin 0.3605  confirmed True
price_decline_48h      validated 0.4123 vs 0.6869  margin 0.2746  confirmed True
```

The 168 h entry is the visible proof that the gate now discriminates: it
has the *best* margin of the three sale targets and is still refused,
because the question is not distinct.

## 2.4 The demand-curve claim

The claim is now gated on an interval and on out-of-sample curve fitting
(`_bootstrap_ratio`, `evaluate_strategy`). First isolated run:

```
improvement: null            <- withheld
improvement_point_estimate: 3.23
improvement_ci: [nan, nan]   <- and this is why it was withheld
status: insufficient_evidence
```

The NaN was a defect, not a finding: `sale_rate()` returns NaN where the
curve cannot be interpolated, pandas' `.mean()` skips NaN while numpy's
does not, so the published ratio was computed over more rows than the
interval — and the interval, being numpy, was not a number. Fixed with
`_finite_pairs()`, used by both the point estimate and the bootstrap
(`test_a_row_that_cannot_be_valued_does_not_poison_the_interval`).

After the fix, same data:

```
listings 3,597 · folds 4 · curve_source ['fold_train']
improvement 3.2289 · 95% CI [3.1162, 3.3407]
status supported
```

The claim is therefore published — with its uncertainty, on an
out-of-sample curve, and no longer with the circular
`share_in_the_optimal_band` figure. Two caveats are stated in the report
text rather than buried: the comparison baseline is the account's own
asking behaviour (median 4.00× the regional sale average), not a simple
"price at 1× reference" rule; and the bootstrap resamples listings, not
items.

---

# Phase 3 — controlled comparison

## 3.1 Dataset rebuild

The frame is built from `tsm_markets.db` and `dfa.db`; no module under
`woweconomy/ml/` imports `woweconomy.intel` or opens `data/intel.sqlite`
(checked by inspection). Phase 1's corrections therefore do **not** change
this frame — verified rather than assumed — and the only frame-affecting
change on the branch is the linking change. A fresh frame was frozen
anyway, so the comparison rests on bytes that cannot move:

```
data/_phase3/frame_v2.joblib  sha256 7da3ee39048a…
4,935 rows · 931 items · 10 realms · ts 1792134307–1792699877
retention_days 30 · rows dropped before cutoff 0
```

## 3.2 Retrain, isolated

```bash
python scripts/dfa_ml.py sentinel \
    --sentinel-dir data/_phase3/dfa_sentinel_v2 \
    --sentinel-reports-dir data/_phase3/reports_v2 \
    --frame-cache data/_phase3/frame_v2.joblib
# 58 s → data/_phase3/dfa_sentinel_v2/20261022T211255Z-3b8c4ced
```

`models/dfa_sentinel/CURRENT` still points at `20261022T195801Z-4e9a79ed`,
and `models/dfa_ml/*.cbm` still carry their 2026-10-21 20:58 timestamps.
Nothing active was overwritten.

## 3.3 Old vs new on identical windows

`scripts/compare_sentinel_models.py`, one frozen frame, one split, one test
block (813 rows) shared by every model — including the baselines.

**sale_24h** — 171 positives

| model | log loss | Brier | ECE | AUC | AP |
|---|---|---|---|---|---|
| item_rate_baseline | 0.3999 | 0.1264 | 0.1039 | 0.8725 | 0.7287 |
| old | 0.4001 | 0.1263 | 0.1085 | 0.8731 | 0.7291 |
| **new** | **0.3229** | **0.0844** | **0.0434** | **0.9053** | **0.7323** |

**sale_48h** — 180 positives

| model | log loss | Brier | ECE | AUC | AP |
|---|---|---|---|---|---|
| item_rate_baseline | 0.4202 | 0.1342 | 0.0981 | 0.8594 | 0.7186 |
| **old** | **0.3613** | 0.0980 | **0.0416** | 0.8795 | 0.6386 |
| new | 0.3808 | **0.0972** | 0.0615 | **0.8874** | **0.7064** |

**sale_168h** — 180 positives (new artifact: `degenerate`)

| model | log loss | Brier | ECE | AUC | AP |
|---|---|---|---|---|---|
| old | 0.4200 | 0.1340 | 0.1029 | 0.8608 | 0.7195 |
| new | **0.3725** | **0.0967** | **0.0601** | **0.8881** | 0.7157 |

The 48 h row is the interesting one and the reason no metric was changed:
log loss and ECE prefer the incumbent, Brier, AUC and AP prefer the new
model, and the economics (§3.4) prefer the new model decisively. Under the
project's stated rule — do not change the global selection metric without
evidence — log loss stays the selection metric and the disagreement is
reported instead of resolved by preference.

## 3.4 Economic outcomes, reported separately

Realised net gold on the same test listings, final outcomes only, per
posting policy (`realised_gold()` refuses censored rows):

| target | policy | old: gold/post | new: gold/post |
|---|---|---|---|
| 24 h | p ≥ 0.5 | 0 (never clears 0.5) | **29** |
| 24 h | top 20 % | 20 | **27** |
| 48 h | p ≥ 0.5 | 21 | **30** |
| 48 h | top 20 % | 18 | **28** |
| 168 h | top 20 % | 20 | **29** |

On every horizon the new model earns more per listing posted. The
`p ≥ 0.5` row for the old 24 h model is zero because its baseline
probabilities never exceed 0.5 — the policy posts nothing, which is a
correct report of a model that cannot be used at that threshold, not a
missing value.

## 3.5 Status: baseline-only is kept where it belongs

No target is `baseline_only` in the new artifact: each probability target
beats the item-rate baseline on log loss by a margin the held-out block
confirms. `sale_168h` is `degenerate`, which is stricter than
baseline-only — it is excluded from `PROBABILITY_STATUSES`, so it cannot
export a probability at all.

---

## 4. Tests

```
python -m pytest tests -q
8 failed, 1143 passed in 21.55s
```

The 8 are the pre-existing addon failures and are environmental:
`DFA_Estimate/Localization.lua` does not exist in the tree, so the lupa
harnesses fail while loading the addon
(`test_auction_intel_addon` ×1, `test_dfa_estimate_addon` ×5,
`test_dfa_pricing` ×2). The same 8 failed before this session's changes
(8 failed / 995 passed at the start of it); no test that passed at the
start fails now. The set was diffed against a saved baseline run and is
identical, name for name.

Targeted runs for the changed areas:

```
tests/test_commodity_pipeline.py        26 passed
tests/test_economic_comparison.py        7 passed
tests/test_market_context.py            15 passed
tests/test_realm_index.py                9 passed
tests/test_strategy_value.py             7 passed
(plus label_audit, calibration_selection, selection, feature_leakage,
 sentinel_probability, tooltip_status → 105 passed together)
```

Three test changes were not cosmetic:

- `tests/test_realm_index.py` — the fixture now writes `price_status`, because
  a row whose price status is unknown is no longer evidence; two tests were
  added asserting that sentinel and unlabelled rows are excluded from the
  babysit risk.
- `tests/test_economic_comparison.py` — new, 7 tests, including that a
  censored listing raises rather than being valued at zero.
- `tests/test_market_context.py` — the inline `ah_auction` schema gained
  `price_status` (default `'missing'`, as in production) and its fixture
  rows state `'ok'` explicitly. Without that every cheapest ask in the file
  would silently become `NULL`, which is why the fixture is worth a comment.
  Three tests added; two of them were confirmed to fail against the
  pre-fix query (`cheapest_ask` came back as `100000000001`) and pass after
  it.

## 5. Changed files

| file | change |
|---|---|
| `woweconomy/intel/prices.py` | sentinel is `>=` (measured: 2,749 rows at the ceiling); outliers grouped by variant, reference pool = statically valid rows, write keyed on `auction_key`; stale flags cleared when a group is too small to judge |
| `woweconomy/intel/store.py` | indexes moved out of `SCHEMA` into `_ensure_ah_indexes()`, called after the additive migration |
| `woweconomy/intel/ah.py` | commodity endpoint, `parse_commodities`, region-wide storage, price status and variant key at parse time |
| `woweconomy/intel/identity.py` | `variant_key`, `variant_concentration` |
| `scripts/watch.py` | `fetch_commodities()` once per tick; `--region` moved before the subcommand |
| `scripts/intel.py` | `build_parser()` extracted; `--commodities` / `--commodities-only` / `--commodities-since`; empty book refused |
| `scripts/backfill_ah_identity.py` | deterministic, idempotent backfill of `variant_key` / `price_status` |
| `scripts/snapshot_db.py` | **new** — checksummed pre-migration copy via the SQLite backup API |
| `scripts/compare_sentinel_models.py` | `realised_gold()`, `economics()`, per-policy gold table |
| `woweconomy/ml/pipeline.py` | `_finite_pairs()` so the point estimate and its interval use the same rows |
| `woweconomy/ml/linking.py` | `price_exact` / `price_ambiguous` split |
| `woweconomy/ml/models.py` | both matching columns blocklisted from features |
| `woweconomy/ml/label_audit.py` | exact horizon agreement, degenerate map, closure |
| `woweconomy/ml/sentinel.py` | `select_candidate`, cross-fitted calibration, calibrator floor, degenerate gate, selection audit trail |
| `woweconomy/ml/report.py` | demand-curve claim only with an interval; no `share_in_the_optimal_band` |
| `woweconomy/ml/dataset.py`, `config.py` | retention window, matching columns in the record |
| `woweconomy/pricing/realm_index.py` | babysit risk reads only `price_status = 'ok'` |
| `woweconomy/ml/market_context.py` | `_snapshot_supply` takes the cheapest *usable* ask (`price_status='ok'`) and returns `None` when there is none, instead of the cheapest raw row; `cheapest_change_pct` suppressed unless both snapshots have a usable price |
| `tests/*` | 6 new commodity tests, 7 new economic tests, 3 new market-context tests, 2 new realm-index tests, 1 new bootstrap test, 2 fixture fixes |
| `reports/phase1_backfill_plan.md` | **new** — the deterministic plan, rollback, measured timings |
| `_measure_*.py`, `_probe_*.py`, `_fetch_commodities_once.py`, `_train_isolated.py` | **new** — the read-only measurement scripts every number here comes from |

## 6. Remaining risks

1. **Commodity history is two snapshots, and they are the same book.**
   296,252 rows each, taken minutes apart, with an identical
   `Last-Modified`, so the second fetch adds no new information — it exists
   because the API republished nothing and the first request was not
   conditional. Price-path, trend and volatility features for commodities
   need days of genuinely hourly fetches before they mean anything. Nothing
   in the model uses them yet, which is why this is a gap and not a defect.
2. **The commodity book is region-wide.** It cannot answer "what does this
   sell for on Dragonmaw" because the API does not distinguish realms.
   Any realm-level commodity price is a modelling assumption, not data.
3. **Nothing consumes the commodity rows yet.** `market_context` still
   reads realm snapshots only, so the newly collected data is validated
   but inert for pricing and ML. Wiring it changes feature values for
   11,711 items and needs its own comparison; doing it under this
   time budget would have been an untested feature path.
4. **Variant pooling remains in the feature layer — the price half is
   now closed, the grouping half is not.** `_snapshot_supply` no longer
   takes `MIN(unit_price_copper)` over every row: it takes the cheapest row
   whose `price_status` is `'ok'`, so a listing parked at the no-data
   ceiling can never become the cheapest ask. An item whose only listings
   are parked reports `cheapest_ask = None` — unknown — instead of
   9,999,999 g, and `cheapest_change_pct` is left `None` rather than
   computed from a sentinel. Listing and unit counts still include every
   row, because a parked listing is still an item someone is trying to
   sell. What is *not* fixed is the aggregation grain: it still groups by
   `item_id`, so "cheapest ask" for item 82800 is the cheapest of 1,014
   different pets. The identity key exists and is backfilled; switching the
   `GROUP BY` to `variant_key` changes feature values for every
   battle-pet and modifier-bearing item and needs its own before/after
   comparison, which is a Phase 3-sized job, not a patch.
5. **The improvement interval is a naive bootstrap over listings.** It is
   not clustered by item (931 items, top item 0.5 % of rows, so the
   distortion is modest but real). A cluster bootstrap by item is the
   rigorous version.
6. **The demand-curve comparison baseline is the account's own asks**
   (median 4.00× the regional sale average). The 3.23× improvement is
   mostly "stop asking 4× the reference", not evidence that the curve
   beats a simple 1× rule.
7. **48 h log loss regressed** against the incumbent artifact. Unexplained
   beyond "different model class, different calibration block"; it is the
   reason promotion is withheld.
8. **`item_id` is NULL for 20 sale events**, including the top seller by
   count. Those rows cannot be joined to any market and are excluded from
   nothing — they are simply unpriced.
9. **The watcher does not survive the shell that starts it.** It died at
   22:07:45 and again at 22:29:23 — both times immediately after a normal
   `tick: changed=[] tasks=[]` line, with no exception and no traceback in
   the log. The log records 12 starts over two days, which is the signature
   of a process being reclaimed when its launching shell exits, not of a
   crash inside the daemon. Two consequences. First, the first hourly `ah`
   tick that would have carried the commodity fix (due 22:43:45) never
   ran, so the production path was verified by executing the watcher's
   exact argv in the foreground instead — see §1.1 — which is a stronger
   check anyway because the result is observable rather than inferred from
   a log line. Second, nobody should treat the current collection as
   continuous: the daemon needs a supervisor (Task Scheduler or a service),
   not a detached `Start-Process`, before commodity history accumulates
   unattended.
10. **Another process edits this working tree.** `scripts/aggregates.py`
    ran at 22:18:30 and `woweconomy/retention.py` changed during this
    session. Mid-write test runs can be inconsistent; the 1143-pass figure
    is from a run after the tree was quiet, and its 8 failures were diffed
    against a saved baseline and match name for name.
11. **The 8 addon test failures** are environmental and predate the branch.
12. **Commodity storage is bounded by retention, not by the API.** Each
    stored book is ~296k rows. `make_snapshot_id` includes `captured_at`,
    so a re-fetch of an unchanged book at a new time is a *distinct*
    snapshot rather than a duplicate to be rejected — deliberate for realm
    scans, where a repeated observation is itself the evidence, but it
    means the only thing preventing 28 MB per hour is the 304. That works
    today (verified, §1.1); if the endpoint ever stops honouring
    `If-Modified-Since`, every tick stores a full book.
    `woweconomy/retention.py` does cover this — `ah_snapshot` is grouped by
    `realm`, commodities are `realm = ''`, so the newest book is protected
    and older ones expire on the normal cutoff — but the same cutoff that
    bounds the disk also bounds how much commodity *history* will ever be
    available for the trend features in §6.1. Anyone planning to use those
    features should size the retention window for it deliberately rather
    than inherit the auction-scan default.

## 7. Precise conditions for promotion

The new artifact (`data/_phase3/dfa_sentinel_v2/20261022T211255Z-3b8c4ced`)
may replace `models/dfa_sentinel/20261022T195801Z-4e9a79ed` only when **all**
of the following hold:

1. **Same data.** Both artifacts scored on one frozen frame whose sha256
   sidecar verifies, with the same split and the same test rows.
2. **No regression on the selection metric.** For every non-degenerate
   probability target, `new log loss <= incumbent log loss + 1e-9` on the
   held-out block.
3. **A real gain somewhere.** For at least one target, Brier, average
   precision or gold-per-post strictly better, and none of them worse by
   more than 1 %.
4. **Confirmation.** `test_confirmation == True` for every target promoted.
5. **No degenerate export.** No target in `degenerate` or
   `validated_unconfirmed` may carry a probability into the addon dataset.
6. **Data prerequisites.** `backfill_ah_identity.py --verify` reports
   `rows_without_variant_key = 0`, `usable_share >= 0.999`, and the latest
   snapshot has `commodity_count > 0`.
7. **Atomic, checksummed promotion** with the previous `CURRENT` retained
   for rollback.

Current standing against those conditions:

| target | condition 2 | condition 3 | verdict |
|---|---|---|---|
| sale_24h | 0.3229 ≤ 0.4001 ✓ | Brier, AP, gold/post all better ✓ | **promotable** |
| sale_48h | 0.3808 > 0.3613 ✗ | Brier, AUC, AP, gold/post better | **blocked** |
| sale_168h | n/a | n/a | excluded (`degenerate`) |
| risk targets | not compared to the incumbent | — | unverified |

Because the artifact promotes as a unit, the whole artifact is **not
promoted**. The honest next step is per-target promotion — 24 h from the
new artifact, 48 h from the incumbent — which is a change to the promotion
mechanism, not a judgement call, and is therefore left undone rather than
improvised.

## 8. Rollback

```bash
# stop the watcher first; it writes intel.sqlite every hour
python -c "import sqlite3;s=sqlite3.connect('data/_phase1/intel.sqlite.premigration');\
d=sqlite3.connect('data/intel.sqlite');s.backup(d)"
```

`models/dfa_sentinel/CURRENT` was never moved, so the model rollback is a
no-op. To abandon the isolated run: delete `data/_phase3/dfa_sentinel_v2`
and `data/_phase3/reports_v2`.

## 9. Exact commands used

```bash
PY="C:/Users/petru/AppData/Local/Microsoft/WindowsApps/python.exe"

$PY _probe_commodities_endpoint.py          # live endpoint semantics
$PY scripts/snapshot_db.py --db data/intel.sqlite --out data/_phase1/intel.sqlite.premigration
$PY scripts/backfill_ah_identity.py --dry-run
$PY scripts/backfill_ah_identity.py         # twice, second must be 0/0/0
$PY scripts/backfill_ah_identity.py --verify
$PY _fetch_commodities_once.py --region eu  # the watcher's own fetch path
$PY _measure_phase1.py
$PY scripts/dfa_ml.py sentinel --sentinel-dir data/_phase3/dfa_sentinel_v2 \
    --sentinel-reports-dir data/_phase3/reports_v2 \
    --frame-cache data/_phase3/frame_v2.joblib
$PY scripts/compare_sentinel_models.py \
    --old models/dfa_sentinel/20261022T195801Z-4e9a79ed \
    --new data/_phase3/dfa_sentinel_v2/20261022T211255Z-3b8c4ced \
    --frame-cache data/_phase3/frame_v2.joblib \
    --out data/_phase3/comparison_v2.json --markdown data/_phase3/comparison_v2.md
$PY _train_isolated.py                      # legacy train → data/_phase3
$PY _measure_phase2.py
$PY -m pytest tests -q
```
