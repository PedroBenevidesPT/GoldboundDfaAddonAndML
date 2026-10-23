"""DFA Sentinel: the validated prediction layer.

This module deliberately sits *on top of* the existing feature builder and
CatBoost models instead of replacing them.  The feature frame produced by
:class:`woweconomy.ml.pipeline.DfaLearningEngine` is already point-in-time:
every column is computed from the state of the market at the posting
timestamp.  What was missing was the part that makes a prediction
trustworthy:

* a calibrated probability, not a raw tree score;
* a chronological train / calibration / test split, so calibration and
  scoring never see the future;
* simple baselines to beat (item sale rate, item median price, base rate);
* a separate target for market undercut pressure and price decline,
  built from timestamped TSM snapshots rather than from asking prices;
* an explicit ``unknown`` answer when there is not enough evidence;
* versioned, checksummed artifacts that are promoted atomically.

The two model families are used together, not as an ensemble for its own
sake: scikit-learn supplies the calibrated linear baselines and the
calibrators, CatBoost supplies the non-linear candidate.  On the
chronological holdout the lower Brier / MAE wins, and the report records
which one won.  A model is never called an improvement unless it beats
the simple baseline it was compared with.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd

from .config import COPPER_PER_GOLD
from .features import ASK_FEATURES
from . import label_audit
from . import snapshot_panel
from .intervals import (CalibratedInterval, SegmentedInterval,
                        calibrate_interval, calibrate_segmented_interval)
from .models import (
    CATBOOST_AVAILABLE,
    NON_FEATURE_COLUMNS,
    PriceModel,
    TimeToSaleModel,
    categorical_columns,
    concordance_index,
    feature_columns,
    load_sale_probability_model,
    load_time_to_sale_model,
    make_pool,
    price_metrics,
    probability_metrics,
    train_price_model,
    train_sale_probability_model,
    train_time_to_sale_model,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard, tested through the unavailable path
    from sklearn.impute import SimpleImputer
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    SKLEARN_AVAILABLE = True
except ImportError:  # pragma: no cover
    SKLEARN_AVAILABLE = False
    SimpleImputer = IsotonicRegression = LogisticRegression = Ridge = None  # type: ignore
    Pipeline = StandardScaler = None  # type: ignore

try:  # pragma: no cover
    from catboost import CatBoostClassifier, CatBoostRegressor

    CATBOOST_AVAILABLE = True
except ImportError:  # pragma: no cover
    CatBoostClassifier = CatBoostRegressor = None  # type: ignore
    CATBOOST_AVAILABLE = False


#: Bumped whenever the artifact layout or the prediction contract changes.
SENTINEL_SCHEMA = "dfa-sentinel/1"
#: Bumped whenever the meaning or the set of model features changes.
FEATURE_SCHEMA = "dfa-pointintime/1"
#: The dataset schema the addon reader and this exporter agree on.
DATASET_SCHEMA = 2

#: The horizons the Sentinel exports.  A 7-day label needs a seven-day
#: observation window; when the archive is shorter the target is reported
#: as ``insufficient_data`` rather than silently fitted on a subset.
DEFAULT_HORIZONS_H: Tuple[int, ...] = (24, 48, 168)

#: Risk targets are market-level measurements.  They are deliberately
#: named ``undercut_pressure`` and not "your listing was undercut": a
#: snapshot shows the cheapest listing, not what happened to our auction.
RISK_TARGETS: Tuple[str, ...] = ("undercut_pressure", "price_decline")
RISK_HORIZONS_H: Tuple[int, ...] = (24, 48)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_log(value: object) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(v) or v <= 0:
        return float("nan")
    return math.log(v)


def _clean(value: object) -> float:
    """A TSM value that is not the sentinel and not a non-number."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(v) or v <= 0 or v >= 99_999_999_000:
        return float("nan")
    return v


# ---------------------------------------------------------------------------
# configuration and the explicit data thresholds
# ---------------------------------------------------------------------------

@dataclass
class SentinelConfig:
    """Training knobs and the minimum evidence each target needs."""

    models_root: Path = Path(r"C:\FarmAnalysis\models\dfa_sentinel")
    tsm_db: Path = Path(r"C:\FarmAnalysis\tsm_markets.db")
    #: Our own auction-house store.  ``panel_source`` decides which of the
    #: two supplies the market panel behind the risk targets.
    intel_db: Path = Path(r"C:\FarmAnalysis\data\intel.sqlite")
    reports_dir: Path = Path(r"C:\FarmAnalysis\reports\dfa_sentinel")
    #: ``"tsm"`` keeps the historical behaviour; ``"blizzard"`` builds the
    #: panel from our own snapshots (every fetched book, variants kept
    #: apart); ``"auto"`` prefers Blizzard once it can actually teach a
    #: temporal target, and falls back to TSM otherwise.
    panel_source: str = "auto"
    min_blizzard_panel_rows: int = 1_000
    #: A panel must span the horizon it is asked about.  A sweep that has
    #: only been running for a day cannot close a 48h target no matter how
    #: many rows it holds, so span -- not row count -- is the gate.  72h
    #: leaves a margin over the longest horizon the risk models use.
    min_blizzard_span_h: float = 72.0

    horizons_h: Tuple[int, ...] = DEFAULT_HORIZONS_H
    risk_horizons_h: Tuple[int, ...] = RISK_HORIZONS_H

    seed: int = 20261021
    test_fraction: float = 0.25
    calibration_fraction: float = 0.20
    iterations: int = 300

    #: The nominal level of the realized-price interval.  The measured
    #: coverage travels with it; the two are never conflated.
    price_interval_level: float = 0.8

    #: Minimum calibration rows per segment before a conditional width is
    #: allowed to replace the global one.  Thin segments fall back to the
    #: global width, so a small calibration block cannot manufacture a
    #: narrow interval.
    min_interval_segment_rows: int = 40

    #: Minimum rows / positives before a target is trained at all.  These
    #: are evidence thresholds, not hyper-parameters: below them the right
    #: answer is "unknown", not a number with a wide error bar.
    min_sale_labels: int = 120
    min_sale_positives: int = 30
    min_price_rows: int = 80
    min_tts_rows: int = 80
    min_tts_events: int = 20
    min_risk_rows: int = 150
    min_risk_positives: int = 20

    #: A candidate has to beat the simple baseline by this much on the
    #: untouched test window before the report calls it an improvement.
    min_probability_gain: float = 0.002   # Brier
    min_price_gain: float = 0.01          # MAE on the log scale
    min_risk_gain: float = 0.002          # Brier

    #: Minimum calibrated C-index before a time-to-sale number is shown.
    min_tts_c_index: float = 0.60
    #: Even a model that ranks durations well must also get the absolute
    #: duration roughly right before it is allowed to quote hours.  This is
    #: the share of observed sales whose prediction is within a factor of
    #: two.  It protects against a model that only separates "sold" from
    #: "did not sell" but cannot say how long a sale takes.
    min_tts_within_2x: float = 0.60

    #: A prediction older than this is marked stale by consumers.
    max_model_age_h: float = 24.0 * 14
    keep_versions: int = 5

    def ensure_dirs(self) -> None:
        self.models_root.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# chronological split, calibration and metrics
# ---------------------------------------------------------------------------

@dataclass
class ChronologicalSplit:
    train: pd.DataFrame
    calibration: pd.DataFrame
    test: pd.DataFrame
    train_span: Tuple[int, int] = (0, 0)
    calibration_span: Tuple[int, int] = (0, 0)
    test_span: Tuple[int, int] = (0, 0)
    note: str = ""

    @property
    def usable(self) -> bool:
        return not (self.train.empty or self.calibration.empty or self.test.empty)


def chronological_split(frame: pd.DataFrame,
                        test_fraction: float = 0.25,
                        calibration_fraction: float = 0.20,
                        ts_column: str = "ts") -> ChronologicalSplit:
    """Cut a frame by time: train | calibration | untouched test.

    The cut is made on *unique timestamps*, not on rows, so two listings
    posted at the same moment can never land on different sides.  The
    assertions are the leakage guard: if a future timestamp ever appears
    in an earlier block the split refuses to return.
    """
    empty = ChronologicalSplit(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    if frame.empty or ts_column not in frame.columns:
        empty.note = "empty frame or no timestamp column"
        return empty
    ordered = frame.sort_values(ts_column, kind="mergesort")
    times = sorted(ordered[ts_column].dropna().unique().tolist())
    if len(times) < 3:
        empty.note = f"only {len(times)} distinct timestamps; chronological split impossible"
        return empty

    n_test = max(1, int(round(len(times) * test_fraction)))
    n_cal = max(1, int(round(len(times) * calibration_fraction)))
    if len(times) - n_test - n_cal < 1:
        n_cal = max(1, (len(times) - n_test) // 3)
    train_times = times[:len(times) - n_test - n_cal]
    cal_times = times[len(times) - n_test - n_cal:len(times) - n_test]
    test_times = times[len(times) - n_test:]
    if not train_times or not cal_times or not test_times:
        empty.note = "not enough timestamps for train/calibration/test"
        return empty

    train = ordered[ordered[ts_column].isin(train_times)].copy()
    cal = ordered[ordered[ts_column].isin(cal_times)].copy()
    test = ordered[ordered[ts_column].isin(test_times)].copy()
    if max(train[ts_column]) >= min(cal[ts_column]):
        raise AssertionError("chronological split leaked: train reaches calibration")
    if max(cal[ts_column]) >= min(test[ts_column]):
        raise AssertionError("chronological split leaked: calibration reaches test")
    return ChronologicalSplit(
        train=train, calibration=cal, test=test,
        train_span=(int(min(train_times)), int(max(train_times))),
        calibration_span=(int(min(cal_times)), int(max(cal_times))),
        test_span=(int(min(test_times)), int(max(test_times))))


@dataclass
class Calibrator:
    """A fitted probability calibrator, or an explicit identity."""

    method: str = "identity"          # identity | isotonic | platt
    payload: object = None
    n: int = 0
    note: str = ""

    @property
    def floor(self) -> float:
        """The smallest probability this calibrator has evidence for.

        Isotonic regression saturates: outside the observed score range it
        returns exactly 0 or exactly 1, and a clip at ``1e-6`` then turns
        that into "one in a million".  With ~800 calibration rows that is
        an assertion the data cannot support, and a single wrong row at
        ``p = 1e-6`` costs 13.8 nats -- more log loss than the rest of the
        test set combined.

        Laplace's rule of succession gives ``1 / (n + 2)``: the smallest
        probability you can defend after seeing ``n`` outcomes.  Below
        that the model is claiming certainty it never observed.
        """
        if self.payload is None or self.n <= 0:
            return 1e-6
        return max(1e-6, 1.0 / (float(self.n) + 2.0))

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1 - 1e-6)
        if self.method == "isotonic" and self.payload is not None:
            out = self.payload.predict(p)
        elif self.method == "platt" and self.payload is not None:
            logit = np.log(p / (1.0 - p)).reshape(-1, 1)
            out = self.payload.predict_proba(logit)[:, 1]
        else:
            return p
        floor = self.floor
        return np.clip(out, floor, 1.0 - floor)


def fit_calibrator(y: np.ndarray, p: np.ndarray,
                   min_rows: int = 40) -> Calibrator:
    """Fit isotonic (preferred) or Platt scaling on a held-out block.

    Calibration is fitted only on the middle chronological block; the test
    block is never used.  With too little data the identity is returned
    and the report says so, rather than fitting a curve to noise.
    """
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    if len(y) < min_rows or len(np.unique(y)) < 2:
        return Calibrator(method="identity", n=int(len(y)),
                          note="too few calibration rows; raw probability kept")
    if not SKLEARN_AVAILABLE:  # pragma: no cover
        return Calibrator(method="identity", n=int(len(y)),
                          note="scikit-learn is not installed")
    positives = int(y.sum())
    if positives >= 10 and len(y) - positives >= 10:
        try:
            iso = IsotonicRegression(out_of_bounds="clip")
            iso.fit(p, y)
            return Calibrator(method="isotonic", payload=iso, n=int(len(y)))
        except Exception as exc:  # pragma: no cover - numerical guard
            logger.warning("isotonic calibration failed: %s", exc)
    try:
        logit = np.log(p / (1.0 - p)).reshape(-1, 1)
        platt = LogisticRegression(C=1.0, max_iter=1000, solver="lbfgs")
        platt.fit(logit, y.astype(int))
        return Calibrator(method="platt", payload=platt, n=int(len(y)))
    except Exception as exc:  # pragma: no cover
        logger.warning("Platt calibration failed: %s", exc)
        return Calibrator(method="identity", n=int(len(y)),
                          note=f"calibration failed: {exc}")


def crossfit_calibrated_probabilities(y: np.ndarray, p: np.ndarray, *,
                                      folds: int = 5,
                                      min_rows: int = 40) -> np.ndarray:
    """Calibrated probabilities where no row is scored by its own calibrator.

    Isotonic regression fitted on ``(p, y)`` reproduces ``y`` almost
    exactly, so scoring a candidate on the block its calibrator was fitted
    on drives log loss to ~1e-6 for *every* candidate and makes the
    selection score meaningless.  Each row here gets a probability from a
    calibrator that never saw it.

    The folds are contiguous, not random: the calibration block is ordered
    in time, and shuffling would let a later row inform an earlier one.
    """
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    n = len(y)
    if n == 0:
        return p
    k = max(2, min(int(folds), n))
    edges = np.linspace(0, n, k + 1).astype(int)
    out = p.copy()
    for i in range(k):
        lo, hi = int(edges[i]), int(edges[i + 1])
        if hi <= lo:
            continue
        mask = np.ones(n, dtype=bool)
        mask[lo:hi] = False
        if int(mask.sum()) < min_rows or len(np.unique(y[mask])) < 2:
            continue
        cal = fit_calibrator(y[mask], p[mask], min_rows=min_rows)
        out[lo:hi] = cal.transform(p[lo:hi])
    return out


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """Mean |predicted - observed| over equal-width probability bins."""
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), 0.0, 1.0)
    if not len(y):
        return float("nan")
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        if not mask.any():
            continue
        total += mask.mean() * abs(float(y[mask].mean()) - float(p[mask].mean()))
    return float(total)


def classification_metrics(y: np.ndarray, p: np.ndarray) -> Dict[str, float]:
    """Brier, log loss, AUC, ECE and a threshold view of the predictions."""
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    out = probability_metrics(y, p)
    out["ece"] = expected_calibration_error(y, p)
    out["brier_baseline"] = float(np.mean((y.mean() - y) ** 2)) if len(y) else float("nan")
    threshold = 0.5
    pred = p >= threshold
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    out["precision_at_0_5"] = float(tp / (tp + fp)) if tp + fp else float("nan")
    out["recall_at_0_5"] = float(tp / (tp + fn)) if tp + fn else float("nan")
    top_n = max(1, int(round(len(p) * 0.1)))
    order = np.argsort(-p)[:top_n]
    out["precision_at_top10pct"] = float(y[order].mean())
    out["lift_at_top10pct"] = (float(y[order].mean() / y.mean())
                               if y.mean() > 0 else float("nan"))
    return out


def regression_metrics(y_log: np.ndarray, pred_log: np.ndarray,
                       y_net: Optional[np.ndarray] = None) -> Dict[str, float]:
    out = price_metrics(np.asarray(y_log, dtype=float),
                        np.asarray(pred_log, dtype=float),
                        np.asarray(y_net, dtype=float) if y_net is not None else None)
    if len(y_log):
        true = np.exp(np.asarray(y_log, dtype=float))
        pred = np.exp(np.asarray(pred_log, dtype=float))
        out["median_absolute_percentage_error"] = float(
            np.median(np.abs(pred - true) / np.maximum(true, 1e-9)))
        out["p90_absolute_percentage_error"] = float(
            np.percentile(np.abs(pred - true) / np.maximum(true, 1e-9), 90))
    return out


# ---------------------------------------------------------------------------
# feature alignment: a missing feature is missing, never zero
# ---------------------------------------------------------------------------

def align_features(frame: pd.DataFrame, features: Sequence[str],
                   categoricals: Sequence[str]) -> pd.DataFrame:
    """Return exactly the model's columns, with missing values as NaN.

    The price model in the existing engine was trained on a frame richer
    than the one-row frame available at prediction time.  Silently
    dropping or zero-filling those columns would change the model's
    meaning, so they are added as NaN and the imputer/booster handles
    them explicitly.
    """
    # Built in one DataFrame rather than a column assignment per call: the
    # prediction path calls this ~1,200 times per export with one row, and
    # pandas charges per inserted column.  The columns, their order and the
    # values are exactly what the loop produced -- a categorical that is
    # also a feature keeps its position and gets the string treatment.
    categorical = set(categoricals)
    order = list(features) + [c for c in categoricals if c not in features]
    width = len(frame.index)
    data = {}
    for col in order:
        if col in frame.columns:
            series = frame[col]
            if col in categorical:
                data[col] = series.fillna("unknown").astype(str).to_numpy()
            else:
                data[col] = series.to_numpy()
        elif col in categorical:
            data[col] = np.full(width, "unknown", dtype=object)
        else:
            data[col] = np.full(width, np.nan, dtype=float)
    return pd.DataFrame(data, index=frame.index, columns=order)


def _baseline_item_rate(train: pd.DataFrame, label: str,
                        item_column: str = "item_id",
                        prior_weight: float = 10.0) -> Dict[str, float]:
    """Smoothed item sale rate, with the global rate as the prior."""
    if train.empty:
        return {"__global__": 0.0}
    global_rate = float(train[label].mean())
    out: Dict[str, float] = {"__global__": global_rate}
    for item, group in train.groupby(item_column):
        n = len(group)
        out[str(int(item))] = float(
            (group[label].sum() + prior_weight * global_rate) / (n + prior_weight))
    return out


def _apply_item_rate(rates: Dict[str, float], frame: pd.DataFrame,
                     item_column: str = "item_id") -> np.ndarray:
    prior = rates.get("__global__", 0.0)
    return np.array([rates.get(str(int(v)), prior) for v in frame[item_column]],
                    dtype=float)


def _item_median_price(train: pd.DataFrame, target: str = "y_net_unit_log",
                       item_column: str = "item_id") -> Dict[str, float]:
    if train.empty:
        return {"__global__": 0.0}
    usable = train[train[target].notna()]
    out: Dict[str, float] = {"__global__": float(usable[target].median())
                             if len(usable) else 0.0}
    for item, group in usable.groupby(item_column):
        out[str(int(item))] = float(group[target].median())
    return out


def _apply_item_median(medians: Dict[str, float], frame: pd.DataFrame,
                       item_column: str = "item_id") -> np.ndarray:
    prior = medians.get("__global__", 0.0)
    return np.array([medians.get(str(int(v)), prior) for v in frame[item_column]],
                    dtype=float)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

@dataclass
class TargetReport:
    status: str = "insufficient_data"
    reason: str = ""
    horizon_h: int = 0
    model_type: str = "none"
    calibrated: str = "identity"
    n_train: int = 0
    n_calibration: int = 0
    n_test: int = 0
    positives_train: int = 0
    positives_test: int = 0
    coverage: Dict[str, object] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    baseline_metrics: Dict[str, float] = field(default_factory=dict)
    candidates: Dict[str, Dict[str, float]] = field(default_factory=dict)
    improvement: bool = False
    price_interval: Optional[Dict[str, object]] = None
    price_interval_conditional: Optional[Dict[str, object]] = None
    residual_sigma: Optional[float] = None
    files: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    #: which block and metric chose the candidate, so the choice can be
    #: audited without re-running the fit
    selection_block: str = ""
    selection_metric: str = ""
    selection_value: Optional[float] = None
    selection_baseline_value: Optional[float] = None
    selection_margin: Optional[float] = None
    #: every candidate's cross-fitted score on the selection block, so the
    #: comparison behind ``selection_*`` is visible rather than implied
    selection_scores: Dict[str, Dict[str, float]] = field(default_factory=dict)
    #: whether the held-out block agrees with the calibration-block choice
    #: on the selection metric; ``None`` when it could not be measured
    test_confirmation: Optional[bool] = None

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def _fit_logistic(train: pd.DataFrame, features: Sequence[str],
                  label: str, seed: int = 7):
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("model", LogisticRegression(C=1.0, max_iter=2000, random_state=seed)),
    ])
    pipeline.fit(align_features(train, features, ()), train[label].astype(int))
    return pipeline


def _fit_ridge(train: pd.DataFrame, features: Sequence[str],
               target: str, seed: int = 7):
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("model", Ridge(alpha=5.0, random_state=seed)),
    ])
    usable = train[train[target].notna()]
    pipeline.fit(align_features(usable, features, ()), usable[target].to_numpy(dtype=float))
    return pipeline


def _fit_catboost_classifier(train: pd.DataFrame, features: Sequence[str],
                             cats: Sequence[str], label: str,
                             iterations: int, seed: int):
    params = dict(
        loss_function="Logloss", iterations=iterations, learning_rate=0.05,
        depth=5, l2_leaf_reg=6.0, random_seed=seed, verbose=False,
        allow_writing_files=False,
    )
    model = CatBoostClassifier(**params)
    model.fit(make_pool(train, list(features), list(cats), label=train[label].astype(int)))
    return model


def _catboost_predict(model, frame: pd.DataFrame, features: Sequence[str],
                      cats: Sequence[str]) -> np.ndarray:
    pool = make_pool(align_features(frame, features, cats), list(features), list(cats))
    return np.asarray(model.predict_proba(pool)[:, 1], dtype=float)


def _sklearn_predict(model, frame: pd.DataFrame, features: Sequence[str]) -> np.ndarray:
    """The model's score for one row: a probability for a classifier.

    ``predict`` on a scikit-learn classifier returns the *class label*, so
    using it as a probability turns every downstream number into a hard
    0/1 decision.  The calibrator then sees only two distinct inputs and
    collapses to two knots, and the exported sale probability can no
    longer tell one item from another.  The estimator decides: a model
    that can produce probabilities is asked for them, a regressor keeps
    its prediction.
    """
    aligned = align_features(frame, features, ())
    if hasattr(model, "predict_proba"):
        proba = np.asarray(model.predict_proba(aligned), dtype=float)
        if proba.ndim == 2 and proba.shape[1] >= 2:
            # classes_ is sorted, so column 1 is P(class == 1).
            return proba[:, 1]
        return proba.ravel()
    return np.asarray(model.predict(aligned), dtype=float)


def _write_joblib(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, path, compress=3)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_predictions(local: Dict[str, object], directory: Path,
                       features: Sequence[str],  # retained for clarity
                       cats: Sequence[str]) -> Dict[str, str]:
    """Persist a fitted candidate and return the files it owns."""
    files: Dict[str, str] = {}
    model_type = str(local["model_type"])
    calibrator: Calibrator = local["calibrator"]  # type: ignore[assignment]
    if model_type == "catboost_price":
        stem = str(local["stem"])
        model = local["model"]
        model_path = directory / f"{stem}.cbm"
        model.save(model_path)  # PriceModel writes its own feature sidecar
        files["model"] = model_path.name
    elif model_type == "catboost_survival":
        stem = str(local["stem"])
        model = local["model"]
        model_path = directory / f"{stem}.cbm"
        model.save(model_path)
        files["model"] = model_path.name
    elif model_type == "catboost":
        # CatBoost's native save_model raised UnicodeDecodeError on this
        # Windows/catboost combination for some risk models.  joblib is the
        # same persistence path used by the sklearn candidates and round-
        # trips the booster reliably; the artifact checksum covers it.
        stem = str(local["stem"])
        model_path = directory / f"{stem}.joblib"
        payload = {k: v for k, v in local.items()
                   if k not in ("calibrator", "stem", "model_type", "features", "cats")}
        payload["model"] = local.get("model")
        payload["features"] = list(features)
        payload["cats"] = list(cats)
        payload["model_type"] = model_type
        _write_joblib(model_path, payload)
        files["model"] = model_path.name
    else:
        stem = str(local["stem"])
        payload_path = directory / f"{stem}.joblib"
        payload = {k: v for k, v in local.items()
                   if k not in ("calibrator", "stem", "model_type", "features", "cats")}
        payload["model"] = local.get("model")
        payload["features"] = list(features)
        payload["cats"] = list(cats)
        payload["model_type"] = model_type
        _write_joblib(payload_path, payload)
        files["model"] = payload_path.name
    cal_path = directory / f"{local['stem']}.calibrator.joblib"
    _write_joblib(cal_path, calibrator)
    files["calibrator"] = cal_path.name
    return files


# ---------------------------------------------------------------------------
# training the sale / price / time targets
# ---------------------------------------------------------------------------

def _prepare_horizon_frame(frame: pd.DataFrame, horizon: int,
                           observation_end: int) -> pd.DataFrame:
    label = f"sold_within_{horizon}h"
    known = f"label_known_{horizon}h"
    if label not in frame.columns or known not in frame.columns:
        return pd.DataFrame()
    usable = frame[(frame[known] == 1) & frame[label].notna()].copy()
    # ``label_known`` is already the censoring rule: a sale or an expiry
    # has a final outcome, so a listing that ended at hour 40 has a known
    # 168-hour label even though that much wall-clock time has not passed.
    # An unobserved/active listing is censored and has label_known = 0, so
    # it is excluded here.  Filtering on ``ts + horizon <= observation_end``
    # as well would throw away valid labels from resolved listings.
    return usable


@dataclass(frozen=True)
class Selection:
    """The outcome of choosing between a baseline and learned candidates."""

    name: str
    metric: str
    value: float
    baseline_value: float
    margin: float
    reason: str

    @property
    def chose_baseline(self) -> bool:
        return self.name == "item_rate_baseline"


#: The name the baseline always has in a selection.
BASELINE_NAME = "item_rate_baseline"

#: Two scores closer than this are the same score.  Float noise must not
#: decide which model ships.
SELECTION_TOLERANCE = 1e-9

#: Target statuses whose probability may be exported as a probability.
#: ``validated_unconfirmed`` is deliberately absent: a model whose
#: calibration-block advantage did not replicate on the held-out block has
#: not demonstrated value, so the caller falls back to the baseline rather
#: than showing the player a number nobody has checked.  ``degenerate`` is
#: absent for the same reason -- two horizons with identical labels are one
#: measurement reported twice.
PROBABILITY_STATUSES = ("validated", "trained_no_measured_gain", "baseline_only")


def select_candidate(baseline_value: float,
                     candidates: Dict[str, Dict[str, float]],
                     *, metric: str = "logloss",
                     tiebreak: str = "brier",
                     min_gain: float = 0.0,
                     tolerance: float = SELECTION_TOLERANCE) -> Selection:
    """Pick one candidate, deterministically, from an **untouched** block.

    Rules, in order:

    1. a candidate whose primary metric is not finite is ineligible;
    2. a candidate must beat the baseline by ``min_gain`` to be eligible
       at all -- a model that is only "less bad" is not an improvement;
    3. among eligible candidates the lowest primary metric wins;
    4. a tie (within ``tolerance``) is broken by the secondary metric,
       then by name in sorted order, so the answer never depends on
       dictionary insertion order or on which thread finished first.

    ``baseline_value`` must come from the same block as the candidates.
    Selecting on the test block and then reporting test metrics on the
    winner is not a measurement; it is a maximum over a sample.
    """
    best_name = BASELINE_NAME
    best_value = float(baseline_value)
    best_tie = float("inf")

    for name in sorted(candidates):
        metrics = candidates[name] or {}
        value = metrics.get(metric)
        if value is None or not math.isfinite(float(value)):
            continue
        value = float(value)
        tie = metrics.get(tiebreak, float("inf"))
        tie = float(tie) if isinstance(tie, (int, float)) and math.isfinite(float(tie)) \
            else float("inf")

        if best_name == BASELINE_NAME:
            if value < best_value - tolerance:
                best_name, best_value, best_tie = name, value, tie
            continue
        if value < best_value - tolerance:
            best_name, best_value, best_tie = name, value, tie
        elif abs(value - best_value) <= tolerance and tie < best_tie - tolerance:
            best_name, best_value, best_tie = name, value, tie

    margin = float(baseline_value) - best_value
    if best_name != BASELINE_NAME and margin < min_gain:
        return Selection(
            name=BASELINE_NAME, metric=metric, value=float(baseline_value),
            baseline_value=float(baseline_value), margin=margin,
            reason=(f"the best candidate beat the baseline by only {margin:.6f} "
                    f"{metric}, below the {min_gain:.6f} required"))
    if best_name == BASELINE_NAME:
        return Selection(
            name=BASELINE_NAME, metric=metric, value=float(baseline_value),
            baseline_value=float(baseline_value), margin=margin,
            reason="no candidate beat the simple baseline on the selection block")
    return Selection(
        name=best_name, metric=metric, value=best_value,
        baseline_value=float(baseline_value), margin=margin,
        reason=f"{best_name} won on {metric} by {margin:.6f}")


def _test_confirms(model_metrics: Dict[str, float],
                   baseline_metrics: Dict[str, float],
                   metric: str) -> Optional[bool]:
    """Does the held-out block agree that the model beats the baseline?

    This is *reporting*, not selection: the winner was already chosen on
    the calibration block.  A model whose advantage does not replicate is
    not promoted, but the test score never picks the winner -- otherwise
    the reported number would be the maximum of a sample.
    """
    if metric not in model_metrics or metric not in baseline_metrics:
        return None
    best = model_metrics.get(metric)
    base = baseline_metrics.get(metric)
    if best is None or base is None:
        return None
    best, base = float(best), float(base)
    if not (math.isfinite(best) and math.isfinite(base)):
        return None
    return best < base - SELECTION_TOLERANCE


def _apply_selection(report: TargetReport, selection: Selection,
                     block: str) -> None:
    report.selection_block = block
    report.selection_metric = selection.metric
    report.selection_value = selection.value
    report.selection_baseline_value = selection.baseline_value
    report.selection_margin = selection.margin


def train_sale_target(frame: pd.DataFrame, horizon: int, observation_end: int,
                      cfg: SentinelConfig, directory: Path) -> Tuple[TargetReport, Optional[Dict[str, object]]]:
    report = TargetReport(horizon_h=horizon)
    usable = _prepare_horizon_frame(frame, horizon, observation_end)
    report.n_train = int(len(usable))
    if usable.empty:
        report.reason = "no confirmed labels for this horizon inside the observation window"
        return report, None
    label = f"sold_within_{horizon}h"
    positives = int(usable[label].sum())
    report.positives_train = positives
    if len(usable) < cfg.min_sale_labels or positives < cfg.min_sale_positives:
        report.reason = (f"only {len(usable)} confirmed labels ({positives} sales); "
                         f"the minimum is {cfg.min_sale_labels} / {cfg.min_sale_positives}")
        return report, None
    if not SKLEARN_AVAILABLE:
        report.reason = "scikit-learn is not installed"
        return report, None

    split = chronological_split(usable, cfg.test_fraction, cfg.calibration_fraction)
    if not split.usable:
        report.reason = split.note or "chronological split unavailable"
        return report, None
    if split.train[label].nunique() < 2 or split.calibration[label].nunique() < 2:
        report.reason = "the training or calibration block has only one class"
        return report, None

    # The exported probability is the chance of selling at a market-
    # reasonable ask, not at whatever ask happens to be in the inference
    # row.  The pricing engine feeds a zero ask when it asks for an
    # estimate, so an ask-trained classifier would be evaluated at an
    # input it never saw.  The strategy layer already owns ask selection.
    feats = [c for c in feature_columns(split.train) if c not in ASK_FEATURES]
    cats = categorical_columns(split.train)
    if not feats:
        report.reason = "no numeric features were available"
        return report, None
    report.n_train = int(len(split.train))
    report.n_calibration = int(len(split.calibration))
    report.n_test = int(len(split.test))
    report.positives_train = int(split.train[label].sum())
    report.positives_test = int(split.test[label].sum())
    report.coverage = {
        "train_ts": list(split.train_span),
        "calibration_ts": list(split.calibration_span),
        "test_ts": list(split.test_span),
        "train_rows": len(split.train),
        "calibration_rows": len(split.calibration),
        "test_rows": len(split.test),
        "features": len(feats),
    }

    # --- baseline: smoothed historical item rate -------------------------
    rates = _baseline_item_rate(split.train, label)
    base_cal = _apply_item_rate(rates, split.calibration)
    base_test = _apply_item_rate(rates, split.test)
    report.baseline_metrics = classification_metrics(
        split.test[label].to_numpy(dtype=float), base_test)
    #: the same baseline on the block the selection is allowed to read.
    #: Without it, "beats the baseline" would be measured on the test set.
    #: It is cross-fitted exactly like the learned candidates so both sides
    #: of the comparison get the same treatment.
    y_cal = split.calibration[label].to_numpy(dtype=float)
    baseline_calibration = classification_metrics(
        y_cal, crossfit_calibrated_probabilities(y_cal, base_cal))
    report.candidates["item_rate_baseline"] = report.baseline_metrics
    report.selection_scores["item_rate_baseline"] = baseline_calibration

    candidates: Dict[str, Dict[str, object]] = {}
    # --- scikit-learn logistic -------------------------------------------
    try:
        logistic = _fit_logistic(split.train, feats, label, cfg.seed)
        p_cal = _sklearn_predict(logistic, split.calibration, feats)
        cal = fit_calibrator(y_cal, p_cal)
        p_test = cal.transform(_sklearn_predict(logistic, split.test, feats))
        metrics = classification_metrics(split.test[label].to_numpy(dtype=float), p_test)
        #: scored out of fold; ``cal`` above saw every calibration row
        calibration_metrics = classification_metrics(
            y_cal, crossfit_calibrated_probabilities(y_cal, p_cal))
        report.candidates["logistic_regression"] = metrics
        report.selection_scores["logistic_regression"] = calibration_metrics
        candidates["logistic_regression"] = {
            "model_type": "logistic", "model": logistic, "calibrator": cal,
            "stem": f"sale_{horizon}h", "metrics": metrics,
            "calibration_metrics": calibration_metrics,
        }
    except Exception as exc:  # pragma: no cover - numerical guard
        report.notes.append(f"logistic regression failed: {exc}")

    # --- CatBoost ---------------------------------------------------------
    if CATBOOST_AVAILABLE:
        try:
            boost = _fit_catboost_classifier(split.train, feats, cats, label,
                                             cfg.iterations, cfg.seed)
            p_cal = _catboost_predict(boost, split.calibration, feats, cats)
            cal = fit_calibrator(y_cal, p_cal)
            p_test = cal.transform(_catboost_predict(boost, split.test, feats, cats))
            metrics = classification_metrics(split.test[label].to_numpy(dtype=float), p_test)
            calibration_metrics = classification_metrics(
                y_cal, crossfit_calibrated_probabilities(y_cal, p_cal))
            report.candidates["catboost"] = metrics
            report.selection_scores["catboost"] = calibration_metrics
            candidates["catboost"] = {
                "model_type": "catboost", "model": boost, "calibrator": cal,
                "stem": f"sale_{horizon}h", "metrics": metrics,
                "calibration_metrics": calibration_metrics,
            }
        except Exception as exc:  # pragma: no cover
            report.notes.append(f"CatBoost classifier failed: {exc}")
    else:
        report.notes.append("CatBoost is not installed; only the scikit-learn candidate was fitted")

    # --- selection: lowest log loss on the calibration block -------------
    # Log loss is the primary selection metric because it is strictly
    # proper and punishes a confident wrong probability, which is exactly
    # the failure Brier can hide.  Brier is the tie-break.
    #
    # The choice is made on the **calibration** block, never on the test
    # block: choosing the winner by its test score and then reporting that
    # same score measures the maximum of a sample, not the model.  The
    # test block stays untouched until the winner has been chosen.
    selection = select_candidate(
        baseline_calibration.get("logloss", float("inf")),
        {name: c["calibration_metrics"] for name, c in candidates.items()},  # type: ignore[misc]
        metric="logloss", tiebreak="brier",
        min_gain=cfg.min_probability_gain)
    best_name = selection.name
    _apply_selection(report, selection, "calibration")
    if best_name != BASELINE_NAME:
        report.notes.append(
            f"selected on calibration ({selection.metric}={selection.value:.6f}); "
            f"the test metrics below are out of sample for the choice")
    report.model_type = best_name
    report.metrics = (report.baseline_metrics if best_name == "item_rate_baseline"
                      else candidates[best_name]["metrics"])  # type: ignore[assignment]
    if best_name == BASELINE_NAME:
        report.improvement = False
        report.reason = selection.reason
        payload = {
            "model_type": "baseline_item_rate", "rates": rates,
            "stem": f"sale_{horizon}h", "calibrator": Calibrator(),
        }
        files = _write_predictions(payload, directory, feats, cats)
        report.files = files
        report.status = "baseline_only"
        report.calibrated = "identity"
        return report, {**payload, "features": feats, "cats": cats, "files": files}

    payload = candidates[best_name]
    #: The decision to ship was made on the calibration block above.  The
    #: test block is asked only whether that advantage replicates; it never
    #: chooses the winner.  A model that wins on calibration but loses on
    #: the held-out block has not demonstrated value, so it is not promoted
    #: as validated -- ``baseline_only`` stays the honest label.
    report.improvement = selection.margin >= cfg.min_probability_gain
    report.test_confirmation = _test_confirms(
        report.metrics, report.baseline_metrics, selection.metric)
    if report.test_confirmation is False:
        report.status = "validated_unconfirmed"
        report.improvement = False
        report.reason = (
            f"{best_name} beat the baseline on the calibration block "
            f"({selection.metric} {selection.value:.4f} vs "
            f"{selection.baseline_value:.4f}) but did not replicate on the "
            f"held-out block ({report.metrics.get(selection.metric):.4f} vs "
            f"{report.baseline_metrics.get(selection.metric):.4f})")
    else:
        report.status = "validated" if report.improvement else "trained_no_measured_gain"
    report.model_type = str(payload.get("model_type") or best_name)
    report.calibrated = payload["calibrator"].method  # type: ignore[union-attr]
    payload["features"] = feats
    payload["cats"] = cats
    payload["files"] = _write_predictions(payload, directory, feats, cats)
    report.files = payload["files"]
    return report, payload


def _price_interval(true_cal, pred_cal, true_test, pred_test,
                    level: float) -> Optional[Dict[str, object]]:
    """Calibrate on one block, measure the coverage on another."""
    interval = calibrate_interval(
        np.asarray(true_cal, dtype=float) - np.asarray(pred_cal, dtype=float),
        np.asarray(true_test, dtype=float) - np.asarray(pred_test, dtype=float),
        level=level)
    return interval.as_dict(prefix="") if isinstance(interval, CalibratedInterval) else None


def _segmented_price_interval(calibration_frame: pd.DataFrame,
                              test_frame: pd.DataFrame,
                              pred_cal: np.ndarray,
                              pred_test: np.ndarray,
                              level: float,
                              min_rows: int) -> Optional[SegmentedInterval]:
    """Conditional interval by ``ref_dispersion`` when the evidence allows."""
    if "ref_dispersion" not in calibration_frame.columns:
        return None
    if "ref_dispersion" not in test_frame.columns:
        return None
    target = "y_net_unit_log"
    if target not in calibration_frame.columns or target not in test_frame.columns:
        return None
    cal_resid = (calibration_frame[target].to_numpy(dtype=float)
                 - np.asarray(pred_cal, dtype=float))
    test_resid = (test_frame[target].to_numpy(dtype=float)
                  - np.asarray(pred_test, dtype=float))
    cal_values = calibration_frame["ref_dispersion"].to_numpy(dtype=float)
    test_values = test_frame["ref_dispersion"].to_numpy(dtype=float)
    return calibrate_segmented_interval(
        cal_resid, cal_values, test_resid, test_values,
        feature="ref_dispersion", level=level, min_rows=min_rows)


def train_price_target(frame: pd.DataFrame, cfg: SentinelConfig,
                       directory: Path) -> Tuple[TargetReport, Optional[Dict[str, object]]]:
    report = TargetReport()
    target = "y_net_unit_log"
    if target not in frame.columns:
        report.reason = "the realized-price target is not in the frame"
        return report, None
    usable = frame[frame[target].notna() & (frame["y_net_unit"] > 0)].copy()
    report.n_train = int(len(usable))
    if len(usable) < cfg.min_price_rows:
        report.reason = (f"only {len(usable)} confirmed sales with a realized price; "
                         f"the minimum is {cfg.min_price_rows}")
        return report, None
    if not SKLEARN_AVAILABLE:
        report.reason = "scikit-learn is not installed"
        return report, None
    split = chronological_split(usable, cfg.test_fraction, cfg.calibration_fraction)
    if not split.usable:
        report.reason = split.note or "chronological split unavailable"
        return report, None
    # A realized-price model must not be told the ask: at inference time
    # the ask is the decision variable, and a model trained on it would
    # simply echo the listing price back.  ASK_FEATURES are excluded for
    # the same reason the original PriceModel excludes them.
    feats = [c for c in feature_columns(split.train) if c not in ASK_FEATURES]
    cats = categorical_columns(split.train)
    report.n_train = int(len(split.train))
    report.n_calibration = int(len(split.calibration))
    report.n_test = int(len(split.test))
    report.coverage = {
        "train_ts": list(split.train_span), "calibration_ts": list(split.calibration_span),
        "test_ts": list(split.test_span), "train_rows": len(split.train),
        "calibration_rows": len(split.calibration), "test_rows": len(split.test),
    }
    y_test = split.test[target].to_numpy(dtype=float)
    net_test = split.test["y_net_unit"].to_numpy(dtype=float)

    # baseline: median realized price per item, global fallback
    medians = _item_median_price(split.train)
    base_cal = _apply_item_median(medians, split.calibration)
    base_test = _apply_item_median(medians, split.test)
    report.baseline_metrics = regression_metrics(y_test, base_test, net_test)
    report.candidates["item_median_baseline"] = report.baseline_metrics
    baseline_interval = _price_interval(split.calibration[target].to_numpy(dtype=float),
                                        base_cal, y_test, base_test,
                                        cfg.price_interval_level)

    candidates: Dict[str, Dict[str, object]] = {}
    # CatBoost (reuse the existing carefully tuned price model)
    if CATBOOST_AVAILABLE:
        try:
            boost = train_price_model(split.train, None, feats, cats,
                                      iterations=cfg.iterations,
                                      seed=cfg.seed, early_stopping=False)
            if boost.trained:
                pred_cal = boost.predict_log(split.calibration)
                pred_test = boost.predict_log(split.test)
                metrics = regression_metrics(y_test, pred_test, net_test)
                report.candidates["catboost"] = metrics
                candidates["catboost"] = {
                    "model_type": "catboost_price", "model": boost, "stem": "price",
                    "metrics": metrics, "calibrator": Calibrator(),
                }
                # The interval is calibrated on the calibration block and
                # its coverage is then measured on the untouched test
                # block.  The old 10th/90th percentile pair was never
                # checked, so it could not honestly be called calibrated.
                true_cal = split.calibration[target].to_numpy(dtype=float)
                interval = _price_interval(true_cal, pred_cal, y_test, pred_test,
                                           cfg.price_interval_level)
                candidates["catboost"]["price_interval"] = interval
                segmented = _segmented_price_interval(
                    split.calibration, split.test, pred_cal, pred_test,
                    cfg.price_interval_level, cfg.min_interval_segment_rows)
                if segmented is not None:
                    candidates["catboost"]["price_interval_conditional"] = \
                        segmented.as_dict()
                candidates["catboost"]["residual_sigma"] = float(np.std(
                    true_cal - pred_cal))
        except Exception as exc:  # pragma: no cover
            report.notes.append(f"CatBoost price model failed: {exc}")
    else:
        report.notes.append("CatBoost is not installed; only Ridge was fitted")

    # Ridge
    try:
        ridge = _fit_ridge(split.train, feats, target, cfg.seed)
        pred_cal = _sklearn_predict(ridge, split.calibration, feats)
        pred_test = _sklearn_predict(ridge, split.test, feats)
        metrics = regression_metrics(y_test, pred_test, net_test)
        report.candidates["ridge"] = metrics
        true_cal = split.calibration[target].to_numpy(dtype=float)
        candidates["ridge"] = {
            "model_type": "ridge", "model": ridge, "stem": "price",
            "metrics": metrics, "calibrator": Calibrator(),
            "price_interval": _price_interval(true_cal, pred_cal, y_test,
                                              pred_test, cfg.price_interval_level),
            "price_interval_conditional": (_segmented_price_interval(
                split.calibration, split.test, pred_cal, pred_test,
                cfg.price_interval_level,
                cfg.min_interval_segment_rows).as_dict()
                if _segmented_price_interval(split.calibration, split.test, pred_cal,
                                             pred_test, cfg.price_interval_level,
                                             cfg.min_interval_segment_rows)
                is not None else None),
            "residual_sigma": float(np.std(true_cal - pred_cal)),
        }
    except Exception as exc:  # pragma: no cover
        report.notes.append(f"Ridge regression failed: {exc}")

    best_name = "item_median_baseline"
    best_mae = report.baseline_metrics.get("mae_log", float("inf"))
    for name, candidate in candidates.items():
        value = candidate["metrics"].get("mae_log", float("inf"))  # type: ignore[union-attr]
        if value < best_mae:
            best_name, best_mae = name, value
    report.model_type = best_name
    report.metrics = (report.baseline_metrics if best_name == "item_median_baseline"
                      else candidates[best_name]["metrics"])  # type: ignore[assignment]
    if best_name == "item_median_baseline":
        report.status = "baseline_only"
        report.reason = "the item-median baseline matched or beat the learned price models"
        payload = {
            "model_type": "baseline_item_median", "medians": medians,
            "stem": "price", "calibrator": Calibrator(),
            "price_interval": baseline_interval,
        }
        files = _write_predictions(payload, directory, feats, cats)
        payload["features"], payload["cats"], payload["files"] = feats, cats, files
        report.files = files
        report.price_interval = baseline_interval
        return report, payload

    payload = candidates[best_name]
    baseline_mae = report.baseline_metrics.get("mae_log", float("inf"))
    report.improvement = bool(baseline_mae - best_mae >= cfg.min_price_gain)
    report.status = "validated" if report.improvement else "trained_no_measured_gain"
    report.model_type = str(payload.get("model_type") or best_name)
    report.price_interval = payload.get("price_interval")  # type: ignore[assignment]
    report.price_interval_conditional = payload.get("price_interval_conditional")  # type: ignore[assignment]
    report.residual_sigma = payload.get("residual_sigma")  # type: ignore[assignment]
    payload["features"] = feats
    payload["cats"] = cats
    payload["files"] = _write_predictions(payload, directory, feats, cats)
    report.files = payload["files"]
    return report, payload


def train_time_to_sale(frame: pd.DataFrame, cfg: SentinelConfig,
                       directory: Path) -> Tuple[TargetReport, Optional[Dict[str, object]]]:
    report = TargetReport()
    needed = {"tts_hours", "tts_censored"}
    if not needed.issubset(frame.columns):
        report.reason = "the time-to-sale columns are not in the frame"
        return report, None
    usable = frame[frame["tts_hours"].notna() & (frame["tts_hours"] > 0)].copy()
    events = int((usable["tts_censored"] == 0).sum()) if len(usable) else 0
    if len(usable) < cfg.min_tts_rows or events < cfg.min_tts_events:
        report.reason = (f"only {len(usable)} durations ({events} observed sales); "
                         f"the minimum is {cfg.min_tts_rows} / {cfg.min_tts_events}")
        report.n_train = int(len(usable))
        return report, None
    split = chronological_split(usable, cfg.test_fraction, cfg.calibration_fraction)
    if not split.usable:
        report.reason = split.note or "chronological split unavailable"
        return report, None
    feats = feature_columns(split.train)
    cats = categorical_columns(split.train)
    report.n_train = int(len(split.train))
    report.n_calibration = int(len(split.calibration))
    report.n_test = int(len(split.test))
    report.coverage = {"train_ts": list(split.train_span),
                       "calibration_ts": list(split.calibration_span),
                       "test_ts": list(split.test_span),
                       "train_rows": len(split.train),
                       "calibration_rows": len(split.calibration),
                       "test_rows": len(split.test)}
    observed_test = split.test["tts_censored"].to_numpy() == 0
    y_test = split.test["tts_hours"].to_numpy(dtype=float)
    # baseline: median observed time to sale in training.  A constant
    # prediction cannot rank two rows, so its C-index is 0.5 by definition
    # (the function gives a tied prediction half credit on every pair).
    baseline = float(split.train.loc[split.train["tts_censored"] == 0, "tts_hours"].median())
    base_pred = np.full(len(split.test), baseline)
    base_abs_log = np.abs(np.log(base_pred[observed_test])
                          - np.log(y_test[observed_test])) if observed_test.any() else np.array([])
    report.baseline_metrics = {
        "c_index": concordance_index(y_test, base_pred, event=observed_test),
        "n": int(len(split.test)),
        "n_events": int(observed_test.sum()),
        "n_censored": int((~observed_test).sum()),
        "censoring_rate": float((~observed_test).mean()),
        "median_observed_hours": float(split.test["tts_hours"].median()),
        "within_2x_events": (float(np.mean(base_abs_log <= np.log(2.0)))
                             if len(base_abs_log) else None),
        "median_absolute_log_error_events": (float(np.median(base_abs_log))
                                             if len(base_abs_log) else None),
    }
    if not CATBOOST_AVAILABLE:
        report.reason = "CatBoost is required for the censored survival model"
        return report, None
    try:
        model = train_time_to_sale_model(split.train, None, feats, cats,
                                         iterations=cfg.iterations, seed=cfg.seed,
                                         early_stopping=False)
    except Exception as exc:  # pragma: no cover
        report.reason = f"the survival model failed: {exc}"
        return report, None
    if not model.trained:
        report.reason = model.note or "the survival model did not fit"
        return report, None
    pred = model.predict_median_hours(split.test)
    raw = model._raw(split.test)
    abs_log = np.abs(np.log(pred[observed_test]) - np.log(y_test[observed_test])) \
        if observed_test.any() else np.array([])
    within_2x = (float(np.mean(abs_log <= np.log(2.0)))
                 if len(abs_log) else None)
    metrics = {
        "c_index": concordance_index(y_test, raw, event=observed_test),
        "n": int(len(split.test)),
        "n_events": int(observed_test.sum()),
        "n_censored": int((~observed_test).sum()),
        "censoring_rate": float((~observed_test).mean()),
        "median_predicted_hours": float(np.median(pred)),
        "median_observed_hours": float(split.test["tts_hours"].median()),
        "within_2x_events": within_2x,
        "median_absolute_log_error_events": (float(np.median(abs_log))
                                             if len(abs_log) else None),
        "baseline_c_index": report.baseline_metrics.get("c_index"),
        "baseline_within_2x_events": report.baseline_metrics.get("within_2x_events"),
    }
    report.metrics = metrics
    report.candidates["catboost_survival"] = metrics
    report.model_type = "catboost_survival"
    ranking_supported = bool(
        math.isfinite(float(metrics["c_index"]))
        and float(metrics["c_index"]) >= cfg.min_tts_c_index)
    point_supported = bool(
        ranking_supported and within_2x is not None
        and within_2x >= cfg.min_tts_within_2x)
    if point_supported:
        report.status = "validated"
    elif ranking_supported:
        report.status = "validated_ranking_only"
        report.reason = (
            f"the censoring-aware C-index is {metrics['c_index']:.3f}, but only "
            f"{within_2x:.0%} of observed sales fall within a factor of two of "
            f"the predicted duration (the minimum is {cfg.min_tts_within_2x:.0%}); "
            "the model ranks durations but is not quoted as an exact time")
    else:
        report.status = "unsupported_signal"
        report.reason = (f"the censoring-aware C-index is {metrics['c_index']:.3f}; "
                         f"below the {cfg.min_tts_c_index:.2f} needed to rank durations")
    model_path = directory / "tts.cbm"
    model.save(model_path)
    cal = Calibrator()  # the survival model carries its own log-time calibration
    _write_joblib(directory / "tts.calibrator.joblib", cal)
    files = {"model": model_path.name, "calibrator": "tts.calibrator.joblib"}
    manifest = {
        "features": feats, "cats": cats, "files": files,
        "calibration": list(model.calibration),
        "label_semantics": "SurvivalAft interval: [duration, duration] for an observed sale, [duration, -1] for right censoring",
        "ranking_supported": ranking_supported,
        "point_supported": point_supported,
    }
    _atomic_write_text(directory / "tts.meta.json",
                       json.dumps(manifest, indent=2))
    files["meta"] = "tts.meta.json"
    report.files = files
    payload = {"model_type": "catboost_survival", "model": model, "calibrator": cal,
               "stem": "tts", "features": feats, "cats": cats, "files": files,
               "ranking_supported": ranking_supported,
               "point_supported": point_supported}
    return report, payload


# ---------------------------------------------------------------------------
# snapshot risk targets
# ---------------------------------------------------------------------------

def load_snapshot_panel(tsm_db: Path,
                        scopes: Optional[Sequence[str]] = None,
                        item_ids: Optional[Sequence[int]] = None,
                        days: int = 30,
                        max_rows: int = 400_000) -> pd.DataFrame:
    """Read timestamped realm snapshots, not the undated summary table.

    ``market_snapshots`` has no time and no realm, so it cannot support a
    temporal target.  ``tsm_snapshot`` plus ``tsm_price_change`` does: each
    row is a complete per-item state at a known ``updated_at``.

    The change table is large (millions of rows on this installation), so
    the panel is restricted to the traded auction houses and items.  This
    is not a modelling shortcut: an item the account never traded has no
    listing outcome to learn from, and the risk model is only claimed for
    the items in the exported records.
    """
    if not Path(tsm_db).exists():
        return pd.DataFrame()
    con = sqlite3.connect(tsm_db)
    try:
        if scopes is None:
            traded = [str(r[0]) for r in con.execute(
                "SELECT DISTINCT realm FROM auction_events WHERE realm <> ''")]
            canonical = {str(name): str(slug) for name, slug in con.execute(
                "SELECT realm_name, canonical_slug FROM tsm_realm_group")}
            scopes = sorted({canonical[realm] for realm in traded
                             if canonical.get(realm)}) or None
        if item_ids is None:
            item_ids = [int(r[0]) for r in con.execute(
                "SELECT DISTINCT item_id FROM auction_events "
                "WHERE item_id IS NOT NULL")]
        where = ["s.kind = 'realm_items'", "c.kind = 'realm_items'"]
        args: List[object] = []
        if scopes:
            where.append("s.scope IN (%s)" % ",".join("?" * len(scopes)))
            args.extend(list(scopes))
        if item_ids:
            where.append("c.key_id IN (%s)" % ",".join("?" * len(item_ids)))
            args.extend([int(v) for v in item_ids])
        if days and days > 0:
            where.append("s.updated_at >= datetime('now', ?)")
            args.append(f"-{int(days)} days")
        query = (
            "SELECT s.updated_at, s.scope, c.key_id, c.market_value, c.min_buyout, "
            "       c.recent, c.historical, c.avg_sale_price, c.sale_rate, "
            "       c.sold_per_day "
            "  FROM tsm_snapshot s "
            "  JOIN tsm_price_change c "
            "    ON c.snapshot_id = s.snapshot_id AND c.scope = s.scope "
            " WHERE " + " AND ".join(where) +
            " ORDER BY s.scope, c.key_id, s.updated_at LIMIT ?")
        args.append(int(max_rows))
        rows = con.execute(query, args).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("snapshot panel unavailable: %s", exc)
        return pd.DataFrame()
    finally:
        con.close()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=[
        "updated_at", "scope", "item_id", "market", "min_buyout", "recent",
        "historical", "avg_sale_price", "sale_rate", "sold_per_day"])
    frame["ts"] = frame["updated_at"].map(_parse_ts)
    frame = frame[frame["ts"].notna()].copy()
    frame["ts"] = frame["ts"].astype("int64")
    for col in ("market", "min_buyout", "recent", "historical",
                "avg_sale_price", "sale_rate", "sold_per_day"):
        frame[col] = frame[col].map(_clean)
    return frame.sort_values(["scope", "item_id", "ts"], kind="mergesort")


def load_panel(cfg: "SentinelConfig",
               item_ids: Optional[Sequence[int]] = None
               ) -> Tuple[pd.DataFrame, str]:
    """The market panel behind the risk targets, and a note about where it came from.

    Two sources exist and they are not equivalent:

    * ``tsm`` -- TSM's own snapshots, restricted to the realms and items the
      account has traded.  This is what the shipped models were trained on.
    * ``blizzard`` -- our own `ah_snapshot`/`ah_auction`, which covers every
      connected realm the fetcher has swept and keeps variants apart.

    ``auto`` prefers Blizzard once it has enough rows, and says so in the
    returned note either way.  Which panel a model was fitted on is part of
    the model, so it is recorded rather than left to be inferred from a
    filename.
    """
    source = str(getattr(cfg, "panel_source", "tsm") or "tsm").lower()
    if source not in ("tsm", "blizzard", "auto"):
        raise ValueError(f"unknown panel_source: {cfg.panel_source!r}")

    blizzard_note = ""
    if source in ("blizzard", "auto"):
        panel, report = snapshot_panel.load_market_panel(
            cfg.intel_db, item_ids=item_ids)
        blizzard_note = report.summary()
        if report.notes:
            blizzard_note += "; " + "; ".join(report.notes)
        if source == "blizzard":
            return panel, "panel=blizzard: " + blizzard_note
        enough_rows = len(panel) >= int(getattr(cfg, "min_blizzard_panel_rows", 1))
        enough_span = report.span_hours >= float(
            getattr(cfg, "min_blizzard_span_h", 72.0))
        if enough_rows and enough_span:
            return panel, "panel=blizzard: " + blizzard_note
        # Not enough of our own observations yet.  Falling back is a
        # decision, so it is written down: the alternative is a risk model
        # that silently trains on a panel too short to close its horizon
        # and then reports "no signal" as if the market had none.
        why = ("span" if not enough_span else "rows")
        note = (f"panel=tsm (blizzard panel too short: {why}; "
                + blizzard_note
                + f"; need {cfg.min_blizzard_panel_rows} rows and"
                  f" {cfg.min_blizzard_span_h:g} h)")
    else:
        note = "panel=tsm"

    return load_snapshot_panel(cfg.tsm_db, item_ids=item_ids), note


def _parse_ts(value: object) -> float:
    if value is None:
        return float("nan")
    text = str(value).strip()
    if not text:
        return float("nan")
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return float("nan")


def _snapshot_features(row: pd.Series, previous: Optional[pd.Series],
                       gap_h: float) -> Dict[str, float]:
    """Market state at a snapshot, using only information from that moment."""
    market = row.get("market")
    minimum = row.get("min_buyout")
    recent = row.get("recent")
    historical = row.get("historical")
    sale = row.get("avg_sale_price")
    return {
        "risk_market_log": _safe_log(market),
        "risk_min_log": _safe_log(minimum),
        "risk_recent_log": _safe_log(recent),
        "risk_historical_log": _safe_log(historical),
        "risk_sale_avg_log": _safe_log(sale),
        "risk_spread_min_market": ((minimum / market) if _valid(market) and _valid(minimum) else np.nan),
        "risk_recent_over_market": ((recent / market) if _valid(market) and _valid(recent) else np.nan),
        "risk_sale_over_market": ((sale / market) if _valid(market) and _valid(sale) else np.nan),
        "risk_sale_rate": row.get("sale_rate", np.nan),
        "risk_sold_per_day": row.get("sold_per_day", np.nan),
        "risk_gap_h": gap_h,
        "risk_market_change": (
            _safe_log(market) - _safe_log(previous.get("market"))
            if previous is not None and _valid(market) and _valid(previous.get("market"))
            else np.nan),
        "risk_scope_code": _scope_code(row.get("scope")),
    }


def _scope_code(scope: object) -> float:
    """A stable code for a realm, so the same data trains the same model.

    This used to be ``abs(hash(str(scope))) % 997``.  Python salts ``hash``
    for strings per process, so the code for ``dragonmaw`` was 210 in one
    run and 493 in the next: the feature matrix changed on every training
    run while the data did not, which makes two runs incomparable and makes
    "the model got worse" indistinguishable from "the realm got a new
    number".  ``crc32`` is deterministic and needs no dependency.
    """
    import zlib
    if scope is None:
        return np.nan
    text = str(scope)
    if not text:
        return np.nan
    return float(zlib.crc32(text.encode("utf-8")) % 997)


def _valid(value: object) -> bool:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def build_risk_examples(panel: pd.DataFrame,
                        horizons_h: Sequence[int] = RISK_HORIZONS_H) -> pd.DataFrame:
    """One row per (snapshot, horizon), labelled only when the horizon closed.

    ``undercut_pressure`` is 1 when the cheapest listing observed at or
    after ``t + horizon`` is below the cheapest listing at ``t``.
    ``price_decline`` is 1 when the market value at that later snapshot is
    at least 5% lower.  Both are *market* measurements; neither claims
    that one of our auctions was the one undercut.
    """
    if panel.empty:
        return pd.DataFrame()
    records: List[Dict[str, object]] = []
    # Group by variant when the panel carries one.  Two listings of item
    # 82800 are a caged pet and a different caged pet; a "cheapest listing
    # for this item" built across both describes neither, and the target is
    # about whether *this* good got undercut.  Panels without a variant
    # column (the TSM one) keep the item-only grouping.
    keys = ["scope", "item_id"]
    if "variant_key" in panel.columns:
        keys = ["scope", "item_id", "variant_key"]
    # Sorted keys, so the row order does not depend on the order the panel
    # happened to arrive in: a chronological split taken over an unordered
    # frame is a different split on every run.
    for key, group in panel.groupby(keys, sort=True):
        scope, item_id = key[0], key[1]
        group = group.sort_values("ts").reset_index(drop=True)
        times = group["ts"].to_numpy(dtype=np.int64)
        for index in range(len(group) - 1):
            current = group.iloc[index]
            current_ts = int(current["ts"])
            previous = group.iloc[index - 1] if index else None
            gap_h = (current_ts - int(previous["ts"])) / 3600.0 if previous is not None else np.nan
            base_features = _snapshot_features(current, previous, gap_h)
            for horizon in horizons_h:
                target_time = current_ts + horizon * 3600
                future_index = int(np.searchsorted(times, target_time, side="left"))
                if future_index >= len(group):
                    continue  # right-censored: the horizon never closed
                future = group.iloc[future_index]
                # A snapshot months later is not evidence about this horizon.
                if int(future["ts"]) - current_ts > horizon * 3600 * 1.5:
                    continue
                if not (_valid(current["min_buyout"]) and _valid(future["min_buyout"])
                        and _valid(current["market"]) and _valid(future["market"])):
                    continue
                record = dict(base_features)
                record.update({
                    "ts": current_ts,
                    "scope": str(scope),
                    "item_id": int(item_id),
                    "horizon_h": int(horizon),
                    "undercut_pressure": int(future["min_buyout"] < current["min_buyout"] * 0.99),
                    "price_decline": int(future["market"] < current["market"] * 0.95),
                    "future_ts": int(future["ts"]),
                })
                if len(key) > 2:
                    record["variant_key"] = str(key[2])
                records.append(record)
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame.from_records(records)
    return frame.sort_values(["ts", "scope", "item_id"], kind="mergesort"
                             ).reset_index(drop=True)


RISK_FEATURES: Tuple[str, ...] = (
    "risk_market_log", "risk_min_log", "risk_recent_log",
    "risk_historical_log", "risk_sale_avg_log", "risk_spread_min_market",
    "risk_recent_over_market", "risk_sale_over_market", "risk_sale_rate",
    "risk_sold_per_day", "risk_gap_h", "risk_market_change",
)


def risk_features_from_rows(local: Optional[Sequence], region: Optional[Sequence],
                           snapshot_age_h: float = 0.0) -> Dict[str, float]:
    """Build the risk model's inputs from the live TSM rows.

    The tuple layouts are the ones ``FeatureBuilder`` already uses:
    local = (market, min_buyout, recent, historical, ...) and
    region = (market, ?, ?, historical, avg_sale, sale_rate, sold_per_day).
    A missing row stays NaN; the imputer decides, the caller does not.
    """
    def at(row: Optional[Sequence], index: int) -> float:
        if row is None or len(row) <= index:
            return float("nan")
        return _clean(row[index])

    market = at(local, 0)
    minimum = at(local, 1)
    recent = at(local, 2)
    historical = at(local, 3)
    region_market = at(region, 0)
    region_sale = at(region, 4)
    values = {
        "risk_market_log": _safe_log(market),
        "risk_min_log": _safe_log(minimum),
        "risk_recent_log": _safe_log(recent),
        "risk_historical_log": _safe_log(historical),
        "risk_sale_avg_log": _safe_log(region_sale),
        "risk_spread_min_market": (minimum / market
                                   if _valid(market) and _valid(minimum) else np.nan),
        "risk_recent_over_market": (recent / market
                                    if _valid(market) and _valid(recent) else np.nan),
        "risk_sale_over_market": (region_sale / region_market
                                  if _valid(region_market) and _valid(region_sale) else np.nan),
        "risk_sale_rate": _clean(region[5]) if region is not None and len(region) > 5 else np.nan,
        "risk_sold_per_day": _clean(region[6]) if region is not None and len(region) > 6 else np.nan,
        "risk_gap_h": float(snapshot_age_h),
        "risk_market_change": np.nan,
    }
    if _valid(market) and _valid(region_market):
        values["risk_spread_min_market"] = minimum / market
    return values


def train_risk_target(examples: pd.DataFrame, target: str, horizon: int,
                      cfg: SentinelConfig,
                      directory: Path) -> Tuple[TargetReport, Optional[Dict[str, object]]]:
    report = TargetReport(horizon_h=horizon)
    if examples.empty or target not in examples.columns:
        report.reason = "no snapshot examples with a closed horizon"
        return report, None
    usable = examples[examples["horizon_h"] == horizon].copy()
    positives = int(usable[target].sum()) if len(usable) else 0
    report.n_train = int(len(usable))
    report.positives_train = positives
    if len(usable) < cfg.min_risk_rows or positives < cfg.min_risk_positives:
        report.reason = (f"only {len(usable)} closed-horizon snapshots ({positives} positive); "
                         f"the minimum is {cfg.min_risk_rows} / {cfg.min_risk_positives}")
        return report, None
    if not SKLEARN_AVAILABLE:
        report.reason = "scikit-learn is not installed"
        return report, None
    split = chronological_split(usable, cfg.test_fraction, cfg.calibration_fraction)
    if not split.usable:
        report.reason = split.note or "chronological split unavailable"
        return report, None
    if split.train[target].nunique() < 2:
        report.reason = "the training block has only one outcome class"
        return report, None
    feats = [c for c in RISK_FEATURES if c in split.train.columns]
    report.n_train = int(len(split.train))
    report.n_calibration = int(len(split.calibration))
    report.n_test = int(len(split.test))
    report.positives_train = int(split.train[target].sum())
    report.positives_test = int(split.test[target].sum())
    report.coverage = {"train_ts": list(split.train_span),
                       "calibration_ts": list(split.calibration_span),
                       "test_ts": list(split.test_span),
                       "scopes": int(split.train["scope"].nunique())}

    base_rate = float(split.train[target].mean())
    baseline_test = np.full(len(split.test), base_rate)
    report.baseline_metrics = classification_metrics(
        split.test[target].to_numpy(dtype=float), baseline_test)
    y_cal = split.calibration[target].to_numpy(dtype=float)
    baseline_calibration = classification_metrics(
        y_cal, crossfit_calibrated_probabilities(
            y_cal, np.full(len(split.calibration), base_rate)))
    report.candidates["base_rate_baseline"] = report.baseline_metrics
    report.selection_scores["base_rate_baseline"] = baseline_calibration

    candidates: Dict[str, Dict[str, object]] = {}
    try:
        logistic = _fit_logistic(split.train, feats, target, cfg.seed)
        p_cal = _sklearn_predict(logistic, split.calibration, feats)
        cal = fit_calibrator(y_cal, p_cal)
        p_test = cal.transform(_sklearn_predict(logistic, split.test, feats))
        metrics = classification_metrics(split.test[target].to_numpy(dtype=float), p_test)
        calibration_metrics = classification_metrics(
            y_cal, crossfit_calibrated_probabilities(y_cal, p_cal))
        report.candidates["logistic_regression"] = metrics
        report.selection_scores["logistic_regression"] = calibration_metrics
        candidates["logistic_regression"] = {
            "model_type": "logistic", "model": logistic, "calibrator": cal,
            "stem": f"risk_{target}_{horizon}h", "metrics": metrics,
            "calibration_metrics": calibration_metrics,
        }
    except Exception as exc:  # pragma: no cover
        report.notes.append(f"logistic risk model failed: {exc}")

    if CATBOOST_AVAILABLE:
        try:
            boost = _fit_catboost_classifier(split.train, feats, [], target,
                                             cfg.iterations, cfg.seed)
            p_cal = _catboost_predict(boost, split.calibration, feats, [])
            cal = fit_calibrator(y_cal, p_cal)
            p_test = cal.transform(_catboost_predict(boost, split.test, feats, []))
            metrics = classification_metrics(split.test[target].to_numpy(dtype=float), p_test)
            calibration_metrics = classification_metrics(
                y_cal, crossfit_calibrated_probabilities(y_cal, p_cal))
            report.candidates["catboost"] = metrics
            report.selection_scores["catboost"] = calibration_metrics
            candidates["catboost"] = {
                "model_type": "catboost", "model": boost, "calibrator": cal,
                "stem": f"risk_{target}_{horizon}h", "metrics": metrics,
                "calibration_metrics": calibration_metrics,
            }
        except Exception as exc:  # pragma: no cover
            report.notes.append(f"CatBoost risk model failed: {exc}")

    selection = select_candidate(
        baseline_calibration.get("logloss", float("inf")),
        {name: c["calibration_metrics"] for name, c in candidates.items()},  # type: ignore[misc]
        metric="logloss", tiebreak="brier", min_gain=cfg.min_risk_gain)
    best_name = selection.name
    _apply_selection(report, selection, "calibration")
    report.model_type = best_name
    report.metrics = (report.baseline_metrics if best_name == "base_rate_baseline"
                      else candidates[best_name]["metrics"])  # type: ignore[assignment]
    if best_name == "base_rate_baseline":
        report.status = "baseline_only"
        report.reason = selection.reason
        payload = {"model_type": "baseline_rate", "rate": base_rate,
                   "stem": f"risk_{target}_{horizon}h", "calibrator": Calibrator()}
        files = _write_predictions(payload, directory, feats, [])
        payload.update({"features": feats, "cats": [], "files": files})
        report.files = files
        return report, payload
    payload = candidates[best_name]
    report.improvement = selection.margin >= cfg.min_risk_gain
    report.test_confirmation = _test_confirms(
        report.metrics, report.baseline_metrics, selection.metric)
    if report.test_confirmation is False:
        report.status = "validated_unconfirmed"
        report.improvement = False
        report.reason = (
            f"{best_name} beat the baseline on the calibration block "
            f"({selection.metric} {selection.value:.4f} vs "
            f"{selection.baseline_value:.4f}) but did not replicate on the "
            f"held-out block ({report.metrics.get(selection.metric):.4f} vs "
            f"{report.baseline_metrics.get(selection.metric):.4f})")
    else:
        report.status = "validated" if report.improvement else "trained_no_measured_gain"
    report.model_type = str(payload.get("model_type") or best_name)
    report.calibrated = payload["calibrator"].method  # type: ignore[union-attr]
    payload["features"] = feats
    payload["cats"] = []
    payload["files"] = _write_predictions(payload, directory, feats, [])
    report.files = payload["files"]
    return report, payload


# ---------------------------------------------------------------------------
# artifact lifecycle
# ---------------------------------------------------------------------------

@dataclass
class SentinelArtifact:
    version: str
    path: Path
    manifest: Dict[str, object]
    models: Dict[str, Dict[str, object]] = field(default_factory=dict)

    def target(self, key: str) -> Optional[Dict[str, object]]:
        return self.models.get(key)


def _manifest_checksums(directory: Path) -> Dict[str, str]:
    return {path.name: _sha256(path) for path in sorted(directory.iterdir())
            if path.is_file() and not path.name.startswith(".")}


def promote_artifact(staging: Path, root: Path, version: str,
                     manifest: Dict[str, object], cfg: SentinelConfig) -> Path:
    """Move a validated staging directory into place, then publish CURRENT.

    The active model is never edited in place: a failed training run leaves
    a ``.staging-*`` directory that the loader ignores, and ``CURRENT`` is
    replaced only after every artifact has been written and checksummed.
    """
    root.mkdir(parents=True, exist_ok=True)
    final = root / version
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)
    _atomic_write_text(root / "CURRENT", version + "\n")
    _atomic_write_text(final / "manifest.json", json.dumps(manifest, indent=2))
    _prune_versions(root, cfg.keep_versions, keep_version=version)
    return final


def _prune_versions(root: Path, keep_count: int, keep_version: str = "") -> None:
    versions = [p for p in root.iterdir()
                if p.is_dir() and not p.name.startswith(".") and p.name != keep_version]
    versions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in versions[max(0, keep_count - 1):]:
        try:
            shutil.rmtree(old)
        except OSError:  # pragma: no cover
            pass


def load_artifact(root: Path,
                  expected_schema: str = SENTINEL_SCHEMA) -> Tuple[Optional[SentinelArtifact], str]:
    """Load CURRENT, verify schema and checksums, and fail closed."""
    root = Path(root)
    current = root / "CURRENT"
    if not current.exists():
        return None, "no trained Sentinel artifact (CURRENT is missing)"
    version = current.read_text(encoding="utf-8").strip()
    if not version:
        return None, "CURRENT is empty"
    directory = root / version
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return None, f"artifact {version} has no manifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, f"artifact {version} has an invalid manifest: {exc}"
    if manifest.get("schema") != expected_schema:
        return None, (f"artifact schema {manifest.get('schema')} is not "
                      f"{expected_schema}")
    if manifest.get("feature_schema") != FEATURE_SCHEMA:
        return None, (f"artifact feature schema {manifest.get('feature_schema')} is not "
                      f"{FEATURE_SCHEMA}")
    for name, expected in (manifest.get("checksums") or {}).items():
        path = directory / name
        if not path.exists() or _sha256(path) != expected:
            return None, f"artifact {version} is corrupt: {name} failed its checksum"
    artifact = SentinelArtifact(version=version, path=directory, manifest=manifest)
    for key, entry in (manifest.get("targets") or {}).items():
        files = (entry or {}).get("files") or {}
        model_type = (entry or {}).get("model_type")
        if not files or model_type in (None, "none"):
            continue
        try:
            loaded = _load_target(directory, entry)
        except Exception as exc:  # pragma: no cover - exercised by corrupt tests
            logger.warning("target %s could not be loaded: %s", key, exc)
            continue
        if loaded is not None:
            artifact.models[key] = loaded
    return artifact, ""


def _load_target(directory: Path, entry: Dict[str, object]) -> Optional[Dict[str, object]]:
    model_type = str(entry.get("model_type"))
    files = entry.get("files") or {}
    model_file = directory / str(files.get("model", ""))
    cal_file = directory / str(files.get("calibrator", ""))
    calibrator = Calibrator()
    if cal_file.exists():
        try:
            calibrator = joblib.load(cal_file)
        except Exception:
            calibrator = Calibrator(note="the calibrator could not be loaded")
    features = list(entry.get("features") or [])
    cats = list(entry.get("cats") or [])
    if model_type == "catboost":
        if model_file.suffix == ".joblib":
            payload = joblib.load(model_file)
            payload["calibrator"] = calibrator
            return payload
        model = CatBoostClassifier()
        model.load_model(str(model_file))
        return {"model_type": model_type, "model": model, "calibrator": calibrator,
                "features": features, "cats": cats}
    if model_type == "catboost_price":
        model = PriceModel.load(model_file)
        return {"model_type": model_type, "model": model, "calibrator": calibrator,
                "features": features, "cats": cats,
                "price_interval": entry.get("price_interval"),
                "price_interval_conditional": entry.get("price_interval_conditional"),
                "residual_interval": entry.get("residual_interval"),
                "residual_sigma": entry.get("residual_sigma")}
    if model_type == "catboost_survival":
        meta_path = directory / "tts.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        model = load_time_to_sale_model(directory / "tts.cbm") if (
            directory / "tts.cbm.features.json").exists() else None
        if model is None:
            model = TimeToSaleModel(
                features=list(meta.get("features") or features),
                categoricals=list(meta.get("cats") or cats),
                calibration=tuple(meta.get("calibration") or (0.0, 1.0)),
                trained=True)
            model.model = CatBoostRegressor()
            model.model.load_model(str(model_file))
        return {"model_type": model_type, "model": model, "calibrator": calibrator,
                "features": features, "cats": cats}
    payload = joblib.load(model_file)
    payload["calibrator"] = calibrator
    return payload


def check_freshness(artifact: SentinelArtifact, max_age_h: float) -> Tuple[bool, float]:
    try:
        created = datetime.strptime(str(artifact.manifest.get("created_at")),
                                    "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False, float("inf")
    age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
    return age_h <= max_age_h, age_h


# ---------------------------------------------------------------------------
# the trainer
# ---------------------------------------------------------------------------

@dataclass
class TrainingResult:
    version: str = ""
    path: str = ""
    promoted: bool = False
    reason: str = ""
    report: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


class SentinelTrainer:
    """Train every target, write a staging artifact and promote it."""

    def __init__(self, cfg: Optional[SentinelConfig] = None) -> None:
        self.cfg = cfg or SentinelConfig()

    def train(self, frame: pd.DataFrame,
              observation_end: int = 0,
              panel: Optional[pd.DataFrame] = None,
              risk_examples: Optional[pd.DataFrame] = None,
              dry_run: bool = False) -> TrainingResult:
        cfg = self.cfg
        cfg.ensure_dirs()
        if frame is None or frame.empty:
            return TrainingResult(reason="the feature frame is empty")
        if observation_end <= 0 and "ts" in frame.columns:
            observation_end = int(frame["ts"].max())
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        staging = cfg.models_root / f".staging-{version}"
        staging.mkdir(parents=True, exist_ok=False)
        reports: Dict[str, TargetReport] = {}
        panel_notes: List[str] = []
        payloads: Dict[str, Dict[str, object]] = {}
        try:
            # Measure the horizons before training them.  An auction lasts
            # at most 48 hours, so a 168h label is the 48h label: training
            # both and reporting two "validated" models manufactures a
            # distinction the market does not have.
            equivalence = label_audit.label_agreement(frame, cfg.horizons_h)
            degenerate = label_audit.degenerate_horizons(frame, cfg.horizons_h)
            closure = label_audit.horizon_closure(frame, observation_end)
            for horizon in cfg.horizons_h:
                report, payload = train_sale_target(frame, horizon, observation_end,
                                                    cfg, staging)
                if horizon in degenerate:
                    report.status = "degenerate"
                    report.improvement = False
                    report.reason = (
                        f"the labels are identical to the {degenerate[horizon]}h "
                        f"target (agreement >= {label_audit.DEFAULT_AGREEMENT}); "
                        "this is the same question, not an independent model")
                    report.notes.append(
                        "trained for artifact compatibility, but not eligible "
                        "for promotion as a separate model")
                reports[f"sale_{horizon}h"] = report
                if payload is not None:
                    payloads[f"sale_{horizon}h"] = payload
            report, payload = train_price_target(frame, cfg, staging)
            reports["price"] = report
            if payload is not None:
                payloads["price"] = payload
            report, payload = train_time_to_sale(frame, cfg, staging)
            reports["time_to_sale"] = report
            if payload is not None:
                payloads["time_to_sale"] = payload

            if risk_examples is None:
                if panel is None:
                    item_ids = (sorted({int(v) for v in frame["item_id"].dropna().unique()})
                                if "item_id" in frame.columns else None)
                    panel, panel_note = load_panel(cfg, item_ids=item_ids)
                    if panel_note:
                        panel_notes.append(panel_note)
                risk_examples = build_risk_examples(panel, cfg.risk_horizons_h)
            for target in RISK_TARGETS:
                for horizon in cfg.risk_horizons_h:
                    key = f"{target}_{horizon}h"
                    report, payload = train_risk_target(risk_examples, target, horizon,
                                                        cfg, staging)
                    reports[key] = report
                    if payload is not None:
                        payloads[key] = payload

            # realm coverage is evidence, not a promise of a separate model
            realm_coverage: Dict[str, Dict[str, object]] = {}
            if "realm" in frame.columns:
                for realm, group in frame.groupby("realm"):
                    entry: Dict[str, object] = {
                        "rows": int(len(group)),
                        "sales": int((group.get("outcome") == "sale").sum())
                        if "outcome" in group.columns else 0,
                        "scope": "pooled_realm",
                    }
                    for horizon in cfg.horizons_h:
                        label = f"sold_within_{horizon}h"
                        known = f"label_known_{horizon}h"
                        if label in group.columns:
                            entry[f"labels_{horizon}h"] = int(
                                ((group[known] == 1) & group[label].notna()).sum())
                            entry[f"positives_{horizon}h"] = int(
                                group.loc[group[known] == 1, label].fillna(0).sum())
                    realm_coverage[str(realm)] = entry

            validated = sum(1 for r in reports.values() if r.status == "validated")
            #: The sale probability is the headline number the addon shows.
            #: If *it* did not replicate, the artifact must not advertise
            #: itself as validated just because the risk targets did.
            sale_reports = {k: r for k, r in reports.items() if k.startswith("sale_")}
            sale_validated = sum(1 for r in sale_reports.values()
                                 if r.status == "validated")
            sale_unconfirmed = sum(1 for r in sale_reports.values()
                                   if r.status == "validated_unconfirmed")
            if sale_validated:
                overall = "validated"
            elif sale_unconfirmed:
                overall = "sale_probability_unconfirmed"
            elif validated:
                overall = "auxiliary_targets_only"
            else:
                overall = "insufficient_or_baseline_only"
            manifest: Dict[str, object] = {
                "schema": SENTINEL_SCHEMA,
                "feature_schema": FEATURE_SCHEMA,
                "dataset_schema": DATASET_SCHEMA,
                "version": version,
                "created_at": now_iso(),
                "config": {
                    "horizons_h": list(cfg.horizons_h),
                    "risk_horizons_h": list(cfg.risk_horizons_h),
                    "seed": cfg.seed,
                    "test_fraction": cfg.test_fraction,
                    "calibration_fraction": cfg.calibration_fraction,
                    "iterations": cfg.iterations,
                    "min_sale_labels": cfg.min_sale_labels,
                    "min_price_rows": cfg.min_price_rows,
                    "min_risk_rows": cfg.min_risk_rows,
                },
                "training": {
                    "rows": int(len(frame)),
                    "first_ts": int(frame["ts"].min()) if "ts" in frame.columns else 0,
                    "last_ts": int(frame["ts"].max()) if "ts" in frame.columns else 0,
                    "observation_end": int(observation_end),
                    "realms": int(frame["realm"].nunique()) if "realm" in frame.columns else 0,
                    "risk_examples": int(len(risk_examples)) if risk_examples is not None else 0,
                    # Which panel the risk targets were fitted on.  A model
                    # is only comparable to another trained on the same
                    # evidence, so the source travels with the manifest.
                    "panel_source": str(cfg.panel_source),
                    "panel_notes": list(panel_notes),
                },
                "targets": {key: report.as_dict() for key, report in reports.items()},
                "horizon_equivalence": equivalence,
                "horizon_closure": closure,
                "degenerate_horizons": {str(k): v for k, v in degenerate.items()},
                "realm_coverage": realm_coverage,
                "validated_targets": validated,
                "sale_targets_validated": sale_validated,
                "sale_targets_unconfirmed": sale_unconfirmed,
                "status": overall,
            }
            manifest["checksums"] = _manifest_checksums(staging)
            _atomic_write_text(staging / "manifest.json", json.dumps(manifest, indent=2))
            # verify before promotion: a corrupt staging directory must never
            # become CURRENT
            check, reason = self._verify(staging, manifest)
            if not check:
                raise RuntimeError(f"the staged artifact did not verify: {reason}")
            result = TrainingResult(version=version, path=str(staging),
                                    promoted=False, report=manifest)
            if dry_run:
                return result
            final = promote_artifact(staging, cfg.models_root, version, manifest, cfg)
            result.path = str(final)
            result.promoted = True
            return result
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def _verify(self, directory: Path,
                manifest: Dict[str, object]) -> Tuple[bool, str]:
        for name, expected in (manifest.get("checksums") or {}).items():
            path = directory / name
            if not path.exists():
                return False, f"{name} is missing"
            if _sha256(path) != expected:
                return False, f"{name} checksum mismatch"
        if manifest.get("schema") != SENTINEL_SCHEMA:
            return False, "wrong schema"
        if manifest.get("feature_schema") != FEATURE_SCHEMA:
            return False, "wrong feature schema"
        # load the selected models back from disk, which catches a broken
        # save before the game ever sees it
        for key, entry in (manifest.get("targets") or {}).items():
            if (entry or {}).get("model_type") not in (None, "none"):
                try:
                    _load_target(directory, entry)
                except Exception as exc:
                    return False, f"{key}: {exc}"
        return True, ""


def monotone_probabilities(probabilities: Dict[int, float]) -> Dict[int, float]:
    """Project independently fitted horizon probabilities onto a curve.

    Selling by 24h is a subset of selling by 48h, so the curve must not
    decrease.  A cumulative maximum is the smallest monotone curve above
    the raw estimates; it is used only for display/export, while the
    per-horizon validation metrics stay as measured.
    """
    horizons = [h for h in sorted(probabilities) if probabilities[h] is not None]
    if not horizons:
        return {}
    # Pool adjacent violators: the L2 projection onto non-decreasing
    # probabilities, not a cumulative maximum that would overstate the
    # later horizons whenever one pair crosses.
    blocks: List[List[float]] = []  # [weighted_sum, weight]
    for horizon in horizons:
        blocks.append([float(probabilities[horizon]), 1.0])
        while len(blocks) > 1:
            previous, current = blocks[-2], blocks[-1]
            if previous[0] / previous[1] <= current[0] / current[1] + 1e-12:
                break
            merged = [previous[0] + current[0], previous[1] + current[1]]
            blocks[-2:] = [merged]
    out: Dict[int, float] = {}
    index = 0
    for total, weight in blocks:
        value = total / weight
        for _ in range(int(weight)):
            out[int(horizons[index])] = value
            index += 1
    return out


# ---------------------------------------------------------------------------
# prediction
# ---------------------------------------------------------------------------

@dataclass
class Prediction:
    """One prediction with its provenance, separate from its confidence."""

    status: str = "unknown"
    fallback_reason: str = ""
    horizon_h: int = 0
    probability: Optional[float] = None
    price: Optional[int] = None
    price_low: Optional[int] = None
    price_high: Optional[int] = None
    price_interval_level: Optional[float] = None
    price_interval_coverage: Optional[float] = None
    price_interval_n: int = 0
    price_interval_method: str = ""
    time_to_sale_hours: Optional[float] = None
    undercut_risk: Optional[float] = None
    price_decline_risk: Optional[float] = None
    training_scope: str = "none"
    model_version: str = ""
    model_type: str = ""
    calibrated: str = "identity"
    data_quality: float = 0.0
    sample_size: int = 0
    generated_at: str = ""
    flags: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


class SentinelPredictor:
    """Load the active artifact and answer with explicit provenance.

    Any failure returns ``status="unknown"`` with a reason.  There is no
    zero default and no silently substituted model.
    """

    def __init__(self, cfg: Optional[SentinelConfig] = None) -> None:
        self.cfg = cfg or SentinelConfig()
        self.artifact: Optional[SentinelArtifact] = None
        self.load_error = ""
        self.stale = False
        self.age_h = float("inf")

    def load(self) -> bool:
        artifact, reason = load_artifact(self.cfg.models_root)
        if artifact is None:
            self.load_error = reason
            self.artifact = None
            return False
        self.artifact = artifact
        self.load_error = ""
        fresh, age = check_freshness(artifact, self.cfg.max_model_age_h)
        self.stale, self.age_h = not fresh, age
        return True

    def target_status(self, key: str) -> Tuple[str, str]:
        if self.artifact is None:
            return "unknown", self.load_error or "no artifact loaded"
        entry = (self.artifact.manifest.get("targets") or {}).get(key)
        if not entry:
            return "unknown", "the target was not trained"
        status = str(entry.get("status") or "unknown")
        reason = str(entry.get("reason") or "")
        if self.stale and status in ("validated", "validated_ranking_only",
                                     "trained_no_measured_gain",
                                     "baseline_only"):
            return "stale", (f"the model is {self.age_h:.0f}h old; "
                             f"the freshness limit is {self.cfg.max_model_age_h:.0f}h")
        if key not in self.artifact.models and status in (
                "validated", "validated_ranking_only", "trained_no_measured_gain"):
            return "unknown", "the model artifact could not be loaded"
        return status, reason

    def _payload(self, key: str) -> Optional[Dict[str, object]]:
        if self.artifact is None:
            return None
        return self.artifact.models.get(key)

    def _scope(self, realm: str) -> str:
        coverage = ((self.artifact.manifest.get("realm_coverage") or {})
                    if self.artifact else {})
        entry = coverage.get(realm) or {}
        if entry.get("scope") == "pooled_realm" and int(entry.get("rows") or 0) >= 30:
            return "realm (pooled model)"
        return "region (pooled fallback)"

    def predict(self, base: Dict[str, object], realm: str = "",
                item_id: int = 0, horizon: int = 24,
                sample_size: int = 0,
                risk_features: Optional[Dict[str, float]] = None) -> Prediction:
        """Predict from a point-in-time feature row."""
        prediction = Prediction(horizon_h=horizon, model_version=(
            self.artifact.version if self.artifact else ""),
            generated_at=now_iso(), sample_size=int(sample_size))
        frame = pd.DataFrame([base])
        if "item_id" not in frame.columns:
            frame["item_id"] = item_id
        key = f"sale_{horizon}h"
        status, reason = self.target_status(key)
        payload = self._payload(key)
        prediction.training_scope = self._scope(realm)
        if status in ("unknown", "insufficient_data", "stale") or payload is None:
            prediction.status = "unknown"
            prediction.fallback_reason = reason or f"no validated {horizon}h sale model"
            prediction.flags.append("sale-probability-unknown")
        else:
            try:
                p = self._predict_classifier(payload, frame)
                prediction.probability = float(p[0])
                prediction.model_type = str(payload.get("model_type"))
                prediction.calibrated = str(payload.get("calibrator").method
                                            if payload.get("calibrator") else "identity")
                prediction.status = status
                prediction.data_quality = self._quality(payload, status, sample_size)
            except Exception as exc:
                prediction.status = "unknown"
                prediction.fallback_reason = f"the sale model failed: {exc}"

        # price
        price_status, price_reason = self.target_status("price")
        price_payload = self._payload("price")
        if price_status in ("unknown", "insufficient_data", "stale") or price_payload is None:
            prediction.flags.append("realized-price-unknown")
            if prediction.fallback_reason:
                prediction.fallback_reason += "; " + (price_reason or "no price model")
            else:
                prediction.fallback_reason = price_reason or "no validated price model"
        else:
            try:
                value = self._predict_regressor(price_payload, frame)
                prediction.price = int(round(float(np.exp(value[0]))))
                interval = price_payload.get("price_interval")
                conditional = price_payload.get("price_interval_conditional")
                selected_interval = interval
                if (isinstance(conditional, dict)
                        and isinstance(interval, dict)
                        and conditional.get("global")):
                    try:
                        dispersion = float(base.get("ref_dispersion"))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        dispersion = float("nan")
                    segments = conditional.get("segments") or {}
                    if math.isfinite(dispersion):
                        segment_name = ("low" if dispersion <= float(
                            conditional.get("threshold", 0.0)) else "high")
                    else:
                        segment_name = None
                    segment = (segments.get(segment_name)
                               if segment_name else None)
                    if (isinstance(segment, dict)
                            and segment.get("supported")
                            and segment.get("half_width_log")):
                        selected_interval = {
                            "half_width_log": float(segment["half_width_log"]),
                            "level": float(conditional.get("level")
                                           or interval.get("level") or 0.8),
                            "coverage": segment.get("coverage"),
                            "test_n": int(segment.get("test_n") or 0),
                            "calibration_n": int(segment.get("calibration_n") or 0),
                            "method": str(conditional.get("method")
                                          or "segmented-split-conformal"),
                        }
                if isinstance(selected_interval, dict) and selected_interval.get("half_width_log"):
                    half = float(selected_interval["half_width_log"])
                    prediction.price_low = max(
                        1, int(round(float(np.exp(value[0] - half)))))
                    prediction.price_high = max(
                        prediction.price_low,
                        int(round(float(np.exp(value[0] + half)))))
                    prediction.price_interval_level = float(
                        selected_interval.get("level") or 0.8)
                    coverage = selected_interval.get("coverage")
                    prediction.price_interval_coverage = (
                        float(coverage) if coverage is not None else None)
                    prediction.price_interval_n = int(
                        selected_interval.get("test_n") or 0)
                    prediction.price_interval_method = str(
                        selected_interval.get("method") or "split-conformal")
                else:
                    # A schema-1 artifact carried signed 10th/90th
                    # percentiles instead of a calibrated width.  Read it
                    # so an old artifact keeps working, but do not call
                    # its coverage measured: it never was.
                    legacy = price_payload.get("residual_interval")
                    if legacy and len(legacy) == 2:
                        prediction.price_low = max(
                            1, int(round(float(np.exp(value[0] + legacy[0])))))
                        prediction.price_high = max(
                            prediction.price_low,
                            int(round(float(np.exp(value[0] + legacy[1])))))
                        prediction.price_interval_level = 0.8
                        prediction.price_interval_method = "percentile-legacy"
                if prediction.status == "unknown":
                    prediction.status = price_status
            except Exception as exc:
                prediction.flags.append("realized-price-unknown")
                prediction.fallback_reason = ((prediction.fallback_reason + "; ")
                                              if prediction.fallback_reason else "") + str(exc)

        # time to sale
        tts_status, tts_reason = self.target_status("time_to_sale")
        tts_payload = self._payload("time_to_sale")
        if tts_status == "validated" and tts_payload is not None:
            try:
                model = tts_payload["model"]
                value = model.predict_median_hours(
                    align_features(frame, tts_payload["features"], tts_payload["cats"]))
                prediction.time_to_sale_hours = float(value[0])
            except Exception as exc:
                prediction.flags.append("time-to-sale-unknown")
                prediction.fallback_reason = ((prediction.fallback_reason + "; ")
                                              if prediction.fallback_reason else "") + str(exc)
        else:
            prediction.flags.append("time-to-sale-unknown")

        # risk targets
        prediction.undercut_risk = self._predict_risk(
            "undercut_pressure", horizon, risk_features, prediction)
        prediction.price_decline_risk = self._predict_risk(
            "price_decline", horizon, risk_features, prediction)
        return prediction

    def _predict_classifier(self, payload: Dict[str, object],
                            frame: pd.DataFrame) -> np.ndarray:
        model_type = str(payload.get("model_type"))
        features = list(payload.get("features") or [])
        cats = list(payload.get("cats") or [])
        if model_type == "catboost":
            raw = _catboost_predict(payload["model"], frame, features, cats)
        elif model_type == "logistic":
            raw = _sklearn_predict(payload["model"], frame, features)
        elif model_type == "baseline_item_rate":
            raw = _apply_item_rate(payload["rates"], frame)  # type: ignore[arg-type]
        elif model_type == "baseline_rate":
            raw = np.full(len(frame), float(payload.get("rate") or 0.0))
        else:
            raise ValueError(f"unsupported classifier type {model_type}")
        calibrator = payload.get("calibrator")
        return (calibrator.transform(raw) if isinstance(calibrator, Calibrator) else raw)

    def _predict_regressor(self, payload: Dict[str, object],
                           frame: pd.DataFrame) -> np.ndarray:
        model_type = str(payload.get("model_type"))
        features = list(payload.get("features") or [])
        cats = list(payload.get("cats") or [])
        if model_type == "catboost_price":
            model: PriceModel = payload["model"]  # type: ignore[assignment]
            return model.predict_log(frame)
        if model_type == "ridge":
            return _sklearn_predict(payload["model"], frame, features)
        if model_type == "baseline_item_median":
            return _apply_item_median(payload["medians"], frame)  # type: ignore[arg-type]
        raise ValueError(f"unsupported regressor type {model_type}")

    def _predict_risk(self, target: str, horizon: int,
                      risk_features: Optional[Dict[str, float]],
                      prediction: Prediction) -> Optional[float]:
        key = f"{target}_{horizon}h"
        status, _reason = self.target_status(key)
        payload = self._payload(key)
        if status not in PROBABILITY_STATUSES or payload is None:
            return None
        if risk_features is None:
            prediction.flags.append(f"{target}-unknown")
            return None
        frame = pd.DataFrame([risk_features])
        try:
            p = self._predict_classifier(payload, frame)
            if target == "undercut_pressure":
                prediction.flags.append("undercut-is-market-pressure")
            return float(p[0])
        except Exception as exc:
            prediction.flags.append(f"{target}-unknown")
            return None

    def _quality(self, payload: Dict[str, object], status: str,
                 sample_size: int = 0) -> float:
        """Data quality: how much *item-level* evidence backs this answer.

        This is deliberately not the model's probability.  A validated
        model answering from one observation is still a thin answer, so
        the score is dominated by the item's own sample and only partly
        by whether the target passed its holdout.
        """
        evidence = min(1.0, math.log1p(max(0, int(sample_size))) / math.log1p(30))
        model_quality = 1.0 if status == "validated" else 0.35
        score = 0.7 * evidence + 0.3 * model_quality
        return round(float(score), 3)


def retrain_decision(cfg: SentinelConfig, frame: pd.DataFrame,
                     observation_end: int) -> Tuple[bool, str]:
    """Should ``watch.py`` spend a training run on this data?

    The rule is deliberately conservative: train when there is no usable
    artifact, when the active one is stale, or when a meaningful number of
    *new confirmed labels* has arrived.  A tick that merely saw a new
    snapshot does not trigger a refit.
    """
    artifact, reason = load_artifact(cfg.models_root)
    if artifact is None:
        return True, reason or "there is no trained artifact"
    fresh, age_h = check_freshness(artifact, cfg.max_model_age_h)
    if not fresh:
        return True, (f"the active model is {age_h:.0f}h old and the limit is "
                      f"{cfg.max_model_age_h:.0f}h")
    training = artifact.manifest.get("training") or {}
    last_ts = int(training.get("last_ts") or 0)
    if observation_end <= last_ts:
        return False, "no observation is newer than the active model"
    horizon = min(cfg.horizons_h) if cfg.horizons_h else 24
    known = f"label_known_{horizon}h"
    if known not in frame.columns:
        return False, "the frame has no confirmed labels to count"
    new_rows = frame[(frame["ts"] > last_ts) & (frame[known] == 1)]
    n_train = int(training.get("rows") or 0)
    required = max(20, int(round(n_train * 0.10)))
    if len(new_rows) >= required:
        return True, (f"{len(new_rows)} new confirmed labels since the active model "
                      f"(threshold {required})")
    return False, (f"only {len(new_rows)} new confirmed labels since the active model "
                   f"(threshold {required})")


# ---------------------------------------------------------------------------
# report helper
# ---------------------------------------------------------------------------

def write_sentinel_report(result: TrainingResult,
                          path: Optional[Path] = None) -> Path:
    cfg = SentinelConfig()
    path = path or (cfg.reports_dir / "sentinel_report.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(result.as_dict(), indent=2, default=str))
    return path
