"""Tests for the token rush liquidation engine.

The engine has to be conservative: a price without evidence is a guess, a
listing that disappeared is not a sale, and a realm with little competition is
not a realm with demand.  These tests pin those rules down.
"""
import pytest

from woweconomy.analytics.mog_operations import (
    LiquidityOperation, build_liquidity_export,
)
from woweconomy.analytics.token_rush import (
    COPPER_PER_GOLD, COMP_HIGH, COMP_LOW, COMP_MID, COMP_UNKNOWN,
    PROFILE_LIQUID, PROFILE_PROVEN, PROFILE_SPECULATIVE, PROFILE_STALE,
    PROFILE_UNKNOWN, CampaignConfig, ItemEvidence, RealmEvidence,
    build_realm_posture, campaign_state, checkpoints, classify_item,
    deposit_for, feedback_from_journal, observed_sell_through, prioritise,
    rush_price,
)
from woweconomy.analytics.tsm_export import deserialize, parse_group_export


GOLD = COPPER_PER_GOLD


def config(**kwargs) -> CampaignConfig:
    base = dict(current_gold=213_246 * GOLD, token_price=398_720 * GOLD,
                token_price_safe=420_000 * GOLD, target_gold=200_000 * GOLD)
    base.update(kwargs)
    return CampaignConfig(**base)


def evidence(**kwargs) -> ItemEvidence:
    base = dict(item_id=1, name="Test Item", qty=1, realm="TestRealm")
    base.update(kwargs)
    return ItemEvidence(**base)


def posture(**kwargs):
    base = dict(realm="TestRealm", canonical="TestRealm", klass="balanced", factor=1.0)
    base.update(kwargs)
    return build_realm_posture(RealmEvidence(**base))


# --- classification -------------------------------------------------------

def test_realized_sales_without_expirations_are_liquid():
    ev = evidence(sales=3, expired=0, attempts=3, sold_copper=300 * GOLD)
    result = classify_item(ev, config())
    assert result.profile == PROFILE_LIQUID
    assert result.confidence == "observada"
    assert any("3 venda" in r for r in result.reasons)


def test_sales_with_many_expirations_are_proven_but_weak():
    ev = evidence(sales=2, expired=8, attempts=10)
    result = classify_item(ev, config())
    assert result.profile == PROFILE_PROVEN
    assert result.sell_through == pytest.approx(0.2)
    assert any("expirações" in r for r in result.reasons)


def test_expirations_without_a_sale_are_stale():
    ev = evidence(sales=0, expired=4, attempts=4)
    result = classify_item(ev, config())
    assert result.profile == PROFILE_STALE
    assert result.confidence == "observada"


def test_no_evidence_at_all_stays_unknown():
    ev = evidence()
    result = classify_item(ev, config())
    assert result.profile == PROFILE_UNKNOWN
    assert result.confidence == "nenhuma"
    # a missing price is missing, not zero
    assert ev.region_market == 0 and ev.sell_through is None


def test_a_market_value_alone_is_not_demand():
    # a fat ask price with no sales is speculative, never liquid
    ev = evidence(region_market=500_000 * GOLD, region_sold_per_day=0.0)
    result = classify_item(ev, config())
    assert result.profile == PROFILE_SPECULATIVE
    assert any("ask" in r for r in result.reasons)


def test_regional_estimate_is_labelled_as_an_estimate():
    ev = evidence(region_market=100 * GOLD, region_sold_per_day=3.0)
    result = classify_item(ev, config())
    assert result.profile == PROFILE_PROVEN
    assert result.confidence == "estimada"
    assert any("estimativa" in r for r in result.reasons)


def test_evidence_coverage_reflects_what_exists():
    bare = classify_item(evidence(), config())
    rich = classify_item(evidence(sales=2, expired=1, attempts=3,
                                  region_market=100 * GOLD,
                                  realm_market=90 * GOLD, listings=2,
                                  vendor_sell=5), config())
    assert rich.coverage > bare.coverage


# --- realm posture --------------------------------------------------------

def test_thin_realm_with_competition_counts_is_a_warning_not_demand():
    p = posture(klass="thin", listings_median=1.0, has_listing_counts=True)
    assert p.competition == COMP_LOW
    assert any("NÃO é prova de procura" in w for w in p.warnings)


def test_duplicate_metrics_are_flagged_as_one_market():
    p = posture(duplicate_of=("Dragonmaw", "Spinebreaker"))
    assert any("mesmo mercado" in w for w in p.warnings)


def test_unknown_competition_is_not_low_competition():
    p = posture(listings_median=None, has_listing_counts=False)
    assert p.competition == COMP_UNKNOWN
    assert any("indisponível" in w for w in p.warnings)


# --- pricing --------------------------------------------------------------

def test_price_uses_the_local_market_not_the_region():
    ev = evidence(region_market=1000 * GOLD, realm_market=300 * GOLD,
                  realm_min_buyout=300 * GOLD, listings=1)
    price = rush_price(ev, classify_item(ev, config()), posture(), config())
    assert price.reference == 300 * GOLD
    assert price.reference_source.startswith("mercado local")


def test_region_is_used_when_there_is_no_local_price():
    ev = evidence(region_market=1000 * GOLD, realm_market=0)
    p = posture(factor=0.5)
    price = rush_price(ev, classify_item(ev, config()), p, config())
    assert price.reference == 500 * GOLD
    assert any("regional" in f for f in price.flags)


def test_a_listing_far_from_the_reference_is_not_a_comparable():
    ev = evidence(realm_market=100 * GOLD, realm_min_buyout=5 * GOLD, listings=1)
    price = rush_price(ev, classify_item(ev, config()), posture(), config())
    assert price.comparable_valid is False
    assert any("comparável" in f for f in price.flags)


def test_the_floor_is_the_vendor_price_and_never_above_the_reference():
    # a cheap item with a vendor price above the local market is not for sale
    ev = evidence(realm_market=10 * GOLD, realm_min_buyout=10 * GOLD,
                  vendor_sell=50 * GOLD, listings=1)
    price = rush_price(ev, classify_item(ev, config()), posture(), config())
    assert price.recommended == 0
    assert any("piso" in f for f in price.flags)


def test_the_loss_limit_is_a_floor():
    ev = evidence(realm_market=1000 * GOLD, realm_min_buyout=1000 * GOLD,
                  sales=1, attempts=1, sold_copper=1000 * GOLD, listings=1)
    price = rush_price(ev, classify_item(ev, config()), posture(),
                       config(max_loss_pct=0.5), cost_basis=1000 * GOLD)
    assert price.min_price == 500 * GOLD
    assert price.recommended >= price.min_price
    assert any("piso de perda" in a for a in price.adjustments)


def test_the_discount_is_capped():
    ev = evidence(realm_market=1000 * GOLD, realm_min_buyout=1000 * GOLD,
                  sales=1, attempts=1, sold_copper=1000 * GOLD, listings=5)
    price = rush_price(ev, classify_item(ev, config()), posture(),
                       config(max_discount_pct=0.1), cost_basis=1000 * GOLD)
    # the undercut alone would go below 90% of the reference: the cap holds
    assert price.recommended >= 900 * GOLD
    assert any("desconto limitado" in f for f in price.flags)


def test_the_deadline_lowers_the_price():
    ev = evidence(realm_market=1000 * GOLD, realm_min_buyout=1000 * GOLD, listings=1)
    klass = classify_item(ev, config())
    early = rush_price(ev, klass, posture(), config(days=12), days_remaining=12)
    late = rush_price(ev, klass, posture(), config(days=12), days_remaining=0)
    assert late.recommended < early.recommended
    assert late.deadline_factor < early.deadline_factor


def test_two_items_in_the_same_band_get_different_prices():
    # same price band, different demand: one sold before, one never did
    liquid = evidence(item_id=1, realm_market=1000 * GOLD,
                      realm_min_buyout=1000 * GOLD, sales=5, expired=0, attempts=5,
                      listings=4)
    stale = evidence(item_id=2, realm_market=1000 * GOLD,
                     realm_min_buyout=1000 * GOLD, sales=0, expired=6, attempts=6,
                     listings=4)
    p = posture(listings_median=4, has_listing_counts=True)
    a = rush_price(liquid, classify_item(liquid, config()), p, config())
    b = rush_price(stale, classify_item(stale, config()), p, config())
    assert a.recommended != b.recommended
    assert b.recommended < a.recommended


def test_the_deposit_follows_the_vendor_price_cap():
    cfg = config()
    # 5% of the price, capped at 300% of the vendor price
    assert deposit_for(1000 * GOLD, 10 * GOLD, cfg) == 30 * GOLD
    # below the cap the percentage applies
    assert deposit_for(100 * GOLD, 100 * GOLD, cfg) == 5 * GOLD
    # unknown vendor price: the cost is unavailable, not guessed
    assert deposit_for(100 * GOLD, None, cfg) is None


# --- ranking --------------------------------------------------------------

def _row(item_id, **kwargs):
    ev = evidence(item_id=item_id, **kwargs)
    klass = classify_item(ev, config())
    p = posture()
    price = rush_price(ev, klass, p, config())
    from woweconomy.analytics.token_rush import RushRow
    return RushRow(item_id=item_id, name=ev.name, realm=ev.realm, qty=ev.qty,
                   evidence=ev, classification=klass, posture=p, price=price)


def test_the_queue_is_ordered_by_contribution_per_hour():
    big = _row(1, realm_market=100_000 * GOLD, realm_min_buyout=100_000 * GOLD,
               sales=10, expired=0, attempts=10, listings=2)
    small = _row(2, realm_market=10 * GOLD, realm_min_buyout=10 * GOLD,
                 sales=1, expired=0, attempts=1, listings=1)
    rows = prioritise([small, big], config())
    assert rows[0].item_id == 1
    assert rows[0].priority > rows[1].priority


def test_an_expected_value_is_only_produced_from_observations():
    observed = _row(1, realm_market=100 * GOLD, realm_min_buyout=100 * GOLD,
                    sales=3, expired=1, attempts=4, listings=1)
    unknown = _row(2, realm_market=100 * GOLD, realm_min_buyout=100 * GOLD,
                   listings=1)
    rows = {r.item_id: r for r in prioritise([observed, unknown], config())}
    assert rows[1].expected is not None
    assert "observada" in rows[1].expected_basis
    assert rows[2].expected is None
    assert "heurística" in rows[2].expected_basis


def test_an_inactive_realm_loses_priority():
    row = _row(1, realm_market=1000 * GOLD, realm_min_buyout=1000 * GOLD,
               sales=5, expired=0, attempts=5, listings=1)
    active = prioritise([row], config())[0].priority
    row.realm_weight = config().inactive_realm_weight
    inactive = prioritise([row], config())[0].priority
    assert inactive < active


# --- campaign arithmetic --------------------------------------------------

def test_the_deficit_follows_the_token_price():
    cfg = config()
    state = campaign_state(cfg)
    # the shortfall to the token, at the safe price
    assert state.gold_needed == 420_000 * GOLD - 213_246 * GOLD
    # what the campaign has to net is the bigger of the shortfall and the target
    assert state.deficit == max(state.gold_needed, state.target_gold)
    # a higher token means a bigger deficit
    bigger = campaign_state(config(token_price_safe=500_000 * GOLD))
    assert bigger.deficit > state.deficit
    # a bigger target also raises it
    assert campaign_state(config(target_gold=900_000 * GOLD)).deficit == 900_000 * GOLD


def test_the_checkpoints_are_on_days_3_6_9_12():
    points = checkpoints(config())
    assert [p["day"] for p in points] == [3, 6, 9, 12]
    assert points[-1]["days_remaining"] == 0
    assert all(p["required_per_day"] > 0 for p in points)


def test_realized_gold_reduces_the_deficit():
    cfg = config()
    without = campaign_state(cfg)
    with_sales = campaign_state(cfg, realized_gold=50_000 * GOLD)
    assert with_sales.deficit < without.deficit
    assert with_sales.gold_now == without.gold_now + 50_000 * GOLD


# --- feedback -------------------------------------------------------------

def test_feedback_uses_sales_and_not_vanished_listings():
    entries = feedback_from_journal([
        {"itemId": 1, "name": "A", "realm": "R", "sold_count": 2,
         "failures": 2, "sold_copper": 100 * GOLD},
    ])
    assert entries[0].sales == 2 and entries[0].expired == 2
    rates = observed_sell_through(entries)
    assert rates[1] == pytest.approx(0.5)
    # an item with no sales and no expirations has no rate at all
    assert observed_sell_through(feedback_from_journal([
        {"itemId": 2, "name": "B", "realm": "R", "sold_count": 0,
         "failures": 0, "sold_copper": 0}])) == {}


# --- the export -----------------------------------------------------------

def test_the_liquidity_export_has_one_group_and_operation_per_item():
    groups = {"1234 Mogul Belt": [1234], "5678 Old Boot": [5678]}
    operations = {
        "1234 Mogul Belt": LiquidityOperation(normal="100g 0s 0c",
                                               minimum="50g 0s 0c",
                                               maximum="200g 0s 0c"),
        "5678 Old Boot": LiquidityOperation(normal="10g 0s 0c",
                                             minimum="5g 0s 0c",
                                             maximum="20g 0s 0c"),
    }
    data = deserialize(build_liquidity_export(groups, operations))
    parsed = parse_group_export(data["groupExport"])
    assert parsed == groups
    ops = data["operations"]["Auctioning"]
    assert set(ops) == {"Rush 1234 Mogul Belt", "Rush 5678 Old Boot"}
    for name, ids in groups.items():
        op = ops[data["groupOperations"][name]["Auctioning"][1]]
        assert op["priceReset"] == "none"
        assert op["aboveMax"] == "maxPrice"
        assert op["duration"] == 12
        assert op["maxExpires"] == 2
        assert op["postCap"] == 1
