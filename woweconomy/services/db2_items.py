"""DB2 item facts — binding, quality and era for every item the client knows.

Why
    `blizzard_items` answers the same question, but only for the ~2k ids that
    were ever asked about through the API.  `blizz_excluded()` reads a missing
    row as "nothing against it", so 98% of drops were never checked for
    binding.  The client DB2 (ItemSparse, imported by scripts/import_db2.py
    into `tsm_markets.db :: db2_items`) answers it for the whole game, offline,
    in one file, and adds the column the API does not expose:

        binding   0 none | 1 pick-up (BoP) | 2 equip (BoE) | 3 use
                  | 4 quest | 7-9 account-bound
        era       0 = the retail row; anything else = an era-Classic row of
                  the same id space (ItemSquishEraID).  A MoP-Classic price
                  must never enter a retail expected value, and this is the
                  only marker for it.

Keying
    By ITEM ID, never by name.  Names repeat across the two games, across
    realms, and across the random-enchant suffixes of one item; ids do not.
    (The appearance join is also id-keyed: db2_item_appearance.itemId ->
    appearanceId, where appearanceId is the DfA `visualid`.)

Merge order
    DB2 wins where it has a row (it is the client build's own data, field for
    field); the Blizzard API row is kept for the ids the DB2 does not know and
    for `sell_price`, which ItemSparse does not carry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from woweconomy.dbcompat import connect_store, missing_table_errors
from woweconomy.services.blizzard_items import BlizzardItem, load_map as load_blizz_map

# Enum.ItemBind (warcraft.wiki.gg/wiki/Enum.ItemBind), verified against the
# live CSV: id 25 "Worn Shortsword" -- a classic BoE -- carries Bonding=2.
BINDING_NAMES: Dict[int, str] = {
    0: "",            # no binding: tradeable
    1: "ON_ACQUIRE",  # binds when picked up (BoP)
    2: "ON_EQUIP",    # binds when equipped (BoE)
    3: "ON_USE",      # binds when used
    4: "QUEST",       # quest item
    7: "TO_ACCOUNT",
    8: "TO_BNET",
    9: "TO_BNET_UNTIL_EQUIPPED",
}

# ItemQuality: 0 POOR .. 7 HEIRLOOM.  Only POOR is ever filtered.
QUALITY_NAMES: Dict[int, str] = {
    0: "POOR", 1: "COMMON", 2: "UNCOMMON", 3: "RARE", 4: "EPIC",
    5: "LEGENDARY", 6: "ARTIFACT", 7: "HEIRLOOM",
}

SQLITE_CHUNK = 900  # stay below SQLite's bound-parameter limit


@dataclass(frozen=True)
class Db2Item:
    """One row of the client's item table."""
    item_id: int
    name: str = ""
    binding: Optional[int] = None  # None = column empty; 0 = explicitly none
    quality: Optional[int] = None
    expansion: int = 0
    era: int = 0
    item_level: int = 0
    required_level: int = 0

    @property
    def binding_name(self) -> str:
        """Same vocabulary as the Blizzard API ('ON_EQUIP', 'ON_ACQUIRE')."""
        if self.binding is None:
            return ""
        return BINDING_NAMES.get(self.binding, f"BIND_{self.binding}")

    @property
    def quality_name(self) -> str:
        if self.quality is None:
            return ""
        return QUALITY_NAMES.get(self.quality, f"QUALITY_{self.quality}")

    @property
    def is_era_classic(self) -> bool:
        """True for an era-Classic row: its prices belong to another game."""
        return bool(self.era)

    @property
    def is_soulbound(self) -> bool:
        return self.binding_name in {"ON_ACQUIRE", "TO_ACCOUNT", "TO_BNET",
                                     "TO_BNET_UNTIL_EQUIPPED"}


def _chunks(ids: List[int]) -> Iterable[List[int]]:
    for start in range(0, len(ids), SQLITE_CHUNK):
        yield ids[start:start + SQLITE_CHUNK]


def _rows(con: Any, sql: str, only: Optional[Set[int]]) -> list:
    if only is None:
        return con.execute(sql).fetchall()
    out: list = []
    ids = sorted(only)
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        out.extend(con.execute(f"{sql} WHERE itemId IN ({marks})", chunk).fetchall())
    return out


def load_map(db_path: str, only: Optional[Iterable[int]] = None
             ) -> Dict[int, Db2Item]:
    """db2_items as a dict keyed by item id ({} when the table is absent).

    `only` restricts the load to those ids; a zone ranking needs a couple of
    thousand rows, not the 176k in the table, and the query stays indexed by
    primary key.
    """
    wanted = None if only is None else {int(i) for i in only}
    if wanted is not None and not wanted:
        return {}
    try:
        con = connect_store(db_path, readonly=True)
        try:
            rows = _rows(
                con,
                "SELECT itemId,name,binding,quality,expansion,era,itemLevel,"
                "requiredLevel FROM db2_items",
                wanted,
            )
        finally:
            con.close()
    except missing_table_errors():
        return {}  # no db2 import yet: behave exactly as before
    return {
        int(r[0]): Db2Item(
            item_id=int(r[0]), name=r[1] or "",
            binding=None if r[2] is None else int(r[2]),
            quality=None if r[3] is None else int(r[3]),
            expansion=int(r[4] or 0), era=int(r[5] or 0),
            item_level=int(r[6] or 0), required_level=int(r[7] or 0),
        )
        for r in rows
    }


def load_facts(db_path: str, only: Optional[Iterable[int]] = None
               ) -> Dict[int, BlizzardItem]:
    """item id -> BlizzardItem, DB2 first and the API as the fallback.

    Returns the same type the ranking functions already speak, so the whole
    `blizz` parameter becomes "the facts about items" instead of "the few
    items the API happened to enrich".  `era` and `sell_price` come from their
    only possible sources.
    """
    wanted = None if only is None else {int(i) for i in only}
    db2 = load_map(db_path, wanted)
    api = load_blizz_map(db_path)
    if wanted is not None:
        api = {i: b for i, b in api.items() if i in wanted}
    facts: Dict[int, BlizzardItem] = {}
    for item_id, b in api.items():
        d = db2.get(item_id)
        facts[item_id] = BlizzardItem(
            item_id=item_id,
            name=(d.name if d and d.name else b.name),
            quality=(d.quality_name if d and d.quality is not None
                     else b.quality),
            binding=(d.binding_name if d and d.binding is not None
                     else b.binding),
            level=(d.item_level if d and d.item_level else b.level),
            sell_price=b.sell_price,  # ItemSparse carries no vendor price
            fetched_at=b.fetched_at,
            era=d.era if d else 0,
        )
    for item_id, d in db2.items():
        if item_id in facts:
            continue
        facts[item_id] = BlizzardItem(
            item_id=item_id, name=d.name, quality=d.quality_name,
            binding=d.binding_name, level=d.item_level,
            sell_price=0, fetched_at="", era=d.era,
        )
    return facts
