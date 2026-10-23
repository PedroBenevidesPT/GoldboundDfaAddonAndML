"""Liquidity as a survival curve, with censoring taken seriously.

An auction that has not sold by the end of the observation period is
*right-censored*: it told us "not yet", not "never".  Dropping those rows
makes every market look faster than it is; counting them as failures
makes it look slower.  The Kaplan-Meier estimator uses both correctly.

The functions are dependency-free and deliberately small:

* :func:`kaplan_meier` -- the survival curve, its median and its
  restricted mean survival time;
* :func:`sale_probability_by` -- ``P(sold by t)`` at chosen horizons,
  which is the liquidity number the rest of the engine consumes;
* :func:`logrank` -- are two realms' time-to-sale distributions
  different, or is the gap consistent with noise?

No model is fitted here.  This is the non-parametric baseline the
CatBoost survival model has to beat.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass
class KMPoint:
    time: float
    survival: float
    at_risk: int
    events: int
    censored: int


@dataclass
class KMResult:
    points: List[KMPoint] = field(default_factory=list)
    n: int = 0
    events: int = 0
    censored: int = 0
    median: Optional[float] = None
    rmst: Optional[float] = None
    tau: Optional[float] = None

    def survival_at(self, t: float) -> float:
        """S(t): the last step at or before ``t`` (1.0 before the first)."""
        value = 1.0
        for point in self.points:
            if point.time > t:
                break
            value = point.survival
        return value

    def as_dict(self) -> Dict[str, object]:
        return {
            "n": self.n,
            "events": self.events,
            "censored": self.censored,
            "median": self.median,
            "rmst": self.rmst,
            "tau": self.tau,
        }


def kaplan_meier(durations: Iterable[float],
                 events: Iterable[int],
                 tau: Optional[float] = None) -> KMResult:
    """Kaplan-Meier survival for right-censored durations.

    ``events[i] = 1`` means the sale was observed; ``0`` means censored
    at that duration (the last moment we knew it had not sold).
    """
    pairs = [(float(d), int(bool(e))) for d, e in zip(durations, events)
             if d is not None and float(d) >= 0]
    result = KMResult(n=len(pairs), events=sum(e for _, e in pairs),
                      censored=sum(1 for _, e in pairs if not e))
    if not pairs:
        return result
    pairs.sort(key=lambda p: p[0])
    survival = 1.0
    times = sorted({t for t, _ in pairs})
    points: List[KMPoint] = []
    for time in times:
        at_risk = sum(1 for d, _ in pairs if d >= time)
        d = sum(1 for dur, event in pairs if dur == time and event)
        c = sum(1 for dur, event in pairs if dur == time and not event)
        if d:
            survival *= 1.0 - d / at_risk
        points.append(KMPoint(time=time, survival=max(0.0, survival),
                              at_risk=at_risk, events=d, censored=c))
    result.points = points
    for point in points:
        if point.survival <= 0.5:
            result.median = point.time
            break
    horizon = float(tau) if tau is not None else max(times)
    result.tau = horizon
    result.rmst = _rmst(points, horizon)
    return result


def _rmst(points: Sequence[KMPoint], tau: float) -> float:
    """Area under the step curve up to ``tau``."""
    if not points or tau <= 0:
        return 0.0
    area = 0.0
    previous_time = 0.0
    previous_survival = 1.0
    for point in points:
        if point.time >= tau:
            break
        area += previous_survival * (point.time - previous_time)
        previous_time = point.time
        previous_survival = point.survival
    area += previous_survival * max(0.0, tau - previous_time)
    return area


def sale_probability_by(durations: Sequence[float], events: Sequence[int],
                        horizons: Sequence[float]) -> Dict[str, Optional[float]]:
    """``P(sold by H)`` for each horizon, censoring included."""
    km = kaplan_meier(durations, events)
    out: Dict[str, Optional[float]] = {}
    for horizon in horizons:
        if not km.points:
            out[str(horizon)] = None
        else:
            out[str(horizon)] = round(1.0 - km.survival_at(float(horizon)), 4)
    return out


def hazard_curve(durations: Sequence[float], events: Sequence[int],
                 horizons: Sequence[float]) -> Dict[str, Optional[float]]:
    """Cumulative hazard ``H(t) = -ln S(t)`` at each horizon."""
    km = kaplan_meier(durations, events)
    out: Dict[str, Optional[float]] = {}
    for horizon in horizons:
        survival = km.survival_at(float(horizon))
        out[str(horizon)] = (round(-math.log(survival), 6)
                             if survival > 0 else None)
    return out


def logrank(group_a: Sequence[Tuple[float, int]],
            group_b: Sequence[Tuple[float, int]]) -> Dict[str, Optional[float]]:
    """Two-sample log-rank test (one degree of freedom).

    Returns the observed/expected counts for group A, the chi-square and
    a p-value.  The p-value uses the chi-square survival function for one
    degree of freedom, ``erfc(sqrt(chi2/2))``, so no SciPy is needed.
    """
    a = [(float(t), int(bool(e))) for t, e in group_a]
    b = [(float(t), int(bool(e))) for t, e in group_b]
    if not a or not b:
        return {"chi2": None, "p_value": None}
    times = sorted({t for t, e in a + b if e})
    observed = 0.0
    expected = 0.0
    variance = 0.0
    for time in times:
        at_risk_a = sum(1 for t, _ in a if t >= time)
        at_risk_b = sum(1 for t, _ in b if t >= time)
        at_risk = at_risk_a + at_risk_b
        d_a = sum(1 for t, e in a if t == time and e)
        d_b = sum(1 for t, e in b if t == time and e)
        d = d_a + d_b
        if at_risk == 0 or d == 0:
            continue
        observed += d_a
        share = at_risk_a / at_risk
        expected += d * share
        if at_risk > 1:
            variance += d * share * (1 - share) * (at_risk - d) / (at_risk - 1)
    if variance <= 0:
        return {"observed_a": observed, "expected_a": expected,
                "chi2": None, "p_value": None}
    chi2 = (observed - expected) ** 2 / variance
    p_value = math.erfc(math.sqrt(chi2 / 2.0))
    return {"observed_a": observed, "expected_a": expected,
            "chi2": round(chi2, 6), "p_value": round(p_value, 6)}
