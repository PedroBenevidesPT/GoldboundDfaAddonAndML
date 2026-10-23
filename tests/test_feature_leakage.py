"""Outcome-derived columns must never reach a model.

``price_exact`` and ``price_ambiguous`` describe how a *sale* was matched
back to a posting.  A listing that never sold has no sale to match, so
``price_exact`` is 1 for sales and 0 for everything else: it is the label
wearing a feature's clothes.  Adding it to the frame in the matching
audit drove held-out AUC for ``sale_48h`` from 0.89 to a perfect 1.00.

These tests fail if any outcome-derived column becomes eligible again.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from woweconomy.ml.models import (NON_FEATURE_COLUMNS, categorical_columns,
                                  feature_columns)


def _frame(**extra) -> pd.DataFrame:
    base = {
        "item_id": [1, 2, 3, 4],
        "realm": ["a", "a", "b", "b"],
        "item_name": ["x", "y", "z", "w"],
        "ts": [1, 2, 3, 4],
        "date": [0, 0, 0, 0],
        "outcome": ["sale", "expired", "sale", "expired"],
        "elapsed_h": [1.0, 2.0, 3.0, 4.0],
        "tts_hours": [1.0, 2.0, 3.0, 4.0],
        "tts_censored": [0, 1, 0, 1],
        "y_net_unit": [10.0, None, 12.0, None],
        "y_net_unit_log": [2.3, None, 2.5, None],
        "sold_within_24h": [1.0, 0.0, 1.0, 0.0],
        "label_known_24h": [1, 1, 1, 1],
        "local_market_log": [5.0, 5.1, 5.2, 5.3],
        "cat_realm": ["a", "a", "b", "b"],
    }
    base.update(extra)
    return pd.DataFrame(base)


@pytest.mark.parametrize("column", ["price_exact", "price_ambiguous"])
def test_the_sale_matching_columns_are_not_features(column):
    frame = _frame(**{column: [1.0, 0.0, 1.0, 0.0]})
    assert column in NON_FEATURE_COLUMNS
    assert column not in feature_columns(frame)


def test_the_matching_columns_are_not_categorical_features_either():
    frame = _frame(price_exact=[1.0, 0.0, 1.0, 0.0])
    assert "price_exact" not in categorical_columns(frame)


def test_every_outcome_derived_column_stays_out_of_the_feature_list():
    """The whole class of leaks, not just the two that were found."""
    outcome_derived = {
        "outcome": ["sale", "expired", "sale", "expired"],
        "elapsed_h": [1.0, 2.0, 3.0, 4.0],
        "tts_hours": [1.0, 2.0, 3.0, 4.0],
        "tts_censored": [0, 1, 0, 1],
        "price_exact": [1.0, 0.0, 1.0, 0.0],
        "price_ambiguous": [0.0, 1.0, 0.0, 1.0],
        "y_net_unit": [10.0, 0.0, 12.0, 0.0],
        "y_net_unit_log": [2.3, 0.0, 2.5, 0.0],
        "y_gross_unit": [11.0, 0.0, 13.0, 0.0],
        "y_gross_unit_log": [2.4, 0.0, 2.6, 0.0],
        "y_net_gold": [0.001, 0.0, 0.0012, 0.0],
        "fee_ratio_observed": [0.05, 0.0, 0.05, 0.0],
    }
    frame = _frame(**outcome_derived)
    leaked = [c for c in outcome_derived if c in feature_columns(frame)]
    assert not leaked, f"outcome-derived columns reached the model: {leaked}"


def test_the_legitimate_market_features_are_still_features():
    """The blocklist must not swallow the real signal as collateral."""
    frame = _frame(local_market_log=[5.0, 5.1, 5.2, 5.3],
                   qty_log=[1.0, 2.0, 3.0, 4.0])
    feats = feature_columns(frame)
    assert "local_market_log" in feats
    assert "qty_log" in feats


def test_a_perfect_single_feature_predictor_is_caught_by_an_auc_scan():
    """The diagnostic that found this must keep working."""
    from sklearn.metrics import roc_auc_score

    y = np.array([1.0, 0.0, 1.0, 0.0, 1.0, 0.0])
    leak = np.array([1.0, 0.0, 1.0, 0.0, 1.0, 0.0])
    honest = np.array([0.4, 0.6, 0.5, 0.5, 0.45, 0.55])
    assert roc_auc_score(y, leak) == pytest.approx(1.0)
    assert roc_auc_score(y, honest) < 0.75
