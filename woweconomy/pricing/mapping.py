"""Mapping WoW items to Data for Azeroth appearances.

The two worlds use different identifiers:

* the **auction house** and TSM speak in *item* ids;
* the **Data for Azeroth** scan speaks in *appearance* ids -- the
  cosmetic set an account may or may not own.

The bridge is ``db2_item_appearance`` in the TSM mirror.  It is not a
clean one-to-one mapping, and pretending otherwise is how a dataset ends
up with a number that belongs to a different item:

* **9,516 items map to more than one appearance** -- an item can be
  worn as several sets (usually a shape variant plus a colour variant);
* **34,849 appearances map to more than one item** -- several items
  share one appearance, so the appearance does not identify the item;
* some items map to **no** appearance at all.

Every record therefore carries its own status:

``exact``
    one item, one appearance, unambiguous;
``multi``
    the item maps to several appearances -- the interest figure is the
    *union* of them and is flagged as such;
``shared``
    the appearance is worn by several items, so the interest figure
    belongs to the set and not to the item alone;
``unmapped``
    the item has no appearance; there is no interest figure to report.

Nothing is dropped silently: the counts of every status are part of the
dataset's own metadata.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# mapping statuses
EXACT = "exact"
MULTI = "multi"
SHARED = "shared"
UNMAPPED = "unmapped"


@dataclass
class ItemMapping:
    """How one item relates to the appearance world."""

    item_id: int
    appearance_ids: List[int] = field(default_factory=list)
    status: str = UNMAPPED
    #: for a shared appearance, the other items that wear it
    shared_with: List[int] = field(default_factory=list)

    @property
    def has_interest(self) -> bool:
        return bool(self.appearance_ids)


@dataclass
class MappingReport:
    """What the mapping could and could not resolve."""

    items: int = 0
    exact: int = 0
    multi: int = 0
    shared: int = 0
    unmapped: int = 0
    appearance_ids: int = 0
    ambiguous_appearances: int = 0

    def summary(self) -> Dict[str, object]:
        return {
            "items": self.items,
            "exact": self.exact,
            "multi": self.multi,
            "shared": self.shared,
            "unmapped": self.unmapped,
            "appearance_ids": self.appearance_ids,
            "ambiguous_appearances": self.ambiguous_appearances,
            "mapped_share": round(
                (self.exact + self.multi + self.shared) / self.items, 4)
            if self.items else None,
        }


def build_item_mapping(tsm_db: Path | str,
                       item_ids: Optional[Sequence[int]] = None) -> Tuple[Dict[int, ItemMapping], MappingReport]:
    """Map item ids to their appearance ids.

    ``item_ids`` of ``None`` maps every item in the bridge, which is what
    the full dataset wants; a list restricts it, which is what a single
    recommendation wants.
    """
    report = MappingReport()
    con = sqlite3.connect(tsm_db)
    try:
        # the bridge
        by_item: Dict[int, List[int]] = {}
        by_appearance: Dict[int, List[int]] = {}
        if item_ids is None:
            rows = con.execute(
                "SELECT itemId, appearanceId FROM db2_item_appearance").fetchall()
        else:
            ids = list(dict.fromkeys(int(i) for i in item_ids))
            rows = []
            for start in range(0, len(ids), 900):
                chunk = ids[start:start + 900]
                marks = ",".join("?" * len(chunk))
                rows.extend(con.execute(
                    f"SELECT itemId, appearanceId FROM db2_item_appearance "
                    f"WHERE itemId IN ({marks})", chunk).fetchall())
        for item_id, appearance_id in rows:
            by_item.setdefault(int(item_id), []).append(int(appearance_id))

        # which appearances are shared?  The answer needs *every* item that
        # wears the appearance, not only the ones that were asked for.
        wanted_appearances = {a for appearances in by_item.values() for a in appearances}
        if wanted_appearances:
            for start in range(0, len(wanted_appearances), 900):
                chunk = sorted(wanted_appearances)[start:start + 900]
                marks = ",".join("?" * len(chunk))
                for item_id, appearance_id in con.execute(
                        f"SELECT itemId, appearanceId FROM db2_item_appearance "
                        f"WHERE appearanceId IN ({marks})", chunk):
                    by_appearance.setdefault(int(appearance_id), []).append(int(item_id))
        for appearance_id in by_appearance:
            by_appearance[appearance_id] = sorted(set(by_appearance[appearance_id]))

        mapping: Dict[int, ItemMapping] = {}
        for item_id, appearances in by_item.items():
            appearances = sorted(set(appearances))
            shared_with: List[int] = []
            is_shared = False
            for appearance in appearances:
                others = [i for i in by_appearance.get(appearance, []) if i != item_id]
                if others:
                    is_shared = True
                    shared_with.extend(others)
            if len(appearances) > 1:
                status = MULTI
            elif is_shared:
                status = SHARED
            else:
                status = EXACT
            mapping[item_id] = ItemMapping(
                item_id=item_id, appearance_ids=appearances, status=status,
                shared_with=sorted(set(shared_with)))

        # the items that asked for but have no appearance
        if item_ids is not None:
            for item_id in item_ids:
                mapping.setdefault(int(item_id),
                                   ItemMapping(item_id=int(item_id), status=UNMAPPED))

        report.items = len(mapping)
        report.exact = sum(1 for m in mapping.values() if m.status == EXACT)
        report.multi = sum(1 for m in mapping.values() if m.status == MULTI)
        report.shared = sum(1 for m in mapping.values() if m.status == SHARED)
        report.unmapped = sum(1 for m in mapping.values() if m.status == UNMAPPED)
        report.appearance_ids = len({a for m in mapping.values() for a in m.appearance_ids})
        report.ambiguous_appearances = sum(
            1 for a, items in by_appearance.items() if len(items) > 1)
        return mapping, report
    finally:
        con.close()


def resolve_interest_appearance(mapping: ItemMapping) -> Optional[int]:
    """The single appearance to report interest for, if there is one.

    A multi-appearance item has no single figure; the caller sums the
    appearances instead and says the item is multi.
    """
    if not mapping.appearance_ids:
        return None
    if len(mapping.appearance_ids) == 1:
        return mapping.appearance_ids[0]
    return None
