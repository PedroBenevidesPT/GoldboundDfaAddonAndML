"""Market context in the decisions: when it changes the answer, and when it does not.

The posting decision without a market is pinned by
``tests/test_posting_decisions.py``; these tests pin what the measured
context adds, and that it never fires without evidence.
"""
from __future__ import annotations

import math

import pytest

from woweconomy.intel.relist import HOLD, RELAST, WATCH, decide as relist_decide
from woweconomy.ml.market_context import ItemMarket
from woweconomy.ml.posting import decide


def _market(**kwargs) -> ItemMarket:
    return ItemMarket(item_id=1, **kwargs)


# -- the posting decision ---------------------------------------------------

def test_a_gap_inside_the_measured_noise_is_not_worth_a_deposit():
    """6 % off the cheapest ask, in a market that moves 10 % a snapshot."""
    common = dict(reference=20000, vendor_sell_copper=100, sale_probability=0.9,
                  confidence=0.8, best_ask=18800, list_price=20000, hours=12)
    without = decide(**common)
    with_market = decide(**common, market=_market(volatility=0.10))
    assert without.action == "relist"
    assert with_market.action == "hold"
    assert "noise" in with_market.reason


def test_a_gap_larger_than_the_noise_still_relists():
    common = dict(reference=20000, vendor_sell_copper=100, sale_probability=0.9,
                  confidence=0.8, best_ask=10000, list_price=20000, hours=12)
    result = decide(**common, market=_market(volatility=0.02))
    assert result.action == "relist"


def test_our_own_stock_is_not_competition_to_undercut():
    result = decide(reference=20000, vendor_sell_copper=100, sale_probability=0.9,
                    confidence=0.8, best_ask=19000, list_price=20000, hours=12,
                    market=_market(own_share=0.5))
    assert result.action == "hold"
    assert "our own" in result.reason


def test_our_own_cheapest_listing_is_not_a_competitor_to_match():
    result = decide(reference=10000, vendor_sell_copper=100, sale_probability=0.9,
                    confidence=0.8, best_ask=9000, list_price=9500, hours=12,
                    market=_market(own_cheapest_ask=9000, own_share=0.1))
    assert result.action == "hold"
    assert "our own" in result.reason


def test_a_falling_market_justifies_a_relist_the_gap_alone_would_not():
    common = dict(reference=20000, vendor_sell_copper=100, sale_probability=0.9,
                  confidence=0.8, best_ask=19600, list_price=20000, hours=12)
    without = decide(**common)
    with_market = decide(**common, market=_market(trend_pct=-0.25, path_snapshots=8))
    assert without.action == "hold"
    assert with_market.action == "relist"
    assert "down" in with_market.reason


def test_a_clearing_market_with_a_cheaper_ask_means_wait():
    result = decide(reference=10000, vendor_sell_copper=100, sale_probability=0.5,
                    confidence=0.8, best_ask=8000,
                    market=_market(items_ahead=3, units_change_pct=-0.2,
                                   trend_pct=0.02, confidence=0.5))
    assert result.action == "wait"
    assert result.ask == 0
    assert result.projected_ask == 10000
    assert "assum" in result.reason  # the assumption is stated, not hidden


def test_waiting_is_refused_when_the_market_is_falling():
    result = decide(reference=10000, vendor_sell_copper=100, sale_probability=0.5,
                    confidence=0.8, best_ask=8000,
                    market=_market(items_ahead=3, units_change_pct=-0.2,
                                   trend_pct=-0.1, confidence=0.5))
    assert result.action == "undercut"


def test_waiting_is_refused_while_supply_is_growing():
    result = decide(reference=10000, vendor_sell_copper=100, sale_probability=0.5,
                    confidence=0.8, best_ask=8000,
                    market=_market(items_ahead=3, units_change_pct=0.30,
                                   trend_pct=0.02, confidence=0.5))
    assert result.action == "undercut"


def test_a_listing_above_the_supported_price_does_not_raise_our_ask():
    """A lone expensive ask is not evidence the item sells there."""
    result = decide(reference=10000, vendor_sell_copper=100, sale_probability=0.5,
                    confidence=0.8, best_ask=34000)
    assert result.action == "post"
    assert result.ask == 10000
    assert "above the supported price" in result.reason


def test_the_market_is_reported_with_the_decision():
    result = decide(reference=10000, vendor_sell_copper=100, sale_probability=0.5,
                    confidence=0.8, market=_market(volatility=0.03, trend_pct=0.1))
    assert result.market is not None
    assert result.market["volatility"] == pytest.approx(0.03)
    assert result.as_dict()["market"]["trend_pct"] == pytest.approx(0.1)


# -- the relist decision (the daemon's path) --------------------------------

def test_relist_ignores_a_gap_inside_the_noise():
    without = relist_decide(own_unit=10000, lowest_unit=9700, items_ahead=0,
                            own_quantity=1, hours_left=20,
                            reference_status="verified")
    with_market = relist_decide(own_unit=10000, lowest_unit=9700, items_ahead=0,
                                own_quantity=1, hours_left=20,
                                reference_status="verified",
                                market=_market(volatility=0.10))
    assert without["advice"] == WATCH
    assert with_market["advice"] == HOLD
    assert "noise" in with_market["reason"]


def test_relist_holds_when_the_cheapest_ask_is_our_own_stock():
    result = relist_decide(own_unit=10000, lowest_unit=9000, items_ahead=5,
                           own_quantity=1, hours_left=20,
                           reference_status="verified",
                           market=_market(own_share=0.6))
    assert result["advice"] == HOLD
    assert "our own" in result["reason"]


def test_relist_follows_a_falling_market():
    result = relist_decide(own_unit=10000, lowest_unit=9700, items_ahead=0,
                           own_quantity=1, hours_left=20,
                           reference_status="verified",
                           market=_market(trend_pct=-0.30))
    assert result["advice"] == RELAST
    assert result["suggested_ask_copper"] == 9699


def test_relist_holds_while_the_cheap_stock_is_clearing():
    result = relist_decide(own_unit=10000, lowest_unit=9700, items_ahead=1,
                           own_quantity=1, hours_left=20,
                           reference_status="verified",
                           market=_market(units_change_pct=-0.25, trend_pct=0.0))
    assert result["advice"] == HOLD
    assert "clearing" in result["reason"]


def test_relist_without_a_market_keeps_its_old_answer():
    result = relist_decide(own_unit=10000, lowest_unit=9700, items_ahead=0,
                           own_quantity=1, hours_left=20,
                           reference_status="verified", market=None)
    assert result["advice"] == WATCH
    assert "market" not in result


def test_an_unverified_reference_is_never_upgraded_by_a_market():
    result = relist_decide(own_unit=10000, lowest_unit=9000, items_ahead=5,
                           own_quantity=1, hours_left=20,
                           reference_status="price-only",
                           market=_market(trend_pct=-0.40, own_share=0.0))
    assert result["advice"] == WATCH
    assert result["review_required"] is True
