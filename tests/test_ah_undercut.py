"""The out-of-game undercut scan: the API has prices, not seller names."""
import time

from woweconomy.intel import ah_undercut, inventory, relist, store, undercut


def add_snapshot(con, snapshot_id="snap-1", realm="Dragonmaw", captured_at=None):
    captured_at = int(time.time()) - 60 if captured_at is None else captured_at
    con.execute(
        "INSERT OR REPLACE INTO ah_snapshot (snapshot_id, region,"
        " connected_realm_id, realm, realm_slug, captured_at,"
        " api_last_modified, source, payload_sha256, auction_count,"
        " commodity_count, rejected_rows, imported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snapshot_id, "eu", 3656, realm, "dragonmaw", captured_at,
         "", "blizzard_api", snapshot_id, 0, 0, 0, ""))
    con.commit()


def add_auction(con, snapshot_id, auction_id, item_id, quantity, unit,
                band="LONG", item_context=None, bonus_lists=None,
                modifiers=None, is_commodity=0):
    con.execute(
        "INSERT OR REPLACE INTO ah_auction (snapshot_id, auction_key,"
        " auction_id, item_id, item_context, quantity, unit_price_copper,"
        " buyout_copper, bid_copper, total_price_copper, unit_price_source,"
        " time_left_raw, time_left_band, is_commodity, bonus_lists,"
        " modifiers)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snapshot_id, f"{snapshot_id}-{auction_id}", auction_id, item_id,
         item_context, quantity, unit, unit * quantity, unit * quantity,
         unit * quantity, "api", band, band, is_commodity, bonus_lists,
         modifiers))
    con.commit()


def add_posted(con, captured_at=None, listings=None, realm="Dragonmaw"):
    captured_at = int(time.time()) - 60 if captured_at is None else captured_at
    listings = listings or [
        {"auctionId": 10, "itemId": 100, "itemName": "Thing",
         "quantity": 5, "unitPriceCopper": 10_000,
         "buyoutCopper": 50_000, "timeLeft": 7_200},
    ]
    payload = {
        "capturedAt": captured_at, "realm": realm, "character": "Bullbound",
        "reason": "auctions",
        "available": {"bags": True, "posted": True},
        "locations": {"bags": [{"itemId": 100, "itemName": "Thing",
                                "quantity": 5}],
                      "posted": [{"itemId": row["itemId"],
                                  "itemName": row["itemName"],
                                  "quantity": row["quantity"]}
                                 for row in listings]},
        "postedListings": listings,
    }
    return inventory.import_snapshot(con, inventory.parse_inventory(payload))


def rows(con):
    return [{key: row[key] for key in row.keys()}
            for row in con.execute("SELECT * FROM undercut_observation")]


def test_a_cheaper_listing_becomes_one_observation_without_a_seller():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 10, 100, 5, 10_000)   # mine
    add_auction(con, "snap-1", 11, 100, 3, 9_000)
    add_auction(con, "snap-1", 12, 100, 4, 8_000)
    add_posted(con)

    result = ah_undercut.scan_realm(con, "Dragonmaw")

    assert result["posted_listings"] == 1
    assert result["inserted"] == 1
    row = rows(con)[0]
    assert row["lowest_unit_price_copper"] == 8_000
    assert row["items_ahead"] == 7
    assert row["undercut_copper"] == 2_000
    assert row["undercut_pct"] == 0.2
    assert row["source"] == "blizzard_api"
    assert row["confidence"] == "price-only"
    assert row["lowest_seller"] == ""
    assert row["lowest_auction_id"] == 12


def test_my_own_listing_is_not_competition():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 10, 100, 5, 10_000)
    add_posted(con)

    result = ah_undercut.scan_realm(con, "Dragonmaw")

    assert result["inserted"] == 0
    assert result["stats"]["no_competitor"] == 1
    assert rows(con) == []


def test_an_item_the_api_does_not_have_is_counted_not_invented():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 99, 555, 1, 100)
    add_posted(con)

    result = ah_undercut.scan_realm(con, "Dragonmaw")

    assert result["stats"]["no_market_data"] == 1
    assert rows(con) == []


def test_the_unknown_seller_is_labelled_in_the_summary():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 10, 100, 5, 10_000)
    add_auction(con, "snap-1", 11, 100, 3, 9_000)
    add_posted(con)
    ah_undercut.scan_realm(con, "Dragonmaw")

    summary = undercut.summarise(con, "Dragonmaw", days=1)

    assert summary["sellers"][0]["seller"] == "(seller not in API)"
    assert summary["observations"] == 1


def test_a_price_only_reference_with_an_unknown_clock_is_reviewed():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 10, 100, 5, 10_000)
    add_auction(con, "snap-1", 11, 100, 3, 8_000)
    add_posted(con, listings=[{"auctionId": 10, "itemId": 100,
                               "itemName": "Thing", "quantity": 5,
                               "unitPriceCopper": 10_000,
                               "buyoutCopper": 50_000, "timeLeft": 0}])
    ah_undercut.scan_realm(con, "Dragonmaw")

    advice = relist.advise(con, "Dragonmaw", days=1)

    assert advice["counts"][relist.EXPIRE] == 0
    assert advice["counts"][relist.RELAST] == 0
    assert advice["advice"][0]["advice"] == relist.WATCH
    assert advice["advice"][0]["review_required"] is True
    assert advice["advice"][0]["reference_status"] == relist.REFERENCE_PRICE_ONLY
    assert advice["advice"][0]["hours_left"] is None


def test_a_variant_bearing_api_listing_is_flagged_as_ambiguous():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 10, 82800, 1, 5_000_000,
                modifiers='[{"type": 6, "value": 21900}]')
    add_auction(con, "snap-1", 11, 82800, 1, 50_000,
                modifiers='[{"type": 6, "value": 46902}]')
    add_posted(con, listings=[{"auctionId": 10, "itemId": 82800,
                               "itemName": "Pet Cage", "quantity": 1,
                               "unitPriceCopper": 5_000_000,
                               "buyoutCopper": 5_000_000, "timeLeft": 0}])

    result = ah_undercut.scan_realm(con, "Dragonmaw")

    assert result["stats"]["variant_ambiguous"] == 1
    row = rows(con)[0]
    assert row["reference_status"] == "variant-ambiguous"
    advice = relist.advise(con, "Dragonmaw", days=1)
    assert advice["counts"][relist.RELAST] == 0
    assert advice["advice"][0]["reference_status"] == "variant-ambiguous"
    assert advice["advice"][0]["review_required"] is True
    assert "suggested_ask_copper" not in advice["advice"][0]


def test_a_different_item_id_is_not_a_competitor():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 10, 100, 1, 10_000)
    add_auction(con, "snap-1", 11, 101, 1, 5_000)  # different item id
    add_posted(con, listings=[{"auctionId": 10, "itemId": 100,
                               "itemName": "Thing", "quantity": 1,
                               "unitPriceCopper": 10_000,
                               "buyoutCopper": 10_000, "timeLeft": 7_200}])

    result = ah_undercut.scan_realm(con, "Dragonmaw")

    assert result["stats"]["no_competitor"] == 1
    assert rows(con) == []


def test_another_own_listing_is_not_treated_as_competition():
    con = store.memory_connection()
    add_snapshot(con)
    add_auction(con, "snap-1", 10, 100, 1, 10_000)  # evaluated
    add_auction(con, "snap-1", 13, 100, 1, 8_000)   # mine, cheaper
    add_auction(con, "snap-1", 11, 100, 1, 9_000)   # real competitor
    add_posted(con, listings=[
        {"auctionId": 10, "itemId": 100, "itemName": "Thing",
         "quantity": 1, "unitPriceCopper": 10_000,
         "buyoutCopper": 10_000, "timeLeft": 7_200},
        {"auctionId": 13, "itemId": 100, "itemName": "Thing",
         "quantity": 1, "unitPriceCopper": 8_000,
         "buyoutCopper": 8_000, "timeLeft": 7_200},
    ])

    ah_undercut.scan_realm(con, "Dragonmaw")
    row = [r for r in rows(con) if r["own_auction_id"] == 10][0]

    assert row["lowest_unit_price_copper"] == 9_000
    assert row["lowest_auction_id"] == 11
    assert row["items_ahead"] == 1


def test_a_price_reset_needs_both_a_jump_and_a_collapse():
    previous = {1: {"cheapest": 1_000, "supply": 10, "listings": 3},
                2: {"cheapest": 1_000, "supply": 10, "listings": 3},
                3: {"cheapest": 1_000, "supply": 10, "listings": 3}}
    current = {
        1: {"cheapest": 1_500, "supply": 4, "listings": 2},    # reset
        2: {"cheapest": 1_500, "supply": 9, "listings": 3},    # supply held
        3: {"cheapest": 1_100, "supply": 1, "listings": 3},    # small jump
    }

    resets = ah_undercut.detect_price_resets(previous, current)

    assert [row["item_id"] for row in resets] == [1]
    assert resets[0]["jump_pct"] == 0.5
    assert resets[0]["supply_drop_pct"] == 0.6


def test_a_sale_lowers_supply_without_being_a_reset():
    previous = {7: {"cheapest": 5_000, "supply": 20, "listings": 4}}
    current = {7: {"cheapest": 5_000, "supply": 2, "listings": 2}}

    assert ah_undercut.detect_price_resets(previous, current) == []


def test_the_scan_refuses_an_empty_owned_list_instead_of_relisting():
    con = store.memory_connection()
    add_snapshot(con)

    result = ah_undercut.scan_realm(con, "Dragonmaw")

    assert "owned-auction list" in result["error"]
    assert rows(con) == []
