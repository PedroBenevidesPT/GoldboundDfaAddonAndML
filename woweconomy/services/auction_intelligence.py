"""Auction Intelligence Ingestion Pipeline & Event Store.

Captures, normalizes, validates, and stores empirical auction house events
with exact timestamps, fee attribution, and source provenance.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
VALID_EVENT_TYPES = {"sale", "purchase", "listing", "cancel", "expire"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auction_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    event_type TEXT NOT NULL,          -- 'sale', 'purchase', 'listing', 'cancel', 'expire'
    ts TEXT NOT NULL,                  -- ISO 8601 UTC timestamp
    timestamp_unix INTEGER NOT NULL,   -- Unix epoch seconds
    item_id INTEGER,                   -- Item ID (null if unresolvable)
    item_name TEXT NOT NULL,
    item_link TEXT,
    qty INTEGER NOT NULL DEFAULT 1,
    unit_price_copper INTEGER NOT NULL DEFAULT 0,
    total_price_copper INTEGER NOT NULL DEFAULT 0,
    deposit_copper INTEGER NOT NULL DEFAULT 0,
    fee_copper INTEGER NOT NULL DEFAULT 0,       -- consignment cut
    net_proceeds_copper INTEGER NOT NULL DEFAULT 0,
    realm TEXT NOT NULL,
    character TEXT,
    faction TEXT,
    counterparty TEXT,                 -- buyer or seller player name
    source TEXT NOT NULL,              -- 'journalator_archive', 'dfa_addon', etc.
    provenance TEXT NOT NULL,          -- origin file / store
    confidence TEXT NOT NULL,          -- 'verified', 'proxy', 'inferred'
    market_ref_copper INTEGER,         -- TSM dbmarket at event time if recorded
    raw_json TEXT,                     -- raw preserved data
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auction_events_item_realm ON auction_events (item_id, realm);
CREATE INDEX IF NOT EXISTS idx_auction_events_type_ts ON auction_events (event_type, timestamp_unix);
CREATE INDEX IF NOT EXISTS idx_auction_events_realm_ts ON auction_events (realm, timestamp_unix);
"""


@dataclass
class IngestionResult:
    total: int = 0
    inserted: int = 0
    duplicates: int = 0
    invalid: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"Ingestion Result: total={self.total}, inserted={self.inserted}, "
                f"duplicates={self.duplicates}, invalid={self.invalid}, "
                f"errors={len(self.errors)}")


def init_auction_events_db(con: sqlite3.Connection) -> None:
    """Ensure the auction_events table and indices exist."""
    con.executescript(SCHEMA_SQL)
    con.commit()


def extract_item_id_from_link(link: Optional[str]) -> Optional[int]:
    """Extract item ID from WoW itemLink, itemString, or battlepet link."""
    if not link:
        return None
    m = re.search(r"item:(\d+)", link)
    if m:
        return int(m.group(1))
    m = re.search(r"battlepet:(\d+)", link)
    if m:
        return int(m.group(1))
    return None


def generate_event_signature(
    event_type: str,
    item_id: Optional[int],
    ts_unix: int,
    qty: int,
    total_price: int,
    realm: str,
    counterparty: str = ""
) -> str:
    """Deterministic unique event signature for deduplication."""
    iid_str = str(item_id) if item_id is not None else "0"
    return f"{event_type}:{iid_str}:{ts_unix}:{qty}:{total_price}:{realm}:{counterparty}"


def load_item_name_cache(con: sqlite3.Connection) -> Dict[str, int]:
    """Cache itemName -> itemId from the items catalog."""
    cache = {}
    try:
        for item_id, name in con.execute("SELECT itemId, name FROM items"):
            if name and name not in cache:
                cache[name] = item_id
                cache[name.lower()] = item_id
    except sqlite3.OperationalError:
        pass
    return cache


def validate_event(raw: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate that an event dict meets schema requirements."""
    errors = []
    ev_type = raw.get("eventType") or raw.get("event_type")
    if ev_type not in VALID_EVENT_TYPES:
        errors.append(f"Invalid or missing eventType: {ev_type}")

    ts = raw.get("timestamp") or raw.get("timestamp_unix")
    if ts is None:
        errors.append("Missing timestamp")
    else:
        try:
            ts_val = int(ts)
            if ts_val <= 0 or ts_val > 2_500_000_000:
                errors.append(f"Timestamp out of bounds: {ts_val}")
        except (ValueError, TypeError):
            errors.append(f"Invalid timestamp format: {ts}")

    qty = raw.get("quantity") if raw.get("quantity") is not None else raw.get("qty")
    if qty is None:
        qty = 1
    try:
        qty_val = int(qty)
        if qty_val <= 0:
            errors.append(f"Quantity must be positive: {qty_val}")
    except (ValueError, TypeError):
        errors.append(f"Invalid quantity: {qty}")

    total_price = raw.get("totalPriceCopper") if raw.get("totalPriceCopper") is not None else raw.get("total_price_copper")
    if total_price is None:
        total_price = 0
    try:
        price_val = int(total_price)
        if price_val < 0:
            errors.append(f"Total price cannot be negative: {price_val}")
    except (ValueError, TypeError):
        errors.append(f"Invalid total price: {total_price}")

    name = raw.get("itemName") or raw.get("item_name")
    if not name or not str(name).strip():
        errors.append("Missing itemName")

    return (len(errors) == 0, errors)


def normalize_event_dict(
    raw: Dict[str, Any],
    item_cache: Optional[Dict[str, int]] = None,
    default_realm: str = "UnknownRealm",
    default_source: str = "unknown"
) -> Dict[str, Any]:
    """Normalize input event dictionary into canonical schema format."""
    ev_type = str(raw.get("eventType") or raw.get("event_type") or "sale").lower()
    ts_unix = int(raw.get("timestamp") or raw.get("timestamp_unix") or 0)
    if ts_unix > 0:
        dt = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
        ts_iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    item_link = raw.get("itemLink") or raw.get("item_link")
    item_id = raw.get("itemId") or raw.get("item_id")
    if item_id is not None:
        try:
            item_id = int(item_id)
        except (ValueError, TypeError):
            item_id = None

    if item_id is None and item_link:
        item_id = extract_item_id_from_link(item_link)

    item_name = str(raw.get("itemName") or raw.get("item_name") or "").strip()
    if item_id is None and item_cache and item_name:
        item_id = item_cache.get(item_name) or item_cache.get(item_name.lower())

    qty = max(1, int(raw.get("quantity") or raw.get("qty") or 1))
    unit_price = int(raw.get("unitPriceCopper") or raw.get("unit_price_copper") or 0)
    total_price = int(raw.get("totalPriceCopper") or raw.get("total_price_copper") or 0)
    if total_price == 0 and unit_price > 0:
        total_price = unit_price * qty
    elif unit_price == 0 and total_price > 0:
        unit_price = total_price // qty

    deposit = max(0, int(raw.get("depositCopper") or raw.get("deposit_copper") or 0))
    fee = max(0, int(raw.get("consignmentFeeCopper") or raw.get("fee_copper") or 0))

    net_proceeds = raw.get("netProceedsCopper") or raw.get("net_proceeds_copper")
    if net_proceeds is not None:
        net_proceeds = int(net_proceeds)
    else:
        if ev_type == "sale":
            net_proceeds = total_price - fee + deposit
        elif ev_type == "purchase":
            net_proceeds = -total_price
        else:
            net_proceeds = 0

    realm = str(raw.get("realm") or default_realm).strip()
    character = str(raw.get("character") or "").strip() or None
    faction = str(raw.get("faction") or "").strip() or None
    counterparty = str(raw.get("counterparty") or raw.get("playerName") or "").strip()

    event_id = raw.get("eventId") or raw.get("event_id")
    if not event_id:
        event_id = generate_event_signature(
            ev_type, item_id, ts_unix, qty, total_price, realm, counterparty
        )

    source = str(raw.get("source") or default_source)
    provenance = str(raw.get("provenance") or "import")
    confidence = str(raw.get("confidence") or "verified")
    market_ref = raw.get("marketRefCopper") or raw.get("market_ref_copper")
    if market_ref is not None:
        try:
            market_ref = int(market_ref)
        except (ValueError, TypeError):
            market_ref = None

    raw_json = raw.get("raw_json")
    if not raw_json:
        raw_copy = dict(raw)
        raw_copy.pop("raw_json", None)
        try:
            raw_json = json.dumps(raw_copy, default=str)
        except Exception:
            raw_json = "{}"

    return {
        "event_id": event_id,
        "schema_version": SCHEMA_VERSION,
        "event_type": ev_type,
        "ts": ts_iso,
        "timestamp_unix": ts_unix,
        "item_id": item_id,
        "item_name": item_name,
        "item_link": item_link,
        "qty": qty,
        "unit_price_copper": unit_price,
        "total_price_copper": total_price,
        "deposit_copper": deposit,
        "fee_copper": fee,
        "net_proceeds_copper": net_proceeds,
        "realm": realm,
        "character": character,
        "faction": faction,
        "counterparty": counterparty,
        "source": source,
        "provenance": provenance,
        "confidence": confidence,
        "market_ref_copper": market_ref,
        "raw_json": raw_json,
    }


def insert_normalized_event(
    con: sqlite3.Connection,
    event: Dict[str, Any],
    imported_at: Optional[str] = None
) -> Tuple[bool, str]:
    """Insert one normalized event. Returns (True, 'inserted') or (False, 'duplicate')."""
    if not imported_at:
        imported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = con.cursor()
    # Check if primary key exists
    row = cur.execute("SELECT 1 FROM auction_events WHERE event_id=?", (event["event_id"],)).fetchone()
    if row:
        return False, "duplicate"

    # Check semantic duplicate (same event_type, item_id, ts, qty, price, realm, counterparty)
    row2 = cur.execute(
        "SELECT 1 FROM auction_events WHERE event_type=? AND (item_id IS ? OR item_id=?) "
        "AND timestamp_unix=? AND qty=? AND total_price_copper=? AND realm=? AND counterparty=?",
        (event["event_type"], event["item_id"], event["item_id"], event["timestamp_unix"],
         event["qty"], event["total_price_copper"], event["realm"], event["counterparty"])
    ).fetchone()
    if row2:
        return False, "duplicate"

    cur.execute(
        """
        INSERT INTO auction_events (
            event_id, schema_version, event_type, ts, timestamp_unix, item_id,
            item_name, item_link, qty, unit_price_copper, total_price_copper,
            deposit_copper, fee_copper, net_proceeds_copper, realm, character,
            faction, counterparty, source, provenance, confidence, market_ref_copper,
            raw_json, imported_at
        ) VALUES (
            :event_id, :schema_version, :event_type, :ts, :timestamp_unix, :item_id,
            :item_name, :item_link, :qty, :unit_price_copper, :total_price_copper,
            :deposit_copper, :fee_copper, :net_proceeds_copper, :realm, :character,
            :faction, :counterparty, :source, :provenance, :confidence, :market_ref_copper,
            :raw_json, :imported_at
        )
        """,
        {**event, "imported_at": imported_at}
    )
    return True, "inserted"


def ingest_jsonl(
    con: sqlite3.Connection,
    file_path: Path | str,
    source_tag: str = "jsonl_file"
) -> IngestionResult:
    """Ingest events from a JSONL file."""
    init_auction_events_db(con)
    item_cache = load_item_name_cache(con)
    result = IngestionResult()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    path = Path(file_path)
    if not path.exists():
        result.errors.append(f"File not found: {path}")
        return result

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            result.total += 1
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                result.invalid += 1
                result.errors.append(f"Line {line_no}: Invalid JSON: {e}")
                continue

            valid, errs = validate_event(data)
            if not valid:
                result.invalid += 1
                result.errors.append(f"Line {line_no}: Validation error: {'; '.join(errs)}")
                continue

            normalized = normalize_event_dict(data, item_cache=item_cache, default_source=source_tag)
            inserted, reason = insert_normalized_event(con, normalized, imported_at=now)
            if inserted:
                result.inserted += 1
            elif reason == "duplicate":
                result.duplicates += 1
            else:
                result.invalid += 1
                result.errors.append(f"Line {line_no}: Failed insertion ({reason})")

    con.commit()
    return result


def export_events_jsonl(
    con: sqlite3.Connection,
    out_path: Path | str,
    since_unix: Optional[int] = None,
    realm: Optional[str] = None
) -> int:
    """Export stored auction events to a JSONL file."""
    query = "SELECT * FROM auction_events WHERE 1=1"
    params: List[Any] = []
    if since_unix is not None:
        query += " AND timestamp_unix >= ?"
        params.append(since_unix)
    if realm:
        query += " AND realm = ?"
        params.append(realm)
    query += " ORDER BY timestamp_unix ASC"

    con.row_factory = sqlite3.Row
    cur = con.execute(query, params)
    rows = cur.fetchall()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            d = dict(r)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    return len(rows)


def ingest_journalator_archive(
    con: sqlite3.Connection,
    lua_path: Optional[Path | str] = None
) -> IngestionResult:
    """Ingest historical archives directly from Journalator.lua SavedVariables using Lupa."""
    init_auction_events_db(con)
    item_cache = load_item_name_cache(con)
    result = IngestionResult()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if lua_path is None:
        candidates = [
            Path(r"C:\Program Files (x86)\World of Warcraft\_retail_\WTF\Account\1186541918#1\SavedVariables\Journalator.lua"),
        ]
        for c in candidates:
            if c.exists():
                lua_path = c
                break

    if not lua_path or not Path(lua_path).exists():
        result.errors.append(f"Journalator.lua not found at {lua_path}")
        return result

    try:
        import lupa
        from lupa import LuaRuntime
    except ImportError:
        result.errors.append("Lupa is not installed; cannot dearchive Journalator archives directly.")
        return result

    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
    unpack = unpack or table.unpack
    LibStub = {}
    local libs = {}
    local minors = {}
    function LibStub:NewLibrary(major, minor)
        libs[major] = libs[major] or {}
        minors[major] = minor
        return libs[major], minor
    end
    function LibStub:GetLibrary(major, silent)
        if not libs[major] and not silent then
            error("Library not found: " .. major)
        end
        return libs[major], minors[major]
    end
    setmetatable(LibStub, { __call = function(self, major, silent) return self:GetLibrary(major, silent) end })
    """)

    # Load embedded libraries from Journalator
    root = Path(__file__).resolve().parent.parent.parent
    deflate_lua = root / "Journalator" / "Embeds" / "Archivist" / "libs" / "LibDeflate" / "LibDeflate.lua"
    serialize_lua = root / "Journalator" / "Embeds" / "Archivist" / "libs" / "LibSerialize" / "LibSerialize.lua"

    if not deflate_lua.exists() or not serialize_lua.exists():
        result.errors.append("Journalator LibDeflate/LibSerialize not found in repository.")
        return result

    lua.execute(deflate_lua.read_text(encoding="utf-8"))
    lua.execute(serialize_lua.read_text(encoding="utf-8"))

    lua.execute("""
    local LibDeflate = LibStub("LibDeflate")
    local LibSerialize = LibStub("LibSerialize")

    function DeArchive(encoded)
        local compressed = LibDeflate:DecodeForPrint(encoded)
        if not compressed then return nil, "DecodeForPrint failed" end
        local serialized = LibDeflate:DecompressDeflate(compressed)
        if not serialized then return nil, "DecompressDeflate failed" end
        local success, data = LibSerialize:Deserialize(serialized)
        if not success then return nil, "Deserialize failed: " .. tostring(data) end
        return data
    end
    """)

    # Load Journalator.lua
    try:
        lua.execute(f'dofile([[{str(Path(lua_path))} ]])')
    except Exception as e:
        result.errors.append(f"Failed to load {lua_path}: {e}")
        return result

    # Extract all stores
    lua.execute("""
    function GetAllArchiveData()
        local all = { invoices = {}, posting = {}, failures = {} }
        if not JOURNALATOR_ARCHIVE or not JOURNALATOR_ARCHIVE.stores then
            return all
        end
        local stores = JOURNALATOR_ARCHIVE.stores["SometimesLocked"] or {}
        for store_id, store_obj in pairs(stores) do
            local data = DeArchive(store_obj.data)
            if data then
                if data.Invoices then
                    for _, inv in ipairs(data.Invoices) do
                        table.insert(all.invoices, inv)
                    end
                end
                if data.Posting then
                    for _, post in ipairs(data.Posting) do
                        table.insert(all.posting, post)
                    end
                end
                if data.Failures then
                    for _, fail in ipairs(data.Failures) do
                        table.insert(all.failures, fail)
                    end
                end
            end
        end
        return all
    end
    """)

    all_data = lua.globals().GetAllArchiveData()
    if not all_data:
        return result

    # Process Invoices
    if hasattr(all_data, "invoices"):
        for inv in all_data.invoices.values():
            result.total += 1
            src = inv.source or {}
            ev_dict = {
                "eventType": "purchase" if inv.invoiceType == "buyer" else "sale",
                "timestamp": inv.time,
                "itemName": inv.itemName,
                "itemLink": inv.itemLink,
                "quantity": inv.count or 1,
                "totalPriceCopper": inv.value or 0,
                "depositCopper": inv.deposit or 0,
                "consignmentFeeCopper": inv.consignment or 0,
                "realm": src.realm or "UnknownRealm",
                "character": src.character,
                "faction": src.faction,
                "counterparty": inv.playerName or "",
                "source": "journalator_archive",
                "provenance": "Journalator.lua:Invoices",
                "confidence": "verified",
            }
            valid, errs = validate_event(ev_dict)
            if not valid:
                result.invalid += 1
                result.errors.append(f"Invoice error: {'; '.join(errs)}")
                continue
            normalized = normalize_event_dict(ev_dict, item_cache=item_cache)
            inserted, reason = insert_normalized_event(con, normalized, imported_at=now)
            if inserted:
                result.inserted += 1
            elif reason == "duplicate":
                result.duplicates += 1

    # Process Postings
    if hasattr(all_data, "posting"):
        for post in all_data.posting.values():
            result.total += 1
            src = post.source or {}
            buyout = int(post.buyout or 0)
            qty = max(1, int(post.count or 1))
            ev_dict = {
                "eventType": "listing",
                "timestamp": post.time,
                "itemName": post.itemName,
                "itemLink": post.itemLink,
                "quantity": qty,
                "unitPriceCopper": buyout,
                "totalPriceCopper": buyout * qty,
                "depositCopper": post.deposit or 0,
                "consignmentFeeCopper": 0,
                "realm": src.realm or "UnknownRealm",
                "character": src.character,
                "faction": src.faction,
                "source": "journalator_archive",
                "provenance": "Journalator.lua:Posting",
                "confidence": "verified",
            }
            valid, errs = validate_event(ev_dict)
            if not valid:
                result.invalid += 1
                result.errors.append(f"Posting error: {'; '.join(errs)}")
                continue
            normalized = normalize_event_dict(ev_dict, item_cache=item_cache)
            inserted, reason = insert_normalized_event(con, normalized, imported_at=now)
            if inserted:
                result.inserted += 1
            elif reason == "duplicate":
                result.duplicates += 1

    # Process Failures
    if hasattr(all_data, "failures"):
        for fail in all_data.failures.values():
            result.total += 1
            src = fail.source or {}
            failed_type = fail.failedType or "expired"
            ev_dict = {
                "eventType": "cancel" if failed_type == "cancelled" else "expire",
                "timestamp": fail.time,
                "itemName": fail.itemName,
                "itemLink": fail.itemLink,
                "quantity": fail.count or 1,
                "totalPriceCopper": 0,
                "depositCopper": 0,
                "consignmentFeeCopper": 0,
                "realm": src.realm or "UnknownRealm",
                "character": src.character,
                "faction": src.faction,
                "source": "journalator_archive",
                "provenance": "Journalator.lua:Failures",
                "confidence": "verified",
            }
            valid, errs = validate_event(ev_dict)
            if not valid:
                result.invalid += 1
                result.errors.append(f"Failure error: {'; '.join(errs)}")
                continue
            normalized = normalize_event_dict(ev_dict, item_cache=item_cache)
            inserted, reason = insert_normalized_event(con, normalized, imported_at=now)
            if inserted:
                result.inserted += 1
            elif reason == "duplicate":
                result.duplicates += 1

    con.commit()
    return result
