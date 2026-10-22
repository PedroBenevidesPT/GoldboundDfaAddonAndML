"""Unit tests for the binding audit (scripts/item_binding).

Fixtures only: a throwaway sqlite with the tables the audit reads.
"""
import csv
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import item_binding  # noqa: E402


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
        CREATE TABLE items (itemId INTEGER PRIMARY KEY, name TEXT, createdAt TEXT);
        CREATE TABLE snapshots (snapshotId INTEGER PRIMARY KEY, takenAt TEXT);
        CREATE TABLE market_snapshots (
            snapshotId INTEGER, itemId INTEGER, marketValue REAL,
            avgSalePrice REAL, saleRate REAL, soldPerDay REAL);
    """)
    con.executemany("INSERT INTO db2_items VALUES (?,?,?,?,?,?,?,?)", [
        (25, "Worn Shortsword", 2, 1, 0, 0, 1, 1),          # BoE retail
        (100, "Bound Blade", 1, 3, 10, 0, 60, 58),           # BoP retail, priced
        (271435, "Slippers of the Hissing Cult", 1, 4, 11, 2, 219, 90),
        (900, "Era BoE", 2, 3, 11, 2, 100, 85),              # BoE but era-Classic
        (200, "Broken Fang", 0, 0, 0, 0, 1, 1),               # no binding
    ])
    con.executemany("INSERT INTO blizzard_items VALUES (?,?,?,?,?,?,?)", [
        (25, "Worn Shortsword", "COMMON", "ON_EQUIP", 1, 0, "2026-01-01"),
        (100, "Bound Blade", "RARE", "ON_EQUIP", 60, 500, "2026-01-01"),  # conflict
        (400, "Api Only", "EPIC", "ON_EQUIP", 70, 900, "2026-01-01"),
    ])
    con.executemany("INSERT INTO items VALUES (?,?,?)", [
        (25, "Worn Shortsword", ""), (100, "Bound Blade", ""),
        (777, "", ""),
    ])
    con.execute("INSERT INTO snapshots VALUES (1, '2026-10-17')")
    con.executemany("INSERT INTO market_snapshots VALUES (?,?,?,?,?,?)", [
        (1, 100, 0, 500000.0, 0.5, 3.0),   # BoP with a price
        (1, 777, 0, 2850000.0, 0.0, 0.0),  # unknown id with a price
        (1, 25, 0, 0.0, 0.0, 0.0),
    ])
    con.commit()
    con.close()
    return str(path)


# --- the one bit everything depends on -----------------------------------

def test_verdict_and_era():
    assert item_binding.verdict(2) == ("BoE (bind on equip)", True)
    assert item_binding.verdict(0)[1] is True      # materials: sellable
    assert item_binding.verdict(3)[1] is True      # binds on use, sellable until
    assert item_binding.verdict(1)[1] is False
    assert item_binding.verdict(4)[1] is False
    assert item_binding.verdict(8)[1] is False
    assert item_binding.verdict(None) == ("sem dado", False)


def test_audit_counts_the_boe_universe_without_era(db):
    a = item_binding.build_audit(db)
    assert a.total == 5
    # 25 is the only retail BoE; 900 is BoE but era-Classic
    assert a.boe_by_expansion[0] == 1
    assert a.sellable_breakdown()[2] == 1
    # 25 + 200 (no binding); the era-Classic BoE is not saleable retail
    assert a.sellable_retail == 2


def test_audit_reports_the_api_conflict(db):
    a = item_binding.build_audit(db)
    assert a.api_overlap == 2          # 25 and 100 are in both sources
    assert [c["item_id"] for c in a.conflicts] == [100]
    assert a.conflicts[0]["db2"] == "ON_ACQUIRE"
    assert a.conflicts[0]["blizzard"] == "ON_EQUIP"


def test_audit_lists_bop_with_price_and_unknown_ids(db):
    a = item_binding.build_audit(db)
    assert [r["item_id"] for r in a.contradictions] == [100]
    assert a.contradictions[0]["price"] == pytest.approx(500000.0)
    # 777 is in the TSM items table but in neither binding source
    assert [r["item_id"] for r in a.unknown] == [777]
    assert a.unknown[0]["price"] > 0


def test_era_classic_bop_is_not_a_contradiction(db):
    # 271435 is BoP but era-Classic: its price is from another game, which is
    # already filtered by era -- it must not inflate the "decide this" list.
    a = item_binding.build_audit(db)
    assert 271435 not in [r["item_id"] for r in a.contradictions]


# --- exports ------------------------------------------------------------

def test_reports_are_written(db, tmp_path):
    a = item_binding.build_audit(db)
    out = tmp_path / "reports"
    paths = item_binding.write_reports(a, item_binding.load_db2_map(db), str(out))
    assert {p.name for p in paths} == {
        "boe_retail.csv", "bop_priced.csv", "binding_conflicts.csv",
        "binding_unknown.csv"}
    with (out / "boe_retail.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["item_id"] for r in rows] == ["25"]
    with (out / "bop_priced.csv").open(encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert row["item_id"] == "100"
    assert float(row["price_gold"]) == pytest.approx(50.0)  # copper -> gold


# --- lookup -------------------------------------------------------------

def test_lookup_by_id_and_name(db, capsys):
    assert item_binding.lookup(["25"], db) == 0
    assert "BoE" in capsys.readouterr().out
    assert item_binding.lookup(["Worn Shortsword"], db) == 0


def test_lookup_flags_the_contradiction(db, capsys):
    assert item_binding.lookup(["100"], db) == 0
    out = capsys.readouterr().out
    assert "BoP" in out and "ATENCAO" in out


def test_lookup_says_it_does_not_know(db, capsys):
    assert item_binding.lookup(["1234567"], db) == 1
    assert "sem binding conhecido" in capsys.readouterr().out
