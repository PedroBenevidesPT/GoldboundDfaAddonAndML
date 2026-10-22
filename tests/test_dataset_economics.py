"""What the dataset ships for money: vendor value and net proceeds.

The tooltip and the loot tracker used to show only the ask.  The ask is a
price to post at; the money is the realised price less the 5 % cut and the
deposit a sale refunds and an expiry does not.  Both are now computed by
the same `posting.sale_net` the decision layer uses, so the addon cannot
disagree with the engine about costs.
"""
from woweconomy.ml.posting import AH_CUT_RATE, deposit_copper, sale_net
from woweconomy.pricing.dataset import DatasetRecord
from woweconomy.pricing.engine import Estimate


def _record(price=10_000, expected_sale_price=None, probability=0.5) -> DatasetRecord:
    estimate = Estimate(realm="Dragonmaw", item_id=1, price=price,
                        expected_sale_price=expected_sale_price,
                        sale_probability=probability, confidence=0.8)
    return DatasetRecord("Dragonmaw", 1, estimate,
                         vendor_price=2_500, net_price=9_375)


def test_the_record_ships_the_vendor_value_and_the_net():
    body = "\n".join(_record().to_lua())
    assert "vendor = 2500" in body
    assert "net = 9375" in body


def test_the_net_is_the_realised_price_after_cut_and_expected_deposit():
    price, vendor, probability = 10_000, 2_500, 0.5
    expected = sale_net(price, vendor, probability, hours=24)
    # 10,000 - 500 cut - half of the 250 copper deposit
    assert expected == 9_375
    assert expected == _record().net_price


def test_a_sale_refunds_the_deposit_an_expiry_pays_it():
    vendor = 2_500
    deposit = deposit_copper(vendor, hours=24)
    assert deposit == 250
    assert sale_net(10_000, vendor, 1.0, hours=24) == 9_500
    assert sale_net(10_000, vendor, 0.0, hours=24) == 9_250


def test_the_regional_net_is_the_price_less_the_cut():
    price = 10_000
    net = int(round(price * (1.0 - AH_CUT_RATE)))
    assert net == 9_500
