"""Classify transmog items into pricing groups.

The classifier is deliberately evidence-first: it uses the region demand
(`sold_per_day`, `sale_rate`), the local wall (`minBuyout`), the 60-day
historical ask and, when available, the collector model's 30-day sale
probability.  It never treats a missing value as zero; a row without enough
evidence is `unknown` and is not posted.

The verdict is made per **appearance** (the transmog look), not per item: a
look is described by every item ID that grants it, so all the variants of the
same look land in the same group even when only one of them has a price row.
The look's market is described by a "donor" variant (the one with the most
regional movement, then the highest realised sale price); the other variants
inherit its numbers.

Groups
------
liquid      estimated turnover >= 0.1/day; price against the local wall
collector   collector audience / higher-value long-tail; patient listing
slow        positive estimated movement 0.01–0.1/day; patient listing
very_slow   positive estimated movement below 0.01/day; long-tail, not dead
unproven    price data exists but movement is zero; test only if cheap / collector fit
unknown     missing movement data or no usable price; do not infer demand
conflict    wall and historical/market disagree by >2x; anchor on historical

`conflict` is also written as a separate subset because it cuts across the
primary groups.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .tsm_ids import tsm_item_string

# Thresholds are labels for estimated movement, not sell/no-sell decisions.
# A regional 0.01/day signal can still justify patient portfolio listing; zero
# means unproven, not dead. Vendoring is governed separately by ask-vs-deposit.
LIQUID_SPD = 0.10
SLOW_SPD = 0.01
COLLECTOR_PRICE_G = 5_000.0       # high-value, low-turnover fallback
COLLECTOR_P30 = 0.05              # 5% chance in 30 days
COLLECTOR_BUYERS = 20
CONFLICT_RATIO = 2.0              # wall vs historical/market, either way
OUTLIER_PRICE_G = 5_000_000.0     # 5M g; above this the ask is suspicious
GROUPS = ("liquid", "collector", "slow", "very_slow", "unproven", "unknown")


@dataclass
class Metrics:
    item_id: int
    name: str
    spd: Optional[float]
    sale_rate: Optional[float]
    avg_sale_g: Optional[float]
    wall_g: Optional[float]
    historical_g: Optional[float]
    market_g: Optional[float]
    listings: Optional[int]
    p30: Optional[float] = None
    buyers: Optional[int] = None
    quality: Optional[int] = None
    era: Optional[int] = None
    expansion: Optional[int] = None
    item_level: Optional[int] = None
    appearance_id: Optional[int] = None
    variants: int = 0

    @property
    def price_g(self) -> Optional[float]:
        values = [v for v in (self.avg_sale_g, self.market_g, self.wall_g) if v and v > 0]
        return max(values) if values else None


@dataclass
class Verdict:
    group: str
    conflict: bool
    confidence: str
    reasons: List[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b <= 0:
        return None
    return a / b


def classify(m: Metrics) -> Verdict:
    """Return the primary group, conflict flag, confidence and reasons."""
    reasons: List[str] = []
    spd = m.spd if m.spd is not None else None
    sr = m.sale_rate
    if sr is not None and sr > 1.0:
        reasons.append(f"sale_rate {sr:.3f} capped at 1")
        sr = 1.0
    price = m.price_g

    if price is None or price <= 0:
        return Verdict("unknown", False, "low", ["no usable price"])
    if spd is None:
        return Verdict("unknown", False, "low", ["no sold_per_day data"])
    if spd < 0:
        return Verdict("unknown", False, "low", ["invalid negative movement value"])
    if spd == 0:
        return Verdict("unproven", False, "low", ["zero observed movement; not proof of no demand"])

    wall_hist = _ratio(m.wall_g, m.historical_g)
    market_wall = _ratio(m.market_g, m.wall_g)
    conflict = False
    if wall_hist is not None and (wall_hist < 1 / CONFLICT_RATIO or wall_hist > CONFLICT_RATIO):
        conflict = True
        reasons.append(f"wall/historical {wall_hist:.2f}x")
    if market_wall is not None and (market_wall < 1 / CONFLICT_RATIO or market_wall > CONFLICT_RATIO):
        conflict = True
        reasons.append(f"market/wall {market_wall:.2f}x")

    collector = False
    if m.p30 is not None and m.buyers is not None:
        collector = m.p30 >= COLLECTOR_P30 and m.buyers >= COLLECTOR_BUYERS
        if collector:
            reasons.append(f"P30 {m.p30:.1%}, buyers {m.buyers}")
    elif price >= COLLECTOR_PRICE_G:
        collector = True
        reasons.append(f"price {price:,.0f}g, no collector sample")

    confidence = "high"
    if m.wall_g is None or m.listings is None:
        confidence = "medium"
    if m.p30 is None or m.buyers is None:
        confidence = "low" if confidence == "high" and collector else confidence
    if price >= OUTLIER_PRICE_G:
        confidence = "low"
        reasons.append(f"outlier price {price:,.0f}g")

    if collector:
        group = "collector"
    elif spd >= LIQUID_SPD:
        group = "liquid"
    elif spd < SLOW_SPD:
        group = "very_slow"
    else:
        group = "slow"

    if group in ("liquid", "slow", "very_slow"):
        reasons.append(f"estimated regional spd {spd:.4f}/day; label only, not sell-through")
    if group in ("slow", "very_slow"):
        if m.listings is not None and m.listings <= 2:
            reasons.append(f"{m.listings} listing(s)")
        if m.wall_g is None:
            reasons.append("no wall")

    return Verdict(group, conflict, confidence, reasons)


def load_collector(path: Path, realm: str) -> Dict[int, Dict[str, float]]:
    """Load the per-item collector row for one canonical realm."""
    out: Dict[int, Dict[str, float]] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("realm") != realm:
                continue
            try:
                out[int(row["item_id"])] = {
                    "p30": float(row["P30"]),
                    "buyers": int(float(row["buyers"])),
                }
            except (KeyError, TypeError, ValueError):
                continue
    return out


def load_universe(
    db_path: Path,
    realm_slug: str,
    collector_path: Path,
    canonical_realm: Optional[str] = None,
    items: str = "variants",
) -> List[Metrics]:
    """Load the transmog universe, one ``Metrics`` row per item ID.

    The verdict is made at the **appearance** level: a look is described by every
    item ID that grants it (its *variants*), so all the variants of the same look
    land in the same group even when only one of them has a price row.

    ``items`` selects which item IDs enter the universe:

    ``priced``
        only the item IDs with a regional price row (the old behaviour);
    ``variants``
        every item ID that grants an appearance some variant is priced for;
    ``all``
        every item ID with an appearance, priced or not (the no-price looks end
        up ``unknown``, which is the honest verdict and never posts).
    """
    if items not in ("priced", "variants", "all"):
        raise ValueError(f"items must be priced/variants/all, not {items!r}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        canonical = canonical_realm
        if canonical is None:
            row = con.execute(
                "SELECT canonical_name FROM tsm_realm_group WHERE realm_slug=?",
                (realm_slug,),
            ).fetchone()
            canonical = row["canonical_name"] if row else realm_slug.title()
        sid = con.execute(
            "SELECT MAX(snapshot_id) FROM tsm_snapshot WHERE scope=? AND kind='realm_items'",
            (realm_slug,),
        ).fetchone()[0]
        if sid is None:
            raise SystemExit(f"no realm snapshot for {realm_slug}")

        # item <-> appearance graph (an item can grant more than one appearance)
        app_items: Dict[int, List[int]] = {}
        item_apps: Dict[int, List[int]] = {}
        for row in con.execute("SELECT itemId, appearanceId FROM db2_item_appearance"):
            item_id, appearance = int(row["itemId"]), int(row["appearanceId"])
            app_items.setdefault(appearance, []).append(item_id)
            item_apps.setdefault(item_id, []).append(appearance)

        # regional price rows (the only movement source; per appearance below)
        region: Dict[int, sqlite3.Row] = {}
        for row in con.execute(
            "SELECT CAST(key_id AS INTEGER) AS item_id, name, market_value, "
            "historical, avg_sale_price, sale_rate, sold_per_day "
            "FROM tsm_region_price "
            "WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM tsm_region_price)"
        ):
            region[row["item_id"]] = row

        # local price of the realm: changed rows plus the AppData scan
        local_price: Dict[int, sqlite3.Row] = {}
        for row in con.execute(
            "SELECT CAST(key_id AS INTEGER) AS item_id, market_value, min_buyout, "
            "historical FROM tsm_price_change "
            "WHERE snapshot_id=? AND scope=? AND kind='realm_items'",
            (sid, realm_slug),
        ):
            local_price[row["item_id"]] = row
        local_market: Dict[int, sqlite3.Row] = {}
        for row in con.execute(
            "SELECT itemId, min_buyout, market, historical, num_auctions "
            "FROM tsm_market WHERE realm=?",
            (canonical,),
        ):
            local_market[int(row["itemId"])] = row

        meta: Dict[int, sqlite3.Row] = {}
        for row in con.execute(
            "SELECT itemId, name, quality, era, expansion, itemLevel FROM db2_items"
        ):
            meta[int(row["itemId"])] = row
    finally:
        con.close()

    collector = load_collector(collector_path, canonical)

    # TSM writes a family of ~1e11 copper sentinels (99999999900, 99999999999)
    # where it has no value; they are placeholders, not asks, and must not
    # become prices.  9,000,000 g is above any item a person lists.
    no_data = 90_000_000_000

    def gold(value) -> Optional[float]:
        if value is None or value <= 0 or value >= no_data:
            return None
        return value / 10_000.0

    def local_of(item_id: int) -> Dict[str, float]:
        """Local wall / market / 60-day ask / listing count for one item.

        The ``tsm_market`` AppData scan is only trusted for its current cheapest
        buyout and its listing count: its ``market``/``historical`` fields carry
        stale and sentinel values (``99999999999``) when the item was delisted.
        """
        out: Dict[str, float] = {}
        changed = local_price.get(item_id)
        scanned = local_market.get(item_id)
        if changed is not None:
            for key, column in (("wall", "min_buyout"),
                                ("market", "market_value"),
                                ("historical", "historical")):
                value = gold(changed[column])
                if value is not None:
                    out[key] = value
        if scanned is not None:
            if "wall" not in out:
                value = gold(scanned["min_buyout"])
                if value is not None:
                    out["wall"] = value
            if scanned["num_auctions"] is not None:
                out["listings"] = int(scanned["num_auctions"])
        return out

    # collector demand is per item; aggregate it to the appearance (the look)
    app_collector: Dict[int, Dict[str, float]] = {}
    for item_id, col in collector.items():
        for appearance in item_apps.get(item_id, ()):
            current = app_collector.get(appearance)
            if current is None or (col["p30"], col["buyers"]) > (current["p30"], current["buyers"]):
                app_collector[appearance] = col

    # One donor per appearance: the variant that best describes the look's
    # market (most movement, then the highest realised sale price).  Every
    # variant of the look inherits the donor's numbers, so they stay in the
    # same group; the donor's local wall is the listing a buyer sees.
    app_agg: Dict[int, dict] = {}
    for appearance, item_ids in app_items.items():
        priced = [i for i in item_ids if i in region]
        if not priced:
            continue
        donor = max(priced, key=lambda i: (region[i]["sold_per_day"] or 0.0,
                                           region[i]["avg_sale_price"] or 0))
        row = region[donor]
        local = local_of(donor)
        if not local.get("wall") and not local.get("market"):
            for item_id in item_ids:
                extra = local_of(item_id)
                if extra.get("wall") or extra.get("market"):
                    local = extra
                    break
        sales = [gold(region[i]["avg_sale_price"]) for i in priced
                 if gold(region[i]["avg_sale_price"]) is not None]
        historical = local.get("historical")
        if historical is None:
            historical = gold(row["historical"])
        col = app_collector.get(appearance, {})
        app_agg[appearance] = {
            "spd": row["sold_per_day"],
            "sale_rate": row["sale_rate"],
            "avg_sale_g": max(sales) if sales else None,
            "market_g": local.get("market"),
            "historical_g": historical,
            "wall_g": local.get("wall"),
            "listings": local.get("listings"),
            "p30": col.get("p30"),
            "buyers": col.get("buyers"),
            "variants": len(item_ids),
        }

    if items == "priced":
        universe = [i for i in region if i in item_apps]
    elif items == "variants":
        priced_apps = set(app_agg)
        universe = [i for i, apps in item_apps.items()
                    if any(a in priced_apps for a in apps)]
    else:
        universe = list(item_apps)

    out: List[Metrics] = []
    for item_id in universe:
        apps = item_apps.get(item_id, [])
        primary: Optional[int] = None
        for appearance in apps:
            agg = app_agg.get(appearance)
            if agg is None:
                continue
            if primary is None:
                primary = appearance
                continue
            best = (agg["spd"] or 0.0, agg["market_g"] or 0.0)
            current = (app_agg[primary]["spd"] or 0.0, app_agg[primary]["market_g"] or 0.0)
            if best > current:
                primary = appearance
        if primary is None and apps:
            primary = apps[0]
        agg = app_agg.get(primary) if primary is not None else None
        agg = agg or {}
        meta_row = meta.get(item_id)
        region_row = region.get(item_id)
        name = (meta_row["name"] if meta_row and meta_row["name"] else None)
        if not name and region_row and region_row["name"]:
            name = region_row["name"]
        out.append(
            Metrics(
                item_id=item_id,
                name=name or str(item_id),
                spd=agg.get("spd"),
                sale_rate=agg.get("sale_rate"),
                avg_sale_g=agg.get("avg_sale_g"),
                wall_g=agg.get("wall_g"),
                historical_g=agg.get("historical_g"),
                market_g=agg.get("market_g"),
                listings=agg.get("listings"),
                p30=agg.get("p30"),
                buyers=agg.get("buyers"),
                quality=meta_row["quality"] if meta_row else None,
                era=meta_row["era"] if meta_row else None,
                expansion=meta_row["expansion"] if meta_row else None,
                item_level=meta_row["itemLevel"] if meta_row else None,
                appearance_id=primary,
                variants=len(app_items.get(primary, ())),
            )
        )
    return out


def write_groups(rows: Iterable[Metrics], out_dir: Path) -> Dict[str, int]:
    """Write the master CSV and one CSV/TXT per group. Returns counts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    master = out_dir / "mog_groups.csv"
    counts: Dict[str, int] = {}
    by_group: Dict[str, list] = {}

    fields = [
        "item_id", "name", "appearance_id", "variants", "group", "conflict",
        "confidence", "reason", "spd", "sale_rate", "avg_sale_g", "wall_g",
        "historical_g", "market_g", "listings", "p30", "buyers", "quality",
        "era", "expansion", "item_level",
    ]    # Remove only stale exports produced by earlier versions of this generator.
    # The category split replaced `thin` / `dead`; leaving their old TXT files
    # beside the new groups would invite importing contradictory item lists.

    for group in ("thin", "dead"):
        for suffix in (".csv", ".txt"):
            (out_dir / f"{group}{suffix}").unlink(missing_ok=True)

    with master.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for m in rows:
            v = classify(m)
            counts[v.group] = counts.get(v.group, 0) + 1
            if v.conflict:
                counts["conflict"] = counts.get("conflict", 0) + 1
            record = {
                "item_id": m.item_id,
                "name": m.name,
                "appearance_id": "" if m.appearance_id is None else m.appearance_id,
                "variants": m.variants,
                "group": v.group,
                "conflict": int(v.conflict),
                "confidence": v.confidence,
                "reason": v.reason,
                "spd": "" if m.spd is None else f"{m.spd:.6f}",
                "sale_rate": "" if m.sale_rate is None else f"{m.sale_rate:.6f}",
                "avg_sale_g": "" if m.avg_sale_g is None else f"{m.avg_sale_g:.2f}",
                "wall_g": "" if m.wall_g is None else f"{m.wall_g:.2f}",
                "historical_g": "" if m.historical_g is None else f"{m.historical_g:.2f}",
                "market_g": "" if m.market_g is None else f"{m.market_g:.2f}",
                "listings": "" if m.listings is None else m.listings,
                "p30": "" if m.p30 is None else f"{m.p30:.6f}",
                "buyers": "" if m.buyers is None else m.buyers,
                "quality": "" if m.quality is None else m.quality,
                "era": "" if m.era is None else m.era,
                "expansion": "" if m.expansion is None else m.expansion,
                "item_level": "" if m.item_level is None else m.item_level,
            }
            writer.writerow(record)
            if v.conflict:
                by_group.setdefault("conflict", []).append(record)
            by_group.setdefault(v.group, []).append(record)

    for group, records in by_group.items():
        records.sort(key=lambda r: (-(float(r["avg_sale_g"] or 0)), r["name"]))
        path = out_dir / f"{group}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
        # One TSM import line per group: `i:123,i:456,...`.  The old plain
        # newline-separated IDs are rejected by TSM 4's group import.
        with (out_dir / f"{group}.txt").open("w", encoding="utf-8", newline="") as fh:
            fh.write(tsm_item_string(int(r["item_id"]) for r in records) + "\n")

    return counts
