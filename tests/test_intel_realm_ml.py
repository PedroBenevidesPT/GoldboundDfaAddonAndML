"""Realm-scoped training, and the censored survival baseline."""
import math

import numpy as np
import pandas as pd
import pytest

from woweconomy.intel import realm_ml, survival


def frame_for(realm, level_gold, rows=120, days=6):
    rng = np.random.default_rng(abs(hash(realm)) % (2 ** 32))
    out = []
    per_day = rows // days
    for day in range(days):
        for i in range(per_day):
            price = level_gold * 10_000 * float(np.exp(rng.normal(0, 0.08)))
            out.append({
                "realm": realm, "date": 20000 + day,
                "ts": (20000 + day) * 86400 + i * 60,
                "item_id": 100 + (i % 5),
                "qty": 1.0,
                "ask_unit_log": math.log(price),
                "local_market_log": math.log(price * 1.05),
                "region_market_log": math.log(price * 1.1),
                "region_sale_avg_log": math.log(price),
                "unit_bid_log": None,
                "deposit_log": math.log(100.0),
                "is_stack": 0.0,
                "hour_of_day": float(i % 24),
                "day_of_week": float(day % 7),
                "y_net_unit": price * 0.95,
                "y_net_unit_log": math.log(price * 0.95),
                "cat_item_id": str(100 + (i % 5)),
                "cat_realm": realm,
            })
    return out


@pytest.fixture()
def frame():
    return pd.DataFrame(frame_for("Dragonmaw", 200) +
                        frame_for("Silvermoon", 900))


def test_realm_split_is_exact(frame):
    groups = realm_ml.split_by_realm(frame)
    assert set(groups) == {"Dragonmaw", "Silvermoon"}
    assert all(sub["realm"].nunique() == 1 for sub in groups.values())


def test_models_are_trained_per_realm_and_the_trainer_never_sees_two(
        frame, monkeypatch, tmp_path):
    calls = []
    original = realm_ml.train_price_model

    def spy(train, valid=None, *args, **kwargs):
        calls.append(set(train["realm"].astype(str)))
        return original(train, valid, *args, **kwargs)

    monkeypatch.setattr(realm_ml, "train_price_model", spy)
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=20,
                                 min_valid_rows=5, iterations=20)
    results = realm_ml.train_all(frame, cfg)
    assert set(results) == {"eu:dragonmaw", "eu:silvermoon"}
    assert calls, "the trainer was never called"
    assert all(len(realms) == 1 for realms in calls), calls
    trained = [r for r in results.values() if r.status == "trained"]
    assert len(trained) == 2
    assert len({r.model_path for r in trained}) == 2


def test_a_realm_with_too_few_sales_gets_no_model_and_no_borrowing(tmp_path):
    tiny = pd.DataFrame(frame_for("Dragonmaw", 200)[:8])
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=40)
    result = realm_ml.train_realm(tiny, "Dragonmaw", cfg)
    assert result.status == "insufficient_data"
    assert result.model_path == ""
    assert "no pooled fallback" in result.note or "nothing is borrowed" in result.note


def test_evaluation_reports_skill_against_the_training_median(frame, tmp_path):
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=20,
                                 min_valid_rows=5, iterations=20)
    result = realm_ml.evaluate_realm_price(frame, "Dragonmaw", cfg)
    assert result.status == "evaluated"
    assert result.folds >= 1
    assert result.holdout_rows > 0, "the untouched window was never scored"
    assert "rmse_log_mean" in result.metrics
    assert "rmse_log_mean" in result.baseline_metrics
    assert result.metrics["rmse_log_mean"] < result.baseline_metrics["rmse_log_mean"]


def test_the_holdout_is_strictly_after_every_training_day(frame, tmp_path,
                                                          monkeypatch):
    seen = []
    original = realm_ml.train_price_model

    def spy(train, valid=None, *args, **kwargs):
        seen.append(int(train["date"].max()))
        return original(train, valid, *args, **kwargs)

    monkeypatch.setattr(realm_ml, "train_price_model", spy)
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=20,
                                 min_valid_rows=5, iterations=20)
    result = realm_ml.evaluate_realm_price(frame, "Dragonmaw", cfg)
    # six synthetic days: 20000..20005; the last quarter is the holdout
    assert result.holdout_span == (20004, 20005)
    assert result.holdout_rows == 40
    assert max(seen) < result.holdout_span[0], (
        "a training row inside the untouched window is leakage")


def test_a_sparse_last_day_still_produces_a_holdout(tmp_path):
    base = pd.DataFrame(frame_for("Dragonmaw", 200, rows=100, days=5))
    # the fixed day fraction would leave four sales in the test window
    frame = pd.concat([base[base["date"] < 20004],
                       base[base["date"] == 20004].head(4)],
                      ignore_index=True)
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=40,
                                 min_valid_rows=5, iterations=20)
    result = realm_ml.evaluate_realm_price(frame, "Dragonmaw", cfg)
    assert result.holdout_rows == 24
    assert result.holdout_span == (20003, 20004)
    assert result.holdout_metrics["rmse_log_mean"] > 0


def test_the_holdout_has_item_level_and_persistence_controls(frame, tmp_path):
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=20,
                                 min_valid_rows=5, iterations=20)
    result = realm_ml.evaluate_realm_price(frame, "Dragonmaw", cfg)
    assert set(result.extra_holdout_baselines) >= {"item_median", "last_price"}
    for name, values in result.extra_holdout_baselines.items():
        assert "rmse_log_mean" in values, name
    # the expanding-window folds are kept next to the holdout, not replaced
    assert "rmse_log_mean" in result.fold_metrics


def test_predictions_use_the_realm_model_and_refuse_another_realm(frame, tmp_path):
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=20,
                                 min_valid_rows=5, iterations=40)
    results = realm_ml.train_all(frame, cfg)
    assert results["eu:dragonmaw"].status == "trained"
    assert results["eu:silvermoon"].status == "trained"
    dragon = frame[frame["realm"] == "Dragonmaw"].iloc[[0]].copy()
    silver = frame[frame["realm"] == "Silvermoon"].iloc[[0]].copy()
    pred_dragon = realm_ml.predict_net_unit(dragon, "Dragonmaw", cfg)[0]
    pred_silver = realm_ml.predict_net_unit(silver, "Silvermoon", cfg)[0]
    assert pred_dragon < pred_silver / 3
    with pytest.raises(ValueError, match="refusing to predict"):
        realm_ml.predict_net_unit(dragon, "Silvermoon", cfg)


def test_a_shared_prior_must_win_before_it_is_accepted(frame, tmp_path):
    cfg = realm_ml.RealmMLConfig(models_dir=tmp_path, min_price_rows=20,
                                 min_valid_rows=5, iterations=20,
                                 min_prior_gain=10.0)
    result = realm_ml.validate_shared_prior(frame, "Dragonmaw", cfg)
    if result["status"] == "evaluated":
        assert result["accepted"] is False
        assert "anchored to this realm" in result["note"]


# ---------------------------------------------------------------------------
# survival
# ---------------------------------------------------------------------------

def test_kaplan_meier_with_every_event_at_the_known_values():
    km = survival.kaplan_meier([1, 2, 3], [1, 1, 1])
    assert [round(p.survival, 4) for p in km.points] == [0.6667, 0.3333, 0.0]
    assert [p.at_risk for p in km.points] == [3, 2, 1]
    assert km.median == 2
    assert km.censored == 0
    assert km.events == 3


def test_censoring_is_not_a_failure():
    km = survival.kaplan_meier([2, 3], [0, 1])
    assert round(km.survival_at(2), 4) == 1.0
    assert km.survival_at(3) == 0.0
    assert km.median == 3
    assert km.censored == 1


def test_restricted_mean_survival_time():
    km = survival.kaplan_meier([1, 2], [1, 1], tau=3)
    # S = 1 on [0,1), 0.5 on [1,2), 0 on [2,3)
    assert km.rmst == pytest.approx(1.5)


def test_sale_probability_uses_the_curve():
    probs = survival.sale_probability_by([1, 2, 3], [1, 0, 1], [1, 2, 3])
    assert probs["1"] == pytest.approx(1 / 3, abs=1e-4)
    assert probs["2"] == pytest.approx(1 / 3, abs=1e-4)
    assert probs["3"] == pytest.approx(1.0, abs=1e-4)


def test_logrank_detects_a_clear_difference_but_not_an_identical_group():
    same = survival.logrank([(t, 1) for t in range(1, 21)],
                            [(t, 1) for t in range(1, 21)])
    assert same["chi2"] is not None and same["p_value"] > 0.9
    different = survival.logrank([(t, 1) for t in range(1, 21)],
                                 [(t, 1) for t in range(50, 70)])
    assert different["p_value"] < 0.01
