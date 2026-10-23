# DFA price-model comparison

Target: `y_net_unit_log`  
Nominal interval level: 0.8

All models were fitted on the training block, all interval widths
were calibrated on the calibration block, and every score below was
measured on the same final test rows. The test block did not choose a
model, a segment or a threshold.

## Chronological cut

| block | first timestamp | last timestamp | rows |
|---|---:|---:|---:|
| train | 1792134314 | 1792351094 | 263 |
| calibration | 1792351100 | 1792450367 | 96 |
| test | 1792450378 | 1792585215 | 120 |

Common test rows: **120** (dropped for a missing prediction: 0).

## Head-to-head

| model | n | MAE log | 95% CI | RMSE log | median ratio | within 50% | interval width | coverage | coverage 95% CI |
|---|---:|---:|---|---:|---:|---:|---:|---:|---|
| record_price_catboost | 120 | 0.436 | 0.359 to 0.530 | 0.627 | 1.16 | 66% | 0.819 | 83% | 0.757 to 0.889 |
| sentinel_price_catboost | 120 | 0.421 | 0.348 to 0.503 | 0.595 | 1.24 | 65% | 0.799 | 86% | 0.785 to 0.910 |
| regional_sale_avg_baseline | 120 | 0.516 | 0.424 to 0.614 | 0.776 | 1.11 | 64% | 1.028 | 87% | 0.794 to 0.916 |
| item_median_baseline | 120 | 0.914 | 0.693 to 1.173 | 1.557 | 1.00 | 61% | 1.203 | 76% | 0.674 to 0.826 |
| shipped_linear | 120 | 0.431 | 0.362 to 0.510 | 0.595 | 1.14 | 63% | 0.976 | 91% | 0.843 to 0.948 |

## Paired difference from the item-median baseline

A negative mean means the model had the lower absolute log error.
The interval is a paired bootstrap over the same test rows.

| model | mean delta MAE log | 95% CI | bootstrap P(better) |
|---|---:|---|---:|
| record_price_catboost | -0.478 | -0.706 to -0.276 | 1.00 |
| sentinel_price_catboost | -0.493 | -0.688 to -0.272 | 1.00 |
| regional_sale_avg_baseline | -0.398 | -0.607 to -0.199 | 1.00 |
| item_median_baseline | 0.000 | 0.000 to 0.000 | 0.00 |
| shipped_linear | -0.484 | -0.728 to -0.280 | 1.00 |

## Pairwise differences

A negative mean means model **a** had the lower absolute log error.
An interval that includes zero does not establish a difference.

| a | b | mean delta MAE(a - b) | 95% CI | bootstrap P(a better) |
|---|---|---:|---|---:|
| record_price_catboost | sentinel_price_catboost | 0.015 | -0.010 to 0.043 | 0.12 |
| record_price_catboost | regional_sale_avg_baseline | -0.080 | -0.153 to -0.007 | 0.98 |
| record_price_catboost | item_median_baseline | -0.478 | -0.700 to -0.286 | 1.00 |
| record_price_catboost | shipped_linear | 0.006 | -0.054 to 0.062 | 0.44 |
| sentinel_price_catboost | regional_sale_avg_baseline | -0.095 | -0.168 to -0.029 | 1.00 |
| sentinel_price_catboost | item_median_baseline | -0.493 | -0.703 to -0.274 | 1.00 |
| sentinel_price_catboost | shipped_linear | -0.009 | -0.058 to 0.046 | 0.62 |
| regional_sale_avg_baseline | item_median_baseline | -0.398 | -0.615 to -0.201 | 1.00 |
| regional_sale_avg_baseline | shipped_linear | 0.086 | 0.013 to 0.172 | 0.00 |
| item_median_baseline | shipped_linear | 0.484 | 0.267 to 0.736 | 0.00 |

## Segment report (descriptive only)

| segment | n | share | record_price_catboost | sentinel_price_catboost | regional_sale_avg_baseline | item_median_baseline | shipped_linear |
|---|---:|---:|---:|---:|---:|---:|---:|
| consumables | 8 | 7% | 0.464 (cov 88%) | 0.497 (cov 88%) | 0.433 (cov 88%) | 0.681 (cov 88%) | 0.416 (cov 88%) |
| materials | 34 | 28% | 0.382 (cov 85%) | 0.355 (cov 91%) | 0.428 (cov 94%) | 0.623 (cov 91%) | 0.339 (cov 97%) |
| other | 78 | 65% | 0.457 (cov 82%) | 0.442 (cov 83%) | 0.563 (cov 83%) | 1.065 (cov 68%) | 0.472 (cov 88%) |

## What this does not say

* These are refitted model families, not the promoted artifact bytes.
* A 100-sale test cannot resolve small differences; read the paired
  bootstrap interval, not only the ranking.
* Segment coverage is descriptive. It is not a per-segment 80% guarantee.
* The historical baseline is only as good as the regional reference it
  uses; its coverage is reported beside the model rows.
