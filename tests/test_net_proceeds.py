"""Net proceeds and the costs they must include.

`AH_CONSIGNMENT_RATE` is 0.05, a rate.  The old body divided it by 100 and
charged 0.05 %, so every decision was made against a cost a hundred times
too small and no deposit at all.  These tests pin the arithmetic.
"""
import math

from woweconomy.ml.config import AH_CONSIGNMENT_RATE
from woweconomy.ml.decision_engine import decide
from woweconomy.ml.market_eval import ItemEvaluation, _compute_net_proceeds


def test_the_commission_is_five_percent_of_the_price():
    assert AH_CONSIGNMENT_RATE == 0.05
    assert _compute_net_proceeds(100_000) == 95_000


def test_a_percentage_passed_as_a_number_still_means_a_percentage():
    """Call sites written against the old signature pass 5.0 for 5 %."""
    assert _compute_net_proceeds(100_000, 5.0) == 95_000
    assert _compute_net_proceeds(100_000, 0.05) == 95_000


def test_an_unknown_sale_probability_pays_the_whole_deposit():
    assert _compute_net_proceeds(100_000, deposit=1_000) == 94_000
    assert _compute_net_proceeds(100_000, deposit=1_000,
                                 sale_probability=math.nan) == 94_000


def test_a_certain_sale_pays_no_deposit_and_an_impossible_one_pays_it_all():
    assert _compute_net_proceeds(100_000, deposit=1_000, sale_probability=1.0) == 95_000
    assert _compute_net_proceeds(100_000, deposit=1_000, sale_probability=0.0) == 94_000
    assert _compute_net_proceeds(100_000, deposit=1_000, sale_probability=0.5) == 94_500


def _evaluation(**overrides) -> ItemEvaluation:
    values = dict(item_id=1, realm="Dragonmaw", current_price=10_000.0,
                  competing_price=9_000.0, own_listings=1.0,
                  deposit_cost=125.0, sale_probability=0.5,
                  vendor_price=2_500.0,
                  is_verified=True, reference_status="verified")
    values.update(overrides)
    return ItemEvaluation(**values)


def test_a_listing_at_the_competitor_price_is_maintained_and_priced_net():
    decision = decide(_evaluation(current_price=9_000.0, competing_price=9_000.0))
    assert decision.action == "MAINTAIN"
    # 9,000 - 5 % - half the 125 copper deposit
    assert decision.net_proceeds == 9_000 * 0.95 - 62.5


def test_a_small_gap_does_not_trigger_a_blind_undercut():
    decision = decide(_evaluation(current_price=10_000.0, competing_price=9_500.0))
    assert decision.action == "MAINTAIN"
    assert any("small price gap" in line for line in decision.evidence)


def test_the_deposit_reaches_the_reported_net_for_a_relisting():
    decision = decide(_evaluation(current_price=20_000.0, competing_price=10_000.0,
                                  sale_probability=0.0))
    assert decision.action in ("UNDERCUT", "CANCEL_AND_RELIST")
    # an item that will not sell pays the whole deposit, so the net is lower
    # than the bare commission would suggest
    assert decision.net_proceeds <= 10_000 * 0.95 - 125
