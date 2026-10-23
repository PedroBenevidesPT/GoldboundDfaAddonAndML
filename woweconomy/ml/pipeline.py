"""The end-to-end pipeline.

    archive -> link -> features -> dataset -> walk-forward -> models -> strategy

Every stage records what it did, so the report can say where the numbers
came from and what had to be thrown away.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import demand as demand_mod
from .config import MLConfig
from .dataset import DatasetReport, build_dataset
from .features import ASK_FEATURES, FEATURE_DOCS, FeatureBuilder
from .journalator import JournalArchive, build_archive
from .linking import LinkReport, link_postings
from .models import (
    CATBOOST_AVAILABLE,
    NON_FEATURE_COLUMNS,
    categorical_columns,
    concordance_index,
    feature_columns,
    make_pool,
    price_metrics,
    probability_metrics,
    train_price_model,
    train_price_quantiles,
    train_sale_probability_model,
    train_time_to_sale_model,
)
from .strategy import ListingStrategy, StrategyConfig
from ..dbcompat import connect as db_connect
from ..dbcompat import missing_table_errors
from .tsm_history import TSMHistory, load_region_history
from .validate import Split, aggregate, make_split, reference_baselines

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_name_index(tsm_db: Path) -> Dict[str, int]:
    """item name -> item id, from every catalogue the mirror holds."""
    index: Dict[str, int] = {}
    # db_connect: in the worker container these tables are in Postgres,
    # and sqlite3.connect on the missing path created an empty file -- the
    # name index then came back empty and every item looked nameless.
    con = db_connect(tsm_db)
    try:
        for item_id, name in con.execute("SELECT itemId, name FROM items"):
            if name:
                index.setdefault(str(name), int(item_id))
        for item_id, name in con.execute("SELECT item_id, name FROM tsm_realm_price"):
            if name:
                index.setdefault(str(name), int(item_id))
        for item_id, name in con.execute("SELECT key_id, name FROM tsm_region_price"):
            if name:
                index.setdefault(str(name), int(item_id))
    except missing_table_errors() as exc:
        logger.warning("name index incomplete: %s", exc)
    finally:
        con.close()
    return index


def base_columns(frame: pd.DataFrame) -> List[str]:
    """Every column the strategy's base row needs.

    The *raw* references are included even though they are not model
    features: the strategy needs the regional sale average to place the
    ask on the same scale the demand curve was measured on.  The raw
    references are told apart from the model's own ``ref_dispersion`` and
    ``ref_count`` features by name, so nothing is added twice.
    """
    cols = list(feature_columns(frame))
    cols += [c for c in frame.columns
             if c.startswith("ref_") and c in NON_FEATURE_COLUMNS]
    return cols


@dataclass
class PipelineResult:
    """Everything the pipeline produced."""

    started_at: str = ""
    finished_at: str = ""
    archive_summary: Dict[str, int] = field(default_factory=dict)
    link_report: Dict[str, object] = field(default_factory=dict)
    dataset_report: Dict[str, object] = field(default_factory=dict)
    split: List[Dict[str, object]] = field(default_factory=list)
    price_validation: Dict[str, object] = field(default_factory=dict)
    probability_validation: Dict[str, object] = field(default_factory=dict)
    time_to_sale_validation: Dict[str, object] = field(default_factory=dict)
    reference_baselines: Dict[str, object] = field(default_factory=dict)
    ablation: Dict[str, object] = field(default_factory=dict)
    feature_importance: Dict[str, float] = field(default_factory=dict)
    probability_importance: Dict[str, Dict[str, float]] = field(default_factory=dict)
    strategy_evaluation: Dict[str, object] = field(default_factory=dict)
    model_paths: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    duration_s: float = 0.0


class DfaLearningEngine:
    """The learning engine, wired to the real data."""

    def __init__(self, config: Optional[MLConfig] = None) -> None:
        self.config = config or MLConfig()
        self.history: Optional[TSMHistory] = None
        self.demand_book = None
        self.archive: Optional[JournalArchive] = None
        self.frame: Optional[pd.DataFrame] = None
        self.dataset_report = DatasetReport()
        #: the end of the observation period: the last moment for which
        #: an outcome is known
        self.observation_end: int = 0
        self._final_price_model = None
        self._final_prob_models: Dict[int, object] = {}
        self._final_tts_model = None
        self._final_quantiles: Dict[float, object] = {}
        self._final_ask_bounds: Optional[Tuple[float, float]] = None

    # -- stage 1: the data ------------------------------------------------
    def load(self) -> None:
        cfg = self.config
        scopes = self._realms_of_interest()
        self.history = TSMHistory(cfg.tsm_db, scopes=scopes or None,
                                  availability_clock=cfg.availability_clock)
        # the older complete region states extend the regional timeline
        # back to July, so a listing on 16 October can see a region
        # reference that is three days old instead of none at all
        legacy = load_region_history(cfg.tsm_db)
        if legacy:
            self.history.inject_full_states("region", "region_items", legacy)
        logger.info("TSM history loaded for %d scopes", len(scopes))

        names = build_name_index(cfg.tsm_db)
        # The libraries come from the published copy when there is one, so
        # the same code reads the journal on the workstation and in the
        # container.  Falling back to the AddOns folder keeps a machine that
        # has never run the publisher working unchanged.
        libs_dir = cfg.journalator_libs_dir
        if not (Path(libs_dir) / "Journalator").exists():
            libs_dir = cfg.addons_dir
        self.archive = build_archive(cfg.journalator_lua, libs_dir, names)
        if self.archive.errors:
            logger.warning("journalator: %s", "; ".join(self.archive.errors))
        # the demand counters are loaded in :meth:`build_rows`, once the
        # universe of items is known -- loading them twice would only
        # make the 2 GB scan slower.

    def _realms_of_interest(self) -> List[str]:
        """The TSM scopes of the realms the account actually trades on.

        A connected-realm group shares one auction house, so TSM files its
        data under the group's canonical realm; the member's own slug
        would find nothing.
        """
        cfg = self.config
        con = db_connect(cfg.tsm_db)
        try:
            rows = con.execute(
                "SELECT DISTINCT realm FROM auction_events WHERE realm <> ''").fetchall()
            canonical = {str(name): str(slug) for name, slug in con.execute(
                "SELECT realm_name, canonical_slug FROM tsm_realm_group")}
        except missing_table_errors():
            rows, canonical = [], {}
        finally:
            con.close()
        scopes: List[str] = []
        for (realm,) in rows:
            scope = canonical.get(str(realm), "")
            if scope and scope not in scopes:
                scopes.append(scope)
        return scopes

    # -- stage 2: the rows ------------------------------------------------
    def build_rows(self) -> Tuple[pd.DataFrame, DatasetReport, LinkReport]:
        cfg = self.config
        assert self.history is not None and self.archive is not None

        outcomes, link_report = link_postings(self.archive)
        self.observation_end = link_report.observation_end
        logger.info("linked %d postings", len(outcomes))

        # A posting older than the retention window has no market history left
        # to describe it: the TSM snapshots that would supply its features have
        # been trimmed.  Keeping such a row would train on rows whose features
        # are silently absent, so they are dropped here and counted, rather
        # than imputed.  Labels are unaffected -- they come from the journal
        # archive, which retention does not touch.
        dropped = 0
        cutoff = 0
        if cfg.retention_days:
            cutoff = int(self.observation_end) - cfg.retention_days * 86400
            before = len(outcomes)
            outcomes = [o for o in outcomes if o.posting.time >= cutoff]
            dropped = before - len(outcomes)
            if dropped:
                logger.info(
                    "dropped %d postings older than the %dd retention cutoff "
                    "(%s)", dropped, cfg.retention_days,
                    datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                )

        # the DfA demand needs the item ids of the rows, so it is loaded
        # once the item universe is known
        item_ids = sorted({o.posting.item_id for o in outcomes if o.posting.item_id})
        if cfg.use_dfa_demand:
            self.demand_book = demand_mod.load_demand(cfg.dfa_db, cfg.tsm_db, item_ids)
            groups = demand_mod.load_realm_groups(cfg.dfa_db)
        else:
            self.demand_book = demand_mod.DemandBook(notes=["disabled by configuration"])
            groups = {}

        builder = FeatureBuilder(self.history, self.demand_book, groups,
                                 freshness_cap_h=cfg.reference_freshness_cap_h)
        frame, report = build_dataset(outcomes, builder, cfg.horizons_h,
                                      link_report.observation_end)
        report.retention_days = int(cfg.retention_days)
        report.cutoff_ts = int(cutoff)
        report.rows_dropped_before_cutoff = int(dropped)
        self.frame = frame
        self.dataset_report = report
        return frame, report, link_report

    # -- stage 3: the validation ------------------------------------------
    def validate(self, split: Split) -> Dict[str, object]:
        """Walk-forward validation of the three models.

        The validation day is the day being scored, so it must not also
        choose the model: every fit here runs with a fixed number of
        trees and no early stopping.
        """
        cfg = self.config
        assert self.frame is not None
        frame = self.frame
        cats = categorical_columns(frame)
        numeric = feature_columns(frame)
        price_feats = [c for c in numeric if c not in ASK_FEATURES]
        iterations = cfg.iterations

        # --- Model A ---------------------------------------------------
        price_fold_metrics: List[Dict[str, float]] = []
        for fold in split.folds:
            train = fold.train[fold.train["y_net_unit_log"].notna()]
            valid = fold.valid[fold.valid["y_net_unit_log"].notna()]
            if len(train) < 30 or len(valid) < 5:
                continue
            model = train_price_model(train, None, price_feats, cats,
                                      early_stopping=False,
                                      iterations=cfg.price_iterations)
            if not model.trained:
                continue
            y = valid["y_net_unit_log"].to_numpy(dtype=float)
            p = model.predict_log(valid)
            price_fold_metrics.append(price_metrics(
                y, np.asarray(p, dtype=float),
                valid["y_net_unit"].to_numpy(dtype=float)))

        price_metrics_all = aggregate(price_fold_metrics)

        # --- Model B ---------------------------------------------------
        probability_metrics_all: Dict[str, object] = {}
        for horizon in cfg.horizons_h:
            fold_metrics: List[Dict[str, float]] = []
            for fold in split.folds:
                train = fold.train[fold.train[f"label_known_{horizon}h"] == 1]
                valid = fold.valid[fold.valid[f"label_known_{horizon}h"] == 1]
                if (len(train) < 30 or len(valid) < 5
                        or train[f"sold_within_{horizon}h"].nunique() < 2):
                    continue
                model = train_sale_probability_model(
                    train, None, horizon, numeric, cats,
                    early_stopping=False, iterations=iterations)
                if not model.trained:
                    continue
                y = valid[f"sold_within_{horizon}h"].to_numpy(dtype=float)
                p = model.predict(valid)
                fold_metrics.append(probability_metrics(y, p))
            probability_metrics_all[str(horizon)] = aggregate(fold_metrics)

        # --- Model C ---------------------------------------------------
        tts_fold_metrics: List[Dict[str, float]] = []
        for fold in split.folds:
            train = fold.train
            valid = fold.valid
            if len(train[train["tts_hours"].notna()]) < 40:
                continue
            model = train_time_to_sale_model(
                train, None, numeric, cats,
                early_stopping=False, iterations=iterations)
            if not model.trained:
                continue
            sub = valid[valid["tts_hours"].notna() & (valid["tts_hours"] > 0)]
            if len(sub) < 10:
                continue
            y = sub["tts_hours"].to_numpy(dtype=float)
            raw = np.asarray(model.model.predict(make_pool(sub, model.features, cats)),
                             dtype=float)
            observed = sub["tts_censored"].to_numpy() == 0
            tts_fold_metrics.append({"c_index": concordance_index(
                y, raw, event=observed),
                                      "n": int(len(sub)),
                                      "n_events": int(observed.sum())})
        tts_metrics = aggregate(tts_fold_metrics)

        return {
            "price": price_metrics_all,
            "probability": probability_metrics_all,
            "time_to_sale": tts_metrics,
            "price_folds": price_fold_metrics,
            "n_price_folds": len(price_fold_metrics),
        }

    # -- stage 3b: the untouched test window --------------------------------
    def test_window(self, frame: pd.DataFrame, split: Split) -> Dict[str, object]:
        """Score the models on windows no fold ever trained on.

        The two models need different test windows, and the reason is the
        shape of the observation period:

        * a **price** is known the moment a sale happens, so the last day
          of data is a valid test window;
        * a **sale probability inside a horizon** needs the horizon to
          *close* before the archive ends.  A listing made on the final
          evening has not had a day in which to sell, so its label is
          censored -- and a score computed on the rows that happened to
          resolve fast is not a score.

        The probability models are therefore tested on the last day whose
        horizon can close, with a model trained only on the days before
        it.
        """
        cfg = self.config
        test = split.test
        out: Dict[str, object] = {
            "rows": int(len(test)),
            "days": [int(split.test_span[0]), int(split.test_span[1])],
            "note": "",
        }
        if test.empty:
            out["note"] = "the test window is empty"
            return out

        cats = categorical_columns(frame)
        numeric = feature_columns(frame)
        price_feats = [c for c in numeric if c not in ASK_FEATURES]
        iterations = cfg.iterations
        # the test window is untouched: nothing is trained on it
        dev = frame[~frame["date"].isin(test["date"].unique())]

        # --- Model A ---------------------------------------------------
        train = dev[dev["y_net_unit_log"].notna()]
        test_price = test[test["y_net_unit_log"].notna()]
        price_block: Dict[str, object] = {"n_train": int(len(train)),
                                          "n_test": int(len(test_price))}
        if len(train) >= 30 and len(test_price) >= 5:
            model = train_price_model(train, None, price_feats, cats,
                                      early_stopping=False,
                                      iterations=cfg.price_iterations)
            if model.trained:
                y = test_price["y_net_unit_log"].to_numpy(dtype=float)
                p = model.predict_log(test_price)
                price_block["metrics"] = price_metrics(
                    y, np.asarray(p, dtype=float),
                    test_price["y_net_unit"].to_numpy(dtype=float))
                price_block["importance"] = dict(sorted(model.importance.items(),
                                                        key=lambda kv: -kv[1])[:20])
        out["price"] = price_block

        # --- Model B ---------------------------------------------------
        # a probability inside a horizon needs the horizon to *close*
        # before the archive ends, so the holdout is the last day on which
        # that can happen, with a model trained only on the days before it
        prob_block: Dict[str, object] = {}
        for horizon in cfg.horizons_h:
            label = f"sold_within_{horizon}h"
            eligible = frame[(frame["ts"] + horizon * 3600) <= self.observation_end]
            holdout = eligible[eligible["date"].isin(dev["date"].unique())]
            if holdout.empty:
                prob_block[str(horizon)] = {
                    "evaluable": False, "n_train": 0, "n_test": 0,
                    "note": ("no listing in the untouched window has had this "
                             "horizon close inside the observation period"),
                }
                continue
            holdout_day = int(holdout["date"].max())
            holdout = holdout[holdout["date"] == holdout_day]
            train_days = [d for d in sorted(dev["date"].unique()) if d < holdout_day]
            train_h = frame[frame["date"].isin(train_days)
                            & (frame[f"label_known_{horizon}h"] == 1)]
            test_h = holdout[holdout[f"label_known_{horizon}h"] == 1]
            coverage = len(test_h) / len(holdout) if len(holdout) else 0.0
            block: Dict[str, object] = {
                "holdout_day": holdout_day,
                "n_train": int(len(train_h)),
                "n_test": int(len(test_h)),
                "n_holdout": int(len(holdout)),
                "label_coverage_on_test": round(coverage, 4),
            }
            if len(train_h) < 30:
                block["evaluable"] = False
                block["note"] = (
                    f"the holdout that works for a {horizon}h horizon is day "
                    f"{holdout_day}, and there is no earlier day left to train on: "
                    "the observation period is not long enough to hold a window "
                    "out for this horizon")
            elif coverage < 0.6:
                block["evaluable"] = False
                block["note"] = ("the observation window ends before this horizon "
                                 "closes for most of the holdout rows, so the score "
                                 "would be measured on the few rows that happened "
                                 "to resolve")
            else:
                block["evaluable"] = True
                if len(test_h) >= 5 and train_h[label].nunique() >= 2:
                    model = train_sale_probability_model(
                        train_h, None, horizon, numeric, cats,
                        early_stopping=False, iterations=iterations)
                    if model.trained:
                        y = test_h[label].to_numpy(dtype=float)
                        p = model.predict(test_h)
                        block["metrics"] = probability_metrics(y, p)
                        block["importance"] = dict(sorted(model.importance.items(),
                                                          key=lambda kv: -kv[1])[:15])
            prob_block[str(horizon)] = block
        out["probability"] = prob_block

        # --- Model C ---------------------------------------------------
        tts_block: Dict[str, object] = {}
        train_t = dev[dev["tts_hours"].notna()]
        test_t = test[test["tts_hours"].notna()]
        tts_block["n_train"] = int(len(train_t))
        tts_block["n_test"] = int(len(test_t))
        if len(train_t) >= 40 and len(test_t) >= 20:
            model = train_time_to_sale_model(
                train_t, None, numeric, cats,
                early_stopping=False, iterations=iterations)
            if model.trained:
                sub = test_t[test_t["tts_hours"].notna() & (test_t["tts_hours"] > 0)]
                if len(sub) >= 10:
                    raw = np.asarray(model.model.predict(
                        make_pool(sub, model.features, cats)), dtype=float)
                    observed = sub["tts_censored"].to_numpy() == 0
                    tts_block["metrics"] = {
                        "c_index": concordance_index(
                            sub["tts_hours"].to_numpy(dtype=float), raw,
                            event=observed),
                        "n": int(len(sub)),
                        "n_events": int(observed.sum()),
                        "n_censored": int((~observed).sum()),
                        "censoring_rate": float((~observed).mean()),
                        "median_predicted_hours": float(
                            np.median(model.predict_median_hours(sub))),
                        "median_observed_hours": float(np.median(sub["tts_hours"])),
                    }
                    tts_block["calibration"] = list(model.calibration)
                    if model.note:
                        tts_block["note"] = model.note
        out["time_to_sale"] = tts_block
        return out

    # -- stage 4: the demand curve ------------------------------------------
    def demand_curve(self, frame: pd.DataFrame,
                     horizon: int = 24,
                     reference: str = "ref_region_sale_avg") -> Dict[str, object]:
        """The empirical demand curve: how the sale rate falls with the ask.

        This is the measurement the strategy needs and the model cannot
        give on its own.  A model of ``P(sell)`` says how likely a sale
        is at the prices the account happened to use; the curve says what
        happens when the ask is moved, which is a different question.

        The reference is the regional sale average -- the average a sale
        actually closed at -- so the ratio is "how far above what buyers
        pay is this listing asking".
        """
        label = f"sold_within_{horizon}h"
        if label not in frame.columns or reference not in frame.columns:
            return {"error": "the columns are not there"}
        usable = frame[(frame[f"label_known_{horizon}h"] == 1)
                       & frame[reference].notna() & (frame[reference] > 0)].copy()
        if usable.empty:
            return {"error": "no rows"}
        usable["ratio"] = usable["ask_unit"] / usable[reference]
        edges = [0.0, 0.5, 0.7, 0.85, 1.0, 1.15, 1.4, 2.0, 3.0, 1e9]
        usable["band"] = pd.cut(usable["ratio"], edges, include_lowest=True)
        out: Dict[str, object] = {"reference": reference, "horizon_h": horizon,
                                  "bands": [], "rows": int(len(usable))}
        for band, group in usable.groupby("band", observed=True):
            if len(group) < 5:
                continue
            lo = float(band.left) if np.isfinite(band.left) else 0.0
            hi = float(band.right) if np.isfinite(band.right) else 0.0
            mid = (lo + hi) / 2.0 if hi < 1e8 else lo * 1.5
            sale_rate = float(group[label].mean())
            out["bands"].append({
                "band": str(band),
                "n": int(len(group)),
                "sale_rate": sale_rate,
                "mid_ratio": mid,
                # the expected gold per listing: the chance of selling
                # times what is collected when it sells.  This is the
                # quantity the strategy is really trying to maximise, and
                # measuring it on the data does not depend on the model
                # being right.
                "expected_gold_ratio": sale_rate * mid,
                "share_of_sales": float(group[label].sum() / max(1, usable[label].sum())),
            })
        if out["bands"]:
            best = max(out["bands"], key=lambda b: b["expected_gold_ratio"])
            out["best_band"] = best["band"]
            out["best_expected_gold_ratio"] = best["expected_gold_ratio"]
            out["best_sale_rate"] = best["sale_rate"]
        ratios = usable["ratio"].to_numpy(dtype=float)
        out["median_ratio"] = float(np.median(ratios))
        out["share_asked_above_3x"] = float(np.mean(ratios > 3.0))
        out["sale_rate_above_3x"] = float(
            usable.loc[usable["ratio"] > 3.0, label].mean()) \
            if (usable["ratio"] > 3.0).any() else None
        out["sale_rate_below_1_4x"] = float(
            usable.loc[usable["ratio"] <= 1.4, label].mean()) \
            if (usable["ratio"] <= 1.4).any() else None
        return out

    # -- stage 4b: the speculation curve -----------------------------------
    def speculation_curve(self, frame: pd.DataFrame, horizon: int = 24) -> Dict[str, object]:
        """What happens when the asking reference detaches from the sales.

        The brief's two hypotheses live in this measurement.  When
        ``dbregionmarketavg`` and ``dbregionsaleavg`` agree, an ask
        placed on the asking reference clears; when the asking reference
        runs far above the price buyers actually pay, an ask placed on it
        does not.

        The curve bins the listings by the *ratio between the two
        references* and reports, for each band, how often the listing
        sold and where the ask sat relative to the asking reference.
        """
        label = f"sold_within_{horizon}h"
        need = ("ref_region_market", "ref_region_sale_avg", "ask_unit")
        if label not in frame.columns or any(c not in frame.columns for c in need):
            return {"error": "the columns are not there"}
        usable = frame[(frame[f"label_known_{horizon}h"] == 1)].copy()
        usable = usable[usable["ref_region_market"].notna()
                        & (usable["ref_region_market"] > 0)
                        & usable["ref_region_sale_avg"].notna()
                        & (usable["ref_region_sale_avg"] > 0)]
        if usable.empty:
            return {"error": "no rows"}
        usable["speculation"] = (usable["ref_region_market"]
                                 / usable["ref_region_sale_avg"])
        usable["ask_over_asking_ref"] = (usable["ask_unit"]
                                         / usable["ref_region_market"])
        edges = [0.0, 1.05, 1.3, 2.0, 4.0, 8.0, 20.0, 1e9]
        usable["band"] = pd.cut(usable["speculation"], edges, include_lowest=True)
        out: Dict[str, object] = {
            "horizon_h": horizon, "rows": int(len(usable)), "bands": [],
            "explanation": ("dbregionmarketavg over dbregionsaleavg: 1.0 means the "
                            "asking reference and the price buyers pay agree"),
        }
        for band, group in usable.groupby("band", observed=True):
            if len(group) < 5:
                continue
            realised = group["y_net_unit"] / group["ref_region_sale_avg"]
            realised = realised.replace([float("inf"), float("-inf")], None).dropna()
            out["bands"].append({
                "band": str(band),
                "n": int(len(group)),
                "sale_rate": float(group[label].mean()),
                "median_ask_over_asking_ref": float(group["ask_over_asking_ref"].median()),
                "median_realised_over_sale_avg": float(realised.median())
                if len(realised) else None,
            })
        return out

    # -- stage 5: the reference credibility --------------------------------
    def horizon_equivalence(self, frame: pd.DataFrame) -> Dict[str, object]:
        """Do the different horizons actually ask different questions?

        An auction lasts 12, 24 or 48 hours.  Once a horizon passes that,
        every sale that was going to happen has already happened, so the
        72-hour, 168-hour and 336-hour questions collapse into the same
        question.  Reporting them as three separate models would be three
        copies of one result.
        """
        out: Dict[str, object] = {"pairs": [], "identical_groups": []}
        horizons = list(self.config.horizons_h)
        groups: List[List[int]] = []
        for i, a in enumerate(horizons):
            for b in horizons[i + 1:]:
                la, lb = f"sold_within_{a}h", f"sold_within_{b}h"
                if la not in frame.columns or lb not in frame.columns:
                    continue
                both = frame[(frame[f"label_known_{a}h"] == 1)
                             & (frame[f"label_known_{b}h"] == 1)]
                if both.empty:
                    continue
                agree = float((both[la] == both[lb]).mean())
                out["pairs"].append({"horizons": [a, b], "rows": int(len(both)),
                                     "label_agreement": round(agree, 4)})
        for horizon in horizons:
            label = f"sold_within_{horizon}h"
            if label not in frame.columns:
                continue
            group = [h for h in horizons
                     if f"sold_within_{h}h" in frame.columns
                     and float((frame[label].fillna(-1)
                                == frame[f"sold_within_{h}h"].fillna(-1)).mean()) > 0.999]
            if len(group) > 1 and group not in groups:
                groups.append(group)
        out["identical_groups"] = groups
        return out

    def reference_credibility(self, frame: pd.DataFrame) -> Dict[str, object]:
        """How far each asking-price family sits from what buyers paid.

        This is the measurement the two hypotheses need: a family whose
        median ratio to the realised price is far above one is an
        inflated reference; a family close to one is commercially
        defensible.
        """
        candidates = {
            "dbmarket (local asking)": "ref_local_market",
            "dbrecent (local recent)": "ref_local_recent",
            "dbhistorical (local historical)": "ref_local_historical",
            "dbminbuyout (local floor)": "ref_local_min_buyout",
            "dbregionmarketavg (regional asking)": "ref_region_market",
            "dbregionhistorical (regional historical)": "ref_region_historical",
            "dbregionsaleavg (regional sales)": "ref_region_sale_avg",
        }
        baselines = reference_baselines(frame, "y_net_unit_log", candidates)
        out: Dict[str, object] = {"per_reference": {},
                                  "n": int(frame["y_net_unit_log"].notna().sum())}
        for name, metrics in baselines.items():
            if "median_ratio" not in metrics:
                out["per_reference"][name] = metrics
                continue
            entry = dict(metrics)
            ratio = metrics.get("median_reference_over_realised")
            if ratio is not None:
                entry["median_ratio"] = ratio
                entry["verdict"] = (
                    "inflated asking reference"
                    if ratio > 1.15 else
                    "commercially defensible"
                    if ratio >= 0.85 else
                    "below what buyers paid")
            out["per_reference"][name] = entry
        return out

    def ablation(self, frame: pd.DataFrame, split: Split,
                 price_feats: Sequence[str], cats: Sequence[str]) -> Dict[str, object]:
        """What does each reference family contribute?

        The model is refitted without the asking-price family and the
        walk-forward error is compared.  A family that changes nothing was
        noise; a family that changes the error a lot is doing work.
        """
        results: Dict[str, object] = {}
        groups = {
            "without the asking references": ("local_market_log", "local_min_buyout_log",
                                              "local_recent_log", "local_historical_log",
                                              "region_market_log", "region_historical_log"),
            "without the region sale average": ("region_sale_avg_log",),
            "without the local references": ("local_market_log", "local_recent_log",
                                             "local_historical_log", "local_min_buyout_log"),
            "without the DfA demand counters": tuple(
                c for c in price_feats if c.startswith("dfa_")),
        }
        for label, drop in groups.items():
            feats = [f for f in price_feats if f not in drop]
            fold_metrics: List[Dict[str, float]] = []
            for fold in split.folds:
                train = fold.train[fold.train["y_net_unit_log"].notna()]
                valid = fold.valid[fold.valid["y_net_unit_log"].notna()]
                if len(train) < 30 or len(valid) < 5:
                    continue
                # the same rule as the walk-forward: the validation day is
                # the day being scored, so it must not choose the model
                model = train_price_model(train, None, feats, cats,
                                          early_stopping=False,
                                          iterations=self.config.price_iterations)
                if not model.trained:
                    continue
                y = valid["y_net_unit_log"].to_numpy(dtype=float)
                p = model.predict_log(valid)
                fold_metrics.append(price_metrics(y, np.asarray(p, dtype=float)))
            results[label] = aggregate(fold_metrics)
        return results

    # -- stage 5b: the final models ------------------------------------------
    def fit_final(self, frame: pd.DataFrame) -> Dict[str, object]:
        """Fit the models on everything, ready for use."""
        cfg = self.config
        cats = categorical_columns(frame)
        numeric = feature_columns(frame)
        price_feats = [c for c in numeric if c not in ASK_FEATURES]
        out: Dict[str, object] = {}

        train_price = frame[frame["y_net_unit_log"].notna()]
        price_model = train_price_model(train_price, None, price_feats, cats,
                                        early_stopping=False,
                                        iterations=cfg.price_iterations)
        if price_model.trained:
            path = cfg.models_dir / "price_model.cbm"
            price_model.ask_ratio_bounds = self.ask_ratio_bounds(frame)
            price_model.save(path)
            out["price"] = {"path": str(path), "trained_on": int(len(train_price))}
            out["feature_importance"] = dict(sorted(price_model.importance.items(),
                                                    key=lambda kv: -kv[1])[:25])
            # the spread of the price, so that a recommendation can say how
            # much it is worth
            quantiles = train_price_quantiles(train_price, price_feats, cats,
                                              iterations=cfg.price_iterations)
            for alpha, model in quantiles.items():
                model.save_model(str(cfg.models_dir / f"price_model.q{int(alpha * 100)}.cbm"))
            if quantiles:
                out["price"]["quantiles"] = sorted(quantiles)
        else:
            out["price"] = {"trained": False, "note": price_model.note}

        prob_models: Dict[int, object] = {}
        for horizon in cfg.horizons_h:
            usable = frame[frame[f"label_known_{horizon}h"] == 1]
            model = train_sale_probability_model(
                usable, None, horizon, numeric, cats,
                early_stopping=False, iterations=cfg.iterations)
            if model.trained:
                path = cfg.models_dir / f"sale_probability_{horizon}h.cbm"
                model.save(path)
                prob_models[horizon] = model
                out.setdefault("probabilities", {})[str(horizon)] = {
                    "path": str(path), "trained_on": int(len(usable)),
                    "positives": int(usable[f"sold_within_{horizon}h"].sum())}
            else:
                out.setdefault("probabilities", {})[str(horizon)] = {
                    "trained": False, "note": model.note}
        out["feature_importance_probability"] = {
            str(h): dict(sorted(m.importance.items(), key=lambda kv: -kv[1])[:15])
            for h, m in prob_models.items()}

        tts_usable = frame[frame["tts_hours"].notna()]
        tts_model = train_time_to_sale_model(
            tts_usable, None, numeric, cats,
            early_stopping=False, iterations=cfg.iterations)
        if tts_model.trained:
            path = cfg.models_dir / "time_to_sale.cbm"
            tts_model.save(path)
            out["time_to_sale"] = {"path": str(path), "trained_on": int(len(tts_usable))}
        else:
            out["time_to_sale"] = {"trained": False, "note": tts_model.note}

        self._final_price_model = price_model
        self._final_prob_models = prob_models
        self._final_tts_model = tts_model
        self._final_quantiles = quantiles if price_model.trained else {}
        self._final_ask_bounds = self.ask_ratio_bounds(frame)
        out["ask_ratio_bounds"] = list(self._final_ask_bounds) \
            if self._final_ask_bounds else None
        return out

    # -- the strategy --------------------------------------------------------
    @staticmethod
    def make_curve(curve: Optional[Dict[str, object]]) -> Optional[Callable[[float], float]]:
        """The measured sale rate as a function of the ask / sale average."""
        if not curve or "bands" not in curve:
            return None
        points: List[Tuple[float, float]] = []
        for band in curve["bands"]:
            mid = band.get("mid_ratio")
            if mid:
                points.append((float(mid), float(band["sale_rate"])))
        if len(points) < 3:
            return None
        points.sort()
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        # the decay of the last measured segment, used to carry the curve
        # past its own support.  Holding the rate flat out there would
        # reward absurd prices: a listing asked at a hundred times the
        # price buyers pay would look as if it collected a hundred times
        # the average for a 2.6 % chance.
        if len(xs) >= 2 and ys[-2] > 0 and ys[-1] > 0 and xs[-1] > xs[-2]:
            slope = (math.log(ys[-1] / ys[-2]) / math.log(xs[-1] / xs[-2]))
        else:
            slope = -1.0

        def sale_rate(ratio: float) -> float:
            if ratio <= xs[0]:
                return ys[0]
            if ratio >= xs[-1]:
                return float(ys[-1] * (ratio / xs[-1]) ** slope)
            return float(np.interp(ratio, xs, ys))

        return sale_rate

    @staticmethod
    def ask_ratio_bounds(frame: pd.DataFrame,
                         low: float = 0.02,
                         high: float = 0.98) -> Optional[Tuple[float, float]]:
        """The range of asks the model was trained on, as a ratio.

        The probability model has only seen the prices the account
        actually asked.  A candidate outside that range is an
        extrapolation, so the strategy's grid is bounded by the observed
        quantiles of ``ask / regional sale average`` -- computed from the
        data, not chosen by hand.
        """
        if "ref_region_sale_avg" not in frame.columns:
            return None
        usable = frame[frame["ref_region_sale_avg"].notna()
                       & (frame["ref_region_sale_avg"] > 0)]
        if usable.empty:
            return None
        ratios = (usable["ask_unit"] / usable["ref_region_sale_avg"]).to_numpy(dtype=float)
        lo = float(np.quantile(ratios, low))
        hi = float(np.quantile(ratios, high))
        return (lo, hi) if hi > lo else None

    def _fold_strategy(self, fold, all_features, cats, curve_fn, numeric):
        """The models of one fold, and the strategy they advise with."""
        cfg = self.config
        price_feats = [c for c in numeric if c not in ASK_FEATURES]
        train, valid = fold.train, fold.valid
        price_train = train[train["y_net_unit_log"].notna()]
        if len(price_train) < 30 or valid.empty:
            return None, None
        price_model = train_price_model(price_train, None, price_feats, cats,
                                        early_stopping=False,
                                        iterations=cfg.price_iterations)
        if not price_model.trained:
            return None, None
        horizon = min(cfg.horizons_h)
        prob_train = train[train[f"label_known_{horizon}h"] == 1]
        prob_model = train_sale_probability_model(
            prob_train, None, horizon, numeric, cats,
            early_stopping=False, iterations=cfg.iterations)
        strategy = ListingStrategy(
            price_model, prob_model, None,
            StrategyConfig(deadline_h=horizon,
                           ask_ratio_bounds=self.ask_ratio_bounds(train)),
            curve=curve_fn)
        return strategy, valid

    def evaluate_strategy(self, frame: pd.DataFrame, split: Split,
                           curve: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        """Replay the strategy over the observed listings, honestly.

        The replay has to use a model that has **not** seen the listing
        being replayed.  The final model is fitted on everything, so
        replaying it over its own training rows would measure how well
        the model memorises the prices it was shown, not how well the
        strategy would have advised in real time.  Each fold therefore
        gets its own models, fitted on the days before it.
        """
        cats = categorical_columns(frame)
        numeric = feature_columns(frame)
        all_features = base_columns(frame)
        curve_fn = self.make_curve(curve)

        collected: List[Tuple[str, Dict[str, object]]] = []
        for fold in split.folds:
            strategy, valid = self._fold_strategy(fold, all_features, cats,
                                                  curve_fn, numeric)
            if strategy is None:
                continue
            outcome = strategy.evaluate(valid, all_features, cats)
            if "error" in outcome:
                continue
            collected.append((fold.name, outcome))
        if not collected:
            return {"error": "no fold could be replayed"}

        rows: List[Dict[str, object]] = []
        for name, block in collected:
            rows.append({
                "fold": name,
                "listings": block.get("listings_evaluated"),
                "sold": block.get("sold"),
                "median_ask_ratio": block.get("median_ask_ratio"),
                "sold_median_ask_ratio": (block.get("sold_listings") or {}).get("median_ask_ratio"),
                "expired_median_ask_ratio": (block.get("expired_listings") or {}).get("median_ask_ratio"),
                "mean_regret_gold": block.get("mean_regret_gold"),
                "median_abs_ask_gold": block.get("median_abs_ask_gold"),
            })
        return {
            "method": ("each fold is replayed with models fitted only on the "
                       "days before it, so the strategy never advises on a "
                       "listing its model has already seen"),
            "folds": rows,
            "listings_evaluated": int(sum(r["listings"] for r in rows)),
            "sold": int(sum(r["sold"] for r in rows)),
            "median_ask_ratio": _median_of(rows, "median_ask_ratio"),
            "sold_median_ask_ratio": _median_of(rows, "sold_median_ask_ratio"),
            "expired_median_ask_ratio": _median_of(rows, "expired_median_ask_ratio"),
            "mean_regret_gold": float(np.mean([r["mean_regret_gold"] for r in rows])),
            "median_abs_ask_gold": _median_of(rows, "median_abs_ask_gold"),
            "note": ("regret is computed under the monotone-demand assumption: "
                     "a lower ask is at least as likely to sell.  It is an upper "
                     "bound on what the strategy gives up, not proof that the "
                     "strategy maximises realised gold -- the counterfactual ask "
                     "was never observed"),
        }

    def strategy_value(self, frame: pd.DataFrame, split: Split,
                       curve: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        """What the strategy would have collected, against what was collected.

        This is the closest thing to a counterfactual that the data
        allows.  The demand curve is *measured* -- it says how often a
        listing at a given ask sold -- so applying it to a different ask
        is an estimate, but it is an estimate about a relationship the
        data actually observed, and not about a model the data cannot
        check.

        The comparison is per listing: the expected gold of asking what
        was asked, against the expected gold of asking what the strategy
        would have asked.
        """
        cats = categorical_columns(frame)
        numeric = feature_columns(frame)
        all_features = base_columns(frame)
        global_curve_fn = self.make_curve(curve)
        if global_curve_fn is None:
            return {"error": "no demand curve"}

        rows: List[Dict[str, object]] = []
        fold_curves: List[str] = []
        for fold in split.folds:
            # The curve must be fitted on the fold's *training* rows.  Using
            # one curve fitted on the whole frame -- including the rows it
            # is then applied to -- makes the comparison in-sample and
            # inflates the improvement by construction.
            fold_curve = self.demand_curve(fold.train)
            fold_fn = self.make_curve(fold_curve)
            sale_rate = fold_fn or global_curve_fn
            fold_curves.append("fold_train" if fold_fn else "global_fallback")
            strategy, valid = self._fold_strategy(fold, all_features, cats,
                                                  fold_fn or global_curve_fn, numeric)
            if strategy is None:
                continue
            for _, row in valid.iterrows():
                reference = row.get("ref_region_sale_avg")
                if not reference or reference <= 0:
                    continue
                actual_ask = int(row.get("ask_unit") or 0)
                if actual_ask <= 0:
                    continue
                base = {name: row.get(name) for name in all_features}
                base.update({name: row.get(name) for name in cats})
                base = {k: (None if (isinstance(v, float) and math.isnan(v)) else v)
                        for k, v in base.items()}
                rec = strategy.recommend(base, qty=int(row.get("qty") or 1))
                recommended = rec.get("recommended_ask")
                if not recommended:
                    continue
                actual_ratio = actual_ask / reference
                rec_ratio = recommended / reference
                rows.append({
                    "actual_ratio": actual_ratio,
                    "recommended_ratio": rec_ratio,
                    "actual_expected": sale_rate(actual_ratio) * actual_ratio,
                    "recommended_expected": sale_rate(rec_ratio) * rec_ratio,
                    "outcome": row.get("outcome"),
                })
        if not rows:
            return {"error": "no listing could be valued"}
        out = pd.DataFrame(rows)
        # One row set for the point estimate *and* the interval.  A
        # ``sale_rate`` that cannot be interpolated at some ratio yields
        # NaN, and pandas' ``.mean()`` skips it while numpy's does not: the
        # published ratio was therefore computed over more rows than the
        # interval, and the interval came out as [nan, nan].
        actual_values, recommended_values = self._finite_pairs(out)
        actual = float(actual_values.mean()) if len(actual_values) else 0.0
        recommended = (float(recommended_values.mean())
                       if len(recommended_values) else 0.0)
        ratio = (recommended / actual) if actual > 0 else None

        # A ratio of means with no interval is a point estimate pretending
        # to be a result.  The bootstrap is seeded, so the interval is
        # reproducible from the same rows.
        ci = self._bootstrap_ratio(out, seed=self.bootstrap_seed)
        supported = bool(
            ci is not None and ci[0] > 1.0
            and len(actual_values) >= self.min_strategy_rows
            and all(source == "fold_train" for source in fold_curves))
        best_band = _best_measured_band(curve)

        return {
            "listings": int(len(out)),
            "folds": len(fold_curves),
            "curve_source": sorted(set(fold_curves)),
            "median_actual_ratio": float(out["actual_ratio"].median()),
            "median_recommended_ratio": float(out["recommended_ratio"].median()),
            "expected_gold_actual": actual,
            "expected_gold_recommended": recommended,
            #: only published when the interval excludes "no improvement"
            "improvement": ratio if supported else None,
            "improvement_point_estimate": ratio,
            "improvement_ci": list(ci) if ci is not None else None,
            "improvement_ci_level": 0.95,
            "improvement_ci_method": "percentile bootstrap over listings, seeded",
            "status": "supported" if supported else "insufficient_evidence",
            "reason": ("the 95% interval on the improvement excludes 1.0 and "
                       "every fold's curve was fitted out of sample"
                       if supported else
                       "the interval on the improvement includes 1.0, the "
                       "sample is too small, or a fold fell back to the "
                       "in-sample curve"),
            "best_measured_band": best_band,
            "note": ("the sale rate is interpolated from a demand curve fitted "
                     "on the fold's training rows only; it is still an estimate "
                     "over an observed relationship, not a realised result"),
        }

    #: Below this many replayed listings the bootstrap is not informative.
    min_strategy_rows: int = 200
    #: Fixed, so the interval is reproducible across runs.
    bootstrap_seed: int = 20261022

    @staticmethod
    def _finite_pairs(out: "pd.DataFrame"):
        """The rows where both expected values are numbers.

        A NaN cannot be bootstrapped and must not be silently dropped by
        one statistic while another keeps it.
        """
        actual = pd.to_numeric(out["actual_expected"], errors="coerce").to_numpy(
            dtype=float)
        recommended = pd.to_numeric(out["recommended_expected"],
                                    errors="coerce").to_numpy(dtype=float)
        keep = np.isfinite(actual) & np.isfinite(recommended)
        return actual[keep], recommended[keep]

    @staticmethod
    def _bootstrap_ratio(out: "pd.DataFrame", seed: int,
                         draws: int = 1000) -> Optional[tuple]:
        """95% percentile interval on ``mean(recommended) / mean(actual)``."""
        actual, recommended = DfaLearningEngine._finite_pairs(out)
        if len(actual) < 20:
            return None
        rng = np.random.default_rng(seed)
        values = []
        for _ in range(draws):
            idx = rng.integers(0, len(actual), len(actual))
            base = actual[idx].mean()
            if base <= 0:
                continue
            values.append(recommended[idx].mean() / base)
        if len(values) < draws // 2:
            return None
        return (float(np.percentile(values, 2.5)),
                float(np.percentile(values, 97.5)))


def _best_measured_band(curve: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """The band with the highest measured expected gold, and its sample size.

    Descriptive, not prescriptive: it says which band collected the most
    in the measurement, not which ask is optimal.  The sample size travels
    with it so a reader can see how thin the evidence is.
    """
    if not curve or not curve.get("bands"):
        return None
    bands = [b for b in curve["bands"] if b.get("expected_gold_ratio") is not None]
    if not bands:
        return None
    best = max(bands, key=lambda b: float(b["expected_gold_ratio"]))
    return {"band": best.get("band"), "mid_ratio": best.get("mid_ratio"),
            "sale_rate": best.get("sale_rate"), "n": best.get("n"),
            "expected_gold_ratio": best.get("expected_gold_ratio")}


def _median_of(rows: List[Dict[str, object]], key: str) -> Optional[float]:
    values = [r[key] for r in rows if r.get(key) is not None]
    return float(np.median(values)) if values else None
