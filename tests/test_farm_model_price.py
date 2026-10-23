"""The farm ranking must price from the model, not from a TSM average.

The report's EV is `drop_rate x net sale price x liquidity`.  Those three
inputs come from the pricing engine now: the *realizable* sale price (the
Sentinel's expectation, not the ask), the sale probability converted to a
daily rate so the liquidity weight is not silently cut to 10 %, and a
fallback chain that is counted rather than assumed.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional

import pytest

from woweconomy.analytics.farm_ranking import ItemMarket, load_model_market


@dataclass
class _Estimate:
    price: Optional[int] = None
    expected_sale_price: Optional[int] = None
    sale_probability: Optional[float] = None
    expected_time_hours: Optional[float] = None
    confidence: float = 0.5
    available: bool = True


class _Config:
    horizon_h = 24


class _Engine:
    def __init__(self, estimates: Dict[int, _Estimate], shipped=None,
                 confidences: Optional[Dict[int, float]] = None):
        self.estimates = estimates
        self.shipped = shipped or {}
        self.confidences = confidences or {}
        self.config = _Config()

    def estimate(self, realm: str, item_id: int) -> Optional[_Estimate]:
        return self.estimates.get(item_id)

    def estimate_with_shipped_model(self, realm: str, item_id: int):
        return self.shipped.get(item_id)


def _market(item_id: int, price: float) -> ItemMarket:
    return ItemMarket(item_id=item_id, name="", price=price,
                      sale_rate=0.0, sold_per_day=0.0)


def test_the_realizable_sale_price_wins_over_the_ask():
    engine = _Engine({1: _Estimate(price=10_000, expected_sale_price=7_000,
                                   sale_probability=0.4)})
    market, counts = load_model_market(engine, [1])
    assert market[1].price == 7_000
    assert counts["estimate"] == 1


def test_the_probability_becomes_a_daily_rate_so_the_weight_is_not_halved():
    engine = _Engine({1: _Estimate(price=10_000, sale_probability=0.25)})
    market, _ = load_model_market(engine, [1])
    # 25 % inside 24 h is a quarter of a sale a day
    assert market[1].sold_per_day == pytest.approx(0.25)


def test_an_expected_time_to_sale_says_the_daily_rate_directly():
    engine = _Engine({1: _Estimate(price=10_000, sale_probability=0.9,
                                   expected_time_hours=12.0)})
    market, _ = load_model_market(engine, [1])
    assert market[1].sold_per_day == pytest.approx(2.0)


def test_a_model_that_cannot_price_falls_back_and_is_counted():
    engine = _Engine({}, shipped={2: 5_000})
    market, counts = load_model_market(engine, [2], fallback={2: _market(2, 9_000)})
    assert market[2].price == 5_000      # the shipped model, not the fallback
    assert counts["shipped"] == 1


def test_the_fallback_only_prices_what_no_model_can():
    engine = _Engine({}, shipped={})
    market, counts = load_model_market(engine, [3], fallback={3: _market(3, 9_000)})
    assert market[3].price == 9_000
    assert counts["fallback"] == 1
    assert counts["unpriced"] == 0


def test_an_item_nothing_can_price_is_left_unpriced():
    engine = _Engine({}, shipped={})
    market, counts = load_model_market(engine, [4])
    assert 4 not in market
    assert counts["unpriced"] == 1


def test_an_implausible_model_price_is_clamped_to_the_reference():
    engine = _Engine({5: _Estimate(price=200, sale_probability=0.5)})
    market, counts = load_model_market(engine, [5], fallback={5: _market(5, 10_000)})
    assert market[5].price == pytest.approx(10_000 * 0.25)
    assert counts["clamped"] == 1


def test_a_price_inside_the_band_is_untouched():
    engine = _Engine({6: _Estimate(price=9_000, sale_probability=0.5)})
    market, counts = load_model_market(engine, [6], fallback={6: _market(6, 10_000)})
    assert market[6].price == 9_000
    assert counts["clamped"] == 0


def test_a_low_confidence_answer_is_not_trusted_with_the_ranking():
    engine = _Engine({7: _Estimate(price=10_000, confidence=0.02)})
    market, counts = load_model_market(engine, [7], fallback={7: _market(7, 8_000)})
    assert market[7].price == 8_000
    assert counts["low_confidence"] == 1
