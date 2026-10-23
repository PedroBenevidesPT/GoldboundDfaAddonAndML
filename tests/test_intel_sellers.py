"""Seller intelligence: facts stay facts, inferences stay inferences."""
import sqlite3

from woweconomy.intel import ah, sellers, store


def obs(observed_at, seller, item_id=100, realm="Dragonmaw", price=None,
        source="tooltip", confidence="verified"):
    name, realm_part = sellers.split_seller(seller)
    return sellers.SellerObservation(
        obs_id=sellers.make_obs_id(observed_at, realm, seller, item_id, None, source),
        observed_at=observed_at, realm=realm, seller=seller, seller_name=name,
        seller_realm=realm_part, item_id=item_id, item_name=f"Item {item_id}",
        unit_price_copper=price, source=source, confidence=confidence,
        raw_line=f"Seller: {seller}")


def sale(ts, buyer, item_id=100, realm="Dragonmaw", price=50_000):
    return sellers.SaleFact(event_id=f"sale-{ts}", ts=ts, realm=realm,
                            item_id=item_id, item_name=f"Item {item_id}",
                            quantity=1, unit_price_copper=price, buyer=buyer,
                            source="test")


def test_the_seller_string_is_split_at_the_first_hyphen():
    assert sellers.split_seller("Tavil-Sylvanas") == ("Tavil", "Sylvanas")
    assert sellers.split_seller("Aoshi") == ("Aoshi", "")
    assert sellers.split_seller("Name-Area-52") == ("Name", "Area-52")


def test_observations_round_trip_and_import_is_idempotent():
    con = store.memory_connection()
    rows = [sellers.parse_observation({
        "observedAt": 100, "realm": "Dragonmaw", "seller": "Tavil-Sylvanas",
        "itemId": 100, "source": "tooltip", "confidence": "verified",
        "rawLine": "Seller: Tavil-Sylvanas"})]
    assert rows[0].seller_name == "Tavil"
    assert sellers.import_observations(con, rows) == 1
    assert sellers.import_observations(con, rows) == 0
    loaded = sellers.load_observations(con, realm="dragonmaw")
    assert len(loaded) == 1
    assert loaded[0].raw_line == "Seller: Tavil-Sylvanas"


def test_a_buyer_who_later_lists_the_same_item_is_an_inference_not_a_fact():
    result = sellers.correlate_sales([sale(1000, "Tavil-Sylvanas")],
                                     [obs(1000 + 3600, "Tavil-Sylvanas")])
    assert len(result["confirmed"]) == 2
    assert result["confirmed"][0]["kind"] == "sale"
    assert result["confirmed"][1]["kind"] == "seller_observation"
    assert len(result["inferred"]) == 1
    inference = result["inferred"][0]
    assert inference["kind"] == "buyer_relisted"
    assert inference["confidence"] == "high"
    assert inference["lag_hours"] == 1.0
    assert "not proof" in inference["limits"]


def test_an_observation_before_the_sale_is_not_evidence_of_a_resale():
    result = sellers.correlate_sales([sale(1000, "Tavil")],
                                     [obs(900, "Tavil")])
    assert result["inferred"] == []
    assert result["sales_without_observation"] == 1


def test_a_different_item_or_a_different_realm_does_not_match():
    result = sellers.correlate_sales(
        [sale(1000, "Tavil")],
        [obs(2000, "Tavil", item_id=999),
         obs(2000, "Tavil", item_id=100, realm="Silvermoon")])
    assert result["inferred"] == []


def test_the_confidence_tier_follows_the_lag():
    medium = sellers.correlate_sales([sale(1000, "Tavil")],
                                     [obs(1000 + 72 * 3600, "Tavil")])
    low = sellers.correlate_sales([sale(1000, "Tavil")],
                                  [obs(1000 + 240 * 3600, "Tavil")])
    assert medium["inferred"][0]["confidence"] == "medium"
    assert low["inferred"][0]["confidence"] == "low"


def test_profiles_count_observations_not_sales():
    profiles = sellers.competitor_profiles(
        [obs(1000, "Tavil-Sylvanas", price=900), obs(2000, "Tavil-Sylvanas", price=800)],
        [{"kind": "buyer_relisted", "buyer": "Tavil-Sylvanas"}])
    assert profiles[0]["observations"] == 2
    assert profiles[0]["also_bought_from_us"] == 1
    assert profiles[0]["basis"] == "confirmed observations"


def test_co_listing_and_undercut_are_labelled_inferences():
    rows = [obs(1000, "Tavil", price=1000), obs(1600, "Aoshi", price=800)]
    result = sellers.co_listing_episodes(rows)
    assert result["kind"] == "inference"
    assert len(result["co_listing"]) == 1
    assert len(result["undercut"]) == 1
    assert result["undercut"][0]["undercutter"] == "Aoshi"
    assert "intent to undercut is not observable" in result["undercut"][0]["limits"]


def test_our_own_auction_is_an_observation_but_never_a_competitor():
    mine = sellers.SellerObservation(
        obs_id="mine", observed_at=1000, realm="Dragonmaw", seller="You",
        seller_name="You", seller_realm="", item_id=100, item_name="Item 100",
        source="tooltip", confidence="verified", character="Bullbound",
        raw_line="Seller: You")
    rival = obs(1600, "Rival-Silvermoon")
    assert mine.is_self is True and rival.is_self is False
    assert "You" not in [p["seller"] for p in
                          sellers.competitor_profiles([mine, rival])]
    episodes = sellers.co_listing_episodes([mine, rival], gap_h=48)
    assert episodes["co_listing"] == []
    assert sellers.correlate_sales([sale(900, "You")], [mine])["inferred"] == []


def test_commodities_have_no_seller_breakdown():
    con = store.memory_connection()
    payload = {"auctions": [
        {"id": 1, "item": {"id": 10}, "quantity": 50, "unit_price": 100,
         "time_left": "SHORT"},
        {"id": 2, "item": {"id": 10}, "quantity": 20, "unit_price": 90,
         "time_left": "LONG"},
    ]}
    parsed = ah.parse_payload(payload, region="eu", realm="Dragonmaw",
                              captured_at=1000, commodity_endpoint=True)
    ah.ingest_bundle(con, parsed)
    summary = sellers.commodity_market_summary(con, "eu", "Dragonmaw")
    assert summary["available"] is True
    assert summary["commodity_listings"] == 2
    assert summary["units_listed"] == 70
    assert summary["min_unit_price"] == 90
    assert summary["seller_identity_available"] is False
    assert "per-seller" not in summary["basis"]


def test_confirmed_sales_are_read_from_the_existing_journal(tmp_path):
    db = tmp_path / "tsm.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE auction_intel_events (event_id TEXT PRIMARY KEY,"
        " event_type TEXT, ts INTEGER, item_id INTEGER, item_name TEXT,"
        " quantity INTEGER, unit_price_copper INTEGER, realm TEXT,"
        " character TEXT, counterparty TEXT, source TEXT, confidence TEXT)")
    con.execute("INSERT INTO auction_intel_events VALUES"
                " ('e1','sale',1000,100,'Item 100',1,50000,'Dragonmaw',"
                " 'Bullbound','Tavil-Sylvanas','journalator','verified')")
    con.execute("INSERT INTO auction_intel_events VALUES"
                " ('e2','sale',1100,100,'Item 100',1,50000,'Dragonmaw',"
                " 'Bullbound','','journalator','verified')")
    con.commit()
    con.close()
    rows = sellers.load_confirmed_sales(db)
    assert len(rows) == 1
    assert rows[0].buyer == "Tavil-Sylvanas"
    assert rows[0].buyer_key == "tavil"
