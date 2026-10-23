"""Tests for the DfA pricing engine, the dataset and the TSM source.

The Lua source is not re-implemented in Python: it is loaded into a real
Lua runtime with a mocked TSM around it, and the *actual* file that goes
into the fork is what is tested.
"""
from __future__ import annotations

import json
import math
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dfa_lua_harness as dfa  # noqa: E402  (shared WoW-client mock)

from woweconomy.pricing.config import DATASET_VERSION, PricingConfig  # noqa: E402
from woweconomy.pricing.dataset import (  # noqa: E402
    GLOBAL_NAME,
    DatasetBuilder,
    DatasetRecord,
    build_dataset,
    validate_dataset,
)
from woweconomy.pricing.interests import load_interests  # noqa: E402
from woweconomy.pricing.mapping import (  # noqa: E402
    EXACT,
    MULTI,
    SHARED,
    UNMAPPED,
    build_item_mapping,
)
from woweconomy.pricing.shipped import (  # noqa: E402
    REFERENCE_COLUMNS,
    SHIPPED_REFERENCES,
    ShippedModel,
    fit_shipped_model,
)

TSM_DIR = Path(
    r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\TradeSkillMaster")
FORK_SOURCE = REPO / "DFA_TSM" / "DFAEstimate.lua"


# ====================================================================== the mapping

def _tiny_tsm_db(tmp_path: Path) -> Path:
    path = tmp_path / "tsm.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE db2_item_appearance(itemId INTEGER, appearanceId INTEGER);
    """)
    con.executemany("INSERT INTO db2_item_appearance VALUES (?, ?)",
                    [(100, 10), (100, 11), (200, 20), (300, 20), (400, 30)])
    con.commit()
    con.close()
    return path


def test_the_mapping_tells_exact_multi_shared_and_unmapped_apart(tmp_path):
    db = _tiny_tsm_db(tmp_path)
    mapping, report = build_item_mapping(db, item_ids=[100, 200, 300, 400, 500])
    assert mapping[100].status == MULTI        # two appearances
    assert mapping[200].status == SHARED        # one appearance, two items
    assert mapping[300].status == SHARED        # the other wearer
    assert mapping[400].status == EXACT         # one appearance, one item
    assert mapping[400].appearance_ids == [30]
    assert mapping[500].status == UNMAPPED     # no appearance at all
    assert mapping[500].appearance_ids == []
    assert report.unmapped == 1
    assert report.multi == 1
    assert report.shared == 2
    assert report.exact == 1


def test_a_shared_appearance_names_the_other_items(tmp_path):
    db = _tiny_tsm_db(tmp_path)
    mapping, _report = build_item_mapping(db, item_ids=[200])
    assert mapping[200].shared_with == [300]


def test_the_mapping_never_invents_an_appearance(tmp_path):
    db = _tiny_tsm_db(tmp_path)
    mapping, _report = build_item_mapping(db, item_ids=[999])
    assert mapping[999].appearance_ids == []
    assert mapping[999].has_interest is False


# ====================================================================== the interest

def test_the_interest_is_a_measurement_not_a_zero(tmp_path):
    db = tmp_path / "dfa.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE account(account TEXT, region TEXT, realm TEXT, name TEXT,
            rank INTEGER, scanned_at TEXT, characters INTEGER, board TEXT);
        CREATE TABLE scanned(kind TEXT, account TEXT, scanned_at TEXT, weight REAL,
            by_realm INTEGER);
        CREATE TABLE missing(kind TEXT, item_id INTEGER, n INTEGER, w REAL);
        CREATE TABLE rarity(kind TEXT, item_id INTEGER, owned_pct REAL);
        CREATE TABLE catalogue(kind TEXT, item_id INTEGER, name TEXT, source TEXT,
            faction TEXT, obtainable INTEGER);
        CREATE TABLE missing_realm(kind TEXT, item_id INTEGER, region TEXT,
            realm TEXT, n INTEGER, w REAL);
    """)
    con.execute("INSERT INTO account VALUES ('a','EU','Dragonmaw','x',1,"
                "'2026-10-19T00:00:00+00:00',1,'eu/connected-dragonmaw')")
    con.execute("INSERT INTO scanned VALUES ('appearances','a','2026-10-19',1.0,1)")
    con.execute("INSERT INTO missing VALUES ('appearances', 55, 30, 1.0)")
    con.execute("INSERT INTO rarity VALUES ('appearances', 55, 20.0)")
    con.execute("INSERT INTO catalogue VALUES ('appearances', 55, 'Belt', 'src', '0', 1)")
    con.execute("INSERT INTO missing_realm VALUES ('appearances', 55, 'EU', 'Dragonmaw', 12, 1.0)")
    con.commit()
    con.close()

    book = load_interests(db, [55])
    assert book.available
    interest = book.get(55)
    assert interest is not None
    assert interest.missing == 30
    assert interest.sample == 1
    assert interest.missing_rate == 30.0
    assert interest.owned_pct == 20.0
    assert interest.realm_coverage == 1
    assert interest.has_measurement


def _dfa_db(tmp_path: Path, missing_realm_rows) -> Path:
    db = tmp_path / "dfa.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE account(account TEXT, region TEXT, realm TEXT, name TEXT,
            rank INTEGER, scanned_at TEXT, characters INTEGER, board TEXT);
        CREATE TABLE scanned(kind TEXT, account TEXT, scanned_at TEXT, weight REAL,
            by_realm INTEGER);
        CREATE TABLE missing(kind TEXT, item_id INTEGER, n INTEGER, w REAL);
        CREATE TABLE rarity(kind TEXT, item_id INTEGER, owned_pct REAL);
        CREATE TABLE catalogue(kind TEXT, item_id INTEGER, name TEXT, source TEXT,
            faction TEXT, obtainable INTEGER);
        CREATE TABLE missing_realm(kind TEXT, item_id INTEGER, region TEXT,
            realm TEXT, n INTEGER, w REAL);
    """)
    con.execute("INSERT INTO account VALUES ('a','EU','Dragonmaw','x',1,"
                "'2026-10-19T00:00:00+00:00',1,'eu/connected-dragonmaw')")
    con.execute("INSERT INTO scanned VALUES ('appearances','a','2026-10-19',1.0,1)")
    con.executemany("INSERT INTO missing VALUES ('appearances', ?, 10, 1.0)",
                    [(55,), (56,)])
    con.executemany("INSERT INTO catalogue VALUES ('appearances', ?, 'x', 's', '0', 1)",
                    [(55,), (56,)])
    con.executemany(
        "INSERT INTO missing_realm VALUES ('appearances', ?, 'EU', ?, ?, 1.0)",
        missing_realm_rows)
    con.commit()
    con.close()
    return db


def test_the_realm_coverage_counts_only_the_realms_that_want_it(tmp_path):
    """`n <= 0` rows are stored and must not count as demand.

    The coverage is a count over 13M rows on the real scan; it is read as a
    covering-index count minus the unwanted rows, so the subtraction is what
    has to stay exact.
    """
    db = _dfa_db(tmp_path, [
        (55, "Dragonmaw", 5), (55, "Silvermoon", 0), (55, "Stormscale", -2),
        (55, "Kazzak", 7),
        (56, "Dragonmaw", 0), (56, "Silvermoon", 0),
    ])
    book = load_interests(db)
    assert book.get(55).realm_coverage == 2
    assert book.get(55).realms_with_demand == 2
    # wanted nowhere: no coverage, not a made-up zero that reads as one realm
    assert book.get(56).realm_coverage == 0


def test_the_overall_demand_does_not_need_the_realm_walk(tmp_path):
    """The engine asks for demand only; coverage is a separate, costly read.

    `realm_coverage` walks 13M rows of `missing_realm`; no price depends on
    it.  The share of accounts that owns the appearance is the demand the
    evidence line shows, and it comes from `rarity`.
    """
    db = _dfa_db(tmp_path, [(55, "Dragonmaw", 5), (55, "Kazzak", 0)])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO rarity VALUES ('appearances', 55, 41.5)")
    con.commit()
    con.close()

    book = load_interests(db, with_realm_coverage=False)
    interest = book.get(55)
    assert interest.missing == 10
    assert interest.owned_pct == 41.5       # the overall demand
    assert interest.realm_coverage == 0     # not read, not guessed
    assert any("coverage not read" in note for note in book.notes)


def test_the_cached_rarity_file_is_the_overall_demand(tmp_path):
    """The 0.8 MB JSON next to the scan is the same measurement, read fast.

    It also keeps working while a scan holds the write lock, which is why
    it is preferred to the table inside the 3 GB database.
    """
    db = _dfa_db(tmp_path, [(55, "Dragonmaw", 5)])
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "appearancesrarity.json").write_text(
        json.dumps({"appearances": {"55": 33.25}}), encoding="utf-8")

    book = load_interests(db, with_realm_coverage=False)
    assert book.get(55).owned_pct == 33.25
    assert any("appearancesrarity.json" in note for note in book.notes)


def test_a_corrupt_rarity_file_falls_back_to_the_table(tmp_path):
    db = _dfa_db(tmp_path, [(55, "Dragonmaw", 5)])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO rarity VALUES ('appearances', 55, 20.0)")
    con.commit()
    con.close()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "appearancesrarity.json").write_text('{"appearances": {', encoding="utf-8")

    book = load_interests(db, with_realm_coverage=False)
    assert book.get(55).owned_pct == 20.0


def test_a_missing_appearance_has_no_interest(tmp_path):
    db = tmp_path / "dfa.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE account(account TEXT, region TEXT, realm TEXT, name TEXT,
            rank INTEGER, scanned_at TEXT, characters INTEGER, board TEXT);
        CREATE TABLE scanned(kind TEXT, account TEXT, scanned_at TEXT, weight REAL,
            by_realm INTEGER);
        CREATE TABLE missing(kind TEXT, item_id INTEGER, n INTEGER, w REAL);
        CREATE TABLE rarity(kind TEXT, item_id INTEGER, owned_pct REAL);
        CREATE TABLE catalogue(kind TEXT, item_id INTEGER, name TEXT, source TEXT,
            faction TEXT, obtainable INTEGER);
        CREATE TABLE missing_realm(kind TEXT, item_id INTEGER, region TEXT,
            realm TEXT, n INTEGER, w REAL);
    """)
    con.commit()
    con.close()
    book = load_interests(db, [55])
    assert book.get(55) is None
    assert book.get(None) is None


# ====================================================================== the shipped model

def test_the_shipped_model_fills_a_missing_reference_the_way_it_was_fitted():
    model = ShippedModel(intercept=0.0, coefficients=[0.5, 0.5],
                         medians=[math.log1p(1000.0), math.log1p(1000.0)],
                         references=["a", "b"])
    both = model.predict({"a": 1000.0, "b": 1000.0})
    one = model.predict({"a": 1000.0, "b": None})
    assert both is not None and one is not None
    assert math.isclose(both, one, rel_tol=1e-9)


def test_the_shipped_model_refuses_a_nonsense_answer():
    model = ShippedModel(intercept=-100.0, coefficients=[0.0],
                         medians=[0.0], references=["a"])
    assert model.predict({"a": 1000.0}) is None


def test_the_shipped_model_survives_a_nan_reference():
    """A missing reference arrives from a frame as NaN, not as None."""
    model = ShippedModel(intercept=0.0, coefficients=[0.5, 0.25],
                         medians=[math.log1p(1000.0), math.log1p(500.0)],
                         references=["a", "b"])
    # the NaN is treated as missing, and the other reference carries the
    # answer
    value = model.predict({"a": float("nan"), "b": 500.0})
    assert value is not None
    assert math.isfinite(value)


def test_the_shipped_model_refuses_an_item_with_no_market_data():
    """No reference at all means no estimate, not 'the price of a typical item'."""
    model = ShippedModel(intercept=0.0, coefficients=[0.5],
                         medians=[math.log1p(1000.0)], references=["a"])
    assert model.predict({"a": None}) is None
    assert model.predict({"a": float("nan")}) is None
    assert model.predict({}) is None


def test_the_shipped_model_uses_only_the_references_it_declares():
    model = ShippedModel(intercept=0.0, coefficients=[0.5],
                         medians=[math.log1p(1000.0)], references=["a"])
    assert model.predict({"a": 1000.0, "b": 999999.0}) == model.predict({"a": 1000.0})


def _synthetic_frame(days: int = 8, per_day: int = 20):
    """A frame with an exactly linear relationship the model can recover."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    rows = []
    # day numbers, in the same shape the learning engine uses
    for day in range(20742, 20742 + days):
        for _ in range(per_day):
            reference = float(rng.integers(1_000, 100_000))
            # a clean, learnable relationship: price = 0.8 * reference
            price = 0.8 * reference
            rows.append({
                "date": day,
                "ref_region_market": reference,
                "ref_region_historical": reference,
                "ref_region_sale_avg": reference,
                "ref_local_market": None,
                "ref_local_recent": None,
                "ref_local_historical": None,
                "y_net_unit": price,
                "y_net_unit_log": math.log(price),
                "local_present": 0.0,
            })
    return pd.DataFrame(rows)


def test_the_confidence_is_measured_out_of_sample():
    """The number the game quotes must not come from the rows it was fitted on.

    A model scored on its own training rows looks better than it is, and
    that flattered number used to travel to the game as the confidence for
    every item with no record.  The walk-forward pool is the honest one.
    """
    from woweconomy.pricing.shipped import (
        evaluate_shipped_model_walk_forward,
        fit_shipped_model,
    )

    frame = _synthetic_frame()
    model = fit_shipped_model(frame)
    assert model.coefficients, "the synthetic model should fit"

    out_of_sample = evaluate_shipped_model_walk_forward(frame)
    assert out_of_sample["method"] == "walk-forward, pooled"
    assert out_of_sample["n"] > 0
    # the relationship is exact, so a chronological split learns it too
    assert out_of_sample["within_25pct"] > 0.9

    # and the pool must only contain rows that were never trained on:
    # an exact fit on the same rows would give a suspiciously small error
    assert out_of_sample["rmse_log"] < 0.05


def test_the_shipped_model_ships_a_calibrated_interval():
    """The game needs a width, not only a point, and its coverage is measured."""
    import numpy as np
    from woweconomy.pricing.shipped import (
        evaluate_shipped_model_walk_forward,
        fit_shipped_model,
    )

    frame = _synthetic_frame(days=20, per_day=40)
    rng = np.random.default_rng(17)
    frame["y_net_unit_log"] = frame["y_net_unit_log"] + rng.normal(0.0, 0.35, len(frame))
    out_of_sample = evaluate_shipped_model_walk_forward(frame)
    assert out_of_sample["interval_level"] == 0.8
    assert out_of_sample["interval_half_width_log"] > 0
    assert 0.65 <= out_of_sample["interval_coverage"] <= 0.95
    assert out_of_sample["interval_test_n"] > 0

    model = fit_shipped_model(frame)
    model.interval_level = out_of_sample["interval_level"]
    model.interval_half_width = out_of_sample["interval_half_width_log"]
    model.interval_coverage = out_of_sample["interval_coverage"]
    model.interval_n = out_of_sample["interval_test_n"]
    model.interval_method = out_of_sample["interval_method"]
    lua = "\n".join(model.as_lua())
    assert "intervalLevel = 0.8000" in lua
    assert "intervalHalfWidth =" in lua
    assert "intervalCoverage =" in lua
    assert "intervalN =" in lua


def test_the_dataset_puts_the_measured_interval_on_the_record():
    from woweconomy.pricing.dataset import DatasetBuilder

    engine = _FakeEngine()
    engine.price_interval = {"level": 0.8, "half_width_log": 0.5,
                             "coverage": 0.78, "test_n": 89}
    builder = DatasetBuilder(config=PricingConfig(), engine=engine)
    estimate = _fake_record()
    builder._attach_interval(estimate)
    assert estimate.price_low == round(1234 * math.exp(-0.5))
    assert estimate.price_high == round(1234 * math.exp(0.5))
    assert estimate.price_interval_level == 0.8
    assert estimate.price_interval_coverage == 0.78
    assert estimate.price_interval_n == 89
    lua = ", ".join(DatasetRecord("Dragonmaw", 100, estimate).to_lua())
    assert f"plow = {estimate.price_low}" in lua
    assert f"phigh = {estimate.price_high}" in lua
    assert "plevel = 0.8" in lua and "pcov = 0.78" in lua and "pintn = 89" in lua


def test_the_walk_forward_confidence_is_not_optimistic():
    """Out of sample can never be better than in sample on a real dataset."""
    from woweconomy.pricing.shipped import (
        evaluate_shipped_model,
        evaluate_shipped_model_walk_forward,
        fit_shipped_model,
    )

    import numpy as np

    frame = _synthetic_frame()
    # inject noise so there is something to overfit
    rng = np.random.default_rng(11)
    noise = rng.normal(0.0, 0.4, len(frame))
    frame["y_net_unit_log"] = frame["y_net_unit_log"] + noise

    model = fit_shipped_model(frame)
    in_sample = evaluate_shipped_model(model, frame)
    out_of_sample = evaluate_shipped_model_walk_forward(frame)
    assert out_of_sample["rmse_log"] >= in_sample["rmse_log"] - 1e-9


# ====================================================================== the dataset

def _fake_record(realm="Dragonmaw", item_id=100, price=1234, confidence=0.5,
                 sample=3, flags=("thin-sample",)):
    from woweconomy.pricing.engine import Estimate
    return Estimate(realm=realm, item_id=item_id, price=price,
                    confidence=confidence, sale_probability=0.4,
                    sample_size=sample, observed_at=1792400000,
                    flags=list(flags), recommended_ask=1500,
                    evidence={"dbmarket": 2000})


class _FakeEngine:
    """Just enough of the engine for the dataset builder."""

    def __init__(self):
        self.shipped_model = ShippedModel(
            intercept=0.1, coefficients=[0.2, 0.3], medians=[1.0, 2.0],
            references=["dbregionmarketavg", "dbregionsaleavg"])
        self._history_cache = {"Dragonmaw": {100: (3, 1792400000, [1000])}}
        self.interests = None
        self.ml_frame = None

    def realm_scope(self, realm):
        return "spinebreaker"

    def estimate(self, realm, item_id):
        return _fake_record(realm, item_id)

    def _build_curve(self):
        return None


def test_the_dataset_is_deterministic():
    """The same inputs must produce the same bytes."""
    config = PricingConfig()
    builder = DatasetBuilder(config=config, engine=_FakeEngine())
    builder.generated_at = 1792450000
    builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                    "items": {100: _fake_record()},
                                    "sampleSize": 3, "updatedAt": 1792400000}}
    builder.records = [type("R", (), {"realm": "Dragonmaw", "item_id": 100,
                                      "estimate": _fake_record()})()]
    first = builder.to_lua()
    second = builder.to_lua()
    assert first == second
    assert builder.checksum() == builder.checksum()


def test_the_content_signature_ignores_when_the_dataset_was_built():
    """Two exports of the same data must sign the same.

    The file checksum covers ``generatedAt``, which is right for "is this
    the same file" and wrong for "did anything change" -- and the
    new-dataset alert asks the second question.  Without this, every
    six-hourly export looks new even when nothing moved.
    """
    def build(generated_at):
        builder = DatasetBuilder(config=PricingConfig(), engine=_FakeEngine())
        builder.generated_at = generated_at
        builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                        "items": {100: _fake_record()},
                                        "sampleSize": 3, "updatedAt": 1792400000}}
        builder.records = [type("R", (), {"realm": "Dragonmaw", "item_id": 100,
                                          "estimate": _fake_record()})()]
        return builder

    first = build(1792450000)
    later = build(1792453600)      # an hour later, same data

    assert first.checksum() != later.checksum(), "the file really did change"
    assert first.content_signature() == later.content_signature()


def test_the_content_signature_ignores_noise_in_the_fourth_decimal():
    """A probability that drifts in the 4th decimal is not news.

    Measured on two real exports a minute apart: ``psell`` went 0.1619 ->
    0.1618 and nothing else in the file moved, because the features
    include how long ago the market was measured.  An alert per export
    would then be a heartbeat.
    """
    def build(sale_probability):
        from woweconomy.pricing.engine import Estimate

        def record():
            return Estimate(realm="Dragonmaw", item_id=100, price=1234,
                            confidence=0.5,
                            sale_probability=sale_probability,
                            sample_size=3, observed_at=1792400000,
                            flags=["thin-sample"], recommended_ask=1500,
                            evidence={"dbmarket": 2000})

        builder = DatasetBuilder(config=PricingConfig(), engine=_FakeEngine())
        builder.generated_at = 1792450000
        builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                        "items": {100: record()},
                                        "sampleSize": 3, "updatedAt": 1792400000}}
        builder.records = [type("R", (), {"realm": "Dragonmaw", "item_id": 100,
                                          "estimate": record()})()]
        return builder

    assert (build(0.1619).content_signature()
            == build(0.1618).content_signature())

    # ...but a move worth telling someone about still counts
    assert (build(0.1619).content_signature()
            != build(0.4019).content_signature())


def test_the_content_signature_notices_a_changed_price():
    """The signature must still see the data move, or it hides real news."""
    def build(price):
        builder = DatasetBuilder(config=PricingConfig(), engine=_FakeEngine())
        builder.generated_at = 1792450000
        builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                        "items": {100: _fake_record(price=price)},
                                        "sampleSize": 3, "updatedAt": 1792400000}}
        builder.records = [type("R", (), {
            "realm": "Dragonmaw", "item_id": 100,
            "estimate": _fake_record(price=price)})()]
        return builder

    assert build(1234).content_signature() != build(9999).content_signature()


def test_the_dataset_ships_the_limit_at_which_a_price_is_stale():
    """The reader must not invent its own staleness rule.

    The engine clamps confidence and flags a stale price at
    ``max_age_hours``; the in-game reader has to apply the same limit, so
    the number travels with the dataset instead of being copied into Lua.
    """
    config = PricingConfig()
    builder = DatasetBuilder(config=config, engine=_FakeEngine())
    builder.generated_at = 1792450000
    builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                    "items": {100: _fake_record()},
                                    "sampleSize": 3, "updatedAt": 1792400000}}
    text = builder.to_lua()
    assert f"maxAgeHours = {int(config.max_age_hours)}" in text
    assert "horizonHours = 24" in text


def test_the_dataset_validates_and_its_version_is_checked():
    config = PricingConfig()
    builder = DatasetBuilder(config=config, engine=_FakeEngine())
    builder.generated_at = 1792450000
    builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                    "items": {100: _fake_record()},
                                    "sampleSize": 3, "updatedAt": 1792400000}}
    text = builder.to_lua()
    result = validate_dataset(text)
    assert result["valid"], result["problems"]
    assert result["version"] == DATASET_VERSION
    assert result["records"] == 1


def test_a_dataset_of_the_wrong_version_is_refused():
    config = PricingConfig()
    builder = DatasetBuilder(config=config, engine=_FakeEngine())
    builder.generated_at = 1792450000
    text = builder.to_lua().replace(f"version = {DATASET_VERSION},",
                                    "version = 999,", 1)
    result = validate_dataset(text)
    assert not result["valid"]
    assert any("version" in problem for problem in result["problems"])


def test_a_record_without_a_positive_price_is_reported():
    config = PricingConfig()
    builder = DatasetBuilder(config=config, engine=_FakeEngine())
    builder.generated_at = 1792450000
    builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                    "items": {100: _fake_record()},
                                    "sampleSize": 3, "updatedAt": 1792400000}}
    text = builder.to_lua().replace("price = 1234", "price = 0", 1)
    result = validate_dataset(text)
    assert not result["valid"]
    assert any("price" in problem for problem in result["problems"])


def test_the_dataset_carries_no_character_data():
    """The in-game file must not carry the account's characters."""
    config = PricingConfig()
    builder = DatasetBuilder(config=config, engine=_FakeEngine())
    builder.generated_at = 1792450000
    builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                    "items": {100: _fake_record()},
                                    "sampleSize": 3, "updatedAt": 1792400000}}
    text = builder.to_lua()
    for secret in ("Bullbound", "Forgehand", "Paracelsus", "Silkweaver", "Tesla"):
        assert secret not in text


def test_the_dataset_names_the_auction_house_of_every_known_realm():
    """A realm with no record still has an auction house.

    The per-realm records only exist for the realms the account traded in,
    but the realm -> auction-house map is a property of TSM, not of the
    account.  Shipping it for every realm is what lets the addon explain
    where an item would be sold even where it has no history.
    """
    config = PricingConfig()
    builder = DatasetBuilder(config=config, engine=_FakeEngine())
    builder.generated_at = 1792450000
    builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                    "items": {100: _fake_record()},
                                    "sampleSize": 3, "updatedAt": 1792400000}}
    builder.realm_groups = {"Dragonmaw": "spinebreaker",
                            "Silvermoon": "silvermoon"}
    text = builder.to_lua()
    assert '["Dragonmaw"] = "spinebreaker"' in text
    assert '["Silvermoon"] = "silvermoon"' in text


# ====================================================================== the Lua source


class LuaHarness:
    """Loads the fork's Lua file with a mocked TSM around it."""

    def __init__(self, dataset=None, realm="Dragonmaw", region_prices=None):
        import lupa
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.registered = {}
        self.lua.execute("""
            _G.DFA_ESTIMATE = nil
            _G.DFA_ESTIMATE_SOURCE = nil
            local registry = {}
            _G.DFA_TEST_REGISTRY = registry
            _G.TSM = {
                LibTSMUtil = { Include = function() return { Info = function() end } end },
                Locale = { GetTable = function() return setmetatable({}, {__index = function() return "" end}) end },
                LibTSMTypes = { Include = function() return {
                    SOURCE_TYPE = { NORMAL = 1, PRICE_DB = 2, VOLATILE = 3 },
                    RegisterSource = function(module, key, label, callback, sourceType)
                        registry[module] = { key = key, label = label, callback = callback, sourceType = sourceType }
                    end,
                } end },
                LibTSMWoW = { Include = function() return {
                    GetRealmName = function() return _G.DFA_TEST_REALM end,
                } end },
                AuctionDB = { GetRegionItemData = function(itemString, key)
                    local prices = _G.DFA_TEST_REGION_PRICES or {}
                    local byKey = prices[itemString] or {}
                    return byKey[key]
                end },
            }
            _G.SessionInfo = nil
            _G.GetRealmName = function() return _G.DFA_TEST_REALM end
            _G.time = function() return 1792450000 end
        """)
        self.lua.globals().DFA_TEST_REALM = realm
        if region_prices:
            # the prices have to be a real Lua table too
            self.lua.execute(f"DFA_TEST_REGION_PRICES = {_lua_source(region_prices)}")
        else:
            self.lua.execute("DFA_TEST_REGION_PRICES = {}")
        if dataset is not None:
            self.lua.execute(f"{GLOBAL_NAME} = {_lua_source(dataset)}")
        # the file is loaded the way the game loads it: the addon name and
        # the TSM table are the varargs, and the source takes the second
        # one as `select(2, ...)`
        loader = self.lua.eval(
            "function(src) return assert(load(src, 'DFAEstimate.lua')) end")
        loader(SOURCE_TEXT)("DFAEstimate", self.lua.globals().TSM)

    def source(self):
        """The registered source, as a dict."""
        registry = self.lua.globals().DFA_TEST_REGISTRY
        for _, entry in registry.items():
            return {
                "module": _,
                "key": entry["key"],
                "label": entry["label"],
                "sourceType": entry["sourceType"],
            }
        return None

    def value(self, item_string):
        registry = self.lua.globals().DFA_TEST_REGISTRY
        for _, entry in registry.items():
            callback = entry["callback"]
            try:
                return callback(item_string)
            except Exception:
                # a Lua function that returns nil comes back as a
                # missing value rather than as None
                return None
        return None

    def source_api(self):
        return self.lua.globals().DFA_ESTIMATE_SOURCE


def _to_lua(value):
    """Convert a python dict/list to a lua table."""
    if isinstance(value, dict):
        return {k: _to_lua(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_lua(v) for v in value]
    return value


def _lua_source(value, indent: int = 0) -> str:
    """The dataset as Lua source.

    A python dict handed to a Lua global stays a python proxy, and
    ``type(x)`` then says "userdata" -- so the dataset has to be *built*
    in Lua for the source's own checks to see a table.
    """
    pad = "\t" * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, item in value.items():
            if isinstance(key, int):
                name = f"[{key}]"
            else:
                name = f'["{key}"]'
            lines.append(f"{pad}\t{name} = {_lua_source(item, indent + 1)},")
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        if not value:
            return "{}"
        lines = ["{"]
        for item in value:
            lines.append(f"{pad}\t{_lua_source(item, indent + 1)},")
        lines.append(pad + "}")
        return "\n".join(lines)
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


SOURCE_TEXT = FORK_SOURCE.read_text(encoding="utf-8") if FORK_SOURCE.exists() else ""


def _dataset(**overrides):
    data = {
        "version": DATASET_VERSION,
        "engineVersion": "1.0.0",
        "generatedAt": 1792450000,
        "horizonHours": 24,
        "model": {
            "intercept": 0.0,
            "coefficients": [0.5],
            "medians": [math.log1p(1000.0)],
            "references": ["dbregionsaleavg"],
            "residualSigma": 0.7,
        },
        "curve": [{"ratio": 1.275, "p": 0.6076, "n": 79}],
        "realmGroups": {"Dragonmaw": "spinebreaker"},
        "realms": {
            "Dragonmaw": {
                "group": "spinebreaker",
                "updatedAt": 1792440000,
                "sampleSize": 3,
                "items": {
                    100: {"price": 5000, "conf": 0.6, "psell": 0.5,
                          "tts": None, "n": 3, "seen": 1792440000,
                          "flags": ["thin-sample"]},
                },
            },
        },
        "appearances": {10: {"missing": 30, "sample": 100, "realms": 200,
                              "ownedPct": 20.0}},
    }
    data.update(overrides)
    return data


def test_the_source_registers_under_the_expected_key():
    harness = LuaHarness(_dataset())
    source = harness.source()
    assert source is not None
    assert source["key"] == "DFAEstimate"
    assert source["module"] == "DFA"


def test_the_source_returns_the_dataset_estimate_for_a_known_item():
    harness = LuaHarness(_dataset())
    assert harness.value("i:100") == 5000


def test_the_source_returns_nil_for_an_item_it_does_not_know():
    harness = LuaHarness(_dataset())
    assert harness.value("i:999") is None


def test_the_source_does_not_read_a_pet_as_an_item():
    """A pet's species id is not an item id.

    The dataset carries no pet data.  1,296 of the first 3,500 pet species
    ids collide with an item price in the shipped dataset, so reading
    ``p:765`` as item 765 would return a confidently wrong number instead
    of no number at all.
    """
    harness = LuaHarness(_dataset(prices={100: 5000, 765: 12345}))
    # the item answers, from its record and from the frozen table
    assert harness.value("i:100") == 5000
    assert harness.value("i:765") == 12345
    # the pet must not borrow either of them
    assert harness.value("p:100") is None
    assert harness.value("p:765") is None


def test_the_source_returns_nil_in_another_realm():
    """The estimate is per realm: another realm's number is not this one's."""
    harness = LuaHarness(_dataset(), realm="Silvermoon")
    assert harness.value("i:100") is None


def test_the_source_returns_nil_without_a_dataset():
    harness = LuaHarness(None)
    assert harness.value("i:100") is None


def test_the_source_still_reads_the_older_schema():
    """Schema 2 is additive; an old dataset must not blank the panel."""
    harness = LuaHarness(_dataset(version=1))
    assert harness.value("i:100") == 5000


def test_the_source_refuses_a_dataset_of_the_wrong_version():
    harness = LuaHarness(_dataset(version=999))
    assert harness.value("i:100") is None
    assert harness.source_api().GetError()


def test_the_source_uses_the_model_when_the_realm_has_no_record():
    """Every item gets an answer from the model, not only the traded ones."""
    prices = {"i:999": {"regionSale": 2000.0}}
    harness = LuaHarness(_dataset(), realm="Silvermoon", region_prices=prices)
    value = harness.value("i:999")
    assert value is not None
    assert value > 0
    # exp(0 + 0.5 * log(2001))
    assert math.isclose(value, math.floor(math.exp(0.5 * math.log(2001))), rel_tol=1e-6)


def test_the_source_falls_back_to_the_frozen_table_without_a_live_reference():
    """The dataset's price table covers items TSM has no live data for.

    The companion addon already reads ``data.prices``; the TSM source has
    to read it too, or an item that the addon can price has no
    ``dfaestimate`` value -- which is what makes the source look as if it
    only works for the items the account traded.
    """
    harness = LuaHarness(_dataset(prices={123: 4500}), realm="Silvermoon")
    assert harness.value("i:123") == 4500
    assert harness.source_api().GetFrozenPrice("i:123") == 4500


def test_the_source_prefers_the_live_model_to_the_frozen_table():
    """A live reference is more current than the build-time table."""
    prices = {"i:999": {"regionSale": 2000.0}}
    harness = LuaHarness(_dataset(prices={999: 4500}),
                         realm="Silvermoon", region_prices=prices)
    value = harness.value("i:999")
    assert value == math.floor(math.exp(0.5 * math.log(2001)))
    assert value != 4500


def test_the_frozen_fallback_never_returns_a_non_positive_price():
    harness = LuaHarness(_dataset(prices={123: 0, 124: -5}),
                         realm="Silvermoon")
    for item in ("i:123", "i:124", "i:0", "not an item string", ""):
        value = harness.value(item)
        assert value is None or value > 0


def test_the_source_never_returns_a_non_positive_price():
    harness = LuaHarness(_dataset())
    for item in ("i:100", "i:999", "i:0", "not an item string", ""):
        value = harness.value(item)
        assert value is None or value > 0


def test_the_source_exposes_the_realm_and_its_auction_house():
    harness = LuaHarness(_dataset())
    api = harness.source_api()
    assert api.GetRealmName() == "Dragonmaw"
    assert api.GetRealmGroup() == "spinebreaker"


def test_the_source_reads_the_measured_curve():
    harness = LuaHarness(_dataset())
    api = harness.source_api()
    assert math.isclose(api.GetCurveProbability(1.275), 0.6076, rel_tol=1e-6)


def test_the_source_registers_even_without_the_optional_helpers():
    """A missing helper must not cost the user the source.

    If the chat messenger or the session helper were unavailable, the
    whole chunk would die and the source would silently not exist -- which
    is what the user sees as "the source is not available".
    """
    harness = LuaHarness(_dataset())
    # take the optional pieces away
    harness.lua.execute("""
        _G.TSM.LibTSMService = nil
        _G.TSM.LibTSMWoW = nil
        _G.TSM.Locale = nil
    """)
    # reload the file with them missing
    loader = harness.lua.eval(
        "function(src) return assert(load(src, 'DFAEstimate.lua')) end")
    loader(SOURCE_TEXT)("DFAEstimate", harness.lua.globals().TSM)
    registry = harness.lua.globals().DFA_TEST_REGISTRY
    # the registry is keyed by module name, so its length is not the count
    assert registry["DFA"] is not None, "the source must still be registered"
    entry = registry["DFA"]
    assert entry["key"] == "DFAEstimate"
    # and it must still answer, without falling over: with the realm
    # helper gone it cannot find the realm, so it answers nil -- which is
    # the correct answer for "no estimate"
    try:
        value = entry["callback"]("i:100")
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"the source raised an error without its helpers: {exc}")
    assert value is None or value > 0


def test_the_source_key_is_writable_as_a_custom_string():
    """TSM's custom string language has no braces and wants letters only.

    A source is written bare -- `dfaestimate`, like `dbmarket` -- and a
    braced or punctuated name is not a valid token at all.  Getting this
    wrong makes the source look like it does not exist.
    """
    harness = LuaHarness(_dataset())
    source = harness.source()
    assert source["key"] == "DFAEstimate"
    # the tokenizer accepts an identifier only when it is letters only
    key = source["key"]
    assert key.isalpha(), f"'{key}' is not a valid custom string identifier"
    # and the custom string is lower cased before it is looked up, so the
    # registry has to answer to the lower case key
    registry = harness.lua.globals().DFA_TEST_REGISTRY
    entry = registry["DFA"]
    assert entry["key"].lower() == "dfaestimate"


def test_the_documentation_never_shows_the_braced_form():
    """The braced form is wrong, and it is what the user typed first.

    In the code the user would copy from, the braced form must not
    appear at all.  In the prose it may appear, but only where it is
    being called invalid.
    """
    for path in (REPO / "DFA_TSM/DFAEstimate.lua",
                 REPO / "DFA_Estimate/UI/Panel.lua",
                 REPO / "DFA_Estimate/UI/Auctionator.lua",
                 REPO / "DFA_Estimate/DFA_Estimate.lua"):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "{dfaestimate}" not in text.lower(), f"{path} shows the invalid syntax"

    for path in (REPO / "COMMANDS.md",
                 REPO / "reports/dfa_pricing/ARCHITECTURE.md"):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if "{dfaestimate}" in line.lower():
                lowered = line.lower()
                assert ("invalid" in lowered or "inválid" in lowered
                        or "não tem chavetas" in lowered), \
                    f"{path}: the braced form is shown as if it worked: {line.strip()}"


def test_the_auctionator_bridge_only_fills_the_field():
    """The bridge fills the price field and does nothing else.

    It must not post, cancel, undercut, or touch Auctionator's undercut
    preference: the user still clicks Post.
    """
    bridge_path = REPO / "DFA_Estimate/UI/Auctionator.lua"
    text = bridge_path.read_text(encoding="utf-8")
    for forbidden in ("PostItem", "RequestPost", "ConfirmPost", "CancelUndercut",
                      "AUCTION_SALES_PREFERENCE", "UNDERCUT_PERCENTAGE"):
        assert forbidden not in text, f"the bridge must not touch {forbidden}"
    # the fill is opt in
    assert "private.enabled" in text
    assert "fillAuctionatorPrice" in text


def test_the_auctionator_bridge_compiles_and_hooks_the_price():
    """The bridge loads into a real Lua runtime and fills the price field.

    This used to build its own bare runtime, which had no ``CreateFrame``:
    the bridge calls one while waiting for Auctionator's tab, so the test
    died before it reached a single assertion.  It uses the shared harness
    now, the same one the other bridge tests use.
    """
    lua = dfa.woW_like_lua()
    dfa.load_addon(lua)
    lua.execute("""
        _G.Addon.DFAEstimate = {
            data = { realms = {} },
            -- ``action.ask`` is the model's decision and already carries the
            -- house cut; the bridge posts it, rounded to a silver boundary.
            Estimate = function(id)
                return { price = 1000, action = { ask = 1053 }, conf = 0.5, n = 2 }
            end,
        }
        _G.__calls = {}
        _G.__frame = {
            -- Auctionator's itemInfo carries the item id; the bridge must
            -- read it instead of the item name that GetItemInfo returns
            itemInfo = { itemID = 1234, location = {} },
            UpdateSalesPrice = function(self, price) _G.__calls.price = price end,
        }
        _G.AuctionatorSellingFrame = { SaleItemFrame = _G.__frame }
    """)
    bridge = lua.globals().Addon.DFAEstimateAuctionator
    bridge.Enable()
    bridge.SetEnabled(True)
    bridge.ApplyEstimate(lua.globals().__frame)
    assert lua.globals().__calls.price == 1100   # ask 1053, next silver

    # turning the estimate off stops it writing at all
    bridge.SetEnabled(False)
    lua.execute("_G.__calls = {}")
    bridge.ApplyEstimate(lua.globals().__frame)
    assert lua.globals().__calls.price is None


def test_the_bridge_waits_for_the_selling_tab_instead_of_giving_up():
    """Auctionator builds its tabs when the auction house opens, not at login.

    The bridge used to try twice (load, login), find nothing and never try
    again -- so the price field stayed empty for the whole session.  This
    drives the shared harness: the event is delivered the way the client
    delivers it, not by reaching into a private frame.
    """
    lua = dfa.woW_like_lua()
    lua.execute("""
        _G.Addon.DFAEstimate = { data = { realms = {} }, Estimate = function() end }
        _G.Auctionator = {
            EventBus = { Fire = function() end },
            Selling = { Events = {
                BagItemClicked = "BAG_ITEM_CLICKED",
                ClearBagItem = "CLEAR_BAG_ITEM",
                PriceSelected = "PRICE_SELECTED",
            } },
        }
    """)
    dfa.load_addon(lua)
    bridge = lua.globals().Addon.DFAEstimateAuctionator
    bridge.SetEnabled(True)
    # no tab yet: the bridge must not claim to be hooked
    assert lua.eval("function(b) return b:Enable() end")(bridge) is False
    # the auction house opens and Auctionator builds the tab
    lua.execute("""
        _G.__price = nil
        _G.AuctionatorSellingFrame = {
            SaleItemFrame = {
                itemInfo = {},
                UpdateSalesPrice = function(self, price) _G.__price = price end,
            },
        }
    """)
    assert dfa.fire(lua, "AUCTION_HOUSE_SHOW") >= 1, \
        "the bridge never waited for Auctionator's tab"
    assert lua.eval("function(b) return b:Enable() end")(bridge) is True, \
        "the bridge did not hook the tab once it existed"
    # the original field update still runs, so the hook is transparent
    frame = lua.globals().AuctionatorSellingFrame.SaleItemFrame
    frame.UpdateSalesPrice(frame, 111)
    assert lua.globals().__price == 111


def test_the_tooltip_hooks_every_item_tooltip_not_only_links():
    """Hooking SetHyperlink misses the bags, the bank and the auction house.

    The modern path is TooltipDataProcessor, which runs after every item
    tooltip however it was built.  SetHyperlink stays as the fallback.
    """
    import lupa
    lua = _woW_like_lua()
    lua.execute("""
        _G.Enum = { TooltipDataType = { Item = 7 } }
        _G.TooltipDataProcessor = {
            AddTooltipPostCall = function(kind, callback)
                _G.__post = { kind = kind, callback = callback }
            end,
        }
    """)
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    frame = lua.globals()._lastFrame
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")
    post = lua.globals().__post
    assert post is not None, "the addon never registered a tooltip post-call"
    assert post.kind == 7
    # a tooltip built by the bags: nothing ever calls SetHyperlink on it
    tooltip = lua.eval("""
        {
            lines = {},
            GetItem = function(self) return "Sword", "|cff9d9d9d|Hitem:765|h[Sword]|h|r" end,
            AddLine = function(self, line) self.lines[#self.lines + 1] = line end,
        }
    """)
    post.callback(tooltip)
    lines = lua.eval("function(t) return table.concat(t.lines, '\\n') end")(tooltip)
    assert "DFA estimate" in lines, lines


def test_the_tooltip_shows_the_calibrated_range_and_its_coverage():
    """A price line carries the range and what the range actually delivered."""
    lua = _woW_like_lua()
    lua.execute("_G.GetRealmName = function() return 'Dragonmaw' end")
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    lua.execute("""
        local item = Addon.DFAEstimate.data.realms["Dragonmaw"].items[765]
        item.price = 15632
        item.plow = 7965
        item.phigh = 32757
        item.plevel = 0.8
        item.pcov = 0.78
        item.pintn = 89
        item.ereal = 16419
        item.elow = 7965
        item.ehigh = 32757
        item.elevel = 0.8
        item.ecov = 0.76
        item.eintn = 116
    """)
    text = lua.eval(
        "function() return table.concat(Addon.DFAEstimateTooltip.GetLines(765), '\\n') end")()
    assert "DFA estimate: 1.56g" in text, text
    assert "80% range: 0.80g - 3.28g (78% of 89 sales)" in text, text
    assert "Expected realized: 1.64g" in text, text
    assert "80% range: 0.80g - 3.28g (76% of 116 sales)" in text, text

    # An old record has no width: it gets a point line, never a fake range.
    lua.execute("""
        local item = Addon.DFAEstimate.data.realms["Dragonmaw"].items[765]            item.plow = nil
            item.phigh = nil
            item.plevel = nil
            item.pcov = nil
            item.pintn = nil
            item.elow = nil
            item.ehigh = nil
            item.elevel = nil
            item.ecov = nil
            item.eintn = nil
        """)
    text = lua.eval(
        "function() return table.concat(Addon.DFAEstimateTooltip.GetLines(765), '\\n') end")()
    assert "DFA estimate: 1.56g" in text, text
    assert "range" not in text, text


def test_the_frozen_model_path_carries_the_calibrated_range():
    """Without TSM or a record, the shipped model still answers with a range."""
    lua = _woW_like_lua()
    lua.execute("_G.TSM_API = nil")
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    lua.execute("""
        local model = Addon.DFAEstimate.data.model
        model.intervalLevel = 0.8
        model.intervalHalfWidth = 0.5
        model.intervalCoverage = 0.79
        model.intervalN = 124
        model.intervalMethod = "split-conformal"
    """)
    record = lua.eval("function() return Addon.DFAEstimate.EstimateFromModel(765) end")()
    assert record["source"] == "table", record["source"]
    assert record["plow"] == round(record["price"] * math.exp(-0.5))
    assert record["phigh"] == round(record["price"] * math.exp(0.5))
    assert record["plevel"] == 0.8
    assert record["pcov"] == 0.79
    assert record["pintn"] == 124


def test_the_tooltip_reads_the_item_id_from_the_tooltip_data():
    """In the auction house `GetItem()` returns nil; the data still has the id.

    The post-call is handed ``(tooltip, data)``, and ``data.id`` is the item
    id.  It is the only reliable source in the auction house, the bank and
    the merchant -- exactly where the player looks at an item -- so a hook
    that reads the link alone shows nothing there.
    """
    lua = _woW_like_lua()
    lua.execute("""
        _G.Enum = { TooltipDataType = { Item = 7 } }
        _G.TooltipDataProcessor = {
            AddTooltipPostCall = function(kind, callback)
                _G.__post = { kind = kind, callback = callback }
            end,
        }
    """)
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    frame = lua.globals()._lastFrame
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")
    post = lua.globals().__post
    assert post is not None, "the addon never registered a tooltip post-call"

    # a tooltip built by the client from data: GetItem() knows nothing
    tooltip = lua.eval("""
        {
            lines = {},
            GetItem = function(self) return nil, nil end,
            AddLine = function(self, line) self.lines[#self.lines + 1] = line end,
        }
    """)
    post.callback(tooltip, lua.eval("({ id = 765, type = 7 })"))
    lines = lua.eval("function(t) return table.concat(t.lines, '\\n') end")(tooltip)
    assert "DFA estimate" in lines, lines

    # a battle pet is not an item: its id is a species id, and the dataset
    # has no pet prices -- it must not be priced as if it were an item
    pet = lua.eval("""
        {
            lines = {},
            GetItem = function(self) return nil, nil end,
            AddLine = function(self, line) self.lines[#self.lines + 1] = line end,
        }
    """)
    post.callback(pet, lua.eval(
        "({ id = 19767, hyperlink = 'battlepet:19767:25:2:0:0:0:0:0' })"))
    pet_lines = lua.eval("function(t) return table.concat(t.lines, '\\n') end")(pet)
    assert "DFA estimate" not in pet_lines, pet_lines
    assert pet_lines == "", pet_lines


def test_a_variant_link_is_priced_as_its_base_item():
    """One price per base item.

    A link or a TSM string can carry bonus ids, an item level and a context;
    the dataset is keyed on the base item id, the first number.  Every form
    has to reduce to it, or the variant is priced as an unknown id and the
    tooltip stays empty for exactly the items people hover in the bags.
    """
    lua = _woW_like_lua()
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)

    base_item = lua.eval("function() return Addon.DFAEstimate.Estimate(765) end")()
    assert base_item is not None
    for form in ("item:765:0:0:0:0:0:0:0",
                 "|cff1eff00|Hitem:765:0:0:0:0:0:0:0|h[Silverleaf]|h|r",
                 "i:765:0:0:0:0:0:0:0"):
        variant = lua.eval("function(text) return Addon.DFAEstimate.Estimate(text) end")(form)
        assert variant is not None, form
        assert variant["price"] == base_item["price"], form
    assert lua.eval("function() return Addon.DFAEstimate.BaseItemId('p:765') end")() is None
    assert lua.eval("function() return Addon.DFAEstimate.BaseItemId(765) end")() == 765


def test_the_model_reads_tsm_references_by_their_bare_name():
    """A braced price source is not a price source.

    TSM's custom price language has no braces: ``{dbregionmarketavg}`` is not
    a price source and ``GetCustomPriceValue`` rejects it.  The model used to
    ask for the braced form, so every live reference came back nil and the
    estimate silently fell back to the frozen table.  The names have to be
    bare, exactly as the dataset ships them.
    """
    lua = _woW_like_lua()
    lua.execute("""
        _G.TSM_API = {
            calls = {},
            GetCustomPriceValue = function(source, itemString)
                _G.TSM_API.calls[#_G.TSM_API.calls + 1] = source .. "|" .. itemString
                if string.find(source, "{", 1, true) then
                    error("a braced price source is not a price source: " .. source)
                end
                return 12345
            end,
        }
    """)
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    frame = lua.globals()._lastFrame
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")

    record = lua.eval("function() return Addon.DFAEstimate.EstimateFromModel(765) end")()
    assert record is not None, "the model returned nothing for an item with references"
    assert record["references"] == 3, record["references"]
    calls = lua.eval("function() return table.concat(TSM_API.calls, ',') end")()
    assert "{" not in calls, calls
    assert "dbregionmarketavg|i:765" in calls, calls
    assert "dbregionhistorical|i:765" in calls, calls
    assert "dbregionsaleavg|i:765" in calls, calls


def test_the_slash_command_is_actually_registered():
    """A mixin's RegisterEvents is called by nobody.

    The addon owns its own event frame, and on ADDON_LOADED it registers
    the command.  Without that, /dfaestimate simply does not exist -- the
    command runs and WoW says "unknown command".
    """
    import lupa
    addon_path = REPO / "DFA_Estimate/DFA_Estimate.lua"
    text = addon_path.read_text(encoding="utf-8")
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        _G.ADDON_NAME = "DFA_Estimate"
        _G.CreateFrame = function() return { RegisterEvent = function() end,
            SetScript = function() end, UnregisterEvent = function() end } end
        _G.SlashCmdList = {}
        _G.Addon = {}
    """)
    loader = lua.eval(
        "function(src) return assert(load(src, 'DFA_Estimate.lua')) end")
    loader(text)("DFA_Estimate", lua.globals().Addon)
    # the event frame must be created and the event registered
    assert "RegisterEvent(\"ADDON_LOADED\")" in text
    assert "SlashCmdList[\"DFAESTIMATE\"]" in (REPO / "DFA_Estimate/UI/Panel.lua").read_text(
        encoding="utf-8")
    # and the command must be wired to the panel
    panel = (REPO / "DFA_Estimate/UI/Panel.lua").read_text(encoding="utf-8")
    assert "SLASH_DFAESTIMATE1" in panel


def test_the_addon_registers_itself_on_addon_loaded():
    """The panel's RegisterEvents has to be called by something.

    It is called through pcall now, so a broken panel cannot stop the
    tooltip hook from being installed.
    """
    text = (REPO / "DFA_Estimate/DFA_Estimate.lua").read_text(encoding="utf-8")
    assert 'RegisterEvent("ADDON_LOADED")' in text
    assert "Addon.DFAEstimatePanel.RegisterEvents" in text
    assert "Addon.DFAEstimatePanel.Initialize" in text


def test_a_broken_panel_cannot_turn_the_tooltip_off():
    """The hook must not depend on the panel's frame.

    CreateFrame can fail on a client whose templates changed.  When the
    panel was initialised before the hook, that error aborted ADDON_LOADED
    and the addon reported "tooltip off" with no reason.  The hook is now
    installed first, and the panel is called through pcall.
    """
    lua = _woW_like_lua()
    lua.execute("""
        _G.Enum = { TooltipDataType = { Item = 7 } }
        _G.TooltipDataProcessor = {
            AddTooltipPostCall = function(kind, callback)
                _G.__post = { kind = kind, callback = callback }
            end,
        }
    """)
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    # the panel fails to build, the way a changed frame template would
    lua.execute("""
        Addon.DFAEstimatePanel.Initialize = function() error("no such template") end
    """)
    frame = lua.globals()._lastFrame
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")
    assert lua.globals().__post is not None, "the panel took the tooltip down with it"
    enabled = lua.eval("function() return Addon.DFAEstimateTooltip.IsEnabled() end")()
    assert enabled is True
    panel_error = lua.eval("function() return Addon.DFAEstimate.panelError end")()
    assert "no such template" in str(panel_error)


def test_lootappraiser_recipes_weapons_and_armor_use_the_dfa_source():
    """LootAppraiser has per-class price sources; point three at us.

    LootAppraiser passes any class source string to TSM as a custom price
    source, so `dfaestimate` needs no code change on its side.  The
    integration writes the three class fields into LootAppraiser's own
    profile, remembers what was there, and can put it back.
    """
    lua = _woW_like_lua()
    lua.execute("""
        _G.DFA_ESTIMATE_DB = {}
        C_Item.GetItemInfoInstant = function(itemID)
            local classIDs = { [2] = 2, [4] = 4, [9] = 9 }
            return itemID, nil, nil, nil, nil, classIDs[itemID] or 0
        end
        _G.__lootappraiser = {
            PriceSources = {
                -- LootAppraiser's own resolver; the integration wraps it
                ResolveForItem = function(itemID, basePriceSource)
                    return "dbmarket"
                end,
            },
            db = { profile = {
                general = { useSubClasses = false },
                classTypeArmor = false,
                classTypeArmorPriceSource = "dbmarket",
                classTypeWeapon = false,
                classTypeWeaponPriceSource = "dbmarket",
                classTypeRecipe = false,
                classTypeRecipePriceSource = "dbmarket",
            } },
            CONST = { PRICE_SOURCE = { DBMarket = "TSM: Market Value" } },
            availablePriceSources = { DBMarket = "TSM: Market Value" },
            UI = { RefreshStatusText = function() end },
        }
        _G.LibStub = function(name, silent)
            if name ~= "AceAddon-3.0" then return nil end
            return {
                GetAddon = function(self, addonName, quiet)
                    if addonName == "LootAppraiser Reloaded" then
                        return _G.__lootappraiser
                    end
                end,
            }
        end
    """)
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua",
                     "Integration/LootAppraiser.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    frame = lua.globals()._lastFrame
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")
    frame.scripts["OnEvent"](frame, "PLAYER_LOGIN")

    profile = lua.eval("function() return _G.__lootappraiser.db.profile end")()
    assert profile.general.useSubClasses is True
    for item_class in ("Armor", "Weapon", "Recipe"):
        assert getattr(profile, "classType" + item_class) is True
        assert getattr(profile, "classType" + item_class + "PriceSource") == "dfaestimate"
    # the source is published for LootAppraiser's dropdown as well
    assert lua.globals().__lootappraiser.availablePriceSources["dfaestimate"] == \
        "DfA: Estimate"
    assert lua.globals().__lootappraiser.CONST.PRICE_SOURCE["dfaestimate"] == \
        "DfA: Estimate"

    # the class decision is made by class id, so it does not depend on the
    # client language; a reagent (class 5) is not intercepted
    resolve = lua.eval(
        "function(id) return _G.__lootappraiser.PriceSources.ResolveForItem("
        "id, 'dbmarket') end")
    assert resolve(2) == "dfaestimate"   # weapon
    assert resolve(4) == "dfaestimate"   # armor
    assert resolve(9) == "dfaestimate"   # recipe
    assert resolve(5) == "dbmarket"      # reagent

    # an explicit off restores the three values that were there before
    lua.eval("function() Addon.DFAEstimateLootAppraiser:Disable(true) end")()
    profile = lua.eval("function() return _G.__lootappraiser.db.profile end")()
    assert profile.general.useSubClasses is False
    for item_class in ("Armor", "Weapon", "Recipe"):
        assert getattr(profile, "classType" + item_class) is False
        assert getattr(profile, "classType" + item_class + "PriceSource") == "dbmarket"
    # the wrapper is still installed but no longer intercepts
    assert resolve(2) == "dbmarket"

    # and it stays off across the next login
    lua.eval("function() Addon.DFAEstimateLootAppraiser:OnLogin(true) end")()
    profile = lua.eval("function() return _G.__lootappraiser.db.profile end")()
    assert profile.classTypeArmor is False


def test_lootappraiser_reinstalls_its_resolver_after_a_reload():
    """The resolver is a Lua function; a saved "installed" flag is a trap.

    An earlier build stored `resolverInstalled = true` in SavedVariables.
    On the next login the live function was gone, but the flag said it was
    there, so the localized LootAppraiser resolver was left in place.
    """
    lua = _woW_like_lua()
    lua.execute("""
        -- the second session: the old saved table still says installed
        _G.DFA_ESTIMATE_DB = {
            lootappraiser = {
                enabled = true,
                resolverInstalled = true,
                originalResolver = nil,
                originals = {
                    useSubClasses = false,
                    classTypeArmor = true,
                    classTypeArmorPriceSource = "dbmarket",
                    classTypeWeapon = true,
                    classTypeWeaponPriceSource = "dbmarket",
                    classTypeRecipe = true,
                    classTypeRecipePriceSource = "dbmarket",
                },
            },
        }
        C_Item.GetItemInfoInstant = function(itemID)
            local classIDs = { [2] = 2, [4] = 4, [9] = 9 }
            return itemID, nil, nil, nil, nil, classIDs[itemID] or 0
        end
        _G.__lootappraiser = {
            PriceSources = {
                -- LootAppraiser's own resolver; on a Portuguese client its
                -- class override table does not match "Arma"/"Armadura"
                ResolveForItem = function(itemID, basePriceSource)
                    return "dbmarket"
                end,
                GetItemValue = function(itemID, priceSource)
                    _G.__get_value_calls = (_G.__get_value_calls or 0) + 1
                    return "tsm"
                end,
            },
            db = { profile = {
                general = { useSubClasses = true },
                classTypeArmor = true,
                classTypeArmorPriceSource = "dfaestimate",
                classTypeWeapon = true,
                classTypeWeaponPriceSource = "dfaestimate",
                classTypeRecipe = true,
                classTypeRecipePriceSource = "dfaestimate",
            } },
            CONST = { PRICE_SOURCE = { DBMarket = "TSM: Market Value" } },
            availablePriceSources = { DBMarket = "TSM: Market Value" },
            UI = { RefreshStatusText = function() end },
        }
        _G.LibStub = function(name)
            if name ~= "AceAddon-3.0" then return nil end
            return {
                GetAddon = function(self, addonName)
                    if addonName == "LootAppraiser Reloaded" then
                        return _G.__lootappraiser
                    end
                end,
            }
        end
    """)
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua",
                     "Integration/LootAppraiser.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    frame = lua.globals()._lastFrame
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")
    frame.scripts["OnEvent"](frame, "PLAYER_LOGIN")

    resolve = lua.eval(
        "function(id) return _G.__lootappraiser.PriceSources.ResolveForItem("
        "id, 'dbmarket') end")
    assert resolve(2) == "dfaestimate", \
        "a reload must reinstall the class-id resolver"
    assert resolve(4) == "dfaestimate"
    assert resolve(9) == "dfaestimate"
    assert resolve(5) == "dbmarket"
    # the saved table must no longer claim that a function is installed
    assert lua.eval(
        "function() return _G.DFA_ESTIMATE_DB.lootappraiser.resolverInstalled end"
    )() is None
    assert "resolver=installed" in lua.eval(
        "function() return Addon.DFAEstimateLootAppraiser:Status() end")()

    # the three classes are answered from the dataset, without TSM: the
    # realm's own record first, the frozen table when the realm has none
    lua.execute("""
        Addon.DFAEstimate.data.prices[2] = 1000
        Addon.DFAEstimate.data.prices[4] = 2000
        Addon.DFAEstimate.data.prices[9] = 3000
        local realm = Addon.DFAEstimate.GetRealmName()
        local block = Addon.DFAEstimate.data.realms[realm]
        if not block then
            block = { items = {} }
            Addon.DFAEstimate.data.realms[realm] = block
        end
        block.items = block.items or {}
        _G.__old_record_2 = block.items[2]
        block.items[2] = { price = 900 }
        _G.__get_value_calls = 0
    """)
    get_value = lua.eval(
        "function(id, source) return "
        "_G.__lootappraiser.PriceSources.GetItemValue(id, source) end")
    assert get_value(2, "dfaestimate") == 900   # realm record wins
    assert get_value(4, "dfaestimate") == 2000  # frozen fallback
    assert get_value(9, "dfaestimate") == 3000  # frozen fallback
    assert lua.globals().__get_value_calls == 0, \
        "the dataset price must not go through TSM"
    lua.execute("""
        local realm = Addon.DFAEstimate.GetRealmName()
        Addon.DFAEstimate.data.realms[realm].items[2] = _G.__old_record_2
    """)
    assert get_value(2, "dfaestimate") == 1000  # frozen fallback
    # a class outside the three, or another source, still uses LootAppraiser
    assert get_value(5, "dfaestimate") == "tsm"
    assert get_value(2, "dbmarket") == "tsm"
    assert lua.globals().__get_value_calls == 2
    assert lua.eval(
        "function() return Addon.DFAEstimateLootAppraiser:IsEnabled() end")() is True

    # LootAppraiser's own switches still decide; the wrapper only fixes the
    # localized class name
    lua.eval("function() _G.__lootappraiser.db.profile.general.useSubClasses = false end")()
    assert resolve(2) == "dbmarket"
    lua.eval("function() _G.__lootappraiser.db.profile.general.useSubClasses = true end")()
    lua.eval("function() _G.__lootappraiser.db.profile.classTypeWeapon = false end")()
    assert resolve(2) == "dbmarket"
    assert resolve(4) == "dfaestimate"


def _woW_like_lua():
    """A Lua runtime that looks enough like the game to load the addon."""
    import lupa
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        -- the 5.1 globals the game has and 5.5 does not
        strlower = string.lower
        strmatch = string.match
        strfind = string.find
        strsub = string.sub
        strrep = string.rep
        format = string.format
        tinsert = table.insert
        tremove = table.remove
        gsub = string.gsub
        gmatch = string.gmatch
        wipe = function(t) for k in pairs(t) do t[k] = nil end end
        _G.time = function() return 1792450000 end
        _G.ADDON_NAME = "DFA_Estimate"
        _G.Addon = {}
        _G.SlashCmdList = {}
        _G.DEFAULT_CHAT_FRAME = { messages = {}, AddMessage = function(self, m)
            self.messages[#self.messages + 1] = m end }
        _G.UIParent = { CreateFontString = function() return {} end }
        _G.GameTooltip = { SetHyperlink = function() end, AddLine = function() end }
        _G.C_Item = { GetItemInfo = function() return "item:2318" end }
        _G._events = {}
        _G.CreateFrame = function()
            local frame = { scripts = {}, events = {} }
            function frame:RegisterEvent(name) self.events[name] = true end
            function frame:UnregisterEvent(name) self.events[name] = nil end
            function frame:SetScript(name, handler) self.scripts[name] = handler end
            function frame:Show() self.shown = true end
            function frame:Hide() self.shown = false end
            function frame:SetText(t) self.text = t end
            function frame:SetPoint() end
            function frame:SetSize() end
            function frame:EnableMouse() end
            function frame:RegisterForDrag() end
            function frame:SetMovable() end
            function frame:SetClampedToScreen() end
            function frame:SetFrameStrata() end
            function frame:CreateFontString()
                return setmetatable({}, { __index = function() return function() end end })
            end
            function frame:GetParent() return nil end
            function frame:StartMoving() end
            function frame:StopMovingOrSizing() end
            -- anything else the addon asks for is a no-op
            setmetatable(frame, { __index = function(t, k)
                if type(k) ~= "string" then return nil end
                local stub = function() end
                rawset(t, k, stub)
                return stub
            end })
            _G._lastFrame = frame
            return frame
        end
    """)
    return lua


def test_the_addon_loads_and_its_command_works():
    """The whole addon, loaded the way the game loads it.

    Every file of the addon is loaded in the order of its TOC, then
    ADDON_LOADED is fired.  The command has to exist afterwards, and
    running it has to say something.
    """
    import lupa
    lua = _woW_like_lua()
    addon_dir = REPO / "DFA_Estimate"
    order = ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
             "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua",
             "Integration/LootAppraiser.lua"]
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in order:
        path = addon_dir / relative
        loader(path.read_text(encoding="utf-8"))("DFA_Estimate", lua.globals().Addon)

    # the addon must have an event frame waiting for ADDON_LOADED
    frame = lua.globals()._lastFrame
    assert frame is not None
    assert frame.events["ADDON_LOADED"], "the addon does not wait for ADDON_LOADED"

    # fire it
    # the client calls the OnEvent script with (frame, event, addonName);
    # a frame cannot have a script per event
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")

    # the command must now exist
    assert "DFAESTIMATE" in lua.globals().SlashCmdList, \
        "the slash command was never registered"
    # and running it must say something
    lua.globals().SlashCmdList["DFAESTIMATE"]("/dfaestimate")
    messages = lua.globals().DEFAULT_CHAT_FRAME.messages
    assert len(messages) > 0, "the command ran but said nothing"
    assert "DFA Estimate" in messages[1]


def test_the_auctionator_bridge_is_enabled_when_auctionator_is_there():
    """The bridge hooks the selling frame as soon as Auctionator is up."""
    import lupa
    lua = _woW_like_lua()
    lua.execute("""
        _G.AuctionatorSellingFrame = {
            SaleItemFrame = {
                itemInfo = { location = {} },
                UpdateSalesPrice = function(self, price) _G.__price = price end,
            },
        }
    """)
    addon_dir = REPO / "DFA_Estimate"
    loader = lua.eval("function(src) return assert(load(src, 'addon.lua')) end")
    for relative in ["Data/DFA_Estimate_Data.lua", "DFA_Estimate.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"]:
        loader((addon_dir / relative).read_text(encoding="utf-8"))(
            "DFA_Estimate", lua.globals().Addon)
    frame = lua.globals()._lastFrame
    # the client calls the OnEvent script with (frame, event, addonName);
    # a frame cannot have a script per event
    frame.scripts["OnEvent"](frame, "ADDON_LOADED", "DFA_Estimate")
    bridge = lua.globals().Addon.DFAEstimateAuctionator
    assert bridge.IsEnabled() is True, \
        "the bridge starts on; /dfaestimate fill off is the opt-out"
    bridge.SetEnabled(False)
    assert bridge.IsEnabled() is False
    bridge.SetEnabled(True)
    assert bridge.IsEnabled() is True
    # and the hook is in place
    assert lua.globals().AuctionatorSellingFrame.SaleItemFrame.UpdateSalesPrice \
        is not None


def test_the_source_does_not_write_to_the_operations():
    """Registering a price source must not touch an operation."""
    harness = LuaHarness(_dataset())
    text = FORK_SOURCE.read_text(encoding="utf-8")
    for forbidden in ("AuctioningOperation", "SniperOperation", "ShoppingOperation",
                      "GroupOperation", "RegisterCustomSource", "AddCustomSource",
                      "SetActiveProfile", "Group.SetItem"):
        assert forbidden not in text, f"the fork must not touch {forbidden}"


# ====================================================================== the patch


def test_the_patch_refuses_a_tsm_it_was_not_written_for(tmp_path):
    from scripts import dfa_tsm_patch  # noqa: E402

    tsm = tmp_path / "TradeSkillMaster"
    tsm.mkdir()
    (tsm / "TradeSkillMaster.toc").write_text(
        "## Version: v0.0.01\nCore\\API.lua\n", encoding="utf-8")
    result = dfa_tsm_patch.status(tsm)
    assert not result.installed
    assert any("written for TSM" in problem for problem in result.problems)


def test_the_patch_applies_and_reverts_without_losing_the_original(tmp_path):
    from scripts import dfa_tsm_patch  # noqa: E402

    tsm = tmp_path / "TradeSkillMaster"
    tsm.mkdir()
    original = "## Version: v4.14.78\nCore\\API.lua\n"
    (tsm / "TradeSkillMaster.toc").write_text(original, encoding="utf-8")
    # the APIs the fork needs, as empty stubs: the check is for their presence
    for relative, pattern in dfa_tsm_patch.REQUIRED_API:
        path = tsm / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # the stub has to contain what the patch looks for: the check is
        # that the API is really there
        path.write_text(f"-- stub\n{pattern.replace(chr(92), '')}\n", encoding="utf-8")

    result = dfa_tsm_patch.apply(tsm)
    assert result.installed, result.problems
    text = (tsm / "TradeSkillMaster.toc").read_text(encoding="utf-8")
    assert dfa_tsm_patch.TOC_LINE in text
    # every original line is still there
    for line in original.strip().splitlines():
        assert line in text
    assert (tsm / dfa_tsm_patch.FORK_FILE_NAME).exists()

    result = dfa_tsm_patch.revert(tsm)
    assert not result.installed
    assert (tsm / "TradeSkillMaster.toc").read_text(encoding="utf-8") == original
    assert not (tsm / dfa_tsm_patch.FORK_FILE_NAME).exists()


def test_the_patch_is_idempotent(tmp_path):
    from scripts import dfa_tsm_patch  # noqa: E402

    tsm = tmp_path / "TradeSkillMaster"
    tsm.mkdir()
    (tsm / "TradeSkillMaster.toc").write_text(
        "## Version: v4.14.78\nCore\\API.lua\n", encoding="utf-8")
    for relative, pattern in dfa_tsm_patch.REQUIRED_API:
        path = tsm / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # the stub has to contain what the patch looks for: the check is
        # that the API is really there
        path.write_text(f"-- stub\n{pattern.replace(chr(92), '')}\n", encoding="utf-8")
    dfa_tsm_patch.apply(tsm)
    dfa_tsm_patch.apply(tsm)
    text = (tsm / "TradeSkillMaster.toc").read_text(encoding="utf-8")
    assert text.count(dfa_tsm_patch.TOC_MARKER) == 1


# ====================================================================== the real install


@pytest.mark.skipif(not TSM_DIR.exists(), reason="TSM is not installed here")
def test_the_installed_tsm_has_the_apis_the_fork_needs():
    from scripts import dfa_tsm_patch  # noqa: E402
    problems = dfa_tsm_patch.check_api(TSM_DIR)
    assert not problems, problems


@pytest.mark.skipif(not TSM_DIR.exists(), reason="TSM is not installed here")
def test_the_fork_does_not_ship_broken_lua():
    """The file the fork adds must at least be syntactically valid Lua."""
    import lupa
    lua = lupa.LuaRuntime()
    lua.execute("local function noop() end")
    try:
        lua.compile(SOURCE_TEXT)
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"the fork's Lua does not compile: {exc}")

