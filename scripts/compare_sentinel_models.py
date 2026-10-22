"""Compare two Sentinel artifacts on one frozen evaluation window.

The requirement is that the old and the new model are judged on the *same*
listings.  Rebuilding the frame twice cannot guarantee that: the watcher
keeps appending snapshots, so ``observation_end`` moves and the
chronological test block silently shifts.  This script therefore builds
the frame once, writes it to a checksummed cache, and refuses to run if the
cache has changed under it.

Reported separately, per target and per artifact:

* log loss      -- the proper scoring rule used for selection
* Brier         -- the squared-error view
* ECE           -- calibration, binned
* AUC / AP      -- discrimination
* base rate     -- so a "good" model can be compared with doing nothing

Usage:
    python scripts/compare_sentinel_models.py \
        --old models/dfa_sentinel/20261022T195801Z-4e9a79ed \
        --new data/_phase3/dfa_sentinel/20261022T202826Z-2844edb8 \
        --out data/_phase3/comparison.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.ml.config import MLConfig                      # noqa: E402
from woweconomy.ml.frame_cache import (                         # noqa: E402
    FrameCacheMismatch,
    file_digest,
    load_or_build_frame,
)
from woweconomy.ml.pipeline import DfaLearningEngine            # noqa: E402
from woweconomy.ml.sentinel import (                            # noqa: E402
    SENTINEL_SCHEMA,
    SentinelArtifact,
    _apply_item_rate,
    _baseline_item_rate,
    _catboost_predict,
    _load_target,
    _prepare_horizon_frame,
    _sklearn_predict,
    chronological_split,
    classification_metrics,
    load_artifact,
)


def load_any(path: Path):
    """Load an artifact root (via CURRENT) or one pinned version directory.

    A controlled comparison must be able to name the exact version it
    means; following CURRENT would silently compare against whatever was
    promoted last.
    """
    path = Path(path)
    if (path / "CURRENT").exists() or not (path / "manifest.json").exists():
        return load_artifact(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SENTINEL_SCHEMA:
        return None, f"{path.name} has schema {manifest.get('schema')}, not {SENTINEL_SCHEMA}"
    artifact = SentinelArtifact(version=path.name, path=path, manifest=manifest)
    for key, entry in (manifest.get("targets") or {}).items():
        if not (entry or {}).get("files"):
            continue
        try:
            loaded = _load_target(path, entry)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"warning: {key} could not be loaded from {path.name}: {exc}")
            continue
        if loaded is not None:
            artifact.models[key] = loaded
    return artifact, ""


def freeze_frame(cache: Path, *, rebuild: bool = False) -> pd.DataFrame:
    """Return the frozen frame, building it once and then never again."""

    def _build() -> pd.DataFrame:
        engine = DfaLearningEngine(MLConfig())
        engine.load()
        frame, _report, _link = engine.build_rows()
        return frame

    try:
        frame = load_or_build_frame(cache, _build, rebuild=rebuild)
    except FrameCacheMismatch as exc:
        raise SystemExit(
            f"{exc}\nrefusing to compare two models on different data; "
            f"delete {cache} and its .sha256 to refreeze") from exc
    print(f"frozen frame: {len(frame)} rows from {cache.name} "
          f"(sha256 {file_digest(cache)[:12]})")
    return frame


def _predict(payload: Dict[str, object], frame: pd.DataFrame) -> np.ndarray:
    """Score a frame with one loaded target payload."""
    model_type = str(payload.get("model_type") or "")
    features = list(payload.get("features") or [])
    cats = list(payload.get("cats") or [])
    calibrator = payload.get("calibrator")

    if model_type in ("baseline_item_rate", "baseline_rate"):
        rates = payload.get("rates")
        if isinstance(rates, dict):
            raw = _apply_item_rate(rates, frame)
        else:
            raw = np.full(len(frame), float(payload.get("rate") or 0.0))
        raw = np.asarray(raw, dtype=float)
    elif model_type == "catboost":
        raw = _catboost_predict(payload["model"], frame, features, cats)
    elif model_type == "logistic":
        raw = _sklearn_predict(payload["model"], frame, features)
    else:
        raise ValueError(f"cannot score model_type={model_type!r}")

    if calibrator is not None and hasattr(calibrator, "transform"):
        return np.asarray(calibrator.transform(raw), dtype=float)
    return np.clip(np.asarray(raw, dtype=float), 1e-6, 1 - 1e-6)


METRIC_KEYS = ("n", "base_rate", "logloss", "brier", "ece", "auc",
               "average_precision", "mean_predicted")

#: Outcomes that are final, and the gold each one realised.
#: A sale returns ``y_net_gold * qty`` (net of the auction cut and the
#: deposit, in gold per unit times units); an expiry or a cancellation
#: returns the item and realises nothing.  ``censored`` is **not** in this
#: map: the outcome is unknown, and counting it as zero would report a
#: strategy's failure to observe as the strategy's failure to earn.
FINAL_OUTCOMES = ("sale", "expired", "cancelled")


def realised_gold(frame: pd.DataFrame) -> np.ndarray:
    """Gold actually earned per listing, for listings whose outcome is known.

    Raises on a censored row rather than treating it as a zero: the caller
    must exclude censored listings *before* asking this question.
    """
    outcome = frame["outcome"].astype(str)
    unknown = sorted(set(outcome.unique()) - set(FINAL_OUTCOMES))
    if unknown:
        raise ValueError(
            f"outcome(s) {unknown} have no realised value; exclude them "
            "instead of counting them as zero")
    sold = outcome == "sale"
    gold = (frame["y_net_gold"].astype(float) * frame["qty"].astype(float))
    return np.where(sold, gold.fillna(0.0).to_numpy(dtype=float), 0.0)


def economics(frame: pd.DataFrame, p: np.ndarray, *,
              thresholds: Sequence[float] = (0.25, 0.5),
              top_fraction: float = 0.2) -> Dict[str, object]:
    """What a posting policy would have earned on the same listings.

    The policy is the one the tool actually offers: post a listing when
    the model says its sale probability clears a threshold.  ``all`` is
    the do-nothing-alternative baseline (post everything); ``top`` is a
    rank-only policy that posts the highest-scoring share, which is
    insensitive to whether the probabilities are calibrated.

    Reported per policy: how many listings were posted, how many sold,
    the net gold earned, and the gold earned per listing posted.  The
    last one is the only column that is comparable across policies with
    different post counts.
    """
    gold = realised_gold(frame)
    sold = (frame["outcome"].astype(str) == "sale").to_numpy()
    n = len(frame)
    out: Dict[str, object] = {"listings": int(n), "policies": {}}

    def summarise(name: str, mask: np.ndarray) -> None:
        posted = int(mask.sum())
        out["policies"][name] = {
            "posted": posted,
            "share_posted": round(posted / n, 4) if n else None,
            "sales": int(sold[mask].sum()),
            "net_gold": round(float(gold[mask].sum()), 2),
            "gold_per_post": round(float(gold[mask].sum()) / posted, 2)
            if posted else None,
        }

    summarise("all", np.ones(n, dtype=bool))
    for threshold in thresholds:
        summarise(f"p>={threshold:g}", np.asarray(p) >= threshold)
    k = max(1, int(round(n * top_fraction)))
    order = np.argsort(-np.asarray(p), kind="stable")[:k]
    top = np.zeros(n, dtype=bool)
    top[order] = True
    summarise(f"top_{int(round(top_fraction * 100))}pct", top)
    return out


def _summarise(y: np.ndarray, p: np.ndarray) -> Dict[str, float]:
    metrics = classification_metrics(y, p)
    return {k: float(metrics.get(k, float("nan"))) for k in METRIC_KEYS}


def compare(old_dir: Optional[Path], new_dir: Path, frame: pd.DataFrame,
            horizons=(24, 48, 168)) -> Dict[str, object]:
    observation_end = int(frame["ts"].max()) if "ts" in frame.columns else 0
    old, old_error = (load_any(old_dir) if old_dir else (None, "no old artifact given"))
    new, new_error = load_any(new_dir)
    if new is None:
        raise SystemExit(f"the new artifact could not be loaded: {new_error}")
    if old is None:
        print(f"warning: the old artifact could not be loaded: {old_error}")

    out: Dict[str, object] = {
        "frame": {
            "rows": int(len(frame)),
            "first_ts": int(frame["ts"].min()),
            "last_ts": int(frame["ts"].max()),
            "observation_end": observation_end,
        },
        "old": {"version": old.version if old else "", "error": old_error},
        "new": {"version": new.version, "error": new_error},
        "targets": {},
    }

    for horizon in horizons:
        key = f"sale_{horizon}h"
        label = f"sold_within_{horizon}h"
        usable = _prepare_horizon_frame(frame, horizon, observation_end)
        if usable.empty:
            out["targets"][key] = {"status": "no_usable_rows"}
            continue
        split = chronological_split(usable, 0.25, 0.20)
        if not split.usable:
            out["targets"][key] = {"status": split.note or "split_unavailable"}
            continue

        # one split, shared by every model compared below
        y_train = split.train[label].to_numpy(dtype=float)
        y_test = split.test[label].to_numpy(dtype=float)
        entry: Dict[str, object] = {
            "window": {
                "train_ts": list(split.train_span),
                "calibration_ts": list(split.calibration_span),
                "test_ts": list(split.test_span),
                "train_rows": int(len(split.train)),
                "calibration_rows": int(len(split.calibration)),
                "test_rows": int(len(split.test)),
                "test_positives": int(y_test.sum()),
            },
            "models": {},
        }

        rates = _baseline_item_rate(split.train, label)
        entry["models"]["item_rate_baseline"] = _summarise(
            y_test, _apply_item_rate(rates, split.test))
        entry["models"]["constant_rate"] = _summarise(
            y_test, np.full(len(split.test), float(y_train.mean())))
        entry["economics"] = {}

        # The baseline earns its own economic row, scored on the same
        # listings and the same policies as the learned candidates.
        try:
            entry["economics"]["item_rate_baseline"] = economics(
                split.test, _apply_item_rate(rates, split.test))
        except Exception as exc:  # pragma: no cover - defensive
            entry["economics"]["item_rate_baseline"] = {"status": str(exc)}

        for name, artifact in (("old", old), ("new", new)):
            if artifact is None:
                continue
            payload = artifact.models.get(key)
            if payload is None:
                entry["models"][name] = {"status": "not_available_in_artifact"}
                continue
            try:
                p = _predict(payload, split.test)
            except Exception as exc:  # pragma: no cover - defensive
                entry["models"][name] = {"status": f"scoring_failed: {exc}"}
                continue
            scored = _summarise(y_test, p)
            scored["model_type"] = str(payload.get("model_type") or "")
            scored["calibrated"] = getattr(payload.get("calibrator"), "method", "identity")
            scored["features"] = len(payload.get("features") or [])
            entry["models"][name] = scored
            try:
                entry["economics"][name] = economics(split.test, p)
            except Exception as exc:  # pragma: no cover - defensive
                entry["economics"][name] = {"status": str(exc)}

        out["targets"][key] = entry
    return out


def _markdown(payload: Dict[str, object]) -> str:
    lines = [
        "# Sentinel: old vs new on one frozen window",
        "",
        f"Frame: {payload['frame']['rows']} rows, "
        f"ts {payload['frame']['first_ts']}–{payload['frame']['last_ts']}.",
        f"Old artifact: `{payload['old']['version']}`.  "
        f"New artifact: `{payload['new']['version']}`.",
        "",
    ]
    for key, entry in (payload.get("targets") or {}).items():
        if "window" not in entry:
            lines.append(f"## {key}: {entry.get('status')}")
            lines.append("")
            continue
        window = entry["window"]
        lines.append(f"## {key}")
        lines.append("")
        lines.append(f"Test window {window['test_ts'][0]}–{window['test_ts'][1]}, "
                     f"{window['test_rows']} rows, "
                     f"{window['test_positives']} positives.")
        lines.append("")
        lines.append("| model | log loss | Brier | ECE | AUC | AP | mean pred | base rate |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for name, metrics in entry["models"].items():
            if "logloss" not in metrics:
                lines.append(f"| {name} | {metrics.get('status', '')} | | | | | | |")
                continue
            lines.append(
                f"| {name} | {metrics['logloss']:.4f} | {metrics['brier']:.4f} | "
                f"{metrics['ece']:.4f} | {metrics['auc']:.4f} | "
                f"{metrics['average_precision']:.4f} | "
                f"{metrics['mean_predicted']:.4f} | {metrics['base_rate']:.4f} |")
        lines.append("")
        economics_rows = entry.get("economics") or {}
        if economics_rows:
            lines.append("Realised net gold on the same listings "
                         "(final outcomes only):")
            lines.append("")
            lines.append("| model | policy | posted | sales | net gold | "
                         "gold per post |")
            lines.append("|---|---|---|---|---|---|")
            for name, payload in economics_rows.items():
                if "policies" not in payload:
                    lines.append(f"| {name} | {payload.get('status', '')} | | | | |")
                    continue
                for policy, row in payload["policies"].items():
                    lines.append(
                        f"| {name} | {policy} | {row['posted']} | {row['sales']} | "
                        f"{row['net_gold']:.0f} | {row['gold_per_post'] or 0:.0f} |")
            lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, default=None,
                        help="artifact directory of the model to beat")
    parser.add_argument("--new", type=Path, required=True,
                        help="artifact directory of the candidate")
    parser.add_argument("--frame-cache", type=Path,
                        default=ROOT / "data" / "_phase3" / "frame.parquet")
    parser.add_argument("--rebuild-frame", action="store_true",
                        help="discard the cache and rebuild it (do not mix)")
    parser.add_argument("--horizons", type=int, nargs="+", default=[24, 48, 168])
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)

    frame = freeze_frame(args.frame_cache, rebuild=args.rebuild_frame)
    payload = compare(args.old, args.new, frame, tuple(args.horizons))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"comparison written to {args.out}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(_markdown(payload), encoding="utf-8")
        print(f"table written to {args.markdown}")
    if not args.out and not args.markdown:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
