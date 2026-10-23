"""The generated dataset: a compact, versioned, game-readable file.

The dataset is what the in-game side reads.  It is deliberately small and
deliberately boring:

* **per realm**, because the market is per realm;
* **keyed by item id**, because that is what the game speaks;
* **aggregated**, because a per-character export would be both large and
  a privacy problem -- the account names, the characters and the guild
  never leave the machine that scanned them;
* **versioned**, because a reader that does not understand the version
  must refuse the file rather than misread it;
* **deterministic**, because a dataset that changes when nothing changed
  cannot be trusted or diffed.

The output is a plain Lua table.  It is loaded by a data addon, not read
from disk by TSM: the addon cannot reach the file system, and the
dataset is a *build artefact* that is shipped, not a live feed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import DATASET_VERSION, ENGINE_VERSION, PricingConfig
from .engine import Estimate, PricingEngine
from .shipped import REFERENCE_ORDER


def vendor_prices(tsm_db: Path | str, item_ids: Sequence[int]) -> Dict[int, int]:
    """Vendor sell price per item, from the Blizzard item table.

    Missing table or a locked database is not fatal: the deposit is then
    zero, which understates the cost rather than inventing one, and the
    record says so by carrying ``vendor = 0``.  This is the same lookup the
    dataset uses, so a loot valuation and the shipped tooltip cannot
    disagree about what a vendor pays.
    """
    import sqlite3

    out: Dict[int, int] = {}
    wanted = [int(i) for i in item_ids if i]
    if not wanted:
        return out
    try:
        con = sqlite3.connect(str(tsm_db), timeout=10.0)
    except sqlite3.Error:
        return out
    try:
        for start in range(0, len(wanted), 900):
            chunk = wanted[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for item_id, sell in con.execute(
                    f"SELECT itemId, sellPrice FROM blizzard_items "
                    f"WHERE itemId IN ({marks})", chunk):
                out[int(item_id)] = int(sell or 0)
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out

logger = logging.getLogger(__name__)

_IDENTIFIER = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _lua_scalar(value: object) -> str:
    """One Lua value, written so the reader gets the same number back."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return "nil"
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", " ")
        return f'"{escaped}"'
    return "nil"


def _lua_table(value: object) -> str:
    """A nested Lua table with sorted keys, so the export is diffable."""
    if isinstance(value, dict):
        parts = []
        for key in sorted(value, key=lambda k: str(k)):
            name = str(key)
            label = (name if name and set(name) <= _IDENTIFIER
                     else f"[{_lua_scalar(name)}]")
            parts.append(f"{label} = {_lua_table(value[key])}")
        return "{ " + ", ".join(parts) + " }"
    if isinstance(value, (list, tuple)):
        return "{ " + ", ".join(_lua_table(item) for item in value) + " }"
    return _lua_scalar(value)

#: the global the data addon publishes
GLOBAL_NAME = "DFA_ESTIMATE"

#: the schema version the in-game loader understands
READER_VERSION = DATASET_VERSION

#: a decimal number in the generated Lua, not part of a longer token.  Used
#: by ``content_signature`` to compare floats at a useful precision.
_FLOAT = re.compile(r"(?<![\w.])(\d+\.\d+)")


@dataclass
class DatasetRecord:
    """One (realm, item) record."""

    realm: str
    item_id: int
    estimate: Estimate
    #: what a vendor pays for the item, from the Blizzard item table.  The
    #: deposit is a share of *this*, so the tooltip and the net price both
    #: need it, and the addon cannot read it reliably from GetItemInfo.
    vendor_price: int = 0
    #: expected net proceeds for one unit: the realised price less the 5 %
    #: cut and the deposit it will not get back because the listing may not
    #: sell.  This is the number a loot tracker should show, not the ask.
    net_price: int = 0
    #: what to do with the item, and the money it is expected to make
    #: (see :func:`woweconomy.ml.posting.decide`).  Empty when the item has
    #: no evidence behind it.
    action: Dict[str, object] = field(default_factory=dict)

    def to_lua(self) -> List[str]:
        """The Lua table body, one field per line, in a fixed order."""
        e = self.estimate
        parts: List[str] = []
        parts.append(f"price = {e.price or 0}")
        parts.append(f"conf = {round(float(e.confidence), 3)}")
        parts.append(f"psell = {round(float(e.sale_probability), 4) if e.sale_probability is not None else 'nil'}")
        parts.append(f"tts = {round(float(e.expected_time_hours), 2) if e.expected_time_hours is not None else 'nil'}")
        parts.append(f"n = {int(e.sample_size)}")
        parts.append(f"seen = {int(e.observed_at or 0)}")
        parts.append(f"flags = {{{', '.join(chr(34) + f + chr(34) for f in sorted(e.flags))}}}")
        # --- the validated Sentinel fields (schema 2) --------------------
        # Missing values are ``nil``.  A consumer can tell "no probability"
        # from "probability zero", which is the whole point of the schema.
        if e.sale_probabilities:
            entries = ", ".join(f"[{int(k)}] = {round(float(v), 4)}"
                                for k, v in sorted(e.sale_probabilities.items()))
            parts.append(f"psellByHorizon = {{ {entries} }}")
        else:
            parts.append("psellByHorizon = nil")
        parts.append(f"plow = {int(e.price_low) if e.price_low else 'nil'}")
        parts.append(f"phigh = {int(e.price_high) if e.price_high else 'nil'}")
        parts.append(f"plevel = {round(float(e.price_interval_level), 4) if e.price_interval_level else 'nil'}")
        parts.append(f"pcov = {round(float(e.price_interval_coverage), 4) if e.price_interval_coverage is not None else 'nil'}")
        parts.append(f"pintn = {int(e.price_interval_n)}")
        parts.append(f"ereal = {int(e.expected_sale_price) if e.expected_sale_price else 'nil'}")
        parts.append(f"elow = {int(e.sale_price_low) if e.sale_price_low else 'nil'}")
        parts.append(f"ehigh = {int(e.sale_price_high) if e.sale_price_high else 'nil'}")
        parts.append(f"elevel = {round(float(e.sale_price_interval_level), 4) if e.sale_price_interval_level else 'nil'}")
        parts.append(f"ecov = {round(float(e.sale_price_interval_coverage), 4) if e.sale_price_interval_coverage is not None else 'nil'}")
        parts.append(f"eintn = {int(e.sale_price_interval_n)}")
        parts.append(f"undercut = {round(float(e.undercut_risk), 4) if e.undercut_risk is not None else 'nil'}")
        parts.append(f"decline = {round(float(e.price_decline_risk), 4) if e.price_decline_risk is not None else 'nil'}")
        parts.append(f"scope = {chr(34)}{e.training_scope or ''}{chr(34)}")
        parts.append(f"model = {chr(34)}{e.model_version or ''}{chr(34)}")
        parts.append(f"status = {chr(34)}{e.prediction_status or 'unknown'}{chr(34)}")
        parts.append(f"fallback = {chr(34)}{e.fallback_reason or ''}{chr(34)}")
        parts.append(f"dq = {round(float(e.data_quality), 3)}")
        # The economics the decision rests on, shipped so the client does not
        # have to re-derive them (and so it cannot disagree with the model).
        parts.append(f"vendor = {int(self.vendor_price)}")
        parts.append(f"net = {int(self.net_price)}")
        if self.estimate.market:
            parts.append(f"market = {_lua_table(self.estimate.market)}")
        if self.action:
            parts.append(f"action = {_lua_table(self.action)}")
        return parts


@dataclass
class DatasetBuilder:
    """Builds the dataset from the engine."""

    config: PricingConfig
    engine: PricingEngine
    records: List[DatasetRecord] = field(default_factory=list)
    realms: Dict[str, Dict[str, object]] = field(default_factory=dict)
    #: realm -> canonical auction-house scope, for every realm TSM knows,
    #: not only the ones with a per-realm record
    realm_groups: Dict[str, str] = field(default_factory=dict)
    appearances: Dict[int, Dict[str, object]] = field(default_factory=dict)
    #: the measured sale rate as a function of the ask / sale average
    curve: List[Dict[str, float]] = field(default_factory=list)
    #: realm -> {factor, undercutShare, babysit24h, ...}.  It is what
    #: gives every known realm a price, not only the traded ones.
    realm_index: Dict[str, Dict[str, object]] = field(default_factory=dict)
    #: item id -> vendor sell price, from the Blizzard item table
    vendor_values: Dict[int, int] = field(default_factory=dict)
    #: item id -> price less the 5 % cut, for the whole regional table
    net_prices: Dict[int, int] = field(default_factory=dict)
    #: item id -> price, for every item the region has references for.
    #: One table, not one per realm: the model's inputs are regional.
    region_prices: Dict[int, int] = field(default_factory=dict)
    #: (realm, item id) -> the record that carries the economics and the
    #: action.  The ``realms`` block holds the estimates; this holds the
    #: enriched records, so the numbers computed here are the ones shipped.
    record_index: Dict[Tuple[str, int], "DatasetRecord"] = field(default_factory=dict)
    #: realm -> the market context the realm block summarises
    realm_index_market: Dict[str, object] = field(default_factory=dict)
    generated_at: int = 0
    notes: List[str] = field(default_factory=list)

    def build(self, realms: Optional[Sequence[str]] = None,
              min_sample: int = 1) -> "DatasetBuilder":
        """Build the records for the given realms."""
        cfg = self.config
        self.generated_at = int(datetime.now(timezone.utc).timestamp())
        self._build_curve()
        target_realms = list(realms) if realms else sorted(self.engine._history_cache)
        for realm in target_realms:
            items = self.engine._history_cache.get(realm, {})
            if not items:
                self.notes.append(f"realm '{realm}' has no transactions")
                continue
            realm_block: Dict[str, object] = {
                "auctionHouse": self.engine.realm_scope(realm),
                "items": {},
            }
            total = 0
            newest = 0
            for item_id in sorted(items):
                estimate = self.engine.estimate(realm, item_id)
                if not estimate.available:
                    continue
                if estimate.sample_size < min_sample:
                    continue
                self._attach_interval(estimate)
                record = DatasetRecord(realm, item_id, estimate)
                self.records.append(record)
                self.record_index[(realm, item_id)] = record
                realm_block["items"][item_id] = estimate  # type: ignore[assignment]
                total += estimate.sample_size
                if estimate.observed_at:
                    newest = max(newest, estimate.observed_at)
            if realm_block["items"]:
                realm_block["sampleSize"] = total
                realm_block["updatedAt"] = newest
                # The realm's market line: what was observed around these
                # items, with its coverage and freshness.  A realm with no
                # snapshot says so instead of showing a zero.
                market = self.engine._realm_market(realm)
                if market is not None:
                    realm_block["market"] = market.summary()
                    self.realm_index_market[realm] = market
                self.realms[realm] = realm_block
            else:
                self.notes.append(f"realm '{realm}' produced no usable estimate")
        self._attach_economics()
        self._collect_appearances()
        # The realm -> auction-house map is not limited to the realms with a
        # per-realm record: a realm the account never traded in still has an
        # auction house, and the addon has to be able to name it.
        history = getattr(self.engine, "history", None)
        if history is not None and hasattr(history, "realm_groups"):
            self.realm_groups = dict(history.realm_groups())
        # Every item the region has references for, priced by the shipped
        # model.  This is what makes the addon cover the market instead of
        # the handful of items that happened to be traded.
        self.region_prices = self.engine.region_price_table()
        # The economics for the whole regional table, so the addon can show a
        # vendor value and a net price for any item it prices -- not only the
        # few the account happened to trade.
        from ..ml.posting import AH_CUT_RATE

        self.vendor_values = self._vendor_prices(sorted(self.region_prices))
        self.net_prices = {int(item_id): int(round(float(price) * (1.0 - AH_CUT_RATE)))
                           for item_id, price in self.region_prices.items()
                           if price and float(price) > 0}
        return self

    def _vendor_prices(self, item_ids: List[int]) -> Dict[int, int]:
        """Vendor sell price per item, from the Blizzard item table."""
        return vendor_prices(self.config.tsm_db, item_ids)

    def _attach_economics(self) -> None:
        """Vendor value and expected net proceeds on every record.

        Net = realised price x (1 - 5 %) - deposit x (1 - P(sale)).  The
        deposit is a share of the vendor value, refunded only on a sale, so
        it belongs in the number a loot tracker shows -- the ask does not.
        """
        from ..ml.posting import sale_net

        from ..ml.posting import decide as posting_decide

        vendors = self._vendor_prices(sorted({r.item_id for r in self.records}))
        hours = int(getattr(self.config, "horizon_h", 24) or 24)
        if hours not in (12, 24, 48):
            hours = 24
        for record in self.records:
            vendor = int(vendors.get(record.item_id, 0) or 0)
            record.vendor_price = vendor
            estimate = record.estimate
            price = int(estimate.expected_sale_price or estimate.price or 0)
            probability = float(estimate.sale_probability or 0.0)
            record.net_price = (sale_net(price, vendor, probability, hours=hours)
                                if price > 0 else 0)
            # The action, with the market evidence when a snapshot or a
            # scan covers the item.  The economics are the same ones the
            # net above uses: 5 % cut, vendor-based deposit, refunded only
            # when the listing sells.
            market = getattr(estimate, "market_detail", None)
            best_ask = None
            cheapest = estimate.market.get("cheapest_ask")
            if isinstance(cheapest, (int, float)) and cheapest > 0:
                best_ask = int(cheapest)
            try:
                edge = posting_decide(
                    reference=price or int(estimate.price or 0),
                    vendor_sell_copper=vendor,
                    sale_probability=probability,
                    hours=hours,
                    qty=1,
                    best_ask=best_ask,
                    confidence=float(estimate.confidence),
                    realm=record.realm,
                    market=market)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("no action for %s/%s: %s",
                               record.realm, record.item_id, exc)
                continue
            record.action = edge.as_dict()

    def _attach_interval(self, estimate: Estimate) -> None:
        """Put the model's measured interval on this record's price."""
        interval = getattr(self.engine, "price_interval", None) or {}
        width = interval.get("half_width_log")
        if not estimate.price or not width:
            return
        try:
            half = float(width)
        except (TypeError, ValueError):
            return
        if not math.isfinite(half) or half <= 0:
            return
        estimate.price_low = max(1, int(round(estimate.price * math.exp(-half))))
        estimate.price_high = max(
            estimate.price_low,
            int(round(estimate.price * math.exp(half))))
        estimate.price_interval_level = float(interval.get("level") or 0.8)
        coverage = interval.get("coverage")
        estimate.price_interval_coverage = (
            float(coverage) if coverage is not None else None)
        estimate.price_interval_n = int(interval.get("test_n") or 0)

    def _build_curve(self) -> None:
        """The measured demand curve, which the game uses to price the ask.

        The curve is *measured*, not modelled: it is the sale rate in each
        band of ``ask / regional sale average``.  The game needs it
        because the sale probability of an ask is the one thing the
        linear model cannot say.
        """
        from ..ml import DfaLearningEngine

        # The curve is measured on the same training frame as the shipped fit,
        # so it is stored with that fit.  Rebuilding the frame here cost 18 s
        # on every export that reused a cached fit.
        cached = getattr(self.engine, "_cached_curve", None)
        if cached and getattr(self.engine, "_shipped_cache_hit", False):
            self.curve = [dict(point) for point in cached]
            return

        if self.engine.ml_frame is None:
            ml_engine = DfaLearningEngine(self.engine.ml_config)
            ml_engine.load()
            frame, _r, _l = ml_engine.build_rows()
            self.engine.ml_frame = frame
        frame = self.engine.ml_frame
        if frame is None or frame.empty:
            return
        label = f"sold_within_{self.config.horizon_h}h"
        if label not in frame.columns or "ref_region_sale_avg" not in frame.columns:
            return
        usable = frame[(frame[f"label_known_{self.config.horizon_h}h"] == 1)
                       & frame["ref_region_sale_avg"].notna()
                       & (frame["ref_region_sale_avg"] > 0)].copy()
        if usable.empty:
            return
        usable["ratio"] = usable["ask_unit"] / usable["ref_region_sale_avg"]
        edges = [0.0, 0.5, 0.7, 0.85, 1.0, 1.15, 1.4, 2.0, 3.0, 1e9]
        usable["band"] = pd.cut(usable["ratio"], edges, include_lowest=True)
        for band, group in usable.groupby("band", observed=True):
            if len(group) < 5:
                continue
            lo = float(band.left) if np.isfinite(band.left) else 0.0
            hi = float(band.right) if np.isfinite(band.right) else 0.0
            mid = (lo + hi) / 2.0 if hi < 1e8 else lo * 1.5
            self.curve.append({
                "ratio": round(mid, 4),
                "p": round(float(group[label].mean()), 4),
                "n": int(len(group)),
            })
        if self.curve and hasattr(self.engine, "store_cached_curve"):
            self.engine.store_cached_curve(self.curve)

    def _collect_appearances(self) -> None:
        """The appearance interest of the items in the dataset."""
        book = self.engine.interests
        if book is None or not book.available:
            self.notes.append("no DfA interest data")
            return
        wanted = set()
        for record in self.records:
            mapping = self.engine.item_mapping.get(record.item_id)
            if mapping:
                wanted.update(mapping.appearance_ids)
        for appearance_id in sorted(wanted):
            interest = book.get(appearance_id)
            if interest is None:
                continue
            entry = {
                "missing": interest.missing,
                "sample": interest.sample,
                "ownedPct": (round(interest.owned_pct, 4)
                             if interest.owned_pct is not None else None),
            }
            # "in how many realms is this wanted" is a separate measurement
            # and the engine does not take it (it walks 13M rows).  When it
            # was not read, the field is omitted: writing 0 would claim the
            # appearance is wanted nowhere, which is a different fact.
            if book.realm_coverage_read:
                entry["realms"] = interest.realm_coverage
            self.appearances[appearance_id] = entry

    # -- the export --------------------------------------------------------
    def metadata(self) -> Dict[str, object]:
        sample_sizes = [r.estimate.sample_size for r in self.records]
        confidences = [r.estimate.confidence for r in self.records]
        return {
            "version": DATASET_VERSION,
            "engineVersion": ENGINE_VERSION,
            "generatedAt": self.generated_at,
            "realmCount": len(self.realms),
            "itemCount": len(self.records),
            "appearanceCount": len(self.appearances),
            "priceCount": len(self.region_prices),
            "totalObservations": int(sum(sample_sizes)),
            "minSampleSize": int(min(sample_sizes)) if sample_sizes else 0,
            "maxSampleSize": int(max(sample_sizes)) if sample_sizes else 0,
            "meanConfidence": (round(sum(confidences) / len(confidences), 4)
                               if confidences else 0.0),
            "horizonHours": self.config.horizon_h,
            # the engine's own staleness limit, shipped so the reader can
            # mark an old price without inventing a threshold of its own
            "maxAgeHours": self.config.max_age_hours,
            "indexedRealms": len(self.realm_index),
            "measuredRealms": sum(1 for entry in self.realm_index.values()
                                  if entry.get("source") == "realm"),
            "sentinelModel": (self.records[0].estimate.model_version
                              if self.records else ""),
            "predictionStatusCounts": self._status_counts(),
        }

    def _status_counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for record in self.records:
            status = record.estimate.prediction_status or "unknown"
            out[status] = out.get(status, 0) + 1
        return out

    def to_lua(self) -> str:
        """The generated Lua file.

        The order is fixed -- realms sorted, items sorted, appearances
        sorted -- so that the same inputs always produce the same bytes.
        """
        meta = self.metadata()
        lines: List[str] = []
        lines.append("-- Generated by the DfA pricing engine.  Do not edit by hand.")
        lines.append(f"-- schema {meta['version']}, engine {meta['engineVersion']}, "
                     f"generated {meta['generatedAt']}")
        lines.append("-- Regenerate with: python scripts/dfa_pricing_export.py")
        lines.append("")
        lines.append(f"{GLOBAL_NAME} = {{")
        lines.append(f"\tversion = {meta['version']},")
        lines.append(f"\tengineVersion = {chr(34)}{meta['engineVersion']}{chr(34)},")
        lines.append(f"\tgeneratedAt = {meta['generatedAt']},")
        lines.append(f"\thorizonHours = {meta['horizonHours']},")
        lines.append(f"\tmaxAgeHours = {meta['maxAgeHours']},")
        lines.append(f"\titemCount = {meta['itemCount']},")
        lines.append(f"\trealmCount = {meta['realmCount']},")
        lines.append(f"\tappearanceCount = {meta['appearanceCount']},")
        lines.append(f"\tpriceCount = {meta['priceCount']},")
        lines.append(f"\tindexedRealms = {meta['indexedRealms']},")
        lines.append(f"\tmeasuredRealms = {meta['measuredRealms']},")
        lines.append(f"\ttotalObservations = {meta['totalObservations']},")
        lines.append(f"\tmeanConfidence = {meta['meanConfidence']},")
        lines.append(f"\tsentinelModel = {chr(34)}{meta['sentinelModel']}{chr(34)},")
        lines.append("\tpredictionStatus = {")
        for status, count in sorted(meta["predictionStatusCounts"].items()):
            lines.append(f"\t\t[{chr(34)}{status}{chr(34)}] = {int(count)},")
        lines.append("\t},")
        lines.append("")

        # --- the model the game runs -----------------------------------
        lines.append("\t-- the model the game runs: a linear model on the")
        lines.append("\t-- logarithms of the market references it can read")
        lines.append("\tmodel = {")
        shipped = self.engine.shipped_model
        if shipped is not None and shipped.coefficients:
            for field in shipped.as_lua():
                lines.append(f"\t\t{field},")
        else:
            lines.append("\t\tintercept = 0,")
            lines.append("\t\tcoefficients = { },")
            lines.append("\t\tmedians = { },")
            lines.append("\t\tresidualSigma = 0,")
            lines.append("\t\tintervalLevel = 0.8,")
            lines.append("\t\tintervalHalfWidth = 0,")
            lines.append("\t\tintervalCoverage = 0,")
            lines.append("\t\tintervalN = 0,")
            lines.append("\t\tnTrain = 0,")
            lines.append("\t\tnComplete = 0,")
        lines.append("\t},")
        lines.append("")

        # --- the model's own table, for the whole region ----------------
        # Packed, several items per line: 31 000 records at one line each
        # would spend 31 000 bytes on indentation alone.
        lines.append("\t-- the model's price for every item the region knows, so the addon")
        lines.append("\t-- answers even when TSM is not installed")
        lines.append("\tprices = {")
        ids = sorted(self.region_prices)
        for start in range(0, len(ids), 6):
            chunk = ids[start:start + 6]
            lines.append("\t\t" + " ".join(
                f"[{item_id}]={self.region_prices[item_id]}," for item_id in chunk))
        lines.append("\t},")
        lines.append("")

        # --- vendor value and net proceeds, for the whole regional table --
        # Loot value is not the ask.  `nets` is the price less the 5 % cut;
        # the per-record `net` goes further and subtracts the expected
        # deposit, which needs the sale probability the regional table does
        # not carry.
        lines.append("\t-- what a vendor pays, from the Blizzard item table")
        lines.append("\tvendors = {")
        vendor_ids = sorted(self.vendor_values)
        for start in range(0, len(vendor_ids), 6):
            chunk = vendor_ids[start:start + 6]
            lines.append("\t\t" + " ".join(
                f"[{item_id}]={self.vendor_values[item_id]}," for item_id in chunk))
        lines.append("\t},")
        lines.append("\t-- the price after the 5 % cut: the money a sale pays")
        lines.append("\tnets = {")
        net_ids = sorted(self.net_prices)
        for start in range(0, len(net_ids), 6):
            chunk = net_ids[start:start + 6]
            lines.append("\t\t" + " ".join(
                f"[{item_id}]={self.net_prices[item_id]}," for item_id in chunk))
        lines.append("\t},")
        lines.append("")

        # --- the measured demand curve ---------------------------------
        lines.append("\t-- the measured sale rate by ask / regional sale average")
        lines.append("\tcurve = {")
        for point in self.curve:
            lines.append(f"\t\t{{ ratio = {point['ratio']}, p = {point['p']}, n = {point['n']} }},")
        lines.append("\t},")
        lines.append("")

        # --- the realms -------------------------------------------------
        lines.append("\t-- the auction house each realm trades in (the connected group)")
        lines.append("\trealmGroups = {")
        groups = dict(self.realm_groups)
        for realm in self.realms:
            groups.setdefault(realm, str(self.realms[realm].get("auctionHouse") or ""))
        for realm in sorted(groups):
            scope = groups[realm] or ""
            lines.append(f"\t\t[{chr(34)}{realm}{chr(34)}] = {chr(34)}{scope}{chr(34)},")
        lines.append("\t},")
        lines.append("")
        lines.append("\t-- one line per known realm: price factor against the region and")
        lines.append("\t-- the measured undercut pressure.  regionalPrice x factor is the")
        lines.append("\t-- realm's estimate where no own record exists")
        lines.append("\trealmIndex = {")
        for realm in sorted(self.realm_index):
            entry = self.realm_index[realm]
            fields = [f"factor = {float(entry.get('factor') or 1.0):.4f}",
                      f"sample = {int(entry.get('sample') or 0)}",
                      f"source = {chr(34)}{entry.get('source') or 'regional'}{chr(34)}"]
            for key in ("medianRatio", "undercutShare", "undercutWatched",
                        "undercutGapPct", "itemsAhead", "babysit24h",
                        "babysitSamples"):
                value = entry.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    fields.append(f"{key} = {round(float(value), 4)}")
            lines.append(f"\t\t[{chr(34)}{realm}{chr(34)}] = {{ {', '.join(fields)} }},")
        lines.append("\t},")
        lines.append("")
        lines.append("\t-- one block per realm, one entry per item the account traded")
        lines.append("\trealms = {")
        for realm in sorted(self.realms):
            block = self.realms[realm]
            lines.append(f"\t\t[{chr(34)}{realm}{chr(34)}] = {{")
            lines.append(f"\t\t\tgroup = {chr(34)}{block.get('auctionHouse') or ''}{chr(34)},")
            lines.append(f"\t\t\tupdatedAt = {int(block.get('updatedAt') or 0)},")
            lines.append(f"\t\t\tsampleSize = {int(block.get('sampleSize') or 0)},")
            market_summary = block.get("market")
            if isinstance(market_summary, dict) and market_summary:
                lines.append(f"\t\t\tmarket = {_lua_table(market_summary)},")
            lines.append("\t\t\titems = {")
            items = block.get("items", {})  # type: ignore[assignment]
            for item_id in sorted(items):
                estimate = items[item_id]
                # The record built in ``build`` carries the vendor value, the
                # net price and the action; rebuilding it here threw those
                # away, which is how "vendor = 0" once shipped for every item.
                record = self.record_index.get((realm, item_id))
                if record is None:
                    record = DatasetRecord(realm, item_id, estimate)
                fields = record.to_lua()
                lines.append(f"\t\t\t\t[{item_id}] = {{ {', '.join(fields)} }},")
            lines.append("\t\t\t},")
            lines.append("\t\t},")
        lines.append("\t},")
        lines.append("")
        lines.append("\t-- the Data for Azeroth interest of the appearances in play")
        lines.append("\tappearances = {")
        for appearance_id in sorted(self.appearances):
            entry = self.appearances[appearance_id]
            fields = [
                f"missing = {int(entry.get('missing') or 0)}",
                f"sample = {int(entry.get('sample') or 0)}",
                f"realms = {int(entry.get('realms') or 0)}",
                # the scan stores the owned share as a percentage
                f"ownedPct = {entry.get('ownedPct') if entry.get('ownedPct') is not None else 'nil'}",
            ]
            lines.append(f"\t\t[{appearance_id}] = {{ {', '.join(fields)} }},")
        lines.append("\t},")
        lines.append("}")
        lines.append("")
        return "\n".join(lines)

    def write(self, path: Optional[Path] = None) -> Path:
        """Write the dataset and return the path.

        The write is atomic (a temporary file, then ``os.replace``): the
        daemon deploys the previous copy while this one lands, and a
        reader must never see half a Lua table.  The bytes are exactly
        ``to_lua()``, so :meth:`checksum` describes the file on disk.
        """
        path = Path(path or self.config.dataset_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_lua()
        from .deploy import write_atomic  # local: deploy reads this module too
        write_atomic(path, text.encode("utf-8"))
        return path

    def checksum(self) -> str:
        """The SHA-256 of the generated file, for the report."""
        return hashlib.sha256(self.to_lua().encode("utf-8")).hexdigest()

    def content_signature(self) -> str:
        """A hash of what the dataset *says*, at the precision that matters.

        See :func:`dataset_content_signature`, which does the work.  Two
        things have to come out before "did anything change" is a question
        with a useful answer:

        * ``generatedAt``.  :meth:`checksum` covers the whole file, so two
          exports of identical data a minute apart hash differently.  That
          is right for "is this the same file" and wrong for the question
          the new-dataset alert asks.

        * the fourth decimal of the probabilities and factors.  The
          features include how long ago the market was measured, so those
          numbers move continuously by design -- measured on two exports a
          minute apart, ``psell`` drifted from 0.1619 to 0.1618 and nothing
          else in the file moved.  An alert per export would then be a
          heartbeat, and the pipeline exports every six hours.
        """
        return dataset_content_signature(self.to_lua())


def build_dataset(engine: PricingEngine,
                  config: Optional[PricingConfig] = None,
                  realms: Optional[Sequence[str]] = None) -> Tuple[DatasetBuilder, List[str]]:
    """Build the dataset from a loaded engine."""
    config = config or engine.config
    builder = DatasetBuilder(config=config, engine=engine)
    builder.build(realms=realms)
    return builder, builder.notes


def dataset_content_signature(text: str) -> str:
    """A hash of a generated dataset's *content*, from its text.

    Used both by the builder and by anything that only has the shipped
    file -- ``run_pipeline.dataset_fingerprint`` -- so that "did the
    dataset change" has exactly one definition in the project.

    The build timestamp comes out, because it says when the file was made
    and not what it contains.  Floats are compared at two decimals (1%),
    because the features include how long ago the market was measured, so
    probabilities drift in the fourth decimal between two exports a minute
    apart: measured, ``psell`` went 0.1619 -> 0.1618 with nothing else in
    1.37 MB changed.  Prices are integers here and are unaffected, so a
    move worth telling someone about still counts.
    """
    lines = [
        line for line in str(text).splitlines()
        if not line.startswith("-- schema ")
        and not line.strip().startswith("generatedAt =")
    ]
    normalised = _FLOAT.sub(lambda m: f"{float(m.group(1)):.2f}", "\n".join(lines))
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def validate_dataset(text: str) -> Dict[str, object]:
    """Check a generated dataset without loading Lua.

    The checks are the ones a reader would want: the version is known,
    the counts agree with the contents, and no record carries a price
    that is not a positive number of copper.
    """
    problems: List[str] = []
    meta: Dict[str, object] = {}
    for key in ("version", "engineVersion", "generatedAt", "itemCount",
                "realmCount", "appearanceCount", "totalObservations",
                "priceCount", "indexedRealms"):
        marker = f"\t{key} = "
        for line in text.splitlines():
            if line.startswith(marker):
                value = line[len(marker):].rstrip(",")
                if value.isdigit():
                    meta[key] = int(value)
                elif value.startswith('"'):
                    meta[key] = value.strip('"')
                else:
                    meta[key] = value
                break
        else:
            problems.append(f"the metadata field '{key}' is missing")

    version = meta.get("version")
    if version is not None and int(version) != READER_VERSION:
        problems.append(f"the dataset version {version} is not the one this "
                        f"reader understands ({READER_VERSION})")

    prices = 0
    bad_prices = 0
    for line in text.splitlines():
        stripped = line.strip()
        if "price = " not in stripped:
            continue
        prices += 1
        try:
            value = int(stripped.split("price = ")[1].split(",")[0].strip())
        except (ValueError, IndexError):
            bad_prices += 1
            continue
        if value <= 0:
            bad_prices += 1
    if bad_prices:
        problems.append(f"{bad_prices} of {prices} records carry a price that "
                        "is not a positive number of copper")
    meta["records"] = prices
    meta["problems"] = problems
    meta["valid"] = not problems
    return meta
