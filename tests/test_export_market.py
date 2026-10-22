"""What the export ships: the action, the market, and the economics it computed.

The regression this pins: the serialiser used to rebuild each record from
the bare estimate, so the vendor value and the net price computed in
``_attach_economics`` were thrown away and every item shipped
``vendor = 0, net = 0``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from woweconomy.ml.market_context import ItemMarket
from woweconomy.pricing.config import PricingConfig
from woweconomy.pricing.dataset import (
    DatasetBuilder,
    DatasetRecord,
    _lua_table,
)
from woweconomy.pricing.engine import Estimate


class _Engine(SimpleNamespace):
    """Just enough engine for the serialiser."""

    shipped_model = None
    price_interval = {}
    interests = None
    item_mapping: dict = {}


def _builder(estimate: Estimate, record: DatasetRecord) -> DatasetBuilder:
    builder = DatasetBuilder(config=PricingConfig(), engine=_Engine())
    builder.generated_at = 1_700_000_000
    builder.records = [record]
    builder.record_index = {(record.realm, record.item_id): record}
    builder.realms = {
        record.realm: {
            "auctionHouse": "dragonmaw",
            "updatedAt": 1_700_000_000,
            "sampleSize": 3,
            "items": {record.item_id: estimate},
            "market": {"coverage": "supply+trend", "confidence": 0.6},
        }
    }
    return builder


def _estimate(**kwargs) -> Estimate:
    base = dict(realm="Dragonmaw", item_id=100, price=10000, confidence=0.5,
                sale_probability=0.4, sample_size=3, observed_at=1_699_000_000,
                prediction_status="validated")
    base.update(kwargs)
    return Estimate(**base)


def test_the_serialised_record_keeps_the_vendor_and_net_it_computed():
    estimate = _estimate()
    record = DatasetRecord("Dragonmaw", 100, estimate, vendor_price=25,
                           net_price=9310,
                           action={"action": "post", "ask": 10000, "net": 9310})
    text = _builder(estimate, record).to_lua()
    assert "vendor = 25" in text
    assert "net = 9310" in text
    assert 'action = { action = "post"' in text


def test_the_market_is_shipped_when_it_was_observed():
    estimate = _estimate(market={"coverage": "supply", "listings": 4,
                                 "units": 6, "cheapest_ask": 9000})
    record = DatasetRecord("Dragonmaw", 100, estimate, vendor_price=25)
    text = _builder(estimate, record).to_lua()
    assert 'market = { cheapest_ask = 9000, coverage = "supply", listings = 4, units = 6 }' in text


def test_no_market_means_no_field_rather_than_an_empty_one():
    estimate = _estimate()
    record = DatasetRecord("Dragonmaw", 100, estimate, vendor_price=25)
    text = _builder(estimate, record).to_lua()
    item_line = next(line for line in text.splitlines() if "[100] = {" in line)
    assert "market" not in item_line
    assert 'action = {' not in item_line


def test_the_realm_block_carries_the_market_line():
    estimate = _estimate()
    record = DatasetRecord("Dragonmaw", 100, estimate)
    text = _builder(estimate, record).to_lua()
    assert 'market = { confidence = 0.6, coverage = "supply+trend" }' in text


def test_the_market_does_not_change_the_price_it_is_attached_to():
    plain = _estimate()
    with_market = _estimate(market={"coverage": "supply", "listings": 4},
                            market_detail=ItemMarket(item_id=100, listings=4))
    plain_text = _builder(plain, DatasetRecord("Dragonmaw", 100, plain)).to_lua()
    market_text = _builder(with_market, DatasetRecord("Dragonmaw", 100, with_market)).to_lua()
    assert "price = 10000" in plain_text
    assert "price = 10000" in market_text
    assert "conf = 0.5" in plain_text and "conf = 0.5" in market_text
    assert "psell = 0.4" in plain_text and "psell = 0.4" in market_text


def test_lua_tables_are_written_with_sorted_keys():
    text = _lua_table({"b": 2, "a": 1, "nested": {"z": None, "y": "x"}})
    assert text == '{ a = 1, b = 2, nested = { y = "x", z = nil } }'


def test_a_quoted_key_is_written_as_an_index():
    assert _lua_table({"sale rate": 0.5}) == '{ ["sale rate"] = 0.5 }'


def test_an_action_with_a_market_summary_is_nested_correctly():
    assert _lua_table({"action": "hold", "market": {"own_share": 0.75}}) == \
        '{ action = "hold", market = { own_share = 0.75 } }'


def test_the_engine_builds_the_realm_market_once(tmp_path):
    """The same realm and items must not be re-read for every item."""
    from woweconomy.pricing.engine import PricingEngine

    config = PricingConfig()
    config.tsm_db = tmp_path / "tsm.db"
    config.intel_db = tmp_path / "missing.sqlite"
    config.dfa_db = tmp_path / "dfa.db"
    sqlite3 = pytest.importorskip("sqlite3")
    con = sqlite3.connect(config.tsm_db)
    con.executescript("""
        CREATE TABLE tsm_realm_group (realm_slug TEXT PRIMARY KEY,
            realm_name TEXT, group_id INTEGER, canonical_slug TEXT NOT NULL,
            canonical_name TEXT, is_canonical INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE tsm_snapshot (snapshot_id INTEGER PRIMARY KEY, scope TEXT,
            kind TEXT, updated_at TEXT, fetched_at TEXT);
        CREATE TABLE tsm_price_change (snapshot_id INTEGER, scope TEXT, kind TEXT,
            key_id INTEGER, market_value INTEGER, min_buyout INTEGER,
            recent INTEGER, historical INTEGER, avg_sale_price INTEGER,
            sale_rate REAL, sold_per_day REAL);
        CREATE TABLE tsm_region_price (key_id INTEGER PRIMARY KEY, name TEXT,
            market_value INTEGER, historical INTEGER, avg_sale_price INTEGER,
            sale_rate REAL, sold_per_day REAL, updated_at TEXT, fetched_at TEXT,
            snapshot_id INTEGER);
    """)
    con.execute("INSERT INTO tsm_realm_group VALUES ('dragonmaw','Dragonmaw',1,'dragonmaw','Dragonmaw',1)")
    for sid in (1, 2, 3):
        con.execute("INSERT INTO tsm_snapshot VALUES (?,?,?,?,?)",
                    (sid, "dragonmaw", "realm_items", "2023-11-14T10:00:00Z",
                     "2023-11-14T10:00:00Z"))
        con.execute("INSERT INTO tsm_price_change VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, "dragonmaw", "realm_items", 100, 1000 * sid,
                     1000 * sid, 0, 0, 0, 0.0, 0.0))
    con.commit()
    con.close()

    engine = PricingEngine(config)
    first = engine.market_for("Dragonmaw", [100])
    second = engine.market_for("Dragonmaw", [100])
    assert first is second
    assert first.get(100).path_snapshots == 3


def test_the_builder_still_serialises_a_record_that_was_not_indexed():
    """An estimate without a record (e.g. a realm added late) must not crash."""
    estimate = _estimate()
    builder = DatasetBuilder(config=PricingConfig(), engine=_Engine())
    builder.generated_at = 1
    builder.records = []
    builder.realms = {"Dragonmaw": {"auctionHouse": "dragonmaw", "items": {100: estimate}}}
    text = builder.to_lua()
    assert "price = 10000" in text
