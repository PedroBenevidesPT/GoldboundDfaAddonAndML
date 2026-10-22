"""Which items do high-score collectors still lack?

This is the inverse of "how many appearances does the average buyer miss".
The bitmaps give, for every account and every appearance, one bit.  Restrict
the accounts to a score band (1M+), then for each appearance count the zero
bits:

    wanters(item) = number of selected accounts whose bit is 0
    want_rate     = wanters / eligible_accounts
    gold_gap      = wanters * price_of_the_item

`wanters` is the reach of that item inside the high-score segment; `gold_gap`
is how much listed value is still missing from their collections.  An item
with 80% want rate is not "everyone owns it" -- it is the opposite: four out of
five 1M+ collectors still need it, which is exactly the shelf a seller wants.

The join to a price is deliberately narrow: an appearance can be granted by
several items, so the report keeps the cheapest **priced BoE retail** item for
that appearance.  A vendor/Trading Post appearance is excluded by the caller;
its "price" is not an auction-house price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class ItemShelf:
    """One purchasable BoE item, with the union of its appearances.

    A single item can grant several appearances (difficulty tints, set pieces).
    Counting each appearance separately would count the same buyer several
    times; ``wanters`` here is the number of accounts missing **at least one**
    of the item's appearances, which is what a single sale actually serves.
    """

    item_id: int
    item_name: str
    appearance_id: int
    name: str
    source: str
    grants: int
    wanters: int
    eligible: int
    price: int  # copper

    @property
    def want_rate(self) -> float:
        return self.wanters / self.eligible if self.eligible else 0.0

    @property
    def gold_gap(self) -> float:
        return self.wanters * self.price / 10_000

    @property
    def gold_per_wanter(self) -> float:
        return self.price / 10_000


@dataclass(frozen=True)
class ItemDemand:
    appearance_id: int
    name: str
    source: str
    wanters: int
    eligible: int
    price: int  # copper
    item_id: int
    item_name: str

    @property
    def want_rate(self) -> float:
        return self.wanters / self.eligible if self.eligible else 0.0

    @property
    def gold_gap(self) -> float:
        return self.wanters * self.price / 10_000

    @property
    def gold_per_wanter(self) -> float:
        return self.price / 10_000


def count_wanters(wants: np.ndarray) -> np.ndarray:
    """Column sums of a 0/1 matrix (rows = accounts, columns = appearances).

    The caller must only pass bitmaps whose ``slots`` cover the full catalogue;
    padding a short blob with zeros would turn "not covered" into "wants it".
    The DfA store keeps ``owned.slots`` for exactly that check.
    """
    return wants.sum(axis=0, dtype=np.int64)


def rank_item_union(wants: np.ndarray, slots: Sequence[int],
                    sellable: Mapping[int, tuple[int, int, str]],
                    catalogue: Mapping[int, tuple[str, str]],
                    min_wanters: int = 50, min_rate: float = 0.25,
                    exclude_sources: Sequence[str] = ("vendor", "trading post",
                                                      "quest", "profession",
                                                      "achievement"),
                    sort: str = "gold") -> list[ItemShelf]:
    """Rank BoE items by the union of buyers missing any of their appearances.

    ``wants`` is accounts x slots; ``slots`` maps column -> appearance id;
    ``sellable`` maps appearance -> (price, item_id, item_name).
    """
    if wants.shape[1] != len(slots):
        raise ValueError("wants and slots must have the same width")
    slot_of = {int(app): i for i, app in enumerate(slots)}
    denied = tuple(s.lower() for s in exclude_sources)
    by_item: dict[int, list[tuple[int, int, str]]] = {}
    for app, (price, item_id, item_name) in sellable.items():
        if int(app) in slot_of:
            by_item.setdefault(int(item_id), []).append(
                (int(app), int(price), item_name))

    eligible = wants.shape[0]
    out: list[ItemShelf] = []
    for item_id, entries in by_item.items():
        allowed = []
        for app, price, item_name in entries:
            hit = catalogue.get(app)
            if not hit:
                continue
            name, source = hit
            if any(term in source.lower() for term in denied):
                continue
            allowed.append((app, price, item_name, name, source))
        if not allowed:
            continue
        cols = [slot_of[a] for a, *_ in allowed]
        missing_any = wants[:, cols].any(axis=1)
        n = int(missing_any.sum())
        if n < min_wanters:
            continue
        rate = n / eligible if eligible else 0.0
        if rate < min_rate:
            continue
        price = min(price for _a, price, _i, _n, _s in allowed)
        if price <= 0:
            continue
        app, _price, item_name, name, source = allowed[0]
        out.append(ItemShelf(
            item_id=item_id, item_name=item_name, appearance_id=app,
            name=name, source=source, grants=len(set(cols)),
            wanters=n, eligible=eligible, price=price))
    if sort == "wanters":
        return sorted(out, key=lambda r: (-r.wanters, -r.gold_gap))
    if sort == "rate":
        return sorted(out, key=lambda r: (-r.want_rate, -r.wanters))
    return sorted(out, key=lambda r: (-r.gold_gap, -r.wanters))


def rank_demand(counts: np.ndarray, eligible: int,
                slots: Sequence[int],
                sellable: Mapping[int, tuple[int, int, str]],
                catalogue: Mapping[int, tuple[str, str]],
                min_wanters: int = 50, min_rate: float = 0.25,
                exclude_sources: Sequence[str] = ("vendor", "trading post",
                                                  "quest", "profession",
                                                  "achievement"),
                sort: str = "gold") -> list[ItemDemand]:
    """Turn per-slot counts into the ranked, sellable item list.

    ``sellable`` maps appearance_id -> (price_copper, item_id, item_name);
    ``catalogue`` maps appearance_id -> (name, source).  Both are keyed by the
    DfA appearance id that the bitmap slot represents.
    """
    if counts.shape[0] != len(slots):
        raise ValueError("counts and slots must have the same length")
    denied = tuple(s.lower() for s in exclude_sources)
    out: list[ItemDemand] = []
    for count, appearance in zip(counts, slots):
        n = int(count)
        if n < min_wanters or appearance not in sellable:
            continue
        hit = catalogue.get(int(appearance))
        if not hit:
            continue
        name, source = hit
        if any(term in source.lower() for term in denied):
            continue
        rate = n / eligible if eligible else 0.0
        if rate < min_rate:
            continue
        price, item_id, item_name = sellable[int(appearance)]
        if price <= 0:
            continue
        out.append(ItemDemand(
            appearance_id=int(appearance), name=name, source=source,
            wanters=n, eligible=eligible, price=int(price),
            item_id=int(item_id), item_name=item_name))
    if sort == "wanters":
        return sorted(out, key=lambda r: (-r.wanters, -r.gold_gap))
    if sort == "rate":
        return sorted(out, key=lambda r: (-r.want_rate, -r.wanters))
    return sorted(out, key=lambda r: (-r.gold_gap, -r.wanters))
