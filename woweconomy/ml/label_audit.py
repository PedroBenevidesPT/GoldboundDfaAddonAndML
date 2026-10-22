"""Do the sale horizons ask different questions?

Model B is trained once per horizon (24h, 48h, 168h, ...).  That is only
meaningful if the labels differ.  They do not, and the reason is
structural rather than empirical:

* an auction lasts at most 48 hours, so a sale that has not happened by
  then never will -- a 168h label is the 48h label;
* the account relists, and a sale is attributed to the most recent
  posting, so the measured elapsed time is compressed toward the start.

This module measures the overlap instead of assuming it.  Two horizons
whose labels agree on every row are the *same target*; training both and
reporting two sets of metrics manufactures the appearance of two models.

The measurement is exact (row-by-row on the rows both horizons can
label), not a correlation, so it can gate promotion.
"""
from __future__ import annotations

from itertools import combinations
from typing import Dict, Iterable, List, Sequence, Tuple

import pandas as pd

#: Two horizons are the same question when their labels agree on at least
#: this share of the rows both of them can label.  The default is set so
#: that a single flipped row in a thousand still counts as "the same"
#: (0.999) -- a horizon that differs by one row is not a different target,
#: it is the same target with a rounding difference.
DEFAULT_AGREEMENT = 0.999


def _known(frame: pd.DataFrame, horizon: int) -> pd.Series:
    label = f"sold_within_{horizon}h"
    known = f"label_known_{horizon}h"
    if label not in frame.columns:
        return pd.Series(False, index=frame.index)
    mask = frame[label].notna()
    if known in frame.columns:
        mask &= frame[known] == 1
    return mask


def label_agreement(frame: pd.DataFrame,
                    horizons: Sequence[int]) -> Dict[str, object]:
    """Exact pairwise agreement between horizon labels.

    Returns the pairs, the rows each pair could compare, and the groups
    that are identical within the tolerance.
    """
    pairs: List[Dict[str, object]] = []
    groups: List[List[int]] = []
    for a, b in combinations(sorted(horizons), 2):
        both = _known(frame, a) & _known(frame, b)
        n = int(both.sum())
        if n == 0:
            pairs.append({"horizons": [a, b], "rows": 0,
                          "label_agreement": None, "disagreements": None})
            continue
        left = frame.loc[both, f"sold_within_{a}h"].astype(float)
        right = frame.loc[both, f"sold_within_{b}h"].astype(float)
        disagreements = int((left != right).sum())
        pairs.append({
            "horizons": [a, b],
            "rows": n,
            "label_agreement": round(1.0 - disagreements / n, 6),
            "disagreements": disagreements,
        })

    ordered = sorted(horizons)
    remaining = list(ordered)
    while remaining:
        head = remaining[0]
        group = [head]
        rest = []
        for other in remaining[1:]:
            pair = next(p for p in pairs if p["horizons"] == sorted([head, other]))
            agreement = pair["label_agreement"]
            if agreement is not None and agreement >= DEFAULT_AGREEMENT:
                group.append(other)
            else:
                rest.append(other)
        if len(group) > 1:
            groups.append(group)
        remaining = rest
    return {"pairs": pairs, "identical_groups": groups,
            "threshold": DEFAULT_AGREEMENT}


def degenerate_horizons(frame: pd.DataFrame,
                        horizons: Sequence[int],
                        threshold: float = DEFAULT_AGREEMENT) -> Dict[int, int]:
    """``{horizon: shorter horizon it duplicates}``.

    The shortest horizon of an identical group is the one that is kept;
    every longer member is degenerate and must not be promoted as an
    independent model.
    """
    equivalence = label_agreement(frame, horizons)
    mapping: Dict[int, int] = {}
    for pair in equivalence["pairs"]:
        agreement = pair["label_agreement"]
        if agreement is None or agreement < threshold:
            continue
        short, long = pair["horizons"]
        mapping.setdefault(long, short)
    # A group of three must all point at the shortest member, not at the
    # middle one, so resolve transitively.
    for long, short in list(mapping.items()):
        while short in mapping:
            short = mapping[short]
        mapping[long] = short
    return {h: s for h, s in mapping.items() if h != s}


def horizon_closure(frame: pd.DataFrame, observation_end: int,
                    max_auction_h: int = 48) -> Dict[str, object]:
    """Which horizons cannot close inside the observation window?

    A horizon longer than the auction is not a longer sale probability;
    a horizon longer than the observation window cannot be labelled at
    all for rows near the end.
    """
    first_ts = int(frame["ts"].min()) if "ts" in frame.columns and len(frame) else 0
    window_h = ((int(observation_end) - first_ts) / 3600.0
                if len(frame) and observation_end > 0 else 0.0)
    return {
        "observation_window_h": round(window_h, 2),
        "max_auction_h": max_auction_h,
        "beyond_auction": [h for h in sorted(_horizons(frame)) if h > max_auction_h],
        "beyond_window": [h for h in sorted(_horizons(frame)) if h > window_h],
    }


def _horizons(frame: pd.DataFrame) -> Iterable[int]:
    for column in frame.columns:
        if column.startswith("sold_within_") and column.endswith("h"):
            try:
                yield int(column[len("sold_within_"):-1])
            except ValueError:
                continue
