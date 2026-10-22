"""Tests for the validated DFA Sentinel prediction layer.

The tests use synthetic frames with a fabricated, known relationship, so a
failure points at the pipeline rather than at the live market.  They cover
the parts that are easy to get subtly wrong: the chronological split, the
right-censoring rule, calibration, baselines, artifact promotion, and the
``unknown`` answer for sparse data.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from woweconomy.ml.intervals import calibrate_interval
from woweconomy.ml.sentinel import (
    Calibrator,
    SentinelConfig,
    SentinelPredictor,
    SentinelTrainer,
    build_risk_examples,
    chronological_split,
    classification_metrics,
    expected_calibration_error,
    fit_calibrator,
    load_artifact,
    retrain_decision,
    risk_features_from_rows,
)
from woweconomy.pricing.config import DATASET_VERSION
from woweconomy.pricing.dataset import DatasetBuilder, DatasetRecord, validate_dataset
from woweconomy.pricing.engine import Estimate


def _synthetic_frame(days: int = 12, per_day: int = 30, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 1_700_000_000
    rows = []
    for day in range(days):
        for index in range(per_day):
            ts = base + day * 86_400 + index * 60
            price = float(1200 * np.exp(rng.normal(0, 0.25)))
            # a real signal: cheaper asks and fresher references sell more
            score = rng.normal() - 0.8 * (index % 5) / 4.0
            sold = int(score > -0.2)
            rows.append({
                "ts": ts, "date": ts // 86_400,
                "item_id": int(index % 8), "realm": "Dragonmaw" if index % 2 else "Silvermoon",
                "ask_unit": price * (1.0 + 0.1 * (index % 5)),
                "qty": 1.0, "local_present": 1.0, "region_present": 1.0,
                "local_market_log": np.log(price),
                "local_min_buyout_log": np.log(price * 0.95),
                "local_recent_log": np.log(price * 1.02),
                "local_historical_log": np.log(price * 1.05),
                "region_market_log": np.log(price * 1.1),
                "region_historical_log": np.log(price * 1.08),
                "region_sale_avg_log": np.log(price),
                "region_sale_rate": 0.3 + (index % 5) * 0.05,
                "region_sold_per_day": 5.0 + (index % 5),
                "cat_item_class": "consumable" if index % 2 else "material",
                "cat_realm": "dragonmaw",
                "y_net_unit": price * 0.92,
                "y_net_unit_log": np.log(price * 0.92),
                "outcome": "sale" if sold else "expired",
                "sold_within_24h": sold,
                "label_known_24h": 1,
                "sold_within_48h": sold,
                "label_known_48h": 1,
                "sold_within_168h": sold,
                "label_known_168h": 1,
                "tts_hours": float(rng.uniform(2, 100)),
                "tts_censored": 0,
            })
    return pd.DataFrame(rows)


def _config(tmp_path: Path) -> SentinelConfig:
    return SentinelConfig(
        models_root=tmp_path / "models",
        reports_dir=tmp_path / "reports",
        # keep unit tests off the live TSM database; the risk panel is
        # tested separately with a known, small fixture
        tsm_db=tmp_path / "missing.db",
        horizons_h=(24,),
        risk_horizons_h=(24,),
        iterations=10,
        min_sale_labels=80,
        min_sale_positives=20,
        min_price_rows=60,
        min_tts_rows=100,
        min_tts_events=20,
        min_risk_rows=20,
        min_risk_positives=3,
    )


# ------------------------------------------------------------------ split / leakage

def test_the_split_is_strictly_chronological():
    frame = _synthetic_frame()
    split = chronological_split(frame, test_fraction=0.25, calibration_fraction=0.2)
    assert split.usable
    assert split.train["ts"].max() < split.calibration["ts"].min()
    assert split.calibration["ts"].max() < split.test["ts"].min()
    # no timestamp may appear in two blocks
    assert not (set(split.train["ts"]) & set(split.test["ts"]))


def test_future_rows_are_not_in_the_training_block():
    frame = _synthetic_frame()
    split = chronological_split(frame)
    cutoff = split.train["ts"].max()
    assert (frame[frame["ts"] > cutoff].shape[0]
            == split.calibration.shape[0] + split.test.shape[0])


def test_a_short_history_returns_an_explicit_unusable_split():
    frame = _synthetic_frame(days=1, per_day=2)
    split = chronological_split(frame)
    assert not split.usable
    assert split.note


# ---------------------------------------------------------------- calibration

def test_isotonic_calibration_reduces_calibration_error():
    rng = np.random.default_rng(7)
    p = rng.uniform(0.05, 0.95, 800)
    true_probability = 0.2 + 0.6 * p
    y = (rng.uniform(size=800) < true_probability).astype(float)
    calibrator = fit_calibrator(y, p)
    assert calibrator.method in ("isotonic", "platt")
    calibrated = calibrator.transform(p)
    assert expected_calibration_error(y, calibrated) < expected_calibration_error(y, p)


def test_an_identity_calibrator_is_explicit_when_there_is_too_little_data():
    calibrator = fit_calibrator(np.array([0, 1]), np.array([0.2, 0.8]))
    assert calibrator.method == "identity"
    assert "too few" in calibrator.note


def test_classification_metrics_report_more_than_accuracy():
    metrics = classification_metrics(np.array([0, 1, 0, 1]), np.array([0.1, 0.9, 0.2, 0.8]))
    for key in ("brier", "logloss", "auc", "ece", "precision_at_0_5", "recall_at_0_5"):
        assert key in metrics


# ------------------------------------------------------------ right censoring

def test_only_rows_with_a_known_outcome_are_labelled():
    frame = _synthetic_frame(days=2, per_day=10)
    # A resolved sale or expiry has a final 168h label even when only two
    # days of wall-clock history exist.  A censored listing does not.
    frame.loc[frame.index[:5], "label_known_168h"] = 0
    frame.loc[frame.index[:5], "sold_within_168h"] = np.nan
    cfg = _config(Path("/tmp"))
    cfg.horizons_h = (168,)
    cfg.min_sale_labels = 5
    cfg.min_sale_positives = 2
    from woweconomy.ml.sentinel import _prepare_horizon_frame
    usable = _prepare_horizon_frame(frame, 168, int(frame["ts"].max()))
    assert len(usable) == len(frame) - 5
    assert usable["sold_within_168h"].notna().all()


def test_risk_examples_only_include_closed_horizons():
    t0 = 1_700_000_000
    panel = pd.DataFrame([
        {"ts": t0, "scope": "dragonmaw", "item_id": 1, "market": 1000.0,
         "min_buyout": 900.0, "recent": 1000.0, "historical": 1100.0,
         "avg_sale_price": 950.0, "sale_rate": 0.4, "sold_per_day": 5.0},
        {"ts": t0 + 24 * 3600, "scope": "dragonmaw", "item_id": 1, "market": 800.0,
         "min_buyout": 700.0, "recent": 850.0, "historical": 1050.0,
         "avg_sale_price": 900.0, "sale_rate": 0.35, "sold_per_day": 4.0},
        # no snapshot after +48h, so the 24h example for the second row is censored
    ])
    examples = build_risk_examples(panel, horizons_h=(24, 48))
    assert list(examples["horizon_h"]) == [24]
    row = examples.iloc[0]
    assert row["undercut_pressure"] == 1
    assert row["price_decline"] == 1
    # features come from the *current* snapshot only
    assert row["risk_market_log"] == pytest.approx(np.log(1000.0))


# ------------------------------------------------------------------- trainer

def test_training_promotes_a_versioned_artifact_and_predicts(tmp_path):
    frame = _synthetic_frame()
    cfg = _config(tmp_path)
    result = SentinelTrainer(cfg).train(frame, observation_end=int(frame["ts"].max()))
    assert result.promoted
    assert (tmp_path / "models" / "CURRENT").exists()
    manifest = json.loads((Path(result.path) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "dfa-sentinel/1"
    assert manifest["dataset_schema"] == DATASET_VERSION
    assert manifest["training"]["rows"] == len(frame)
    assert "sale_24h" in manifest["targets"]
    assert manifest["targets"]["sale_24h"]["n_test"] > 0

    predictor = SentinelPredictor(cfg)
    assert predictor.load()
    prediction = predictor.predict(frame.iloc[0].to_dict(), "Dragonmaw", 1, 24, 10)
    assert prediction.status in ("validated", "trained_no_measured_gain", "baseline_only")
    assert prediction.probability is not None
    assert 0 <= prediction.probability <= 1
    assert prediction.price is not None and prediction.price > 0
    assert prediction.model_version == result.version
    assert prediction.training_scope


def test_a_calibrated_interval_states_what_it_delivered():
    rng = np.random.default_rng(11)
    calibration = rng.normal(0, 1, 1200)
    test = rng.normal(0, 1, 600)
    interval = calibrate_interval(calibration, test, level=0.8)
    assert interval is not None
    assert interval.half_width_log > 0
    assert interval.calibration_n == 1200
    assert interval.test_n == 600
    # nominal 80 %, measured on a block the calibration never saw
    assert 0.75 <= interval.coverage <= 0.85
    low, high = interval.bounds(1000)
    assert low < 1000 < high
    assert low == round(1000 * np.exp(-interval.half_width_log))


def test_the_trained_price_interval_is_exported_and_used(tmp_path):
    frame = _synthetic_frame()
    cfg = _config(tmp_path)
    result = SentinelTrainer(cfg).train(frame, observation_end=int(frame["ts"].max()))
    manifest = json.loads((Path(result.path) / "manifest.json").read_text(encoding="utf-8"))
    price = manifest["targets"]["price"]["price_interval"]
    assert price["level"] == 0.8
    assert price["half_width_log"] > 0
    assert price["test_n"] > 0
    assert price["coverage"] is not None

    predictor = SentinelPredictor(cfg)
    assert predictor.load()
    prediction = predictor.predict(frame.iloc[0].to_dict(), "Dragonmaw", 1, 24, 10)
    assert prediction.price is not None
    assert prediction.price_low is not None and prediction.price_low < prediction.price
    assert prediction.price_high is not None and prediction.price_high > prediction.price
    assert prediction.price_interval_level == 0.8
    assert prediction.price_interval_n > 0
    assert prediction.price_interval_coverage is not None
    assert prediction.price_interval_method == "split-conformal"


def test_a_missing_artifact_returns_unknown_not_zero(tmp_path):
    predictor = SentinelPredictor(_config(tmp_path))
    assert not predictor.load()
    prediction = predictor.predict({"item_id": 1, "ask_unit": 100}, "Dragonmaw", 1)
    assert prediction.status == "unknown"
    assert prediction.probability is None
    assert prediction.price is None
    assert prediction.fallback_reason


def test_a_corrupt_artifact_is_refused(tmp_path):
    frame = _synthetic_frame()
    cfg = _config(tmp_path)
    result = SentinelTrainer(cfg).train(frame, observation_end=int(frame["ts"].max()))
    manifest = json.loads((Path(result.path) / "manifest.json").read_text(encoding="utf-8"))
    sale = manifest["targets"]["sale_24h"]
    model_file = Path(result.path) / sale["files"]["model"]
    model_file.write_bytes(b"not a model")
    artifact, reason = load_artifact(tmp_path / "models")
    assert artifact is None
    assert "corrupt" in reason or "checksum" in reason


def test_a_stale_artifact_reports_stale_instead_of_a_confident_prediction(tmp_path):
    frame = _synthetic_frame()
    cfg = _config(tmp_path)
    result = SentinelTrainer(cfg).train(frame, observation_end=int(frame["ts"].max()))
    manifest_path = Path(result.path) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2020-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    predictor = SentinelPredictor(cfg)
    assert predictor.load()
    status, reason = predictor.target_status("sale_24h")
    assert status == "stale"
    assert "old" in reason or "freshness" in reason


def test_the_retrain_decision_is_false_without_new_labels(tmp_path):
    frame = _synthetic_frame()
    cfg = _config(tmp_path)
    assert retrain_decision(cfg, frame, int(frame["ts"].max()))[0] is True
    SentinelTrainer(cfg).train(frame, observation_end=int(frame["ts"].max()))
    decision, reason = retrain_decision(cfg, frame, int(frame["ts"].max()))
    assert decision is False
    assert ("new confirmed labels" in reason) or ("observation" in reason)


# ------------------------------------------------------------------- exporting

def test_horizon_probabilities_are_projected_onto_a_monotone_curve():
    from woweconomy.ml.sentinel import monotone_probabilities
    raw = {24: 0.61, 48: 0.57, 168: 0.58}
    curve = monotone_probabilities(raw)
    pooled = (0.61 + 0.57 + 0.58) / 3
    assert curve == {24: pooled, 48: pooled, 168: pooled}
    assert curve == dict(sorted(curve.items()))
    increasing = monotone_probabilities({24: 0.2, 48: 0.3, 168: 0.4})
    assert increasing == {24: 0.2, 48: 0.3, 168: 0.4}


def test_the_exported_record_distinguishes_the_new_targets():
    estimate = Estimate(
        realm="Dragonmaw", item_id=100, price=1234, confidence=0.6,
        sale_probability=0.4, expected_time_hours=12.0, sample_size=5,
        sale_probabilities={24: 0.4, 48: 0.55}, expected_sale_price=1200,
        sale_price_low=900, sale_price_high=1600, undercut_risk=0.3,
        sale_price_interval_level=0.8, sale_price_interval_coverage=0.77,
        sale_price_interval_n=116, price_low=800, price_high=1900,
        price_interval_level=0.8, price_interval_coverage=0.74,
        price_interval_n=89, price_decline_risk=0.2,
        training_scope="realm (pooled model)", model_version="v1",
        prediction_status="validated", data_quality=0.7)
    lua = ", ".join(DatasetRecord("Dragonmaw", 100, estimate).to_lua())
    assert "ereal = 1200" in lua
    assert "elow = 900" in lua and "ehigh = 1600" in lua
    assert "elevel = 0.8" in lua and "ecov = 0.77" in lua and "eintn = 116" in lua
    assert "plow = 800" in lua and "phigh = 1900" in lua
    assert "plevel = 0.8" in lua and "pcov = 0.74" in lua and "pintn = 89" in lua
    assert "psellByHorizon" in lua and "[48] = 0.55" in lua
    assert "undercut = 0.3" in lua and "decline = 0.2" in lua
    assert 'status = "validated"' in lua
    assert 'scope = "realm (pooled model)"' in lua


def test_an_unknown_prediction_is_nil_not_zero_in_lua():
    estimate = Estimate(realm="Dragonmaw", item_id=100, price=5)
    lua = ", ".join(DatasetRecord("Dragonmaw", 100, estimate).to_lua())
    assert "ereal = nil" in lua
    assert "plow = nil" in lua and "phigh = nil" in lua
    assert "elevel = nil" in lua and "plevel = nil" in lua
    assert "undercut = nil" in lua
    assert 'status = "unknown"' in lua


def test_the_dataset_schema_is_versioned():
    assert DATASET_VERSION == 2
    estimate = Estimate(realm="Dragonmaw", item_id=100, price=5,
                        prediction_status="validated", data_quality=0.5)
    class _Engine:
        shipped_model = None
        _history_cache = {}
        interests = None
        ml_frame = None

        def realm_scope(self, realm):
            return "dragonmaw"

        def region_price_table(self):
            return {}

    builder = DatasetBuilder(config=type("C", (), {
        "horizon_h": 24, "dataset_path": Path("unused"), "models_dir": Path("."),
        "realm_models_dir": Path("."), "realm_model_region": "eu",
        "reports_dir": Path("."), "addon_dir": Path("."), "tsm_db": Path("."),
        "dfa_db": Path("."), "min_sample_size": 1, "max_age_hours": 1,
        "base_confidence": 0.25, "confidence_reference_n": 8, "extra": {},
        "ensure_dirs": lambda self: None,
    })(), engine=_Engine())
    builder.records = [type("R", (), {"realm": "Dragonmaw", "item_id": 100,
                                      "estimate": estimate})()]
    builder.realms = {"Dragonmaw": {"auctionHouse": "dragonmaw", "items": {100: estimate},
                                    "sampleSize": 1, "updatedAt": 0}}
    text = builder.to_lua()
    assert f"version = {DATASET_VERSION}," in text
    assert "sentinelModel" in text


def test_live_risk_features_never_turn_missing_into_zero():
    features = risk_features_from_rows(None, None, 0.0)
    assert np.isnan(features["risk_market_log"])
    assert np.isnan(features["risk_min_log"])


def test_a_risk_model_with_too_few_examples_is_not_trained(tmp_path):
    cfg = _config(tmp_path)
    examples = pd.DataFrame([
        {"ts": 1, "scope": "a", "item_id": 1, "horizon_h": 24,
         "undercut_pressure": 1, "price_decline": 0,
         "risk_market_log": 1.0, "risk_min_log": 1.0},
    ])
    from woweconomy.ml.sentinel import train_risk_target
    report, payload = train_risk_target(examples, "undercut_pressure", 24, cfg,
                                        tmp_path / "risk")
    assert report.status == "insufficient_data"
    assert payload is None
