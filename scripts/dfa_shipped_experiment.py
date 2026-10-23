"""Compare the reference sets the shipped model could use, out of sample.

The shipped model is fitted in sample today: ``fit_shipped_model`` fits and
``evaluate_shipped_model`` scores the same rows.  That number is optimistic
and cannot decide between two variants of the model.

This script does the comparison properly: each variant is fitted on the
training window and scored on the window that follows it, using the same
chronological folds the learning engine uses.  It also reports how often a
local reference was actually present, because a variant that wins by
imputing a median is not winning for the reason it appears to.

    python scripts/dfa_shipped_experiment.py
    python scripts/dfa_shipped_experiment.py --folds 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from woweconomy.ml import MLConfig, DfaLearningEngine  # noqa: E402
from woweconomy.ml.validate import make_split  # noqa: E402
from woweconomy.pricing.shipped import (  # noqa: E402
    REFERENCE_COLUMNS,
    evaluate_shipped_model,
    fit_shipped_model,
)

logger = logging.getLogger("dfa_shipped_experiment")

#: The variants to compare.  The first one is what ships today.
VARIANTS: List[Tuple[str, Sequence[str]]] = [
    ("regional only (ships today)",
     ("dbregionmarketavg", "dbregionhistorical", "dbregionsaleavg")),
    ("all six",
     ("dbmarket", "dbrecent", "dbhistorical",
      "dbregionmarketavg", "dbregionhistorical", "dbregionsaleavg")),
    ("regional + local market",
     ("dbmarket", "dbregionmarketavg", "dbregionhistorical", "dbregionsaleavg")),
    ("regional + local historical",
     ("dbhistorical", "dbregionmarketavg", "dbregionhistorical", "dbregionsaleavg")),
    ("regional + local market + historical",
     ("dbmarket", "dbhistorical", "dbregionmarketavg",
      "dbregionhistorical", "dbregionsaleavg")),
    ("local only",
     ("dbmarket", "dbrecent", "dbhistorical")),
    ("the regional sale average alone",
     ("dbregionsaleavg",)),
]


def _pairs(names: Sequence[str]) -> List[Tuple[str, str]]:
    return [(name, REFERENCE_COLUMNS[name]) for name in names]


def _score(frame, pairs, target: str) -> Dict[str, float]:
    """Fit on ``frame`` and score it, returning the metrics and the coverage."""
    model = fit_shipped_model(frame, target=target, references=pairs)
    if not model.coefficients:
        return {"n": 0, "note": "not enough rows to fit"}
    metrics = evaluate_shipped_model(model, frame, target=target)
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--target", default="y_net_unit_log")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-7s %(name)s: %(message)s")

    config = MLConfig()
    engine = DfaLearningEngine(config)
    engine.load()
    frame, _report, _link = engine.build_rows()
    target = args.target
    if target not in frame.columns:
        print(f"error: the dataset has no {target} column")
        return 1

    price_rows = frame[frame[target].notna()].copy()
    local_share = float((price_rows["local_present"] == 1).mean()) \
        if "local_present" in price_rows.columns else 0.0
    print(f"price rows (a realised sale): {len(price_rows)}")
    print(f"  with a local reference at that moment: "
          f"{int((price_rows['local_present'] == 1).sum())} "
          f"({local_share * 100:.1f}%)")
    print()

    split = make_split(frame, n_folds=args.folds,
                       test_fraction=args.test_fraction)
    if not split.folds:
        print(f"error: no chronological fold: {split.note}")
        return 1

    # the development window is everything the folds ever saw; the test
    # window comes after it and no fold has touched it
    dev = frame[frame["date"] < split.test_span[0]] \
        if len(split.test) else frame

    results: Dict[str, Dict[str, object]] = {}
    for label, names in VARIANTS:
        pairs = _pairs(names)
        folds = []
        for fold in split.folds:
            folds.append(_score(fold.train, pairs, target))  # fit
        # the honest number: fit on the training block, score the block
        # that follows it
        fold_scores = []
        for fold in split.folds:
            model = fit_shipped_model(fold.train, target=target, references=pairs)
            if not model.coefficients:
                continue
            fold_scores.append(evaluate_shipped_model(model, fold.valid, target=target))
        test_model = fit_shipped_model(dev, target=target, references=pairs)
        test = evaluate_shipped_model(test_model, split.test, target=target) \
            if test_model.coefficients else {"n": 0}

        def _mean(key):
            vals = [m.get(key) for m in fold_scores
                    if isinstance(m.get(key), (int, float))]
            return float(np.mean(vals)) if vals else float("nan")

        results[label] = {
            "references": list(names),
            "folds": fold_scores,
            "walk_forward": {
                "rmse_log": _mean("rmse_log"),
                "median_ratio": _mean("median_ratio"),
                "within_25pct": _mean("within_25pct"),
                "within_50pct": _mean("within_50pct"),
            },
            "test": test,
            "coefficients": [round(c, 4) for c in (test_model.coefficients or [])],
            "n_train_dev": int(test_model.n_train),
        }

    print(f"{'variant':38s} {'wf rmse':>8s} {'wf ±25%':>8s} {'test rmse':>10s} "
          f"{'test ±25%':>10s} {'test n':>7s}")
    print("-" * 86)
    for label, entry in results.items():
        wf = entry["walk_forward"]
        te = entry["test"]
        print(f"{label:38s} {wf['rmse_log']:8.3f} {wf['within_25pct'] * 100:7.0f}% "
              f"{te.get('rmse_log', float('nan')):10.3f} "
              f"{te.get('within_25pct', 0) * 100:9.0f}% "
              f"{te.get('n', 0):7d}")

    print()
    print("wf = mean over the chronological folds (fit on the past, score "
          "the window after it)")
    print("test = the untouched window after every fold")

    out = Path(config.reports_dir) / "dfa_shipped_experiment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"price_rows": len(price_rows),
                               "local_share": local_share,
                               "variants": results}, indent=2, default=str),
                   encoding="utf-8")
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
