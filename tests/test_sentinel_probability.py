"""Guards on the sentinel's probability path.

A classifier that is asked for a probability must not answer with a class
label.  When it does, the calibrator is fitted on two distinct inputs, it
collapses to two knots, and every item in a realm is exported with the
same sale probability -- a number that looks calibrated and carries no
information about the item.  These tests pin the distinction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from woweconomy.ml.sentinel import (
    _sklearn_predict,
    fit_calibrator,
)

sklearn = pytest.importorskip("sklearn")

from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.linear_model import LogisticRegression, Ridge  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402


def make_frame(rows: int = 400, seed: int = 7) -> pd.DataFrame:
    """Two informative features, so a classifier has something to learn."""
    rng = np.random.default_rng(seed)
    x1 = rng.normal(size=rows)
    x2 = rng.normal(size=rows)
    logit = 1.6 * x1 - 0.9 * x2 - 0.3
    y = (rng.random(rows) < 1.0 / (1.0 + np.exp(-logit))).astype(int)
    return pd.DataFrame({"x1": x1, "x2": x2, "y": y})


def fit_logistic(frame: pd.DataFrame) -> Pipeline:
    """The same shape the sentinel stores: imputer, scaler, logistic."""
    model = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("model", LogisticRegression(max_iter=500, random_state=0)),
    ])
    model.fit(frame[["x1", "x2"]], frame["y"])
    return model


def test_a_classifier_is_asked_for_a_probability_not_a_label():
    frame = make_frame()
    model = fit_logistic(frame)
    p = _sklearn_predict(model, frame, ["x1", "x2"])

    assert p.shape == (len(frame),)
    assert p.min() > 0.0 and p.max() < 1.0
    assert set(np.unique(p)) - {0.0, 1.0} == set(np.unique(p)), "labels leaked"
    assert len(np.unique(np.round(p, 6))) > 50, "the score has no resolution"
    # it is the probability, not the label
    expected = model.predict_proba(frame[["x1", "x2"]])[:, 1]
    np.testing.assert_allclose(p, expected, rtol=0, atol=1e-12)


def test_a_regressor_keeps_its_prediction():
    frame = make_frame()
    ridge = Ridge().fit(frame[["x1", "x2"]], frame["y"].astype(float))
    out = _sklearn_predict(ridge, frame, ["x1", "x2"])

    assert out.shape == (len(frame),)
    assert not np.all(np.isin(out, [0.0, 1.0])), "a regressor must not be binarised"
    np.testing.assert_allclose(out, ridge.predict(frame[["x1", "x2"]]), atol=1e-12)


def test_missing_columns_are_imputed_rather_than_dropped():
    frame = make_frame()
    model = fit_logistic(frame)
    sparse = frame[["x1"]].copy()  # x2 absent entirely
    out = _sklearn_predict(model, sparse, ["x1", "x2"])

    assert out.shape == (len(frame),)
    assert np.all(np.isfinite(out))
    assert out.min() > 0.0 and out.max() < 1.0


def test_calibration_keeps_its_resolution_when_fed_probabilities():
    frame = make_frame(rows=800)
    model = fit_logistic(frame)
    p = _sklearn_predict(model, frame, ["x1", "x2"])
    cal = fit_calibrator(frame["y"].to_numpy(dtype=float), p)

    assert cal.method == "isotonic"
    transformed = cal.transform(p)
    # Isotonic regression is piecewise constant, so the count is the number
    # of steps, not the number of rows.  What matters is that it is not the
    # two-value step function a label-based fit produces.
    steps = len(np.unique(np.round(transformed, 6)))
    assert steps > 5, "the calibrator collapsed to a step function"
    assert steps < len(frame), "the calibrator is interpolating, not calibrating"
    # monotone: a better raw score never becomes a worse probability
    order = np.argsort(p)
    assert np.all(np.diff(transformed[order]) >= -1e-12)


def test_labels_as_probabilities_would_collapse_the_calibrator():
    """The regression: the old behaviour is what the guard above catches."""
    frame = make_frame(rows=800)
    model = fit_logistic(frame)
    labels = model.predict(frame[["x1", "x2"]]).astype(float)

    cal = fit_calibrator(frame["y"].to_numpy(dtype=float), labels)
    collapsed = cal.transform(labels)

    assert len(np.unique(np.round(labels, 6))) == 2
    assert len(np.unique(np.round(collapsed, 6))) <= 2, (
        "this is the failure the fix prevents: two values for every item")
