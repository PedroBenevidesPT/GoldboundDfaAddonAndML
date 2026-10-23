"""How a process decides which database it is talking to.

The dangerous failure here is not a crash: it is a worker that quietly uses
a local SQLite file while the pipeline writes to Postgres.  The data then
splits in two and every report is answered from the wrong half.

Two things are pinned here:

* ``load_env`` reads ``.env`` into the environment, and only when asked --
  a library that did it on import would flip the test suite to Postgres,
  where a test handing over a temporary SQLite path would write to the
  shared database instead;
* a run that falls back to SQLite while the project's ``.env`` names a
  Postgres database says so, once, instead of looking like it used the
  shared stack.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from woweconomy import dbcompat


DSN = "postgresql://goldbound:s3cret@localhost:5432/goldbound"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every test starts with no backend chosen."""
    monkeypatch.delenv("WOW_PIPELINE_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(dbcompat, "_warned_about_env", False, raising=False)
    yield


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# reading the file
# ---------------------------------------------------------------------------
def test_the_env_file_is_parsed_without_touching_the_environment(tmp_path: Path):
    path = write_env(tmp_path, f"DATABASE_URL={DSN}\nOTHER=1\n")

    values = dbcompat.read_env_file(path)

    assert values == {"DATABASE_URL": DSN, "OTHER": "1"}


def test_comments_blanks_and_quotes_are_handled(tmp_path: Path):
    path = write_env(
        tmp_path,
        "# a comment\n"
        "\n"
        'QUOTED="a value with spaces"\n'
        "SINGLE='another'\n"
        "export EXPORTED=yes\n"
        "NOT_A_PAIR\n"
        "EMPTY=\n",
    )

    values = dbcompat.read_env_file(path)

    assert values["QUOTED"] == "a value with spaces"
    assert values["SINGLE"] == "another"
    assert values["EXPORTED"] == "yes"
    assert values["EMPTY"] == ""
    assert "NOT_A_PAIR" not in values


def test_a_missing_env_file_reads_as_empty(tmp_path: Path):
    assert dbcompat.read_env_file(tmp_path / "nothing.env") == {}


# ---------------------------------------------------------------------------
# applying it
# ---------------------------------------------------------------------------
def test_load_env_sets_the_backend(tmp_path: Path, monkeypatch):
    path = write_env(tmp_path, f"WOW_PIPELINE_BACKEND=postgres\nDATABASE_URL={DSN}\n")

    dbcompat.load_env(path)

    assert dbcompat.backend() == "postgres"


def test_load_env_does_not_override_what_the_caller_set(tmp_path: Path, monkeypatch):
    """`WOW_PIPELINE_BACKEND=sqlite python ...` must still mean what it says."""
    monkeypatch.setenv("WOW_PIPELINE_BACKEND", "sqlite")
    path = write_env(tmp_path, "WOW_PIPELINE_BACKEND=postgres\n")

    dbcompat.load_env(path)

    assert dbcompat.backend() == "sqlite"


def test_load_env_can_override_when_asked(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WOW_PIPELINE_BACKEND", "sqlite")
    path = write_env(tmp_path, "WOW_PIPELINE_BACKEND=postgres\n")

    dbcompat.load_env(path, override=True)

    import os
    assert os.environ["WOW_PIPELINE_BACKEND"] == "postgres"


def test_importing_the_module_does_not_choose_a_backend(monkeypatch):
    """The suite would otherwise write to the shared database."""
    assert dbcompat.backend() == "sqlite"


# ---------------------------------------------------------------------------
# the warning that stops a silent split
# ---------------------------------------------------------------------------
def test_a_sqlite_run_says_so_when_the_env_names_postgres(tmp_path: Path):
    path = write_env(tmp_path, f"DATABASE_URL={DSN}\n")

    assert dbcompat.env_file_disagrees(path) == DSN


def test_a_postgres_run_has_nothing_to_say(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WOW_PIPELINE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", DSN)
    path = write_env(tmp_path, f"DATABASE_URL={DSN}\n")

    assert dbcompat.env_file_disagrees(path) == ""


def test_an_env_without_a_dsn_has_nothing_to_say(tmp_path: Path):
    path = write_env(tmp_path, "SOMETHING=else\n")

    assert dbcompat.env_file_disagrees(path) == ""


def test_the_warning_is_printed_once_and_hides_the_password(
        tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setattr(dbcompat, "env_file_path", lambda path=None: path or
                        write_env(tmp_path, f"DATABASE_URL={DSN}\n"))
    monkeypatch.setattr(dbcompat, "PROJECT_ROOT", tmp_path)

    with caplog.at_level(logging.WARNING, logger=dbcompat.__name__):
        dbcompat.store_exists(None)
        dbcompat.store_exists(None)

    warnings = [r for r in caplog.records if "sqlite" in r.getMessage()]
    assert len(warnings) == 1, "a warning per call would flood a loop"
    assert "s3cret" not in warnings[0].getMessage()
    assert "goldbound:***@" in warnings[0].getMessage()


def test_the_dsn_is_redacted_for_a_log_line():
    assert dbcompat.redact_dsn(DSN) == "postgresql://goldbound:***@localhost:5432/goldbound"


def test_a_dsn_without_credentials_is_left_alone():
    assert dbcompat.redact_dsn("postgresql://localhost/db") == \
        "postgresql://localhost/db"
