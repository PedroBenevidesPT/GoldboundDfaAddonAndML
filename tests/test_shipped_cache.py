"""The shipped fit is reused only when the inputs are the same.

Building the training frame and fitting on it cost 13 s and the demand
curve measured on the same frame cost another 18 s of frame rebuilding.
Both are now stored together, keyed by a fingerprint of the training
inputs.  These tests pin the contract: a hit reuses, a changed input
refits, and a failed refit raises instead of quietly serving the old
model.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from woweconomy.ml.config import MLConfig
from woweconomy.pricing.config import PricingConfig
from woweconomy.pricing.dataset import DatasetBuilder
from woweconomy.pricing.engine import PricingEngine
from woweconomy.pricing.shipped import ShippedModel


class _StubLearning:
    """What ``DfaLearningEngine`` has to provide to the fit."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.fits = 0

    def load(self) -> "_StubLearning":
        return self

    def build_rows(self):
        return self.frame, {}, {}


def _config(tmp_path: Path) -> PricingConfig:
    import sqlite3

    tsm = tmp_path / "tsm.db"
    dfa = tmp_path / "dfa.db"
    # real (empty) databases: the fingerprint reads the mirror's counters, and
    # rewriting the files would move the mtime the fallback hashes
    for path in (tsm, dfa):
        if not path.exists():
            sqlite3.connect(path).close()
    config = PricingConfig()
    config.tsm_db = tsm
    config.dfa_db = dfa
    config.models_dir = tmp_path / "models"
    config.models_dir.mkdir(exist_ok=True)
    return config


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "ref_local_market": [10.0, 20.0, 30.0, 40.0],
        "ref_local_recent": [10.0, 20.0, 30.0, 40.0],
        "ref_local_historical": [10.0, 20.0, 30.0, 40.0],
        "ref_region_market": [10.0, 20.0, 30.0, 40.0],
        "ref_region_historical": [10.0, 20.0, 30.0, 40.0],
        "ref_region_sale_avg": [10.0, 20.0, 30.0, 40.0],
        "y_net_unit_log": [1.0, 2.0, 3.0, 4.0],
    })


def _fit_with(monkeypatch, fitting: list) -> None:
    """Make ``fit_shipped_model`` return a known model and count calls."""

    def fake_fit(frame):
        fitting.append(1)
        return ShippedModel(intercept=1.0,
                            coefficients=[0.1] * 6,
                            medians=[0.0] * 6,
                            references=["dbmarket", "dbrecent", "dbhistorical",
                                        "dbregionmarketavg", "dbregionhistorical",
                                        "dbregionsaleavg"],
                            residual_sigma=0.5, n_train=len(frame),
                            n_complete=len(frame))

    monkeypatch.setattr("woweconomy.pricing.shipped.fit_shipped_model", fake_fit)
    monkeypatch.setattr("woweconomy.pricing.shipped.evaluate_shipped_model",
                        lambda model, frame: {"n": len(frame), "within_25pct": 0.5,
                                              "within_50pct": 0.8})
    monkeypatch.setattr(
        "woweconomy.pricing.shipped.evaluate_shipped_model_walk_forward",
        lambda frame: {"n": len(frame), "within_25pct": 0.4, "within_50pct": 0.75})


def _engine(tmp_path, monkeypatch, fitting):
    config = _config(tmp_path)
    _fit_with(monkeypatch, fitting)
    monkeypatch.setattr("woweconomy.ml.DfaLearningEngine",
                        lambda ml_config: _StubLearning(_frame()))
    return PricingEngine(config, MLConfig(dfa_db=config.dfa_db))


def test_a_second_fit_with_the_same_inputs_reuses_the_first(tmp_path, monkeypatch):
    fitting: list = []
    first = _engine(tmp_path, monkeypatch, fitting)
    first.fit_shipped_model()
    assert fitting == [1]
    assert not first._shipped_cache_hit

    second = _engine(tmp_path, monkeypatch, fitting)
    model = second.fit_shipped_model()
    assert fitting == [1], "the second export refitted"
    assert second._shipped_cache_hit is True
    assert model.coefficients == [0.1] * 6
    # the measurement travels with the fit
    assert model.within_25 == pytest.approx(0.4)
    assert second.shipped_evaluation_out_of_sample.get("n") == 4


def test_a_changed_input_refits(tmp_path, monkeypatch):
    fitting: list = []
    first = _engine(tmp_path, monkeypatch, fitting)
    first.fit_shipped_model()

    # a new snapshot lands in the mirror
    first.config.tsm_db.write_bytes(b"tsm and something new")

    second = _engine(tmp_path, monkeypatch, fitting)
    second.fit_shipped_model()
    assert fitting == [1, 1], "a changed database did not invalidate the fit"
    assert second._shipped_cache_hit is False


def test_a_changed_horizon_refits(tmp_path, monkeypatch):
    fitting: list = []
    first = _engine(tmp_path, monkeypatch, fitting)
    first.fit_shipped_model()

    other = _engine(tmp_path, monkeypatch, fitting)
    other.config.horizon_h = 48
    other.fit_shipped_model()
    assert fitting == [1, 1], "the horizon is part of the fit's inputs"


def test_a_sync_that_imports_nothing_does_not_invalidate_the_fit(tmp_path, monkeypatch):
    """TSM rewrites its AppData constantly; the fit must survive an empty sync."""
    import os
    import sqlite3

    fitting: list = []
    config = _config(tmp_path)
    con = sqlite3.connect(config.tsm_db)
    con.executescript("""
        CREATE TABLE tsm_snapshot (snapshot_id INTEGER PRIMARY KEY, scope TEXT,
            kind TEXT, updated_at TEXT, fetched_at TEXT);
        CREATE TABLE auction_events (event_id TEXT PRIMARY KEY,
            timestamp_unix INTEGER NOT NULL);
        INSERT INTO tsm_snapshot VALUES (1, 'dragonmaw', 'realm_items', '', '');
        INSERT INTO auction_events VALUES ('a', 100);
    """)
    con.commit()
    con.close()

    _fit_with(monkeypatch, fitting)
    monkeypatch.setattr("woweconomy.ml.DfaLearningEngine",
                        lambda ml_config: _StubLearning(_frame()))
    first = PricingEngine(config, MLConfig(dfa_db=config.dfa_db))
    first.fit_shipped_model()
    assert fitting == [1]

    # the same content, a new mtime: this is what a no-op sync looks like
    os.utime(config.tsm_db, (1_800_000_000, 1_800_000_000))
    second = PricingEngine(config, MLConfig(dfa_db=config.dfa_db))
    second.fit_shipped_model()
    assert fitting == [1], "an untouched database forced a refit"
    assert second._shipped_cache_hit is True

    # a real import moves the counter and must refit
    con = sqlite3.connect(config.tsm_db)
    con.execute("INSERT INTO tsm_snapshot VALUES (2, 'dragonmaw', 'realm_items', '', '')")
    con.commit()
    con.close()
    third = PricingEngine(config, MLConfig(dfa_db=config.dfa_db))
    third.fit_shipped_model()
    assert fitting == [1, 1], "a new snapshot did not invalidate the fit"
    assert third._shipped_cache_hit is False


def test_a_failed_refit_raises_instead_of_serving_the_old_model(tmp_path, monkeypatch):
    fitting: list = []
    first = _engine(tmp_path, monkeypatch, fitting)
    first.fit_shipped_model()

    second = _engine(tmp_path, monkeypatch, fitting)

    def broken(frame):
        raise RuntimeError("the frame is empty")

    monkeypatch.setattr("woweconomy.pricing.shipped.fit_shipped_model", broken)
    with pytest.raises(RuntimeError):
        second.fit_shipped_model(force=True)
    assert second.shipped_model is None
    assert second._shipped_cache_hit is False


def test_the_curve_is_stored_with_the_fit_it_was_measured_on(tmp_path, monkeypatch):
    fitting: list = []
    first = _engine(tmp_path, monkeypatch, fitting)
    first.fit_shipped_model()
    curve = [{"ratio": 0.25, "p": 0.61, "n": 79}, {"ratio": 0.775, "p": 0.42, "n": 120}]
    first.store_cached_curve(curve)

    payload = json.loads((first.config.models_dir / "shipped_fit.json").read_text())

    assert payload["curve"] == curve
    second = _engine(tmp_path, monkeypatch, fitting)
    second.fit_shipped_model()
    assert second._cached_curve == curve


def test_the_cached_curve_is_used_without_rebuilding_the_frame(tmp_path, monkeypatch):
    fitting: list = []
    engine = _engine(tmp_path, monkeypatch, fitting)
    engine.fit_shipped_model()
    engine.store_cached_curve([{"ratio": 1.0, "p": 0.33, "n": 40}])

    cached = _engine(tmp_path, monkeypatch, fitting)
    cached.fit_shipped_model()
    cached.ml_frame = None  # a cache hit never builds one
    builder = DatasetBuilder(config=cached.config, engine=cached)

    builder._build_curve()

    assert builder.curve == [{"ratio": 1.0, "p": 0.33, "n": 40}]
    assert cached.ml_frame is None, "the frame was rebuilt for a cached curve"


def test_a_measured_curve_is_stored_for_the_next_export(tmp_path, monkeypatch):
    fitting: list = []
    engine = _engine(tmp_path, monkeypatch, fitting)
    engine.fit_shipped_model()
    frame = pd.DataFrame({
        "ask_unit": [1.0] * 8 + [2.0] * 8,
        "ref_region_sale_avg": [1.0] * 16,
        "label_known_24h": [1] * 16,
        "sold_within_24h": [1, 1, 1, 0, 0, 0, 0, 0] + [0] * 8,
    })
    monkeypatch.setattr("woweconomy.ml.DfaLearningEngine",
                        lambda ml_config: _StubLearning(frame))
    engine.ml_frame = None
    builder = DatasetBuilder(config=engine.config, engine=engine)

    builder._build_curve()

    # the band midpoints: (0.85, 1.0] and (1.4, 2.0]
    assert [point["ratio"] for point in builder.curve] == [0.925, 1.7]
    assert builder.curve[0]["p"] == pytest.approx(0.375)
    assert builder.curve[1]["p"] == pytest.approx(0.0)
    payload = json.loads((engine.config.models_dir / "shipped_fit.json").read_text())
    assert payload["curve"] == builder.curve
