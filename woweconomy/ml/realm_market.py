"""Realm-level market demand, measured from the TSM realm snapshots we keep.

The realm export TSM writes carries no ``saleRate`` column -- only
``marketValue``, ``minBuyout``, ``recent`` and ``historical`` -- so a realm
sale rate has to be *derived* from consecutive snapshots rather than read.
This module does exactly that, and labels the result for what it is:

* a **sale proxy**, not a sale: when the cheapest ask for an item is higher
  in the next snapshot, the cheap unit is gone.  It was bought, cancelled
  or expired -- the snapshot cannot say which.  The proxy therefore
  over-counts cancels and expiries, and it is never presented as a
  transaction count;
* an **undercut share**: when the cheapest ask fell, a cheaper listing
  appeared.  That is competition, not demand;
* unchanged is the rest, and it is the majority.

The region export *does* carry ``saleRate`` (``region.region_items.csv``),
and the ingested ``tsm_region_price`` table holds it for 77.7 % of items.
That is the reference rate; the realm proxy is what says whether *this*
realm is more or less liquid than the region, which is the part a single
region number cannot answer.

Nothing here trains on listings: the pair of snapshots is an observation
of the market, and the label says ``estimate``.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

#: what a rise in the cheapest ask is evidence of, and what it is not
SALE_PROXY_NOTE = (
    "cheapest ask rose between two snapshots: the cheap unit was bought, "
    "cancelled or expired -- an estimate, not a confirmed sale")


@dataclass(frozen=True)
class RealmDemand:
    """One realm's liquidity, from one pair of snapshots."""

    realm: str
    items: int                 # items priced in both snapshots
    rose: int                  # cheapest ask went up  -> sale/cancel/expiry proxy
    fell: int                  # cheapest ask went down -> a cheaper listing appeared
    hours: float               # between the two snapshots
    as_of: int                 # the newer snapshot id
    label: str = "estimate"

    @property
    def sale_proxy(self) -> float:
        return self.rose / self.items if self.items else 0.0

    @property
    def undercut_share(self) -> float:
        return self.fell / self.items if self.items else 0.0

    @property
    def per_day(self) -> float:
        """The proxy normalised to 24 h, so realms with different gaps compare."""
        if self.hours <= 0:
            return 0.0
        return self.sale_proxy * (24.0 / self.hours)

    def as_dict(self) -> Dict[str, object]:
        return {
            "realm": self.realm, "items": self.items,
            "rose": self.rose, "fell": self.fell,
            "sale_proxy": round(self.sale_proxy, 4),
            "undercut_share": round(self.undercut_share, 4),
            "per_day": round(self.per_day, 4),
            "hours": round(self.hours, 2), "as_of": self.as_of,
            "label": self.label, "note": SALE_PROXY_NOTE,
        }


def _stamp(text: object) -> float:
    """An ISO-8601 timestamp as a POSIX second count (0.0 when unparseable)."""
    from datetime import datetime, timezone
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _hours_between(con: sqlite3.Connection, first: int, second: int) -> float:
    """Hours between two snapshots, from the vintage TSM recorded for them.

    ``tsm_snapshot`` stores ISO timestamps (``updated_at`` is the vintage of
    the data, ``fetched_at`` when we read it); the data vintage is the one
    that measures the gap between two observations.
    """
    try:
        rows = con.execute(
            "SELECT snapshot_id, updated_at, fetched_at FROM tsm_snapshot"
            " WHERE snapshot_id IN (?,?)", (first, second)).fetchall()
    except sqlite3.OperationalError:
        return 0.0
    stamps = {int(r[0]): (_stamp(r[1]) or _stamp(r[2])) for r in rows}
    a, b = stamps.get(int(first), 0.0), stamps.get(int(second), 0.0)
    if a > 0 and b > 0 and b > a:
        return (b - a) / 3600.0
    return 0.0


def snapshot_pairs(con: sqlite3.Connection, kind: str = "realm_items",
                   pairs: int = 1) -> Dict[str, List[Tuple[int, ...]]]:
    """The newest snapshot pairs per realm, oldest first inside each pair.

    A realm needs two snapshots with prices to say anything at all; one is
    not a measurement and is left out rather than defaulted to zero.
    """
    rows = con.execute(
        """
        WITH s AS (
          SELECT scope, snapshot_id,
                 ROW_NUMBER() OVER (PARTITION BY scope
                                    ORDER BY snapshot_id DESC) rn
          FROM (SELECT DISTINCT scope, snapshot_id FROM tsm_price_change
                WHERE kind=? AND min_buyout > 0)
        )
        SELECT scope, snapshot_id FROM s WHERE rn <= ?
        """, (kind, pairs * 2)).fetchall()
    # Ordered here rather than in SQL: GROUP_CONCAT does not promise order,
    # and a pair handed over as (older, newer) silently reported zero hours.
    by_realm: Dict[str, List[int]] = {}
    for scope, snapshot in rows:
        by_realm.setdefault(str(scope), []).append(int(snapshot))
    out: Dict[str, List[Tuple[int, ...]]] = {}
    for scope, ids in by_realm.items():
        newest_first = sorted(set(ids), reverse=True)
        if len(newest_first) < 2:
            continue
        out[scope] = [tuple(newest_first[i:i + 2])
                      for i in range(0, len(newest_first) - 1, 2)]
    return out


def measure_realm(con: sqlite3.Connection, realm: str,
                  newer: int, older: int) -> Optional[RealmDemand]:
    """The sale proxy for one realm between two snapshots.

    Only items priced in **both** snapshots count: an item that disappeared
    is indistinguishable from one that stopped being tracked, so it is left
    out instead of being counted as a sale.
    """
    rows = con.execute(
        """
        SELECT a.min_buyout, b.min_buyout
        FROM tsm_price_change a
        JOIN tsm_price_change b
          ON b.scope = a.scope AND b.key_id = a.key_id AND b.kind = a.kind
        WHERE a.scope=? AND a.kind='realm_items'
          AND a.snapshot_id=? AND b.snapshot_id=?
          AND a.min_buyout > 0 AND b.min_buyout > 0
        """, (realm, newer, older)).fetchall()
    if not rows:
        return None
    rose = sum(1 for new, old in rows if new > old)
    fell = sum(1 for new, old in rows if new < old)
    return RealmDemand(realm=realm, items=len(rows), rose=rose, fell=fell,
                       hours=_hours_between(con, older, newer), as_of=int(newer))


def measure_all(con: sqlite3.Connection,
                realms: Optional[Sequence[str]] = None) -> Dict[str, RealmDemand]:
    """The sale proxy for every realm that has two snapshots."""
    wanted = set(realms) if realms else None
    out: Dict[str, RealmDemand] = {}
    for realm, pairs in snapshot_pairs(con).items():
        if wanted is not None and realm not in wanted:
            continue
        newer, older = pairs[0]
        demand = measure_realm(con, realm, newer, older)
        if demand is not None:
            out[realm] = demand
    return out


def relative_liquidity(home: Optional[RealmDemand],
                       other: Optional[RealmDemand],
                       floor: float = 0.5,
                       ceiling: float = 2.0) -> float:
    """How much more (or less) liquid ``other`` is than ``home``.

    A ratio of the two sale proxies, clipped: the proxy is a small-sample
    estimate and an unbounded ratio would turn a quiet realm into a claim
    of ten times the sales.  With either side missing the answer is 1.0 --
    "no evidence of a difference", not a guess in either direction.
    """
    if home is None or other is None or home.sale_proxy <= 0:
        return 1.0
    ratio = other.sale_proxy / home.sale_proxy
    if not math.isfinite(ratio):
        return 1.0
    return min(ceiling, max(floor, ratio))
