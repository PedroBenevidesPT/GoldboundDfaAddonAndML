"""Unit tests for the collector-demand join.

Fixtures only — no network, no browser, no DfA requests.
"""
import csv
import sqlite3
from pathlib import Path

import pytest

from woweconomy.analytics.collector_demand import (
    GAP_STRONG,
    GAP_WEAK,
    MIN_REALM_SAMPLE,
    AppearanceDemand,
    DemandBook,
    DemandRow,
    GapItem,
    GapReport,
    RealmDemand,
    analyse,
    demand_passes,
    item_appearances,
    load_appearance_demand,
    load_realm_demand,
    load_realm_market,
    print_realm_section,
    realm_rows,
    build_gap_items,
    drop_ev,
    gap_class,
    item_base,
    item_verdict,
    load_demand,
    load_inventory,
    band_labels,
    band_slug,
    demand_bands,
    mob_demand,
    normalize_name,
    parse_bands,
    parse_cuts,
    print_id_bands,
    print_item,
    print_report,
    resolve_items,
    IDS_CUTS,
    _money,
    write_csv,
    write_items_csv,
)
from woweconomy.analytics.farm_ranking import ItemMarket, NpcDrop
from woweconomy.services.blizzard_items import BlizzardItem


def mkrow(kind="appearances", item_id=1, name="Pagan Britches", miss=80.0,
          own=5.0, missing=80, collectors=100):
    value = miss * (100.0 - own) / 100.0
    return DemandRow(kind=kind, item_id=item_id, name=name, missing=missing,
                     collectors=collectors, missing_pct=miss, owned_pct=own,
                     value=value)


def mkbook(*rows):
    book = DemandBook()
    for r in rows:
        book.by_name.setdefault(normalize_name(r.name), []).append(r)
        book.sample[r.kind] = 100
        book.rows += 1
    return book


# --- name normalisation -------------------------------------------------

def test_recipe_prefix_decides_the_catalog():
    # "Pattern: X" is the recipe item, never the appearance of the crafted X;
    # both collapse to the same string, so the family has to break the tie.
    assert item_base("Pattern: Blue Dragonscale Breastplate") == (
        "blue dragonscale breastplate", "recipes")
    assert item_base("Blue Dragonscale Breastplate") == (
        "blue dragonscale breastplate", "")


def test_reins_of_the_is_a_mount():
    assert item_base("Reins of the Brown Horse") == ("brown horse", "mounts")
    assert normalize_name("Reins of the Brown Horse") == "brown horse"


def test_punctuation_and_case_do_not_matter():
    assert normalize_name("Mystic's  Gloves") == "mystic s gloves"
    assert normalize_name("  MYSTIC'S GLOVES ") == "mystic s gloves"


# --- matching -----------------------------------------------------------

def test_exact_name_wins_before_suffix_stripping():
    # The suffix must not eat a real item name.
    book = mkbook(mkrow(name="Slippers of the Hissing Cult", item_id=2))
    hits, rule = book.match_detail("Slippers of the Hissing Cult")
    assert rule == "exato"
    assert [h.item_id for h in hits] == [2]


def test_random_enchant_suffix_falls_back_to_base_appearance():
    # "Pagan Britches of the Fireflash" is the same transmog as Pagan Britches.
    book = mkbook(mkrow(name="Pagan Britches", item_id=3))
    hits, rule = book.match_detail("Pagan Britches of the Fireflash")
    assert rule == "base"
    assert hits[0].item_id == 3


def test_suffix_fallback_never_invents_a_match():
    book = mkbook(mkrow(name="Pagan Britches", item_id=3))
    hits, rule = book.match_detail("Plains Ring of the Bear")
    assert hits == [] and rule == ""


def test_appearance_ids_are_never_joined_by_number():
    # Appearance ids and item ids share a numeric range; joining by id would
    # match item 4672 to appearance 4672 by coincidence.
    book = mkbook(mkrow(kind="appearances", item_id=4672, name="Ancestral Belt"))
    hits, _ = book.match_detail("Something Else Entirely")
    assert hits == []


def test_recipe_item_does_not_match_the_appearance_of_its_product():
    book = mkbook(mkrow(kind="appearances", name="Blue Dragonscale Breastplate",
                        item_id=10))
    hits, rule = book.match_detail("Pattern: Blue Dragonscale Breastplate")
    assert hits == [] and rule == ""


# --- thresholds ---------------------------------------------------------

def test_gap_class_boundaries():
    assert gap_class(None) == "-"
    assert gap_class(GAP_STRONG) == "COLECTOR"
    assert gap_class(GAP_STRONG - 0.01) == "NICHO"
    assert gap_class(GAP_WEAK) == "NICHO"
    assert gap_class(GAP_WEAK - 0.01) == "COMUM"


def test_gap_item_attaches_demand_and_marks_rule():
    book = mkbook(mkrow(name="Pagan Britches", miss=90.0, own=2.0))
    market = {4: ItemMarket(4, "Pagan Britches", 10000.0, 0.5, 3.0)}
    items = build_gap_items(
        [GapItem(item_id=4, name="Pagan Britches of the Bear")], book, market)
    assert items[0].gap == pytest.approx(90.0)
    assert items[0].match_rule == "base"
    assert items[0].price == pytest.approx(10000.0)  # filled from the market


def test_no_price_is_not_zero_filled():
    book = mkbook(mkrow(name="Ghost Item"))
    items = build_gap_items([GapItem(item_id=9, name="Ghost Item")], book, {})
    assert items[0].price == 0.0


# --- loaders ------------------------------------------------------------

def test_load_demand_reads_all_three_books(tmp_path):
    for kind in ("mounts", "recipes", "appearances"):
        (tmp_path / f"demand_{kind}.csv").write_text(
            "kind,item_id,name,source,faction,missing,collectors,missing_pct,"
            "owned_pct,value,wowhead,note\n"
            f"{kind},7,Thing {kind},Drop,,60,100,60.00,10.0,54.0,,,\n",
            encoding="utf-8")
    book = load_demand(tmp_path)
    assert book.rows == 3
    assert book.sample == {"mounts": 100, "recipes": 100, "appearances": 100}
    assert book.match("Thing mounts")[0].kind == "mounts"


def test_missing_csv_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="--dfa-dir"):
        load_demand(tmp_path)


def test_load_inventory_accepts_both_export_formats(tmp_path):
    bag = tmp_path / "inv.csv"
    bag.write_text("ItemID;Name;Quality;iLvl;BonusIDs;Modifiers;Quantity\n"
                   "1515;Rough Wooden Staff;;0;6657;9=87;2\n"
                   "1515;Rough Wooden Staff;;0;6657;9=87;1\n"
                   "6661;Recipe: Savory Deviate Delight;;0;;;1\n",
                   encoding="utf-8-sig")
    rows = {r.item_id: r for r in load_inventory(str(bag))}
    assert rows[1515].qty == 3  # duplicates summed: that is what you can list
    assert rows[6661].name == "Recipe: Savory Deviate Delight"


def test_load_inventory_reads_the_priced_file_back(tmp_path):
    priced = tmp_path / "inventory_priced.csv"
    with priced.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Name", "Qty", "ItemID", "Min", "Normal"])
        w.writerow(["Quarter Staff", "2", "854", "179", "248"])
    rows = load_inventory(str(priced))
    assert (rows[0].item_id, rows[0].qty) == (854, 2.0)


def test_load_inventory_without_id_column_fails_loudly(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Name;Qty\nfoo;1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="item-id"):
        load_inventory(str(bad))


# --- binding / era: out of the EV, still in the report -------------------

ERA = {271435: BlizzardItem(271435, "Slippers of the Hissing Cult", "EPIC",
                            "ON_ACQUIRE", 219, 0, "", era=2)}
BOP = {100: BlizzardItem(100, "Bound Blade", "RARE", "ON_ACQUIRE", 60, 500)}
BOE = {25: BlizzardItem(25, "Worn Shortsword", "COMMON", "ON_EQUIP", 1, 0)}


def mkmarket(*ids, price=10000.0, sr=0.5, spd=10.0):
    return {i: ItemMarket(i, f"item-{i}", price, sr, spd) for i in ids}


def test_era_classic_item_has_no_ev_but_stays_listed():
    book = mkbook(mkrow(name="Slippers of the Hissing Cult"))
    items = build_gap_items(
        [GapItem(item_id=271435, name="Slippers of the Hissing Cult",
                 drop_rate=0.01)],
        book, mkmarket(271435), ERA)
    it = items[0]
    assert it.excluded == "era"
    assert it.era == 2
    assert it.price == pytest.approx(10000.0)  # the price is shown, not used
    assert it.ev == 0.0 and it.ev_collector == 0.0
    assert it.demand is not None               # the audience is still real


def test_bop_item_has_no_ev():
    items = build_gap_items([GapItem(item_id=100, name="Bound Blade",
                                     drop_rate=0.5)], mkbook(),
                           mkmarket(100), BOP)
    assert items[0].excluded == "bop"
    assert items[0].ev == 0.0


def test_boe_item_keeps_its_ev():
    items = build_gap_items([GapItem(item_id=25, name="Worn Shortsword",
                                     drop_rate=0.5)], mkbook(),
                           mkmarket(25), BOE)
    assert items[0].excluded == ""
    assert items[0].binding == "ON_EQUIP"
    assert items[0].ev > 0


def test_without_facts_nothing_is_excluded():
    # No DB2 import yet: behave exactly as before, and say so in the note
    # (main() prints the warning), instead of guessing from the name.
    items = build_gap_items(
        [GapItem(item_id=271435, name="Slippers of the Hissing Cult",
                 drop_rate=0.01)], mkbook(), mkmarket(271435))
    assert items[0].excluded == ""
    assert items[0].ev > 0


def test_report_counts_the_reasons_and_skips_them_in_the_showcase():
    rows = [
        GapItem(item_id=271435, name="Era", drop_rate=0.01),
        GapItem(item_id=100, name="Bop", drop_rate=0.5),
        GapItem(item_id=25, name="Boe", drop_rate=0.5, qty=2.0),
    ]
    book = mkbook(mkrow(name="Era", miss=90.0), mkrow(name="Bop", miss=90.0),
                  mkrow(name="Boe", miss=90.0))
    report = analyse(rows, book, mkmarket(271435, 100, 25), "t", facts={**ERA, **BOP, **BOE})
    assert len(report.excluded) == 2
    assert report.excluded_count("era") == 1
    assert report.excluded_count("bop") == 1
    assert report.market_value == pytest.approx(20000.0)  # BoE 10000 x qty 2


def test_csv_carries_the_reason(tmp_path):
    book = mkbook(mkrow(name="Slippers of the Hissing Cult"))
    report = analyse([GapItem(item_id=271435, name="Slippers of the Hissing Cult",
                              drop_rate=0.01)], book, mkmarket(271435),
                     "t", facts=ERA)
    out = tmp_path / "gap.csv"
    write_csv(report, str(out), with_drop=True)
    with out.open(encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        row = dict(zip(header, fh.readline().strip().split(",")))
    assert {"era", "binding", "excluded"} <= set(header)
    assert row["era"] == "2" and row["excluded"] == "era"


# --- demand by appearance id (the DfA collections) ----------------------

def test_appearance_demand_picks_the_worst_still_missing_look():
    d = AppearanceDemand(missing={10: 60, 11: 90}, sample=100,
                         rarity={10: 50.0, 11: 10.0})
    row = d.row_for([10, 11])
    assert row.item_id == 11 and row.missing == 90
    assert row.missing_pct == pytest.approx(90.0)
    assert row.owned_pct == pytest.approx(10.0)
    assert row.value == pytest.approx(81.0)   # 90 x (100-10)/100
    assert d.row_for([]) is None
    assert d.row_for([999]) is None           # nobody lacks it


def test_zone_report_shows_the_boe_missing_section(capsys):
    items = [GapItem(item_id=25, name="Slayer's Slippers", drop_rate=0.01)]
    report = analyse(items, mkbook(), {}, "zona", facts=BOE,
                     appearances={25: [153]},
                     demand_by_appearance=AppearanceDemand(
                         missing={153: 2287}, sample=2307))
    print_report(report, top_n=5, with_drop=True)
    out = capsys.readouterr().out
    assert "BoE QUE A AMOSTRA AINDA NAO TEM (1 de 1)" in out
    assert "2287/2307" in out


def test_boe_section_skips_items_nobody_is_missing():
    items = [GapItem(item_id=25, name="Slayer's Slippers", drop_rate=0.01)]
    report = analyse(items, mkbook(), {}, "zona", facts=BOE,
                     appearances={25: [153]},
                     demand_by_appearance=AppearanceDemand(missing={}, sample=2307))
    assert report.boe_missing == []


def test_id_join_wins_over_a_name_match():
    # The name is a decoy: the item's own appearance is what the collectors
    # actually lack, and the id is the only key that says which.
    book = mkbook(mkrow(kind="recipes", item_id=1, name="Fine Sword"))
    items = [GapItem(item_id=25, name="Fine Sword", drop_rate=0.5)]
    out = build_gap_items(items, book, {}, appearances={25: [153]},
                          demand_by_appearance=AppearanceDemand(
                              missing={153: 80}, sample=100))
    assert out[0].match_rule == "aparencia"
    assert out[0].demand.kind == "appearances"
    assert out[0].gap == pytest.approx(80.0)


def test_appearance_without_missing_does_not_fall_back_to_the_name():
    book = mkbook(mkrow(kind="appearances", item_id=1, name="Fine Sword"))
    out = build_gap_items([GapItem(item_id=25, name="Fine Sword")], book, {},
                          appearances={25: [153]},
                          demand_by_appearance=AppearanceDemand(missing={},
                                                                sample=100))
    assert out[0].match_rule == "aparencia-comum"
    assert out[0].matches == []


def test_item_without_appearance_still_matches_by_name():
    book = mkbook(mkrow(kind="recipes", name="Pattern: Fine Sword"))
    out = build_gap_items([GapItem(item_id=42, name="Pattern: Fine Sword")],
                          book, {}, appearances={},
                          demand_by_appearance=AppearanceDemand())
    assert out[0].match_rule == "exato"


def test_load_appearance_demand_prefers_the_scan_db(tmp_path):
    con = sqlite3.connect(tmp_path / "dfa.db")
    con.executescript("""
        CREATE TABLE missing (kind TEXT, item_id INTEGER, n INTEGER);
        CREATE TABLE scanned (kind TEXT, account TEXT, scanned_at TEXT);
    """)
    con.execute("INSERT INTO missing VALUES ('appearances', 153, 2)")
    con.execute("INSERT INTO missing VALUES ('appearances', 154, 0)")  # n=0: dropped
    con.executemany("INSERT INTO scanned VALUES ('appearances', ?, 'x')",
                    [("a",), ("b",), ("c",), ("d",)])
    con.commit()
    con.close()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "appearancesrarity.json").write_text(
        '{"appearances": {"153": 40.0}}', encoding="utf-8")
    d = load_appearance_demand(tmp_path)
    assert d.source == "dfa.db::missing"
    assert d.sample == 4 and d.missing == {153: 2}
    row = d.row_for([153])
    assert row.missing_pct == pytest.approx(50.0)
    assert row.owned_pct == pytest.approx(40.0)


def test_item_appearances_reads_the_bridge(tmp_path):
    con = sqlite3.connect(tmp_path / "i.db")
    con.execute("CREATE TABLE db2_item_appearance (itemId INT, appearanceId INT)")
    con.executemany("INSERT INTO db2_item_appearance VALUES (?,?)",
                    [(25, 153), (25, 154), (26, 155)])
    con.commit()
    con.close()
    assert item_appearances(str(tmp_path / "i.db"), [25, 26]) == {
        25: [153, 154], 26: [155]}
    assert item_appearances(str(tmp_path / "i.db"), []) == {}


def test_item_mode_annotates_and_zeroes_the_drop_ev():
    drop = NpcDrop(1, "Mob", 331, "ash", "drops", 271435, "Slippers", 0.01)
    views = resolve_items(["271435"], mkbook(), mkmarket(271435), [], [drop],
                          facts=ERA)
    v = views[0]
    assert v.excluded == "era" and v.era == 2
    assert v.drop_ev(drop) == 0.0
    assert "FORA DO EV" in item_verdict(v)


# --- one item lookup ----------------------------------------------------

def test_item_by_id_reads_bag_market_and_wishlist():
    book = mkbook(mkrow(name="Pagan Britches", item_id=8231, miss=90.0, own=2.0))
    market = {4: ItemMarket(4, "Pagan Britches", 120_000.0, 0.5, 3.0)}
    bag = [GapItem(item_id=4, name="Pagan Britches", qty=3.0)]
    view = resolve_items(["4"], book, market, bag, [])[0]
    assert (view.item_id, view.name, view.qty) == (4, "Pagan Britches", 3.0)
    assert view.owned and view.gap == pytest.approx(90.0)
    assert view.matches[0].kind == "appearances"


def test_item_by_name_tolerates_the_random_enchant_suffix():
    market = {4: ItemMarket(4, "Pagan Britches", 10000.0)}
    view = resolve_items(["Pagan Britches of the Fireflash"], mkbook(), market,
                         [], [])[0]
    assert (view.item_id, view.name) == (4, "Pagan Britches")


def test_item_that_only_exists_in_a_wishlist_is_still_found():
    # You neither own it nor does any loaded zone drop it: the wishlist is the
    # only place it exists, and that is exactly the interesting case.
    book = mkbook(mkrow(name="Slippers of the Hissing Cult", miss=99.0, own=0.1))
    view = resolve_items(["Slippers of the Hissing Cult"], book, {}, [], [])[0]
    assert view.found and view.item_id == 0
    assert view.gap == pytest.approx(99.0)
    assert not view.owned


def test_unknown_id_is_not_reported_as_found():
    # An id nobody knows is not a hit just because it is a number.
    view = resolve_items(["999999999"], mkbook(), {}, [], [])[0]
    assert not view.found and view.item_id == 999999999
    assert view.matches == [] and view.drops == []


def test_unknown_item_suggests_close_names():
    market = {4: ItemMarket(4, "Pagan Britches", 1.0),
              5: ItemMarket(5, "Pagan Gloves", 1.0)}
    view = resolve_items(["Pagan Britchez"], mkbook(), market, [], [])[0]
    assert not view.found
    assert "Pagan Britches" in view.suggestions


def test_drop_rows_are_ranked_and_valued_per_kill():
    market = {4: ItemMarket(4, "Thing", 1_000_000.0, 0.05, 2.0)}
    drops = [NpcDrop(1, "Mob A", 16, "azshara", "drops", 4, "Thing", 0.001),
             NpcDrop(2, "Mob B", 16, "azshara", "drops", 4, "Thing", 0.01)]
    view = resolve_items(["4"], mkbook(), market, [], drops)[0]
    assert [d.npc_name for d in view.drops] == ["Mob B", "Mob A"]
    # 1% x 100g x (sr 5% x haircut 1.0) = 5s per kill
    assert view.drop_ev(view.drops[0]) == pytest.approx(500.0)


def test_verdict_names_the_absence_of_volume():
    book = mkbook(mkrow(name="Thing", miss=80.0, own=1.0))
    market = {4: ItemMarket(4, "Thing", 10000.0, 0.5, 0.0)}  # spd=0
    view = resolve_items(["4"], book, market, [], [])[0]
    assert "spd=0" in item_verdict(view)


def test_items_csv_carries_the_same_fields_as_the_report(tmp_path):
    book = mkbook(mkrow(name="Thing", miss=80.0, own=1.0))
    market = {4: ItemMarket(4, "Thing", 10000.0, 0.5, 2.0)}
    bag = [GapItem(item_id=4, name="Thing", qty=2.0)]
    drops = [NpcDrop(1, "Mob A", 16, "azshara", "drops", 4, "Thing", 0.01)]
    views = resolve_items(["4", "Nothing Here"], book, market, bag, drops)
    out = tmp_path / "items.csv"
    write_items_csv(views, str(out))
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 2
    assert rows[0]["name"] == "Thing" and rows[0]["qty"] == "2.0"
    assert rows[0]["classe"] == "COLECTOR" and rows[0]["drops"] == "1"
    assert rows[0]["drop_npc"] == "Mob A"
    assert rows[1]["found"] == "0" and rows[1]["item_id"] == "0"


def test_print_item_shows_all_three_views(capsys):
    book = mkbook(mkrow(name="Thing", miss=80.0, own=1.0))
    market = {4: ItemMarket(4, "Thing", 500_000.0, 0.2, 1.0)}
    bag = [GapItem(item_id=4, name="Thing", qty=2.0)]
    drops = [NpcDrop(1, "Mob A", 16, "azshara", "drops", 4, "Thing", 0.01)]
    print_item(resolve_items(["4"], book, market, bag, drops)[0], "inv.csv")
    out = capsys.readouterr().out
    assert "inventario : 2 un." in out
    assert "COLECTOR" in out and "Mob A" in out
    assert "leitura" in out


# --- demand per realm ---------------------------------------------------

def test_realm_demand_reads_only_the_asked_ids(tmp_path):
    con = sqlite3.connect(tmp_path / "dfa.db")
    con.executescript("""
        CREATE TABLE missing_realm (kind TEXT, item_id INTEGER, region TEXT,
                                    realm TEXT, n INTEGER, w REAL);
        CREATE TABLE scanned (kind TEXT, account TEXT, scanned_at TEXT,
                              by_realm INTEGER);
        CREATE TABLE account (account TEXT, region TEXT, realm TEXT, name TEXT);
    """)
    con.executemany("INSERT INTO missing_realm VALUES ('appearances', ?, 'EU',"
                    " 'Stormreaver', ?, 1.0)",
                    [(153, 3), (154, 3), (999, 3)])   # 999 nao e' pedido
    con.execute("INSERT INTO missing_realm VALUES ('appearances', 155, 'EU',"
                " 'Stormreaver', 0, 0.0)")            # n=0: ninguem falta
    con.executemany("INSERT INTO scanned VALUES ('appearances', ?, 'x', 1)",
                    [("a",), ("b",)])
    con.execute("INSERT INTO scanned VALUES ('appearances', 'c', 'x', 0)")
    con.executemany("INSERT INTO account VALUES (?, 'EU', 'Stormreaver', ?)",
                    [("a", "Ana"), ("b", "Bia"), ("c", "Cris")])
    con.commit()
    con.close()
    d = load_realm_demand(tmp_path, [153, 154, 155])
    assert d.source == "dfa.db::missing_realm"
    # 'c' foi lido sem carimbo de realm: nao entra no denominador
    assert d.sample == {"Stormreaver": 2}
    assert d.missing == {"Stormreaver": {153: 3, 154: 3}}


def test_realm_demand_without_ids_is_empty_not_a_crash(tmp_path):
    (tmp_path / "dfa.db").write_bytes(b"")
    d = load_realm_demand(tmp_path, [])
    assert d.missing == {} and d.sample == {}
    assert load_realm_demand(tmp_path, [1]).source.startswith("erro:")


def test_realm_market_keeps_auctions_and_drops_commodities(tmp_path):
    con = sqlite3.connect(tmp_path / "tsm.db")
    con.execute("CREATE TABLE tsm_market (itemId INT, realm TEXT, min_buyout INT,"
                " market REAL, num_auctions INT)")
    con.executemany("INSERT INTO tsm_market VALUES (?,?,?,?,?)",
                    [(10, "Stormreaver", 400_000, 500_000.0, 3),
                     (11, "Stormreaver", 0, 0.0, 0),
                     (10, "", 0, 1.0, 1)])
    con.commit()
    con.close()
    assert load_realm_market(str(tmp_path / "tsm.db")) == {
        "Stormreaver": {10: (500_000.0, 3), 11: (0.0, 0)}}
    assert load_realm_market("") == {}


def _realm_report():
    items = [GapItem(item_id=10, name="A", matches=[mkrow(item_id=153)]),
             GapItem(item_id=11, name="B", matches=[mkrow(item_id=154)])]
    return GapReport(title="t", items=items)


def test_realm_rows_rate_is_the_average_missing_share():
    demand = RealmDemand(missing={"Big": {153: 30, 154: 10}, "Small": {153: 1}},
                         sample={"Big": 100, "Small": 2}, source="s")
    # item 11 esta' anunciado com num_auctions=0: ninguem o listou, nao e' preco.
    market = {"Big": {10: (500_000.0, 3), 11: (100_000.0, 0)}}
    rows = realm_rows(_realm_report(), demand, market, appearances={10: [153],
                                                                  11: [154]})
    # Big: (30+10)/(100 x 2) = 20%; Small: 1/(2 x 1) = 50%.  A fatia de 2 contas
    # e' que le' mais alto, e e' por isso que vai para o fim da tabela.
    assert [(r[0], round(r[7], 1)) for r in rows] == [("Big", 20.0), ("Small", 50.0)]
    big = rows[0]
    assert (big[1], big[2], big[3], big[4]) == (100, 2, 2, 40)
    assert big[5] == 1                       # so' o item de 3 anuncios
    assert big[6] == pytest.approx(500_000.0)  # mediana dos anunciados


def test_realm_rows_put_thin_slices_last_but_still_show_them():
    demand = RealmDemand(missing={"Thin": {153: 1}, "Solid": {153: 10}},
                         sample={"Thin": 1, "Solid": 20}, source="s")
    rows = realm_rows(_realm_report(), demand, {}, appearances={10: [153]})
    assert [r[0] for r in rows] == ["Solid", "Thin"]
    assert rows[0][7] == pytest.approx(50.0) and rows[1][7] == pytest.approx(100.0)


def test_realm_rows_ignore_items_without_demand():
    report = GapReport(title="t", items=[
        GapItem(item_id=10, name="A", matches=[mkrow(item_id=153)]),
        GapItem(item_id=12, name="C")])       # sem wishlist
    rows = realm_rows(report, RealmDemand(missing={"R": {153: 5}}, sample={"R": 5}),
                      {}, appearances={10: [153], 12: [199]})
    assert rows[0][3] == 1                     # denominador: 1 item, nao 2


def test_print_realm_section_flags_thin_slices_and_prices(capsys):
    demand = RealmDemand(missing={"Stormreaver": {153: 40, 154: 5},
                                  "Sylvanas": {153: 1}},
                         sample={"Stormreaver": 500, "Sylvanas": 3},
                         source="dfa.db::missing_realm")
    print_realm_section(_realm_report(), demand,
                        {"Stormreaver": {10: (500_000.0, 3)}},
                        appearances={10: [153], 11: [154]})
    out = capsys.readouterr().out
    assert "-- DEMANDA POR REALM" in out
    assert "503 contas atribuidas" in out
    assert "falta%" in out and "4.5" in out         # 45/(500 x 2)
    assert "50g" in out                             # 500.000 copper
    assert "~" in out and f"menos de {MIN_REALM_SAMPLE} contas" in out
    assert "so' onde o TSM varre" in out
    assert "AH partilhada" in out
    # a fatia fina (3 contas, 1 item) le' 33.3% mas vem depois da solida
    assert out.index("Stormreaver") < out.index("Sylvanas")
    assert "nao um censo" in out


def test_print_realm_section_without_the_slice_explains_itself(capsys):
    print_realm_section(GapReport(title="t", items=[]), RealmDemand(), {})
    out = capsys.readouterr().out
    assert "-- DEMANDA POR REALM" in out
    assert "scan antigo" in out and "reweight" in out


# --- EV per mob and what a mob's loot still owes the collectors ----------

def mkdrop(npc_id, npc_name, item_id, item_name, rate=1.0, qty=1.0):
    """One Wowhead drop row: a mob, an item, and the chance that ties them."""
    return NpcDrop(npc_id=npc_id, npc_name=npc_name, zone=331,
                   zone_name="Ashenvale", category="", item_id=item_id,
                   item_name=item_name, drop_rate=rate, qty=qty)


def test_mob_ev_pays_each_mob_its_own_rate_not_the_items_best():
    # One item, two mobs, two very different rates: the mob that drops it
    # rarely must not inherit the rate of the item's best source.
    book = mkbook(mkrow(name="Alpha Blade", miss=90.0, own=10.0))
    drops = [mkdrop(1, "Alpha", 10, "Alpha Blade", rate=0.5),
             mkdrop(2, "Beta", 10, "Alpha Blade", rate=0.01)]
    items = build_gap_items([GapItem(item_id=10, name="Alpha Blade")],
                            book, mkmarket(10))
    mobs = mob_demand(drops, items)
    alpha, beta = (m for m in mobs)
    it = items[0]
    assert alpha.ev == pytest.approx(drop_ev(it, 0.5))
    assert beta.ev == pytest.approx(drop_ev(it, 0.01))
    assert mobs[0].npc_id == 1              # sorted by EV per kill
    assert alpha.top_item == "Alpha Blade"


def test_mob_lack_pct_weights_each_drop_by_its_own_chance():
    # The 90% drop is the one that decides the number; the 10% one must not
    # pull it the same way a plain average of the two gaps would.
    book = mkbook(mkrow(name="Rare Blade", miss=0.0, own=100.0),
                  mkrow(name="Wanted Blade", miss=100.0, own=0.0))
    drops = [mkdrop(1, "Mob", 10, "Rare Blade", rate=0.1),
             mkdrop(1, "Mob", 11, "Wanted Blade", rate=0.9)]
    items = build_gap_items([GapItem(item_id=10, name="Rare Blade"),
                             GapItem(item_id=11, name="Wanted Blade")],
                            book, mkmarket(10, 11))
    mob = mob_demand(drops, items)[0]
    assert mob.lack_pct == pytest.approx(90.0)      # (0.1x0 + 0.9x100) / 1.0
    assert mob.kill_lack_pct == pytest.approx(90.0)
    assert mob.measured == 2 and mob.drops == 2


def test_mob_kill_lack_is_the_chance_of_at_least_one_lacking_drop():
    book = mkbook(mkrow(name="A", miss=100.0, own=0.0),
                  mkrow(name="B", miss=100.0, own=0.0))
    drops = [mkdrop(1, "Mob", 10, "A", rate=0.5), mkdrop(1, "Mob", 11, "B", rate=0.5)]
    items = build_gap_items([GapItem(item_id=10, name="A"),
                             GapItem(item_id=11, name="B")], book, mkmarket(10, 11))
    mob = mob_demand(drops, items)[0]
    assert mob.lack_pct == pytest.approx(100.0)
    assert mob.kill_lack_pct == pytest.approx(75.0)  # 1 - 0.5 x 0.5


def test_mob_kill_lack_never_goes_negative_on_a_multi_unit_row():
    # A certainty that drops three at a time: 1 - 3 would be less than zero.
    book = mkbook(mkrow(name="A", miss=100.0, own=0.0))
    drops = [mkdrop(1, "Mob", 10, "A", rate=1.0, qty=3.0)]
    items = build_gap_items([GapItem(item_id=10, name="A")], book, mkmarket(10))
    assert mob_demand(drops, items)[0].kill_lack_pct == pytest.approx(100.0)


def test_mob_without_an_audience_keeps_its_ev_and_says_nothing_about_lack():
    # Nobody in the sample wants it: the price is still real, the audience is
    # not measured, and no percentage is invented for it.
    drops = [mkdrop(1, "Mob", 10, "Vendor Junk", rate=0.5)]
    items = build_gap_items([GapItem(item_id=10, name="Vendor Junk")],
                            mkbook(), mkmarket(10))
    mob = mob_demand(drops, items)[0]
    assert mob.measured == 0 and mob.lack_pct is None
    assert mob.ev > 0 and mob.ev_collector == 0.0
    assert mob.kill_lack_pct == 0.0


def test_mob_drop_list_is_ordered_by_who_still_lacks_the_drop():
    # The showcase is the drop most of the sample does NOT have, not the one
    # worth the most: the list is read for its audience.
    book = mkbook(mkrow(name="Cheap Blade", miss=95.0, missing=95, own=5.0),
                  mkrow(name="Pricey Blade", miss=40.0, missing=40, own=5.0))
    drops = [mkdrop(1, "Mob", 10, "Cheap Blade", rate=0.10),
             mkdrop(1, "Mob", 11, "Pricey Blade", rate=0.01)]
    items = [GapItem(item_id=10, name="Cheap Blade", drop_rate=0.10),
             GapItem(item_id=11, name="Pricey Blade", drop_rate=0.01)]
    market = {10: ItemMarket(10, "Cheap Blade", 500.0, 0.5, 10.0),
              11: ItemMarket(11, "Pricey Blade", 90_000.0, 0.5, 10.0)}
    items = build_gap_items(items, book, market)
    mob = mob_demand(drops, items)[0]
    assert mob.ev == pytest.approx(drop_ev(items[0], 0.10) + drop_ev(items[1], 0.01))
    assert items[1].price > items[0].price          # the pricey one is worth more
    assert mob.top_item == "Pricey Blade"           # ... and it is what pays
    assert [d.name for d in mob.drop_list] == ["Cheap Blade", "Pricey Blade"]
    assert mob.showcase.name == "Cheap Blade" and mob.showcase.missing == 95
    assert mob.showcase.lack_pct == pytest.approx(95.0)
    assert mob.showcase.collectors == 100


def test_mob_montra_is_the_price_of_each_sellable_drop_counted_once():
    # The showcase value is a portfolio, not a rate: one listing per item, and
    # a repeated drop row must not list the same item twice.
    book = mkbook(mkrow(name="Cheap Blade", miss=90.0, missing=90, own=5.0),
                  mkrow(name="Pricey Blade", miss=40.0, missing=40, own=5.0))
    drops = [mkdrop(1, "Mob", 10, "Cheap Blade", rate=0.10),
             mkdrop(1, "Mob", 10, "Cheap Blade", rate=0.02),
             mkdrop(1, "Mob", 11, "Pricey Blade", rate=0.01)]
    market = {10: ItemMarket(10, "Cheap Blade", 500.0, 0.5, 10.0),
              11: ItemMarket(11, "Pricey Blade", 90_000.0, 0.5, 10.0)}
    items = build_gap_items([GapItem(item_id=10, name="Cheap Blade"),
                             GapItem(item_id=11, name="Pricey Blade")],
                            book, market)
    mob = mob_demand(drops, items)[0]
    assert mob.market_value == pytest.approx(500.0 + 90_000.0)
    assert mob.ev < mob.market_value        # per kill, not the portfolio


def test_mob_montra_leaves_out_what_cannot_be_sold():
    # era-Classic and BoP have no TSM price in this game, so they are not
    # showcase value either -- the same items drop_ev already zeroes.
    book = mkbook(mkrow(name="Classic Blade", miss=90.0, missing=90, own=5.0),
                  mkrow(name="Soulbound Blade", miss=90.0, missing=90, own=5.0),
                  mkrow(name="Sellable Blade", miss=90.0, missing=90, own=5.0))
    drops = [mkdrop(1, "Mob", 10, "Classic Blade", rate=0.5),
             mkdrop(1, "Mob", 11, "Soulbound Blade", rate=0.5),
             mkdrop(1, "Mob", 12, "Sellable Blade", rate=0.5)]
    facts = {10: BlizzardItem(10, "Classic Blade", "RARE", "ON_EQUIP", 60, 500,
                              era=2),
             11: BlizzardItem(11, "Soulbound Blade", "RARE", "ON_ACQUIRE",
                              60, 500)}
    market = {10: ItemMarket(10, "Classic Blade", 1_000_000.0, 0.5, 10.0),
              11: ItemMarket(11, "Soulbound Blade", 2_000_000.0, 0.5, 10.0),
              12: ItemMarket(12, "Sellable Blade", 5_000.0, 0.5, 10.0)}
    items = build_gap_items([GapItem(item_id=10, name="Classic Blade"),
                             GapItem(item_id=11, name="Soulbound Blade"),
                             GapItem(item_id=12, name="Sellable Blade")],
                            book, market, facts)
    assert [i.excluded for i in items] == ["era", "bop", ""]
    mob = mob_demand(drops, items)[0]
    assert mob.market_value == pytest.approx(5_000.0)   # only the sellable one


def test_mob_order_follows_the_showcase_of_each_mob():
    # A mob whose best drop fewer people lack sorts below one whose best drop
    # almost nobody has, even when it pays more per kill.
    book = mkbook(mkrow(name="Wanted", miss=99.0, missing=99, own=1.0),
                  mkrow(name="Common", miss=10.0, missing=10, own=90.0))
    drops = [mkdrop(1, "Rich Mob", 11, "Common", rate=0.9),
             mkdrop(2, "Poor Mob", 10, "Wanted", rate=0.05)]
    market = {10: ItemMarket(10, "Wanted", 1_000.0, 0.9, 20.0),
              11: ItemMarket(11, "Common", 100_000.0, 0.9, 20.0)}
    items = build_gap_items([GapItem(item_id=10, name="Wanted", drop_rate=0.05),
                             GapItem(item_id=11, name="Common", drop_rate=0.9)],
                            book, market)
    mobs = mob_demand(drops, items)
    assert mobs[0].npc_name == "Poor Mob"
    assert mobs[0].ev < mobs[1].ev                  # ... though it pays less


def test_zone_report_lists_every_mob_with_its_drops(capsys):
    book = mkbook(mkrow(name="Wanted Blade", miss=90.0, missing=90, own=5.0),
                  mkrow(name="Common Blade", miss=10.0, missing=10, own=90.0))
    drops = [mkdrop(7, "Ashenvale Bear", 10, "Wanted Blade", rate=0.3),
             mkdrop(7, "Ashenvale Bear", 11, "Common Blade", rate=0.5)]
    items = build_gap_items(
        [GapItem(item_id=10, name="Wanted Blade", drop_rate=0.3),
         GapItem(item_id=11, name="Common Blade", drop_rate=0.5)],
        book, mkmarket(10, 11))
    report = analyse(items, book, mkmarket(10, 11), "zona")
    report.mobs = mob_demand(drops, report.items)
    print_report(report, top_n=5, with_drop=True, mob_drops=1)
    out = capsys.readouterr().out
    assert "DROPS POR MOB (1 mobs)" in out
    assert "falta/kill%" in out
    body = out.split("-- DROPS POR MOB")[1].split("-- BoE")[0]
    assert len([ln for ln in body.splitlines() if "Ashenvale Bear" in ln]) == 1
    assert "Wanted Blade" in body                    # the showcase drop
    assert "Common Blade" not in body                # capped by --mob-drops 1
    assert "montra" in out                            # the mob's portfolio column
    assert _money(sum(i.price for i in report.items)) in body
    print_report(report, top_n=5, with_drop=True, mob_drops=0)
    body = capsys.readouterr().out.split("-- DROPS POR MOB")[1].split("-- BoE")[0]
    assert "Common Blade" in body                     # 0 = todos os drops


# --- ids for a TSM group import ----------------------------------------

def mk_band_items(gaps):
    """GapItems with the given missing shares, in the same order."""
    items = []
    for n, gap in enumerate(gaps, start=1):
        d = mkrow(item_id=100 + n, name=f"Item {n}", miss=gap, own=0.0,
                  missing=int(gap), collectors=100)
        items.append(GapItem(item_id=100 + n, name=f"Item {n}", matches=[d]))
    return items


def test_demand_bands_splits_at_the_cuts_inclusive():
    items = mk_band_items([90.0, 85.0, 84.9, 70.0, 65.0, 64.9, 0.0])
    bands = demand_bands(items)
    assert IDS_CUTS == (85.0, 65.0)
    assert list(bands) == ["85+", "65-85", "<65"]      # top first, tail last
    assert bands["85+"] == [101, 102]                   # 85.0 still counts
    assert bands["65-85"] == [103, 104, 105]            # 65.0 still counts
    assert bands["<65"] == [106, 107]


def test_demand_bands_five_cuts_make_five_groups():
    # More separations are just more cuts: 4 cuts = 5 groups, in order.
    items = mk_band_items([90.0, 80.0, 70.0, 60.0, 50.0])
    bands = demand_bands(items, parse_cuts("85,75,65,55"))
    assert list(bands) == ["85+", "75-85", "65-75", "55-65", "<55"]
    assert [bands[k] for k in bands] == [[101], [102], [103], [104], [105]]
    assert band_labels((85.0, 65.0)) == ["85+", "65-85", "<65"]


def test_demand_bands_skip_what_cannot_be_listed_or_wanted():
    # era-Classic has no price in this game, BoP never reaches the AH, and an
    # item nobody in the sample wants is not a group of anything.
    items = mk_band_items([90.0, 90.0, 90.0])
    items[0].excluded = "era"
    items[1].excluded = "bop"
    no_demand = GapItem(item_id=999, name="Nobody Wants It")
    bands = demand_bands(items + [no_demand])
    assert bands["85+"] == [103]
    assert all(999 not in ids for ids in bands.values())


def test_demand_bands_count_each_item_id_once():
    # Two drop rows of one item (two NPCs) must not produce the id twice: the
    # TSM import would just list it twice.
    items = mk_band_items([90.0, 90.0])
    items[1].item_id = items[0].item_id
    bands = demand_bands(items)
    assert bands["85+"] == [101]


def test_print_id_bands_writes_one_comma_line_per_band(capsys, tmp_path):
    items = mk_band_items([90.0, 70.0, 30.0])
    bands = demand_bands(items)
    print_id_bands(bands, "zona", IDS_CUTS, ["85+", "65-85"],
                   note="nota", out_prefix=str(tmp_path / "mogs"))
    out = capsys.readouterr().out
    assert "85+ (falta >= 85%): 1 ids" in out
    assert "\ni:101\n" in out                  # the line to paste, alone
    assert "65-85 (falta 65-85%): 1 ids" in out
    assert "\ni:102\n" in out
    assert "<65" not in out                     # not asked for
    assert (tmp_path / "mogs_85plus.txt").read_text().strip() == "i:101"
    assert (tmp_path / "mogs_65-85.txt").read_text().strip() == "i:102"
    assert not (tmp_path / "mogs_abaixo65.txt").exists()


def test_band_slugs_are_file_names():
    assert band_slug("85+") == "85plus"
    assert band_slug("75-85") == "75-85"
    assert band_slug("<55") == "abaixo55"


def test_parse_cuts_sorts_dedupes_and_refuses_junk():
    assert parse_cuts("65,85") == [85.0, 65.0]          # order does not matter
    assert parse_cuts("85, 85,75") == [85.0, 75.0]      # a repeat is one cut
    assert parse_cuts("85.5,65") == [85.5, 65.0]
    for bad in ("", "alta", "85,101", "-5"):
        with pytest.raises(SystemExit) as exc:
            parse_cuts(bad)
        assert exc.value.code == 2


def test_parse_bands_takes_labels_positions_aliases_and_refuses_typos():
    labels = band_labels((85.0, 75.0, 65.0, 55.0))
    assert parse_bands("65-75", labels) == ["65-75"]
    assert parse_bands("2", labels) == ["75-85"]        # 1-based position
    assert parse_bands("alta,nicho", labels) == ["85+", "75-85"]
    assert parse_bands("comum", labels) == ["<55"]      # the tail
    assert parse_bands("todas", labels) == labels
    assert parse_bands("<55,85+", labels) == ["<55", "85+"]
    for bad in ("", "alta,altissima", "9"):
        with pytest.raises(SystemExit) as exc:
            parse_bands(bad, labels)
        assert exc.value.code == 2


# --- --min-score (the DfA value cut) ------------------------------------

def test_demand_passes_scores_the_dfa_value():
    weak = mkrow(miss=20.0, own=90.0)       # value = 2.0
    strong = mkrow(miss=90.0, own=10.0)     # value = 81.0
    assert demand_passes(strong, 50) is True
    assert demand_passes(weak, 50) is False
    assert demand_passes(weak, 0) is True   # 0 e' "sem filtro"
    assert demand_passes(None, 50) is False  # sem procura, sem valor
    assert demand_passes(DemandRow("mounts", 1, "X"), 10) is False


def test_analyse_min_score_cuts_rows_and_summaries_together():
    items = [GapItem(item_id=1, name="Strong", drop_rate=0.5),
             GapItem(item_id=2, name="Weak", drop_rate=0.5)]
    book = mkbook(mkrow(item_id=1, name="Strong", miss=90.0, own=10.0),
                  mkrow(item_id=2, name="Weak", miss=20.0, own=90.0))
    report = analyse(items, book, {}, "t", min_score=50)
    assert [i.name for i in report.items] == ["Strong"]
    assert [i.name for i in report.with_demand] == ["Strong"]
    assert len(report.strong) == 1
    plain = analyse(items, book, {}, "t")          # default = tudo
    assert len(plain.items) == 2

