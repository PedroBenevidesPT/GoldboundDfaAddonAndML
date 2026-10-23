"""What a mob is actually worth: kills, loot and copper, observed in game.

MobInfo2 keeps the same primitives (see ``MI2_Database``/``MI2_Entry``): a
kill counter per NPC, a loot counter per corpse, the copper that dropped and
an item dictionary.  Its SavedVariables, however, are LibSerialize blobs
compressed with LibDeflate, so reusing them from Python would mean
reimplementing that format.  The addon therefore records the same primitives
natively (``DFA_AuctionIntelligence/Kills.lua``) and exports them as JSONL
next to the auction events, and this module is the only reader.

What is stored here is what happened, never a price:

``mob_observation``
    one row per (batch, NPC): kills, looted corpses, empty corpses, copper
    and the first/last kill timestamp.  A batch is flushed by the addon and
    its id is stable, so importing the same export twice stores it once.

``mob_loot_observation``
    the item counts of that observation, one row per item.

``mob_valuation``
    a *dated* valuation of those observations.  The three money columns are
    kept apart on purpose -- a vendor pays, the market estimates, a sale
    realises -- and ``valued_at``/``price_source`` are stored with them, so a
    number can be recomputed from the same observations and a newer price.

Nothing here trains a model, and nothing invents a price: an item without one
is left out of that column and the coverage is reported.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: where an observation came from.  There is exactly one writer today, and
#: naming it keeps a later importer (MobInfo2, say) distinguishable.
SOURCE = "game_addon"

COPPER_PER_GOLD = 10_000

#: a window shorter than this says nothing about kills per hour
MIN_SPAN_S = 60.0

#: a drop rate needs a denominator to mean anything
MIN_KILLS_FOR_DROP_RATE = 20

#: a price older than this is reported as stale, never silently dropped
DEFAULT_MAX_PRICE_AGE_H = 72.0


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def observation_id(batch_id: str, npc_id: int) -> str:
    """Stable id: one NPC in one flushed batch is one observation."""
    raw = f"{batch_id}|{int(npc_id)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def valuation_id(realm: str, npc_id: int, valued_at: int, price_source: str) -> str:
    raw = f"{realm.casefold()}|{int(npc_id)}|{int(valued_at)}|{price_source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


# --- parsing ---------------------------------------------------------------

def parse_observation(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One addon row, validated.  Anything without a batch or an NPC is dropped."""
    if not isinstance(row, dict):
        return None
    npc_id = _int(row.get("npcId") or row.get("npc_id"))
    batch_id = str(row.get("batchId") or row.get("batch_id") or "").strip()
    if npc_id <= 0 or not batch_id:
        return None
    kills = max(0, _int(row.get("kills")))
    loots = max(0, _int(row.get("loots") or row.get("loot_events")))
    empty = max(0, _int(row.get("empty") or row.get("empty_loot_events")))
    copper = max(0, _int(row.get("moneyCopper") or row.get("money_copper")))
    items: List[Dict[str, int]] = []
    for entry in row.get("items") or []:
        if not isinstance(entry, dict):
            continue
        item_id = _int(entry.get("itemId") or entry.get("item_id"))
        quantity = _int(entry.get("quantity"))
        if item_id > 0 and quantity > 0:
            items.append({"item_id": item_id, "quantity": quantity})
    if not (kills or loots or empty or copper or items):
        return None
    first_at = _int(row.get("firstAt") or row.get("first_at"))
    last_at = _int(row.get("lastAt") or row.get("last_at"))
    if last_at and first_at:
        last_at = max(first_at, last_at)
    return {
        "observation_id": observation_id(batch_id, npc_id),
        "batch_id": batch_id,
        "realm": str(row.get("realm") or ""),
        "character": str(row.get("character") or ""),
        "npc_id": npc_id,
        "npc_name": str(row.get("npcName") or row.get("npc_name") or "").strip(),
        "kills": kills,
        "loot_events": loots,
        "empty_loot_events": empty,
        "money_copper": copper,
        "first_at": first_at,
        "last_at": last_at,
        "items": items,
    }


def parse_kills_jsonl(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = parse_observation(row)
        if parsed is not None:
            out.append(parsed)
    return out


def parse_export_file(path: Path | str) -> List[Dict[str, Any]]:
    from woweconomy.analytics.auction_intel import extract_lua_string

    content = Path(path).read_text(encoding="utf-8", errors="replace")
    raw = extract_lua_string(content, "kills_jsonl")
    if raw is None:
        raise ValueError(
            f"no kills_jsonl block found in {path}"
            " (the addon records kills since 1.5.0)")
    return parse_kills_jsonl(raw)


# --- storage ---------------------------------------------------------------

def import_observations(con: sqlite3.Connection,
                        rows: Iterable[Dict[str, Any]]) -> int:
    """Insert what is new.  The same batch is inserted once, whatever the retry."""
    added = 0
    imported_at = _utcnow()
    for row in rows:
        cursor = con.execute(
            "INSERT OR IGNORE INTO mob_observation (observation_id, batch_id,"
            " realm, character, npc_id, npc_name, kills, loot_events,"
            " empty_loot_events, money_copper, first_at, last_at, source,"
            " imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["observation_id"], row["batch_id"], row["realm"],
             row["character"], row["npc_id"], row["npc_name"], row["kills"],
             row["loot_events"], row["empty_loot_events"], row["money_copper"],
             row["first_at"], row["last_at"], SOURCE, imported_at))
        if cursor.rowcount <= 0:
            continue
        added += 1
        for item in row["items"]:
            con.execute(
                "INSERT OR REPLACE INTO mob_loot_observation"
                " (observation_id, item_id, quantity) VALUES (?,?,?)",
                (row["observation_id"], item["item_id"], item["quantity"]))
    con.commit()
    return added


def load_observations(con: sqlite3.Connection, realm: str = "",
                      days: int = 30, now: Optional[int] = None) -> List[Dict[str, Any]]:
    """Observations whose last kill falls inside the window."""
    now = int(now if now is not None else time.time())
    since = now - max(1, int(days)) * 86400
    sql = ("SELECT * FROM mob_observation WHERE last_at >= ?")
    args: List[Any] = [since]
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        args.append(realm)
    rows = [dict(row) for row in con.execute(sql, args)]
    if not rows:
        return rows
    items: Dict[str, List[Dict[str, int]]] = {}
    for row in con.execute(
            "SELECT observation_id, item_id, quantity FROM mob_loot_observation"):
        items.setdefault(row["observation_id"], []).append(
            {"item_id": int(row["item_id"]), "quantity": int(row["quantity"])})
    for row in rows:
        row["items"] = items.get(row["observation_id"], [])
    return rows


# --- measurements ----------------------------------------------------------

def kill_rates(con: sqlite3.Connection, realm: str = "", days: int = 30,
               min_span_s: float = MIN_SPAN_S,
               now: Optional[int] = None) -> Dict[int, Dict[str, Any]]:
    """Kills per hour per NPC, from the time actually spent killing.

    The denominator is the summed span of the batches that carry a usable
    window, not the calendar distance between the first and the last kill:
    two sessions a week apart must not read as a slow farm.
    """
    grouped: Dict[int, Dict[str, Any]] = {}
    for row in load_observations(con, realm, days, now):
        npc = int(row["npc_id"])
        entry = grouped.setdefault(npc, {
            "npc_id": npc, "npc_name": row["npc_name"],
            "kills": 0, "loot_events": 0, "empty_loot_events": 0,
            "money_copper": 0, "time_s": 0.0, "observations": 0,
            "windows": 0,
        })
        entry["kills"] += int(row["kills"])
        entry["loot_events"] += int(row["loot_events"])
        entry["empty_loot_events"] += int(row["empty_loot_events"])
        entry["money_copper"] += int(row["money_copper"])
        entry["observations"] += 1
        span = max(0.0, float(row["last_at"]) - float(row["first_at"]))
        if row["kills"] > 0 and span >= min_span_s:
            entry["time_s"] += span
            entry["windows"] += 1
    for entry in grouped.values():
        if entry["time_s"] > 0:
            entry["kills_per_hour"] = entry["kills"] / (entry["time_s"] / 3600.0)
            entry["kill_rate_basis"] = (
                f"{entry['kills']} kills over {entry['time_s'] / 3600.0:.2f} h"
                f" in {entry['windows']} observed window(s)")
        else:
            entry["kills_per_hour"] = None
            entry["kill_rate_basis"] = (
                "no observation covered at least a minute of killing;"
                " the rate is unknown, not zero")
    return grouped


def drop_rates(con: sqlite3.Connection, realm: str = "", days: int = 30,
               min_kills: int = MIN_KILLS_FOR_DROP_RATE,
               now: Optional[int] = None) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """Units of an item per kill, per (NPC, item), when the sample allows it."""
    kills: Dict[int, int] = {}
    units: Dict[Tuple[int, int], int] = {}
    observations: Dict[int, int] = {}
    for row in load_observations(con, realm, days, now):
        npc = int(row["npc_id"])
        kills[npc] = kills.get(npc, 0) + int(row["kills"])
        observations[npc] = observations.get(npc, 0) + 1
        for item in row["items"]:
            key = (npc, int(item["item_id"]))
            units[key] = units.get(key, 0) + int(item["quantity"])
    out: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for key, quantity in units.items():
        npc = key[0]
        denominator = kills.get(npc, 0)
        if denominator < min_kills:
            continue
        out[key] = {
            "npc_id": npc, "item_id": key[1],
            "quantity": quantity, "kills": denominator,
            "rate": quantity / denominator,
            "basis": f"{quantity} units over {denominator} observed kills",
        }
    return out


# --- valuation -------------------------------------------------------------

@dataclass
class MobValue:
    """One mob, valued from observed loot at a stated, dated price."""

    realm: str
    npc_id: int
    npc_name: str = ""
    kills: int = 0
    money_copper: int = 0
    vendor_copper: int = 0
    market_copper: Optional[int] = None
    realized_copper: Optional[int] = None
    gold_per_mob_vendor: Optional[float] = None
    gold_per_mob_market: Optional[float] = None
    gold_per_mob_realized: Optional[float] = None
    total_units: int = 0
    priced_units: int = 0
    stale: bool = False
    price_age_h: Optional[float] = None
    price_source: str = ""
    valued_at: int = 0
    detail: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.total_units > 0 and self.priced_units < self.total_units

    def as_dict(self) -> Dict[str, Any]:
        return {
            "realm": self.realm, "npc_id": self.npc_id,
            "npc_name": self.npc_name, "kills": self.kills,
            "money_copper": self.money_copper,
            "vendor_copper": self.vendor_copper,
            "market_copper": self.market_copper,
            "realized_copper": self.realized_copper,
            "gold_per_mob_vendor": self.gold_per_mob_vendor,
            "gold_per_mob_market": self.gold_per_mob_market,
            "gold_per_mob_realized": self.gold_per_mob_realized,
            "total_units": self.total_units, "priced_units": self.priced_units,
            "partial": self.partial, "stale": self.stale,
            "price_age_h": self.price_age_h,
            "price_source": self.price_source, "valued_at": self.valued_at,
            "notes": list(self.notes),
            "items": [dict(item) for item in self.detail],
        }


def _parse_stamp(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def price_age_hours(tsm_db: Path | str, now: Optional[int] = None) -> Optional[float]:
    """How old the newest TSM snapshot behind the prices is, in hours."""
    now = int(now if now is not None else time.time())
    try:
        con = sqlite3.connect(str(tsm_db), timeout=10.0)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT MAX(updated_at) FROM tsm_snapshot WHERE kind='realm_items'"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    stamp = _parse_stamp(row[0] if row else None)
    if not stamp:
        return None
    return max(0.0, (now - stamp) / 3600.0)


PRICE_SOURCE = ("vendor=blizzard_items.sellPrice;"
                "market=tsm_realm_price.market_value;"
                "realized=journal confirmed sales, else region sale average"
                " discounted to 70%")


def value_mobs(con: sqlite3.Connection, tsm_db: Path | str, realm: str,
               region: str = "eu", days: int = 30,
               max_price_age_h: float = DEFAULT_MAX_PRICE_AGE_H,
               now: Optional[int] = None) -> List[MobValue]:
    """Gold per mob, with vendor, market and realised value kept apart.

    ``gold_per_mob_vendor`` is what a vendor would pay for the observed loot
    plus the copper that dropped.  ``gold_per_mob_market`` is the same with
    TSM's ``market_value`` instead of the vendor price.  ``gold_per_mob_realized``
    uses what the realm's own confirmed sales say a unit fetches, after the
    house cut.  They are three answers to three different questions and are
    never blended.
    """
    from woweconomy.pricing.dataset import vendor_prices

    from . import farming

    now = int(now if now is not None else time.time())
    aggregated: Dict[int, Dict[str, Any]] = {}
    for row in load_observations(con, realm, days, now):
        npc = int(row["npc_id"])
        entry = aggregated.setdefault(npc, {
            "npc_id": npc, "npc_name": row["npc_name"], "kills": 0,
            "money_copper": 0, "items": {},
        })
        entry["kills"] += int(row["kills"])
        entry["money_copper"] += int(row["money_copper"])
        entry["npc_name"] = entry["npc_name"] or row["npc_name"]
        for item in row["items"]:
            item_id = int(item["item_id"])
            entry["items"][item_id] = entry["items"].get(item_id, 0) + int(item["quantity"])
    if not aggregated:
        return []

    item_ids = sorted({item_id for entry in aggregated.values()
                       for item_id in entry["items"]})
    cfg = farming.FarmConfig()
    book = farming.build_price_book(tsm_db, con, region, realm, item_ids, cfg)
    vendors = vendor_prices(tsm_db, item_ids)
    age = price_age_hours(tsm_db, now)
    stale = age is not None and age > max_price_age_h
    cut = float(cfg.ah_cut)
    valued_at = now

    values: List[MobValue] = []
    for npc, entry in sorted(aggregated.items()):
        value = MobValue(realm=realm, npc_id=npc, npc_name=entry["npc_name"],
                         kills=entry["kills"], money_copper=entry["money_copper"],
                         price_age_h=age, stale=stale,
                         price_source=PRICE_SOURCE, valued_at=valued_at)
        vendor_total = 0
        market_total = 0
        realized_total = 0
        market_units = 0
        realized_units = 0
        for item_id, quantity in sorted(entry["items"].items()):
            point = book.get(item_id)
            vendor = int(vendors.get(item_id, 0) or 0)
            market = _float(point.market_value) if point else 0.0
            realized = _float(point.realizable_unit_price) if point else 0.0
            vendor_total += vendor * quantity
            detail = {
                "item_id": item_id, "quantity": quantity,
                "vendor_unit_copper": vendor,
                "market_unit_copper": round(market, 2) if market > 0 else None,
                "realized_unit_copper": round(realized, 2) if realized > 0 else None,
                "price_basis": (point.price_basis if point else
                                "no price for this item"),
            }
            if market > 0:
                market_total += market * quantity
                market_units += quantity
            if realized > 0:
                realized_total += realized * quantity
                realized_units += quantity
            value.detail.append(detail)
        value.total_units = sum(entry["items"].values())
        value.priced_units = market_units
        value.vendor_copper = int(round(vendor_total))
        value.market_copper = int(round(market_total)) if market_units else None
        value.realized_copper = int(round(realized_total)) if realized_units else None
        if value.kills > 0:
            value.gold_per_mob_vendor = round(
                (value.vendor_copper + value.money_copper) / value.kills / COPPER_PER_GOLD, 4)
            if value.market_copper is not None:
                value.gold_per_mob_market = round(
                    (value.market_copper + value.money_copper) / value.kills
                    / COPPER_PER_GOLD, 4)
            if value.realized_copper is not None:
                net = value.realized_copper * (1.0 - cut) + value.money_copper
                value.gold_per_mob_realized = round(
                    net / value.kills / COPPER_PER_GOLD, 4)
        else:
            value.notes.append("no kills observed; gold per mob is undefined")
        if value.total_units and value.priced_units == 0:
            value.notes.append(
                "no market price for any observed item: the market column is"
                " unknown here, not zero")
        elif value.total_units and value.priced_units < value.total_units:
            value.notes.append(
                f"{value.total_units - value.priced_units} of {value.total_units}"
                " unit(s) have no market price: the market figure is a lower bound")
        if value.realized_copper is None:
            value.notes.append("no realizable price for any observed item")
        if stale:
            value.notes.append(
                f"the price snapshot is {age:.1f} h old; revalue when it refreshes")
        values.append(value)
    return values


def persist_valuations(con: sqlite3.Connection,
                       values: Sequence[MobValue]) -> int:
    """Keep each dated valuation.  Re-running the same one writes nothing new."""
    added = 0
    for value in values:
        identifier = valuation_id(value.realm, value.npc_id,
                                  value.valued_at, value.price_source)
        cursor = con.execute(
            "INSERT OR IGNORE INTO mob_valuation (valuation_id, realm, npc_id,"
            " npc_name, valued_at, price_source, kills, money_copper,"
            " vendor_copper, market_copper, realized_copper,"
            " gold_per_mob_vendor, gold_per_mob_market, gold_per_mob_realized,"
            " priced_units, total_units, stale, price_age_h, detail_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, value.realm, value.npc_id, value.npc_name,
             value.valued_at, value.price_source, value.kills,
             value.money_copper, value.vendor_copper, value.market_copper,
             value.realized_copper, value.gold_per_mob_vendor,
             value.gold_per_mob_market, value.gold_per_mob_realized,
             value.priced_units, value.total_units, 1 if value.stale else 0,
             value.price_age_h, json.dumps(value.detail)))
        if cursor.rowcount > 0:
            added += 1
    con.commit()
    return added


# --- the farm pipeline's view ----------------------------------------------

def observed_rates(con: sqlite3.Connection, realm: str = "", days: int = 30,
                   now: Optional[int] = None) -> Dict[str, Any]:
    """Measured kill rates and drop rates, in the shape ``farming`` reads.

    Both are optional for the pipeline: a missing measurement leaves the
    Wowhead drop rate or the default kill rate in place, clearly labelled.
    """
    rates = kill_rates(con, realm, days, now=now)
    drops = drop_rates(con, realm, days, now=now)
    return {
        "kill_rates": {npc: row["kills_per_hour"] for npc, row in rates.items()
                       if row["kills_per_hour"]},
        "drop_rates": {key: row["rate"] for key, row in drops.items()},
        "kill_rate_basis": {npc: row["kill_rate_basis"]
                            for npc, row in rates.items()},
        "drop_rate_basis": {key: row["basis"] for key, row in drops.items()},
        "npcs": len(rates),
    }


def summary(con: sqlite3.Connection, tsm_db: Path | str, realm: str,
            region: str = "eu", days: int = 30, limit: int = 20,
            persist: bool = False, now: Optional[int] = None) -> Dict[str, Any]:
    """The CLI's answer: what was observed, and what it is worth."""
    observations = load_observations(con, realm, days, now)
    rates = kill_rates(con, realm, days, now=now)
    drops = drop_rates(con, realm, days, now=now)
    values = value_mobs(con, tsm_db, realm, region, days, now=now)
    persisted = persist_valuations(con, values) if persist else 0
    values.sort(key=lambda v: (-(v.gold_per_mob_realized or 0),
                               -(v.gold_per_mob_vendor or 0), v.npc_name))
    return {
        "realm": realm,
        "days": days,
        "observations": len(observations),
        "kills_total": sum(r["kills"] for r in rates.values()),
        "kills_per_hour": {str(npc): round(row["kills_per_hour"], 2)
                           for npc, row in rates.items()
                           if row["kills_per_hour"]},
        "drop_rates": {f"{npc}:{item}": round(row["rate"], 4)
                       for (npc, item), row in drops.items()},
        "valuations_persisted": persisted,
        "mobs": [value.as_dict() for value in values[:limit]],
        "basis": ("vendor = what a vendor pays; market = TSM market value;"
                  " realized = confirmed realm sales after the house cut."
                  " They are kept apart on purpose."),
    }
