"""Every known realm gets a price factor and a measured competition line."""
import time

from woweconomy.pricing import realm_index
from woweconomy.intel import inventory, store


class Engine:
    def __init__(self, cache):
        self._history_cache = cache


def test_a_thin_realm_is_shrunk_towards_the_region():
    engine = Engine({
        "Thin": {1: (1, 0, [200.0])},
        "Thick": {item: (1, 0, [200.0]) for item in range(1, 21)},
        "None": {},
    })

    factors = realm_index.price_factors(
        engine, {item: 100 for item in range(1, 21)}, ["Thin", "Thick", "None"])

    assert factors["None"] == {"factor": 1.0, "sample": 0, "source": "regional"}
    assert factors["Thin"]["source"] == "thin"
    assert 1.0 < factors["Thin"]["factor"] < factors["Thick"]["factor"]
    assert factors["Thick"]["factor"] > 1.5


def test_an_undercut_share_needs_the_posted_list_as_denominator():
    con = store.memory_connection()
    now = int(time.time()) - 60
    con.execute(
        "INSERT INTO inventory_snapshot (snapshot_id, captured_at, character,"
        " realm, reason, source, bag_slots, available_json, imported_at)"
        " VALUES ('s',?,?,?,?,?,?,?,?)",
        (now, "Bullbound", "Dragonmaw", "auctions", "addon", 4,
         '["bags","posted"]', ""))
    for auction_id in range(1, 5):
        con.execute(
            "INSERT INTO inventory_posted_listing (snapshot_id, auction_id,"
            " item_id, item_name, quantity, unit_price_copper, buyout_copper,"
            " time_left) VALUES ('s',?,100,'Thing',1,10000,10000,7200)",
            (auction_id,))
    for auction_id in (1, 2):
        con.execute(
            "INSERT INTO undercut_observation (observation_id, scan_id,"
            " observed_at, realm, character, item_id, item_name,"
            " own_auction_id, own_quantity, own_unit_price_copper,"
            " lowest_unit_price_copper, undercut_pct, items_ahead,"
            " time_left, source, confidence, imported_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"o{auction_id}", "scan-1", now, "Dragonmaw", "Bullbound", 100,
             "Thing", auction_id, 1, 10_000, 9_000, 0.1, 2, 7_200,
             "blizzard_api", "price-only", ""))
    con.commit()

    share = realm_index.undercut_share(con, "Dragonmaw")

    assert share["watched"] == 4
    assert share["undercut"] == 2
    assert share["share"] == 0.5


def test_a_share_without_a_stock_picture_stays_unknown():
    con = store.memory_connection()
    now = int(time.time()) - 60
    con.execute(
        "INSERT INTO undercut_observation (observation_id, scan_id,"
        " observed_at, realm, item_id, item_name, own_auction_id,"
        " own_quantity, own_unit_price_copper, lowest_unit_price_copper,"
        " undercut_pct, items_ahead, time_left, source, confidence, imported_at)"
        " VALUES ('o','s',?,'Dragonmaw',1,'Thing',1,1,100,90,0.1,1,7200,"
        " 'blizzard_api','price-only','')", (now,))
    con.commit()

    assert realm_index.undercut_share(con, "Dragonmaw") is None


def _snapshot(con, snapshot_id, captured_at, prices, status="ok"):
    """Write one snapshot the way ``ah.ingest_bundle`` would.

    ``price_status`` is part of the row: a price whose status is unknown
    (``missing``) is not a price a consumer may aggregate, so a fixture
    that means "this item was listed at this price" has to say so.
    """
    con.execute(
        "INSERT INTO ah_snapshot (snapshot_id, region, realm, captured_at,"
        " source, payload_sha256, imported_at) VALUES (?,?,?,?,?,?,?)",
        (snapshot_id, "eu", "Dragonmaw", captured_at, "blizzard_api",
         snapshot_id, ""))
    for item_id, price in prices.items():
        con.execute(
            "INSERT INTO ah_auction (snapshot_id, auction_key, auction_id,"
            " item_id, quantity, unit_price_copper, is_commodity, price_status)"
            " VALUES (?,?,?,?,?,?,0,?)",
            (snapshot_id, f"{snapshot_id}-{item_id}", item_id, item_id, 1,
             price, status))
    con.commit()


def test_babysit_risk_uses_price_drops_between_two_snapshots():
    con = store.memory_connection()
    _snapshot(con, "a", 1_000, {1: 100, 2: 100, 3: 100, 4: 100})
    _snapshot(con, "b", 4_600, {1: 90, 2: 100, 3: 100, 4: 110})

    risk = realm_index.babysit_risk(con, "Dragonmaw", horizon_h=24)

    assert risk["drops"] == 1
    assert risk["exposures"] == 4
    assert risk["probability"] > 0.9


def test_one_snapshot_is_not_a_probability():
    con = store.memory_connection()
    _snapshot(con, "a", 1_000, {1: 100})

    assert realm_index.babysit_risk(con, "Dragonmaw") is None


def test_a_sentinel_price_is_not_the_cheapest_ask():
    """A row whose price may not be used must not set the floor.

    99,999,999,000c is the "no data" sentinel; if ``MIN`` saw it the
    babysit risk would invent an undercut that never happened, and if it
    saw an impossible low value it would hide the real floor.
    """
    con = store.memory_connection()
    _snapshot(con, "a", 1_000, {1: 100, 2: 100}, status="ok")
    _snapshot(con, "b", 4_600, {1: 90, 2: 100}, status="ok")
    # the same two snapshots also carry unusable rows for the same items
    for snapshot_id, prices in (("a", {1: 99_999_999_000, 2: 99_999_999_000}),
                                ("b", {1: 99_999_999_000, 2: 99_999_999_000})):
        for item_id, price in prices.items():
            con.execute(
                "INSERT INTO ah_auction (snapshot_id, auction_key, auction_id,"
                " item_id, quantity, unit_price_copper, is_commodity,"
                " price_status) VALUES (?,?,?,?,?,?,0,'sentinel')",
                (snapshot_id, f"{snapshot_id}-{item_id}-sentinel", 9_000 + item_id,
                 item_id, 1, price))
    con.commit()

    risk = realm_index.babysit_risk(con, "Dragonmaw", horizon_h=24)

    assert risk["drops"] == 1
    assert risk["exposures"] == 2


def test_a_row_with_unknown_price_status_is_not_evidence():
    con = store.memory_connection()
    _snapshot(con, "a", 1_000, {1: 100, 2: 100}, status="missing")
    _snapshot(con, "b", 4_600, {1: 90, 2: 100}, status="missing")

    assert realm_index.babysit_risk(con, "Dragonmaw", horizon_h=24) is None


def test_best_realm_discounts_price_by_the_measured_undercut_share():
    index = {
        "Pricey": {"factor": 2.0, "source": "regional"},
        "Heavy": {"factor": 1.2, "source": "realm", "undercutShare": 0.9,
                  "undercutWatched": 10},
    }

    ranked = realm_index.best_realms(1, 1_000, index, limit=2)

    assert [row["realm"] for row in ranked] == ["Pricey", "Heavy"]
    assert ranked[0]["score"] == 2_000
    assert ranked[1]["score"] == 660


def test_a_realm_record_beats_the_factor():
    index = {"A": {"factor": 1.0, "source": "regional"},
             "B": {"factor": 1.0, "source": "realm", "undercutShare": 0.2,
                   "undercutWatched": 5}}
    records = {"B": {1: type("R", (), {"price": 3_000})()}}

    ranked = realm_index.best_realms(1, 1_000, index, records=records, limit=2)

    assert ranked[0]["realm"] == "B"
    assert ranked[0]["price_copper"] == 3_000
    assert ranked[0]["score"] == 2_700
