"""Calibrated prediction intervals, with the coverage measured.

A point estimate that hides its error is worse than an interval that
states it.  The intervals here are **split-conformal** on the log scale:
the width is the empirical quantile of the absolute residuals on a
calibration block, and the coverage is then measured on a block the
calibration never saw.  That gives a number the tooltip can print:

    "80% range: 0.66g - 3.68g (78% of 89 sales)"

The level is a *nominal* promise; the coverage is what actually happened.
The two are kept apart on purpose.  If the coverage is far below the
level, the caller can say so instead of calling the interval calibrated.

The residual is a logarithm, so an interval of +/- ``half_width`` is a
*multiplicative* range in copper: ``price * exp(-half_width)`` to
``price * exp(half_width)``.  Prices span four orders of magnitude; an
additive interval would be meaningless at both ends.
"""
from __future__ import annotations

import math

import numpy as np
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Optional, Sequence

#: Below this many calibration rows, an empirical quantile is noise
#: dressed as a guarantee, so the caller is told there is no interval.
MIN_CALIBRATION_ROWS = 20
MIN_SEGMENT_ROWS = 40


@dataclass
class SegmentCalibration:
    """One segment's width, support and measured coverage."""
    half_width_log: float = 0.0
    calibration_n: int = 0
    coverage: Optional[float] = None
    test_n: int = 0
    supported: bool = False
    fallback: bool = True
    note: str = ""


@dataclass
class SegmentedInterval:
    """A global interval plus conditional widths for supported segments."""
    level: float = 0.8
    feature: str = ""
    threshold: float = 0.0
    min_rows: int = MIN_SEGMENT_ROWS
    method: str = "segmented-split-conformal"
    global_interval: Optional[CalibratedInterval] = None
    segments: Dict[str, SegmentCalibration] = field(default_factory=dict)
    fallback_n: int = 0
    fallback_share: float = 0.0
    selected_coverage: Optional[float] = None
    selected_test_n: int = 0
    note: str = ""

    def width_for(self, value: object) -> float:
        """The width to use for one row."""
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            number = float("nan")
        if not math.isfinite(number) or self.global_interval is None:
            return float("nan")
        if number <= self.threshold:
            segment = self.segments.get("low")
        else:
            segment = self.segments.get("high")
        if segment is not None and segment.supported and segment.half_width_log > 0:
            return float(segment.half_width_log)
        return float(self.global_interval.half_width_log)

    def as_dict(self) -> Dict[str, object]:
        return {
            "level": float(self.level),
            "feature": self.feature,
            "threshold": float(self.threshold),
            "min_rows": int(self.min_rows),
            "method": self.method,
            "global": (self.global_interval.as_dict(prefix="")
                       if self.global_interval is not None else None),
            "segments": {
                name: asdict(segment) for name, segment in self.segments.items()
            },
            "fallback_n": int(self.fallback_n),
            "fallback_share": float(self.fallback_share),
            "selected_coverage": self.selected_coverage,
            "selected_test_n": int(self.selected_test_n),
            "note": self.note,
        }


def calibrate_segmented_interval(
        calibration_residuals: Sequence[object],
        calibration_values: Sequence[object],
        test_residuals: Optional[Sequence[object]] = None,
        test_values: Optional[Sequence[object]] = None,
        *, feature: str = "ref_dispersion",
        level: float = 0.8,
        min_rows: int = MIN_SEGMENT_ROWS) -> Optional[SegmentedInterval]:
    """Calibrate one global interval plus supported conditional segments.

    The threshold is the median of the calibration values; it is chosen from
    the calibration block alone.  A segment is used only when it has at
    least ``min_rows`` calibration residuals and a finite conformal width.
    Otherwise every row in that segment falls back to the global width,
    so the result is never narrower than the global interval's evidence
    allows.
    """
    cal_resid = np.asarray(_finite(calibration_residuals), dtype=float)
    cal_values = np.asarray(calibration_values, dtype=float)
    if len(cal_resid) != len(cal_values):
        return None
    global_interval = calibrate_interval(
        calibration_residuals, test_residuals, level=level)
    if global_interval is None:
        return None
    finite_cal = np.isfinite(cal_values)
    if finite_cal.sum() < min_rows:
        return None
    threshold = float(np.median(cal_values[finite_cal]))
    segments: Dict[str, SegmentCalibration] = {}
    for label, mask in (("low", cal_values <= threshold),
                        ("high", cal_values > threshold)):
        segment_mask = finite_cal & mask
        segment_resid = cal_resid[segment_mask]
        width = conformal_half_width(segment_resid, level)
        supported = (int(len(segment_resid)) >= int(min_rows)
                     and width is not None)
        if width is None:
            width = global_interval.half_width_log
        segments[label] = SegmentCalibration(
            half_width_log=float(width),
            calibration_n=int(len(segment_resid)),
            coverage=None,
            test_n=0,
            supported=bool(supported),
            fallback=not bool(supported),
        )

    test_resid = (_finite(test_residuals) if test_residuals is not None
                  else [])
    test_vals = (np.asarray(test_values, dtype=float)
                 if test_values is not None else np.array([]))
    if test_residuals is not None and len(test_resid) and len(test_vals) == len(test_residuals):
        selected = np.full(len(test_resid), global_interval.half_width_log,
                           dtype=float)
        fallback_n = 0
        for index, value in enumerate(test_vals):
            if not math.isfinite(float(value)):
                fallback_n += 1
                continue
            label = "low" if float(value) <= threshold else "high"
            segment = segments.get(label)
            if segment is not None and segment.supported:
                selected[index] = segment.half_width_log
            else:
                fallback_n += 1
        inside = np.abs(np.asarray(test_resid, dtype=float)) <= selected
        selected_coverage = float(inside.mean()) if len(inside) else None
        selected_n = int(len(test_resid))
        # per-segment measured coverage under the selected widths
        for label, mask in (("low", test_vals <= threshold),
                            ("high", test_vals > threshold)):
            segment_mask = np.isfinite(test_vals) & mask
            segment_values = test_vals[segment_mask]
            segment_resid = np.asarray(test_resid, dtype=float)[segment_mask]
            segment = segments.get(label)
            if segment is None:
                continue
            segment.test_n = int(len(segment_resid))
            if len(segment_resid):
                width = (segment.half_width_log if segment.supported
                         else global_interval.half_width_log)
                segment.coverage = float(
                    np.mean(np.abs(segment_resid) <= width))
    else:
        fallback_n = 0
        selected_coverage = None
        selected_n = 0

    fallback_share = (float(fallback_n / selected_n)
                      if selected_n else 0.0)
    note = ""
    if fallback_n:
        note = (f"{fallback_n} of {selected_n} test rows fell back to the "
                f"global width")
    if fallback_share > 0.5:
        note += "; the conditional width is not supported on most rows"
    return SegmentedInterval(
        level=float(level),
        feature=str(feature),
        threshold=threshold,
        min_rows=int(min_rows),
        global_interval=global_interval,
        segments=segments,
        fallback_n=int(fallback_n),
        fallback_share=float(fallback_share),
        selected_coverage=selected_coverage,
        selected_test_n=int(selected_n),
        note=note.strip("; ").strip(),
    )


def _finite(values: Iterable[object]) -> list[float]:
    out: list[float] = []
    for value in values:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def conformal_half_width(residuals: Sequence[object],
                         level: float = 0.8) -> Optional[float]:
    """The split-conformal half-width for ``level`` coverage.

    ``residuals`` are signed errors on a calibration block.  The width is
    the ``ceil((n + 1) * level)``-th smallest absolute residual, which is
    the finite-sample conformal quantile: it is slightly wider than the
    plain empirical quantile, which is what keeps the guarantee honest on
    a small sample.

    ``None`` is returned when there are too few rows or when the
    residuals are not numbers.  A caller must not turn that into a zero
    width: "no interval" and "a point estimate" are different claims.
    """
    if not 0.0 < float(level) < 1.0:
        raise ValueError("the interval level must be between 0 and 1")
    errors = sorted(abs(value) for value in _finite(residuals))
    n = len(errors)
    if n < MIN_CALIBRATION_ROWS:
        return None
    rank = int(math.ceil((n + 1) * float(level)))
    rank = max(1, min(n, rank))
    width = errors[rank - 1]
    if not math.isfinite(width) or width <= 0:
        return None
    return float(width)


def empirical_coverage(residuals: Sequence[object],
                       half_width: float) -> Optional[float]:
    """The share of residuals inside ``[-half_width, +half_width]``."""
    errors = _finite(residuals)
    if not errors or not math.isfinite(float(half_width)) or half_width < 0:
        return None
    inside = sum(1 for error in errors if abs(error) <= half_width)
    return float(inside / len(errors))


@dataclass
class CalibratedInterval:
    """A width, the level it promises, and the coverage it delivered."""

    level: float = 0.8
    half_width_log: float = 0.0
    method: str = "split-conformal"
    calibration_n: int = 0
    coverage: Optional[float] = None
    test_n: int = 0
    note: str = ""

    def bounds(self, price: float) -> tuple[int, int]:
        """The copper bounds around a positive price."""
        value = float(price)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("the price must be a positive number")
        low = max(1, int(round(value * math.exp(-self.half_width_log))))
        high = max(low, int(round(value * math.exp(self.half_width_log))))
        return low, high

    def as_dict(self, prefix: str = "interval_") -> dict:
        data = asdict(self)
        return {f"{prefix}{key}": value for key, value in data.items()}


def calibrate_interval(calibration_residuals: Sequence[object],
                       test_residuals: Optional[Sequence[object]] = None,
                       level: float = 0.8) -> Optional[CalibratedInterval]:
    """Calibrate on one block and measure the coverage on another.

    The calibration block chooses the width; the test block only measures
    how often the true value landed inside it.  Reusing the calibration
    block for the measurement would report the nominal level by
    construction, which is exactly the number this module exists to avoid.
    """
    width = conformal_half_width(calibration_residuals, level)
    if width is None:
        return None
    calibration_n = len(_finite(calibration_residuals))
    test = _finite(test_residuals) if test_residuals is not None else []
    coverage = empirical_coverage(test, width) if test else None
    note = ""
    if coverage is None:
        note = "no held-out block was supplied; coverage is unmeasured"
    elif coverage + 0.05 < float(level):
        note = (f"the measured coverage ({coverage * 100:.0f}%) is below "
                f"the nominal level ({float(level) * 100:.0f}%)")
    return CalibratedInterval(
        level=float(level),
        half_width_log=float(width),
        calibration_n=calibration_n,
        coverage=coverage,
        test_n=len(test),
        note=note,
    )
