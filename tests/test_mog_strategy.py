"""Transmog strategy: choose by expected net gold, not by sale probability.

Two rules are defended here:

1. **The cheapest price is not the best price.**  A test that a higher
   candidate wins on expected net gold while a lower one wins on sale
   probability.  If that test ever fails, the strategy has collapsed back
   into "maximise the sale rate".
2. **No probability means no recommendation.**  A price with no sale
   probability is not posted; it is reported as undecidable.
"""
from __future__ import annotations

import pytest

from woweconomy.analytics.ah_costs import net_proceeds
from woweconomy.analytics.mog_strategy import (
    ACTION_DO_NOT_POST,
    ACTION_MAINTAIN,
    ACTION_POST,
    ACTION_POST_PATIENT,
    ACTION_UNDERCUT,
    ACTION_VENDOR,
    DEFAULT_CYCLES,
    PRICE_CANDIDATES,
    P_SOURCE_MEASURED,
    P_SOURCE_MODEL,
    P_SOURCE_NONE,
    PostingPlan,
    StrategyInput,
    _measured_probability,
    evaluate,
)

ESTIMATE = 100_000          # 10 g
VENDOR = 5_000              # 50 s


def _input(**kw) -> StrategyInput:
    base = dict(item_id=1, name="Test Item",
                estimate_unit_copper=ESTIMATE,
                confidence="high", observations=20, quantity=1,
                vendor_unit_copper=VENDOR,
                competition_unit_copper=ESTIMATE, competition_count=3,
                horizon_days=30.0, expected_cycles=DEFAULT_CYCLES)
    base.update(kw)
    return StrategyInput(**base)


def _best(plan):
    """The winning candidate: measured prices only."""
    scored = [c for c in plan.candidates
              if c.measured and c.expected_net_copper is not None]
    return max(scored, key=lambda c: c.expected_net_copper)



# ----------------------------------------------------------------------
# the expected-net-gold rule
# ----------------------------------------------------------------------


def test_a_higher_price_can_beat_a_lower_one_on_expected_net_gold():
    """The whole point: sale probability alone must not decide."""
    plan = evaluate(_input(sold_per_day=0.05))
    best = _best(plan)
    cheapest = min(plan.candidates, key=lambda c: c.price_unit_copper)
    # the cheapest candidate must have the highest sale probability
    assert cheapest.p_sale >= best.p_sale
    # and must not be the winner on expected net gold
    assert best.price_unit_copper > cheapest.price_unit_copper
    assert best.expected_net_copper > cheapest.expected_net_copper


def test_the_recommendation_is_the_highest_expected_net_gold_candidate():
    """With nothing in the way, expected net gold picks the price.

    ``competition_count=0`` is the point of this test: the rule being checked
    is the expected-net optimum, and a live wall legitimately overrides it
    (that is what the wall tests below are about).
    """
    plan = evaluate(_input(sold_per_day=0.10, competition_count=0))
    scored = [c for c in plan.candidates
              if c.measured and c.expected_net_copper is not None]
    best = max(scored, key=lambda c: c.expected_net_copper)
    assert plan.price_unit_copper == best.price_unit_copper
    assert plan.expected_net_copper == pytest.approx(best.expected_net_copper)
    assert plan.action in (ACTION_POST, ACTION_POST_PATIENT,
                           ACTION_UNDERCUT, ACTION_MAINTAIN)


def test_expected_net_accounts_for_the_auction_cut_and_the_deposit():
    plan = evaluate(_input(sold_per_day=1.0))     # sells within the day
    scored = [c for c in plan.candidates
              if c.measured and c.expected_net_copper is not None]
    best = max(scored, key=lambda c: c.expected_net_copper)
    # at p -> 1 the answer approaches net(total) less the expected deposits
    assert best.expected_net_copper <= net_proceeds(best.price_unit_copper) + 1
    assert best.expected_net_copper > 0


def test_a_price_above_the_measured_level_is_listed_but_not_scored():
    """The forbidden extrapolation: no invented price-response curve."""
    plan = evaluate(_input(sold_per_day=50.0))
    above = [c for c in plan.candidates if c.price_unit_copper > ESTIMATE]
    assert above, "the higher bands should still be reported"
    for candidate in above:
        assert candidate.measured is False
        assert candidate.p_sale is None
        assert candidate.expected_net_copper is None
        # the gross is still known even though the chance is not
        assert candidate.gross_if_sold_copper > 0
    assert any("not scored" in f for f in plan.flags)


def test_a_certain_sale_posts_at_the_measured_level_not_above_it():
    """With no measured upside, the best defensible price is the estimate."""
    plan = evaluate(_input(sold_per_day=50.0, competition_count=0))
    assert plan.price_unit_copper == pytest.approx(ESTIMATE)


def test_the_deposit_is_charged_on_every_unsuccessful_cycle():
    plan = evaluate(_input(sold_per_day=0.02, expected_cycles=1))
    best = max((c for c in plan.candidates if c.measured),
               key=lambda c: c.expected_net_copper)
    assert best.relist_deposits_copper == 0
    plan_many = evaluate(_input(sold_per_day=0.02, expected_cycles=5))
    best_many = max((c for c in plan_many.candidates if c.measured),
                    key=lambda c: c.expected_net_copper)
    assert best_many.relist_deposits_copper > best.relist_deposits_copper
    assert best_many.expected_net_copper < best.expected_net_copper


def test_the_vendor_price_is_the_fallback_when_posting_loses():
    """A 5-silver item with a 50-silver vendor price must be vendored."""
    plan = evaluate(_input(estimate_unit_copper=500, sold_per_day=0.001,
                           vendor_unit_copper=5_000,
                           competition_unit_copper=500, competition_count=1))
    assert plan.action == ACTION_VENDOR
    assert "vendor" in plan.reason


def test_no_price_evidence_means_vendor_or_nothing_never_a_guess():
    plan = evaluate(_input(estimate_unit_copper=None))
    assert plan.action == ACTION_VENDOR
    assert plan.price_unit_copper is None
    assert plan.expected_net_copper is None

    no_vendor = evaluate(_input(estimate_unit_copper=None, vendor_unit_copper=None))
    assert no_vendor.action == ACTION_DO_NOT_POST
    assert "nothing to decide with" in no_vendor.reason


def test_no_sale_probability_means_no_recommendation():
    """A price without a probability is not posted."""
    plan = evaluate(_input(sold_per_day=None, p_sale=None))
    assert plan.action == ACTION_DO_NOT_POST
    assert plan.p_sale is None
    assert plan.p_sale_source == P_SOURCE_NONE
    assert "no sale probability" in " ".join(plan.flags)


# ----------------------------------------------------------------------
# probability sources are labelled, never conflated
# ----------------------------------------------------------------------


def test_a_measured_rate_is_labelled_as_a_measured_rate():
    plan = evaluate(_input(sold_per_day=0.05))
    assert plan.p_sale_source == P_SOURCE_MEASURED
    assert plan.p_sale is not None
    assert 0 < plan.p_sale < 1


def test_a_model_probability_is_labelled_as_a_model_probability():
    plan = evaluate(_input(sold_per_day=None, p_sale=0.4,
                           p_sale_source=P_SOURCE_MODEL))
    assert plan.p_sale_source == P_SOURCE_MODEL
    assert plan.p_sale == pytest.approx(0.4)


def test_a_model_probability_wins_over_a_measured_rate():
    plan = evaluate(_input(sold_per_day=0.9, p_sale=0.2,
                           p_sale_source=P_SOURCE_MODEL))
    assert plan.p_sale == pytest.approx(0.2)


def test_a_zero_turnover_gives_no_probability():
    """Zero measured movement is unproven, not a zero chance of sale."""
    plan = evaluate(_input(sold_per_day=0.0))
    assert plan.p_sale is None
    assert plan.action == ACTION_DO_NOT_POST


# ----------------------------------------------------------------------
# uncertainty and confidence
# ----------------------------------------------------------------------


def test_confidence_tracks_the_number_of_observations():
    assert evaluate(_input(sold_per_day=0.1, observations=0)).confidence == "none"
    assert evaluate(_input(sold_per_day=0.1, observations=2)).confidence == "low"
    assert evaluate(_input(sold_per_day=0.1, observations=5)).confidence == "medium"
    assert evaluate(_input(sold_per_day=0.1, observations=20)).confidence == "high"


def test_a_thin_evidence_estimate_has_a_wide_uncertainty():
    thin = evaluate(_input(sold_per_day=0.1, observations=1))
    thick = evaluate(_input(sold_per_day=0.1, observations=50))
    thin_best = _best(thin)
    thick_best = _best(thick)
    assert thin_best.uncertainty_copper > thick_best.uncertainty_copper


def test_zero_observations_gives_an_uncertainty_as_wide_as_the_estimate():
    plan = evaluate(_input(sold_per_day=0.1, observations=0))
    best = _best(plan)
    assert best.uncertainty_copper == pytest.approx(abs(best.expected_net_copper))


def test_an_unmeasurable_opportunity_cost_is_not_assumed_to_be_zero():
    plan = evaluate(_input(sold_per_day=0.1))
    best = _best(plan)
    assert best.opportunity_cost_copper == 0.0
    # and the plan says the tied-up gold, so the caller can apply its own
    assert plan.tied_up_gold == pytest.approx(ESTIMATE / 10_000.0)


def test_a_supplied_opportunity_cost_reduces_the_expected_net():
    free = evaluate(_input(sold_per_day=0.05))
    costly = evaluate(_input(sold_per_day=0.05, opportunity_cost_per_day=100.0))
    free_best = _best(free)
    costly_best = _best(costly)
    assert costly_best.expected_net_copper < free_best.expected_net_copper
    assert costly_best.opportunity_cost_copper > 0


# ----------------------------------------------------------------------
# the measured-probability boundary
# ----------------------------------------------------------------------


def test_a_price_at_or_below_the_estimate_keeps_the_measured_probability():
    assert _measured_probability(0.3, 100_000, 100_000) == pytest.approx(0.3)
    assert _measured_probability(0.3, 50_000, 100_000) == pytest.approx(0.3)


def test_a_price_above_the_estimate_has_no_measured_probability():
    """Refusing to answer is the point: no curve was ever fitted."""
    assert _measured_probability(0.5, 150_000, 100_000) is None
    assert _measured_probability(0.5, 300_000, 100_000) is None


def test_no_base_probability_means_no_probability_at_all():
    assert _measured_probability(None, 100_000, 100_000) is None
    assert _measured_probability(0.5, 100_000, 0) is None


def test_a_slow_item_is_posted_patiently_rather_than_discounted():
    plan = evaluate(_input(sold_per_day=0.02, competition_unit_copper=None,
                           competition_count=0))
    assert plan.action == ACTION_POST_PATIENT
    assert "holding the price" in plan.reason


def test_a_slow_look_at_the_wall_is_maintained_not_undercut():
    """A rare look with one competitor: an undercut gives gold away.

    Undercutting buys almost no extra chance when the buyer is shopping for
    an appearance, so the correct move is to hold the price.
    """
    plan = evaluate(_input(sold_per_day=0.02,
                           competition_unit_copper=ESTIMATE,
                           competition_count=1))
    assert plan.action == ACTION_MAINTAIN
    assert "without adding a sale" in plan.reason


def test_a_fast_look_at_the_wall_is_undercut():
    """When it moves quickly, being cheapest is what converts."""
    plan = evaluate(_input(sold_per_day=2.0,
                           competition_unit_copper=ESTIMATE,
                           competition_count=1))
    assert plan.action == ACTION_UNDERCUT


def test_a_price_under_the_wall_is_posted_without_an_undercut():
    plan = evaluate(_input(sold_per_day=2.0,
                           competition_unit_copper=ESTIMATE * 3.0,
                           competition_count=1))
    assert plan.action == ACTION_POST


def test_every_candidate_price_is_scored():
    plan = evaluate(_input(sold_per_day=0.1, competition_count=0))
    assert len(plan.candidates) == len(PRICE_CANDIDATES)
    prices = [c.price_unit_copper for c in plan.candidates]
    assert prices == sorted(prices)
    assert prices[0] == pytest.approx(ESTIMATE * PRICE_CANDIDATES[0])


def test_a_live_wall_adds_the_price_that_beats_it():
    """The band cannot express an undercut, so the beating price is scored.

    Without it, ``undercut`` could only ever be recommended at a price at or
    above the wall it is meant to beat -- a plan the addon cannot act on.
    """
    plan = evaluate(_input(sold_per_day=2.0, competition_unit_copper=ESTIMATE,
                           competition_count=1))
    prices = [c.price_unit_copper for c in plan.candidates]
    assert ESTIMATE - 1 in prices
    assert len(prices) == len(PRICE_CANDIDATES) + 1


def test_an_undercut_carries_a_price_below_the_wall():
    plan = evaluate(_input(sold_per_day=2.0,
                           competition_unit_copper=ESTIMATE,
                           competition_count=1))
    assert plan.action == ACTION_UNDERCUT
    assert plan.price_unit_copper < ESTIMATE
    assert plan.price_unit_copper == ESTIMATE - 1
    # the stored expected net belongs to the price that was chosen
    chosen = next(c for c in plan.candidates
                  if c.price_unit_copper == plan.price_unit_copper)
    assert plan.expected_net_copper == pytest.approx(chosen.expected_net_copper)


def test_a_maintained_price_above_the_wall_states_the_undercut_it_refused():
    plan = evaluate(_input(sold_per_day=0.02,
                           competition_unit_copper=ESTIMATE,
                           competition_count=1))
    assert plan.action == ACTION_MAINTAIN
    assert "undercutting to" in plan.reason
    assert "without adding a sale" in plan.reason
