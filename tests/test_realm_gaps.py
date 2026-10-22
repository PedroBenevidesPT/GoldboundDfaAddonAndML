"""Tests for the realm × item aggregation of the demand list."""
import sqlite3

from woweconomy.analytics.realm_gaps import (
    cheapest_realms, load_rows, realm_totals, region_value,
)


def _row(realm, item_id, wanters, gold, name="Item"):
    return {"realm": realm, "item_id": item_id, "item_name": name,
            "name": name, "wanters": wanters, "eligible": 100,
            "want_rate": wanters / 100, "price_copper": 1_000_000,
            "gold_gap": gold, "price_source": "região"}


def test_realm_totals_skips_thin_rows_and_sorts_by_gold():
    rows = [
        _row("Kazzak", 1, 10, 5_000),
        _row("Kazzak", 2, 4, 900),
        _row("Kazzak", 3, 2, 9_999),      # below min_wanters
        _row("Draenor", 1, 8, 3_000),
    ]
    totals = realm_totals(rows, {"Kazzak": 33, "Draenor": 30}, min_wanters=3)
    assert [t["realm"] for t in totals] == ["Kazzak", "Draenor"]
    assert totals[0]["items"] == 2
    assert totals[0]["buyers"] == 14   # 10 + 4, a conta pode faltar a dois itens
    assert totals[0]["gold_gap"] == 5_900
    assert totals[0]["accounts"] == 33
    assert totals[0]["best_item_id"] == 1
    assert totals[1]["items"] == 1


def test_realm_totals_keeps_realms_without_account_data():
    totals = realm_totals([_row("Ghost", 1, 5, 100)], {}, min_wanters=3)
    assert totals[0]["accounts"] == 0


def test_load_rows_normalizes_numbers(tmp_path):
    path = tmp_path / "where.csv"
    path.write_text(
        "item_id,item_name,name,realm,wanters,eligible,want_rate,"
        "price_copper,gold_gap,price_source\n"
        "229149,Rifle,Rifle,Kazzak,24,760,0.0316,50525973,1212623.0,região\n",
        encoding="utf-8")
    rows = load_rows(path)
    assert rows[0]["item_id"] == 229149
    assert rows[0]["wanters"] == 24
    assert rows[0]["price_copper"] == 50525973
    assert rows[0]["gold_gap"] == 1212623
    assert rows[0]["want_rate"] == 0.0316


def _tsm(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tsm_realm_price (scope TEXT, item_id INTEGER, "
                "name TEXT, market_value INTEGER, min_buyout INTEGER)")
    con.executemany("INSERT INTO tsm_realm_price VALUES (?,?,?,?,?)", rows)
    con.execute("CREATE TABLE tsm_region_price (key_id INTEGER, "
                "market_value INTEGER)")
    con.execute("INSERT INTO tsm_region_price VALUES (229149, 50525973507)")
    return con


def test_cheapest_realms_prefers_a_real_listing():
    con = _tsm([
        ("cheap-no-listing", 229149, "Rifle", 100_000, 0),
        ("has-listing", 229149, "Rifle", 296_000, 296_000),
        ("pricey", 229149, "Rifle", 900_000, 900_000),
    ])
    rows = cheapest_realms(con, 229149, 3)
    assert [r["realm"] for r in rows] == ["has-listing", "pricey",
                                          "cheap-no-listing"]
    assert rows[0]["cost"] == 296_000
    assert rows[2]["cost"] == 100_000     # sem anúncio, usa o mercado


def test_region_value_reads_the_region_table():
    con = _tsm([])
    assert region_value(con, 229149) == 50525973507
    assert region_value(con, 1) == 0
