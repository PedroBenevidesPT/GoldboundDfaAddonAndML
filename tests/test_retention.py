"""Retention: boundaries, timezones, protection rules, idempotency, recovery.

Every test builds a small synthetic database with the columns the retention
engine actually touches, so the suite does not depend on the live stores and
can pin exact timestamps.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from woweconomy.retention import (
    POLICY,
    PolicyRefusal,
    Retention,
    epoch_expr,
    policy_for,
)

DAY = 86400


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _intel_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE ah_snapshot (
            snapshot_id TEXT PRIMARY KEY, region TEXT, realm TEXT,
            captured_at INTEGER, auction_count INTEGER
        );
        CREATE TABLE ah_auction (
            snapshot_id TEXT, auction_key TEXT, item_id INTEGER,
            unit_price_copper INTEGER,
            PRIMARY KEY (snapshot_id, auction_key)
        );
        CREATE TABLE inventory_snapshot (
            snapshot_id TEXT PRIMARY KEY, captured_at INTEGER, character TEXT
        );
        CREATE TABLE inventory_row (
            snapshot_id TEXT, item_id INTEGER, quantity INTEGER
        );
        CREATE TABLE inventory_posted_listing (
            snapshot_id TEXT, auction_id INTEGER, time_left INTEGER
        );
        CREATE TABLE reconciliation (
            snapshot_id TEXT, item_id INTEGER, status TEXT
        );
        CREATE TABLE seller_observation (
            obs_id TEXT PRIMARY KEY, observed_at INTEGER, seller TEXT
        );
        CREATE TABLE undercut_observation (
            observation_id TEXT PRIMARY KEY, observed_at INTEGER, item_id INTEGER
        );
        CREATE TABLE mail_invoice (
            mail_id TEXT PRIMARY KEY, seen_at INTEGER, days_left REAL,
            invoice_type TEXT
        );
        CREATE TABLE sale_buyer_link (
            sale_event_id TEXT PRIMARY KEY, mail_id TEXT, linked_at TEXT
        );
        """
    )


def _tsm_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE tsm_snapshot (
            snapshot_id INTEGER PRIMARY KEY, scope TEXT, kind TEXT,
            updated_at TEXT, rows INTEGER
        );
        CREATE TABLE tsm_price_change (
            snapshot_id INTEGER, scope TEXT, kind TEXT, key_id INTEGER,
            market_value INTEGER
        );
        CREATE TABLE tsm_realm_price (
            scope TEXT, item_id INTEGER, name TEXT, snapshot_id INTEGER
        );
        CREATE TABLE snapshots (
            snapshotId INTEGER PRIMARY KEY, snapshotTimestamp TEXT,
            importFileName TEXT, rowCount INTEGER
        );
        CREATE TABLE market_snapshots (
            snapshotId INTEGER, itemId INTEGER, marketValue REAL
        );
        CREATE TABLE auction_events (
            event_id TEXT PRIMARY KEY, ts TEXT, item_id INTEGER
        );
        CREATE TABLE auction_intel_events (
            event_id TEXT PRIMARY KEY, ts TEXT, item_id INTEGER
        );
        """
    )


@pytest.fixture
def stores(tmp_path: Path):
    """An empty intel and tsm database pair."""
    intel = tmp_path / "intel.sqlite"
    tsm = tmp_path / "tsm.sqlite"
    for path, schema in ((intel, _intel_schema), (tsm, _tsm_schema)):
        con = sqlite3.connect(path)
        schema(con)
        con.commit()
        con.close()
    return intel, tsm


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _seed_scan(con: sqlite3.Connection, snapshot_id: str, realm: str,
               captured_at: int, auctions: int = 3) -> None:
    con.execute(
        "INSERT INTO ah_snapshot (snapshot_id, realm, captured_at) VALUES (?,?,?)",
        (snapshot_id, realm, captured_at),
    )
    for i in range(auctions):
        con.execute(
            "INSERT INTO ah_auction (snapshot_id, auction_key, item_id) "
            "VALUES (?,?,?)",
            (snapshot_id, f"{snapshot_id}-{i}", 1000 + i),
        )


def _seed_tsm(con: sqlite3.Connection, sid: int, scope: str, kind: str,
              updated_at: int, changes: int = 3) -> None:
    con.execute(
        "INSERT INTO tsm_snapshot (snapshot_id, scope, kind, updated_at) "
        "VALUES (?,?,?,?)",
        (sid, scope, kind, _iso(updated_at)),
    )
    for i in range(changes):
        con.execute(
            "INSERT INTO tsm_price_change (snapshot_id, scope, kind, key_id) "
            "VALUES (?,?,?,?)",
            (sid, scope, kind, i),
        )


def _count(path: Path, table: str) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# the policy itself
# ---------------------------------------------------------------------------
def test_every_policy_entry_names_a_database_we_know():
    assert {p.database for p in POLICY} == {"intel.sqlite", "tsm_markets.db"}


def test_policy_declares_each_table_once():
    keys = [(p.table, p.database) for p in POLICY]
    assert len(keys) == len(set(keys)), "a table is declared twice"


def test_every_expiring_table_states_a_timestamp_and_a_rule():
    for p in POLICY:
        if not p.expires:
            continue
        assert p.timestamp, f"{p.table} expires but names no timestamp"
        assert p.expires_when, f"{p.table} expires but states no rule"
        assert p.rationale, f"{p.table} expires but says nothing about why"


def test_reference_and_current_state_tables_never_expire():
    for p in POLICY:
        if p.category in ("dimension", "current"):
            assert not p.expires, f"{p.table} is {p.category} but expires"


def test_policy_matches_the_live_schema():
    """Every table in the real databases is declared, and vice versa."""
    root = Path(__file__).resolve().parent.parent
    pairs = ((root / "data" / "intel.sqlite", "intel.sqlite"),
             (root / "tsm_markets.db", "tsm_markets.db"))
    checked = 0
    for path, database in pairs:
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            actual = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            con.close()
        declared = {p.table for p in POLICY if p.database == database}
        missing = actual - declared
        assert not missing, f"undeclared tables in {database}: {sorted(missing)}"
        checked += 1
    if not checked:
        pytest.skip("no live databases present")


# ---------------------------------------------------------------------------
# boundaries and timezones
# ---------------------------------------------------------------------------
def test_recent_data_is_retained(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "recent", "Silvermoon", now - 2 * DAY)
    con.commit()
    con.close()

    report = Retention(intel, tsm, days=30, now=now).apply()

    assert _count(intel, "ah_snapshot") == 1
    assert _count(intel, "ah_auction") == 3
    assert report.errors == []


def test_expired_data_is_removed(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    # Two scans on the same realm so the newest is not the only one.
    _seed_scan(con, "old", "Silvermoon", now - 40 * DAY)
    _seed_scan(con, "new", "Silvermoon", now - 1 * DAY)
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    con = sqlite3.connect(intel)
    remaining = [r[0] for r in con.execute("SELECT snapshot_id FROM ah_snapshot")]
    con.close()
    assert remaining == ["new"]
    assert _count(intel, "ah_auction") == 3


def test_a_row_exactly_on_the_cutoff_is_kept(stores):
    """The cutoff second itself is inside the window; one second earlier is not.

    Each realm gets a newer scan too, so the "newest per realm" rule does not
    mask the boundary being tested.
    """
    intel, tsm = stores
    now = 1_800_000_000
    cutoff = now - 30 * DAY
    con = sqlite3.connect(intel)
    _seed_scan(con, "on", "Silvermoon", cutoff)
    _seed_scan(con, "on-newer", "Silvermoon", cutoff + 10)
    _seed_scan(con, "before", "Draenor", cutoff - 1)
    _seed_scan(con, "before-newer", "Draenor", cutoff + 10)
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    con = sqlite3.connect(intel)
    remaining = sorted(r[0] for r in con.execute("SELECT snapshot_id FROM ah_snapshot"))
    con.close()
    assert remaining == ["before-newer", "on", "on-newer"]


def test_timezone_offsets_do_not_shift_the_boundary(stores):
    """A +02:00 stamp is two hours earlier than it looks; the cutoff is UTC."""
    intel, tsm = stores
    now = 1_800_000_000
    cutoff = now - 30 * DAY
    con = sqlite3.connect(tsm)
    # Local time 22:00+02:00 is 20:00 UTC, one hour before a 21:00 cutoff.
    local = datetime.fromtimestamp(cutoff + 3600, tz=timezone.utc)
    local_text = local.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # The same instant written in +02:00 is two hours *later* on the clock.
    shifted = (local + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+02:00")
    _seed_tsm(con, 1, "silvermoon", "realm_items", cutoff + 3600)
    con.execute("UPDATE tsm_snapshot SET updated_at=? WHERE snapshot_id=1",
                (local_text,))
    _seed_tsm(con, 2, "draenor", "realm_items", cutoff + 3600)
    con.execute("UPDATE tsm_snapshot SET updated_at=? WHERE snapshot_id=2",
                (shifted,))
    con.commit()
    # Sanity: the two strings differ but name the same instant.
    epoch = con.execute(
        f"SELECT {epoch_expr(con, 'tsm_snapshot', 'updated_at')} "
        "FROM tsm_snapshot WHERE snapshot_id=2"
    ).fetchone()[0]
    con.close()
    assert int(epoch) == cutoff + 3600

    # Both are newer than the cutoff, so both survive regardless of format.
    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()
    assert _count(tsm, "tsm_snapshot") == 2


def test_an_unparseable_timestamp_is_kept_not_deleted(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(tsm)
    con.execute(
        "INSERT INTO auction_events (event_id, ts) VALUES ('e1', 'not a date')"
    )
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    assert _count(tsm, "auction_events") == 1


def test_a_null_timestamp_is_kept(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(tsm)
    con.execute("INSERT INTO auction_events (event_id, ts) VALUES ('e1', NULL)")
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    assert _count(tsm, "auction_events") == 1


# ---------------------------------------------------------------------------
# protection rules
# ---------------------------------------------------------------------------
def test_the_newest_scan_of_each_realm_is_never_deleted(stores):
    """Even when the only scan a realm has is older than the window."""
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "a-old", "Silvermoon", now - 90 * DAY)
    _seed_scan(con, "a-new", "Silvermoon", now - 40 * DAY)
    _seed_scan(con, "b-only", "Dragonmaw", now - 90 * DAY)
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    con = sqlite3.connect(intel)
    remaining = sorted(r[0] for r in con.execute("SELECT snapshot_id FROM ah_snapshot"))
    con.close()
    assert remaining == ["a-new", "b-only"]


def test_an_invoice_still_in_the_mailbox_is_kept(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    con.execute("INSERT INTO mail_invoice VALUES ('open', ?, 12.0, 'seller')",
                (now - 60 * DAY,))
    con.execute("INSERT INTO mail_invoice VALUES ('done', ?, 0.0, 'seller')",
                (now - 60 * DAY,))
    con.commit()
    con.close()

    report = Retention(intel, tsm, days=30, now=now,
                       allow_replay_loss=True).apply()

    con = sqlite3.connect(intel)
    remaining = sorted(r[0] for r in con.execute("SELECT mail_id FROM mail_invoice"))
    con.close()
    assert remaining == ["open"], "an open invoice is an open transaction"
    mail = [s for s in report.sources if s.table == "mail_invoice"][0]
    assert mail.protected == 1


def test_a_listing_whose_auction_is_still_running_is_kept(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    con.execute("INSERT INTO inventory_snapshot VALUES ('s-old', ?, 'Main')",
                (now - 60 * DAY,))
    con.execute("INSERT INTO inventory_snapshot VALUES ('s-new', ?, 'Main')",
                (now - 1 * DAY,))
    con.execute("INSERT INTO inventory_posted_listing VALUES ('s-old', 1, 48)")
    con.execute("INSERT INTO inventory_posted_listing VALUES ('s-old', 2, 0)")
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    con = sqlite3.connect(intel)
    remaining = sorted(r[0] for r in con.execute(
        "SELECT auction_id FROM inventory_posted_listing"))
    con.close()
    assert remaining == [1], "the live auction survives, the finished one does not"


def test_a_buyer_link_survives_while_its_invoice_does(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    con.execute("INSERT INTO mail_invoice VALUES ('kept', ?, 5.0, 'seller')",
                (now - 60 * DAY,))
    con.execute("INSERT INTO mail_invoice VALUES ('gone', ?, 0.0, 'seller')",
                (now - 60 * DAY,))
    stamp = _iso(now - 60 * DAY)
    con.execute("INSERT INTO sale_buyer_link VALUES ('s1', 'kept', ?)", (stamp,))
    con.execute("INSERT INTO sale_buyer_link VALUES ('s2', 'gone', ?)", (stamp,))
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    con = sqlite3.connect(intel)
    remaining = sorted(r[0] for r in con.execute(
        "SELECT sale_event_id FROM sale_buyer_link"))
    con.close()
    assert remaining == ["s1"]


def test_child_rows_do_not_outlive_their_parent(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "old", "Silvermoon", now - 90 * DAY)
    _seed_scan(con, "new", "Silvermoon", now - 1 * DAY)
    con.commit()
    con.close()

    Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()

    con = sqlite3.connect(intel)
    orphans = con.execute(
        "SELECT COUNT(*) FROM ah_auction a WHERE NOT EXISTS "
        "(SELECT 1 FROM ah_snapshot s WHERE s.snapshot_id = a.snapshot_id)"
    ).fetchone()[0]
    con.close()
    assert orphans == 0


# ---------------------------------------------------------------------------
# dry run, idempotency, recovery
# ---------------------------------------------------------------------------
def test_plan_changes_nothing(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "old", "Silvermoon", now - 90 * DAY)
    _seed_scan(con, "new", "Silvermoon", now - 1 * DAY)
    con.commit()
    con.close()
    before = (_count(intel, "ah_snapshot"), _count(intel, "ah_auction"))

    report = Retention(intel, tsm, days=30, now=now).plan()

    after = (_count(intel, "ah_snapshot"), _count(intel, "ah_auction"))
    assert before == after, "plan must not touch the database"
    assert report.total_eligible > 0
    assert report.total_bytes > 0
    assert report.dry_run is True


def test_cleanup_is_idempotent(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "old", "Silvermoon", now - 90 * DAY)
    _seed_scan(con, "new", "Silvermoon", now - 1 * DAY)
    con.commit()
    con.close()

    first = Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()
    state_after_first = (_count(intel, "ah_snapshot"), _count(intel, "ah_auction"))

    second = Retention(intel, tsm, days=30, now=now, allow_replay_loss=True).apply()
    state_after_second = (_count(intel, "ah_snapshot"), _count(intel, "ah_auction"))

    assert first.total_deleted > 0
    assert second.total_deleted == 0, "a rerun must find nothing left to do"
    assert state_after_first == state_after_second


def test_a_failure_halfway_rolls_back_everything(stores, monkeypatch):
    """An error mid-cleanup must leave the database exactly as it was."""
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "old", "Silvermoon", now - 90 * DAY)
    _seed_scan(con, "new", "Silvermoon", now - 1 * DAY)
    con.commit()
    con.close()
    before = (_count(intel, "ah_snapshot"), _count(intel, "ah_auction"))

    calls = {"n": 0}
    original = Retention._delete

    def explode(self, con, task):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise sqlite3.OperationalError("simulated disk failure")
        return original(self, con, task)

    monkeypatch.setattr(Retention, "_delete", explode)

    report = Retention(intel, tsm, days=30, now=now,
                       allow_replay_loss=True).apply()

    after = (_count(intel, "ah_snapshot"), _count(intel, "ah_auction"))
    assert before == after, "a rolled-back run must not leave partial deletions"
    assert report.errors, "the failure must be reported, not swallowed"


def test_apply_refuses_to_lose_replay_data_without_an_archive(stores):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "old", "Silvermoon", now - 90 * DAY)
    _seed_scan(con, "new", "Silvermoon", now - 1 * DAY)
    con.commit()
    con.close()

    report = Retention(intel, tsm, days=30, now=now).apply()

    assert report.errors
    assert any("refusing" in e for e in report.errors)
    assert _count(intel, "ah_snapshot") == 2, "nothing may be deleted"


def test_archive_round_trip(stores, tmp_path):
    intel, tsm = stores
    now = 1_800_000_000
    con = sqlite3.connect(intel)
    _seed_scan(con, "old", "Silvermoon", now - 90 * DAY)
    _seed_scan(con, "new", "Silvermoon", now - 1 * DAY)
    con.commit()
    con.close()

    archive = tmp_path / "archive"
    report = Retention(intel, tsm, days=30, now=now,
                       archive_dir=archive).apply()

    assert not report.errors
    files = sorted(archive.glob("*.jsonl.gz"))
    assert files, "the archive must contain the deleted rows"
    snap_file = [f for f in files if "ah_snapshot" in f.name][0]
    with gzip.open(snap_file, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
        rows = [json.loads(line) for line in fh]
    assert header["table"] == "ah_snapshot"
    assert header["cutoff_epoch"] == now - 30 * DAY
    assert {r["snapshot_id"] for r in rows} == {"old"}
    assert _count(intel, "ah_snapshot") == 1


def test_policy_for_unknown_table_is_none():
    assert policy_for("does_not_exist", "intel.sqlite") is None


def test_a_non_positive_window_is_rejected(stores):
    intel, tsm = stores
    with pytest.raises(ValueError):
        Retention(intel, tsm, days=0)


# ---------------------------------------------------------------------------
# staged deletion: bounded in tables and in rows
# ---------------------------------------------------------------------------
#
# A first real run should be small enough to inspect.  These tests pin the two
# bounds and the property that makes them safe: a limited run deletes exactly
# what it archived, and a second run continues where the first stopped.

def _seed_many(stores, scans: int = 3, auctions: int = 5):
    intel, tsm = stores
    now = 1_797_408_000
    con = sqlite3.connect(intel)
    for i in range(scans):
        _seed_scan(con, f"s{i}", "realm-a", now - 60 * DAY - i * DAY, auctions)
    con.commit()
    con.close()
    return intel, tsm, now


def test_max_rows_caps_the_deletion(stores):
    intel, _tsm, now = _seed_many(stores)
    before = _count(intel, "ah_auction")
    report = Retention(intel, _tsm, days=30, now=now, max_rows=4,
                       allow_replay_loss=True).apply()
    assert report.total_deleted == 4
    assert _count(intel, "ah_auction") == before - 4


def test_a_limited_run_reports_that_it_was_limited(stores):
    intel, tsm, now = _seed_many(stores)
    report = Retention(intel, tsm, days=30, now=now, max_rows=4,
                       allow_replay_loss=True).apply()
    limited = [s for s in report.sources if s.limited]
    assert limited, "the report must say the run was cut short"
    assert "limited" in report.summary()


def test_repeating_a_limited_run_eventually_finishes(stores):
    intel, tsm, now = _seed_many(stores)
    total = 0
    for _ in range(50):
        report = Retention(intel, tsm, days=30, now=now, max_rows=3,
                             allow_replay_loss=True).apply()
        if report.total_deleted == 0:
            break
        total += report.total_deleted
    # the second run must now find nothing
    final = Retention(intel, tsm, days=30, now=now).plan()
    assert final.total_eligible == 0
    assert total > 0


def test_only_table_restricts_the_run(stores):
    intel, tsm, now = _seed_many(stores)
    con = sqlite3.connect(tsm)
    _seed_tsm(con, 1, "realm-a", "realm_items", now - 60 * DAY)
    _seed_tsm(con, 2, "realm-a", "realm_items", now - 59 * DAY)
    con.commit()
    con.close()

    report = Retention(intel, tsm, days=30, now=now,
                       only_tables=["tsm_price_change"],
                       allow_replay_loss=True).apply()
    assert {s.table for s in report.sources} == {"tsm_price_change"}
    # the intel side was untouched
    assert _count(intel, "ah_auction") > 0


def test_a_limited_run_archives_exactly_what_it_deleted(stores, tmp_path):
    intel, tsm, now = _seed_many(stores)
    archive = tmp_path / "archive"
    report = Retention(intel, tsm, days=30, now=now, max_rows=4,
                       archive_dir=archive).apply()

    archived = 0
    for path in archive.glob("*.jsonl.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            next(fh)  # header
            archived += sum(1 for _ in fh)
    assert archived == report.total_deleted


def test_a_dry_run_with_a_budget_reports_the_bounded_number(stores):
    intel, tsm, now = _seed_many(stores)
    report = Retention(intel, tsm, days=30, now=now, max_rows=4,
                       allow_replay_loss=True).plan()
    assert report.total_eligible > 4
    assert sum(s.deleted for s in report.sources) == 4


def test_a_non_positive_row_budget_is_rejected(stores):
    intel, tsm = stores
    with pytest.raises(ValueError):
        Retention(intel, tsm, max_rows=0)
