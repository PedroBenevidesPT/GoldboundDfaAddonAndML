"""Chronological validation.

Snapshots of the same item taken a few hours apart are not independent
observations: they share the item, the market, the day.  A random split
would put the same listing's neighbours on both sides of the fence and
report a score that means nothing.  Every split here is therefore made on
the clock.

The layout is the one the brief asks for:

1. **train** on an earlier window,
2. **validate** on a later window -- the early-stopping signal,
3. **test** on a still later window that no fold ever trains or validates
   on.

Folds expand: each one trains on everything up to its own start and
validates on the block that follows, so the model is always asked to
predict a window it has never seen, at a point in time it has never
reached.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Fold:
    """One chronological fold."""

    name: str
    train: pd.DataFrame
    valid: pd.DataFrame
    train_span: Tuple[int, int] = (0, 0)
    valid_span: Tuple[int, int] = (0, 0)


@dataclass
class Split:
    """The folds plus the untouched test window."""

    folds: List[Fold] = field(default_factory=list)
    test: pd.DataFrame = field(default_factory=pd.DataFrame)
    test_span: Tuple[int, int] = (0, 0)
    days: List[int] = field(default_factory=list)
    note: str = ""

    def describe(self) -> List[Dict[str, object]]:
        out = []
        for fold in self.folds:
            out.append({
                "fold": fold.name,
                "train_rows": len(fold.train),
                "valid_rows": len(fold.valid),
                "train_days": [int(fold.train_span[0]), int(fold.train_span[1])],
                "valid_days": [int(fold.valid_span[0]), int(fold.valid_span[1])],
            })
        out.append({
            "fold": "TEST (untouched)",
            "rows": len(self.test),
            "days": [int(self.test_span[0]), int(self.test_span[1])],
        })
        return out


def make_split(frame: pd.DataFrame,
               n_folds: int = 3,
               test_fraction: float = 0.25,
               min_train_rows: int = 40,
               min_valid_rows: int = 10) -> Split:
    """Cut the frame into expanding-window folds and a final test window."""
    split = Split()
    if frame.empty or "date" not in frame.columns:
        split.note = "empty frame"
        return split

    days = sorted(int(d) for d in frame["date"].unique())
    split.days = days
    if len(days) < 3:
        split.note = (f"only {len(days)} distinct days of history; "
                      "a chronological split is not possible")
        split.test = frame
        return split

    n_test_days = max(1, int(round(len(days) * test_fraction)))
    test_days = days[-n_test_days:]
    dev_days = days[:-n_test_days]
    split.test = frame[frame["date"].isin(test_days)].copy()
    split.test_span = (test_days[0], test_days[-1])

    usable = [d for d in dev_days if d not in test_days]
    if len(usable) < 2:
        split.note = "not enough development days for a fold"
        return split

    # expanding windows over the development days: fold *i* trains on
    # everything before its validation block and validates on the block
    # itself, so the model is always asked about a window it has never
    # seen.
    block = max(1, len(usable) // max(1, n_folds))
    for i in range(1, n_folds + 1):
        end = block * i
        if end > len(usable):
            break
        train_days = usable[:end - block]
        valid_days = usable[end - block:end]
        if not train_days or not valid_days:
            continue
        train = frame[frame["date"].isin(train_days)]
        valid = frame[frame["date"].isin(valid_days)]
        if len(train) < min_train_rows or len(valid) < min_valid_rows:
            continue
        split.folds.append(Fold(
            name=f"fold{i}",
            train=train.copy(), valid=valid.copy(),
            train_span=(min(train_days), max(train_days)),
            valid_span=(min(valid_days), max(valid_days))))
    if not split.folds:
        split.note = "no fold had enough rows"
    return split


def aggregate(metrics: Sequence[Dict[str, float]]) -> Dict[str, float]:
    """Mean and spread of a metric across the folds."""
    out: Dict[str, float] = {}
    if not metrics:
        return out
    keys = set()
    for m in metrics:
        keys.update(k for k, v in m.items() if isinstance(v, (int, float)))
    for key in sorted(keys):
        values = [m[key] for m in metrics
                  if key in m and isinstance(m.get(key), (int, float))
                  and not (isinstance(m.get(key), float) and math.isnan(m[key]))]
        if not values:
            continue
        out[f"{key}_mean"] = float(np.mean(values))
        out[f"{key}_std"] = float(np.std(values))
        if len(values) > 1:
            out[f"{key}_min"] = float(np.min(values))
            out[f"{key}_max"] = float(np.max(values))
    return out


def reference_baselines(frame: pd.DataFrame,
                         target: str,
                         candidates: Dict[str, str]) -> Dict[str, Dict[str, float]]:
    """How well does each raw reference predict the target on its own?

    This is the control the learned model has to beat, and it is also the
    measurement the brief asks for: how far each asking-price family sits
    from what buyers actually paid.

    The candidate columns hold the reference in **copper**; the target is
    a logarithm of a copper amount, so the reference is compared on the
    same scale before the logarithm is taken.
    """
    from .models import price_metrics

    out: Dict[str, Dict[str, float]] = {}
    mask = frame[target].notna()
    if not mask.any():
        return out
    y_true = frame.loc[mask, target].to_numpy(dtype=float)
    y_net = frame.loc[mask, "y_net_unit"].to_numpy(dtype=float) \
        if "y_net_unit" in frame.columns else None
    for name, column in candidates.items():
        if column not in frame.columns:
            continue
        sub = frame.loc[mask, column]
        ok = (sub.notna() & (sub > 0)).to_numpy()
        if ok.sum() < 5:
            out[name] = {"n": int(ok.sum()), "note": "too few rows"}
            continue
        values = sub[ok].to_numpy(dtype=float)
        pred = np.log(values)
        metrics = price_metrics(y_true[ok], pred, y_net[ok] if y_net is not None else None)
        metrics["n"] = int(ok.sum())
        # the ratio of the reference to what the buyer paid, in gold
        realised = np.exp(y_true[ok])
        ratios = values / np.maximum(realised, 1e-9)
        metrics["median_reference_over_realised"] = float(np.median(ratios))
        metrics["reference_over_realised_p25"] = float(np.percentile(ratios, 25))
        metrics["reference_over_realised_p75"] = float(np.percentile(ratios, 75))
        metrics["share_reference_above_realised"] = float(np.mean(ratios > 1.0))
        out[name] = metrics
    return out
