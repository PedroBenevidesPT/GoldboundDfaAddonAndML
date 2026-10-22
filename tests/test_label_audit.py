"""The horizon audit: are the sale targets actually different questions?"""
from __future__ import annotations

import numpy as np
import pandas as pd

from woweconomy.ml.label_audit import (
    DEFAULT_AGREEMENT,
    degenerate_horizons,
    horizon_closure,
    label_agreement,
)


def frame_with(labels: dict, rows: int = 100) -> pd.DataFrame:
    """A frame where every horizon shares the same positive set."""
    data = {"ts": list(range(rows))}
    for horizon, positives in labels.items():
        values = np.zeros(rows)
        values[:positives] = 1.0
        data[f"sold_within_{horizon}h"] = values
        data[f"label_known_{horizon}h"] = np.ones(rows)
    return pd.DataFrame(data)


def test_horizons_that_label_the_same_rows_are_one_question():
    frame = frame_with({24: 40, 48: 40, 168: 40, 336: 40})
    agreement = label_agreement(frame, [24, 48, 168, 336])
    assert all(p["label_agreement"] == 1.0 for p in agreement["pairs"])
    assert agreement["identical_groups"] == [[24, 48, 168, 336]]


def test_a_horizon_that_differs_by_one_row_is_still_degenerate():
    frame = frame_with({24: 40, 48: 41, 168: 41})
    mapping = degenerate_horizons(frame, [24, 48, 168])
    # 48 and 168 agree with each other; 48 differs from 24 by one row in
    # 100, which is below the 0.999 threshold, so only 168 collapses.
    assert mapping == {168: 48}


def test_genuinely_different_horizons_are_not_collapsed():
    frame = frame_with({24: 10, 48: 50, 168: 90})
    mapping = degenerate_horizons(frame, [24, 48, 168])
    assert mapping == {}


def test_a_group_of_three_collapses_to_its_shortest_member():
    frame = frame_with({24: 40, 48: 40, 168: 40})
    assert degenerate_horizons(frame, [24, 48, 168]) == {48: 24, 168: 24}


def test_rows_one_horizon_cannot_label_are_excluded_from_the_comparison():
    frame = frame_with({24: 40, 168: 40})
    # the last 30 rows have no 168h label: the horizon did not close
    frame.loc[70:, "label_known_168h"] = 0
    frame.loc[70:, "sold_within_168h"] = np.nan
    agreement = label_agreement(frame, [24, 168])
    assert agreement["pairs"][0]["rows"] == 70
    assert agreement["pairs"][0]["label_agreement"] == 1.0


def test_a_horizon_beyond_the_auction_is_reported_as_closed():
    frame = frame_with({24: 1, 168: 1})
    closure = horizon_closure(frame, observation_end=48 * 3600)
    assert closure["beyond_auction"] == [168]
    assert closure["beyond_window"] == [168]


def test_the_threshold_is_documented_and_used():
    assert DEFAULT_AGREEMENT == 0.999
