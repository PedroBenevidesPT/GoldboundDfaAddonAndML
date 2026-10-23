"""Inventory reconciliation.

The tests encode the rule the brief insists on: an item that left the
bags without a confirmed event is *unexplained*, not a sale.
"""
import json

from woweconomy.intel import inventory, store


def payload(captured, locations, character="Bullbound", realm="Dragonmaw",
            reason="login"):
    return {
        "capturedAt": captured, "character": character, "realm": realm,
        "reason": reason, "bagSlots": 120,
        "locations": {name: [{"itemId": item, "itemName": f"Item {item}",
                              "quantity": qty}
                             for item, qty in rows]
                      for name, rows in locations.items()},
    }


def snap(captured, locations, **kwargs):
    return inventory.parse_inventory(payload(captured, locations, **kwargs))


def test_duplicate_slots_are_summed_and_empty_rows_are_dropped():
    parsed = inventory.parse_inventory({
        "capturedAt": 100, "character": "B", "realm": "R",
        "locations": {"bags": [
            {"itemId": 7, "quantity": 5},
            {"itemId": 7, "quantity": 3},
            {"itemId": 8, "quantity": 0},
            {"itemId": 0, "quantity": 4},
        ]},
    })
    assert parsed.rows[("bags", 7)] == ("", 8)
    assert ("bags", 8) not in parsed.rows
    assert parsed.total_units == 8


def test_a_listing_moves_stock_from_bags_to_posted():
    prev = snap(100, {"bags": [(1, 10)]})
    cur = snap(200, {"bags": [(1, 8)], "posted": [(1, 2)]})
    report = inventory.reconcile(prev, cur, [
        {"event_type": "listing", "ts": 150, "item_id": 1, "quantity": 2},
    ])
    assert report.summary()["matched_rows"] == 2
    assert report.unexplained_decrease_units == 0
    assert report.confirmed_listing_units == 2


def test_a_confirmed_sale_explains_only_the_posted_decrease():
    prev = snap(100, {"bags": [(1, 5)], "posted": [(1, 5)]})
    cur = snap(200, {"bags": [(1, 5)], "posted": [(1, 3)]})
    report = inventory.reconcile(prev, cur, [
        {"event_type": "sale", "ts": 180, "item_id": 1, "quantity": 2},
    ])
    assert report.summary()["matched_rows"] == 1
    assert report.confirmed_sales_units == 2
    assert report.sales_not_reflected == 0


def test_a_disappearance_without_an_event_is_never_a_sale():
    prev = snap(100, {"bags": [(1, 10)]})
    cur = snap(200, {"bags": [(1, 7)]})
    report = inventory.reconcile(prev, cur, [])
    row = report.rows[0]
    assert row.status == "unexplained_decrease"
    assert row.residual == -3
    assert report.unexplained_decrease_units == 3
    assert report.confirmed_sales_units == 0
    assert any("never as a sale" in note for note in report.notes)


def test_a_cancel_returns_the_item_to_the_bags():
    prev = snap(100, {"posted": [(1, 2)]})
    cur = snap(200, {"bags": [(1, 2)], "posted": []})
    report = inventory.reconcile(prev, cur, [
        {"event_type": "cancel", "ts": 150, "item_id": 1, "quantity": 2},
    ])
    assert report.summary()["matched_rows"] == 2
    assert report.confirmed_failure_units == 2


def test_a_sale_that_did_not_move_posted_stock_is_flagged_not_ignored():
    prev = snap(100, {"posted": [(1, 5)]})
    cur = snap(200, {"posted": [(1, 5)]})
    report = inventory.reconcile(prev, cur, [
        {"event_type": "sale", "ts": 180, "item_id": 1, "quantity": 1},
    ])
    row = report.rows[0]
    assert row.status == "event_not_reflected"
    assert report.sales_not_reflected == 1


def test_events_from_another_character_are_not_used():
    prev = snap(100, {"bags": [(1, 4)]})
    cur = snap(200, {"bags": [(1, 2)]})
    report = inventory.reconcile(prev, cur, [
        {"event_type": "listing", "ts": 150, "item_id": 1, "quantity": 2,
         "character": "SomeoneElse"},
    ])
    assert report.rows[0].status == "unexplained_decrease"


def test_an_unreadable_bank_is_unknown_not_empty():
    prev = snap(100, {"bags": [(1, 5)], "bank": [(2, 4)]})
    cur_payload = payload(200, {"bags": [(1, 5)]})
    cur_payload["available"] = {"bags": True, "bank": False, "posted": False}
    cur = inventory.parse_inventory(cur_payload)
    assert "bank" not in cur.available
    report = inventory.reconcile(prev, cur, [])
    assert all(row.location != "bank" for row in report.rows)
    assert "locations compared: bags" in report.notes


def test_an_explicitly_unreadable_snapshot_stays_unreadable():
    parsed = inventory.parse_inventory({
        "capturedAt": 100, "character": "B", "realm": "R",
        "available": {"bags": False, "bank": False, "posted": False},
        "locations": {},
    })
    con = store.memory_connection()
    inventory.import_snapshot(con, parsed)
    loaded = inventory.load_snapshot(con, parsed.snapshot_id)
    assert loaded.available == set()


def test_snapshot_storage_is_idempotent_and_round_trips():
    con = store.memory_connection()
    first = snap(100, {"bags": [(1, 10)], "posted": [(2, 1)]})
    assert inventory.import_snapshot(con, first) is True
    assert inventory.import_snapshot(con, first) is False
    loaded = inventory.load_snapshot(con, first.snapshot_id)
    assert loaded.rows == first.rows
    assert loaded.units("bags") == 10


def test_current_units_sums_only_the_latest_readable_snapshot():
    con = store.memory_connection()
    old = snap(100, {"bags": [(1, 5)]})
    new_payload = payload(200, {"bags": [(1, 2)], "bank": [(1, 3)]})
    new_payload["available"] = {"bags": True, "bank": False, "posted": False}
    new = inventory.parse_inventory(new_payload)
    inventory.import_snapshot(con, old)
    inventory.import_snapshot(con, new)
    # the bank was unreadable, so its 3 units are not part of the stock
    assert inventory.current_units(con, "Dragonmaw") == {1: 2}
    assert inventory.current_units(con, "Silvermoon") == {}


def test_the_guild_bank_is_recorded_but_never_reconciled():
    prev = snap(100, {"bags": [(1, 5)], "guildbank": [(9, 40)]})
    cur = snap(200, {"bags": [(1, 5)], "guildbank": [(9, 10)]})
    report = inventory.reconcile(prev, cur, [])
    # another guild member withdrew 30: that is not this character's stock
    assert report.unexplained_decrease_units == 0
    assert report.rows == []
    assert any("guild bank" in note for note in report.notes)
    assert cur.rows[("guildbank", 9)] == ("Item 9", 10)


def test_the_guild_bank_is_not_counted_as_personal_stock():
    con = store.memory_connection()
    inventory.import_snapshot(
        con, snap(100, {"bags": [(1, 5)], "guildbank": [(9, 40)]}))
    assert inventory.current_units(con, "Dragonmaw") == {1: 5}


def test_an_unreadable_guild_bank_is_unknown_not_empty():
    raw = payload(100, {"bags": [(1, 5)]})
    raw["available"] = {"bags": True, "bank": False, "posted": False,
                        "guildbank": False}
    parsed = inventory.parse_inventory(raw)
    assert "guildbank" not in parsed.available
    assert not any(loc == "guildbank" for loc, _ in parsed.rows)


def test_the_warband_bank_is_shared_and_never_reconciled():
    prev = snap(100, {"bags": [(1, 5)], "warband": [(9, 40)]})
    cur = snap(200, {"bags": [(1, 5)], "warband": [(9, 10)]})
    report = inventory.reconcile(prev, cur, [])
    # an alt withdrew 30 from the account bank: not this character's movement
    assert report.unexplained_decrease_units == 0
    assert report.rows == []
    assert cur.rows[("warband", 9)] == ("Item 9", 10)


def test_the_warband_is_not_counted_as_personal_stock_but_is_kept():
    con = store.memory_connection()
    inventory.import_snapshot(
        con, snap(100, {"bags": [(1, 5)], "warband": [(9, 40)]}))
    assert inventory.current_units(con, "Dragonmaw") == {1: 5}
    loaded = inventory.load_snapshot(
        con, inventory.parse_inventory(payload(100, {"bags": [(1, 5)],
                                                 "warband": [(9, 40)]}))
        .snapshot_id)
    assert loaded.rows[("warband", 9)] == ("Item 9", 40)


def test_posted_listings_keep_the_price_the_blizzard_scan_needs():
    import time
    con = store.memory_connection()
    now = int(time.time()) - 60
    raw = payload(now, {"bags": [(1, 5)], "posted": [(1, 5)]})
    raw["postedListings"] = [{
        "auctionId": 77, "itemId": 1, "itemName": "Item 1",
        "quantity": 5, "unitPriceCopper": 10_000,
        "buyoutCopper": 50_000, "timeLeft": 7_200,
    }]
    parsed = inventory.parse_inventory(raw)
    assert parsed.posted_listings[0]["unit_price_copper"] == 10_000
    inventory.import_snapshot(con, parsed)

    posted = inventory.latest_posted(con, "Dragonmaw")
    assert len(posted) == 1
    assert posted[0]["auction_id"] == 77
    assert posted[0]["time_left"] == 7_200


def test_an_unreadable_owned_list_has_no_posted_rows_to_match():
    con = store.memory_connection()
    raw = payload(100, {"bags": [(1, 5)]})
    raw["available"] = {"bags": True, "posted": False}
    inventory.import_snapshot(con, inventory.parse_inventory(raw))

    assert inventory.latest_posted(con, "Dragonmaw") == []


def test_the_saved_variables_block_is_read(tmp_path):
    inv = payload(100, {"bags": [(1, 10)]})
    text = json.dumps(inv)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    lua = ('DFA_AUCTION_INTELLIGENCE_EXPORT = {\n'
           f'["inventory_json"] = "{escaped}",\n'
           '["total_events"] = 0,\n}\n')
    path = tmp_path / "DFA_AuctionIntelligence.lua"
    path.write_text(lua, encoding="utf-8")
    parsed = inventory.parse_export_file(path)
    assert parsed.rows[("bags", 1)] == ("Item 1", 10)
