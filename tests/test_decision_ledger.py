"""The advice is written down before the outcome exists, then judged."""
import json
import sqlite3
import time

import pytest

from scripts import intel, watch
from woweconomy.intel import decision_ledger, relist, store

REALM = "Dragonmaw"
CHARACTER = "Bullbound"
#: ten days ago: inside ``advise``'s default 14-day window, so the tests
#: exercise the same query the watcher uses rather than a synthetic one
BASE = int(time.time()) - 10 * 86400


def observe(con, auction_id=1, own=10_000, lowest=8_000, ahead=1, qty=1,
            time_left=20 * 3600, observed_at=None, item="Thing", scan="scan-1",
            item_id=1001, character=CHARACTER, confidence="verified"):
    observed = BASE if observed_at is None else int(observed_at)
    status = "verified" if confidence == "verified" else "price-only"
    gap = (own - lowest) / own if own else 0.0
    con.execute(
        "INSERT OR REPLACE INTO undercut_observation (observation_id,"
        " scan_id, observed_at, realm, character, item_id, item_name, item_key,"
        " item_link, own_auction_id, own_quantity, own_buyout_copper,"
        " own_unit_price_copper, lowest_unit_price_copper, lowest_seller,"
        " lowest_seller_realm, lowest_auction_id, undercut_copper,"
        " undercut_pct, items_ahead, sellers_below, time_left, sellers_json,"
        " source, confidence, reference_status, imported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{scan}-{auction_id}", scan, observed, REALM, character, item_id,
         item, f"{item_id}:0:0:0", "", auction_id, qty, own, own, lowest,
         "Under-Stormscale", "Stormscale", 9001, own - lowest, gap, ahead, 1,
         time_left, "[]", "auctionator_undercut_scan", confidence, status, ""))
    con.commit()


def event_db(tmp_path, events):
    path = tmp_path / "tsm.db"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE auction_events (event_type TEXT, timestamp_unix INTEGER,"
        " item_id INTEGER, qty INTEGER, unit_price_copper INTEGER,"
        " total_price_copper INTEGER, net_proceeds_copper INTEGER,"
        " realm TEXT, character TEXT)")
    con.executemany(
        "INSERT INTO auction_events VALUES (?,?,?,?,?,?,?,?,?)",
        [(e["type"], e["ts"], e["item"], e.get("qty", 1), e["unit"],
          e.get("total", e["unit"] * e.get("qty", 1)), e.get("net"),
          REALM, e.get("character", CHARACTER)) for e in events])
    con.commit()
    con.close()
    return path


def current(con, decision_id=None):
    if decision_id:
        return dict(con.execute(
            "SELECT * FROM relist_decision WHERE decision_id = ?",
            (decision_id,)).fetchone())
    return [dict(row) for row in con.execute(
        "SELECT * FROM relist_decision ORDER BY decided_at")]


def test_the_same_scan_recorded_twice_is_one_recommendation(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con)
    advice = relist.advise(con, REALM, days=30, now=BASE)

    first = decision_ledger.record(con, advice, REALM, now=BASE)
    second = decision_ledger.record(con, advice, REALM, now=BASE)

    assert first["recorded"] == 1
    assert second["recorded"] == 0
    assert second["already_present"] == 1
    assert len(current(con)) == 1


def test_a_later_scan_supersedes_the_open_recommendation_for_the_same_auction(
        tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con, scan="scan-1")
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)

    observe(con, scan="scan-2", observed_at=BASE + 3600, ahead=9)
    later = decision_ledger.record(
        con, relist.advise(con, REALM, days=30, now=BASE + 3600), REALM,
        now=BASE + 3600)

    rows = current(con)
    assert later["recorded"] == 1
    assert later["superseded"] == 1
    assert [row["superseded"] for row in rows] == [1, 0]
    assert sum(1 for row in rows if not row["superseded"]) == 1


def test_a_sale_after_the_scan_is_linked_with_the_house_cut(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con)
    advice = relist.advise(con, REALM, days=30, now=BASE)
    decision_ledger.record(con, advice, REALM, now=BASE)
    tsm = event_db(tmp_path, [{"type": "sale", "ts": BASE + 3600,
                               "item": 1001, "unit": 10_000}])

    result = decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    row = current(con)[0]
    assert result["linked"] == 1
    assert row["outcome"] == decision_ledger.SOLD
    assert row["outcome_source"] == "auction_events"
    assert row["realised_copper"] == 9_500  # 10,000 gross, 5 % cut


def test_the_net_proceeds_the_event_carries_win_over_the_estimate(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con)
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    tsm = event_db(tmp_path, [{"type": "sale", "ts": BASE + 3600,
                               "item": 1001, "unit": 10_000, "net": 9_123}])

    decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    assert current(con)[0]["realised_copper"] == 9_123


def test_an_event_before_the_scan_is_not_this_listings_outcome(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con)
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    tsm = event_db(tmp_path, [{"type": "sale", "ts": BASE - 3600,
                               "item": 1001, "unit": 10_000}])

    result = decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    assert result["linked"] == 0
    assert current(con)[0]["outcome"] == decision_ledger.OPEN


def test_expiry_and_cancel_are_outcomes_but_return_no_gold(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con, auction_id=1, item="A", item_id=1001)
    observe(con, auction_id=2, item="B", item_id=1002, own=5_000,
            lowest=4_000, scan="scan-2")
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    tsm = event_db(tmp_path, [
        {"type": "expire", "ts": BASE + 3600, "item": 1001, "unit": 10_000},
        {"type": "cancel", "ts": BASE + 3600, "item": 1002, "unit": 5_000},
    ])

    decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    by_item = {row["item_id"]: row for row in current(con)}
    assert by_item[1001]["outcome"] == decision_ledger.EXPIRED
    assert by_item[1002]["outcome"] == decision_ledger.CANCELLED
    assert by_item[1001]["realised_copper"] == 0
    assert by_item[1002]["realised_copper"] == 0


def test_an_expiry_without_a_price_is_matched_on_item_and_character(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con, auction_id=1, item="A", item_id=1001)
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    # Live expiries carry unit_price_copper = 0; they can still be the
    # outcome of the one open listing of that item and character.
    tsm = event_db(tmp_path, [{"type": "expire", "ts": BASE + 3600,
                               "item": 1001, "unit": 0, "qty": 1}])

    result = decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    row = current(con)[0]
    assert result["linked"] == 1
    assert row["outcome"] == decision_ledger.EXPIRED
    assert "carries no price" in row["evidence"]


def test_an_unpriced_event_cannot_choose_between_two_listings_of_the_item(
        tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    # Same character, same item, two different asks: the expiry says which
    # item it was, not which price, so neither row may claim it.
    observe(con, auction_id=1, item="A", item_id=1001, own=10_000)
    observe(con, auction_id=2, item="A", item_id=1001, own=20_000, scan="scan-2")
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    tsm = event_db(tmp_path, [{"type": "expire", "ts": BASE + 3600,
                               "item": 1001, "unit": 0, "qty": 1}])

    result = decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    assert result["linked"] == 0
    assert all(row["outcome"] == decision_ledger.OPEN for row in current(con))
    assert all(row["evidence"].startswith("ambiguous") for row in current(con))


def test_two_open_listings_at_the_same_price_are_left_open_as_ambiguous(
        tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con, auction_id=1)
    observe(con, auction_id=2)
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    tsm = event_db(tmp_path, [{"type": "sale", "ts": BASE + 3600,
                               "item": 1001, "unit": 10_000}])

    result = decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    assert result["ambiguous_auctions"] == 2
    assert result["linked"] == 0
    for row in current(con):
        assert row["outcome"] == decision_ledger.OPEN
        assert row["evidence"].startswith("ambiguous")


def test_the_summary_rates_only_resolved_listings(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    # Both are HOLDs (ask below the only competitor): one sells, one expires.
    observe(con, auction_id=1, own=9_000, lowest=10_000, item="A", item_id=1001)
    observe(con, auction_id=2, own=9_000, lowest=10_000, item="B", item_id=1002,
            scan="scan-2")
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    tsm = event_db(tmp_path, [
        {"type": "sale", "ts": BASE + 3600, "item": 1001, "unit": 9_000},
        {"type": "expire", "ts": BASE + 3600, "item": 1002, "unit": 9_000},
    ])
    decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    report = decision_ledger.summary(con, REALM, days=30, now=BASE + 7200)

    hold = report["by_advice"][relist.HOLD]
    assert hold["resolved"] == 2
    assert hold["sale_rate"] == 0.5
    assert hold["gold_per_resolved"] == round(9_000 * 0.95 / 2, 2)
    assert report["readings"]["hold_or_watch_sale_rate"] == 0.5
    assert "not orders" in report["readings"]["note"]


def test_a_relist_followed_by_a_cancel_is_not_counted_as_a_failure(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con)
    decision_ledger.record(con, relist.advise(con, REALM, days=30, now=BASE),
                           REALM, now=BASE)
    tsm = event_db(tmp_path, [{"type": "cancel", "ts": BASE + 3600,
                               "item": 1001, "unit": 10_000}])
    decision_ledger.link_outcomes(con, tsm, REALM, now=BASE + 7200)

    report = decision_ledger.summary(con, REALM, days=30, now=BASE + 7200)

    relist_bucket = report["by_advice"][relist.RELAST]
    assert relist_bucket["cancelled"] == 1
    assert report["readings"]["relist_followed_share"] == 1.0
    assert report["readings"]["hold_or_watch_sale_rate"] is None


def test_backfill_replays_each_scan_at_its_own_time(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con, scan="scan-1", observed_at=BASE)
    observe(con, scan="scan-2", observed_at=BASE + 3600, ahead=9)

    result = decision_ledger.backfill(con, REALM, days=3650,
                                      now=BASE + 7200)

    rows = current(con)
    assert result["scans"] == 2
    assert len(rows) == 2
    current_row = [row for row in rows if not row["superseded"]][0]
    # The scan said 20 h left; a replay at "now" would have said 0 h and
    # turned this into let_expire.
    assert current_row["hours_left"] == 20.0
    assert current_row["advice"] == relist.RELAST


def test_replaying_the_same_scans_leaves_one_standing_recommendation(tmp_path):
    con = store.connect(tmp_path / "intel.sqlite")
    observe(con, scan="scan-1", observed_at=BASE)
    observe(con, scan="scan-2", observed_at=BASE + 3600, ahead=9)

    decision_ledger.backfill(con, REALM, days=3650, now=BASE + 7200)
    second = decision_ledger.backfill(con, REALM, days=3650, now=BASE + 7200)

    rows = current(con)
    standing = [row for row in rows if not row["superseded"]]
    # A replay is a no-op: the oldest scan must not mark the current row
    # superseded on its way past it.
    assert second["recorded"] == 0
    assert len(standing) == 1
    assert standing[0]["scan_id"] == "scan-2"


def test_the_watcher_records_the_advice_it_alerts_on(tmp_path, monkeypatch):
    db = tmp_path / "intel.sqlite"
    con = store.connect(db)
    observe(con)
    cfg = watch.Config(
        offline=True, notify=False, state_path=tmp_path / "state.json",
        log_path=tmp_path / "watch.log", intel_db=str(db),
        tsm_db=str(tmp_path / "tsm.db"), stable_s=0)
    watcher = watch.Watch(cfg, runner=lambda argv, timeout=0: {"returncode": 0})
    monkeypatch.setattr(watch.ah_undercut, "scan_realm",
                        lambda connection, realm: {"realm": realm})
    monkeypatch.setattr(watch.Watch, "realm_market",
                        lambda self, realm, now: None)

    result = watcher.derive_undercut(BASE, REALM)

    assert result["advice"][relist.RELAST] == 1
    assert result["ledger"]["recorded"] == 1
    assert len(current(con)) == 1


def test_the_cli_exposes_the_decisions_command():
    args = intel.build_parser().parse_args(
        ["decisions", "--realm", REALM, "--backfill", "--link"])

    assert args.func is intel.cmd_decisions
    assert args.realm == REALM
    assert args.backfill is True
    assert args.link is True


def test_a_legacy_database_gains_the_table_without_losing_the_observations(
        tmp_path):
    path = tmp_path / "intel.sqlite"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE undercut_observation (observation_id TEXT PRIMARY KEY,"
        " scan_id TEXT, observed_at INTEGER, realm TEXT, character TEXT,"
        " item_id INTEGER, item_name TEXT, item_key TEXT, item_link TEXT,"
        " own_auction_id INTEGER, own_quantity INTEGER,"
        " own_buyout_copper INTEGER, own_unit_price_copper INTEGER,"
        " lowest_unit_price_copper INTEGER, lowest_seller TEXT,"
        " lowest_seller_realm TEXT, lowest_auction_id INTEGER,"
        " undercut_copper INTEGER, undercut_pct REAL, items_ahead INTEGER,"
        " sellers_below INTEGER, time_left INTEGER, sellers_json TEXT,"
        " source TEXT, confidence TEXT, reference_status TEXT,"
        " imported_at TEXT)")
    con.execute(
        "INSERT INTO undercut_observation (observation_id, realm) VALUES"
        " ('kept', 'Dragonmaw')")
    con.commit()
    con.close()

    migrated = store.connect(path)

    assert migrated.execute(
        "SELECT COUNT(*) FROM undercut_observation").fetchone()[0] == 1
    assert migrated.execute(
        "SELECT COUNT(*) FROM relist_decision").fetchone()[0] == 0
