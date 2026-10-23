"""Unit tests for the SQL translator. No database required.

These pin the rewrites that the Postgres backend depends on.  They are pure
string functions on purpose: a translation bug found here costs a second,
while the same bug found through the pipeline costs a corrupted database.
"""
from __future__ import annotations

import pytest

from woweconomy.dbcompat.dialect import (
    UnsupportedSQL,
    is_pragma,
    parse_pragma,
    split_statements,
    to_pyformat,
    translate,
    translate_ddl,
    translate_insert_or,
    translate_cast_types,
    translate_sqlite_master,
    translate_strftime_epoch,
)


# ---------------------------------------------------------------------------
# placeholders
# ---------------------------------------------------------------------------

def test_question_marks_become_pyformat_placeholders():
    sql, n = to_pyformat("SELECT * FROM t WHERE a = ? AND b = ?")
    assert sql == "SELECT * FROM t WHERE a = %s AND b = %s"
    assert n == 2


def test_a_literal_percent_is_escaped_because_psycopg2_scans_the_whole_string():
    sql, n = to_pyformat("SELECT * FROM t WHERE name LIKE '%dragon%' AND id = ?")
    assert sql == "SELECT * FROM t WHERE name LIKE '%%dragon%%' AND id = %s"
    assert n == 1


def test_a_question_mark_inside_a_string_literal_is_not_a_placeholder():
    sql, n = to_pyformat("SELECT * FROM t WHERE note = 'really? yes' AND id = ?")
    assert sql == "SELECT * FROM t WHERE note = 'really? yes' AND id = %s"
    assert n == 1


def test_a_doubled_quote_does_not_end_the_literal():
    sql, n = to_pyformat("SELECT * FROM t WHERE n = 'it''s ?' AND id = ?")
    assert sql == "SELECT * FROM t WHERE n = 'it''s ?' AND id = %s"
    assert n == 1


def test_no_placeholders_reports_zero():
    _, n = to_pyformat("SELECT 1")
    assert n == 0


# ---------------------------------------------------------------------------
# statement splitting
# ---------------------------------------------------------------------------

def test_script_splits_on_semicolons():
    assert split_statements("CREATE TABLE a(x); CREATE TABLE b(y);") == [
        "CREATE TABLE a(x)",
        "CREATE TABLE b(y)",
    ]


def test_a_semicolon_inside_a_literal_does_not_split():
    parts = split_statements("INSERT INTO t VALUES('a;b'); SELECT 1;")
    assert parts == ["INSERT INTO t VALUES('a;b')", "SELECT 1"]


def test_comments_are_preserved_and_not_treated_as_code():
    parts = split_statements("-- a note\nCREATE TABLE a(x);\n/* block */ SELECT 1;")
    assert len(parts) == 2
    assert parts[0].startswith("-- a note")
    assert "block" in parts[1]


# ---------------------------------------------------------------------------
# INSERT OR REPLACE / IGNORE
# ---------------------------------------------------------------------------

def test_insert_or_replace_becomes_an_upsert_on_the_primary_key():
    out = translate_insert_or(
        "INSERT OR REPLACE INTO aggregate_meta(key, value) VALUES(?,?)",
        ["key"],
    )
    # "OR REPLACE" must be gone: Postgres rejects the token outright.
    assert "OR REPLACE" not in out
    assert out == (
        'INSERT INTO aggregate_meta(key, value) VALUES(?,?)'
        ' ON CONFLICT ("key") DO UPDATE SET "value" = EXCLUDED."value"'
    )


def test_a_composite_key_is_used_in_order():
    out = translate_insert_or(
        "INSERT OR REPLACE INTO ah_realm (region, realm_slug, realm_name)"
        " VALUES (?,?,?)",
        ["region", "realm_slug"],
    )
    assert 'ON CONFLICT ("region", "realm_slug")' in out
    assert 'DO UPDATE SET "realm_name" = EXCLUDED."realm_name"' in out


def test_a_row_that_is_all_key_has_nothing_to_update():
    out = translate_insert_or(
        "INSERT OR REPLACE INTO t (a, b) VALUES (?, ?)", ["a", "b"]
    )
    assert out.endswith("DO NOTHING")


def test_insert_or_ignore_becomes_do_nothing():
    out = translate_insert_or("INSERT OR IGNORE INTO t (a) VALUES (?)", ["a"])
    assert "OR IGNORE" not in out
    assert out == "INSERT INTO t (a) VALUES (?) ON CONFLICT DO NOTHING"


def test_replace_without_a_primary_key_is_refused_rather_than_appended():
    with pytest.raises(UnsupportedSQL, match="no primary key"):
        translate_insert_or("INSERT OR REPLACE INTO t (a) VALUES (?)", [])


def test_replace_without_a_column_list_is_refused():
    with pytest.raises(UnsupportedSQL, match="column list"):
        translate_insert_or("INSERT OR REPLACE INTO t VALUES (?)", ["a"])


def test_an_existing_on_conflict_clause_is_left_alone():
    sql = ("INSERT OR REPLACE INTO t (a, b) VALUES (?, ?)"
           " ON CONFLICT (a) DO NOTHING")
    assert translate_insert_or(sql, ["a"]) == sql


def test_quoted_identifiers_are_normalised_for_the_conflict_target():
    out = translate_insert_or(
        'INSERT OR REPLACE INTO "aggregate_meta" ("key", "value") VALUES (?,?)',
        ["key"],
    )
    assert 'ON CONFLICT ("key")' in out


# ---------------------------------------------------------------------------
# sqlite_master
# ---------------------------------------------------------------------------

def test_sqlite_master_becomes_a_catalog_view():
    out = translate_sqlite_master(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    assert "information_schema.tables" in out
    # The alias has to keep the name the query asked for, and the original
    # WHERE clause has to survive after it.
    assert "AS sqlite_master WHERE type='table'" in out
    # The columns SQLite exposes must all be present.
    for column in ("AS name", "AS type", "AS tbl_name", "AS sql"):
        assert column in out


def test_sqlite_schema_is_the_same_alias():
    out = translate_sqlite_master("SELECT name FROM sqlite_schema")
    assert "information_schema.tables" in out


# ---------------------------------------------------------------------------
# strftime
# ---------------------------------------------------------------------------

def test_strftime_seconds_becomes_extract_epoch():
    out = translate_strftime_epoch("CAST(strftime('%s', ts) AS INTEGER)")
    assert out == "CAST(EXTRACT(EPOCH FROM ts) AS BIGINT)"


def test_a_bare_strftime_seconds_call_is_also_translated():
    out = translate_strftime_epoch("SELECT strftime('%s', ts) FROM t")
    assert out == "SELECT EXTRACT(EPOCH FROM ts) FROM t"


def test_an_untranslatable_strftime_format_is_refused():
    with pytest.raises(UnsupportedSQL, match="format other than"):
        translate_strftime_epoch("strftime('%Y-%m-%d', ts)")


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

def test_autoincrement_becomes_an_identity_column():
    out = translate_ddl("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    assert out == "CREATE TABLE t (id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY)"


def test_blob_and_datetime_are_renamed():
    out = translate_ddl("CREATE TABLE t (a BLOB, b DATETIME)")
    assert out == "CREATE TABLE t (a BYTEA, b TIMESTAMPTZ)"


def test_integer_widens_to_bigint_because_sqlite_integers_are_64_bit():
    # A 2.5-billion-copper price overflows a Postgres INTEGER.  This is not a
    # cosmetic rename: keeping INTEGER would reject real auction data.
    out = translate_ddl("CREATE TABLE t (unit_price_copper INTEGER NOT NULL)")
    assert out == "CREATE TABLE t (unit_price_copper BIGINT NOT NULL)"


def test_real_widens_to_double_precision():
    out = translate_ddl("CREATE TABLE t (days_left REAL NOT NULL DEFAULT 0)")
    assert out == "CREATE TABLE t (days_left DOUBLE PRECISION NOT NULL DEFAULT 0)"


def test_text_only_ddl_passes_through_unchanged():
    sql = "CREATE TABLE t (a TEXT NOT NULL, b TEXT DEFAULT '')"
    assert translate_ddl(sql) == sql


# ---------------------------------------------------------------------------
# CAST target types
# ---------------------------------------------------------------------------

def test_cast_to_real_becomes_double_precision():
    # Postgres REAL is a 4-byte float; SQLite REAL is a double.  Casting a
    # price to REAL on Postgres silently rounds it (893312600 -> 893312576),
    # which changes every derived market value.
    out = translate_cast_types("CAST(SUM(a.unit_price_copper * a.quantity) AS REAL)")
    assert out == "CAST(SUM(a.unit_price_copper * a.quantity) AS DOUBLE PRECISION)"


def test_cast_to_integer_becomes_bigint():
    out = translate_cast_types("CAST(x AS INTEGER)")
    assert out == "CAST(x AS BIGINT)"


def test_nested_parentheses_inside_a_cast_are_handled():
    out = translate_cast_types("CAST(coalesce(SUM(a * (b + 1)), 0) AS REAL) / 2")
    assert out == "CAST(coalesce(SUM(a * (b + 1)), 0) AS DOUBLE PRECISION) / 2"


def test_a_nested_cast_is_widened_too():
    out = translate_cast_types("CAST(CAST(x AS REAL) AS INTEGER)")
    assert out == "CAST(CAST(x AS DOUBLE PRECISION) AS BIGINT)"


def test_cast_to_text_is_left_alone():
    sql = "CAST(item_id AS TEXT)"
    assert translate_cast_types(sql) == sql


def test_a_column_named_like_a_type_is_not_rewritten():
    sql = "SELECT real_value FROM t WHERE integer_id = ?"
    assert translate_cast_types(sql) == sql


# ---------------------------------------------------------------------------
# PRAGMA parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql,expected",
    [
        ("PRAGMA table_info(ah_auction)", ("table_info", "ah_auction", None)),
        ("PRAGMA journal_mode=WAL", ("journal_mode", None, "WAL")),
        ("PRAGMA page_size", ("page_size", None, None)),
        ("  pragma  BUSY_TIMEOUT = 5000 ; ", ("busy_timeout", None, "5000")),
    ],
)
def test_pragma_forms_are_parsed(sql, expected):
    assert parse_pragma(sql) == expected


def test_a_select_is_not_a_pragma():
    assert not is_pragma("SELECT * FROM t")


# ---------------------------------------------------------------------------
# composite
# ---------------------------------------------------------------------------

def test_translate_applies_every_rewrite_in_one_pass():
    out = translate(
        "INSERT OR REPLACE INTO t (k, v) VALUES (?,"
        " CAST(strftime('%s', ?) AS INTEGER))",
        pk_lookup=lambda table: ["k"],
    )
    assert "ON CONFLICT" in out
    assert "EXTRACT(EPOCH FROM" in out
    assert "strftime" not in out


def test_translate_asks_for_the_primary_key_by_table_name():
    seen = []

    def lookup(table):
        seen.append(table)
        return ["k"]

    translate("INSERT OR IGNORE INTO \"Ah_Meta\" (k) VALUES (?)", pk_lookup=lookup)
    assert seen == ["Ah_Meta"]


def test_translate_without_a_lookup_refuses_insert_or():
    with pytest.raises(UnsupportedSQL, match="primary-key lookup"):
        translate("INSERT OR IGNORE INTO t (a) VALUES (?)")


def test_ordinary_dml_is_untouched():
    sql = "UPDATE t SET a = a + 1 WHERE b = ?"
    assert translate(sql, pk_lookup=lambda t: ()) == sql
