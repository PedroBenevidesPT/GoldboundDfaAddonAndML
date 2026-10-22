"""Tests for the Token Rush liquidation guard.

The rules under test:

* the posting decision follows the **addon's** semantics exactly, including
  the invalid-operation cases and the fact that a lower minimum neither
  cancels nor reposts;
* the guard floors the minimum and keeps the operation valid;
* the liquidation classification separates measured facts from heuristics.
"""
import pytest

from woweconomy.analytics import token_rush_guard as guard

GOLD = guard.COPPER_PER_GOLD


# --- the posting decision (the addon's semantics) --------------------------

def test_no_competitor_posts_at_the_normal_price():
    assert guard.posting_decision(None, 0, 100 * GOLD, 200 * GOLD, 300 * GOLD,
                                  has_competitor=False) == guard.POST_NORMAL


def test_a_wall_above_the_min_is_undercut():
    assert guard.posting_decision(150 * GOLD, 0, 100 * GOLD, 200 * GOLD,
                                  300 * GOLD) == guard.POST_UNDERCUT


def test_a_wall_below_the_min_blocks_the_posting():
    # priceReset = "none": TSM returns NOT_POSTING.BELOW_MIN and does not post
    assert guard.posting_decision(50 * GOLD, 0, 100 * GOLD, 200 * GOLD,
                                  300 * GOLD) == guard.BLOCKED_BELOW_MIN


def test_a_normal_price_below_the_min_makes_the_operation_invalid():
    # this is the trap the guard has to avoid: the item is not posted at all
    assert guard.posting_decision(150 * GOLD, 0, 200 * GOLD, 100 * GOLD,
                                  300 * GOLD) == guard.INVALID_NORMAL_BELOW_MIN


def test_a_max_price_below_the_min_makes_the_operation_invalid():
    assert guard.posting_decision(150 * GOLD, 0, 200 * GOLD, 250 * GOLD,
                                  100 * GOLD) == guard.INVALID_MAX_BELOW_MIN


def test_a_missing_price_source_makes_the_operation_invalid():
    assert guard.posting_decision(150 * GOLD, 0, None, 200 * GOLD,
                                  300 * GOLD) == guard.INVALID_MISSING


def test_a_wall_above_the_max_posts_at_the_cap():
    assert guard.posting_decision(500 * GOLD, 0, 100 * GOLD, 200 * GOLD,
                                  300 * GOLD) == guard.POST_MAX


def test_the_expires_window_stops_the_posting():
    # maxExpires counts the expiries since the last sale
    assert guard.posting_decision(150 * GOLD, 0, 100 * GOLD, 200 * GOLD, 300 * GOLD,
                                  num_expires=5, max_expires=2) == guard.BLOCKED_MAX_EXPIRES
    assert guard.posting_decision(150 * GOLD, 0, 100 * GOLD, 200 * GOLD, 300 * GOLD,
                                  num_expires=1, max_expires=2) == guard.POST_UNDERCUT


def test_posted_and_blocked_are_disjoint_and_complete():
    outcomes = [guard.POST_UNDERCUT, guard.POST_NORMAL, guard.POST_MAX,
                guard.BLOCKED_BELOW_MIN, guard.BLOCKED_MAX_EXPIRES,
                guard.INVALID_NORMAL_BELOW_MIN, guard.INVALID_MAX_BELOW_MIN,
                guard.INVALID_MISSING]
    for outcome in outcomes:
        # an outcome is either posted, blocked, or invalid -- never two of them
        assert sum([guard.is_posted(outcome), guard.is_blocked(outcome),
                    outcome.startswith("invalido")]) == 1, outcome
    assert not any(guard.is_posted(o) and guard.is_blocked(o) for o in outcomes)


def test_a_lower_minimum_does_not_change_a_valid_posting():
    # the demonstration that a lower minimum does not "cause a repost": the
    # outcome of a posting attempt only changes where the wall is between the
    # two minimums
    wall = 80 * GOLD
    high = guard.posting_decision(wall, 0, 100 * GOLD, 200 * GOLD, 300 * GOLD)
    low = guard.posting_decision(wall, 0, 50 * GOLD, 200 * GOLD, 300 * GOLD)
    assert high == guard.BLOCKED_BELOW_MIN
    assert low == guard.POST_UNDERCUT
    # ... and where the wall is far away, nothing changes
    far = 150 * GOLD
    assert (guard.posting_decision(far, 0, 100 * GOLD, 200 * GOLD, 300 * GOLD)
            == guard.posting_decision(far, 0, 50 * GOLD, 200 * GOLD, 300 * GOLD))


# --- the guard ------------------------------------------------------------

def test_the_floor_only_applies_where_it_is_enabled():
    assert guard.guard_minimum(50 * GOLD, 100 * GOLD, weak_evidence=True,
                                apply_floor=True) == 100 * GOLD
    assert guard.guard_minimum(50 * GOLD, 100 * GOLD, weak_evidence=True,
                                apply_floor=False) == 50 * GOLD
    # the narrow floor: only the weak-evidence items
    assert guard.guard_minimum(50 * GOLD, 100 * GOLD, weak_evidence=False,
                                apply_floor=True) == 50 * GOLD


def test_the_guard_never_lowers_the_minimum():
    for weak in (True, False):
        assert guard.guard_minimum(150 * GOLD, 100 * GOLD, weak_evidence=weak,
                                    apply_floor=True) == 150 * GOLD


def test_alternative_c_keeps_the_operation_valid():
    chosen = guard.apply_alternative(
        guard.ALTERNATIVE_C,
        weighted_min=50 * GOLD, weighted_normal=80 * GOLD, weighted_max=300 * GOLD,
        original_min=200 * GOLD, original_normal=250 * GOLD, original_max=400 * GOLD,
        weak_evidence=True)
    assert chosen.min_price == 200 * GOLD
    assert chosen.normal_price >= chosen.min_price
    assert chosen.max_price >= chosen.normal_price
    assert guard.posting_decision(150 * GOLD, 0, chosen.min_price,
                                  chosen.normal_price, chosen.max_price) \
        != guard.INVALID_NORMAL_BELOW_MIN


def test_alternative_d_only_floors_the_weak_evidence_items():
    weak = guard.apply_alternative(
        guard.ALTERNATIVE_D,
        weighted_min=50 * GOLD, weighted_normal=80 * GOLD, weighted_max=300 * GOLD,
        original_min=200 * GOLD, original_normal=250 * GOLD, original_max=400 * GOLD,
        weak_evidence=True)
    strong = guard.apply_alternative(
        guard.ALTERNATIVE_D,
        weighted_min=50 * GOLD, weighted_normal=80 * GOLD, weighted_max=300 * GOLD,
        original_min=200 * GOLD, original_normal=250 * GOLD, original_max=400 * GOLD,
        weak_evidence=False)
    assert weak.min_price == 200 * GOLD
    assert strong.min_price == 50 * GOLD
    assert strong.normal_price == 80 * GOLD


def test_alternative_a_is_the_original_untouched():
    chosen = guard.apply_alternative(
        guard.ALTERNATIVE_A,
        weighted_min=50 * GOLD, weighted_normal=80 * GOLD, weighted_max=300 * GOLD,
        original_min=200 * GOLD, original_normal=250 * GOLD, original_max=400 * GOLD,
        weak_evidence=True)
    assert (chosen.min_price, chosen.normal_price, chosen.max_price) == \
        (200 * GOLD, 250 * GOLD, 400 * GOLD)


def test_an_unknown_alternative_is_refused():
    with pytest.raises(ValueError):
        guard.apply_alternative("Z", 1, 1, 1, 1, 1, 1, weak_evidence=True)


# --- the liquidation classification ---------------------------------------

def facts(**kwargs):
    base = dict(item_id=1, name="X", realm="R", want_rate=0.2, demand_known=True,
                listings=3, local_market=100 * GOLD, local_wall=100 * GOLD,
                dispersion=0.1, divergence=0.05, freshness_hours=1.0,
                sales=0, expired=0, attempts=0, qty=1,
                min_price=50 * GOLD, wall=100 * GOLD, undercut=0,
                normal_price=100 * GOLD, max_price=200 * GOLD)
    base.update(kwargs)
    return guard.LiquidationFacts(**base)


def test_measured_demand_means_liquidate():
    outcome, reasons = guard.classify_liquidation(facts())
    assert outcome == guard.LIQUIDATE
    assert any("procura medida" in reason for reason in reasons)


def test_own_sales_with_a_good_sell_through_mean_liquidate():
    outcome, _ = guard.classify_liquidation(facts(want_rate=None, demand_known=False,
                                                 sales=4, attempts=8))
    assert outcome == guard.LIQUIDATE


def test_no_evidence_means_hold_the_price():
    outcome, _ = guard.classify_liquidation(facts(want_rate=0.01, demand_known=True,
                                                  listings=5, divergence=0.05))
    assert outcome == guard.HOLD


def test_repeated_expirations_without_a_sale_mean_do_not_repost():
    outcome, reasons = guard.classify_liquidation(facts(expired=3, sales=0))
    assert outcome == guard.NO_REPOST
    assert any("expirações" in reason for reason in reasons)


def test_a_severe_divergence_without_demand_means_do_not_repost():
    outcome, reasons = guard.classify_liquidation(
        facts(want_rate=None, demand_known=False, divergence=1.5))
    assert outcome == guard.NO_REPOST
    assert any("divergência" in reason for reason in reasons)


def test_a_floor_above_the_wall_means_do_not_repost():
    outcome, reasons = guard.classify_liquidation(
        facts(min_price=150 * GOLD, wall=100 * GOLD))
    assert outcome == guard.NO_REPOST
    assert any("piso" in reason for reason in reasons)


def test_the_weak_evidence_flag_uses_measured_facts():
    shallow = facts(listings=1)
    deep = facts(listings=8)
    unknown_demand = facts(demand_known=False, want_rate=None)
    severe = facts(divergence=1.2)
    assert shallow.weak_evidence is True
    assert deep.weak_evidence is False
    assert unknown_demand.weak_evidence is True
    assert severe.weak_evidence is True


def test_the_sell_through_needs_attempts():
    assert facts(sales=1, attempts=0).sell_through is None
    assert facts(sales=1, attempts=2).sell_through == pytest.approx(0.5)


# --- regression: the coefficient is part of the price ---------------------

def test_the_band_coefficient_is_part_of_the_price_the_tsm_uses():
    # the price field is "<coeficiente> * (referência)": evaluating only the
    # reference inside the parentheses would compare the wrong numbers (this
    # defect once made 83 items look blocked when only 4 are)
    from woweconomy.analytics import reference_price as rp
    from woweconomy.analytics.tsm_export import deserialize

    values = {"dbmarket": 100 * GOLD, "dbregionmarketavg": 100 * GOLD,
              "dbregionhistorical": 100 * GOLD}
    field = "0.250 * (first(dbmarket, dbregionmarketavg, dbregionhistorical))"
    whole = rp.evaluate_tsm_expression(field, values)
    inner = rp.evaluate_tsm_expression(
        "first(dbmarket, dbregionmarketavg, dbregionhistorical)", values)
    assert whole == pytest.approx(0.25 * inner)
    assert whole < inner


def test_the_generated_payload_orders_its_price_fields():
    from pathlib import Path

    from woweconomy.analytics import reference_price as rp
    from woweconomy.analytics.tsm_export import deserialize

    for name in ("pricing_groups_proposal.tsm", "pricing_groups_weighted_proposal.tsm"):
        path = Path(r"C:\FarmAnalysis\reports\pricing_groups") / name
        if not path.exists():
            continue
        data = deserialize(path.read_text(encoding="utf-8").strip())
        for operation, fields in data["operations"]["Auctioning"].items():
            values = {"dbmarket": 100 * GOLD, "dbregionmarketavg": 400 * GOLD,
                      "dbregionhistorical": 90 * GOLD}
            minimum = rp.evaluate_tsm_expression(fields["minPrice"], values)
            normal = rp.evaluate_tsm_expression(fields["normalPrice"], values)
            maximum = rp.evaluate_tsm_expression(fields["maxPrice"], values)
            assert minimum <= normal <= maximum, (name, operation)
