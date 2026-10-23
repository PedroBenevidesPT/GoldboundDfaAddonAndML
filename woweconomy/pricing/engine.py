"""The pricing engine: one explainable answer per (realm, item).

The engine is **per realm** because the market is per realm.  Two realms
of the same region are two auction houses with two different prices; and
realms of one *connected group* share a single auction house, so their
local reference is the same while their history -- the account's own
behaviour there -- is not.

The engine separates five things that are easy to confuse:

``price``
    what a unit will really fetch, net of the house cut.  It is the
    median of the model's distribution, and it is a *transaction*
    estimate: it is never trained against an asking price.

``sale probability``
    the chance of selling inside the horizon, with the sample size and
    the evidence behind it.

``time to sale``
    only when the outcome data supports it.  When it does not, the
    field is absent -- an absent field is not a zero.

``recommendation``
    the ask, and what it is worth.

``confidence``
    how much the answer is worth, from the sample behind it, the age of
    the evidence and the model's own error.

The engine never posts, cancels, undercuts or reprices anything.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..dbcompat import connect as db_connect
from ..dbcompat import missing_table_errors
from ..ml.config import MLConfig
from ..ml.features import FeatureBuilder
from ..ml.journalator import Posting
from ..ml.models import (
    PriceModel,
    SaleProbabilityModel,
    TimeToSaleModel,
    feature_columns,
    categorical_columns,
    make_pool,
)
from ..ml.strategy import ListingStrategy, StrategyConfig
from ..ml.tsm_history import TSMHistory
from .config import AH_CONSIGNMENT_RATE, COPPER_PER_GOLD, PricingConfig
from .interests import AppearanceInterest, InterestBook, load_interests
from .mapping import ItemMapping, build_item_mapping
from .shipped import REFERENCE_ORDER, ShippedModel

logger = logging.getLogger(__name__)

#: the warnings a recommendation can carry
FLAG_STALE = "stale"
FLAG_THIN_SAMPLE = "thin-sample"
FLAG_SPECULATIVE_REFERENCE = "speculative-reference"
FLAG_CONTRADICTORY = "contradictory-references"
FLAG_NO_LOCAL_REFERENCE = "no-local-reference"
FLAG_SHARED_APPEARANCE = "shared-appearance"
FLAG_TIME_UNSUPPORTED = "time-to-sale-unsupported"
FLAG_LOW_CONFIDENCE = "low-confidence"


def _number(value) -> Optional[int]:
    """A clean integer, or ``None``.

    The database carries zeroes and the data frames carry ``NaN``; both
    mean "there is no such number", and neither may reach the evidence.
    """
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass
class Estimate:
    """One answer, for one item in one realm."""

    realm: str
    item_id: int
    #: the estimated realizable price per unit, net of the house cut
    price: Optional[int] = None
    #: the calibrated interval around ``price``, and what it delivered
    price_low: Optional[int] = None
    price_high: Optional[int] = None
    price_interval_level: Optional[float] = None
    price_interval_coverage: Optional[float] = None
    price_interval_n: int = 0
    #: 0..1
    confidence: float = 0.0
    #: the chance of selling inside the horizon
    sale_probability: Optional[float] = None
    #: the expected time to sale, when the data supports one
    expected_time_hours: Optional[float] = None
    #: how many observations of this item in this realm the estimate rests on
    sample_size: int = 0
    #: when the newest observation happened
    observed_at: Optional[int] = None
    #: the numbers the answer was built from
    evidence: Dict[str, object] = field(default_factory=dict)
    #: the warnings
    flags: List[str] = field(default_factory=list)
    #: the recommended ask, when one could be computed
    recommended_ask: Optional[int] = None
    #: why the recommendation is what it is
    reasons: List[str] = field(default_factory=list)
    #: a human-readable reason the estimate is unavailable
    unavailable_reason: str = ""
    #: which price model produced the number: "realm" or "regional"
    model_scope: str = ""
    #: calibrated probability by horizon, keyed by hours
    sale_probabilities: Dict[int, float] = field(default_factory=dict)
    #: the Sentinel's expected realized sale price, distinct from the ask
    expected_sale_price: Optional[int] = None
    sale_price_low: Optional[int] = None
    sale_price_high: Optional[int] = None
    sale_price_interval_level: Optional[float] = None
    sale_price_interval_coverage: Optional[float] = None
    sale_price_interval_n: int = 0
    #: market-level risks, not claims about one of our auctions
    undercut_risk: Optional[float] = None
    price_decline_risk: Optional[float] = None
    #: where the Sentinel model was trained: realm, connected market or region
    training_scope: str = ""
    model_version: str = ""
    prediction_status: str = "unknown"
    fallback_reason: str = ""
    #: confidence in the *data*, separate from confidence in the prediction
    data_quality: float = 0.0
    #: the measured market around this item (supply, competition, price
    #: path), when a snapshot or a scan covers it.  Empty when nothing was
    #: observed: a missing market is unknown, not empty.
    market: Dict[str, object] = field(default_factory=dict)
    #: the same context as the object the decision layer reads.  It is not
    #: exported; the dict above is, so the dataset stays plain Lua.
    market_detail: Optional[object] = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        return self.price is not None and self.price > 0

    def as_record(self) -> Dict[str, object]:
        return {
            "realm": self.realm,
            "item_id": self.item_id,
            "price": self.price,
            "price_low": self.price_low,
            "price_high": self.price_high,
            "price_interval_level": self.price_interval_level,
            "price_interval_coverage": self.price_interval_coverage,
            "price_interval_n": self.price_interval_n,
            "confidence": round(float(self.confidence), 4),
            "sale_probability": (round(float(self.sale_probability), 4)
                                  if self.sale_probability is not None else None),
            "expected_time_hours": (round(float(self.expected_time_hours), 2)
                                     if self.expected_time_hours is not None else None),
            "sample_size": int(self.sample_size),
            "observed_at": self.observed_at,
            "evidence": self.evidence,
            "flags": list(self.flags),
            "recommended_ask": self.recommended_ask,
            "reasons": list(self.reasons),
            "unavailable_reason": self.unavailable_reason,
            "model_scope": self.model_scope,
            "sale_probabilities": {str(k): round(v, 4)
                                   for k, v in sorted(self.sale_probabilities.items())},
            "expected_sale_price": self.expected_sale_price,
            "sale_price_low": self.sale_price_low,
            "sale_price_high": self.sale_price_high,
            "sale_price_interval_level": self.sale_price_interval_level,
            "sale_price_interval_coverage": self.sale_price_interval_coverage,
            "sale_price_interval_n": self.sale_price_interval_n,
            "undercut_risk": self.undercut_risk,
            "price_decline_risk": self.price_decline_risk,
            "training_scope": self.training_scope,
            "model_version": self.model_version,
            "prediction_status": self.prediction_status,
            "fallback_reason": self.fallback_reason,
            "data_quality": round(float(self.data_quality), 3),
            "market": dict(self.market),
        }


class PricingEngine:
    """Estimates the realizable price of an item in a realm."""

    def __init__(self, config: Optional[PricingConfig] = None,
                 ml_config: Optional[MLConfig] = None) -> None:
        self.config = config or PricingConfig()
        self.ml_config = ml_config or MLConfig()
        self.history: Optional[TSMHistory] = None
        self.demand_book: Optional[InterestBook] = None
        self.item_mapping: Dict[int, ItemMapping] = {}
        self.price_model: Optional[PriceModel] = None
        self.probability_model: Optional[SaleProbabilityModel] = None
        self.time_model: Optional[TimeToSaleModel] = None
        self.feature_builder: Optional[FeatureBuilder] = None
        self.strategy: Optional[ListingStrategy] = None
        #: the model the game runs, fitted by :meth:`fit_shipped_model`
        self.shipped_model: Optional[ShippedModel] = None
        #: realm -> its own trained price model, when one exists
        self.realm_models: Dict[str, object] = {}
        self.shipped_evaluation: Dict[str, float] = {}
        #: the same model scored on windows it was not fitted on
        self.shipped_evaluation_out_of_sample: Dict[str, float] = {}
        #: the calibrated interval of the record price model, measured on
        #: a chronological holdout.  It is global: one width per model, not
        #: a claim that every item has the same error.
        self.price_interval: Dict[str, object] = {}
        #: the validated Sentinel prediction layer (sklearn + CatBoost)
        self.sentinel = None
        #: the learning engine's dataset, kept for the model fitting
        self.ml_frame = None
        #: True when the last fit came from the cache instead of a refit
        self._shipped_cache_hit = False
        #: the measured demand curve stored with that cached fit
        self._cached_curve: Optional[List[Dict[str, object]]] = None
        #: the appearance interest, for the evidence
        self.interests: Optional[InterestBook] = None
        #: realm -> item -> (count, last_seen, prices)
        self._history_cache: Dict[str, Dict[int, Tuple[int, int, List[int]]]] = {}
        #: realm -> the market context of the items asked about, built once
        #: per export.  The sources are read only when the key changes.
        self._market_cache: Dict[Tuple[str, Tuple[int, ...]], object] = {}

    def load_realm_models(self) -> Dict[str, object]:
        """Load the price model of every realm that has one.

        A realm model is fitted only on that realm's sales and was accepted
        only when it beat the item-median baseline on an untouched window;
        a realm without one keeps the regional model.  This is deliberately
        opt-in at export time: it changes the numbers the game gets.
        """
        from ..intel.realm_ml import RealmMLConfig, model_path
        from ..ml.models import PriceModel

        cfg = RealmMLConfig(models_dir=self.config.realm_models_dir,
                            region=self.config.realm_model_region)
        loaded: Dict[str, object] = {}
        for realm in sorted(self._history_cache):
            path = model_path(cfg, realm)
            if not path.exists():
                continue
            try:
                loaded[realm] = PriceModel.load(path)
            except Exception as exc:  # pragma: no cover - corrupt model
                logger.warning("the %s realm model could not be loaded: %s",
                               realm, exc)
        self.realm_models = loaded
        return loaded

    def _predict_price(self, realm: str, frame) -> tuple[float, str]:
        """The realm's own model when it has one, else the regional one."""
        model = self.realm_models.get(realm)
        if model is not None:
            try:
                return float(model.predict_net_unit(frame)[0]), "realm"
            except Exception as exc:
                logger.warning("the %s realm model could not predict: %s",
                               realm, exc)
        return float(self.price_model.predict_net_unit(frame)[0]), "regional"

    # -- loading ---------------------------------------------------------
    def load(self) -> "PricingEngine":
        """Load everything the engine needs.

        The models come from the learning engine; the references come from
        the TSM mirror; the interest comes from the DfA scan.
        """
        from ..ml.demand import load_demand, load_realm_groups
        from ..ml.models import load_sale_probability_model

        cfg = self.config
        ml = self.ml_config

        # The account's own sales are the only realms that can produce a
        # per-realm record, so they are the only realm scopes whose local
        # reference the estimate needs.  The region is always loaded, and
        # the realm map is read whole -- loading all 92 realms would spend
        # minutes and gigabytes on references no record can use.
        self._load_history()
        self.history = TSMHistory(cfg.tsm_db, scopes=self._traded_scopes() or None,
                                  availability_clock=ml.availability_clock)

        price_path = cfg.models_dir / "price_model.cbm"
        if price_path.exists():
            self.price_model = PriceModel.load(price_path)
        prob_path = cfg.models_dir / f"sale_probability_{cfg.horizon_h}h.cbm"
        if prob_path.exists():
            self.probability_model = load_sale_probability_model(prob_path)
        tts_path = cfg.models_dir / "time_to_sale.cbm"
        if tts_path.exists():
            from ..ml.models import load_time_to_sale_model
            self.time_model = load_time_to_sale_model(tts_path)

        # the DfA demand, for the features the models already know.  The
        # item list matters: the demand book carries the item -> appearance
        # bridge, and without it the demand features are missing and the
        # model cannot be fed the columns it was trained on.
        item_ids = sorted({item_id for bucket in self._history_cache.values()
                           for item_id in bucket})
        self.demand_book = load_demand(ml.dfa_db, cfg.tsm_db, item_ids)
        groups = load_realm_groups(ml.dfa_db)
        self.feature_builder = FeatureBuilder(self.history, self.demand_book, groups)

        if self.price_model is not None and self.probability_model is not None:
            self.strategy = ListingStrategy(
                self.price_model, self.probability_model, self.time_model,
                StrategyConfig(deadline_h=cfg.horizon_h))

        self.item_mapping, self.mapping_report = build_item_mapping(cfg.tsm_db)
        # The interest is read once, after the mapping exists.  It used to be
        # read twice: the first call passed the appearance ids of a mapping
        # that had not been built yet (so it was always empty), cost the same
        # full scan as the second, and was overwritten one line later.
        #
        # Overall demand only: the share of scanned accounts that owns each
        # appearance, which is what the evidence line shows.  The per-realm
        # coverage is a different, much more expensive quantity (a walk of
        # 13M rows for "in how many realms is this wanted") and no price
        # depends on it, so the engine does not ask for it.
        self.interests = load_interests(cfg.dfa_db, with_realm_coverage=False)

        # The validated Sentinel layer is optional: an installation that has
        # not trained it yet keeps the existing estimates unchanged.
        try:
            from ..ml.sentinel import SentinelConfig, SentinelPredictor

            sentinel_cfg = SentinelConfig(models_root=Path(cfg.models_dir).parent / "dfa_sentinel",
                                          tsm_db=Path(cfg.tsm_db))
            predictor = SentinelPredictor(sentinel_cfg)
            if predictor.load():
                self.sentinel = predictor
            else:
                logger.info("Sentinel predictions unavailable: %s", predictor.load_error)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("the Sentinel prediction layer could not be loaded: %s", exc)
        return self

    def _realm_market(self, realm: str):
        """The realm's market context for every item it has a transaction in."""
        items = sorted(self._history_cache.get(realm, {}))
        if not items:
            return None
        return self.market_for(realm, items)

    def _load_history(self) -> None:
        """The account's own transactions, per realm and item."""
        # db_connect, not sqlite3.connect: the container has no tsm_markets.db,
        # and sqlite3.connect would create an empty one and then fail on the
        # first query.  The tables are in the shared database there.
        con = db_connect(self.config.tsm_db)
        try:
            rows = con.execute(
                "SELECT realm, item_id, COUNT(*), MAX(timestamp_unix), "
                "AVG(unit_price_copper) FROM auction_events "
                "WHERE event_type='sale' AND item_id IS NOT NULL "
                "GROUP BY realm, item_id").fetchall()
        except missing_table_errors():
            rows = []
        finally:
            con.close()
        for realm, item_id, count, last, avg in rows:
            bucket = self._history_cache.setdefault(str(realm), {})
            bucket[int(item_id)] = (int(count), int(last or 0), [int(avg or 0)])

    def _traded_scopes(self) -> List[str]:
        """The canonical TSM scopes of the realms the account sold in."""
        if not self._history_cache:
            return []
        con = db_connect(self.config.tsm_db)
        try:
            canonical = {str(name): str(slug) for name, slug in con.execute(
                "SELECT realm_name, canonical_slug FROM tsm_realm_group")}
        except missing_table_errors():
            canonical = {}
        finally:
            con.close()
        scopes: List[str] = []
        for realm in self._history_cache:
            scope = canonical.get(str(realm), "")
            if scope and scope not in scopes:
                scopes.append(scope)
        return sorted(scopes)

    # -- helpers ---------------------------------------------------------
    def realm_scope(self, realm: str) -> str:
        """The TSM scope that holds this realm's auction house."""
        if self.history is None:
            return ""
        return self.history.canonical_scope(realm)

    def _references(self, realm: str) -> Dict[str, Optional[int]]:
        """The market references in force for this realm right now."""
        if self.history is None:
            return {}
        scope = self.realm_scope(realm)
        out: Dict[str, Optional[int]] = {}
        if scope:
            local = self.history.latest_state(scope, "realm_items")
            for item_id, row in local.items():
                out.setdefault("local", {})  # type: ignore[arg-type]
        return out

    def _local_row(self, realm: str, item_id: int) -> Optional[tuple]:
        if self.history is None:
            return None
        scope = self.realm_scope(realm)
        if not scope:
            return None
        return self.history.latest_state(scope, "realm_items").get(item_id)

    def _region_row(self, item_id: int) -> Optional[tuple]:
        if self.history is None:
            return None
        return self.history.latest_state("region", "region_items").get(item_id)

    def _item_history(self, realm: str, item_id: int) -> Tuple[int, int]:
        bucket = self._history_cache.get(realm, {})
        entry = bucket.get(item_id)
        if not entry:
            return 0, 0
        return entry[0], entry[1]

    # -- the shipped model -------------------------------------------------
    SHIPPED_CACHE_VERSION = 3

    def _content_version(self, path: Path) -> str:
        """What the fit actually depends on in a database, not its mtime.

        The mirror's sign is ``MAX(snapshot_id)`` plus the number of
        snapshots: a new import always moves it, and a sync that imports
        nothing leaves it alone.  Using the file's mtime instead meant a
        no-op sync -- TSM rewrites its AppData every few minutes with the
        same content -- invalidated the cache and paid the 13 s refit for
        no new data.

        The DfA scan database has no such counter and is rewritten while
        it runs, so there the size and mtime stay: the scan *is* the change.
        """
        if Path(path) == Path(self.config.tsm_db):
            try:
                # db_connect: the read-only file URI is a SQLite-only notion,
                # and in the container these tables are in Postgres.
                con = db_connect(path)
                try:
                    snapshots = con.execute(
                        "SELECT COUNT(*), MAX(snapshot_id) FROM tsm_snapshot").fetchone()
                    events = con.execute(
                        "SELECT COUNT(*), MAX(timestamp_unix) FROM auction_events").fetchone()
                finally:
                    con.close()
                return f"snap={snapshots[0]}:{snapshots[1]}|events={events[0]}:{events[1]}"
            except missing_table_errors():
                pass  # fall back to the file, never to "unchanged"
            except Exception:
                pass
        try:
            stat = Path(path).stat()
            return f"file={stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            return "missing"

    def _shipped_fingerprint(self) -> str:
        """Everything the fitted model depends on, hashed.

        Building the training frame and fitting on it is 13 s, and the
        export does it every run.  Reusing the fit is only safe when the
        inputs are identical, so the fingerprint covers the content version
        of both databases, the horizon, the learning configuration and the
        cache format version.
        """
        import hashlib

        digest = hashlib.sha256()
        digest.update(f"v{self.SHIPPED_CACHE_VERSION}|".encode())
        # the horizon picks the label the demand curve and the interval are
        # measured on, so it is part of what the cache holds
        digest.update(f"h{self.config.horizon_h}|".encode())
        digest.update(repr(sorted((k, repr(v)) for k, v in
                                  vars(self.ml_config).items())).encode())
        for path in (self.config.tsm_db, self.ml_config.dfa_db):
            digest.update(f"{path}|{self._content_version(Path(path))}|".encode())
        return digest.hexdigest()

    def _shipped_cache_path(self) -> Path:
        return Path(self.config.models_dir) / "shipped_fit.json"

    def _load_shipped_cache(self, fingerprint: str) -> Optional["ShippedModel"]:
        """The cached fit, when it belongs to exactly these inputs."""
        import json

        from .shipped import ShippedModel

        path = self._shipped_cache_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if payload.get("fingerprint") != fingerprint:
            return None
        model = payload.get("model") or {}
        if not model.get("coefficients"):
            return None
        try:
            restored = ShippedModel(**model)
        except TypeError:
            return None
        self.shipped_evaluation = payload.get("evaluation") or {}
        self.shipped_evaluation_out_of_sample = (
            payload.get("evaluation_out_of_sample") or {})
        # The interval is measured on the training frame, so it travels with
        # the fit: without this a cached fit would export nil price ranges
        # while a refit exported measured ones.
        self.price_interval = dict(payload.get("price_interval") or {})
        # The measured demand curve is computed from the same frame.  Without
        # it a cached fit saved 13 s on the fit and spent 18 s rebuilding the
        # frame to recompute the curve.
        self._cached_curve = payload.get("curve")
        self._shipped_cache_hit = True
        logger.info("shipped model: reusing the fit for these inputs (%s)",
                    fingerprint[:12])
        return restored

    def store_cached_curve(self, curve: List[Dict[str, object]]) -> None:
        """Keep the measured curve next to the fit it was measured with."""
        self._cached_curve = list(curve)
        if self.shipped_model is not None:
            self._save_shipped_cache(self._shipped_fingerprint())

    def _save_shipped_cache(self, fingerprint: str,
                            interval: Optional[Dict[str, object]] = None) -> None:
        import json
        from dataclasses import asdict

        if self.shipped_model is None:
            return
        path = self._shipped_cache_path()
        payload = {
            "version": self.SHIPPED_CACHE_VERSION,
            "fingerprint": fingerprint,
            "model": asdict(self.shipped_model),
            "evaluation": self.shipped_evaluation,
            "evaluation_out_of_sample": self.shipped_evaluation_out_of_sample,
            "price_interval": (interval if interval is not None
                               else dict(self.price_interval or {})),
            "curve": list(getattr(self, "_cached_curve", None) or []),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:  # a cache that cannot be written is not a failure
            logger.warning("could not write the shipped-model cache: %s", exc)

    def fit_shipped_model(self, force: bool = False) -> "ShippedModel":
        """Fit the linear model the game will run.

        The game cannot run the ensemble, and it does not have the
        features the ensemble uses.  What it has is the family of market
        references, so the engine distils the ensemble into a linear
        model over their logarithms -- and then measures what the
        distillation costs.

        The fit is cached against a fingerprint of the training inputs, so
        an export with unchanged data does not rebuild a 13-second frame
        to arrive at the same three coefficients.  A cache miss, a changed
        input or ``force=True`` refits; a failed refit raises rather than
        falling back to an older model.
        """
        from ..ml import DfaLearningEngine
        from .shipped import (evaluate_shipped_model,
                              evaluate_shipped_model_walk_forward,
                              fit_shipped_model)

        fingerprint = self._shipped_fingerprint()
        if not force:
            cached = self._load_shipped_cache(fingerprint)
            if cached is not None:
                self.shipped_model = cached
                return self.shipped_model

        # A refit is a different fit: the curve measured on the old frame
        # must not survive it.
        self._shipped_cache_hit = False
        self._cached_curve = None
        ml_engine = DfaLearningEngine(self.ml_config)
        ml_engine.load()
        frame, _report, _link = ml_engine.build_rows()
        self.ml_frame = frame
        self.shipped_model = fit_shipped_model(frame)
        # in sample: what the model does with every row available
        self.shipped_evaluation = evaluate_shipped_model(self.shipped_model, frame)
        # out of sample: the number that may be quoted, because it is
        # measured on windows the model was not fitted on
        self.shipped_evaluation_out_of_sample = \
            evaluate_shipped_model_walk_forward(frame)
        # The measured hit rate travels with the model: it is the confidence
        # the game states for an item the dataset has no record of.  It has
        # to be the out-of-sample number -- the in-sample one is optimistic
        # by construction, and a confidence that flatters itself is worse
        # than a low one that is true.
        out_of_sample = self.shipped_evaluation_out_of_sample
        source = out_of_sample if out_of_sample.get("n") else self.shipped_evaluation
        self.shipped_model.within_25 = float(source.get("within_25pct") or 0.0)
        self.shipped_model.within_50 = float(source.get("within_50pct") or 0.0)
        # The interval travels with the model, so the game can show a range
        # for an item it has never seen.  Its coverage is the measured
        # out-of-sample one, never the nominal level dressed up as a result.
        if out_of_sample.get("interval_half_width_log"):
            self.shipped_model.interval_level = float(
                out_of_sample.get("interval_level") or 0.8)
            self.shipped_model.interval_half_width = float(
                out_of_sample["interval_half_width_log"])
            coverage = out_of_sample.get("interval_coverage")
            self.shipped_model.interval_coverage = (
                float(coverage) if coverage is not None else 0.0)
            self.shipped_model.interval_n = int(
                out_of_sample.get("interval_test_n") or 0)
            self.shipped_model.interval_method = str(
                out_of_sample.get("interval_method") or "split-conformal")
        self._save_shipped_cache(fingerprint)
        return self.shipped_model

    def calibrate_price_interval(self, level: float = 0.8) -> Dict[str, object]:
        """Measure the record price model's interval on a real holdout.

        The record price is a CatBoost model, so its uncertainty cannot be
        borrowed from the shipped linear model.  This fits the same model
        family on the development window, calibrates the width on the last
        part of that window and measures the coverage on the untouched test
        window.  The width is then applied to the *final* model, which was
        fitted on more data; the interval is therefore conservative, not
        optimistic.  That is the right direction for a warning.
        """
        from ..ml.features import ASK_FEATURES
        from ..ml.intervals import calibrate_interval
        from ..ml.models import (
            categorical_columns,
            feature_columns,
            train_price_model,
        )
        from ..ml.validate import make_split

        # A cached fit did not build the frame, but it carries the interval
        # that was measured on it: same frame, same measurement.
        if self.price_interval and getattr(self, "_shipped_cache_hit", False):
            return dict(self.price_interval)
        frame = self.ml_frame
        if frame is None or frame.empty or "date" not in frame.columns:
            return {}
        split = make_split(frame)
        if split.test.empty:
            return {}
        test_days = {int(day) for day in split.test["date"].unique()}
        dev_days = sorted({int(day) for day in frame["date"].unique()} - test_days)
        if len(dev_days) < 3:
            return {}
        calibration_days = max(1, len(dev_days) // 4)
        cal_days = set(dev_days[-calibration_days:])
        fit_days = set(dev_days[:-calibration_days])
        if not fit_days:
            return {}
        train = frame[frame["date"].isin(fit_days)
                      & frame["y_net_unit_log"].notna()]
        calibration = frame[frame["date"].isin(cal_days)
                            & frame["y_net_unit_log"].notna()]
        test = split.test[split.test["y_net_unit_log"].notna()]
        if len(train) < 40 or len(calibration) < 20 or len(test) < 10:
            return {}
        cats = categorical_columns(frame)
        feats = [c for c in feature_columns(frame) if c not in ASK_FEATURES]
        model = train_price_model(
            train, None, feats, cats, early_stopping=False,
            iterations=self.ml_config.price_iterations)
        if not model.trained:
            return {}
        calibration_residual = (
            model.predict_log(calibration)
            - calibration["y_net_unit_log"].to_numpy(dtype=float))
        test_residual = (
            model.predict_log(test)
            - test["y_net_unit_log"].to_numpy(dtype=float))
        interval = calibrate_interval(calibration_residual, test_residual,
                                      level=level)
        if interval is None:
            return {}
        out = interval.as_dict(prefix="")
        out.update({"fit_rows": len(train),
                    "calibration_rows": len(calibration),
                    "test_rows": len(test)})
        self.price_interval = out
        # Keep it next to the fit so the next export does not need the frame.
        if self.shipped_model is not None:
            self._save_shipped_cache(self._shipped_fingerprint(), interval=out)
        return out

    def estimate_with_shipped_model(self, realm: str, item_id: int) -> Optional[int]:
        """The estimate the game would compute for an item it has never seen.

        Only the live references are used, so the answer follows the
        market rather than a frozen table.
        """
        if self.shipped_model is None or not self.shipped_model.coefficients:
            return None
        references = self.live_references(realm, item_id)
        return self.shipped_model.predict(references)

    def region_price_table(self) -> Dict[int, int]:
        """The shipped model's price for every item the region knows about.

        The regional references are the same for every realm, so this is
        one table, not one per realm: the dataset ships it so that the
        addon can answer for an item even when TSM is not installed, and
        the panel can list the whole market instead of only the items that
        happened to sell.  An item whose references are all missing gets
        no entry -- the model refuses to invent a price.
        """
        if self.shipped_model is None or not self.shipped_model.coefficients:
            return {}
        if self.history is None:
            return {}
        rows = self.history.latest_state("region", "region_items")
        out: Dict[int, int] = {}
        for item_id, row in rows.items():
            references = {
                "dbregionmarketavg": _number(row[0]),
                "dbregionhistorical": _number(row[3]) if len(row) > 3 else None,
                "dbregionsaleavg": _number(row[4]) if len(row) > 4 else None,
            }
            price = self.shipped_model.predict(references)
            if price is None or price <= 0:
                continue
            out[int(item_id)] = int(round(price))
        return out

    def live_references(self, realm: str, item_id: int) -> Dict[str, Optional[float]]:
        """The market references in force for this item in this realm."""
        references: Dict[str, Optional[float]] = {name: None for name in
                                                 REFERENCE_ORDER}
        local = self._local_row(realm, item_id)
        region = self._region_row(item_id)
        if local:
            references["dbmarket"] = _number(local[0])
            references["dbminbuyout"] = _number(local[1])
            references["dbrecent"] = _number(local[2])
            references["dbhistorical"] = _number(local[3])
        if region:
            references["dbregionmarketavg"] = _number(region[0])
            references["dbregionhistorical"] = _number(region[3])
            references["dbregionsaleavg"] = _number(region[4])
        return references

    # -- the market around the item ---------------------------------------
    def market_for(self, realm: str, item_ids: Sequence[int]):
        """The market context for these items in this realm, built once.

        Supply comes from the Blizzard snapshots in ``intel.sqlite``; the
        price path from the TSM mirror; competition and our own stock from
        the undercut scans.  A source that is missing or unreadable leaves
        that part unknown -- it never becomes a zero, and the caller can see
        what answered in ``coverage``.
        """
        from ..ml.market_context import build_realm_market

        ids = tuple(sorted({int(i) for i in item_ids if i}))
        if not ids:
            return None
        key = (str(realm), ids)
        if key in self._market_cache:
            return self._market_cache[key]
        intel = getattr(self.config, "intel_db", None)
        try:
            market = build_realm_market(
                self.config.tsm_db, intel, realm, ids,
                now=int(datetime.now(timezone.utc).timestamp()))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("the market context for %s is unavailable: %s",
                           realm, exc)
            market = None
        self._market_cache[key] = market
        return market

    # -- the estimate ----------------------------------------------------
    def estimate(self, realm: str, item_id: int,
                 qty: int = 1) -> Estimate:
        """Estimate what a unit of the item fetches in the realm."""
        result = Estimate(realm=realm, item_id=item_id)
        if self.price_model is None or self.feature_builder is None:
            result.unavailable_reason = "the price model is not loaded"
            return result

        local = self._local_row(realm, item_id)
        region = self._region_row(item_id)
        if local is None and region is None:
            result.unavailable_reason = ("no market reference exists for this "
                                         "item in this realm")
            result.flags.append(FLAG_NO_LOCAL_REFERENCE)
            return result

        sample_size, observed_at = self._item_history(realm, item_id)
        result.sample_size = sample_size
        result.observed_at = observed_at or None

        # a posting-shaped row, with the ask left at zero: the price model
        # does not use the ask, and the recommendation supplies one later
        posting = Posting(time=int(datetime.now(timezone.utc).timestamp()),
                           realm=realm, character="", faction="",
                           item_name="", item_id=item_id, qty=qty,
                           unit_buyout=0, unit_bid=0, deposit=0)
        try:
            row = self.feature_builder.build(posting, posting.time)
        except Exception as exc:  # pragma: no cover - defensive
            result.unavailable_reason = f"the features could not be built: {exc}"
            return result

        base = dict(row.features)
        base.update({f"cat_{k}": v for k, v in row.categorical.items()})

        # --- the price -------------------------------------------------
        try:
            frame = pd.DataFrame([base])
            price, scope = self._predict_price(realm, frame)
            result.model_scope = scope
        except Exception as exc:
            logger.warning("the price model could not predict: %s", exc)
            result.unavailable_reason = "the price model could not produce a value"
            return result
        if not math.isfinite(price) or price <= 0:
            result.unavailable_reason = "the price model produced no usable value"
            return result
        result.price = int(round(price))

        # --- the sale probability ---------------------------------------
        if self.probability_model is not None and self.probability_model.trained:
            try:
                result.sale_probability = float(self.probability_model.predict(
                    pd.DataFrame([base]))[0])
            except Exception as exc:
                logger.warning("the probability model could not predict: %s", exc)

        # --- the time to sale -------------------------------------------
        # only when the survival model has a signal: the learning engine
        # measured a concordance index near chance, and a number the data
        # cannot support is worse than no number
        if self.time_model is not None and self.time_model.trained:
            if self._time_is_supported():
                try:
                    result.expected_time_hours = float(
                        self.time_model.predict_median_hours(pd.DataFrame([base]))[0])
                except Exception:
                    result.expected_time_hours = None
            else:
                result.flags.append(FLAG_TIME_UNSUPPORTED)

        # --- the confidence ----------------------------------------------
        result.confidence = self._confidence(sample_size, observed_at, base)

        # --- the validated Sentinel layer --------------------------------
        if self.sentinel is not None:
            try:
                from ..ml.sentinel import risk_features_from_rows

                age_h = 0.0
                if self.history is not None:
                    scope = self.realm_scope(realm)
                    snap_ts = self.history.snapshot_time_at(scope, "realm_items", posting.time)
                    age_h = ((posting.time - snap_ts) / 3600.0) if snap_ts else 0.0
                risk = risk_features_from_rows(local, region, age_h)
                sentinel = self.sentinel.predict(
                    base, realm=realm, item_id=item_id,
                    horizon=self.config.horizon_h, sample_size=sample_size,
                    risk_features=risk)
                result.prediction_status = sentinel.status
                result.fallback_reason = sentinel.fallback_reason
                result.model_version = sentinel.model_version
                result.training_scope = sentinel.training_scope
                result.data_quality = sentinel.data_quality
                result.expected_sale_price = sentinel.price
                result.sale_price_low = sentinel.price_low
                result.sale_price_high = sentinel.price_high
                result.sale_price_interval_level = sentinel.price_interval_level
                result.sale_price_interval_coverage = sentinel.price_interval_coverage
                result.sale_price_interval_n = sentinel.price_interval_n
                result.undercut_risk = sentinel.undercut_risk
                result.price_decline_risk = sentinel.price_decline_risk
                if sentinel.probability is not None:
                    result.sale_probabilities[self.config.horizon_h] = sentinel.probability
                    result.sale_probability = sentinel.probability
                for key in (24, 48, 168):
                    if key == self.config.horizon_h:
                        continue
                    p = self._sentinel_probability(base, realm, item_id, key, sample_size)
                    if p is not None:
                        result.sale_probabilities[key] = p
                # Independent horizon classifiers can cross: a 24h model
                # may say 0.61 while the 48h model says 0.57.  A sale by
                # 24h is a subset of a sale by 48h, so the exported curve
                # is made monotone by a cumulative maximum.  This is a
                # projection, not a new prediction; the raw per-horizon
                # metrics remain in the Sentinel report.
                from ..ml.sentinel import monotone_probabilities

                result.sale_probabilities = monotone_probabilities(
                    result.sale_probabilities)
                result.sale_probability = result.sale_probabilities.get(
                    self.config.horizon_h, result.sale_probability)
                if sentinel.time_to_sale_hours is not None:
                    result.expected_time_hours = sentinel.time_to_sale_hours
                for flag in sentinel.flags:
                    if flag not in result.flags:
                        result.flags.append(flag)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("the Sentinel prediction failed: %s", exc)
                result.prediction_status = "unknown"
                result.fallback_reason = f"the Sentinel prediction failed: {exc}"

        # --- the market around the item -------------------------------------
        # A snapshot or a scan covering this item is evidence the models do
        # not see: how much stock is listed, how much of it is ours, which
        # way the price has been moving and how noisy it is.  It is attached
        # to the estimate, never folded into the model's prediction.
        try:
            realm_market = self._realm_market(realm)
            item_market = realm_market.get(item_id) if realm_market else None
            if item_market is not None and item_market.coverage != "none":
                result.market = item_market.as_dict()
                result.market_detail = item_market
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("no market context for %s/%s: %s", realm, item_id, exc)

        # --- the evidence -------------------------------------------------
        result.evidence = self._evidence(realm, item_id, local, region, base)

        # --- the recommendation --------------------------------------------
        if self.strategy is not None:
            try:
                rec = self.strategy.recommend(base, qty=qty)
                result.recommended_ask = rec.get("recommended_ask")
                result.reasons = self._reasons(result, rec)
            except Exception as exc:
                logger.warning("the strategy could not advise: %s", exc)

        self._flag(result, base)
        return result

    def _sentinel_probability(self, base: Dict[str, object], realm: str,
                              item_id: int, horizon: int,
                              sample_size: int) -> Optional[float]:
        """One extra horizon from the active Sentinel artifact, if any."""
        if self.sentinel is None:
            return None
        try:
            p = self.sentinel.predict(base, realm=realm, item_id=item_id,
                                      horizon=horizon, sample_size=sample_size)
            return p.probability
        except Exception:  # pragma: no cover - defensive
            return None

    def _time_is_supported(self) -> bool:
        """Whether the survival model is good enough to quote a duration."""
        report = self.config.models_dir.parent.parent / "reports" / "dfa_ml" / "dfa_ml_report.json"
        try:
            import json
            data = json.loads(report.read_text(encoding="utf-8"))
            c_index = (data.get("validation", {})
                       .get("time_to_sale", {})
                       .get("c_index_mean"))
            return bool(c_index and float(c_index) >= 0.6)
        except Exception:
            return False

    def _confidence(self, sample_size: int, observed_at: Optional[int],
                    base: Dict[str, object]) -> float:
        """How much the answer is worth.

        Three things move it: how many observations of this item in this
        realm stand behind it, how old they are, and how much the market
        references disagree with each other (a market that contradicts
        itself is not evidence of anything).
        """
        cfg = self.config
        confidence = cfg.base_confidence
        if sample_size > 0:
            # more observations move the answer towards the model's own
            # typical accuracy
            weight = sample_size / (sample_size + cfg.confidence_reference_n)
            confidence = max(confidence, weight)
        if observed_at:
            age_h = (datetime.now(timezone.utc).timestamp() - observed_at) / 3600.0
            if age_h > cfg.max_age_hours:
                confidence *= 0.4
            elif age_h > cfg.max_age_hours / 3:
                confidence *= 0.75
        # a market whose references disagree is not a confident market
        dispersion = base.get("ref_dispersion")
        if isinstance(dispersion, (int, float)) and dispersion > 1.0:
            confidence *= 0.7
        return float(max(0.0, min(1.0, confidence)))

    def _evidence(self, realm: str, item_id: int, local, region,
                  base: Dict[str, object]) -> Dict[str, object]:
        """The numbers the answer was built from."""
        evidence: Dict[str, object] = {
            "realm": realm,
            "auctionHouse": self.realm_scope(realm),
        }
        if local:
            evidence["dbmarket"] = _number(local[0])
            evidence["dbminbuyout"] = _number(local[1])
            evidence["dbrecent"] = _number(local[2])
            evidence["dbhistorical"] = _number(local[3])
        if region:
            evidence["dbregionmarketavg"] = _number(region[0])
            evidence["dbregionhistorical"] = _number(region[3])
            evidence["dbregionsaleavg"] = _number(region[4])
            evidence["dbregionsoldperday"] = _number(region[6])
            evidence["dbregionsalerate"] = _number(region[5])
        # the appearance interest, with its own status
        mapping = self.item_mapping.get(item_id)
        if mapping is not None:
            evidence["appearanceStatus"] = mapping.status
            if mapping.appearance_ids and self.interests is not None:
                interest = self.interests.union(mapping.appearance_ids)
                if interest is not None:
                    evidence["dfaMissing"] = interest.missing or None
                    evidence["dfaSample"] = interest.sample or None
                    evidence["dfaRealmCoverage"] = interest.realm_coverage or None
                    evidence["dfaAppearanceName"] = interest.name or None
        return evidence

    def _reasons(self, result: Estimate, rec: Dict[str, object]) -> List[str]:
        """Why the recommendation is what it is, in words."""
        reasons: List[str] = []
        predicted = rec.get("predicted_unit_price")
        if predicted:
            reasons.append(
                f"the price model expects a unit to fetch about "
                f"{int(round(predicted))} copper net of the house cut")
        probability = rec.get("sale_probability")
        if probability is not None:
            reasons.append(
                f"at that ask the chance of selling inside "
                f"{self.config.horizon_h}h is about {probability * 100:.0f}%")
        source = rec.get("probability_source")
        if source:
            reasons.append(f"the sale probability comes from {source}")
        if result.sample_size:
            reasons.append(
                f"the account has sold this item here {result.sample_size} time(s)")
        else:
            reasons.append(
                "the account has no sale of this item in this realm yet")
        return reasons

    def _flag(self, result: Estimate, base: Dict[str, object]) -> None:
        """The warnings the answer deserves."""
        cfg = self.config
        if result.sample_size < cfg.min_sample_size:
            result.flags.append(FLAG_THIN_SAMPLE)
        if result.observed_at:
            age_h = (datetime.now(timezone.utc).timestamp() - result.observed_at) / 3600.0
            if age_h > cfg.max_age_hours:
                result.flags.append(FLAG_STALE)
        # a market whose asking price has run away from its own history
        ratio = base.get("region_market_over_historical")
        if isinstance(ratio, (int, float)) and ratio > 3.0:
            result.flags.append(FLAG_SPECULATIVE_REFERENCE)
        spread = base.get("ref_dispersion")
        if isinstance(spread, (int, float)) and spread > 2.0:
            result.flags.append(FLAG_CONTRADICTORY)
        status = result.evidence.get("appearanceStatus")
        if status == "shared":
            result.flags.append(FLAG_SHARED_APPEARANCE)
        if result.confidence < 0.35:
            result.flags.append(FLAG_LOW_CONFIDENCE)
