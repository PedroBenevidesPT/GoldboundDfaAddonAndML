"""The comparative dry run: coverage classification, costs, and space.

The rehearsal exists so that a deletion can be judged rather than merely
counted.  These tests pin the judgements it makes: which tables keep their
statistics, which lose them, and what it says about disk.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from woweconomy.aggregates import build
from woweconomy.rehearsal import (
    COVERED,
    DERIVED,
    UNCOVERED,
    _net_note,
    _signed,
    compare_space,
    rehearse,
    space_payload,
    space_report,
)

DAY = 86400
NOW = 1_797_408_000


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _schema(tmp_path: Path):
    intel = tmp_path / "intel.sqlite"
    tsm = tmp_path / "tsm_markets.db"
    con = sqlite3.connect(intel)
    con.executescript("""
        CREATE TABLE ah_snapshot (snapshot_id TEXT PRIMARY KEY, realm TEXT,
                                  realm_slug TEXT, captured_at INTEGER);
        CREATE TABLE ah_auction (snapshot_id TEXT, auction_key TEXT,
                                 item_id INTEGER, quantity INTEGER,
                                 unit_price_copper INTEGER,
                                 total_price_copper INTEGER,
                                 is_commodity INTEGER);
        CREATE TABLE inventory_snapshot (snapshot_id TEXT PRIMARY KEY,
                                         captured_at INTEGER, character TEXT);
        CREATE TABLE inventory_row (snapshot_id TEXT, item_id INTEGER,
                                    quantity INTEGER);
        CREATE TABLE inventory_posted_listing (snapshot_id TEXT,
                                               auction_id INTEGER,
                                               time_left INTEGER);
        CREATE TABLE reconciliation (snapshot_id TEXT, item_id INTEGER,
                                     status TEXT);
        CREATE TABLE seller_observation (obs_id TEXT PRIMARY KEY,
                                         observed_at INTEGER, seller TEXT);
        CREATE TABLE undercut_observation (observation_id TEXT PRIMARY KEY,
                                           observed_at INTEGER,
                                           item_id INTEGER);
        CREATE TABLE mail_invoice (mail_id TEXT PRIMARY KEY, seen_at INTEGER,
                                   days_left REAL, invoice_type TEXT);
        CREATE TABLE sale_buyer_link (sale_event_id TEXT PRIMARY KEY,
                                      mail_id TEXT, linked_at TEXT);
    """)
    con.commit()
    con.close()

    con = sqlite3.connect(tsm)
    con.executescript("""
        CREATE TABLE tsm_snapshot (snapshot_id INTEGER PRIMARY KEY, scope TEXT,
                                   kind TEXT, updated_at TEXT, rows INTEGER,
                                   changed INTEGER);
        CREATE TABLE tsm_price_change (snapshot_id INTEGER, scope TEXT,
                                       kind TEXT, key_id INTEGER,
                                       market_value INTEGER,
                                       min_buyout INTEGER,
                                       avg_sale_price INTEGER,
                                       sale_rate REAL, sold_per_day REAL);
        CREATE TABLE tsm_realm_price (scope TEXT, item_id INTEGER, name TEXT,
                                      market_value INTEGER, min_buyout INTEGER,
                                      recent INTEGER, historical INTEGER,
                                      updated_at TEXT, snapshot_id INTEGER);
        CREATE TABLE snapshots (snapshotId INTEGER PRIMARY KEY,
                                snapshotTimestamp TEXT);
        CREATE TABLE market_snapshots (snapshotId INTEGER, itemId INTEGER,
                                       marketValue REAL);
        CREATE TABLE auction_events (event_id TEXT PRIMARY KEY, ts TEXT,
                                     timestamp_unix INTEGER, event_type TEXT,
                                     qty INTEGER, total_price_copper INTEGER,
                                     net_proceeds_copper INTEGER, realm TEXT);
        CREATE TABLE auction_intel_events (event_id TEXT PRIMARY KEY, ts INTEGER,
                                           event_type TEXT, quantity INTEGER,
                                           total_price_copper INTEGER,
                                           net_proceeds_copper INTEGER,
                                           realm TEXT);
    """)
    con.commit()
    con.close()
    return intel, tsm


def _seed(intel: Path, tsm: Path, age_days: int = 90) -> None:
    stamp = NOW - age_days * DAY
    con = sqlite3.connect(intel)
    for i in range(2):  # two scans so the older one can expire
        sid = f"s{i}"
        con.execute("INSERT INTO ah_snapshot VALUES (?,?,?,?)",
                    (sid, "realm-a", "realm-a", stamp + i * 3600))
        con.execute("INSERT INTO ah_auction VALUES (?,?,?,?,?,?,?)",
                    (sid, f"{sid}-0", 1000, 2, 500, 1000, 0))
    con.commit()
    con.close()

    con = sqlite3.connect(tsm)
    for i in range(2):
        sid = i + 1
        ts = _iso(stamp + i * 3600)
        con.execute("INSERT INTO tsm_snapshot VALUES (?,?,?,?,?,?)",
                    (sid, "realm-a", "realm_items", ts, 10, 5))
        con.execute(
            "INSERT INTO tsm_realm_price VALUES (?,?,?,?,?,?,?,?,?)",
            ("realm-a", 1000, "Widget", 100 + i, 90 + i, 100, 110, ts, sid))
        con.execute(
            "INSERT INTO tsm_price_change VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "realm-a", "realm_items", 1000, 100 + i, 90 + i, 105,
             0.5, 2.0))
        con.execute(
            "INSERT INTO auction_events VALUES (?,?,?,?,?,?,?,?)",
            (f"e{sid}", ts, stamp + i * 3600, "sale", 1, 1000, 950, "realm-a"))
    con.commit()
    con.close()


@pytest.fixture
def store(tmp_path):
    intel, tsm = _schema(tmp_path)
    _seed(intel, tsm)
    agg = tmp_path / "aggregates.sqlite"
    build(intel, tsm, agg)
    return intel, tsm, agg


# ---------------------------------------------------------------------------
# space
# ---------------------------------------------------------------------------

def test_space_report_measures_pages_and_free_space(tmp_path):
    path = tmp_path / "x.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (a INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5000)])
    con.commit()
    con.close()

    report = space_report(path)
    assert report.file_bytes > 0
    assert report.page_size > 0
    assert report.page_count > 0
    assert report.live_bytes <= report.file_bytes
    assert report.vacuum_would_reclaim >= 0


def test_space_report_on_a_missing_file_is_zeroed(tmp_path):
    report = space_report(tmp_path / "nope.sqlite")
    assert report.file_bytes == 0
    assert report.page_count == 0


# ---------------------------------------------------------------------------
# coverage classification
# ---------------------------------------------------------------------------

def test_a_table_with_an_aggregate_is_marked_covered(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    by_name = {t.table: t for t in report.tables}
    assert by_name["tsm_price_change"].coverage == COVERED
    assert by_name["tsm_price_change"].aggregate == "daily_item_price"
    assert by_name["ah_auction"].coverage == COVERED
    assert by_name["ah_auction"].aggregate == "daily_ah_auction"


def test_a_header_table_is_marked_derived(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    by_name = {t.table: t for t in report.tables}
    assert by_name["tsm_snapshot"].coverage == DERIVED
    assert by_name["ah_snapshot"].coverage == DERIVED


def test_a_table_with_no_aggregate_is_marked_uncovered(store):
    """Nothing summarises an invoice, so deleting one loses it."""
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    by_name = {t.table: t for t in report.tables}
    assert by_name["mail_invoice"].coverage == UNCOVERED
    assert by_name["seller_observation"].coverage == UNCOVERED


def test_unreproducible_lists_only_tables_that_actually_lose_something(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    names = {t.table for t in report.unreproducible}
    # these have rows eligible for deletion and no aggregate
    assert "seller_observation" not in names  # empty, so nothing is lost
    # and nothing covered is ever reported as a loss
    for t in report.unreproducible:
        assert t.coverage == UNCOVERED
        assert t.eligible > 0


def test_a_covered_table_is_reproducible_when_the_aggregate_spans_its_days(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    by_name = {t.table: t for t in report.tables}
    assert by_name["tsm_price_change"].expired_range
    assert by_name["tsm_price_change"].reproducible is True


def test_the_current_state_table_is_never_considered_for_deletion(store):
    """tsm_realm_price is one upserted row per item, not history.

    It must not appear in the rehearsal at all: appearing would mean the plan
    had built a delete for it, and its updated_at is a write time that would
    then have been used as an expiry boundary.
    """
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    assert "tsm_realm_price" not in {t.table for t in report.tables}
    assert "tsm_realm_price" not in report.to_dict()["unreproducible"]


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------

def test_the_archive_estimate_is_measured_not_guessed(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW,
                      archive_dir="D:/archive")
    by_name = {t.table: t for t in report.tables}
    ah = by_name["ah_auction"]
    assert ah.eligible > 0
    assert ah.archive_rows == ah.eligible
    assert ah.archive_bytes_est > 0
    # JSONL of integer columns compresses, but cannot compress to nothing
    assert ah.archive_bytes_est < ah.bytes_est


def test_the_net_change_counts_the_aggregate_as_a_cost(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    assert report.aggregate_bytes > 0
    assert report.net_bytes == (report.raw_bytes
                                - report.aggregate_bytes
                                - report.archive_bytes_est)


def test_the_summary_names_the_tables_that_lose_their_history(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    text = report.summary()
    assert "comparative dry run" in text
    assert "net disk change" in text


def test_the_net_line_prints_the_disk_delta_and_says_which_way(store):
    """A reclaim must not print as a positive "change" under a change label.

    ``net_bytes`` is a reclaim; the line reports the disk *delta*, which is
    its negation, and then spells the direction out.  The two must never
    disagree, because "net disk change: 2.0 GB" on a run that grew the
    database is the exact misread this line exists to prevent.
    """
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW)
    line = next(l for l in report.summary().splitlines()
                if "net disk change" in l)
    body = line.split(":", 1)[1].strip()
    if report.net_bytes > 0:            # reclaimed space: disk went down
        assert body.startswith("-"), line
        assert "shrinks" in body, line
    elif report.net_bytes < 0:          # the aggregate cost more than it freed
        assert body.startswith("+"), line
        assert "grows" in body, line
    else:
        assert "no change" in body, line


def test_a_zero_delta_has_no_sign():
    """Zero is not a small reclaim.  The old form printed ``-0.00B``."""
    assert not _signed(0).startswith(("-", "+"))
    assert _net_note(0) == "  (no change)"


def test_a_window_that_deletes_nothing_says_so(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=10_000, now=NOW)
    assert report.total_rows == 0
    assert "nothing is older than the cutoff" in report.summary()


def test_validation_state_is_carried_into_the_report(store):
    intel, tsm, agg = store
    report = rehearse(intel, tsm, agg, days=30, now=NOW,
                      validation_ok=False,
                      validation_note="validation FAILED")
    assert report.validation_ok is False
    assert "validation FAILED" in report.summary()
    assert report.to_dict()["validation_ok"] is False


# ---------------------------------------------------------------------------
# measuring the reclaim rather than assuming it
# ---------------------------------------------------------------------------

def _rows_db(path: Path, n: int = 600) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    con.executemany("INSERT INTO t VALUES (?,?)",
                    [(i, "x" * 200) for i in range(n)])
    con.commit()
    con.close()
    return path


def test_space_payload_records_every_database_by_name(tmp_path):
    a = _rows_db(tmp_path / "a.sqlite")
    b = _rows_db(tmp_path / "b.sqlite")
    payload = space_payload(a, b)
    assert set(payload["databases"]) == {"a.sqlite", "b.sqlite"}
    assert payload["measured_at"]
    assert payload["databases"]["a.sqlite"]["file_bytes"] > 0


def test_two_identical_positions_report_nothing_freed(tmp_path):
    a = _rows_db(tmp_path / "a.sqlite")
    before = space_payload(a)
    after = space_payload(a)
    text = compare_space(before, after)
    assert "nothing was freed" in text


def test_a_deletion_moves_pages_to_the_freelist_without_shrinking_the_file(
        tmp_path):
    a = _rows_db(tmp_path / "a.sqlite")
    before = space_payload(a)

    con = sqlite3.connect(a)
    con.execute("DELETE FROM t WHERE a < 500")
    con.commit()
    con.close()

    after = space_payload(a)
    text = compare_space(before, after)
    assert "was freed but the files did not shrink" in text
    assert "VACUUM" in text
    assert "still on the freelist" in text
    # the file itself is unchanged, which is the whole point of auto_vacuum=OFF
    assert (after["databases"]["a.sqlite"]["file_bytes"]
            == before["databases"]["a.sqlite"]["file_bytes"])


def test_after_a_vacuum_the_filesystem_reclaim_is_reported(tmp_path):
    a = _rows_db(tmp_path / "a.sqlite")
    before = space_payload(a)

    con = sqlite3.connect(a)
    con.execute("DELETE FROM t WHERE a < 500")
    con.commit()
    con.execute("VACUUM")
    con.close()

    after = space_payload(a)
    text = compare_space(before, after)
    assert "filesystem received" in text
    assert after["databases"]["a.sqlite"]["file_bytes"] < \
        before["databases"]["a.sqlite"]["file_bytes"]


def test_growth_is_shown_with_a_sign_rather_than_as_a_reclaim(tmp_path):
    a = _rows_db(tmp_path / "a.sqlite", n=10)
    before = space_payload(a)
    con = sqlite3.connect(a)
    con.executemany("INSERT INTO t VALUES (?,?)",
                    [(i, "y" * 400) for i in range(400)])
    con.commit()
    con.close()
    text = compare_space(before, space_payload(a))
    assert "+" in text
    assert "the filesystem received" not in text


def test_a_baseline_for_a_missing_file_is_read_as_zero(tmp_path):
    a = _rows_db(tmp_path / "a.sqlite")
    text = compare_space({"measured_at": "then", "databases": {}},
                         space_payload(a))
    assert "a.sqlite" in text
    assert "grew by" in text
    assert "the filesystem received" not in text
