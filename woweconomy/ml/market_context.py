"""The market around one item, measured from the data we already hold.

The pricing models answer "what is a unit worth" and "will it sell".  They
do not answer the questions a seller actually faces when they open the
Auction House: how much stock is already listed, am I bidding against
myself, is the price falling, is the cheap stock clearing.  Those are
market facts, and this module reads them from the databases the project
already writes -- never from a new scraper and never from a guess.

## Sources, and what each one can and cannot say

``ah_auction`` (``data/intel.sqlite``)
    The Blizzard snapshot import: one row per listed auction, with quantity
    and the unit price.  This is the only **observed supply** in the
    project.  It covers three realms so far; everywhere else supply is
    unknown, and unknown is reported as ``None``, never as zero.

``tsm_price_change`` (``tsm_markets.db``)
    The TSM mirror's change table, one row per key per snapshot.  Consecutive
    snapshots give a *price path*: direction (trend) and how much it moves
    (volatility).  It says nothing about quantities.

``undercut_observation`` (``data/intel.sqlite``)
    The addon's own undercut scan: how many units are ahead of a listing and
    how many distinct sellers are below it.  That is **competition**, with a
    named seller attached, and it is only ever about the listings the scan
    saw.

``inventory_posted_listing`` (``data/intel.sqlite``)
    Our own listings, with their auction ids.  It exists here for one
    reason: to keep the auction house we are reading from counting *us* as
    competition, and to notice when our own stock is a large share of what
    is visible -- the feedback loop that would otherwise make a low
    reference look like a market.

``tsm_region_price``
    The regional ``sale_rate`` and ``sold_per_day``.  A measured liquidity
    figure for the region, not for the realm.

## What is a fact and what is an estimate

Listing counts, quantities, cheapest asks, seller counts and price changes
are **observed** in a snapshot.  A sale is not observed: an item whose
cheapest ask rose may have been bought, cancelled or expired, and the
module says so (``SALE_PROXY_NOTE``).  Nothing here turns a disappearance
into a sale, and nothing here is fed back into the reference prices as if
it were a transaction.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: how many TSM snapshots the price path is measured over
DEFAULT_TREND_SNAPSHOTS = 8
#: a price path shorter than this is a direction, not a trend
MIN_TREND_SNAPSHOTS = 3
#: an observation older than this cannot describe the market today
DEFAULT_MAX_AGE_H = 48.0
#: a market this far from ours is priced from stale stock
STALE_AGE_H = 96.0
#: below this share of visible stock, our own listings are not the market
OWN_SHARE_WARN = 0.20

SALE_PROXY_NOTE = (
    "a rise in the cheapest ask is an estimate of a sale: the unit was "
    "bought, cancelled or expired, and the snapshot cannot say which")


def _hours(seconds: float) -> float:
    return seconds / 3600.0 if seconds and seconds > 0 else 0.0


def _pct(new: float, old: float) -> Optional[float]:
    if old <= 0 or new <= 0:
        return None
    return (new - old) / old


def _safe_median(values: Iterable[float]) -> Optional[float]:
    cleaned = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return median(cleaned) if cleaned else None


@dataclass(frozen=True)
class ItemMarket:
    """One item's market, in one realm, as of the newest observation."""

    item_id: int
    #: observed supply: listed auctions / units, from ``ah_auction``
    listings: Optional[int] = None
    units: Optional[int] = None
    cheapest_ask: Optional[int] = None
    #: change against the previous snapshot, when there is one
    units_change_pct: Optional[float] = None
    cheapest_change_pct: Optional[float] = None
    #: competition the scan actually saw
    items_ahead: Optional[int] = None
    sellers_below: Optional[int] = None
    undercut_pct: Optional[float] = None
    #: our own stock against the visible stock (the feedback guard)
    own_units: Optional[int] = None
    own_share: Optional[float] = None
    #: the cheapest ask that is ours, when the inventory snapshot says so
    own_cheapest_ask: Optional[int] = None
    #: the price path from consecutive TSM snapshots
    trend_pct: Optional[float] = None
    volatility: Optional[float] = None
    path_snapshots: int = 0
    #: regional liquidity
    region_sale_rate: Optional[float] = None
    region_sold_per_day: Optional[float] = None
    #: when the newest source was measured, and how old that is.  The ages
    #: are for the decisions made now; they are not shipped, because an age
    #: changes with the clock and would make the dataset differ every run
    #: for no reason.  What is shipped is the timestamp each source was
    #: measured at, which the reader can age itself.
    as_of: int = 0
    supply_as_of: int = 0
    scan_as_of: int = 0
    supply_age_h: Optional[float] = None
    trend_age_h: Optional[float] = None
    scan_age_h: Optional[float] = None
    #: which sources answered: "supply+trend+competition", "trend", ...
    coverage: str = "none"
    confidence: float = 0.0
    observed: Tuple[str, ...] = ()
    estimates: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {"coverage": self.coverage,
                                  "confidence": round(self.confidence, 3)}
        for name in ("listings", "units", "cheapest_ask", "items_ahead",
                     "sellers_below", "own_units", "own_cheapest_ask",
                     "path_snapshots"):
            value = getattr(self, name)
            if value is not None:
                out[name] = int(value)
        for name in ("units_change_pct", "cheapest_change_pct", "undercut_pct",
                     "own_share", "trend_pct", "volatility",
                     "region_sale_rate", "region_sold_per_day"):
            value = getattr(self, name)
            if value is not None:
                out[name] = round(float(value), 4)
        for name in ("as_of", "supply_as_of", "scan_as_of"):
            value = int(getattr(self, name) or 0)
            if value:
                out[{"as_of": "asOf", "supply_as_of": "supplyAsOf",
                     "scan_as_of": "scanAsOf"}[name]] = value
        if self.observed:
            out["observed"] = list(self.observed)
        if self.estimates:
            out["estimates"] = list(self.estimates)
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


@dataclass
class RealmMarket:
    """Every item we asked about in one realm, plus the realm summary."""

    realm: str
    items: Dict[int, ItemMarket] = field(default_factory=dict)
    #: the realm-wide measures, so a record can be read in context
    sale_proxy: Optional[float] = None
    undercut_share: Optional[float] = None
    snapshots: int = 0
    as_of: int = 0
    coverage: str = "none"
    confidence: float = 0.0
    sources: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def get(self, item_id: int) -> Optional[ItemMarket]:
        return self.items.get(int(item_id))

    def summary(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "coverage": self.coverage,
            "confidence": round(self.confidence, 3),
            "snapshots": int(self.snapshots),
        }
        if self.sale_proxy is not None:
            out["saleProxy"] = round(float(self.sale_proxy), 4)
        if self.undercut_share is not None:
            out["undercutShare"] = round(float(self.undercut_share), 4)
        if self.as_of:
            out["asOf"] = int(self.as_of)
        priced = [i for i in self.items.values() if i.listings is not None]
        out["itemsWithSupply"] = len(priced)
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


def _chunks(values: Sequence[int], size: int = 900) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _realm_slugs(con: sqlite3.Connection, realms: Sequence[str]) -> Dict[str, str]:
    """realm display name -> canonical TSM scope, when it can be resolved."""
    out: Dict[str, str] = {}
    try:
        rows = con.execute(
            "SELECT realm_name, canonical_slug FROM tsm_realm_group").fetchall()
    except sqlite3.OperationalError:
        return out
    canonical = {str(name): str(slug) for name, slug in rows}
    for realm in realms:
        if str(realm) in canonical:
            out[str(realm)] = canonical[str(realm)]
    return out


# -- the price path ---------------------------------------------------------

def price_path(con: sqlite3.Connection, scope: str, item_ids: Sequence[int],
               snapshots: int = DEFAULT_TREND_SNAPSHOTS
               ) -> Tuple[Dict[int, Dict[str, Optional[float]]], List[int]]:
    """Trend and volatility per item, from the realm's own snapshots.

    Only items priced in at least two of the snapshots get a path: one price
    is a point, and a point has no direction.  The trend compares the first
    and the last price in the window; the volatility is the mean absolute
    change between consecutive snapshots, which is the size of the noise a
    relist has to beat to be worth its deposit.
    """
    try:
        heads = [int(r[0]) for r in con.execute(
            "SELECT DISTINCT snapshot_id FROM tsm_price_change"
            " WHERE kind='realm_items' AND scope=?"
            " ORDER BY snapshot_id DESC LIMIT ?", (scope, int(snapshots)))]
    except sqlite3.OperationalError:
        return {}, []
    if len(heads) < 2:
        return {}, sorted(heads)
    marks = ",".join("?" * len(heads))
    out: Dict[int, Dict[str, Optional[float]]] = {}
    for chunk in _chunks(list(item_ids)):
        id_marks = ",".join("?" * len(chunk))
        sql = (f"SELECT key_id, snapshot_id, min_buyout, market_value"
               f" FROM tsm_price_change WHERE kind='realm_items' AND scope=?"
               f" AND key_id IN ({id_marks}) AND snapshot_id IN ({marks})")
        try:
            rows = con.execute(sql, [scope, *chunk, *heads]).fetchall()
        except sqlite3.OperationalError:
            return {}, sorted(heads)
        series: Dict[int, List[Tuple[int, float]]] = {}
        for key_id, snapshot_id, min_buyout, market_value in rows:
            price = float(min_buyout or 0) or float(market_value or 0)
            if price <= 0:
                continue
            series.setdefault(int(key_id), []).append((int(snapshot_id), price))
        for key_id, points in series.items():
            points.sort()
            prices = [p for _sid, p in points]
            if len(prices) < 2:
                continue
            trend = _pct(prices[-1], prices[0])
            moves = [abs(math.log(b / a)) for a, b in zip(prices, prices[1:])
                     if a > 0 and b > 0]
            out[key_id] = {
                "trend_pct": trend,
                "volatility": (mean(moves) if moves else None),
                "points": len(prices),
            }
    return out, sorted(heads)


# -- observed supply --------------------------------------------------------

def _realm_snapshots(intel: sqlite3.Connection, realm: str,
                     limit: int = 2) -> List[Tuple[str, int]]:
    try:
        rows = intel.execute(
            "SELECT snapshot_id, captured_at FROM ah_snapshot"
            " WHERE LOWER(realm) = LOWER(?) ORDER BY captured_at DESC LIMIT ?",
            (realm, int(limit))).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(r[0]), int(r[1])) for r in rows]


def _snapshot_supply(intel: sqlite3.Connection, snapshot_id: str,
                     item_ids: Sequence[int]) -> Dict[int, Tuple[int, int, int, int]]:
    """item -> (listings, units, cheapest, own auction ids matched)."""
    out: Dict[int, Tuple[int, int, int, int]] = {}
    for chunk in _chunks(list(item_ids)):
        marks = ",".join("?" * len(chunk))
        try:
            rows = intel.execute(
                f"SELECT item_id, COUNT(*) AS n, SUM(quantity) AS units,"
                f" MIN(CASE WHEN unit_price_copper > 0 THEN unit_price_copper END) AS cheapest"
                f" FROM ah_auction WHERE snapshot_id=? AND item_id IN ({marks})"
                f" GROUP BY item_id", [snapshot_id, *chunk]).fetchall()
        except sqlite3.OperationalError:
            return out
        for item_id, n, units, cheapest in rows:
            out[int(item_id)] = (int(n or 0), int(units or 0), int(cheapest or 0), 0)
    return out


def _own_listings(intel: sqlite3.Connection, realm: str, item_ids: Sequence[int],
                  around: int) -> Dict[int, Dict[str, int]]:
    """Our own posted stock per item, from the inventory snapshot nearest ``around``."""
    out: Dict[int, Dict[str, int]] = {}
    try:
        snapshot = intel.execute(
            "SELECT snapshot_id FROM inventory_snapshot"
            " WHERE LOWER(realm) = LOWER(?) ORDER BY ABS(captured_at - ?) LIMIT 1",
            (realm, int(around))).fetchone()
    except sqlite3.OperationalError:
        return out
    if not snapshot:
        return out
    for chunk in _chunks(list(item_ids)):
        marks = ",".join("?" * len(chunk))
        try:
            rows = intel.execute(
                f"SELECT item_id, SUM(quantity), COUNT(*),"
                f" MIN(CASE WHEN unit_price_copper > 0 THEN unit_price_copper END)"
                f" FROM inventory_posted_listing"
                f" WHERE snapshot_id=? AND item_id IN ({marks}) GROUP BY item_id",
                [snapshot[0], *chunk]).fetchall()
        except sqlite3.OperationalError:
            return out
        for item_id, units, n, cheapest in rows:
            out[int(item_id)] = {"units": int(units or 0), "listings": int(n or 0),
                                 "cheapest": int(cheapest or 0)}
    return out


# -- observed competition ---------------------------------------------------

def latest_scans(intel: sqlite3.Connection, realm: str,
                 item_ids: Sequence[int]) -> Dict[int, Dict[str, object]]:
    """The newest undercut scan per item, with how old it is."""
    out: Dict[int, Dict[str, object]] = {}
    for chunk in _chunks(list(item_ids)):
        marks = ",".join("?" * len(chunk))
        try:
            rows = intel.execute(
                f"SELECT item_id, observed_at, items_ahead, sellers_below,"
                f" undercut_pct, lowest_seller FROM undercut_observation"
                f" WHERE LOWER(realm) = LOWER(?) AND item_id IN ({marks})"
                f" ORDER BY observed_at", [realm, *chunk]).fetchall()
        except sqlite3.OperationalError:
            return out
        for item_id, at, ahead, sellers, pct, seller in rows:
            out[int(item_id)] = {
                "observed_at": int(at or 0), "items_ahead": int(ahead or 0),
                "sellers_below": int(sellers or 0),
                "undercut_pct": float(pct or 0.0),
                "lowest_seller": str(seller or ""),
            }
    return out


# -- regional liquidity -----------------------------------------------------

def region_liquidity(con: sqlite3.Connection, item_ids: Sequence[int]
                     ) -> Dict[int, Tuple[Optional[float], Optional[float]]]:
    """item -> (sale_rate, sold_per_day), the region's measured liquidity."""
    out: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    for chunk in _chunks(list(item_ids)):
        marks = ",".join("?" * len(chunk))
        try:
            rows = con.execute(
                f"SELECT key_id, sale_rate, sold_per_day FROM tsm_region_price"
                f" WHERE key_id IN ({marks})", list(chunk)).fetchall()
        except sqlite3.OperationalError:
            return out
        for key_id, rate, per_day in rows:
            out[int(key_id)] = (float(rate) if rate is not None else None,
                                float(per_day) if per_day is not None else None)
    return out


def _confidence(coverage: str, path: int, supply_age_h: Optional[float],
                scan_age_h: Optional[float], warnings: Sequence[str]) -> float:
    """A transparent score, not a calibrated probability.

    It counts the evidence present, discounts it by age, and is capped below
    1 because no combination of these sources observes a sale.
    """
    score = 0.0
    if "supply" in coverage:
        score += 0.45
    if path >= MIN_TREND_SNAPSHOTS:
        score += 0.3
    elif path == 2:
        score += 0.12
    if "competition" in coverage:
        score += 0.2
    if supply_age_h is not None:
        score *= max(0.0, 1.0 - min(1.0, supply_age_h / (2 * DEFAULT_MAX_AGE_H)))
    if scan_age_h is not None and scan_age_h > DEFAULT_MAX_AGE_H:
        score *= 0.7
    score -= 0.05 * len(warnings)
    return round(max(0.0, min(0.95, score)), 3)


# -- the public builder -----------------------------------------------------

def build_realm_market(wow_db: Path | str, intel_db: Optional[Path | str],
                       realm: str, item_ids: Sequence[int],
                       *, now: int, snapshots: int = DEFAULT_TREND_SNAPSHOTS,
                       max_age_h: float = DEFAULT_MAX_AGE_H,
                       intel: Optional[sqlite3.Connection] = None,
                       con: Optional[sqlite3.Connection] = None) -> RealmMarket:
    """The market for ``item_ids`` in one realm.

    Neither database is required: a missing ``intel.sqlite`` means no
    observed supply and no scan, and the price path still comes from the TSM
    mirror.  What is missing is reported in ``coverage`` and ``warnings``;
    it is never filled in with a zero.
    """
    ids = sorted({int(i) for i in item_ids if i})
    market = RealmMarket(realm=str(realm))
    own_connection = con is None
    connection = con or sqlite3.connect(str(wow_db))
    connection.row_factory = sqlite3.Row
    intel_own = False
    if intel is None and intel_db is not None and Path(intel_db).exists():
        intel = sqlite3.connect(f"file:{Path(intel_db).as_posix()}?mode=ro", uri=True)
        intel.row_factory = sqlite3.Row
        intel_own = True
    try:
        scope = _realm_slugs(connection, [realm]).get(str(realm), "")
        path: Dict[int, Dict[str, Optional[float]]] = {}
        path_heads: List[int] = []
        if scope:
            path, path_heads = price_path(connection, scope, ids, snapshots)
        liquidity = region_liquidity(connection, ids)

        supply: Dict[int, Tuple[int, int, int, int]] = {}
        previous_supply: Dict[int, Tuple[int, int, int, int]] = {}
        own: Dict[int, Dict[str, int]] = {}
        scans: Dict[int, Dict[str, object]] = {}
        snapshots_seen: List[Tuple[str, int]] = []
        if intel is not None and ids:
            snapshots_seen = _realm_snapshots(intel, realm, limit=2)
            if snapshots_seen:
                supply = _snapshot_supply(intel, snapshots_seen[0][0], ids)
                if len(snapshots_seen) > 1:
                    previous_supply = _snapshot_supply(
                        intel, snapshots_seen[1][0], ids)
                own = _own_listings(intel, realm, ids, snapshots_seen[0][1])
            scans = latest_scans(intel, realm, ids)

        trend_age = None
        if path_heads:
            try:
                row = connection.execute(
                    "SELECT updated_at FROM tsm_snapshot WHERE snapshot_id=?",
                    (path_heads[0],)).fetchone()
                stamp = _stamp(row[0]) if row else 0.0
                trend_age = _hours(now - stamp) if stamp else None
            except sqlite3.OperationalError:
                trend_age = None

        supply_at = snapshots_seen[0][1] if snapshots_seen else 0
        supply_age = _hours(now - supply_at) if supply_at else None

        for item_id in ids:
            data = supply.get(item_id)
            prev = previous_supply.get(item_id)
            entry_path = path.get(item_id) or {}
            scan = scans.get(item_id) or {}
            mine = own.get(item_id) or {}
            rate, per_day = liquidity.get(item_id, (None, None))

            units_change = cheapest_change = None
            if data and prev and prev[1] > 0 and prev[2] > 0:
                units_change = _pct(float(data[1]), float(prev[1]))
                cheapest_change = _pct(float(data[2]), float(prev[2]))
            own_share = None
            if data and data[1] > 0 and mine.get("units"):
                own_share = min(1.0, float(mine["units"]) / float(data[1]))

            scan_age = (_hours(now - int(scan["observed_at"]))
                        if scan.get("observed_at") else None)
            item_warnings: List[str] = []
            if own_share is not None and own_share >= OWN_SHARE_WARN:
                item_warnings.append(
                    f"own stock is {own_share:.0%} of the visible supply: the "
                    "cheapest ask is partly ours")
            own_cheapest = mine.get("cheapest") or 0
            if data and own_cheapest and data[2] and own_cheapest <= data[2]:
                item_warnings.append(
                    "the cheapest listing on this item is ours: the reference"
                    " ask is not independent market evidence")
            if supply_age is not None and supply_age > STALE_AGE_H:
                item_warnings.append(
                    f"the supply snapshot is {supply_age:.0f}h old")
            if scan_age is not None and scan_age > max_age_h:
                item_warnings.append(
                    f"the competition scan is {scan_age:.0f}h old")
            if entry_path.get("trend_pct") is not None and prev and data and \
                    cheapest_change is not None:
                # two sources disagreeing on direction is worth saying out loud
                if (entry_path["trend_pct"] > 0.05 and cheapest_change < -0.05):
                    item_warnings.append(
                        "the TSM path and the snapshot cheapest ask disagree on direction")

            # what this *item* has, not what the realm has: a realm-level
            # source that does not cover this item is not evidence about it
            item_observed: List[str] = []
            if data:
                item_observed.append("supply")
            if entry_path:
                item_observed.append("pricePath")
            if scan:
                item_observed.append("competition")
            item_estimates: List[str] = []
            if rate is not None or per_day is not None:
                item_estimates.append("liquidity")
            coverage_parts = []
            if data:
                coverage_parts.append("supply")
            if entry_path:
                coverage_parts.append("trend")
            if scan:
                coverage_parts.append("competition")
            coverage = "+".join(coverage_parts) if coverage_parts else "none"

            item_supply_age = supply_age if data else None
            item_trend_age = trend_age if entry_path else None
            confidence = _confidence(coverage, int(entry_path.get("points") or 0),
                                     item_supply_age, scan_age, item_warnings)
            as_of = max(supply_at if data else 0,
                        int(scan.get("observed_at") or 0))
            if not as_of and entry_path and trend_age is not None:
                as_of = int(now - trend_age * 3600)
            market.items[item_id] = ItemMarket(
                item_id=item_id,
                listings=data[0] if data else None,
                units=data[1] if data else None,
                cheapest_ask=(data[2] or None) if data else None,
                units_change_pct=units_change,
                cheapest_change_pct=cheapest_change,
                items_ahead=scan.get("items_ahead"),  # type: ignore[arg-type]
                sellers_below=scan.get("sellers_below"),  # type: ignore[arg-type]
                undercut_pct=scan.get("undercut_pct"),  # type: ignore[arg-type]
                own_units=mine.get("units"),
                own_share=own_share,
                own_cheapest_ask=(mine.get("cheapest") or None),
                trend_pct=entry_path.get("trend_pct"),
                volatility=entry_path.get("volatility"),
                path_snapshots=int(entry_path.get("points") or 0),
                region_sale_rate=rate,
                region_sold_per_day=per_day,
                as_of=as_of,
                supply_as_of=supply_at if data else 0,
                scan_as_of=int(scan.get("observed_at") or 0),
                supply_age_h=item_supply_age,
                trend_age_h=item_trend_age,
                scan_age_h=scan_age,
                coverage=coverage,
                confidence=confidence,
                observed=tuple(item_observed),
                estimates=tuple(item_estimates),
                warnings=tuple(item_warnings),
            )

        # snapshots of the price path that actually priced something for
        # these items: a realm can have snapshots and none of them anywhere
        # near these items, and that is not coverage
        market.snapshots = len(path_heads) if path else 0
        market.as_of = max([i.as_of for i in market.items.values()] or [0])
        coverage_parts = []
        if supply:
            coverage_parts.append("supply")
        if path:
            coverage_parts.append("trend")
        if scans:
            coverage_parts.append("competition")
        market.coverage = "+".join(coverage_parts) if coverage_parts else "none"
        priced = [i for i in market.items.values() if i.confidence > 0]
        market.confidence = (round(mean(i.confidence for i in priced), 3)
                             if priced else 0.0)
        market.sources = {
            "tsm": str(wow_db),
            "intel": str(intel_db) if intel_db else "",
            "trendSnapshots": path_heads,
            "supplySnapshots": [s[0] for s in snapshots_seen],
        }
        if supply_age is not None and supply_age > STALE_AGE_H:
            market.warnings.append(f"supply is {supply_age:.0f}h old")
        if trend_age is not None and trend_age > STALE_AGE_H:
            market.warnings.append(f"the price path is {trend_age:.0f}h old")
        if not supply:
            market.warnings.append(
                "no observed supply for this realm: listing counts are unknown, not zero")
        if not path:
            market.warnings.append("no price path: trend and volatility unknown")
        if not scans:
            market.warnings.append("no undercut scan for this realm")
        return market
    finally:
        if own_connection:
            connection.close()
        if intel_own and intel is not None:
            intel.close()


def _stamp(text: object) -> float:
    from datetime import datetime, timezone
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def supply_pressure(market: Optional[ItemMarket]) -> Optional[float]:
    """How much stock is arriving relative to what was there, clipped.

    A doubling of supply is a pressure of 1.0; a market that halved is -0.5.
    ``None`` when there is no previous snapshot to compare against -- one
    snapshot cannot say whether stock is arriving.
    """
    if market is None or market.units_change_pct is None:
        return None
    return max(-1.0, min(2.0, float(market.units_change_pct)))


def competition_pressure(market: Optional[ItemMarket]) -> Optional[float]:
    """Units ahead of the cheapest ask, normalised, when a scan saw them."""
    if market is None or market.items_ahead is None:
        return None
    return max(0.0, min(10.0, float(market.items_ahead))) / 10.0
