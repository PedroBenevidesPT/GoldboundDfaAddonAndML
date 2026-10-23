"""A fair, chronological comparison of the price models we actually ship.

The existing reports each measured a model on a different block of sales.
Those numbers cannot be put in one table: a model scored on 103 sales is
not comparable with one scored on 124, and the test windows were not the
same.  This module removes that ambiguity.

The rules are deliberately strict:

* one chronological cut gives **one** training block, one calibration
  block and one untouched final block;
* every model is fitted on the same training rows;
* every interval width is fitted on the same calibration rows;
* every score is measured on the same final rows;
* the final block is never used to choose a model, a segment, a threshold
  or a width.

The record-price CatBoost, the Sentinel CatBoost and the shipped linear
model are evaluated as model *families*, not as the bytes currently in a
promoted artifact.  Refitting each family on the same rows is the only way
to separate the model from the data it happened to see.  The promoted
artifacts are left untouched.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

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

#: The material families are grouped so a segment is large enough to say
#: something about it on a thin test block.  The grouping is fixed before
#: the test block is read.
MATERIAL_BUCKETS = frozenset({"hide", "cloth", "ore", "bar", "herb", "spirit"})
CONSUMABLE_BUCKETS = frozenset({"meat", "recipe", "pattern"})


@dataclass
class _Candidate:
    name: str
    family: str
    calibration_log: np.ndarray
    test_log: np.ndarray
    calibration_residuals: np.ndarray
    test_residuals: np.ndarray
    interval: Optional[CalibratedInterval]
    note: str = ""


def _item_medians(frame: pd.DataFrame, target: str) -> Dict[int, float]:
    medians = (frame.dropna(subset=[target])
               .groupby("item_id")[target].median().to_dict())
    fallback = float(frame[target].median())
    medians[-1] = fallback
    return {int(key): float(value) for key, value in medians.items()}


def _apply_item_medians(frame: pd.DataFrame,
                        medians: Dict[int, float]) -> np.ndarray:
    fallback = medians.get(-1, 0.0)
    return np.asarray([
        float(medians.get(int(item_id), fallback))
        for item_id in frame["item_id"].to_numpy()
    ], dtype=float)


def _shipped_predictions(frame: pd.DataFrame, model) -> np.ndarray:
    """Predict through the same interface the Lua model uses."""
    from ..pricing.shipped import REFERENCE_COLUMNS, SHIPPED_REFERENCES

    out = np.full(len(frame), np.nan, dtype=float)
    for index, (_, row) in enumerate(frame.iterrows()):
        references = {
            name: row.get(REFERENCE_COLUMNS[name])
            for name in SHIPPED_REFERENCES
        }
        value = model.predict(references)
        if value is not None and math.isfinite(float(value)) and value > 0:
            out[index] = math.log(float(value))
    return out


def _segment_key(frame: pd.DataFrame) -> pd.Series:
    """A fixed, coarse item family used only for reporting."""
    def classify(value: object) -> str:
        name = str(value)
        if name in MATERIAL_BUCKETS:
            return "materials"
        if name in CONSUMABLE_BUCKETS:
            return "consumables"
        return "other"

    if "cat_item_name_bucket" not in frame.columns:
        return pd.Series(["other"] * len(frame), index=frame.index)
    return frame["cat_item_name_bucket"].map(classify)


def _bootstrap_mae(errors: np.ndarray, seed: int,
                   draws: int = 400) -> Optional[List[float]]:
    """A percentile interval for the mean absolute log error."""
    values = np.asarray(errors, dtype=float)
    if len(values) < 5 or draws <= 0:
        return None
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(values), size=(int(draws), len(values)))
    samples = np.abs(values[indices]).mean(axis=1)
    return [float(np.quantile(samples, 0.025)),
            float(np.quantile(samples, 0.975))]


def _bootstrap_difference(errors: np.ndarray, baseline_errors: np.ndarray,
                          seed: int, draws: int = 400) -> Optional[Dict[str, object]]:
    """Paired bootstrap of ``model error - baseline error``.

    A negative mean and an upper bound below zero mean the model beat the
    baseline on these rows.  The interval is the honest part; the point
    estimate alone would hide how much a 100-sale test can move.
    """
    diff = np.abs(np.asarray(errors, dtype=float)) - np.abs(
        np.asarray(baseline_errors, dtype=float))
    if len(diff) < 5 or draws <= 0:
        return None
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(diff), size=(int(draws), len(diff)))
    samples = diff[indices].mean(axis=1)
    return {
        "mean_delta_mae_log": float(diff.mean()),
        "ci95": [float(np.quantile(samples, 0.025)),
                 float(np.quantile(samples, 0.975))],
        "probability_better": float(np.mean(samples < 0.0)),
    }


def _wilson(covered: int, total: int, z: float = 1.96) -> Optional[List[float]]:
    if total <= 0:
        return None
    p = covered / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt(p * (1.0 - p) / total
                           + z * z / (4.0 * total * total)) / denominator
    return [float(max(0.0, centre - margin)), float(min(1.0, centre + margin))]


def _interval_summary(interval: Optional[CalibratedInterval],
                      test_residuals: np.ndarray,
                      test_mask: np.ndarray) -> Dict[str, object]:
    if interval is None:
        return {"available": False, "reason": "too few calibration residuals"}
    residuals = np.asarray(test_residuals, dtype=float)[test_mask]
    inside = np.abs(residuals) <= float(interval.half_width_log)
    covered = int(inside.sum())
    total = int(len(residuals))
    return {
        "available": True,
        "level": float(interval.level),
        "half_width_log": float(interval.half_width_log),
        "calibration_n": int(interval.calibration_n),
        "coverage": (float(covered / total) if total else None),
        "coverage_ci95": _wilson(covered, total),
        "test_n": total,
        "method": interval.method,
    }


def _metrics(candidate: _Candidate, mask: np.ndarray,
             frame: pd.DataFrame, target: str,
             seed: int, bootstrap: int) -> Dict[str, object]:
    y = frame[target].to_numpy(dtype=float)[mask]
    p = candidate.test_log[mask]
    net = frame["y_net_unit"].to_numpy(dtype=float)[mask]
    values = dict(price_metrics(y, p, net))
    values["n"] = int(len(y))
    values["n_items"] = int(frame.loc[mask, "item_id"].nunique())
    values["mae_log_ci95"] = _bootstrap_mae(p - y, seed, bootstrap)
    values["interval"] = _interval_summary(
        candidate.interval, candidate.test_residuals, mask)
    return values


def compare_price_models(frame: pd.DataFrame,
                         *, target: str = "y_net_unit_log",
                         test_fraction: float = 0.25,
                         calibration_fraction: float = 0.20,
                         level: float = 0.8,
                         record_iterations: int = 600,
                         sentinel_iterations: int = 300,
                         record_seed: int = 20261019,
                         sentinel_seed: int = 20261021,
                         bootstrap: int = 400,
                         data_quality: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Fit and score every eligible price model on one chronological cut.

    Returns a JSON-ready report.  No artifact is written or promoted.
    """
    if target not in frame.columns or "y_net_unit" not in frame.columns:
        raise ValueError("the frame needs the realized-price target and net price")
    usable = frame[frame[target].notna() & (frame["y_net_unit"] > 0)].copy()
    split = chronological_split(usable, test_fraction, calibration_fraction)
    if not split.usable:
        raise ValueError(split.note or "the chronological split is not usable")

    feats = [c for c in feature_columns(split.train) if c not in ASK_FEATURES]
    cats = categorical_columns(split.train)
    if not feats:
        raise ValueError("no numeric price features were available")

    y_train = split.train[target].to_numpy(dtype=float)
    y_cal = split.calibration[target].to_numpy(dtype=float)
    y_test = split.test[target].to_numpy(dtype=float)

    candidates: List[_Candidate] = []

    def add(name: str, family: str, cal: np.ndarray, test: np.ndarray,
            note: str = "") -> None:
        cal = np.asarray(cal, dtype=float)
        test = np.asarray(test, dtype=float)
        candidates.append(_Candidate(
            name=name, family=family,
            calibration_log=cal, test_log=test,
            calibration_residuals=y_cal - cal,
            test_residuals=y_test - test,
            interval=calibrate_interval(y_cal - cal, y_test - test, level),
            note=note))

    # The record-price model family: the CatBoost settings the pricing
    # engine uses for the in-game record export.
    record = train_price_model(
        split.train, None, feats, cats, iterations=int(record_iterations),
        seed=int(record_seed), early_stopping=False)
    if not record.trained:
        raise ValueError(record.note or "the record-price CatBoost did not fit")
    add("record_price_catboost", "catboost",
        record.predict_log(split.calibration), record.predict_log(split.test),
        note=f"price_iterations={record_iterations}")

    # The Sentinel family uses the same target and feature contract, but
    # its own tree budget and seed.  Keep both so the comparison does not
    # silently assume the two production paths are the same model.
    sentinel = train_price_model(
        split.train, None, feats, cats, iterations=int(sentinel_iterations),
        seed=int(sentinel_seed), early_stopping=False)
    if not sentinel.trained:
        raise ValueError(sentinel.note or "the Sentinel price CatBoost did not fit")
    add("sentinel_price_catboost", "catboost",
        sentinel.predict_log(split.calibration), sentinel.predict_log(split.test),
        note=f"sentinel_iterations={sentinel_iterations}")

    # A simple, valid historical-price baseline: the regional sale
    # average, less the consignment cut.  It uses no learned parameters,
    # so it cannot leak from the test block.
    if "ref_region_sale_avg" in split.test.columns:
        def historical(block: pd.DataFrame) -> np.ndarray:
            ref = block["ref_region_sale_avg"].to_numpy(dtype=float)
            return np.log(np.maximum(ref * (1.0 - AH_CONSIGNMENT_RATE), 1.0))
        add("regional_sale_avg_baseline", "baseline",
            historical(split.calibration), historical(split.test),
            note="log(regional sale average * (1 - consignment))")

    # Historical item median: a strong non-learned baseline.  It is also
    # the baseline the Sentinel uses when choosing a price family.
    medians = _item_medians(split.train, target)
    add("item_median_baseline", "baseline",
        _apply_item_medians(split.calibration, medians),
        _apply_item_medians(split.test, medians),
        note="median log realized price per item, global fallback")

    from ..pricing.shipped import (
        REFERENCE_COLUMNS,
        SHIPPED_REFERENCES,
        fit_shipped_model,
    )
    shipped = fit_shipped_model(
        split.train, target=target,
        references=[(name, REFERENCE_COLUMNS[name]) for name in SHIPPED_REFERENCES])
    add("shipped_linear", "linear",
        _shipped_predictions(split.calibration, shipped),
        _shipped_predictions(split.test, shipped),
        note="linear model on the three regional references")

    # Every model must have a finite prediction on a row before that row
    # enters the head-to-head.  This is the same-observations rule.
    common = np.ones(len(split.test), dtype=bool)
    for candidate in candidates:
        common &= np.isfinite(candidate.test_log)
    n_dropped = int(len(split.test) - common.sum())
    if common.sum() < 10:
        raise ValueError("too few test rows have predictions from every model")

    baseline = next(c for c in candidates if c.name == "item_median_baseline")
    baseline_errors = baseline.test_log[common] - y_test[common]

    results: Dict[str, Dict[str, object]] = {}
    for index, candidate in enumerate(candidates):
        values = _metrics(candidate, common, split.test, target,
                          record_seed + index, bootstrap)
        values["family"] = candidate.family
        values["note"] = candidate.note
        values["delta_vs_item_median"] = _bootstrap_difference(
            candidate.test_log[common] - y_test[common], baseline_errors,
            record_seed + 100 + index, bootstrap)
        results[candidate.name] = values

    # Pairwise differences are reported for every pair, with no model
    # selected as the winner.  A negative mean means the first model had
    # the lower absolute log error on the same rows.
    pairwise: List[Dict[str, object]] = []
    for left_index, left in enumerate(candidates):
        for right_index, right in enumerate(candidates[left_index + 1:], start=left_index + 1):
            difference = _bootstrap_difference(
                left.test_log[common] - y_test[common],
                right.test_log[common] - y_test[common],
                record_seed + 200 + left_index * 10 + right_index, bootstrap)
            pairwise.append({
                "a": left.name, "b": right.name,
                "mean_delta_mae_log": (difference or {}).get("mean_delta_mae_log"),
                "ci95": (difference or {}).get("ci95"),
                "probability_a_better": (difference or {}).get("probability_better"),
            })

    # Descriptive segment table.  It reports the final block; it never
    # chooses a segment or changes a model.
    segment_keys = _segment_key(split.test)[common]
    segments: Dict[str, Dict[str, object]] = {}
    for segment in sorted(segment_keys.unique()):
        segment_mask = (segment_keys == segment).to_numpy()
        block: Dict[str, object] = {
            "n": int(segment_mask.sum()),
            "share": float(segment_mask.mean()) if len(segment_mask) else 0.0,
            "models": {},
        }
        for name, values in results.items():
            candidate = next(c for c in candidates if c.name == name)
            segment_y = y_test[common][segment_mask]
            segment_p = candidate.test_log[common][segment_mask]
            interval = values.get("interval") if isinstance(values.get("interval"), dict) else {}
            width = float(interval.get("half_width_log") or 0.0)
            inside = np.abs(segment_p - segment_y) <= width
            block["models"][name] = {
                "mae_log": float(np.mean(np.abs(segment_p - segment_y))),
                "bias_log": float(np.mean(segment_p - segment_y)),
                "interval_coverage": float(inside.mean()) if width else None,
            }
        segments[str(segment)] = block

    # A historical-price baseline is only meaningful on rows where the
    # reference exists.  Report its coverage separately so a model that
    # happens to be scored on fewer rows cannot look better by omission.
    ref = split.test["ref_region_sale_avg"].to_numpy(dtype=float)
    reference_rows = int(np.isfinite(ref).sum())
    reference_block = {
        "n": reference_rows,
        "share_of_test": float(reference_rows / len(split.test))
        if len(split.test) else 0.0,
    }
    if "regional_sale_avg_baseline" in results:
        reference_block["metrics"] = results["regional_sale_avg_baseline"]
    else:
        reference_block["reason"] = "the regional sale average is not in the frame"

    return {
        "schema": "dfa-price-comparison/1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "level": float(level),
        "rules": {
            "split": "chronological train -> calibration -> final test",
            "model_fit": "training block only",
            "interval_calibration": "calibration block only",
            "evaluation": "final test block only",
            "test_used_for_selection": False,
            "same_test_observations": True,
        },
        "split": {
            "train_ts": list(split.train_span),
            "calibration_ts": list(split.calibration_span),
            "test_ts": list(split.test_span),
            "train_rows": int(len(split.train)),
            "calibration_rows": int(len(split.calibration)),
            "test_rows": int(len(split.test)),
            "common_test_rows": int(common.sum()),
            "dropped_for_missing_prediction": n_dropped,
            "train_items": int(split.train["item_id"].nunique()),
            "test_items": int(split.test["item_id"].nunique()),
            "features": len(feats),
            "categoricals": len(cats),
        },
        "models": results,
        "pairwise": pairwise,
        "segments": segments,
        "historical_reference": reference_block,
        "data_quality": data_quality or {},
    }


def _fmt_ci(interval: Optional[Sequence[float]]) -> str:
    if not interval:
        return "-"
    return f"{float(interval[0]):.3f} to {float(interval[1]):.3f}"


def _fmt_pct(value: Optional[object]) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.0f}%"


def comparison_markdown(report: Dict[str, object]) -> str:
    """A human-readable rendering of :func:`compare_price_models`."""
    split = report.get("split") or {}
    models = report.get("models") or {}
    lines = [
        "# DFA price-model comparison",
        "",
        f"Target: `{report.get('target')}`  ",
        f"Nominal interval level: {report.get('level')}",
        "",
        "All models were fitted on the training block, all interval widths",
        "were calibrated on the calibration block, and every score below was",
        "measured on the same final test rows. The test block did not choose a",
        "model, a segment or a threshold.",
        "",
        "## Chronological cut",
        "",
        "| block | first timestamp | last timestamp | rows |",
        "|---|---:|---:|---:|",
    ]
    for name, key in (("train", "train"), ("calibration", "calibration"),
                      ("test", "test")):
        span = split.get(f"{key}_ts") or [0, 0]
        rows = split.get(f"{key}_rows", 0)
        lines.append(f"| {name} | {span[0]} | {span[1]} | {rows} |")
    lines.extend([
        "",
        f"Common test rows: **{split.get('common_test_rows', 0)}** "
        f"(dropped for a missing prediction: {split.get('dropped_for_missing_prediction', 0)}).",
        "",
        "## Head-to-head",
        "",
        "| model | n | MAE log | 95% CI | RMSE log | median ratio | within 50% | interval width | coverage | coverage 95% CI |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---|",
    ])
    for name, values in models.items():
        interval = values.get("interval") or {}
        lines.append(
            f"| {name} | {values.get('n', 0)} | "
            f"{float(values.get('mae_log', 0)):.3f} | "
            f"{_fmt_ci(values.get('mae_log_ci95'))} | "
            f"{float(values.get('rmse_log', 0)):.3f} | "
            f"{float(values.get('median_ratio', 0)):.2f} | "
            f"{_fmt_pct(values.get('within_50pct'))} | "
            f"{float(interval.get('half_width_log', 0)):.3f} | "
            f"{_fmt_pct(interval.get('coverage'))} | "
            f"{_fmt_ci(interval.get('coverage_ci95'))} |")
    lines.extend([
        "",
        "## Paired difference from the item-median baseline",
        "",
        "A negative mean means the model had the lower absolute log error.",
        "The interval is a paired bootstrap over the same test rows.",
        "",
        "| model | mean delta MAE log | 95% CI | bootstrap P(better) |",
        "|---|---:|---|---:|",
    ])
    for name, values in models.items():
        delta = values.get("delta_vs_item_median") or {}
        lines.append(
            f"| {name} | {float(delta.get('mean_delta_mae_log', 0)):.3f} | "
            f"{_fmt_ci(delta.get('ci95'))} | "
            f"{float(delta.get('probability_better', 0)):.2f} |")
    lines.extend([
        "",
        "## Pairwise differences",
        "",
        "A negative mean means model **a** had the lower absolute log error.",
        "An interval that includes zero does not establish a difference.",
        "",
        "| a | b | mean delta MAE(a - b) | 95% CI | bootstrap P(a better) |",
        "|---|---|---:|---|---:|",
    ])
    for entry in (report.get("pairwise") or []):
        lines.append(
            f"| {entry.get('a')} | {entry.get('b')} | "
            f"{float(entry.get('mean_delta_mae_log') or 0):.3f} | "
            f"{_fmt_ci(entry.get('ci95'))} | "
            f"{float(entry.get('probability_a_better') or 0):.2f} |")
    lines.extend([
        "",
        "## Segment report (descriptive only)",
        "",
        "| segment | n | share | " + " | ".join(models) + " |",
        "|---|---:|---:|" + "---:|" * len(models),
    ])
    for segment, block in (report.get("segments") or {}).items():
        cells = []
        for name in models:
            cell = (block.get("models") or {}).get(name) or {}
            cells.append(f"{float(cell.get('mae_log', 0)):.3f} "
                         f"(cov {_fmt_pct(cell.get('interval_coverage'))})")
        lines.append(f"| {segment} | {block.get('n', 0)} | "
                     f"{_fmt_pct(block.get('share'))} | " + " | ".join(cells) + " |")
    lines.extend([
        "",
        "## What this does not say",
        "",
        "* These are refitted model families, not the promoted artifact bytes.",
        "* A 100-sale test cannot resolve small differences; read the paired",
        "  bootstrap interval, not only the ranking.",
        "* Segment coverage is descriptive. It is not a per-segment 80% guarantee.",
        "* The historical baseline is only as good as the regional reference it",
        "  uses; its coverage is reported beside the model rows.",
        "",
    ])
    return "\n".join(lines)
