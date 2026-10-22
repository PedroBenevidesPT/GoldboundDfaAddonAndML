"""Who is still hungry, in items and in gold, per connected-realm market.

The DfA bitmaps say *how many* high-score collectors still lack each appearance;
the TSM snapshots say *what that appearance costs on their auction house*.  This
module joins the two without touching the network.

Two numbers matter and they answer different questions:

* ``items_per_buyer`` -- how many appearances the average collector in that
  market still lacks.  A big number means the market is far from saturated.
* ``gold_per_buyer`` -- what it would cost to buy that average missing list at
  the market's own prices.  A big number means there is a lot of addressable
  gold, even if the item count is not the largest.

A realm can score high on the first and low on the second (cheap items), or the
other way round (few, expensive pieces).  Ranking on both avoids the trap of
calling a realm "hungry" just because its sample is large.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np

# TSM stores copper; the reports speak gold.
COPPER_PER_GOLD = 10_000


@dataclass(frozen=True)
class HungerRow:
    realm: str
    canonical_slug: str
    buyers: int
    gap_items: int
    gap_gold: float  # copper
    priced_items: int
    wanted_items: int
    price_index: Optional[float]
    capped_items: int = 0

    @property
    def items_per_buyer(self) -> float:
        return self.gap_items / self.buyers if self.buyers else 0.0

    @property
    def gold_per_buyer(self) -> float:
        return self.gap_gold / self.buyers / COPPER_PER_GOLD if self.buyers else 0.0

    @property
    def coverage(self) -> float:
        return self.priced_items / self.wanted_items if self.wanted_items else 0.0


def price_of(realm_row: Optional[tuple[int, int]],
             region_value: Optional[int]) -> int:
    """Copper a seller can ask: cheapest listing, then market value, then region.

    ``min_buyout == 0`` means no listing right now, not a free item, so it must
    not win the choice.  The region market value is the last resort and is
    flagged by the caller's coverage number.
    """
    if realm_row:
        market, min_buyout = realm_row
        if min_buyout and min_buyout > 0:
            return int(min_buyout)
        if market and market > 0:
            return int(market)
    if region_value and region_value > 0:
        return int(region_value)
    return 0


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> Optional[float]:
    """Median of ``values`` weighted by ``weights``; None when nothing weighs."""
    if len(values) != len(weights) or not values:
        return None
    pairs = sorted((float(v), float(w)) for v, w in zip(values, weights) if w > 0)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    half = total / 2.0
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= half:
            return value
    return pairs[-1][0]


def compute_hunger(realm: str, canonical_slug: str, counts: np.ndarray,
                   item_ids: Sequence[int],
                   realm_prices: Mapping[int, tuple[int, int]],
                   region_prices: Mapping[int, int],
                   buyers: int, price_cap: int = 0) -> HungerRow:
    """Join one market's missing counts with its prices.

    ``counts`` is aligned with ``item_ids`` and holds, for each appearance, how
    many of the selected collectors lack it.  Prices are copper.  ``price_cap``
    (copper) stops a single 10M-gold placeholder listing from deciding the gold
    ranking; 0 disables it.
    """
    if counts.shape[0] != len(item_ids):
        raise ValueError("counts and item_ids must have the same length")
    gap_items = 0
    gap_gold = 0.0
    priced = 0
    wanted = 0
    capped = 0
    ratios: list[float] = []
    ratio_weights: list[float] = []
    for count, item_id in zip(counts, item_ids):
        n = int(count)
        if n <= 0:
            continue
        wanted += 1
        gap_items += n
        unit = price_of(realm_prices.get(int(item_id)),
                        region_prices.get(int(item_id)))
        if unit > 0:
            priced += 1
            if price_cap and unit > price_cap:
                unit = price_cap
                capped += 1
            gap_gold += n * unit
            rp = realm_prices.get(int(item_id))
            region = region_prices.get(int(item_id))
            if rp and region and rp[0] > 0 and region > 0:
                ratios.append(rp[0] / region)
                ratio_weights.append(n)
    return HungerRow(
        realm=realm,
        canonical_slug=canonical_slug,
        buyers=buyers,
        gap_items=gap_items,
        gap_gold=gap_gold,
        priced_items=priced,
        wanted_items=wanted,
        price_index=weighted_median(ratios, ratio_weights),
        capped_items=capped,
    )


def rank(rows: Iterable[HungerRow], by: str = "gold") -> list[HungerRow]:
    """Sort by addressable gold per buyer, or by items per buyer."""
    rows = list(rows)
    if by == "items":
        return sorted(rows, key=lambda r: (-r.items_per_buyer, -r.gold_per_buyer))
    return sorted(rows, key=lambda r: (-r.gold_per_buyer, -r.items_per_buyer))
