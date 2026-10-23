"""Blizzard Auction House ingestion.

The fixture mirrors the public payload shape: ``auctions`` with
``item.id``, ``quantity``, ``buyout``/``bid`` and ``time_left``.  The
tests pin the two rules that matter: the original time-left string is
never rewritten, and a missing price is missing rather than zero.
"""
import json

from woweconomy.intel import ah, store

PAYLOAD = {
    "auctions": [
        {"id": 11, "item": {"id": 19019, "context": 0}, "quantity": 1,
         "buyout": 500_000, "bid": 400_000, "time_left": "SHORT"},
        {"id": 12, "item": {"id": 19020, "context": 0}, "quantity": 20,
         "buyout": 2_000_000, "bid": 0, "time_left": "VERY_LONG"},
        {"id": 13, "item": {"id": 21877}, "quantity": 200,
         "unit_price": 1500, "time_left": "LONG"},
        # an unknown category must survive untouched
        {"id": 14, "item": {"id": 21878}, "quantity": 5,
         "unit_price": 700, "time_left": "EXTENDED"},
        # an auction with no price at all: None, never zero
        {"id": 15, "item": {"id": 19021}, "quantity": 1, "time_left": "SHORT"},
        # no item id: rejected and counted
        {"id": 16, "quantity": 1, "time_left": "SHORT"},
    ]
}


def bundle(**kwargs):
    params = dict(region="eu", realm="Kazzak", realm_slug="kazzak",
                  connected_realm_id=1099, captured_at=1_792_500_000)
    params.update(kwargs)
    return ah.parse_payload(PAYLOAD, **params)


def test_time_left_is_preserved_and_unknown_values_are_not_guessed():
    parsed = bundle()
    by_id = {a.auction_id: a for a in parsed.auctions}
    assert by_id[11].time_left_raw == "SHORT"
    assert by_id[11].time_left_band == "SHORT"
    assert by_id[12].time_left_band == "VERY_LONG"
    assert by_id[14].time_left_raw == "EXTENDED"
    assert by_id[14].time_left_band is None
    assert len(parsed.rejected) == 1
    assert "no item id" in parsed.rejected[0]


def test_unit_prices_are_derived_only_when_the_api_sends_a_buyout():
    by_id = {a.auction_id: a for a in bundle().auctions}
    eleven = by_id[11]
    assert eleven.buyout_copper == 500_000
    assert eleven.unit_price_copper == 500_000
    assert eleven.unit_price_source == "derived_from_buyout"
    twenty = by_id[12]
    assert twenty.unit_price_copper == 100_000
    assert twenty.total_price_copper == 2_000_000
    assert by_id[13].unit_price_source == "api_unit_price"
    assert by_id[13].is_commodity is True
    missing = by_id[15]
    assert missing.unit_price_copper is None
    assert missing.total_price_copper is None
    assert missing.unit_price_source == ""


def test_ingestion_is_idempotent_and_a_new_capture_is_a_new_snapshot():
    con = store.memory_connection()
    first = ah.ingest_bundle(con, bundle())
    assert first.inserted is True
    assert first.auctions == 3          # 19019, 19020, 19021
    assert first.commodities == 2       # unit_price rows (21877, 21878)
    assert first.rejected == 1

    again = ah.ingest_bundle(con, bundle())
    assert again.inserted is False and again.already_present is True
    assert con.execute("SELECT COUNT(*) FROM ah_snapshot").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM ah_auction").fetchone()[0] == 5

    later = ah.ingest_bundle(con, bundle(captured_at=1_792_503_600))
    assert later.inserted is True
    assert con.execute("SELECT COUNT(*) FROM ah_snapshot").fetchone()[0] == 2


def test_the_stored_row_keeps_the_raw_time_left_and_the_provenance():
    con = store.memory_connection()
    parsed = bundle()
    ah.ingest_bundle(con, parsed)
    rows = ah.snapshot_auctions(con, parsed.snapshot_id)
    stored = {r["auction_id"]: r for r in rows}
    assert stored[14]["time_left_raw"] == "EXTENDED"
    assert stored[14]["time_left_band"] is None
    assert stored[12]["unit_price_source"] == "derived_from_buyout"
    snap = ah.latest_snapshot(con, "eu", "kazzak")
    assert snap["payload_sha256"] == parsed.payload_sha256
    assert snap["connected_realm_id"] == 1099


def test_the_summary_counts_unknown_categories_and_unpriced_listings():
    con = store.memory_connection()
    parsed = bundle()
    ah.ingest_bundle(con, parsed)
    summary = ah.summary(con, parsed.snapshot_id)
    assert summary["listings"] == 5
    assert summary["items"] == 5
    assert summary["unpriced_listings"] == 1
    assert summary["unknown_time_left"] == 1
    assert summary["time_left_counts"]["EXTENDED"] == 1


def test_a_commodity_endpoint_marks_every_row_commodity():
    payload = {"auctions": [
        {"id": 1, "item": {"id": 10}, "quantity": 50, "unit_price": 99,
         "time_left": "SHORT"}]}
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak",
                              captured_at=1, commodity_endpoint=True)
    assert parsed.auctions[0].is_commodity is True
    assert parsed.commodity_count == 1


def test_the_api_total_is_preserved_even_when_the_unit_price_rounds():
    payload = {"auctions": [
        {"id": 1, "item": {"id": 10}, "quantity": 3, "buyout": 100,
         "time_left": "SHORT"}]}
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak", captured_at=1)
    record = parsed.auctions[0]
    assert record.unit_price_copper == 33
    assert record.total_price_copper == 100


def test_the_realm_index_is_read_through_the_dynamic_namespace():
    class FakeClient:
        region = "eu"

        def __init__(self):
            self.calls = []

        def get_json(self, path, modified_since=""):
            self.calls.append(path)
            if path.endswith("/index"):
                return {"connected_realms": [{
                    "href": "https://eu.api.blizzard.com/data/wow/connected-realm/1099"
                            "?namespace=dynamic-eu"}]}, ""
            return {"id": 1099, "realms": [
                {"slug": "kazzak", "name": "Kazzak"},
                {"slug": "silvermoon", "name": "Silvermoon"}]}, ""

    con = store.memory_connection()
    client = FakeClient()
    assert ah.sync_realm_index(con, client) == 2
    assert con.execute("SELECT COUNT(*) FROM ah_realm").fetchone()[0] == 2
    assert ah.connected_realm_id(con, "Silvermoon") == 1099
    assert client.calls[0].endswith("/index")
    assert "/data/wow/connected-realm/1099" in client.calls[1]


def test_realm_index_lookup_is_case_insensitive():
    con = store.memory_connection()
    con.execute("INSERT INTO ah_realm (region, realm_slug, realm_name,"
                " connected_realm_id) VALUES ('eu','kazzak','Kazzak',1099)")
    assert ah.connected_realm_id(con, "kazzak") == 1099
    assert ah.connected_realm_id(con, "Kazzak") == 1099
    assert ah.connected_realm_id(con, "Silvermoon") is None


def test_ingest_file_round_trips(tmp_path):
    path = tmp_path / "auctions.json"
    path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
    con = store.memory_connection()
    result = ah.ingest_file(con, path, region="eu", realm="Kazzak",
                            captured_at=1_792_500_000)
    assert result.inserted is True
    assert result.auctions + result.commodities == 5
