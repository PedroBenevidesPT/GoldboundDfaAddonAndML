"""Undercuts derived from the Blizzard auction snapshot, without the game.

The addon's scan answers "who is below me, by name" -- but only while the
client is open.  The public Auction House API returns every active auction
of a connected realm and is refreshed roughly hourly.  It has no seller
field, so it cannot name a competitor; it *can* say, for every listing the
player has posted, the cheapest competing price, how many units are ahead,
and how the picture changed since the previous hour.

That is what this module builds:

* :func:`derive_observations` matches the addon's owned-auction list
  (item, auction id, unit price, time left) against one API snapshot and
  returns rows in the same shape as an in-game undercut observation, with
  ``source="blizzard_api"`` and ``confidence="price-only"``;
* :func:`detect_price_resets` compares two snapshots and reports items
  whose cheapest price jumped while supply collapsed -- the signature of a
  buy-out and repost, not of normal trading.

The seller is left empty on purpose and the summary labels it "(seller not
in API)".  An empty name is honest; inventing one would poison the seller
profiles the addon collects.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import ah, inventory, undercut

#: Blizzard's coarse time-left categories, in hours.  The addon sends
#: seconds when it has them; the API only ever sends the band, and a band
#: midpoint would be a fabricated number, so these are used only to keep
#: the "let it expire" rule from firing on an unknown clock.
BAND_HOURS = {"SHORT": 0.5, "MEDIUM": 6.0, "LONG": 24.0, "VERY_LONG": 72.0}

SOURCE = "blizzard_api"
CONFIDENCE = "price-only"


def hours_left(seconds: Any, band: Any = "") -> Optional[float]:
    """Hours left, from the addon's seconds or the API's band."""
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return value / 3600.0
    if isinstance(band, str) and band.upper() in BAND_HOURS:
        return BAND_HOURS[band.upper()]
    # Unknown, not zero: `relist` treats None as "no clock", so a missing
    # time left cannot be mistaken for "about to expire".
    return None


def _rows(con: sqlite3.Connection, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
    try:
        return [{key: row[key] for key in row.keys()}
                for row in con.execute(sql, params)]
    except sqlite3.OperationalError:
        return []


def latest_snapshot(con: sqlite3.Connection, realm: str) -> Optional[Dict[str, Any]]:
    where, params = ah.realm_match(con, realm)
    rows = _rows(con,
                 f"SELECT * FROM ah_snapshot WHERE {where}"
                 " ORDER BY captured_at DESC LIMIT 1", params)
    return rows[0] if rows else None


def load_api_auctions(con: sqlite3.Connection, snapshot_id: str) -> List[Dict[str, Any]]:
    return _rows(con,
                 "SELECT auction_id, item_id, item_context, quantity,"
                 " unit_price_copper, time_left_band, is_commodity,"
                 " pet_species_id, bonus_lists, modifiers"
                 " FROM ah_auction WHERE snapshot_id = ?"
                 " AND unit_price_copper IS NOT NULL AND unit_price_copper > 0",
                 (snapshot_id,))


def _has_variant_fields(row: Dict[str, Any]) -> bool:
    """Whether an API row names a variant that an item id alone cannot carry.

    A non-commodity auction can be a different item level, suffix, bonus
    list or battle-pet species.  The addon's owned-auction export does not
    yet keep the matching item key, so those rows are not comparable to the
    cheapest API listing without more evidence.
    """
    context = row.get("item_context")
    bonus = str(row.get("bonus_lists") or "").strip().lower()
    modifiers = str(row.get("modifiers") or "").strip().lower()
    pet = row.get("pet_species_id")
    empty = ("", "[]", "null", "none")
    return (context not in (None, 0, "0")
            or bonus not in empty
            or modifiers not in empty
            or pet not in (None, 0, "0"))


def _by_item(rows: Iterable[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        item_id = int(row.get("item_id") or 0)
        if item_id <= 0:
            continue
        out.setdefault(item_id, []).append(row)
    return out


def derive_observations(posted: Iterable[Dict[str, Any]],
                        api_rows: Iterable[Dict[str, Any]],
                        realm: str, character: str, captured_at: int,
                        scan_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """One undercut row per posted auction that has a cheaper competitor."""
    api_rows = list(api_rows)
    posted = list(posted)
    listings = _by_item(api_rows)
    bands = {int(row.get("auction_id") or 0): str(row.get("time_left_band") or "")
             for row in api_rows if row.get("auction_id")}
    # Every auction the player owns is excluded, not just the one being
    # evaluated: a second own listing must never become the "competitor"
    # that justifies cancelling the first.
    own_ids = {int(row.get("auction_id") or 0) for row in posted
               if int(row.get("auction_id") or 0) > 0}
    stats = {"posted": 0, "priced": 0, "undercut": 0,
             "no_competitor": 0, "no_market_data": 0,
             "variant_ambiguous": 0}
    observations: List[Dict[str, Any]] = []

    for own in posted:
        stats["posted"] += 1
        item_id = int(own.get("item_id") or 0)
        own_unit = int(own.get("unit_price_copper") or 0)
        own_qty = max(1, int(own.get("quantity") or 1))
        if item_id <= 0 or own_unit <= 0:
            continue
        stats["priced"] += 1
        market = listings.get(item_id)
        if not market:
            stats["no_market_data"] += 1
            continue

        own_auction_id = int(own.get("auction_id") or 0)
        competitors = [row for row in market
                       if int(row.get("auction_id") or 0) not in own_ids]
        if not competitors:
            stats["no_competitor"] += 1
            continue
        below = [row for row in competitors
                 if 0 < int(row.get("unit_price_copper") or 0) < own_unit]
        if not below:
            continue

        lowest = min(below, key=lambda row: int(row["unit_price_copper"]))
        lowest_unit = int(lowest["unit_price_copper"])
        items_ahead = sum(int(row.get("quantity") or 1) for row in competitors
                          if int(row.get("unit_price_copper") or 0) < own_unit)
        # The API has no seller and the owned-auction snapshot does not retain
        # the item key.  A variant-bearing non-commodity cannot be compared to
        # the cheapest listing -- or counted in the queue -- on item id alone.
        if (not bool(lowest.get("is_commodity"))
                and any(_has_variant_fields(row) for row in below)):
            reference_status = "variant-ambiguous"
            stats["variant_ambiguous"] += 1
        else:
            reference_status = "price-only"
        band = bands.get(own_auction_id, "")
        # Only the addon's own clock is stored; the API band is a category,
        # and turning its midpoint into "hours left" would invent precision.
        # `hours_left()` keeps the band for a caller that asks for it.
        _ = hours_left(own.get("time_left"), band)
        time_left = int(own.get("time_left") or 0)
        stats["undercut"] += 1
        observations.append({
            "observation_id": undercut.observation_id(
                realm, character, scan_id, own_auction_id or item_id),
            "scan_id": scan_id,
            "observed_at": captured_at,
            "realm": realm,
            "character": character,
            "item_id": item_id,
            "item_name": str(own.get("item_name") or ""),
            "item_key": f"i:{item_id}",
            "item_link": "",
            "own_auction_id": own_auction_id or item_id,
            "own_quantity": own_qty,
            "own_buyout_copper": int(own.get("buyout_copper") or 0),
            "own_unit_price_copper": own_unit,
            "lowest_unit_price_copper": lowest_unit,
            # no seller in this source -- the field stays empty
            "lowest_seller": "",
            "lowest_seller_realm": "",
            "lowest_auction_id": int(lowest.get("auction_id") or 0),
            "undercut_copper": own_unit - lowest_unit,
            "undercut_pct": round((own_unit - lowest_unit) / own_unit, 6),
            # my units are not ahead: the competitor's are
            "items_ahead": items_ahead,
            "sellers_below": 0,
            "time_left": time_left,
            "sellers": [],
            "source": SOURCE,
            "confidence": CONFIDENCE,
            # price-only is evidence, not an instruction; `relist` keeps it
            # out of cancel-and-relist until a verified scan confirms the
            # same item key and a named seller.
            "reference_status": reference_status,
        })
    return observations, stats


def scan_realm(con: sqlite3.Connection, realm: str, character: str = "",
               snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Derive and store the undercut picture for one realm, from the API."""
    snapshot = snapshot or latest_snapshot(con, realm)
    if not snapshot:
        return {"realm": realm, "error": "no Blizzard snapshot for this realm"}
    posted = inventory.latest_posted(con, realm)
    if not posted:
        return {"realm": realm, "error": "no readable owned-auction list"
                " (open the Auction House with the 1.4.0 addon)"}
    api_rows = load_api_auctions(con, snapshot["snapshot_id"])
    observations, stats = derive_observations(
        posted, api_rows, realm=realm, character=character or "",
        captured_at=int(snapshot["captured_at"]),
        scan_id=f"{SOURCE}:{snapshot['snapshot_id']}")
    inserted = undercut.import_observations(con, observations)
    return {
        "realm": realm,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_at": int(snapshot["captured_at"]),
        "api_auctions": len(api_rows),
        "posted_listings": len(posted),
        "stats": stats,
        "inserted": inserted,
        "source": SOURCE,
        "confidence": CONFIDENCE,
        "note": ("the API carries no seller: these rows say which of my"
                 " auctions is behind, at what price and by how many units,"
                 " not who is behind them"),
    }


def price_state(api_rows: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """item id -> cheapest unit price and total supply, for one snapshot."""
    state: Dict[int, Dict[str, Any]] = {}
    for row in api_rows:
        item_id = int(row.get("item_id") or 0)
        unit = int(row.get("unit_price_copper") or 0)
        qty = int(row.get("quantity") or 0)
        if item_id <= 0 or unit <= 0:
            continue
        entry = state.get(item_id)
        if entry is None:
            state[item_id] = {"cheapest": unit, "supply": qty,
                              "listings": 1}
        else:
            entry["cheapest"] = min(entry["cheapest"], unit)
            entry["supply"] += qty
            entry["listings"] += 1
    return state


def detect_price_resets(previous: Dict[int, Dict[str, Any]],
                        current: Dict[int, Dict[str, Any]],
                        min_jump: float = 0.30,
                        min_supply_drop: float = 0.40,
                        min_listings: int = 2) -> List[Dict[str, Any]]:
    """Items whose cheapest price jumped while supply collapsed.

    That is the shape of a buy-out and repost (a reset), not of normal
    trading: a sale lowers supply without raising the cheapest price, and a
    new listing lowers the price.
    """
    out: List[Dict[str, Any]] = []
    for item_id, now in current.items():
        before = previous.get(item_id)
        if not before or before["cheapest"] <= 0 or before["supply"] <= 0:
            continue
        if now["listings"] < min_listings or before["listings"] < min_listings:
            continue
        jump = (now["cheapest"] - before["cheapest"]) / before["cheapest"]
        drop = (before["supply"] - now["supply"]) / before["supply"]
        if jump >= min_jump and drop >= min_supply_drop:
            out.append({
                "item_id": item_id,
                "previous_cheapest": before["cheapest"],
                "current_cheapest": now["cheapest"],
                "jump_pct": round(jump, 4),
                "previous_supply": before["supply"],
                "current_supply": now["supply"],
                "supply_drop_pct": round(drop, 4),
                "previous_listings": before["listings"],
                "current_listings": now["listings"],
            })
    out.sort(key=lambda row: row["jump_pct"], reverse=True)
    return out
