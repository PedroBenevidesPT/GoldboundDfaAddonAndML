"""Farming on realizable returns, and an auditable experiment ledger."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from woweconomy.intel import ah, farming, ledger, store


def make_tsm_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE tsm_realm_group (realm_slug TEXT, realm_name TEXT,
        group_id INTEGER, canonical_slug TEXT, canonical_name TEXT, is_canonical INTEGER);
    CREATE TABLE tsm_realm_price (scope TEXT, item_id INTEGER, name TEXT,
        market_value INTEGER, min_buyout INTEGER, recent INTEGER, historical INTEGER,
        updated_at TEXT, fetched_at TEXT, snapshot_id INTEGER);
    CREATE TABLE tsm_region_price (key_id INTEGER, name TEXT, market_value INTEGER,
        historical INTEGER, avg_sale_price INTEGER, sale_rate REAL, sold_per_day REAL,
        updated_at TEXT, fetched_at TEXT, snapshot_id INTEGER);
    CREATE TABLE auction_intel_events (event_id TEXT PRIMARY KEY, event_type TEXT,
        ts INTEGER, item_id INTEGER, item_name TEXT, quantity INTEGER,
        unit_price_copper INTEGER, realm TEXT, character TEXT, counterparty TEXT,
        source TEXT, confidence TEXT);
    """)
    con.execute("INSERT INTO tsm_realm_group VALUES"
                " ('dragonmaw','Dragonmaw',1,'dragonmaw','Dragonmaw',1)")
    for item_id, name, market in ((100, "Herb", 90_000),
                                  (200, "Ore", 80_000),
                                  (300, "Shiny", 1_000_000),
                                  (400, "Thin", 70_000)):
        con.execute("INSERT INTO tsm_realm_price VALUES"
                    " ('dragonmaw',?,?,?,0,0,0,'','',0)",
                    (item_id, name, market))
    con.execute("INSERT INTO tsm_region_price VALUES"
                " (200,'Ore',80000,0,50000,0.4,100.0,'','',0)")
    con.execute("INSERT INTO tsm_region_price VALUES"
                " (400,'Thin',70000,0,40000,0.2,10.0,'','',0)")
    for i in range(10):
        con.execute("INSERT INTO auction_intel_events VALUES"
                    " (?,'sale',?,100,'Herb',2,10000,'Dragonmaw','B','',"
                    "'journalator','verified')",
                    (f"h{i}", i * 86_400))
    con.execute("INSERT INTO auction_intel_events VALUES"
                " ('t1','sale',1000,400,'Thin',1,9000,'Dragonmaw','B','',"
                "'journalator','verified')")

    con.commit()
    con.close()


def make_intel():
    con = store.memory_connection()
    payload = {"auctions": [
        {"id": i, "item": {"id": 100}, "quantity": 5, "unit_price": 9500,
         "time_left": "SHORT"} for i in range(1, 3)]}
    ah.ingest_bundle(con, ah.parse_payload(payload, region="eu",
                                           realm="Dragonmaw", captured_at=1000))
    return con


def drop(npc_id, npc_name, item_id, name, rate, qty=1.0):
    return SimpleNamespace(npc_id=npc_id, npc_name=npc_name, zone_name="Azshara",
                           item_id=item_id, item_name=name, drop_rate=rate, qty=qty)


def test_the_price_hierarchy_prefers_confirmed_realm_sales():
    cfg = farming.FarmConfig(min_observed_sales=5)
    confirmed = farming.resolve_price(farming.PricePoint(
        item_id=1, observed_sales=10, observed_unit_price=10_000,
        region_sale_avg=50_000), cfg)
    assert confirmed.realizable_unit_price == 10_000
    assert confirmed.price_confidence == "high"
    assert "confirmed realm" in confirmed.price_basis

    regional = farming.resolve_price(farming.PricePoint(
        item_id=2, region_sale_avg=50_000, region_sale_rate=0.4), cfg)
    assert regional.realizable_unit_price == 35_000
    # a regional sale rate is not evidence about this realm
    assert regional.price_confidence == "low"
    assert "discounted" in regional.price_basis

    asking_only = farming.resolve_price(farming.PricePoint(
        item_id=3, market_value=1_000_000, min_buyout=900_000), cfg)
    assert asking_only.realizable_unit_price is None
    assert "asking price is not a realizable price" in asking_only.price_basis


def test_a_thin_sample_is_marked_low_confidence():
    point = farming.resolve_price(farming.PricePoint(
        item_id=4, observed_sales=1, observed_unit_price=9_000,
        region_sale_avg=40_000), farming.FarmConfig(min_observed_sales=5))
    assert point.realizable_unit_price == 9_000
    assert point.price_confidence == "low"
    assert "thin" in point.price_basis


def test_liquidity_and_competition_cap_the_realizable_rate(tmp_path):
    db = tmp_path / "tsm.db"
    make_tsm_db(db)
    book = farming.build_price_book(db, make_intel(), "eu", "Dragonmaw",
                                    [100, 200, 300, 400])
    assert book[100].observed_sales == 10
    assert book[100].observed_units_per_day == pytest.approx(20 / 9)
    assert book[100].competing_units == 10
    assert book[200].region_sale_avg == 50_000
    assert book[300].realizable_unit_price is None

    result = farming.recommend_farm(
        [drop(1, "Herbalist", 100, "Herb", 0.5),
         drop(1, "Herbalist", 300, "Shiny", 0.01)],
        book, farming.FarmConfig(kills_per_hour=20))
    rec = result["recommendations"][0]
    item = next(i for i in rec.items if i.item_id == 100)
    # gross rate 0.5 * 20 = 10 units/h; liquidity cap = (20/9)/24;
    # competition = 10 * 0.25 / 24, which is looser
    assert item.units_per_hour == 10
    assert item.liquidity_cap_per_hour == round((20 / 9) / 24, 4)
    assert item.competition_cap_per_hour == round(10 * 0.25 / 24, 4)
    assert item.realizable_units_per_hour == round((20 / 9) / 24, 4)
    assert item.realizable_gold_per_hour < item.gross_gold_per_hour
    assert rec.excluded[0]["item_id"] == 300
    assert "not a realizable price" in rec.excluded[0]["reason"]


def test_unknown_liquidity_is_labelled_as_an_assumption():
    book = {7: farming.resolve_price(farming.PricePoint(
        item_id=7, region_sale_avg=10_000), farming.FarmConfig())}
    result = farming.recommend_farm([drop(2, "Miner", 7, "Ore", 1.0)], book,
                                    farming.FarmConfig(kills_per_hour=10))
    item = result["recommendations"][0].items[0]
    assert "assuming" in item.liquidity_basis
    assert item.realizable_units_per_hour == 10 * 0.25


def test_stock_on_hand_removes_the_need_to_farm_more_of_it():
    book = {7: farming.resolve_price(farming.PricePoint(
        item_id=7, observed_sales=10, observed_unit_price=10_000,
        observed_units=20, observed_window_days=10.0), farming.FarmConfig())}
    # 2 units/day of demand, 20 units in stock, a 7-day horizon: the stock
    # already covers more than a week of sales, so nothing more is farmed.
    result = farming.recommend_farm(
        [drop(3, "Miner", 7, "Ore", 1.0)], book,
        farming.FarmConfig(kills_per_hour=10), inventory_units={7: 20})
    item = result["recommendations"][0].items[0]
    assert item.current_inventory_units == 20
    assert item.inventory_cap_per_hour == 0
    assert item.realizable_units_per_hour == 0
    assert "leaving 0.00/day" in item.inventory_note


def test_a_measured_npc_rate_replaces_the_global_default():
    book = {7: farming.resolve_price(farming.PricePoint(
        item_id=7, observed_sales=10, observed_unit_price=10_000,
        observed_units=40, observed_window_days=10.0), farming.FarmConfig())}
    cfg = farming.FarmConfig(kills_per_hour=10, kill_rates={1: 40})
    result = farming.recommend_farm(
        [drop(1, "Fast", 7, "Ore", 1.0), drop(2, "Slow", 7, "Ore", 1.0)],
        book, cfg)
    by_npc = {rec.npc_id: rec for rec in result["recommendations"]}
    assert by_npc[1].items[0].units_per_hour == 40
    assert by_npc[2].items[0].units_per_hour == 10
    assert "supplied" in by_npc[1].kill_rate_basis
    assert "not measured" in by_npc[2].kill_rate_basis
    assert result["kill_rates"]["defaulted_npcs"] == [2]


def test_competition_summary_counts_an_item_once_across_npcs():
    book = {7: farming.resolve_price(farming.PricePoint(
        item_id=7, region_sale_avg=10_000), farming.FarmConfig())}
    result = farming.recommend_farm(
        [drop(1, "A", 7, "Ore", 0.5), drop(2, "B", 7, "Ore", 0.5)], book)
    assert result["competition_data"]["items_priced"] == 1
    assert result["competition_data"]["items_without_listing_data"] == 1


def test_missing_competition_is_reported_as_missing_not_zeroed():
    book = {7: farming.resolve_price(farming.PricePoint(
        item_id=7, region_sale_avg=10_000), farming.FarmConfig())}
    result = farming.recommend_farm([drop(2, "Miner", 7, "Ore", 0.5)], book)
    item = result["recommendations"][0].items[0]
    assert item.competition_cap_per_hour is None
    assert item.competition_data_missing is True
    assert "not measured" in item.competition_basis
    assert result["competition_data"]["items_without_listing_data"] == 1
    assert result["competition_data"]["snapshot_id"] == ""


def test_not_listed_in_a_snapshot_differs_from_having_no_snapshot(tmp_path):
    db = tmp_path / "tsm.db"
    make_tsm_db(db)
    con = make_intel()
    book = farming.build_price_book(db, con, "eu", "Dragonmaw", [100, 200])
    assert book[100].competing_units == 10
    assert book[200].competition_checked is True
    assert book[200].competing_units is None
    result = farming.recommend_farm([drop(4, "Trader", 200, "Ore", 0.5)], book)
    item = result["recommendations"][0].items[0]
    assert "not listed in snapshot" in item.competition_basis
    assert item.competition_data_missing is True
    assert result["competition_data"]["snapshot_id"]


def test_kill_rates_are_validated(tmp_path):
    bad = tmp_path / "rates.json"
    bad.write_text(json.dumps({"12": 0}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be positive"):
        farming.load_kill_rates(bad)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"12": 30}), encoding="utf-8")
    assert farming.load_kill_rates(good) == {12: 30.0}


def test_recommendations_are_ranked_by_realizable_gold(tmp_path):
    db = tmp_path / "tsm.db"
    make_tsm_db(db)
    con = make_intel()
    zone = tmp_path / "zone.json"
    zone.write_text(json.dumps([{
        "npc_id": 10, "npc_name": "Miner", "zone": 16, "name": "azshara",
        "items": [{"item_id": 200, "item_name": "Ore", "drop_rate": 0.5, "qty": 1}],
    }]), encoding="utf-8")
    result = farming.recommend_for_realm(db, con, "eu", "Dragonmaw", [zone])
    assert result["realm"] == "Dragonmaw"
    assert result["recommendations"][0].npc_name == "Miner"
    assert result["recommendations"][0].realizable_gold_per_hour > 0
    assert "confirmed realm sales first" in result["basis_note"]


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def test_the_ledger_chains_and_verifies(tmp_path):
    log = ledger.ExperimentLedger(tmp_path / "ledger.jsonl")
    first = log.append("hypothesis", {"claim": "undercut gap predicts sale"},
                       ts="2026-10-20T00:00:00Z")
    log.append("action", {"ask": 1234, "snapshot": "abc"}, ts="2026-10-20T01:00:00Z")
    log.append("outcome", {"hypothesis_seq": first.seq, "sold": False},
               ts="2026-10-21T00:00:00Z")
    report = log.verify()
    assert report["ok"] is True and report["records"] == 3
    assert [r.kind for r in log.records()] == ["hypothesis", "action", "outcome"]


def test_editing_a_record_breaks_the_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    log = ledger.ExperimentLedger(path)
    log.append("hypothesis", {"claim": "a"})
    log.append("action", {"ask": 1})
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["claim"] = "b"
    lines[0] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = log.verify()
    assert report["ok"] is False
    assert report["at"] == 1
    assert "hash" in report["error"]


def test_leakage_is_a_window_overlap(tmp_path):
    log = ledger.ExperimentLedger(tmp_path / "ledger.jsonl")
    log.append("action", {"train_window": [100, 200], "test_window": [300, 400]})
    log.append("action", {"train_window": [100, 350], "test_window": [300, 400]})
    report = ledger.leakage_report(log.records())
    assert report["ok"] is False
    assert len(report["findings"]) == 1
    assert report["findings"][0]["seq"] == 2
