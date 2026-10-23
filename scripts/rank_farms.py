#!/usr/bin/env python
"""Rank farms with the DfA estimate: trained dataset prices, EU realms.

What this adds to ``woweconomy.analytics.farm_ranking``
-------------------------------------------------------
The farm report ranks a zone with the **asking** prices of the TSM
market.  This script ranks the same zone with the **estimate**: what the
trained dataset says the item fetches when it sells, and the chance that
it sells at all.

Two layers, both trained:

``sentinel``
    the validated Sentinel artifact (``models/dfa_sentinel``): a CatBoost
    ensemble over the learning engine's dataset.  It answers with the
    expected realized sale price (net of the house cut), a calibrated
    interval around it, the probability of selling inside the horizon and
    the market risks (undercut, price decline).

``shipped``
    the linear model the game runs, as the exported dataset carries it
    (``DFA_Estimate/Data/DFA_Estimate_Data.lua``): a linear model on the
    logarithms of the regional market references, plus the price it
    produced for every item the region knows.  It needs no per-realm
    data, so it prices an item the Sentinel has never seen.

Every number carries its provenance: which layer priced it, which model
version, the prediction status, the interval and the data quality.  An
item the estimate cannot price is reported as **unpriced** and left out
of the EV; it is never zero-filled.

The EV
------
    EV_GROSS = rate x qty x price
    EV_NET   = EV_GROSS x P(sells inside the horizon)

``price`` is net of the auction house cut (both layers are trained on the
realized net unit price), so it is directly comparable with the farm
report's ``avgSalePrice x 0.95``.

Realms and region
-----------------
The account trades on EU realms and the mirror holds EU data, so the
script reads EU: the regional references are the EU region's and the
per-realm references come from the realm's own EU auction house.  The
realms default to the one the account sells in the most; ``--realms``
picks them explicitly and ``--all-realms`` ranks every EU realm the
account trades in.  A realm that is not in the EU index is refused with
the reason -- the script does not silently price a US realm with EU
references.

This is the per-realm mode of the one farm command, reached as
``python scripts/farm_ranking.py realms ...``.  It keeps its own module
because it batches the estimate over thousands of items instead of calling
``PricingEngine.estimate`` per item, but it is not a second entry point: the
zone report, the leaderboards and this view all price from the same trained
dataset, so the same item is worth one amount, not three.

    python scripts/farm_ranking.py realms
    python scripts/farm_ranking.py realms --realms "Silvermoon,Twisting Nether"
    python scripts/farm_ranking.py realms --all-realms --top 15
    python scripts/farm_ranking.py realms --out reports/farms_estimate.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from woweconomy.analytics.farm_ranking import NpcDrop, load_zone_drops  # noqa: E402
from woweconomy.ml.journalator import Posting  # noqa: E402
from woweconomy.ml.sentinel import risk_features_from_rows  # noqa: E402
from woweconomy.pricing.config import PricingConfig  # noqa: E402
from woweconomy.pricing.shipped import ShippedModel  # noqa: E402

COPPER_PER_GOLD = 10_000
DEFAULT_REALMS_INDEX = ROOT / "reports" / "realms_eu.json"
DEFAULT_ZONE_DIR = ROOT / "woweconomy" / "wowhead_zones"
DEFAULT_DATASET = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"

#: a number in the dataset's Lua
_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


# ----------------------------------------------------------------------
# The dataset: the model the game runs
# ----------------------------------------------------------------------

@dataclass
class DatasetModel:
    """What the exported dataset carries.

    ``model`` is the linear model the addon runs; ``prices`` is that
    model's price for every item the region knows -- the *trained
    dataset's* price, frozen at the moment the dataset was generated.
    """

    model: ShippedModel
    prices: Dict[int, int] = field(default_factory=dict)
    generated_at: str = ""
    generated_ts: int = 0
    sentinel_model: str = ""
    horizon_hours: int = 0
    item_count: int = 0
    price_count: int = 0
    source: str = ""


def _lua_block(text: str, name: str) -> str:
    """The body of the ``name = { ... }`` table in a Lua file."""
    match = re.search(rf"\b{re.escape(name)}\s*=\s*\{{", text)
    if not match:
        return ""
    depth = 1
    index = match.end()
    while index < len(text) and depth:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return text[match.end():index - 1] if depth == 0 else text[match.end():]


def _lua_number(text: str, name: str, default: float = 0.0) -> float:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*({_NUM})", text)
    return float(match.group(1)) if match else default


def _lua_int(text: str, name: str, default: int = 0) -> int:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*({_NUM})", text)
    return int(float(match.group(1))) if match else default


def _lua_string(text: str, name: str, default: str = "") -> str:
    match = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', text)
    return match.group(1) if match else default


def load_dataset(path: Path | str = DEFAULT_DATASET) -> DatasetModel:
    """Read the shipped model and its price table out of the dataset.

    The dataset is the artefact the game loads, so the numbers here are
    the numbers the game quotes -- not a refit.
    """
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"the dataset is not here: {path}")
    text = path.read_text(encoding="utf-8")

    body = _lua_block(text, "model")
    if not body:
        raise SystemExit(f"the dataset has no model block: {path}")
    coefficients = [float(x) for x in re.findall(_NUM, _lua_block(body, "coefficients"))]
    medians = [float(x) for x in re.findall(_NUM, _lua_block(body, "medians"))]
    references = re.findall(r'"([^"]+)"', _lua_block(body, "references"))
    if not coefficients:
        raise SystemExit(f"the dataset's model has no coefficients: {path}")

    model = ShippedModel(
        intercept=_lua_number(body, "intercept"),
        coefficients=coefficients,
        medians=medians,
        references=references,
    )
    model.residual_sigma = _lua_number(body, "residualSigma")
    model.within_25 = _lua_number(body, "within25")
    model.within_50 = _lua_number(body, "within50")
    model.interval_level = _lua_number(body, "intervalLevel", 0.8)
    model.interval_half_width = _lua_number(body, "intervalHalfWidth")
    model.interval_coverage = _lua_number(body, "intervalCoverage")
    model.interval_n = _lua_int(body, "intervalN")
    model.interval_method = _lua_string(body, "intervalMethod")
    model.n_train = _lua_int(body, "nTrain")
    model.n_complete = _lua_int(body, "nComplete")

    prices = {int(i): int(p) for i, p in
              re.findall(r"\[(\d+)\]\s*=\s*(\d+)", _lua_block(text, "prices"))}

    generated_ts = _lua_int(text, "generatedAt")
    return DatasetModel(
        model=model,
        prices=prices,
        generated_at=(time.strftime("%Y-%m-%d %H:%M", time.gmtime(generated_ts))
                      if generated_ts else ""),
        generated_ts=generated_ts,
        sentinel_model=_lua_string(text, "sentinelModel"),
        horizon_hours=_lua_int(text, "horizonHours"),
        item_count=_lua_int(text, "itemCount"),
        price_count=_lua_int(text, "priceCount"),
        source=str(path),
    )


# ----------------------------------------------------------------------
# The EU realms
# ----------------------------------------------------------------------

@dataclass
class EuIndex:
    """The EU realm index, and the realms the account trades in."""

    region: str = "eu"
    name_to_slug: Dict[str, str] = field(default_factory=dict)
    account_realms: List[Tuple[str, int]] = field(default_factory=list)
    source: str = ""


def load_eu_index(index_path: Path | str = DEFAULT_REALMS_INDEX,
                  tsm_db: Path | str | None = None) -> EuIndex:
    """The EU realm index and the realms the account sold in.

    A realm that is not in the index is not an EU realm, and the script
    refuses it rather than pricing it with EU references.
    """
    index_path = Path(index_path)
    if not index_path.exists():
        raise SystemExit(f"the EU realm index is not here: {index_path}")
    data = json.loads(index_path.read_text(encoding="utf-8"))
    index = EuIndex(region=str(data.get("region") or "eu"), source=str(index_path))
    for realm in data.get("realms") or []:
        name = str(realm.get("name") or realm.get("slug") or "")
        slug = str(realm.get("slug") or "")
        if name and slug:
            index.name_to_slug[name] = slug

    if tsm_db is not None and Path(tsm_db).exists():
        con = sqlite3.connect(f"file:{Path(tsm_db).as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT realm, COUNT(*) FROM auction_events "
                "WHERE event_type='sale' GROUP BY realm ORDER BY COUNT(*) DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.close()
        index.account_realms = [(str(realm), int(count)) for realm, count in rows]
    return index


def resolve_realms(requested: Sequence[str], index: EuIndex) -> List[str]:
    """The EU realms to rank, in the order they were asked for.

    A realm the account never sold in is still accepted -- it may be a
    new one -- but it has to be an EU realm.
    """
    lookup = {name.lower(): name for name in index.name_to_slug}
    out: List[str] = []
    unknown: List[str] = []
    for realm in requested:
        canonical = lookup.get(str(realm).strip().lower())
        if canonical is None:
            unknown.append(str(realm))
            continue
        if canonical not in out:
            out.append(canonical)
    if unknown:
        raise SystemExit(
            "not EU realms (the index is " + index.source + "): "
            + ", ".join(unknown)
            + " -- this installation reads EU data only")
    if not out:
        raise SystemExit("no realm to rank")
    return out


def default_realms(index: EuIndex, all_realms: bool) -> List[str]:
    """The realms to rank when none were asked for."""
    if not index.account_realms:
        raise SystemExit("the journal has no realm the account sold in")
    names = [realm for realm, _ in index.account_realms]
    if all_realms:
        return resolve_realms(names, index)
    return resolve_realms(names[:1], index)


# ----------------------------------------------------------------------
# The estimate
# ----------------------------------------------------------------------

@dataclass
class DropValue:
    """What the estimate says about one item, with its provenance."""

    item_id: int
    name: str = ""
    #: the price used in the EV, net of the house cut (copper)
    price: Optional[int] = None
    #: which layer priced it: "sentinel", "shipped", "record" or "none"
    price_source: str = "none"
    price_low: Optional[int] = None
    price_high: Optional[int] = None
    interval_level: Optional[float] = None
    interval_coverage: Optional[float] = None
    sale_probability: Optional[float] = None
    #: which layer said it would sell: "sentinel" or "region-sale-rate"
    liquidity_source: str = "none"
    #: the other layers, for the cross-check
    shipped_price: Optional[int] = None
    record_price: Optional[int] = None
    model_scope: str = ""
    training_scope: str = ""
    model_version: str = ""
    status: str = ""
    reason: str = ""
    confidence: float = 0.0
    data_quality: float = 0.0
    flags: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "item_id": self.item_id, "name": self.name,
            "price": self.price, "price_source": self.price_source,
            "price_low": self.price_low, "price_high": self.price_high,
            "interval_level": self.interval_level,
            "interval_coverage": self.interval_coverage,
            "sale_probability": self.sale_probability,
            "liquidity_source": self.liquidity_source,
            "shipped_price": self.shipped_price,
            "record_price": self.record_price,
            "model_scope": self.model_scope, "training_scope": self.training_scope,
            "model_version": self.model_version, "status": self.status,
            "reason": self.reason, "confidence": round(self.confidence, 4),
            "data_quality": round(self.data_quality, 3), "flags": list(self.flags),
        }


@dataclass
class Farm:
    """One NPC, ranked."""

    npc_id: int
    npc_name: str
    zone: int
    zone_name: str
    realm: str = ""
    spawns: int = 0
    #: the kills per minute the EV/hour is scaled with
    kpm: float = 20.0
    gross: float = 0.0     # EV without the sale odds
    net: float = 0.0       # EV with the sale odds
    net_low: float = 0.0   # the summed price band
    net_high: float = 0.0
    priced: int = 0
    unpriced: int = 0
    drops: List[Tuple[NpcDrop, DropValue]] = field(default_factory=list)

    @property
    def ev_per_hour(self) -> float:
        return self.net * 60.0 * self.kpm

    def as_dict(self) -> Dict[str, object]:
        return {
            "npc_id": self.npc_id, "npc_name": self.npc_name,
            "zone": self.zone, "zone_name": self.zone_name, "realm": self.realm,
            "spawns": self.spawns,
            "ev_per_kill_gross": round(self.gross),
            "ev_per_kill_net": round(self.net),
            "ev_per_kill_net_low": round(self.net_low),
            "ev_per_kill_net_high": round(self.net_high),
            "ev_per_hour_net": round(self.net * 60.0 * self.kpm),
            "priced_drops": self.priced, "unpriced_drops": self.unpriced,
            "drops": [
                {
                    "item_id": drop.item_id, "item_name": drop.item_name,
                    "drop_rate": drop.drop_rate, "qty": drop.qty,
                    "price": value.price, "price_source": value.price_source,
                    "price_low": value.price_low, "price_high": value.price_high,
                    "sale_probability": value.sale_probability,
                    "liquidity_source": value.liquidity_source,
                    "shipped_price": value.shipped_price,
                    "record_price": value.record_price,
                    "status": value.status, "reason": value.reason,
                    "model_version": value.model_version,
                    "training_scope": value.training_scope,
                    "flags": list(value.flags),
                    "ev_gross": round(drop.drop_rate * drop.qty * (value.price or 0)),
                    "ev_net": round(drop.drop_rate * drop.qty * (value.price or 0)
                                    * (value.sale_probability or 0.0)),
                }
                for drop, value in self.drops
            ],
        }


def load_region_sale_rates(tsm_db: Path | str) -> Dict[int, float]:
    """The region's sell-through, the fallback when the Sentinel has no answer."""
    con = sqlite3.connect(f"file:{Path(tsm_db).as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT key_id, sale_rate FROM tsm_region_price").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return {int(item_id): float(rate or 0.0) for item_id, rate in rows}


def load_sale_history(tsm_db: Path | str) -> Dict[Tuple[str, int], int]:
    """How many times the account sold each item, per realm.

    That is the sample the estimate's confidence rests on.
    """
    con = sqlite3.connect(f"file:{Path(tsm_db).as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT realm, item_id, COUNT(*) FROM auction_events "
            "WHERE event_type='sale' AND item_id IS NOT NULL "
            "GROUP BY realm, item_id").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return {(str(realm), int(item_id)): int(count) for realm, item_id, count in rows}


def _base_features(engine, realm: str, item_id: int, qty: int,
                   now: int) -> Dict[str, object]:
    """The feature row the models are fed, built exactly as the engine builds it."""
    posting = Posting(time=now, realm=realm, character="", faction="",
                      item_name="", item_id=item_id, qty=qty,
                      unit_buyout=0, unit_bid=0, deposit=0)
    row = engine.feature_builder.build(posting, now)
    base = dict(row.features)
    base.update({f"cat_{k}": v for k, v in row.categorical.items()})
    return base


def estimate_items(engine, realm: str, item_ids: Sequence[int],
                   dataset: DatasetModel, sale_rates: Dict[int, float],
                   history: Dict[Tuple[str, int], int],
                   horizon: int) -> Dict[int, DropValue]:
    """Ask the estimate about every item, once, and keep the provenance.

    ``PricingEngine.estimate`` also builds a recommendation and predicts
    three extra sale horizons.  The farm EV needs the price, the sale
    probability and the interval, so the script asks for those alone --
    several times faster over thousands of items.  ``--verify`` checks
    the two paths against each other.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    scope = engine.realm_scope(realm)
    local_state = engine.history.latest_state(scope, "realm_items") if scope else {}
    region_state = engine.history.latest_state("region", "region_items")
    snap_ts = engine.history.snapshot_time_at(scope, "realm_items", now) if scope else 0
    age_h = ((now - snap_ts) / 3600.0) if snap_ts else 0.0

    sentinel = engine.sentinel
    values: Dict[int, DropValue] = {}
    for item_id in item_ids:
        value = DropValue(item_id=int(item_id))
        value.shipped_price = dataset.prices.get(int(item_id))
        try:
            base = _base_features(engine, realm, int(item_id), 1, now)
        except Exception as exc:
            value.status = "unknown"
            value.reason = f"the features could not be built: {exc}"
            values[int(item_id)] = value
            continue

        # the record price model: the price the learning engine learned
        if engine.price_model is not None:
            try:
                frame = pd.DataFrame([base])
                model = engine.realm_models.get(realm)
                if model is not None:
                    value.record_price = int(round(float(model.predict_net_unit(frame)[0])))
                    value.model_scope = "realm"
                else:
                    value.record_price = int(round(float(
                        engine.price_model.predict_net_unit(frame)[0])))
                    value.model_scope = "regional"
            except Exception as exc:
                value.flags.append(f"record-price-failed: {exc}")

        # the Sentinel: the price, the interval, the sale probability
        if sentinel is not None:
            risk = risk_features_from_rows(local_state.get(int(item_id)),
                                           region_state.get(int(item_id)), age_h)
            try:
                prediction = sentinel.predict(
                    base, realm=realm, item_id=int(item_id), horizon=horizon,
                    sample_size=history.get((realm, int(item_id)), 0),
                    risk_features=risk)
            except Exception as exc:
                prediction = None
                value.reason = f"the Sentinel prediction failed: {exc}"
            if prediction is not None:
                value.status = prediction.status
                value.reason = prediction.reason if hasattr(prediction, "reason") else ""
                value.reason = getattr(prediction, "fallback_reason", "") or value.reason
                value.model_version = prediction.model_version
                value.training_scope = prediction.training_scope
                value.data_quality = float(prediction.data_quality or 0.0)
                for flag in prediction.flags:
                    if flag not in value.flags:
                        value.flags.append(flag)
                if prediction.price:
                    value.price = int(prediction.price)
                    value.price_source = "sentinel"
                    value.price_low = prediction.price_low
                    value.price_high = prediction.price_high
                    value.interval_level = prediction.price_interval_level
                    value.interval_coverage = prediction.price_interval_coverage
                if prediction.probability is not None:
                    value.sale_probability = float(prediction.probability)
                    value.liquidity_source = "sentinel"

        if value.price is None and value.shipped_price:
            value.price = int(value.shipped_price)
            value.price_source = "shipped"
        if value.price is None and value.record_price:
            value.price = int(value.record_price)
            value.price_source = "record"
        if value.sale_probability is None and int(item_id) in sale_rates:
            value.sale_probability = float(sale_rates[int(item_id)])
            value.liquidity_source = "region-sale-rate"
        values[int(item_id)] = value
    return values


def verify_against_engine(engine, realm: str, item_ids: Sequence[int],
                           values: Dict[int, DropValue],
                           horizon: int) -> Dict[str, object]:
    """Compare the fast path with the engine's own estimate.

    The script does not use ``PricingEngine.estimate`` because it is
    four times slower; this proves the two agree on the same items.
    """
    checked = 0
    price_diff = 0.0
    probability_diff = 0.0
    for item_id in item_ids:
        estimate = engine.estimate(realm, int(item_id))
        value = values.get(int(item_id))
        if value is None:
            continue
        checked += 1
        if estimate.price and value.record_price:
            price_diff = max(price_diff, abs(estimate.price - value.record_price)
                              / max(estimate.price, 1))
        if estimate.sale_probability is not None and value.sale_probability is not None:
            probability_diff = max(probability_diff,
                                   abs(estimate.sale_probability - value.sale_probability))
    return {
        "items": checked,
        "max_relative_price_difference": round(price_diff, 6),
        "max_absolute_probability_difference": round(probability_diff, 6),
    }


# ----------------------------------------------------------------------
# The ranking
# ----------------------------------------------------------------------

def rank_farms(drops: Sequence[NpcDrop], values: Dict[int, DropValue],
               realm: str, kpm: float) -> List[Farm]:
    """One farm per NPC, with the EV of its drops."""
    farms: Dict[int, Farm] = {}
    for drop in drops:
        farm = farms.get(drop.npc_id)
        if farm is None:
            farm = Farm(npc_id=drop.npc_id, npc_name=drop.npc_name,
                        zone=drop.zone, zone_name=drop.zone_name,
                        realm=realm, spawns=drop.spawns, kpm=kpm)
            farms[drop.npc_id] = farm
        value = values.get(drop.item_id)
        if value is None or value.price is None:
            farm.unpriced += 1
            continue
        farm.priced += 1
        farm.drops.append((drop, value))
        price = float(value.price)
        gross = drop.drop_rate * drop.qty * price
        liquidity = float(value.sale_probability or 0.0)
        farm.gross += gross
        farm.net += gross * liquidity
        low = value.price_low if value.price_low is not None else value.price
        high = value.price_high if value.price_high is not None else value.price
        farm.net_low += drop.drop_rate * drop.qty * float(low) * liquidity
        farm.net_high += drop.drop_rate * drop.qty * float(high) * liquidity
    out = list(farms.values())
    out.sort(key=lambda f: (f.net, f.gross), reverse=True)
    return out


# ----------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------

def _gold(copper: float) -> str:
    return f"{copper / COPPER_PER_GOLD:.2f}g"


def print_report(farms: Sequence[Farm], realm: str, dataset: DatasetModel,
                 values: Dict[int, DropValue], kpm: float,
                 horizon: int) -> None:
    """The console report: the ranking, then the provenance behind it."""
    print(f"== REALM {realm} (EU) ==")
    print(f"   prices: Sentinel {dataset.sentinel_model or 'n/a'} "
          f"(net of the house cut) | shipped dataset {dataset.generated_at or 'n/a'} "
          f"({len(dataset.prices)} items)")
    print(f"   EV/kill = sum(rate x qty x price); EV/hour at {kpm:g} kills/min; "
          f"P(sell) horizon {horizon}h")
    print()
    header = (f"  {'#':>2}  {'NPC':<26.26} {'zone':<16.16} {'sp':>3} "
              f"{'EV/kill':>9} {'EV/hour':>10} {'band low..high':>21} {'priced':>7}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for index, farm in enumerate(farms, 1):
        print(f"  {index:>2}  {farm.npc_name[:26]:<26.26} "
              f"{(farm.zone_name or str(farm.zone))[:16]:<16.16} {farm.spawns:>3} "
              f"{_gold(farm.net):>9} {_gold(farm.net * 60.0 * kpm):>10} "
              f"{_gold(farm.net_low) + '..' + _gold(farm.net_high):>21} "
              f"{farm.priced:>3}/{farm.priced + farm.unpriced:<3}")
    print()

    for index, farm in enumerate(farms[:3], 1):
        print(f"-- #{index} {farm.npc_name} ({farm.zone_name or farm.zone}) --")
        ranked = sorted(farm.drops,
                        key=lambda pair: pair[1].price * pair[0].drop_rate
                        * pair[0].qty * (pair[1].sale_probability or 0.0),
                        reverse=True)
        for drop, value in ranked[:8]:
            band = ""
            if value.price_low and value.price_high:
                band = f" [{_gold(value.price_low)}..{_gold(value.price_high)}]"
            probability = (f"{value.sale_probability:.3f}" if value.sale_probability
                           is not None else "n/a")
            print(f"   {drop.item_name[:32]:<32.32} x{drop.qty:g} @{drop.drop_rate:.3%} "
                  f"= {_gold(value.price):>10} {value.price_source:<8} "
                  f"P(sell)={probability} ({value.liquidity_source}){band}")
            if value.flags or value.reason:
                notes = ", ".join(value.flags) or value.reason
                print(f"      {notes[:110]}")
        print()

    sources: Dict[str, int] = {}
    liquidity: Dict[str, int] = {}
    unpriced = 0
    probabilities: Dict[float, int] = {}
    for value in values.values():
        sources[value.price_source] = sources.get(value.price_source, 0) + 1
        liquidity[value.liquidity_source] = liquidity.get(value.liquidity_source, 0) + 1
        if value.price is None:
            unpriced += 1
        if value.sale_probability is not None:
            key = round(float(value.sale_probability), 3)
            probabilities[key] = probabilities.get(key, 0) + 1
    print("provenance:")
    print("   price: " + ", ".join(f"{key}={count}"
                                   for key, count in sorted(sources.items()))
          + f" | unpriced={unpriced}")
    print("   liquidity: " + ", ".join(f"{key}={count}"
                                       for key, count in sorted(liquidity.items())))
    versions = sorted({value.model_version for value in values.values() if value.model_version})
    if versions:
        print(f"   sentinel model: {', '.join(versions)}")
    scopes = sorted({value.training_scope for value in values.values() if value.training_scope})
    if scopes:
        print(f"   training scope: {', '.join(scopes)}")
    print(f"   shipped model: {dataset.model.references} "
          f"n_train={dataset.model.n_train} within25={dataset.model.within_25:.3f} "
          f"interval={dataset.model.interval_level:.0%} "
          f"coverage={dataset.model.interval_coverage:.3f}")
    if probabilities:
        top = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)[:5]
        print("   P(sell): " + ", ".join(f"{key:.3f} x{count}"
                                         for key, count in top)
              + f" ({len(probabilities)} distinct values)")


# ----------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------

def load_engine(cfg: PricingConfig, *, with_interest_book: bool = False):
    """The pricing engine.

    The appearance interest book is *evidence*, not a price: reading it
    whole takes minutes (a full scan of the scan database) and the farm
    EV does not use it.  It is skipped by default and the report says so.
    """
    import woweconomy.pricing.engine as engine_module
    from woweconomy.pricing.interests import InterestBook

    original = engine_module.load_interests
    if not with_interest_book:
        def _interests(dfa_db, appearance_ids=None):
            book = InterestBook()
            book.notes.append("the appearance interest book was not read")
            return book
        engine_module.load_interests = _interests
    try:
        return engine_module.PricingEngine(cfg).load()
    finally:
        engine_module.load_interests = original


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rank farms with the DfA estimate (trained dataset prices, EU realms).")
    ap.add_argument("--max-items", type=int, default=None,
                    help="Limit number of items processed (for faster debugging runs)." )
    ap.add_argument("zones", nargs="*", default=[],
                    help="zone JSON file(s) (default: every zone_*.json)")
    ap.add_argument("--zone-dir", default=str(DEFAULT_ZONE_DIR))
    ap.add_argument("--realms", default="",
                    help="EU realms to rank, comma separated (default: the "
                         "realm the account sells in the most)")
    ap.add_argument("--all-realms", action="store_true",
                    help="rank every EU realm the account sold in")
    ap.add_argument("--region-index", default=str(DEFAULT_REALMS_INDEX),
                    help="the EU realm index (default: %(default)s)")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET),
                    help="the exported dataset (default: %(default)s)")
    ap.add_argument("--db", default=str(ROOT / "tsm_markets.db"),
                    help="the TSM mirror (default: %(default)s)")
    ap.add_argument("--horizon", type=int, default=0,
                    help="the sale horizon in hours (default: the engine's own)")
    ap.add_argument("--top", type=int, default=15, help="farms to print per realm")
    ap.add_argument("--kpm", type=float, default=20.0, help="kills per minute")
    ap.add_argument("--verify", type=int, default=0,
                    help="check the fast path against the engine's own estimate "
                         "on N items")
    ap.add_argument("--out", default="", help="write the full report as JSON")
    ap.add_argument("--quiet", action="store_true", help="no progress output")
    args = ap.parse_args(argv)

    def say(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    zone_paths = [Path(z) for z in args.zones]
    if not zone_paths:
        zone_paths = sorted(Path(args.zone_dir).glob("zone_*.json"))
    if not zone_paths:
        raise SystemExit(f"no zone file in {args.zone_dir}")
    missing = [str(z) for z in zone_paths if not z.exists()]
    if missing:
        raise SystemExit("missing zone file(s): " + ", ".join(missing))

    dataset = load_dataset(args.dataset)
    index = load_eu_index(args.region_index, args.db)
    if index.region != "eu":
        raise SystemExit(f"the realm index is {index.region}, not eu")
    if args.realms:
        realms = resolve_realms([r for r in args.realms.split(",") if r.strip()], index)
    else:
        realms = default_realms(index, args.all_realms)

    drops = load_zone_drops(zone_paths)
    if not drops:
        raise SystemExit("no drops in the zone file(s)")
    item_ids = sorted({drop.item_id for drop in drops})
    # optional limit for faster runs (e.g., debugging)
    if getattr(args, 'max_items', None):
        item_ids = item_ids[:args.max_items]

    say(f"zones: {len(zone_paths)} | drops: {len(drops)} | items: {len(item_ids)}")
    say(f"realms: {', '.join(realms)}")

    cfg = PricingConfig(tsm_db=Path(args.db))
    if args.horizon:
        cfg.horizon_h = int(args.horizon)
    started = time.time()
    engine = load_engine(cfg)
    say(f"the engine is loaded ({time.time() - started:.0f}s)")

    sale_rates = load_region_sale_rates(args.db)
    history = load_sale_history(args.db)
    report: Dict[str, object] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "region": "eu",
        "realms": list(realms),
        "horizon_h": cfg.horizon_h,
        "kills_per_minute": args.kpm,
        "dataset": {
            "source": dataset.source,
            "generated_at": dataset.generated_at,
            "sentinel_model": dataset.sentinel_model,
            "prices": len(dataset.prices),
            "shipped_references": list(dataset.model.references),
            "shipped_n_train": dataset.model.n_train,
            "shipped_within_25": dataset.model.within_25,
            "shipped_within_50": dataset.model.within_50,
            "shipped_interval_level": dataset.model.interval_level,
            "shipped_interval_coverage": dataset.model.interval_coverage,
        },
        "farms": {},
    }

    for realm in realms:
        scope = engine.realm_scope(realm)
        if not scope:
            say(f"  {realm}: no auction house in the mirror -- skipped")
            continue
        started = time.time()
        values = estimate_items(engine, realm, item_ids, dataset, sale_rates,
                                history, cfg.horizon_h)
        say(f"  {realm}: {len(values)} items priced in {time.time() - started:.0f}s")
        farms = rank_farms(drops, values, realm, args.kpm)
        entry: Dict[str, object] = {
            "scope": scope,
            "farms": [farm.as_dict() for farm in farms[:max(args.top, 1)]],
            "all_farms": [farm.as_dict() for farm in farms],
        }
        if args.verify:
            checked = verify_against_engine(engine, realm,
                                            item_ids[:args.verify], values, cfg.horizon_h)
            say(f"  {realm}: verified {checked['items']} items against the engine's "
                f"own estimate: max price difference "
                f"{checked['max_relative_price_difference']}, max probability "
                f"difference {checked['max_absolute_probability_difference']}")
            entry["verification"] = checked
        report["farms"][realm] = entry
        print_report(farms, realm, dataset, values, args.kpm, cfg.horizon_h)
        print()

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        print(f"report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
