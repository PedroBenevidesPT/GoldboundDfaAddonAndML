# Market data & model audit — 2026-10-22

Scope: `data/intel.sqlite` (7.7M auction rows), `tsm_markets.db` (1.5 GB),
`models/dfa_ml`, `models/dfa_sentinel`, `models/dfa_ml/shipped_fit.json`,
and the reports they generate.

Everything below was measured against the live databases, not inferred from
the docs. Queries are in the appendix so each claim can be re-run.

---

## Verdict

The engineering around the model is stronger than the data underneath it.
Point-in-time replay, censoring rules, calibration, baselines and versioned
artifacts are all real and correct. What is broken is **coverage and
identity**: the market snapshot contains zero commodities (the items the
account actually sells), 9 % of all rows are one pooled battle-pet item,
prices up to the gold cap are averaged in unfiltered, and the five sale
horizons the product advertises are one label asked five times.

Net: the model is well-built, well-instrumented, and trained on a market it
mostly cannot see.

---

## Severity table

| # | Issue | Severity | Evidence |
|---|---|---|---|
| D1 | Commodity auctions are never fetched — 0 of 7.7M rows | **P0** | `commodity_count` = 0 in all 104 snapshots; Goldthorn/Murglesnout/Thick Hide/Linen Cloth = 0 rows |
| D2 | Only 7.7 % of the account's sale events have any AH snapshot coverage | **P0** | 43 of 557 sales; every top-selling item is absent from `ah_auction` |
| D3 | Variant pooling — one `item_id` covers hundreds of distinct goods | **P0** | item 82800 = 694,416 rows (9.0 %), prices 400 c → 9,999,999 g |
| D4 | Gold-cap / sentinel-valued auctions averaged as real prices | P1 | 3,485 rows ≥ 9M g; 2,749 exactly `99_999_999_000`; averages inflate 2.6× |
| M1 | Five "horizons" are the same label; selection still differs per horizon | **P0** | label agreement 24h↔336h = 0.9988; 48h promoted, 168h rejected, on identical labels |
| M2 | Time-to-sale measures mail-check latency, not sale latency | P1 | 92.4 % censored, 59 events, median predicted 25.9 h; matcher picks the *newest* listing |
| M3 | `sold_at_ask_rate` 0.998 is a tautology | P2 | sale price is read from the invoice and matched by exact price equality |
| M4 | The shipped linear model and the evaluated CatBoost disagree in bias | P1 | shipped `median_ratio` 0.962 vs CatBoost `bias_log` +0.213 (17 % high) |
| M5 | The demand curve is non-monotonic and has no confidence intervals | P1 | 0.25×→57.6 %, 0.60×→67.1 %; n = 85–182 per band, ±5 pp SE |
| D5 | Two parallel event stores disagree by 13× | P1 | 577 sales in `auction_events` vs 43 seller rows in `mail_invoice` |
| D6 | Import validates nothing | P2 | `rejected_rows` = 0 across 104 imports |
| D7 | Competition layer covers 3 realms / 211 items | P2 | `undercut_observation`: 1,773 rows, 34 h |
| M6 | 9 of 10 per-realm models are `insufficient_data` | P2 | `realm_model_run` |
| M7 | Docs contradict artifacts on time-to-sale | P2 | README: "concordance index is near chance"; artifact: C-index 0.913 |

---

## Part 1 — Data

### D1. The market snapshot contains no commodities at all (P0)

`ah_auction` has 7,704,472 rows. `is_commodity = 0` for every one of them,
`quantity = 1` for every one of them, and `commodity_count` sums to 0 across
all 104 snapshots.

The parser supports commodities (`woweconomy/intel/ah.py`,
`commodity_endpoint=True`). The CLI exposes `--commodity-endpoint`
(`scripts/intel.py:363`) with `action="store_true"`, i.e. **off by default**.
The automated collector never passes it:

```python
# scripts/watch.py:565
argv = [PY, "scripts/intel.py", "ah-fetch", "--realm", realm]
```

So the commodity endpoint has never been called. Verification:

```
Goldthorn (3820)            0 rows
Murglesnout (27422)         0 rows
Thick Hide (8169)           0 rows
Primal Bat Leather (21888)  0 rows
Linen Cloth (2589)          0 rows
Copper Ore (2770)           0 rows
```

This is the item class the whole product exists to price. Farm output is
herbs, ore, leather, cloth and elemental materials — all commodities. The
`market_context` layer (supply, cheapest ask, competition, price path) can
therefore only ever return `None` for them, which the code correctly reports
as "unknown" but which means the farm ranking is flying blind on its own
subject matter.

Fix: pass `--commodity-endpoint` in `scripts/watch.py`, and backfill — the
commodity endpoint is region-wide and cheap to fetch.

### D2. 92 % of realised revenue has no market context (P0)

Cross-referencing the account's own Journalator events against the snapshot:

```
distinct items in Journalator events : 954
present in ah_auction               : 746  (78.2 %)
sale events with AH coverage        : 43 / 557  (7.7 %)
```

The 78 % item figure is misleading — it is driven by transmog and pets, each
of which sells once. By **volume**, the picture is:

```
Light Leather        14 sales   in_ah = False
Medium Leather       13         False
Small Spider Leg     13         False
Wool Cloth           12         False
Linen Cloth          11         False
Gooey Spider Leg     10         False
Briarthorn           10         False
```

Every top seller is invisible to the market snapshot. In research terms the
sample is not representative of the population being predicted: the model
learns from commodity sales and then attaches non-commodity market features
to them.

### D3. Variant pooling — one `item_id` is not one good (P0)

| item_id | rows | share | distinct prices | notes |
|---|---|---|---|---|
| 82800 | 694,416 | 9.0 % | 27,806 | `pet_species_id` takes 100s of values |
| 188152 | 32,899 | 0.4 % | — | 14 modifiers |
| 114821 | 17,204 | 0.2 % | — | no modifiers at all |

Item 82800 is the caged-battle-pet container: a single `item_id` shared by
hundreds of different pets, with prices spanning 400 copper to
9,999,999 gold. It is 11,000+ concurrent listings per snapshot and 9 % of the
entire table.

More broadly, **597,086 `(item_id, snapshot_id)` groups contain more than one
distinct `bonus_lists|modifiers` variant.** Every item-level statistic built
on `item_id` alone — min buyout, market value, "the cheapest ask", the
`cat_item_id` categorical in CatBoost — is therefore an average over
incomparable goods. For pets and ilvl-scaled gear this is not noise, it is a
different product.

`auction_key` already encodes the variant correctly; the aggregation layer
does not use it. Fix: aggregate on `(item_id, bonus_lists, modifiers)` (or a
`variant_key`), and reserve bare `item_id` for "any variant".

### D4. Gold-cap and sentinel-valued auctions poison every average (P1)

```
rows >= 90,000,000,000 copper (9,000,000 g) : 3,485
rows == 99,999,999,000 (the TSM sentinel)   : 2,749
distinct items affected                     : 213
```

`woweconomy/ml/config.py` defines `SENTINEL_COPPER = 99_999_999_000` and its
own docstring says *"TSM writes this when it has no data. It is not a price."*
Yet 2,749 rows in `ah_auction` carry exactly that value, and the aggregation
path does not filter them.

Measured effect on item means:

| item_id | mean, filtered | mean, as stored | inflation |
|---|---|---|---|
| 258943 | 126,757,499 | 334,960,218 | 2.64× |
| 258950 | 48,362,447 | 281,768,462 | 5.83× |
| 258951 | 113,828,026 | 351,434,657 | 3.09× |

A single gold-cap listing moves an item's "market price" by 2–6×. Any
`avg`/`max`/percentile over `unit_price_copper` needs a hard ceiling (say
p99.9 per item per snapshot) plus an explicit sentinel drop.

### D5. Two event stores, 13× apart (P1)

| store | sales recorded |
|---|---|
| `tsm_markets.db:auction_events` | 577 |
| `intel.sqlite:mail_invoice` | 43 |

`journal` is empty while `journal_stats` holds 1,301 rows, and
`sale_buyer_link` is empty (buyer-side attribution never runs). Two writers,
two schemas, no reconciliation. Whichever one the model reads, the other is
dead weight that will silently drift.

### D6. The importer accepts everything (P2)

`rejected_rows = 0` for all 104 snapshots, totalling 7.7M accepted rows. A
parser that has never rejected a row is not validating — and D4 shows what
got through. `reconciliation` agrees: of 1,648 deltas, 320 matched (19 %),
1,128 (68 %) are `unexplained_increase` / `unexplained_decrease`.

### D7. The competition layer is a rounding error (P2)

`undercut_observation`: 1,773 rows, **3 realms**, 211 items, over 34 hours.
`seller_observation`: 339 rows, 6 realms, 177 items. Against 18,688 items and
267 realms, "seller-level competition" exists for about 1 % of the catalogue.
The model ships 52 features; the ones that describe competitors are
effectively absent for 99 % of predictions.

---

## Part 2 — Model

### M1. The five horizons are one label — but selection treats them as five (P0)

From `reports/dfa_ml/dfa_ml_report.json`:

```
horizon_positive: {24: 482, 48: 485, 72: 486, 168: 486, 336: 486}
identical_groups: [[24, 48, 72, 168, 336]]
label agreement 24h vs 336h: 0.9988
```

486 sales, 482 of them "within 24 h", and **none** after 72 h. The report
states the cause honestly: an auction lasts at most 48 h, so a longer horizon
asks a question the auction house cannot answer.

The problem is what happens downstream. In the Sentinel run, on the same
labels and the same test window (`test_ts` identical for all three):

| target | chosen model | status |
|---|---|---|
| `sale_24h` | `item_rate_baseline` | `baseline_only` |
| `sale_48h` | `logistic` | `validated` |
| `sale_168h` | `item_rate_baseline` | `baseline_only` |

`sale_48h` and `sale_168h` report **byte-identical baseline metrics**
(AUC `0.8712960299194477`, AP `0.729343211534405`) yet the learned candidates
differ (`0.8887` vs `0.8929`) and one is promoted while the other is
rejected. Identical labels, identical features, identical split, different
winner — that is selection noise, not evidence. It also means the promotion
gate is not deterministic, which undermines the whole `CURRENT` pointer.

Three further consequences:

- **Five artifacts ship for one question** (`sale_probability_24h/48h/72h/168h/336h.cbm`).
  `pricing/engine.py:902` then applies a "monotonic projection" across
  24/48/168 because "independent horizon classifiers can cross" — patching a
  distinction that does not exist.
- **168 h and 336 h are shipped despite being marked "not evaluable"** in the
  report ("no listing in the untouched window has had this horizon close
  inside the observation period").
- The 24 h label is not identical to 48 h (482 vs 485), so the *one* horizon
  that carries information is the one that gets rejected in favour of a
  baseline.

Fix: ship a single sale-probability target (probability of selling at all,
before the auction ends), rename it honestly, and delete the other four.

### M2. Time-to-sale is measuring when the mailbox was opened (P1)

```
n_test 773 | n_events 59 | censoring 92.4 %
c_index 0.913 | within_2x_events 27.1 % | median predicted 25.9 h
```

The README says durations are upper bounds because outcomes arrive by mail.
That is correct, but the consequence is worse than "conservative":

- Median predicted duration (25.9 h) and median observed (25.7 h) both sit
  almost exactly on a day boundary — the shape of a polling interval, not of
  buyer behaviour.
- `linking.py:232` resolves an ambiguous match with
  `chosen = candidates[-1]` — **the most recent listing** at or before the
  outcome. With 4,727 postings and 684 cancellations (i.e. constant relisting),
  a sale is routinely attributed to a listing posted minutes earlier, which
  collapses `elapsed` toward zero. **381 of 489 sale matches (78 %) are
  ambiguous**, so this affects most of the training set.
- 59 events is far too few to support a C-index quoted to three decimals.

The model is correctly gated to `validated_ranking_only`, but it is ranked on
a quantity that is mostly relist latency.

### M3. `sold_at_ask_rate = 0.998` is circular (P2)

`linking.py:240` sets `out.unit_price = payload.value // qty` from the
invoice, then `linking.py:246` compares it to `chosen.unit_buyout`. When the
match was made *by* price equality, the comparison is guaranteed to pass.
The report presents 99.8 % as a market finding ("sellers get their ask"); it
is an artefact of the matching rule.

### M4. The model that is evaluated is not the model that ships (P1)

| | shipped linear (`shipped_fit.json`) | CatBoost (`price.cbm`) |
|---|---|---|
| median ratio | 0.962 (3.8 % **under**) | 1.172 (17 % **over**) |
| within ±25 % | 42.0 % | 45.0 % |
| within ±50 % | 68.9 % | 64.1 % |
| log RMSE | 0.711 | 0.546 |

Two different biases in two different directions, reported in two different
files. The README says the distillation is "checked, not assumed" — but the
check is not a check if the two sides disagree about which way the error
points. In-game the linear model will systematically *under*-ask relative to
what the CatBoost evaluation implies, which is the expensive direction to be
wrong in for a listing tool (it leaves gold on the table), while CatBoost
over-asks by 17 % (which leaves stock unsold).

Also inside `shipped_fit.json`: the three inputs are
`dbregionmarketavg`, `dbregionhistorical`, `dbregionsaleavg` — three
near-collinear regional aggregates of the same item — with coefficients
`[0.238, 0.239, 0.544]` summing to 1.02. The model is a calibrated blend of
three views of one number, not a learned relationship. That is fine as an
engineering choice, but it caps the achievable accuracy: `dbregionsaleavg`
carries 54 % of the weight, so the shipped model inherits TSM's regional
sale average wholesale — including its staleness (last TSM update
2026-10-21 07:31 vs AH snapshots 2026-10-22 19:44, ~36 h behind) and its
inclusion of the account's own sales.

The interval metadata is also self-inconsistent: `model.interval_coverage`
= 0.848 (n = 132) and `price_interval.coverage` = 0.780 (n = 123) both claim
to describe an 80 % interval in the same artifact.

### M5. The demand curve is non-monotonic and unqualified (P1)

`sale_rate` by ask/reference band:

| ratio | sale rate | n |
|---|---|---|
| 0.25 | 57.6 % | 85 |
| 0.60 | **67.1 %** | 85 |
| 0.78 | 63.4 % | 101 |
| 0.93 | 60.9 % | 138 |
| 1.08 | 55.7 % | 115 |
| 1.28 | 43.5 % | 131 |
| 1.70 | 25.3 % | 182 |
| 2.50 | 6.7 % | 312 |
| 4.50 | 2.0 % | 2,112 |

The cheapest band sells **worse** than the 0.6× band. That is not a demand
curve, it is confounding: listings priced far below the reference are
disproportionately items whose reference is wrong or whose demand is dead.
Because ratio is not randomised, the curve cannot be read as causal — and yet
it is used to pick the "optimal band" and to compute
`strategy_value.improvement = 3.06×`.

Worse, no band carries a confidence interval. With n = 85 and p ≈ 0.6 the
standard error is ≈ 5.3 pp, so the 67.1 % peak vs 57.6 % trough is inside
one SE. The apparent optimum is noise.

`strategy_value.share_in_the_optimal_band = 0.9975` is circular by
construction: the band is defined by the same curve used to score it.

`reference_credibility` is directionally useful but rests on tiny samples —
`dbmarket` n = 30, `dbrecent` n = 24 — to declare "inflated asking reference"
(median 2.07× realised, 90 % of references above realised). The direction is
almost certainly right; the precision is overstated.

### M6. Nine of ten per-realm models are empty (P2)

```
Dragonmaw      trained            440 rows
Antonidas      insufficient_data    8
Auchindoun     insufficient_data   17
Twisting Nether insufficient_data  24
Silvermoon     insufficient_data    7
Blackrock/Defias/Draenor/Kazzak      1
```

A per-realm model layer that has one realm is a single-realm model with
nine empty slots. Either pool across realms with a realm embedding, or drop
the layer.

### M7. Documentation and artifacts disagree (P2)

`woweconomy/ml/README.md`: *"the time to sale is **not** predictable from
these features — the concordance index is near chance."*
`sentinel_report.json`: `c_index 0.913` against a 0.500 baseline.

Both cannot be true. One of them is stale. Given M2, the README's conclusion
is the right one and the artifact's number is the misleading one — which
means the artifact is currently winning the argument with the user.

---

## Part 3 — What actually holds up

Worth saying plainly, because most of this codebase is better than the data:

- **Point-in-time replay is correct.** `tsm_history.snapshot_for()` selects
  the newest snapshot whose availability is ≤ `ts`; `state_at()` replays the
  change table to that snapshot. No lookahead in the reference features. I
  looked for it specifically and did not find it.
- **Censoring is modelled, not ignored.** Censored and cancelled outcomes are
  excluded from both classes rather than folded into "didn't sell". That is
  the single most common mistake in this kind of model and it was avoided.
- **Calibration is real.** ECE 0.016–0.047 with isotonic calibration on a
  held-out block, not in-sample.
- **Baselines are honest and sometimes win.** Three targets fall back to
  `item_rate_baseline` and say so.
- **Artifacts are checksummed and atomically promoted**, with `CURRENT`
  pointing only at a verified version.
- **The reports volunteer their own limits.** `horizon_equivalence`,
  `interval_note`, the "not evaluable" flags — the pipeline already knows
  most of Part 2. The gap is that the *artifacts* and the *README* do not
  obey what the *report* knows.

---

## Part 4 — Fix order

**This week (data correctness, no modelling):**

1. `scripts/watch.py:565` — add `--commodity-endpoint`, backfill, confirm
   `commodity_count > 0`. Unblocks everything in D1/D2.
2. Add a price ceiling + sentinel drop before any aggregation
   (`unit_price_copper >= 90_000_000_000` → reject, log to `rejected_rows`).
3. Key the variant, not the item: aggregate on
   `(item_id, bonus_lists, modifiers)`; keep `item_id` for rollup only.
4. Make `rejected_rows` meaningful — count what the parser refuses.

**Next:**

5. Collapse the five sale targets to one, re-run selection, confirm the
   promotion gate is deterministic on identical inputs.
6. Fix `linking.py` ambiguity: on multiple candidates, mark the row
   `ambiguous=1` and either drop it from price training or weight it down —
   do not silently pick `candidates[-1]`.
7. Reconcile `auction_events` vs `mail_invoice`, or delete one.
8. Put confidence intervals on the demand curve bands and refuse to publish
   an "optimal band" whose peak is inside the noise.

**Then:**

9. Re-examine the shipped linear model against the CatBoost it claims to
   distil, and state one bias number in one place.
10. Either pool realms for the per-realm layer or remove it.
11. Add a covering index for item-level aggregates across snapshots; the
    7.7M-row full scans currently time out.

---

## Appendix — verification queries

```sql
-- D1: no commodities
SELECT sum(commodity_count), sum(auction_count) FROM ah_snapshot;      -- (0, 7704472)
SELECT count(*) FROM ah_auction WHERE item_id IN (3820,27422,8169,2589,2770);  -- 0

-- D2: coverage of realised sales (two DBs)
-- 43 of 557 sale events present in ah_auction

-- D3: variant pooling
SELECT item_id, count(*), count(DISTINCT pet_species_id)
FROM ah_auction WHERE item_id = 82800;                                 -- 694416
SELECT count(*) FROM (SELECT item_id, snapshot_id,
  count(DISTINCT bonus_lists||'|'||modifiers) c
  FROM ah_auction GROUP BY 1,2 HAVING c>1);                            -- 597086

-- D4: gold-cap / sentinel
SELECT count(*) FROM ah_auction WHERE unit_price_copper = 99999999900;  -- 2749
SELECT count(*) FROM ah_auction WHERE unit_price_copper >= 90000000000; -- 3485

-- D5: two stores
SELECT count(*) FROM auction_events WHERE event_type='sale';            -- 577
SELECT count(*) FROM mail_invoice WHERE invoice_type='seller';          -- 43

-- M1: horizons are one label
-- reports/dfa_ml/dfa_ml_report.json → dataset.horizon_positive
--   {24: 482, 48: 485, 72: 486, 168: 486, 336: 486}
--   horizon_equivalence.identical_groups == [[24,48,72,168,336]]

-- D7: competition coverage
SELECT count(*), count(DISTINCT realm), count(DISTINCT item_id)
FROM undercut_observation;                                             -- (1773, 3, 211)

-- D6: reconciliation
SELECT status, count(*) FROM reconciliation GROUP BY 1;
-- matched 320 | unexplained_decrease 618 | unexplained_increase 510 | event_not_reflected 200
```
