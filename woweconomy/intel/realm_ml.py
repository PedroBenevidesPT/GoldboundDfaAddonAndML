"""One model per (region, realm).  Never one model for all of Europe.

A realm's auction house has its own population, its own supply and its
own price level.  Pooling Dragonmaw with Silvermoon produces a model that
is wrong for both, and a prediction that cannot say which market it
describes.

The rules implemented here:

* every training frame is filtered to exactly one realm before it reaches
  CatBoost.  ``train_realm`` asserts it, and the test monkeypatches the
  trainer to prove it;
* a realm with too few observed sales is reported as
  ``insufficient_data`` -- it does not inherit another realm's model;
* the out-of-sample score is chronological within the realm, using the
  existing :func:`woweconomy.ml.validate.make_split` and a fixed number
  of trees (no early stopping on the window being scored);
* a shared prior is only *reported* after it beats the realm-only model
  on the realm's own untouched window.  Even then it is a blend weight,
  not a pooled prediction: the served number stays anchored to the realm.
"""
from __future__ import annotations

import logging
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from woweconomy.ml.features import ASK_FEATURES
from woweconomy.ml.models import (
    CATBOOST_AVAILABLE,
    categorical_columns,
    feature_columns,
    price_metrics,
    train_price_model,
)
from woweconomy.ml.validate import aggregate, make_split

from . import store

logger = logging.getLogger(__name__)

DEFAULT_MODELS_DIR = Path(r"C:\FarmAnalysis\models\intel")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def realm_slug(realm: str) -> str:
    """An ASCII, filesystem-safe slug (``Aggra (Português)`` -> ``aggra-portugues``)."""
    text = unicodedata.normalize("NFKD", str(realm))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("'", "").replace("(", "").replace(")", "")
    text = "".join(ch if ch.isalnum() else "-" for ch in text)
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-") or "unknown"


def realm_key(region: str, realm: str) -> str:
    return f"{region.lower()}:{realm.strip().casefold()}"


@dataclass
class RealmMLConfig:
    region: str = "eu"
    models_dir: Path = DEFAULT_MODELS_DIR
    min_price_rows: int = 40
    min_valid_rows: int = 10
    n_folds: int = 3
    test_fraction: float = 0.25
    iterations: int = 300
    seed: int = 7
    #: a shared prior must win by at least this much on the realm's own
    #: untouched window before it is even reported as usable
    min_prior_gain: float = 0.05

    def ensure_dirs(self) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class RealmResult:
    region: str
    realm: str
    model_kind: str
    status: str
    n_rows: int = 0
    n_price_rows: int = 0
    metrics: Dict[str, float] = field(default_factory=dict)
    baseline_metrics: Dict[str, float] = field(default_factory=dict)
    #: metrics on the untouched window that follows every fold
    holdout_metrics: Dict[str, float] = field(default_factory=dict)
    holdout_baseline_metrics: Dict[str, float] = field(default_factory=dict)
    holdout_rows: int = 0
    holdout_span: Tuple[int, int] = (0, 0)
    #: the expanding-window folds, kept for transparency next to the holdout
    fold_metrics: Dict[str, float] = field(default_factory=dict)
    fold_baseline_metrics: Dict[str, float] = field(default_factory=dict)
    #: item-median and last-price controls on the same holdout rows
    extra_holdout_baselines: Dict[str, Dict[str, float]] = field(default_factory=dict)
    model_path: str = ""
    note: str = ""
    folds: int = 0

    @property
    def skill_vs_baseline(self) -> Optional[float]:
        """1 - model_rmse / baseline_rmse.  Positive means the model learned."""
        def rmse(values: Dict[str, float]) -> Optional[float]:
            # ``aggregate`` suffixes the per-fold mean; a single-fold
            # metric arrives unsuffixed.  Both are the same quantity.
            value = values.get("rmse_log", values.get("rmse_log_mean"))
            return float(value) if value is not None else None

        model_rmse = rmse(self.metrics)
        base_rmse = rmse(self.baseline_metrics)
        if not model_rmse or not base_rmse:
            return None
        return float(1.0 - model_rmse / base_rmse)


def split_by_realm(frame: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if frame.empty or "realm" not in frame.columns:
        return {}
    return {str(realm): sub.copy() for realm, sub in frame.groupby("realm", sort=True)}


def _realm_frame(frame: pd.DataFrame, realm: str) -> pd.DataFrame:
    if frame.empty or "realm" not in frame.columns:
        return frame.iloc[0:0].copy()
    mask = frame["realm"].astype(str).str.casefold() == realm.strip().casefold()
    return frame.loc[mask].copy()


def _training_columns(frame: pd.DataFrame) -> Tuple[List[str], List[str], List[str]]:
    numeric = feature_columns(frame)
    price_feats = [c for c in numeric if c not in ASK_FEATURES]
    cats = categorical_columns(frame)
    return numeric, price_feats, cats


def _baseline(train: pd.DataFrame, valid: pd.DataFrame,
              target: str = "y_net_unit_log") -> Dict[str, float]:
    """The constant prediction: the training median.  A model must beat it."""
    known = train[target].dropna()
    if known.empty or valid.empty:
        return {}
    constant = float(known.median())
    y = valid[target].dropna().to_numpy(dtype=float)
    if len(y) == 0:
        return {}
    return price_metrics(y, np.full(len(y), constant))


def _item_median_baseline(train: pd.DataFrame, valid: pd.DataFrame,
                          target: str = "y_net_unit_log") -> Dict[str, float]:
    """Per-item training median, falling back to the global median.

    This is the control that matters for a market: a model that cannot
    beat "remember what this item usually sold for" has learned nothing
    about the realm's dynamics.
    """
    if "item_id" not in train.columns or valid.empty:
        return {}
    known = train[train[target].notna()]
    if known.empty:
        return {}
    by_item = known.groupby("item_id")[target].median()
    fallback = float(known[target].median())
    rows = valid[valid[target].notna()]
    if rows.empty:
        return {}
    pred = rows["item_id"].map(by_item).fillna(fallback).to_numpy(dtype=float)
    return price_metrics(rows[target].to_numpy(dtype=float), pred)


def _last_price_baseline(train: pd.DataFrame, valid: pd.DataFrame,
                         target: str = "y_net_unit_log") -> Dict[str, float]:
    """Naive persistence: the item's most recent training price.

    A model that cannot beat "the price did not move" is not useful for a
    market whose whole question is how the price moves.
    """
    if "item_id" not in train.columns or valid.empty:
        return {}
    known = train[train[target].notna()]
    if known.empty:
        return {}
    order = [c for c in ("date", "ts") if c in known.columns]
    ordered = known.sort_values(order, kind="stable") if order else known
    last = ordered.groupby("item_id")[target].last()
    fallback = float(ordered[target].iloc[-1])
    rows = valid[valid[target].notna()]
    if rows.empty:
        return {}
    pred = rows["item_id"].map(last).fillna(fallback).to_numpy(dtype=float)
    return price_metrics(rows[target].to_numpy(dtype=float), pred)


def _latest_day_holdout(sub: pd.DataFrame, cfg: "RealmMLConfig",
                        target: str = "y_net_unit_log"):
    """Grow the test window backwards until it holds enough priced sales.

    A fixed fraction of *days* is wrong for a sparse realm: its last day
    can contain four sales, leaving an empty holdout.  This walks back
    day by day, and every training row still sits strictly before the
    first test day.
    """
    priced = sub[sub[target].notna()].copy()
    days = sorted(int(d) for d in priced["date"].dropna().unique())
    test_days: List[int] = []
    for day in reversed(days):
        test_days.insert(0, day)
        test = priced.loc[priced["date"].isin(test_days)]
        train = priced.loc[priced["date"] < min(test_days)]
        if (len(test) >= cfg.min_valid_rows
                and len(train) >= cfg.min_price_rows):
            return train, test, (min(test_days), max(test_days))
        if len(train) < cfg.min_price_rows:
            return None
    return None


def _select_holdout(sub: pd.DataFrame, cfg: "RealmMLConfig"):
    """Reserve the last window *before* any fold is built.

    The fixed day fraction is tried first.  If its test window is too
    sparse to score, the window grows backwards until it holds enough
    priced sales.  Either way the folds are built only on days before
    the chosen window, so no fold can ever validate inside it.
    """
    target = "y_net_unit_log"
    priced = sub[sub[target].notna()].copy()
    days = sorted(int(d) for d in priced["date"].dropna().unique())
    if len(days) < 2:
        return None
    n_test = max(1, int(round(len(days) * cfg.test_fraction)))
    test_days = days[-n_test:]
    train = priced[priced["date"] < test_days[0]]
    test = priced[priced["date"].isin(test_days)]
    if len(test) >= cfg.min_valid_rows and len(train) >= cfg.min_price_rows:
        return train, test, (test_days[0], test_days[-1])
    return _latest_day_holdout(sub, cfg, target)


def _score_holdout(train: pd.DataFrame, test: pd.DataFrame, span,
                   cfg: "RealmMLConfig", price_feats: List[str],
                   cats: List[str]) -> Optional[Dict[str, object]]:
    """Fit on every development day, score the untouchable test window.

    Nothing from the test span reaches the trainer: the frame is cut on
    the clock, not by row.  The three controls are computed on the exact
    same rows so the comparison is like for like.
    """
    target = "y_net_unit_log"
    if train["realm"].astype(str).str.casefold().nunique() != 1:
        raise AssertionError("realm training frame contains more than one realm")
    model = train_price_model(train, None, price_feats, cats,
                              early_stopping=False,
                              iterations=cfg.iterations, seed=cfg.seed)
    if not model.trained:
        return None
    y = test[target].to_numpy(dtype=float)
    pred = np.asarray(model.predict_log(test), dtype=float)
    metrics = price_metrics(
        y, pred, test["y_net_unit"].to_numpy(dtype=float)
        if "y_net_unit" in test.columns else None)
    controls = {
        "training_median": _baseline(train, test),
        "item_median": _item_median_baseline(train, test),
        "last_price": _last_price_baseline(train, test),
    }
    return {"metrics": metrics,
            "baselines": {k: v for k, v in controls.items() if v},
            "rows": int(len(test)),
            "span": (int(span[0]), int(span[1]))}


def evaluate_realm_price(frame: pd.DataFrame, realm: str,
                         cfg: Optional[RealmMLConfig] = None) -> RealmResult:
    """Chronological, realm-only validation of the price model."""
    cfg = cfg or RealmMLConfig()
    result = RealmResult(region=cfg.region, realm=realm,
                         model_kind="price", status="insufficient_data")
    sub = _realm_frame(frame, realm)
    result.n_rows = int(len(sub))
    if sub.empty or "y_net_unit_log" not in sub.columns:
        result.note = "the realm has no rows"
        return result
    sub = sub[sub["date"].notna()].copy()
    priced = sub[sub["y_net_unit_log"].notna()]
    result.n_price_rows = int(len(priced))
    if len(priced) < cfg.min_price_rows:
        result.note = (f"{len(priced)} observed sales, need {cfg.min_price_rows};"
                       " no pooled fallback is used")
        return result
    holdout = _select_holdout(sub, cfg)
    # Folds live strictly before the reserved window; their own test split
    # is not scored, so no fold can touch the holdout.
    dev = sub[sub["date"] < holdout[2][0]] if holdout else sub
    split = make_split(dev, n_folds=cfg.n_folds,
                       test_fraction=cfg.test_fraction,
                       min_train_rows=cfg.min_price_rows,
                       min_valid_rows=cfg.min_valid_rows)
    numeric, price_feats, cats = _training_columns(sub)
    fold_metrics: List[Dict[str, float]] = []
    baseline_metrics: List[Dict[str, float]] = []
    for fold in split.folds:
        train = fold.train[fold.train["y_net_unit_log"].notna()]
        valid = fold.valid[fold.valid["y_net_unit_log"].notna()]
        if len(train) < cfg.min_price_rows or len(valid) < cfg.min_valid_rows:
            continue
        if train["realm"].astype(str).str.casefold().nunique() != 1:
            raise AssertionError("realm training frame contains more than one realm")
        model = train_price_model(train, None, price_feats, cats,
                                  early_stopping=False,
                                  iterations=cfg.iterations, seed=cfg.seed)
        if not model.trained:
            continue
        y = valid["y_net_unit_log"].to_numpy(dtype=float)
        pred = np.asarray(model.predict_log(valid), dtype=float)
        fold_metrics.append(price_metrics(
            y, pred, valid["y_net_unit"].to_numpy(dtype=float)
            if "y_net_unit" in valid.columns else None))
        base = _baseline(train, valid)
        if base:
            baseline_metrics.append(base)
    result.folds = len(fold_metrics)
    result.fold_metrics = aggregate(fold_metrics)
    result.fold_baseline_metrics = aggregate(baseline_metrics)
    scored = (_score_holdout(holdout[0], holdout[1], holdout[2], cfg,
                             price_feats, cats)
              if holdout is not None else None)
    if scored is not None:
        result.holdout_metrics = aggregate([scored["metrics"]])
        baselines = scored["baselines"]
        result.holdout_baseline_metrics = aggregate(
            [baselines["training_median"]] if "training_median" in baselines else [])
        result.extra_holdout_baselines = {
            name: aggregate([metrics]) for name, metrics in baselines.items()
            if name != "training_median"}
        result.holdout_rows = int(scored["rows"])
        result.holdout_span = tuple(scored["span"])
        result.metrics = result.holdout_metrics
        result.baseline_metrics = result.holdout_baseline_metrics
        result.note = ("scored on the untouched window after every fold;"
                       " the model saw no row from it")
    elif fold_metrics:
        result.metrics = result.fold_metrics
        result.baseline_metrics = result.fold_baseline_metrics
        result.note = ("no untouched holdout had enough rows;"
                       " reporting the expanding-window folds")
    else:
        result.note = (split.note or
                       "no chronological fold had enough rows in this realm")
        return result
    result.status = "evaluated"
    if result.skill_vs_baseline is not None and result.skill_vs_baseline <= 0:
        result.note += "; the model did not beat the training median out of sample"
    return result


def train_realm(frame: pd.DataFrame, realm: str,
                cfg: Optional[RealmMLConfig] = None) -> RealmResult:
    """Fit the final realm model on every priced row of that realm."""
    cfg = cfg or RealmMLConfig()
    sub = _realm_frame(frame, realm)
    priced = sub[sub["y_net_unit_log"].notna()] if "y_net_unit_log" in sub.columns else sub.iloc[0:0]
    result = RealmResult(region=cfg.region, realm=realm, model_kind="price",
                         status="insufficient_data", n_rows=int(len(sub)),
                         n_price_rows=int(len(priced)))
    if len(priced) < cfg.min_price_rows:
        result.note = (f"{len(priced)} observed sales, need {cfg.min_price_rows};"
                       " the model is not trained and nothing is borrowed")
        return result
    if len(priced["realm"].astype(str).str.casefold().unique()) != 1:
        raise AssertionError("refusing to fit a model on more than one realm")
    numeric, price_feats, cats = _training_columns(sub)
    model = train_price_model(priced, None, price_feats, cats,
                              early_stopping=False, iterations=cfg.iterations,
                              seed=cfg.seed)
    if not model.trained:
        result.note = model.note or "the trainer returned an untrained model"
        return result
    cfg.ensure_dirs()
    path = cfg.models_dir / cfg.region / realm_slug(realm) / "price_model.cbm"
    model.save(path)
    result.status = "trained"
    result.model_path = str(path)
    result.metrics = dict(model.metrics)
    result.note = "fitted on this realm only"
    return result


def train_all(frame: pd.DataFrame,
              cfg: Optional[RealmMLConfig] = None,
              con=None) -> Dict[str, RealmResult]:
    """Evaluate and fit one model per realm, independently.

    Returns a mapping keyed by ``region:realm`` so two regions can never
    overwrite each other's model file.
    """
    cfg = cfg or RealmMLConfig()
    out: Dict[str, RealmResult] = {}
    for realm, sub in split_by_realm(frame).items():
        evaluation = evaluate_realm_price(frame, realm, cfg)
        trained = train_realm(frame, realm, cfg)
        trained.metrics = evaluation.metrics or trained.metrics
        trained.baseline_metrics = evaluation.baseline_metrics
        trained.folds = evaluation.folds
        trained.fold_metrics = evaluation.fold_metrics
        trained.fold_baseline_metrics = evaluation.fold_baseline_metrics
        trained.holdout_metrics = evaluation.holdout_metrics
        trained.holdout_baseline_metrics = evaluation.holdout_baseline_metrics
        trained.holdout_rows = evaluation.holdout_rows
        trained.holdout_span = evaluation.holdout_span
        trained.extra_holdout_baselines = evaluation.extra_holdout_baselines
        if evaluation.status != "evaluated" and trained.status == "trained":
            trained.note = (trained.note + "; " + evaluation.note).strip("; ")
        out[realm_key(cfg.region, realm)] = trained
        if con is not None:
            payload = {**trained.metrics,
                       **{f"baseline_{k}": v for k, v in
                          trained.baseline_metrics.items()},
                       "holdout_rows": trained.holdout_rows,
                       "holdout_start": trained.holdout_span[0],
                       "holdout_end": trained.holdout_span[1]}
            for name, values in trained.extra_holdout_baselines.items():
                payload.update({f"baseline_{name}_{k}": v for k, v in values.items()})
            payload.update({f"fold_{k}": v for k, v in trained.fold_metrics.items()})
            con.execute(
                "INSERT OR REPLACE INTO realm_model_run (run_id, created_at,"
                " region, realm, model_kind, status, n_rows, metrics_json,"
                " model_path, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (realm_key(cfg.region, realm), _utcnow(), cfg.region, realm,
                 "price", trained.status, trained.n_price_rows,
                 pd.Series(payload).to_json(), trained.model_path, trained.note))
            con.commit()
    return out


def model_path(cfg: RealmMLConfig, realm: str) -> Path:
    return cfg.models_dir / cfg.region / realm_slug(realm) / "price_model.cbm"


def load_realm_price_model(realm: str, cfg: Optional[RealmMLConfig] = None):
    """Load the final model of exactly one realm."""
    from woweconomy.ml.models import PriceModel
    cfg = cfg or RealmMLConfig()
    path = model_path(cfg, realm)
    if not path.exists():
        raise FileNotFoundError(
            f"no trained model for {cfg.region}:{realm}; run train_realm first")
    return PriceModel.load(path)


def predict_net_unit(frame: pd.DataFrame, realm: str,
                     cfg: Optional[RealmMLConfig] = None) -> np.ndarray:
    """Predict with the realm's own model, and refuse a cross-realm row.

    The check is the point: a prediction that silently used another
    realm's model would be wrong in a way the caller could not see.
    """
    cfg = cfg or RealmMLConfig()
    if frame.empty:
        return np.asarray([], dtype=float)
    if "realm" in frame.columns:
        realms = set(frame["realm"].astype(str).str.casefold())
        if realms != {realm.casefold()}:
            raise ValueError(
                f"refusing to predict {sorted(realms)} with the {realm} model")
    model = load_realm_price_model(realm, cfg)
    return np.asarray(model.predict_net_unit(frame), dtype=float)


def validate_shared_prior(frame: pd.DataFrame, realm: str,
                          cfg: Optional[RealmMLConfig] = None) -> Dict[str, object]:
    """Would borrowing strength from *other* realms help this one?

    Both models are fitted on the same development days: the realm model
    on the target realm only, the prior on the other realms only.  They
    are scored on the target realm's untouched test window.  The prior is
    accepted only when it improves the log-RMSE by ``min_prior_gain``;
    otherwise the answer is "no" and the realm keeps its own model.
    """
    cfg = cfg or RealmMLConfig()
    sub = _realm_frame(frame, realm)
    if sub.empty or "y_net_unit_log" not in sub.columns:
        return {"realm": realm, "status": "no_data"}
    split = make_split(sub, n_folds=cfg.n_folds,
                       test_fraction=cfg.test_fraction,
                       min_train_rows=cfg.min_price_rows,
                       min_valid_rows=cfg.min_valid_rows)
    test = split.test[split.test["y_net_unit_log"].notna()]
    if test.empty or split.test_span == (0, 0):
        return {"realm": realm, "status": "no_holdout", "note": split.note}
    dev_days = [d for d in split.days if d < split.test_span[0]]
    own_train = sub[sub["date"].isin(dev_days)
                    & sub["y_net_unit_log"].notna()]
    others = frame[~frame["realm"].astype(str).str.casefold().eq(realm.casefold())]
    prior_train = others[others["date"].isin(dev_days)
                         & others["y_net_unit_log"].notna()]
    if len(own_train) < cfg.min_price_rows or len(prior_train) < cfg.min_price_rows:
        return {"realm": realm, "status": "insufficient_data",
                "own_rows": int(len(own_train)),
                "prior_rows": int(len(prior_train))}
    _numeric, price_feats, cats = _training_columns(sub)
    own = train_price_model(own_train, None, price_feats, cats,
                            early_stopping=False, iterations=cfg.iterations,
                            seed=cfg.seed)
    prior = train_price_model(prior_train, None, price_feats, cats,
                              early_stopping=False, iterations=cfg.iterations,
                              seed=cfg.seed)
    if not own.trained or not prior.trained:
        return {"realm": realm, "status": "not_trainable"}
    y = test["y_net_unit_log"].to_numpy(dtype=float)
    own_metrics = price_metrics(y, np.asarray(own.predict_log(test), dtype=float))
    prior_metrics = price_metrics(y, np.asarray(prior.predict_log(test), dtype=float))
    own_rmse = own_metrics.get("rmse_log")
    prior_rmse = prior_metrics.get("rmse_log")
    gain = (1.0 - prior_rmse / own_rmse) if own_rmse and prior_rmse else None
    accepted = bool(gain is not None and gain >= cfg.min_prior_gain)
    return {
        "realm": realm,
        "status": "evaluated",
        "n_test": int(len(test)),
        "own_rmse_log": own_rmse,
        "prior_rmse_log": prior_rmse,
        "gain": gain,
        "accepted": accepted,
        "note": ("the prior is a validation result, not a served model: a"
                 " prediction is always anchored to this realm"),
    }
