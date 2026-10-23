"""Tests for the evidence-based pricing engine.

The rules under test are the ones the data forced:

* a missing, invalid or sentinel price is rejected with a reason and never
  becomes zero;
* correlated TSM sources are one family, not independent observations;
* a very low average sale price is evidence, not noise: it lowers the
  estimate and lowers the confidence, and it is never discarded because it is
  unattractive;
* a high price with compatible, corroborated sales is **not** lowered;
* the liquidity model is separate from the price and never multiplies it;
* the weights come from a measurement, and an uncalibrated family does not
  inherit a weight it has not earned.
"""
import math

import pytest

from woweconomy.analytics import pricing_engine as pe

GOLD = pe.COPPER_PER_GOLD


def source(name, value, **kwargs):
    base = dict(name=name, value=value, family=kwargs.pop("family", pe.FAMILY_LOCAL_ASK),
                kind=kwargs.pop("kind", pe.ASKING),
                scope=kwargs.pop("scope", pe.LOCAL))
    base.update(kwargs)
    return pe.SourceValue(**base)


def evidence(sources=None, **kwargs):
    base = dict(item_id=1, name="X", realm="R", band="2k-5k", qty=1)
    base.update(kwargs)
    return pe.Evidence(sources=list(sources or []), **base)


# --- missing, invalid and sentinel values ---------------------------------

def test_a_missing_value_is_rejected_and_never_becomes_zero():
    ev = evidence([source("dbmarket", 0), source("dbregionmarketavg", 100 * GOLD,
                                                  family=pe.FAMILY_REGIONAL_ASK)])
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    assert estimate.price == 100 * GOLD
    assert any("dbmarket" in r["name"] and "ausente" in r["reason"]
               for r in estimate.rejected)


def test_the_sentinel_is_rejected_with_its_reason():
    ev = evidence([source("dbmarket", pe.SENTINEL_COPPER),
                   source("dbregionmarketavg", 100 * GOLD,
                          family=pe.FAMILY_REGIONAL_ASK)])
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    assert estimate.price == 100 * GOLD
    assert any("sentinel" in r["reason"] for r in estimate.rejected)


def test_a_negative_or_non_finite_value_is_rejected():
    ev = evidence([source("dbmarket", -5), source("dbregionmarketavg", float("inf"),
                                                  family=pe.FAMILY_REGIONAL_ASK),
                   source("dbregionsaleavg", 50 * GOLD,
                          family=pe.FAMILY_REGIONAL_SALES)])
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    assert estimate.price == 50 * GOLD
    reasons = " ".join(r["reason"] for r in estimate.rejected)
    assert "negativo" in reasons or "zero" in reasons
    assert "finito" in reasons


def test_no_valid_source_means_no_estimate():
    ev = evidence([source("dbmarket", 0)])
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    assert estimate.price is None
    assert estimate.usable is False
    assert estimate.confidence == 0.0


# --- correlated evidence ---------------------------------------------------

def test_the_four_local_ask_sources_are_one_family_not_four():
    # the same level repeated four times must not quadruple the evidence
    one = evidence([source("dbmarket", 100 * GOLD)])
    four = evidence([source("dbmarket", 100 * GOLD),
                     source("dbrecent", 100 * GOLD),
                     source("dbhistorical", 100 * GOLD),
                     source("dbminbuyout", 100 * GOLD)])
    config = pe.EngineConfig()
    a = pe.estimate_value(one, config)
    b = pe.estimate_value(four, config)
    assert a.price == b.price
    # and the family is reduced to one value, not four
    assert len(b.contributions) == 1
    assert b.contributions[0]["members"] == ["dbmarket", "dbrecent", "dbhistorical",
                                            "dbminbuyout"]


def test_asking_and_transaction_families_are_not_mixed_blindly():
    # a low sale average must not be averaged with an inflated asking price
    ev = evidence([
        source("dbmarket", 10_000 * GOLD),
        source("dbregionmarketavg", 12_000 * GOLD, family=pe.FAMILY_REGIONAL_ASK),
        source("dbregionsaleavg", 2_000 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ])
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    # the transaction decides: the estimate is the realized level, not 8 000
    assert estimate.price == 2_000 * GOLD
    assert estimate.basis == "transacções"


def test_a_family_that_was_never_calibrated_does_not_inherit_a_weight():
    calibration = pe.Calibration(family_weight={pe.FAMILY_REGIONAL_SALES: 0.8},
                                 sample=10)
    assert calibration.weight_of(pe.FAMILY_REGIONAL_SALES) == 0.8
    assert calibration.weight_of(pe.FAMILY_LOCAL_ASK) == calibration.default_weight


# --- a very low sale average ----------------------------------------------

def test_a_very_low_sale_average_is_kept_and_lowers_the_estimate():
    ev = evidence([
        source("dbmarket", 10_000 * GOLD),
        source("dbregionmarketavg", 12_000 * GOLD, family=pe.FAMILY_REGIONAL_ASK),
        source("dbregionsaleavg", 200 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ], market_clearance=0.05, market_clearance_n=5_000)
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    assert estimate.price == 200 * GOLD
    assert any("inflacionado" in h for h in estimate.hypotheses)


def test_a_high_price_with_compatible_sales_is_not_lowered():
    ev = evidence([
        source("dbmarket", 100_000 * GOLD),
        source("dbregionmarketavg", 100_000 * GOLD, family=pe.FAMILY_REGIONAL_ASK),
        source("dbregionsaleavg", 95_000 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ], market_clearance=0.95, market_clearance_n=5_000)
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    assert estimate.price == 95_000 * GOLD
    assert any("defensável" in h or "acima do mercado" in h
               for h in estimate.hypotheses)


def test_the_confidence_falls_when_the_ask_and_the_sales_disagree():
    config = pe.EngineConfig()
    compatible = pe.estimate_value(evidence([
        source("dbmarket", 100 * GOLD),
        source("dbregionsaleavg", 98 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ], market_clearance=0.98, market_clearance_n=5_000), config)
    divergent = pe.estimate_value(evidence([
        source("dbmarket", 100 * GOLD),
        source("dbregionsaleavg", 10 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ], market_clearance=0.1, market_clearance_n=5_000), config)
    assert divergent.confidence < compatible.confidence
    assert any("discrepância" in flag for flag in divergent.flags)


# --- samples, depth and dispersion ----------------------------------------

def test_a_small_sample_weighs_less_than_a_substantial_one():
    config = pe.EngineConfig()
    small = pe.estimate_value(evidence([
        source("venda própria", 100 * GOLD, family=pe.FAMILY_OWN_SALES,
               kind=pe.TRANSACTION, sample_size=1),
    ]), config)
    large = pe.estimate_value(evidence([
        source("venda própria", 100 * GOLD, family=pe.FAMILY_OWN_SALES,
               kind=pe.TRANSACTION, sample_size=20),
    ]), config)
    assert small.contributions[0]["weight"] < large.contributions[0]["weight"]


def test_a_thin_market_weighs_less_than_a_deep_one():
    config = pe.EngineConfig()
    thin = pe.estimate_value(evidence([
        source("dbmarket", 100 * GOLD, depth=0),
        source("dbregionsaleavg", 90 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ]), config)
    deep = pe.estimate_value(evidence([
        source("dbmarket", 100 * GOLD, depth=9),
        source("dbregionsaleavg", 90 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ]), config)
    assert thin.contributions[0]["weight"] < deep.contributions[0]["weight"]


def test_a_dispersed_family_weighs_less_than_a_consistent_one():
    config = pe.EngineConfig()
    spread = pe.estimate_value(evidence([
        source("dbmarket", 100 * GOLD, dispersion=2.0),
        source("dbrecent", 300 * GOLD, dispersion=2.0),
    ]), config)
    tight = pe.estimate_value(evidence([
        source("dbmarket", 100 * GOLD, dispersion=0.05),
        source("dbrecent", 102 * GOLD, dispersion=0.05),
    ]), config)
    assert spread.contributions[0]["weight"] < tight.contributions[0]["weight"]


def test_a_stale_observation_weighs_less_than_a_fresh_one():
    config = pe.EngineConfig()
    stale = pe.estimate_value(evidence([
        source("dbregionsaleavg", 100 * GOLD, family=pe.FAMILY_REGIONAL_SALES,
               age_hours=240),
    ]), config)
    fresh = pe.estimate_value(evidence([
        source("dbregionsaleavg", 100 * GOLD, family=pe.FAMILY_REGIONAL_SALES,
               age_hours=0),
    ]), config)
    assert stale.contributions[0]["weight"] < fresh.contributions[0]["weight"]


def test_a_zero_weight_is_refused():
    class NoTrust(pe.Calibration):
        def weight_of(self, family):
            return 0.0

    ev = evidence([source("dbmarket", 100 * GOLD),
                   source("dbregionsaleavg", 90 * GOLD, family=pe.FAMILY_REGIONAL_SALES)])
    estimate = pe.estimate_value(ev, pe.EngineConfig(), NoTrust())
    assert estimate.price is None
    assert any("peso nulo" in r["reason"] for r in estimate.rejected)


# --- the liquidity model ---------------------------------------------------

def test_the_liquidity_model_never_touches_the_price():
    ev = evidence([
        source("dbmarket", 100 * GOLD, depth=5),
        source("dbregionsaleavg", 90 * GOLD, family=pe.FAMILY_REGIONAL_SALES,
               volume=5.0),
    ], qty=100, own_sales=4, own_expired=1, own_sell_through=0.8)
    liquidity = pe.assess_liquidity(ev)
    assert liquidity.level in (pe.LIQUIDITY_HIGH, pe.LIQUIDITY_MEDIUM)
    assert liquidity.velocity == 5.0
    assert liquidity.observed_sell_through == pytest.approx(0.8)
    # the volume is regional and the coverage says so
    assert any("regional" in flag for flag in liquidity.flags)
    assert liquidity.stock_coverage_days == pytest.approx(20.0)


def test_missing_demand_and_velocity_leave_the_liquidity_unknown():
    ev = evidence([source("dbmarket", 100 * GOLD)])
    liquidity = pe.assess_liquidity(ev)
    assert liquidity.level == pe.LIQUIDITY_UNKNOWN
    assert liquidity.velocity is None
    assert liquidity.stock_coverage_days is None
    assert any("procura" in flag for flag in liquidity.flags)


def test_the_regional_volume_never_becomes_a_local_promise():
    ev = evidence([source("dbregionsaleavg", 90 * GOLD, family=pe.FAMILY_REGIONAL_SALES,
                          volume=10.0)], qty=50)
    liquidity = pe.assess_liquidity(ev)
    assert any("não promete venda local" in flag for flag in liquidity.flags)


# --- the listing recommendation -------------------------------------------

def test_insufficient_evidence_returns_the_explicit_marker():
    ev = evidence([source("dbmarket", 100 * GOLD)])
    value = pe.ValueEstimate(None, None, None, 0.0)
    liquidity = pe.assess_liquidity(ev)
    listing = pe.recommend_listing(value, liquidity, ev)
    assert listing.price is None
    assert listing.reason == pe.INSUFFICIENT_EVIDENCE


def test_a_low_confidence_estimate_is_recommended_at_the_conservative_end():
    ev = evidence([source("dbmarket", 100 * GOLD)])
    value = pe.ValueEstimate(100 * GOLD, 70 * GOLD, 130 * GOLD, 0.1, "transacções")
    listing = pe.recommend_listing(value, pe.assess_liquidity(ev), ev)
    # not a refusal: the uncertainty is expressed in the price
    assert listing.price == 70 * GOLD
    assert "confiança baixa" in listing.reason
    assert listing.reason != pe.INSUFFICIENT_EVIDENCE


def test_an_item_without_any_estimate_is_never_recommended():
    ev = evidence([source("dbmarket", 100 * GOLD)])
    value = pe.ValueEstimate(None, None, None, 0.0)
    listing = pe.recommend_listing(value, pe.assess_liquidity(ev), ev)
    assert listing.reason == pe.INSUFFICIENT_EVIDENCE


def test_the_recommendation_separates_gross_from_net_and_marks_the_costs():
    ev = evidence([source("dbregionsaleavg", 100 * GOLD, family=pe.FAMILY_REGIONAL_SALES)],
                  qty=2, vendor_sell=1 * GOLD)
    value = pe.ValueEstimate(100 * GOLD, 90 * GOLD, 110 * GOLD, 0.8, "transacções")
    liquidity = pe.assess_liquidity(ev)
    listing = pe.recommend_listing(value, liquidity, ev)
    assert listing.price == 100 * GOLD
    assert listing.gross_proceeds == 200 * GOLD
    assert listing.net_proceeds == int(round(200 * GOLD * 0.95))
    assert listing.costs_assumed is True
    assert any("assumidos" in flag for flag in listing.flags)


def test_the_recommendation_never_goes_below_the_vendor_price():
    ev = evidence([source("dbregionsaleavg", 10 * GOLD, family=pe.FAMILY_REGIONAL_SALES)],
                  vendor_sell=50 * GOLD)
    value = pe.ValueEstimate(10 * GOLD, 9 * GOLD, 11 * GOLD, 0.8, "transacções")
    listing = pe.recommend_listing(value, pe.assess_liquidity(ev), ev)
    assert listing.price == 50 * GOLD
    assert "fornecedor" in listing.reason


def test_a_liquid_item_is_listed_at_the_estimate_and_a_illiquid_one_below():
    config = pe.EngineConfig()
    liquid = pe.assess_liquidity(evidence(
        [source("dbregionsaleavg", 100 * GOLD, family=pe.FAMILY_REGIONAL_SALES,
                volume=2.0)],
        want_rate=0.2, demand_known=True, own_sales=8, own_expired=0,
        own_sell_through=1.0))
    value = pe.ValueEstimate(100 * GOLD, 90 * GOLD, 110 * GOLD, 0.8, "transacções")
    assert pe.recommend_listing(value, liquid, evidence(), config).price == 100 * GOLD
    thin = pe.assess_liquidity(evidence(
        [source("dbregionsaleavg", 100 * GOLD, family=pe.FAMILY_REGIONAL_SALES)],
        want_rate=0.001, demand_known=True, listings=9))
    assert pe.recommend_listing(value, thin, evidence(), config).price < 100 * GOLD


# --- reproducibility -------------------------------------------------------

def test_the_estimate_is_reproducible():
    ev = evidence([
        source("dbmarket", 100 * GOLD, depth=3),
        source("dbregionmarketavg", 120 * GOLD, family=pe.FAMILY_REGIONAL_ASK),
        source("dbregionsaleavg", 90 * GOLD, family=pe.FAMILY_REGIONAL_SALES,
               volume=1.0),
    ], market_clearance=0.8, market_clearance_n=1000)
    config = pe.EngineConfig()
    calibration = pe.Calibration(family_weight={pe.FAMILY_REGIONAL_SALES: 0.8},
                                 family_error={pe.FAMILY_REGIONAL_SALES: 0.2},
                                 coverage={pe.FAMILY_REGIONAL_SALES: 1.0},
                                 measured_at="2026-10-19T00:00:00Z", sample=100)
    first = pe.estimate_value(ev, config, calibration)
    second = pe.estimate_value(ev, config, calibration)
    assert first.price == second.price
    assert first.confidence == second.confidence
    assert first.explanation == second.explanation
    assert calibration.as_dict()["measured_at"] == "2026-10-19T00:00:00Z"


def test_the_provenance_lists_every_source_used_and_rejected():
    ev = evidence([
        source("dbmarket", 100 * GOLD, depth=3),
        source("dbrecent", 0),
        source("dbregionsaleavg", 90 * GOLD, family=pe.FAMILY_REGIONAL_SALES),
    ])
    estimate = pe.estimate_value(ev, pe.EngineConfig())
    used = [name for c in estimate.contributions for name in c["members"]]
    assert "dbmarket" in used and "dbregionsaleavg" in used
    assert any(r["name"] == "dbrecent" for r in estimate.rejected)
    assert estimate.explanation
