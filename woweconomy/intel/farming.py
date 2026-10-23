"""What to farm today, priced at what the market actually pays.

The existing :mod:`woweconomy.analytics.farm_ranking` answers "which NPC
drops the most value per kill" with ``avgSalePrice`` and a liquidity
weight.  It is a good ranking, but ``avgSalePrice`` is a regional model
and it can be far above a realm's realised price.

This pipeline is separate, and it starts from the other end:

``price``
    the median **confirmed sale** on this realm, when there is a sample;
    otherwise the regional sale average *discounted* by a stated factor.
    ``dbmarket``/``marketValue`` is never a realizable price.

``liquidity``
    the realm's observed sold units per day, when the journal has enough
    sales; otherwise a labelled assumption, never a measurement.

``competition``
    the units currently listed on the realm, from a Blizzard snapshot,
    with a stated maximum share of that shelf.

``recommendation``
    ``min(drop rate x kills/hour, liquidity cap, competition cap)``
    priced at the realizable price, after the auction cut.

An item with no defensible price is excluded and the reason is recorded.
That is deliberate: the alternative is a recommendation built on an ask
nobody paid.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from woweconomy.ml.config import SENTINEL_COPPER

from . import ah as ah_module

AH_CUT = 0.05
COPPER_PER_GOLD = 10_000


def _clean(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number >= SENTINEL_COPPER:
        return None
    return number


def _slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("'", "")
    text = "".join(ch if ch.isalnum() else "-" for ch in text)
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


@dataclass
class PricePoint:
    item_id: int
    name: str = ""
    observed_unit_price: Optional[float] = None
    observed_sales: int = 0
    observed_units: int = 0
    observed_window_days: float = 0.0
    region_sale_avg: Optional[float] = None
    region_sale_rate: Optional[float] = None
    region_sold_per_day: Optional[float] = None
    min_buyout: Optional[float] = None
    market_value: Optional[float] = None
    competing_units: Optional[int] = None
    competing_listings: Optional[int] = None
    #: whether a snapshot existed at all, so "not listed" and "not looked"
    #: cannot be reported as the same thing
    competition_checked: bool = False
    competition_snapshot: str = ""
    realizable_unit_price: Optional[float] = None
    price_basis: str = ""
    price_confidence: str = "none"

    @property
    def observed_units_per_day(self) -> Optional[float]:
        if self.observed_units and self.observed_window_days > 0:
            return self.observed_units / self.observed_window_days
        return None


@dataclass
class FarmConfig:
    #: global fallback, used only for an NPC without a supplied rate
    kills_per_hour: float = 20.0
    #: npc_id -> measured kills per hour.  There is no honest way to derive
    #: this from a drop table, so it is an explicit input.
    kill_rates: Dict[int, float] = field(default_factory=dict)
    min_observed_sales: int = 5
    region_conservatism: float = 0.70
    #: how much of the realm's observed daily demand we assume we can take
    max_share_of_observed_demand: float = 1.0
    #: how much of the currently listed units we assume we can move per day
    max_share_of_listed_units: float = 0.25
    #: when no realm demand is observed, this share of the gross rate is
    #: assumed sellable.  It is an assumption and is labelled as one.
    unknown_liquidity_share: float = 0.25
    #: How many days of demand the stock on hand is allowed to cover before
    #: farming more of the same item stops being the best use of the hour.
    inventory_horizon_days: float = 7.0
    ah_cut: float = AH_CUT
    hours: float = 1.0


@dataclass
class FarmItem:
    item_id: int
    name: str
    units_per_hour: float
    unit_price: float
    price_basis: str
    price_confidence: str
    realizable_units_per_hour: float
    gross_gold_per_hour: float
    realizable_gold_per_hour: float
    liquidity_cap_per_hour: Optional[float] = None
    competition_cap_per_hour: Optional[float] = None
    inventory_cap_per_hour: Optional[float] = None
    current_inventory_units: int = 0
    kill_rate_per_hour: float = 0.0
    kill_rate_basis: str = ""
    #: units per kill this row was priced with, and where that came from:
    #: the game's own loot log, or a Wowhead estimate
    drop_rate: float = 0.0
    drop_rate_basis: str = ""
    competition_data_missing: bool = False
    liquidity_basis: str = ""
    competition_basis: str = ""
    inventory_note: str = ""


@dataclass
class FarmRecommendation:
    npc_id: int
    npc_name: str
    zone_name: str
    items: List[FarmItem] = field(default_factory=list)
    excluded: List[Dict[str, Any]] = field(default_factory=list)
    gross_gold_per_hour: float = 0.0
    realizable_gold_per_hour: float = 0.0
    kill_rate_per_hour: float = 0.0
    kill_rate_basis: str = ""
    confidence: str = "none"
    notes: List[str] = field(default_factory=list)

    @property
    def realizable_gold(self) -> float:
        return self.realizable_gold_per_hour

    @property
    def gross_gold(self) -> float:
        return self.gross_gold_per_hour


def resolve_price(point: PricePoint, cfg: Optional[FarmConfig] = None) -> PricePoint:
    """Choose a realizable unit price and say why.

    The order is deliberate: a realm's own confirmed sales first, then a
    discounted regional sale average.  An asking price never wins.
    """
    cfg = cfg or FarmConfig()
    if (point.observed_sales >= cfg.min_observed_sales
            and point.observed_unit_price and point.observed_unit_price > 0):
        point.realizable_unit_price = float(point.observed_unit_price)
        point.price_basis = (f"median of {point.observed_sales} confirmed realm"
                            f" sales ({point.observed_units} units)")
        point.price_confidence = "high"
        return point
    if point.observed_sales > 0 and point.observed_unit_price:
        point.realizable_unit_price = float(point.observed_unit_price)
        point.price_basis = (f"median of only {point.observed_sales} confirmed"
                            " realm sales; thin sample")
        point.price_confidence = "low"
        return point
    if point.region_sale_avg and point.region_sale_avg > 0:
        point.realizable_unit_price = (float(point.region_sale_avg)
                                       * cfg.region_conservatism)
        point.price_basis = (f"regional sale average {point.region_sale_avg:,.0f}"
                            f" copper discounted to {cfg.region_conservatism:.0%};"
                            " no realm sales observed")
        # A regional sale rate is not this realm's demand.  Without a
        # confirmed realm sale this is a fallback, so it stays low even
        # when the regional series looks active.
        point.price_confidence = "low"
        return point
    point.realizable_unit_price = None
    point.price_basis = ("no confirmed realm sale and no regional sale average;"
                        " the asking price is not a realizable price")
    point.price_confidence = "none"
    return point


def _confidence_rank(value: str) -> int:
    return {"none": 0, "low": 1, "medium": 2, "high": 3}.get(value, 0)


def _lowest_confidence(values: Iterable[str]) -> str:
    ranked = min((_confidence_rank(v), v) for v in values)
    return ranked[1]


def recommend_farm(drops: Sequence[Any], book: Dict[int, PricePoint],
                   cfg: Optional[FarmConfig] = None,
                   inventory_units: Optional[Dict[int, int]] = None,
                   observed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One row per NPC, priced from the book and capped by market depth.

    ``inventory_units`` is the stock already on the account.  It is not
    there to inflate a return: stock on hand is supply that has to clear
    before new farming of the same item is the best use of the hour.

    ``observed`` carries what the game measured (see
    :func:`woweconomy.intel.mobs.observed_rates`): ``kill_rates`` keyed by
    NPC and ``drop_rates`` keyed by ``(npc, item)``.  A measured rate wins
    over the Wowhead estimate; where nothing was measured, the estimate
    stays and the row says so.
    """
    cfg = cfg or FarmConfig()
    inventory_units = inventory_units or {}
    observed = observed or {}
    observed_kills: Dict[int, float] = observed.get("kill_rates") or {}
    observed_drops: Dict[Any, float] = observed.get("drop_rates") or {}
    kill_basis: Dict[int, str] = observed.get("kill_rate_basis") or {}
    drop_basis: Dict[Any, str] = observed.get("drop_rate_basis") or {}
    by_npc: Dict[int, List[Any]] = {}
    for drop in drops:
        by_npc.setdefault(int(getattr(drop, "npc_id", 0)), []).append(drop)

    recommendations: List[FarmRecommendation] = []
    for npc_id, npc_drops in by_npc.items():
        first = npc_drops[0]
        supplied = npc_id in cfg.kill_rates
        measured = (not supplied and npc_id in observed_kills
                    and float(observed_kills[npc_id]) > 0)
        if supplied:
            kill_rate = float(cfg.kill_rates[npc_id])
            rate_basis = "supplied rate for this NPC"
        elif measured:
            kill_rate = float(observed_kills[npc_id])
            rate_basis = ("measured in game: "
                          + kill_basis.get(npc_id, "observed kills per hour"))
        else:
            kill_rate = float(cfg.kills_per_hour)
            rate_basis = (f"global default {cfg.kills_per_hour:.0f}/h;"
                          " not measured for this NPC")
        rec = FarmRecommendation(npc_id=npc_id,
                                 npc_name=str(getattr(first, "npc_name", "")),
                                 zone_name=str(getattr(first, "zone_name", "")),
                                 kill_rate_per_hour=kill_rate,
                                 kill_rate_basis=rate_basis)
        for drop in npc_drops:
            item_id = int(drop.item_id)
            point = book.get(item_id)
            if point is None:
                rec.excluded.append({"item_id": item_id,
                                     "name": drop.item_name,
                                     "reason": "no price data"})
                continue
            resolve_price(point, cfg)
            if not point.realizable_unit_price:
                rec.excluded.append({"item_id": item_id,
                                     "name": drop.item_name,
                                     "reason": point.price_basis})
                continue
            drop_rate = max(0.0, float(drop.drop_rate))
            drop_rate_basis = "Wowhead estimate"
            measured_drop = observed_drops.get((npc_id, item_id))
            if measured_drop is not None:
                drop_rate = max(0.0, float(measured_drop))
                drop_rate_basis = ("measured in game: "
                                   + drop_basis.get((npc_id, item_id),
                                                    "observed units per kill"))
            rate = drop_rate * max(0.0, float(drop.qty))
            units_per_hour = rate * kill_rate
            if units_per_hour <= 0:
                continue
            liquidity_cap = None
            liquidity_basis = ""
            daily = point.observed_units_per_day
            if daily is not None:
                liquidity_cap = (daily * cfg.max_share_of_observed_demand) / 24.0
                liquidity_basis = (f"observed {daily:.2f} units/day on this realm;"
                                   f" using {cfg.max_share_of_observed_demand:.0%}")
            else:
                liquidity_cap = units_per_hour * cfg.unknown_liquidity_share
                liquidity_basis = ("no realm sales observed; assuming "
                                   f"{cfg.unknown_liquidity_share:.0%} of the gross"
                                   " rate is sellable")
            competition_cap = None
            competition_basis = ""
            competition_missing = point.competing_units is None
            if point.competing_units is not None:
                competition_cap = (point.competing_units
                                   * cfg.max_share_of_listed_units / 24.0)
                competition_basis = (f"{point.competing_units} units listed on the"
                                     f" realm; using {cfg.max_share_of_listed_units:.0%}")
            elif not point.competition_checked:
                competition_basis = ("no auction-house snapshot for this realm;"
                                     " competition was not measured")
            else:
                competition_basis = (
                    f"item not listed in snapshot {point.competition_snapshot or '?'};"
                    " competition was not observed")
            stock = int(inventory_units.get(item_id, 0) or 0)
            inventory_cap = None
            inventory_note = ""
            if daily is not None:
                covered_per_day = stock / max(1e-9, cfg.inventory_horizon_days)
                remaining_daily = max(
                    0.0,
                    daily * cfg.max_share_of_observed_demand - covered_per_day)
                inventory_cap = remaining_daily / 24.0
                inventory_note = (
                    f"{stock} units in stock; {cfg.inventory_horizon_days:.0f} days"
                    f" of demand is {daily * cfg.inventory_horizon_days:.1f} units,"
                    f" leaving {remaining_daily:.2f}/day to farm")
            elif stock:
                inventory_note = (
                    f"{stock} units in stock; realm demand is unknown, so no"
                    " stock cap is applied")
            caps = [c for c in (liquidity_cap, competition_cap, inventory_cap)
                    if c is not None]
            realizable = min([units_per_hour] + caps)
            unit_price = float(point.realizable_unit_price)
            net_price = unit_price * (1.0 - cfg.ah_cut)
            rec.items.append(FarmItem(
                item_id=item_id, name=str(drop.item_name),
                units_per_hour=round(units_per_hour, 4),
                unit_price=unit_price, price_basis=point.price_basis,
                price_confidence=point.price_confidence,
                realizable_units_per_hour=round(realizable, 4),
                gross_gold_per_hour=round(units_per_hour * net_price
                                          / COPPER_PER_GOLD, 2),
                realizable_gold_per_hour=round(realizable * net_price
                                               / COPPER_PER_GOLD, 2),
                liquidity_cap_per_hour=(round(liquidity_cap, 4)
                                        if liquidity_cap is not None else None),
                competition_cap_per_hour=(round(competition_cap, 4)
                                          if competition_cap is not None else None),
                inventory_cap_per_hour=(round(inventory_cap, 4)
                                       if inventory_cap is not None else None),
                current_inventory_units=stock,
                kill_rate_per_hour=kill_rate,
                kill_rate_basis=rate_basis,
                drop_rate=round(drop_rate, 6),
                drop_rate_basis=drop_rate_basis,
                competition_data_missing=competition_missing,
                liquidity_basis=liquidity_basis,
                competition_basis=competition_basis,
                inventory_note=inventory_note))
        if not rec.items:
            rec.notes.append("no item with a defensible price")
            recommendations.append(rec)
            continue
        rec.gross_gold_per_hour = round(sum(i.gross_gold_per_hour for i in rec.items), 2)
        rec.realizable_gold_per_hour = round(
            sum(i.realizable_gold_per_hour for i in rec.items), 2)
        rec.confidence = _lowest_confidence(i.price_confidence for i in rec.items)
        if rec.gross_gold_per_hour > 0:
            rec.notes.append(
                f"realizable is {rec.realizable_gold_per_hour / rec.gross_gold_per_hour:.0%}"
                " of gross after the auction cut and the caps")
        recommendations.append(rec)

    recommendations.sort(key=lambda r: (-r.realizable_gold_per_hour,
                                        -r.gross_gold_per_hour, r.npc_name))
    # The same item can drop from several NPCs; count it once, or the
    # "priced items" total is really "item mentions".
    priced = {item.item_id: item for rec in recommendations
              for item in rec.items}
    defaulted = sorted(rec.npc_id for rec in recommendations
                       if rec.npc_id not in cfg.kill_rates)
    snapshot = next((p.competition_snapshot for p in book.values()
                     if p.competition_snapshot), "")
    return {
        "kills_per_hour": cfg.kills_per_hour,
        "kill_rates": {
            "default_per_hour": cfg.kills_per_hour,
            "supplied_per_npc": {str(k): v for k, v in cfg.kill_rates.items()},
            "measured_per_npc": {str(k): round(v, 2)
                                 for k, v in observed_kills.items()},
            "defaulted_npcs": defaulted,
            "note": ("kills/hour is an input, not a measurement; supply a"
                     " measured rate per NPC to replace the default"),
        },
        "observed_data": {
            "kill_rates": len(observed_kills),
            "drop_rates": len(observed_drops),
            "note": ("rates measured in game replace the Wowhead drop rate"
                     " and the default kills/hour where they exist"),
        },
        "competition_data": {
            "snapshot_id": snapshot,
            "items_priced": len(priced),
            "items_with_listing_data": sum(1 for i in priced.values()
                                           if not i.competition_data_missing),
            "items_without_listing_data": sum(1 for i in priced.values()
                                              if i.competition_data_missing),
            "note": ("a missing listing is reported as missing, not as zero"
                     " competition; no cap is applied in that case"),
        },
        "ah_cut": cfg.ah_cut,
        "recommendations": recommendations,
        "priced_items": sum(1 for p in book.values() if p.realizable_unit_price),
        "basis_note": ("realizable, not reference: confirmed realm sales first,"
                       " then a discounted regional sale average.  Asking prices"
                       " are never used as a return."),
    }


def load_kill_rates(path: Path | str) -> Dict[int, float]:
    """Read measured kills/hour per NPC from JSON.

    Accepted shapes: ``{"123": 40}`` or ``[{"npc_id": 123,
    "kills_per_hour": 40}]``.  A non-positive or non-finite rate is a
    mistake in the input, not a fast farm, so it is rejected loudly.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.items() if isinstance(data, dict) else (
        (row.get("npc_id"), row.get("kills_per_hour")) for row in data)
    out: Dict[int, float] = {}
    for raw_npc, raw_rate in rows:
        npc_id = int(raw_npc)
        rate = float(raw_rate)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"kill rate for NPC {npc_id} must be positive, got {rate}")
        out[npc_id] = rate
    return out


def load_drops(paths: Sequence[Path | str]) -> List[Any]:
    """Reuse the existing Wowhead zone loader."""
    from woweconomy.analytics.farm_ranking import load_zone_drops
    return load_zone_drops([Path(p) for p in paths])


# ---------------------------------------------------------------------------
# the price book, assembled from the two existing sources
# ---------------------------------------------------------------------------

def _canonical_scope(con: sqlite3.Connection, realm: str) -> str:
    try:
        row = con.execute(
            "SELECT canonical_slug FROM tsm_realm_group WHERE LOWER(realm_name)=LOWER(?)",
            (realm,)).fetchone()
        if row and row[0]:
            return str(row[0])
    except sqlite3.OperationalError:
        pass
    return _slugify(realm)


def _chunks(values: Sequence[int], size: int = 800):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def build_price_book(tsm_db: Path | str, con, region: str, realm: str,
                     item_ids: Optional[Sequence[int]] = None,
                     cfg: Optional[FarmConfig] = None) -> Dict[int, PricePoint]:
    """Assemble one realm's price book.

    ``tsm_realm_price`` is the realm's local asking data (kept only for
    the floor); ``tsm_region_price`` supplies the regional sale average;
    the journal supplies the confirmed sales; the AH snapshot supplies the
    competing shelf.  Every source is labelled on the point.
    """
    cfg = cfg or FarmConfig()
    book: Dict[int, PricePoint] = {}
    # `connect_store`, not `sqlite3.connect`: on the stack the TSM tables are
    # rows in the one Postgres database, and opening the workstation file
    # there returns an empty book instead of an error.
    from woweconomy.dbcompat import connect_store

    tsm = connect_store(tsm_db, readonly=True)
    try:
        scope = _canonical_scope(tsm, realm)
        wanted = [int(i) for i in item_ids] if item_ids else None

        def item_filter(column: str) -> Tuple[str, List[Any]]:
            if wanted is None:
                return "", []
            return f" AND {column} IN (%s)" % ",".join("?" * len(wanted)), list(wanted)

        realm_sql = ("SELECT item_id, name, market_value, min_buyout FROM"
                     " tsm_realm_price WHERE scope = ?")
        region_sql = ("SELECT key_id, name, avg_sale_price, sale_rate, sold_per_day"
                      " FROM tsm_region_price WHERE 1=1")
        if wanted is None:
            realm_rows = tsm.execute(realm_sql, (scope,)).fetchall()
            region_rows = tsm.execute(region_sql).fetchall()
        else:
            realm_rows, region_rows = [], []
            for chunk in _chunks(wanted):
                placeholders = ",".join("?" * len(chunk))
                realm_rows.extend(tsm.execute(
                    realm_sql + f" AND item_id IN ({placeholders})",
                    (scope, *chunk)).fetchall())
                region_rows.extend(tsm.execute(
                    "SELECT key_id, name, avg_sale_price, sale_rate, sold_per_day"
                    " FROM tsm_region_price WHERE" f" key_id IN ({placeholders})",
                    chunk).fetchall())
    finally:
        tsm.close()

    for item_id, name, market_value, min_buyout in realm_rows:
        book[int(item_id)] = PricePoint(
            item_id=int(item_id), name=str(name or ""),
            market_value=_clean(market_value), min_buyout=_clean(min_buyout))
    for item_id, name, avg_sale, sale_rate, sold_per_day in region_rows:
        point = book.setdefault(int(item_id), PricePoint(item_id=int(item_id)))
        point.name = point.name or str(name or "")
        point.region_sale_avg = _clean(avg_sale)
        point.region_sale_rate = float(sale_rate) if sale_rate else None
        point.region_sold_per_day = float(sold_per_day) if sold_per_day else None

    _add_observed_sales(book, tsm_db, realm, wanted)
    _add_competition(book, con, region, realm)
    for point in book.values():
        resolve_price(point, cfg)
    return book


def _add_observed_sales(book: Dict[int, PricePoint], tsm_db: Path | str,
                        realm: str, wanted: Optional[Sequence[int]]) -> None:
    from woweconomy.dbcompat import connect_store, missing_table_errors

    con = connect_store(tsm_db, readonly=True)
    rows: List[Tuple] = []
    try:
        try:
            rows.extend(con.execute(
                "SELECT item_id, unit_price_copper, quantity, ts FROM"
                " auction_intel_events WHERE event_type='sale'"
                " AND LOWER(realm)=LOWER(?) AND unit_price_copper > 0",
                (realm,)).fetchall())
        except missing_table_errors():
            pass
        try:
            rows.extend(con.execute(
                "SELECT item_id, unit_price_copper, qty AS quantity,"
                " timestamp_unix AS ts FROM auction_events WHERE event_type='sale'"
                " AND LOWER(realm)=LOWER(?) AND unit_price_copper > 0",
                (realm,)).fetchall())
        except missing_table_errors():
            pass
    finally:
        con.close()
    grouped: Dict[int, List[sqlite3.Row]] = {}
    seen: set = set()
    wanted_set = set(wanted) if wanted is not None else None
    for row in rows:
        item_id = int(row["item_id"] or 0)
        if item_id <= 0 or (wanted_set is not None and item_id not in wanted_set):
            continue
        # Both journal tables can carry the same sale.  Counting it twice
        # would double the apparent demand, so the facts are deduplicated.
        key = (item_id, int(row["ts"] or 0), int(row["quantity"] or 0),
               int(row["unit_price_copper"] or 0))
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(item_id, []).append(row)
    for item_id, items in grouped.items():
        prices = [float(r["unit_price_copper"]) for r in items]
        stamps = [int(r["ts"]) for r in items if r["ts"] is not None]
        units = sum(int(r["quantity"] or 0) for r in items)
        window_days = ((max(stamps) - min(stamps)) / 86400.0) if len(stamps) > 1 else 1.0
        point = book.setdefault(item_id, PricePoint(item_id=item_id))
        point.observed_unit_price = statistics.median(prices)
        point.observed_sales = len(items)
        point.observed_units = units
        point.observed_window_days = max(1.0, window_days)


def _add_competition(book: Dict[int, PricePoint], con, region: str,
                     realm: str) -> None:
    snap = ah_module.latest_snapshot(con, region, realm) if con is not None else None
    if not snap:
        return
    counts: Dict[int, List[int]] = {}
    # Both books carry a listed quantity, so both say something about the
    # shelf a farm would compete with.  Only seller identity is missing
    # for commodities, and this function does not use it.
    for row in ah_module.snapshot_auctions(con, snap["snapshot_id"]):
        item_id = int(row["item_id"])
        listing, units = counts.setdefault(item_id, [0, 0])
        counts[item_id] = [listing + 1, units + int(row["quantity"])]
    for point in book.values():
        point.competition_checked = True
        point.competition_snapshot = str(snap["snapshot_id"])
    for item_id, (listings, units) in counts.items():
        point = book.setdefault(item_id, PricePoint(item_id=item_id))
        point.competing_listings = listings
        point.competing_units = units
        point.competition_checked = True
        point.competition_snapshot = str(snap["snapshot_id"])


def recommend_for_realm(tsm_db: Path | str, intel_con, region: str, realm: str,
                        zone_paths: Sequence[Path | str],
                        cfg: Optional[FarmConfig] = None,
                        inventory_units: Optional[Dict[int, int]] = None,
                        observed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The convenience entry point used by the CLI."""
    cfg = cfg or FarmConfig()
    drops = load_drops(zone_paths)
    item_ids = sorted({int(d.item_id) for d in drops})
    book = build_price_book(tsm_db, intel_con, region, realm, item_ids, cfg)
    result = recommend_farm(drops, book, cfg, inventory_units, observed)
    result.update({"region": region, "realm": realm,
                   "zones": [str(p) for p in zone_paths]})
    return result
