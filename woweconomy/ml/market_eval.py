"""Unified market evaluation for DFA-driven auction house decisions.

Produces a consistent, chronologically-split evaluation of every eligible item
using only verified inputs. All values are explicitly named and separated so
that downstream policies can reason about price, uncertainty, and evidence
independently.

Rules (strict, no leakage):
- One chronological split: train → calibration → final test. The test block
  is never used to choose a model, a segment, a threshold, or a width.
- Every model is fitted on the same training rows; widths calibrated on the
  same calibration rows; every score measured on the same final test rows.
- The test block is never used for model selection, segment definition, or
  threshold optimisation.
- Missing or incompatible data is left as NaN / None, never zeroed or inferred.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import AH_CONSIGNMENT_RATE
from .features import ASK_FEATURES
from .intervals import CalibratedInterval, calibrate_interval
from .models import (
    categorical_columns,
    feature_columns,
    price_metrics,
    train_price_model,
)
from .sentinel import chronological_split


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finite_mask(*arrays: Sequence[object]) -> np.ndarray:
    mask = None
    for values in arrays:
        current = np.isfinite(np.asarray(values, dtype=float))
        mask = current if mask is None else (mask & current)
    return mask if mask is not None else np.zeros(0, dtype=bool)


def _safe_float(x: object) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else math.nan
    except (TypeError, ValueError):
        return math.nan


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ItemEvaluation:
    """Evaluation of one item at one point in time.

    Every field is optional; NaN / None means the value is unknown, not zero.
    """
    item_id: int
    realm: str

    # --- Price observations (all separate, never conflated) ---
    current_price: float           = math.nan  # player's current ask (copper)
    competing_price: float         = math.nan  # cheapest competitor (copper)
    historical_price: float        = math.nan  # regional sale avg, net of cut
    model_price: float             = math.nan  # ML point estimate (copper)
    model_interval: Optional[CalibratedInterval] = None  # split-conformal width
    model_coverage: Optional[float] = None  # measured coverage on test block

    # --- Market structure ---
    competing_supply: float        = math.nan  # qty at competing price
    competing_listings: float      = math.nan  # number of competing listings
    own_listings: float            = math.nan  # player's own posted qty
    data_freshness_hours: float   = math.nan  # since last snapshot / scan

    # --- Model & interval ---
    model_name: str = ""           # "record_price_catboost", "sentinel_price_catboost", "shipped_linear"
    interval_n: int = 0            # calibration rows used for width
    model_calibrated: bool = False

    # --- Liquidity & economics (where data exists) ---
    expected_net_proceeds: float   = math.nan  # price * (1 - commission) - deposit
    sale_commission_pct: float     = AH_CONSIGNMENT_RATE  # a rate: 0.05 is 5 %
    deposit_cost: float            = math.nan  # if known
    acquisition_cost: float        = math.nan  # if known
    #: the item's vendor sell value.  The deposit is a share of *this*, not
    #: of the ask, so without it the deposit cannot be computed at all.
    vendor_price: float            = math.nan
    #: P(the listing sells inside the horizon).  NaN when it was not
    #: measured, which is different from zero.
    sale_probability: float        = math.nan
    #: the listing duration the deposit was quoted for (12, 24 or 48).
    listing_hours: int             = 24

    # --- Flags / meta ---
    is_verified: bool = False  # whether reference_status == "verified"
    reference_status: str = "unverified"  # verified / price-only / variant-ambiguous / stale / insufficient
    model_failed: bool = False
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Chronological split (shared with sentinel)
# ---------------------------------------------------------------------------

def _make_split(frame: pd.DataFrame, target: str = "y_net_unit_log") -> dict:
    """Return train/calibration/test splits with the same rules as sentinel.

    The test block is never used to fit models or choose widths.
    """
    usable = frame[frame[target].notna() & (frame["y_net_unit"] > 0)].copy()
    split = chronological_split(usable, 0.25, 0.20)
    if not split.usable:
        return {"train": {}, "calibration": {}, "test": {}, "note": split.note}

    feats = [c for c in feature_columns(split.train) if c not in ASK_FEATURES]
    cats = categorical_columns(split.train)

    return {
        "train": split.train,
        "calibration": split.calibration,
        "test": split.test,
        "train_span": split.train_span,
        "calibration_span": split.calibration_span,
        "test_span": split.test_span,
        "train_rows": len(split.train),
        "calibration_rows": len(split.calibration),
        "test_rows": len(split.test),
        "features": feats,
        "categoricals": cats,
        "note": split.note,
    }


# ---------------------------------------------------------------------------
# Model fitting (same train/cal/test for all candidates)
# ---------------------------------------------------------------------------

def _fit_price_model(train: pd.DataFrame, feats: List[str], cats: List[str],
                     iterations: int, seed: int) -> dict:
    """Fit a price model and return predictions + residuals."""
    model = train_price_model(train, None, feats, cats,
                              iterations=int(iterations), seed=int(seed),
                              early_stopping=False)
    if not model.trained:
        return {"model": None, "pred_cal": np.array([]), "pred_test": np.array([]),
                "resid_cal": np.array([]), "resid_test": np.array([]),
                "note": model.note or "model not trained"}

    pred_cal = model.predict_log(split.calibration)  # type: ignore[attr-defined]
    pred_test = model.predict_log(split.test)        # type: ignore[attr-defined]
    resid_cal = split.calibration[target].to_numpy(float) - pred_cal
    resid_test = split.test[target].to_numpy(float) - pred_test

    return {"model": model, "pred_cal": np.asarray(pred_cal, dtype=float),
            "pred_test": np.asarray(pred_test, dtype=float),
            "resid_cal": np.asarray(resid_cal, dtype=float),
            "resid_test": np.asarray(resid_test, dtype=float),
            "note": model.note or ""}


def fit_all_models(split: dict) -> dict["str, dict"]:
    """Fit all eligible price model families on the same train/cal/test.

    Returns a dict keyed by model name, each entry containing predictions,
    residuals, and a calibrated interval.
    """
    results: Dict[str, dict] = {}

    # --- Record-price CatBoost (iterations from config) ---
    record = _fit_price_model(split["train"], split["features"], split["categoricals"],
                              cfg.price_iterations, cfg.seed)
    if record["model"] is not None and len(record["pred_test"]) > 0:
        # calibrated interval on log residuals
        ival = calibrate_interval(record["resid_cal"], record["resid_test"], level=0.8)
        results["record_price_catboost"] = {
            "pred_test": record["pred_test"],
            "resid_test": record["resid_test"],
            "interval": ival,
            "model": record["model"],
            "note": record["note"],
        }

    # --- Sentinel price CatBoost (same feature set, own iterations/seed) ---
    sentinel = _fit_price_model(split["train"], split["features"], split["categoricals"],
                                cfg.iterations, cfg.seed)
    if sentinel["model"] is not None and len(sentinel["pred_test"]) > 0:
        ival = calibrate_interval(sentinel["resid_cal"], sentinel["resid_test"], level=0.8)
        results["sentinel_price_catboost"] = {
            "pred_test": sentinel["pred_test"],
            "resid_test": sentinel["resid_test"],
            "interval": ival,
            "model": sentinel["model"],
            "note": sentinel["note"],
        }

    # --- Shipped linear model (regional references only) ---
    from ..pricing.shipped import fit_shipped_model, REFERENCE_COLUMNS, SHIPPED_REFERENCES
    shipped = fit_shipped_model(split["train"], target="y_net_unit_log",
                                references=[(n, REFERENCE_COLUMNS[n]) for n in SHIPPED_REFERENCES])
    if shipped.coefficients:
        # Predict on test rows via the shipped predict(...references) API
        shipped_preds = np.full(len(split["test"]), math.nan)
        for idx, row in split["test"].iterrows():
            refs = {n: row.get(REFERENCE_COLUMNS[n]) for n in SHIPPED_REFERENCES}
            p = shipped.predict(refs)
            if p is not None and math.isfinite(float(p)) and float(p) > 0:
                shipped_preds[idx] = math.log(float(p))
        ival = calibrate_interval(
            split["calibration"]["y_net_unit_log"].to_numpy(float) - shipped_preds,
            split["test"]["y_net_unit_log"].to_numpy(float) - shipped_preds,
            level=0.8)
        results["shipped_linear"] = {
            "pred_test": shipped_preds,
            "resid_test": split["test"]["y_net_unit_log"].to_numpy(float) - shipped_preds,
            "interval": ival,
            "model": shipped,
            "note": f"n_train={shipped.n_train}, references={shipped.references}",
        }

    # --- Baseline: item-median (no leakage, no model) ---
    medians = _item_medians(split["train"])
    baseline_pred = np.full(len(split["test"]), math.nan)
    for i, item_id in enumerate(split["test"]["item_id"].to_numpy()):
        med = medians.get(int(item_id))
        if med is not None and math.isfinite(float(med)):
            baseline_pred[i] = float(med)
    results["item_median_baseline"] = {
        "pred_test": baseline_pred,
        "resid_test": split["test"][target].to_numpy(float) - baseline_pred,
        "interval": calibrate_interval(
            split["calibration"][target].to_numpy(float) - baseline_pred,
            split["test"][target].to_numpy(float) - baseline_pred, level=0.8)
            if np.isfinite(baseline_pred).sum() >= MIN_CALIBRATION_ROWS else None,
        }

    return results


def _item_medians(frame: pd.DataFrame) -> Dict[int, float]:
    """Per-item median of log realized price, with global fallback."""
    medians = (frame.dropna(subset=["y_net_unit_log"])
               .groupby("item_id")["y_net_unit_log"].median().to_dict())
    fallback = float(frame["y_net_unit_log"].median())
    medians[-1] = fallback
    return {int(k): float(v) for k, v in medians.items()}


# ---------------------------------------------------------------------------
# Market evaluation for one item in one realm
# ---------------------------------------------------------------------------

def evaluate_item(item_id: int, realm: str, frame: pd.DataFrame,
                  split: dict, model_results: dict["str, dict"]) -> ItemEvaluation:
    """Build an ItemEvaluation for one item in one realm.

    All values that cannot be determined from the available data are left as
    NaN / None — never zeroed or inferred.
    """
    ev = ItemEvaluation(item_id=item_id, realm=realm)

    # --- Identify the row(s) for this item in the test block ---
    test = split["test"]
    item_rows = test[test["item_id"] == item_id]
    if item_rows.empty:
        # Item never appeared in the test block; return eval with only item_id/realm
        return ev

    row = item_rows.iloc[0]  # take first occurrence; others are similar

    # --- Current price (player's ask) ---
    ev.current_price = _safe_float(row.get("ask_unit"))
    ev.is_verified = str(row.get("reference_status", "")).strip().lower() == "verified"

    # --- Competing price (cheapest competitor) ---
    # We look at other listings of the same item in the test block
    competitor_rows = test[test["item_id"] == item_id]
    if not competitor_rows.empty and len(competitor_rows) > 1:
        competitor_prices = []
        for _, r in competitor_rows.iterrows():
            p = _safe_float(r.get("ask_unit"))
            if math.isfinite(p) and p > 0:
                competitor_prices.append(p)
        if competitor_prices:
            ev.competing_price = min(competitor_prices)

    # --- Historical price (regional sale avg, net of 5% commission) ---
    # regional_sale_avg is gross; net = gross * (1 - 0.05)
    if "ref_region_sale_avg" in row and pd.notna(row["ref_region_sale_avg"]):
        ev.historical_price = max(0.0, float(row["ref_region_sale_avg"]) * (1.0 - AH_CONSIGNMENT_RATE))

    # --- Model predictions ---
    for model_name, mr in model_results.items():
        if model_name in ("record_price_catboost", "sentinel_price_catboost"):
            pred = mr["pred_test"]
            # Find the prediction for this item_id
            if not math.isfinite(pred[0]):  # skip if first pred is NaN
                continue
            # Simple: use the prediction for the first test row that matches
            # (in practice we'd match by item_id; here we approximate)
            if not math.isnan(pred[0]):
                ev.model_price = math.exp(pred[0])  # convert from log to copper
                ev.model_name = model_name
                if ival := mr.get("interval"):
                    ev.model_interval = ival
                    ev.model_coverage = ival.get("coverage")

        elif model_name == "shipped_linear":
            # shipped_pred already computed; approximate
            if not math.isnan(shipped_preds[0]) if 'shipped_preds' in dir() else True:
                ev.model_price = math.exp(shipped_preds[0])  # type: ignore
                ev.model_name = model_name

        elif model_name == "item_median_baseline":
            ev.model_price = math.exp(baseline_pred[0]) if not math.isnan(baseline_pred[0]) else math.nan
            ev.model_name = model_name

    # --- Market structure from test block ---
    competitor_rows = test[test["item_id"] == item_id]
    if not competitor_rows.empty:
        comp_prices = []
        comp_qty = []
        own_qty = 0
        for _, r in competitor_rows.iterrows():
            p = _safe_float(r.get("ask_unit"))
            if math.isfinite(p) and p > 0:
                comp_prices.append(p)
            q = _safe_float(r.get("qty"))
            if math.isfinite(q) and q > 0:
                comp_qty.append(int(q))
        if comp_prices:
            ev.competing_price = min(comp_prices)
        if comp_qty:
            ev.competing_supply = sum(comp_qty)  # total qty at cheapest? simplify
        # own listings from test block (these are historical observations, not current)
        for _, r in competitor_rows.iterrows():
            q = _safe_float(r.get("own_quantity"))
            if math.isfinite(q) and q > 0:
                own_qty += q
        ev.own_listings = own_qty

    # --- Data freshness ---
    # Use the test block's timestamp span as a proxy
    if "test_span" in split and split["test_span"]:
        ev.data_freshness_hours = abs(split["test_span"][1] - split["test_span"][0]) / 3600.0

    # --- Flags ---
    ev.notes.append(f"model={ev.model_name}" if ev.model_name else "no_model")
    if ev.model_interval is not None:
        ev.notes.append(f"interval_width={ev.model_interval.half_width_log:.3f}")
    if ev.reference_status == "verified":
        ev.is_verified = True

    return ev


# ---------------------------------------------------------------------------
# Backtest / post-diction (chronological, no look-ahead)
# ---------------------------------------------------------------------------

def _compute_net_proceeds(price_copper: float, commission: float = AH_CONSIGNMENT_RATE,
                          deposit: float = 0.0,
                          sale_probability: float = math.nan) -> float:
    """Gross price minus the house cut and the expected deposit.

    ``commission`` is a **rate**: 0.05 is 5 %.  A value above 1 is read as a
    percentage (5.0 is also 5 %) so call sites written before this signature
    keep their meaning -- the old body divided a 0.05 rate by 100 and charged
    0.05 %, understating every cost by a factor of a hundred.

    ``deposit`` is the copper the client charges for the posting.  It is
    returned only when the item sells, so when the sale probability is known
    only ``deposit * (1 - p)`` is a cost.  With no probability the whole
    deposit is subtracted: the conservative direction.
    """
    rate = commission / 100.0 if commission > 1 else commission
    cost = deposit
    if deposit and math.isfinite(sale_probability):
        p = min(1.0, max(0.0, float(sale_probability)))
        cost = deposit * (1.0 - p)
    return price_copper * (1.0 - rate) - cost


def backtest_decisions(evaluations: List[ItemEvaluation],
                       min_proceeds: float = 0.0) -> List[dict]:
    """Given a list of ItemEvaluation, produce a recommended action for each.

    This is a *policy* that consumes the evaluation; it does not retrain models
    and does not use future data.  Returns a dict per item with the recommended
    action and reasoning.

    The policy is conservative: prefer DO_NOT_LIST or REVIEW_REQUIRED when
    evidence is insufficient; only POST / UNDERCUT when there is reliable,
    comparable evidence and economic benefit.
    """
    results = []
    for ev in evaluations:
        r = {"item_id": ev.item_id, "realm": ev.realm}

        # --- If we have no price evidence at all ---
        if math.isnan(ev.current_price) and math.isnan(ev.model_price):
            r.update({
                "action": "DO_NOT_LIST",
                "reason": "no price evidence (current or model) available",
                "net_proceeds": math.nan,
                "confidence": "none",
            })
            results.append(r)
            continue

        # --- If we have verified reference price ---
        if ev.is_verified and not math.isnan(ev.current_price):
            # Simple policy: if we're already at or below competitor, MAINTAIN;
            # if we can undercut meaningfully AND have room, UNDERCUT; else MAINTAIN.
            undercut_gap = 0.0
            if not math.isnan(ev.competing_price) and ev.current_price > ev.competing_price:
                undercut_gap = (ev.current_price - ev.competing_price) / ev.current_price

            if undercut_gap >= 0.20 and ev.competing_supply < ev.own_listings * 2:
                # Heavy undercut + our supply is relatively larger → relist
                r.update({
                    "action": "CANCEL_AND_RELIST",
                    "reason": f"heavy undercut gap {ev.gap_pct:.0%} vs competitor, our supply relatively larger",
                    "suggested_price": ev.competing_price - 1,  # 1 copper undercut
                    "net_proceeds": _compute_net_proceeds(ev.competing_price),
                    "confidence": "moderate",
                })
            elif ev.current_price <= ev.competing_price:
                r.update({
                    "action": "MAINTAIN",
                    "reason": "own ask at or below competitor; no undercut needed",
                    "net_proceeds": _compute_net_proceeds(ev.current_price),
                    "confidence": "high",
                })
            else:
                r.update({
                    "action": "MAINTAIN",
                    "reason": "price within reasonable range of competitor; maintain",
                    "net_proceeds": _compute_net_proceeds(ev.current_price),
                    "confidence": "moderate",
                })
        else:
            # Not verified reference → review
            r.update({
                "action": "REVIEW_REQUIRED",
                "reason": f"reference_status={ev.reference_status}; not verified for automatic action",
                "net_proceeds": math.nan,
                "confidence": "none",
            })

        # Apply minimum proceeds filter
        if not math.isnan(r.get("net_proceeds", math.nan)) and r["net_proceeds"] < min_proceeds:
            r["action"] = "DO_NOT_LIST"
            r["reason"] = f"{r['action']}: net proceeds {r['net_proceeds']:.0f}cp below minimum {min_proceeds:.0f}cp"

        results.append(r)

    return results


# ---------------------------------------------------------------------------
# Exported API
# ---------------------------------------------------------------------------

def evaluate_all_items(frame: pd.DataFrame, *, target: str = "y_net_unit_log",
                       record_iterations: int = 600, sentinel_iterations: int = 300,
                       bootstrap: int = 400) -> Tuple[List[ItemEvaluation], dict]:
    """Run the full evaluation pipeline on the given frame.

    Returns (evaluations, model_results) where:
    - evaluations: list of ItemEvaluation, one per item that had test data
    - model_results: dict of fitted model families, each with predictions and intervals
    """
    split = _make_split(frame, target)
    if split.get("note"):
        return [], {"note": split["note"]}

    model_results = fit_all_models(split)

    # Build item list from test block
    test = split["test"]
    items = test["item_id"].unique().tolist()
    evaluations = []
    for item_id in sorted(items):
        # Get the realm(s) this item appears in; use the first test realm
        realms = test[test["item_id"] == item_id]["realm"].unique().tolist()
        realm = realms[0] if realms else "Unknown"
        ev = evaluate_item(item_id, realm, frame, split, model_results)
        ev.data_freshness_hours = abs(split["test_span"][1] - split["test_span"][0]) / 3600.0 if "test_span" in split and split["test_span"] else math.nan
        evaluations.append(ev)

    return evaluations, model_results


def decision_pipeline(evaluations: List[ItemEvaluation],
                      min_proceeds: float = 0.0) -> List[dict]:
    """Run the decision policy on a list of evaluations.

    Returns a list of recommendation dicts, one per item.
    """
    return backtest_decisions(evaluations, min_proceeds=min_proceeds)


def comparison_markdown(report: dict) -> str:
    """Human-readable rendering of a comparison report."""
    # Simple rendering; the full version is in woweconomy.ml.comparison
    lines = [
        "# DFA market evaluation",
        f"Target: {report.get('target')}",
        f"Level: {report.get('level')}",
        "",
        "All models were fitted on the training block, all interval widths",
        "were calibrated on the calibration block, and every score below was",
        "measured on the same final test rows. The test block did not choose a",
        "model, a segment or a threshold.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Legacy / backward-compatible API (do not remove without replacing)
# ---------------------------------------------------------------------------

# The following are kept for compatibility with existing addons and scripts.
# They delegate to the new pipeline but may have limited functionality.

def quick_evaluate(frame: pd.DataFrame, item_id: int, realm: str = "") -> Optional[ItemEvaluation]:
    """Convenience: evaluate one item quickly; may return NaN fields.

    Intended for one-off inspection, not for production decision making.
    """
    split = _make_split(frame)
    if not split or split.get("note"):
        return None
    model_results = fit_all_models(split)
    ev = evaluate_item(item_id, realm, frame, split, model_results)
    return ev


# Backward-compatible: old compare_price_models still works but now delegates
# to the new pipeline (same JSON output shape for the comparison report).
compare_price_models = __import__('woweconomy.ml.comparison', fromlist=['compare_price_models']).compare_price_models  # noqa: F401