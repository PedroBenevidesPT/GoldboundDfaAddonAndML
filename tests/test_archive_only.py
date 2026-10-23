"""Archive-only runs: write the evidence, delete nothing.

This is the step that has to be demonstrated before a real deletion is
considered.  A run in this mode must produce exactly the archive that an
``apply`` would have produced, verify it, and leave every row where it was.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from test_archive import NOW, _seed, _stores
from woweconomy.retention import MANIFEST_NAME, Retention, read_manifest


def _pair(tmp_path: Path, name: str, n_scans: int = 2):
    root = tmp_path / name
    root.mkdir()
    intel, tsm = _stores(root)
    _seed(intel, tsm, n_scans=n_scans)
    return intel, tsm


def _counts(intel: Path, tsm: Path) -> dict:
    """Every table's row count, so "nothing was deleted" is checked, not assumed."""
    out = {}
    for path in (intel, tsm):
        con = sqlite3.connect(path)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                out[f"{path.name}.{table}"] = con.execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            con.close()
    return out


@pytest.fixture
def runner(tmp_path):
    intel, tsm = _pair(tmp_path, "live")
    archive = tmp_path / "archive"
    return intel, tsm, archive, Retention(
        intel_db=intel, tsm_db=tsm, days=30, now=NOW,
        archive_dir=archive, archive_only=True)


# ---------------------------------------------------------------------------
# it writes, and it does not delete
# ---------------------------------------------------------------------------

def test_archive_only_deletes_nothing_at_all(runner):
    intel, tsm, archive, run = runner
    before = _counts(intel, tsm)

    report = run.archive()

    assert not report.errors, report.errors
    assert _counts(intel, tsm) == before, "archive-only must not delete a row"
    assert report.total_deleted == 0
    assert report.total_archived_rows > 0
    assert (archive / MANIFEST_NAME).exists()


def test_archive_only_writes_real_rows_with_a_header(runner):
    _intel, _tsm, archive, run = runner
    run.archive()

    manifest = read_manifest(archive)
    assert manifest["files"]
    for entry in manifest["files"]:
        path = archive / entry["name"]
        assert path.exists()
        assert entry["rows"] > 0
        assert entry["columns"], "the header must name the columns"
        assert entry["table"]
        assert len(entry["file_sha256"]) == 64
        assert len(entry["content_sha256"]) == 64


def test_archive_only_verifies_what_it_wrote(runner):
    _intel, _tsm, _archive, run = runner
    report = run.archive()

    verification = report.archive_verification
    assert verification, "an unverified archive is not evidence"
    assert verification["ok"] is True, verification["problems"]
    assert verification["files_checked"] >= 1
    assert verification["files_ok"] == verification["files_checked"]
    assert verification["rows"] == report.total_archived_rows


# ---------------------------------------------------------------------------
# it is the same archive an apply would have written
# ---------------------------------------------------------------------------

def test_archive_only_archives_exactly_what_apply_would_delete(tmp_path):
    intel_a, tsm_a = _pair(tmp_path, "archive")
    intel_b, tsm_b = _pair(tmp_path, "apply")

    archiver = Retention(intel_db=intel_a, tsm_db=tsm_a, days=30, now=NOW,
                         archive_dir=tmp_path / "arch-a", archive_only=True)
    applier = Retention(intel_db=intel_b, tsm_db=tsm_b, days=30, now=NOW,
                        archive_dir=tmp_path / "arch-b")

    archived = archiver.archive()
    applied = applier.apply()

    assert not archived.errors, archived.errors
    assert not applied.errors, applied.errors
    assert archived.total_archived_rows == applied.total_deleted
    assert archived.total_archived_rows > 0

    # and the same per table, not merely the same total
    by_table = {s.table: s.archived_rows for s in archived.sources
                if s.archived_rows}
    deleted = {s.table: s.deleted for s in applied.sources if s.deleted}
    assert by_table == deleted


def test_archive_only_matches_the_plan_it_precedes(runner):
    _intel, _tsm, _archive, run = runner
    planned = run.plan().total_eligible
    archived = run.archive().total_archived_rows
    assert archived == planned


# ---------------------------------------------------------------------------
# the row budget still applies, so a first run can be small
# ---------------------------------------------------------------------------

def test_archive_only_honours_the_row_budget(tmp_path):
    intel, tsm = _pair(tmp_path, "budget")
    run = Retention(intel_db=intel, tsm_db=tsm, days=30, now=NOW,
                    archive_dir=tmp_path / "arch", archive_only=True,
                    max_rows=3)
    report = run.archive()
    assert not report.errors, report.errors
    assert report.total_archived_rows == 3
    assert any(s.limited for s in report.sources)

    # rerunning continues rather than starting over, and still deletes nothing
    before = _counts(intel, tsm)
    run2 = Retention(intel_db=intel, tsm_db=tsm, days=30, now=NOW,
                     archive_dir=tmp_path / "arch", archive_only=True,
                     max_rows=3, only_tables=["ah_auction"])
    report2 = run2.archive()
    assert report2.total_archived_rows == 3
    assert _counts(intel, tsm) == before


def test_archive_only_requires_an_archive_directory(tmp_path):
    intel, tsm = _pair(tmp_path, "nope")
    with pytest.raises(ValueError):
        Retention(intel_db=intel, tsm_db=tsm, days=30, now=NOW,
                  archive_only=True)


# ---------------------------------------------------------------------------
# a damaged archive in the directory is reported, not trusted
# ---------------------------------------------------------------------------

def test_a_damaged_earlier_archive_fails_the_run(tmp_path):
    intel, tsm = _pair(tmp_path, "damaged")
    archive = tmp_path / "arch"

    first = Retention(intel_db=intel, tsm_db=tsm, days=30, now=NOW,
                      archive_dir=archive, archive_only=True).archive()
    assert first.archive_verification["ok"] is True

    victim = archive / read_manifest(archive)["files"][0]["name"]
    with open(victim, "ab") as fh:
        fh.write(b"\x00 not gzip\n")

    # a different window writes new files and leaves the damaged one in place
    second = Retention(intel_db=intel, tsm_db=tsm, days=60, now=NOW,
                       archive_dir=archive, archive_only=True).archive()

    assert second.archive_verification["ok"] is False
    assert any("verification failed" in e for e in second.errors)
    assert second.total_deleted == 0


def test_archive_only_is_repeatable_without_growing_the_manifest(runner):
    _intel, _tsm, archive, run = runner
    first = run.archive()
    names_first = [e["name"] for e in read_manifest(archive)["files"]]

    again = Retention(intel_db=run.intel_db, tsm_db=run.tsm_db, days=30,
                      now=NOW, archive_dir=archive, archive_only=True).archive()
    names_second = [e["name"] for e in read_manifest(archive)["files"]]

    assert names_first == names_second
    assert again.total_archived_rows == first.total_archived_rows
    assert again.archive_verification["ok"] is True


def test_the_report_says_nothing_was_deleted(runner):
    _intel, _tsm, _archive, run = runner
    text = run.archive().summary()
    assert "archived (nothing deleted)" in text
    assert "archive verification: PASS" in text
