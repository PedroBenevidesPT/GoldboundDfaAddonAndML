# The DfA learning engine -- what it learned and how much to trust it

Generated 2026-10-21T19:58:58Z.

**Listings show what sellers want to receive; transactions show what someone actually paid.**  Every model here is trained against the second kind, and the asking-price families -- `dbmarket`, `dbregionmarketavg` and their relatives -- are inputs, never labels.

## What it was trained on

- **4,724 listings** over 922 items and 10 realms
- **489** of them reached a sale, 2,794 expired without one, 279 were withdrawn by the seller and 1,209 had no observed outcome at all
- the linkage is **99.8% price-exact**: the amount that changed hands is exactly the posted buyout times the stack size.  A wrong pairing would not do that.
- every row has a regional reference; 3,202 have a local one

## Model A -- the realizable price

**Target:** the money the seller pocketed per unit, in logarithms.  Never `dbmarket`, never `dbregionmarketavg`: those are asking prices, and an asking price is a hypothesis about a price, not a price.

**The ask is deliberately withheld from this model.**  Without it the model has to answer "what will the market pay for this item?"; with it, the model would simply learn that the answer is whatever you asked, which is true of 99.7 % of the sales here and useless as a guide to what to ask.

### Walk-forward (train on an earlier day, validate on a later one)

| metric | mean over the folds | spread |
|---|---|---|
| RMSE on the log scale | 0.937 | 0.750 .. 1.124 |
| median of the error factor | 1.109 | 1.00 means the estimate is right on the median |
| mean absolute error | 23.7g | in gold, per unit |
| symmetric error | 51.6% | on the gold scale |

### The untouched test window

Trained on 373 sales from the earlier days, scored on 113 sales from the last day that no fold ever saw.

| metric | value |
|---|---|
| RMSE on the log scale | 0.648 |
| median of the error factor | 1.033 |
| within 25 % of the price paid | 42.5% |
| within 50 % of the price paid | 68.1% |
| mean absolute error | 15.9g |

> The estimate is unbiased on the median but wide: the market references do not pin down the price of one particular sale.  Read it as a price *level* with a band, not as a number.

### The demand curve: what happens when the ask moves

The model of `P(sell)` says how likely a sale is at the prices the account happened to use.  The curve below says what happens when the ask is moved -- the ratio is *the ask over the regional sale average*, the average a sale actually closed at, so the ratio is "how far above what buyers pay is this listing asking".

| ask / regional sale average | listings | sale rate within 24 h | expected gold per listing | share of all the sales |
|---|---|---|---|---|
| (-0.001, 0.5] | 85 | 57.6% | 0.14 | 10.2% |
| (0.5, 0.7] | 85 | 67.1% | 0.40 | 11.8% |
| (0.7, 0.85] | 101 | 63.4% | 0.49 | 13.3% |
| (0.85, 1.0] | 138 | 60.9% | 0.56 | 17.4% |
| (1.0, 1.15] | 115 | 55.7% | 0.60 | 13.3% |
| (1.15, 1.4] | 131 | 43.5% | 0.55 | 11.8% |
| (1.4, 2.0] | 182 | 25.3% | 0.43 | 9.5% |
| (2.0, 3.0] | 303 | 6.9% | 0.17 | 4.4% |
| (3.0, 1000000000.0] | 2089 | 1.9% | 0.09 | 8.3% |

**The expected gold per listing peaks in the band `(1.0, 1.15]`** -- the sale rate there is 55.7% and each listing collects about 0.60 times the regional sale average when it sells.  Below that band the ask is so low that the sale is nearly certain but the price is tiny; above it the ask is so high that the listing simply sits there.  This is the answer the data gives without any model in the way, and it is the benchmark the strategy is measured against.

The median listing asks **4.36x** the regional sale average, and the sale rate is flat until the ask runs well past the level buyers pay -- and then it collapses.  64.7% of the listings ask more than three times the regional sale average, and only 1.9% of those sold, against 57.3% for the listings that asked within 1.4x.

That is the speculative-reference problem in the account's own behaviour: the asking prices are not what the market is paying, and the listings that are priced far above the market are the ones that sit unsold.

### The mechanism: what actually stops a listing selling

The table below says what the gap between the asking references and the transactions costs in sales, and it is the sharpest result in this report.

| dbregionmarketavg / dbregionsaleavg | listings | sale rate within 24 h | ask as a fraction of the asking reference |
|---|---|---|---|
| (-0.001, 1.05] | 199 | 48.2% | 0.92 |
| (1.05, 1.3] | 261 | 65.9% | 0.90 |
| (1.3, 2.0] | 174 | 50.0% | 0.84 |
| (2.0, 4.0] | 368 | 12.8% | 1.00 |
| (4.0, 8.0] | 757 | 5.5% | 0.79 |
| (8.0, 20.0] | 817 | 3.3% | 0.60 |
| (20.0, 1000000000.0] | 653 | 1.7% | 0.57 |

When the asking reference and the price buyers pay agree, an ask placed on the asking reference clears.  When the asking reference has run far ahead of the transactions, the ask still sits on that inflated reference -- and the listing does not sell.  The account's failures are not bad luck and not a bad item: they are asks placed on a price the market stopped paying.

This is the whole argument of the engine in one table.  A tool that averages `dbmarket` and `dbregionsaleavg` cannot tell these two situations apart -- they look like the same item at two prices.  A model trained against the outcomes can, because the *sale rate* differs by an order of magnitude between the bands.

### The two hypotheses, measured

Each asking-price family is compared with what the buyer actually paid, on the same transactions.  A family whose median sits above one promises more than the market pays -- an inflated reference.  A family near one is commercially defensible, and the model is not allowed to lower the price just because it looks expensive.

| reference | median vs realised | quartile spread | share above the realised price | verdict |
|---|---|---|---|---|
| dbmarket (local asking) | 2.067 | 1.186 .. 8.620 | 90.0% | inflated asking reference |
| dbrecent (local recent) | 1.411 | 1.052 .. 6.521 | 91.7% | inflated asking reference |
| dbhistorical (local historical) | 2.931 | 1.268 .. 6.558 | 90.0% | inflated asking reference |
| dbminbuyout (local floor) | 1.579 | 1.052 .. 6.521 | 91.7% | inflated asking reference |
| dbregionmarketavg (regional asking) | 1.372 | 1.097 .. 2.458 | 84.0% | inflated asking reference |
| dbregionhistorical (regional historical) | 1.336 | 1.035 .. 1.958 | 77.8% | inflated asking reference |
| dbregionsaleavg (regional sales) | 1.060 | 0.797 .. 1.412 | 54.7% | commercially defensible |

The model does not average these families.  It receives the ratios between them as features and learns from the transactions which family to believe -- and the ablation below says what each family is worth.

### What each family contributes (walk-forward)

| the model without... | RMSE on the log scale | mean absolute error |
|---|---|---|
| without the asking references | 0.944 | 0.637 |
| without the region sale average | 0.993 | 0.631 |
| without the local references | 0.920 | 0.593 |
| without the DfA demand counters | 0.910 | 0.582 |

A family whose removal does not hurt was noise.  A family whose removal *helps* was misleading the model, which is the speculative-reference problem showing up in the numbers.

### What the price model leans on

| feature | share of the model's attention |
|---|---|
| `region_market_log` | 24.4% |
| `tsm_normalmarket_log` | 20.6% |
| `tsm_normal_log` | 20.3% |
| `region_sale_avg_log` | 14.6% |
| `region_historical_log` | 11.5% |
| `ref_dispersion` | 0.6% |
| `region_sale_rate` | 0.6% |
| `local_age_h` | 0.6% |
| `day_of_week` | 0.5% |
| `region_sold_per_day` | 0.4% |
| `region_age_h` | 0.4% |
| `region_market_over_historical` | 0.4% |
| `region_sale_avg_over_historical` | 0.4% |
| `spread_market_vs_sale` | 0.4% |
| `region_market_chg` | 0.4% |


## Model B -- the probability of selling

**Target:** did the listing sell inside the horizon?  A listing whose outcome was never observed is *censored* and is in neither class; a listing the seller withdrew is *cancelled* and is in neither class either -- a seller changing their mind is not evidence that nobody wanted the item.

| horizon | labelled rows | base rate | AUC | Brier | log loss |
|---|---|---|---|---|---|
| 24h | 685 | 15.9% | 0.947 | 0.064 | 0.213 |
| 48h | 685 | 16.0% | 0.947 | 0.064 | 0.215 |
| 72h | 685 | 16.0% | 0.945 | 0.065 | 0.213 |
| 168h | 685 | 16.0% | 0.945 | 0.065 | 0.213 |
| 336h | 685 | 16.0% | 0.945 | 0.065 | 0.213 |

**The 24h, 48h, 72h, 168h, 336h horizons are the same question.**  An auction lasts 12, 24 or 48 hours, so once the horizon passes that, every sale that was going to happen has already happened.  The identical scores are not a coincidence and not three models: they are one model asked the same thing three times, and a longer horizon is a promise the auction house cannot keep.

- **24h on a held-out window:** AUC 0.958 on 869 labelled rows (base rate 12.4%), trained on 1882 rows from the days before it.
- **48h on a held-out window:** AUC 0.957 on 790 labelled rows (base rate 10.9%), trained on 1882 rows from the days before it.
- **72h on a held-out window:** AUC 0.962 on 697 labelled rows (base rate 13.6%), trained on 1069 rows from the days before it.
- **168h on a held-out window:** not evaluable -- no listing in the untouched window has had this horizon close inside the observation period
- **336h on a held-out window:** not evaluable -- no listing in the untouched window has had this horizon close inside the observation period

## Model C -- the time to sell

**Method:** CatBoost's `SurvivalAft`, an accelerated-failure-time model that takes right-censored observations natively.  A sale is an event; an expiry and an unobserved listing are censored at the moment the account *knew* the item had not sold.

- concordance index on the walk-forward folds: **0.908** (spread 0.885 .. 0.932)

On the untouched test window the concordance index is 0.944 over 1485 rows.
The median observed time to sale was 21.6 hours against a median predicted 30.8 hours.

## Model D -- the listing-price strategy

This is **not** a fourth model.  It is the decision layer: it takes the predicted price, the predicted probability of selling and the deadline, and evaluates a grid of candidate asks.

**How the two probability sources are combined.**  The *ask* dimension comes from the measured demand curve: it is an observation of how often a listing at that ask sold, and the model cannot be trusted outside the range of asks it was trained on.  The *item* dimension comes from the model, which ranks items well -- it separates the things that sell from the things that do not -- and is scaled into the curve as a lift.  Each source is used where it is strong, and the composition is stated rather than hidden.

Each fold is replayed with models fitted **only on the days before it**, so the strategy never advises on a listing its model has already seen.  1,566 listings with a known outcome were replayed in total, of which 208 sold.

| fold | listings | sold | median recommended / actual | on the sales | on the expiries | median gap |
|---|---|---|---|---|---|---|
| fold2 | 604 | 113 | 0.31 | 1.34 | 0.25 | 75.6g |
| fold3 | 962 | 95 | 0.24 | 1.38 | 0.22 | 131.0g |

A recommendation of **1.0x** the ask that was actually used means the strategy agrees with the seller.  Above one means it would have asked more; below one, less.

- across the folds the median recommendation sits at 0.28x the ask that was actually used
- on the listings that **sold** it is 1.36x the ask that cleared the market
- on the listings that **expired** it is 0.23x the ask that did not
- the recommendations are a median of 103.3g away from the ask that was actually used
- the mean regret is 2.2g per listing

**The trade-off, in plain words.**  On the listings that sold the strategy asks *more* than the seller did -- the market was paying that much.  On the listings that expired it asks a fraction of what the seller asked, and that is where the money is: most of the account's listings were priced where nothing was buying.  The regret is the price left on the table on the few sales where the strategy recommended less than the seller did.  It is the cost of the strategy, and it is paid deliberately.

**The assumption, stated rather than hidden.**  A counterfactual ask cannot be observed, so the evaluation rests on *monotone demand*: a lower ask is at least as likely to sell.  Under that assumption a listing that sold at `A` would also have sold at any `P <= A` -- so recommending `P` instead of `A` left `A - P` on the table -- and a listing that expired at `A` would also have expired at any `P >= A`.  The regret is therefore an upper bound on what the strategy gives up, not a claim that the strategy maximises realised gold.  It does not, and no model on this data can prove that it does.

**The second limit.**  The sale probability of the *ask* comes from the measured curve, which is an observation; the sale probability of the *item* comes from the model, which has only seen the prices this account actually asked.  A candidate ask far outside that behaviour is an extrapolation, and the engine says so in the answer it gives rather than presenting it as a measurement.

### What the strategy would have collected

The demand curve is *measured*, so applying it to a different ask is an estimate over a relationship the data actually observed -- not a realised result, but the closest thing to a counterfactual that this data allows.

|  | median ask | expected gold per listing |
|---|---|---|
| the ask that was used | 4.00x the regional sale average | 0.189 |
| the ask the strategy would have used | 1.08x the regional sale average | 0.579 |

**The expected gold per listing improves by 206.3%** over the asking behaviour that was actually used, and 99.8% of the recommendations land in the band the curve says is the best (1x to 2x the regional sale average).

The account's own median listing asks 4.00x the regional sale average; the strategy's median is 1.08x.  The whole difference is the inflated asking reference, taken out of the price and put into the sale rate.

Over 1,620 replayed listings.  The sale rate is interpolated from the measured demand curve, so this is an estimate over a relationship the data observed; it is not a realised result.

## What the engine cannot do

| limit | what it means |
|---|---|
| The window is 6 days | the transactions run from 16 to 21 October.  The walk-forward has 2 folds and a test day.  More history would sharpen every score here; none of it is available yet. |
| The prices are the ask | 99.7 % of the sales closed at exactly the posted buyout, because the account posts buyout-only auctions.  The price model therefore learns the market's clearing level, not a bidding behaviour. |
| The outcomes arrive by mail | a sale or an expiry is recorded when the account opens the mail, so every duration is an upper bound.  The models are deliberately conservative because of it. |
| The demand signal has no clock | the DfA counters are a live aggregate.  A demand feature describes today's audience applied to a historical row, and the feature set says so. |
| The market depth is missing | `num_auctions` has no history, so competition is represented only by the regional sold-per-day and sale-rate estimates. |
| The horizon cannot exceed the auction | a 7-day or 14-day horizon is not a longer sale probability; it is the same probability with more time in which the auction has already ended. |

## Reproducing this

```
python scripts/dfa_ml.py audit      # the data-readiness report
python scripts/dfa_ml.py train      # walk-forward, test window, models
python scripts/dfa_ml.py recommend  # a listing recommendation
```

