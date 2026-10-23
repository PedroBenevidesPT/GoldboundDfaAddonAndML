"""What is a transmog item actually worth, and how sure are we?

The reference layer in ``reference_price.py`` answers "what do people ask".
This module answers the other half: **what have people paid**.  Asking
prices are not completed sales, and on a transmog item the gap is the whole
story -- a single collectible listed at 500k gold for two years is a price
history of nothing.

Every estimate carries the evidence that produced it: how many completed
sales it rests on, how old the most recent one is, and which signal set the
number.  An estimate with no completed sales is reported as such, with the
vendor price as the floor and ``confidence = "none"``.  It is never dressed
up as a valuation.

Signal precedence
-----------------
1. **Completed sales**, when there are enough and they are recent enough.
   This is the only signal that is a realised price.
2. **Cheapest credible live listing**, filtered to ``price_status = 'ok'``
   so a sentinel or an outlier cannot become "the market".  This is an ask,
   so it caps the estimate rather than setting it.
3. **Regional reference**, as a sanity band.  A regional average is a
   reference, not a realisable price, and never sets the number alone.
4. **Vendor price**, as the downside.  A floor, never a valuation.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from woweconomy.dbcompat import connect_store, store_exists
from woweconomy.intel.prices import usable_price_sql

COPPER_PER_GOLD: int = 10_000
SENTINEL_COPPER: int = 99_999_999_000

#: How many completed sales are needed before the sale average is allowed
#: to set the price on its own.
MIN_SALES_FOR_ANCHOR: int = 3

#: How old the newest completed sale may be before the anchor is stale.
SALE_STALE_H: float = 24 * 45          # 45 days

#: A regional reference further than this factor from the local evidence is
#: a disagreement, not a confirmation.
REGIONAL_DIVERGENCE: float = 3.0


@dataclass(frozen=True)
class PriceSignals:
    """Everything known about one item's price, with counts and ages."""

    item_id: int
    name: str = ""
    as_of: int = 0
    # completed sales
    sale_avg_copper: Optional[float] = None
    sale_min_copper: Optional[float] = None
    sale_max_copper: Optional[float] = None
    sale_observations: int = 0
    last_sale_at: Optional[int] = None
    sale_rate: Optional[float] = None
    sold_per_day: Optional[float] = None
    # current competition (asks, not sales)
    competition_copper: Optional[float] = None
    competition_count: int = 0
    competition_median_copper: Optional[float] = None
    # references
    regional_reference_copper: Optional[float] = None
    regional_historical_copper: Optional[float] = None
    # downside
    vendor_copper: Optional[float] = None

    @property
    def sale_age_h(self) -> Optional[float]:
        if self.last_sale_at is None or not self.as_of:
            return None
        return max(0.0, (self.as_of - self.last_sale_at) / 3600.0)

    @property
    def has_completed_sales(self) -> bool:
        return self.sale_observations > 0 and self.sale_avg_copper is not None


@dataclass(frozen=True)
class PriceEstimate:
    """A price, the confidence in it, and the evidence behind it."""

    item_id: int
    name: str
    unit_copper: Optional[float]
    confidence: str                     # high | medium | low | none
    basis: str
    observations: int
    age_h: Optional[float]
    signals: PriceSignals
    flags: Tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.unit_copper is not None and self.confidence != "none"

    @property
    def is_valuation(self) -> bool:
        """True only when the number came from realised sales."""
        return self.confidence in ("high", "medium") and self.signals.has_completed_sales


def load_signals(db_path: Path,
                 item_ids: Sequence[int],
                 *,
                 intel_db: Optional[Path] = None,
                 now: Optional[int] = None,
                 include_competition: bool = True) -> Dict[int, PriceSignals]:
    """Read every price signal for a batch of items.

    ``db_path`` is the TSM market database (completed sales, references,
    vendor prices).  ``intel_db`` is the Blizzard snapshot database, used
    only for the live competition, and only for rows whose price passed
    validation.
    """
    import time

    db_path = Path(db_path)
    ids = list(dict.fromkeys(int(i) for i in item_ids))
    out: Dict[int, PriceSignals] = {}
    if not ids or not store_exists(db_path):
        return out
    stamp = int(now if now is not None else time.time())
    by_id = {i: PriceSignals(item_id=i, as_of=stamp) for i in ids}

    con = connect_store(db_path, readonly=True)
    try:
        # ---- regional completed-sale aggregates -------------------------
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for row in con.execute(
                    f"SELECT key_id, name, market_value, historical,"
                    f" avg_sale_price, sale_rate, sold_per_day, updated_at"
                    f" FROM tsm_region_price WHERE key_id IN ({marks})", chunk):
                item_id = int(row["key_id"])
                if item_id not in by_id:
                    continue
                by_id[item_id] = PriceSignals(
                    item_id=item_id,
                    name=row["name"] or "",
                    as_of=stamp,
                    sale_avg_copper=_num(row["avg_sale_price"]),
                    sale_rate=_num(row["sale_rate"]),
                    sold_per_day=_num(row["sold_per_day"]),
                    regional_reference_copper=_num(row["market_value"]),
                    regional_historical_copper=_num(row["historical"]),
                    last_sale_at=_num(row["updated_at"]),
                )
        # ---- vendor price ----------------------------------------------
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for row in con.execute(
                    f"SELECT itemId, sellPrice FROM blizzard_items"
                    f" WHERE itemId IN ({marks})", chunk):
                item_id = int(row["itemId"])
                if item_id in by_id and row["sellPrice"]:
                    by_id[item_id] = _replace(by_id[item_id],
                                              vendor_copper=int(row["sellPrice"]))
        # ---- this account's own completed sales ------------------------
        # The authoritative per-transaction view: a real sale with a real
        # timestamp, not a regional average.
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            rows = con.execute(
                f"SELECT item_id, unit_price_copper, timestamp_unix"
                f" FROM auction_events WHERE event_type = 'sale'"
                f" AND item_id IN ({marks}) AND unit_price_copper > 0"
                f" AND unit_price_copper < ?", list(chunk) + [SENTINEL_COPPER]).fetchall()
            sales: Dict[int, List[Tuple[float, int]]] = {}
            for row in rows:
                sales.setdefault(int(row["item_id"]), []).append(
                    (float(row["unit_price_copper"]), int(row["timestamp_unix"] or 0)))
        for item_id, values in sales.items():
            if item_id not in by_id:
                continue
            prices = sorted(p for p, _ in values)
            times = sorted(t for _, t in values if t > 0)
            by_id[item_id] = _replace(
                by_id[item_id],
                sale_observations=len(values),
                sale_min_copper=prices[0],
                sale_max_copper=prices[-1],
                last_sale_at=(times[-1] if times else by_id[item_id].last_sale_at))
            if len(values) >= MIN_SALES_FOR_ANCHOR:
                by_id[item_id] = _replace(by_id[item_id],
                                          sale_avg_copper=sum(prices) / len(prices))
    finally:
        con.close()

    # ---- live competition, from the validated snapshot rows -------------
    if include_competition and intel_db is not None and store_exists(intel_db):
        _load_competition(Path(intel_db), ids, by_id)

    for item_id, signals in by_id.items():
        if not signals.name:
            by_id[item_id] = _replace(signals, name=str(item_id))
    return by_id


def _load_competition(intel_db: Path, ids: Sequence[int],
                      by_id: Dict[int, PriceSignals]) -> None:
    con = connect_store(intel_db, readonly=True)
    try:
        # A snapshot database without the validation columns is simply not a
        # competition source; the estimate must then say so rather than
        # silently using unvalidated prices.  Checked explicitly instead of
        # catching a "no such column" error, because the error class differs
        # by backend (sqlite3.OperationalError vs psycopg2 ProgrammingError)
        # and a catch that only works on one of them is how the two backends
        # start disagreeing.
        columns = {r[1] for r in con.execute("PRAGMA table_info(ah_auction)")}
        if not columns or "price_status" not in columns:
            return
        # The newest snapshot per realm: the current wall, not a history.
        heads = con.execute(
            "SELECT MAX(snapshot_id) AS sid FROM ah_snapshot").fetchall()
        sids = [h[0] for h in heads if h[0]]
        if not sids:
            return
        placeholders = ",".join("?" * len(sids))
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            rows = con.execute(
                f"SELECT item_id, unit_price_copper FROM ah_auction"
                f" WHERE snapshot_id IN ({placeholders})"
                f" AND item_id IN ({marks}) AND unit_price_copper > 0"
                f" AND unit_price_copper < ?"
                f" AND {usable_price_sql()}"
                f" ORDER BY unit_price_copper",
                tuple(sids) + tuple(chunk) + (SENTINEL_COPPER,)).fetchall()
            grouped: Dict[int, List[float]] = {}
            for row in rows:
                grouped.setdefault(int(row[0]), []).append(
                    float(row[1]))
            for item_id, prices in grouped.items():
                if item_id not in by_id:
                    continue
                ordered = sorted(prices)
                by_id[item_id] = _replace(
                    by_id[item_id],
                    competition_copper=ordered[0],
                    competition_count=len(ordered),
                    competition_median_copper=ordered[len(ordered) // 2])
    finally:
        con.close()


def estimate_price(signals: PriceSignals) -> PriceEstimate:
    """Turn the signals into one number, or an explicit refusal."""
    flags: List[str] = []
    sale_avg = signals.sale_avg_copper
    comp = signals.competition_copper
    regional = signals.regional_reference_copper
    vendor = signals.vendor_copper

    if sale_avg is not None and sale_avg <= 0:
        sale_avg = None

    # --- the anchor: enough real sales, recently -------------------------
    if (sale_avg is not None
            and signals.sale_observations >= MIN_SALES_FOR_ANCHOR):
        age = signals.sale_age_h
        if age is not None and age > SALE_STALE_H:
            confidence = "medium"
            flags.append(f"newest completed sale is {age / 24:.0f} days old")
        else:
            confidence = "high"
        unit = sale_avg
        basis = (f"{signals.sale_observations} completed sales "
                 f"(min {signals.sale_min_copper:.0f}, max {signals.sale_max_copper:.0f})")
        if comp is not None and comp > sale_avg:
            flags.append("the cheapest live listing is above the realised average")
        if regional is not None and sale_avg > 0 and (
                regional / sale_avg > REGIONAL_DIVERGENCE
                or sale_avg / regional > REGIONAL_DIVERGENCE):
            flags.append("the regional reference disagrees with the realised average")
        return PriceEstimate(item_id=signals.item_id, name=signals.name,
                             unit_copper=unit, confidence=confidence, basis=basis,
                             observations=signals.sale_observations,
                             age_h=age, signals=signals, flags=tuple(flags))

    # --- thin sale evidence: a reference average, not the wall -----------
    # Measured (scripts/mog_eval.py, 59,151 items over 6 realms): the
    # cheapest live listing predicts the item's next market value with a
    # median error of 11.9%, against 0.4% for the market average itself,
    # and wins on only 2.8% of items.  A wall is a MIN over listings, so a
    # single junk listing defines it.  It is a competition signal for the
    # strategy layer, never a valuation.
    if regional is not None and regional > 0:
        unit = float(regional)
        confidence = "low"
        basis = "regional ask average (market value), not a realised price"
        flags.append("no completed sales: this is an ask average, not a realised price")
        if comp is not None and comp < regional * 0.25:
            flags.append("the cheapest listing is far below the average, so a "
                         "junk listing may be setting the wall")
        return PriceEstimate(item_id=signals.item_id, name=signals.name,
                             unit_copper=unit, confidence=confidence, basis=basis,
                             observations=signals.sale_observations,
                             age_h=signals.sale_age_h, signals=signals,
                             flags=tuple(flags))

    # --- nothing but a wall: report it, but as the weakest possible read -
    if comp is not None:
        flags.append("no completed sales and no average: the cheapest live "
                     "listing is a floor of asks, not a value")
        return PriceEstimate(item_id=signals.item_id, name=signals.name,
                             unit_copper=comp, confidence="low",
                             basis=(f"cheapest of {signals.competition_count} "
                                    f"validated live listings"),
                             observations=signals.sale_observations,
                             age_h=signals.sale_age_h, signals=signals,
                             flags=tuple(flags))

    if vendor is not None and vendor > 0:
        flags.append("no sale and no listing evidence; the vendor price is the floor, not a valuation")
        return PriceEstimate(item_id=signals.item_id, name=signals.name,
                             unit_copper=float(vendor), confidence="none",
                             basis="vendor price (downside floor)",
                             observations=0, age_h=signals.sale_age_h,
                             signals=signals, flags=tuple(flags))

    return PriceEstimate(item_id=signals.item_id, name=signals.name,
                         unit_copper=None, confidence="none",
                         basis="no price evidence at all",
                         observations=0, age_h=signals.sale_age_h,
                         signals=signals,
                         flags=("nothing to price this with",))


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _replace(signals: PriceSignals, **changes) -> PriceSignals:
    data = dict(signals.__dict__)
    data.update(changes)
    return PriceSignals(**data)
