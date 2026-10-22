"""Transmog valuation: realised sales first, asks second, never invented.

The rule being defended: an estimate with no completed sales is labelled
as an ask or a reference, never as a valuation.  ``is_valuation`` is the
flag a UI must check before it calls a number "the price".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from woweconomy.analytics.mog_valuation import (
    MIN_SALES_FOR_ANCHOR,
    SALE_STALE_H,
    PriceEstimate,
    PriceSignals,
    estimate_price,
    load_signals,
)

REAL_DB = Path("tsm_markets.db")
REAL_INTEL = Path("data/intel.sqlite")


def _signals(**kw) -> PriceSignals:
    base = dict(item_id=1, name="Test Item", as_of=1_800_000_000)
    base.update(kw)
    return PriceSignals(**base)


def _sales_signals(**kw) -> PriceSignals:
    base = dict(item_id=1, name="Test Item", as_of=1_800_000_000,
                sale_observations=5, sale_avg_copper=100_000.0,
                sale_min_copper=90_000.0, sale_max_copper=110_000.0,
                last_sale_at=1_800_000_000 - 3600)
    base.update(kw)
    return PriceSignals(**base)


# ----------------------------------------------------------------------
# the anchor: completed sales
# ----------------------------------------------------------------------


def test_enough_recent_sales_anchor_the_price():
    estimate = estimate_price(_sales_signals())
    assert estimate.usable
    assert estimate.is_valuation
    assert estimate.confidence == "high"
    assert estimate.unit_copper == pytest.approx(100_000.0)
    assert "5 completed sales" in estimate.basis


def test_too_few_sales_do_not_anchor_the_price():
    """Two sales is not a market, even a consistent one."""
    estimate = estimate_price(_sales_signals(
        sale_observations=MIN_SALES_FOR_ANCHOR - 1))
    assert not estimate.is_valuation
    assert estimate.confidence != "high"


def test_a_stale_sale_history_downgrades_to_medium():
    stale = 1_800_000_000 - int(SALE_STALE_H * 3600) - 3600
    estimate = estimate_price(_sales_signals(last_sale_at=stale))
    assert estimate.is_valuation
    assert estimate.confidence == "medium"
    assert "days old" in " ".join(estimate.flags)


def test_no_sales_means_the_estimate_is_an_ask_not_a_price():
    estimate = estimate_price(_signals(
        competition_copper=250_000.0, competition_count=1,
        regional_reference_copper=100_000.0))
    assert estimate.usable
    assert not estimate.is_valuation
    assert estimate.confidence == "low"
    assert "not a realised price" in " ".join(estimate.flags)


def test_the_wall_is_not_used_as_the_valuation():
    """Measured: the wall predicts the next value 11.9% off; the average 0.4%.

    A wall is a MIN over listings, so one junk listing defines it.  The
    average must win when both are available.
    """
    estimate = estimate_price(_signals(
        competition_copper=1.0, competition_count=5,
        regional_reference_copper=100_000.0))
    assert estimate.unit_copper == pytest.approx(100_000.0)
    assert "ask average" in estimate.basis
    assert any("junk listing" in f for f in estimate.flags)


def test_a_wall_alone_is_the_weakest_possible_read():
    estimate = estimate_price(_signals(
        competition_copper=250_000.0, competition_count=3))
    assert estimate.unit_copper == pytest.approx(250_000.0)
    assert estimate.confidence == "low"
    assert not estimate.is_valuation
    assert "floor of asks" in " ".join(estimate.flags)


def test_only_a_vendor_price_is_a_floor_not_a_valuation():
    estimate = estimate_price(_signals(vendor_copper=4_000.0))
    assert estimate.unit_copper == pytest.approx(4_000.0)
    assert estimate.confidence == "none"
    assert not estimate.is_valuation
    assert "floor, not a valuation" in " ".join(estimate.flags)


def test_no_evidence_at_all_returns_no_price():
    estimate = estimate_price(_signals())
    assert estimate.unit_copper is None
    assert estimate.confidence == "none"
    assert not estimate.usable
    assert estimate.signals.has_completed_sales is False


def test_a_zero_or_negative_sale_average_is_ignored():
    estimate = estimate_price(_sales_signals(
        sale_avg_copper=0.0, sale_observations=5,
        competition_copper=120_000.0, competition_count=2))
    assert estimate.unit_copper == pytest.approx(120_000.0)
    assert not estimate.is_valuation


def test_a_wall_above_the_realised_average_is_flagged():
    estimate = estimate_price(_sales_signals(competition_copper=400_000.0))
    assert estimate.is_valuation
    assert any("above the realised average" in f for f in estimate.flags)


def test_a_regional_disagreement_is_flagged():
    estimate = estimate_price(_sales_signals(regional_reference_copper=10_000.0))
    assert estimate.is_valuation
    assert any("regional reference disagrees" in f for f in estimate.flags)


# ----------------------------------------------------------------------
# against the real database
# ----------------------------------------------------------------------


@pytest.mark.skipif(not REAL_DB.exists(), reason="no tsm_markets.db in the workspace")
def test_signals_load_for_real_items_and_never_invent_a_sale():
    import sqlite3

    con = sqlite3.connect(REAL_DB)
    try:
        ids = [r[0] for r in con.execute(
            "SELECT DISTINCT item_id FROM auction_events"
            " WHERE item_id IS NOT NULL LIMIT 40")]
    finally:
        con.close()
    signals = load_signals(REAL_DB, ids, include_competition=False)
    assert len(signals) == len(ids)
    for item_id, row in signals.items():
        # a sale count can never exceed the number of observed prices
        assert row.sale_observations >= 0
        if row.sale_observations:
            assert row.sale_avg_copper is not None
            assert row.sale_min_copper <= row.sale_max_copper


@pytest.mark.skipif(not REAL_DB.exists(), reason="no tsm_markets.db in the workspace")
def test_a_missing_database_yields_no_signals(tmp_path):
    signals = load_signals(tmp_path / "gone.db", [1, 2, 3])
    assert signals == {}
