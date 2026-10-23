"""Collector demand — cross what you own (or farm) with what collectors lack.

Inputs
    demand_<kind>.csv from the Data for Azeroth scan (services/dataforazeroth):
        kind, item_id, name, source, faction, missing, collectors,
        missing_pct, owned_pct, value, wowhead, note
        One row per catalog entry that at least one sampled account is missing.
        `missing_pct` = share of the sample that does NOT own it (the audience),
        `owned_pct`   = share of all DfA profiles that own it (the competition),
        `value`       = missing_pct x (100 - owned_pct) / 100, i.e. wanted AND
                        rare.  That product is the interesting one: a 100%
                        missing item owned by everybody is worthless.
    your inventory CSV (scripts/inventario.csv: ItemID;Name;...;Quantity)
    wowhead zone JSONs (woweconomy/wowhead_zones/zone_<id>.json)
    latest TSM snapshot (avgSalePrice / saleRate / soldPerDay)

Modes
    inventory  every bag item vs the wishlists
    zone       every drop of one or more zones vs the wishlists
    ids        the zone's item ids as TSM group imports, banded by how much of
               the sample still lacks them (`--cuts 85,65` = three groups,
               `--cuts 85,75,65,55` = five)
    item       ONE item (by name or id) resolved everywhere at once: bag,
               market, wishlists and which NPCs/zone drop it

The join is BY NAME, deliberately.  The three catalogs live in different key
spaces: DfA `mounts.id` is a mountID (not the item that teaches it), `recipes`
.id is a recipeID (not the recipe item), and `appearances.id` is an
appearanceID.  Appearance ids and item ids happen to share the numeric range,
so joining appearances by id "matches" ~80% of a zone's drops by pure
coincidence — a silent false positive.  Names are the only shared key, so the
normaliser below strips the prefixes that differ (`Recipe:`, `Pattern:`,
`Reins of the`) and the rest is joined verbatim.

The item decides its own catalog: a `Recipe: X` item can only be the recipe,
never the appearance of the crafted X (the two names collapse to the same
string after normalisation, which would double-count a transmog you cannot
wear).

What this does NOT claim: `missing_pct` says collectors still need it, not that
they will buy it from you tomorrow.  `sale_rate` / `sold_per_day` (TSM) stay the
only evidence of actual movement, and they ride along in every row so the
reader judges.

Binding / era (why the EV is not simply price x rate)
    The DB2 is consulted by ITEM ID (never by name): an item whose row is
era-Classic carries a TSM price from another game, and a bind-on-pickup item
never reaches the AH.  Both stay in the report, marked, with EV = 0 -- the
number is wrong for them, the fact that you own or loot them is not.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from woweconomy.analytics.farm_ranking import (
    FarmRankingConfig,
    ItemMarket,
    NpcDrop,
    _g,
    _p,
    item_exclusion_reason,
    load_latest_market,
    load_zone_drops,
    realization_weight,
)
from woweconomy.analytics.tsm_ids import tsm_item_string
from woweconomy.config import WowEconomyConfig
from woweconomy.services.db2_items import load_facts as load_item_facts

logger = logging.getLogger(__name__)

KINDS = ("mounts", "recipes", "appearances")

# Thresholds on missing_pct (share of the sampled collectors still lacking it).
GAP_STRONG = 50.0   # most collectors lack it -> a real audience, do not vendor
GAP_WEAK = 15.0     # some still want it -> niche

# "Recipe: X" / "Pattern: X" -> the item is the recipe, not the crafted thing.
_RECIPE_PREFIX = re.compile(
    r"^(recipe|pattern|plans|design|formula|schematic|manual|tome|codex|"
    r"grimoire)\s*:\s*", re.I)
# Mount items are sold as "Reins of the X"; DfA catalogues the mount name.
_MOUNT_NOISE = re.compile(r"^(reins of the|reins of|summon)\s+", re.I)
_NON_WORD = re.compile(r"[^a-z0-9]+")
# Random-enchant suffix of a dropped green: "Pagan Britches of the Fireflash"
# shares its APPEARANCE with "Pagan Britches".  Only used as a fallback: names
# that really do contain "of the" ("Slippers of the Hissing Cult") match
# exactly first, so this never overrides a real hit.
_SUFFIX = re.compile(r"\s+of(?:\s+the)?\s+\w", re.I)


# ----------------------------------------------------------------------
# Names
# ----------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Lowercase, prefix-free, punctuation-free key used for every join."""
    s = (name or "").strip().lower()
    s = _RECIPE_PREFIX.sub("", s)
    s = _MOUNT_NOISE.sub("", s)
    return " ".join(_NON_WORD.sub(" ", s).split())


def item_base(name: str) -> Tuple[str, str]:
    """(normalised name, prefix family) — family is 'recipes', 'mounts' or ''.

    The family is what stops a `Recipe: X` from matching the appearance of the
    X you would craft with it.
    """
    raw = (name or "").strip()
    if _RECIPE_PREFIX.match(raw):
        family = "recipes"
    elif _MOUNT_NOISE.match(raw):
        family = "mounts"
    else:
        family = ""
    return normalize_name(raw), family


# ----------------------------------------------------------------------
# Demand catalog
# ----------------------------------------------------------------------

@dataclass
class DemandRow:
    """One entry of one DfA wishlist."""
    kind: str
    item_id: int
    name: str
    source: str = ""
    missing: int = 0
    collectors: int = 0
    missing_pct: Optional[float] = None
    owned_pct: Optional[float] = None
    value: Optional[float] = None
    wowhead: str = ""

    @property
    def gap(self) -> float:
        """Audience share (0-100); 0 when the sample had no size."""
        return self.missing_pct if self.missing_pct is not None else 0.0


@dataclass
class DemandBook:
    """All wishlists, joined by normalised name."""
    by_name: Dict[str, List[DemandRow]] = field(default_factory=dict)
    sample: Dict[str, int] = field(default_factory=dict)  # kind -> classifiers
    rows: int = 0

    def match(self, name: str) -> List[DemandRow]:
        """Wishlist rows for an item name, best catalog first."""
        return self.match_detail(name)[0]

    def match_detail(self, name: str) -> Tuple[List[DemandRow], str]:
        """(rows, how it matched) -- 'exato' or 'base' (suffix stripped).

        Exact name first; only when that finds nothing do we drop a random
        enchant suffix, because "Pagan Britches of the Fireflash" and "Pagan
        Britches" are one transmog, but "Slippers of the Hissing Cult" is a
        real item name that must not lose its tail.
        """
        base, family = item_base(name)
        if not base:
            return [], ""

        def keep(hits: List[DemandRow]) -> List[DemandRow]:
            if family == "recipes":
                hits = [r for r in hits if r.kind == "recipes"]
            elif family == "mounts":
                hits = [r for r in hits if r.kind == "mounts"]
            return sorted(hits, key=lambda r: -r.gap)

        hits = keep(self.by_name.get(base, []))
        if hits:
            return hits, "exato"
        stem = _SUFFIX.split(base)[0].strip()
        if stem and stem != base and len(stem) > 3:
            hits = keep(self.by_name.get(stem, []))
            if hits:
                return hits, "base"
        return [], ""


def _float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def load_demand(dfa_dir: Path, kinds: Sequence[str] = KINDS) -> DemandBook:
    """Read the three demand_<kind>.csv files (the DfA scan's output)."""
    dfa_dir = Path(dfa_dir)
    book = DemandBook()
    for kind in kinds:
        path = dfa_dir / f"demand_{kind}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found -- run the Data for Azeroth scan's `report` "
                f"first, or pass --dfa-dir")
        with path.open(encoding="utf-8") as fh:
            for raw in csv.DictReader(fh):
                name = (raw.get("name") or "").strip()
                if not name:
                    continue  # catalog entry dropped: nothing to join on
                row = DemandRow(
                    kind=kind,
                    item_id=int(raw.get("item_id") or 0),
                    name=name,
                    source=raw.get("source") or "",
                    missing=int(raw.get("missing") or 0),
                    collectors=int(raw.get("collectors") or 0),
                    missing_pct=_float(raw.get("missing_pct")),
                    owned_pct=_float(raw.get("owned_pct")),
                    value=_float(raw.get("value")),
                    wowhead=raw.get("wowhead") or "",
                )
                book.by_name.setdefault(normalize_name(name), []).append(row)
                book.rows += 1
                book.sample[kind] = max(book.sample.get(kind, 0), row.collectors)
    return book


def scanned_now(dfa_dir: Path) -> Optional[int]:
    """Accounts written to dfa.db, read-only (the scan may be running).

    Used only to warn when the CSVs are older than the database: a stale
    `missing_pct` divides by a smaller sample than the one that exists.
    """
    db = Path(dfa_dir) / "dfa.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                              timeout=5.0)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM account WHERE scanned_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def refresh_hint(dfa_dir: Path) -> str:
    return (f"python -m woweconomy.services.dataforazeroth report "
            f"--out-dir {dfa_dir}")


# ----------------------------------------------------------------------
# Items under analysis
# ----------------------------------------------------------------------

@dataclass
class GapItem:
    """One item you own or can farm, with its wishlist and market evidence."""
    item_id: int
    name: str
    qty: float = 1.0
    drop_rate: float = 0.0       # per kill, when it came from a zone
    npc_name: str = ""
    npc_id: int = 0
    price: float = 0.0           # TSM avgSalePrice (copper)
    sale_rate: float = 0.0
    sold_per_day: float = 0.0
    matches: List[DemandRow] = field(default_factory=list)
    match_rule: str = ""          # "exato" | "base" (sufixo aleatorio removido)
    era: int = 0                  # DB2 ItemSquishEraID (0 = retail)
    binding: str = ""             # DB2/Blizzard binding ("ON_EQUIP" = BoE)
    excluded: str = ""            # "era" | "bop" | "junk" -> no EV

    @property
    def demand(self) -> Optional[DemandRow]:
        return self.matches[0] if self.matches else None

    @property
    def gap(self) -> float:
        d = self.demand
        return d.gap if d else 0.0

    @property
    def ev(self) -> float:
        """TSM-expected value per kill (0 for a bag item, an era-Classic row
        or anything bound: those prices are not retail gold)."""
        return drop_ev(self, self.drop_rate, self.qty)

    @property
    def ev_collector(self) -> float:
        """The slice of `ev` carried by collectors who still lack it."""
        return self.ev * self.gap / 100.0


def drop_ev(item: GapItem, rate: float, qty: float = 1.0) -> float:
    """TSM-expected value of ONE drop row, under `item`'s price and liquidity.

    The rate is the caller's, never the item's best one: two mobs dropping the
    same item must not both be credited the better of the two rates.
    """
    if item.excluded:
        return 0.0
    if rate <= 0 or item.price <= 0:
        return 0.0
    return (rate * (qty or 1.0) * item.price
            * realization_weight(ItemMarket(0, item.name, item.price,
                                            item.sale_rate,
                                            item.sold_per_day)))


@dataclass
class DropLine:
    """One drop of one mob, as the per-mob list prints it.

    This is the item-level row the reader wants next to the mob: how many
    sampled accounts still lack it (`missing` / `collectors`), its drop chance,
    its price, and what it is worth per kill.  A drop nobody in the sample wants
    keeps its place with no percentages: that is a fact about the market, not a
    reason to hide the row.
    """
    item_id: int
    name: str
    missing: int = 0
    collectors: int = 0
    lack_pct: Optional[float] = None
    drop_rate: float = 0.0
    qty: float = 1.0
    price: float = 0.0
    ev: float = 0.0

    @property
    def showcase(self) -> bool:
        """True when this is the drop most of the sample still lacks."""
        return self.missing > 0


@dataclass
class MobDemand:
    """One mob's loot per kill: what it pays and who still lacks it.

    `lack_pct` is the audience share (the same `missing_pct` the item rows
    show) averaged over the mob's drops and weighted by their drop chance, so a
    0.01% drop does not pull the number like a 30% one.  `kill_lack_pct` is the
    other reading of the same data: the chance that a kill yields at least one
    item the sampled collectors still do not have.  `drop_list` carries the
    drops themselves, most-lacked first -- the first of them is the mob's
    showcase.
    """
    npc_id: int
    npc_name: str
    zone_name: str = ""
    ev: float = 0.0                 # per kill, summed over the mob's drops
    ev_collector: float = 0.0       # the slice carried by whoever still lacks it
    lack_pct: Optional[float] = None  # drop-weighted mean of the item gaps
    kill_lack_pct: float = 0.0      # P(kill yields >= 1 drop a collector lacks)
    drops: int = 0                  # every drop row of the mob (rate > 0)
    measured: int = 0               # ... of which have a measured audience
    valued: int = 0                 # ... of which have an audience AND a price
    market_value: float = 0.0       # TSM price of each distinct sellable drop,
                                    # once: the mob's showcase if all listed
    top_item: str = ""
    top_ev: float = 0.0             # the drop that pays for the kill
    drop_list: List[DropLine] = field(default_factory=list)

    @property
    def showcase(self) -> Optional[DropLine]:
        """The drop the sample is missing the most -- the mob's shop window."""
        return self.drop_list[0] if self.drop_list else None


# A drop row cannot contribute more than one unit-of-a-kind per kill; above that
# the "at least one" product would go negative, so the weight is capped.
_MAX_KILL_UNITS = 1.0


def mob_demand(drops: Sequence[NpcDrop], items: Sequence[GapItem]) -> List[MobDemand]:
    """Aggregate the zone's drop rows per mob, using the items' own rows.

    Prices, era/BoP exclusions and audiences come from the very same GapItems
    the item table above prints, so a mob's EV and the item's EV can never
    disagree about what an item is worth.
    """
    by_item = {i.item_id: i for i in items}
    grouped: Dict[int, List[NpcDrop]] = {}
    for d in drops:
        if d.item_id is None or d.drop_rate <= 0:
            continue
        grouped.setdefault(d.npc_id, []).append(d)

    out: List[MobDemand] = []
    for npc_id, rows in grouped.items():
        m = MobDemand(npc_id=npc_id, npc_name=rows[0].npc_name,
                      zone_name=rows[0].zone_name, drops=len(rows))
        weights: List[float] = []
        gaps: List[float] = []
        none_lacking = 1.0
        listed: Set[int] = set()
        for d in rows:
            item = by_item.get(d.item_id)
            if item is None:
                continue
            # The montra is a portfolio, not a rate: one listing of each
            # sellable drop, at its TSM price.  Repeats of the same item on
            # one mob (two drop rows) must not be counted twice.
            if (d.item_id not in listed and item.price > 0
                    and not item.excluded):
                listed.add(d.item_id)
                m.market_value += item.price
            value = drop_ev(item, d.drop_rate, d.qty)
            m.ev += value
            if value > m.top_ev:
                m.top_ev, m.top_item = value, item.name or d.item_name
            demand = item.demand
            line = DropLine(
                item_id=d.item_id, name=item.name or d.item_name,
                missing=demand.missing if demand else 0,
                collectors=demand.collectors if demand else 0,
                lack_pct=demand.missing_pct if demand else None,
                drop_rate=d.drop_rate, qty=d.qty or 1.0,
                price=item.price, ev=value)
            m.drop_list.append(line)
            if demand is None or demand.missing_pct is None:
                continue
            m.measured += 1
            if value > 0:
                m.valued += 1
            gap = demand.gap / 100.0
            weight = min(_MAX_KILL_UNITS, d.drop_rate * (d.qty or 1.0))
            m.ev_collector += value * gap
            weights.append(weight)
            gaps.append(demand.gap)
            none_lacking *= 1.0 - weight * gap
        if gaps:
            total = sum(weights)
            m.lack_pct = (sum(w * g for w, g in zip(weights, gaps)) / total
                          if total > 0 else sum(gaps) / len(gaps))
        m.kill_lack_pct = 100.0 * (1.0 - none_lacking)
        # Most-lacked drop first: that is the mob's showcase, and the list is read
        # from it downwards.
        m.drop_list.sort(key=lambda x: (-x.missing, -x.ev, x.name))
        out.append(m)
    # The mobs themselves are ordered by that showcase, so the top of the list is
    # the mob whose best drop most of the sample still needs.
    out.sort(key=lambda x: (-(x.showcase.missing if x.showcase else 0),
                            -x.ev, x.npc_name))
    return out


def gap_class(missing_pct: Optional[float]) -> str:
    """COLECTOR / NICHO / COMUM / — for a wishlist share."""
    if missing_pct is None:
        return "-"
    if missing_pct >= GAP_STRONG:
        return "COLECTOR"
    if missing_pct >= GAP_WEAK:
        return "NICHO"
    return "COMUM"


def _market_for(market: Dict[int, ItemMarket], item_id: int) -> ItemMarket:
    return market.get(item_id) or ItemMarket(item_id)


# ----------------------------------------------------------------------
# Demand by APPEARANCE ID (the DfA collections, not the catalogue names)
# ----------------------------------------------------------------------

@dataclass
class AppearanceDemand:
    """appearanceId -> how many sampled accounts still LACK it.

    The DfA collection endpoint answers what each account HAS; the scan stores
    `catalogue - owned` per appearance id in `dfa.db :: missing`.  So a row in
    `missing` means "this id was not in that account's response", and the
    count is the audience.  Keyed by id, never by name.
    """
    missing: Dict[int, int] = field(default_factory=dict)
    sample: int = 0
    source: str = ""
    rarity: Dict[int, float] = field(default_factory=dict)  # id -> owned % (all profiles)

    def row_for(self, appearance_ids: Sequence[int]) -> Optional[DemandRow]:
        """The still-missing appearance of one item (highest count wins)."""
        best_id, best_n = 0, 0
        for appearance_id in appearance_ids:
            n = self.missing.get(appearance_id, 0)
            if n > best_n:
                best_id, best_n = appearance_id, n
        if not best_id:
            return None
        miss_pct = (100.0 * best_n / self.sample) if self.sample else None
        owned = self.rarity.get(best_id)
        return DemandRow(
            kind="appearances", item_id=best_id, name="",
            missing=best_n, collectors=self.sample,
            missing_pct=miss_pct, owned_pct=owned,
            value=(miss_pct * (100.0 - owned) / 100.0
                   if miss_pct is not None and owned is not None else None),
        )

    @property
    def summary(self) -> str:
        return (f"aparencias por id: {len(self.missing)} ids em falta, "
                f"{self.sample} contas ({self.source})")


def item_appearances(db_path: str, item_ids: Sequence[int]) -> Dict[int, List[int]]:
    """itemId -> [appearanceId] (the DB2 bridge, chunked IN query)."""
    out: Dict[int, List[int]] = {}
    if not item_ids:
        return out
    from woweconomy.dbcompat import connect_store, missing_table_errors

    con = connect_store(db_path, readonly=True)
    try:
        ids = list(dict.fromkeys(int(i) for i in item_ids))
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for item_id, appearance_id in con.execute(
                    f"SELECT itemId, appearanceId FROM db2_item_appearance "
                    f"WHERE itemId IN ({marks})", chunk):
                out.setdefault(int(item_id), []).append(int(appearance_id))
    except missing_table_errors():
        return {}  # no DB2 import: callers fall back to the name join
    finally:
        con.close()
    return out


def load_appearance_rarity(dfa_dir: Path) -> Dict[int, float]:
    """appearanceId -> share of ALL DfA profiles that own it (best effort)."""
    path = Path(dfa_dir) / "cache" / "appearancesrarity.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("appearances", data) if isinstance(data, dict) else {}
    out: Dict[int, float] = {}
    for key, value in rows.items():
        try:
            out[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def load_appearance_demand(dfa_dir: Path) -> AppearanceDemand:
    """Read the scan's own missing counters (dfa.db first, CSV fallback)."""
    db = Path(dfa_dir) / "dfa.db"
    rarity = load_appearance_rarity(dfa_dir)
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                                  timeout=5.0)
            try:
                sample = con.execute(
                    "SELECT COUNT(*) FROM scanned WHERE kind='appearances'"
                ).fetchone()[0] or con.execute(
                    "SELECT COUNT(*) FROM scanned").fetchone()[0]
                rows = con.execute(
                    "SELECT item_id, n FROM missing "
                    "WHERE kind='appearances' AND n > 0").fetchall()
            finally:
                con.close()
            return AppearanceDemand(
                missing={int(i): int(n) for i, n in rows},
                sample=int(sample or 0), source="dfa.db::missing",
                rarity=rarity)
        except sqlite3.Error as exc:
            logger.warning("dfa.db ilegivel (%s) -- a usar o CSV", exc)
    book = load_demand(Path(dfa_dir))
    missing: Dict[int, int] = {}
    for rows in book.by_name.values():
        for row in rows:
            if row.kind == "appearances" and row.item_id > 0:
                missing[row.item_id] = max(missing.get(row.item_id, 0), row.missing)
    return AppearanceDemand(missing=missing,
                            sample=book.sample.get("appearances", 0),
                            source="demand_appearances.csv", rarity=rarity)


@dataclass
class RealmDemand:
    """The same wishlists sliced by the account's OWN realm.

    `n` in `dfa.db :: missing_realm` counts the accounts of that realm's slice
    that lack the appearance, and `sample` is how many accounts that slice has,
    so the two are the numerator and denominator of the same fraction.  Only
    the accounts a scan attributed count (`scanned.by_realm`): accounts read
    before that existed, or by `reweight`, are missing from the slice, and a
    realm with a handful of accounts in it is noise -- which is why the report
    prints the denominator next to every row.
    """
    missing: Dict[str, Dict[int, int]] = field(default_factory=dict)  # realm -> id -> n
    sample: Dict[str, int] = field(default_factory=dict)              # realm -> accounts
    source: str = ""

    @property
    def summary(self) -> str:
        return (f"por realm: {len(self.sample)} realms, "
                f"{sum(self.sample.values())} contas atribuidas ({self.source})")


def load_realm_demand(dfa_dir: Path, appearance_ids: Sequence[int]) -> RealmDemand:
    """The per-realm slice, for the report's ids only.

    Reading the whole table is 8M rows for nothing: one report asks about a few
    hundred appearance ids and the filtered query answers in milliseconds.
    """
    db = Path(dfa_dir) / "dfa.db"
    if not db.exists():
        return RealmDemand(source="sem dfa.db")
    ids = sorted({int(i) for i in appearance_ids if i})
    if not ids:
        return RealmDemand(source="dfa.db (0 ids de aparencia)")
    out = RealmDemand(source="dfa.db::missing_realm")
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            out.sample = {str(realm): int(n) for realm, n in con.execute(
                "SELECT a.realm, COUNT(DISTINCT s.account) FROM scanned s "
                "JOIN account a ON a.account = s.account "
                "WHERE s.kind='appearances' AND s.by_realm=1 GROUP BY a.realm")}
            for start in range(0, len(ids), 900):
                chunk = ids[start:start + 900]
                marks = ",".join("?" * len(chunk))
                for realm, item_id, n in con.execute(
                        f"SELECT realm, item_id, n FROM missing_realm "
                        f"WHERE kind='appearances' AND n > 0 "
                        f"AND item_id IN ({marks})", chunk):
                    out.missing.setdefault(str(realm), {})[int(item_id)] = int(n)
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.warning("missing_realm ilegivel (%s)", exc)
        return RealmDemand(source=f"erro: {exc}")
    return out


def load_realm_market(db_path: str) -> Dict[str, Dict[int, Tuple[float, int]]]:
    """realm -> itemId -> (market value, auctions), where TSM has the realm.

    TSM stores one row per item per realm it scans, and repeats the same values
    across the realms of one connected group -- they share an auction house, so
    that repetition is the market, not four markets.
    """
    out: Dict[str, Dict[int, Tuple[float, int]]] = {}
    if not db_path:
        return out
    from woweconomy.dbcompat import connect_store, missing_table_errors

    try:
        con = connect_store(db_path, readonly=True)
        try:
            for realm, item_id, market, auctions in con.execute(
                    "SELECT realm, itemId, market, num_auctions FROM tsm_market "
                    "WHERE realm <> ''"):
                out.setdefault(str(realm), {})[int(item_id)] = (
                    float(market or 0), int(auctions or 0))
        finally:
            con.close()
    except missing_table_errors() as exc:
        logger.warning("tsm_market ilegivel (%s)", exc)
    return out


def build_gap_items(items: Sequence[GapItem], book: DemandBook,
                    market: Dict[int, ItemMarket],
                    facts: Optional[Dict] = None,
                    r_cfg: Optional[FarmRankingConfig] = None,
                    appearances: Optional[Dict[int, List[int]]] = None,
                    demand_by_appearance: Optional[AppearanceDemand] = None
                    ) -> List[GapItem]:
    """Attach wishlist matches, TSM evidence and the item's own facts.

    `facts` is the DB2-derived map (item id -> BlizzardItem).  It decides the
    `excluded` reason by ID: a name can be shared by an era-Classic row and a
    retail row, an id cannot.

    Demand is resolved the same way: when the item has appearance ids, they are
    looked up in the DfA counters by ID and the name join is not even tried.
    A name can match a different catalogue entry; an id cannot.  Names remain
    the fallback for items without an appearance (recipes, junk, quest items).
    """
    # exclude_poor is OFF here on purpose: a grey item still has an appearance,
    # and appearance demand is exactly what this report is about (farm_ranking
    # keeps it on: grey drops are vendor trash per kill).
    r_cfg = r_cfg or FarmRankingConfig(exclude_poor=False)
    facts = facts or {}
    appearances = appearances or {}
    out: List[GapItem] = []
    for it in items:
        m = _market_for(market, it.item_id)
        if not it.name:
            it.name = m.name
        it.price = it.price or m.price
        it.sale_rate = it.sale_rate or m.sale_rate
        it.sold_per_day = it.sold_per_day or m.sold_per_day
        b = facts.get(it.item_id)
        if b is not None:
            it.era = b.era
            it.binding = b.binding
        it.excluded = item_exclusion_reason(it.item_id, facts, r_cfg)
        apps = appearances.get(it.item_id) or []
        if apps and demand_by_appearance is not None:
            row = demand_by_appearance.row_for(apps)
            it.matches = [row] if row else []
            # "aparencia" = id matched and somebody lacks it; "aparencia-comum"
            # = id matched, nobody in the sample lacks it (not a name guess).
            it.match_rule = "aparencia" if row else "aparencia-comum"
        elif not it.matches and it.name:
            it.matches, it.match_rule = book.match_detail(it.name)
        out.append(it)
    return out


# ----------------------------------------------------------------------
# Loaders for the two inputs
# ----------------------------------------------------------------------

_ID_COLS = ("ItemID", "itemId", "item_id", "itemID", "ID", "id")
_NAME_COLS = ("Name", "name", "itemName", "ItemName")
_QTY_COLS = ("Quantity", "Qty", "qty", "Count", "count")


def _pick(fieldnames: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    by_lower = {f.lower(): f for f in fieldnames}
    for cand in candidates:
        if cand in fieldnames:
            return cand
        if cand.lower() in by_lower:
            return by_lower[cand.lower()]
    return None


def load_inventory(path: str) -> List[GapItem]:
    """Read a bag export: `ItemID;Name;...;Quantity` (; or , separated).

    Accepts both scripts/inventario.csv and inventory_priced.csv, so the
    already-priced file can be fed back in without a second export.  Duplicate
    item ids are summed: the bag quantity is what you can actually list.
    """
    p = Path(path)
    with p.open(encoding="utf-8-sig", newline="") as fh:
        head = fh.readline()
        fh.seek(0)
        delim = ";" if head.count(";") >= head.count(",") else ","
        reader = csv.DictReader(fh, delimiter=delim)
        cols = reader.fieldnames or []
        id_col = _pick(cols, _ID_COLS)
        if id_col is None:
            raise ValueError(f"{p}: no item-id column in {list(cols)}")
        name_col = _pick(cols, _NAME_COLS)
        qty_col = _pick(cols, _QTY_COLS)
        merged: Dict[int, GapItem] = {}
        for raw in reader:
            try:
                item_id = int(raw[id_col])
            except (TypeError, ValueError):
                continue
            qty = 1.0
            if qty_col:
                try:
                    qty = float(raw[qty_col] or 1)
                except (TypeError, ValueError):
                    qty = 1.0
            cur = merged.get(item_id)
            if cur is None:
                merged[item_id] = GapItem(
                    item_id=item_id,
                    name=(raw.get(name_col) or "").strip() if name_col else "",
                    qty=max(qty, 0.0))
            else:
                cur.qty += max(qty, 0.0)
    return list(merged.values())


def zone_items(drops: Sequence[NpcDrop]) -> List[GapItem]:
    """One row per item of a zone: best drop source kept (highest rate)."""
    best: Dict[int, GapItem] = {}
    for d in drops:
        if d.item_id is None or d.drop_rate <= 0:
            continue
        cur = best.get(d.item_id)
        if cur is None or d.drop_rate > cur.drop_rate:
            best[d.item_id] = GapItem(
                item_id=d.item_id, name=d.item_name, qty=d.qty or 1.0,
                drop_rate=d.drop_rate, npc_name=d.npc_name, npc_id=d.npc_id)
    return list(best.values())


# ----------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------

@dataclass
class GapReport:
    title: str
    items: List[GapItem] = field(default_factory=list)
    sample: Dict[str, int] = field(default_factory=dict)
    note: str = ""
    # Per-mob view, filled in for zone reports only (a bag has no mobs).
    mobs: List[MobDemand] = field(default_factory=list)

    @property
    def with_demand(self) -> List[GapItem]:
        return [i for i in self.items if i.demand]

    @property
    def strong(self) -> List[GapItem]:
        return [i for i in self.items if i.gap >= GAP_STRONG]

    @property
    def excluded(self) -> List[GapItem]:
        """Items kept in the report but out of the EV (era-Classic / BoP / grey)."""
        return [i for i in self.items if i.excluded]

    def excluded_count(self, reason: str) -> int:
        return sum(1 for i in self.items if i.excluded == reason)

    @property
    def ev_total(self) -> float:
        return sum(i.ev for i in self.items)

    @property
    def ev_collector(self) -> float:
        return sum(i.ev_collector for i in self.items)

    @property
    def collector_share(self) -> float:
        total = self.ev_total
        return (self.ev_collector / total) if total > 0 else 0.0

    @property
    def market_value(self) -> float:
        """Bag value if every sellable gap item hit its TSM price (not a promise)."""
        return sum(i.price * i.qty for i in self.strong if not i.excluded)

    @property
    def boe_missing(self) -> List[GapItem]:
        """BoE items the sample does not own (the farm/buy shortlist)."""
        return [i for i in self.items
                if i.binding == "ON_EQUIP" and not i.excluded
                and i.demand is not None and (i.demand.missing or 0) > 0]


def demand_passes(d: Optional[DemandRow], min_score: float) -> bool:
    """True when a wishlist row reaches the DfA value cut (0 = no filter).

    `value` is the DfA's own per-item score: how much of the sample still lacks
    the item times how rare it is, 0-100 -- the "valor" column of the report.
    It is the only DfA score that exists *per item* in the demand files: the
    collector ladder score is per account, and the demand is already aggregated
    (`missing` keeps counts, not who), so a cut on it would be a guess.
    """
    if min_score <= 0:
        return True
    return d is not None and (d.value or 0.0) >= min_score


def analyse(items: Sequence[GapItem], book: DemandBook, market: Dict[int, ItemMarket],
            title: str, note: str = "", facts: Optional[Dict] = None,
            r_cfg: Optional[FarmRankingConfig] = None,
            appearances: Optional[Dict[int, List[int]]] = None,
            demand_by_appearance: Optional[AppearanceDemand] = None,
            min_score: float = 0.0) -> GapReport:
    rows = build_gap_items(items, book, market, facts, r_cfg, appearances,
                           demand_by_appearance)
    if min_score > 0:
        # One cut, one place: the tables, the mob section, the CSV and every
        # summary below all read `items`, so they can never disagree about
        # which results survived the filter.
        rows = [i for i in rows if demand_passes(i.demand, min_score)]
    sample = dict(book.sample)
    if demand_by_appearance is not None and demand_by_appearance.sample:
        # the id join reads the scan directly, so it is the fresher sample
        sample["appearances"] = demand_by_appearance.sample
    return GapReport(title=title, items=rows, sample=sample, note=note)


def _gap_row(i: GapItem, with_drop: bool) -> str:
    d = i.demand
    cnt = f"{d.missing:>5}/{d.collectors:<5}" if d is not None else f"{'':>11}"
    miss = f"{i.gap:5.1f}%" if d and d.missing_pct is not None else "    - "
    own = f"{d.owned_pct:6.2f}" if d and d.owned_pct is not None else "     ?"
    val = f"{d.value:5.1f}" if d and d.value is not None else "    ?"
    price = _g(i.price) if i.price > 0 else "-"
    parts = [cnt, miss]
    if with_drop:
        parts.append(f"{_p(i.drop_rate):>8s}")
    parts += [own, f"{val:>5s}", f"{price:>9s}",
              f"sr={i.sale_rate:5.1%}", f"spd={i.sold_per_day:6.1f}"]
    kind = d.kind[:4] if d else "----"
    mark = "~" if i.match_rule == "base" else " "
    row = " ".join(parts) + f" {mark}{i.name[:34]:34s} [{kind}]"
    if with_drop and i.npc_name:
        row += f" <= {i.npc_name[:22]}"
    if i.excluded:
        row += f"  [{i.excluded.upper()}]"
    return row


def _header(with_drop: bool) -> str:
    head = "sem/amostra  miss%  " + ("   drop%  " if with_drop else "") + \
        "own%  valor    preco    sr    spd  item"
    return head


def _boe_row(i: GapItem) -> str:
    """One line per BoE item: how many sampled accounts still lack it."""
    d = i.demand
    price = _g(i.price) if i.price > 0 else "-"
    ev = _money(i.ev) if i.ev > 0 else "-"
    row = (f"{d.missing:>5}/{d.collectors:<5} {i.gap:5.1f}% "
           f"{_p(i.drop_rate):>8s} {price:>9s} {ev:>8s} {i.sale_rate:6.2%} "
           f"{i.sold_per_day:5.1f}  {i.name[:32]:32s}")
    if i.npc_name:
        row += f" <= {i.npc_name[:22]}"
    return row


def _mob_row(m: MobDemand) -> str:
    """One line per mob: what a kill pays, how much of it is collectors', and
    what the mob's showcase is worth on the AH (one listing of each drop)."""
    falta = f"{m.lack_pct:6.1f}%" if m.lack_pct is not None else "     -"
    montra = _money(m.market_value) if m.market_value > 0 else "-"
    return (f"{m.npc_name[:28]:28s} {_money(m.ev):>9s} "
            f"{_money(m.ev_collector):>9s} {montra:>9s} {falta} "
            f"{m.kill_lack_pct:6.1f}% {m.valued:>3}/{m.drops}")


def _drop_row(d: DropLine) -> str:
    """One drop of one mob: who still lacks it, its rate, price and EV per kill."""
    cnt = f"{d.missing:>5}/{d.collectors:<5}" if d.collectors else f"{'':>11}"
    falta = f"{d.lack_pct:5.1f}%" if d.lack_pct is not None else "    - "
    price = _g(d.price) if d.price > 0 else "-"
    ev = _money(d.ev) if d.ev > 0 else "-"
    return (f"{cnt} {falta} {_p(d.drop_rate):>8s} {price:>9s} {ev:>8s}  "
            f"{d.name[:36]}")


def print_report(r: GapReport, top_n: int = 30, with_drop: bool = False,
                 mob_drops: int = 3) -> None:
    sample = ", ".join(f"{k}={v}" for k, v in sorted(r.sample.items()))
    print(f"\n=== {r.title} ===")
    if sample:
        print(f"amostra: {sample} contas (share that still LACKS the item)")
    if r.note:
        print(r.note)
    by_app = sum(1 for i in r.items if i.match_rule.startswith("aparencia"))
    by_name = sum(1 for i in r.items if i.match_rule in ("exato", "base"))
    print(f"itens: {len(r.items)} | com procura: {len(r.with_demand)} "
          f"| gap forte (>= {GAP_STRONG:.0f}%): {len(r.strong)}")
    print(f"join: {by_app} por aparencia (id) | {by_name} por nome | "
          f"{len(r.items) - by_app - by_name} sem par")
    if r.excluded:
        print(f"fora do EV: {len(r.excluded)} (era-Classic {r.excluded_count('era')}, "
              f"BoP {r.excluded_count('bop')}, grey {r.excluded_count('junk')}) "
              f"-- binding/era do DB2, por item id")
    if with_drop and r.ev_total > 0:
        print(f"EV da zona: {_g(r.ev_total)}/kill | "
              f"atribuivel a coleccionadores: {r.collector_share:.0%} "
              f"({_g(r.ev_collector)}/kill)")
    if not with_drop:
        print(f"valor de montra dos gaps fortes: {_g(r.market_value)} "
              f"(preco TSM x quantidade, NAO e promessa de venda)")

    if with_drop and r.mobs:
        print(f"\n-- DROPS POR MOB ({len(r.mobs)} mobs) --")
        print("   a lista esta' ordenada pela MONTRA: o mob cujo drop mais falta a")
        print("   amostra vem primeiro, e dentro de cada mob o primeiro drop e' a montra")
        print("   mob                          EV/kill  EV coleccao    montra  "
              "falta%  falta/kill%  pago/tot")
        print("   sem/amostra  falta%   drop%    preco   EV/kill  drop")
        for m in r.mobs[:top_n]:
            print("   " + _mob_row(m))
            lines = m.drop_list if mob_drops <= 0 else m.drop_list[:mob_drops]
            for d in lines:
                print("  " + _drop_row(d))
        print("   'sem' = contas cuja resposta nao trazia a aparencia do drop")
        print("   (MINIMO: quem tem a aparencia por outro item conta como 'tem');")
        print("   falta% do mob = media dos drops pesada pelo drop%; falta/kill% =")
        print("   P(uma kill largar >= 1 item que a amostra nao tem); pago = drops")
        print("   com audiencia E preco.  montra = soma do preco TSM de cada drop")
        print("   vendavel do mob, uma vez (o que terias na AH se listasses tudo):")
        print("   e' um portfolio, somado sobre TODOS os drops -- os que --mob-drops")
        print("   nao mostra contam na mesma -- e nao e' EV por kill nem promessa")
        print("   de venda.  O preco da montra (o drop do topo) esta' na linha dele.")
        print("   --mob-drops 0 mostra todos os drops do mob;")
        print("   o EV de um mob conta os drops DELE (um item largado por dois mobs")
        print("   entra nos dois) -- nao e' uma soma unica do EV da zona.")

    boe = r.boe_missing
    total_boe = sum(1 for i in r.items
                    if i.binding == "ON_EQUIP" and not i.excluded)
    if boe:
        print(f"\n-- BoE QUE A AMOSTRA AINDA NAO TEM ({len(boe)} de {total_boe}) --")
        print("   sem/amostra  falta%   drop%    preco   EV/kill     sr    spd  item")
        for i in sorted(boe, key=lambda x: (-(x.demand.missing or 0), -x.price))[:top_n]:
            print(_boe_row(i))
        print("   'sem' = contas cuja resposta nao trazia a aparencia do item")
        print("   (minimo: quem tem a aparencia por outro item conta como 'tem')")
        print("   EV/kill = drop% x preco x liquidez, deste item, neste mob")

    print(f"\n-- PROCURA FORTE (coleccionadores ainda nao tem) --")
    print(_header(with_drop))
    shown = 0
    for i in sorted(r.strong, key=lambda x: -x.gap):
        print(_gap_row(i, with_drop))
        shown += 1
        if shown >= top_n:
            break
    if not shown:
        print("  (nada acima do limiar -- a amostra ainda e pequena)")

    niche = [i for i in r.items if GAP_WEAK <= i.gap < GAP_STRONG]
    if niche:
        print(f"\n-- NICHO ({GAP_WEAK:.0f}-{GAP_STRONG:.0f}%) --")
        for i in sorted(niche, key=lambda x: -x.gap)[:top_n]:
            print(_gap_row(i, with_drop))

    if not with_drop:
        common = [i for i in r.items if i.demand and i.gap < GAP_WEAK]
        if common:
            print(f"\n-- COMUM (a maioria ja tem: o preco vem do mercado, "
                  f"nao da coleccao) --")
            for i in sorted(common, key=lambda x: -x.price)[:top_n]:
                print(_gap_row(i, with_drop))
        none = [i for i in r.items if not i.demand]
        print(f"\n-- SEM ENTRADA NAS WISHLISTS ({len(none)}) --")
        print("  (nem um dos colecionadores da amostra o quer -- seja porque\n"
              "   quase todos ja o tem, seja porque nao e colecionavel: anel,\n"
              "   trinket, item sem aparencia.  Nao significa sem valor.)")
        print("  " + ", ".join(i.name[:26] or f"#{i.item_id}"
                               for i in sorted(none, key=lambda x: -x.price)[:40]))

    if r.excluded:
        print(f"\n-- FORA DO EV ({len(r.excluded)}: preco de outro jogo, "
              f"ou item ligado) --")
        for i in sorted(r.excluded, key=lambda x: -x.price)[:top_n]:
            print(_gap_row(i, with_drop))
        print("  era = linha era-Classic do DB2: o preco TSM deste id e' de outro")
        print("  jogo, nao deste.  bop = bind on pickup: nunca chega a AH.  (POOR/")
        print("  cinzento NAO e' filtrado aqui: a aparencia tambem se vende.)")
        print("  Ficam listados porque os tens ou caem -- o valor e' que nao conta.")


# Below this many attributed accounts a realm's slice is noise, not a market.
MIN_REALM_SAMPLE = 10


def _median(values: Sequence[float]) -> float:
    vals = sorted(values)
    n = len(vals)
    if not n:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def realm_rows(r: GapReport, demand: RealmDemand,
               market: Dict[str, Dict[int, Tuple[float, int]]],
               appearances: Optional[Dict[int, List[int]]] = None
               ) -> List[Tuple[str, int, int, int, int, int, float, float]]:
    """One row per realm: (realm, contas, itens, total, soma, com_ah, mediana, falta%).

    `soma` is the number of missing votes the report's items got in that realm,
    so `100 * soma / (contas * itens)` is the average share of the report's
    items that a slice of that realm lacks -- a percentage, comparable between
    realms of different sizes.  Thin slices come last, never hidden: a realm
    with one account can top the average without being a market.
    """
    pairs: List[Tuple[int, List[int]]] = []
    for i in r.items:
        if i.demand is None:
            continue
        apps = (appearances or {}).get(i.item_id) or []
        if not apps and i.match_rule.startswith("aparencia"):
            apps = [i.demand.item_id]
        if apps:
            pairs.append((i.item_id, apps))
    rows: List[Tuple[str, int, int, int, int, int, float, float]] = []
    for realm, miss in demand.missing.items():
        contas = demand.sample.get(realm, 0)
        hits = soma = com_ah = 0
        prices: List[float] = []
        for item_id, apps in pairs:
            n = max((miss.get(a, 0) for a in apps), default=0)
            if n:
                hits += 1
                soma += n
            mk = (market.get(realm) or {}).get(item_id)
            if mk and mk[1] > 0:          # only an advertised item has a price
                com_ah += 1
                if mk[0] > 0:
                    prices.append(mk[0])
        rate = (100.0 * soma / (contas * hits)) if contas and hits else 0.0
        rows.append((realm, contas, hits, len(pairs), soma, com_ah, _median(prices),
                     rate))
    rows.sort(key=lambda x: (0 if x[1] >= MIN_REALM_SAMPLE else 1, -x[7], -x[1]))
    return rows


def print_realm_section(r: GapReport, demand: RealmDemand,
                        market: Dict[str, Dict[int, Tuple[float, int]]],
                        appearances: Optional[Dict[int, List[int]]] = None,
                        top_n: int = 30) -> None:
    print(f"\n-- DEMANDA POR REALM ({demand.summary}) --")
    rows = realm_rows(r, demand, market, appearances)
    if not rows:
        print("  (o dfa.db nao tem a fatia por realm destes itens: os resultados")
        print("   sao de contas carregadas por um scan antigo.  Um `scan` novo")
        print("   escreve-a para as contas que carimba; `reweight --realm X`")
        print("   rebusca as que faltam nesse realm)")
        return
    print("   contas = contas da amostra que o scan atribuiu a esse realm (o")
    print("   denominador); itens = dos N itens deste report, quantos esse realm")
    print("   ainda nao tem; falta% = media, por item, de quantas dessas contas")
    print("   nao o tem (a coluna que compara realms de tamanhos diferentes);")
    print("   AH = quantos desses itens estao anunciados la' e preco = mediana do")
    print("   market value TSM so' dos anunciados.  Fatias com menos de "
          f"{MIN_REALM_SAMPLE} contas ficam no fim, com '~'.")
    print("   realm                    contas    itens    falta%     AH        preco")
    for realm, contas, hits, total, soma, com_ah, price, rate in rows[:top_n]:
        thin = "~" if contas < MIN_REALM_SAMPLE else " "
        money = _money(price) if price else "-"
        print(f"   {realm[:22]:<22} {contas:>5}{thin} {hits:>4}/{total:<4} "
              f"{rate:>8.1f} {com_ah:>5} {money:>11}")
    thin_n = sum(1 for x in rows if x[1] < MIN_REALM_SAMPLE)
    print(f"   '~' = fatia com menos de {MIN_REALM_SAMPLE} contas: ruido, nao "
          f"mercado ({thin_n} dos {len(rows)} realms)")
    priced = sorted(k for k in market if k != "EU (commodity)")
    if priced:
        print(f"   preco/AH so' onde o TSM varre: {len(priced)} realms "
              f"({', '.join(priced[:8])}{', ...' if len(priced) > 8 else ''});")
        print("   nos outros a coluna fica vazia (nao ha' preco local, nao e' zero).")
        print("   O TSM repete os mesmos valores nos realms de um grupo ligado: e'")
        print("   uma AH partilhada, nao varios mercados com o mesmo preco.")
    else:
        print("   sem tsm_market: nenhum preco por realm (passa --db tsm_markets.db)")
    print("   A fatia de um realm e' quem o scan la' carimbou, nao um censo: onde a")
    print("   escada foi lida fundo entram contas de score mais baixo, que faltam")
    print("   mais -- o falta% desse realm le' acima da media global da amostra.")
    solid = [x for x in rows if x[1] >= MIN_REALM_SAMPLE]
    if solid:
        print("   onde ha' procura com fatia utilizavel: " + ", ".join(
            f"{x[0]} {x[7]:.0f}%"
            f"{f' ({_money(x[6])}, {x[5]} anunciados)' if x[6] else ''}"
            for x in solid[:5]))


# Audience cuts for the TSM group import, in descending order.  The number of
# separations is just the number of cuts: 85,65 is three groups, 85,75,65,55 is
# five.  Different cuts from GAP_STRONG/GAP_WEAK on purpose -- those mark
# "strong/niche demand" in the tables, while a group is an operational choice.
IDS_CUTS = (85.0, 65.0)
# Legacy names, kept because they are what the docs and the first groups used:
# the first band, the second, and the tail.
BAND_ALIASES = {"alta": 0, "nicho": 1, "comum": -1}
_ALL = ("todas", "todos", "all", "*")


def parse_cuts(spec: str) -> List[float]:
    """`--cuts 85,75,65,55` -> [85.0, 75.0, 65.0, 55.0]: descending, unique.

    Order does not matter in the input (`65,85` works); a repeated cut is not
    an error, it is the same cut; a value outside 0-100 is a typo, not a cut.
    """
    out: List[float] = []
    for part in re.split(r"[,;]", spec or ""):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            print(f"corte invalido: {part!r} (percentagens: --cuts 85,75,65,55)",
                  file=sys.stderr)
            raise SystemExit(2)
        if not 0.0 <= value <= 100.0:
            print(f"corte fora de 0-100: {value:g}%", file=sys.stderr)
            raise SystemExit(2)
        if value not in out:
            out.append(value)
    if not out:
        print("--cuts sem valores (ex.: --cuts 85,75,65,55)", file=sys.stderr)
        raise SystemExit(2)
    return sorted(out, reverse=True)


def band_labels(cuts: Sequence[float]) -> List[str]:
    """Range labels for descending cuts: 85,65 -> ['85+', '65-85', '<65']."""
    labels = [f"{cut:g}+" if i == 0 else f"{cut:g}-{cuts[i - 1]:g}"
              for i, cut in enumerate(cuts)]
    labels.append(f"<{cuts[-1]:g}")
    return labels


def band_captions(cuts: Sequence[float]) -> Dict[str, str]:
    """label -> what it means in words: 'falta >= 85%', 'falta < 65%'."""
    captions: Dict[str, str] = {}
    for i, cut in enumerate(cuts):
        captions[f"{cut:g}+" if i == 0 else f"{cut:g}-{cuts[i - 1]:g}"] = (
            f"falta >= {cut:g}%" if i == 0
            else f"falta {cut:g}-{cuts[i - 1]:g}%")
    captions[f"<{cuts[-1]:g}"] = f"falta < {cuts[-1]:g}%"
    return captions


def band_slug(label: str) -> str:
    """A file-name-safe band label: '85+' -> '85plus', '<55' -> 'abaixo55'."""
    slug = label.replace("+", "plus").replace("<", "abaixo").replace(">", "acima")
    return re.sub(r"[^0-9A-Za-z._-]+", "_", slug).strip("_")


def demand_bands(items: Sequence[GapItem],
                 cuts: Sequence[float] = IDS_CUTS) -> Dict[str, List[int]]:
    """Item ids per audience band, ready for a TSM group import.

    A cut is an inclusive lower bound: with 85,65 the item with 85.0 goes to
    '85+'.  Only items with a wishlist row are here: an id nobody in the sample
    wants is not a group of anything.  Era-Classic and BoP are out for the same
    reason they are out of the EV: you cannot list them on this realm's auction
    house.
    """
    labels = band_labels(cuts)
    out: Dict[str, List[int]] = {lab: [] for lab in labels}
    seen: Set[int] = set()
    for i in sorted(items, key=lambda x: (-x.gap, x.item_id)):
        if i.item_id in seen or i.demand is None or i.excluded:
            continue
        seen.add(i.item_id)
        for pos, cut in enumerate(cuts):
            if i.gap >= cut:
                out[labels[pos]].append(i.item_id)
                break
        else:
            out[labels[-1]].append(i.item_id)
    return out


def parse_bands(spec: str, labels: Sequence[str]) -> List[str]:
    """`--band alta,65-85` -> band labels, in the order asked.

    Accepts the labels themselves, a 1-based position (`2`), the legacy names
    (`alta` = first band, `nicho` = second, `comum` = the tail) and
    `todas`/`all` for every band.  Names that do not exist are an error: a band
    silently dropped is a group that never gets made.
    """
    wanted: List[str] = []
    for part in re.split(r"[,;]", spec or ""):
        part = part.strip()
        if not part:
            continue
        key = part.lower()
        if key in _ALL:
            picked = list(labels)
        elif key in BAND_ALIASES:
            picked = [labels[BAND_ALIASES[key]]]
        elif part in labels:
            picked = [part]
        elif key.isdigit() and 1 <= int(key) <= len(labels):
            picked = [labels[int(key) - 1]]
        else:
            print(f"banda invalida: {part!r} (bandas: {', '.join(labels)}; "
                  f"atalhos: alta, nicho, comum, todas)", file=sys.stderr)
            raise SystemExit(2)
        for lab in picked:
            if lab not in wanted:
                wanted.append(lab)
    if not wanted:
        print(f"--band sem valores (bandas: {', '.join(labels)})", file=sys.stderr)
        raise SystemExit(2)
    return wanted


def print_id_bands(bands: Dict[str, List[int]], title: str,
                   cuts: Sequence[float], wanted: Sequence[str],
                   note: str = "", out_prefix: str = "") -> None:
    """One TSM import line per band: ``i:1,i:2,...``."""
    captions = band_captions(cuts)
    print(f"\n=== ids para TSM -- {title} ===")
    if note:
        print(note)
    for label in wanted:
        ids = bands[label]
        print(f"\n{label} ({captions[label]}): {len(ids)} ids")
        line = tsm_item_string(ids)
        print(line if line else "(vazio -- baixa o corte ou carrega outras zonas)")
        if out_prefix:
            path = Path(f"{out_prefix}_{band_slug(label)}.txt")
            path.write_text(line + "\n", encoding="utf-8")
            print(f"-> {path} (cola no Import do grupo)", file=sys.stderr)
    print(f"\n(um grupo TSM por linha, {len(wanted)} de {len(bands)} bandas; "
          f"fora daqui ficam era-Classic, BoP e os itens sem linha nas "
          f"wishlists; ids unicos, ordenados por falta)")


def write_csv(r: GapReport, path: str, with_drop: bool = False) -> None:
    cols = ["item_id", "name", "gap", "classe", "kind", "missing", "collectors",
            "missing_pct", "owned_pct", "value", "price", "sale_rate",
            "sold_per_day", "qty", "era", "binding", "excluded"]
    if with_drop:
        cols += ["drop_rate", "npc_id", "npc_name"]
    cols.append("match_rule")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for i in r.items:
            d = i.demand
            row = {
                "item_id": i.item_id, "name": i.name,
                "gap": f"{i.gap:.2f}", "classe": gap_class(d.missing_pct if d else None),
                "kind": d.kind if d else "",
                "missing": d.missing if d else "",
                "collectors": d.collectors if d else "",
                "missing_pct": d.missing_pct if d else "",
                "owned_pct": d.owned_pct if d else "",
                "value": d.value if d else "",
                "price": round(i.price, 2), "sale_rate": i.sale_rate,
                "sold_per_day": i.sold_per_day, "qty": i.qty,
                "era": i.era, "binding": i.binding, "excluded": i.excluded,
                "match_rule": i.match_rule,
            }
            if with_drop:
                row.update(drop_rate=f"{i.drop_rate:.8f}", npc_id=i.npc_id,
                           npc_name=i.npc_name)
            w.writerow(row)


# ----------------------------------------------------------------------
# One item, asked by name or id
# ----------------------------------------------------------------------

@dataclass
class ItemView:
    """Everything the tools know about a single item, resolved from a query."""
    query: str
    item_id: int = 0
    name: str = ""
    qty: float = 0.0            # units in the bag export (0 = not owned)
    market: Optional[ItemMarket] = None
    matches: List[DemandRow] = field(default_factory=list)
    match_rule: str = ""          # como o nome casou com a wishlist
    drops: List[NpcDrop] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    era: int = 0                  # DB2 ItemSquishEraID
    binding: str = ""             # DB2/Blizzard binding
    excluded: str = ""            # "era" | "bop" | "junk" -> no EV

    @property
    def owned(self) -> bool:
        return self.qty > 0

    @property
    def best(self) -> Optional[DemandRow]:
        return self.matches[0] if self.matches else None

    @property
    def gap(self) -> float:
        d = self.best
        return d.gap if d else 0.0

    @property
    def found(self) -> bool:
        """True only when SOMETHING was resolved, not merely an id was typed."""
        return bool(self.matches or self.qty or self.market is not None
                    or (self.item_id and self.name))

    def drop_ev(self, d: NpcDrop) -> float:
        """EV of this drop per kill, same liquidity rule as farm_ranking."""
        if self.excluded:
            return 0.0
        if d.drop_rate <= 0 or self.market is None or self.market.price <= 0:
            return 0.0
        return (d.drop_rate * (d.qty or 1.0) * self.market.price
                * realization_weight(self.market))


def _name_index(market: Dict[int, ItemMarket], inventory: Sequence[GapItem],
                drops: Sequence[NpcDrop]) -> Dict[str, Tuple[int, str]]:
    """normalised name -> (item_id, display name), bag first, then drops/market."""
    index: Dict[str, Tuple[int, str]] = {}
    for it in inventory:
        if it.name:
            index.setdefault(normalize_name(it.name), (it.item_id, it.name))
    for d in drops:
        if d.item_name and d.item_id is not None:
            index.setdefault(normalize_name(d.item_name), (d.item_id, d.item_name))
    for iid, m in market.items():
        if m.name:
            index.setdefault(normalize_name(m.name), (iid, m.name))
    return index


def _suggest(index: Dict[str, Tuple[int, str]], key: str,
             limit: int = 5) -> List[str]:
    """Close names for a query that matched nothing (shared words, best first)."""
    toks = {t for t in key.split() if len(t) > 2}
    if not toks:
        return []
    scored = []
    for k, (_iid, name) in index.items():
        shared = len(toks & set(k.split()))
        if shared:
            scored.append((shared + (0.5 if key in k else 0.0), name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: List[str] = []
    for _score, name in scored:
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def _name_for_id(item_id: int, inventory: Sequence[GapItem],
                 drops: Sequence[NpcDrop],
                 market: Dict[int, ItemMarket]) -> str:
    """Display name for an id: bag export first, then drops, then the market."""
    for it in inventory:
        if it.item_id == item_id and it.name:
            return it.name
    for d in drops:
        if d.item_id == item_id and d.item_name:
            return d.item_name
    m = market.get(item_id)
    return m.name if m is not None and m.name else ""


def resolve_items(queries: Sequence[str], book: DemandBook,
                  market: Dict[int, ItemMarket], inventory: Sequence[GapItem],
                  drops: Sequence[NpcDrop], facts: Optional[Dict] = None,
                  r_cfg: Optional[FarmRankingConfig] = None) -> List[ItemView]:
    """Resolve each query (name or numeric id) into everything known about it."""
    facts = facts or {}
    r_cfg = r_cfg or FarmRankingConfig(exclude_poor=False)
    index = _name_index(market, inventory, drops)
    views: List[ItemView] = []
    for query in queries:
        v = ItemView(query=query)
        q = (query or "").strip()
        if not q:
            continue
        if q.isdigit():
            v.item_id = int(q)
        else:
            key = normalize_name(q)
            hit = index.get(key)
            if hit is None:
                # same tolerance as the wishlist join: a random enchant suffix
                # ("Pagan Britches of the Fireflash") is still the item.
                stem = _SUFFIX.split(key)[0].strip()
                if stem and stem != key and len(stem) > 3:
                    hit = index.get(stem)
            if hit:
                v.item_id, v.name = hit
            else:
                v.name = q  # wishlist-only name: nothing owns or drops it

        v.qty = sum(i.qty for i in inventory if i.item_id == v.item_id and v.item_id)
        if v.item_id:
            v.market = market.get(v.item_id)
            if not v.name:
                # An id alone says nothing about the name, and the wishlist join
                # is by name: the bag, then the drops, then the market name.
                v.name = _name_for_id(v.item_id, inventory, drops, market)

        if v.item_id:
            v.drops = sorted((d for d in drops if d.item_id == v.item_id),
                             key=lambda d: -d.drop_rate)
            b = facts.get(v.item_id)
            if b is not None:
                v.era = b.era
                v.binding = b.binding
            v.excluded = item_exclusion_reason(v.item_id, facts, r_cfg)

        # The wishlist is keyed by NAME, so try every name we ended up with;
        # the raw query last, which is what makes a wishlist-only lookup work.
        if v.market is not None and v.market.name:
            names = [v.name, v.market.name, q]
        else:
            names = [v.name, q]
        for cand in names:
            if not cand:
                continue
            hits, rule = book.match_detail(cand)
            if hits:
                v.matches, v.match_rule = hits, rule
                break

        if not v.found:
            v.suggestions = _suggest(index, normalize_name(q))
        views.append(v)
    return views


def _money(copper: float) -> str:
    """Copper -> gold, keeping silver/copper visible for tiny per-kill EV."""
    if copper <= 0:
        return "-"
    if copper >= 10_000:
        return _g(copper)
    if copper >= 100:
        return f"{copper / 100:,.1f}s"
    return f"{copper:,.0f}c"


def item_verdict(v: ItemView) -> str:
    """One honest sentence: what the numbers mean for this item."""
    d = v.best
    m = v.market
    if v.excluded:
        why = ("era-Classic: o preco TSM deste id vem de outro jogo (MoP-Classic "
               "e afins), nao do retail" if v.excluded == "era" else
               "bind on pickup / conta: nunca chega a AH" if v.excluded == "bop"
               else "qualidade POOR (lixo de vendor)")
        return (f"FORA DO EV -- {why}.  O item/aparência e' real (podes te-lo "
                f"ou farma-lo); o valor e' que nao conta para retail.")
    if d is None:
        if v.owned:
            return ("Sem procura na amostra: nem um coleccionador a quem falta. "
                    "Isso nao e' 'sem valor' -- aneis, trinkets e itens sem "
                    "aparencia nunca entram nestas wishlists; vende pelo "
                    "mercado ou pelo vendor.")
        return ("Sem entrada nas wishlists nem no mercado: o item pode nao ser "
                "colecionavel, ou o nome nao e' o do catalogo.")
    own = f"{d.owned_pct:.2f}%" if d.owned_pct is not None else "?"
    head = (f"{d.missing_pct:.1f}% dos coleccionadores da amostra ainda nao "
            f"o tem; {own} dos perfis DfA possuem") if d.missing_pct is not None \
        else "presente nas wishlists, mas a amostra nao tem tamanho"
    if v.gap >= GAP_STRONG:
        action = ("Guardar e listar na AH" if v.owned else "Vale ir buscar")
        head = f"{head} -> {action}: e' procura de coleccao, nao de NPC."
    elif v.gap >= GAP_WEAK:
        head += " -> nicho: vende devagar, testa um preco alto primeiro."
    else:
        head += (" -> quase todos ja tem: o preco vem do mercado, nao da "
                 "coleccao.")
    if m is not None and m.sold_per_day <= 0:
        head += ("\n  spd=0: nenhum volume regional registado, logo a procura "
                 "e' declarada e nao provada por vendas.")
    return head


ITEM_COLUMNS = ("query", "found", "item_id", "name", "qty", "price",
                "sale_rate", "sold_per_day", "realized", "gap", "classe",
                "wishlists", "best_kind", "best_missing_pct", "best_owned_pct",
                "best_value",                "drops", "drop_npc", "drop_zone", "drop_rate",
                "ev_kill", "era", "binding", "excluded")


def write_items_csv(views: Sequence[ItemView], path: str) -> None:
    """One row per queried item: the same fields the report prints."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ITEM_COLUMNS))
        w.writeheader()
        for v in views:
            d = v.best
            m = v.market
            top = v.drops[0] if v.drops else None
            ev = v.drop_ev(top) if top is not None else 0.0
            w.writerow({
                "query": v.query, "found": int(v.found), "item_id": v.item_id,
                "name": v.name, "qty": v.qty,
                "price": round(m.price, 2) if m else "",
                "sale_rate": m.sale_rate if m else "",
                "sold_per_day": m.sold_per_day if m else "",
                "realized": round(m.price * realization_weight(m), 2) if m else "",
                "gap": f"{v.gap:.2f}",
                "classe": gap_class(d.missing_pct if d else None),
                "wishlists": len(v.matches),
                "best_kind": d.kind if d else "",
                "best_missing_pct": d.missing_pct if d else "",
                "best_owned_pct": d.owned_pct if d else "",
                "best_value": d.value if d else "",
                "drops": len(v.drops),
                "drop_npc": top.npc_name if top else "",
                "drop_zone": top.zone_name if top else "",
                "drop_rate": f"{top.drop_rate:.8f}" if top else "",
                "ev_kill": round(ev, 4),
                "era": v.era, "binding": v.binding, "excluded": v.excluded,
            })


def print_item(v: ItemView, inventory_label: str = "") -> None:
    if not v.found:
        print(f"\n=== {v.query} ===")
        print("  nao encontrado no inventario, no mercado nem nos drops das "
              "zonas carregadas.")
        if v.suggestions:
            print("  queria dizer: " + ", ".join(v.suggestions))
        return

    print(f"\n=== {v.name or v.query} ===")
    if v.item_id:
        print(f"id         : {v.item_id}")
    if v.qty:
        print(f"inventario : {v.qty:g} un." + (f"  ({inventory_label})"
                                               if inventory_label else ""))
    else:
        print("inventario : nao tens")
    m = v.market
    if m is not None and m.price > 0:
        weight = realization_weight(m)
        print(f"mercado    : {_g(m.price)}  sr={m.sale_rate:5.1%}  "
              f"spd={m.sold_per_day:6.1f}/dia  -> realizado "
              f"~{_money(m.price * weight)}/un a este ritmo")
    else:
        print("mercado    : sem avgSalePrice no snapshot (nao entra em EV)")
    if v.binding or v.era:
        bind = {"ON_EQUIP": "BoE (vende na AH)",
                "ON_ACQUIRE": "BoP",
                "QUEST": "item de quest"}.get(v.binding, v.binding or "-")
        era = "retail" if not v.era else f"era-Classic (era={v.era})"
        print(f"binding    : {bind} | {era}")
    if v.item_id:
        print(f"wowhead    : https://www.wowhead.com/item={v.item_id}")

    print("\n-- WISHLISTS --")
    if not v.matches:
        print("  nenhuma (nome fora dos tres catalogos do DfA)")
    else:
        print("catalogo      id_cat   miss  amostra   miss%    own%  valor  "
              "classe    regra")
        for d in v.matches:
            miss = f"{d.missing_pct:6.2f}" if d.missing_pct is not None else "     -"
            own = f"{d.owned_pct:7.2f}" if d.owned_pct is not None else "      ?"
            val = f"{d.value:6.1f}" if d.value is not None else "     ?"
            print(f"{d.kind:12s} {d.item_id:>7} {d.missing:>6} {d.collectors:>8} "
                  f"{miss}% {own} {val}  {gap_class(d.missing_pct):9s} "
                  f"{v.match_rule or '-'}")
        print("  (id_cat e' o id NO CATALOGO -- para appearances e' um "
              "appearanceID, nao o itemID)")
        if len(v.matches) > 1 and len({d.kind for d in v.matches}) == 1:
            print(f"  {len(v.matches)} entradas do mesmo catalogo: variantes do "
                  f"mesmo modelo, a de cima e' a mais valiosa.")

    print(f"\n-- DROPS ({len(v.drops)}) --")
    if not v.drops:
        print("  nenhum NPC das zonas carregadas o larga")
    else:
        print("zona             npc                          drop%     qtd   "
              "EV/kill  EV coleccao")
        for d in v.drops:
            ev = v.drop_ev(d)
            evs = _money(ev)
            evc = _money(ev * v.gap / 100.0)
            print(f"{d.zone_name[:16]:16s} {d.npc_name[:28]:28s} "
                  f"{_p(d.drop_rate):>8s} {d.qty:>5.0f} {evs:>9s} {evc:>11s}")

    print("\nleitura   : " + item_verdict(v))


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def default_dfa_dir() -> Optional[Path]:
    """Find the DfA out-dir without hardcoding one machine's paths."""
    import os
    env = os.environ.get("DFA_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2]
    for cand in (here / "woweconomy" / "dfa",
                 here.parent / "economics" / "woweconomy" / "dfa",
                 Path.cwd() / "woweconomy" / "dfa"):
        if (cand / "demand_mounts.csv").exists():
            return cand
    return None


def _load_market(args) -> Tuple[Dict[int, ItemMarket], str]:
    if args.db:
        return load_latest_market(args.db), f"db:{args.db}"
    from woweconomy.analytics.farm_ranking import load_csv_market
    return load_csv_market(args.market_csv), f"csv:{args.market_csv}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cruza o inventario / drops de zona com o que os "
                    "colecionadores do Data for Azeroth ainda nao tem.")
    ap.add_argument("mode", choices=("inventory", "zone", "item", "ids"))
    ap.add_argument("paths", nargs="*",
                    help="inventory: CSV do inventario (default "
                         "scripts/inventario.csv) | zone/ids: zone_*.json | "
                         "item: nomes ou IDs a consultar")
    ap.add_argument("--dfa-dir", default="",
                    help="pasta com demand_*.csv (default: $DFA_DIR ou "
                         "woweconomy/dfa do projecto)")
    ap.add_argument("--market-csv", default="csv/items.csv")
    ap.add_argument("--db", default="",
                    help="tsm_markets.db (precos mais recentes que o CSV)")
    ap.add_argument("--facts-db", default="tsm_markets.db",
                    help="sqlite com db2_items + blizzard_items: binding e era "
                         "por item id (vazio desliga o filtro)")
    ap.add_argument("--no-era-filter", action="store_true",
                    help="manter as linhas era-Classic no EV (os precos sao "
                         "de outro jogo: desligado por omissao)")
    ap.add_argument("--min-score", type=int, default=0,
                    help="esconde os resultados cujo valor DfA do item (a coluna "
                         "'valor', 0-100) seja menor que N -- 0 desliga.  O score "
                         "do colecionador (a escada, milhoes) e' por conta e a "
                         "demanda ja' vem agregada: nao da' para cortar por ele "
                         "sem rebuscar as wishlists")
    ap.add_argument("--by-realm", action="store_true",
                    help="acrescenta a seccao DEMANDA POR REALM (modos inventory/"
                         "zone): a mesma demanda do dfa.db::missing_realm, por realm, "
                         "com o preco e os anuncios que o TSM tem desse realm.  So' "
                         "conta as contas que o scan atribuiu a um realm")
    ap.add_argument("--inventory", default="scripts/inventario.csv",
                    help="CSV do inventario usado pelo modo item")
    # One comma-separated string, not nargs="*": a greedy list would swallow
    # the item names that follow the flag, and fail with a puzzling "indica
    # pelo menos um nome".
    ap.add_argument("--zones", default="",
                    help="ficheiros zone_*.json separados por virgula a varrer "
                         "no modo item (default: todos em "
                         "woweconomy/wowhead_zones)")
    ap.add_argument("--no-zones", action="store_true",
                    help="modo item: nao procurar drops")
    ap.add_argument("--top", type=int, default=30,
                    help="mobs/itens mostrados por seccao (default: %(default)s)")
    ap.add_argument("--mob-drops", type=int, default=3,
                    help="drops mostrados de cada mob, do mais em falta para baixo "
                         "(default: %(default)s; 0 = todos)")
    ap.add_argument("--cuts", default=",".join(f"{c:g}" for c in IDS_CUTS),
                    help="modo ids: cortes de falta%%%, do maior para o menor; N "
                         "cortes = N+1 grupos (default: %(default)s; "
                         "85,75,65,55 faz 5 grupos)")
    ap.add_argument("--band", default="alta,nicho",
                    help="modo ids: que bandas imprimir, por rotulo (85+, "
                         "75-85, <55...), posicao (1,2), atalho (alta, nicho, "
                         "comum) ou todas (default: %(default)s)")
    ap.add_argument("--out-prefix", default="",
                    help="modo ids: grava <prefixo>_alta.txt / _nicho.txt com "
                         "a linha de ids de cada banda")
    ap.add_argument("--out", default="", help="escrever o relatorio em .txt")
    ap.add_argument("--csv", default="", help="escrever a tabela em .csv")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.ERROR if args.quiet else logging.WARNING)

    dfa_dir = Path(args.dfa_dir) if args.dfa_dir else default_dfa_dir()
    if dfa_dir is None or not Path(dfa_dir).exists():
        print("pasta do Data for Azeroth nao encontrada: passa --dfa-dir "
              "<dir com demand_mounts.csv> (ou define DFA_DIR)")
        return 2
    book = load_demand(dfa_dir)
    market, market_src = _load_market(args)
    r_cfg = FarmRankingConfig(exclude_poor=False,
                              exclude_era=not args.no_era_filter)
    facts = load_item_facts(args.facts_db) if args.facts_db else {}
    app_demand = load_appearance_demand(dfa_dir)
    note = f"precos: {market_src}"
    note += f"\n{app_demand.summary}"
    if facts:
        note += f"\nbinding/era: {args.facts_db} -- {len(facts)} itens, por item id"
    else:
        note += ("\nAVISO: sem db2_items em " + (args.facts_db or "-") +
                 " -- binding e era NAO verificados (o EV pode contar BoP e "
                 "precos era-Classic); corre scripts/import_db2.py")
    live = scanned_now(dfa_dir)
    csv_sample = max(book.sample.values() or [0])
    # A scan running in another window is always a few accounts ahead; that is
    # not staleness, it is a live sample.  Only complain when the share would
    # actually move: >2% of accounts behind (and at least a full percentage).
    if live and live > csv_sample and (live - csv_sample) > max(20, 0.02 * live):
        note += (f"\nAVISO: os demand_*.csv sao de {csv_sample} contas mas o "
                 f"dfa.db ja tem {live} -- os missing_pct estao velhos. "
                 f"Refresca com:\n  {refresh_hint(dfa_dir)}")

    if args.mode == "item":
        if not args.paths:
            print("indica pelo menos um nome ou ID de item")
            return 2
        bag: List[GapItem] = []
        bag_label = ""
        if Path(args.inventory).exists():
            try:
                bag = load_inventory(args.inventory)
                bag_label = args.inventory
            except (OSError, ValueError) as exc:
                print(f"aviso: inventario ilegivel ({exc}) -- continuo sem ele")
        else:
            print(f"aviso: {args.inventory} nao existe -- continuo sem inventario")
        zone_drops: List[NpcDrop] = []
        if not args.no_zones:
            zpaths = [Path(p.strip()) for p in re.split(r"[,;]", args.zones)
                      if p.strip()]
            if not zpaths:
                zpaths = sorted(Path("woweconomy/wowhead_zones").glob("zone_*.json"))
            zpaths = [p for p in zpaths if p.exists()]
            if zpaths:
                zone_drops = load_zone_drops(zpaths)
        views = resolve_items(args.paths, book, market, bag, zone_drops,
                              facts=facts, r_cfg=r_cfg)
        if args.min_score:
            views = [v for v in views
                     if demand_passes(v.best, args.min_score)]

        def render() -> None:
            if note:
                print(note)
            for v in views:
                print_item(v, bag_label)

        if args.out:
            import contextlib
            with open(args.out, "w", encoding="utf-8") as fh:
                with contextlib.redirect_stdout(fh):
                    render()
            print(f"report -> {args.out}")
        else:
            render()
        if args.csv:
            write_items_csv(views, args.csv)
            print(f"tabela -> {args.csv}")
        return 0 if any(v.found for v in views) else 1

    if args.mode == "inventory":
        path = args.paths[0] if args.paths else "scripts/inventario.csv"
        items = load_inventory(path)
        bridges = (item_appearances(args.facts_db, [i.item_id for i in items])
                   if args.facts_db else {})
        report = analyse(items, book, market, f"INVENTARIO vs COLECIONADORES",
                         note + f"\ninventario: {path}",
                         facts=facts, r_cfg=r_cfg,
                         appearances=bridges, demand_by_appearance=app_demand,
                         min_score=args.min_score)
        with_drop = False
    else:
        if not args.paths:
            print("indica pelo menos um zone_*.json")
            return 2
        missing = [p for p in args.paths if not Path(p).exists()]
        if missing:
            print("nao encontrei: " + ", ".join(missing))
            return 2
        drops = load_zone_drops([Path(p) for p in args.paths])
        names = sorted({d.zone_name for d in drops if d.zone_name})
        title = " / ".join(names) or "ZONA"
        items = zone_items(drops)
        bridges = (item_appearances(args.facts_db, [i.item_id for i in items])
                   if args.facts_db else {})
        report = analyse(items, book, market, f"{title} vs COLECIONADORES", note,
                         facts=facts, r_cfg=r_cfg,
                         appearances=bridges, demand_by_appearance=app_demand,
                         min_score=args.min_score)
        with_drop = True
        if args.mode == "zone":
            report.mobs = mob_demand(drops, report.items)
        else:
            # ids: the same analysis, printed as TSM group imports instead of
            # tables -- the numbers and the filter can never disagree with
            # the report, because both read `report.items`.
            cuts = parse_cuts(args.cuts)
            bands = demand_bands(report.items, cuts)
            print_id_bands(bands, report.title, cuts,
                           parse_bands(args.band, list(bands)),
                           note=report.note, out_prefix=args.out_prefix)
            return 0

    realm_demand = RealmDemand()
    realm_market: Dict[str, Dict[int, Tuple[float, int]]] = {}
    if args.by_realm:
        # Only the ids this report asks about: one report is a few hundred
        # appearance ids, the table has eight million rows.
        app_ids: List[int] = []
        for i in report.items:
            app_ids.extend(bridges.get(i.item_id) or [])
            if i.demand is not None and i.match_rule.startswith("aparencia"):
                app_ids.append(i.demand.item_id)
        realm_demand = load_realm_demand(dfa_dir, app_ids)
        realm_market = load_realm_market(args.db or "")

    def render() -> None:
        print_report(report, args.top, with_drop, args.mob_drops)
        if args.by_realm:
            print_realm_section(report, realm_demand, realm_market, bridges, args.top)

    import contextlib
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            with contextlib.redirect_stdout(fh):
                render()
        print(f"report -> {args.out}")
    else:
        render()
    if args.csv:
        write_csv(report, args.csv, with_drop)
        print(f"tabela -> {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
