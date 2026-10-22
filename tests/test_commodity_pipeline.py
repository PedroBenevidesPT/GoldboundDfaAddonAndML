"""Commodity fetching and price validation.

The commodity endpoint is a different resource from the per-realm auction
endpoint, and the difference is not cosmetic:

* it is **region-wide** -- there is no connected realm in the path, so one
  book serves every realm;
* its rows are wrapped in ``auctions``, not ``commodities``;
* every row is a stack of one item at one unit price, so ``unit_price`` is
  always present and always the price of a single unit.

The fixture below is a verbatim excerpt of a live EU response captured on
2026-10-22, so a future change to the payload shape fails here rather than
silently producing an empty snapshot.
"""
import json
import sqlite3
import types

import pytest

from woweconomy.intel import ah, prices, store

#: Verbatim shape from GET /data/wow/auctions/commodities (EU, 2026-10-22).
COMMODITY_PAYLOAD = {
    "_links": {"self": {"href": "https://eu.api.blizzard.com/data/wow/auctions/commodities"}},
    "auctions": [
        {"id": 1098316520, "item": {"id": 237371}, "quantity": 8,
         "unit_price": 899900, "time_left": "MEDIUM"},
        {"id": 1098362588, "item": {"id": 13467}, "quantity": 99,
         "unit_price": 98800, "time_left": "MEDIUM"},
        {"id": 1098270822, "item": {"id": 245843}, "quantity": 1,
         "unit_price": 1308900, "time_left": "MEDIUM"},
        {"id": 1098123456, "item": {"id": 3820}, "quantity": 20,
         "unit_price": 4100, "time_left": "SHORT"},
    ],
}

REALM_PAYLOAD = {
    "auctions": [
        {"id": 11, "item": {"id": 19019, "context": 0}, "quantity": 1,
         "buyout": 500_000, "bid": 400_000, "time_left": "SHORT"},
    ]
}


def commodity_bundle(**kwargs):
    params = dict(region="eu", captured_at=1_792_500_000)
    params.update(kwargs)
    return ah.parse_commodities(COMMODITY_PAYLOAD, **params)


# ---------------------------------------------------------------------------
# Phase 1.1 -- the payload semantics
# ---------------------------------------------------------------------------

def test_the_commodity_payload_is_wrapped_in_auctions_and_reads_item_id():
    parsed = commodity_bundle()
    assert len(parsed.auctions) == 4
    assert parsed.rejected == []
    assert {a.item_id for a in parsed.auctions} == {237371, 13467, 245843, 3820}


def test_every_commodity_row_is_marked_commodity_and_keeps_its_unit_price():
    parsed = commodity_bundle()
    first = next(a for a in parsed.auctions if a.item_id == 237371)
    assert first.is_commodity is True
    assert first.unit_price_copper == 899_900
    assert first.unit_price_source == "api_unit_price"
    assert first.time_left_band == "MEDIUM"
    assert parsed.commodity_count == 4
    assert parsed.auction_count == 0


def test_the_commodity_book_is_stored_region_wide_not_against_a_realm():
    con = store.memory_connection()
    parsed = commodity_bundle()
    ah.ingest_bundle(con, parsed)

    snap = con.execute(
        "SELECT realm, realm_slug, connected_realm_id, commodity_count,"
        " auction_count FROM ah_snapshot WHERE snapshot_id = ?",
        (parsed.snapshot_id,)).fetchone()
    assert snap["realm"] == ah.REGION_WIDE_REALM
    assert snap["realm_slug"] == "commodities"
    assert snap["connected_realm_id"] is None
    assert snap["commodity_count"] == 4
    assert snap["auction_count"] == 0


def test_a_commodity_snapshot_never_appears_in_a_realm_query():
    con = store.memory_connection()
    realm_bundle = ah.parse_payload(REALM_PAYLOAD, region="eu", realm="Kazzak",
                                    realm_slug="kazzak", connected_realm_id=1099,
                                    captured_at=1_792_500_000)
    ah.ingest_bundle(con, realm_bundle)
    ah.ingest_bundle(con, commodity_bundle())

    latest = ah.latest_snapshot(con, "eu", "kazzak")
    assert latest["snapshot_id"] == realm_bundle.snapshot_id
    realm_rows = con.execute(
        "SELECT COUNT(*) FROM ah_auction WHERE snapshot_id = ?",
        (latest["snapshot_id"],)).fetchone()[0]
    assert realm_rows == 1
    # and the commodity rows are still there, under the region scope
    assert con.execute("SELECT COUNT(*) FROM ah_auction").fetchone()[0] == 5


def test_an_empty_commodity_book_is_rejected_by_the_cli(monkeypatch, tmp_path):
    """A 200 with no rows is a failed fetch, not a market with nothing for sale."""
    from scripts import intel

    class FakeClient:
        region = "eu"

        def __init__(self, *a, **k):
            pass

        def fetch_commodities(self, modified_since=""):
            return {"auctions": []}, ""

    monkeypatch.setattr(ah, "AuctionHouseClient", FakeClient)
    db = tmp_path / "intel.sqlite"

    args = types.SimpleNamespace(
        db=str(db), region="eu", realm="", realm_slug="",
        connected_realm_id=None, modified_since="", sync_realms=False,
        commodities=False, commodities_only=True, commodities_since="")

    assert intel.cmd_ah_fetch(args) == 4
    con = store.connect(db)
    assert con.execute("SELECT COUNT(*) FROM ah_snapshot").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Phase 1.1 -- the watcher must actually ask for it
# ---------------------------------------------------------------------------

def test_the_watcher_requests_the_commodity_book_once_per_tick(tmp_path):
    from scripts import watch

    db = tmp_path / "intel.sqlite"
    con = store.connect(db)
    con.execute("INSERT INTO ah_realm (region, realm_slug, realm_name,"
                " connected_realm_id) VALUES ('eu','kazzak','Kazzak',1099)")
    con.commit()
    con.close()

    runner = FakeRunner({"snapshot_id": "s1", "auctions": 0})
    cfg = watch.Config(dry_run=True, realms=["Kazzak"], intel_db=str(db),
                       state_path=tmp_path / "state.json",
                       log_path=tmp_path / "watch.log")
    daemon = watch.Watch(cfg, runner=runner)
    daemon.task_ah(1_792_500_000)

    commodity_calls = [c for c in runner.calls if "--commodities-only" in c]
    assert len(commodity_calls) == 1, runner.calls
    idx = commodity_calls[0].index("--commodities-only")
    assert commodity_calls[0][idx - 1] == ""
    assert "--region" in commodity_calls[0]
    # and the realm scan still happened
    assert any("--realm" in c and "Kazzak" in c for c in runner.calls)


def test_every_argv_the_watcher_builds_parses(tmp_path):
    """A fake runner cannot catch a flag in the wrong position.

    ``--region`` is a global flag of intel.py and must precede the
    subcommand.  The watcher builds its argv as a list of strings, so a
    misplaced flag is accepted by the fake runner and rejected by the real
    parser with exit code 2 -- which is exactly what happened: the
    commodity fetch failed on its first production run.  Parsing the
    recorded argv through the real parser is the test that catches it.
    """
    from scripts import intel, watch

    db = tmp_path / "intel.sqlite"
    con = store.connect(db)
    con.execute("INSERT INTO ah_realm (region, realm_slug, realm_name,"
                " connected_realm_id) VALUES ('eu','kazzak','Kazzak',1099)")
    con.commit()
    con.close()

    runner = FakeRunner({"snapshot_id": "s1", "auctions": 0})
    cfg = watch.Config(dry_run=True, realms=["Kazzak"], intel_db=str(db),
                       state_path=tmp_path / "state.json",
                       log_path=tmp_path / "watch.log")
    daemon = watch.Watch(cfg, runner=runner)
    daemon.task_ah(1_792_500_000)

    parser = intel.build_parser()
    intel_calls = [c for c in runner.calls if any("intel.py" in part for part in c)]
    assert intel_calls, runner.calls
    for call in intel_calls:
        index = next(i for i, part in enumerate(call) if part.endswith("intel.py"))
        parsed = parser.parse_args(call[index + 1:])
        assert parsed.command == "ah-fetch"

    commodity = next(c for c in intel_calls if "--commodities-only" in c)
    parsed = parser.parse_args(commodity[index + 1:])
    assert parsed.region == "eu"
    assert parsed.realm == ""
    assert parsed.commodities_only is True


class FakeRunner:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {}

    def __call__(self, argv, timeout=1800):
        self.calls.append(list(argv))
        return {"returncode": 0, "payload": self.payload, "stdout": "", "stderr": ""}


# ---------------------------------------------------------------------------
# Phase 1.3 -- price validation
# ---------------------------------------------------------------------------

def test_the_sentinel_is_not_a_price():
    verdict = prices.classify_static(prices.SENTINEL_COPPER)
    assert verdict.status == prices.SENTINEL
    assert verdict.usable is False


def test_the_maximum_buyout_is_not_a_price_either():
    """9,999,999g 99s is what the client accepts, and what parkers use.

    It is 900 copper above the constant, so an exact-match rule let all
    2,749 of the live rows through as ordinary prices.
    """
    assert prices.classify_static(prices.SENTINEL_COPPER + 900).status == prices.SENTINEL
    assert prices.classify_static(prices.SENTINEL_COPPER - 1).status == prices.OK


@pytest.mark.parametrize("value", [0, -1, prices.GOLD_CAP_COPPER + 1])
def test_impossible_values_are_flagged(value):
    assert prices.classify_static(value).status == prices.IMPOSSIBLE


def test_a_missing_price_stays_missing_and_is_never_zero():
    assert prices.classify_static(None).status == prices.MISSING


def test_a_normal_price_is_ok():
    assert prices.classify_static(150_000).status == prices.OK


def test_a_gold_cap_listing_is_kept_but_flagged_as_an_outlier():
    con = store.memory_connection()
    payload = {"auctions": [
        {"id": i, "item": {"id": 82800}, "quantity": 1,
         "buyout": 2_000 + i, "time_left": "SHORT"} for i in range(10)
    ] + [{"id": 99, "item": {"id": 82800}, "quantity": 1,
          "buyout": 9_000_000 * prices.COPPER_PER_GOLD, "time_left": "SHORT"}]}
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak",
                              captured_at=1_792_500_000)
    ah.ingest_bundle(con, parsed)

    row = con.execute(
        "SELECT unit_price_copper, price_status, price_reason FROM ah_auction"
        " WHERE snapshot_id = ? AND auction_id = 99",
        (parsed.snapshot_id,)).fetchone()
    # raw observation preserved ...
    assert row["unit_price_copper"] == 9_000_000 * prices.COPPER_PER_GOLD
    # ... and labelled unusable for aggregation
    assert row["price_status"] == prices.OUTLIER
    assert "median" in row["price_reason"]
    assert con.execute(
        "SELECT COUNT(*) FROM ah_auction WHERE snapshot_id = ? AND price_status = ?",
        (parsed.snapshot_id, prices.OK)).fetchone()[0] == 10


def test_outlier_marking_is_idempotent():
    con = store.memory_connection()
    payload = {"auctions": [
        {"id": i, "item": {"id": 100}, "quantity": 1,
         "buyout": 1_000 + i, "time_left": "SHORT"} for i in range(5)
    ] + [{"id": 77, "item": {"id": 100}, "quantity": 1,
          "buyout": 50_000_000_000, "time_left": "SHORT"}]}
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak",
                              captured_at=1_792_500_000)
    ah.ingest_bundle(con, parsed)
    first = prices.status_counts(con, parsed.snapshot_id)
    assert prices.mark_outliers(con, parsed.snapshot_id) == 0
    assert prices.status_counts(con, parsed.snapshot_id) == first


def test_a_legacy_database_is_migrated_without_losing_a_row(tmp_path):
    """The additive migration must work on a database that predates it.

    This is the only path that can break a live install: ``CREATE TABLE IF
    NOT EXISTS`` does not add a column to an existing table, so an index
    on a new column, created before the migration, raises "no such
    column".  The columns are therefore added first and indexed after.
    """
    path = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE ah_snapshot (
            snapshot_id TEXT PRIMARY KEY, region TEXT, connected_realm_id INTEGER,
            realm TEXT, realm_slug TEXT, captured_at INTEGER, api_last_modified TEXT,
            source TEXT, payload_sha256 TEXT, auction_count INTEGER,
            commodity_count INTEGER, rejected_rows INTEGER, imported_at TEXT);
        CREATE TABLE ah_auction (
            snapshot_id TEXT NOT NULL, auction_key TEXT NOT NULL, auction_id INTEGER,
            item_id INTEGER NOT NULL, item_context INTEGER, quantity INTEGER NOT NULL,
            unit_price_copper INTEGER, buyout_copper INTEGER, bid_copper INTEGER,
            total_price_copper INTEGER, unit_price_source TEXT NOT NULL DEFAULT '',
            time_left_raw TEXT, time_left_band TEXT,
            is_commodity INTEGER NOT NULL DEFAULT 0, pet_species_id INTEGER,
            bonus_lists TEXT, modifiers TEXT,
            PRIMARY KEY (snapshot_id, auction_key));
        INSERT INTO ah_snapshot VALUES ('s1','eu',1,'Kazzak','',1000,'','blizzard_api',
            'h',1,0,0,'');
        INSERT INTO ah_auction (snapshot_id, auction_key, auction_id, item_id,
            quantity, unit_price_copper) VALUES ('s1','a1',1,2589,20,1500);
    """)
    con.commit()
    con.close()

    migrated = store.connect(path)

    columns = {row[1] for row in migrated.execute("PRAGMA table_info(ah_auction)")}
    assert {"price_status", "price_reason", "variant_key"} <= columns
    row = migrated.execute(
        "SELECT unit_price_copper, price_status, variant_key FROM ah_auction"
    ).fetchone()
    assert row["unit_price_copper"] == 1500          # raw observation intact
    assert row["price_status"] == prices.MISSING     # not assumed valid
    assert row["variant_key"] is None                # not computed yet
    indexes = {row[1] for row in migrated.execute(
        "SELECT * FROM sqlite_master WHERE type = 'index'")}
    assert "idx_ah_auction_variant" in indexes
    assert "idx_ah_auction_price_status" in indexes
    migrated.close()


def test_a_price_with_no_comparison_is_not_called_an_outlier():
    assert prices.classify_against_median(50_000_000, None).status == prices.OK


def test_a_stale_outlier_flag_is_cleared_when_the_group_is_too_small():
    """A verdict this pass cannot justify must not survive it.

    A group of two has no median, so the pass declines to judge it -- and
    declining must mean "restore the static verdict", not "leave whatever
    an earlier pass wrote".  Otherwise a flag written by a differently
    scoped run is permanent.
    """
    con = store.memory_connection()
    payload = {"auctions": [
        {"id": 1, "item": {"id": 500}, "quantity": 1, "buyout": 1_000,
         "time_left": "SHORT"},
        {"id": 2, "item": {"id": 500}, "quantity": 1, "buyout": 1_000,
         "time_left": "SHORT"},
    ]}
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak",
                              captured_at=1_792_500_000)
    ah.ingest_bundle(con, parsed)
    con.execute("UPDATE ah_auction SET price_status = 'outlier',"
                " price_reason = 'stale' WHERE snapshot_id = ?",
                (parsed.snapshot_id,))
    con.commit()

    assert prices.mark_outliers(con, parsed.snapshot_id) == 2
    assert con.execute(
        "SELECT COUNT(*) FROM ah_auction WHERE snapshot_id = ? AND price_status = ?",
        (parsed.snapshot_id, prices.OUTLIER)).fetchone()[0] == 0


def test_an_outlier_verdict_does_not_spill_onto_another_variant_at_the_same_price():
    """Two variants of one item can share a price and not share a verdict.

    The status is per row, so the write must be keyed by the row.  A
    predicate on ``(item_id, price)`` would flag the normal species too,
    because it happens to be listed at the same number -- and that row
    then leaves its own variant's reference pool, which is how a single
    pass started flagging a few more rows on every run.
    """
    con = store.memory_connection()
    cheap = [{"id": 100 + i, "item": {"id": 82800, "pet_species_id": 162},
              "quantity": 1, "buyout": 100, "time_left": "SHORT"}
             for i in range(3)]
    absurd = [{"id": 200, "item": {"id": 82800, "pet_species_id": 162},
               "quantity": 1, "buyout": 50_000_000_000, "time_left": "SHORT"}]
    dear = [{"id": 300 + i, "item": {"id": 82800, "pet_species_id": 2908},
             "quantity": 1, "buyout": 50_000_000_000, "time_left": "SHORT"}
            for i in range(3)]
    parsed = ah.parse_payload({"auctions": cheap + absurd + dear}, region="eu",
                              realm="Kazzak", captured_at=1_792_500_000)
    ah.ingest_bundle(con, parsed)

    statuses = dict(con.execute(
        "SELECT auction_key, price_status FROM ah_auction WHERE snapshot_id = ?",
        (parsed.snapshot_id,)).fetchall())
    assert statuses["200"] == prices.OUTLIER
    assert [statuses[str(300 + i)] for i in range(3)] == [prices.OK] * 3

    # and the second pass is a no-op
    assert prices.mark_outliers(con, parsed.snapshot_id) == 0


def test_outliers_are_judged_within_a_variant_not_across_one_item_id():
    """Two pets under item 82800 are two goods at two price levels.

    A median over the pooled item would call the expensive species an
    outlier, or hide a genuinely absurd price behind a cheap sibling.
    Grouping by ``variant_key`` keeps each species' own distribution.
    """
    con = store.memory_connection()
    cheap = [{"id": 100 + i, "item": {"id": 82800, "pet_species_id": 162},
              "quantity": 1, "buyout": 5_000 + i, "time_left": "SHORT"}
             for i in range(5)]
    dear = [{"id": 200 + i, "item": {"id": 82800, "pet_species_id": 2908},
             "quantity": 1, "buyout": 5_000_000 + i, "time_left": "SHORT"}
            for i in range(5)]
    parsed = ah.parse_payload({"auctions": cheap + dear}, region="eu",
                              realm="Kazzak", captured_at=1_792_500_000)
    ah.ingest_bundle(con, parsed)

    assert prices.status_counts(con, parsed.snapshot_id).get(prices.OUTLIER, 0) == 0

    # the absurd one, in the cheap species, is still caught
    con.execute(
        "INSERT INTO ah_auction (snapshot_id, auction_key, auction_id, item_id,"
        " quantity, unit_price_copper, is_commodity, price_status, variant_key)"
        " VALUES (?,?,?,?,?,?,0,'ok',?)",
        (parsed.snapshot_id, "999", 999, 82800, 1, 90_000_000_000,
         parsed.auctions[0].variant_key))
    con.commit()

    prices.mark_outliers(con, parsed.snapshot_id)

    row = con.execute(
        "SELECT price_status FROM ah_auction WHERE snapshot_id = ? AND auction_key = '999'",
        (parsed.snapshot_id,)).fetchone()
    assert row["price_status"] == prices.OUTLIER
    assert con.execute(
        "SELECT COUNT(*) FROM ah_auction WHERE snapshot_id = ? AND price_status = ?",
        (parsed.snapshot_id, prices.OUTLIER)).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Phase 1.4 -- product identity
# ---------------------------------------------------------------------------

def test_auction_key_is_an_auction_id_not_a_product_id():
    """Two live auctions of the same good must share the variant, not the key."""
    from woweconomy.intel import identity

    payload = {"auctions": [
        {"id": 111, "item": {"id": 82800, "pet_species_id": 2908},
         "quantity": 1, "buyout": 50_000, "time_left": "SHORT"},
        {"id": 222, "item": {"id": 82800, "pet_species_id": 2908},
         "quantity": 1, "buyout": 51_000, "time_left": "SHORT"},
    ]}
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak",
                              captured_at=1_792_500_000)
    a, b = parsed.auctions
    assert a.auction_key != b.auction_key
    assert a.variant_key == b.variant_key
    assert identity.variant_key(82800, pet_species_id=2908) == a.variant_key


def test_two_pet_species_under_one_item_id_are_two_products():
    payload = {"auctions": [
        {"id": 1, "item": {"id": 82800, "pet_species_id": 2908},
         "quantity": 1, "buyout": 50_000, "time_left": "SHORT"},
        {"id": 2, "item": {"id": 82800, "pet_species_id": 162},
         "quantity": 1, "buyout": 50_000, "time_left": "SHORT"},
    ]}
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak",
                              captured_at=1_792_500_000)
    keys = {a.variant_key for a in parsed.auctions}
    assert len(keys) == 2
    assert len({a.item_id for a in parsed.auctions}) == 1


def test_the_variant_key_is_stored_and_counted_per_snapshot():
    from woweconomy.intel import identity

    payload = {"auctions": [
        {"id": i, "item": {"id": 82800, "pet_species_id": 1000 + i},
         "quantity": 1, "buyout": 10_000 + i, "time_left": "SHORT"}
        for i in range(4)
    ]}
    con = store.memory_connection()
    parsed = ah.parse_payload(payload, region="eu", realm="Kazzak",
                              captured_at=1_792_500_000)
    ah.ingest_bundle(con, parsed)
    assert con.execute(
        "SELECT COUNT(DISTINCT variant_key) FROM ah_auction"
        " WHERE snapshot_id = ?", (parsed.snapshot_id,)).fetchone()[0] == 4

    stats = identity.variant_concentration(con, parsed.snapshot_id)
    assert stats["items"] == 1
    assert stats["variants"] == 4
    assert stats["items_with_more_than_one_variant"] == 1


# ---------------------------------------------------------------------------
# Phase 1.5 -- the backfill is deterministic and idempotent
# ---------------------------------------------------------------------------

def _legacy_db(path):
    """A database written before the derived columns existed."""
    con = store.connect(path)
    con.execute(
        "INSERT INTO ah_snapshot (snapshot_id, region, realm, captured_at,"
        " source, payload_sha256) VALUES ('s1','eu','Kazzak',1,'test','x')")
    rows = [
        # (key, item, price)  -- a normal row, a sentinel, an impossible one
        ("a", 100, 1_500),
        ("b", 100, 1_600),
        ("c", 100, 1_700),
        ("d", 100, prices.SENTINEL_COPPER),
        ("e", 100, prices.GOLD_CAP_COPPER + 1),
        ("f", 100, None),
    ]
    con.executemany(
        "INSERT INTO ah_auction (snapshot_id, auction_key, item_id, quantity,"
        " unit_price_copper, unit_price_source, price_status)"
        " VALUES ('s1', ?, ?, 1, ?, 'api_unit_price', 'missing')", rows)
    con.commit()
    con.close()


def test_the_backfill_labels_legacy_rows_without_touching_the_raw_price(tmp_path):
    from scripts import backfill_ah_identity as backfill

    db = tmp_path / "intel.sqlite"
    _legacy_db(db)

    dry = backfill.run(db, dry_run=True)
    assert dry["variants_filled"] == 6
    assert dry["prices_labelled"] == 5      # the NULL price stays missing

    con = store.connect(db)
    assert con.execute("SELECT COUNT(*) FROM ah_auction"
                       " WHERE variant_key IS NULL").fetchone()[0] == 6
    con.close()

    done = backfill.run(db, dry_run=False)
    assert done["variants_filled"] == 6
    assert done["prices_labelled"] == 5

    con = store.connect(db)
    status = dict(con.execute(
        "SELECT auction_key, price_status FROM ah_auction").fetchall())
    assert status == {"a": prices.OK, "b": prices.OK, "c": prices.OK,
                      "d": prices.SENTINEL, "e": prices.IMPOSSIBLE,
                      "f": prices.MISSING}
    # the raw observations are intact, including the sentinel
    raw = dict(con.execute(
        "SELECT auction_key, unit_price_copper FROM ah_auction").fetchall())
    assert raw["d"] == prices.SENTINEL_COPPER
    assert raw["e"] == prices.GOLD_CAP_COPPER + 1
    assert raw["f"] is None
    con.close()


def test_the_backfill_is_idempotent(tmp_path):
    from scripts import backfill_ah_identity as backfill

    db = tmp_path / "intel.sqlite"
    _legacy_db(db)
    backfill.run(db, dry_run=False)
    second = backfill.run(db, dry_run=False)
    assert second["variants_filled"] == 0
    assert second["prices_labelled"] == 0
    assert second["outliers_flagged"] == 0


