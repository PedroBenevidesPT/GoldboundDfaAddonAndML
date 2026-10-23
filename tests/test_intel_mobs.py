"""Kills, loot and gold per mob: counted once, priced three separate ways.

The addon records what happened; this is the reader.  The tests pin the
things that would make the numbers wrong rather than merely different:

* one death is one kill, however many times the client reports it;
* a reopened corpse is not a second drop;
* a re-import of the same batch stores nothing twice;
* kills per hour comes from the time actually spent killing;
* vendor, market and realised value stay apart, and a missing price is
  reported as missing instead of counted as zero.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from woweconomy.intel import farming, mobs, store


def row(batch="Bullbound:Dragonmaw:1792521600:1", npc=224654, name="Bristleback Boar",
        kills=3, loots=2, empty=1, copper=4500, first=None, last=None,
        items=None, realm="Dragonmaw", character="Bullbound"):
    now = int(time.time())
    first = now - 600 if first is None else first
    last = now if last is None else last
    return {
        "batchId": batch, "realm": realm, "character": character,
        "npcId": npc, "npcName": name, "kills": kills, "loots": loots,
        "empty": empty, "moneyCopper": copper, "firstAt": first, "lastAt": last,
        "items": items if items is not None else [
            {"itemId": 2589, "quantity": 4}],
    }


def _tsm_db(tmp_path: Path, *, snapshot_updated_at="2026-10-22T00:00:00Z",
            market=1000, region_avg=2000, vendor=10):
    path = tmp_path / "tsm.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE tsm_realm_group (realm_name TEXT, canonical_slug TEXT);
        CREATE TABLE tsm_realm_price (scope TEXT, item_id INTEGER, name TEXT,
            market_value REAL, min_buyout REAL);
        CREATE TABLE tsm_region_price (key_id INTEGER, name TEXT,
            avg_sale_price REAL, sale_rate REAL, sold_per_day REAL);
        CREATE TABLE tsm_snapshot (snapshot_id INTEGER, scope TEXT, kind TEXT,
            updated_at TEXT, fetched_at TEXT);
        CREATE TABLE blizzard_items (itemId INTEGER, sellPrice INTEGER);
        CREATE TABLE auction_intel_events (event_id TEXT, event_type TEXT,
            item_id INTEGER, realm TEXT, unit_price_copper INTEGER,
            quantity INTEGER, ts INTEGER);
        CREATE TABLE auction_events (event_id TEXT, event_type TEXT,
            item_id INTEGER, realm TEXT, unit_price_copper INTEGER,
            qty INTEGER, timestamp_unix INTEGER);
    """)
    con.execute("INSERT INTO tsm_realm_group VALUES ('Dragonmaw', 'dragonmaw')")
    con.execute("INSERT INTO tsm_realm_price VALUES ('dragonmaw', 2589,"
                " 'Linen Cloth', ?, 900)", (market,))
    con.execute("INSERT INTO tsm_region_price VALUES (2589, 'Linen Cloth', ?,"
                " 0.5, 100)", (region_avg,))
    con.execute("INSERT INTO tsm_snapshot VALUES (1, 'dragonmaw', 'realm_items',"
                " ?, '2026-10-22T00:00:00Z')", (snapshot_updated_at,))
    con.execute("INSERT INTO blizzard_items VALUES (2589, ?)", (vendor,))
    con.commit()
    con.close()
    return path


def _import(con, *rows):
    parsed = [mobs.parse_observation(r) for r in rows]
    return mobs.import_observations(con, [p for p in parsed if p])


# --- parsing ---------------------------------------------------------------

def test_a_row_keeps_its_kills_items_and_copper():
    parsed = mobs.parse_observation(row())
    assert parsed["npc_id"] == 224654
    assert parsed["kills"] == 3
    assert parsed["money_copper"] == 4500
    assert parsed["items"] == [{"item_id": 2589, "quantity": 4}]
    assert parsed["observation_id"] == mobs.observation_id(parsed["batch_id"], 224654)


def test_a_row_without_a_batch_or_an_npc_is_dropped():
    assert mobs.parse_observation({**row(), "batchId": ""}) is None
    assert mobs.parse_observation({**row(), "npcId": 0}) is None
    assert mobs.parse_observation({**row(), "kills": 0, "loots": 0,
                                   "empty": 0, "moneyCopper": 0,
                                   "items": []}) is None


def test_an_export_without_the_block_says_so(tmp_path):
    sv = tmp_path / "sv.lua"
    sv.write_text('DFA_AUCTION_INTELLIGENCE_DB = {\n}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        mobs.parse_export_file(sv)
    empty = tmp_path / "empty.lua"
    empty.write_text('DFA_AUCTION_INTELLIGENCE_DB = {\n["kills_jsonl"] = ""\n}\n',
                     encoding="utf-8")
    assert mobs.parse_export_file(empty) == []


# --- the counting contract -------------------------------------------------

def test_importing_the_same_batch_twice_stores_it_once():
    con = store.memory_connection()
    assert _import(con, row()) == 1
    assert _import(con, row()) == 0
    assert con.execute("SELECT COUNT(*) FROM mob_observation").fetchone()[0] == 1
    assert con.execute("SELECT quantity FROM mob_loot_observation").fetchone()[0] == 4


def test_two_batches_are_two_observations():
    con = store.memory_connection()
    assert _import(con, row(batch="b1"), row(batch="b2", kills=1)) == 2
    observations = mobs.load_observations(con, "Dragonmaw", days=40000)
    assert len(observations) == 2
    assert sum(o["kills"] for o in observations) == 4


def test_kills_per_hour_comes_from_the_windows_that_were_observed():
    con = store.memory_connection()
    now = int(time.time())
    # 30 kills in 10 minutes, then 30 in 10 more: 60 kills over 20 minutes
    _import(con,
            row(batch="b1", kills=30, first=now - 1500, last=now - 900),
            row(batch="b2", kills=30, first=now - 600, last=now))
    rates = mobs.kill_rates(con, "Dragonmaw", days=1, now=now)
    assert rates[224654]["kills"] == 60
    assert rates[224654]["kills_per_hour"] == pytest.approx(180.0, rel=0.01)


def test_a_window_too_short_to_measure_is_not_a_rate():
    con = store.memory_connection()
    now = int(time.time())
    _import(con, row(batch="b1", kills=1, first=now - 5, last=now))
    rates = mobs.kill_rates(con, "Dragonmaw", days=1, now=now)
    assert rates[224654]["kills_per_hour"] is None
    assert "unknown, not zero" in rates[224654]["kill_rate_basis"]


def test_a_drop_rate_needs_a_denominator():
    con = store.memory_connection()
    now = int(time.time())
    _import(con, row(batch="b1", kills=5, items=[{"itemId": 2589, "quantity": 2}]))
    assert mobs.drop_rates(con, "Dragonmaw", days=40000, now=now) == {}
    _import(con, row(batch="b2", kills=45, items=[{"itemId": 2589, "quantity": 18}]))
    drops = mobs.drop_rates(con, "Dragonmaw", days=40000, now=now)
    assert drops[(224654, 2589)]["rate"] == pytest.approx(20 / 50)
    assert "20 units over 50 observed kills" in drops[(224654, 2589)]["basis"]


# --- valuation -------------------------------------------------------------

def test_gold_per_mob_keeps_vendor_market_and_realized_apart(tmp_path):
    con = store.memory_connection()
    tsm = _tsm_db(tmp_path)
    now = int(time.time())
    _import(con, row(kills=2, copper=4500,
                     items=[{"itemId": 2589, "quantity": 4}]))
    value = mobs.value_mobs(con, tsm, "Dragonmaw", now=now)[0]

    # vendor: 4 x 10 copper; market: 4 x 1000; realised: 4 x (2000 * 0.70)
    assert value.vendor_copper == 40
    assert value.market_copper == 4_000
    assert value.realized_copper == 5_600
    # per mob, each with the copper that dropped, in gold
    assert value.gold_per_mob_vendor == pytest.approx((40 + 4500) / 2 / 10_000)
    assert value.gold_per_mob_market == pytest.approx((4_000 + 4500) / 2 / 10_000)
    # realised is net of the house cut: 4 x 1400 x 0.95 + 4500, over 2 kills
    assert value.gold_per_mob_realized == pytest.approx(
        (5_600 * 0.95 + 4500) / 2 / 10_000)
    assert value.priced_units == 4 and value.total_units == 4


def test_an_item_without_a_price_is_reported_not_counted_as_zero(tmp_path):
    con = store.memory_connection()
    tsm = _tsm_db(tmp_path)
    now = int(time.time())
    _import(con, row(kills=2, copper=0,
                     items=[{"itemId": 2589, "quantity": 1},
                            {"itemId": 99999, "quantity": 3}]))
    value = mobs.value_mobs(con, tsm, "Dragonmaw", now=now)[0]
    assert value.total_units == 4 and value.priced_units == 1
    assert value.market_copper == 1_000  # only the priced unit
    assert value.partial is True
    assert any("no market price" in note for note in value.notes)
    unpriced = [item for item in value.detail if item["item_id"] == 99999][0]
    assert unpriced["market_unit_copper"] is None


def test_a_stale_snapshot_is_flagged_with_its_age(tmp_path):
    con = store.memory_connection()
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 86400))
    tsm = _tsm_db(tmp_path, snapshot_updated_at=old)
    _import(con, row())
    value = mobs.value_mobs(con, tsm, "Dragonmaw")[0]
    assert value.stale is True
    assert value.price_age_h > 100
    assert any("revalue" in note for note in value.notes)


def test_a_valuation_is_persisted_once_with_its_source(tmp_path):
    con = store.memory_connection()
    tsm = _tsm_db(tmp_path)
    _import(con, row())
    values = mobs.value_mobs(con, tsm, "Dragonmaw", now=1_700_000_000)
    assert mobs.persist_valuations(con, values) == 1
    assert mobs.persist_valuations(con, values) == 0
    stored = con.execute("SELECT valued_at, price_source, gold_per_mob_realized"
                         " FROM mob_valuation").fetchone()
    assert stored["valued_at"] == 1_700_000_000
    assert "blizzard_items.sellPrice" in stored["price_source"]
    assert stored["gold_per_mob_realized"] > 0
    # a later valuation of the same observations is a new row, not an update
    later = mobs.value_mobs(con, tsm, "Dragonmaw", now=1_700_003_600)
    assert mobs.persist_valuations(con, later) == 1
    assert con.execute("SELECT COUNT(*) FROM mob_valuation").fetchone()[0] == 2


# --- the farm pipeline -----------------------------------------------------

class _Drop:
    def __init__(self, npc_id, item_id, rate=1.0, name="Thing"):
        self.npc_id = npc_id
        self.item_id = item_id
        self.drop_rate = rate
        self.qty = 1
        self.item_name = name
        self.npc_name = f"NPC {npc_id}"
        self.zone_name = "Zone"


def test_measured_rates_win_and_wowhead_remains_the_fallback():
    book = {2589: farming.PricePoint(item_id=2589, name="Linen Cloth",
                                     region_sale_avg=2_000)}
    drops = [_Drop(224654, 2589, rate=0.10), _Drop(7, 2589, rate=0.50)]
    observed = {
        "kill_rates": {224654: 120.0},
        "drop_rates": {(224654, 2589): 0.25},
        "kill_rate_basis": {224654: "60 kills over 0.50 h in 2 window(s)"},
        "drop_rate_basis": {(224654, 2589): "30 units over 120 observed kills"},
    }
    result = farming.recommend_farm(drops, book, observed=observed)
    measured = next(r for r in result["recommendations"] if r.npc_id == 224654)
    estimated = next(r for r in result["recommendations"] if r.npc_id == 7)
    assert measured.kill_rate_per_hour == 120.0
    assert "measured in game" in measured.kill_rate_basis
    assert measured.items[0].drop_rate == 0.25
    assert "measured in game" in measured.items[0].drop_rate_basis
    # no measurement for NPC 7: the Wowhead rate and the default stay
    assert estimated.kill_rate_per_hour == 20.0
    assert estimated.items[0].drop_rate == 0.50
    assert estimated.items[0].drop_rate_basis == "Wowhead estimate"
    assert result["observed_data"]["kill_rates"] == 1


def test_the_cli_imports_kills_and_reports_gold_per_mob(tmp_path, capsys):
    from scripts.intel import main

    tsm = _tsm_db(tmp_path)
    payload = json.dumps(json.dumps(row()))
    sv = tmp_path / "sv.lua"
    sv.write_text("DFA_AUCTION_INTELLIGENCE_DB = {\n[\"kills_jsonl\"] = "
                  + payload + "\n}\n", encoding="utf-8")
    argv = ["--db", str(tmp_path / "intel.sqlite"), "--tsm-db", str(tsm)]
    assert main(argv + ["addon-import", "--path", str(sv)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["kills"] == {"parsed": 1, "inserted": 1}
    # the same file again is a no-op for the kill counters
    assert main(argv + ["addon-import", "--path", str(sv)]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["kills"] == {"parsed": 1, "inserted": 0}

    assert main(argv + ["mobs", "--realm", "Dragonmaw", "--days", "40000",
                        "--persist"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["observations"] == 1
    assert report["kills_total"] == 3
    assert report["valuations_persisted"] == 1
    mob = report["mobs"][0]
    assert mob["npc_id"] == 224654
    assert mob["gold_per_mob_vendor"] > 0
    assert mob["gold_per_mob_realized"] > mob["gold_per_mob_vendor"]
