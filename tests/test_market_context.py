"""The market around an item: what is observed, what is estimated, what is unknown.

The tests pin the two things that make this layer trustworthy: a missing
source is reported as unknown rather than as zero, and the numbers come from
the snapshot they claim to come from.  A synthetic database is used so every
assertion can be checked by hand.
"""
from __future__ import annotations

import sqlite3

import pytest

from woweconomy.intel.prices import (
    GOLD_CAP_COPPER,
    IMPOSSIBLE,
    OK,
    SENTINEL,
    SENTINEL_COPPER,
)
from woweconomy.ml.market_context import (
    OWN_SHARE_WARN,
    STALE_AGE_H,
    build_realm_market,
    competition_pressure,
    supply_pressure,
)

NOW = 1_700_000_000


def _wow_db(tmp_path, *, mapping=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "tsm.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE tsm_realm_group (
            realm_slug TEXT PRIMARY KEY, realm_name TEXT, group_id INTEGER,
            canonical_slug TEXT NOT NULL, canonical_name TEXT,
            is_canonical INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE tsm_snapshot (
            snapshot_id INTEGER PRIMARY KEY, scope TEXT, kind TEXT,
            updated_at TEXT, fetched_at TEXT);
        CREATE TABLE tsm_price_change (
            snapshot_id INTEGER, scope TEXT, kind TEXT, key_id INTEGER,
            market_value INTEGER, min_buyout INTEGER, recent INTEGER,
            historical INTEGER, avg_sale_price INTEGER, sale_rate REAL,
            sold_per_day REAL);
        CREATE TABLE tsm_region_price (
            key_id INTEGER PRIMARY KEY, name TEXT, market_value INTEGER,
            historical INTEGER, avg_sale_price INTEGER, sale_rate REAL,
            sold_per_day REAL, updated_at TEXT, fetched_at TEXT,
            snapshot_id INTEGER);
    """)
    if mapping:
        con.execute("INSERT INTO tsm_realm_group VALUES"
                    " ('dragonmaw','Dragonmaw',1,'dragonmaw','Dragonmaw',1)")
    for sid, stamp in ((1, "2023-11-14T10:00:00Z"), (2, "2023-11-14T11:00:00Z"),
                       (3, "2023-11-14T12:00:00Z")):
        con.execute("INSERT INTO tsm_snapshot VALUES (?,?,?,?,?)",
                    (sid, "dragonmaw", "realm_items", stamp, stamp))
        # item 100: 1000 -> 1200 -> 1500 (rising); item 101: 500 -> 450 -> 400
        for key, prices in ((100, (1000, 1200, 1500)), (101, (500, 450, 400))):
            con.execute(
                "INSERT INTO tsm_price_change VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "dragonmaw", "realm_items", key, prices[sid - 1],
                 prices[sid - 1], 0, 0, 0, 0.0, 0.0))
    con.execute("INSERT INTO tsm_region_price VALUES"
                " (100,'Item 100',0,0,0,0.25,3.5,'', '', 3)")
    con.commit()
    con.close()
    return path


def _intel_db(tmp_path, *, supply=True, stale=False, parked=False):
    path = tmp_path / "intel.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE ah_snapshot (
            snapshot_id TEXT PRIMARY KEY, region TEXT, connected_realm_id INTEGER,
            realm TEXT, realm_slug TEXT, captured_at INTEGER,
            api_last_modified TEXT, source TEXT, payload_sha256 TEXT,
            auction_count INTEGER, commodity_count INTEGER,
            rejected_rows INTEGER, imported_at TEXT);
        CREATE TABLE ah_auction (
            snapshot_id TEXT, auction_key TEXT, auction_id INTEGER,
            item_id INTEGER, item_context INTEGER, quantity INTEGER,
            unit_price_copper INTEGER, buyout_copper INTEGER,
            bid_copper INTEGER, total_price_copper INTEGER,
            unit_price_source TEXT, time_left_raw TEXT, time_left_band TEXT,
            is_commodity INTEGER, pet_species_id INTEGER, bonus_lists TEXT,
            modifiers TEXT,
            price_status TEXT NOT NULL DEFAULT 'missing',
            price_reason TEXT NOT NULL DEFAULT '',
            variant_key TEXT,
            PRIMARY KEY (snapshot_id, auction_key));
        CREATE TABLE undercut_observation (
            observation_id TEXT PRIMARY KEY, scan_id TEXT, observed_at INTEGER,
            realm TEXT, character TEXT, item_id INTEGER, item_name TEXT,
            item_key TEXT, item_link TEXT, own_auction_id INTEGER,
            own_quantity INTEGER, own_buyout_copper INTEGER,
            own_unit_price_copper INTEGER, lowest_unit_price_copper INTEGER,
            lowest_seller TEXT, lowest_seller_realm TEXT,
            lowest_auction_id INTEGER, undercut_copper INTEGER,
            undercut_pct REAL, items_ahead INTEGER, sellers_below INTEGER,
            time_left INTEGER, sellers_json TEXT, source TEXT, confidence TEXT,
            reference_status TEXT, imported_at TEXT);
        CREATE TABLE inventory_snapshot (
            snapshot_id TEXT PRIMARY KEY, captured_at INTEGER, character TEXT,
            realm TEXT, reason TEXT, source TEXT, bag_slots INTEGER,
            available_json TEXT, imported_at TEXT);
        CREATE TABLE inventory_posted_listing (
            snapshot_id TEXT, auction_id INTEGER, item_id INTEGER,
            item_name TEXT, quantity INTEGER, unit_price_copper INTEGER,
            buyout_copper INTEGER, time_left INTEGER,
            PRIMARY KEY (snapshot_id, auction_id, item_id));
    """)
    if not supply:
        con.commit()
        con.close()
        return path
    older = NOW - (6 * 86400 if stale else 3600)
    newer = older + 1800
    con.execute("INSERT INTO ah_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("old", "eu", 1, "Dragonmaw", "dragonmaw", older, "", "blizzard_api",
                 "a", 3, 0, 0, ""))
    con.execute("INSERT INTO ah_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("new", "eu", 1, "Dragonmaw", "dragonmaw", newer, "", "blizzard_api",
                 "b", 3, 0, 0, ""))
    rows = [
        ("old", "a1", 11, 100, 1, 4, 1000),
        ("old", "a2", 12, 100, 1, 2, 900),
        ("new", "b1", 11, 100, 1, 3, 1200),
        ("new", "b2", 13, 100, 1, 1, 1200),
        ("new", "b3", 14, 101, 1, 2, 400),
    ]
    # ``price_status`` is stated explicitly and is ``'ok'``: the column
    # defaults to ``'missing'``, and a row without a verdict has no usable
    # price.  Leaving it out here would make every cheapest ask below NULL.
    for snapshot, key, auction, item, ctx, qty, price in rows:
        con.execute(
            "INSERT INTO ah_auction (snapshot_id, auction_key, auction_id,"
            " item_id, item_context, quantity, unit_price_copper,"
            " buyout_copper, total_price_copper, time_left_band,"
            " is_commodity, price_status) VALUES (?,?,?,?,?,?,?,?,?,'LONG',0,'ok')",
            (snapshot, key, auction, item, ctx, qty, price, price * qty, price * qty))
    if parked:
        # item 102 is only ever listed at the no-data ceiling; item 103 only
        # above the gold cap; item 104 has one parked row beside a real ask.
        for key, auction, item, price, status in (
                ("b4", 15, 102, SENTINEL_COPPER + 900, SENTINEL),
                ("b5", 16, 103, GOLD_CAP_COPPER + 1, IMPOSSIBLE),
                ("b6", 17, 104, SENTINEL_COPPER + 900, SENTINEL),
                ("b7", 18, 104, 800, OK)):
            con.execute(
                "INSERT INTO ah_auction (snapshot_id, auction_key, auction_id,"
                " item_id, item_context, quantity, unit_price_copper,"
                " buyout_copper, total_price_copper, time_left_band,"
                " is_commodity, price_status) VALUES"
                " ('new',?,?,?,1,1,?,?,?,'LONG',0,?)",
                (key, auction, item, price, price, price, status))
    con.execute("INSERT INTO inventory_snapshot VALUES"
                " ('inv', ?, 'me', 'Dragonmaw', '', '', 0, '', '')", (newer - 60,))
    con.execute("INSERT INTO inventory_posted_listing VALUES"
                " ('inv', 11, 100, 'Item 100', 3, 1200, 3600, 24)")
    con.execute(
        "INSERT INTO undercut_observation (observation_id, observed_at, realm,"
        " item_id, items_ahead, sellers_below, undercut_pct, lowest_seller)"
        " VALUES ('u1', ?, 'Dragonmaw', 100, 3, 2, 0.08, 'Someone')",
        (newer - 120,))
    con.commit()
    con.close()
    return path


def test_supply_comes_from_the_newest_snapshot_only(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [100], now=NOW)
    item = market.get(100)
    assert item is not None
    assert item.listings == 2          # the two rows in the new snapshot
    assert item.units == 4             # 3 + 1, not the old 4 + 2
    assert item.cheapest_ask == 1200
    assert "supply" in item.observed


def test_supply_and_cheapest_change_against_the_previous_snapshot(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [100], now=NOW)
    item = market.get(100)
    # 6 units before, 4 now
    assert item.units_change_pct == pytest.approx(-1 / 3, abs=1e-4)
    # cheapest 900 -> 1200
    assert item.cheapest_change_pct == pytest.approx(1 / 3, abs=1e-4)
    assert supply_pressure(item) == pytest.approx(-1 / 3, abs=1e-4)


def test_our_own_stock_is_counted_and_flagged(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [100], now=NOW)
    item = market.get(100)
    assert item.own_units == 3
    assert item.own_share == pytest.approx(0.75)
    assert item.own_share >= OWN_SHARE_WARN
    assert item.own_cheapest_ask == 1200
    assert any("own stock" in warning for warning in item.warnings)
    # our listing is also the cheapest one in the snapshot
    assert any("cheapest listing on this item is ours" in warning
               for warning in item.warnings)


def test_the_price_path_gives_direction_and_noise(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [100, 101], now=NOW)
    rising = market.get(100)
    falling = market.get(101)
    assert rising.trend_pct == pytest.approx(0.5)      # 1000 -> 1500
    assert falling.trend_pct == pytest.approx(-0.2)    # 500 -> 400
    assert rising.path_snapshots == 3
    assert rising.volatility is not None and rising.volatility > 0


def test_competition_is_taken_from_the_scan_that_saw_it(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [100], now=NOW)
    item = market.get(100)
    assert item.items_ahead == 3
    assert item.sellers_below == 2
    assert item.undercut_pct == pytest.approx(0.08)
    assert competition_pressure(item) == pytest.approx(0.3)


def test_regional_liquidity_is_an_estimate_not_an_observation(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [100], now=NOW)
    item = market.get(100)
    assert item.region_sale_rate == pytest.approx(0.25)
    assert item.region_sold_per_day == pytest.approx(3.5)
    assert "liquidity" in item.estimates
    assert "liquidity" not in item.observed


def test_a_missing_intel_database_leaves_supply_unknown_not_zero(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), tmp_path / "absent.sqlite",
                               "Dragonmaw", [100], now=NOW)
    item = market.get(100)
    assert item.listings is None
    assert item.units is None
    assert item.cheapest_ask is None
    assert "supply" not in item.observed
    assert item.trend_pct == pytest.approx(0.5)  # the path still answers
    assert item.coverage == "trend"
    assert any("unknown, not zero" in warning for warning in market.warnings)


def test_no_intel_rows_for_this_item_is_not_a_zero_supply(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [999], now=NOW)
    item = market.get(999)
    assert item.listings is None
    assert item.coverage == "none"
    assert item.confidence == 0.0


def test_a_listing_parked_at_the_no_data_ceiling_is_supply_without_a_price(
        tmp_path):
    """99,999,999,900 copper is TSM's "I don't know", not 9,999,999 gold.

    The row is still evidence that someone is trying to sell the item, so
    the listing and unit counts stay.  It is *not* evidence of what the item
    costs, so the cheapest ask is unknown.  Reporting the ceiling here would
    teach the model that the market price is ten million gold.
    """
    market = build_realm_market(_wow_db(tmp_path),
                                _intel_db(tmp_path, parked=True),
                                "Dragonmaw", [102], now=NOW)
    item = market.get(102)
    assert item.listings == 1
    assert item.units == 1
    assert item.cheapest_ask is None
    assert "supply" in item.observed


def test_a_buyout_above_the_gold_cap_is_not_a_price_either(tmp_path):
    market = build_realm_market(_wow_db(tmp_path),
                                _intel_db(tmp_path, parked=True),
                                "Dragonmaw", [103], now=NOW)
    item = market.get(103)
    assert item.listings == 1
    assert item.cheapest_ask is None


def test_a_parked_listing_does_not_become_the_cheapest_ask(tmp_path):
    """The cheapest *usable* ask, not the cheapest row."""
    market = build_realm_market(_wow_db(tmp_path),
                                _intel_db(tmp_path, parked=True),
                                "Dragonmaw", [104], now=NOW)
    item = market.get(104)
    assert item.listings == 2      # both rows are supply
    assert item.units == 2
    assert item.cheapest_ask == 800  # the parked row is not a candidate


def test_a_stale_snapshot_is_flagged_and_worth_less(tmp_path):
    fresh = build_realm_market(_wow_db(tmp_path / "fresh"),
                               _intel_db(tmp_path / "fresh"),
                               "Dragonmaw", [100], now=NOW).get(100)
    stale = build_realm_market(_wow_db(tmp_path / "stale"),
                               _intel_db(tmp_path / "stale", stale=True),
                               "Dragonmaw", [100], now=NOW).get(100)
    assert stale.supply_age_h > STALE_AGE_H
    assert any("old" in warning for warning in stale.warnings)
    assert stale.confidence < fresh.confidence


def test_a_realm_without_a_tsm_scope_still_reports_what_it_has(tmp_path):
    market = build_realm_market(_wow_db(tmp_path, mapping=False),
                                _intel_db(tmp_path),
                                "Dragonmaw", [100], now=NOW)
    item = market.get(100)
    assert item.trend_pct is None
    assert item.listings == 2
    assert item.coverage == "supply+competition"
    assert any("no price path" in warning for warning in market.warnings)


def test_the_exported_market_carries_timestamps_not_ages(tmp_path):
    """An age changes with the clock; a timestamp does not."""
    item = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                              "Dragonmaw", [100], now=NOW).get(100)
    exported = item.as_dict()
    assert "supply_age_h" not in exported
    assert exported["supplyAsOf"] > 0
    assert exported["asOf"] == item.as_of


def test_the_summary_states_coverage_and_which_items_have_supply(tmp_path):
    market = build_realm_market(_wow_db(tmp_path), _intel_db(tmp_path),
                                "Dragonmaw", [100, 101, 102], now=NOW)
    summary = market.summary()
    assert summary["coverage"] == "supply+trend+competition"
    assert summary["itemsWithSupply"] == 2
    assert summary["asOf"] == market.as_of
    assert market.sources["trendSnapshots"] == [1, 2, 3]
