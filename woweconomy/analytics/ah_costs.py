"""Auction-house transaction costs, measured from the account's own sales.

Why this module exists
---------------------
The posting and relisting maths needs three numbers: the auction-house cut,
the deposit, and the minimum deposit.  They were previously *assumed*
(``deposit_pct = 0.05``, ``deposit_min_gold = 1``,
``deposit_vendor_cap_pct = 3.0`` in ``pricing_engine``).  The project's own
sale records say otherwise, and an assumption that is wrong by 3x turns
every "is this worth posting?" answer into noise.

The constants below are therefore **measured**, not chosen, and
:func:`measure_from_db` re-derives them from the data so the fit can be
re-checked after every import.  A constant that stops fitting the data is a
bug report, not a rounding error.

What was measured
-----------------
577 completed sales in ``tsm_markets.db :: auction_events`` carry
``deposit_copper``, ``fee_copper``, ``net_proceeds_copper`` and
``total_price_copper``.

* **The cut is 5%.**  ``net_proceeds_copper / total_price_copper`` is
  0.95 for 205 of 577 rows and 0.950-0.968 overall (median 0.9525).
* **The deposit is 15% of the vendor price per unit, floored at 100c.**
  ``max(0.15 * sellPrice * qty, 100)`` reproduces 343 of the 404 rows that
  have a vendor price, to the copper.
* The old constants reproduce **0 of 404**.

The units trap
--------------
``unit_price_copper`` is **per unit** while ``net_proceeds_copper`` and
``total_price_copper`` are **stack totals**.  Comparing them directly
gives ``net / unit_price`` with a median of 3.8 and a maximum of 266, which
looks like a data error and is not one.  Every function here takes the
stack total explicitly so the mistake cannot be made by accident.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Sequence

#: The auction house keeps 5% of the sale.  Measured, not assumed.
AH_CUT: float = 0.05

#: The deposit is 15% of the item's vendor price, per unit.  Measured.
DEPOSIT_RATE: float = 0.15

#: The smallest deposit the game will charge, in copper.  Note this is 10
#: *silver*, not 1 gold: no row in the data has a deposit of exactly
#: 10,000c, and 96 rows sit at 100c.
DEPOSIT_FLOOR_COPPER: int = 100

COPPER_PER_GOLD: int = 10_000


@dataclass(frozen=True)
class CostAudit:
    """How well the constants reproduce the account's real sales."""

    sale_rows: int = 0
    deposit_rows: int = 0
    deposit_exact: int = 0
    cut_rows: int = 0
    cut_within_half_pct: int = 0
    notes: Sequence[str] = field(default_factory=tuple)

    @property
    def deposit_fit(self) -> float:
        return (self.deposit_exact / self.deposit_rows) if self.deposit_rows else 0.0

    @property
    def cut_fit(self) -> float:
        return (self.cut_within_half_pct / self.cut_rows) if self.cut_rows else 0.0

    @property
    def deposit_fits(self) -> bool:
        return self.deposit_rows > 0 and self.deposit_fit >= 0.80

    @property
    def cut_fits(self) -> bool:
        return self.cut_rows > 0 and self.cut_fit >= 0.50

    @property
    def trustworthy(self) -> bool:
        return self.deposit_fits and self.cut_fits

    def as_dict(self) -> Dict[str, object]:
        return {
            "sale_rows": self.sale_rows,
            "deposit_rows": self.deposit_rows,
            "deposit_exact": self.deposit_exact,
            "deposit_fit": round(self.deposit_fit, 4),
            "cut_rows": self.cut_rows,
            "cut_within_half_pct": self.cut_within_half_pct,
            "cut_fit": round(self.cut_fit, 4),
            "trustworthy": self.trustworthy,
            "notes": list(self.notes),
        }


def stack_total(unit_price_copper: float, quantity: int) -> float:
    """The stack total.  ``quantity`` below 1 is treated as 1.

    This is the whole point of the helper: it forces the caller to say
    whether a number is a unit price or a stack total.
    """
    qty = max(1, int(quantity or 1))
    return float(unit_price_copper) * qty


def deposit_copper(vendor_copper: Optional[float], quantity: int) -> int:
    """The deposit for one posting of ``quantity`` units.

    ``max(0.15 * vendor * qty, 100c)``.  A missing or zero vendor price
    still costs the floor, because the game charges a deposit either way;
    it is never zero.
    """
    qty = max(1, int(quantity or 1))
    if vendor_copper is None or vendor_copper <= 0:
        return DEPOSIT_FLOOR_COPPER
    return int(max(DEPOSIT_RATE * float(vendor_copper) * qty,
                   float(DEPOSIT_FLOOR_COPPER)))


def net_proceeds(total_copper: float) -> float:
    """What the seller receives for a stack that sold at ``total_copper``."""
    return float(total_copper) * (1.0 - AH_CUT)


def net_proceeds_unit(unit_price_copper: float, quantity: int) -> float:
    return net_proceeds(stack_total(unit_price_copper, quantity))


def measure_from_db(db_path: Path,
                    event_table: str = "auction_events") -> CostAudit:
    """Re-derive the constants from the account's own completed sales.

    Read-only.  Runs the two fits and reports how many rows each explains,
    so a data import that changes the picture is visible immediately.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return CostAudit(notes=(f"{db_path} does not exist",))
    from woweconomy.dbcompat import connect_store, missing_table_errors

    con = connect_store(db_path, readonly=True)
    try:
        try:
            rows = con.execute(
                f"SELECT e.unit_price_copper AS p, e.qty AS q,"
                f" e.deposit_copper AS d, e.total_price_copper AS tp,"
                f" e.net_proceeds_copper AS np, b.sellPrice AS vendor"
                f" FROM {event_table} e"
                f" LEFT JOIN blizzard_items b ON b.itemId = e.item_id"
                f" WHERE e.event_type = 'sale' AND e.unit_price_copper > 0"
                f"   AND e.deposit_copper > 0").fetchall()
        except missing_table_errors() as exc:
            return CostAudit(notes=(f"{event_table} could not be read: {exc}",))
    finally:
        con.close()

    sale_rows = len(rows)
    if not sale_rows:
        return CostAudit(notes=("no completed sales with a deposit",))

    deposit_rows = deposit_exact = 0
    for row in rows:
        vendor = row["vendor"]
        if vendor is None or vendor <= 0:
            continue
        deposit_rows += 1
        qty = max(1, int(row["q"] or 1))
        expected = max(DEPOSIT_RATE * float(vendor) * qty,
                       float(DEPOSIT_FLOOR_COPPER))
        if abs(float(row["d"]) - expected) < 1:
            deposit_exact += 1

    cut_rows = cut_ok = 0
    for row in rows:
        total = row["tp"]
        if not total or total <= 0:
            continue
        cut_rows += 1
        if abs(float(row["np"]) / float(total) - (1.0 - AH_CUT)) <= 0.005:
            cut_ok += 1

    notes = []
    if deposit_rows and deposit_exact / deposit_rows < 0.80:
        notes.append(
            f"the deposit formula explains only {deposit_exact}/{deposit_rows}"
            f" rows; the mechanic or the vendor prices may have moved")
    if cut_rows and cut_ok / cut_rows < 0.50:
        notes.append(
            f"the 5% cut explains only {cut_ok}/{cut_rows} rows")
    return CostAudit(
        sale_rows=sale_rows,
        deposit_rows=deposit_rows,
        deposit_exact=deposit_exact,
        cut_rows=cut_rows,
        cut_within_half_pct=cut_ok,
        notes=tuple(notes))


#: The constants the code used before they were measured, kept so the
#: regression test can show they explain nothing.
LEGACY_DEPOSIT_PCT = 0.05
LEGACY_DEPOSIT_MIN_GOLD = 1
LEGACY_DEPOSIT_VENDOR_CAP_PCT = 3.0


def legacy_deposit_copper(unit_price_copper: float,
                          vendor_copper: Optional[float],
                          quantity: int) -> int:
    """The old assumption, for the audit that shows it does not fit."""
    qty = max(1, int(quantity or 1))
    base = max(LEGACY_DEPOSIT_PCT * float(unit_price_copper or 0),
               LEGACY_DEPOSIT_MIN_GOLD * COPPER_PER_GOLD)
    if vendor_copper is not None and vendor_copper > 0:
        base = min(base, LEGACY_DEPOSIT_VENDOR_CAP_PCT * float(vendor_copper))
    return int(base)
