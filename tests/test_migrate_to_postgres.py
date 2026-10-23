"""Tests for the SQLite -> Postgres loader's value encoding.

The encoder is the part of the migration that can corrupt data without
changing a row count, so it is tested on its own: every escape, the NULL
marker, and the distinction between NULL and the empty string.

The round-trip tests at the bottom run against a real Postgres and are skipped
when none is reachable.
"""
from __future__ import annotations

import os
import sqlite3
import uuid

import pytest

from scripts.migrate_to_postgres import _CopyStream, encode_text_value

DSN = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql://goldbound:goldbound@localhost:5432/goldbound"),
)


def _postgres_available() -> bool:
    try:
        import psycopg2

        con = psycopg2.connect(DSN, connect_timeout=2)
    except Exception:
        return False
    con.close()
    return True


requires_pg = pytest.mark.skipif(
    not _postgres_available(), reason="no Postgres reachable"
)


# ---------------------------------------------------------------------------
# value encoding
# ---------------------------------------------------------------------------

def test_null_uses_the_copy_marker():
    assert encode_text_value(None) == r"\N"


def test_the_empty_string_is_not_null():
    # This is the whole reason the loader uses text format instead of CSV:
    # CSV writes both of these as nothing.
    assert encode_text_value("") == ""
    assert encode_text_value(None) != encode_text_value("")


def test_backslash_is_escaped_first():
    assert encode_text_value("a\\b") == "a\\\\b"


def test_control_characters_are_escaped():
    assert encode_text_value("a\tb") == "a\\tb"
    assert encode_text_value("a\nb") == "a\\nb"
    assert encode_text_value("a\rb") == "a\\rb"


def test_a_value_that_looks_like_the_null_marker_is_escaped():
    # A literal "\N" in the data must not be read back as NULL.
    assert encode_text_value("\\N") == "\\\\N"


def test_numbers_are_plain():
    assert encode_text_value(42) == "42"
    assert encode_text_value(-7) == "-7"
    assert encode_text_value(1.5) == "1.5"


def test_booleans_use_postgres_literals():
    assert encode_text_value(True) == "t"
    assert encode_text_value(False) == "f"


def test_bytes_become_bytea_hex_with_the_backslash_escaped():
    out = encode_text_value(b"\x00\xff")
    assert out == r"\\x00ff"


def test_unicode_passes_through_unchanged():
    assert encode_text_value("Dragonmaw·Å") == "Dragonmaw·Å"


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------

def test_the_stream_emits_one_tab_separated_line_per_row():
    stream = _CopyStream(iter([(1, "a"), (2, "b")]), chunk_rows=10)
    assert stream.read() == "1\ta\n2\tb\n"
    assert stream.rows_written == 2


def test_the_stream_handles_an_empty_table():
    stream = _CopyStream(iter([]), chunk_rows=10)
    assert stream.read() == ""
    assert stream.rows_written == 0


def test_the_stream_chunks_without_losing_rows():
    rows = [(i, f"v{i}") for i in range(25)]
    stream = _CopyStream(iter(rows), chunk_rows=4)
    chunks = []
    while True:
        piece = stream.read(16)
        if not piece:
            break
        chunks.append(piece)
    joined = "".join(chunks)
    assert joined.count("\n") == 25
    assert joined.startswith("0\tv0\n")
    assert joined.endswith("24\tv24\n")


def test_read_all_at_once_returns_everything():
    rows = [(i,) for i in range(3)]
    assert _CopyStream(iter(rows)).read(-1) == "0\n1\n2\n"


# ---------------------------------------------------------------------------
# live round-trip
# ---------------------------------------------------------------------------

@pytest.fixture()
def pg_schema():
    import psycopg2

    from woweconomy.dbcompat import PgConnection, Row

    schema = "mig_" + uuid.uuid4().hex[:10]
    raw = psycopg2.connect(DSN)
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    raw.autocommit = False
    con = PgConnection(raw)
    con.row_factory = Row
    con.execute(f'SET search_path TO "{schema}"')
    try:
        yield con
    finally:
        con._raw.rollback()
        con._raw.autocommit = True
        with con._raw.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        con._raw.close()


@requires_pg
def test_tricky_values_survive_a_copy_round_trip(pg_schema):
    """NULL, empty string, tabs, newlines, backslashes and unicode."""
    pg_schema.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)"
    )
    source = [
        (1, None, ""),
        (2, "", None),
        (3, "tab\there", "line\nbreak"),
        (4, "back\\slash", "\\N"),
        (5, "Dragonmaw·Å", "quote'and\"double"),
    ]
    stream = _CopyStream(iter(source))
    pg_schema.copy_expert(
        "COPY t (id, a, b) FROM STDIN WITH (FORMAT text)", stream
    )

    rows = pg_schema.execute("SELECT id, a, b FROM t ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == source


@requires_pg
def test_bytea_survives_a_copy_round_trip(pg_schema):
    pg_schema.executescript("CREATE TABLE b (id INTEGER PRIMARY KEY, payload BYTEA)")
    payload = b"\x00\x01\xfe\xff"
    stream = _CopyStream(iter([(1, payload)]))
    pg_schema.copy_expert(
        "COPY b (id, payload) FROM STDIN WITH (FORMAT text)", stream
    )
    got = pg_schema.execute("SELECT payload FROM b").fetchone()[0]
    assert bytes(got) == payload


@requires_pg
def test_a_real_sqlite_table_round_trips_through_copy(pg_schema, tmp_path):
    """The actual path: read from SQLite, encode, COPY, compare."""
    lite = sqlite3.connect(str(tmp_path / "src.sqlite"))
    lite.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT, n INTEGER)")
    data = [
        ("empty", "", 0),
        ("null", None, 1),
        ("tab", "a\tb", 2),
        ("newline", "a\nb", 3),
        ("unicode", "Ångström", 4),
        ("percent", "100%", 5),
    ]
    lite.executemany("INSERT INTO kv (k, v, n) VALUES (?,?,?)", data)
    lite.commit()

    pg_schema.executescript("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT, n INTEGER)")
    stream = _CopyStream(lite.execute("SELECT k, v, n FROM kv"))
    pg_schema.copy_expert(
        "COPY kv (k, v, n) FROM STDIN WITH (FORMAT text)", stream
    )
    got = pg_schema.execute("SELECT k, v, n FROM kv ORDER BY k").fetchall()
    assert sorted((r[0], r[1], r[2]) for r in got) == sorted(data)
    lite.close()
