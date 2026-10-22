"""Which snapshots belong to which realm.

The auction house is shared per **connected realm**.  ``Dragonmaw``,
``Haomarush``, ``Spinebreaker``, ``Stormreaver`` and ``Vashj`` are one
market with one book (id 3656); the API has no per-realm auction endpoint at
all.  Two consequences are pinned here:

* a snapshot must be found through *any* realm in its group, not only the
  name it happened to be stored under -- otherwise a player on ``Vashj``
  sees an empty market that was scanned every hour;
* a sweep of "all realms" must make 92 requests (one per book), not 267
  (one per realm name), and must survive one book failing.

The resolver is the single implementation every consumer uses
(``latest_snapshot``, ``snapshots_between``, ``market_context``,
``realm_index``, ``ah_undercut``, ``watch.detect_resets``), so these tests
guard the whole family rather than one call site.
"""
from __future__ import annotations

import sqlite3

import pytest

from woweconomy.intel import ah, store

#: cr 3656 really is these five realms on EU; cr 3391 is Silvermoon alone.
CONNECTED = [
    ("eu", "dragonmaw", "Dragonmaw", 3656),
    ("eu", "haomarush", "Haomarush", 3656),
    ("eu", "spinebreaker", "Spinebreaker", 3656),
    ("eu", "stormreaver", "Stormreaver", 3656),
    ("eu", "vashj", "Vashj", 3656),
    ("eu", "silvermoon", "Silvermoon", 3391),
    ("eu", "garona", "Garona", 509),
    ("eu", "nerzhul", "Ner'zhul", 509),
]

PAYLOAD = {"auctions": [
    {"id": 11, "item": {"id": 19019, "context": 0}, "quantity": 1,
     "buyout": 500_000, "bid": 400_000, "time_left": "SHORT"},
    {"id": 12, "item": {"id": 19020, "context": 0}, "quantity": 20,
     "buyout": 2_000_000, "bid": 0, "time_left": "VERY_LONG"},
]}


def _db(tmp_path, *, realms=True):
    con = store.connect(tmp_path / "intel.sqlite")
    if realms:
        con.executemany(
            "INSERT OR REPLACE INTO ah_realm (region, realm_slug, realm_name,"
            " connected_realm_id, refreshed_at) VALUES (?,?,?,?,'')", CONNECTED)
        con.commit()
    return con


def _ingest(con, *, realm, cr_id, at=1_792_500_000):
    parsed = ah.parse_payload(PAYLOAD, region="eu", realm=realm,
                              realm_slug=realm.lower(), connected_realm_id=cr_id,
                              captured_at=at, source="blizzard_api",
                              api_last_modified="")
    return ah.ingest_bundle(con, parsed)


# -- the resolver ----------------------------------------------------------

def test_a_member_realm_finds_its_connected_realms_snapshot(tmp_path):
    """The bug this was written for: five realms, one book, one name."""
    con = _db(tmp_path)
    _ingest(con, realm="Dragonmaw", cr_id=3656)
    for member in ("Dragonmaw", "Haomarush", "Spinebreaker", "Stormreaver", "Vashj"):
        found = ah.latest_snapshot(con, "eu", member)
        assert found is not None, f"{member} could not see its own market"
        assert found["connected_realm_id"] == 3656


def test_another_connected_realm_is_not_matched(tmp_path):
    """Resolution must not leak across groups."""
    con = _db(tmp_path)
    _ingest(con, realm="Dragonmaw", cr_id=3656)
    assert ah.latest_snapshot(con, "eu", "Silvermoon") is None


def test_a_realm_with_no_mapping_still_matches_by_name(tmp_path):
    """Snapshots written before ``ah_realm`` existed must stay visible.

    Matching only on ``connected_realm_id`` would be tidier and would hide
    every historical row, so the name is kept as an alternative.
    """
    con = _db(tmp_path, realms=False)
    _ingest(con, realm="Kazzak", cr_id=1305)
    found = ah.latest_snapshot(con, "eu", "Kazzak")
    assert found is not None
    assert found["realm"] == "Kazzak"


def test_the_resolver_degrades_when_there_is_no_realm_table():
    """A database without ``ah_realm`` is a name-only database, not an error."""
    con = sqlite3.connect(":memory:")
    where, params = ah.realm_match(con, "Dragonmaw")
    assert where == "LOWER(realm) = LOWER(?)"
    assert params == ["Dragonmaw"]


def test_the_resolver_matches_the_slug_as_well_as_the_name(tmp_path):
    con = _db(tmp_path)
    where, params = ah.realm_match(con, "vashj")
    assert 3656 in params


def test_snapshots_between_uses_the_same_resolution(tmp_path):
    con = _db(tmp_path)
    _ingest(con, realm="Dragonmaw", cr_id=3656, at=1_000)
    rows = ah.snapshots_between(con, "eu", "Haomarush", 0, 2_000)
    assert len(rows) == 1


def test_market_context_reads_through_a_member_realm(tmp_path):
    """The consumer, not just the helper: a pet market is not 'unknown'."""
    from woweconomy.ml import market_context

    con = _db(tmp_path)
    _ingest(con, realm="Dragonmaw", cr_id=3656)
    heads = market_context._realm_snapshots(con, "Vashj", limit=2)
    assert len(heads) == 1


# -- the sweep -------------------------------------------------------------

class FakeClient:
    """Records the books asked for; returns a payload unless told to fail."""

    region = "eu"

    def __init__(self, *, fail=(), unchanged=(), payload=None):
        self.calls = []
        self.fail = set(fail)
        self.unchanged = set(unchanged)
        self.payload = payload or PAYLOAD

    def fetch_auctions(self, cr_id, modified_since=""):
        self.calls.append((cr_id, modified_since))
        if cr_id in self.fail:
            raise RuntimeError("blizzard said no")
        if cr_id in self.unchanged:
            return None, modified_since
        return self.payload, "Thu, 22 Oct 2026 20:00:00 GMT"


def _run(monkeypatch, tmp_path, client, argv):
    from scripts import intel

    monkeypatch.setattr(ah, "AuctionHouseClient", lambda **kw: client)
    args = intel.build_parser().parse_args(
        ["--region", "eu", "--db", str(tmp_path / "intel.sqlite"),
         "ah-fetch", *argv])
    return args.func(args)


def _seed_realms(tmp_path):
    con = _db(tmp_path)
    con.close()


def test_all_realms_makes_one_request_per_book_not_per_realm(monkeypatch, tmp_path):
    """8 realm names are 3 books; asking 8 times would be 8 identical downloads."""
    _seed_realms(tmp_path)
    client = FakeClient()
    code = _run(monkeypatch, tmp_path, client, ["--all-realms"])
    assert code == 0
    assert [c[0] for c in client.calls] == [509, 3391, 3656]


def test_all_realms_stores_one_snapshot_per_book(monkeypatch, tmp_path):
    _seed_realms(tmp_path)
    _run(monkeypatch, tmp_path, FakeClient(), ["--all-realms"])
    con = sqlite3.connect(tmp_path / "intel.sqlite")
    rows = con.execute(
        "SELECT connected_realm_id, realm FROM ah_snapshot"
        " WHERE realm <> '' ORDER BY connected_realm_id").fetchall()
    assert [r[0] for r in rows] == [509, 3391, 3656]
    # the representative is the alphabetically first member, deterministically
    assert [r[1] for r in rows] == ["Garona", "Silvermoon", "Dragonmaw"]


def test_a_failed_book_does_not_abort_the_sweep(monkeypatch, tmp_path):
    """91 good books are still worth having; the exit code reports the loss."""
    _seed_realms(tmp_path)
    client = FakeClient(fail={3391})
    code = _run(monkeypatch, tmp_path, client, ["--all-realms"])
    assert code == 1
    assert len(client.calls) == 3
    con = sqlite3.connect(tmp_path / "intel.sqlite")
    got = {r[0] for r in con.execute(
        "SELECT connected_realm_id FROM ah_snapshot WHERE realm <> ''")}
    assert got == {509, 3656}


def test_an_unchanged_book_writes_nothing(monkeypatch, tmp_path):
    _seed_realms(tmp_path)
    client = FakeClient(unchanged={509, 3391, 3656})
    code = _run(monkeypatch, tmp_path, client, ["--all-realms"])
    assert code == 0
    con = sqlite3.connect(tmp_path / "intel.sqlite")
    assert con.execute("SELECT COUNT(*) FROM ah_snapshot").fetchone()[0] == 0


def test_the_second_sweep_asks_conditionally(monkeypatch, tmp_path):
    """Without this every hourly run downloads 92 full books for nothing."""
    _seed_realms(tmp_path)
    _run(monkeypatch, tmp_path, FakeClient(), ["--all-realms"])
    client = FakeClient(unchanged={509, 3391, 3656})
    _run(monkeypatch, tmp_path, client, ["--all-realms"])
    assert all(since == "Thu, 22 Oct 2026 20:00:00 GMT"
               for _cr, since in client.calls)


def test_limit_stops_early_for_a_smoke_run(monkeypatch, tmp_path):
    _seed_realms(tmp_path)
    client = FakeClient()
    _run(monkeypatch, tmp_path, client, ["--all-realms", "--limit", "2"])
    assert len(client.calls) == 2


def test_all_realms_refuses_an_empty_realm_table(monkeypatch, tmp_path):
    con = _db(tmp_path, realms=False)
    con.close()
    code = _run(monkeypatch, tmp_path, FakeClient(), ["--all-realms"])
    assert code == 2
