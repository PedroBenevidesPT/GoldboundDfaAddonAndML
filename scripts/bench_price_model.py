"""Measure CatBoost variants on the real walk-forward folds (not a guess)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from woweconomy.ml import MLConfig, DfaLearningEngine
from woweconomy.ml.validate import make_split
from woweconomy.ml.models import (categorical_columns, feature_columns,
                                  make_pool, price_metrics)
from woweconomy.ml.features import ASK_FEATURES
from catboost import CatBoostRegressor

config = MLConfig()
engine = DfaLearningEngine(config)
engine.load()
frame, report, link = engine.build_rows()
split = make_split(frame, n_folds=4, test_fraction=0.2)
cats = categorical_columns(frame)
feats = [c for c in feature_columns(frame) if c not in ASK_FEATURES]
print(f"rows {len(frame)}  price rows {report.price_rows}  folds {len(split.folds)}  feats {len(feats)}")


def run(name, params, weight_half_life_days=0.0):
    fold_metrics = []
    t0 = time.time()
    for fold in split.folds:
        train = fold.train[fold.train["y_net_unit_log"].notna()]
        valid = fold.valid[fold.valid["y_net_unit_log"].notna()]
        if len(train) < 30 or len(valid) < 5:
            continue
        weight = None
        if weight_half_life_days > 0 and "ts" in train.columns:
            newest = float(train["ts"].max())
            age_days = (newest - train["ts"].astype(float)) / 86400.0
            weight = 0.5 ** (age_days / weight_half_life_days)
        pool = make_pool(train, feats, cats, label=train["y_net_unit_log"], weight=weight)
        model = CatBoostRegressor(**params)
        model.fit(pool)
        y = valid["y_net_unit_log"].to_numpy(dtype=float)
        p = np.asarray(model.predict(make_pool(valid, feats, cats)), dtype=float)
        fold_metrics.append(price_metrics(
            y, p, valid["y_net_unit"].to_numpy(dtype=float)))
    keys = ("rmse_log", "mae_log", "within_25pct", "within_50pct", "mape_gold")
    agg = {k: float(np.mean([m.get(k, np.nan) for m in fold_metrics])) for k in keys}
    print(f"{name:<28} rmse {agg['rmse_log']:.4f}  mae {agg['mae_log']:.4f}  "
          f"w25 {agg['within_25pct']*100:4.1f}%  w50 {agg['within_50pct']*100:4.1f}%  "
          f"({time.time()-t0:.0f}s, {len(fold_metrics)} folds)")
    return agg


base = dict(loss_function="RMSE", iterations=config.iterations, learning_rate=0.05,
            depth=5, l2_leaf_reg=6.0, random_seed=7, verbose=False,
            allow_writing_files=False)

run("baseline", base)
run("lossguide31 min10", {**base, "grow_policy": "Lossguide", "max_leaves": 31,
                          "min_data_in_leaf": 10})
run("lossguide15 min10", {**base, "grow_policy": "Lossguide", "max_leaves": 15,
                          "min_data_in_leaf": 10})
run("lossguide31 min20", {**base, "grow_policy": "Lossguide", "max_leaves": 31,
                          "min_data_in_leaf": 20})
run("lossguide63 min20", {**base, "grow_policy": "Lossguide", "max_leaves": 63,
                          "min_data_in_leaf": 20})
run("MAE depth5", {**base, "loss_function": "MAE"})
run("Huber d1", {**base, "loss_function": "Huber:delta=1"})
run("lossguide31+MAE", {**base, "grow_policy": "Lossguide", "max_leaves": 31,
                        "min_data_in_leaf": 10, "loss_function": "MAE"})
run("lossguide31 bc64", {**base, "grow_policy": "Lossguide", "max_leaves": 31,
                         "min_data_in_leaf": 10, "border_count": 64})
run("lossguide31 it600", {**base, "grow_policy": "Lossguide", "max_leaves": 31,
                          "min_data_in_leaf": 10, "iterations": 600})
