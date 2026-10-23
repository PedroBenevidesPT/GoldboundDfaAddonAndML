"""Conformance tests: the same statements, run on SQLite and on Postgres.

These are the tests that decide whether the compatibility layer is real.  Each
one performs an operation through the *same* code path twice -- once against a
throwaway SQLite file, once against a throwaway Postgres schema -- and asserts
the two agree.

They are skipped, not failed, when no Postgres is reachable, so a workstation
run still works.  When the stack is up (``docker compose up -d postgres``)
they run for real against PostgreSQL 16, not a mock.
"""
from __future__ import annotations

import os
import sqlite3
import uuid

import pytest

from woweconomy.dbcompat import PgConnection, Row, UnsupportedSQL, connect
from woweconomy.dbcompat.dialect import translate

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
    not _postgres_available(),
    reason="no Postgres reachable; start it with `docker compose up -d postgres`",
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sqlite_con(tmp_path):
    con = sqlite3.connect(str(tmp_path / "conformance.sqlite"))
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0)"
    )
    con.commit()
    yield con
    con.close()


@pytest.fixture()
def pg_con():
    """A PgConnection scoped to a private schema, dropped afterwards."""
    import psycopg2

    schema = "conf_" + uuid.uuid4().hex[:12]
    raw = psycopg2.connect(DSN)
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    raw.autocommit = False
    con = PgConnection(raw)
    con.row_factory = Row
    con.execute(f'SET search_path TO "{schema}"')
    con.executescript(
        "CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0)"
    )
    try:
        yield con
    finally:
        con._raw.rollback()
        con._raw.autocommit = True
        with con._raw.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        con._raw.close()


# ---------------------------------------------------------------------------
# upsert semantics
# ---------------------------------------------------------------------------

def _upsert(con, k, v):
    return con.execute(
        "INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, v)
    )


@requires_pg
def test_insert_or_replace_updates_in_place_on_both_backends(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        _upsert(con, "a", "first")
        con.commit()
        _upsert(con, "a", "second")
        con.commit()
        rows = con.execute("SELECT k, v FROM kv").fetchall()
        assert len(rows) == 1, f"{type(con).__name__} kept a duplicate row"
        assert rows[0][1] == "second"


@requires_pg
def test_insert_or_ignore_keeps_the_first_row_on_both_backends(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        con.execute("INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)", ("a", "first"))
        con.execute("INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)", ("a", "second"))
        con.commit()
        rows = con.execute("SELECT v FROM kv WHERE k = ?", ("a",)).fetchall()
        assert [r[0] for r in rows] == ["first"]


@requires_pg
def test_a_conditional_update_reports_the_race_winner_on_both_backends(sqlite_con, pg_con):
    """The lease pattern: only the process whose UPDATE matches wins.

    The winner takes the lease by moving ``n`` off its claimable value, so the
    loser's identical statement no longer matches.  ``rowcount`` is what the
    lease store reads to decide who won.
    """
    for con in (sqlite_con, pg_con):
        con.execute("INSERT INTO kv (k, v, n) VALUES ('lease', 'free', 0)")
        con.commit()
        first = con.execute(
            "UPDATE kv SET v = ?, n = 1 WHERE k = 'lease' AND n = 0", ("owner-a",)
        )
        con.commit()
        second = con.execute(
            "UPDATE kv SET v = ?, n = 1 WHERE k = 'lease' AND n = 0", ("owner-b",)
        )
        con.commit()
        assert first.rowcount == 1, f"{type(con).__name__} lost the race it should win"
        assert second.rowcount == 0, f"{type(con).__name__} won a race it should lose"
        assert con.execute("SELECT v FROM kv").fetchone()[0] == "owner-a"


# ---------------------------------------------------------------------------
# row shape
# ---------------------------------------------------------------------------

@requires_pg
def test_rows_are_addressable_by_name_and_position_on_both_backends(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        con.execute("INSERT INTO kv (k, v, n) VALUES (?, ?, ?)", ("a", "b", 7))
        con.commit()
        row = con.execute("SELECT k, v, n FROM kv").fetchone()
        assert row["k"] == "a" and row[0] == "a"
        assert row["n"] == 7 and row[2] == 7
        assert list(row) == ["a", "b", 7]
        assert len(row) == 3
        assert row.keys() == ["k", "v", "n"]


# ---------------------------------------------------------------------------
# PRAGMA emulation
# ---------------------------------------------------------------------------

@requires_pg
def test_table_info_reports_the_same_shape_as_sqlite(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        rows = con.execute("PRAGMA table_info(kv)").fetchall()
        assert [r["name"] for r in rows] == ["k", "v", "n"]
        assert [r["pk"] for r in rows] == [1, 0, 0]
        assert rows[0]["cid"] == 0
        # `notnull` differs on the primary key by definition: SQLite lets a
        # TEXT primary key hold NULL (a historical quirk), Postgres does not.
        # Every non-key column must agree, and the key must be the only
        # difference -- that is the documented, expected divergence.
        assert [r["notnull"] for r in rows[1:]] == [1, 1]
        assert rows[0]["notnull"] in (0, 1)


@requires_pg
def test_index_list_sees_a_created_index_on_both_backends(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        con.execute("CREATE INDEX kv_v_idx ON kv(v)")
        con.commit()
        names = [r["name"] for r in con.execute("PRAGMA index_list(kv)").fetchall()]
        assert "kv_v_idx" in names


@requires_pg
def test_a_tuning_pragma_is_accepted_and_ignored_on_postgres(pg_con):
    # These must not raise: a pipeline that dies because it cannot set
    # journal_mode would be a pointless outage.
    pg_con.execute("PRAGMA journal_mode=WAL")
    pg_con.execute("PRAGMA busy_timeout=5000")
    pg_con.execute("PRAGMA foreign_keys=ON")
    pg_con.execute("PRAGMA synchronous=NORMAL")


@requires_pg
def test_an_unknown_pragma_is_refused_rather_than_ignored(pg_con):
    with pytest.raises(UnsupportedSQL, match="no Postgres equivalent"):
        pg_con.execute("PRAGMA some_future_thing(1)")


# ---------------------------------------------------------------------------
# sqlite_master
# ---------------------------------------------------------------------------

@requires_pg
def test_sqlite_master_lists_tables_on_both_backends(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        assert "kv" in names


# ---------------------------------------------------------------------------
# strftime
# ---------------------------------------------------------------------------

@requires_pg
def test_strftime_epoch_agrees_between_backends(sqlite_con, pg_con):
    pg_con.execute("CREATE TABLE ts (t TIMESTAMPTZ NOT NULL)")
    pg_con.execute("INSERT INTO ts (t) VALUES ('2026-01-02 03:04:05+00')")
    pg_con.commit()
    pg_value = pg_con.execute(
        "SELECT CAST(strftime('%s', t) AS INTEGER) FROM ts"
    ).fetchone()[0]

    sqlite_con.execute("CREATE TABLE ts (t TEXT NOT NULL)")
    sqlite_con.execute("INSERT INTO ts (t) VALUES ('2026-01-02 03:04:05')")
    sqlite_con.commit()
    lite_value = sqlite_con.execute(
        "SELECT CAST(strftime('%s', t) AS INTEGER) FROM ts"
    ).fetchone()[0]

    assert int(pg_value) == int(lite_value)


@requires_pg
def test_date_with_unixepoch_agrees_between_backends(sqlite_con, pg_con):
    """The day string must be identical, including the UTC boundary.

    2023-11-14 22:13:20 UTC is the 14th; a server in CET would call it the
    15th, which would move a row into the wrong aggregate day.
    """
    epoch = 1_700_000_000
    lite = sqlite_con.execute(f"SELECT date({epoch}, 'unixepoch')").fetchone()[0]
    pg_value = pg_con.execute(f"SELECT date({epoch}, 'unixepoch')").fetchone()[0]
    assert str(pg_value) == str(lite) == "2023-11-14"


# ---------------------------------------------------------------------------
# LIKE with a literal percent
# ---------------------------------------------------------------------------

@requires_pg
def test_like_with_a_literal_percent_survives_parameter_binding(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        con.execute("INSERT INTO kv (k, v) VALUES (?, ?)", ("dragonmaw", "eu"))
        con.execute("INSERT INTO kv (k, v) VALUES (?, ?)", ("blackrock", "eu"))
        con.commit()
        rows = con.execute(
            "SELECT k FROM kv WHERE k LIKE ? AND v = ?", ("%dragon%", "eu")
        ).fetchall()
        assert [r[0] for r in rows] == ["dragonmaw"]


# ---------------------------------------------------------------------------
# CAST precision
# ---------------------------------------------------------------------------

@requires_pg
def test_a_weighted_average_is_numerically_identical_on_both_backends(sqlite_con, pg_con):
    """The exact expression the market panel uses.

    ``CAST(SUM(price * qty) AS REAL) / SUM(qty)`` must not lose precision on
    Postgres, where REAL is a 4-byte float.  With 893312600 the float32 answer
    is 893312576 -- a 24-copper error on a single row, and a different model
    input for every price in the panel.
    """
    rows = [("a", 893312600, 1), ("b", 2500000, 2), ("c", 99999999900, 3)]
    values = {}
    for con in (sqlite_con, pg_con):
        con.execute("DROP TABLE IF EXISTS kv")
        con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, price BIGINT, qty BIGINT)")
        for row in rows:
            con.execute("INSERT INTO kv VALUES (?, ?, ?)", row)
        con.commit()
        values[type(con).__name__] = float(
            con.execute(
                "SELECT CAST(SUM(price * qty) AS REAL) / SUM(qty) FROM kv"
            ).fetchone()[0]
        )

    lite, pg = values["Connection"], values["PgConnection"]
    assert lite == pg, f"backends disagree: sqlite={lite!r} pg={pg!r}"
    # And the shared answer must be the 64-bit one, not the float32 rounding
    # of it (which would be off by tens of copper).
    exact = 300898312300 / 6
    assert abs(pg - exact) < 1e-3, f"precision lost: {pg!r} vs {exact!r}"


# ---------------------------------------------------------------------------
# transactions and scripts
# ---------------------------------------------------------------------------

@requires_pg
def test_rollback_discards_the_work_on_both_backends(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        con.execute("INSERT INTO kv (k, v) VALUES (?, ?)", ("a", "b"))
        con.rollback()
        assert con.execute("SELECT count(*) FROM kv").fetchone()[0] == 0


@requires_pg
def test_executescript_handles_several_statements_and_ddl_types(pg_con):
    pg_con.executescript(
        """
        CREATE TABLE scripted (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload BLOB,
            seen DATETIME
        );
        CREATE INDEX scripted_seen_idx ON scripted(seen);
        """
    )
    info = pg_con.execute("PRAGMA table_info(scripted)").fetchall()
    assert [r["name"] for r in info] == ["id", "payload", "seen"]
    assert [r["pk"] for r in info] == [1, 0, 0]
    # The identity column has to actually generate keys.
    pg_con.execute("INSERT INTO scripted (payload) VALUES (?)", (b"\x00\x01",))
    pg_con.commit()
    assert pg_con.execute("SELECT id FROM scripted").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# the real schema
# ---------------------------------------------------------------------------

@requires_pg
def test_the_intel_store_schema_builds_and_accepts_writes_on_postgres(pg_con):
    """The actual production DDL, not a toy table."""
    from woweconomy.intel import store

    pg_con.executescript(store.SCHEMA)
    store._ensure_ah_indexes(pg_con)

    pg_con.execute(
        "INSERT OR REPLACE INTO ah_snapshot (snapshot_id, region, realm,"
        " realm_slug, captured_at, source, payload_sha256, auction_count)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("snap-1", "eu", "Dragonmaw", "dragonmaw", 1_700_000_000, "api", "abc", 3),
    )
    pg_con.commit()
    # Re-ingesting the same snapshot id must replace, not duplicate.
    pg_con.execute(
        "INSERT OR REPLACE INTO ah_snapshot (snapshot_id, region, realm,"
        " realm_slug, captured_at, source, payload_sha256, auction_count)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("snap-1", "eu", "Dragonmaw", "dragonmaw", 1_700_000_001, "api", "abc", 4),
    )
    pg_con.commit()

    rows = pg_con.execute(
        "SELECT snapshot_id, captured_at, auction_count FROM ah_snapshot"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["captured_at"] == 1_700_000_001
    assert rows[0]["auction_count"] == 4

    # A column added by the legacy-upgrade path must work too.
    store._ensure_column(pg_con, "ah_auction", "variant_key", "TEXT")
    cols = [r["name"] for r in pg_con.execute("PRAGMA table_info(ah_auction)").fetchall()]
    assert "variant_key" in cols


# ---------------------------------------------------------------------------
# executemany batching
# ---------------------------------------------------------------------------

@requires_pg
def test_executemany_matches_individual_inserts(sqlite_con, pg_con):
    rows = [(f"k{i}", f"v{i}", i) for i in range(50)]
    for con in (sqlite_con, pg_con):
        con.executemany(
            "INSERT INTO kv (k, v, n) VALUES (?, ?, ?)", rows
        )
        con.commit()
        got = con.execute("SELECT k, v, n FROM kv ORDER BY n").fetchall()
        assert [tuple(r) for r in got] == rows


@requires_pg
def test_executemany_honours_on_conflict_do_nothing(sqlite_con, pg_con):
    for con in (sqlite_con, pg_con):
        con.execute("INSERT INTO kv (k, v, n) VALUES (?, ?, ?)", ("dup", "first", 1))
        con.commit()
        # The second row collides on the primary key and must be ignored
        # rather than raising -- this is the auction re-ingest path.
        con.executemany(
            "INSERT OR IGNORE INTO kv (k, v, n) VALUES (?, ?, ?)",
            [("dup", "second", 2), ("new", "third", 3)],
        )
        con.commit()
        rows = dict(
            (r[0], r[1]) for r in con.execute("SELECT k, v FROM kv").fetchall()
        )
        assert rows == {"dup": "first", "new": "third"}


@requires_pg
def test_executemany_of_a_complex_statement_still_works(pg_con):
    # A tuple the batching pattern cannot handle must fall back, not fail.
    pg_con.executemany(
        "INSERT INTO kv (k, v, n) VALUES (?, lower(?), ?)",
        [("a", "ABC", 1), ("b", "DEF", 2)],
    )
    pg_con.commit()
    rows = [tuple(r) for r in pg_con.execute("SELECT k, v FROM kv ORDER BY k").fetchall()]
    assert rows == [("a", "abc"), ("b", "def")]


@requires_pg
def test_executemany_of_nothing_is_a_no_op(pg_con):
    pg_con.executemany("INSERT INTO kv (k, v, n) VALUES (?, ?, ?)", [])
    assert pg_con.execute("SELECT count(*) FROM kv").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------

def test_connect_defaults_to_sqlite_so_nothing_changes_without_config(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WOW_PIPELINE_BACKEND", raising=False)
    con = connect(tmp_path / "plain.sqlite")
    assert isinstance(con, sqlite3.Connection)
    con.close()


def test_selecting_postgres_without_a_url_is_an_error_not_a_silent_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WOW_PIPELINE_BACKEND", "postgres")
    with pytest.raises(ValueError, match="requires DATABASE_URL"):
        connect(tmp_path / "x.sqlite")


@requires_pg
def test_a_database_url_selects_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("WOW_PIPELINE_BACKEND", raising=False)
    con = connect("ignored.sqlite")
    try:
        assert isinstance(con, PgConnection)
        assert con.execute("SELECT 1").fetchone()[0] == 1
    finally:
        con.close()


@requires_pg
def test_a_bad_backend_name_is_rejected(monkeypatch):
    monkeypatch.setenv("WOW_PIPELINE_BACKEND", "mysql")
    with pytest.raises(ValueError, match="not one of"):
        connect("x.sqlite")


# ---------------------------------------------------------------------------
# reading in pages
# ---------------------------------------------------------------------------
#
# A plain psycopg2 cursor buffers the whole result set client-side inside
# execute(), so a read over the 17M-row auction join allocated 3.5 GB before
# the first fetchmany() ran.  Reads therefore stream from a server-side cursor
# by default.  These tests pin the *semantics* -- which is what a caller can
# observe -- rather than the mechanism, so the implementation stays free to
# change as long as SQLite and Postgres keep agreeing.

def _seed_rows(con, n):
    con.executemany(
        "INSERT INTO kv (k, v, n) VALUES (?, ?, ?)",
        [(f"k{i:05d}", f"v{i}", i) for i in range(n)],
    )
    con.commit()


@requires_pg
def test_iterating_a_result_agrees_between_backends(sqlite_con, pg_con):
    _seed_rows(sqlite_con, 250)
    _seed_rows(pg_con, 250)
    q = "SELECT k, n FROM kv ORDER BY n"
    assert ([tuple(r) for r in sqlite_con.execute(q)]
            == [tuple(r) for r in pg_con.execute(q)])
    assert len(list(pg_con.execute(q))) == 250


@requires_pg
def test_fetchone_advances_on_both_backends(sqlite_con, pg_con):
    _seed_rows(sqlite_con, 5)
    _seed_rows(pg_con, 5)
    q = "SELECT k FROM kv ORDER BY k"
    s, p = sqlite_con.execute(q), pg_con.execute(q)
    for _ in range(5):
        assert tuple(s.fetchone()) == tuple(p.fetchone())
    assert s.fetchone() is None
    assert p.fetchone() is None


@requires_pg
def test_fetchmany_then_fetchall_returns_the_remainder_on_both_backends(
        sqlite_con, pg_con):
    _seed_rows(sqlite_con, 40)
    _seed_rows(pg_con, 40)
    q = "SELECT n FROM kv ORDER BY n"
    s, p = sqlite_con.execute(q), pg_con.execute(q)
    assert [tuple(r) for r in s.fetchmany(7)] == [tuple(r) for r in p.fetchmany(7)]
    assert ([tuple(r) for r in s.fetchall()]
            == [tuple(r) for r in p.fetchall()])


@requires_pg
def test_a_row_is_addressable_by_name_on_a_streamed_read(pg_con):
    _seed_rows(pg_con, 3)
    for row in pg_con.execute("SELECT k, n FROM kv ORDER BY n"):
        assert row["k"] == f"k{row['n']:05d}"


@requires_pg
def test_a_buffered_read_returns_the_same_rows_as_a_streamed_one(pg_con):
    _seed_rows(pg_con, 120)
    q = "SELECT k, n FROM kv ORDER BY n"
    streamed = [tuple(r) for r in pg_con.execute(q)]
    buffered = [tuple(r) for r in pg_con.execute(q, buffered=True)]
    assert streamed == buffered
    assert len(streamed) == 120


@requires_pg
def test_a_write_keeps_a_meaningful_rowcount(pg_con):
    """Writes must not be routed to a server-side cursor: rowcount is how the
    lease code detects that it won a race."""
    cur = pg_con.execute(
        "INSERT OR IGNORE INTO kv (k, v, n) VALUES (?, ?, ?)", ("a", "x", 1))
    assert cur.rowcount == 1
    cur = pg_con.execute(
        "INSERT OR IGNORE INTO kv (k, v, n) VALUES (?, ?, ?)", ("a", "x", 1))
    assert cur.rowcount == 0


@requires_pg
def test_a_streamed_read_works_on_an_autocommit_connection(pg_con):
    """The aggregate builder writes on an autocommit connection and reads on
    the same one; a named cursor there needs WITH HOLD, not an exception."""
    _seed_rows(pg_con, 10)
    pg_con.isolation_level = None
    try:
        rows = [tuple(r) for r in pg_con.execute("SELECT n FROM kv ORDER BY n")]
        assert len(rows) == 10
    finally:
        pg_con.isolation_level = ""
