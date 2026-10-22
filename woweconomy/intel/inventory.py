"""Reconciling bags, bank and posted stock against confirmed events.

The addon takes a full inventory picture on login, on logout and on
demand (``/dfaai inventory``).  This module compares consecutive pictures
and tries to explain every unit that moved with a **confirmed** journal
event.

The accounting, per snapshot window:

======================  =========================================
event                   movement
======================  =========================================
``listing``             bags -qty, posted +qty
``sale``                posted -qty (the gold is not an item)
``cancel`` / ``expire`` posted -qty, bags +qty (the mail was opened)
``purchase``            bags +qty
======================  =========================================

Anything left over is reported as ``unexplained_increase`` or
``unexplained_decrease``.  An unexplained decrease is **not** a sale:
it can be a vendor sale, a mail attachment, a bank deposit the addon did
not see, a disconnect between snapshots, or a real sale whose event was
never recorded.  The report says so instead of picking the convenient
story.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: personal locations, reconciled against the journal
LOCATIONS = ("bags", "bank", "posted")
#: shared storage: recorded, never reconciled as the player's own stock.
#: The warband bank is account-wide too: an alt moving an item is not a
#: movement this character made, so it explains nothing in the reconciler.
SHARED_LOCATIONS = ("guildbank", "warband")
ALL_LOCATIONS = LOCATIONS + SHARED_LOCATIONS

#: event type -> (location, sign) movements
MOVEMENTS: Dict[str, Tuple[Tuple[str, int], ...]] = {
    "listing": (("bags", -1), ("posted", +1)),
    "sale": (("posted", -1),),
    "cancel": (("posted", -1), ("bags", +1)),
    "expire": (("posted", -1), ("bags", +1)),
    "purchase": (("bags", +1),),
}

_CANONICAL_EVENT = {
    "auction_post": "listing",
    "posted": "listing",
    "sold": "sale",
    "cancelled": "cancel",
    "expired": "expire",
    "bought": "purchase",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def canonical_event_type(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    key = _CANONICAL_EVENT.get(key, key)
    return key if key in MOVEMENTS else None


@dataclass
class InventorySnapshot:
    snapshot_id: str
    captured_at: int
    character: str = ""
    realm: str = ""
    reason: str = ""
    source: str = "addon"
    bag_slots: int = 0
    #: locations the addon could actually read in this session.  A bank
    #: the character was nowhere near is *unknown*, not empty.
    available: set = field(default_factory=lambda: set(LOCATIONS))
    #: (location, item_id) -> (item_name, quantity)
    rows: Dict[Tuple[str, int], Tuple[str, int]] = field(default_factory=dict)
    #: one row per owned auction, with its price and time left.  The
    #: aggregate `posted` rows say how much is posted; these say at what
    #: price, which is what the out-of-game Blizzard scan matches against.
    posted_listings: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total_units(self) -> int:
        return sum(qty for _, qty in self.rows.values())

    def units(self, location: str) -> int:
        return sum(qty for (loc, _), (_, qty) in self.rows.items()
                   if loc == location)


@dataclass
class ReconciliationRow:
    location: str
    item_id: int
    item_name: str
    prev_qty: int
    cur_qty: int
    actual_delta: int
    expected_delta: int
    residual: int
    status: str
    evidence: str = ""

    def as_row(self) -> Tuple:
        return (self.location, self.item_id, self.item_name, self.prev_qty,
                self.cur_qty, self.actual_delta, self.expected_delta,
                self.residual, self.status, self.evidence)


@dataclass
class ReconciliationReport:
    snapshot_id: str
    previous_snapshot_id: str = ""
    rows: List[ReconciliationRow] = field(default_factory=list)
    confirmed_sales_units: int = 0
    confirmed_purchase_units: int = 0
    confirmed_listing_units: int = 0
    confirmed_failure_units: int = 0
    unexplained_decrease_units: int = 0
    unexplained_increase_units: int = 0
    event_not_reflected: int = 0
    sales_not_reflected: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def matched_rows(self) -> int:
        return sum(1 for r in self.rows if r.status == "matched")

    def summary(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "previous_snapshot_id": self.previous_snapshot_id,
            "rows": len(self.rows),
            "matched_rows": self.matched_rows,
            "unexplained_decrease_units": self.unexplained_decrease_units,
            "unexplained_increase_units": self.unexplained_increase_units,
            "event_not_reflected": self.event_not_reflected,
            "sales_not_reflected": self.sales_not_reflected,
            "confirmed_sales_units": self.confirmed_sales_units,
            "confirmed_purchase_units": self.confirmed_purchase_units,
            "confirmed_listing_units": self.confirmed_listing_units,
            "confirmed_failure_units": self.confirmed_failure_units,
            "notes": list(self.notes),
        }


def snapshot_id_for(character: str, realm: str, captured_at: int,
                    reason: str) -> str:
    raw = f"{character}|{realm}|{captured_at}|{reason}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def parse_inventory(payload: Dict[str, Any]) -> InventorySnapshot:
    """Normalise the addon's inventory export.

    Accepts both the JSON object written by the addon and the same object
    after a Lua round-trip.  Duplicate ``(location,item)`` rows are
    summed: the addon may emit one row per bag slot.
    """
    if not isinstance(payload, dict):
        raise ValueError("inventory payload is not an object")
    captured = _int(payload.get("capturedAt") or payload.get("captured_at"))
    character = str(payload.get("character") or "")
    realm = str(payload.get("realm") or "")
    reason = str(payload.get("reason") or "")
    snapshot = InventorySnapshot(
        snapshot_id=str(payload.get("snapshotId") or
                        snapshot_id_for(character, realm, captured, reason)),
        captured_at=captured, character=character, realm=realm,
        reason=reason, source=str(payload.get("source") or "addon"),
        bag_slots=_int(payload.get("bagSlots") or payload.get("bag_slots")),
    )
    available_payload = payload.get("available")
    #: an explicit map is the addon saying what it could read, including the
    #: locations that were readable and empty; rows must not override it
    explicit_available = isinstance(available_payload, (dict, list))
    if isinstance(available_payload, dict):
        snapshot.available = {loc for loc in ALL_LOCATIONS
                              if bool(available_payload.get(loc))}
    elif isinstance(available_payload, list):
        snapshot.available = {str(loc).lower() for loc in available_payload
                              if str(loc).lower() in ALL_LOCATIONS}
    listings = (payload.get("postedListings") or payload.get("posted_listings")
                or [])
    if isinstance(listings, list):
        for row in listings:
            if not isinstance(row, dict):
                continue
            item_id = _int(row.get("itemId") or row.get("item_id"))
            quantity = _int(row.get("quantity") or row.get("qty"))
            if item_id <= 0 or quantity <= 0:
                continue
            snapshot.posted_listings.append({
                "auction_id": _int(row.get("auctionId") or row.get("auction_id")),
                "item_id": item_id,
                "item_name": str(row.get("itemName") or row.get("item_name") or ""),
                "quantity": quantity,
                "unit_price_copper": _int(row.get("unitPriceCopper")
                                          or row.get("unit_price_copper")),
                "buyout_copper": _int(row.get("buyoutCopper")
                                      or row.get("buyout_copper")),
                "time_left": _int(row.get("timeLeft") or row.get("time_left")),
            })
    locations = payload.get("locations") or {}
    if not isinstance(locations, dict):
        return snapshot
    for location, rows in locations.items():
        loc = str(location).lower()
        if loc not in ALL_LOCATIONS or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = _int(row.get("itemId") or row.get("item_id"))
            quantity = _int(row.get("quantity") or row.get("qty"))
            if item_id <= 0 or quantity <= 0:
                continue
            name = str(row.get("itemName") or row.get("item_name") or "")
            key = (loc, item_id)
            old_name, old_qty = snapshot.rows.get(key, (name, 0))
            snapshot.rows[key] = (old_name or name, old_qty + quantity)
            # Rows that exist were read.  Older exports did not carry an
            # `available` map, and a guild bank that produced items must not
            # be reported as unreadable because of that.
            if not explicit_available:
                snapshot.available.add(loc)
    return snapshot


def parse_inventory_json(text: str) -> InventorySnapshot:
    return parse_inventory(json.loads(text))


def parse_export_file(path: Path | str) -> InventorySnapshot:
    """Read the inventory object out of the addon's SavedVariables file."""
    from woweconomy.analytics.auction_intel import extract_lua_string
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    for key in ("inventory_json", "inventory"):
        raw = extract_lua_string(content, key)
        if raw:
            try:
                return parse_inventory_json(raw)
            except (ValueError, TypeError):
                continue
    raise ValueError(f"no inventory block found in {path}")


def import_snapshot(con, snapshot: InventorySnapshot) -> bool:
    """Store a snapshot idempotently.  Returns True when it was new."""
    exists = con.execute("SELECT 1 FROM inventory_snapshot WHERE snapshot_id = ?",
                         (snapshot.snapshot_id,)).fetchone()
    if exists:
        return False
    con.execute(
        "INSERT INTO inventory_snapshot (snapshot_id, captured_at, character,"
        " realm, reason, source, bag_slots, available_json, imported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (snapshot.snapshot_id, snapshot.captured_at, snapshot.character,
         snapshot.realm, snapshot.reason, snapshot.source, snapshot.bag_slots,
         json.dumps(sorted(snapshot.available)), _utcnow()))
    con.executemany(
        "INSERT INTO inventory_row (snapshot_id, location, item_id, item_name,"
        " quantity) VALUES (?,?,?,?,?)",
        [(snapshot.snapshot_id, loc, item_id, name, qty)
         for (loc, item_id), (name, qty) in snapshot.rows.items()])
    con.executemany(
        "INSERT OR REPLACE INTO inventory_posted_listing (snapshot_id,"
        " auction_id, item_id, item_name, quantity, unit_price_copper,"
        " buyout_copper, time_left) VALUES (?,?,?,?,?,?,?,?)",
        [(snapshot.snapshot_id, row.get("auction_id") or 0,
          row.get("item_id") or 0, row.get("item_name") or "",
          row.get("quantity") or 1, row.get("unit_price_copper") or 0,
          row.get("buyout_copper") or 0, row.get("time_left") or 0)
         for row in snapshot.posted_listings])
    con.commit()
    return True


def load_snapshot(con, snapshot_id: str) -> Optional[InventorySnapshot]:
    head = con.execute("SELECT * FROM inventory_snapshot WHERE snapshot_id = ?",
                       (snapshot_id,)).fetchone()
    if not head:
        return None
    try:
        available = set(json.loads(head["available_json"]))
    except (ValueError, TypeError, IndexError, KeyError):
        # A row written before the column existed is treated as fully
        # readable; a row that explicitly says "nothing was readable"
        # stays empty and is not promoted to a full picture.
        available = set(LOCATIONS)
    snap = InventorySnapshot(
        snapshot_id=head["snapshot_id"], captured_at=int(head["captured_at"]),
        character=head["character"], realm=head["realm"], reason=head["reason"],
        source=head["source"], bag_slots=int(head["bag_slots"]),
        available=available)
    for row in con.execute("SELECT * FROM inventory_row WHERE snapshot_id = ?",
                           (snapshot_id,)):
        snap.rows[(row["location"], int(row["item_id"]))] = (
            row["item_name"], int(row["quantity"]))
    return snap


def latest_posted(con, realm: str,
                  max_age_s: int = 7 * 86400) -> List[Dict[str, Any]]:
    """The newest snapshot whose owned-auction list was actually loaded.

    At login `posted` is unknown, so the newest snapshot is usually the
    wrong one to read; walking back to the last readable list is what
    makes the out-of-game scan work after a session that ended at the
    Auction House.  Empty still means "not loaded yet", never "no
    auctions": the caller must not relist from an empty answer.
    """
    heads = con.execute(
        "SELECT snapshot_id, captured_at, available_json FROM inventory_snapshot"
        " WHERE LOWER(realm) = LOWER(?) ORDER BY captured_at DESC LIMIT 40",
        (realm,)).fetchall()
    now = int(__import__("time").time())
    for head in heads:
        try:
            available = json.loads(head["available_json"])
        except (ValueError, TypeError):
            available = list(LOCATIONS)
        if "posted" not in {str(loc).lower() for loc in available}:
            continue
        if max_age_s and now - int(head["captured_at"] or 0) > max_age_s:
            return []
        rows = con.execute(
            "SELECT * FROM inventory_posted_listing WHERE snapshot_id = ?"
            " ORDER BY item_id, unit_price_copper", (head["snapshot_id"],))
        return [{key: row[key] for key in row.keys()} for row in rows]
    return []


def current_units(con, realm: str) -> Dict[int, int]:
    """The latest readable stock for a realm, summed over its locations.

    A location the snapshot could not read contributes nothing (it has no
    rows), so an unreadable bank cannot look like empty stock.  Returns an
    empty mapping when the realm has no snapshot at all.
    """
    head = con.execute(
        "SELECT snapshot_id, available_json FROM inventory_snapshot"
        " WHERE LOWER(realm) = LOWER(?) ORDER BY captured_at DESC LIMIT 1",
        (realm,)).fetchone()
    if not head:
        return {}
    try:
        available = [str(loc).lower() for loc in json.loads(head["available_json"])]
    except (ValueError, TypeError):
        available = list(LOCATIONS)
    available = [loc for loc in available if loc in LOCATIONS]
    if not available:
        return {}
    placeholders = ",".join("?" * len(available))
    rows = con.execute(
        "SELECT item_id, SUM(quantity) FROM inventory_row WHERE snapshot_id = ?"
        f" AND location IN ({placeholders}) GROUP BY item_id",
        (head["snapshot_id"], *available)).fetchall()
    return {int(row[0]): int(row[1] or 0) for row in rows if int(row[1] or 0) > 0}


def previous_snapshot(con, snapshot: InventorySnapshot) -> Optional[InventorySnapshot]:
    row = con.execute(
        "SELECT snapshot_id FROM inventory_snapshot WHERE captured_at < ?"
        " AND character = ? AND LOWER(realm) = LOWER(?)"
        " ORDER BY captured_at DESC LIMIT 1",
        (snapshot.captured_at, snapshot.character, snapshot.realm)).fetchone()
    return load_snapshot(con, row["snapshot_id"]) if row else None


def _movement(events: Iterable[Dict[str, Any]],
              window_start: int, window_end: int,
              character: str) -> Tuple[Dict[Tuple[str, int], int],
                                        Dict[Tuple[str, int], List[str]],
                                        Dict[str, int]]:
    expected: Dict[Tuple[str, int], int] = {}
    evidence: Dict[Tuple[str, int], List[str]] = {}
    counts = {"sale": 0, "listing": 0, "purchase": 0, "failure": 0}
    for event in events:
        kind = canonical_event_type(event.get("event_type") or event.get("eventType"))
        if kind is None:
            continue
        ts = _int(event.get("ts") or event.get("timestamp") or event.get("timestamp_unix"))
        if not (window_start < ts <= window_end):
            continue
        owner = str(event.get("character") or "")
        if character and owner and owner != character:
            continue
        item_id = _int(event.get("item_id") or event.get("itemId"))
        quantity = _int(event.get("quantity") or event.get("qty"))
        if item_id <= 0 or quantity <= 0:
            continue
        if kind == "sale":
            counts["sale"] += quantity
        elif kind == "listing":
            counts["listing"] += quantity
        elif kind == "purchase":
            counts["purchase"] += quantity
        else:
            counts["failure"] += quantity
        for location, sign in MOVEMENTS[kind]:
            key = (location, item_id)
            expected[key] = expected.get(key, 0) + sign * quantity
            evidence.setdefault(key, []).append(
                f"{kind}:{quantity}@{ts}")
    return expected, evidence, counts


def reconcile(prev: InventorySnapshot, cur: InventorySnapshot,
              events: Sequence[Dict[str, Any]]) -> ReconciliationReport:
    """Compare two pictures and explain the movement with events."""
    report = ReconciliationReport(snapshot_id=cur.snapshot_id,
                                  previous_snapshot_id=prev.snapshot_id)
    report.notes.append(
        "a decrease with no matching event is reported as unexplained, never as a sale")
    expected, evidence, counts = _movement(
        events, prev.captured_at, cur.captured_at, cur.character or prev.character)
    report.confirmed_sales_units = counts["sale"]
    report.confirmed_listing_units = counts["listing"]
    report.confirmed_purchase_units = counts["purchase"]
    report.confirmed_failure_units = counts["failure"]

    # The guild bank is shared with the whole guild: another member moving an
    # item is not an unexplained movement of this character's stock, so it is
    # recorded but never reconciled here.
    comparable = prev.available & cur.available & set(LOCATIONS)
    report.notes.append("locations compared: " + ", ".join(sorted(comparable)))
    if cur.available & set(SHARED_LOCATIONS):
        report.notes.append("guild bank recorded but not reconciled: shared storage")
    keys = {k for k in (set(prev.rows) | set(cur.rows) | set(expected))
            if k[0] in comparable}
    for location, item_id in sorted(keys, key=lambda k: (k[0], k[1])):
        name_prev, qty_prev = prev.rows.get((location, item_id), ("", 0))
        name_cur, qty_cur = cur.rows.get((location, item_id), ("", 0))
        delta = qty_cur - qty_prev
        want = expected.get((location, item_id), 0)
        residual = delta - want
        if delta == 0 and want == 0:
            continue
        if residual == 0:
            status = "matched"
        elif delta == 0 and want != 0:
            status = "event_not_reflected"
        elif residual > 0:
            status = "unexplained_increase"
        else:
            status = "unexplained_decrease"
        row = ReconciliationRow(
            location=location, item_id=item_id,
            item_name=name_cur or name_prev, prev_qty=qty_prev, cur_qty=qty_cur,
            actual_delta=delta, expected_delta=want, residual=residual,
            status=status, evidence=";".join(evidence.get((location, item_id), ())))
        report.rows.append(row)
        if status == "unexplained_decrease":
            report.unexplained_decrease_units += abs(residual)
        elif status == "unexplained_increase":
            report.unexplained_increase_units += residual
        elif status == "event_not_reflected":
            report.event_not_reflected += 1
            if any(e.startswith("sale:") for e in
                   evidence.get((location, item_id), ())) and location == "posted":
                report.sales_not_reflected += 1
    return report


def persist_report(con, report: ReconciliationReport) -> None:
    con.execute("DELETE FROM reconciliation WHERE snapshot_id = ?",
                (report.snapshot_id,))
    con.executemany(
        "INSERT INTO reconciliation (snapshot_id, location, item_id, item_name,"
        " prev_qty, cur_qty, actual_delta, expected_delta, residual, status,"
        " evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(report.snapshot_id,) + row.as_row() for row in report.rows])
    con.commit()


def load_journal_events(tsm_db: Path | str, start_ts: int, end_ts: int,
                        character: str = "") -> List[Dict[str, Any]]:
    """Read our confirmed events from the existing journal tables.

    ``auction_intel_events`` is the addon's own log and is preferred;
    ``auction_events`` is the older importer's table.  The two have
    different column names, so each is normalised to the same shape.
    """
    import sqlite3
    out: List[Dict[str, Any]] = []
    con = sqlite3.connect(str(tsm_db))
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute(
                "SELECT event_type, ts, item_id, quantity, character FROM"
                " auction_intel_events WHERE ts >= ? AND ts <= ?",
                (start_ts, end_ts)).fetchall()
            for row in rows:
                out.append({"event_type": row["event_type"], "ts": row["ts"],
                            "item_id": row["item_id"],
                            "quantity": row["quantity"],
                            "character": row["character"]})
        except sqlite3.OperationalError:
            pass
        try:
            rows = con.execute(
                "SELECT event_type, timestamp_unix, item_id, qty, character FROM"
                " auction_events WHERE timestamp_unix >= ? AND timestamp_unix <= ?",
                (start_ts, end_ts)).fetchall()
            for row in rows:
                out.append({"event_type": row["event_type"],
                            "ts": row["timestamp_unix"],
                            "item_id": row["item_id"], "quantity": row["qty"],
                            "character": row["character"]})
        except sqlite3.OperationalError:
            pass
    finally:
        con.close()
    if character:
        out = [e for e in out if not e["character"] or e["character"] == character]
    # Both tables can hold the same event (the addon import feeds one, the
    # legacy importer the other).  Deduplicate on the facts we compare.
    seen = set()
    unique = []
    for event in sorted(out, key=lambda e: (e["ts"], str(e["event_type"]))):
        key = (event["event_type"], int(event["ts"] or 0),
               int(event["item_id"] or 0), int(event["quantity"] or 0))
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique
