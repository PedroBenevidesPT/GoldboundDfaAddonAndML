"""Transmog classification: id-keyed, evidence-first, uncertainty preserved.

The rule being defended: an item with no appearance row is ``unknown``, not
``plain``.  Absence of a row is absence of evidence, and calling it "not
transmog" invents metadata the data does not contain.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from woweconomy.analytics.collector_demand import AppearanceDemand, DemandRow
from woweconomy.analytics.mog_classify import (
    CATEGORIES,
    CATEGORY_COLLECTIBLE,
    CATEGORY_NICHE,
    CATEGORY_PLAIN,
    CATEGORY_RESTRICTED,
    CATEGORY_UNKNOWN,
    TransmogClassifier,
    classify_item,
)
from woweconomy.services.db2_items import Db2Item

REAL_DB = Path("tsm_markets.db")


def _demand(missing: dict, sample: int = 1000) -> AppearanceDemand:
    book = AppearanceDemand(missing=dict(missing), sample=sample, source="test")
    return book


def _db2(**kw) -> Db2Item:
    base = dict(item_id=1, name="Test Item", binding=2, quality=3, expansion=0,
                era=0, item_level=10, required_level=5)
    base.update(kw)
    return Db2Item(**base)


# ----------------------------------------------------------------------
# the evidence rules
# ----------------------------------------------------------------------


def test_a_look_most_collectors_lack_is_collectible():
    verdict = classify_item(10, [500], _demand({500: 900}), _db2())
    assert verdict.category == CATEGORY_COLLECTIBLE
    assert verdict.best_missing_pct == pytest.approx(90.0)
    assert verdict.is_transmog
    assert verdict.has_demand


def test_a_look_some_collectors_lack_is_niche():
    verdict = classify_item(10, [500], _demand({500: 200}), _db2())
    assert verdict.category == CATEGORY_NICHE


def test_a_look_nobody_lacks_is_plain_not_collectible():
    """The look exists; the audience does not.  Still a real transmog item."""
    verdict = classify_item(10, [500], _demand({500: 0}, sample=1000), _db2())
    assert verdict.category == CATEGORY_PLAIN
    assert verdict.is_transmog
    assert not verdict.has_demand
    assert "none of" in verdict.reason


def test_no_appearance_row_is_unknown_never_plain():
    """The core rule: absence of evidence is not evidence of absence."""
    verdict = classify_item(10, [], _demand({}), _db2())
    assert verdict.category == CATEGORY_UNKNOWN
    assert verdict.no_metadata
    assert not verdict.is_transmog
    assert "cannot be told apart" in verdict.reason


def test_no_demand_sample_is_unknown():
    verdict = classify_item(10, [500], None, _db2())
    assert verdict.category == CATEGORY_UNKNOWN
    assert "no demand sample" in verdict.reason


def test_a_demand_row_without_a_sample_size_is_unknown():
    book = AppearanceDemand(missing={500: 5}, sample=0, source="test")
    verdict = classify_item(10, [500], book, _db2())
    assert verdict.category == CATEGORY_UNKNOWN
    assert "no sample size" in verdict.reason


def test_an_untradeable_item_is_restricted_not_collectible():
    """A BoP item's look cannot be sold, however many collectors want it."""
    verdict = classify_item(10, [500], _demand({500: 900}),
                            _db2(binding=1))
    assert verdict.category == CATEGORY_RESTRICTED
    assert not verdict.tradeable
    assert "not tradeable" in verdict.reason


def test_an_era_classic_row_is_restricted():
    verdict = classify_item(10, [500], _demand({500: 900}), _db2(era=3))
    assert verdict.category == CATEGORY_RESTRICTED
    assert "era-Classic" in verdict.reason


def test_the_best_appearance_is_the_most_wanted_one():
    book = _demand({500: 100, 501: 800, 502: 300})
    verdict = classify_item(10, [500, 501, 502], book, _db2())
    assert len(verdict.appearances) == 3
    assert verdict.appearances[0].appearance_id == 501
    assert verdict.best_missing_pct == pytest.approx(80.0)


def test_an_item_with_no_db2_row_is_not_assumed_tradeable():
    """An unimported DB2 row means binding is unknown, not BoE.

    ``tradeable`` defaults to False, so a missing row routes to
    ``restricted`` rather than silently posting an item that may be
    soulbound.  The classification of the *look* is still reported.
    """
    verdict = classify_item(10, [500], _demand({500: 600}), None)
    assert verdict.binding is None
    assert verdict.tradeable is False
    assert verdict.category == CATEGORY_RESTRICTED
    # the look itself is still measured, so the caller is not left blind
    assert verdict.is_transmog
    assert verdict.best_missing_pct == pytest.approx(60.0)
    assert verdict.appearances[0].missing == 600


def test_every_category_is_reachable():
    produced = set()
    produced.add(classify_item(1, [1], _demand({1: 900}), _db2()).category)
    produced.add(classify_item(2, [1], _demand({1: 200}), _db2()).category)
    produced.add(classify_item(3, [1], _demand({1: 0}), _db2()).category)
    produced.add(classify_item(4, [1], _demand({1: 900}), _db2(binding=1)).category)
    produced.add(classify_item(5, [], _demand({}), _db2()).category)
    assert produced == set(CATEGORIES)


# ----------------------------------------------------------------------
# against the real database
# ----------------------------------------------------------------------


@pytest.mark.skipif(not REAL_DB.exists(), reason="no tsm_markets.db in the workspace")
def test_the_real_classifier_separates_equipment_from_consumables():
    con = sqlite3.connect(REAL_DB)
    try:
        ids = [r[0] for r in con.execute(
            "SELECT DISTINCT item_id FROM auction_events"
            " WHERE item_id IS NOT NULL ORDER BY RANDOM() LIMIT 200")]
    finally:
        con.close()
    classifier = TransmogClassifier(REAL_DB)
    verdicts = classifier.classify(ids)
    assert len(verdicts) == len(ids)
    summary = classifier.summary(verdicts)
    assert sum(summary.values()) == len(ids)
    # a real transmog item must appear, and a real consumable must not be
    # forced into the transmog categories
    assert summary[CATEGORY_COLLECTIBLE] + summary[CATEGORY_NICHE] > 0
    assert summary[CATEGORY_UNKNOWN] > 0


@pytest.mark.skipif(not REAL_DB.exists(), reason="no tsm_markets.db in the workspace")
def test_a_known_transmog_item_is_classified_as_transmog():
    """War Torn Cape: 85.7% of the sample lacks appearance 4148."""
    classifier = TransmogClassifier(REAL_DB)
    verdict = classifier.classify_one(15483)
    assert verdict.is_transmog
    assert verdict.category == CATEGORY_COLLECTIBLE
    assert verdict.best_missing_pct > 50.0


@pytest.mark.skipif(not REAL_DB.exists(), reason="no tsm_markets.db in the workspace")
def test_a_recipe_is_not_classified_as_a_collectible():
    """Recipes have no appearance row; they must be unknown, not plain."""
    classifier = TransmogClassifier(REAL_DB)
    verdict = classifier.classify_one(3831)
    assert not verdict.is_transmog
    assert verdict.category == CATEGORY_UNKNOWN
    assert verdict.no_metadata


@pytest.mark.skipif(not REAL_DB.exists(), reason="no tsm_markets.db in the workspace")
def test_the_classifier_caches_appearances_and_answers_consistently():
    classifier = TransmogClassifier(REAL_DB)
    first = classifier.classify_one(15483)
    again = classifier.classify_one(15483)
    assert first.category == again.category
    assert first.best_missing_pct == again.best_missing_pct
