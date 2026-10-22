"""The realm sale proxy: what it measures, and what it refuses to claim.

The realm export has no sale rate, so it is derived from a pair of
snapshots: a cheapest ask that rose means the cheap unit is gone (bought,
cancelled or expired -- an estimate), a cheapest ask that fell means a
cheaper listing appeared (competition).  Items missing from one of the two
snapshots are left out, never counted as sales.
"""
import sqlite3

import pytest

from woweconomy.ml.realm_market import (
    RealmDemand,
    measure_all,
    measure_realm,
    relative_liquidity,
    snapshot_pairs,
)


def _db(tmp_path, rows, snapshots=(1, 2)):
    con = sqlite3.connect(tmp_path / "m.db")
    con.executescript("""
        CREATE TABLE tsm_price_change(snapshot_id INTEGER, scope TEXT, kind TEXT,
            key_id INTEGER, market_value INTEGER, min_buyout INTEGER,
            recent INTEGER, historical INTEGER, avg_sale_price REAL,
            sale_rate REAL, sold_per_day REAL);
        CREATE TABLE tsm_snapshot(snapshot_id INTEGER PRIMARY KEY, scope TEXT,
            kind TEXT, updated_at TEXT, fetched_at TEXT);
    """)
    con.executemany(
        "INSERT INTO tsm_price_change(snapshot_id, scope, kind, key_id, min_buyout)"
        " VALUES (?,?,?,?,?)",
        [(s, realm, "realm_items", item, price) for s, realm, item, price in rows])
    for i, snap in enumerate(snapshots, start=1):
        con.execute("INSERT INTO tsm_snapshot VALUES (?,?,?,?,?)",
                    (snap, "dragonmaw", "realm_items",
                     f"2026-10-{20 + i}T00:00:00Z", f"2026-10-{20 + i}T00:10:00Z"))
    con.commit()
    return con


def test_a_rise_is_a_sale_proxy_and_a_fall_is_an_undercut(tmp_path):
    con = _db(tmp_path, [
        (1, "dragonmaw", 10, 1000), (2, "dragonmaw", 10, 1200),   # rose
        (1, "dragonmaw", 11, 500), (2, "dragonmaw", 11, 400),     # fell
        (1, "dragonmaw", 12, 700), (2, "dragonmaw", 12, 700),     # unchanged
    ])
    demand = measure_realm(con, "dragonmaw", newer=2, older=1)
    assert demand.items == 3
    assert demand.rose == 1 and demand.fell == 1
    assert demand.sale_proxy == pytest.approx(1 / 3)
    assert demand.undercut_share == pytest.approx(1 / 3)
    assert demand.label == "estimate"


def test_an_item_missing_from_one_snapshot_is_not_a_sale(tmp_path):
    con = _db(tmp_path, [
        (1, "dragonmaw", 10, 1000),
        (2, "dragonmaw", 10, 1000),
        (2, "dragonmaw", 11, 500),   # appeared only in the newer one
    ])
    demand = measure_realm(con, "dragonmaw", newer=2, older=1)
    assert demand.items == 1
    assert demand.rose == 0


def test_a_zero_price_is_not_a_measurement(tmp_path):
    con = _db(tmp_path, [
        (1, "dragonmaw", 10, 0), (2, "dragonmaw", 10, 900),
        (1, "dragonmaw", 11, 900), (2, "dragonmaw", 11, 0),
    ])
    assert measure_realm(con, "dragonmaw", newer=2, older=1) is None


def test_the_proxy_is_normalised_to_a_day(tmp_path):
    con = _db(tmp_path, [
        (1, "dragonmaw", 10, 1000), (2, "dragonmaw", 10, 1200),
    ])
    demand = measure_realm(con, "dragonmaw", newer=2, older=1)
    assert demand.hours == pytest.approx(24.0)   # the injected vintage gap
    assert demand.per_day == pytest.approx(demand.sale_proxy)


def test_a_pair_needs_two_snapshots(tmp_path):
    con = _db(tmp_path, [(1, "dragonmaw", 10, 1000)], snapshots=(1,))
    assert snapshot_pairs(con) == {}
    assert measure_all(con) == {}


def test_relative_liquidity_is_clipped_and_neutral_without_evidence():
    cheap = RealmDemand("a", 100, 1, 1, 24.0, 2)
    rich = RealmDemand("b", 100, 50, 1, 24.0, 2)
    assert relative_liquidity(cheap, rich) == pytest.approx(2.0)
    assert relative_liquidity(rich, cheap) == pytest.approx(0.5)
    assert relative_liquidity(None, rich) == 1.0
    assert relative_liquidity(rich, None) == 1.0
    assert relative_liquidity(RealmDemand("c", 0, 0, 0, 24.0, 2), rich) == 1.0
