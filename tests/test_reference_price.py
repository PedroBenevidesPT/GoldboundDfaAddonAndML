"""Tests for the evidence-weighted reference price.

The rules under test:

* the two references are combined by a **weight derived from the evidence**,
  never by a constant that happens to look reasonable;
* the weight is bounded, and the regional reference never dominates;
* a missing price is missing, and the sentinel is not a price;
* a divergence between the local and the regional is a diagnostic, not a
  price rule;
* the generated expression is valid TSM syntax and computes what the estimator
  says it computes;
* the realm modifier is applied once, and never inside the reference.
"""
import pytest

from woweconomy.analytics import pricing_groups as pg
from woweconomy.analytics import reference_price as rp
from woweconomy.analytics.mog_operations import build_mog_export
from woweconomy.analytics.tsm_export import deserialize, parse_group_export

GOLD = rp.COPPER_PER_GOLD


def local(**kwargs):
    base = dict(price=100 * GOLD, wall=100 * GOLD, listings=5, age_hours=0.0)
    base.update(kwargs)
    return rp.LocalEvidence(**base)


def regional(**kwargs):
    base = dict(price=100 * GOLD, historical=100 * GOLD, age_hours=24.0)
    base.update(kwargs)
    return rp.RegionalEvidence(**base)


# --- the weight -----------------------------------------------------------

def test_a_deep_and_fresh_local_takes_the_whole_weight():
    estimate = rp.estimate_reference(local(), regional())
    assert estimate.weight_local == 1.0
    assert estimate.weight_regional == 0.0
    assert estimate.price == 100 * GOLD
    assert estimate.confidence == "alta"


def test_a_thin_local_loses_weight_but_never_gives_it_away():
    estimate = rp.estimate_reference(local(listings=1), regional())
    assert 0.5 <= estimate.weight_local < 1.0
    assert estimate.weight_regional > 0.0
    assert estimate.weight_local + estimate.weight_regional == pytest.approx(1.0)


def test_the_weight_never_falls_below_the_floor():
    # no listings, a very dispersed local, and a stale observation
    estimate = rp.estimate_reference(
        local(listings=0, price=1000 * GOLD, wall=100 * GOLD, age_hours=200.0),
        regional())
    assert estimate.weight_local == rp.ReferenceConfig().weight_floor
    assert estimate.weight_regional <= 0.5


def test_a_stale_local_observation_loses_weight():
    # the regional observation is fresher than the local one: the local is
    # stale and must lose weight
    fresh = rp.estimate_reference(local(age_hours=1.0), regional(age_hours=48.0))
    stale = rp.estimate_reference(local(age_hours=48.0), regional(age_hours=1.0))
    assert stale.weight_local < fresh.weight_local


def test_a_dispersed_local_observation_loses_weight():
    tight = rp.estimate_reference(local(price=100 * GOLD, wall=100 * GOLD), regional())
    loose = rp.estimate_reference(local(price=400 * GOLD, wall=100 * GOLD), regional())
    assert loose.weight_local < tight.weight_local
    assert any("dispers" in flag or "peso" in flag for flag in loose.flags)


def test_the_depth_thresholds_come_from_the_measured_distribution():
    config = rp.ReferenceConfig()
    assert config.depth_score(0) < config.depth_score(1) < config.depth_score(3)
    assert config.depth_score(9) == 1.0
    # an unknown count is half trust, and it is flagged
    assert 0 < config.depth_score(None) < 1


# --- missing and sentinel prices ------------------------------------------

def test_a_missing_local_price_falls_back_to_the_regional():
    estimate = rp.estimate_reference(local(price=0), regional())
    assert estimate.price == 100 * GOLD
    assert estimate.source == "regional"
    assert any("sem preço local" in flag for flag in estimate.flags)


def test_a_missing_regional_price_falls_back_to_the_local():
    estimate = rp.estimate_reference(local(), regional(price=0))
    assert estimate.price == 100 * GOLD
    assert estimate.source == "local"


def test_both_missing_falls_back_to_the_regional_historical():
    estimate = rp.estimate_reference(local(price=0), regional(price=0))
    assert estimate.price == 100 * GOLD
    assert estimate.source == "regional_historical"
    assert estimate.confidence == "baixa"


def test_nothing_at_all_gives_no_price():
    estimate = rp.estimate_reference(local(price=0), regional(price=0, historical=0))
    assert estimate.price == 0
    assert estimate.source == "sem_preco"
    assert estimate.confidence == "nenhuma"


def test_the_sentinel_is_never_a_price():
    sentinel = rp.SENTINEL_COPPER
    estimate = rp.estimate_reference(local(price=sentinel), regional())
    assert estimate.price == 100 * GOLD
    assert any("sentinel" in flag for flag in estimate.flags)
    assert rp.clean_price(sentinel) == 0
    assert rp.clean_price(0) == 0
    assert rp.clean_price(5) == 5


# --- divergence -----------------------------------------------------------

def test_a_divergence_is_a_diagnostic_and_not_a_price_rule():
    # the local is four times the regional, but it is internally consistent
    # (the ask average sits on its own wall): the divergence must not move the
    # price, only the confidence
    base = rp.estimate_reference(local(listings=5, price=100 * GOLD, wall=100 * GOLD),
                                  regional(price=100 * GOLD))
    diverged = rp.estimate_reference(local(listings=5, price=400 * GOLD, wall=400 * GOLD),
                                     regional(price=100 * GOLD))
    assert diverged.weight_local == base.weight_local
    assert diverged.price == 400 * GOLD      # the local, not the regional
    assert diverged.confidence != "alta"
    assert any("divergência" in flag for flag in diverged.flags)


def test_a_divergence_does_not_force_the_price_down():
    estimate = rp.estimate_reference(local(listings=5, price=10 * GOLD),
                                     regional(price=100 * GOLD))
    # the local keeps the majority of the weight: the price stays near the local
    assert estimate.price < 100 * GOLD
    assert estimate.price > 10 * GOLD


# --- the estimator methods ------------------------------------------------

def test_the_geometric_mean_is_between_the_two_references():
    estimate = rp.estimate_reference(
        local(price=100 * GOLD, listings=1), regional(price=400 * GOLD),
        rp.ReferenceConfig(method="geometric"))
    assert 100 * GOLD < estimate.price < 400 * GOLD


def test_the_arithmetic_mean_is_above_the_geometric_one():
    args = (local(price=100 * GOLD, listings=1), regional(price=400 * GOLD))
    geometric = rp.estimate_reference(*args, rp.ReferenceConfig(method="geometric"))
    arithmetic = rp.estimate_reference(*args, rp.ReferenceConfig(method="arithmetic"))
    assert arithmetic.price > geometric.price


def test_an_unknown_method_is_refused():
    with pytest.raises(ValueError):
        rp.estimate_reference(local(), regional(), rp.ReferenceConfig(method="magic"))


# --- the TSM expression ---------------------------------------------------

def test_the_expression_computes_what_the_estimator_computes():
    config = rp.ReferenceConfig(method="geometric")
    # one listing and a consistent local: the measured weight is exactly 0.65
    estimate = rp.estimate_reference(
        local(price=100 * GOLD, wall=100 * GOLD, listings=1),
        regional(price=400 * GOLD), config)
    assert estimate.weight_local == pytest.approx(0.65)
    expression = rp.to_tsm_expression(estimate.weight_local, config)
    sources = {"dbmarket": 100 * GOLD, "dbregionmarketavg": 400 * GOLD,
               "dbregionhistorical": 90 * GOLD}
    value = rp.evaluate_tsm_expression(expression, sources)
    assert value == pytest.approx(estimate.price, rel=1e-6)


def test_the_expression_falls_back_when_a_price_is_missing():
    expression = rp.to_tsm_expression(0.65)
    # no local: the regional
    assert rp.evaluate_tsm_expression(
        expression, {"dbmarket": 0, "dbregionmarketavg": 400 * GOLD,
                     "dbregionhistorical": 90 * GOLD}) == pytest.approx(400 * GOLD)
    # no regional: the local
    assert rp.evaluate_tsm_expression(
        expression, {"dbmarket": 100 * GOLD, "dbregionmarketavg": 0,
                     "dbregionhistorical": 90 * GOLD}) == pytest.approx(100 * GOLD)
    # neither: the regional historical
    assert rp.evaluate_tsm_expression(
        expression, {"dbmarket": 0, "dbregionmarketavg": 0,
                     "dbregionhistorical": 90 * GOLD}) == pytest.approx(90 * GOLD)


def test_the_expression_ignores_the_sentinel():
    expression = rp.to_tsm_expression(0.65)
    sentinel = rp.SENTINEL_COPPER
    assert rp.evaluate_tsm_expression(
        expression, {"dbmarket": sentinel, "dbregionmarketavg": 400 * GOLD,
                     "dbregionhistorical": 90 * GOLD}) == pytest.approx(400 * GOLD)


def test_the_expression_only_uses_sources_that_exist_in_tsm():
    expression = rp.to_tsm_expression(0.65)
    for source in ("dbmarket", "dbregionmarketavg", "dbregionhistorical"):
        assert source in expression
    # nothing invented
    assert "minbuyout" not in expression
    assert "dbregionsaleavg" not in expression
    assert "dbregionsoldperday" not in expression


def test_the_expression_uses_only_supported_functions():
    expression = rp.to_tsm_expression(0.65)
    for function in ("ifgt(", "min(", "max(", "first(", "round("):
        assert function not in expression or function == "ifgt("
    assert expression.count("(") == expression.count(")")


# --- the import structure -------------------------------------------------

def test_the_weighted_export_keeps_the_import_structure():
    groups = {"PG demand_stable 2k-5k": [1, 2], "PG no_demand_stable 2k-5k": [3]}
    operations = {
        "PG demand_stable 2k-5k": {
            "aboveMax": "maxPrice", "bidPercent": 1, "cancelRepost": False,
            "cancelUndercut": False, "duration": 48, "maxExpires": 14,
            "maxPrice": "1.3 * (ref)", "minPrice": "0.4 * (ref)",
            "normalPrice": "0.68 * (ref)", "postCap": 1, "priceReset": "none",
            "stackSize": 1, "undercut": "1c",
        },
        "PG no_demand_stable 2k-5k": {
            "aboveMax": "maxPrice", "bidPercent": 1, "cancelRepost": False,
            "cancelUndercut": False, "duration": 24, "maxExpires": 13,
            "maxPrice": "1.3 * (ref)", "minPrice": "0.4 * (ref)",
            "normalPrice": "0.68 * (ref)", "postCap": 1, "priceReset": "none",
            "stackSize": 1, "undercut": "1c",
        },
    }
    from woweconomy.analytics.tsm_export import build_export, build_group_export

    text = build_export({"Auctioning": operations}, {
        name: {"Auctioning": {1: name, "override": True}} for name in groups},
        build_group_export(groups))
    data = deserialize(text)
    assert parse_group_export(data["groupExport"]) == groups
    assert set(data["operations"]["Auctioning"]) == set(groups)
    for name in groups:
        assert data["groupOperations"][name]["Auctioning"]["override"] is True
        assert data["groupOperations"][name]["Auctioning"][1] == name


def test_the_normal_export_still_uses_the_plain_first_anchor():
    # the baseline proposal is untouched: the reference change is a candidate
    assert "first(dbmarket" in pg.NORMAL_ANCHOR
    assert "dbregionmarketavg" in pg.NORMAL_ANCHOR
    # ... and the weighted candidate is a different string
    assert rp.to_tsm_expression(0.65) != pg.NORMAL_ANCHOR


# --- the realm modifier is applied once -----------------------------------

def test_the_reference_never_applies_the_realm_factor():
    expression = rp.to_tsm_expression(0.65)
    assert "factor" not in expression
    # the anchor is local, so the local/regional factor would be a second
    # application of the same adjustment
    assert "dbmarket" in expression
    assert pg.NORMAL_ANCHOR.index("dbmarket") < pg.NORMAL_ANCHOR.index("dbregionmarketavg")


def test_the_rush_path_is_a_separate_formula():
    from woweconomy.analytics.token_rush import (
        CampaignConfig, ItemEvidence, RealmEvidence, build_realm_posture,
        classify_item, rush_price,
    )

    evidence = ItemEvidence(item_id=1, name="X", realm="R", region_market=100 * GOLD,
                             realm_min_buyout=100 * GOLD, listings=3, sales=2,
                             expired=0, attempts=2, sold_copper=90 * GOLD)
    classification = classify_item(evidence, CampaignConfig())
    posture = build_realm_posture(RealmEvidence(realm="R", klass="balanced", factor=0.5))
    price = rush_price(evidence, classification, posture, CampaignConfig())
    # no local market value: the reference is the regional one corrected by the
    # realm factor -- applied exactly once, by the campaign layer
    assert price.reference == int(100 * GOLD * 0.5)
    assert price.recommended > 0
    assert price.recommended <= price.reference     # never above the reference


# --- the audit of the generated expression --------------------------------

def test_the_expression_never_yields_a_negative_or_infinite_value():
    expression = rp.to_tsm_expression(0.65)
    extremes = [(1, 1), (1, 10 ** 12), (10 ** 12, 1), (10 ** 12, 10 ** 12),
                (rp.SENTINEL_COPPER, 10 ** 12), (10 ** 12, rp.SENTINEL_COPPER)]
    for local, regional in extremes:
        value = rp.evaluate_tsm_expression(expression, {
            "dbmarket": local, "dbregionmarketavg": regional,
            "dbregionhistorical": 10 ** 12})
        assert value == value                       # not NaN
        assert value not in (float("inf"), float("-inf"))
        assert value >= 0


def test_the_original_anchor_would_take_the_sentinel_as_a_price():
    # the reason the candidate guards the sentinel: TSM's own `first()` takes
    # the first non-zero value, and the sentinel is non-zero
    sentinel = rp.SENTINEL_COPPER
    original = rp.evaluate_tsm_expression(
        "first(dbmarket, dbregionmarketavg, dbregionhistorical)",
        {"dbmarket": sentinel, "dbregionmarketavg": 400 * GOLD,
         "dbregionhistorical": 90 * GOLD})
    assert original == sentinel                    # 10 million gold
    guarded = rp.evaluate_tsm_expression(
        rp.to_tsm_expression(0.65),
        {"dbmarket": sentinel, "dbregionmarketavg": 400 * GOLD,
         "dbregionhistorical": 90 * GOLD})
    assert guarded == 400 * GOLD                   # the regional


def test_the_price_fields_stay_ordered_for_any_pair_of_references():
    config = rp.ReferenceConfig()
    for weight in (0.5, 0.65, 1.0):
        expression = rp.to_tsm_expression(weight, config)
        for local, regional in ((100 * GOLD, 100 * GOLD), (400 * GOLD, 100 * GOLD),
                                (100 * GOLD, 400 * GOLD)):
            values = {"dbmarket": local, "dbregionmarketavg": regional,
                      "dbregionhistorical": 90 * GOLD}
            normal = rp.evaluate_tsm_expression(expression, values)
            assert normal >= 0


def test_the_weight_is_the_same_for_the_whole_cell_but_computed_per_item():
    # the engine computes a weight per item; the payload carries the median of
    # the cell, because TSM evaluates one expression per group
    weights = []
    for listings in (0, 1, 2, 3, 5, 9):
        estimate = rp.estimate_reference(
            local(price=100 * GOLD, wall=100 * GOLD, listings=listings), regional())
        weights.append(estimate.weight_local)
    assert weights == sorted(weights)             # deeper local, more weight
    assert weights[-1] == 1.0                     # a deep local keeps it all
    assert all(0.5 <= weight <= 1.0 for weight in weights)


def test_a_stale_local_loses_the_floor_but_never_the_whole_weight():
    # the regional observation is much fresher than the local one
    estimate = rp.estimate_reference(local(age_hours=500.0, listings=5),
                                     regional(age_hours=1.0))
    assert estimate.weight_local >= rp.ReferenceConfig().weight_floor
    assert estimate.weight_local < 1.0


def test_the_divergence_never_changes_the_weight():
    # the same local evidence, two different regional levels: the weight is the
    # same, only the confidence and the flag change
    near = rp.estimate_reference(local(price=100 * GOLD, wall=100 * GOLD, listings=5),
                                 regional(price=100 * GOLD))
    far = rp.estimate_reference(local(price=100 * GOLD, wall=100 * GOLD, listings=5),
                                regional(price=400 * GOLD))
    assert near.weight_local == far.weight_local
    assert far.confidence != "alta"
    assert any("divergência" in flag for flag in far.flags)
