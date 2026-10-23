"""Tests for the per-realm Token Rush group export.

The rules under test:

* the groups are the behaviour groups, and the band enters the price;
* the price is a **live** expression: the measured ratio of the group applied
  to the local market, so it follows the market;
* the minimum is the bottom of the estimate's interval and the maximum its top:
  the three prices are ordered, and the vendor floor holds them;
* the operation is a rush operation: short duration, short holding window, no
  automatic cancel, no post below the floor;
* the export has one group and one operation per cell, with no duplicated
  items.
"""
import pytest

from woweconomy.analytics import mog_realm
from woweconomy.analytics import reference_price as rp
from woweconomy.analytics.tsm_export import deserialize, parse_group_export

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import token_rush_profiles as trp  # noqa: E402


GOLD = rp.COPPER_PER_GOLD


def result(item_id, group, band, local_market, recommended, low, high, liquidity="média"):
    return {
        "item_id": item_id, "name": f"Item {item_id}", "group": group, "band": band,
        "local_market": local_market, "estimated": recommended, "low": low,
        "high": high, "confidence": 0.5, "liquidity": liquidity,
        "recommended": recommended, "reason": "",
    }


def test_the_ratio_expression_is_banded_over_the_price_level():
    from woweconomy.analytics.pricing_groups import band_order

    ratios = {band: 0.5 for band in band_order()}
    expression = trp.banded_ratio_expression(ratios)
    assert expression.startswith("ifgt(dbmarket, 100g,")
    assert expression.count("ifgt(") == 8          # eight band thresholds
    assert "0.5 * dbmarket" in expression


def test_a_ratio_is_never_written_in_scientific_notation():
    # 1.2e-05 is not a valid TSM number token
    assert trp.fmt_ratio(0.000012) == "0.000012"
    assert "e" not in trp.fmt_ratio(0.0000001)
    assert trp.fmt_ratio(1.0) == "1"


def test_the_money_token_of_tsm_is_understood_by_the_evaluator():
    # "50000g" is 50 000 gold in copper
    assert rp.evaluate_tsm_expression("50000g", {}) == pytest.approx(50_000 * GOLD)
    assert rp.evaluate_tsm_expression("ifgt(dbmarket, 50000g, 1, 2)",
                                      {"dbmarket": 100_000 * GOLD}) == 1
    assert rp.evaluate_tsm_expression("ifgt(dbmarket, 50000g, 1, 2)",
                                      {"dbmarket": 10 * GOLD}) == 2


def test_the_groups_carry_the_items_and_the_measured_ratios():
    results = [
        result(1, "demand_stable", "100-500", 100 * GOLD, 20 * GOLD,
               15 * GOLD, 30 * GOLD),
        result(2, "demand_stable", "100-500", 200 * GOLD, 60 * GOLD,
               50 * GOLD, 80 * GOLD),
        result(3, "no_demand_falling", "2k-5k", 1000 * GOLD, 100 * GOLD,
               90 * GOLD, 150 * GOLD),
    ]
    membership = {"demand_stable": [1, 2, 3]}
    groups, rows, operations = trp.groups_for_realm(membership, results)
    # the membership passes through untouched: the same items as the mog export
    assert groups["demand_stable"] == [1, 2, 3]
    # the ratio is the median of the two items: (0.2 + 0.3) / 2
    row = next(r for r in rows if r["tsm_group"] == "demand_stable")
    assert row["ratio"] == pytest.approx(0.25, rel=1e-3)
    assert row["priced"] == 2      # the third row belongs to another group


def test_the_three_prices_are_ordered_and_the_vendor_floor_holds():
    results = [result(1, "demand_stable", "100-500", 100 * GOLD, 20 * GOLD,
                      15 * GOLD, 30 * GOLD)]
    membership = {"demand_stable": [1]}
    groups, rows, operations = trp.groups_for_realm(membership, results)
    operation = operations["demand_stable"]
    sources = {"dbmarket": 100 * GOLD, "vendorsell": 1 * GOLD}
    normal = rp.evaluate_tsm_expression(operation.normal, sources)
    minimum = rp.evaluate_tsm_expression(operation.minimum, sources)
    maximum = rp.evaluate_tsm_expression(operation.maximum, sources)
    assert minimum <= normal <= maximum
    assert minimum >= 1.5 * GOLD        # the vendor floor
    assert normal == pytest.approx(0.2 * 100 * GOLD)


def test_the_operation_is_a_rush_operation():
    results = [result(1, "demand_stable", "100-500", 100 * GOLD, 20 * GOLD,
                      15 * GOLD, 30 * GOLD)]
    membership = {"demand_stable": [1]}
    groups, rows, operations = trp.groups_for_realm(membership, results)
    operation = operations["demand_stable"]
    assert operation.duration == 12
    assert operation.max_expires == 13          # 7 days at 12h
    assert operation.price_reset == "none"
    assert operation.above_max == "maxPrice"
    assert operation.undercut == "1c"


def test_the_export_has_one_group_and_one_operation_per_cell():
    from woweconomy.analytics.mog_operations import build_liquidity_export

    results = [
        result(1, "demand_stable", "100-500", 100 * GOLD, 20 * GOLD,
               15 * GOLD, 30 * GOLD),
        result(2, "no_demand_falling", "2k-5k", 1000 * GOLD, 100 * GOLD,
               90 * GOLD, 150 * GOLD),
    ]
    membership = {"demand_stable": [1], "no_demand_falling": [2]}
    groups, rows, operations = trp.groups_for_realm(membership, results)
    text = build_liquidity_export(groups, operations, operation_prefix="")
    data = deserialize(text)
    parsed = parse_group_export(data["groupExport"])
    assert parsed == groups
    assert set(data["operations"]["Auctioning"]) == set(groups)
    for name in groups:
        assert data["groupOperations"][name]["Auctioning"]["override"] is True
        assert data["groupOperations"][name]["Auctioning"][1] == name


def test_the_rush_export_is_the_mog_export_with_only_the_timing_changed():
    # "faz como o mogs faz": the same groups, the same items, the same prices
    # and the same demand system -- only the operational timing is the
    # campaign's
    from woweconomy.analytics.mog_operations import build_mog_export
    from woweconomy.analytics.tsm_export import deserialize

    items = trp.mog_export.load_items(trp.OUT_DIR)
    profile = mog_realm.resolve("Dragonmaw")
    reference = deserialize(build_mog_export(items, with_operations=True, realm=profile))
    text, rows = trp.build_rush_export(items, profile)
    rush = deserialize(text)

    assert parse_group_export(rush["groupExport"]) == \
        parse_group_export(reference["groupExport"])
    for name, operation in reference["operations"]["Auctioning"].items():
        other = rush["operations"]["Auctioning"][name]
        for field, value in operation.items():
            if field in ("duration", "maxExpires"):
                continue
            assert other[field] == value, (name, field)
    for name, operation in rush["operations"]["Auctioning"].items():
        assert operation["duration"] == 12
        assert operation["maxExpires"] == 13
    assert len(rows) == len(reference["operations"]["Auctioning"])


def test_the_membership_comes_from_the_mog_export_itself():
    # the rush export must carry the very same items as the mog export
    import sqlite3

    from woweconomy.analytics.mog_operations import build_mog_export
    from woweconomy.analytics.tsm_export import deserialize

    con = sqlite3.connect(r"file:C:\FarmAnalysis\tsm_markets.db?mode=ro", uri=True)
    membership = trp.group_membership(con)
    con.close()
    items = trp.mog_export.load_items(trp.OUT_DIR)
    reference = parse_group_export(
        deserialize(build_mog_export(items, with_operations=False))["groupExport"])
    assert membership == reference
    assert sum(len(v) for v in membership.values()) == 127_155
