"""Tests for the inventory x item-demand comparison."""
import csv

import pytest

from woweconomy.analytics.inventory_demand import (
    INVENTORY_CANDIDATES, InventoryLine, compare, load_demand, load_inventory,
    missing_names, realm_wanters, resolve_inventory, resolve_names,
    sale_pressure,
)


def write_inventory(tmp_path, text):
    path = tmp_path / "inv.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_inventory_semicolon_and_duplicate_ids(tmp_path):
    path = write_inventory(
        tmp_path,
        "ItemID;Name;Quantity\n"
        "100;Alpha;2\n"
        "100;Alpha;3\n"
        "200;Beta;\n"
        "0;Ignored;1\n"
        "x;Bad;1\n")
    inv = load_inventory(path)
    assert inv[100].qty == 5
    assert inv[200].qty == 1
    assert 0 not in inv
    assert len(inv) == 2


def test_inventory_comma_fallback(tmp_path):
    path = write_inventory(tmp_path, "item_id,name,qty\n7,Seven,4\n")
    inv = load_inventory(path)
    assert inv[7].qty == 4


def test_load_demand_and_compare(tmp_path):
    demand_path = tmp_path / "demand.csv"
    with demand_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "item_name", "appearance_id", "name", "source",
                    "grants", "wanters", "eligible", "want_rate",
                    "price_copper", "gold_gap"])
        w.writerow([100, "Alpha", 1, "Alpha look", "Drop", 2, 10, 20, 0.5,
                    5_000_000, 5_000])
        w.writerow([200, "Beta", 2, "Beta look", "Drop", 1, 10, 20, 0.5,
                    10_000_000, 10_000])
    inv = load_inventory(write_inventory(
        tmp_path, "ItemID;Name;Quantity\n100;Alpha;3\n300;Gamma;1\n"))
    demand = load_demand(demand_path)
    matched, missing, extra = compare(inv, demand)
    assert len(matched) == 1
    assert matched[0].demand.item_id == 100
    assert matched[0].qty == 3
    assert matched[0].stock_value == 3 * 500  # 5,000,000 copper = 500 gold
    assert matched[0].coverage == 0.3
    assert [d.item_id for d in missing] == [200]
    assert [e.item_id for e in extra] == [300]


def test_resolve_inventory_finds_the_root_export(tmp_path):
    # O caso real: o export vivia em scripts/inventario.csv e passou a
    # inventory.csv na raiz; o script nao pode depender de um caminho fixo.
    target = tmp_path / "inventory.csv"
    target.write_text("ItemID;Name;Quantity\n100;Alpha;1\n", encoding="utf-8")
    assert resolve_inventory(tmp_path) == target


def test_resolve_inventory_prefers_the_first_candidate(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "inventory.csv").write_text("a", encoding="utf-8")
    (tmp_path / "scripts" / "inventario.csv").write_text("b", encoding="utf-8")
    assert resolve_inventory(tmp_path) == tmp_path / "inventory.csv"


def test_resolve_inventory_point_also_works(tmp_path):
    (tmp_path / "reports").mkdir()
    target = tmp_path / "reports" / "inventario.csv"
    target.write_text("a", encoding="utf-8")
    assert resolve_inventory(tmp_path) == target


def test_resolve_inventory_explicit_path_wins_and_missing_fails(tmp_path):
    target = tmp_path / "qualquer.csv"
    target.write_text("a", encoding="utf-8")
    assert resolve_inventory(tmp_path, str(target)) == target
    with pytest.raises(FileNotFoundError) as exc:
        resolve_inventory(tmp_path, str(tmp_path / "nao.csv"))
    assert "nao.csv" in str(exc.value)


def test_resolve_inventory_error_lists_where_it_looked(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        resolve_inventory(tmp_path)
    for rel in INVENTORY_CANDIDATES:
        assert rel in str(exc.value)


def test_missing_names_flags_only_the_lost_ones():
    lines = {1: InventoryLine(1, "Thorium Boots", 1),
             2: InventoryLine(2, "Unknown", 2),
             3: InventoryLine(3, "", 1),
             4: InventoryLine(4, "?", 1)}
    assert missing_names(lines) == [2, 3, 4]


def test_resolve_names_fills_unknown_and_keeps_the_rest():
    lines = {1: InventoryLine(1, "Thorium Boots", 1),
             2: InventoryLine(2, "Unknown", 2)}
    out = resolve_names(lines, {2: "Master Engineer's Goggles", 1: "Nope"})
    assert out[2].name == "Master Engineer's Goggles"
    assert out[2].qty == 2
    assert out[1].name == "Thorium Boots"   # o export manda


def test_resolve_names_leaves_the_item_alone_without_a_name():
    lines = {9: InventoryLine(9, "Unknown", 1)}
    assert resolve_names(lines, {})[9].name == "Unknown"


def test_realm_wanters_counts_the_union_of_appearances():
    import numpy as np
    # 4 contas x 3 slots, 1 = nao tem
    wants = np.array([
        [1, 1, 0],   # falta-lhe as duas aparências: conta uma vez, nao duas
        [1, 0, 0],
        [0, 0, 0],   # tem tudo
        [0, 1, 0],
    ], dtype=np.uint8)
    realms = ["Kazzak", "Kazzak", "Draenor", "Draenor"]
    rows = realm_wanters(wants, realms, columns=[0, 1])
    by = {r["realm"]: r for r in rows}
    assert by["Kazzak"]["wanters"] == 2   # uniao; a soma das colunas seria 3
    assert by["Kazzak"]["accounts"] == 2
    assert by["Draenor"]["wanters"] == 1
    assert by["Kazzak"]["share"] == 1.0
    # uma coluna só: sem uniao para fazer, e Draenor fica de fora (0 querem)
    single = realm_wanters(wants, realms, columns=[0])
    assert {r["realm"]: r["wanters"] for r in single} == {"Kazzak": 2}


def test_realm_wanters_single_column_and_limits():
    import numpy as np
    wants = np.array([[1], [1], [1], [0]], dtype=np.uint8)
    realms = ["A", "A", "A", "B"]
    everything = realm_wanters(wants, realms, columns=[0], per_realm=0,
                               min_wanters=0)
    assert [r["realm"] for r in everything] == ["A", "B"]
    assert [r["wanters"] for r in everything] == [3, 0]
    # per_realm corta pelo maior; realms sem procura ficam de fora
    assert [r["realm"] for r in realm_wanters(wants, realms, columns=[0])] == ["A"]
    assert realm_wanters(wants, realms, columns=[0], per_realm=1)[0]["realm"] == "A"
    assert realm_wanters(wants, realms, columns=[]) == []


def test_sale_pressure_labels_the_missing_cases():
    assert sale_pressure(0, 5) == (0.0, "sem procura")
    assert sale_pressure(10, 0) == (10.0, "sem anúncio")
    assert sale_pressure(10, 4) == (2.5, "")
