"""The model that ships to the game.

The CatBoost ensemble is far too heavy to run in WoW, and it needs
features the game does not have.  What the game *does* have, through
TSM's own price sources, is the family of market references:

    dbmarket, dbrecent, dbhistorical,
    dbregionmarketavg, dbregionhistorical, dbregionsaleavg

So the engine distils what it learned into a form the game can compute:
a linear model on the **logarithms** of those references, with the
medians of the training sample shipped alongside so that a missing
reference can be filled in the same way on both sides.

The distillation is *checked*, not assumed: the linear model is scored on
the same walk-forward windows as the ensemble, and the report states
what it gives up.  A model that is 90 % as good and computable in Lua
beats one that is perfect and cannot run.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def _is_finite(value) -> bool:
    """Whether a value is a real number.

    A missing reference arrives from a data frame as ``NaN``, and ``NaN``
    is not ``<= 0`` -- so a naive check lets it through and poisons
    every arithmetic operation downstream.
    """
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False

#: the references the shipped model uses, in a fixed order.  The order
#: is part of the dataset's contract: the in-game side reads the
#: coefficients positionally.
REFERENCE_ORDER: Tuple[str, ...] = (
    "dbmarket",
    "dbrecent",
    "dbhistorical",
    "dbregionmarketavg",
    "dbregionhistorical",
    "dbregionsaleavg",
)

#: the column of the learning engine's dataset that carries each one
REFERENCE_COLUMNS: Dict[str, str] = {
    "dbmarket": "ref_local_market",
    "dbrecent": "ref_local_recent",
    "dbhistorical": "ref_local_historical",
    "dbregionmarketavg": "ref_region_market",
    "dbregionhistorical": "ref_region_historical",
    "dbregionsaleavg": "ref_region_sale_avg",
}

#: The references the shipped model actually uses.  The choice is
#: measured, not assumed: a realm's own references exist only for the
#: items *currently* in its auction house, so they are missing for 96 %
#: of the transactions the model was fitted on, while the regional
#: references exist for every item.  On the same rows the regional
#: variant reaches rmse_log 0.739 against 0.961 for the full set -- and
#: the full set cannot even be evaluated, because there is nothing left
#: to evaluate it on.
SHIPPED_REFERENCES: Tuple[str, ...] = (
    "dbregionmarketavg",
    "dbregionhistorical",
    "dbregionsaleavg",
)


@dataclass
class ShippedModel:
    """A linear model on the logarithms of the market references."""

    intercept: float = 0.0
    #: one coefficient per reference, in :data:`REFERENCE_ORDER`
    coefficients: List[float] = field(default_factory=list)
    #: the training median of each logarithm, used to fill a missing value
    medians: List[float] = field(default_factory=list)
    #: the references the model uses, in the order of the coefficients
    references: List[str] = field(default_factory=list)
    #: the typical error of the model, on the log scale
    residual_sigma: float = 0.0
    #: how many rows it was fitted on
    n_train: int = 0
    #: how many of the rows had every reference present
    n_complete: int = 0
    #: the measured share of items this model prices within 25 % / 50 % of
    #: the realised net price.  It ships because the game has to state a
    #: confidence for an item the dataset has no record of, and a made-up
    #: 50 % would be worse than no number at all.
    within_25: float = 0.0
    within_50: float = 0.0
    #: A split-conformal interval on the log scale, calibrated on the
    #: walk-forward errors and measured on a later block of them.  The
    #: game multiplies the price by ``exp(+/- interval_half_width)``.
    interval_level: float = 0.8
    interval_half_width: float = 0.0
    interval_coverage: float = 0.0
    interval_n: int = 0
    interval_method: str = ""

    def predict(self, references: Dict[str, Optional[float]]) -> Optional[float]:
        """The estimated price for one set of references, in copper.

        A missing reference is replaced by the **median of its
        logarithm**, which is what the model was fitted with -- so the
        replacement is already on the log scale and must not be
        transformed a second time.

        At least one reference has to be real.  An item with no market
        data at all gets no estimate: "the price of a typical item" is
        not an estimate of this item's price, it is a guess dressed up
        as one.
        """
        if not self.coefficients:
            return None
        names = self.references or list(REFERENCE_ORDER)
        if len(self.coefficients) != len(names):
            return None
        total = self.intercept
        present = 0
        for index, name in enumerate(names):
            value = references.get(name)
            # a missing reference arrives as None *or* as NaN, and NaN is
            # not <= 0, so it has to be caught explicitly
            if value is None or not _is_finite(value) or value <= 0:
                total += self.coefficients[index] * self.medians[index]
            else:
                present += 1
                total += self.coefficients[index] * math.log1p(value)
        if present == 0:
            return None
        if not math.isfinite(total) or total <= 0:
            return None
        return math.exp(total)

    def as_lua(self) -> List[str]:
        """The Lua body of the model."""
        return [
            f"intercept = {self.intercept:.6f}",
            "coefficients = { " + ", ".join(f"{c:.6f}" for c in self.coefficients) + " }",
            "medians = { " + ", ".join(f"{m:.6f}" for m in self.medians) + " }",
            "references = { " + ", ".join(f'"{r}"' for r in (self.references or REFERENCE_ORDER)) + " }",
            f"residualSigma = {self.residual_sigma:.6f}",
            f"within25 = {self.within_25:.4f}",
            f"within50 = {self.within_50:.4f}",
            f"intervalLevel = {self.interval_level:.4f}",
            f"intervalHalfWidth = {self.interval_half_width:.6f}",
            f"intervalCoverage = {self.interval_coverage:.4f}",
            f"intervalN = {self.interval_n}",
            f"intervalMethod = {chr(34)}{self.interval_method}{chr(34)}",
            f"nTrain = {self.n_train}",
            f"nComplete = {self.n_complete}",
        ]


def fit_shipped_model(frame, target: str = "y_net_unit_log",
                      references: Optional[Sequence[Tuple[str, str]]] = None) -> ShippedModel:
    """Fit the linear model on the learning engine's dataset.

    A missing reference is filled with the training median of that
    reference, and the medians are shipped so that the in-game side
    fills it the same way.

    ``references`` chooses which references the model uses.  The default
    is every reference TSM exposes; the regional-only variant is what
    the game can rely on for *every* item, because a realm's own
    references exist only for the items currently in its auction house.
    """
    model = ShippedModel()
    pairs = list(references) if references else [
        (name, REFERENCE_COLUMNS[name]) for name in SHIPPED_REFERENCES]
    columns = [column for _name, column in pairs]
    if frame.empty or target not in frame.columns:
        return model
    usable = frame[frame[target].notna()].copy()
    model.n_train = int(len(usable))
    model.references = [name for name, _column in pairs]
    if len(usable) < 30:
        return model

    # the medians of the logarithms, and the values behind them
    log_values: Dict[str, List[float]] = {}
    for name, column in pairs:
        if column not in usable.columns:
            return model
        values = usable[column]
        present = values[(values.notna()) & (values > 0)]
        if present.empty:
            return model
        log_values[name] = [math.log1p(float(v)) for v in present]
        model.medians.append(float(np.median(log_values[name])))

    model.n_complete = int(sum(
        1 for _, row in usable.iterrows()
        if all(row.get(column) is not None and row.get(column, 0) > 0
               for column in columns)))

    # the design matrix: intercept + one column per reference
    matrix = np.empty((len(usable), len(pairs) + 1), dtype=float)
    target_values = np.empty(len(usable), dtype=float)
    for i, (_, row) in enumerate(usable.iterrows()):
        matrix[i, 0] = 1.0
        for j, (name, column) in enumerate(pairs):
            value = row.get(column)
            if value is None or not (value > 0):
                matrix[i, j + 1] = model.medians[j]
            else:
                matrix[i, j + 1] = math.log1p(float(value))
        target_values[i] = float(row[target])

    coefficients, *_ = np.linalg.lstsq(matrix, target_values, rcond=None)
    model.intercept = float(coefficients[0])
    model.coefficients = [float(c) for c in coefficients[1:]]
    residuals = target_values - matrix @ coefficients
    model.residual_sigma = float(np.std(residuals))
    return model


def _log_error_records(model: ShippedModel, frame,
                       target: str = "y_net_unit_log") -> List[Tuple[float, float]]:
    """One ``(date, signed log error)`` per row the model can price."""
    if not model.coefficients:
        return []
    records: List[Tuple[float, float]] = []
    for _, row in frame.iterrows():
        try:
            true = float(row[target])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(true):
            continue
        references = {name: row.get(column)
                      for name, column in REFERENCE_COLUMNS.items()
                      if name in (model.references or REFERENCE_ORDER)}
        prediction = model.predict(references)
        if prediction is None:
            continue
        try:
            date = float(row.get("date"))
        except (TypeError, ValueError):
            date = 0.0
        if not math.isfinite(date):
            date = 0.0
        records.append((date, math.log(prediction) - true))
    return records


def _log_errors(model: ShippedModel, frame, target: str = "y_net_unit_log"
                ) -> List[float]:
    """One signed log error per row the model can price."""
    return [error for _date, error in _log_error_records(model, frame, target)]


def _metrics_from_errors(errors: Sequence[float]) -> Dict[str, float]:
    if not errors:
        return {"n": 0}
    return {
        "n": len(errors),
        "rmse_log": float(np.sqrt(np.mean(np.square(errors)))),
        "mae_log": float(np.mean(np.abs(errors))),
        "median_ratio": float(np.median(np.exp(errors))),
        "within_25pct": float(np.mean(np.abs(np.expm1(errors)) <= 0.25)),
        "within_50pct": float(np.mean(np.abs(np.expm1(errors)) <= 0.50)),
    }


def evaluate_shipped_model(model: ShippedModel, frame,
                           target: str = "y_net_unit_log") -> Dict[str, float]:
    """How good is the shipped model on the given rows?

    This is an **in-sample** number when ``frame`` is the frame the model
    was fitted on.  It is kept for the report because it is what the model
    can do with everything available; the confidence the game states comes
    from :func:`evaluate_shipped_model_walk_forward`, not from here.
    """
    return _metrics_from_errors(_log_errors(model, frame, target))


def evaluate_shipped_model_walk_forward(
        frame, references: Optional[Sequence[Tuple[str, str]]] = None,
        target: str = "y_net_unit_log", n_folds: int = 3,
        test_fraction: float = 0.25) -> Dict[str, float]:
    """Score the model on the future it has never seen.

    The shipped model is fitted in sample, so scoring it on the same rows
    overstates its accuracy -- and that overstated number used to travel
    to the game as the confidence for every item with no record.  This
    fits the model on each chronological training block, predicts the
    block that follows it, and pools the errors, so the share of items
    priced within 25 % / 50 % is measured the way the addon will use it.
    """
    from ..ml.intervals import calibrate_interval
    from ..ml.validate import make_split

    split = make_split(frame, n_folds=n_folds, test_fraction=test_fraction)
    records: List[Tuple[float, float]] = []
    for fold in split.folds:
        model = fit_shipped_model(fold.train, target=target, references=references)
        if model.coefficients:
            records.extend(_log_error_records(model, fold.valid, target))
    # and the untouched window that no fold trained or validated on
    if len(split.test) and split.folds:
        development = frame[frame["date"] < split.test_span[0]] \
            if "date" in frame.columns else frame
        model = fit_shipped_model(development, target=target, references=references)
        if model.coefficients:
            records.extend(_log_error_records(model, split.test, target))
    errors = [error for _date, error in records]
    metrics = _metrics_from_errors(errors)
    metrics["method"] = "walk-forward, pooled"
    # Calibrate the width on the earlier part of the pooled walk-forward
    # errors and measure it on the later part.  Using the same errors for
    # both would report the nominal level by construction; this is the
    # number the tooltip is allowed to call measured.
    if len(records) >= 40:
        records.sort(key=lambda pair: pair[0])
        cut = max(20, int(round(len(records) * 0.6)))
        calibration = [error for _date, error in records[:cut]]
        validation = [error for _date, error in records[cut:]]
        interval = calibrate_interval(calibration, validation, level=0.8)
        if interval is not None:
            metrics.update(interval.as_dict(prefix="interval_"))
    return metrics
