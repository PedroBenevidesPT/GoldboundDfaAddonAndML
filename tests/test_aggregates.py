"""The daily aggregates: correctness, honest scope, drift, and reconstruction.

The definitions are checked against hand-computed fixtures here.  ``validate()``
cannot do that job: it rebuilds with the same code, so a wrong definition would
agree with itself.  These tests are the independent check; ``validate()`` is the
check against partial builds and tampering, exercised separately below.

The most important test in this file is
``test_a_price_level_is_reconstructed_by_carrying_the_last_observation``: it is
the whole justification for building aggregates before deleting anything.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from woweconomy.aggregates import (
    AGGREGATE_FOR_TABLE,
    AGGREGATE_SCHEMA_VERSION,
    COVERED_BY_CHILD,
    SchemaMismatch,
    build,
    coverage,
    day_price,
    open_aggregates,
    price_as_of,
    validate,
)


def _epoch(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).timestamp())


def _tsm(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE tsm_snapshot (
            snapshot_id INTEGER PRIMARY KEY, scope TEXT, kind TEXT,
            updated_at TEXT, fetched_at TEXT, checksum TEXT, rows INTEGER,
            changed INTEGER, path TEXT, etag TEXT, last_modified TEXT);
        CREATE TABLE tsm_price_change (
            snapshot_id INTEGER, scope TEXT, kind TEXT, key_id INTEGER,
            market_value INTEGER, min_buyout INTEGER, recent INTEGER,
            historical INTEGER, avg_sale_price INTEGER, sale_rate REAL,
            sold_per_day REAL);
        CREATE TABLE tsm_realm_price (
            scope TEXT, item_id INTEGER, name TEXT, market_value INTEGER,
            min_buyout INTEGER, recent INTEGER, historical INTEGER,
            updated_at TEXT, fetched_at TEXT, snapshot_id INTEGER);
        CREATE TABLE auction_events (
            event_id TEXT PRIMARY KEY, schema_version INTEGER, event_type TEXT,
            ts TEXT, timestamp_unix INTEGER, item_id INTEGER, item_name TEXT,
            item_link TEXT, qty INTEGER, unit_price_copper INTEGER,
            total_price_copper INTEGER, deposit_copper INTEGER,
            fee_copper INTEGER, net_proceeds_copper INTEGER, realm TEXT,
            character TEXT, faction TEXT, counterparty TEXT, source TEXT,
            provenance TEXT, confidence TEXT, market_ref_copper INTEGER,
            raw_json TEXT, imported_at TEXT);
        CREATE TABLE auction_intel_events (
            event_id TEXT PRIMARY KEY, event_type TEXT, ts INTEGER,
            item_id INTEGER, item_name TEXT, quantity INTEGER,
            unit_price_copper INTEGER, total_price_copper INTEGER,
            deposit_copper INTEGER, consignment_fee_copper INTEGER,
            net_proceeds_copper INTEGER, market_ref_copper INTEGER,
            realm TEXT, character TEXT, counterparty TEXT, source TEXT,
            provenance TEXT, confidence TEXT, schema_version INTEGER,
            captured_at INTEGER, imported_at TEXT);
    """)
    con.commit()
    return con


def _intel(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE ah_snapshot (
            snapshot_id TEXT PRIMARY KEY, region TEXT,
            connected_realm_id INTEGER, realm TEXT, realm_slug TEXT,
            captured_at INTEGER, api_last_modified TEXT, source TEXT,
            payload_sha256 TEXT, auction_count INTEGER, commodity_count INTEGER,
            rejected_rows INTEGER, imported_at TEXT);
        CREATE TABLE ah_auction (
            snapshot_id TEXT, auction_key TEXT, auction_id INTEGER,
            item_id INTEGER, item_context INTEGER, quantity INTEGER,
            unit_price_copper INTEGER, buyout_copper INTEGER, bid_copper INTEGER,
            total_price_copper INTEGER, unit_price_source TEXT,
            time_left_raw TEXT, time_left_band TEXT, is_commodity INTEGER,
            pet_species_id INTEGER, bonus_lists TEXT, modifiers TEXT,
            price_status TEXT, price_reason TEXT, variant_key TEXT);
    """)
    con.commit()
    return con


@pytest.fixture
def dbs(tmp_path):
    tsm = tmp_path / "tsm_markets.db"
    intel = tmp_path / "intel.sqlite"
    agg = tmp_path / "aggregates.sqlite"
    tc, ic = _tsm(tsm), _intel(intel)
    yield tsm, intel, agg, tc, ic
    tc.close()
    ic.close()


def _seed(con: sqlite3.Connection) -> None:
    """Item 1 moves twice on the 20th, not at all on the 21st, again on the 22nd.

    That shape is what makes the carry-forward rule testable: the price on the
    21st is whatever the 20th closed at.
    """
    con.executemany(
        "INSERT INTO tsm_snapshot (snapshot_id, scope, kind, updated_at, rows,"
        " changed) VALUES (?,?,?,?,?,?)",
        [(1, "realm-a", "realm_items", "2026-10-20T08:00:00Z", 100, 2),
         (2, "realm-a", "realm_items", "2026-10-20T20:00:00Z", 100, 1),
         (3, "realm-a", "realm_items", "2026-10-21T10:00:00Z", 100, 1),
         (4, "realm-a", "realm_items", "2026-10-22T09:00:00Z", 100, 1)],
    )
    con.executemany(
        "INSERT INTO tsm_price_change (snapshot_id, scope, kind, key_id,"
        " market_value, min_buyout, avg_sale_price, sale_rate, sold_per_day)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "realm-a", "realm_items", 1, 100, 90, 105, 0.5, 2.0),
            (2, "realm-a", "realm_items", 1, 200, 180, 205, 0.6, 2.5),
            (3, "realm-a", "realm_items", 2, 50, 40, 52, 0.1, 0.5),
            (4, "realm-a", "realm_items", 1, 300, 280, 305, 0.7, 3.0),
        ],
    )
    con.commit()


# ---------------------------------------------------------------------------
# what the numbers are
# ---------------------------------------------------------------------------

def test_a_day_of_observations_matches_hand_computed_values(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    level = day_price(agg, "2026-10-20", "realm-a", "realm_items", 1)
    assert level is not None
    assert level["n_observations"] == 2
    assert level["n_snapshots"] == 2
    assert level["market_value"] == {"min": 100, "max": 200,
                                     "mean": 150.0, "last": 200}
    assert level["min_buyout"]["last"] == 180


def test_the_last_value_is_the_closing_observation_not_the_maximum(dbs):
    tsm, intel, agg, tc, _ic = dbs
    tc.execute("INSERT INTO tsm_snapshot VALUES "
               "(1,'realm-a','realm_items','2026-10-20T08:00:00Z',1,1,"
               "'c','1','p','e','l')")
    tc.execute("INSERT INTO tsm_snapshot VALUES "
               "(2,'realm-a','realm_items','2026-10-20T20:00:00Z',1,1,"
               "'c','1','p','e','l')")
    tc.execute("INSERT INTO tsm_price_change VALUES (1,'realm-a','realm_items',"
               "7,9000,9000,0,0,0,0,0)")
    tc.execute("INSERT INTO tsm_price_change VALUES (2,'realm-a','realm_items',"
               "7,10,10,0,0,0,0,0)")
    tc.commit()
    build(intel, tsm, agg)

    level = day_price(agg, "2026-10-20", "realm-a", "realm_items", 7)
    assert level["market_value"]["last"] == 10
    assert level["market_value"]["max"] == 9000


def test_a_missing_price_is_not_a_zero(dbs):
    """A NULL observation must leave the sums NULL, not make the item cheaper."""
    tsm, intel, agg, tc, _ic = dbs
    tc.execute("INSERT INTO tsm_snapshot VALUES "
               "(1,'realm-a','realm_items','2026-10-20T08:00:00Z',1,2,"
               "'c','1','p','e','l')")
    tc.execute("INSERT INTO tsm_price_change VALUES (1,'realm-a','realm_items',"
               "5,100,90,0,0,0,0,0)")
    tc.execute("INSERT INTO tsm_price_change VALUES (1,'realm-a','realm_items',"
               "5,NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    tc.commit()
    build(intel, tsm, agg)

    level = day_price(agg, "2026-10-20", "realm-a", "realm_items", 5)
    assert level["n_observations"] == 2
    assert level["market_value"]["min"] == 100
    assert level["market_value"]["mean"] == 50.0  # 100 over 2 observations


def test_the_day_boundary_is_utc(dbs):
    """23:59 and 00:01 fall in different days even though they are minutes apart."""
    tsm, intel, agg, tc, _ic = dbs
    tc.executemany(
        "INSERT INTO tsm_snapshot (snapshot_id, scope, kind, updated_at)"
        " VALUES (?,?,?,?)",
        [(1, "realm-a", "realm_items", "2026-10-20T23:59:00Z"),
         (2, "realm-a", "realm_items", "2026-10-21T00:01:00Z")],
    )
    tc.executemany(
        "INSERT INTO tsm_price_change (snapshot_id, scope, kind, key_id,"
        " market_value) VALUES (?,?,?,?,?)",
        [(1, "realm-a", "realm_items", 1, 100),
         (2, "realm-a", "realm_items", 1, 200)],
    )
    tc.commit()
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    days = dict(con.execute(
        "SELECT day, last_market_value FROM daily_item_price").fetchall())
    con.close()
    assert days == {"2026-10-20": 100, "2026-10-21": 200}


# ---------------------------------------------------------------------------
# honesty: the current-state table is not aggregated, and is not history
# ---------------------------------------------------------------------------

def test_the_current_state_table_has_no_aggregate_and_is_not_a_history_source():
    """tsm_realm_price is one row per item, upserted.  There is no history in
    it to aggregate, and expiring it would delete current prices."""
    assert "tsm_realm_price" not in AGGREGATE_FOR_TABLE
    assert "tsm_realm_price" not in COVERED_BY_CHILD
    # the history table is the one that is aggregated
    assert AGGREGATE_FOR_TABLE["tsm_price_change"] == "daily_item_price"
    # and a snapshot header survives through its aggregated child
    assert COVERED_BY_CHILD["tsm_snapshot"] == "daily_item_price"


def test_the_aggregate_is_built_from_the_change_log_not_the_current_state(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    # a current-state row that must not appear in the aggregate
    tc.execute("INSERT INTO tsm_realm_price VALUES "
               "('realm-a', 99, 'NeverChanged', 777, 777, 0, 0,"
               "'2026-10-20T09:00:00Z', '2026-10-20T09:00:00Z', 1)")
    tc.commit()
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    items = {r[0] for r in con.execute(
        "SELECT DISTINCT item_id FROM daily_item_price").fetchall()}
    con.close()
    assert 99 not in items, "current state is not an observation"


# ---------------------------------------------------------------------------
# reconstruction: the point of the whole exercise
# ---------------------------------------------------------------------------

def test_a_day_with_no_change_has_no_row_and_that_is_correct(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)
    assert day_price(agg, "2026-10-21", "realm-a", "realm_items", 1) is None


def test_a_price_level_is_reconstructed_by_carrying_the_last_observation(dbs):
    """The 21st had no change for item 1, so its price is the 20th's close."""
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    carried = price_as_of(agg, "2026-10-21", "realm-a", "realm_items", 1)
    assert carried is not None
    assert carried["observed_on"] == "2026-10-20"
    assert carried["market_value"] == 200
    assert carried["min_buyout"] == 180

    later = price_as_of(agg, "2026-10-22", "realm-a", "realm_items", 1)
    assert later["market_value"] == 300


def test_reconstruction_matches_the_raw_history(dbs):
    """What the aggregates say must be what the raw rows said."""
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    raw = sqlite3.connect(tsm)
    expected = raw.execute(
        "SELECT p.market_value FROM tsm_price_change p"
        "  JOIN tsm_snapshot s ON s.snapshot_id = p.snapshot_id"
        " WHERE p.key_id = 1 AND date(s.updated_at) <= '2026-10-21'"
        " ORDER BY s.updated_at DESC, p.snapshot_id DESC LIMIT 1"
    ).fetchone()[0]
    raw.close()

    assert price_as_of(agg, "2026-10-21", "realm-a", "realm_items",
                       1)["market_value"] == expected


def test_reconstruction_still_works_after_the_raw_rows_are_deleted(dbs):
    """The actual scenario: build, delete the raw history, then reconstruct."""
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)
    before = price_as_of(agg, "2026-10-21", "realm-a", "realm_items", 1)

    tc.execute("DELETE FROM tsm_price_change")
    tc.execute("DELETE FROM tsm_snapshot")
    tc.commit()

    after = price_as_of(agg, "2026-10-21", "realm-a", "realm_items", 1)
    assert after == before


def test_reconstruction_before_any_observation_returns_nothing(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)
    assert price_as_of(agg, "2026-01-01", "realm-a", "realm_items", 1) is None


# ---------------------------------------------------------------------------
# incremental rebuilds
# ---------------------------------------------------------------------------

def test_a_second_build_over_unchanged_data_writes_nothing(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    first = build(intel, tsm, agg)
    assert first.rows_written["daily_item_price"] == 3  # item1 x2 days, item2 x1

    second = build(intel, tsm, agg)
    assert second.rows_written["daily_item_price"] == 0
    assert second.partitions_skipped["daily_item_price"] == 3


def test_a_changed_source_partition_is_rebuilt(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    tc.execute("UPDATE tsm_price_change SET market_value=999"
               " WHERE snapshot_id=4")
    tc.commit()

    report = build(intel, tsm, agg)
    assert report.rows_written["daily_item_price"] == 1
    level = day_price(agg, "2026-10-22", "realm-a", "realm_items", 1)
    assert level["market_value"]["last"] == 999


def test_force_rebuilds_everything(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)
    report = build(intel, tsm, agg, force=True)
    assert report.rows_written["daily_item_price"] == 3


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------

def test_every_partition_records_its_source_rows_and_checksum(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    row = con.execute(
        "SELECT source_table, source_rows, source_checksum FROM aggregate_partition"
        " WHERE aggregate='daily_item_price' AND day='2026-10-20'"
    ).fetchone()
    con.close()
    assert row[0] == "tsm_price_change"
    assert row[1] == 2, "two change rows went into the 20th"
    assert len(row[2]) == 64


def test_the_checksum_changes_when_a_source_row_changes(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)
    con = sqlite3.connect(agg)
    before = con.execute(
        "SELECT source_checksum FROM aggregate_partition"
        " WHERE aggregate='daily_item_price' AND day='2026-10-20'").fetchone()[0]
    con.close()

    tc.execute("UPDATE tsm_price_change SET sale_rate=0.99 WHERE snapshot_id=1")
    tc.commit()
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    after = con.execute(
        "SELECT source_checksum FROM aggregate_partition"
        " WHERE aggregate='daily_item_price' AND day='2026-10-20'").fetchone()[0]
    con.close()
    assert before != after


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_validation_passes_on_a_fresh_build(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)
    report = validate(agg, intel, tsm)
    assert report.ok, report.summary()
    assert report.rows_compared["daily_item_price"] == 3


def test_validation_catches_a_tampered_aggregate(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    con.execute("UPDATE daily_item_price SET max_market_value=1"
                " WHERE item_id=1 AND day='2026-10-20'")
    con.commit()
    con.close()

    report = validate(agg, intel, tsm)
    assert not report.ok
    assert any(m.column == "max_market_value" and m.recomputed == 200
               for m in report.mismatches)


def test_validation_catches_a_missing_aggregate_row(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    con.execute("DELETE FROM daily_item_price WHERE item_id=2")
    con.commit()
    con.close()

    report = validate(agg, intel, tsm,
                      days=["2026-10-20", "2026-10-21", "2026-10-22"])
    assert not report.ok
    assert report.missing["daily_item_price"] == 1


def test_validation_reports_no_aggregates_rather_than_passing(dbs):
    tsm, intel, agg, tc, _ic = dbs
    build(intel, tsm, agg)
    report = validate(agg, intel, tsm)
    assert not report.ok
    assert any("no aggregates" in e for e in report.errors)


def test_validation_reports_a_missing_database(dbs, tmp_path):
    tsm, intel, _agg, tc, _ic = dbs
    report = validate(tmp_path / "nope.sqlite", intel, tsm)
    assert not report.ok
    assert any("missing" in e for e in report.errors)


# ---------------------------------------------------------------------------
# schema version
# ---------------------------------------------------------------------------

def test_a_database_from_another_schema_version_is_refused(dbs):
    tsm, intel, agg, tc, _ic = dbs
    build(intel, tsm, agg)
    con = sqlite3.connect(agg)
    con.execute("UPDATE aggregate_meta SET value=? WHERE key='schema_version'",
                (str(AGGREGATE_SCHEMA_VERSION + 1),))
    con.commit()
    con.close()

    with pytest.raises(SchemaMismatch):
        open_aggregates(agg)


# ---------------------------------------------------------------------------
# the other two aggregates
# ---------------------------------------------------------------------------

def test_ah_auction_aggregate_counts_and_sums_per_realm_per_item(dbs):
    tsm, intel, agg, _tc, ic = dbs
    ic.execute(
        "INSERT INTO ah_snapshot (snapshot_id, realm, captured_at) VALUES (?,?,?)",
        ("s1", "realm-a", _epoch("2026-10-20T09:00:00Z")))
    ic.executemany(
        "INSERT INTO ah_auction (snapshot_id, auction_key, item_id, quantity,"
        " unit_price_copper, total_price_copper, is_commodity)"
        " VALUES (?,?,?,?,?,?,?)",
        [("s1", "k1", 10, 2, 500, 1000, 0),
         ("s1", "k2", 10, 3, 400, 1200, 0),
         ("s1", "k3", 11, 1, 900, 900, 1)],
    )
    ic.commit()
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    row = con.execute(
        "SELECT n_auctions, n_commodity, sum_quantity, min_unit_price,"
        "       max_unit_price, sum_unit_price FROM daily_ah_auction"
        " WHERE day='2026-10-20' AND realm='realm-a' AND item_id=10"
    ).fetchone()
    con.close()
    assert row == (2, 0, 5, 400, 500, 900)


def test_events_aggregate_covers_both_event_tables(dbs):
    tsm, intel, agg, tc, _ic = dbs
    tc.execute(
        "INSERT INTO auction_events (event_id, event_type, ts, timestamp_unix,"
        " qty, total_price_copper, net_proceeds_copper, realm)"
        " VALUES ('e1','sale','2026-10-20T10:00:00Z',?,2,1000,950,'realm-a')",
        (_epoch("2026-10-20T10:00:00Z"),))
    tc.commit()
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    row = con.execute(
        "SELECT n_events, sum_quantity, sum_total, sum_net FROM daily_event"
        " WHERE day='2026-10-20' AND source='auction_events'"
    ).fetchone()
    con.close()
    assert row == (1, 2, 1000, 950)


def test_coverage_reports_what_exists(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)
    info = coverage(agg)
    assert info["exists"] is True
    assert info["aggregates"]["daily_item_price"]["days"] == 3


def test_coverage_of_a_missing_file_is_not_an_error(dbs, tmp_path):
    assert coverage(tmp_path / "nope.sqlite") == {"exists": False}


# ---------------------------------------------------------------------------
# the rebuild window
# ---------------------------------------------------------------------------

def test_a_window_rebuilds_only_the_days_it_names(dbs):
    """``days=1`` touches today and leaves every earlier partition alone.

    This is the property that makes a daily run cheap: the cost must be one
    day of auctions, not the whole history.  The earlier partitions must still
    hold their original values, which is what distinguishes "not rebuilt" from
    "rebuilt to the same numbers".
    """
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    before = con.execute(
        "SELECT day, source_checksum FROM aggregate_partition"
        " WHERE aggregate='daily_item_price' ORDER BY day").fetchall()
    con.close()
    assert len(before) == 3

    report = build(intel, tsm, agg, only=["daily_item_price"], days=1)
    assert report.window["daily_item_price"] == ["2026-10-22"]

    con = sqlite3.connect(agg)
    after = con.execute(
        "SELECT day, source_checksum FROM aggregate_partition"
        " WHERE aggregate='daily_item_price' ORDER BY day").fetchall()
    con.close()
    # Every partition still exists with the same content fingerprint; only the
    # last day was in scope, and it had not changed, so nothing was written.
    assert before == after
    assert report.rows_written["daily_item_price"] == 0
    assert report.partitions_skipped["daily_item_price"] == 1


def test_a_window_does_not_invent_a_partition_for_a_day_with_no_source(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    report = build(intel, tsm, agg, only=["daily_item_price"],
                   since="2030-01-01")
    assert report.rows_written.get("daily_item_price", 0) == 0
    assert any("no source day" in e for e in report.errors)
    con = sqlite3.connect(agg)
    assert con.execute("SELECT COUNT(*) FROM daily_item_price").fetchone()[0] == 0
    con.close()


def test_without_a_window_every_day_is_built(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    report = build(intel, tsm, agg, only=["daily_item_price"])
    assert report.window.get("daily_item_price", []) == []
    con = sqlite3.connect(agg)
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT day FROM daily_item_price ORDER BY day")]
    con.close()
    assert days == ["2026-10-20", "2026-10-21", "2026-10-22"]


def test_a_window_that_contains_an_incomplete_day_still_rebuilds_it(dbs):
    """Yesterday may still be receiving snapshots; a window of 2 must cover it."""
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg, only=["daily_item_price"], days=1)
    # A new observation arrives on the last day, after the first build.
    tc.execute(
        "INSERT INTO tsm_snapshot (snapshot_id, scope, kind, updated_at, rows,"
        " changed) VALUES (5, 'realm-a', 'realm_items',"
        " '2026-10-22T22:00:00Z', 100, 1)")
    tc.execute(
        "INSERT INTO tsm_price_change (snapshot_id, scope, kind, key_id,"
        " market_value, min_buyout, avg_sale_price, sale_rate, sold_per_day)"
        " VALUES (5, 'realm-a', 'realm_items', 1, 999, 900, 950, 0.9, 4.0)")
    tc.commit()

    report = build(intel, tsm, agg, only=["daily_item_price"], days=1)
    assert report.rows_written["daily_item_price"] == 1
    con = sqlite3.connect(agg)
    last = con.execute(
        "SELECT last_market_value, n_observations FROM daily_item_price"
        " WHERE day='2026-10-22' AND scope='realm-a' AND item_id=1").fetchone()
    con.close()
    assert last == (999, 2)


# ---------------------------------------------------------------------------
# the commodities market
# ---------------------------------------------------------------------------

def test_a_snapshot_without_a_realm_is_the_commodities_market(dbs):
    """The region-wide commodities snapshot has realm='' and a slug.

    Keying it on the blank realm would file it under a name that reads as
    "unknown" and that no consumer can match against a real market.  It is a
    market in its own right and must be named as one.
    """
    tsm, intel, agg, _tc, ic = dbs
    ic.execute(
        "INSERT INTO ah_snapshot (snapshot_id, realm, realm_slug, captured_at)"
        " VALUES ('c1', '', 'commodities', ?)", (_epoch("2026-10-20T09:00:00Z"),))
    ic.execute(
        "INSERT INTO ah_auction (snapshot_id, auction_key, item_id, quantity,"
        " unit_price_copper, total_price_copper, is_commodity)"
        " VALUES ('c1','k1',124124,1,2500,2500,1)")
    ic.commit()
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    rows = con.execute(
        "SELECT realm, n_auctions, n_commodity FROM daily_ah_auction"
        " WHERE day='2026-10-20'").fetchall()
    con.close()
    assert rows == [("commodities", 1, 1)]


def test_two_snapshots_of_one_market_are_one_group(dbs):
    """A market's rows must not be split by another market sorting between them."""
    tsm, intel, agg, _tc, ic = dbs
    ic.executemany(
        "INSERT INTO ah_snapshot (snapshot_id, realm, realm_slug, captured_at)"
        " VALUES (?,?,?,?)",
        [("s1", "alpha", "alpha", _epoch("2026-10-20T08:00:00Z")),
         ("s2", "beta", "beta", _epoch("2026-10-20T09:00:00Z")),
         ("s3", "alpha", "", _epoch("2026-10-20T10:00:00Z"))],
    )
    ic.executemany(
        "INSERT INTO ah_auction (snapshot_id, auction_key, item_id, quantity,"
        " unit_price_copper, total_price_copper, is_commodity)"
        " VALUES (?,?,?,?,?,?,0)",
        [("s1", "k1", 10, 1, 100, 100),
         ("s2", "k1", 10, 1, 200, 200),
         ("s3", "k1", 10, 1, 300, 300)],
    )
    ic.commit()
    build(intel, tsm, agg)

    con = sqlite3.connect(agg)
    rows = con.execute(
        "SELECT realm, n_auctions, sum_unit_price FROM daily_ah_auction"
        " WHERE day='2026-10-20' AND item_id=10 ORDER BY realm").fetchall()
    con.close()
    # alpha's two snapshots (one with a slug, one without) are one group.
    assert rows == [("alpha", 2, 400), ("beta", 1, 200)]



# ---------------------------------------------------------------------------
# a partition is replaced, not merged
# ---------------------------------------------------------------------------
#
# These are the two ways an aggregate can end up claiming rows the raw data no
# longer has.  Both were live: a full rebuild left six rows behind that had no
# GROUP BY counterpart, which is exactly the kind of quiet disagreement the
# aggregates exist to prevent.

def test_a_row_that_leaves_the_source_leaves_the_aggregate(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg, only=["daily_item_price"])
    con = sqlite3.connect(agg)
    before = con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-22'"
    ).fetchone()[0]
    con.close()
    assert before == 1

    # The item's only observation for that day moves to a different item id:
    # the original key no longer exists in the source at all.
    tc.execute("UPDATE tsm_price_change SET key_id = 99 WHERE snapshot_id = 4")
    tc.commit()
    build(intel, tsm, agg, only=["daily_item_price"], days=1)

    con = sqlite3.connect(agg)
    rows = con.execute(
        "SELECT item_id FROM daily_item_price WHERE day='2026-10-22'"
        " ORDER BY item_id").fetchall()
    con.close()
    assert rows == [(99,)]


def test_a_partition_that_disappears_from_the_source_is_pruned(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg, only=["daily_item_price"])
    con = sqlite3.connect(agg)
    assert con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-21'"
    ).fetchone()[0] == 1
    con.close()

    # The whole day's snapshot is gone: no source rows for the 21st at all.
    tc.execute("DELETE FROM tsm_price_change WHERE snapshot_id = 3")
    tc.execute("DELETE FROM tsm_snapshot WHERE snapshot_id = 3")
    tc.commit()

    report = build(intel, tsm, agg, only=["daily_item_price"])
    con = sqlite3.connect(agg)
    left = con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-21'"
    ).fetchone()[0]
    tracked = con.execute(
        "SELECT COUNT(*) FROM aggregate_partition"
        " WHERE aggregate='daily_item_price' AND day='2026-10-21'"
    ).fetchone()[0]
    con.close()
    assert left == 0
    assert tracked == 0
    assert report.partitions_pruned["daily_item_price"] == 1


def test_a_windowed_build_does_not_prune_a_day_outside_its_window(dbs):
    """Pruning is authority: it may only be exercised over the days it rebuilt."""
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg, only=["daily_item_price"])

    # Remove the 21st from the source, then rebuild only the 22nd.
    tc.execute("DELETE FROM tsm_price_change WHERE snapshot_id = 3")
    tc.execute("DELETE FROM tsm_snapshot WHERE snapshot_id = 3")
    tc.commit()
    report = build(intel, tsm, agg, only=["daily_item_price"], days=1)

    con = sqlite3.connect(agg)
    left = con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-21'"
    ).fetchone()[0]
    con.close()
    # The 21st is stale now, but a build that only looked at the 22nd must not
    # touch it -- deleting data outside the window it was asked about would
    # make a cheap daily run quietly destructive.
    assert left == 1
    assert report.partitions_pruned.get("daily_item_price", 0) == 0


# ---------------------------------------------------------------------------
# a partition is replaced, not merged
# ---------------------------------------------------------------------------
#
# These are the two ways an aggregate can end up claiming rows the raw data no
# longer has.  Both were live: a full rebuild left six rows behind that had no
# GROUP BY counterpart, which is exactly the kind of quiet disagreement the
# aggregates exist to prevent.

def test_a_row_that_leaves_the_source_leaves_the_aggregate(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg, only=["daily_item_price"])
    con = sqlite3.connect(agg)
    before = con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-22'"
    ).fetchone()[0]
    con.close()
    assert before == 1

    # The item's only observation for that day moves to a different item id:
    # the original key no longer exists in the source at all.
    tc.execute("UPDATE tsm_price_change SET key_id = 99 WHERE snapshot_id = 4")
    tc.commit()
    build(intel, tsm, agg, only=["daily_item_price"], days=1)

    con = sqlite3.connect(agg)
    rows = con.execute(
        "SELECT item_id FROM daily_item_price WHERE day='2026-10-22'"
        " ORDER BY item_id").fetchall()
    con.close()
    assert rows == [(99,)]


def test_a_partition_that_disappears_from_the_source_is_pruned(dbs):
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg, only=["daily_item_price"])
    con = sqlite3.connect(agg)
    assert con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-21'"
    ).fetchone()[0] == 1
    con.close()

    # The whole day's snapshot is gone: no source rows for the 21st at all.
    tc.execute("DELETE FROM tsm_price_change WHERE snapshot_id = 3")
    tc.execute("DELETE FROM tsm_snapshot WHERE snapshot_id = 3")
    tc.commit()

    report = build(intel, tsm, agg, only=["daily_item_price"])
    con = sqlite3.connect(agg)
    left = con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-21'"
    ).fetchone()[0]
    tracked = con.execute(
        "SELECT COUNT(*) FROM aggregate_partition"
        " WHERE aggregate='daily_item_price' AND day='2026-10-21'"
    ).fetchone()[0]
    con.close()
    assert left == 0
    assert tracked == 0
    assert report.partitions_pruned["daily_item_price"] == 1


def test_a_windowed_build_does_not_prune_a_day_outside_its_window(dbs):
    """Pruning is authority: it may only be exercised over the days it rebuilt."""
    tsm, intel, agg, tc, _ic = dbs
    _seed(tc)
    build(intel, tsm, agg, only=["daily_item_price"])

    # Remove the 21st from the source, then rebuild only the 22nd.
    tc.execute("DELETE FROM tsm_price_change WHERE snapshot_id = 3")
    tc.execute("DELETE FROM tsm_snapshot WHERE snapshot_id = 3")
    tc.commit()
    report = build(intel, tsm, agg, only=["daily_item_price"], days=1)

    con = sqlite3.connect(agg)
    left = con.execute(
        "SELECT COUNT(*) FROM daily_item_price WHERE day='2026-10-21'"
    ).fetchone()[0]
    con.close()
    # The 21st is stale now, but a build that only looked at the 22nd must not
    # touch it -- deleting data outside the window it was asked about would
    # make a cheap daily run quietly destructive.
    assert left == 1
    assert report.partitions_pruned.get("daily_item_price", 0) == 0
