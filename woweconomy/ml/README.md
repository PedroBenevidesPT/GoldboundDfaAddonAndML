# The DfA learning engine

A supervised learning engine for the auction-house economy of the DfA
realms.  It learns from what the account actually did -- the Journalator
archive -- and predicts what a listing is likely to fetch, how likely it
is to sell, and how long that is likely to take.

```
scripts/dfa_ml.py audit      # the data-readiness report
scripts/dfa_ml.py train      # walk-forward validation + the final models
scripts/dfa_ml.py sentinel   # calibrated sklearn + CatBoost prediction layer
scripts/dfa_ml.py recommend  # a recommended ask for one item
```

The `sentinel` command writes versioned artifacts under
`models/dfa_sentinel/` and a report under `reports/dfa_sentinel/`.  It is
what the pricing export reads for sale probability, expected realized
price, time to sale and market risk.  `--if-needed` skips the fit when no
meaningful number of new confirmed labels has arrived; `watch.py` uses it
once a day.

Runtime dependencies are in `requirements-ml.txt`.  scikit-learn is used
for probability calibration (isotonic/Platt) and for the logistic/Ridge
baselines that CatBoost must beat, not as a replacement for CatBoost.

## The validated layer

The original engine reported walk-forward metrics but the numbers that
reached the game were uncalibrated and had no baseline comparison.  The
`sentinel.py` layer adds:

* a chronological train / calibration / untouched-test split (the test
  timestamps are strictly after calibration);
* calibration fitted only on the middle block;
* item-rate, base-rate and item-median baselines;
* Brier, log loss, AUC, ECE, precision/recall for probabilities; MAE on
  the log scale, SMAPE and within-X% for prices; C-index for survival;
* market undercut-pressure and price-decline targets built from timestamped
  `tsm_snapshot` + `tsm_price_change` rows, never from asking prices;
* an explicit `unknown` when the sample is below the configured minimum;
* checksummed, schema-checked, atomically promoted artifacts with
  `CURRENT` only pointing to a verified version.


## The principle

**Listings show what sellers want to receive; transactions show what
someone actually paid.**  Every price source in the installation is one
or the other, and the two are never averaged as if they measured the
same thing.

The asking families -- `dbmarket`, `dbrecent`, `dbminbuyout`,
`dbregionmarketavg`, `dbregionhistorical` -- are inputs.  The transaction
family -- `dbregionsaleavg` and the account's own invoices -- is the
evidence.  The engine measures how far each asking family sits from what
buyers paid and lets CatBoost learn when to believe which, rather than
choosing the weights by hand.

## The four models

| Model | What it answers | Loss | Target |
|---|---|---|---|
| **A** | what will a unit fetch? | `RMSE` on a log target | net proceeds per unit |
| **B** | will it sell in time? | `Logloss` | `sold_within_<H>h` |
| **C** | how long will it take? | `SurvivalAft` | hours to sale, right-censored |
| **D** | what should I ask? | -- | the decision layer over A and B |

Model D is deliberately **not** a model.  It is a decision layer: it
evaluates a grid of candidate asks with the predicted price, the predicted
sale probability, the costs, the stock and the deadline.

## The three rules the code is built around

**1. A feature must have been true at the prediction moment.**

The TSM mirror stores a *change* table: a row exists only for the keys
whose prices moved.  The state at time `T` is therefore the value of the
last change at or before `T`, and `woweconomy.ml.tsm_history` replays it
that way.  The engine never reads a value that was written later.

**2. A missing value is not a value.**

TSM writes a sentinel when it has no data.  A reference that is absent
gets a missing indicator and a freshness measure, not a zero that would
read as "free".

**3. An outcome that was not observed is not a failure.**

A listing whose outcome was never observed is *censored*: it is either
still on the market or its mail has not been opened.  It is in neither
class.  A *cancellation* is the seller changing their mind and is in
neither class either -- it is not evidence that nobody wanted the item.

## The modules

| Module | What it owns |
|---|---|
| `config.py` | the paths, the horizons, the constants |
| `tsm_history.py` | the point-in-time reconstruction of the TSM state |
| `journalator.py` | reading the compressed Journalator archive |
| `linking.py` | matching listings to outcomes, with censoring |
| `demand.py` | the DfA demand counters |
| `features.py` | assembling the feature row |
| `dataset.py` | the labels and the censoring rules |
| `models.py` | the three CatBoost models and their metrics |
| `sentinel.py` | the calibrated sklearn + CatBoost layer, baselines, risk targets, versioned artifacts |
| `strategy.py` | the listing-price decision layer |
| `market_context.py` | the observed market around an item: supply, competition, price path, liquidity, freshness |
| `posting.py` | the posting decision: post, undercut, hold, relist, change realm, wait, stop |
| `validate.py` | the chronological splits |
| `pipeline.py` | the wiring |
| `audit.py` | the data-readiness report |
| `report.py` | the report that says what was learned |

## What the data cannot support

The engine says so in its own reports.  In particular:

* the time to sale is **not** predictable from these features -- the
  concordance index is near chance, and no time-to-sale figure is offered
  rather than a confident wrong one;
* a horizon longer than the auction adds nothing, because the auction is
  over;
* the DfA demand counters have no clock, so they are not point-in-time and
  are flagged as such;
* observed supply exists only where a Blizzard snapshot was imported (three
  realms so far).  Everywhere else the listing count is **unknown**, and
  `market_context` reports it as `None` rather than zero -- the models were
  not trained on a feature that 97 % of realms do not have;
* the seller-level competition comes from the addon's own undercut scans,
  so it exists only for the items a scan looked at, and it says nothing
  about a seller who was not in the result set;
* a rise in the cheapest ask is a **sale proxy** (bought, cancelled or
  expired), never a confirmed sale, and it is labelled that way wherever it
  is used;
* our own listings are counted in the visible supply, and the share they
  are of it is reported: a reference ask that is really our own listing is
  not independent market evidence.

## What the market context changes

`market_context.py` reads what the pipeline already stores -- the Blizzard
snapshot in `data/intel.sqlite`, the TSM change table, the undercut scans and
our own posted stock -- and `posting.decide` uses it in four places where the
gap alone is not the answer: a gap inside the measured noise does not justify
a deposit, our own stock is not a competitor, a falling market justifies
matching the cheapest even when the gap is small, and a market whose cheap
stock is clearing is worth waiting in rather than starting a price war.  With
no market the decision is byte-for-byte the one it always was.

## The tests

`tests/test_dfa_ml.py` covers the guards: that a state never contains a
later value, that a censored listing never becomes a failure, that a
cancellation is not a failure, that the sale is matched to the
price-compatible listing, and that the split is chronological.

`tests/test_market_context.py` covers the market layer (missing sources stay
unknown, staleness is flagged, own stock is counted, the price path gives a
direction and a noise) and `tests/test_market_decisions.py` covers what the
context changes and what it must not change.
