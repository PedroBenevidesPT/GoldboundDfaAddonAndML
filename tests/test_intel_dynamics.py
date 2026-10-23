"""Per-realm dynamics: undercuts, departures, liquidity and cancels."""
from woweconomy.intel import ah, dynamics, store

DAY = 86_400


def row(key, item_id, price, qty=1, time_left="SHORT"):
    return {
        "auction_key": key, "auction_id": None, "item_id": item_id,
        "quantity": qty, "unit_price_copper": price, "buyout_copper": price * qty,
        "bid_copper": 0, "time_left_raw": time_left,
        "time_left_band": time_left, "is_commodity": 0,
    }


SNAP_A = [
    row("a", 1, 1000, 5),
    row("b", 1, 1200, 2, "LONG"),
    row("c", 2, 500, 10),
]
SNAP_B = [
    row("a", 1, 900, 5),
    row("d", 3, 300, 1),
]


def payload(rows, ids):
    return {"auctions": [
        {"id": ids[r["auction_key"]], "item": {"id": r["item_id"]},
         "quantity": r["quantity"], "unit_price": r["unit_price_copper"],
         "time_left": r["time_left_raw"]}
        for r in rows]}


def test_the_ladder_measures_the_undercut_without_seller_names():
    metrics = dynamics.market_metrics(SNAP_A)
    assert metrics["listings"] == 3
    assert metrics["items"] == 2
    assert metrics["units"] == 17
    assert metrics["min_unit_price"] == 500
    assert metrics["median_undercut_gap_pct"] == 16.667
    assert metrics["share_units_at_floor"] == 0.8824
    assert metrics["time_left"]["LONG"] == 1


def test_an_item_that_disappeared_is_a_departure_not_a_sale():
    change = dynamics.compare_snapshots(SNAP_A, SNAP_B, gap_hours=1.0)
    item2 = next(i for i in change["items"] if i["item_id"] == 2)
    assert item2["unresolved_departures"] == 1
    assert item2["departed_units"] == 10
    assert item2["cur_min_unit_price"] is None
    assert "not a sale" in change["note"]
    assert change["new_listings"] == 1
    item1 = next(i for i in change["items"] if i["item_id"] == 1)
    assert item1["min_price_change_pct"] == -10.0


def test_item_metrics_uses_sellers_only_when_they_were_observed():
    sellers = [{"item_id": 1, "seller": "Tavil-Sylvanas"},
               {"item_id": 1, "seller": "Aoshi-Dragonmaw"}]
    with_sellers = dynamics.item_metrics(SNAP_A, 1, sellers)
    assert with_sellers["distinct_sellers"] == 2
    assert with_sellers["top_seller_share"] == 0.5
    without = dynamics.item_metrics(SNAP_A, 1)
    assert without["distinct_sellers"] is None
    assert "unavailable" in without["seller_basis"]


def test_liquidity_counts_only_confirmed_sales_and_keeps_censoring():
    events = [
        {"event_type": "listing", "ts": 1000, "item_id": 1, "quantity": 1,
         "unit_price_copper": 1000, "realm": "Dragonmaw", "character": "B"},
        {"event_type": "sale", "ts": 1000 + 24 * 3600, "item_id": 1,
         "quantity": 1, "unit_price_copper": 1000, "total_price_copper": 1000,
         "realm": "Dragonmaw", "character": "B"},
        {"event_type": "listing", "ts": 1000, "item_id": 2, "quantity": 1,
         "unit_price_copper": 500, "realm": "Dragonmaw", "character": "B"},
        {"event_type": "expire", "ts": 1000 + 48 * 3600, "item_id": 2,
         "quantity": 1, "realm": "Dragonmaw", "character": "B"},
        {"event_type": "listing", "ts": 1000, "item_id": 3, "quantity": 1,
         "unit_price_copper": 300, "realm": "Dragonmaw", "character": "B"},
        {"event_type": "cancel", "ts": 1000 + 12 * 3600, "item_id": 3,
         "quantity": 1, "realm": "Dragonmaw", "character": "B"},
    ]
    liquidity = dynamics.liquidity_from_events(events, "Dragonmaw", horizons=(24,))
    assert liquidity["confirmed_sales"] == 1
    assert liquidity["expiries"] == 1
    assert liquidity["cancellations"] == 1
    assert liquidity["sale_rate"] == 0.5
    assert "sales / (sales + expiries)" in liquidity["sale_rate_note"]
    assert liquidity["time_to_sale"]["median"] == 24
    assert liquidity["sale_probability_by_h"]["24"] == 0.5


def test_cancel_response_reports_the_ladder_around_the_cancel():
    events = [{"event_type": "cancel", "ts": 1500, "item_id": 1,
               "quantity": 5, "realm": "Dragonmaw"}]
    with_both = dynamics.cancel_scan_response(
        events, [(1000, SNAP_A), (2000, SNAP_B)], "Dragonmaw")
    assert with_both[0]["observed"] is True
    assert with_both[0]["prev_listings"] == 2
    assert with_both[0]["next_listings"] == 1
    assert "not a causal effect" in with_both[0]["note"]
    before_only = dynamics.cancel_scan_response(
        events, [(1000, SNAP_A)], "Dragonmaw")
    assert before_only[0]["observed"] is False


def test_realm_dynamics_is_scoped_to_one_realm():
    con = store.memory_connection()
    ids = {"a": 1, "b": 2, "c": 3, "d": 4}
    first = ah.parse_payload(payload(SNAP_A, ids), region="eu",
                             realm="Dragonmaw", captured_at=1000)
    second = ah.parse_payload(payload(SNAP_B, ids), region="eu",
                              realm="Dragonmaw", captured_at=1000 + 3600)
    other = ah.parse_payload(payload(SNAP_A, ids), region="eu",
                             realm="Silvermoon", captured_at=1000)
    for bundle in (first, second, other):
        ah.ingest_bundle(con, bundle)
    report = dynamics.realm_dynamics(con, "eu", "Dragonmaw")
    assert report["available"] is True
    assert report["realm"] == "Dragonmaw"
    assert report["change"]["unresolved_departures"] == 2
    silvermoon = dynamics.realm_dynamics(con, "eu", "Silvermoon")
    assert silvermoon["snapshot_id"] != report["snapshot_id"]
    assert silvermoon["captured_at"] == 1000
