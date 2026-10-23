"""Tests for the pricing-group discovery engine.

The rules under test are the ones that keep the analysis honest:

* an observed statistic never turns into a policy by accident;
* the two clearance estimators stay apart, and E2 is never faked;
* a group classification never silently becomes a price discount;
* missing data stays missing;
* the normal and the liquidation formulas stay separate;
* the realm factor is not counted twice.
"""
import sqlite3

import pytest

from woweconomy.analytics import pricing_groups as pg


GOLD = pg.COPPER_PER_GOLD


def evidence(p25=None, p50=None, p75=None, n=0, own_sales=0, own_clearance=None):
    e1 = pg.Clearance(key="E1", p25=p25, p50=p50, p75=p75, n=n,
                      source=pg.E1_SOURCE, caveat=pg.E1_CAVEAT, usable=bool(n))
    e2 = pg.Clearance(key="E2", n=0, source=pg.E2_SOURCE, caveat=pg.E2_CAVEAT,
                      usable=False, note=pg.E2_UNAVAILABLE_NOTE)
    return pg.BandEvidence(band="2k-5k", n_items=100, e1=e1, e2=e2,
                            own_sales=own_sales, own_clearance_p50=own_clearance)


# --- bands ----------------------------------------------------------------

def test_band_edges_are_a_partition():
    labels = [pg.band_of(value) for value in (1, 99, 100, 499, 500, 1999, 2000,
                                              4999, 5000, 14999, 15000, 49999,
                                              50000, 149999, 150000, 499999,
                                              500000, 500001)]
    assert len(set(labels)) == len(pg.band_order())
    assert labels[0] == "<100"
    assert labels[-1] == ">500k"


def test_a_sparse_band_is_folded_into_its_neighbour_and_flagged():
    data = {
        "<100": evidence(p50=0.2, n=5_000),
        "100-500": evidence(p50=0.2, n=10),
        "500-2k": evidence(p50=0.1, n=4_000),
    }
    merged, flags = pg.merge_sparse_bands(data)
    assert any("100-500" in flag for flag in flags)
    assert "100-500" not in merged
    # the kept band keeps its own statistics (averaging percentiles of two
    # populations would be meaningless)
    assert merged["500-2k"].e1.n == 4_000


# --- statistics vs policy -------------------------------------------------

def test_the_clearance_statistics_are_kept_apart_from_the_policy():
    e = evidence(p25=0.04, p50=0.12, p75=0.40, n=100_000)
    policy = pg.BandPolicy(band="2k-5k", normal=0.68, minimum=0.40, maximum=1.30,
                            source="modelo actual")
    # the statistics are what was measured
    assert e.e1.p25 == 0.04 and e.e1.p50 == 0.12 and e.e1.p75 == 0.40
    # the policy is what was decided, and says so
    assert policy.source == "modelo actual"
    assert policy.normal == 0.68


def test_the_policy_floors_are_policy_not_statistics():
    # a clearance of 0.02 would post at 2% of the ask; the floor is a decision
    e = evidence(p25=0.005, p50=0.02, p75=0.2, n=50_000)
    normal, minimum, maximum, spread = pg.band_coefficients(e)
    assert normal == pg.FLOOR_NORMAL
    assert minimum == pg.FLOOR_MINIMUM
    assert normal > e.e1.p50           # the floor is above the measurement
    assert "p50" in spread             # and the spread travels with it
    # the floors are parameters
    relaxed, _, _, _ = pg.band_coefficients(e, floor_normal=0.01, floor_minimum=0.005)
    assert relaxed == pytest.approx(0.02)


# --- the two estimators ---------------------------------------------------

def test_e2_is_not_faked_when_there_are_no_dated_sales():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tsm_snapshot (snapshot_id INTEGER, kind TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE tsm_price_change (kind TEXT, scope TEXT, key_id INTEGER, "
                "snapshot_id INTEGER, min_buyout INTEGER)")
    estimate = pg.estimate_e2(con, [(1, "spinebreaker", 1000, None)])
    assert estimate.usable is False
    assert estimate.n == 0
    # the reason is reported: no dates, so no wall at the moment of the sale
    assert "parede" in estimate.note.lower()
    assert "data" in estimate.note.lower()


def test_e2_uses_the_wall_of_the_moment_and_not_the_current_one():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tsm_snapshot (snapshot_id INTEGER, kind TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE tsm_price_change (kind TEXT, scope TEXT, key_id INTEGER, "
                "snapshot_id INTEGER, min_buyout INTEGER)")
    con.execute("INSERT INTO tsm_snapshot VALUES (10, 'realm_items', '2026-10-16T00:00:00Z')")
    con.execute("INSERT INTO tsm_snapshot VALUES (20, 'realm_items', '2026-10-19T00:00:00Z')")
    con.execute("INSERT INTO tsm_price_change VALUES "
                "('realm_items', 'spinebreaker', 42, 10, 1000)")
    con.execute("INSERT INTO tsm_price_change VALUES "
                "('realm_items', 'spinebreaker', 42, 20, 5000)")
    estimate = pg.estimate_e2(con, [(42, "spinebreaker", 2000, "2026-10-17T00:00:00Z")])
    assert estimate.usable is True
    assert estimate.n == 1
    # the wall at the moment of the sale was 1000, not the current 5000
    assert estimate.p50 == pytest.approx(2.0)


def test_the_e1_caveat_travels_with_the_estimate():
    e = evidence(p50=0.1, n=1000)
    assert e.e1.caveat == pg.E1_CAVEAT
    assert "snipes" in e.e1.caveat


# --- classification -------------------------------------------------------

def facts(**kwargs):
    base = dict(item_id=1, region_market=5_000 * GOLD, local_wall=5_000 * GOLD)
    base.update(kwargs)
    return pg.ItemFacts(**base)


def test_a_missing_demand_is_unknown_and_not_a_zero():
    item = facts(want_rate=None, demand_known=False, decay_78d=0.5)
    group = pg.assign_group(item)
    assert group.startswith("unknown_")
    # the same item, with the demand measured and low, is a different group
    item = facts(want_rate=0.01, demand_known=True, decay_78d=0.5)
    assert pg.assign_group(item).startswith("no_demand_")


def test_the_demand_axis_separates_the_groups():
    high = facts(want_rate=0.4, demand_known=True, decay_78d=1.0)
    low = facts(want_rate=0.01, demand_known=True, decay_78d=1.0)
    assert pg.assign_group(high).startswith("demand_")
    assert pg.assign_group(low).startswith("no_demand_")


def test_a_commodity_and_an_item_without_a_price_are_their_own_groups():
    assert pg.assign_group(facts(is_commodity=True)) == "commodity"
    assert pg.assign_group(pg.ItemFacts(item_id=2)) == "no_reference"


def test_the_group_assignment_never_looks_at_the_price():
    # the same behaviour, ten times the price: the same group
    cheap = facts(want_rate=0.4, demand_known=True, decay_78d=1.0,
                  region_market=100 * GOLD, local_wall=100 * GOLD)
    dear = facts(want_rate=0.4, demand_known=True, decay_78d=1.0,
                 region_market=100_000 * GOLD, local_wall=100_000 * GOLD)
    assert pg.assign_group(cheap) == pg.assign_group(dear)


# --- the group adjustment -------------------------------------------------

def test_without_evidence_the_adjustment_is_one_and_labelled():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.2, 5_000, 0.1)
    assert adjustment.multiplier == 1.0
    assert adjustment.material is False
    assert "heurístico" in adjustment.basis
    # the measured ratio is still reported
    assert adjustment.measured_ratio == pytest.approx(2.0)


def test_the_measured_ratio_is_not_applied_because_e1_is_not_identified():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.05, 5_000, 0.1)
    assert adjustment.multiplier == 1.0
    assert "E1" in adjustment.basis


def test_only_the_accounts_own_sales_can_move_the_price():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.05, 5_000, 0.1,
                                         own_sales=30, own_clearance=0.25)
    assert adjustment.multiplier == pytest.approx(2.5)
    assert adjustment.material is True
    assert "própria conta" in adjustment.basis


def test_a_small_sample_never_moves_the_price():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.05, 12, 0.1)
    assert adjustment.multiplier == 1.0
    assert "amostra" in adjustment.basis


# --- distinctness ---------------------------------------------------------

def stat(group, sold, n=1_000):
    return pg.GroupStatistics(group=group, band="2k-5k", n_items=n, n_with_sales=n,
                               sold_per_day_p50=sold)


def test_groups_with_the_same_turnover_are_not_distinct():
    verdict, reason = pg.distinctness(stat("a", 0.01), stat("b", 0.011))
    assert verdict == "same"
    assert "quase igual" in reason


def test_groups_with_different_turnover_are_distinct():
    verdict, reason = pg.distinctness(stat("a", 0.01), stat("b", 0.5))
    assert verdict == "distinct"
    assert "diferente" in reason


def test_a_thin_sample_is_inconclusive_and_never_a_merge():
    verdict, reason = pg.distinctness(stat("a", 0.01, n=10), stat("b", 0.5, n=10))
    assert verdict == "unknown"
    assert "amostra" in reason


# --- the strategy ---------------------------------------------------------

def policy(band="2k-5k"):
    return pg.BandPolicy(band=band, normal=0.68, minimum=0.40, maximum=1.30,
                          source="modelo actual", confidence="média")


def test_the_normal_and_the_liquidation_formulas_are_separate():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.1, 1_000, 0.1)
    strategy = pg.build_strategy("demand_stable", policy(), adjustment)
    assert "NORMAL_ANCHOR" not in strategy.normal_formula
    assert strategy.normal_formula.startswith("0.680")
    assert "parede_local" in strategy.rush_formula
    assert "undercut" in strategy.rush_formula


def test_the_realm_factor_is_not_applied_twice():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.1, 1_000, 0.1)
    strategy = pg.build_strategy("demand_stable", policy(), adjustment)
    # the anchor is local (dbmarket): the local/regional factor must not appear
    assert "dbmarket" in strategy.normal_formula
    assert "factor_do_reino" not in strategy.normal_formula
    # ... the strategy is per group, not per realm: no realm factor anywhere
    assert "factor_do_reino" not in strategy.rush_formula
    # the campaign layer is where the realm factor lives (once)


def test_the_group_adjustment_never_reaches_the_wall_target():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.05, 1_000, 0.1,
                                         own_sales=30, own_clearance=0.25)
    strategy = pg.build_strategy("demand_stable", policy(), adjustment)
    # the wall target is the wall minus the undercut: no group multiplier
    assert "parede_local * (1 - undercut" in strategy.rush_formula
    assert f"{adjustment.multiplier:g} * parede_local" not in strategy.rush_formula


def test_a_group_without_a_price_cannot_be_posted():
    adjustment = pg.estimate_adjustment("no_reference", "2k-5k", None, 0, None)
    strategy = pg.build_strategy("no_reference", policy(), adjustment)
    assert strategy.postable is False


def test_the_strategy_reports_its_own_confidence():
    adjustment = pg.estimate_adjustment("demand_stable", "2k-5k", 0.1, 1_000, 0.1)
    strategy = pg.build_strategy("demand_stable", policy(), adjustment)
    assert strategy.confidence in ("nenhuma", "baixa", "média", "alta")


# --- the TSM semantics the model relies on -------------------------------

def test_the_documented_tsm_semantics_match_the_project():
    from woweconomy.analytics.mog_operations import build_mog_export
    from woweconomy.analytics.tsm_export import deserialize

    data = deserialize(build_mog_export({"liquid": [1]}))
    operation = data["operations"]["Auctioning"]["Mogs Liquid"]
    assert operation["priceReset"] == "none"       # below the minimum: do not post
    assert operation["aboveMax"] == "maxPrice"     # above the maximum: post at the cap
    assert operation["postCap"] == 1               # one auction per item
    assert operation["maxExpires"] >= 1            # holding window, not a duration
    assert operation["duration"] in (12, 24, 48)
    assert operation["undercut"] in ("0c", "1c")
    # the anchor is never the realm's own market
    for field in ("normalPrice", "minPrice", "maxPrice"):
        assert "dbmarket" not in operation[field].replace("dbregionmarketavg", "")


def test_the_anchor_of_the_normal_strategy_is_local():
    assert "dbmarket" in pg.NORMAL_ANCHOR
    assert pg.NORMAL_ANCHOR.index("dbmarket") < pg.NORMAL_ANCHOR.index("dbregionmarketavg")
