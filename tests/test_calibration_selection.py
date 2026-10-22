"""A candidate must not be scored by the calibrator that was fitted on it.

Isotonic regression reproduces its training labels almost exactly.  When
the selection block's log loss is computed as
``cal.transform(p_cal)`` with ``cal`` fitted on ``p_cal``, every candidate
scores ~1e-6, the selection is decided by numerical noise, and the report
claims a model that looks perfect.
"""
from __future__ import annotations

import numpy as np
import pytest

from woweconomy.ml.sentinel import (classification_metrics,
                                    crossfit_calibrated_probabilities,
                                    fit_calibrator)


def _separable(n: int = 600, seed: int = 3):
    """A signal a model can learn but not perfectly."""
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.3).astype(float)
    p = np.clip(0.3 * y + rng.normal(0.35, 0.18, n), 1e-3, 1 - 1e-3)
    return y, p


def _well_separated(n: int = 600, seed: int = 5):
    """Two clearly distinct score clusters: isotonic can memorise them."""
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.5).astype(float)
    p = np.where(y == 1, 0.78, 0.22) + rng.normal(0.0, 0.02, n)
    return y, np.clip(p, 1e-3, 1 - 1e-3)


def test_scoring_a_calibrator_on_its_own_fit_rows_is_near_perfect():
    """This is the defect, pinned so it cannot come back unnoticed.

    On well-separated scores isotonic memorises the calibration block: it
    returns exactly two values, the floor and its mirror, and the in-sample
    log loss drops to whatever that floor implies.  Before the floor
    existed the clip was ``1e-6``, which is where the production report's
    ``selection logloss = 1.000000500029089e-06`` came from.
    """
    y, p = _well_separated()
    cal = fit_calibrator(y, p)
    fitted = cal.transform(p)
    in_sample = classification_metrics(y, fitted)["logloss"]
    out_of_fold = classification_metrics(
        y, crossfit_calibrated_probabilities(y, p))["logloss"]
    assert len(np.unique(fitted)) == 2, (
        "isotonic no longer saturates here, so this test is measuring "
        "something else")
    assert in_sample < 0.01, "the in-sample score should be implausibly good"
    assert out_of_fold > in_sample, (
        f"in-sample {in_sample:.3g} vs out-of-fold {out_of_fold:.3g}: "
        "the in-sample score is not the optimistic one, so cross-fitting "
        "is not doing anything")


def test_in_sample_calibration_is_optimistic_even_when_scores_overlap():
    y, p = _separable()
    cal = fit_calibrator(y, p)
    in_sample = classification_metrics(y, cal.transform(p))["logloss"]
    out_of_fold = classification_metrics(
        y, crossfit_calibrated_probabilities(y, p))["logloss"]
    assert out_of_fold > in_sample, (
        "an in-sample calibration score should never be the better one")


def test_cross_fitted_scores_are_not_near_perfect():
    y, p = _separable()
    out = crossfit_calibrated_probabilities(y, p)
    metrics = classification_metrics(y, out)
    assert metrics["logloss"] > 0.05, (
        f"cross-fitted log loss {metrics['logloss']:.6f} is still too good; "
        "the folds are not isolating the calibrator")


def test_cross_fitting_keeps_the_length_and_the_order():
    y, p = _separable(n=300)
    out = crossfit_calibrated_probabilities(y, p)
    assert len(out) == len(p)
    assert np.all((out > 0) & (out < 1))


def test_cross_fitting_is_deterministic():
    y, p = _separable()
    a = crossfit_calibrated_probabilities(y, p)
    b = crossfit_calibrated_probabilities(y, p)
    assert np.array_equal(a, b)


def test_a_row_is_never_scored_by_a_calibrator_that_saw_it():
    """A fold's scores must depend only on the rows outside that fold."""
    y, p = _separable(n=500)
    base = crossfit_calibrated_probabilities(y, p, folds=5)
    # fold 0 is rows [0:100); change only those labels
    y2, p2 = y.copy(), p.copy()
    y2[0:100] = 1.0 - y2[0:100]
    moved = crossfit_calibrated_probabilities(y2, p2, folds=5)
    assert np.array_equal(base[0:100], moved[0:100]), (
        "fold 0's scores moved when fold 0's own labels changed, so the "
        "calibrator that produced them had seen fold 0")


def test_too_few_rows_falls_back_to_the_raw_probability():
    y = np.array([0.0, 1.0, 0.0, 1.0])
    p = np.array([0.1, 0.9, 0.2, 0.8])
    out = crossfit_calibrated_probabilities(y, p, folds=5, min_rows=40)
    assert np.allclose(out, p)


def test_a_single_class_block_does_not_raise():
    y = np.zeros(200)
    p = np.linspace(0.01, 0.5, 200)
    out = crossfit_calibrated_probabilities(y, p)
    assert len(out) == 200
    assert np.all(np.isfinite(out))


def test_a_better_predictor_still_wins_under_cross_fitted_scoring():
    """The fix must not destroy the ability to tell candidates apart."""
    rng = np.random.default_rng(11)
    n = 1200
    y = (rng.random(n) < 0.35).astype(float)
    strong = np.clip(0.6 * y + rng.normal(0.3, 0.15, n), 1e-3, 1 - 1e-3)
    weak = np.clip(0.3 + rng.normal(0.0, 0.05, n), 1e-3, 1 - 1e-3)
    strong_ll = classification_metrics(
        y, crossfit_calibrated_probabilities(y, strong))["logloss"]
    weak_ll = classification_metrics(
        y, crossfit_calibrated_probabilities(y, weak))["logloss"]
    assert strong_ll < weak_ll - 0.05


# ---------------------------------------------------------------------------
# a calibration-block win that does not replicate must not be promoted
# ---------------------------------------------------------------------------

def test_test_confirmation_reads_the_selection_metric():
    from woweconomy.ml.sentinel import _test_confirms

    assert _test_confirms({"logloss": 0.30}, {"logloss": 0.42}, "logloss") is True
    assert _test_confirms({"logloss": 0.51}, {"logloss": 0.42}, "logloss") is False
    assert _test_confirms({"logloss": 0.42}, {"logloss": 0.42}, "logloss") is False


def test_test_confirmation_is_unknown_when_the_metric_is_absent():
    from woweconomy.ml.sentinel import _test_confirms

    assert _test_confirms({}, {"logloss": 0.4}, "logloss") is None
    assert _test_confirms({"logloss": float("nan")}, {"logloss": 0.4}, "logloss") is None


def test_an_unconfirmed_model_may_not_export_a_probability():
    """The fallback path is the whole point of the status."""
    from woweconomy.ml.sentinel import PROBABILITY_STATUSES

    assert "validated" in PROBABILITY_STATUSES
    assert "baseline_only" in PROBABILITY_STATUSES
    assert "validated_unconfirmed" not in PROBABILITY_STATUSES
    assert "degenerate" not in PROBABILITY_STATUSES


# ---------------------------------------------------------------------------
# a calibrator may not claim certainty its sample size cannot support
# ---------------------------------------------------------------------------

def test_the_probability_floor_follows_laplaces_rule():
    from woweconomy.ml.sentinel import Calibrator, fit_calibrator

    y, p = _well_separated(n=800)
    cal = fit_calibrator(y, p)
    assert cal.method == "isotonic"
    assert cal.floor == pytest.approx(1.0 / 802.0, rel=0.01)
    assert cal.floor > 1e-6


def test_an_identity_calibrator_keeps_the_raw_probability():
    from woweconomy.ml.sentinel import Calibrator

    cal = Calibrator()
    assert cal.floor == pytest.approx(1e-6)
    out = cal.transform(np.array([1e-9, 0.5, 1.0 - 1e-9]))
    assert out[0] == pytest.approx(1e-6)
    assert out[1] == pytest.approx(0.5)


def test_isotonic_saturation_is_clipped_to_the_supported_floor():
    """The defect that made log loss worse than the baseline."""
    from woweconomy.ml.sentinel import fit_calibrator

    y, p = _well_separated(n=400)
    cal = fit_calibrator(y, p)
    # far outside the observed range: isotonic returns exactly 0 or 1
    out = cal.transform(np.array([-5.0, 5.0]))
    assert out.min() >= cal.floor - 1e-12
    assert out.max() <= 1.0 - cal.floor + 1e-12
    assert out.min() > 1e-6, "the clip fell back to 'one in a million'"


def test_clipping_saturation_reduces_the_cost_of_a_confident_miss():
    from woweconomy.ml.sentinel import fit_calibrator

    y, p = _well_separated(n=400)
    cal = fit_calibrator(y, p)
    capped = float(-np.log(cal.transform(np.array([-5.0]))[0]))
    uncapped = float(-np.log(1e-6))
    assert capped < uncapped - 3.0, (
        f"a confident miss still costs {capped:.2f} nats; the floor did nothing")
