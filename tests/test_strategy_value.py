"""The strategy's improvement claim must carry its uncertainty."""
from __future__ import annotations

import numpy as np
import pandas as pd

from woweconomy.ml.pipeline import DfaLearningEngine, _best_measured_band


def frame(actual, recommended):
    return pd.DataFrame({"actual_expected": actual,
                         "recommended_expected": recommended})


def test_a_clear_improvement_has_an_interval_above_one():
    rng = np.random.default_rng(0)
    actual = rng.uniform(0.1, 0.2, 400)
    ci = DfaLearningEngine._bootstrap_ratio(frame(actual, actual * 2), seed=7)
    assert ci is not None
    assert ci[0] > 1.5 and ci[1] < 2.5


def test_no_improvement_has_an_interval_that_contains_one():
    rng = np.random.default_rng(0)
    actual = rng.uniform(0.1, 0.2, 400)
    noisy = actual * rng.uniform(0.9, 1.1, 400)
    ci = DfaLearningEngine._bootstrap_ratio(frame(actual, noisy), seed=7)
    assert ci is not None
    assert ci[0] < 1.0 < ci[1]


def test_the_interval_is_reproducible():
    rng = np.random.default_rng(0)
    actual = rng.uniform(0.1, 0.2, 300)
    recommended = actual * rng.uniform(1.0, 1.6, 300)
    first = DfaLearningEngine._bootstrap_ratio(frame(actual, recommended), seed=11)
    second = DfaLearningEngine._bootstrap_ratio(frame(actual, recommended), seed=11)
    assert first == second


def test_too_few_rows_give_no_interval():
    assert DfaLearningEngine._bootstrap_ratio(
        frame([0.1] * 5, [0.2] * 5), seed=1) is None


def test_a_row_that_cannot_be_valued_does_not_poison_the_interval():
    """A NaN ``sale_rate`` used to make the interval [nan, nan].

    pandas' ``.mean()`` skips a NaN and numpy's does not, so the published
    point estimate and its interval were computed over different rows --
    and the interval, being numpy, was not a number at all.  Both now use
    the rows where both values are finite.
    """
    import math

    actual = [0.1] * 40 + [0.1] * 10
    recommended = [0.2] * 40 + [float("nan")] * 10
    ci = DfaLearningEngine._bootstrap_ratio(frame(actual, recommended), seed=3)
    assert ci is not None
    assert all(math.isfinite(value) for value in ci)
    assert ci[0] > 1.5 and ci[1] < 2.5

    kept_actual, kept_recommended = DfaLearningEngine._finite_pairs(
        frame(actual, recommended))
    assert len(kept_actual) == len(kept_recommended) == 40


def test_the_best_measured_band_is_the_one_with_the_highest_expected_gold():
    curve = {"bands": [
        {"band": "a", "mid_ratio": 0.5, "sale_rate": 0.6,
         "expected_gold_ratio": 0.3, "n": 10},
        {"band": "b", "mid_ratio": 1.0, "sale_rate": 0.55,
         "expected_gold_ratio": 0.55, "n": 20},
        {"band": "c", "mid_ratio": 2.0, "sale_rate": 0.2,
         "expected_gold_ratio": 0.4, "n": 30},
    ]}
    best = _best_measured_band(curve)
    assert best["band"] == "b"
    assert best["n"] == 20


def test_no_curve_means_no_band():
    assert _best_measured_band(None) is None
    assert _best_measured_band({"bands": []}) is None
