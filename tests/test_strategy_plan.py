"""The posting plan: which mog items to plan, what is stored, what is replaced.

These tests stub the *classifier* only.  Classification has its own suite and
needs the DB2 appearance import plus the DfA demand cache to say anything; what
is being tested here is the decision this module makes about the verdicts it is
handed -- which ones become plans, what a plan with no price looks like, and
that the stored set is the current plan rather than an accumulation.

The valuation is *not* stubbed: the store below is a real SQLite file with real
``tsm_region_price`` and ``blizzard_items`` rows, so the numbers in the stored
plan are produced by the same code that produces them in production.
"""
from __future__ import annotations

import sqlite3

import pytest

from woweconomy.analytics.mog_classify import (
    CATEGORY_COLLECTIBLE,
    CATEGORY_NICHE,
    CATEGORY_PLAIN,
    CATEGORY_UNKNOWN,
    TransmogVerdict,
)
from woweconomy.analytics.mog_strategy import (
    ACTION_DO_NOT_POST,
    ACTION_POST,
    ACTION_POST_PATIENT,
    ACTION_UNDERCUT,
)
from woweconomy.analytics.strategy_plan import (
    PLANNED_CATEGORIES,
    build_plans,
    candidate_items,
    ensure_schema,
    read_plans,
    store_plans,
)

SCHEMA = """
CREATE TABLE tsm_region_price (
    key_id          INTEGER PRIMARY KEY,
    name            TEXT,
    market_value    REAL,
    historical      REAL,
    avg_sale_price  REAL,
    sale_rate       REAL,
    sold_per_day    REAL,
    updated_at      INTEGER
);
CREATE TABLE blizzard_items (
    itemId      INTEGER PRIMARY KEY,
    sellPrice   INTEGER,
    fetchedAt   INTEGER
);
CREATE TABLE auction_events (
    event_type        TEXT,
    item_id           INTEGER,
    unit_price_copper REAL,
    timestamp_unix    INTEGER
);
"""


class StubClassifier:
    """Answers with the categories the test asked for, and nothing else."""

    def __init__(self, categories):
        self.categories = dict(categories)
        self.seen = []

    def classify(self, item_ids):
        self.seen = list(item_ids)
        return [TransmogVerdict(item_id=int(i),
                                name=f"Item {i}",
                                category=self.categories.get(int(i),
                                                             CATEGORY_UNKNOWN))
                for i in item_ids]

    def summary(self, verdicts):
        out = {}
        for verdict in verdicts:
            out[verdict.category] = out.get(verdict.category, 0) + 1
        return out


def _store(tmp_path, rows, *, vendor=5_000):
    """A real store: one region price row and one vendor price per item."""
    path = tmp_path / "tsm_markets.db"
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        for item_id, price, sold_per_day in rows:
            con.execute(
                "INSERT INTO tsm_region_price"
                " (key_id, name, market_value, historical, avg_sale_price,"
                "  sale_rate, sold_per_day, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (item_id, f"Item {item_id}", price, price, price,
                 0.5, sold_per_day, 1_700_000_000))
            con.execute(
                "INSERT INTO blizzard_items (itemId, sellPrice, fetchedAt)"
                " VALUES (?,?,?)", (item_id, vendor, 1_700_000_000))
        con.commit()
    finally:
        con.close()
    return path


# ----------------------------------------------------------------------
# which verdicts become plans
# ----------------------------------------------------------------------


def test_only_the_planned_categories_become_plans(tmp_path):
    """``plain`` and ``unknown`` are not planned, and are reported as skipped."""
    path = _store(tmp_path, [(1, 500_000, 0.4), (2, 500_000, 0.4),
                             (3, 500_000, 0.4), (4, 500_000, 0.4)])
    classifier = StubClassifier({
        1: CATEGORY_COLLECTIBLE,
        2: CATEGORY_NICHE,
        3: CATEGORY_PLAIN,
        4: CATEGORY_UNKNOWN,
    })
    report = build_plans(path, classifier=classifier, intel_path=None)

    assert report.errors == []
    assert report.considered == 2
    planned = {item.plan.item_id for item in report.plans}
    assert planned == {1, 2}
    # the categories it did not plan are still counted, so the run is auditable
    assert report.categories[CATEGORY_PLAIN] == 1
    assert report.categories[CATEGORY_UNKNOWN] == 1


def test_the_planned_categories_are_the_transmog_ones():
    assert set(PLANNED_CATEGORIES) == {CATEGORY_COLLECTIBLE, CATEGORY_NICHE}


def test_an_item_with_no_price_is_stored_as_do_not_post(tmp_path):
    """A refusal is a plan.  Omitting the row would hide the decision."""
    path = _store(tmp_path, [(1, 500_000, 0.4)])
    con = sqlite3.connect(path)
    try:
        # a price of zero is not a price; the strategy must refuse it
        con.execute("UPDATE tsm_region_price SET market_value = 0,"
                    " avg_sale_price = 0, historical = 0, sale_rate = 0,"
                    " sold_per_day = 0 WHERE key_id = 1")
        con.commit()
    finally:
        con.close()

    report = build_plans(path,
                         classifier=StubClassifier({1: CATEGORY_COLLECTIBLE}),
                         intel_path=None)
    assert len(report.plans) == 1
    item = report.plans[0]
    assert item.plan.action in (ACTION_DO_NOT_POST, "vendor")
    assert item.plan.reason, "a refusal without a reason is not auditable"


def test_a_planned_item_carries_the_numbers_it_was_decided_on(tmp_path):
    path = _store(tmp_path, [(7, 900_000, 0.6)])
    report = build_plans(path,
                         classifier=StubClassifier({7: CATEGORY_COLLECTIBLE}),
                         intel_path=None)
    item = report.plans[0]
    assert item.category == CATEGORY_COLLECTIBLE
    assert item.estimate_copper == pytest.approx(900_000)
    assert item.plan.price_unit_copper is not None
    # with no competition source, the count is zero rather than invented
    assert item.competition_count == 0
    assert item.competition_copper is None


def test_the_limit_truncates_and_says_so(tmp_path):
    path = _store(tmp_path, [(i, 500_000, 0.4) for i in range(1, 6)])
    report = build_plans(path,
                         classifier=StubClassifier({i: CATEGORY_COLLECTIBLE
                                                    for i in range(1, 6)}),
                         limit=3, intel_path=None)
    assert report.truncated is True
    assert report.considered == 3


def test_without_a_limit_the_whole_market_is_planned(tmp_path):
    """A cap that silently plans part of the market is worse than no plan.

    The live store holds 31,184 region prices; a default cap of 20,000 planned
    two thirds of them while reporting a complete run.
    """
    path = _store(tmp_path, [(i, 500_000, 0.4) for i in range(1, 6)])
    report = build_plans(path,
                         classifier=StubClassifier({i: CATEGORY_COLLECTIBLE
                                                    for i in range(1, 6)}),
                         limit=None, intel_path=None)
    assert report.truncated is False
    assert report.considered == 5
    assert len(candidate_items(path)) == 5


def test_a_store_with_no_region_prices_reports_rather_than_plans(tmp_path):
    path = _store(tmp_path, [])
    report = build_plans(path, classifier=StubClassifier({}), intel_path=None)
    assert report.plans == []
    assert report.errors


def test_a_missing_store_is_an_error_not_an_empty_plan(tmp_path):
    report = build_plans(tmp_path / "gone.db", intel_path=None)
    assert report.plans == []
    assert report.errors


def test_candidate_items_reads_the_region_market(tmp_path):
    path = _store(tmp_path, [(1, 1, 1), (2, 1, 1), (3, 1, 1)])
    assert candidate_items(path) == [1, 2, 3]


# ----------------------------------------------------------------------
# what is stored
# ----------------------------------------------------------------------


def test_the_stored_plan_replaces_the_previous_one(tmp_path):
    """The table holds the current plan, not a history.

    A row that is no longer planned has to disappear, or the addon keeps
    showing a recommendation the pipeline has stopped making.
    """
    path = _store(tmp_path, [(1, 500_000, 0.4), (2, 500_000, 0.4)])
    first = build_plans(path,
                        classifier=StubClassifier({1: CATEGORY_COLLECTIBLE,
                                                   2: CATEGORY_COLLECTIBLE}),
                        intel_path=None)
    assert store_plans(path, first) == 2
    assert {r["item_id"] for r in read_plans(path)} == {1, 2}

    # second run: item 2 is no longer a mog item
    second = build_plans(path,
                         classifier=StubClassifier({1: CATEGORY_COLLECTIBLE,
                                                    2: CATEGORY_PLAIN}),
                         intel_path=None)
    assert store_plans(path, second) == 1
    assert {r["item_id"] for r in read_plans(path)} == {1}


def test_a_stored_plan_records_when_it_was_built(tmp_path):
    path = _store(tmp_path, [(1, 500_000, 0.4)])
    report = build_plans(path,
                         classifier=StubClassifier({1: CATEGORY_COLLECTIBLE}),
                         intel_path=None)
    store_plans(path, report, built_at="2026-10-23T00:00:00Z")
    row = read_plans(path)[0]
    assert row["built_at"] == "2026-10-23T00:00:00Z"
    assert row["category"] == CATEGORY_COLLECTIBLE
    assert row["estimate_copper"] == pytest.approx(500_000)


def test_an_empty_run_does_not_wipe_the_stored_plan(tmp_path):
    """Nothing built is not the same instruction as "post nothing"."""
    path = _store(tmp_path, [(1, 500_000, 0.4)])
    report = build_plans(path,
                         classifier=StubClassifier({1: CATEGORY_COLLECTIBLE}),
                         intel_path=None)
    store_plans(path, report)
    before = read_plans(path)

    empty = build_plans(path, classifier=StubClassifier({}), intel_path=None)
    assert store_plans(path, empty) == 0
    assert read_plans(path) == before


def test_read_plans_orders_by_expected_net_and_filters_by_action(tmp_path):
    path = _store(tmp_path, [(1, 200_000, 0.5), (2, 900_000, 0.5),
                             (3, 500_000, 0.5)])
    report = build_plans(path,
                         classifier=StubClassifier({i: CATEGORY_COLLECTIBLE
                                                    for i in (1, 2, 3)}),
                         intel_path=None)
    store_plans(path, report)

    rows = read_plans(path)
    nets = [r["expected_net_copper"] for r in rows if r["expected_net_copper"]]
    assert nets == sorted(nets, reverse=True)

    posting = read_plans(path, action=ACTION_POST)
    assert all(r["action"] == ACTION_POST for r in posting)
    assert len(posting) <= len(rows)

    limited = read_plans(path, limit=1)
    assert len(limited) == 1


def test_the_actions_read_back_are_the_ones_the_strategy_chose(tmp_path):
    path = _store(tmp_path, [(1, 500_000, 0.5)])
    report = build_plans(path,
                         classifier=StubClassifier({1: CATEGORY_COLLECTIBLE}),
                         intel_path=None)
    chosen = report.plans[0].plan.action
    store_plans(path, report)
    assert read_plans(path)[0]["action"] == chosen
    assert chosen in (ACTION_POST, ACTION_POST_PATIENT, ACTION_UNDERCUT,
                      ACTION_DO_NOT_POST, "vendor", "hold", "relist",
                      "maintain")


def test_reading_a_plan_that_was_never_built_returns_nothing(tmp_path):
    path = _store(tmp_path, [])
    assert read_plans(path) == []


def test_ensure_schema_creates_the_table_on_sqlite(tmp_path):
    path = _store(tmp_path, [])
    con = sqlite3.connect(path)
    try:
        ensure_schema(con)
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "mog_strategy_plan" in names
        # idempotent: a second call is not an error
        ensure_schema(con)
    finally:
        con.close()
