"""Tests for the TSM public-CSV snapshot store and the hunger join.

Fixtures only: small CSVs, a throwaway sqlite, no network and no real realm
index.  The point is that a connected group is downloaded once, an unchanged
file is skipped, and a price change is recorded as a single delta.
"""
import json
import sqlite3

import numpy as np
import pytest

from woweconomy.analytics.hungry_buyers import (
    compute_hunger, price_of, weighted_median,
)
from woweconomy.services import tsm_snapshots as ts


def realm_json(tmp_path):
    path = tmp_path / "realms.json"
    path.write_text(json.dumps({
        "realms": [
            {"id": 1, "slug": "draenor", "name": "Draenor"},
            {"id": 2, "slug": "kazzak", "name": "Kazzak"},
            {"id": 3, "slug": "silvermoon", "name": "Silvermoon"},
            {"id": 4, "slug": "aggra-português", "name": "Aggra (Português)"},
        ],
        "connected_realms": [
            {"id": 10, "members": [
                {"id": 1, "slug": "draenor", "name": "Draenor"},
                {"id": 2, "slug": "kazzak", "name": "Kazzak"},
            ]},
            {"id": 11, "members": [
                {"id": 3, "slug": "silvermoon", "name": "Silvermoon"},
            ]},
            {"id": 12, "members": [
                {"id": 4, "slug": "aggra-português", "name": "Aggra (Português)"},
            ]},
        ],
    }), encoding="utf-8")
    return path


@pytest.fixture()
def con():
    c = sqlite3.connect(":memory:")
    ts.ensure_schema(c)
    yield c
    c.close()


def test_connected_group_is_one_market(tmp_path):
    realm_map = ts.load_realm_map(realm_json(tmp_path))
    assert len(realm_map.canonical_groups) == 3
    group = realm_map.resolve("Kazzak")
    assert group.canonical_slug == "draenor"
    assert group.member_slugs == ("draenor", "kazzak")

    tasks = ts.build_tasks(realm_map, ["realm_items"],
                           realms=["kazzak", "draenor"], include_region=False)
    assert len(tasks) == 1
    assert tasks[0].scope == "draenor"
    assert tasks[0].url.endswith("/realm/draenor/items.csv")


def test_accented_slug_is_percent_encoded(tmp_path):
    realm_map = ts.load_realm_map(realm_json(tmp_path))
    tasks = ts.build_tasks(realm_map, ["realm_items"], realms=["Aggra (Português)"],
                           include_region=False)
    assert tasks[0].url.endswith("aggra-portugu%C3%AAs/items.csv")


def test_snapshot_dedupe_and_delta(con):
    task = ts.Task(scope="draenor", kind="realm_items",
                   url="https://example.invalid/items.csv")
    first = (b"itemId,name,marketValue,minBuyout,recent,historical,updatedAt\n"
             b"1,Alpha,1000,900,950,800,2026-10-18T06:00:00Z\n"
             b"2,Beta,2000,0,1900,1800,2026-10-18T06:00:00Z\n")
    r1 = ts.import_content(con, task, first, "2026-10-18T07:00:00Z", "/tmp/x",
                           write_file=False)
    assert (r1.rows, r1.changed, r1.skipped) == (2, 2, False)
    assert con.execute("SELECT COUNT(*) FROM tsm_snapshot").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM tsm_price_change").fetchone()[0] == 2
    assert con.execute("SELECT market_value, min_buyout FROM tsm_realm_price "
                       "WHERE scope='draenor' AND item_id=1").fetchone() == (1000, 900)

    # Same bytes -> no second snapshot, no extra history.
    r2 = ts.import_content(con, task, first, "2026-10-18T08:00:00Z", "/tmp/x",
                           write_file=False)
    assert r2.skipped
    assert con.execute("SELECT COUNT(*) FROM tsm_snapshot").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM tsm_price_change").fetchone()[0] == 2

    # One changed price -> one delta, and the change row carries the right column.
    second = first.replace(b"1000,900", b"1100,950")
    r3 = ts.import_content(con, task, second, "2026-10-18T09:00:00Z", "/tmp/x",
                           write_file=False)
    assert (r3.rows, r3.changed) == (2, 1)
    assert con.execute("SELECT COUNT(*) FROM tsm_snapshot").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM tsm_price_change").fetchone()[0] == 3
    row = con.execute(
        "SELECT kind, market_value, min_buyout, recent, historical, "
        "avg_sale_price FROM tsm_price_change ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    assert row == ("realm_items", 1100, 950, 950, 800, 0)


def test_pets_use_species_key(con):
    task = ts.Task(scope="draenor", kind="realm_pets",
                   url="https://example.invalid/pets.csv")
    body = (b"petSpeciesId,name,marketValue,minBuyout,recent,historical,updatedAt\n"
            b"1656,Young Talbuk,500,400,450,300,2026-10-18T06:00:00Z\n")
    result = ts.import_content(con, task, body, "2026-10-18T07:00:00Z", "/tmp/x",
                               write_file=False)
    assert result.rows == 1
    assert con.execute("SELECT name FROM tsm_realm_pet_price "
                       "WHERE scope='draenor' AND pet_species_id=1656").fetchone()[0] \
        == "Young Talbuk"


def test_region_file_columns(con):
    task = ts.Task(scope="region", kind="region_items",
                   url="https://example.invalid/region.csv")
    body = (b"itemId,name,marketValue,historical,avgSalePrice,saleRate,"
            b"soldPerDay,updatedAt\n"
            b"194,Tattered Cloth Pants,4000,3000,2500,0.011,0.125,"
            b"2026-10-17T00:00:00Z\n")
    result = ts.import_content(con, task, body, "2026-10-18T07:00:00Z", "/tmp/x",
                               write_file=False)
    assert result.rows == 1
    assert con.execute("SELECT market_value, avg_sale_price, sale_rate, "
                       "sold_per_day FROM tsm_region_price WHERE key_id=194"
                       ).fetchone() == (4000, 2500, 0.011, 0.125)


def test_price_of_falls_back_in_order():
    assert price_of((1000, 900), 800) == 900       # cheapest listing wins
    assert price_of((1000, 0), 800) == 1000        # no listing -> market value
    assert price_of((0, 0), 800) == 800            # nothing local -> region
    assert price_of(None, 0) == 0


def test_weighted_median():
    # Two equal weights: the lower middle is the conservative choice.
    assert weighted_median([1, 2], [1, 1]) == 1
    assert weighted_median([1, 2], [9, 1]) == 1
    assert weighted_median([1, 2], [1, 9]) == 2
    assert weighted_median([], []) is None


def test_hunger_math():
    row = compute_hunger(
        realm="Draenor", canonical_slug="draenor",
        counts=np.array([2, 0, 3], dtype=np.int64),
        item_ids=[1, 2, 3],
        realm_prices={1: (1000, 500), 3: (0, 0)},
        region_prices={1: 800, 3: 2000},
        buyers=2)
    assert row.gap_items == 5
    assert row.gap_gold == 2 * 500 + 3 * 2000
    assert row.priced_items == 2
    assert row.wanted_items == 2
    assert row.items_per_buyer == 2.5
    assert row.gold_per_buyer == 0.35
    assert row.price_index == 1.25  # only item 1 has both realm and region
    assert row.coverage == 1.0


def test_order_for_resume_puts_the_missing_files_first(con):
    """Um run que pare a meio tem de avançar, não repetir a mesma cabeça."""
    con.executemany(
        "INSERT INTO tsm_snapshot(scope, kind, updated_at, fetched_at, checksum, "
        "rows, changed, path) VALUES(?,?,?,?,?,?,?,?)",
        [("draenor", "realm_items", "t", "2026-10-18T01:00:00Z", "aaa", 1, 0, ""),
         ("kazzak", "realm_items", "t", "2026-10-18T05:00:00Z", "bbb", 1, 0, "")])
    con.commit()
    tasks = [ts.Task(scope=s, kind="realm_items", url=f"u/{s}")
             for s in ("silvermoon", "kazzak", "draenor")]
    assert [t.scope for t in ts.order_for_resume(con, tasks)] == \
        ["silvermoon", "draenor", "kazzak"]


def test_a_429_records_a_cooldown_that_blocks_the_next_run(con):
    assert ts.cooldown_until(con) == 0.0
    ts._set_cooldown(con, 3600)
    assert ts.cooldown_until(con) > 3500
    assert ts.cooldown_until(con, now=__import__("time").time() + 7200) == 0.0


def test_conditional_get_sends_the_stored_etag_and_accepts_304(monkeypatch):
    import requests

    seen = {}

    class Resp:
        status_code = 304
        headers = {"ETag": '"abc"'}
        content = b""

        def raise_for_status(self):
            raise AssertionError("304 não deve chegar aqui")

    def fake_get(url, timeout=None, headers=None):
        seen.update(headers or {})
        seen["url"] = url
        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    task = ts.Task(scope="draenor", kind="realm_items", url="https://x/items.csv")
    dl = ts.download(task, etag='"abc"', last_modified="Sun, 18 Oct 2026 16:49:01 GMT")
    assert dl.not_modified and dl.error == "" and dl.status == 304
    assert seen["If-None-Match"] == '"abc"'
    assert seen["If-Modified-Since"] == "Sun, 18 Oct 2026 16:49:01 GMT"


def test_a_long_429_aborts_and_writes_the_cooldown(con, monkeypatch):
    import requests

    class Resp:
        status_code = 429
        headers = {"Retry-After": "3600"}
        content = b""

        def raise_for_status(self):
            raise AssertionError("429 não deve chegar aqui")

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    task = ts.Task(scope="draenor", kind="realm_items", url="https://x/items.csv")
    stats = ts.sync(con, [task], "/tmp/x", workers=1, delay=0)
    assert stats.failed == 1 and stats.aborted
    assert "429" in stats.errors[0]
    assert ts.cooldown_until(con) > 3500          # a próxima passagem não tenta nada


# ----------------------------------------------------------------------
# the store is whichever backend is configured
# ----------------------------------------------------------------------


def test_the_sweep_writes_to_the_configured_backend(tmp_path):
    """The sweep used to write a local tsm_markets.db unconditionally.

    Everything else reads the shared database, so a sweep that wrote a local
    file would be the split brain this project is removing -- the fetch would
    look like it worked and nothing downstream would see the data.
    """
    import woweconomy.services.tsm_snapshots as module

    path = tmp_path / "store.db"
    con = module.connect(path)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "tsm_snapshot" in names
        # the SQLite-only tuning must not be sent to a backend that has no
        # such pragma; on SQLite it is what makes a concurrent read work
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()


def test_an_insert_gets_the_key_the_database_generated(tmp_path):
    """SQLite's lastrowid has no Postgres equivalent; the helper covers both."""
    from woweconomy.dbcompat import insert_returning_id

    path = tmp_path / "store.db"
    con = ts.connect(path)
    try:
        first = insert_returning_id(
            con,
            "INSERT INTO tsm_snapshot(scope, kind, updated_at, fetched_at,"
            " checksum, rows, changed, path, etag, last_modified)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("a", "realm_items", "t", "t", "c1", 1, 0, "", "", ""),
            column="snapshot_id")
        second = insert_returning_id(
            con,
            "INSERT INTO tsm_snapshot(scope, kind, updated_at, fetched_at,"
            " checksum, rows, changed, path, etag, last_modified)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("b", "realm_items", "t", "t", "c2", 1, 0, "", "", ""),
            column="snapshot_id")
        assert first > 0
        assert second == first + 1
        stored = {r[0] for r in con.execute(
            "SELECT snapshot_id FROM tsm_snapshot")}
        assert stored == {first, second}
    finally:
        con.close()
