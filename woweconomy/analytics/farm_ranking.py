"""Farm ranking — turn zone drop tables + TSM prices into farm plans.

Inputs
    zone JSONs from woweconomy/services/wowhead.py
        (woweconomy/wowhead_zones/zone_<id>.json — one entry per NPC with
        items [{item_id, item_name, item_type, drop_rate, qty, ...}])
    latest market snapshot per item (avgSalePrice, saleRate, soldPerDay),
        read from the frozen ``market_snapshots`` table
    item facts keyed by item id (tsm_markets.db :: db2_items, then
    blizzard_items): binding, quality and ItemSquishEraID.  The DB2 covers
    the whole item table; the API only the ids that were enriched.

Core formula (per drop: probability p, mean qty q, net proceeds v)
    EV_GROSS      = p * q * v                  (theoretical, copper)
    LIQUIDITY     = DBRegionSaleRate as-is     (verified 0-1 decimal: fraction
                                               of listed region auctions that
                                               sell; NO time dimension)
    EV_NET        = EV_GROSS * LIQUIDITY * VOL (VOL = 1 if sold_per_day > 0
                                               else 0.1: no recorded sales)
    v = avgSalePrice x (1 - ah_cut); vendor fallback uses Blizzard sell_price.
    Rows whose item is era-Classic, bound on pickup or grey are EXCLUDED: an
    era-Classic price belongs to another game, and a BoP item never reaches
    the AH.  Missing prices are EXCLUDED too (never zero-filled).
    Units: TSM/blizzard
    money fields are copper; display divides by 10,000 for gold.
    Days-to-sale is N/A: the metric cannot support it. No daily compounding.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from woweconomy.config import WowEconomyConfig
from woweconomy.services.blizzard_items import BlizzardItem
from woweconomy.services.db2_items import load_facts as load_item_facts

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------

@dataclass
class ItemMarket:
    """Sellable value of one item from the latest TSM snapshot (strict)."""
    item_id: int
    name: str = ""
    price: float = 0.0      # avgSalePrice ONLY (copper); 0/None = excluded
    sale_rate: float = 0.0  # TSM DBRegionSaleRate: fraction of listed region
                            # auctions that sell (0-1 decimal, verified scale).
                            # Per-LISTING sell-through, NO time dimension.
    sold_per_day: float = 0.0  # regional volume (mean 0.035-scale context)

    def net_value(self, ah_cut: float) -> float:
        """Estimated auction proceeds per unit (copper). Retail AH cut ~5%."""
        return max(self.price, 0.0) * (1.0 - ah_cut)


@dataclass
class NpcDrop:
    npc_id: int
    npc_name: str
    zone: int
    zone_name: str
    category: str
    item_id: int
    item_name: str
    drop_rate: float  # pure probability 0-1 (Wowhead % field)
    qty: float = 1.0  # mean units per drop
    item_type: str = ""  # Wowhead Type cell: Junk, Quest, Cloth Armor, ...
    spawns: int = 0  # spawn points in this zone (#locations count)


@dataclass
class DropContrib:
    item_id: int
    item_name: str
    category: str
    drop_rate: float
    qty: float
    unit_price: float  # avgSalePrice, raw copper
    net_value: float   # unit_price x (1 - ah_cut)
    expected: float      # EV_GROSS = rate x qty x net (theoretical)
    realizable: float    # EV_NET = gross x liquidity x volume factor
    vendor_value: float = 0.0  # p x qty x Blizzard sell_price (immediate gold)
    sale_rate: float = 0.0
    sold_per_day: float = 0.0


@dataclass
class NpcFarm:
    npc_id: int
    npc_name: str
    zone: int
    zone_name: str
    ev_per_kill: float = 0.0  # EV_GROSS sum (theoretical, before sale odds)
    realizable_ev: float = 0.0  # EV_NET_EXPECTED sum (liquidity-weighted)
    vendor_per_kill: float = 0.0  # VENDOR_GOLD_PER_KILL (immediate, no AH)
    confidence: float = 0.0  # DATA_CONFIDENCE = evidence share x freshness;
                             # evidence coverage, NOT a sale probability
    n_drops_valued: int = 0
    n_drops_unpriced: int = 0
    top_drops: List[DropContrib] = field(default_factory=list)
    jackpot_item: str = ""
    jackpot_value: float = 0.0  # max single-drop sale-weighted value
    jackpot_rate: float = 0.0   # its drop probability (for profile + P math)
    jackpot_qty: float = 1.0
    spawns: int = 0  # spawn points in this zone (camp density)


@dataclass
class JackpotHit:
    npc_id: int
    npc_name: str
    zone_name: str
    item_id: int
    item_name: str
    drop_rate: float
    qty: float = 1.0
    unit_price: float = 0.0
    net_value: float = 0.0
    expected: float = 0.0  # sale-weighted EV per kill
    p_drop_100: float = 0.0  # P(>=1 drop in 100 kills) = 1-(1-p)^100
    sale_rate: float = 0.0  # evidence: TSM sell-through
    sold_per_day: float = 0.0  # evidence: regional volume
    vendor_value: float = 0.0  # evidence: Blizzard vendor sell (copper)


@dataclass
class MogCandidate:
    item_id: int
    item_name: str
    npc_id: int
    npc_name: str
    zone_name: str
    drop_rate: float
    unit_price: float
    net_value: float
    sale_rate: float
    score: float  # = unit_price (highest avgSalePrice first)
    ev_weighted: float = 0.0  # rate x qty x net x sale weight (realizable)
    vendor_value: float = 0.0  # Blizzard vendor sell price (copper / unit)


@dataclass
class PriceLeader:
    """Most expensive items: one row per item, best farm source."""
    item_id: int
    item_name: str
    npc_id: int
    npc_name: str
    zone_name: str
    drop_rate: float
    unit_price: float
    sale_rate: float = 0.0
    sold_per_day: float = 0.0


@dataclass
class MatLeader:
    """Best material source: expected units/kill of one mat class from one NPC."""
    npc_id: int
    npc_name: str
    zone_name: str
    mat: str
    units_per_kill: float
    ev_per_kill: float


@dataclass
class MogFarm:
    """Transmog portfolio of one NPC: joint sell-through per listing round,
    acquisition-aware (you can only sell what you loot)."""
    npc_id: int
    npc_name: str
    zone_name: str
    n_mogs: int = 0
    portfolio_value: float = 0.0  # sum of avgSalePrice if all listed (montra)
    realizable_ev: float = 0.0  # sale-weighted EV of the portfolio per kill
    p_cycle_sale: float = 0.0  # 1-prod(1-P(acquire) x sale weight): any sells
    days_to_sale: Optional[float] = None  # always None: not computable
    top_items: list = field(default_factory=list)  # (name, rate, unit)


@dataclass
class FarmStop:
    """One camp in the rotation: a set of NPCs farmed before moving on."""
    stop: int
    npc_ids: List[int]
    npc_names: List[str]
    stop_ev_per_kill: float


@dataclass
class FarmRotation:
    stops: List[FarmStop] = field(default_factory=list)
    revisit_after_stops: int = 0


@dataclass
class ZoneRanking:
    zone: int
    zone_name: str
    npc_count: int  # NPCs with >= 1 item row (empty NPCs are skipped by design)
    farms: List[NpcFarm] = field(default_factory=list)
    mogs: List[MogCandidate] = field(default_factory=list)
    jackpots: List[JackpotHit] = field(default_factory=list)
    rotation: FarmRotation = field(default_factory=FarmRotation)
    excluded_blizz: int = 0  # drop rows killed by era / binding / quality
    excluded_era: int = 0    # ... of which era-Classic rows (DB2)
    excluded_bop: int = 0    # ... of which BoP/account-bound rows
    excluded_junk: int = 0   # ... of which grey quality rows
    mats: List[MatLeader] = field(default_factory=list)
    mog_farms: List[MogFarm] = field(default_factory=list)
    expensive: List[PriceLeader] = field(default_factory=list)
    vendor_farms: List[NpcFarm] = field(default_factory=list)
    recipes: List[JackpotHit] = field(default_factory=list)
    boe: List[JackpotHit] = field(default_factory=list)
    mixed: List[MixedFarm] = field(default_factory=list)
    mog_ev: List[MogCandidate] = field(default_factory=list)
    mog_fast: List[MogCandidate] = field(default_factory=list)
    mog_thin: List[MogCandidate] = field(default_factory=list)
    mog_vendor: List[MogCandidate] = field(default_factory=list)
    sessions: List[SessionPlan] = field(default_factory=list)
    price_asof: str = ""
    price_stale: bool = False


@dataclass
class FarmRankingConfig:
    """Knobs specific to farm ranking (market gates live in WowEconomyConfig)."""
    top_n: int = 10
    mog_max_drop_rate: float = 0.05  # mogs are rare by definition
    jackpot_min_rate: float = 0.0005  # ignore noise below 0.05%
    jackpot_max_rate: float = 0.10   # a jackpot is rare: below 10% rate
    min_spd_for_mog_jackpot: float = 0.0  # must SELL (spd above this)
    exclude_soulbound: bool = True   # BoP/account-bound (DB2 or Blizzard): unsellable
    exclude_poor: bool = True        # POOR quality: grey trash
    exclude_era: bool = True         # DB2 era-Classic rows: another game's prices
    sort_by: str = "ev"              # "ev" (steady gold) or "jackpot" (lottery)
    mog_acquire_kills: float = 600.0  # kills assumed when estimating whether
                                     # you even loot each portfolio mog
    price_age_days: Optional[int] = None  # snapshot age; scales DATA_CONFIDENCE
    price_base_mult: float = 1.0    # list-price multiplier machinery:
    price_rate_mult: float = 0.0    #   target = avg x (base + sr x rate_mult)
    price_vendor_floor_mult: float = 0.0  # floor = vendor x this (0 = off).
                                     # Defaults reproduce reference pricing
                                     # exactly (no silent behavior change).
    rotation_stops: int = 3          # camps to cycle (hyperspawn dodge)
    kills_per_minute: float = 20.0   # for cycle-time estimates
    travel_sec_per_stop: float = 60.0  # inter-camp travel per full cycle
    session_minutes: tuple = (15, 30, 60)  # BEST GOLDBOUND SESSION budgets


def item_exclusion_reason(item_id: int, facts: Dict[int, BlizzardItem],
                          r_cfg: "FarmRankingConfig") -> str:
    """Why this item cannot carry retail gold: '' means nothing against it.

    Keyed by ITEM ID, in this order:
        era   the DB2 says this row belongs to an era-Classic build, so its
              TSM price comes from a different game (the same id space).
        bop   bound on pickup / account: it can never reach the AH.
        junk  grey vendor trash.
    `era` is checked first on purpose: an era-Classic BoE is still not retail
    gold, and reporting it as "bop" would hide the real reason.
    """
    b = facts.get(item_id)
    if b is None:
        return ""  # no data: keep old behaviour rather than guess
    if r_cfg.exclude_era and b.is_era_classic:
        return "era"
    if r_cfg.exclude_soulbound and b.is_soulbound:
        return "bop"
    if r_cfg.exclude_poor and b.is_junk_quality:
        return "junk"
    return ""


def blizz_excluded(drop: "NpcDrop", facts: Dict[int, BlizzardItem],
                   r_cfg: "FarmRankingConfig") -> bool:
    """True when the item's binding/era/quality says it can never be gold."""
    return bool(item_exclusion_reason(drop.item_id, facts, r_cfg))


# Wowhead item types that are never worth farming for value.
JUNK_TYPES = {"junk"}


# Names that are never transmog (materials, consumables, quests, recipes).
_MOG_EXCLUDE = (
    "cloth", "leather", "hide", "scale", "ore", "bar", "herb", "bloom",
    "root", "moss", "lotus", "weed", "meat", "fish", "bread", "soup",
    "stew", "potion", "elixir", "flask", "scroll", "bandage", "poison",
    "reagent", "mote", "primal", "eternal", "essence", "dust", "shard",
    "crystal", "thread", "dye", "oil", "flux", "coal", "stone", "gem",
    "recipe", "plans", "pattern", "schematic", "design", "formula",
    "quest", "token", "coin", "mark", "emblem", "sigil", "heart", "blood",
    "fang", "claw", "horn", "tusk", "eye", "liver", "kidney", "meat",
    "tender", "mystery", "junk", "grey", "gray",
)


def _looks_like_mog(item_name: str) -> bool:
    nl = item_name.lower()
    return not any(w in nl for w in _MOG_EXCLUDE)


_MAT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("LEATHER", ("leather", "hide", "scale", "pelt", "fur", "scraps")),
    ("CLOTH", ("cloth", "linen", "wool", "silk", "mageweave", "runecloth",
               "netherweave", "thread")),
    ("HERB", ("herb", "bloom", "root", "moss", "lotus", "weed", "lichen",
              "vine", "thistle", "sage", "liferoot", "goldensansam")),
    ("ORE", ("ore", "bar", "stone", "ingot")),
    ("MEAT", ("meat", "flank", "egg", "fish", "clam", "chunk", "tender")),
    ("ENCHANT", ("dust", "essence", "shard")),
]


def mat_class(item_name: str) -> str:
    nl = (item_name or "").lower()
    for mat, words in _MAT_KEYWORDS:
        if any(w in nl for w in words):
            return mat
    return "OTHER"


def top_expensive(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 10,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[PriceLeader]:
    """Most expensive items, plain price rank: one row per item (best source).

    No appearance or rarity filter — pure avgSalePrice desc.  BoP/junk and
    priceless rows are out (same rules as everywhere else).  Evidence
    (sale_rate, sold_per_day) rides along so the reader judges the price.
    """
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    best: Dict[int, tuple] = {}
    for d in drops:
        if d.drop_rate <= 0 or blizz_excluded(d, blizz, r_cfg):
            continue
        m = market.get(d.item_id)
        if m is None or m.price <= 0:
            continue
        cur = best.get(d.item_id)
        if cur is None or d.drop_rate > cur[0].drop_rate:
            best[d.item_id] = (d, m)
    out = [PriceLeader(
        item_id=d.item_id, item_name=d.item_name or m.name,
        npc_id=d.npc_id, npc_name=d.npc_name, zone_name=d.zone_name,
        drop_rate=d.drop_rate, unit_price=m.price,
        sale_rate=m.sale_rate, sold_per_day=m.sold_per_day,
    ) for d, m in best.values()]
    out.sort(key=lambda x: x.unit_price, reverse=True)
    return out[:top_n]


def top_mats(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 15,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[MatLeader]:
    """Best (npc, material) pairs: expected mat units/kill + their EV."""
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    agg: Dict[tuple, list] = {}  # (npc_id, mat) -> [npc_name, zone, units, ev]
    for d in drops:
        if d.drop_rate <= 0:
            continue
        mat = mat_class(d.item_name)
        if mat == "OTHER":
            continue
        if blizz_excluded(d, blizz, r_cfg):
            continue
        m = market.get(d.item_id)
        if m is None or m.price <= 0:
            continue
        net = eff_net(m, blizz.get(d.item_id), cfg, r_cfg)
        units = d.drop_rate * d.qty
        exp = units * net * realization_weight(m)
        key = (d.npc_id, mat)
        row = agg.setdefault(key, [d.npc_name, d.zone_name, 0.0, 0.0])
        row[2] += units
        row[3] += exp
    leaders = [MatLeader(npc_id=k[0], npc_name=v[0], zone_name=v[1], mat=k[1],
                         units_per_kill=v[2], ev_per_kill=v[3])
               for k, v in agg.items()]
    leaders.sort(key=lambda x: x.ev_per_kill, reverse=True)
    return leaders[:top_n]


# ----------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------

def load_zone_drops(paths: List[Path]) -> List[NpcDrop]:
    """Wowhead drops as flat ``NpcDrop`` rows.

    On the shared Postgres the canonical table is ``wowhead_drop`` (written by
    the scraper, see :mod:`woweconomy.services.wowhead_store`); the zone JSONs
    are only a fallback for a legacy store that has not been imported yet.
    On SQLite the JSON files remain the input, which keeps the offline
    workflow and the tests working without a migration runner.
    """
    from woweconomy.dbcompat import is_postgres

    if is_postgres():
        from woweconomy.services import wowhead_store

        stored = wowhead_store.load_drops()
        if stored:
            return [NpcDrop(
                npc_id=int(row["npc_id"]), npc_name=row["npc_name"] or "",
                zone=int(row["zone_id"]), zone_name=row["zone_name"] or "",
                category=row["category"] or "",
                item_id=int(row["item_id"]),
                item_name=row["item_name"] or "",
                drop_rate=float(row["drop_rate"] or 0),
                qty=float(row["qty"] or 1),
                item_type=row["item_type"] or "",
                spawns=int(row["spawns"] or 0),
            ) for row in stored]

    drops: List[NpcDrop] = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # Accept both a list of npc entries and {npc_id: entry} maps.
        entries = data.values() if isinstance(data, dict) else data
        for entry in entries:
            for it in entry.get("items", []):
                iid = it.get("item_id")
                if iid is None:
                    continue
                drops.append(NpcDrop(
                    npc_id=int(entry.get("npc_id", 0)),
                    npc_name=entry.get("npc_name", ""),
                    zone=int(entry.get("zone", 0)),
                    zone_name=entry.get("name", entry.get("zone_name", "")),
                    # per-item category first (merged rows carry "drops+skinning")
                    category=it.get("category") or entry.get("category", ""),
                    item_id=int(iid),
                    item_name=it.get("item_name", ""),
                    drop_rate=float(it.get("drop_rate", 0) or 0),
                    qty=float(it.get("qty", 1) or 1),
                    item_type=it.get("item_type", ""),
                    spawns=int(entry.get("spawns", 0) or 0),
                ))
    return drops


def load_csv_market(path: str) -> Dict[int, ItemMarket]:
    """TSM snapshot from csv/items.csv: avgSalePrice-based, same shape as DB."""
    import csv
    out: Dict[int, ItemMarket] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                iid = int(r["itemId"])
            except (KeyError, ValueError):
                continue
            # Strict: avgSalePrice only — no marketValue fallback.
            # Items without avgSalePrice are dropped downstream (price <= 0).
            price = float(r.get("avgSalePrice") or 0)
            out[iid] = ItemMarket(
                item_id=iid, name=r.get("name", ""),
                price=price,
                sale_rate=float(r.get("saleRate") or 0),
                sold_per_day=float(r.get("soldPerDay") or 0),
            )
    return out


def load_region_market(db_path: str) -> Dict[int, ItemMarket]:
    """The live regional market: {item_id: ItemMarket}.

    This is the estimate's own price source: ``tsm_region_price`` holds
    the current regional TSM state, and its ``avg_sale_price`` is
    exactly the ``dbregionsaleavg`` reference the shipped model
    consumes (see :data:`woweconomy.pricing.shipped.REFERENCE_COLUMNS`),
    with ``sale_rate`` and ``sold_per_day`` next to it for the liquidity
    weight.  This is the farm report's price source: the report and the
    estimate must quote the same number, or the same item is worth two
    different amounts in two screens.  The frozen snapshot stays
    reachable with ``--price-source legacy``.
    """
    from woweconomy.dbcompat import connect_store

    conn = connect_store(db_path, readonly=True)
    try:
        rows = conn.execute("""
            SELECT r.key_id, COALESCE(NULLIF(r.name, ''), i.name, ''),
                   r.avg_sale_price, r.sale_rate, r.sold_per_day
            FROM tsm_region_price r
            LEFT JOIN items i ON i.itemId = r.key_id
        """).fetchall()
    finally:
        conn.close()
    out: Dict[int, ItemMarket] = {}
    for iid, name, asp, sr, spd in rows:
        # Strict: avg_sale_price only — no market_value fallback.
        out[int(iid)] = ItemMarket(
            item_id=int(iid), name=name or "",
            price=float(asp or 0),
            sale_rate=float(sr or 0), sold_per_day=float(spd or 0),
        )
    return out


def load_snapshot_market(db_path: str) -> Dict[int, ItemMarket]:
    """The frozen ``market_snapshots`` table (legacy).

    Kept for A/B and for a DB written before the live regional table
    existed: it is a snapshot of the same feed, at an older vintage.
    """
    from woweconomy.dbcompat import connect_store

    conn = connect_store(db_path, readonly=True)
    try:
        rows = conn.execute("""
            SELECT ms.itemId, COALESCE(i.name, ''),
                   ms.marketValue, ms.avgSalePrice, ms.saleRate, ms.soldPerDay
            FROM market_snapshots ms
            LEFT JOIN items i ON i.itemId = ms.itemId
            WHERE ms.snapshotId = (SELECT MAX(snapshotId) FROM snapshots)
        """).fetchall()
    finally:
        conn.close()
    out: Dict[int, ItemMarket] = {}
    for iid, name, mv, asp, sr, spd in rows:
        # Strict: avgSalePrice only — no marketValue fallback.
        out[int(iid)] = ItemMarket(
            item_id=int(iid), name=name or "",
            price=float(asp or 0),
            sale_rate=float(sr or 0), sold_per_day=float(spd or 0),
        )
    return out


def load_latest_market(db_path: str) -> Dict[int, ItemMarket]:
    """The latest market per item: {item_id: ItemMarket}.

    The live regional table wins: it is the one the DfA estimate reads,
    so the farm EV and the estimate quote the same price for the same
    item.  A DB written before that table existed (or one whose region
    table is empty) falls back to the frozen ``market_snapshots``
    snapshot, which stays reachable on purpose as the legacy source.
    """
    from woweconomy.dbcompat import missing_table_errors

    try:
        market = load_region_market(db_path)
    except missing_table_errors():
        market = {}
    if market:
        return market
    try:
        return load_snapshot_market(db_path)
    except missing_table_errors():
        return {}


def load_model_market(engine, item_ids, realm: str = "",
                      fallback: Optional[Dict[int, ItemMarket]] = None,
                      names: Optional[Dict[int, str]] = None,
                      min_confidence: float = 0.15,
                      sane_ratio: Tuple[float, float] = (0.25, 4.0)
                      ) -> Tuple[Dict[int, ItemMarket], Dict[str, int]]:
    """The farm's prices from the pricing engine, not from a TSM average.

    Two model paths are tried, in order of what they know:

    1. ``engine.estimate`` -- the realm/regional estimate, with its
       calibrated interval and sale probability.  Its ``expected_sale_price``
       is the *realizable* sale price (what the Sentinel expects the item to
       actually fetch), which is the number an EV should use; the ask is an
       asking price, not a realised one.
    2. ``engine.estimate_with_shipped_model`` -- the linear model on the
       regional references, for an item the realm has no history for.

    Only when both refuse the item does ``fallback`` (a market table) price
    it, and the return counts say how many took each path so the report can
    state its own coverage instead of implying the model priced everything.
    """
    fallback = fallback or {}
    names = names or {}
    out: Dict[int, ItemMarket] = {}
    counts = {"estimate": 0, "shipped": 0, "fallback": 0, "unpriced": 0,
              "low_confidence": 0, "clamped": 0}
    for item_id in item_ids:
        item_id = int(item_id)
        price: Optional[float] = None
        probability = 0.0
        sold_per_day = 0.0
        try:
            estimate = engine.estimate(realm, item_id)
        except Exception:  # a model that cannot answer must not stop the
            estimate = None  # rest of the zone from being priced
        confidence = float(getattr(estimate, "confidence", 0.0) or 0.0) if estimate else 0.0
        trustworthy = (estimate is not None and getattr(estimate, "available", False)
                       and confidence >= min_confidence)
        if estimate is not None and getattr(estimate, "available", False) and not trustworthy:
            # A number the model itself barely believes is not a farm price.
            # It falls through to the shipped model, and is counted, so the
            # report can say how much of the ranking rests on thin evidence.
            counts["low_confidence"] += 1
        if trustworthy:
            realizable = (getattr(estimate, "expected_sale_price", None)
                          or estimate.price)
            price = float(realizable) if realizable else None
            probability = float(getattr(estimate, "sale_probability", None) or 0.0)
            # The report's liquidity weight drops a listing to 10 % when
            # ``sold_per_day`` is zero, so the model's probability has to be
            # converted into the daily rate it stands for: p sales inside the
            # horizon is ``p * 24 / horizon`` sales a day.  An expected
            # time-to-sale, when the model has one, says it directly.
            hours = float(getattr(estimate, "expected_time_hours", None) or 0.0)
            if hours > 0:
                sold_per_day = 24.0 / hours
            else:
                horizon = float(getattr(estimate, "horizon_h", None)
                                or getattr(engine.config, "horizon_h", 24) or 24)
                sold_per_day = probability * 24.0 / horizon
            counts["estimate"] += 1
        else:
            try:
                shipped = engine.estimate_with_shipped_model(realm, item_id)
            except Exception:
                shipped = None
            if shipped:
                price = float(shipped)
                counts["shipped"] += 1
        # A guard, not a measurement: an EV that prices a transmog at 2 % (or
        # 1100 %) of the regional reference is a model extrapolation, not a
        # farm.  The reference is used only to bound it, and the clamp is
        # counted so the report can say it happened.
        reference = fallback.get(item_id)
        if price and price > 0 and reference and reference.price > 0:
            low, high = reference.price * sane_ratio[0], reference.price * sane_ratio[1]
            if price < low or price > high:
                price = min(high, max(low, price))
                counts["clamped"] += 1
        if price and price > 0:
            out[item_id] = ItemMarket(
                item_id=item_id, name=names.get(item_id, ""), price=price,
                sale_rate=probability, sold_per_day=sold_per_day)
        elif item_id in fallback:
            out[item_id] = fallback[item_id]
            counts["fallback"] += 1
        else:
            counts["unpriced"] += 1
    return out, counts


@dataclass
class MarketSource:
    """Where a ranking's prices came from, and how much of it they covered."""

    market: Dict[int, ItemMarket]
    source: str
    label: str
    asof: str
    counts: Dict[str, int]

    @property
    def age_days(self) -> Optional[int]:
        return price_age_days(self.asof)

    @property
    def stale(self) -> bool:
        age = self.age_days
        return age is not None and age > 30


def _model_artifact() -> Optional[Path]:
    """The trained price model, if it is there to read."""
    try:
        from woweconomy.pricing.config import PricingConfig

        path = PricingConfig().models_dir / "price_model.cbm"
    except Exception:
        return None
    return path if path.exists() else None


def resolve_market(item_ids, *, names: Optional[Dict[int, str]] = None,
                   source: str = "auto", db_path: str = "",
                   csv_path: str = "csv/items.csv",
                   engine=None) -> MarketSource:
    """The one place that decides where a farm ranking's prices come from.

    There were three: the zone report chose a source inline, the leaderboards
    read the TSM table directly, and `scripts/rank_farms.py` estimated its own
    way.  Three answers to "what is this worth" is how two reports about the
    same zone end up disagreeing, so the decision lives here and both callers
    share it.

    ``auto`` prefers the trained model.  A model answers with a realizable sale
    price *and* the chance of selling; a TSM average is a listing reference
    that says nothing about whether anyone buys.  The market table stays as the
    per-item fallback for what the model cannot price, and the counts say how
    much of the ranking rests on which path -- a report that implied the model
    priced everything would be lying about its own coverage.
    """
    ids = [int(i) for i in item_ids]
    names = names or {}
    resolved = source
    if resolved == "auto":
        resolved = ("model" if _model_artifact() is not None
                    else ("region" if db_path else "csv"))

    if resolved == "csv":
        return MarketSource(load_csv_market(csv_path), "csv",
                            "csv:" + csv_path, market_asof_csv(csv_path), {})

    if resolved == "model":
        if engine is None:
            from woweconomy.pricing.config import PricingConfig
            from woweconomy.pricing.engine import PricingEngine

            engine = PricingEngine(PricingConfig()).load()
        fallback = load_region_market(db_path) if db_path else {}
        mkt, counts = load_model_market(engine, ids, fallback=fallback,
                                        names=names)
        label = (f"model ({counts['estimate']} estimate, "
                 f"{counts['shipped']} shipped, {counts['fallback']} fallback, "
                 f"{counts['unpriced']} unpriced)")
        return MarketSource(mkt, "model", label, "", counts)

    if not db_path:
        raise SystemExit(f"--price-source {resolved} needs a --db path")
    mkt = (load_snapshot_market(db_path) if resolved == "legacy"
           else load_region_market(db_path))
    return MarketSource(mkt, resolved, f"db:{resolved}:{db_path}",
                        market_asof_db(db_path, resolved), {})


def price_age_days(iso_ts: str, today: Optional[object] = None) -> Optional[int]:
    """Age of an ISO-8601 snapshot timestamp in days (None if unparseable)."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    now = today or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, (now - ts).days)


def market_asof_csv(path: str) -> str:
    """Newest updatedAt in the CSV ('' when unreadable)."""
    import csv
    try:
        with open(path, encoding="utf-8") as f:
            vals = [r.get("updatedAt", "") for r in csv.DictReader(f)]
    except OSError:
        return ""
    return max((v for v in vals if v), default="")


def market_asof_db(db_path: str, source: str = "region") -> str:
    """The vintage of the prices the DB holds ('' when unreadable).

    The default is the live regional table's ``updated_at``, because that
    is the source the report now prices with; ``source="legacy"`` reports
    the frozen snapshot's vintage instead.
    """
    if source == "region":
        candidates = ("SELECT MAX(updated_at) FROM tsm_region_price",
                      "SELECT MAX(snapshotTimestamp) FROM snapshots")
    else:
        candidates = ("SELECT MAX(snapshotTimestamp) FROM snapshots",)
    from woweconomy.dbcompat import connect_store, missing_table_errors

    for sql in candidates:
        try:
            conn = connect_store(db_path, readonly=True)
            try:
                row = conn.execute(sql).fetchone()
            finally:
                conn.close()
        except missing_table_errors():
            continue
        if row and row[0]:
            return row[0]
    return ""


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------

def liquidity_weight(sale_rate: float) -> float:
    """TSM DBRegionSaleRate used as-is: fraction of listed region auctions
    that sell (verified 0-1 decimal scale, dataset max exactly 1.0).

    Applied as the expected revenue fraction per listing round: list your
    loot like the market and ~`sale_rate` of its value sells per round.
    This is a LIQUIDITY weight, not a time-based probability — there is no
    horizon, no compounding, no days-to-sale.  NaN/negatives -> 0.
    """
    try:
        sr = float(sale_rate or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(sr):
        return 0.0
    return min(max(sr, 0.0), 1.0)


def realization_weight(m: ItemMarket) -> float:
    """Liquidity weight with thin-market haircut: listings with zero recorded
    daily volume (spd == 0) keep 10% weight — a high price nobody was seen
    buying is weak evidence.  Documented assumption, not data."""
    return liquidity_weight(m.sale_rate) * (1.0 if m.sold_per_day > 0 else 0.1)


def freshness_factor(age_days: Optional[int]) -> float:
    """Price-freshness multiplier for DATA_CONFIDENCE: 1/(1+age/30).

    Fresh (<=1d) ~= 0.97, month-old ~= 0.5, 79d stale ~= 0.27.
    Unknown age -> 0.5 (absence of provenance is itself weak evidence).
    """
    if age_days is None:
        return 0.5
    return 1.0 / (1.0 + max(age_days, 0) / 30.0)


def list_price(m: ItemMarket, vendor_copper: float,
               r_cfg: Optional[FarmRankingConfig] = None) -> float:
    """Effective list price: avgSalePrice x (base + sr x rate_mult),
    floored at vendor x floor_mult when enabled.  Defaults (1.0 / 0.0 /
    0.0) reproduce the reference price exactly (no silent change).

    ``r_cfg`` defaults rather than raising: every ranking function already
    takes it as optional, and ``top_mogs`` passed its own ``None`` straight
    through here, which crashed with "'NoneType' object has no attribute
    'price_base_mult'" for any caller that did not supply a config.
    """
    r_cfg = r_cfg or FarmRankingConfig()
    target = m.price * (r_cfg.price_base_mult + m.sale_rate * r_cfg.price_rate_mult)
    if r_cfg.price_vendor_floor_mult > 0 and vendor_copper > 0:
        target = max(target, vendor_copper * r_cfg.price_vendor_floor_mult)
    return max(target, 0.0)


def eff_net(m: ItemMarket, b: Optional[BlizzardItem], cfg: WowEconomyConfig,
            r_cfg: Optional[FarmRankingConfig] = None) -> float:
    """Estimated auction proceeds per unit after fee, on the EFFECTIVE
    list price (reference avgSalePrice adjusted by the pricing multiplier
    and vendor floor)."""
    vendor = float(b.sell_price if b else 0)
    return max(list_price(m, vendor, r_cfg), 0.0) * (1.0 - cfg.ah_cut_pct)


def score_npcs(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[NpcFarm]:
    """Aggregate drop rows into per-NPC expected value per kill."""
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    by_npc: Dict[int, List[NpcDrop]] = {}
    for d in drops:
        by_npc.setdefault(d.npc_id, []).append(d)

    farms: List[NpcFarm] = []
    for npc_id, rows in by_npc.items():
        contribs: List[DropContrib] = []
        unpriced = 0
        for d in rows:
            if d.drop_rate <= 0:
                continue
            if blizz_excluded(d, blizz, r_cfg):
                continue
            m = market.get(d.item_id)
            if m is None or m.price <= 0:
                unpriced += 1
                continue
            b = blizz.get(d.item_id)
            net = eff_net(m, b, cfg, r_cfg)
            exp = d.drop_rate * d.qty * net
            w = realization_weight(m)
            vend = d.drop_rate * d.qty * float(b.sell_price if b else 0)
            contribs.append(DropContrib(
                item_id=d.item_id, item_name=d.item_name or m.name,
                category=d.category,
                drop_rate=d.drop_rate, qty=d.qty,
                unit_price=m.price,
                net_value=net, expected=exp,
                realizable=exp * w,
                vendor_value=vend,
                sale_rate=m.sale_rate, sold_per_day=m.sold_per_day,
            ))
        contribs.sort(key=lambda c: c.realizable, reverse=True)
        ev = sum(c.expected for c in contribs)
        rev = sum(c.realizable for c in contribs)
        vend_sum = sum(c.vendor_value for c in contribs)
        evidence = (sum(c.realizable for c in contribs if c.sold_per_day > 0) / rev
                    if rev > 0 else 0.0)
        conf = evidence * freshness_factor(r_cfg.price_age_days)
        # Jackpot = biggest single-drop hit by sale-weighted value.
        best_raw = max(contribs, key=lambda c: c.realizable, default=None)
        farms.append(NpcFarm(
            npc_id=npc_id, npc_name=rows[0].npc_name,
            zone=rows[0].zone, zone_name=rows[0].zone_name,
            ev_per_kill=ev, realizable_ev=rev,
            vendor_per_kill=vend_sum, confidence=conf,
            n_drops_valued=len(contribs), n_drops_unpriced=unpriced,
            top_drops=contribs[:5],
            jackpot_item=best_raw.item_name if best_raw else "",
            jackpot_value=best_raw.realizable if best_raw else 0.0,
            jackpot_rate=best_raw.drop_rate if best_raw else 0.0,
            jackpot_qty=best_raw.qty if best_raw else 1.0,
            spawns=rows[0].spawns,
        ))
    farms.sort(key=lambda f: f.realizable_ev, reverse=True)
    return farms


def top_jackpots(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 10,
    min_rate: float = 0.0005,
    max_rate: float = 0.10,
    min_spd: float = 0.0,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[JackpotHit]:
    """Lottery tickets: rare (<10%) drops of pricey items that ACTUALLY sell.

    Junk-typed items and listings with no recorded sales (spd == 0) are out —
    a 26k listing that never moves is not a jackpot.
    """
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    # Best rate observed per (npc, item).
    best: Dict[tuple, NpcDrop] = {}
    for d in drops:
        if not (min_rate <= d.drop_rate <= max_rate):
            continue
        if d.item_type.strip().lower() in JUNK_TYPES:
            continue
        if blizz_excluded(d, blizz, r_cfg):
            continue
        key = (d.npc_id, d.item_id)
        if key not in best or d.drop_rate > best[key].drop_rate:
            best[key] = d
    hits: List[JackpotHit] = []
    for d in best.values():
        m = market.get(d.item_id)
        if m is None or m.price <= 0 or m.sold_per_day <= min_spd:
            continue
        net = eff_net(m, blizz.get(d.item_id), cfg, r_cfg)
        p = min(max(d.drop_rate, 0.0), 1.0)
        b = blizz.get(d.item_id)
        hits.append(JackpotHit(
            npc_id=d.npc_id, npc_name=d.npc_name, zone_name=d.zone_name,
            item_id=d.item_id, item_name=d.item_name or m.name,
            drop_rate=d.drop_rate, qty=d.qty,
            unit_price=m.price, net_value=net,
            expected=d.drop_rate * d.qty * net
            * realization_weight(m),
            p_drop_100=1.0 - (1.0 - p) ** 100,
            sale_rate=m.sale_rate, sold_per_day=m.sold_per_day,
            vendor_value=float(b.sell_price if b else 0),
        ))
    hits.sort(key=lambda h: h.expected, reverse=True)
    return hits[:top_n]


def top_mogs(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 10,
    max_drop_rate: float = 0.05,
    min_spd: float = 0.0,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[MogCandidate]:
    """Top mog ITEMS by avgSalePrice: rare, appearance-like, and SELLING.

    One row per item (best farm source kept).  Junk-typed items and listings
    with no recorded sales are out.  Returns the FULL ranked list; slicing
    is the caller's job (extra views re-sort it).
    """
    cfg = cfg or WowEconomyConfig.default()
    best_src: Dict[int, NpcDrop] = {}
    for d in drops:
        if d.drop_rate > max_drop_rate or d.drop_rate <= 0:
            continue
        if not _looks_like_mog(d.item_name):
            continue
        if d.item_type.strip().lower() in JUNK_TYPES:
            continue
        if blizz_excluded(d, blizz or {}, r_cfg or FarmRankingConfig()):
            continue
        cur = best_src.get(d.item_id)
        if cur is None or d.drop_rate > cur.drop_rate:
            best_src[d.item_id] = d
    cands: List[MogCandidate] = []
    for d in best_src.values():
        m = market.get(d.item_id)
        if m is None or m.price <= 0 or m.sale_rate <= 0 or m.sold_per_day <= min_spd:
            continue
        b = (blizz or {}).get(d.item_id)
        net = eff_net(m, b, cfg, r_cfg)
        cands.append(MogCandidate(
            item_id=d.item_id, item_name=d.item_name or m.name,
            npc_id=d.npc_id, npc_name=d.npc_name, zone_name=d.zone_name,
            drop_rate=d.drop_rate, unit_price=m.price, net_value=net,
            sale_rate=m.sale_rate, score=m.price,
            ev_weighted=(d.drop_rate * d.qty * net
                         * realization_weight(m)),
            vendor_value=float(b.sell_price if b else 0),
        ))
    cands.sort(key=lambda c: c.score, reverse=True)
    return cands


def top_mog_farms(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 10,
    max_drop_rate: float = 0.05,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[MogFarm]:
    """NPCs ranked by joint sell-through of what you plausibly LOOT.

    Old flaw: P(any sale) over 100+ mogs saturated near 100% because it
    assumed every mog listed.  Now each item contributes only through
    P(acquire in `mog_acquire_kills` kills) x sale weight - a 0.0001% drop
    you will likely never see barely moves P.  No time claim: days N/A.
    """
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    by_npc: Dict[int, list] = {}
    for d in drops:
        if d.drop_rate > max_drop_rate or d.drop_rate <= 0:
            continue
        if not _looks_like_mog(d.item_name):
            continue
        if d.item_type.strip().lower() in JUNK_TYPES:
            continue
        if blizz_excluded(d, blizz, r_cfg):
            continue
        m = market.get(d.item_id)
        if m is None or m.price <= 0 or m.sale_rate <= 0:
            continue
        by_npc.setdefault(d.npc_id, []).append((d, m))
    out: List[MogFarm] = []
    for npc_id, pairs in by_npc.items():
        # one row per item: best rate observed
        best: Dict[int, tuple] = {}
        for d, m in pairs:
            cur = best.get(d.item_id)
            if cur is None or d.drop_rate > cur[0].drop_rate:
                best[d.item_id] = (d, m)
        p_none = 1.0
        portfolio = 0.0
        realiz = 0.0
        for d, m in best.values():
            # only what you plausibly loot: P(acquire) x sale weight
            p = min(max(d.drop_rate, 0.0), 1.0)
            p_acquire = 1.0 - (1.0 - p) ** r_cfg.mog_acquire_kills
            w = realization_weight(m)
            p_none *= max(0.0, 1.0 - p_acquire * w)
            portfolio += m.price
            realiz += (d.drop_rate * d.qty
                       * eff_net(m, blizz.get(d.item_id), cfg, r_cfg) * w)
        p = 1.0 - p_none
        top = sorted(((d.item_name or m.name, d.drop_rate, m.price)
                      for d, m in best.values()),
                     key=lambda t: t[2], reverse=True)[:3]
        d0 = pairs[0][0]
        out.append(MogFarm(
            npc_id=npc_id, npc_name=d0.npc_name, zone_name=d0.zone_name,
            n_mogs=len(best), portfolio_value=portfolio,
            realizable_ev=realiz,
            p_cycle_sale=p, days_to_sale=None,
            top_items=top,
        ))
    out.sort(key=lambda f: (f.p_cycle_sale, f.portfolio_value), reverse=True)
    return out[:top_n]


RECIPE_WORDS = (
    "recipe", "plans:", "pattern:", "schematic:", "design:", "formula:",
    "manual", "tome", "codex", "grimoire",
)


def _jackpot_row(d: NpcDrop, m: ItemMarket, cfg: WowEconomyConfig,
                 r_cfg: FarmRankingConfig,
                 blizz: Optional[Dict[int, BlizzardItem]] = None) -> JackpotHit:
    net = eff_net(m, (blizz or {}).get(d.item_id), cfg, r_cfg)
    p = min(max(d.drop_rate, 0.0), 1.0)
    b = (blizz or {}).get(d.item_id)
    return JackpotHit(
        npc_id=d.npc_id, npc_name=d.npc_name, zone_name=d.zone_name,
        item_id=d.item_id, item_name=d.item_name or m.name,
        drop_rate=d.drop_rate, qty=d.qty,
        unit_price=m.price, net_value=net,
        expected=(d.drop_rate * d.qty * net
                  * realization_weight(m)),
        p_drop_100=1.0 - (1.0 - p) ** 100,
        sale_rate=m.sale_rate, sold_per_day=m.sold_per_day,
        vendor_value=float(b.sell_price if b else 0),
    )


def top_recipes(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 10,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[JackpotHit]:
    """Profession recipes/patterns by sale-weighted EV (name-based class)."""
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    best: Dict[tuple, NpcDrop] = {}
    for d in drops:
        nl = d.item_name.lower()
        if d.drop_rate <= 0 or not any(w in nl for w in RECIPE_WORDS):
            continue
        if blizz_excluded(d, blizz, r_cfg):
            continue
        key = (d.npc_id, d.item_id)
        if key not in best or d.drop_rate > best[key].drop_rate:
            best[key] = d
    hits = []
    for d in best.values():
        m = market.get(d.item_id)
        if m is None or m.price <= 0:
            continue
        hits.append(_jackpot_row(d, m, cfg, r_cfg, blizz))
    hits.sort(key=lambda h: h.expected, reverse=True)
    return hits[:top_n]


def top_boe(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 10,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[JackpotHit]:
    """Tradeable equipment, verified by the client DB2 (binding == ON_EQUIP).

    No name guessing: if neither the DB2 nor Blizzard confirms Bind-on-Equip,
    the item is not listed here.  With the DB2 loaded this now covers the whole
    item table instead of the few hundred ids the API was asked about.
    """
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    best: Dict[tuple, NpcDrop] = {}
    for d in drops:
        if d.drop_rate <= 0:
            continue
        b = blizz.get(d.item_id)
        if b is None or b.binding != "ON_EQUIP":
            continue
        key = (d.npc_id, d.item_id)
        if key not in best or d.drop_rate > best[key].drop_rate:
            best[key] = d
    hits = []
    for d in best.values():
        m = market.get(d.item_id)
        if m is None or m.price <= 0 or m.sold_per_day <= 0:
            continue
        hits.append(_jackpot_row(d, m, cfg, r_cfg, blizz))
    hits.sort(key=lambda h: h.expected, reverse=True)
    return hits[:top_n]


@dataclass
class MixedFarm:
    npc_id: int
    npc_name: str
    zone_name: str
    pillars: int = 0  # of {vendor, mats, mogs} with real EV
    vendor_ev: float = 0.0
    mat_ev: float = 0.0
    mog_ev: float = 0.0
    total_ev: float = 0.0


def top_mixed(
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    top_n: int = 10,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
) -> List[MixedFarm]:
    """NPCs paying from >= 2 pillars (vendor / materials / mogs)."""
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    agg: Dict[int, list] = {}  # npc_id -> [name, zone, vendor, mat, mog]
    for d in drops:
        if d.drop_rate <= 0 or blizz_excluded(d, blizz, r_cfg):
            continue
        m = market.get(d.item_id)
        if m is None or m.price <= 0:
            continue
        net = eff_net(m, blizz.get(d.item_id), cfg, r_cfg)
        w = realization_weight(m)
        row = agg.setdefault(d.npc_id, [d.npc_name, d.zone_name, 0.0, 0.0, 0.0])
        b = blizz.get(d.item_id)
        row[2] += d.drop_rate * d.qty * float(b.sell_price if b else 0)
        ev = d.drop_rate * d.qty * net * w
        if mat_class(d.item_name) != "OTHER":
            row[3] += ev
        elif _looks_like_mog(d.item_name):
            row[4] += ev
    out = []
    for nid, (name, zone, vend, mat, mog) in agg.items():
        pillars = sum(1 for v in (vend, mat, mog) if v > 0)
        if pillars >= 2:
            out.append(MixedFarm(npc_id=nid, npc_name=name, zone_name=zone,
                                 pillars=pillars, vendor_ev=vend,
                                 mat_ev=mat, mog_ev=mog,
                                 total_ev=vend + mat + mog))
    out.sort(key=lambda f: f.total_ev, reverse=True)
    return out[:top_n]


@dataclass
class SessionPlan:
    """BEST GOLDBOUND SESSION answer for one time budget + scope.

    vendor_gold:      received on the spot at a vendor (no AH, no waiting).
    auction_est:      sale-weighted estimate (baseline, NOT guaranteed).
    speculative_gold: gross minus weighted (may never sell).
    conservative_gold: floor = vendor + fast movers (sr >= 5%) only.
    """
    scope: str            # "route (3 stops)" or "solo: <npc>"
    minutes: float = 0.0
    kills: float = 0.0
    vendor_gold: float = 0.0      # immediate, no AH
    auction_est: float = 0.0      # sale-weighted estimate, NOT guaranteed
    speculative_gold: float = 0.0  # raw - weighted: may never sell
    conservative_gold: float = 0.0  # vendor + fast movers (sr >= 5%) only


def _fast_ev_for_npcs(npc_ids: set[int], drops: List[NpcDrop],
                      market: Dict[int, ItemMarket], cfg: WowEconomyConfig,
                      blizz: Dict[int, BlizzardItem],
                      r_cfg: FarmRankingConfig) -> float:
    total = 0.0
    for d in drops:
        if d.npc_id not in npc_ids or d.drop_rate <= 0:
            continue
        if blizz_excluded(d, blizz, r_cfg):
            continue
        m = market.get(d.item_id)
        if m is None or m.price <= 0 or m.sale_rate < 0.05:
            continue
        total += (d.drop_rate * d.qty * m.net_value(cfg.ah_cut_pct)
                  * realization_weight(m))
    return total / max(len(npc_ids), 1)


def build_sessions(
    rotation: FarmRotation,
    farms_all: List[NpcFarm],
    drops: List[NpcDrop],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
    minutes_list: tuple = (15, 30, 60),
) -> List[SessionPlan]:
    """Where to spend the next 15/30/60 minutes: route vs best solo NPC.

    Assumptions (exposed, not hidden): kills spread evenly over stops;
    travel costs travel_sec_per_stop per stop per full cycle; a cycle
    visits every stop once.  Single-NPC farm assumes no travel and no
    hyperspawn throttle (optimistic — the route exists to dodge it).
    """
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    kpm = r_cfg.kills_per_minute
    plans: List[SessionPlan] = []
    scopes: list[tuple[str, set[int]]] = []
    if rotation.stops:
        route_ids = {i for s in rotation.stops for i in s.npc_ids}
        scopes.append((f"route ({len(rotation.stops)} stops)", route_ids))
    if farms_all:
        top = farms_all[0]
        scopes.append((f"solo: {top.npc_name or top.npc_id}", {top.npc_id}))
    by_npc_farm = {f.npc_id: f for f in farms_all}
    for label, ids in scopes:
        members = [by_npc_farm[i] for i in ids if i in by_npc_farm]
        if not members:
            continue
        n = len(members)
        avg_vend = sum(f.vendor_per_kill for f in members) / n
        avg_real = sum(f.realizable_ev for f in members) / n
        avg_raw = sum(f.ev_per_kill for f in members) / n
        avg_fast = _fast_ev_for_npcs(ids, drops, market, cfg, blizz, r_cfg)
        travel_min = (len(rotation.stops) * r_cfg.travel_sec_per_stop / 60.0
                      if label.startswith("route") else 0.0)
        for minutes in minutes_list:
            kill_min = max(0.0, minutes - travel_min)
            kills = kill_min * kpm
            plans.append(SessionPlan(
                scope=label, minutes=float(minutes), kills=kills,
                vendor_gold=kills * avg_vend,
                auction_est=kills * avg_real,
                speculative_gold=kills * max(avg_raw - avg_real, 0.0),
                conservative_gold=kills * (avg_vend + avg_fast),
            ))
    return plans


def farm_profile(f: NpcFarm) -> str:
    """Income profile for route planning.

    JACKPOT: one rare hit dominates (>=50% of realizable EV).
    STEADY:  no dominant hit (jackpot <=20% of EV) — regular income.
    MIXED:   in between. THIN: nothing worth killing for.
    """
    if f.realizable_ev <= 0:
        return "THIN"
    share = f.jackpot_value / f.realizable_ev if f.realizable_ev else 0.0
    if share >= 0.5:
        return "JACKPOT"
    if share <= 0.2:
        return "STEADY"
    return "MIXED"


def build_rotation(
    farms: List[NpcFarm],
    stops: int = 3,
    top_pool: int = 9,
) -> FarmRotation:
    """Cycle distinct NPCs across `stops` camps.

    Anti-hyperspawn logic: grinding one spot non-stop gets loot-throttled,
    so the farmer rotates camps and each camp is revisited only after
    `stops - 1` other camps.  NPCs here are a proxy for camps until
    coordinate data lands — then cluster geographically instead.
    """
    pool = [f for f in farms if f.realizable_ev > 0][:max(top_pool, stops)]
    if not pool:
        return FarmRotation()
    per_stop = max(1, len(pool) // stops)
    out: List[FarmStop] = []
    for s in range(stops):
        chunk = pool[s * per_stop:(s + 1) * per_stop] or pool[s:s + 1]
        if not chunk:
            break
        out.append(FarmStop(
            stop=s + 1,
            npc_ids=[f.npc_id for f in chunk],
            npc_names=[f.npc_name or f"npc={f.npc_id}" for f in chunk],
            stop_ev_per_kill=sum(f.realizable_ev for f in chunk) / len(chunk),
        ))
    return FarmRotation(stops=out, revisit_after_stops=len(out))


def rank_zone(
    zone_paths: List[Path],
    market: Dict[int, ItemMarket],
    cfg: Optional[WowEconomyConfig] = None,
    r_cfg: Optional[FarmRankingConfig] = None,
    blizz: Optional[Dict[int, BlizzardItem]] = None,
) -> ZoneRanking:
    """Full ranking for one (or several merged) zone files."""
    cfg = cfg or WowEconomyConfig.default()
    r_cfg = r_cfg or FarmRankingConfig()
    blizz = blizz or {}
    drops = load_zone_drops(zone_paths)
    reasons = [item_exclusion_reason(d.item_id, blizz, r_cfg) for d in drops]
    excluded = sum(1 for r in reasons if r)
    farms_all = score_npcs(drops, market, cfg, blizz, r_cfg)
    if r_cfg.sort_by == "jackpot":
        farms_all = sorted(farms_all, key=lambda f: f.jackpot_value, reverse=True)
    zones = {d.zone for d in drops}
    zone = farms_all[0].zone if farms_all else 0
    zone_name = farms_all[0].zone_name if farms_all else ""
    if len(zones) > 1:
        zone_name = f"{len(zones)} zones"
        zone = 0
    all_mogs = top_mogs(drops, market, cfg, len(drops), r_cfg.mog_max_drop_rate,
                        r_cfg.min_spd_for_mog_jackpot, blizz, r_cfg)
    rotation = build_rotation(farms_all, r_cfg.rotation_stops)
    return ZoneRanking(
        zone=zone, zone_name=zone_name,
        npc_count=len({d.npc_id for d in drops}),
        farms=farms_all[:r_cfg.top_n],
        mogs=all_mogs[:r_cfg.top_n],
        mog_ev=sorted(all_mogs, key=lambda c: c.ev_weighted,
                      reverse=True)[:r_cfg.top_n],
        mog_fast=[c for c in all_mogs if c.sale_rate >= 0.05][:r_cfg.top_n],
        mog_thin=[c for c in all_mogs if c.sale_rate < 0.01][:r_cfg.top_n],
        mog_vendor=sorted(all_mogs, key=lambda c: c.vendor_value,
                          reverse=True)[:r_cfg.top_n],
        jackpots=top_jackpots(drops, market, cfg, r_cfg.top_n,
                              r_cfg.jackpot_min_rate, r_cfg.jackpot_max_rate,
                              r_cfg.min_spd_for_mog_jackpot, blizz, r_cfg),
        rotation=rotation,
        excluded_blizz=excluded,
        excluded_era=reasons.count("era"),
        excluded_bop=reasons.count("bop"),
        excluded_junk=reasons.count("junk"),
        mats=top_mats(drops, market, cfg, r_cfg.top_n, blizz, r_cfg),
        mog_farms=top_mog_farms(drops, market, cfg, r_cfg.top_n,
                                r_cfg.mog_max_drop_rate, blizz, r_cfg),
        expensive=top_expensive(drops, market, cfg, r_cfg.top_n, blizz, r_cfg),
        vendor_farms=sorted(farms_all, key=lambda f: f.vendor_per_kill,
                            reverse=True)[:r_cfg.top_n],
        recipes=top_recipes(drops, market, cfg, r_cfg.top_n, blizz, r_cfg),
        boe=top_boe(drops, market, cfg, r_cfg.top_n, blizz, r_cfg),
        mixed=top_mixed(drops, market, cfg, r_cfg.top_n, blizz, r_cfg),
        sessions=build_sessions(rotation, farms_all, drops, market, cfg,
                                blizz, r_cfg,
                                minutes_list=tuple(r_cfg.session_minutes)),
    )


def _g(copper: float) -> str:
    """TSM values are copper: format as gold."""
    return f"{copper / 10_000:,.0f}g"


def _p(rate: float) -> str:
    """Drop rate with precision for tiny values (0.004% not 0.00%)."""
    if rate > 0 and rate < 0.0001:
        return f"{rate * 100:.4f}%"
    if rate > 0 and rate < 0.01:
        return f"{rate * 100:.3f}%"
    return f"{rate:6.2%}"


def print_report(r: ZoneRanking, kills_per_minute: float = 20.0) -> None:
    gph = kills_per_minute * 60
    print(f"\n=== {r.zone_name or r.zone} — {r.npc_count} NPCs ===")
    print("\n-- TOP FARMS (EV gross | real = gross x sell-through, per kill) --")
    print("   conf = evidence share x price freshness (coverage, NOT a sale P)")
    for i, f in enumerate(r.farms, 1):
        jp = f" JP={f.jackpot_item[:28]:28s} {_g(f.jackpot_value)}" if f.jackpot_item else ""
        print(f"{i:2d}. {f.npc_name or f.npc_id} [{f.npc_id}] "
              f"EV={_g(f.ev_per_kill)} real={_g(f.realizable_ev)} "
              f"({_g(f.realizable_ev * gph)}/h) drops={f.n_drops_valued} "
              f"vendor={_g(f.vendor_per_kill)} conf={f.confidence:.0%}{jp}")
        for c in f.top_drops[:3]:
            print(f"      {_p(c.drop_rate):>7s} x{c.qty:4.1f} [{c.category[:4]}] "
                  f"{c.item_name[:30]:30s} u={_g(c.unit_price):>9s} "
                  f"EV={_g(c.realizable)}")
    print("\n-- TOP MOGS (one row per item, best farm source) --")
    for i, m in enumerate(r.mogs, 1):
        src = f"{m.npc_name} [{m.npc_id}]" if m.npc_name else f"npc={m.npc_id}"
        print(f"{i:2d}. {m.item_name[:40]:40s} {_p(m.drop_rate):>7s} "
              f"u={_g(m.unit_price):>9s} <= {src}")
    print("\n-- TOP JACKPOTS (rare <10%, pricey, actually selling) --")
    for i, j in enumerate(r.jackpots, 1):
        print(f"{i:2d}. [{j.item_id}] {j.item_name[:30]:30s} {_p(j.drop_rate):>7s} "
              f"x{j.qty:4.1f} u={_g(j.unit_price):>9s} E={_g(j.expected)} "
              f"Pdrop100={j.p_drop_100:.0%} sr={j.sale_rate:.1%} "
              f"spd={j.sold_per_day:.1f} vend={_g(j.vendor_value)} "
              f"({j.npc_name or 'npc'} [{j.npc_id}])")
    print(f"\n-- ROTATION ({len(r.rotation.stops)} camps, "
          f"revisit after {r.rotation.revisit_after_stops} stops) --")
    for s in r.rotation.stops:
        print(f"Camp {s.stop}: {', '.join(s.npc_names)} "
              f"(avg real EV {_g(s.stop_ev_per_kill)}/kill)")
    print("\n-- TOP MATS (units/kill per NPC) --")
    for i, t in enumerate(r.mats, 1):
        print(f"{i:2d}. {t.mat:8s} {t.units_per_kill:7.2f}u/kill "
              f"EV {_g(t.ev_per_kill):>9s} <= {t.npc_name or t.npc_id} "
              f"({t.zone_name})")
    print("\n-- TOP MONTRAS (biggest showcase: portfolio value if all listed) --")
    for i, f in enumerate(sorted(r.mog_farms, key=lambda f: f.portfolio_value,
                                 reverse=True)[:10], 1):
        print(f"{i:2d}. {f.npc_name or f.npc_id} [{f.npc_id}] "
              f"{f.n_mogs} mogs montra={_g(f.portfolio_value)} "
              f"realiz={_g(f.realizable_ev)} P={f.p_cycle_sale:.1%}")
        for name, rate, unit in f.top_items:
            print(f"      {_p(rate):>7s} {name[:38]:38s} u={_g(unit):>9s}")
    print("\n-- TOP ITENS (most expensive, best source + evidence) --")
    for i, t in enumerate(r.expensive, 1):
        src = f"{t.npc_name} [{t.npc_id}]" if t.npc_name else f"npc={t.npc_id}"
        print(f"{i:2d}. [{t.item_id}] {t.item_name[:32]:32s} "
              f"u={_g(t.unit_price):>9s} sr={t.sale_rate:.1%} "
              f"spd={t.sold_per_day:.1f} <= {src} ({t.zone_name})")
    print("\n-- TOP MOG FARMS (montra value vs realizable EV vs P(any sells)) --")
    for i, f in enumerate(r.mog_farms, 1):
        print(f"{i:2d}. {f.npc_name or f.npc_id} [{f.npc_id}] "
              f"{f.n_mogs} mogs montra={_g(f.portfolio_value)} "
              f"realiz={_g(f.realizable_ev)} "
              f"P={f.p_cycle_sale:.1%} days=n/a")
        for name, rate, unit in f.top_items:
            print(f"      {_p(rate):>7s} {name[:38]:38s} u={_g(unit):>9s}")
    if r.price_asof:
        stale = " STALE - refresh prices" if r.price_stale else ""
        print(f"\nPrices as of {r.price_asof}{stale}")
    print("\n-- TOP VENDOR FARMS (immediate gold, no AH) --")
    for i, f in enumerate(r.vendor_farms, 1):
        if f.vendor_per_kill <= 0:
            continue
        print(f"{i:2d}. {f.npc_name or f.npc_id} [{f.npc_id}] "
              f"{_g(f.vendor_per_kill)}/kill")
    print("\n-- TOP RECIPES (profession collectibles, sale-weighted EV) --")
    for i, j in enumerate(r.recipes, 1):
        print(f"{i:2d}. {j.item_name[:40]:40s} {_p(j.drop_rate):>7s} "
              f"u={_g(j.unit_price):>9s} E={_g(j.expected)} "
              f"({j.npc_name or j.npc_id})")
    print("\n-- TOP BOE (DB2-verified Bind-on-Equip, sale-weighted EV) --")
    for i, j in enumerate(r.boe, 1):
        print(f"{i:2d}. {j.item_name[:40]:40s} {_p(j.drop_rate):>7s} "
              f"u={_g(j.unit_price):>9s} E={_g(j.expected)} "
              f"({j.npc_name or j.npc_id})")
    print("\n-- TOP MIXED (>=2 pillars: vendor / mats / mogs) --")
    for i, f in enumerate(r.mixed, 1):
        print(f"{i:2d}. {f.npc_name or f.npc_id} [{f.npc_id}] "
              f"total={_g(f.total_ev)} "
              f"(vendor={_g(f.vendor_ev)} mats={_g(f.mat_ev)} mogs={_g(f.mog_ev)})")
    print("\n-- MOG VIEWS --")
    print("  by EV/kill:")
    for i, m in enumerate(r.mog_ev, 1):
        print(f"  {i:2d}. {m.item_name[:40]:40s} E={_g(m.ev_weighted)} "
              f"u={_g(m.unit_price):>9s} <= {m.npc_name or m.npc_id}")
    print("  fast movers (sale_rate >= 5%):")
    for i, m in enumerate(r.mog_fast, 1):
        print(f"  {i:2d}. {m.item_name[:40]:40s} sr={m.sale_rate:.1%} "
              f"u={_g(m.unit_price):>9s} <= {m.npc_name or m.npc_id}")
    print("  high-value, low liquidity (sale_rate < 1%):")
    for i, m in enumerate(r.mog_thin, 1):
        print(f"  {i:2d}. {m.item_name[:40]:40s} sr={m.sale_rate:.2%} "
              f"u={_g(m.unit_price):>9s} <= {m.npc_name or m.npc_id}")
    print("  vendor fallback:")
    for i, m in enumerate(r.mog_vendor, 1):
        if m.vendor_value <= 0:
            continue
        print(f"  {i:2d}. {m.item_name[:40]:40s} "
              f"vendor={_g(m.vendor_value)} u={_g(m.unit_price):>9s}")
    print("\n-- BEST GOLDBOUND SESSION (travel-aware, kills spread over stops) --")
    print("   (assumptions: measure YOUR kpm/travel in-game; defaults below)")
    for s in r.sessions:
        print(f"  {s.scope}: {s.minutes:.0f}min -> {s.kills:.0f} kills | "
              f"vendor={_g(s.vendor_gold)} auction_est={_g(s.auction_est)} "
              f"speculative={_g(s.speculative_gold)} conservative={_g(s.conservative_gold)}")


def main(argv=None) -> int:
    """The farm-ranking command line.

    One implementation, called by ``scripts/farm_ranking.py``.  It lives here
    rather than in the script so that the flags and the ranking cannot drift
    apart: there used to be a second entry point at the repo root that
    imported a module which did not exist, caught the error, and wrote a
    report full of "Test Item" placeholders.

        python scripts/farm_ranking.py zone_331.json --db tsm_markets.db
        python scripts/farm_ranking.py zone_331.json --top 20 --out report.txt
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="farm_ranking",
        description="Rank farms for scraped zone files.")
    ap.add_argument("zones", nargs="+", help="wowhead zone JSON file(s)")
    ap.add_argument("--market-csv", default="csv/items.csv",
                    help="CSV market, used when --price-source csv")
    ap.add_argument("--db", default="",
                    help="sqlite with the market data "
                         "(default: tsm_markets.db when it exists)")
    ap.add_argument("--price-source", default="auto",
                    choices=("auto", "model", "region", "legacy", "csv"),
                    help="where the prices come from: auto (default) is the "
                         "live regional table the estimate reads, with the "
                         "frozen market_snapshots only as a fallback; legacy "
                         "and csv force those sources")
    ap.add_argument("--blizz-db", default="tsm_markets.db",
                    help="sqlite with db2_items + blizzard_items (binding, "
                         "quality, era); empty map if missing")
    ap.add_argument("--no-era-filter", action="store_true",
                    help="keep era-Classic rows in the EV (prices from "
                         "another game: off by default)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--stops", type=int, default=3)
    ap.add_argument("--kpm", type=float, default=20.0)
    ap.add_argument("--sort", choices=("ev", "jackpot"), default="ev",
                    help="rank farms by steady EV or best single-drop hit")
    ap.add_argument("--horizon", type=int, default=30,
                    help="DEPRECATED (ignored): sale_rate is per-listing "
                         "sell-through, not a daily probability. Kept so old "
                         "commands still parse.")
    ap.add_argument("--travel-sec", type=float, default=60.0,
                    help="inter-camp travel seconds per full rotation cycle")
    ap.add_argument("--session", type=float, nargs="*", default=[15, 30, 60],
                    help="session lengths in minutes for BEST GOLDBOUND SESSION")
    ap.add_argument("--mog-kills", type=float, default=600.0,
                    help="kills assumed when estimating whether you loot each mog")
    ap.add_argument("--price-base", type=float, default=1.0,
                    help="list-price base multiplier (default 1 = reference price)")
    ap.add_argument("--price-rate", type=float, default=0.0,
                    help="list-price sale-rate multiplier: target = avg*(base+sr*rate)")
    ap.add_argument("--price-floor", type=float, default=0.0,
                    help="list-price floor as multiple of vendor sell (0 = off)")
    ap.add_argument("--out", default="",
                    help="write the full report to this .txt file (utf-8)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    db_path = args.db or ("tsm_markets.db"
                          if Path("tsm_markets.db").exists() else "")
    missing = [z for z in args.zones if not Path(z).exists()]
    if missing:
        print("Missing zone file(s): " + ", ".join(missing))
        print("Scrape them first, e.g.:")
        print('  python -m woweconomy.services.wowhead "zone=331/ashenvale"')
        print("(a partial zone_331.json appears after the first 25 NPCs)")
        raise SystemExit(2)
    drops = load_zone_drops([Path(z) for z in args.zones])
    source = resolve_market(
        {d.item_id for d in drops},
        names={d.item_id: d.item_name for d in drops if d.item_name},
        source=args.price_source, db_path=db_path, csv_path=args.market_csv)
    mkt, asof, source_label = source.market, source.asof, source.label
    age = source.age_days
    print(f"Market: {len(mkt)} priced items "
          f"({source_label}) as of {asof or 'unknown'}"
          + (f" ({age}d old - STALE, refresh the source)" if age is not None and age > 30 else ""))
    if args.horizon != 30:
        print("NOTE: --horizon is deprecated and ignored: DBRegionSaleRate "
              "is per-listing sell-through, not a daily probability.")
    ranking = rank_zone(
        [Path(z) for z in args.zones], mkt,
        WowEconomyConfig.default(),
        FarmRankingConfig(top_n=args.top, rotation_stops=args.stops,
                          sort_by=args.sort,
                          travel_sec_per_stop=args.travel_sec,
                          session_minutes=tuple(args.session),
                          mog_acquire_kills=args.mog_kills,
                          price_age_days=age,
                          price_base_mult=args.price_base,
                          price_rate_mult=args.price_rate,
                          price_vendor_floor_mult=args.price_floor,
                          exclude_era=not args.no_era_filter),
        load_item_facts(args.blizz_db),
    )
    ranking.price_asof = asof
    ranking.price_stale = age is not None and age > 30
    if ranking.excluded_blizz:
        print(f"Item filter: {ranking.excluded_blizz} drop rows out of the EV "
              f"(era-Classic {ranking.excluded_era}, BoP {ranking.excluded_bop}, "
              f"grey {ranking.excluded_junk})")
    if args.out:
        import contextlib
        with open(args.out, "w", encoding="utf-8") as fh:
            with contextlib.redirect_stdout(fh):
                print_report(ranking, kills_per_minute=args.kpm)
        print(f"Report written to {args.out}")
    else:
        print_report(ranking, kills_per_minute=args.kpm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
