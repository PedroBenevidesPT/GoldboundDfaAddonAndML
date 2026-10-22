"""Build the DfA pricing dataset and the recommendation report.

    python scripts/dfa_pricing_export.py            # build the dataset
    python scripts/dfa_pricing_export.py --report   # also write the report
    python scripts/dfa_pricing_export.py --realm Dragonmaw

The dataset is written to ``DFA_Estimate/Data/DFA_Estimate_Data.lua``,
which the data addon loads.  Nothing here talks to the game.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from woweconomy.pricing.config import PricingConfig  # noqa: E402
from woweconomy.pricing.dataset import build_dataset, validate_dataset  # noqa: E402
from woweconomy.pricing.engine import PricingEngine  # noqa: E402
from woweconomy.pricing.report import write_report  # noqa: E402
from woweconomy.pricing.shipped import SHIPPED_REFERENCES  # noqa: E402

logger = logging.getLogger("dfa_pricing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tsm-db", default=None)
    parser.add_argument("--dfa-db", default=None)
    parser.add_argument("--realm", action="append", default=None,
                        help="limit the dataset to these realms (repeatable)")
    parser.add_argument("--realm-model", action="store_true",
                        help="use a realm's own trained model where one exists")
    parser.add_argument("--all-realms", dest="all_realms", action="store_true",
                        default=True,
                        help="ship a factor/competition line for every known realm"
                             " so the game can price and rank realms it never traded in")
    parser.add_argument("--no-all-realms", dest="all_realms", action="store_false")
    parser.add_argument("--report", action="store_true",
                        help="also write the recommendation report")
    parser.add_argument("--validation", action="store_true",
                        help="also write the validation report")
    parser.add_argument("--dry-run", action="store_true",
                        help="build it but do not write the file")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    config = PricingConfig()
    if args.tsm_db:
        config.tsm_db = Path(args.tsm_db)
    if args.dfa_db:
        config.dfa_db = Path(args.dfa_db)
    config.ensure_dirs()

    engine = PricingEngine(config)
    engine.load()
    if engine.price_model is None:
        print("error: no price model found in", config.models_dir)
        print("run `python scripts/dfa_ml.py train` first")
        return 1

    if args.realm_model:
        loaded = engine.load_realm_models()
        print("realm models used: "
              + (", ".join(sorted(loaded)) if loaded else "(none trained)"))

    print("fitting the model the game will run ...")
    shipped = engine.fit_shipped_model()
    if not shipped.coefficients:
        print("error: the shipped model could not be fitted")
        return 1
    evaluation = engine.shipped_evaluation
    out_of_sample = engine.shipped_evaluation_out_of_sample
    print(f"  trained on {shipped.n_train} rows "
          f"({shipped.n_complete} with every reference)")
    print(f"  residual sigma {shipped.residual_sigma:.3f} on the log scale")
    if evaluation:
        print(f"  in sample  --> rmse_log {evaluation.get('rmse_log', 0):.3f}  "
              f"within 25% {evaluation.get('within_25pct', 0) * 100:.0f}%  "
              f"within 50% {evaluation.get('within_50pct', 0) * 100:.0f}%")
    if out_of_sample.get("n"):
        print(f"  out of sample --> rmse_log {out_of_sample.get('rmse_log', 0):.3f}  "
              f"within 25% {out_of_sample.get('within_25pct', 0) * 100:.0f}%  "
              f"within 50% {out_of_sample.get('within_50pct', 0) * 100:.0f}%  "
              f"(n={int(out_of_sample['n'])})")
        print(f"  the confidence shipped is the out-of-sample one: "
              f"{shipped.within_25 * 100:.0f}% / {shipped.within_50 * 100:.0f}%")
    print("  coefficients: " + ", ".join(
        f"{name}={value:+.3f}" for name, value in zip(shipped.references, shipped.coefficients)))
    print(f"  the model uses: {', '.join(SHIPPED_REFERENCES)}")

    # The record price is the CatBoost model, not the shipped linear one;
    # its range has to be calibrated on its own holdout.  One extra fit,
    # once per export, and the number travels with the dataset.
    print("calibrating the record price interval ...")
    record_interval = engine.calibrate_price_interval()
    if record_interval:
        coverage = record_interval.get("coverage")
        print(f"  {float(record_interval.get('level') or 0.8) * 100:.0f}% interval "
              f"+/- {float(record_interval['half_width_log']):.3f} on the log scale; "
              f"coverage "
              + (f"{float(coverage) * 100:.0f}% of "
                 f"{int(record_interval.get('test_n') or 0)} test sales"
                 if coverage is not None else "unmeasured"))
    else:
        print("  no interval: the archive is too short for a chronological holdout")

    builder, notes = build_dataset(engine, config, realms=args.realm)
    if args.all_realms:
        from woweconomy.intel import store
        from woweconomy.pricing.realm_index import build_index

        con = store.connect(config.dfa_db)
        realms = sorted(builder.realm_groups) or None
        builder.realm_index = build_index(engine, builder.region_prices, con,
                                          realms=realms,
                                          horizon_h=config.horizon_h)
        for realm in builder.realm_index:
            builder.realm_groups.setdefault(
                realm, str((builder.realms.get(realm) or {}).get("auctionHouse") or ""))
        measured = sum(1 for entry in builder.realm_index.values()
                       if entry.get("source") == "realm")
        with_undercut = sum(1 for entry in builder.realm_index.values()
                            if entry.get("undercutShare") is not None)
        with_risk = sum(1 for entry in builder.realm_index.values()
                        if entry.get("babysit24h") is not None)
        print(f"realm index: {len(builder.realm_index)} realms "
              f"({measured} with own sales, {with_undercut} with a measured"
              f" undercut share, {with_risk} with a 24h babysit risk)")
    if args.realm_model:
        used = sorted(getattr(engine, "realm_models", {}))
        builder.notes.append("realm price models used: "
                             + (", ".join(used) if used else "none"))
    text = builder.to_lua()
    validation = validate_dataset(text)

    print(f"realms with records: {len(builder.realms)}   "
          f"realms mapped: {len(builder.realm_groups)}   "
          f"records: {len(builder.records)}   "
          f"regional prices: {len(builder.region_prices)}   "
          f"appearances: {len(builder.appearances)}")
    if len(builder.region_prices) > len(builder.records):
        print("  the model answers for every item in the regional price table, "
              "in every mapped realm; the records are only the items the "
              "account traded there")
    print(f"checksum: {builder.checksum()[:16]}...")
    if validation["problems"]:
        print("the dataset did not validate:")
        for problem in validation["problems"]:
            print("   -", problem)
        return 1
    print("the dataset validates")

    if not args.dry_run:
        path = builder.write()
        print(f"written to {path} ({path.stat().st_size:,} bytes)")

    if args.report:
        report = write_report(config, engine, builder)
        print(f"report written to {report}")
    if args.validation:
        from woweconomy.pricing.validation import write_validation_report
        validation = write_validation_report(config, engine, builder)
        print(f"validation report written to {validation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
