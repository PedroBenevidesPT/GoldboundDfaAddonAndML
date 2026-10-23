"""Is this item a transmog item, and how sure are we?

The classification is evidence-first and **id-keyed**.  Nothing here looks at
a name: names repeat across the two games, across realms, and across the
random-enchant suffixes of one item.  Every decision comes from an id join:

    db2_items            binding, quality, era      -> can it be sold at all
    db2_item_appearance  itemId -> appearanceId     -> does it have a look
    collector_demand     appearanceId -> missing n   -> does anyone want it

The output is a category plus the evidence that produced it.  The category
``unknown`` is a first-class answer, not a failure: an item whose DB2 rows
were never imported has no appearance row, and absence of a row is absence
of evidence, not evidence that the item has no appearance.

Categories
----------
``collectible``    has an appearance the sampled collectors still lack
``niche``          has an appearance, and a small but non-zero audience
``plain``          sellable equipment with no appearance demand
``restricted``     the look is there but the item cannot be traded freely
                   (BoP, quest-bound, or an era-Classic row)
``unknown``        no appearance row, or no demand sample to read

What this deliberately does NOT claim
-------------------------------------
An appearance id proves the item *grants* a look.  It does not prove
anyone wants that look, and it certainly does not prove a sale.  The demand
sample is a set of accounts this machine scanned, not the server
population; ``missing_pct`` is a share of that sample.  The price and
probability layers live elsewhere and must be consulted before anything is
posted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from woweconomy.analytics.collector_demand import (
    AppearanceDemand,
    default_dfa_dir,
    item_appearances,
    load_appearance_demand,
)
from woweconomy.services.db2_items import Db2Item, load_map as load_db2_map

CATEGORY_COLLECTIBLE = "collectible"
CATEGORY_NICHE = "niche"
CATEGORY_PLAIN = "plain"
CATEGORY_RESTRICTED = "restricted"
CATEGORY_UNKNOWN = "unknown"

CATEGORIES = (CATEGORY_COLLECTIBLE, CATEGORY_NICHE, CATEGORY_PLAIN,
              CATEGORY_RESTRICTED, CATEGORY_UNKNOWN)

#: Binding codes that make an item tradeable on the auction house.
#: 0 (none) and 2 (bind on equip) are the sellable ones; 1/3/4 bind to the
#: character, 7-9 bind to the account or Battle.net.
TRADEABLE_BINDINGS = (0, 2)

#: How big a share of the demand sample must still lack the look before it
#: is called collectible rather than niche.  These are labels for the size
#: of the audience, not sell / do-not-sell thresholds.
COLLECTIBLE_PCT = 50.0
NICHE_PCT = 15.0


@dataclass
class Appearance:
    """One look an item grants, with how many sampled accounts lack it."""

    appearance_id: int
    missing: int = 0
    missing_pct: Optional[float] = None
    owned_pct: Optional[float] = None


@dataclass
class TransmogVerdict:
    """The classification of one item, and the evidence behind it."""

    item_id: int
    name: str = ""
    category: str = CATEGORY_UNKNOWN
    reason: str = ""
    appearances: List[Appearance] = field(default_factory=list)
    best_missing_pct: Optional[float] = None
    binding: Optional[int] = None
    quality: Optional[int] = None
    era: int = 0
    tradeable: bool = False
    demand_sample: int = 0
    #: True when the DB2 rows were never imported, so the classification
    #: rests on nothing.  Callers must not treat this as ``plain``.
    no_metadata: bool = False

    @property
    def is_transmog(self) -> bool:
        """Does the item grant a look at all?  ``None`` evidence is excluded."""
        return bool(self.appearances)

    @property
    def has_demand(self) -> bool:
        return bool(self.appearances) and any(a.missing > 0 for a in self.appearances)


def classify_item(item_id: int,
                  appearances: Optional[Sequence[int]],
                  demand: Optional[AppearanceDemand],
                  db2: Optional[Db2Item]) -> TransmogVerdict:
    """Classify one item from its appearance ids, demand book and DB2 row.

    Every argument is optional on purpose: a missing piece must produce
    ``unknown`` with a reason, never a confident guess.
    """
    verdict = TransmogVerdict(item_id=int(item_id))
    if db2 is not None:
        verdict.name = db2.name
        verdict.binding = db2.binding
        verdict.quality = db2.quality
        verdict.era = int(db2.era or 0)
        verdict.tradeable = db2.binding in TRADEABLE_BINDINGS

    appearance_ids = list(appearances or [])
    if not appearance_ids:
        verdict.no_metadata = True
        verdict.category = CATEGORY_UNKNOWN
        verdict.reason = ("no row in db2_item_appearance: either the DB2 was "
                          "never imported or the item grants no look; the two "
                          "cannot be told apart here")
        return verdict

    sample = int(demand.sample) if demand is not None else 0
    verdict.demand_sample = sample
    for appearance_id in appearance_ids:
        row = demand.row_for([appearance_id]) if demand is not None else None
        if row is None:
            verdict.appearances.append(Appearance(appearance_id=int(appearance_id)))
            continue
        verdict.appearances.append(Appearance(
            appearance_id=int(appearance_id),
            missing=int(row.missing),
            missing_pct=row.missing_pct,
            owned_pct=row.owned_pct))
    verdict.appearances.sort(key=lambda a: (-(a.missing_pct or 0.0), a.appearance_id))
    best = next((a for a in verdict.appearances if a.missing > 0), None)
    verdict.best_missing_pct = best.missing_pct if best else None

    # A look that nobody in the sample lacks is still a look: the item is
    # transmog, it just has no measured audience.
    if best is None:
        if sample <= 0:
            verdict.category = CATEGORY_UNKNOWN
            verdict.reason = ("the item grants a look, but no demand sample "
                              "was available to say whether anyone lacks it")
        else:
            verdict.category = CATEGORY_PLAIN
            verdict.reason = (f"grants {len(appearance_ids)} look(s); none of "
                              f"the {sample} sampled accounts lacks any of them")
        return verdict

    if not verdict.tradeable:
        verdict.category = CATEGORY_RESTRICTED
        verdict.reason = (f"binding={verdict.binding} is not tradeable, so the "
                          f"look cannot be sold on the auction house")
        return verdict
    if verdict.era != 0:
        verdict.category = CATEGORY_RESTRICTED
        verdict.reason = (f"era={verdict.era} is an era-Classic row; its price "
                          f"must not enter a retail expected value")
        return verdict

    pct = best.missing_pct
    if pct is None:
        verdict.category = CATEGORY_UNKNOWN
        verdict.reason = "the demand row carried no sample size, so no share exists"
    elif pct >= COLLECTIBLE_PCT:
        verdict.category = CATEGORY_COLLECTIBLE
        verdict.reason = (f"{pct:.1f}% of {sample} sampled accounts lack "
                          f"appearance {best.appearance_id}")
    elif pct >= NICHE_PCT:
        verdict.category = CATEGORY_NICHE
        verdict.reason = (f"{pct:.1f}% of {sample} sampled accounts lack "
                          f"appearance {best.appearance_id}")
    else:
        verdict.category = CATEGORY_PLAIN
        verdict.reason = (f"only {pct:.1f}% of {sample} sampled accounts lack "
                          f"appearance {best.appearance_id}")
    return verdict


class TransmogClassifier:
    """Batch classifier.  Loads each source once, then answers per item."""

    def __init__(self, db_path: Path,
                 dfa_dir: Optional[Path] = None,
                 db2: Optional[Dict[int, Db2Item]] = None,
                 demand: Optional[AppearanceDemand] = None) -> None:
        self.db_path = Path(db_path)
        self.db2 = db2 if db2 is not None else load_db2_map(self.db_path)
        folder = Path(dfa_dir) if dfa_dir else default_dfa_dir()
        self.demand = demand
        if self.demand is None and folder is not None and Path(folder).exists():
            try:
                self.demand = load_appearance_demand(Path(folder))
            except Exception:  # pragma: no cover - unreadable DfA cache
                self.demand = None
        self._appearance_cache: Dict[int, List[int]] = {}

    def appearances(self, item_ids: Sequence[int]) -> Dict[int, List[int]]:
        wanted = [int(i) for i in item_ids if int(i) not in self._appearance_cache]
        if wanted:
            found = item_appearances(str(self.db_path), wanted)
            for item_id, ids in found.items():
                self._appearance_cache.setdefault(int(item_id), list(ids))
        return {int(i): list(self._appearance_cache.get(int(i), []))
                for i in item_ids}

    def classify(self, item_ids: Sequence[int]) -> List[TransmogVerdict]:
        wanted = list(dict.fromkeys(int(i) for i in item_ids))
        appearances = self.appearances(wanted)
        return [self.classify_one(i) for i in wanted]

    def classify_one(self, item_id: int) -> TransmogVerdict:
        item_id = int(item_id)
        if item_id not in self._appearance_cache:
            found = item_appearances(str(self.db_path), [item_id])
            self._appearance_cache.update(found)
        return classify_item(item_id,
                             self._appearance_cache.get(item_id),
                             self.demand,
                             self.db2.get(item_id))

    def summary(self, verdicts: Sequence[TransmogVerdict]) -> Dict[str, int]:
        out = {category: 0 for category in CATEGORIES}
        for verdict in verdicts:
            out[verdict.category] = out.get(verdict.category, 0) + 1
        return out
