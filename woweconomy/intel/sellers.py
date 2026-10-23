"""Seller identity: what we saw, and what we are only allowed to infer.

Where the name comes from
-------------------------
The public Game Data API's auction payload has no seller field.  The
client, however, shows the seller for a non-commodity auction (the tooltip
in the Browse tab, and the auction-info structure behind it).  The addon
therefore captures the name **from the UI**, stores the raw tooltip line
it parsed, and never touches commodities -- the commodity book is
anonymous by design.

This module consumes those observations and does two separate things:

**confirmed facts** -- rows copied from an event log or an observation:
our sale to a buyer, and a later listing by a seller of the same name.
These are returned under ``confirmed`` and carry their source.

**inferences** -- a hypothesis that the buyer relisted the item, or that
one seller undercut another.  Every inference carries its method, its
evidence references and a confidence tier, and is returned under
``inferred``.  Nothing in ``inferred`` is ever counted as a fact.

Competitor-to-competitor transactions are *not* observable from our own
journal: we see our sale and, separately, a listing by the buyer.  The
code says exactly that instead of inventing the other side of the trade.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CONFIDENCE_TIERS = ("verified", "high", "medium", "low")
_COPPER = 10_000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm(value: Any) -> str:
    """NFC + casefold for player names.  Accents are preserved, not stripped."""
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value).strip().casefold()


def split_seller(raw: str) -> Tuple[str, str]:
    """``Name-Realm`` -> ``(Name, Realm)``; a bare name keeps an empty realm.

    A player name cannot contain ``-``, so the first hyphen is the
    separator even when the realm itself has one.
    """
    if not isinstance(raw, str):
        return "", ""
    text = raw.strip()
    if "-" not in text:
        return text, ""
    name, _, realm = text.partition("-")
    return name.strip(), realm.strip()


@dataclass(frozen=True)
class SellerObservation:
    obs_id: str
    observed_at: int
    realm: str
    seller: str
    seller_name: str
    seller_realm: str
    source: str
    confidence: str
    item_id: Optional[int] = None
    item_name: str = ""
    auction_id: Optional[int] = None
    unit_price_copper: Optional[int] = None
    quantity: Optional[int] = None
    label: str = ""
    raw_line: str = ""
    character: str = ""

    @property
    def name_key(self) -> str:
        return _norm(self.seller_name)

    @property
    def is_self(self) -> bool:
        """Our own auction: the client prints the literal "You".

        These rows are kept as observations -- they are real -- but they
        are not competitors and must never enter a competitor count.
        """
        if self.name_key == "you":
            return True
        return bool(self.character) and self.name_key == _norm(self.character)


@dataclass
class SaleFact:
    event_id: str
    ts: int
    realm: str
    item_id: Optional[int]
    item_name: str
    quantity: int
    unit_price_copper: int
    buyer: str
    character: str = ""
    source: str = ""
    confidence: str = "verified"

    @property
    def buyer_key(self) -> str:
        return _norm(split_seller(self.buyer)[0] or self.buyer)


def make_obs_id(observed_at: int, realm: str, seller: str,
                item_id: Optional[int], auction_id: Optional[int],
                source: str) -> str:
    raw = f"{observed_at}|{_norm(realm)}|{_norm(seller)}|{item_id}|{auction_id}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def parse_observation(row: Dict[str, Any]) -> Optional[SellerObservation]:
    if not isinstance(row, dict):
        return None
    seller_raw = str(row.get("seller") or "")
    name = str(row.get("sellerName") or row.get("seller_name") or "")
    realm_part = str(row.get("sellerRealm") or row.get("seller_realm") or "")
    if not name:
        name, realm_part = split_seller(seller_raw)
    if not name:
        return None
    seller = seller_raw or (f"{name}-{realm_part}" if realm_part else name)
    observed = _int(row.get("observedAt") or row.get("observed_at"))
    realm = str(row.get("realm") or "")
    item_id = _int(row.get("itemId") or row.get("item_id")) or None
    auction_id = _int(row.get("auctionId") or row.get("auction_id")) or None
    source = str(row.get("source") or "tooltip")
    confidence = str(row.get("confidence") or "verified").lower()
    if confidence not in CONFIDENCE_TIERS:
        confidence = "low"
    quantity = _int(row.get("quantity")) or None
    price = _int(row.get("unitPriceCopper") or row.get("unit_price_copper")) or None
    return SellerObservation(
        obs_id=str(row.get("obsId") or make_obs_id(observed, realm, seller,
                                                   item_id, auction_id, source)),
        observed_at=observed, realm=realm, seller=seller, seller_name=name,
        seller_realm=realm_part, item_id=item_id,
        item_name=str(row.get("itemName") or row.get("item_name") or ""),
        auction_id=auction_id, unit_price_copper=price, quantity=quantity,
        source=source, confidence=confidence,
        label=str(row.get("label") or ""),
        raw_line=str(row.get("rawLine") or row.get("raw_line") or ""),
        character=str(row.get("character") or ""))


def parse_sellers_jsonl(text: str) -> List[SellerObservation]:
    out: List[SellerObservation] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = parse_observation(row)
        if parsed is not None:
            out.append(parsed)
    return out


def parse_sellers_from_export(export: Dict[str, Any]) -> List[SellerObservation]:
    rows = export.get("sellers")
    if isinstance(rows, list):
        return [o for o in (parse_observation(r) for r in rows) if o is not None]
    text = export.get("sellers_jsonl")
    return parse_sellers_jsonl(text) if isinstance(text, str) else []


def parse_export_file(path: Path | str) -> List[SellerObservation]:
    from woweconomy.analytics.auction_intel import extract_lua_string
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    raw = extract_lua_string(content, "sellers_jsonl")
    if not raw:
        raise ValueError(f"no sellers_jsonl block found in {path}")
    return parse_sellers_jsonl(raw)


def import_observations(con, observations: Iterable[SellerObservation]) -> int:
    added = 0
    for obs in observations:
        cursor = con.execute(
            "INSERT OR IGNORE INTO seller_observation (obs_id, observed_at,"
            " realm, seller, seller_name, seller_realm, item_id, item_name,"
            " auction_id, unit_price_copper, source, confidence, label, raw_line,"
            " character, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs.obs_id, obs.observed_at, obs.realm, obs.seller, obs.seller_name,
             obs.seller_realm, obs.item_id, obs.item_name, obs.auction_id,
             obs.unit_price_copper, obs.source, obs.confidence, obs.label,
             obs.raw_line, obs.character, _utcnow()))
        added += cursor.rowcount
    con.commit()
    return added


def load_observations(con, realm: str = "", since: int = 0,
                      until: int = 0) -> List[SellerObservation]:
    sql = "SELECT * FROM seller_observation WHERE observed_at >= ?"
    params: List[Any] = [since]
    if until:
        sql += " AND observed_at <= ?"
        params.append(until)
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        params.append(realm)
    sql += " ORDER BY observed_at, obs_id"
    out = []
    for row in con.execute(sql, params):
        out.append(SellerObservation(
            obs_id=row["obs_id"], observed_at=int(row["observed_at"]),
            realm=row["realm"], seller=row["seller"],
            seller_name=row["seller_name"], seller_realm=row["seller_realm"],
            source=row["source"], confidence=row["confidence"],
            item_id=int(row["item_id"]) if row["item_id"] is not None else None,
            item_name=row["item_name"],
            auction_id=int(row["auction_id"]) if row["auction_id"] is not None else None,
            unit_price_copper=(int(row["unit_price_copper"])
                               if row["unit_price_copper"] is not None else None),
            label=row["label"], raw_line=row["raw_line"],
            character=row["character"]))
    return out


# ---------------------------------------------------------------------------
# confirmed sales from the existing journal
# ---------------------------------------------------------------------------

def load_confirmed_sales(tsm_db: Path | str, since: int = 0,
                         until: int = 0, realm: str = "") -> List[SaleFact]:
    """Our own sales, where the invoice carried a counterparty.

    A sale without a buyer name is still a sale, but it cannot be
    correlated with a seller observation, so it is excluded here and
    counted nowhere else.
    """
    from woweconomy.dbcompat import connect_store, missing_table_errors

    con = connect_store(tsm_db, readonly=True)
    facts: Dict[str, SaleFact] = {}
    try:
        sql = ("SELECT event_id, ts, item_id, item_name, quantity,"
               " unit_price_copper, realm, character, counterparty, source,"
               " confidence FROM auction_intel_events"
               " WHERE event_type='sale' AND counterparty IS NOT NULL"
               " AND counterparty <> '' AND ts >= ?")
        params: List[Any] = [since]
        if until:
            sql += " AND ts <= ?"
            params.append(until)
        if realm:
            sql += " AND LOWER(realm) = LOWER(?)"
            params.append(realm)
        try:
            for row in con.execute(sql, params):
                facts[row["event_id"]] = SaleFact(
                    event_id=row["event_id"], ts=int(row["ts"]),
                    realm=row["realm"], item_id=row["item_id"],
                    item_name=row["item_name"], quantity=int(row["quantity"] or 0),
                    unit_price_copper=int(row["unit_price_copper"] or 0),
                    buyer=row["counterparty"], character=row["character"] or "",
                    source="auction_intel_events", confidence=row["confidence"] or "verified")
        except missing_table_errors():
            pass
        # older importer, same facts under different column names
        sql = ("SELECT event_id, timestamp_unix, item_id, item_name, qty,"
               " unit_price_copper, realm, character, counterparty, source,"
               " confidence FROM auction_events WHERE event_type='sale'"
               " AND counterparty IS NOT NULL AND counterparty <> ''"
               " AND timestamp_unix >= ?")
        params = [since]
        if until:
            sql += " AND timestamp_unix <= ?"
            params.append(until)
        if realm:
            sql += " AND LOWER(realm) = LOWER(?)"
            params.append(realm)
        try:
            for row in con.execute(sql, params):
                key = row["event_id"]
                if key in facts:
                    continue
                facts[key] = SaleFact(
                    event_id=key, ts=int(row["timestamp_unix"]),
                    realm=row["realm"], item_id=row["item_id"],
                    item_name=row["item_name"], quantity=int(row["qty"] or 0),
                    unit_price_copper=int(row["unit_price_copper"] or 0),
                    buyer=row["counterparty"], character=row["character"] or "",
                    source="auction_events", confidence=row["confidence"] or "verified")
        except missing_table_errors():
            pass
    finally:
        con.close()
    return sorted(facts.values(), key=lambda s: (s.ts, s.event_id))


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------

def _fact_sale(sale: SaleFact) -> Dict[str, Any]:
    return {"kind": "sale", "event_id": sale.event_id, "ts": sale.ts,
            "realm": sale.realm, "item_id": sale.item_id,
            "quantity": sale.quantity, "unit_price_copper": sale.unit_price_copper,
            "buyer": sale.buyer, "source": sale.source,
            "confidence": sale.confidence}


def _fact_obs(obs: SellerObservation) -> Dict[str, Any]:
    return {"kind": "seller_observation", "obs_id": obs.obs_id,
            "ts": obs.observed_at, "realm": obs.realm, "item_id": obs.item_id,
            "seller": obs.seller, "source": obs.source,
            "confidence": obs.confidence, "raw_line": obs.raw_line,
            "unit_price_copper": obs.unit_price_copper}


def correlate_sales(sales: Sequence[SaleFact],
                    observations: Sequence[SellerObservation],
                    window_h: int = 336) -> Dict[str, Any]:
    """Match a confirmed sale's buyer to a later observed listing.

    This is a name match plus a time window, not proof of a resale: the
    buyer could have listed the item they already owned, or bought a
    second copy.  The inference says so and carries its confidence.
    """
    by_key: Dict[Tuple[str, Optional[int], str], List[SellerObservation]] = defaultdict(list)
    for obs in observations:
        if obs.is_self:
            continue
        by_key[(obs.name_key, obs.item_id, _norm(obs.realm))].append(obs)
    for rows in by_key.values():
        rows.sort(key=lambda o: o.observed_at)

    confirmed: List[Dict[str, Any]] = []
    inferred: List[Dict[str, Any]] = []
    unmatched = 0
    for sale in sales:
        buyer = sale.buyer_key
        candidates = [o for o in by_key.get((buyer, sale.item_id, _norm(sale.realm)), ())
                      if sale.ts < o.observed_at <= sale.ts + window_h * 3600]
        if not candidates:
            unmatched += 1
            continue
        first = candidates[0]
        lag_h = (first.observed_at - sale.ts) / 3600.0
        if lag_h <= 48:
            confidence = "high"
        elif lag_h <= 168:
            confidence = "medium"
        else:
            confidence = "low"
        confirmed.extend([_fact_sale(sale), _fact_obs(first)])
        inferred.append({
            "kind": "buyer_relisted",
            "realm": sale.realm,
            "item_id": sale.item_id,
            "item_name": sale.item_name,
            "buyer": sale.buyer,
            "seller_observation": first.obs_id,
            "lag_hours": round(lag_h, 2),
            "confidence": confidence,
            "method": "buyer name equals an observed seller of the same item,"
                      " on the same realm, after the sale",
            "evidence": {"sale_event": sale.event_id,
                         "seller_observation": first.obs_id,
                         "raw_line": first.raw_line},
            "limits": ("our journal sees only our own sale; the buyer's own"
                       " purchase from another competitor is not observable,"
                       " and a name match is not proof of the same player"),
        })
        if len(candidates) > 1:
            inferred[-1]["other_observations"] = [o.obs_id for o in candidates[1:6]]
    return {
        "confirmed": confirmed,
        "inferred": inferred,
        "sales_examined": len(sales),
        "sales_without_observation": unmatched,
        "note": ("confirmed rows are observations; 'buyer_relisted' is a"
                 " probabilistic inference and must not be counted as a fact"),
    }


def competitor_profiles(observations: Sequence[SellerObservation],
                        inferences: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """One row per observed seller.  Counts are observations, not sales."""
    buyers = Counter(_norm(split_seller(str(i.get("buyer", "")))[0])
                     for i in inferences if i.get("kind") == "buyer_relisted")
    groups: Dict[str, List[SellerObservation]] = defaultdict(list)
    for obs in observations:
        if obs.is_self:
            continue
        groups[obs.name_key].append(obs)
    out: List[Dict[str, Any]] = []
    for key, rows in groups.items():
        rows.sort(key=lambda o: o.observed_at)
        display = Counter(o.seller for o in rows).most_common(1)[0][0]
        items = sorted({o.item_id for o in rows if o.item_id is not None})
        out.append({
            "seller": display,
            "seller_name": rows[0].seller_name,
            "seller_realm": rows[0].seller_realm,
            "realms": sorted({o.realm for o in rows if o.realm}),
            "observations": len(rows),
            "items_observed": len(items),
            "first_seen": rows[0].observed_at,
            "last_seen": rows[-1].observed_at,
            "sources": dict(Counter(o.source for o in rows)),
            "also_bought_from_us": int(buyers.get(key, 0)),
            "basis": "confirmed observations",
        })
    out.sort(key=lambda r: (-r["observations"], r["seller"]))
    return out


def co_listing_episodes(observations: Sequence[SellerObservation],
                        gap_h: int = 12) -> Dict[str, Any]:
    """Pairs of sellers seen listing the same item close together.

    This is *co-presence*, which is evidence of competition, not of a
    transaction between them.  The return value labels it that way.
    """
    grouped: Dict[Tuple[str, Optional[int]], List[SellerObservation]] = defaultdict(list)
    for obs in observations:
        if obs.item_id is None or obs.is_self:
            continue
        grouped[(_norm(obs.realm), obs.item_id)].append(obs)
    co: List[Dict[str, Any]] = []
    undercut: List[Dict[str, Any]] = []
    seen_pairs = set()
    for (realm, item_id), rows in grouped.items():
        rows.sort(key=lambda o: o.observed_at)
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                if right.observed_at - left.observed_at > gap_h * 3600:
                    break
                if left.name_key == right.name_key:
                    continue
                pair = tuple(sorted((left.name_key, right.name_key)))
                key = (realm, item_id, pair)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    co.append({
                        "realm": left.realm, "item_id": item_id,
                        "seller_a": left.seller, "seller_b": right.seller,
                        "first_seen": left.observed_at, "second_seen": right.observed_at,
                        "gap_minutes": round((right.observed_at - left.observed_at) / 60.0, 1),
                        "confidence": "medium" if abs(right.observed_at - left.observed_at) <= 6 * 3600 else "low",
                        "method": "both sellers observed listing the same item on the"
                                  " same realm inside the gap window",
                        "evidence": {"obs_a": left.obs_id, "obs_b": right.obs_id},
                        "limits": "co-listing is not a transaction between the two",
                    })
                if (left.unit_price_copper and right.unit_price_copper
                        and right.unit_price_copper < left.unit_price_copper):
                    undercut.append({
                        "realm": left.realm, "item_id": item_id,
                        "undercutter": right.seller, "undercut": left.seller,
                        "from_copper": left.unit_price_copper,
                        "to_copper": right.unit_price_copper,
                        "observed_at": right.observed_at,
                        "confidence": "low",
                        "method": "a later observation of the same item by another"
                                  " seller carries a lower unit price",
                        "evidence": {"obs_a": left.obs_id, "obs_b": right.obs_id},
                        "limits": "the earlier listing may already have been sold or"
                                  " cancelled; intent to undercut is not observable",
                    })
    return {
        "co_listing": co,
        "undercut": undercut,
        "kind": "inference",
        "note": ("neither list is a confirmed transaction; both describe"
                 " observed listings"),
    }


def commodity_market_summary(con, region: str, realm: str) -> Dict[str, Any]:
    """Aggregate-only view of a commodity book (no seller identity exists)."""
    from . import ah as ah_module
    snap = ah_module.latest_snapshot(con, region, realm)
    if not snap:
        return {"realm": realm, "region": region, "available": False,
                "reason": "no auction snapshot ingested"}
    rows = [r for r in ah_module.snapshot_auctions(con, snap["snapshot_id"])
            if r["is_commodity"]]
    prices = [int(r["unit_price_copper"]) for r in rows
              if r["unit_price_copper"] is not None]
    return {
        "realm": realm, "region": region, "available": True,
        "snapshot_id": snap["snapshot_id"],
        "captured_at": snap["captured_at"],
        "commodity_listings": len(rows),
        "commodity_items": len({r["item_id"] for r in rows}),
        "units_listed": sum(int(r["quantity"]) for r in rows),
        "min_unit_price": min(prices) if prices else None,
        "median_unit_price": int(statistics.median(prices)) if prices else None,
        "seller_identity_available": False,
        "basis": "aggregate market only; commodities are anonymous",
        "note": ("per-seller competition cannot be measured for commodities;"
                 " use listing depth and price dispersion instead"),
    }
