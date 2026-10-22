"""Unit tests for the DB2 item facts (binding / quality / era by item id).

Fixtures only: a throwaway sqlite with the two tables the import writes.
"""
import sqlite3

import pytest

from woweconomy.analytics.farm_ranking import (
    FarmRankingConfig,
    NpcDrop,
    blizz_excluded,
    item_exclusion_reason,
)
from woweconomy.services.blizzard_items import BlizzardItem
from woweconomy.services.db2_items import (
    Db2Item,
    load_facts,
    load_map,
)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "items.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE db2_items (
            itemId INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            binding INTEGER, quality INTEGER, expansion INTEGER, era INTEGER,
            itemLevel INTEGER, requiredLevel INTEGER);
        CREATE TABLE blizzard_items (
            itemId INTEGER PRIMARY KEY, name TEXT, quality TEXT, binding TEXT,
            level INTEGER, sellPrice INTEGER, fetchedAt TEXT);
    """)
    con.executemany(
        "INSERT INTO db2_items VALUES (?,?,?,?,?,?,?,?)",
        [
            # retail BoE (Worn Shortsword)
            (25, "Worn Shortsword", 2, 1, 0, 0, 1, 1),
            # retail BoP
            (100, "Bound Blade", 1, 3, 0, 0, 60, 58),
            # era-Classic BoP: the MoP-Classic slippers, price from another game
            (271435, "Slippers of the Hissing Cult", 1, 4, 11, 2, 219, 90),
            # era-Classic BoE: still not retail gold
            (900, "Era BoE", 2, 3, 11, 2, 100, 85),
            # grey retail trash
            (200, "Broken Fang", 0, 0, 0, 0, 1, 1),
            # binding column empty -> unknown, must not be treated as bound
            (300, "Unknown Binding", None, 2, 0, 0, 10, 10),
        ],
    )
    con.executemany(
        "INSERT INTO blizzard_items VALUES (?,?,?,?,?,?,?)",
        [
            # API row with an old binding; DB2 wins, sell_price survives
            (100, "Bound Blade", "RARE", "ON_EQUIP", 60, 500, "2026-01-01"),
            # id the DB2 does not know: API is the only source
            (400, "Api Only", "EPIC", "ON_EQUIP", 70, 900, "2026-01-01"),
        ],
    )
    con.commit()
    con.close()
    return str(path)


# --- enum mapping ------------------------------------------------------

def test_binding_and_quality_names():
    assert Db2Item(1, binding=2).binding_name == "ON_EQUIP"
    assert Db2Item(1, binding=1).binding_name == "ON_ACQUIRE"
    assert Db2Item(1, binding=0).binding_name == ""       # explicit: tradeable
    assert Db2Item(1, binding=None).binding_name == ""    # unknown
    assert Db2Item(1, quality=0).quality_name == "POOR"
    assert Db2Item(1, quality=4).quality_name == "EPIC"


def test_era_is_not_retail():
    assert not Db2Item(1, era=0).is_era_classic
    assert Db2Item(1, era=2).is_era_classic


# --- storage -----------------------------------------------------------

def test_load_map_keeps_era_and_binding(db):
    m = load_map(db)
    assert m[25].binding == 2 and m[25].era == 0
    assert m[271435].era == 2 and m[271435].binding == 1
    assert m[300].binding is None  # empty column stays empty, not "bound"


def test_load_map_only_filters_without_losing_rows(db):
    m = load_map(db, only=[25, 271435])
    assert set(m) == {25, 271435}
    assert load_map(db, only=[]) == {}


def test_load_map_without_the_table_is_empty_not_fatal(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert load_map(str(empty)) == {}


# --- merge -------------------------------------------------------------

def test_db2_wins_over_the_api_but_keeps_its_sell_price(db):
    f = load_facts(db)
    assert f[100].binding == "ON_ACQUIRE"   # DB2 (BoP), not the API's ON_EQUIP
    assert f[100].sell_price == 500         # only the API has this
    assert f[400].binding == "ON_EQUIP"     # id absent from DB2: API only
    assert f[271435].era == 2


# --- the filter both modules use --------------------------------------

def mkdrop(item=25, rate=0.5):
    return NpcDrop(1, "Mob", 331, "ash", "drops", item, "Item", rate)


def test_reason_order_is_era_then_bop_then_junk(db):
    f = load_facts(db)
    r = FarmRankingConfig()
    assert item_exclusion_reason(271435, f, r) == "era"   # era beats bop
    assert item_exclusion_reason(900, f, r) == "era"      # era BoE is out too
    assert item_exclusion_reason(100, f, r) == "bop"
    assert item_exclusion_reason(200, f, r) == "junk"
    assert item_exclusion_reason(25, f, r) == ""          # BoE: sellable
    assert item_exclusion_reason(300, f, r) == ""         # unknown binding
    assert item_exclusion_reason(999, f, r) == ""         # no data at all


def test_switches_turn_each_rule_off(db):
    f = load_facts(db)
    # The era item is also BoP: turning the era rule off re-reports the real
    # reason instead of letting it back into the EV.
    assert item_exclusion_reason(271435, f,
                                 FarmRankingConfig(exclude_era=False)) == "bop"
    # The era BoE has no other objection: it is exactly what --no-era-filter is for.
    assert not blizz_excluded(mkdrop(900), f,
                              FarmRankingConfig(exclude_era=False))
    assert blizz_excluded(mkdrop(271435), f, FarmRankingConfig())
    assert not blizz_excluded(mkdrop(100), f,
                              FarmRankingConfig(exclude_soulbound=False))
    assert not blizz_excluded(mkdrop(200), f, FarmRankingConfig(exclude_poor=False))


def test_empty_facts_never_exclude():
    assert not blizz_excluded(mkdrop(271435), {}, FarmRankingConfig())


def test_blizzard_item_defaults_to_retail():
    assert not BlizzardItem(1, "x").is_era_classic
    assert BlizzardItem(1, "x", era=2).is_era_classic
