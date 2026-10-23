"""Auction-house costs must be the measured ones, not the assumed ones.

The old constants (``deposit_pct = 0.05``, ``deposit_min_gold = 1``,
``deposit_vendor_cap_pct = 3.0``) reproduce **0 of 404** real sale rows.
These tests pin the measured formula so it cannot drift back.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from woweconomy.analytics.ah_costs import (
    AH_CUT,
    DEPOSIT_FLOOR_COPPER,
    DEPOSIT_RATE,
    CostAudit,
    deposit_copper,
    legacy_deposit_copper,
    measure_from_db,
    net_proceeds,
    net_proceeds_unit,
    stack_total,
)

REAL_DB = Path("tsm_markets.db")


def _db_available() -> bool:
    return REAL_DB.exists()


@pytest.mark.skipif(not _db_available(), reason="no tsm_markets.db in the workspace")
def test_the_measured_constants_reproduce_the_accounts_own_sales():
    audit = measure_from_db(REAL_DB)
    assert audit.sale_rows > 100, "expected a real sale history to audit"
    assert audit.deposit_rows > 50, "expected real rows with a vendor price"
    assert audit.deposit_fits, (
        f"the deposit formula explains only {audit.deposit_exact}/"
        f"{audit.deposit_rows} rows")
    assert audit.cut_fits, (
        f"the {AH_CUT:.0%} cut explains only {audit.cut_within_half_pct}/"
        f"{audit.cut_rows} rows")
    assert audit.trustworthy


@pytest.mark.skipif(not _db_available(), reason="no tsm_markets.db in the workspace")
def test_the_legacy_constants_explain_nothing():
    """The bug, pinned: the old formula fit 0 of 404 rows."""
    con = sqlite3.connect(REAL_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT e.unit_price_copper AS p, e.qty AS q, e.deposit_copper AS d,"
            " b.sellPrice AS vendor FROM auction_events e"
            " LEFT JOIN blizzard_items b ON b.itemId = e.item_id"
            " WHERE e.event_type = 'sale' AND e.unit_price_copper > 0"
            "   AND e.deposit_copper > 0").fetchall()
    finally:
        con.close()
    with_vendor = [r for r in rows if r["vendor"] and r["vendor"] > 0]
    assert with_vendor, "expected rows with a vendor price"
    hits = sum(1 for r in with_vendor
               if abs(r["d"] - legacy_deposit_copper(r["p"], r["vendor"], r["q"])) < 1)
    assert hits / len(with_vendor) < 0.05, (
        f"the legacy formula now explains {hits}/{len(with_vendor)} rows; "
        f"either it was fixed or the data changed")


@pytest.mark.skipif(not _db_available(), reason="no tsm_markets.db in the workspace")
def test_the_audit_survives_an_empty_database(tmp_path):
    audit = measure_from_db(tmp_path / "nope.db")
    assert audit.sale_rows == 0
    assert not audit.trustworthy
    assert audit.notes


def test_deposit_is_fifteen_percent_of_vendor_per_unit():
    assert deposit_copper(1000, 1) == 150
    assert deposit_copper(1000, 5) == 750
    assert deposit_copper(22_500, 8) == 27_000


def test_deposit_floor_is_ten_silver_not_one_gold():
    """No row in the data has a 10,000c deposit; 96 sit at 100c."""
    assert DEPOSIT_FLOOR_COPPER == 100
    assert deposit_copper(3, 16) == 100        # 15% x 3 x 16 = 7.2c
    assert deposit_copper(0, 1) == 100         # no vendor price still costs
    assert deposit_copper(None, 1) == 100


def test_deposit_has_no_vendor_cap():
    """The old 300%-of-vendor cap does not exist in the data."""
    assert deposit_copper(22_500, 8) == 27_000      # 120% of vendor
    assert deposit_copper(1_000_000, 20) == 3_000_000   # 3000% of vendor


def test_the_auction_house_keeps_five_percent():
    assert AH_CUT == pytest.approx(0.05)
    assert net_proceeds(100_000) == pytest.approx(95_000)
    assert net_proceeds_unit(10_000, 5) == pytest.approx(47_500)


def test_stack_total_treats_a_missing_quantity_as_one():
    assert stack_total(10_000, 0) == 10_000
    assert stack_total(10_000, None) == 10_000
    assert stack_total(10_000, 3) == 30_000


def test_the_units_trap_is_impossible_to_hit_by_accident():
    """unit x qty is the total; comparing unit to net is the classic error."""
    unit, qty = 500, 99
    assert stack_total(unit, qty) == 49_500
    assert net_proceeds(stack_total(unit, qty)) == pytest.approx(47_025)
    # the wrong comparison would have given net / unit_price = 94, which
    # looks like corruption and is not
    assert net_proceeds(stack_total(unit, qty)) / unit == pytest.approx(94.05)


def test_cost_audit_reports_a_fit_rate():
    audit = CostAudit(sale_rows=100, deposit_rows=50, deposit_exact=45,
                      cut_rows=100, cut_within_half_pct=80)
    assert audit.deposit_fit == pytest.approx(0.9)
    assert audit.cut_fit == pytest.approx(0.8)
    assert audit.trustworthy
    assert audit.as_dict()["deposit_exact"] == 45


def test_a_cost_audit_below_threshold_is_not_trustworthy():
    audit = CostAudit(sale_rows=10, deposit_rows=10, deposit_exact=2,
                      cut_rows=10, cut_within_half_pct=1)
    assert not audit.trustworthy
    assert "deposit" in " ".join(audit.as_dict()["notes"]) or audit.deposit_fit < 0.8
