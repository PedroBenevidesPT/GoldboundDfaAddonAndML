"""Tests for the per-realm scorecard (scripts/realm_report.py).

Fixtures only.  The database-facing functions are exercised against the live
store by the script itself; what is pinned here is the arithmetic that turns
raw columns into a ranking, because a silent error there would rank realms
confidently and wrongly.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.realm_report import (  # noqa: E402
    percentile,
    slug,
    verdict,
)


def test_slug_matches_the_tsm_scope_format():
    assert slug("Silvermoon") == "silvermoon"
    assert slug("Tarren Mill") == "tarrenmill"
    assert slug("Al'Akir") == "alakir"
    assert slug("Area 52") == "area52"


def test_percentile_ranks_between_the_other_values():
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 1.0) == 0.0
    assert percentile(values, 4.0) == 1.0
    assert 0.6 < percentile(values, 3.0) < 0.7


def test_a_missing_value_ranks_worst_not_middle():
    """A realm with no data must not outrank one with bad data."""
    assert percentile([1.0, 2.0, 3.0], None) == 0.0


def test_lower_depth_can_be_better():
    """Competition is inverted: fewer listings per item ranks higher."""
    values = [1.0, 5.0, 9.0]
    assert percentile(values, 1.0, higher_is_better=False) == 1.0
    assert percentile(values, 9.0, higher_is_better=False) == 0.0


def test_a_realm_with_no_items_says_so_rather_than_ranking_it():
    assert "no data" in verdict({"items": 0})


def test_a_price_wall_is_called_out_over_the_undercut_number():
    """69% identical prices is the finding; a 0% gap is its symptom."""
    said = verdict({"items": 100, "identical": 0.69, "undercut": 0.0,
                    "churn": 0.5})
    assert "price wall" in said
    assert "69%" in said


def test_a_thin_market_is_named_as_one():
    said = verdict({"items": 100, "identical": 0.1, "undercut": 0.2,
                    "churn": 0.02})
    assert "barely move" in said
