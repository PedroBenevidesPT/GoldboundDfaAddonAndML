"""Is our sale visible in the Blizzard snapshot?  What the check may claim.

The live data cannot answer this: the recorded sales end on 2026-10-21 and the
snapshots begin the same day, so no sale falls inside the snapshot period.
That makes the *coverage* reporting as important to test as the matching -- a
run with no overlap must say so, not print a zero that reads like a finding.

The stores below are real SQLite files with the columns the check reads, so the
SQL is exercised rather than mocked.
"""
from __future__ import annotations

import sqlite3

import pytest

from woweconomy.analytics.sale_visibility import (
    DEFAULT_LOOKBACK_S,
    by_realm,
    check_sale_visibility,
)

SCHEMA = """
CREATE TABLE auction_events (
    event_id          TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    item_id           INTEGER,
    realm             TEXT NOT NULL,
    timestamp_unix    INTEGER NOT NULL,
    unit_price_copper INTEGER NOT NULL
);
CREATE TABLE ah_snapshot (
    snapshot_id     TEXT PRIMARY KEY,
    region          TEXT NOT NULL DEFAULT 'eu',
    realm           TEXT NOT NULL,
    realm_slug      TEXT NOT NULL DEFAULT '',
    captured_at     INTEGER NOT NULL
);
CREATE TABLE ah_auction (
    snapshot_id        TEXT NOT NULL,
    auction_key        TEXT NOT NULL,
    item_id            INTEGER NOT NULL,
    quantity           INTEGER NOT NULL DEFAULT 1,
    unit_price_copper  INTEGER,
    price_status       TEXT NOT NULL DEFAULT 'ok',
    PRIMARY KEY (snapshot_id, auction_key)
);
"""

DAY = 86_400
SALE_AT = 1_700_000_000


def _store(tmp_path):
    path = tmp_path / "store.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path


def _sale(path, event_id="s1", item_id=100, realm="Dragonmaw",
          at=SALE_AT, price=50_000):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO auction_events (event_id, event_type, item_id, realm,"
        " timestamp_unix, unit_price_copper) VALUES (?,?,?,?,?,?)",
        (event_id, "sale", item_id, realm, at, price))
    con.commit()
    con.close()


def _snapshot(path, snapshot_id="snap1", realm="Dragonmaw", at=SALE_AT - 3600,
              listings=()):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO ah_snapshot (snapshot_id, realm, captured_at)"
        " VALUES (?,?,?)", (snapshot_id, realm, at))
    for index, (item_id, price) in enumerate(listings):
        con.execute(
            "INSERT INTO ah_auction (snapshot_id, auction_key, item_id,"
            " unit_price_copper) VALUES (?,?,?,?)",
            (snapshot_id, f"a{index}", item_id, price))
    con.commit()
    con.close()


# ----------------------------------------------------------------------
# no overlap: the report must not answer a question it cannot
# ----------------------------------------------------------------------


def test_sales_before_every_snapshot_are_reported_as_unmeasurable(tmp_path):
    path = _store(tmp_path)
    _sale(path, at=SALE_AT)
    _snapshot(path, at=SALE_AT + DAY, listings=[(100, 50_000)])

    report = check_sale_visibility(path)
    assert report.sales_total == 1
    assert report.sales_covered == 0
    assert report.overlap is False
    assert report.matched == [] and report.unmatched == []
    assert report.before_snapshots == 1
    assert any("NO OVERLAP" in line for line in report.summary().splitlines())
    # the distinction the whole module exists for
    assert "not the same as" in report.summary()


def test_a_store_with_no_sales_says_so(tmp_path):
    path = _store(tmp_path)
    report = check_sale_visibility(path)
    assert report.sales_total == 0
    assert report.overlap is False
    assert any("no sales" in note for note in report.notes)


def test_a_missing_store_is_reported_not_raised(tmp_path):
    report = check_sale_visibility(tmp_path / "absent.db")
    assert report.sales_total == 0
    assert any("no store" in note for note in report.notes)


# ----------------------------------------------------------------------
# matching
# ----------------------------------------------------------------------


def test_a_listing_at_our_price_before_the_sale_is_a_match(tmp_path):
    path = _store(tmp_path)
    _sale(path, price=50_000)
    _snapshot(path, at=SALE_AT - 3600, listings=[(100, 50_000)])

    report = check_sale_visibility(path)
    assert report.sales_covered == 1
    assert len(report.matched) == 1
    match = report.matched[0]
    assert match.visible is True
    assert match.same_price == 1
    assert match.consistent_with_ours is True
    assert match.wall_low_copper == 50_000
    assert len(report.same_price_matches()) == 1


def test_a_listing_at_another_price_is_seen_but_not_ours(tmp_path):
    """Another seller's listing is not evidence that ours was visible."""
    path = _store(tmp_path)
    _sale(path, price=50_000)
    _snapshot(path, at=SALE_AT - 3600, listings=[(100, 61_000), (100, 70_000)])

    report = check_sale_visibility(path)
    match = report.matched[0]
    assert match.listings == 2
    assert match.same_price == 0
    assert match.consistent_with_ours is False
    assert match.wall_low_copper == 61_000
    assert match.wall_high_copper == 70_000


def test_no_listing_of_the_item_is_unmatched(tmp_path):
    path = _store(tmp_path)
    _sale(path, item_id=100)
    _snapshot(path, at=SALE_AT - 3600, listings=[(999, 50_000)])

    report = check_sale_visibility(path)
    assert report.sales_covered == 1
    assert report.matched == []
    assert len(report.unmatched) == 1
    assert report.unmatched[0].visible is False


def test_a_listing_on_another_realm_does_not_count(tmp_path):
    """The auction house is per realm; a Dragonmaw sale is not seen on Kazzak."""
    path = _store(tmp_path)
    _sale(path, realm="Dragonmaw")
    _snapshot(path, realm="Kazzak", at=SALE_AT - 3600,
              listings=[(100, 50_000)])

    report = check_sale_visibility(path)
    assert report.matched == []
    assert len(report.unmatched) == 1


def test_a_listing_outside_the_lookback_does_not_count(tmp_path):
    """A posting from a week earlier is a different posting, not this sale."""
    path = _store(tmp_path)
    _sale(path)
    _snapshot(path, at=SALE_AT - 7 * DAY, listings=[(100, 50_000)])

    report = check_sale_visibility(path)
    assert report.matched == []


def test_a_listing_after_the_sale_does_not_count(tmp_path):
    path = _store(tmp_path)
    _sale(path)
    _snapshot(path, at=SALE_AT + 60, listings=[(100, 50_000)])

    report = check_sale_visibility(path)
    assert report.matched == []


def test_the_lookback_is_configurable(tmp_path):
    path = _store(tmp_path)
    _sale(path)
    _snapshot(path, at=SALE_AT - 7 * DAY, listings=[(100, 50_000)])

    assert check_sale_visibility(path).matched == []
    wide = check_sale_visibility(path, lookback_s=8 * DAY)
    assert len(wide.matched) == 1


def test_non_sale_events_are_ignored(tmp_path):
    """Only a completed sale has a moment at which it could have been seen."""
    path = _store(tmp_path)
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO auction_events (event_id, event_type, item_id, realm,"
        " timestamp_unix, unit_price_copper) VALUES (?,?,?,?,?,?)",
        ("e1", "listed", 100, "Dragonmaw", SALE_AT, 50_000))
    con.commit()
    con.close()
    _snapshot(path, at=SALE_AT - 3600, listings=[(100, 50_000)])

    report = check_sale_visibility(path)
    assert report.sales_total == 0


# ----------------------------------------------------------------------
# filters and the per-realm view
# ----------------------------------------------------------------------


def test_a_realm_filter_narrows_the_check(tmp_path):
    path = _store(tmp_path)
    _sale(path, event_id="s1", realm="Dragonmaw")
    _sale(path, event_id="s2", realm="Kazzak")
    _snapshot(path, snapshot_id="n1", realm="Dragonmaw",
              at=SALE_AT - 3600, listings=[(100, 50_000)])
    _snapshot(path, snapshot_id="n2", realm="Kazzak",
              at=SALE_AT - 3600, listings=[(100, 50_000)])

    report = check_sale_visibility(path, realms=["Dragonmaw"])
    assert report.sales_total == 1
    assert report.matched[0].realm == "Dragonmaw"


def test_the_limit_checks_only_the_most_recent_sales(tmp_path):
    path = _store(tmp_path)
    _sale(path, event_id="old", at=SALE_AT - 2 * DAY)
    _sale(path, event_id="new", at=SALE_AT)
    _snapshot(path, at=SALE_AT - 3600, listings=[(100, 50_000)])

    report = check_sale_visibility(path, limit=1)
    assert report.sales_total == 1
    assert report.matched[0].event_id == "new"


def test_by_realm_splits_the_counts(tmp_path):
    path = _store(tmp_path)
    _sale(path, event_id="s1", realm="Dragonmaw", at=SALE_AT)
    _sale(path, event_id="s2", realm="Dragonmaw", item_id=200, at=SALE_AT)
    _sale(path, event_id="s3", realm="Kazzak", at=SALE_AT)
    _snapshot(path, snapshot_id="n1", realm="Dragonmaw",
              at=SALE_AT - 3600, listings=[(100, 50_000)])
    _snapshot(path, snapshot_id="n2", realm="Kazzak",
              at=SALE_AT - 3600, listings=[(999, 1)])

    rows = by_realm(check_sale_visibility(path))
    assert rows["Dragonmaw"]["covered"] == 2
    assert rows["Dragonmaw"]["seen"] == 1
    assert rows["Dragonmaw"]["same_price"] == 1
    assert rows["Kazzak"]["covered"] == 1
    assert rows["Kazzak"]["seen"] == 0


def test_the_reported_span_describes_only_the_sales_it_checks(tmp_path):
    """A span that includes rows the check skips is a report that lies.

    The live store had a sale with no usable price at 08:53 and its last
    checkable sale at 07:12; reporting the first as "the sales span" made the
    report claim a period it had then refused to examine.
    """
    path = _store(tmp_path)
    _sale(path, event_id="usable", at=SALE_AT, price=50_000)
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO auction_events (event_id, event_type, item_id, realm,"
        " timestamp_unix, unit_price_copper) VALUES (?,?,?,?,?,?)",
        ("priceless", "sale", 100, "Dragonmaw", SALE_AT + 2 * DAY, 0))
    con.commit()
    con.close()
    _snapshot(path, at=SALE_AT - 3600, listings=[(100, 50_000)])

    report = check_sale_visibility(path)
    assert report.sales_total == 1
    assert report.last_sale_at == SALE_AT


def test_the_default_lookback_is_a_day():
    assert DEFAULT_LOOKBACK_S == 86_400
