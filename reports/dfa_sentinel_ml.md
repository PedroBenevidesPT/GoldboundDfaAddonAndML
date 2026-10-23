# DFA Sentinel — validated ML engine (21 October 2026)

This is the audit and delivery note for the prediction layer that feeds
DFA Estimate. It records what already existed, what was actually trained,
what was measured on a chronological holdout, and what is still not
supported by the data.

## 1. What existed before the change

The repository already had a substantial **CatBoost** pipeline, not a
heuristic-only estimator:

| Component | Before | What it really was |
|---|---|---|
| `woweconomy/ml/dataset.py` | listing-level labels | sales, expiries, cancellations and censored listings were already separated |
| `woweconomy/ml/models.py` | price / sale / time-to-sale | three CatBoost models, with a survival model for censored durations |
| `woweconomy/ml/pipeline.py` | walk-forward validation | expanding chronological folds, untouched test window |
| `scripts/dfa_ml.py` | `audit`, `train`, `recommend` | real training and reporting |
| `woweconomy/intel/realm_ml.py` | one price model per realm | per-realm CatBoost, guarded by a no-gain fallback to the regional model |
| `woweconomy/pricing/engine.py` | per-realm estimate | existing price model, strategy, confidence; some sale/undercut values were heuristic or measured shares |
| `DFA_Estimate/Data/*.lua` | game dataset | 237 records, 31 184 regional prices, schema 1 at the start |
| `DFA_AuctionIntelligence` | event capture | genuine event store; it was failing to load because of a client event name |

The genuine gaps were:

1. **No probability calibration.** CatBoost scores were exported as if they
   were probabilities; no isotonic/Platt step existed.
2. **No simple baseline in the exported path.** The walk-forward report had
   reference baselines, but the number the game received was never compared
   with an item sale rate or a median-price baseline.
3. **No market risk targets.** Undercut and price-decline risk were not
   trained; the realm index measured shares from scans, not a temporal
   probability.
4. **No artifact lifecycle.** Models were overwritten in place; there was no
   `CURRENT`, schema check, checksum, or atomic promotion.
5. **The 48-hour horizon was not in the dataset at all** — `DEFAULT_HORIZONS_H`
   was `(24, 72, 168, 336)`.
6. **No explicit `unknown` contract.** A missing model or a sparse realm had
   no stable status/fallback field in the exported record.

## 2. What was implemented

### 2.1 `woweconomy/ml/sentinel.py`

A new layer on top of the existing feature frame, not a replacement:

* `chronological_split()` cuts **train / calibration / untouched test** by
  unique timestamp and asserts that no later timestamp can enter an earlier
  block.
* `fit_calibrator()` fits **isotonic** calibration, falling back to **Platt**
  scaling, and to identity with an explicit note when there is too little
  data. Calibration is fitted only on the middle block.
* **scikit-learn** is used for the calibrated logistic baseline, the Ridge
  price baseline, the imputer/scaler, and the calibrators. **CatBoost**
  remains the non-linear candidate. Both are trained; the winner is selected
  on the untouched test block.
* `woweconomy/ml/intervals.py` implements a **split-conformal** interval on
  the log scale. The width comes from the absolute residuals of the
  calibration block; the coverage is measured on the later test block. The
  two are exported separately, so the tooltip can print "80% range" beside
  "77% of 103 sales" without conflating the promise with the result.
* Sale selection uses **log loss** as the primary metric because it is
  strictly proper and punishes overconfident wrong probabilities; Brier,
  AUC, ECE, precision/recall and top-decile lift are reported beside it.
  The improvement test still requires a Brier gain over the item-rate
  baseline.
* Price selection uses **MAE on the log scale** against an item-median
  baseline; MAE in gold, SMAPE, median APE and P90 APE are reported.
* Time-to-sale uses the existing censoring-aware CatBoost `SurvivalAft`
  model and reports Harrell's C-index. It is exported **only** when the
  C-index clears 0.60.
* Risk targets are built from timestamped `tsm_snapshot` +
  `tsm_price_change` rows:
  * `undercut_pressure`: the cheapest listing observed at or after
    `t + horizon` is lower than at `t`;
  * `price_decline`: the market value at that later snapshot is at least 5 %
    lower.
  Both are **market-level** measurements. They do not claim that one of our
  listings was the one undercut.
* Per-realm coverage is recorded as evidence (`pooled_realm`), never as a
  pretend independent model. A sparse realm's prediction is labelled
  `region (pooled fallback)`.
* Artifacts are staged in `.staging-*`, checksummed, loaded back to verify,
  then promoted by atomically replacing `CURRENT`. A failed run cannot
  corrupt the active model.

### 2.2 Targets and status

| Target | Label source | Status in this run |
|---|---|---|
| Sale probability, 24h / 48h / 168h | confirmed Journalator sale, expiry = 0, cancellation/censored excluded | **validated** (calibrated CatBoost) |
| Expected realized sale price | net proceeds of confirmed sales, log target | **validated** (CatBoost, ask features excluded) |
| Time to sale | sale duration; expiry/unobserved right-censored | **unsupported signal** (C-index 0.461; not exported) |
| Undercut pressure, 24h / 48h | future snapshot minimum buyout vs current | **validated** (CatBoost) |
| Price decline, 24h / 48h | future snapshot market value vs current | **validated** (CatBoost) |

The 48-hour horizon was added to `DEFAULT_HORIZONS_H` so the dataset carries
the labels. A resolved sale or expiry has a final 24/48/168h label even if
that many wall-clock hours have not elapsed; an active or unobserved
listing is censored and is not labelled. This is the censoring rule that
made the 7-day model trainable on a five-day archive without inventing
labels.

## 3. Dataset and label limitations

* Journalator archive: **4 446 linked postings**, **10 realms**, first
  `1792134307`, last `1792590777` (about 5.3 days). The `auction_events`
  table holds 577 sale rows; the linked listing frame used for training has
  **476 confirmed sales** (the rest are duplicate/unlinked or outside the
  posting window).
* A snapshot does **not** prove a sale. Sale labels come only from
  confirmed sale events. Expiry is a confirmed non-sale only when the
  listing outcome was observed. Cancellations and censored rows are
  excluded from that horizon, not treated as failures.
* Time-to-sale is not identifiable from snapshots alone; the existing
  censored survival model is the only time-to-event attempt, and it did not
  beat chance on this archive. The field is `unsupported` and omitted.
* The risk panel is restricted to the traded auction houses and the items
  present in the account's archive (91 412 snapshot rows, 64 275 closed
  horizon examples). It is not a claim about every item in the region.
* Price is **net realized proceeds**, never `dbmarket`, `dbminbuyout`, or
  `dbregionmarketavg`. The exported `price` remains the existing pricing
  engine's estimate; `ereal` is the Sentinel's separate realized-price
  estimate. They are deliberately not the same field.

## 4. Measured chronological holdout

Artifact: `20261021T141105Z-d38797de` (the promoted artifact at the time of
writing). Training rows are split by timestamp; calibration and test are
later blocks that no model fitted on. The live archive moved slightly
between runs (watch/import), so the counts below are the counts recorded in
this artifact's manifest.

### Sale probability

| Horizon | n train / cal / test | Positives train / test | Log loss | Brier | AUC | ECE | Item-rate baseline log loss / Brier / AUC |
|---|---|---|---|---|---|---|---|
| 24h | 1 603 / 646 / 667 | 234 / 159 | **0.3488** | 0.0894 | 0.9148 | **0.032** | 0.4503 / 0.1462 / 0.8656 |
| 48h | 1 603 / 646 / 667 | 235 / 160 | **0.3518** | 0.0917 | 0.9074 | **0.031** | 0.4531 / 0.1472 / 0.8639 |
| 168h | 1 603 / 646 / 667 | 236 / 160 | **0.3613** | 0.0912 | 0.9100 | **0.042** | 0.4530 / 0.1472 / 0.8639 |

On this run the calibrated CatBoost candidate beat both the item-rate
baseline and the calibrated logistic on log loss, Brier and AUC. The
logistic candidate scored 0.3582–0.3729 log loss and 0.1099–0.1139 Brier;
CatBoost was selected because log loss is the primary metric. The report
still records the baseline beside it, so the gain is visible rather than
assumed.

### Expected realized price

| Model | MAE log | Within 25 % | MAE gold | SMAPE | Median APE |
|---|---|---|---|---|---|
| CatBoost (selected) | **0.4337** | **42.86 %** | **11.57 g** | **0.3984** | 0.3401 |
| Item-median baseline | 0.9234 | 39.50 % | 24.11 g | 0.6363 | 0.4225 |
| Ridge | 1.0208 MAE log (candidate) | 40.34 % | — | — | — |

n = 262 train / 95 calibration / 119 test confirmed sales. The CatBoost
gain is measured on the untouched block, not in sample. Ask-derived
features are excluded from the price model, so this is not an echo of the
listing price. The model is materially better than the median baseline,
but 57 % of test sales fall outside ±25 %; it must not be presented as a
guarantee.

### Calibrated intervals

The price is now exported with a **split-conformal interval**, not a
single point. The width is the conformal quantile of the absolute log
residuals on the calibration block; the coverage is measured on the block
the calibration never saw. These are the numbers in the active dataset:

| Model | Nominal level | Half-width (log) | Measured coverage | Test sales |
|---|---|---|---|---|
| Sentinel realized price | 80 % | 0.764 | **85.7 %** | 119 |
| Record price (CatBoost) | 80 % | 0.641 | **77.7 %** | 103 |
| Shipped model | 80 % | 0.751 | **84.7 %** | 124 |

The Sentinel interval over-covers on this holdout (85.7 % for a nominal
80 %), which is the conservative direction: the tooltip shows the width
and the measured coverage together, so a wide interval is visible as wide,
not hidden behind a point estimate. The record and shipped intervals are
stored on the dataset so the addon can print the range even with no live
TSM reference.

### Market risk

| Target | n test | Base rate | Brier | Log loss | AUC | ECE |
|---|---|---|---|---|---|---|
| Undercut pressure 24h | 10 467 | 22.4 % | **0.1657** | 0.5143 | 0.643 | 0.027 |
| Undercut pressure 48h | 4 215 | 38.3 % | **0.2206** | 0.6364 | 0.654 | 0.021 |
| Price decline 24h | 10 467 | 35.5 % | **0.0826** | 0.3059 | 0.932 | 0.020 |
| Price decline 48h | 4 215 | 47.1 % | **0.1209** | 0.3999 | 0.892 | 0.018 |

The base-rate baseline Brier values were 0.1748, 0.2364, 0.2295 and 0.2534
respectively. The price-decline AUC is high because the current trend is
informative; it is still a market movement, not proof that a particular
listing was undercut.

## 5. Realm / connected-realm coverage

The linked frame covers 10 realms. The model is **pooled across realms**;
realm is a categorical context, not ten separate fits.

| Realm | Rows | Confirmed sales | 24h labels | 24h positives |
|---|---|---|---|---|
| Dragonmaw | 2 228 | 416 | 1 680 | 415 |
| Auchindoun | 610 | 16 | 417 | 16 |
| Silvermoon | 468 | 7 | 263 | 6 |
| Twisting Nether | 326 | 24 | 231 | 23 |
| Blackrock | 232 | 0 | 100 | 0 |
| Draenor | 210 | 1 | 53 | 1 |
| Antonidas | 182 | 3 | 83 | 3 |
| Kazzak | 111 | 1 | 60 | 1 |
| Defias Brotherhood | 64 | 1 | 20 | 1 |
| Argent Dawn | 15 | 7 | 9 | 6 |

Exported records: **237**. Scope counts: **230 `realm (pooled model)`**,
**7 `region (pooled fallback)`**; **0 `unknown`**, **0 `baseline_only`** at
the record level (all had a validated model, though individual targets can
still be missing). The region fallback is used where the record has no
per-realm sale context; it is never presented as a realm-specific fit.

The connected-realm mapping comes from `tsm_realm_group`; local snapshots
use the canonical `scope`, while inventory, characters and sales stay keyed
by realm. Five realms have their own confirmed sales; the rest are pooled.

## 6. Export contract (dataset schema 2)

New per-record fields, all optional/nil when unavailable:

```lua
psellByHorizon = { [24] = 0.59, [48] = 0.59, [168] = 0.59 },
plow = 1234, phigh = 3456, plevel = 0.8, pcov = 0.777, pintn = 103,
ereal = 1925, elow = 1559, ehigh = 2546,
elevel = 0.8, ecov = 0.857, eintn = 119,
undercut = 0.32, decline = 0.91,
scope = "realm (pooled model)",
model = "20261021T141105Z-d38797de",
status = "validated",
fallback = "",
dq = 0.441
```

The shipped model block carries its own interval
(`intervalLevel`, `intervalHalfWidth`, `intervalCoverage`, `intervalN`),
so the addon can print a range for an item with no record and no TSM
installed. Every interval field is optional; an old dataset has none and
the tooltip falls back to the point estimate rather than inventing a
range.

`dq` is a **data-quality** score (dominated by the item's own sample and
partly by whether the target passed its holdout), not the model's
probability. It is not a confidence percentage. With one observation it is
about 0.44 even when the model is validated; that is intentional.

Existing fields (`price`, `conf`, `psell`, `tts`, `n`, `seen`, `flags`)
remain. Missing probabilities are `nil`, not `0`. The dataset version is 2;
the reader accepts schema 1 and 2 (schema 2 is additive), so an old dataset
does not blank the panel. The TSM source accepts the same versions.

Probability curves are projected onto a monotone curve with pool-adjacent-
violators because independent horizon models can cross. The per-horizon
metrics above are the raw measured numbers, not the projection.

## 7. Lifecycle, retraining and `watch.py`

* Artifacts live under `models/dfa_sentinel/<version>/` with
  `manifest.json`, feature lists, calibration files and SHA-256 checksums.
* `CURRENT` is written only after every file is written, checksummed and
  loaded back successfully.
* Loading refuses a wrong schema, wrong feature schema, missing file or bad
  checksum. A stale artifact (default 14 days) returns `stale`, not a
  confident number.
* `retrain_decision()` trains when there is no artifact, when it is stale,
  or when at least `max(20, 10 % of the previous training rows)` new
  confirmed labels have arrived. It does not refit on every tick.
* `watch.py` schedules `dfa_ml.py sentinel --if-needed` once a day and
  rebuilds the export when the command reports `promoted=True`.
* `dfa_ml.py sentinel [--if-needed] [--dry-run]` is the manual entry point.
  `--dry-run` stages and verifies without publishing `CURRENT`.

## 8. Addon loading and the empty panel

Two independent defects were found in the game log:

1. `DFA_AuctionIntelligence/Core.lua:16` registered
   `PLAYERBANKTABS_CHANGED`, which the client does not know. The error
   aborted the whole main chunk, so the slash command and login sync never
   existed. Every registration is now wrapped in `pcall`; an unknown event
   is recorded in `DFA_AI.registrationWarnings` and skipped.
2. `Sellers.lua` installed `OnTooltipCleared` and `OnShow` without
   protection; one unsupported tooltip script could stop the other hooks.
   Each hook is now installed independently.
3. The installed `DFA_Estimate` was still the schema-1 reader while the
   dataset was schema 2, so `Load()` refused the file and the panel stayed
   empty. The reader now accepts 1 and 2; the installed copy, dataset and
   TOC were synchronised to the repository.
4. `SetScrollChild` already receives a frame in the repository; the log
   entry at 11:54 was from the old installed copy. The installed panel has
   the fixed version and the regression test
   `tests/test_dfa_estimate_panel.py` passes.

The project and installed addon trees are byte-identical after the sync
(`tests/test_auction_intel_addon.py` checks this).

### Bitmaps / encoding?

The generated dataset is **696 KB** (237 records + 31 184 packed prices),
not the 19 MB SavedVariables file. It is already written as packed Lua
tables; a bitmap/delta encoding would save perhaps a few hundred KB but
would require a decoder in Lua and make partial/unknown values harder to
represent. It was not added. The 19 MB file is `DFA_AUCTION_INTELLIGENCE_DB`
history, not the estimate dataset; if load time becomes a problem, the
right fix is pruning/compacting the event store, not bit-packing prices.

## 9. Commands and verification

```powershell
python -m pip install -r requirements-ml.txt
python scripts/dfa_ml.py sentinel                 # train and promote
python scripts/dfa_ml.py sentinel --if-needed     # retrain only if due
python scripts/dfa_ml.py sentinel --dry-run       # stage, verify, do not publish
python scripts/dfa_pricing_export.py              # rebuild the addon dataset
python -m pytest -q
```

Actual results:

* `python scripts/dfa_ml.py sentinel` → `promoted=True`, artifact
  `20261021T141105Z-d38797de`; 8 targets validated, 1 unsupported (time to
  sale).
* `python scripts/dfa_pricing_export.py --report` → dataset validates,
  about 723 KB, 237 records, 31 184 prices, checksum written; the record
  price interval is 0.641 log at 80 %, covering 77.7 % of 103 held-out
  sales.
* `python -m pytest -q` → **706 passed** (includes the calibrated-interval
  tests, dataset schema tests, watch scheduling and the addon-installed-copy
  check).
* `python scripts/intel_addon_harness.py` → 66/66 checks passed.
* `python scripts/auction_intel_harness.py` → 18/18 checks passed.
* `diff -rq` between the project addon folders and the installed client
  copies → identical (exit 0).

## 10. Remaining blockers

* **Time to sale is not operational.** The censoring-aware C-index is
  0.461 on the final artifact, below the 0.60 threshold; no duration is
  exported.
* **Exact sale timestamp is not observable from snapshots.** The duration
  target depends on Journalator posting/sale timestamps; when those are
  missing, the row is censored or excluded.
* **The archive is about five days long.** The 168h model now has resolved
  labels, but it cannot be validated across a full week of market regimes.
* **Per-realm independent models are not claimed.** The pooled model has
  realm context and exposes realm coverage; only 5 of 10 realms have any
  confirmed sale. A realm-specific fit would need far more data.
* **Undercut risk is market pressure.** It is the probability that the
  cheapest listing falls, not proof that a specific auction of ours was
  undercut. A confirmed per-listing undercut target would need the seller
  scan joined to the listing timeline.
* **Price model error remains material:** 0.434 log MAE, 43 % within 25 %,
  62 % within 50 % on 119 test sales. It beats the median baseline but is
  not precise enough to treat as a guarantee, which is why the tooltip now
  shows the calibrated range beside it.
* **The risk model is item-limited** to the traded universe in the panel;
  an item with no snapshot history gets no risk value, not a zero.
