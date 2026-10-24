"""Wowhead drops have one store, and a rescan replaces the zone."""
import json

from woweconomy.services import wowhead_store


def row(npc_id=830, item_id=2674, rate=0.41, name="Crawler Meat", **extra):
    data = {
        "npc_id": npc_id, "item_id": item_id, "npc_name": "Sand Crawler",
        "zone": 16, "zone_name": "azshara", "category": "drops",
        "item_name": name, "item_type": "Meat", "drop_rate": rate,
        "qty": 1.0, "source_url": f"https://www.wowhead.com/item={item_id}",
    }
    data.update(extra)
    return data


def test_a_zone_round_trips_with_its_drops(tmp_path):
    db = tmp_path / "market.db"

    saved = wowhead_store.save_zone(
        [row(), row(item_id=2675, name="Crawler Claw", rate=0.39)],
        zone_id=16, zone_name="azshara", npc_total=1, spawns={830: 19},
        db_path=db)

    stored = wowhead_store.load_drops(16, db_path=db)
    assert saved["rows"] == 2
    assert len(stored) == 2
    by_item = {r["item_id"]: r for r in stored}
    assert by_item[2674]["drop_rate"] == 0.41
    assert by_item[2674]["spawns"] == 19
    assert by_item[2674]["zone_name"] == "azshara"


def test_a_rescan_replaces_the_zone_instead_of_accumulating(tmp_path):
    db = tmp_path / "market.db"
    wowhead_store.save_zone([row(), row(item_id=2675, name="Claw")],
                            zone_id=16, zone_name="azshara", db_path=db)

    # The second scan no longer finds item 2675: it must disappear, or the
    # ranking keeps pricing a drop nobody has seen since.
    wowhead_store.save_zone([row()], zone_id=16, zone_name="azshara",
                            db_path=db)

    stored = wowhead_store.load_drops(16, db_path=db)
    assert [r["item_id"] for r in stored] == [2674]


def test_the_same_item_seen_twice_keeps_the_better_rate(tmp_path):
    db = tmp_path / "market.db"
    wowhead_store.save_zone(
        [row(rate=0.10), row(rate=0.41), row(rate=0.20)],
        zone_id=16, zone_name="azshara", db_path=db)

    stored = wowhead_store.load_drops(16, db_path=db)
    assert len(stored) == 1
    assert stored[0]["drop_rate"] == 0.41


def test_a_zone_without_items_still_records_the_scan(tmp_path):
    db = tmp_path / "market.db"
    wowhead_store.save_zone([], zone_id=719, zone_name="blackfathom-deeps",
                            npc_total=12, db_path=db)

    assert wowhead_store.load_drops(719, db_path=db) == []
    import sqlite3
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT npc_total FROM wowhead_zone WHERE zone_id=719"
                           ).fetchone()[0] == 12
    finally:
        con.close()


def test_import_json_uses_the_same_writer(tmp_path):
    db = tmp_path / "market.db"
    source = tmp_path / "zone_16.json"
    source.write_text(json.dumps([{
        "npc_id": 830, "zone": 16, "name": "azshara",
        "npc_name": "Sand Crawler", "category": "drops", "spawns": 19,
        "items": [{"item_id": 2674, "item_name": "Crawler Meat",
                   "item_type": "Meat", "drop_rate": 0.41, "qty": 1.0,
                   "source_url": "https://www.wowhead.com/item=2674"}],
    }]), encoding="utf-8")

    result = wowhead_store.import_json([source], db_path=db)

    stored = wowhead_store.load_drops(16, db_path=db)
    assert result == {"zones": 1, "rows": 1}
    assert stored[0]["item_id"] == 2674
    assert stored[0]["spawns"] == 19
