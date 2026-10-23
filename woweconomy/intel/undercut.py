"""Who is undercutting my auctions, by name.

The addon reads the sellers straight out of the Auctionator undercut scan's
own search results: each result carries an ``owners`` array, and the
player's own listings say ``"player"``.  That makes a price fact with an
identity attached, which the tooltip capture never had:

    at time T, for item I, these sellers had listings below my unit price.

Nothing here infers a sale, and nothing is attributed to a seller who was
not seen in the result set.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

MAX_STORED_SELLERS = 50


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def observation_id(realm: str, character: str, scan_id: str,
                   own_auction_id: int) -> str:
    raw = "|".join((str(realm or "").casefold(),
                    str(character or "").casefold(),
                    str(scan_id or ""),
                    str(int(own_auction_id or 0))))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _sellers(raw: Any) -> List[Dict[str, Any]]:
    rows = raw if isinstance(raw, list) else []
    out: List[Dict[str, Any]] = []
    for row in rows[:MAX_STORED_SELLERS]:
        if not isinstance(row, dict):
            continue
        seller = str(row.get("seller") or "").strip()
        if not seller:
            continue
        out.append({
            "seller": seller,
            "seller_realm": str(row.get("sellerRealm")
                                or row.get("seller_realm") or ""),
            "unit_price_copper": max(0, _int(row.get("unitPriceCopper")
                                             or row.get("unit_price_copper"))),
            "buyout_copper": max(0, _int(row.get("buyoutCopper")
                                         or row.get("buyout_copper"))),
            "quantity": max(1, _int(row.get("quantity"), 1)),
            "auction_id": _int(row.get("auctionId") or row.get("auction_id")),
            "time_left": _int(row.get("timeLeft") or row.get("time_left")),
        })
    return out


def parse_observation(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    seller = str(row.get("lowestSeller") or row.get("lowest_seller") or "").strip()
    item_name = str(row.get("itemName") or row.get("item_name") or "").strip()
    if not seller or not item_name:
        return None
    realm = str(row.get("realm") or "")
    character = str(row.get("character") or "")
    scan_id = str(row.get("scanId") or row.get("scan_id") or "")
    own_auction_id = _int(row.get("ownAuctionId") or row.get("own_auction_id"))
    return {
        "observation_id": str(row.get("observationId")
                              or row.get("observation_id")
                              or observation_id(realm, character, scan_id,
                                                own_auction_id)),
        "scan_id": scan_id,
        "observed_at": _int(row.get("observedAt") or row.get("observed_at")),
        "realm": realm,
        "character": character,
        "item_id": _int(row.get("itemId") or row.get("item_id")),
        "item_name": item_name,
        "item_key": str(row.get("itemKey") or row.get("item_key") or ""),
        "item_link": str(row.get("itemLink") or row.get("item_link") or ""),
        "own_auction_id": own_auction_id,
        "own_quantity": max(1, _int(row.get("ownQuantity")
                                    or row.get("own_quantity"), 1)),
        "own_buyout_copper": max(0, _int(row.get("ownBuyoutCopper")
                                         or row.get("own_buyout_copper"))),
        "own_unit_price_copper": max(0, _int(row.get("ownUnitPriceCopper")
                                             or row.get("own_unit_price_copper"))),
        "lowest_unit_price_copper": max(0, _int(row.get("lowestUnitPriceCopper")
                                                or row.get("lowest_unit_price_copper"))),
        "lowest_seller": seller,
        "lowest_seller_realm": str(row.get("lowestSellerRealm")
                                   or row.get("lowest_seller_realm") or ""),
        "lowest_auction_id": _int(row.get("lowestAuctionId")
                                  or row.get("lowest_auction_id")),
        "undercut_copper": _int(row.get("undercutCopper")
                                or row.get("undercut_copper")),
        "undercut_pct": _float(row.get("undercutPct")
                               or row.get("undercut_pct")),
        "items_ahead": _int(row.get("itemsAhead") or row.get("items_ahead")),
        "sellers_below": _int(row.get("sellersBelow")
                              or row.get("sellers_below")),
        "time_left": _int(row.get("timeLeft") or row.get("time_left")),
        "sellers": _sellers(row.get("sellers")),
        "source": str(row.get("source") or ""),
        "confidence": str(row.get("confidence") or ""),
        # The addon's own scan has the item key and the seller, so its
        # reference is comparable and named.  A missing field means an older
        # export: keep the honest confidence-derived default rather than
        # silently promoting it to verified.
        "reference_status": _reference_status(row),
    }


def _reference_status(row: Dict[str, Any]) -> str:
    explicit = str(row.get("referenceStatus") or row.get("reference_status")
                   or "").strip().lower()
    if explicit:
        return explicit
    confidence = str(row.get("confidence") or "").strip().lower()
    return "verified" if confidence == "verified" else "price-only"


def parse_undercut_jsonl(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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


def parse_export_file(path: Path | str) -> List[Dict[str, Any]]:
    from woweconomy.analytics.auction_intel import extract_lua_string
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    raw = extract_lua_string(content, "undercuts_jsonl")
    if raw is None:
        raise ValueError(
            f"no undercuts_jsonl block found in {path}"
            " (run an Auctionator cancel scan with the 1.3.0+ addon loaded)")
    # An empty block is valid: the scan ran, nobody was below my price.
    return parse_undercut_jsonl(raw)


def import_observations(con: sqlite3.Connection,
                        rows: Iterable[Dict[str, Any]]) -> int:
    added = 0
    for row in rows:
        cursor = con.execute(
            "INSERT OR IGNORE INTO undercut_observation (observation_id,"
            " scan_id, observed_at, realm, character, item_id, item_name,"
            " item_key, item_link, own_auction_id, own_quantity,"
            " own_buyout_copper, own_unit_price_copper,"
            " lowest_unit_price_copper, lowest_seller, lowest_seller_realm,"
            " lowest_auction_id, undercut_copper, undercut_pct, items_ahead,"
            " sellers_below, time_left, sellers_json, source, confidence,"
            " reference_status, imported_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["observation_id"], row["scan_id"], row["observed_at"],
             row["realm"], row["character"], row["item_id"], row["item_name"],
             row["item_key"], row["item_link"], row["own_auction_id"],
             row["own_quantity"], row["own_buyout_copper"],
             row["own_unit_price_copper"], row["lowest_unit_price_copper"],
             row["lowest_seller"], row["lowest_seller_realm"],
             row["lowest_auction_id"], row["undercut_copper"],
             row["undercut_pct"], row["items_ahead"], row["sellers_below"],
             row["time_left"], json.dumps(row["sellers"]), row["source"],
             row["confidence"],
             row.get("reference_status")
             or ("verified" if str(row.get("confidence") or "").lower() == "verified"
                 else "price-only"),
             _utcnow()))
        added += cursor.rowcount
    con.commit()
    return added


def load_observations(con: sqlite3.Connection, realm: str = "",
                      since: int = 0) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM undercut_observation WHERE observed_at >= ?"
    params: List[Any] = [since]
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        params.append(realm)
    sql += " ORDER BY observed_at, own_auction_id"
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        try:
            item["sellers"] = json.loads(item.get("sellers_json") or "[]")
        except (ValueError, TypeError):
            item["sellers"] = []
        out.append(item)
    return out


def summarise(con: sqlite3.Connection, realm: str = "", days: int = 30,
              limit: int = 20) -> Dict[str, Any]:
    """Group the observations by seller and by item.

    A seller only appears when a scan saw their listing below one of ours;
    the counts are observations, not sales, and the same scan is never
    counted twice (the import is idempotent).
    """
    import time
    since = int(time.time()) - max(1, days) * 86400
    rows = load_observations(con, realm, since)
    scans = {row["scan_id"] for row in rows if row["scan_id"]}
    sellers: Dict[str, Dict[str, Any]] = {}
    items: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = (row["lowest_seller"].casefold(), row["lowest_seller_realm"])
        entry = sellers.setdefault(key, {
            "seller": row["lowest_seller"],
            "seller_realm": row["lowest_seller_realm"],
            "observations": 0,
            "items": set(),
            "undercut_copper_total": 0,
            "undercut_pct_total": 0.0,
            "first_seen": row["observed_at"],
            "last_seen": row["observed_at"],
            "worst_example": None,
        })
        entry["observations"] += 1
        entry["items"].add(row["item_name"])
        entry["undercut_copper_total"] += max(0, row["undercut_copper"])
        entry["undercut_pct_total"] += max(0.0, row["undercut_pct"])
        entry["first_seen"] = min(entry["first_seen"], row["observed_at"])
        entry["last_seen"] = max(entry["last_seen"], row["observed_at"])
        worst = entry["worst_example"]
        if worst is None or row["undercut_pct"] > worst["undercut_pct"]:
            entry["worst_example"] = {
                "item_name": row["item_name"],
                "own_unit_price_copper": row["own_unit_price_copper"],
                "lowest_unit_price_copper": row["lowest_unit_price_copper"],
                "undercut_pct": round(row["undercut_pct"], 4),
                "observed_at": row["observed_at"],
            }

        # One item, whatever price each own auction carried: the row shows
        # the price of the latest observation next to the cheapest listing.
        item = items.setdefault(row["item_name"].casefold(), {
            "item_name": row["item_name"],
            "own_unit_price_copper": row["own_unit_price_copper"],
            "observations": 0,
            "sellers": {},
            "lowest_unit_price_copper": row["lowest_unit_price_copper"],
            "last_seen": row["observed_at"],
        })
        if row["observed_at"] >= item["last_seen"]:
            item["own_unit_price_copper"] = row["own_unit_price_copper"]
        item["observations"] += 1
        item["lowest_unit_price_copper"] = min(
            item["lowest_unit_price_copper"], row["lowest_unit_price_copper"])
        item["last_seen"] = max(item["last_seen"], row["observed_at"])
        item["sellers"][row["lowest_seller"]] = (
            item["sellers"].get(row["lowest_seller"], 0) + 1)

    seller_rows = []
    for entry in sellers.values():
        count = entry["observations"]
        seller_rows.append({
            # A Blizzard-API observation has no seller.  Labelling the gap
            # is more honest than inventing a competitor name.
            "seller": entry["seller"] or "(seller not in API)",
            "seller_realm": entry["seller_realm"],
            "observations": count,
            "items_affected": len(entry["items"]),
            "avg_undercut_copper": round(entry["undercut_copper_total"] / count),
            "avg_undercut_pct": round(entry["undercut_pct_total"] / count, 4),
            "first_seen": entry["first_seen"],
            "last_seen": entry["last_seen"],
            "worst_example": entry["worst_example"],
        })
    seller_rows.sort(key=lambda r: (r["observations"], r["avg_undercut_pct"]),
                     reverse=True)

    item_rows = []
    for entry in items.values():
        item_rows.append({
            "item_name": entry["item_name"],
            "own_unit_price_copper": entry["own_unit_price_copper"],
            "observations": entry["observations"],
            "lowest_unit_price_copper": entry["lowest_unit_price_copper"],
            "last_seen": entry["last_seen"],
            "sellers": sorted(entry["sellers"].items(),
                              key=lambda kv: kv[1], reverse=True)[:5],
        })
    item_rows.sort(key=lambda r: r["observations"], reverse=True)

    return {
        "realm": realm or "all",
        "days": days,
        "scans": len(scans),
        "observations": len(rows),
        "sellers_total": len(seller_rows),
        "items_total": len(item_rows),
        "sellers": seller_rows[:limit],
        "items": item_rows[:limit],
        "note": ("each row is a scan fact: this seller had a listing below my"
                 " unit price. It is not a sale, and the lowest price is the"
                 " cheapest listing seen, not necessarily the one that sold."),
    }
