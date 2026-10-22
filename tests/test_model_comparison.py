"""Tests for the fair chronological price-model comparison.

The comparison exists to stop three models being reported on three
different windows.  These tests check the rules that make the table
interpretable: strict chronology, one training/calibration/test split,
same test rows for every model, and no test row used to fit a width.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from woweconomy.ml.comparison import comparison_markdown, compare_price_models


def _frame(rows: int = 240, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 1_700_000_000
    records = []
    for index in range(rows):
        day = index // 20
        ts = base + day * 86_400 + (index % 20) * 60
        signal = rng.normal()
        reference = float(1200 * np.exp(0.45 * signal + rng.normal(0, 0.15)))
        net = reference * float(np.exp(rng.normal(0, 0.20)))
        records.append({
            "ts": ts,
            "date": ts // 86_400,
            "item_id": int(index % 12),
            "realm": "Dragonmaw",
            "qty": float(1 + index % 3),
            "x_signal": signal,
            "ref_region_market": reference * 1.10,
            "ref_region_historical": reference * 1.05,
            "ref_region_sale_avg": reference,
            "cat_item_name_bucket": "hide" if index % 3 else "other",
            "cat_realm": "dragonmaw",
            "y_net_unit": net,
            "y_net_unit_log": float(np.log(net)),
            "ask_unit": reference * 1.2,
        })
    return pd.DataFrame(records)


def test_the_comparison_uses_one_strictly_chronological_split():
    report = compare_price_models(
        _frame(), record_iterations=8, sentinel_iterations=8, bootstrap=20)
    split = report["split"]
    assert split["train_ts"][1] < split["calibration_ts"][0]
    assert split["calibration_ts"][1] < split["test_ts"][0]
    assert split["train_rows"] > 0
    assert split["calibration_rows"] > 0
    assert split["test_rows"] > 0
    assert report["rules"]["test_used_for_selection"] is False
    assert report["rules"]["same_test_observations"] is True


def test_every_model_is_scored_on_the_same_test_rows():
    report = compare_price_models(
        _frame(), record_iterations=8, sentinel_iterations=8, bootstrap=20)
    common = report["split"]["common_test_rows"]
    assert common == report["split"]["test_rows"]
    assert set(report["models"]) == {
        "record_price_catboost",
        "sentinel_price_catboost",
        "regional_sale_avg_baseline",
        "item_median_baseline",
        "shipped_linear",
    }
    for values in report["models"].values():
        assert values["n"] == common
        assert np.isfinite(values["mae_log"])
        assert values["interval"]["available"] is True
        assert values["interval"]["calibration_n"] >= 20
        assert values["interval"]["test_n"] == common


def test_the_test_block_never_sets_the_interval_width():
    # The width is a function of the calibration residuals.  Changing only
    # the final block's target must therefore leave the calibrated width
    # unchanged, while the measured coverage is allowed to move.
    frame = _frame()
    first = compare_price_models(
        frame, record_iterations=8, sentinel_iterations=8, bootstrap=0)
    changed = frame.copy()
    mask = changed["ts"] >= max(changed["ts"]) - 86_400
    changed.loc[mask, "y_net_unit"] = changed.loc[mask, "y_net_unit"] * 3.0
    changed.loc[mask, "y_net_unit_log"] = np.log(changed.loc[mask, "y_net_unit"])
    second = compare_price_models(
        changed, record_iterations=8, sentinel_iterations=8, bootstrap=0)
    for name in first["models"]:
        assert (first["models"][name]["interval"]["half_width_log"]
                == second["models"][name]["interval"]["half_width_log"])


def test_the_markdown_states_the_limits_and_the_segment_table():
    report = compare_price_models(
        _frame(), record_iterations=8, sentinel_iterations=8, bootstrap=20)
    text = comparison_markdown(report)
    assert "same final test rows" in text
    assert "not a per-segment 80% guarantee" in text
    assert "item_median_baseline" in text
    assert sum(block["n"] for block in report["segments"].values()) == \
        report["split"]["common_test_rows"]
