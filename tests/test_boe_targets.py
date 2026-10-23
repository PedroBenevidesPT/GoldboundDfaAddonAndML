"""Unit tests for the BoE targets join (BoE x still-missing appearances).

Fixtures only: a throwaway sqlite plus a fake DfA folder.
"""
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from woweconomy.analytics.boe_targets import (
    STATUS_COMUM,
    STATUS_FALTA,
    STATUS_FORA_CATALOGO,
    STATUS_INOBTENIVEL,
    STATUS_SEM,
    appearance_catalog,
    build_targets,
    print_missing_items,
    lookup_demand,
    sort_targets,
    write_csv,
)

DEMAND_HEADER = ("kind,item_id,name,source,faction,missing,collectors,"
                 "missing_pct,owned_pct,value,wowhead,note\n")


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "items.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE db2_items (
            itemId INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            binding INTEGER, quality INTEGER, expansion INTEGER, era INTEGER,
            itemLevel INTEGER, requiredLevel INTEGER);
        CREATE TABLE db2_item_appearance (
            itemId INTEGER NOT NULL, appearanceId INTEGER NOT NULL,
            PRIMARY KEY(itemId, appearanceId));
        CREATE TABLE items (itemId INTEGER PRIMARY KEY, name TEXT, createdAt TEXT);
        CREATE TABLE snapshots (snapshotId INTEGER PRIMARY KEY, takenAt TEXT);
        CREATE TABLE market_snapshots (
            snapshotId INTEGER, itemId INTEGER, marketValue REAL,
            avgSalePrice REAL, saleRate REAL, soldPerDay REAL);
    """)
    con.executemany("INSERT INTO db2_items VALUES (?,?,?,?,?,?,?,?)", [
        (25, "Worn Shortsword", 2, 1, 0, 0, 1, 1),
        (100, "Bound Blade", 2, 3, 0, 0, 60, 58),      # BoE, look already common
        (300, "Signet Ring", 2, 3, 0, 0, 60, 58),      # BoE, no appearance
        (900, "Era BoE", 2, 3, 11, 2, 100, 85),        # era-Classic: not a target
        (200, "BoP Blade", 1, 3, 0, 0, 60, 58),        # BoP: not sellable
    ])
    con.executemany("INSERT INTO db2_item_appearance VALUES (?,?)", [
        (25, 153), (100, 154), (900, 155), (200, 156),
    ])
    con.execute("INSERT INTO snapshots VALUES (1, '2026-10-17')")
    con.executemany("INSERT INTO market_snapshots VALUES (?,?,?,?,?,?)", [
        (1, 25, 0, 100000.0, 0.5, 2.0),
        (1, 100, 0, 200000.0, 0.1, 0.0),
    ])
    con.commit()
    con.close()
    return str(path)


@pytest.fixture()
def dfa(tmp_path):
    folder = tmp_path / "dfa"
    folder.mkdir()
    (folder / "demand_appearances.csv").write_text(
        DEMAND_HEADER +
        "appearances,153,Worn Shortsword,Drop,,60,100,60.00,10.00,54.0,,\n"
        "appearances,155,Era BoE,Drop,,99,100,99.00,0.10,98.9,,\n"
        "appearances,156,BoP Blade,Drop,,99,100,99.00,0.10,98.9,,\n",
        encoding="utf-8")
    for kind in ("mounts", "recipes"):
        (folder / f"demand_{kind}.csv").write_text(DEMAND_HEADER, encoding="utf-8")
    return folder


def by_id(targets):
    return {t.item_id: t for t in targets}


# --- the join -----------------------------------------------------------

def test_targets_are_boe_retail_only(db, dfa):
    targets = by_id(build_targets(db, dfa))
    assert set(targets) == {25, 100, 300}     # no BoP (200), no era-Classic (900)


def test_status_marks_what_collectors_still_lack(db, dfa):
    t = by_id(build_targets(db, dfa))
    assert t[25].status == STATUS_FALTA
    assert t[25].missing and t[25].missing_appearances == 1
    assert t[25].missing_pct == pytest.approx(60.0)
    assert t[25].appearances == 1
    assert t[25].price == pytest.approx(100000.0)
    # has an appearance, but the sample already owns it
    assert t[100].status == STATUS_COMUM and not t[100].missing
    # BoE without any appearance row: a recipe or a ring, not a transmog
    assert t[300].status == STATUS_SEM and t[300].missing_appearances == 0


def test_counts_come_from_the_scan_not_from_the_appearance_list(db, dfa):
    t = by_id(build_targets(db, dfa))[25]
    # 60 of the 100 sampled accounts did not have appearance 153.
    assert t.missing_count == 60
    assert t.collectors == 100
    assert by_id(build_targets(db, dfa))[300].missing_count == 0


def test_faltam_prints_one_row_per_item(db, dfa, capsys):
    print_missing_items(build_targets(db, dfa), top_n=5)
    out = capsys.readouterr().out
    assert "60/100" in out and "Worn Shortsword" in out
    assert "Bound Blade" not in out      # not missing -> not in the table


def test_sorting_by_value_uses_price_times_the_missing_share(db, dfa):
    targets = build_targets(db, dfa)
    t = by_id(targets)
    assert t[25].value == pytest.approx(60000.0)
    assert t[100].value == 0.0               # not missing -> no sort value
    assert sort_targets(targets, "valor")[0].item_id == 25
    assert sort_targets(targets, "preco")[0].item_id == 100


def test_csv_carries_the_status(db, dfa, tmp_path):
    out = tmp_path / "targets.csv"
    path = write_csv(build_targets(db, dfa), str(out))
    with Path(path).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {"status", "missing_pct", "price_gold"} <= set(rows[0])
    assert rows[0]["status"] == STATUS_FALTA          # sorted: falta first
    assert {r["status"] for r in rows} == {STATUS_FALTA, STATUS_COMUM, STATUS_SEM}


def test_lookup_demand_for_one_item(db, dfa):
    n, best = lookup_demand(25, db, dfa)
    assert n == 1 and best is not None and best.missing_pct == pytest.approx(60.0)
    assert lookup_demand(100, db, dfa)[0] == 1
    assert lookup_demand(100, db, dfa)[1] is None     # not missing
    assert lookup_demand(300, db, dfa) == (0, None)   # no appearance at all


def test_catalog_separates_dead_looks_from_common(db, dfa):
    # The demand CSV only counts OBTAINABLE appearances, so "absent" could mean
    # "everyone has it" or "the source is dead".  The catalogue tells them apart.
    con = sqlite3.connect(db)
    con.execute("INSERT INTO db2_items VALUES (500,'Dead Look',2,3,0,0,60,58)")
    con.execute("INSERT INTO db2_item_appearance VALUES (500,157)")
    con.execute("INSERT INTO db2_items VALUES (600,'Uncatalogued',2,3,0,0,60,58)")
    con.execute("INSERT INTO db2_item_appearance VALUES (600,999)")
    con.commit()
    con.close()
    (dfa / "cache").mkdir()
    (dfa / "cache" / "appearancesources.json").write_text(json.dumps({
        "appearancesources": [
            {"id": 153, "visualid": 153, "name": "a"},
            {"id": 154, "visualid": 154, "name": "b"},
            {"id": 157, "visualid": 157, "name": "dead", "unobtainable": 1},
            {"id": 5, "visualid": 5, "name": "excluded", "excluded": True},
        ]}), encoding="utf-8")
    t = by_id(build_targets(db, dfa))
    assert t[500].status == STATUS_INOBTENIVEL
    assert t[600].status == STATUS_FORA_CATALOGO
    assert t[100].status == STATUS_COMUM   # gettable, just not missing


def test_catalog_marks_excluded_and_gated(db, dfa):
    (dfa / "cache").mkdir()
    path = dfa / "cache" / "appearancesources.json"
    path.write_text(json.dumps({"appearancesources": [
        {"id": 1, "visualid": 1},
        {"id": 2, "visualid": 2, "unobtainable": 2},   # never in the game
        {"id": 3, "visualid": 3, "unobtainable": 3},   # obtainable, gated
        {"id": 4, "visualid": 4, "excluded": True},
    ]}), encoding="utf-8")
    catalog = appearance_catalog(dfa)
    assert catalog == {1: True, 2: False, 3: True, 4: False}


def test_catalog_absent_means_unknown_not_dead(db, dfa):
    assert appearance_catalog(dfa) is None
    # Without the catalogue the report must not invent "inobtenivel".
    assert by_id(build_targets(db, dfa))[100].status == STATUS_COMUM


def test_missing_dfa_folder_is_not_an_error(db, tmp_path):
    assert lookup_demand(25, db, tmp_path / "nope") == (1, None)


def test_build_targets_without_dfa_folder_fails_loudly(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DIR", str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError, match="Data for Azeroth"):
        build_targets(db)
