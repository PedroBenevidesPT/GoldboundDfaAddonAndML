"""Can we see our own listings in the Blizzard auction house data?

The question the pipeline needs answered is not "did we sell something" -- the
addon reports that -- but "was the thing we sold visible in the Blizzard
snapshot before it sold".  Three answers follow from it, and they are all
things the rest of the system assumes:

* if our listing **is** in the snapshot, then the competition wall we measure
  includes us, and an item we are the only seller of looks like it has
  competition;
* if it is **not**, then the snapshot is a partial view and a thin wall is not
  evidence of a thin market;
* if the snapshot period does not overlap our sales at all, then neither
  question can be answered yet, and the honest report says so rather than
  printing a zero that reads like "we were never there".

That last case is not hypothetical: the recorded sales run from 2026-10-16 to
2026-10-21 and the snapshots begin on 2026-10-21, so there is no overlap to
measure.  A report that answered "0 of 557 sales were seen" would be a lie
about the data, not a finding about the market.

What is deliberately not claimed
--------------------------------
A match is evidence, not proof.  Two sellers listing the same item at the same
copper price produce the same observation, and the Blizzard API does not say
who posted a listing.  So a match is reported as "a listing at this price
existed", and only a same-price match is counted as "consistent with ours".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from woweconomy.dbcompat import connect_store, store_exists

#: How far before a sale to look for the listing that produced it.  A listing
#: has to exist in a snapshot *before* the sale; a day is long enough to cover
#: the snapshot cadence without reaching back to a previous posting of the
#: same item at a different price.
DEFAULT_LOOKBACK_S = 86_400


@dataclass
class SaleMatch:
    """One sale, and what the snapshots said about its item beforehand."""

    event_id: str
    item_id: int
    realm: str
    sold_at: int
    unit_price_copper: int
    #: how many listings of this item the snapshots held in the lookback window
    listings: int = 0
    #: the cheapest and dearest of them, to see how wide the wall was
    wall_low_copper: Optional[int] = None
    wall_high_copper: Optional[int] = None
    #: listings at exactly the price we sold for: consistent with ours
    same_price: int = 0

    @property
    def visible(self) -> bool:
        return self.listings > 0

    @property
    def consistent_with_ours(self) -> bool:
        return self.same_price > 0


@dataclass
class VisibilityReport:
    """What the check could and could not answer."""

    sales_total: int = 0
    sales_covered: int = 0
    matched: List[SaleMatch] = field(default_factory=list)
    #: sales that fall inside the snapshot period but were not in any snapshot
    unmatched: List[SaleMatch] = field(default_factory=list)
    #: sales from before the first snapshot, which cannot be checked at all
    before_snapshots: int = 0
    first_sale_at: Optional[int] = None
    last_sale_at: Optional[int] = None
    first_snapshot_at: Optional[int] = None
    last_snapshot_at: Optional[int] = None
    elapsed_s: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def overlap(self) -> bool:
        """Whether any sale at all falls inside the snapshot period.

        Not "do the two date ranges touch": the last sale and the first
        snapshot can be the same day and still leave every sale before every
        snapshot.  What matters is whether there is a single sale the
        snapshots could have seen.
        """
        return self.sales_covered > 0

    def same_price_matches(self) -> List[SaleMatch]:
        return [m for m in self.matched if m.consistent_with_ours]

    def summary(self) -> str:
        lines = [
            f"sale visibility checked in {self.elapsed_s:.1f}s",
            f"  {self.sales_total} sale(s) recorded, "
            f"{self.sales_covered} inside the snapshot period",
        ]
        if self.first_sale_at and self.last_sale_at:
            lines.append(
                f"  sales span       {_day(self.first_sale_at)} .. "
                f"{_day(self.last_sale_at)}")
        if self.first_snapshot_at and self.last_snapshot_at:
            lines.append(
                f"  snapshots span   {_day(self.first_snapshot_at)} .. "
                f"{_day(self.last_snapshot_at)}")
        if not self.overlap:
            lines.append(
                "  NO OVERLAP: no recorded sale falls inside the snapshot "
                "period, so this question cannot be answered from the data "
                "yet -- which is not the same as 'we were never listed'")
        else:
            lines.append(f"  seen in a snapshot   {len(self.matched):>7,}")
            lines.append(f"  not seen             {len(self.unmatched):>7,}")
            lines.append(
                f"  at our exact price   {len(self.same_price_matches()):>7,}")
            if self.matched:
                rate = len(self.matched) / max(1, self.sales_covered)
                lines.append(f"  visibility rate      {rate:>7.1%}")
        for note in self.notes:
            lines.append(f"  {note}")
        return "\n".join(lines)


def _day(stamp: Optional[int]) -> str:
    if not stamp:
        return "unknown"
    return time.strftime("%Y-%m-%d", time.gmtime(int(stamp)))


def _span(con, sql: str) -> tuple:
    row = con.execute(sql).fetchone()
    if not row:
        return (None, None)
    return (row[0], row[1])


def check_sale_visibility(store_path: Path, *, lookback_s: int = DEFAULT_LOOKBACK_S,
                          realms: Optional[Sequence[str]] = None,
                          limit: Optional[int] = None) -> VisibilityReport:
    """For every recorded sale, was its item listed in a snapshot beforehand?

    ``realms`` restricts the check to named realms; ``limit`` to the most
    recent N sales.  Both exist because the answer is per-realm and a full run
    over a long sales history is not free.
    """
    started = time.perf_counter()
    report = VisibilityReport()

    if not store_exists(store_path):
        report.notes.append(f"no store at {store_path}")
        report.elapsed_s = time.perf_counter() - started
        return report

    con = connect_store(store_path, readonly=True)
    try:
        # The span is computed over the *same* sales the check measures.  Using
        # every sale row reported a period ending 08:53 when the last sale with
        # a usable price ended at 07:12 -- a report whose dates describe rows it
        # then refuses to check is a report nobody should trust.
        usable = ("event_type = 'sale' AND item_id IS NOT NULL"
                  " AND unit_price_copper > 0")
        report.first_sale_at, report.last_sale_at = _span(
            con, "SELECT MIN(timestamp_unix), MAX(timestamp_unix)"
                 f" FROM auction_events WHERE {usable}")
        report.first_snapshot_at, report.last_snapshot_at = _span(
            con, "SELECT MIN(captured_at), MAX(captured_at) FROM ah_snapshot")

        where = [usable]
        params: List[object] = []
        if realms:
            where.append("realm IN (" + ",".join("?" * len(realms)) + ")")
            params.extend(realms)
        sales = con.execute(
            "SELECT event_id, item_id, realm, timestamp_unix, unit_price_copper"
            "  FROM auction_events WHERE " + " AND ".join(where) +
            " ORDER BY timestamp_unix DESC" +
            (" LIMIT ?" if limit else ""),
            tuple(params + ([int(limit)] if limit else []))).fetchall()
        report.sales_total = len(sales)
        if not sales:
            report.notes.append("no sales recorded; nothing to check")
            report.elapsed_s = time.perf_counter() - started
            return report

        first_snapshot = report.first_snapshot_at
        for row in sales:
            event_id, item_id, realm, sold_at, unit_price = (
                str(row[0]), int(row[1]), str(row[2]), int(row[3]),
                int(row[4]))
            match = SaleMatch(event_id=event_id, item_id=item_id, realm=realm,
                              sold_at=sold_at, unit_price_copper=unit_price)
            if first_snapshot is None or sold_at < first_snapshot:
                report.before_snapshots += 1
                continue
            report.sales_covered += 1
            found = con.execute(
                "SELECT COUNT(*), MIN(a.unit_price_copper),"
                "       MAX(a.unit_price_copper),"
                "       COUNT(*) FILTER (WHERE a.unit_price_copper = ?)"
                "  FROM ah_snapshot h"
                "  JOIN ah_auction a ON a.snapshot_id = h.snapshot_id"
                " WHERE h.realm = ? AND a.item_id = ?"
                "   AND h.captured_at BETWEEN ? AND ?",
                (unit_price, realm, item_id, sold_at - int(lookback_s),
                 sold_at)).fetchone()
            if found:
                match.listings = int(found[0] or 0)
                match.wall_low_copper = (int(found[1])
                                         if found[1] is not None else None)
                match.wall_high_copper = (int(found[2])
                                          if found[2] is not None else None)
                match.same_price = int(found[3] or 0)
            if match.visible:
                report.matched.append(match)
            else:
                report.unmatched.append(match)
    finally:
        con.close()

    if not report.overlap:
        report.notes.append(
            f"{report.before_snapshots} sale(s) predate the first snapshot; "
            f"the check needs a sale that happened while snapshots were being "
            f"taken")
    elif report.before_snapshots:
        report.notes.append(
            f"{report.before_snapshots} sale(s) predate the first snapshot and "
            f"were not counted either way")
    report.elapsed_s = time.perf_counter() - started
    return report


def by_realm(report: VisibilityReport) -> Dict[str, Dict[str, int]]:
    """The same counts, split by realm, so one quiet realm does not hide."""
    out: Dict[str, Dict[str, int]] = {}
    for match in report.matched + report.unmatched:
        entry = out.setdefault(match.realm, {"covered": 0, "seen": 0,
                                             "same_price": 0})
        entry["covered"] += 1
        if match.visible:
            entry["seen"] += 1
        if match.consistent_with_ours:
            entry["same_price"] += 1
    return out
