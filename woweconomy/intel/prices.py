"""Price validation for auction-house observations.

The auction payload is untrusted input.  A price can be absent, can be a
vendor sentinel, can be arithmetically impossible, or can be so far from
the item's own distribution that it is not a market observation at all.

The rule this module enforces is the one the project already states
elsewhere: **a raw observation is never destroyed**.  The value the API
sent stays in ``ah_auction.unit_price_copper``; this module only attaches
a status that says whether the value may be *used* as a price.  A
consumer that wants a price filters on :data:`OK`; an auditor reads the
raw column and the status side by side.

Statuses
--------
``ok``          the value is usable as a price
``missing``     the API sent no price (stored as NULL, never as zero)
``sentinel``    the value is at or above the known "no data" ceiling, which
                is also where the client's maximum buyout sits
``impossible``  non-positive, above the gold cap, or otherwise invalid
``outlier``     plausible arithmetic, but far outside the item's own
                distribution in the same snapshot -- kept, flagged, and
                excluded from aggregates by default

``outlier`` is deliberately advisory.  It is computed against the item's
median *in the same snapshot*, so it never needs history, and it is
flagged rather than dropped because a genuine one-off (a rare transmog
appearing after months) must not be deleted from the record.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple

#: TSM writes this when it has no data.  It is not a price.  The constant
#: is duplicated from :mod:`woweconomy.ml.config` on purpose: the ingest
#: layer must not import the learning engine.  It doubles as the ceiling
#: above which an auction price is a parking price rather than a market
#: observation -- see :func:`classify_static`.
SENTINEL_COPPER: int = 99_999_999_000

COPPER_PER_GOLD: int = 10_000

#: The in-game gold cap.  A single unit cannot cost more than this.
GOLD_CAP_COPPER: int = 10_000_000 * COPPER_PER_GOLD

#: An outlier must be at least this many times the item's own median...
OUTLIER_FACTOR: float = 1000.0
#: ...and at least this many copper in absolute terms, so a cheap item
#: with a noisy median cannot flag a normal listing.
OUTLIER_FLOOR_COPPER: int = 100_000_000  # 10,000 gold

OK = "ok"
MISSING = "missing"
SENTINEL = "sentinel"
IMPOSSIBLE = "impossible"
OUTLIER = "outlier"

#: The statuses a price consumer may treat as a price.
USABLE = (OK,)

#: The statuses that must be excluded from any average, percentile or
#: aggregate.  ``outlier`` is excluded by default but kept in the table.
EXCLUDED_FROM_AGGREGATES = (SENTINEL, IMPOSSIBLE, OUTLIER)


@dataclass(frozen=True)
class PriceVerdict:
    """The status of one observed unit price, with the reason."""

    status: str
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.status == OK


def classify_static(unit_price_copper: Optional[int], *,
                    is_commodity: bool = False) -> PriceVerdict:
    """Validate a price using only the value itself.

    Runs at parse time, before anything is written, so an impossible
    value is labelled on the way in rather than discovered later.
    """
    if unit_price_copper is None:
        return PriceVerdict(MISSING, "the API sent no unit price")
    if unit_price_copper <= 0:
        return PriceVerdict(IMPOSSIBLE, "non-positive unit price")
    if unit_price_copper > GOLD_CAP_COPPER:
        # Checked before the sentinel: anything above the gold cap is
        # arithmetically impossible, and that is a different statement
        # from "priced at the ceiling".
        return PriceVerdict(IMPOSSIBLE, "above the in-game gold cap")
    if unit_price_copper >= SENTINEL_COPPER:
        # ``>=``, not ``==``, and for a measured reason: the auction book
        # contains no row at exactly 99,999,999,000c, but 2,749 rows sit at
        # 99,999,999,900c -- 9,999,999g 99s, the maximum buyout the client
        # will accept.  Those are items parked at the ceiling, not prices
        # anyone trades at, and ``reference_price`` has always treated
        # anything at or above this constant as unusable.  An exact match
        # would have let every one of them through.
        return PriceVerdict(SENTINEL,
                            "value is at or above the no-data ceiling "
                            f"({SENTINEL_COPPER})")
    # Commodities are quoted per unit and legitimately reach the cap on
    # rare materials; nothing else may.
    return PriceVerdict(OK, "")


def classify_against_median(unit_price_copper: Optional[int],
                            item_median: Optional[float], *,
                            factor: float = OUTLIER_FACTOR,
                            floor_copper: int = OUTLIER_FLOOR_COPPER
                            ) -> PriceVerdict:
    """Second pass: is the value far outside the item's own snapshot?

    ``item_median`` is the median of the *other* usable observations of
    the same product in the same snapshot.  When there is nothing to
    compare against, the value is left as it was: absence of evidence is
    not evidence of an outlier.
    """
    base = classify_static(unit_price_copper)
    if base.status != OK:
        return base
    if item_median is None or item_median <= 0:
        return base
    threshold = max(factor * float(item_median), float(floor_copper))
    if float(unit_price_copper) > threshold:
        return PriceVerdict(
            OUTLIER,
            f"{unit_price_copper} > {threshold:.0f} "
            f"({factor:.0f}x the snapshot median {item_median:.0f})")
    return base


def _median(values) -> Optional[float]:
    ordered = sorted(v for v in values if v is not None and v > 0)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def mark_outliers(con: sqlite3.Connection, snapshot_id: str, *,
                  factor: float = OUTLIER_FACTOR,
                  floor_copper: int = OUTLIER_FLOOR_COPPER) -> int:
    """Flag outliers in one snapshot, in place, deterministically.

    Groups by **variant**, not by ``item_id``: item 82800 is the caged-pet
    container, where one id covers hundreds of species priced from a few
    gold to the gold cap.  A median over that mixture describes no good
    that exists, and comparing a 500,000g pet against it would either flag
    a normal listing or fail to flag an absurd one.  Rows whose
    ``variant_key`` was never computed fall back to ``item_id``, so a
    half-migrated table still gets a defensible answer instead of an
    exception.

    Returns the number of rows whose status changed.  Running it twice
    changes nothing the second time, which is what makes a rebuild
    reproducible.

    The reference median is taken over every *statically valid* row, not
    over the rows that are currently ``ok``.  Those differ on a second
    pass -- the first pass moved some rows to ``outlier`` -- and a median
    computed from the survivors is a different number, so the rule would
    keep flagging a few more rows on every run (36 of them, measured on
    the 2026-10-22 backfill).  Using the full statically valid pool makes
    the verdict a function of the raw data alone, and lets a row that was
    flagged once return to ``ok`` if the pool later says it is ordinary.
    """
    rows = con.execute(
        "SELECT COALESCE(variant_key, 'item:' || item_id) AS product,"
        " auction_key, item_id, unit_price_copper, price_status FROM ah_auction"
        " WHERE snapshot_id = ? AND price_status IN (?, ?)",
        (snapshot_id, OK, OUTLIER)).fetchall()
    by_product: dict = {}
    for row in rows:
        by_product.setdefault(row["product"], []).append(row)

    updates = []
    for product, observations in by_product.items():
        prices = [row["unit_price_copper"] for row in observations]
        median = _median(prices) if len(prices) >= 3 else None
        if median is None:
            # Two observations have no meaningful median.  A row flagged by
            # an earlier, differently-scoped pass must not keep a verdict
            # this pass cannot justify, so it returns to its static status.
            for row in observations:
                verdict = classify_static(row["unit_price_copper"])
                if verdict.status != row["price_status"]:
                    updates.append((verdict.status, verdict.reason,
                                    snapshot_id, row["auction_key"]))
            continue
        for row in observations:
            verdict = classify_against_median(row["unit_price_copper"], median,
                                              factor=factor,
                                              floor_copper=floor_copper)
            if verdict.status != row["price_status"]:
                updates.append((verdict.status, verdict.reason, snapshot_id,
                                row["auction_key"]))
    if not updates:
        return 0
    con.executemany(
        "UPDATE ah_auction SET price_status = ?, price_reason = ?"
        " WHERE snapshot_id = ? AND auction_key = ?",
        updates)
    con.commit()
    return len(updates)


#: SQL predicate for "this row carries a price that may be aggregated".
def usable_price_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}price_status = '{OK}'"


def status_counts(con: sqlite3.Connection,
                  snapshot_id: Optional[str] = None) -> dict:
    """How many rows carry each status -- the audit view of one snapshot."""
    sql = ("SELECT price_status, COUNT(*) FROM ah_auction"
           + (" WHERE snapshot_id = ?" if snapshot_id else "")
           + " GROUP BY price_status")
    params: Tuple = (snapshot_id,) if snapshot_id else ()
    return {status: count for status, count in con.execute(sql, params)}
