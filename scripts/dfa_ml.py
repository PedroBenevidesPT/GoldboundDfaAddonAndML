"""Run the DfA CatBoost learning engine end to end.

    python scripts/dfa_ml.py audit     # data-readiness report
    python scripts/dfa_ml.py train     # walk-forward validation + final models
    python scripts/dfa_ml.py recommend # a listing recommendation for an item
    python scripts/dfa_ml.py all

``--refresh-journal`` re-reads the Journalator saved variables and pushes
them into ``auction_events`` before anything else runs, so the engine
always works from the newest archive.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from woweconomy.ml import MLConfig, DfaLearningEngine  # noqa: E402
from woweconomy.ml.audit import write_audit  # noqa: E402
from woweconomy.ml.comparison import comparison_markdown, compare_price_models  # noqa: E402
from woweconomy.ml.pipeline import build_name_index, now_iso  # noqa: E402
from woweconomy.ml.validate import make_split  # noqa: E402

logger = logging.getLogger("dfa_ml")


def refresh_journalator(config: MLConfig) -> dict:
    """Re-read the Journalator archive into ``auction_events``."""
    from woweconomy.services.auction_intelligence import ingest_journalator_archive

    con = sqlite3.connect(config.tsm_db)
    try:
        result = ingest_journalator_archive(con, config.journalator_lua)
        return {
            "total": result.total, "inserted": result.inserted,
            "duplicates": result.duplicates, "invalid": result.invalid,
            "errors": result.errors,
        }
    finally:
        con.close()


def cmd_audit(config: MLConfig, args) -> int:
    engine = DfaLearningEngine(config)
    engine.load()
    frame, report, link = engine.build_rows()
    path = write_audit(config, engine, frame, report, link)
    print(f"data-readiness report written to {path}")
    print(json.dumps({"archive": engine.archive.summary(),
                      "link": link.summary(),
                      "dataset": report.summary()}, indent=2, default=str))
    return 0


def cmd_train(config: MLConfig, args) -> int:
    engine = DfaLearningEngine(config)
    engine.load()
    frame, report, link = engine.build_rows()
    split = make_split(frame, n_folds=args.folds, test_fraction=args.test_fraction)
    print(f"rows: {len(frame)}  price rows: {report.price_rows}  "
          f"folds: {len(split.folds)}  test rows: {len(split.test)}")
    for entry in split.describe():
        print("   ", entry)

    validation = engine.validate(split)
    test_block = engine.test_window(frame, split)
    credibility = engine.reference_credibility(frame)
    horizons = engine.horizon_equivalence(frame)
    curve = engine.demand_curve(frame)
    speculation = engine.speculation_curve(frame)
    from woweconomy.ml.models import categorical_columns, feature_columns
    from woweconomy.ml.features import ASK_FEATURES
    price_feats = [c for c in feature_columns(frame) if c not in ASK_FEATURES]
    ablation = engine.ablation(frame, split, price_feats, categorical_columns(frame))
    final = engine.fit_final(frame)
    strategy = engine.evaluate_strategy(frame, split, curve)
    value = engine.strategy_value(frame, split, curve)

    payload = {
        "generated_at": now_iso(),
        "archive": engine.archive.summary(),
        "link": link.summary(),
        "dataset": report.summary(),
        "split": split.describe(),
        "validation": validation,
        "test_window": test_block,
        "horizon_equivalence": horizons,
        "demand_curve": curve,
        "speculation_curve": speculation,
        "reference_credibility": credibility,
        "ablation": ablation,
        "final_models": final,
        "strategy_evaluation": strategy,
        "strategy_value": value,
    }
    config.ensure_dirs()
    out = config.reports_dir / "dfa_ml_report.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"report written to {out}")
    from woweconomy.ml.report import write_report
    md = write_report(config, payload)
    print(f"report written to {md}")
    return 0


def cmd_sentinel(config: MLConfig, args) -> int:
    """Train the validated Sentinel layer and print its real metrics."""
    from woweconomy.ml.sentinel import (
        SentinelConfig,
        SentinelTrainer,
        retrain_decision,
        write_sentinel_report,
    )

    engine = DfaLearningEngine(config)
    engine.load()
    frame, report, link = engine.build_rows()
    if getattr(args, "frame_cache", None):
        # The watcher keeps appending snapshots, so two runs a minute apart
        # do not see the same rows and their test blocks are not comparable.
        # Freezing the frame makes "identical chronological windows" a fact
        # rather than a hope.
        from woweconomy.ml.frame_cache import load_or_build_frame
        frame = load_or_build_frame(Path(args.frame_cache), frame)
    cfg = SentinelConfig(tsm_db=Path(config.tsm_db),
                         models_root=Path(args.sentinel_dir) if args.sentinel_dir
                         else Path(config.models_dir).parent / "dfa_sentinel",
                         reports_dir=Path(args.sentinel_reports_dir)
                         if getattr(args, "sentinel_reports_dir", None)
                         else Path(config.reports_dir).parent / "dfa_sentinel",
                         iterations=args.sentinel_iterations or config.iterations)
    cfg.ensure_dirs()
    decision, reason = retrain_decision(cfg, frame, engine.observation_end)
    print(f"retrain decision: {decision} ({reason})")
    if args.if_needed and not decision:
        return 0
    result = SentinelTrainer(cfg).train(
        frame, observation_end=engine.observation_end, dry_run=args.dry_run)
    report_path = write_sentinel_report(result, cfg.reports_dir / "sentinel_report.json")
    print(f"Sentinel artifact: {result.path} (promoted={result.promoted})")
    for key, entry in (result.report.get("targets") or {}).items():
        metrics = entry.get("metrics") or {}
        score = metrics.get("brier")
        if score is None:
            score = metrics.get("mae_log")
        if score is None:
            score = metrics.get("c_index")
        print(f"  {key:24s} {entry.get('status'):24s} "
              f"{entry.get('model_type'):22s} score={score}")
    print(f"report written to {report_path}")
    return 0


def cmd_recommend(config: MLConfig, args) -> int:
    import pandas as pd

    from woweconomy.ml.models import load_sale_probability_model, load_price_quantiles
    from woweconomy.ml.strategy import ListingStrategy, StrategyConfig
    from woweconomy.ml.models import PriceModel
    from woweconomy.ml.pipeline import DfaLearningEngine

    engine = DfaLearningEngine(config)
    engine.load()
    frame, report, link = engine.build_rows()

    price_model = PriceModel.load(config.models_dir / "price_model.cbm")
    horizon = args.horizon
    prob = load_sale_probability_model(
        config.models_dir / f"sale_probability_{horizon}h.cbm")
    quantiles = load_price_quantiles(config.models_dir / "price_model")

    # the measured demand curve: the ask dimension of the question
    curve = engine.demand_curve(frame, horizon=horizon)
    curve_fn = engine.make_curve(curve)

    strategy = ListingStrategy(price_model, prob, None,
                               StrategyConfig(deadline_h=horizon,
                                              objective=args.objective),
                               quantiles=quantiles, curve=curve_fn)
    target = frame[(frame["realm"] == args.realm) & (frame["item_id"] == args.item_id)]
    if target.empty:
        print(f"no observation of item {args.item_id} in {args.realm}")
        print("the engine can only price an item it has seen being listed; "
              "check the item id with the audit report")
        return 1
    base = target.iloc[-1].to_dict()
    recommendation = strategy.recommend(base, qty=args.qty, deadline_h=horizon)
    # the grid is long; the answer is at the top of it
    candidates = recommendation.pop("candidates", [])
    best = recommendation.get("recommended_ask")
    near = [c for c in candidates if best and abs(c["ask_unit"] - best) <= 0]
    print(json.dumps(recommendation, indent=2, default=str))
    if candidates:
        print(f"\n-- the grid around the recommendation ({len(candidates)} candidates) --")
        for entry in candidates:
            mark = " <=" if entry["ask_unit"] == best else ""
            print(f"  ask {entry['ask_unit']:>10}  p(sell) {entry['sale_probability']:.3f}"
                  f"  E[gold] {entry['expected_net_gold']:8.2f}{mark}")
    return 0


def cmd_compare(config: MLConfig, args) -> int:
    """Run the fair chronological price-model comparison."""
    engine = DfaLearningEngine(config)
    engine.load()
    frame, _report, link = engine.build_rows()
    result = compare_price_models(
        frame,
        target="y_net_unit_log",
        test_fraction=args.test_fraction,
        calibration_fraction=0.20,
        level=0.8,
        record_iterations=config.price_iterations,
        sentinel_iterations=config.iterations,
        bootstrap=args.comparison_bootstrap,
        data_quality={"observation_end": int(engine.observation_end)})
    reports_dir = Path(config.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "dfa_price_comparison.json"
    md_path = reports_dir / "dfa_price_comparison.md"
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md_path.write_text(comparison_markdown(result), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    for name, values in (result.get("models") or {}).items():
        interval = values.get("interval") or {}
        print(f"  {name:<28} MAE {values.get('mae_log', 0):.3f}  "
              f"width {interval.get('half_width_log', 0):.3f}  "
              f"coverage {interval.get('coverage')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command",
                        choices=["audit", "train", "sentinel", "compare",
                                 "recommend", "all"])
    parser.add_argument("--tsm-db", default=None)
    parser.add_argument("--dfa-db", default=None)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--no-dfa-demand", action="store_true")
    parser.add_argument("--availability-clock", choices=["updated", "fetched"], default="updated")
    parser.add_argument("--refresh-journal", action="store_true",
                        help="re-read the Journalator archive into auction_events first")
    parser.add_argument("--realm", default="Dragonmaw")
    parser.add_argument("--item-id", type=int, default=0)
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--horizon", type=int, default=72)
    parser.add_argument("--deadline", type=int, default=72)
    parser.add_argument("--objective", choices=["ev", "ev_deadline", "probability"],
                        default="ev")
    parser.add_argument("--iterations", type=int, default=None,
                        help="trees per model; a small number makes a wiring "
                             "check run in seconds")
    parser.add_argument("--sentinel-dir", default=None,
                        help="where the versioned Sentinel artifacts live")
    parser.add_argument("--sentinel-reports-dir", default=None,
                        help="where the Sentinel report is written; an isolated "
                             "run must not overwrite the active report")
    parser.add_argument("--sentinel-iterations", type=int, default=None,
                        help="trees for the Sentinel candidates")
    parser.add_argument("--frame-cache", default=None,
                        help="with sentinel: read the feature frame from this "
                             "parquet file instead of rebuilding it, so two "
                             "runs are scored on exactly the same listings. "
                             "The file is written when it does not exist yet.")
    parser.add_argument("--comparison-bootstrap", type=int, default=400,
                        help="paired bootstrap draws for the comparison command")
    parser.add_argument("--if-needed", action="store_true",
                        help="with sentinel: skip the fit when no new labels arrived")
    parser.add_argument("--dry-run", action="store_true",
                        help="with sentinel: stage the artifact but do not promote it")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    config = MLConfig()
    if args.tsm_db:
        config.tsm_db = Path(args.tsm_db)
    if args.dfa_db:
        config.dfa_db = Path(args.dfa_db)
    if args.no_dfa_demand:
        config.use_dfa_demand = False
    if args.iterations:
        config.iterations = args.iterations
    config.availability_clock = args.availability_clock
    config.ensure_dirs()

    if args.refresh_journal:
        print("refreshing the Journalator archive ...")
        result = refresh_journalator(config)
        print("   ", json.dumps(result, default=str))

    if args.command == "audit":
        return cmd_audit(config, args)
    if args.command == "train":
        return cmd_train(config, args)
    if args.command == "sentinel":
        return cmd_sentinel(config, args)
    if args.command == "compare":
        return cmd_compare(config, args)
    if args.command == "recommend":
        return cmd_recommend(config, args)
    cmd_audit(config, args)
    return cmd_train(config, args)


if __name__ == "__main__":
    raise SystemExit(main())
