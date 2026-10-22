"""The three learned models.

**Model A -- the realizable price.**  A regression on the price a buyer
actually paid per unit and the money the seller actually pocketed per
unit.  It is trained against *transactions*, never against ``dbmarket`` or
``dbregionmarketavg``: those are asking prices and they are inputs here,
not answers.  The target lives on a log scale because the price
distribution spans four orders of magnitude.

**Model B -- the probability of selling.**  A classifier per horizon.
Rows whose outcome is censored, or whose outcome is a seller
cancellation, are not in the training set for that horizon; they are
counted instead.

**Model C -- the time to sell.**  A CatBoost ``SurvivalAft`` model: an
accelerated-failure-time model that takes censored observations natively.
A sale is an event, an expiry and an unobserved listing are right
censored.  This model is only fitted when the linkage quality report says
the outcomes can be trusted; otherwise the engine reports the limitation
instead of inventing a number.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import COPPER_PER_GOLD

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    from catboost import CatBoostError, CatBoostRegressor, Pool

    CATBOOST_AVAILABLE = True
except ImportError:  # pragma: no cover
    CatBoostError = Exception  # type: ignore
    CatBoostRegressor = None  # type: ignore
    Pool = None  # type: ignore
    CATBOOST_AVAILABLE = False


#: Columns that are never features.
NON_FEATURE_COLUMNS = (
    "item_id", "realm", "item_name", "ts", "date", "outcome", "elapsed_h",
    "inferred_duration_h", "ask_unit", "qty", "ambiguous",
    "y_gross_unit", "y_net_unit", "y_net_unit_log", "y_gross_unit_log",
    "y_net_gold", "fee_ratio_observed", "tts_hours", "tts_censored",
    # the raw references: kept for the credibility analysis, never a
    # feature (their logarithms already are)
    "ref_local_market", "ref_local_min_buyout", "ref_local_recent",
    "ref_local_historical", "ref_region_market", "ref_region_historical",
    "ref_region_sale_avg",
)


def feature_columns(frame: pd.DataFrame, drop: Sequence[str] = ()) -> List[str]:
    """The numeric feature columns of a frame, in a stable order."""
    blocked = set(NON_FEATURE_COLUMNS) | set(drop)
    cols = [c for c in frame.columns
            if c not in blocked and not c.startswith(("label_known_", "sold_within_"))
            and pd.api.types.is_numeric_dtype(frame[c])]
    return sorted(cols)


def categorical_columns(frame: pd.DataFrame) -> List[str]:
    cols = [c for c in frame.columns
            if c.startswith("cat_") and c not in NON_FEATURE_COLUMNS]
    return sorted(cols)


def make_pool(frame: pd.DataFrame,
              features: Sequence[str],
              categoricals: Sequence[str],
              label: Optional[pd.Series] = None,
              weight: Optional[pd.Series] = None,
              label_2d: Optional[np.ndarray] = None):
    """Build a CatBoost pool, with the categorical columns as strings."""
    if not CATBOOST_AVAILABLE:
        raise RuntimeError("catboost is not installed")
    data = frame.loc[:, list(features)].copy()
    for col in categoricals:
        if col in data.columns:
            data[col] = data[col].astype(str)
    cat_idx = [data.columns.get_loc(c) for c in categoricals if c in data.columns]
    kwargs = {}
    if label_2d is not None:
        kwargs["label"] = label_2d
    elif label is not None:
        kwargs["label"] = label
    if weight is not None:
        kwargs["weight"] = weight
    return Pool(data, cat_features=cat_idx, **kwargs)


@dataclass
class PriceModel:
    """Model A: the realizable price of a unit."""

    model: object = None
    features: List[str] = field(default_factory=list)
    categoricals: List[str] = field(default_factory=list)
    target: str = "y_net_unit_log"
    metrics: Dict[str, float] = field(default_factory=dict)
    importance: Dict[str, float] = field(default_factory=dict)
    #: the range of asks the strategy may offer, as a ratio of the
    #: regional sale average -- the range the model was trained on
    ask_ratio_bounds: Optional[Tuple[float, float]] = None
    trained: bool = False
    note: str = ""

    def predict_net_unit(self, frame: pd.DataFrame) -> np.ndarray:
        if not self.trained:
            raise RuntimeError("the price model is not trained")
        pool = make_pool(frame, self.features, self.categoricals)
        return np.exp(self.model.predict(pool))

    def predict_log(self, frame: pd.DataFrame) -> np.ndarray:
        if not self.trained:
            raise RuntimeError("the price model is not trained")
        pool = make_pool(frame, self.features, self.categoricals)
        return self.model.predict(pool)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(path))
        meta = {"features": self.features,
                "categoricals": self.categoricals,
                "target": self.target}
        bounds = getattr(self, "ask_ratio_bounds", None)
        if bounds:
            meta["ask_ratio_bounds"] = list(bounds)
        _write_sidecar(path, meta)

    @classmethod
    def load(cls, path: Path) -> "PriceModel":
        """Load a model together with the exact columns it was trained on.

        The sidecar matters: the price model is fitted without the asking
        features, and predicting with a different column set would be
        quietly wrong rather than loudly broken.
        """
        meta = _read_sidecar(path)
        if not meta:
            raise FileNotFoundError(
                f"{path} has no feature sidecar; refit the model")
        model = CatBoostRegressor()
        model.load_model(str(path))
        return cls(model=model, features=list(meta.get("features", [])),
                   categoricals=list(meta.get("categoricals", [])),
                   target=meta.get("target", "y_net_unit_log"), trained=True)


@dataclass
class SaleProbabilityModel:
    """Model B: the probability of selling inside a horizon."""

    model: object = None
    features: List[str] = field(default_factory=list)
    categoricals: List[str] = field(default_factory=list)
    horizon_h: int = 0
    metrics: Dict[str, float] = field(default_factory=dict)
    importance: Dict[str, float] = field(default_factory=dict)
    trained: bool = False
    note: str = ""

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if not self.trained:
            raise RuntimeError(f"the {self.horizon_h}h model is not trained")
        pool = make_pool(frame, self.features, self.categoricals)
        return self.model.predict_proba(pool)[:, 1]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(path))
        _write_sidecar(path, {"features": self.features,
                              "categoricals": self.categoricals,
                              "horizon_h": self.horizon_h,
                              "importance": self.importance})


@dataclass
class TimeToSaleModel:
    """Model C: the time to sell, with right censoring.

    CatBoost's ``SurvivalAft`` is an accelerated-failure-time model: it
    fits ``log(time) = f(features) + sigma * noise`` and takes censored
    observations natively.  Its output is the *location* of the
    distribution, not a time, so the reading is calibrated on the
    training rows: ``exp(a + b * f(x))`` is the conditional median time in
    hours.
    """

    model: object = None
    features: List[str] = field(default_factory=list)
    categoricals: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    importance: Dict[str, float] = field(default_factory=dict)
    calibration: Tuple[float, float] = (0.0, 1.0)
    trained: bool = False
    note: str = ""

    def _raw(self, frame: pd.DataFrame) -> np.ndarray:
        pool = make_pool(frame, self.features, self.categoricals)
        return np.asarray(self.model.predict(pool), dtype=float)

    def predict_median_hours(self, frame: pd.DataFrame) -> np.ndarray:
        """The conditional median time to sale, in hours."""
        if not self.trained:
            raise RuntimeError("the time-to-sale model is not trained")
        raw = self._raw(frame)
        a, b = self.calibration
        return np.exp(a + b * raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(path))
        _write_sidecar(path, {"features": self.features,
                              "categoricals": self.categoricals,
                              "calibration": list(self.calibration)})


# --------------------------------------------------------------------------
# the sidecar: the columns a model was fitted on
# --------------------------------------------------------------------------

def _sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".features.json")


def _write_sidecar(path: Path, meta: Dict[str, object]) -> None:
    import json
    _sidecar_path(path).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _read_sidecar(path: Path) -> Dict[str, object]:
    import json
    side = _sidecar_path(path)
    if not side.exists():
        return {}
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_time_to_sale_model(path: Path) -> "TimeToSaleModel":
    """Load the survival model with the columns and calibration it was fitted on."""
    from catboost import CatBoostRegressor
    meta = _read_sidecar(path)
    if not meta:
        raise FileNotFoundError(f"{path} has no feature sidecar; refit the model")
    model = CatBoostRegressor()
    model.load_model(str(path))
    calibration = meta.get("calibration") or [0.0, 1.0]
    return TimeToSaleModel(model=model,
                           features=list(meta.get("features", [])),
                           categoricals=list(meta.get("categoricals", [])),
                           calibration=(float(calibration[0]), float(calibration[1])),
                           trained=True)


def load_sale_probability_model(path: Path) -> "SaleProbabilityModel":
    """Load a probability model with the columns it was fitted on."""
    from catboost import CatBoostClassifier
    meta = _read_sidecar(path)
    if not meta:
        raise FileNotFoundError(f"{path} has no feature sidecar; refit the model")
    model = CatBoostClassifier()
    model.load_model(str(path))
    return SaleProbabilityModel(model=model, features=list(meta.get("features", [])),
                                 categoricals=list(meta.get("categoricals", [])),
                                 horizon_h=int(meta.get("horizon_h", 0)),
                                 importance={k: float(v) for k, v
                                             in (meta.get("importance") or {}).items()},
                                 trained=True)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def price_metrics(y_true_log: np.ndarray, y_pred_log: np.ndarray,
                  y_true_net: Optional[np.ndarray] = None) -> Dict[str, float]:
    """Errors of the price model, on the log scale and on the gold scale."""
    out: Dict[str, float] = {}
    if len(y_true_log) == 0:
        return out
    err = y_pred_log - y_true_log
    out["rmse_log"] = float(np.sqrt(np.mean(err ** 2)))
    out["mae_log"] = float(np.mean(np.abs(err)))
    out["bias_log"] = float(np.mean(err))
    # the median of the multiplicative error, i.e. the typical over/under
    # statement of the estimate
    out["median_ratio"] = float(np.median(np.exp(err)))
    out["within_25pct"] = float(np.mean(np.abs(np.expm1(err)) <= 0.25))
    out["within_50pct"] = float(np.mean(np.abs(np.expm1(err)) <= 0.50))
    if y_true_net is not None and len(y_true_net) == len(y_pred_log):
        true_gold = np.asarray(y_true_net, dtype=float) / COPPER_PER_GOLD
        pred_gold = np.exp(y_pred_log) / COPPER_PER_GOLD
        out["mae_gold"] = float(np.mean(np.abs(pred_gold - true_gold)))
        out["smape_gold"] = float(np.mean(
            2.0 * np.abs(pred_gold - true_gold)
            / np.maximum(np.abs(pred_gold) + np.abs(true_gold), 1e-9)))
    return out


def probability_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if len(y_true) == 0:
        return out
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.clip(np.asarray(y_pred, dtype=float), 1e-6, 1 - 1e-6)
    out["n"] = int(len(y_true))
    out["base_rate"] = float(y_true.mean())
    out["mean_predicted"] = float(y_pred.mean())
    out["logloss"] = float(-np.mean(y_true * np.log(y_pred)
                                     + (1 - y_true) * np.log(1 - y_pred)))
    out["brier"] = float(np.mean((y_pred - y_true) ** 2))
    out["auc"] = float(_auc(y_true, y_pred))
    try:
        from sklearn.metrics import average_precision_score
        out["average_precision"] = float(average_precision_score(y_true, y_pred))
    except Exception:
        pass
    return out


def _auc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Rank AUC without a dependency on scikit-learn."""
    order = np.argsort(y_pred)
    ranks = np.empty(len(y_pred), dtype=float)
    ranks[order] = np.arange(1, len(y_pred) + 1)
    # average the ranks of ties
    sorted_pred = y_pred[order]
    i = 0
    while i < len(sorted_pred):
        j = i
        while j + 1 < len(sorted_pred) and sorted_pred[j + 1] == sorted_pred[i]:
            j += 1
        if j > i:
            avg = (i + 1 + j + 1) / 2.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
        i = j + 1
    pos = y_true > 0.5
    n_pos = int(pos.sum())
    n_neg = int(len(y_true) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def concordance_index(y_true: np.ndarray, y_pred: np.ndarray,
                      event: Optional[np.ndarray] = None) -> float:
    """Harrell's C-index for right-censored durations (higher is better).

    ``y_true`` is the observed duration and ``y_pred`` is the predicted
    duration.  Lower predictions mean that the event is expected sooner,
    so a pair is concordant when the earlier observation also has the
    smaller prediction.

    ``event`` must be true for an observed event and false for a
    right-censored observation.  A pair is comparable only when the
    earlier observation is an event: a censored row tells us only that
    the event had not happened yet, so it cannot establish an ordering by
    itself.  Omitting ``event`` treats every row as an observed event.

    Tied durations are comparable only when both rows are events; a tied
    prediction contributes one half, because the model did not separate
    the pair.  Rows with non-finite values are dropped rather than allowed
    to corrupt the score.  ``nan`` is returned when no pair is comparable.
    """
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_pred, dtype=float)
    if y.shape != p.shape:
        raise ValueError("durations and predictions must have the same shape")
    if event is None:
        observed = np.ones(len(y), dtype=bool)
    else:
        raw = np.asarray(event)
        if raw.shape != y.shape:
            raise ValueError("events must have the same shape as durations")
        try:
            numeric = raw.astype(float)
            observed = np.isfinite(numeric) & (numeric != 0.0)
        except (TypeError, ValueError):
            observed = raw.astype(bool)
    keep = np.isfinite(y) & np.isfinite(p)
    y, p, observed = y[keep], p[keep], observed[keep]
    n = len(y)
    if n < 2:
        return float("nan")

    concordant = 0.0
    total = 0.0
    # Chunk the comparison matrix so the working set stays bounded even
    # when the validation block is large.  Each pair is visited once
    # (i < j); the old double loop counted every pair twice.
    chunk = 512
    indices = np.arange(n)
    for start in range(0, n, chunk):
        stop = min(n, start + chunk)
        yi = y[start:stop, None]
        pi = p[start:stop, None]
        ei = observed[start:stop, None]
        yj = y[None, :]
        pj = p[None, :]
        ej = observed[None, :]
        later = indices[None, :] > indices[start:stop, None]
        earlier_i = later & ei & (yi < yj)
        earlier_j = later & ej & (yj < yi)
        comparable = earlier_i | earlier_j
        total += float(comparable.sum())
        equal_pred_i = earlier_i & (pi == pj)
        equal_pred_j = earlier_j & (pj == pi)
        concordant += float((earlier_i & (pi < pj)).sum())
        concordant += float((earlier_j & (pj < pi)).sum())
        concordant += 0.5 * float(equal_pred_i.sum() + equal_pred_j.sum())
        # Two observed events at exactly the same time give no temporal
        # ordering.  They are still informative: a model that scores them
        # equally gets full credit, otherwise half.
        tied_events = later & ei & ej & (yi == yj)
        n_tied = float(tied_events.sum())
        if n_tied:
            total += n_tied
            concordant += 0.5 * n_tied
            concordant += 0.5 * float((tied_events & (pi == pj)).sum())
    return float(concordant / total) if total else float("nan")


# --------------------------------------------------------------------------
# training
# --------------------------------------------------------------------------

#: How the price model grows its trees.  Chosen by measuring the
#: walk-forward on this archive, not by taste: a leaf-wise tree with 31
#: leaves and at least 10 rows per leaf scored rmse_log 0.93 against 1.03
#: for the symmetric default on the same folds, for every seed tried, and
#: the gap held at 300 and at 600 trees.  The price target has 355 rows, so
#: a symmetric tree of depth five is too coarse to split an item's own
#: price band; a leaf-wise one spends its splits where the price actually
#: varies.
PRICE_GROW_POLICY = "Lossguide"
PRICE_MAX_LEAVES = 31
PRICE_MIN_DATA_IN_LEAF = 10


def train_price_model(train: pd.DataFrame,
                      valid: Optional[pd.DataFrame] = None,
                      features: Optional[Sequence[str]] = None,
                      categoricals: Optional[Sequence[str]] = None,
                      target: str = "y_net_unit_log",
                      iterations: int = 300,
                      learning_rate: float = 0.05,
                      depth: int = 5,
                      seed: int = 7,
                      l2_leaf_reg: float = 6.0,
                      early_stopping: bool = True,
                      verbose: bool = False) -> PriceModel:
    """Fit the price model.

    ``early_stopping`` uses the validation rows to choose the number of
    trees.  That is fine when the validation rows are a *different* day
    from the rows that are scored, and it is **not** fine when the same
    rows are used for both -- the score would then be measured on the
    data that chose the model.  The walk-forward therefore fits with
    ``early_stopping=False`` and a fixed number of trees, and the score
    is honest.
    """
    if not CATBOOST_AVAILABLE:
        raise RuntimeError("catboost is not installed")
    feats = list(features) if features else feature_columns(train)
    cats = list(categoricals) if categoricals is not None else categorical_columns(train)
    model = PriceModel(features=feats, categoricals=cats, target=target)
    if len(train) < 30:
        model.note = f"only {len(train)} training rows"
        return model

    params = dict(
        loss_function="RMSE",
        iterations=iterations,
        learning_rate=learning_rate,
        depth=depth,
        l2_leaf_reg=l2_leaf_reg,
        random_seed=seed,
        verbose=False,
        allow_writing_files=False,
        grow_policy=PRICE_GROW_POLICY,
        max_leaves=PRICE_MAX_LEAVES,
        min_data_in_leaf=PRICE_MIN_DATA_IN_LEAF,
    )
    if early_stopping:
        params.update(od_type="Iter", od_wait=80)
    train_pool = make_pool(train, feats, cats, label=train[target])
    model.model = CatBoostRegressor(**params)
    if early_stopping and valid is not None and len(valid) and valid[target].notna().any():
        valid_pool = make_pool(valid.dropna(subset=[target]), feats, cats,
                               label=valid.dropna(subset=[target])[target])
        model.model.fit(train_pool, eval_set=valid_pool)
    else:
        model.model.fit(train_pool)

    model.trained = True
    imp = model.model.get_feature_importance()
    model.importance = {f: float(v) for f, v in zip(feats, imp)}
    if valid is not None and len(valid):
        mask = valid[target].notna()
        if mask.any():
            y = valid.loc[mask, target].to_numpy(dtype=float)
            p = model.model.predict(make_pool(valid.loc[mask], feats, cats))
            model.metrics = price_metrics(y, np.asarray(p, dtype=float))
    return model


def train_sale_probability_model(train: pd.DataFrame,
                                 valid: Optional[pd.DataFrame],
                                 horizon_h: int,
                                 features: Optional[Sequence[str]] = None,
                                 categoricals: Optional[Sequence[str]] = None,
                                 iterations: int = 300,
                                 learning_rate: float = 0.05,
                                 depth: int = 5,
                                 seed: int = 7,
                                 l2_leaf_reg: float = 6.0,
                                 early_stopping: bool = True) -> SaleProbabilityModel:
    if not CATBOOST_AVAILABLE:
        raise RuntimeError("catboost is not installed")
    label = f"sold_within_{horizon_h}h"
    feats = list(features) if features else feature_columns(train)
    cats = list(categoricals) if categoricals is not None else categorical_columns(train)
    model = SaleProbabilityModel(features=feats, categoricals=cats, horizon_h=horizon_h)
    train = train[train[f"label_known_{horizon_h}h"] == 1]
    if len(train) < 30 or train[label].nunique() < 2:
        model.note = (f"only {len(train)} usable rows "
                      f"({int(train[label].sum())} positive) for {horizon_h}h")
        return model
    params = dict(
        loss_function="Logloss",
        iterations=iterations,
        learning_rate=learning_rate,
        depth=depth,
        l2_leaf_reg=l2_leaf_reg,
        random_seed=seed,
        verbose=False,
        allow_writing_files=False,
    )
    if early_stopping:
        params.update(od_type="Iter", od_wait=80)
    train_pool = make_pool(train, feats, cats, label=train[label])
    model.model = _make_classifier(params)
    if early_stopping and valid is not None:
        valid = valid[valid[f"label_known_{horizon_h}h"] == 1]
        if len(valid) and valid[label].nunique() >= 1:
            valid_pool = make_pool(valid, feats, cats, label=valid[label])
            model.model.fit(train_pool, eval_set=valid_pool)
        else:
            model.model.fit(train_pool)
    else:
        model.model.fit(train_pool)
    model.trained = True
    imp = model.model.get_feature_importance()
    model.importance = {f: float(v) for f, v in zip(feats, imp)}
    if valid is not None and len(valid):
        y = valid[label].to_numpy(dtype=float)
        p = model.model.predict_proba(make_pool(valid, feats, cats))[:, 1]
        model.metrics = probability_metrics(y, p)
    return model


def _make_classifier(params: Dict[str, object]):
    from catboost import CatBoostClassifier
    return CatBoostClassifier(**params)


def train_time_to_sale_model(train: pd.DataFrame,
                             valid: Optional[pd.DataFrame],
                             features: Optional[Sequence[str]] = None,
                             categoricals: Optional[Sequence[str]] = None,
                             iterations: int = 300,
                             learning_rate: float = 0.05,
                             depth: int = 5,
                             seed: int = 7,
                             l2_leaf_reg: float = 6.0,
                             early_stopping: bool = True) -> TimeToSaleModel:
    """Fit the censored time-to-sale model.

    CatBoost's ``SurvivalAft`` takes a two-column *interval* label:
    ``[duration, duration]`` for an observed sale and ``[duration, -1]``
    for a right-censored listing (``-1`` means ``+infinity``).  The second
    column is an upper bound, not an event flag; passing an event flag
    there describes a different, invalid interval and was the reason the
    old model's C-index sat below chance.
    """
    if not CATBOOST_AVAILABLE:
        raise RuntimeError("catboost is not installed")
    feats = list(features) if features else feature_columns(train)
    cats = list(categoricals) if categoricals is not None else categorical_columns(train)
    model = TimeToSaleModel(features=feats, categoricals=cats)
    usable = train[train["tts_hours"].notna() & (train["tts_hours"] > 0)]
    if len(usable) < 40:
        model.note = f"only {len(usable)} rows with a usable duration"
        return model
    params = dict(
        loss_function="SurvivalAft:dist=Normal",
        iterations=iterations,
        learning_rate=learning_rate,
        depth=depth,
        l2_leaf_reg=l2_leaf_reg,
        random_seed=seed,
        verbose=False,
        allow_writing_files=False,
    )
    if early_stopping:
        params.update(od_type="Iter", od_wait=80)
    train_pool = make_pool(usable, feats, cats,
                           label_2d=_survival_label(usable))
    model.model = _make_survival(params)
    if early_stopping and valid is not None:
        v = valid[valid["tts_hours"].notna() & (valid["tts_hours"] > 0)]
        if len(v) >= 10:
            model.model.fit(train_pool, eval_set=make_pool(v, feats, cats,
                                                           label_2d=_survival_label(v)))
        else:
            model.model.fit(train_pool)
    else:
        model.model.fit(train_pool)
    model.trained = True
    imp = model.model.get_feature_importance()
    model.importance = {f: float(v) for f, v in zip(feats, imp)}

    # The raw output is the AFT *location*: log(time) = f(x) + sigma * noise.
    # The reading into hours is a calibration, and a calibration that came
    # out with a negative slope would flip the model's ranking -- so the
    # quality of the model is measured on the raw output, and a calibration
    # that would invert it is not applied.
    raw = np.asarray(model.model.predict(make_pool(usable, feats, cats)), dtype=float)
    events = usable["tts_censored"].to_numpy() == 0
    model.calibration = (0.0, 1.0)
    if events.sum() >= 10:
        y = np.log(usable.loc[events, "tts_hours"].to_numpy(dtype=float))
        b, a = np.polyfit(raw[events], y, 1)
        if b > 0:
            model.calibration = (float(a), float(b))
        else:
            model.note = ("the fitted reading of the survival model came out "
                          "inverted; no time estimate is offered")

    if valid is not None and len(valid) >= 10:
        mask = valid["tts_hours"].notna() & (valid["tts_hours"] > 0)
        sub = valid[mask]
        if len(sub) >= 10:
            y = sub["tts_hours"].to_numpy(dtype=float)
            sub_events = sub["tts_censored"].to_numpy() == 0
            # the ranking quality is measured on the raw location
            raw_valid = np.asarray(model.model.predict(make_pool(sub, feats, cats)),
                                   dtype=float)
            model.metrics = {
                "c_index": concordance_index(y, raw_valid, event=sub_events),
                "c_index_calibrated": concordance_index(
                    y, model.predict_median_hours(sub), event=sub_events),
                "n": int(len(sub)),
                "n_events": int(sub_events.sum()),
                "n_censored": int((~sub_events).sum()),
                "censoring_rate": float((~sub_events).mean()),
            }
    return model


def _survival_label(frame: pd.DataFrame) -> np.ndarray:
    """CatBoost's survival interval: ``[lower, upper]``.

    An observed sale has a known duration, so the interval is
    ``[duration, duration]``.  A right-censored listing is known only to
    have lasted at least ``duration``, which CatBoost writes as
    ``[duration, -1]`` (``-1`` means ``+infinity``).
    """
    duration = frame["tts_hours"].to_numpy(dtype=float)
    event = frame["tts_censored"].to_numpy() == 0
    upper = np.where(event, duration, -1.0)
    return np.column_stack([duration, upper])


def train_price_quantiles(train: pd.DataFrame,
                          features: Sequence[str],
                          categoricals: Sequence[str],
                          quantiles: Sequence[float] = (0.1, 0.9),
                          iterations: int = 300,
                          learning_rate: float = 0.05,
                          depth: int = 5,
                          seed: int = 7,
                          l2_leaf_reg: float = 6.0) -> Dict[float, object]:
    """The spread of the price, not just its middle.

    A point estimate that says "this item is worth 40 gold" is worth much
    less than one that says "this item is worth 40 gold, somewhere between 22
    and 85, and the width tells you how much to trust it".  The quantiles
    are fitted on the same log scale as the point estimate, so the
    interval multiplies out of the logarithms.
    """
    if not CATBOOST_AVAILABLE:
        raise RuntimeError("catboost is not installed")
    out: Dict[float, object] = {}
    usable = train[train["y_net_unit_log"].notna()]
    if len(usable) < 40:
        return out
    for alpha in quantiles:
        params = dict(
            loss_function=f"Quantile:alpha={alpha}",
            iterations=iterations,
            learning_rate=learning_rate,
            depth=depth,
            l2_leaf_reg=l2_leaf_reg,
            random_seed=seed,
            verbose=False,
            allow_writing_files=False,
        )
        model = CatBoostRegressor(**params)
        model.fit(make_pool(usable, list(features), list(categoricals),
                            label=usable["y_net_unit_log"]))
        out[alpha] = model
    return out


def load_price_quantiles(path_stem: Path) -> Dict[float, object]:
    """Load the quantile models saved beside the price model."""
    out: Dict[float, object] = {}
    for alpha in (0.1, 0.9):
        path = Path(f"{path_stem}.q{int(alpha * 100)}.cbm")
        if not path.exists():
            continue
        model = CatBoostRegressor()
        model.load_model(str(path))
        out[alpha] = model
    return out


def _make_survival(params: Dict[str, object]):
    from catboost import CatBoostRegressor
    return CatBoostRegressor(**params)
