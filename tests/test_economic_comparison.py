"""The economic table must measure gold, not a score.

A model can win on log loss and still lose money if it ranks the wrong
listings; it can also lose on log loss and still earn more if its errors
sit on cheap listings.  The comparison therefore reports the two
separately, and these tests pin the arithmetic of the gold column --
including the rule that a censored listing is never counted as zero.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "compare_sentinel_models", ROOT / "scripts" / "compare_sentinel_models.py")
compare = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compare)


def _frame(outcomes, gold, qty=None):
    return pd.DataFrame({
        "outcome": list(outcomes),
        "y_net_gold": list(gold),
        "qty": list(qty) if qty is not None else [1] * len(outcomes),
    })


def test_a_sale_earns_its_net_gold_times_quantity():
    frame = _frame(["sale", "sale"], [10.0, 5.0], qty=(2, 20))
    assert list(compare.realised_gold(frame)) == [20.0, 100.0]


def test_an_expiry_or_cancellation_earns_nothing():
    frame = _frame(["expired", "cancelled"], [None, None])
    assert list(compare.realised_gold(frame)) == [0.0, 0.0]


def test_a_censored_listing_is_refused_not_counted_as_zero():
    """Counting it as zero would charge the strategy for not observing it."""
    frame = _frame(["sale", "censored"], [10.0, None])
    with pytest.raises(ValueError, match="censored"):
        compare.realised_gold(frame)


def test_a_threshold_policy_posts_only_what_clears_it():
    frame = _frame(["sale", "sale", "expired"], [10.0, 100.0, None])
    p = np.array([0.9, 0.1, 0.8])
    table = compare.economics(frame, p, thresholds=(0.5,))

    posted = table["policies"]["p>=0.5"]
    assert posted["posted"] == 2          # rows 0 and 2
    assert posted["sales"] == 1           # only row 0 sold
    assert posted["net_gold"] == 10.0
    assert posted["gold_per_post"] == 5.0


def test_posting_everything_is_the_do_nothing_alternative():
    frame = _frame(["sale", "expired"], [40.0, None])
    table = compare.economics(frame, np.array([0.0, 0.0]))
    assert table["policies"]["all"]["posted"] == 2
    assert table["policies"]["all"]["net_gold"] == 40.0


def test_the_top_share_policy_is_rank_only_and_ignores_calibration():
    frame = _frame(["sale", "expired", "expired", "sale"],
                   [10.0, None, None, 100.0])
    # same ranking (the two sales first), different scale: identical choice
    a = compare.economics(frame, np.array([0.9, 0.1, 0.2, 0.8]),
                          thresholds=(), top_fraction=0.5)
    b = compare.economics(frame, np.array([0.09, 0.01, 0.02, 0.08]),
                          thresholds=(), top_fraction=0.5)
    assert a["policies"]["top_50pct"]["posted"] == 2
    assert (a["policies"]["top_50pct"]["net_gold"]
            == b["policies"]["top_50pct"]["net_gold"] == 110.0)


def test_a_better_ranker_earns_more_at_the_same_threshold():
    frame = _frame(["sale", "expired", "sale", "expired"],
                   [10.0, None, 10.0, None])
    good = compare.economics(frame, np.array([0.9, 0.1, 0.9, 0.1]),
                             thresholds=(0.5,))["policies"]["p>=0.5"]
    bad = compare.economics(frame, np.array([0.1, 0.9, 0.1, 0.9]),
                            thresholds=(0.5,))["policies"]["p>=0.5"]
    assert good["net_gold"] == 20.0
    assert bad["net_gold"] == 0.0
