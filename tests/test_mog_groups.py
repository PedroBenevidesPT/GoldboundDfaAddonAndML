"""Tests for the transmog group classifier."""
import csv
import sqlite3
from pathlib import Path

import pytest

from woweconomy.analytics.mog_groups import (
    Metrics, classify, load_universe, write_groups,
)
from woweconomy.analytics.tsm_ids import parse_item_ids, tsm_item_string


def m(**kw):
    base = dict(
        item_id=1, name="Test", spd=0.05, sale_rate=0.1, avg_sale_g=1000.0,
        wall_g=950.0, historical_g=900.0, market_g=950.0, listings=4,
        p30=None, buyers=None, quality=2, era=0, expansion=0, item_level=60,
    )
    base.update(kw)
    return Metrics(**base)


def test_liquid_when_turnover_is_credible():
    v = classify(m(spd=0.5, wall_g=900, historical_g=950, market_g=950))
    assert v.group == "liquid"
    assert not v.conflict


def test_collector_with_measured_demand():
    v = classify(m(spd=0.02, avg_sale_g=20_000, wall_g=22_000,
                    historical_g=20_000, market_g=21_000, p30=0.10, buyers=50))
    assert v.group == "collector"


def test_very_slow_positive_movement_is_not_dead():
    v = classify(m(spd=0.001, avg_sale_g=50, wall_g=None, historical_g=None,
                    market_g=None, p30=0.0, buyers=2))
    assert v.group == "very_slow"
    assert "not sell-through" in v.reason


def test_zero_movement_is_unproven_not_dead():
    v = classify(m(spd=0, avg_sale_g=50, wall_g=None, historical_g=None,
                    market_g=None, p30=0.0, buyers=2))
    assert v.group == "unproven"
    assert "not proof of no demand" in v.reason


def test_missing_movement_is_unknown_not_zero():
    v = classify(m(spd=None, avg_sale_g=50))
    assert v.group == "unknown"


def test_slow_when_movement_is_positive_and_wall_is_thin():
    v = classify(m(spd=0.02, avg_sale_g=500, wall_g=None, historical_g=600,
                    market_g=500, listings=None, p30=0.0, buyers=2))
    assert v.group == "slow"
    assert "no wall" in v.reason


def test_conflict_is_a_flag_not_a_primary_group():
    v = classify(m(spd=0.2, wall_g=100, historical_g=1000, market_g=950))
    assert v.group == "liquid"
    assert v.conflict


def test_unknown_when_no_price():
    assert classify(m(spd=0.1, avg_sale_g=None, wall_g=None,
                      historical_g=None, market_g=None)).group == "unknown"


def test_write_groups_outputs_master_and_subsets(tmp_path):
    rows = [
        m(item_id=1, name="Liquid", spd=0.5),
        m(item_id=2, name="Collector", spd=0.02, avg_sale_g=20_000,
          wall_g=22_000, historical_g=20_000, market_g=21_000,
          p30=0.10, buyers=50),
        m(item_id=3, name="Conflict", spd=0.2, wall_g=100,
          historical_g=1000, market_g=950),
    ]
    counts = write_groups(rows, tmp_path)
    assert counts["liquid"] == 2  # the conflict row stays liquid
    assert counts["collector"] == 1
    assert counts["conflict"] == 1
    assert (tmp_path / "dead.txt").exists() is False
    assert (tmp_path / "thin.txt").exists() is False
    master = list(csv.DictReader((tmp_path / "mog_groups.csv").open(encoding="utf-8")))
    assert len(master) == 3
    assert {"item_id", "appearance_id", "variants", "group"} <= set(master[0])
    assert (tmp_path / "conflict.txt").read_text(encoding="utf-8").strip() == "i:3"
    liquid = (tmp_path / "liquid.txt").read_text(encoding="utf-8")
    assert liquid.strip() == "i:3,i:1"


def test_tsm_item_string_uses_item_prefix_and_drops_duplicates():
    assert tsm_item_string([245776, 245778, 245776]) == "i:245776,i:245778"


def test_parse_item_ids_accepts_raw_prefixed_and_multiline_lists():
    assert parse_item_ids("123, i:456\nitem:789;123") == [123, 456, 789]


@pytest.fixture()
def universe_db(tmp_path):
    """Two look-A variants (one priced), one look-B item, one price-less look."""
    path = tmp_path / "tsm.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE db2_items(itemId INTEGER PRIMARY KEY, name TEXT,
            quality INTEGER, expansion INTEGER, era INTEGER, itemLevel INTEGER,
            requiredLevel INTEGER);
        CREATE TABLE db2_item_appearance(itemId INTEGER, appearanceId INTEGER,
            PRIMARY KEY(itemId, appearanceId));
        CREATE TABLE tsm_region_price(snapshot_id INTEGER, key_id, name,
            market_value, historical, avg_sale_price, sale_rate, sold_per_day,
            updated_at, fetched_at);
        CREATE TABLE tsm_price_change(snapshot_id INTEGER, scope TEXT, kind TEXT,
            key_id, market_value, min_buyout, recent, historical, avg_sale_price,
            sale_rate, sold_per_day);
        CREATE TABLE tsm_market(itemId INTEGER, realm TEXT, min_buyout INTEGER,
            market INTEGER, num_auctions INTEGER, market_recent, historical);
        CREATE TABLE tsm_realm_group(realm_slug TEXT, canonical_name TEXT);
        CREATE TABLE tsm_snapshot(snapshot_id INTEGER PRIMARY KEY, scope TEXT,
            kind TEXT, updated_at TEXT, fetched_at TEXT, checksum TEXT,
            rows INTEGER, changed INTEGER, path TEXT, etag TEXT, last_modified TEXT);
    """)
    con.executemany("INSERT INTO db2_items VALUES (?,?,?,?,?,?,?)", [
        (100, "Look A rep", 2, 0, 0, 60, 58),
        (101, "Look A variant", 2, 0, 0, 60, 58),
        (200, "Look B", 2, 0, 0, 60, 58),
        (300, "No price look", 2, 0, 0, 60, 58),
    ])
    con.executemany("INSERT INTO db2_item_appearance VALUES (?,?)", [
        (100, 1), (101, 1), (200, 2), (300, 3),
    ])
    con.executemany("INSERT INTO tsm_region_price VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, "100", "Look A rep", 1_000_000, 900_000, 1_000_000, 0.5, 0.5, "", ""),
        (1, "200", "Look B", 500_000, 500_000, 500_000, 0.0, 0.0, "", ""),
    ])
    con.execute("INSERT INTO tsm_price_change VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (1, "spinebreaker", "realm_items", "100", 1_000_000, 950_000, 0,
                 900_000, 0, 0, 0))
    con.execute("INSERT INTO tsm_market VALUES (?,?,?,?,?,?,?)",
                (100, "Spinebreaker", 950_000, 1_000_000, 4, 0, 900_000))
    con.execute("INSERT INTO tsm_realm_group VALUES (?,?)",
                ("spinebreaker", "Spinebreaker"))
    con.execute("INSERT INTO tsm_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (1, "spinebreaker", "realm_items", "", "", "", 0, 0, "", "", ""))
    con.commit()
    con.close()
    return path


def test_load_universe_variants_follow_their_appearance(universe_db):
    rows = load_universe(universe_db, "spinebreaker", Path("nope.csv"),
                         items="variants")
    by_id = {m.item_id: m for m in rows}
    assert set(by_id) == {100, 101, 200}          # 300 has no priced appearance
    assert by_id[101].appearance_id == 1
    assert by_id[101].variants == 2
    # the unpriced variant inherits the look's verdict and local wall
    assert classify(by_id[100]).group == classify(by_id[101]).group
    assert by_id[101].wall_g == 95.0              # 950_000 copper / 10_000


def test_load_universe_all_keeps_unpriced_looks_as_unknown(universe_db):
    rows = load_universe(universe_db, "spinebreaker", Path("nope.csv"),
                         items="all")
    by_id = {m.item_id: m for m in rows}
    assert set(by_id) == {100, 101, 200, 300}
    assert classify(by_id[300]).group == "unknown"


def test_load_universe_priced_is_the_old_membership(universe_db):
    rows = load_universe(universe_db, "spinebreaker", Path("nope.csv"),
                         items="priced")
    assert {m.item_id for m in rows} == {100, 200}


def test_write_groups_keeps_a_look_together(universe_db, tmp_path):
    rows = load_universe(universe_db, "spinebreaker", Path("nope.csv"),
                         items="variants")
    write_groups(rows, tmp_path)
    with (tmp_path / "mog_groups.csv").open(encoding="utf-8") as fh:
        master = {int(r["item_id"]): r for r in csv.DictReader(fh)}
    # the priced variant and its unpriced sibling share appearance 1 -> one group
    assert master[100]["group"] == master[101]["group"]
    assert master[101]["appearance_id"] == "1"
    assert master[101]["variants"] == "2"
