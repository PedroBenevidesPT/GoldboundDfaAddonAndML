"""The external archive: written atomically, and verifiable after the fact.

An archive that has not been verified is not evidence.  These tests check that
the manifest describes exactly what is on disk, that a damaged file is caught
rather than trusted, and that an interrupted write cannot masquerade as a
complete one.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from woweconomy.retention import (
    MANIFEST_NAME,
    Retention,
    read_manifest,
    verify_archive,
)

DAY = 86400
NOW = 1_797_408_000


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _stores(tmp_path: Path):
    intel = tmp_path / "intel.sqlite"
    tsm = tmp_path / "tsm.sqlite"
    con = sqlite3.connect(intel)
    con.executescript("""
        CREATE TABLE ah_snapshot (snapshot_id TEXT PRIMARY KEY, realm TEXT,
                                  captured_at INTEGER);
        CREATE TABLE ah_auction (snapshot_id TEXT, auction_key TEXT,
                                 item_id INTEGER);
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
                                   kind TEXT, updated_at TEXT);
        CREATE TABLE tsm_price_change (snapshot_id INTEGER, scope TEXT,
                                       kind TEXT, key_id INTEGER,
                                       market_value INTEGER);
        CREATE TABLE tsm_realm_price (scope TEXT, item_id INTEGER, name TEXT,
                                      snapshot_id INTEGER);
        CREATE TABLE snapshots (snapshotId INTEGER PRIMARY KEY,
                                snapshotTimestamp TEXT);
        CREATE TABLE market_snapshots (snapshotId INTEGER, itemId INTEGER);
        CREATE TABLE auction_events (event_id TEXT PRIMARY KEY, ts TEXT);
        CREATE TABLE auction_intel_events (event_id TEXT PRIMARY KEY, ts TEXT);
    """)
    con.commit()
    con.close()
    return intel, tsm


def _seed(intel: Path, tsm: Path, n_scans: int = 2, n_auctions: int = 4) -> None:
    con = sqlite3.connect(intel)
    for i in range(n_scans):
        sid = f"s{i}"
        con.execute("INSERT INTO ah_snapshot VALUES (?,?,?)",
                    (sid, "realm-a", NOW - 90 * DAY - i * DAY))
        for j in range(n_auctions):
            con.execute("INSERT INTO ah_auction VALUES (?,?,?)",
                        (sid, f"{sid}-{j}", 1000 + j))
    con.commit()
    con.close()
    con = sqlite3.connect(tsm)
    for i in range(n_scans):
        con.execute("INSERT INTO tsm_snapshot VALUES (?,?,?,?)",
                    (i + 1, "realm-a", "realm_items",
                     _iso(NOW - 90 * DAY - i * DAY)))
        for j in range(n_auctions):
            con.execute("INSERT INTO tsm_price_change VALUES (?,?,?,?,?)",
                        (i + 1, "realm-a", "realm_items", j, 100 * (j + 1)))
    con.commit()
    con.close()


@pytest.fixture
def run(tmp_path):
    intel, tsm = _stores(tmp_path)
    _seed(intel, tsm)
    archive = tmp_path / "archive"
    runner = Retention(intel_db=intel, tsm_db=tsm, days=30, now=NOW,
                       archive_dir=archive)
    return intel, tsm, archive, runner


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def test_applying_writes_an_archive_and_a_manifest(run):
    _intel, _tsm, archive, runner = run
    report = runner.apply()
    assert not report.errors, report.errors
    assert (archive / MANIFEST_NAME).exists()

    manifest = read_manifest(archive)
    assert manifest["format"] == "dfa-archive"
    assert manifest["version"] >= 1
    assert manifest["files"], "the manifest must list what was archived"

    for entry in manifest["files"]:
        assert (archive / entry["name"]).exists()
        assert entry["rows"] > 0
        assert len(entry["file_sha256"]) == 64
        assert len(entry["content_sha256"]) == 64


def test_a_dry_run_writes_no_archive_and_no_manifest(run):
    _intel, _tsm, archive, runner = run
    runner.plan()
    assert not archive.exists() or not (archive / MANIFEST_NAME).exists()


def test_the_archived_rows_are_the_rows_that_were_deleted(run):
    intel, _tsm, archive, runner = run
    before = sqlite3.connect(intel).execute(
        "SELECT COUNT(*) FROM ah_auction").fetchone()[0]
    report = runner.apply()
    after = sqlite3.connect(intel).execute(
        "SELECT COUNT(*) FROM ah_auction").fetchone()[0]
    deleted = before - after

    manifest = read_manifest(archive)
    archived = sum(e["rows"] for e in manifest["files"]
                   if e["table"] == "ah_auction")
    assert archived == deleted
    assert report.total_deleted == sum(
        e["rows"] for e in manifest["files"])


def test_the_archive_header_names_the_table_and_columns(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    manifest = read_manifest(archive)
    entry = next(e for e in manifest["files"] if e["table"] == "ah_auction")
    with gzip.open(archive / entry["name"], "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
    assert header["table"] == "ah_auction"
    assert header["database"] == "intel.sqlite"
    assert header["columns"] == entry["columns"]


def test_no_part_file_survives_a_successful_run(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    assert not list(archive.glob("*.part"))


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def test_a_fresh_archive_verifies(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    result = verify_archive(archive)
    assert result.ok, result.summary()
    assert result.files_checked == result.files_ok
    assert result.rows > 0


def test_verification_notices_a_flipped_byte(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    manifest = read_manifest(archive)
    path = archive / manifest["files"][0]["name"]

    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))

    result = verify_archive(archive)
    assert not result.ok
    problems = " ".join(p for f in result.files for p in f.problems)
    assert "sha256" in problems or "cannot read" in problems


def test_verification_notices_a_truncated_file(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    manifest = read_manifest(archive)
    path = archive / manifest["files"][0]["name"]
    path.write_bytes(path.read_bytes()[:40])

    result = verify_archive(archive)
    assert not result.ok


def test_verification_notices_a_missing_file(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    manifest = read_manifest(archive)
    (archive / manifest["files"][0]["name"]).unlink()

    result = verify_archive(archive)
    assert not result.ok
    assert any("missing" in p for f in result.files for p in f.problems)


def test_verification_notices_a_file_the_manifest_does_not_list(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    (archive / "stray.jsonl.gz").write_bytes(b"not really an archive")

    result = verify_archive(archive)
    assert not result.ok
    assert "stray.jsonl.gz" in result.orphans


def test_verification_notices_a_leftover_part_file(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    (archive / "interrupted.jsonl.gz.part").write_bytes(b"partial")

    result = verify_archive(archive)
    assert not result.ok
    assert any(p.endswith(".part") for p in result.orphans)


def test_verification_notices_a_rewritten_row_count(run):
    """A gzip that still decompresses but no longer has the recorded rows."""
    _intel, _tsm, archive, runner = run
    runner.apply()
    manifest = read_manifest(archive)
    entry = manifest["files"][0]
    path = archive / entry["name"]

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = fh.readlines()
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.writelines(lines[:-1])  # drop one data row

    result = verify_archive(archive)
    assert not result.ok
    problems = " ".join(p for f in result.files for p in f.problems)
    assert "row count" in problems or "size" in problems


def test_verifying_a_directory_that_does_not_exist_fails_cleanly(tmp_path):
    result = verify_archive(tmp_path / "nope")
    assert not result.ok
    assert any("does not exist" in p for p in result.problems)


def test_verifying_a_directory_without_a_manifest_reports_no_files(tmp_path):
    (tmp_path / "empty").mkdir()
    result = verify_archive(tmp_path / "empty")
    assert result.ok  # nothing claimed, nothing broken
    assert result.files_checked == 0


def test_a_manifest_from_a_newer_version_is_refused(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    path = archive / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")

    result = verify_archive(archive)
    assert not result.ok
    assert any("version" in p for p in result.problems)


def test_a_foreign_manifest_format_is_refused(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    path = archive / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["format"] = "something-else"
    path.write_text(json.dumps(data), encoding="utf-8")

    result = verify_archive(archive)
    assert not result.ok


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------

def test_a_second_apply_archives_nothing_new(run):
    _intel, _tsm, archive, runner = run
    runner.apply()
    first = read_manifest(archive)
    second_report = runner.apply()
    assert second_report.total_deleted == 0
    after = read_manifest(archive)
    assert len(after["files"]) == len(first["files"])


def test_two_runs_merge_into_one_manifest(run, tmp_path):
    """A second run adds its files without duplicating the first run's."""
    intel, tsm, archive, runner = run
    runner.apply()
    first = read_manifest(archive)
    first_names = {e["name"] for e in first["files"]}

    # a further batch of old rows, which the second run will archive.  Two
    # scans, because the newest scan of a realm is always kept: a lone scan
    # would be protected and nothing would expire.
    con = sqlite3.connect(intel)
    con.execute("INSERT INTO ah_snapshot VALUES (?,?,?)",
                ("s9", "realm-b", NOW - 120 * DAY))
    con.execute("INSERT INTO ah_snapshot VALUES (?,?,?)",
                ("s10", "realm-b", NOW - 119 * DAY))
    con.execute("INSERT INTO ah_auction VALUES (?,?,?)", ("s9", "s9-0", 42))
    con.commit()
    con.close()

    later = Retention(intel_db=intel, tsm_db=tsm, days=30, now=NOW,
                      archive_dir=archive)
    later.apply()

    manifest = read_manifest(archive)
    assert manifest["runs"] >= 2
    names = [e["name"] for e in manifest["files"]]
    assert len(names) == len(set(names)), "no file may be listed twice"
    assert first_names <= set(names), "the first run's files must still be listed"
