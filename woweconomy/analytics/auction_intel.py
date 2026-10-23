"""Ingest and measure the DFA Auction Intelligence event log.

The addon (``DFA_AuctionIntelligence``) captures, per auction house event, the
things the DFA database was missing:

======================  ==========================================  ==========
field                  what it is                                 used for
======================  ==========================================  ==========
``timestamp``          unix seconds of the event                  time to sale
``eventType``          sale / purchase / listing / cancel /expire the funnel
``unitPriceCopper``    what the buyer paid per unit               realised price
``depositCopper``      the deposit actually charged              cost, measured
``consignmentFeeCopper`` the AH cut actually charged              cost, measured
``netProceedsCopper``  what actually landed in the bag            net gold
``marketRefCopper``    TSM's ``dbmarket`` at the moment of event  the reference
                        (the ask average of the realm)            *at that time*
======================  ==========================================  ==========

That last pair is what turns the "E2" estimator from impossible into
measurable: the realised price divided by the market reference **as it stood
when the sale happened**.

This module is deliberately dull: a parser for the addon's SavedVariables, an
idempotent importer into ``tsm_markets.db`` and a handful of pure functions
that turn the event log into measurements.  Nothing here posts, cancels or
reprices anything.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

COPPER_PER_GOLD = 10_000

EVENT_TYPES = ("sale", "purchase", "listing", "cancel", "expire")

#: The addon's event log gets its **own** table.  ``auction_events`` already
#: belongs to :mod:`woweconomy.services.auction_intelligence` -- the
#: Journalator/TSM import -- with a different schema (``ts`` /
#: ``timestamp_unix`` / ``qty`` / ``fee_copper`` / ``item_link``).  Sharing the
#: name made every addon import die with "table auction_events has no column
#: named quantity", which is why the addon's log never reached the database.
TABLE = "auction_intel_events"

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS auction_intel_events (
    event_id               TEXT PRIMARY KEY,
    event_type             TEXT NOT NULL,
    ts                     INTEGER NOT NULL,
    item_id                INTEGER,
    item_name              TEXT NOT NULL DEFAULT '',
    quantity               INTEGER NOT NULL DEFAULT 0,
    unit_price_copper      INTEGER NOT NULL DEFAULT 0,
    total_price_copper     INTEGER NOT NULL DEFAULT 0,
    deposit_copper         INTEGER NOT NULL DEFAULT 0,
    consignment_fee_copper INTEGER NOT NULL DEFAULT 0,
    net_proceeds_copper    INTEGER NOT NULL DEFAULT 0,
    market_ref_copper      INTEGER NOT NULL DEFAULT 0,
    realm                  TEXT NOT NULL DEFAULT '',
    character              TEXT NOT NULL DEFAULT '',
    counterparty           TEXT NOT NULL DEFAULT '',
    source                 TEXT NOT NULL DEFAULT '',
    provenance             TEXT NOT NULL DEFAULT '',
    confidence             TEXT NOT NULL DEFAULT '',
    schema_version         INTEGER NOT NULL DEFAULT 0,
    captured_at            INTEGER NOT NULL DEFAULT 0,
    imported_at            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auction_intel_item ON auction_intel_events(item_id, realm, ts);
CREATE INDEX IF NOT EXISTS idx_auction_intel_type ON auction_intel_events(event_type, ts);
"""

#: the SavedVariables variable the addon writes
EXPORT_NAME = "DFA_AUCTION_INTELLIGENCE_EXPORT"
STORE_NAME = "DFA_AUCTION_INTELLIGENCE_DB"

#: The file on disk is named after the **addon folder**, not after the
#: variable: WoW writes ``<AddOnFolder>.lua``.  Looking only for
#: ``DFA_AUCTION_INTELLIGENCE_EXPORT.lua`` never found anything, so the whole
#: sync path reported "no export" while a 74 KB file sat in SavedVariables.
ADDON_NAME = "DFA_AuctionIntelligence"

SAVED_VARIABLE_NAMES = (ADDON_NAME, EXPORT_NAME, STORE_NAME)

WTF_GLOBS = (
    r"C:\Program Files (x86)\World of Warcraft\_retail_\WTF\Account\*\SavedVariables\{name}.lua",
    r"C:\Program Files (x86)\World of Warcraft\_retail_\WTF\Account\*\SavedVariables\{name}.lua.bak",
)


def _wtf_globs() -> Tuple[str, ...]:
    """The globs to search, including a mounted WTF directory.

    The pipeline runs in a Linux container that cannot see ``Program Files``;
    compose mounts the game's ``WTF`` folder and names it in ``WOW_WTF_DIR``,
    so the same search finds the SavedVariables there.  The Windows paths stay
    in the list for a run from the source tree.
    """
    base = os.environ.get("WOW_WTF_DIR", "").strip()
    if not base:
        return WTF_GLOBS
    root = Path(base) / "Account" / "*" / "SavedVariables"
    return (str(root / "{name}.lua"), str(root / "{name}.lua.bak"),
            *WTF_GLOBS)


def find_saved_variables(name: Optional[str] = None) -> Optional[Path]:
    """The most recent SavedVariables file of the addon.

    ``name`` is accepted for compatibility; with no name every spelling the
    addon is saved under is searched (the folder name first, because that is
    the one the client actually writes).
    """
    import glob
    names = (name,) if name else SAVED_VARIABLE_NAMES
    hits: List[Path] = []
    for candidate in names:
        for pattern in _wtf_globs():
            hits.extend(Path(p) for p in glob.glob(pattern.format(name=candidate)))
    if not hits:
        return None
    # an export is only an export if it holds events; prefer such a file even
    # when a newer .lua.bak exists
    with_events = [p for p in hits if parse_saved_variables(p)]
    pool = with_events or hits
    return max(pool, key=lambda p: p.stat().st_mtime)


# --- parsing ---------------------------------------------------------------


def _unescape_lua(text: str) -> str:
    """Turn a Lua string literal's escapes into the real characters."""
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
            if nxt in mapping:
                out.append(mapping[nxt])
                index += 2
                continue
            if nxt == "\n":       # a line continuation
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def extract_lua_string(text: str, key: str) -> Optional[str]:
    """The value of ``["key"] = "..."`` in a Lua file, unescaped."""
    match = re.search(r'\["%s"\]\s*=\s*"' % re.escape(key), text)
    if not match:
        return None
    start = match.end()
    out = []
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            out.append(text[index:index + 2])
            index += 2
            continue
        if char == '"':
            break
        out.append(char)
        index += 1
    return _unescape_lua("".join(out))


def parse_events_jsonl(jsonl: str) -> List[dict]:
    """The events of the addon's JSONL export."""
    events = []
    for line in jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def parse_saved_variables(path: Path) -> List[dict]:
    """Read the addon's export (the JSONL is the stable path)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    jsonl = extract_lua_string(text, "jsonl")
    if jsonl:
        events = parse_events_jsonl(jsonl)
        if events:
            return events
    return parse_lua_event_table(text)


def parse_lua_event_table(text: str) -> List[dict]:
    """Fallback: the ``events`` table of the SavedVariables (a Lua literal)."""
    start = text.find('["events"]')
    if start < 0:
        return []
    events: List[dict] = []
    # each event is a {...} block; parse the key/value pairs of each
    for block in re.finditer(r"\{\s*(\[\"eventType\"\].*?)\n\t\}", text[start:], re.S):
        chunk = block.group(1)
        payload: dict = {}
        for key, value in re.finditer(r'\["(\w+)"\]\s*=\s*("(?:[^"\\]|\\.)*"|-?[\d.]+|true|false|nil)', chunk):
            name, raw = key.group(1), value.group(2)
            if raw.startswith('"'):
                payload[name] = _unescape_lua(raw[1:-1])
            elif raw in ("true", "false"):
                payload[name] = raw == "true"
            elif raw == "nil":
                payload[name] = None
            else:
                try:
                    payload[name] = float(raw) if "." in raw else int(raw)
                except ValueError:
                    payload[name] = 0
        if payload.get("eventType"):
            events.append(payload)
    return events


# --- the database ----------------------------------------------------------


def ensure_table(con: sqlite3.Connection) -> None:
    con.executescript(TABLE_DDL)


def event_row(event: dict, imported_at: str) -> tuple:
    def number(*names, default=0):
        for name in names:
            value = event.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default
        return default

    def text(*names, default=""):
        for name in names:
            value = event.get(name)
            if value:
                return str(value)
        return default

    event_type = text("eventType", "event_type", default="unknown")
    ts = number("timestamp", "ts")
    item_id = number("itemId", "item_id")
    event_id = text("eventId", "event_id") or f"{event_type}:{item_id}:{ts}"
    return (
        event_id, event_type, ts, item_id or None,
        text("itemName", "item_name"),
        number("quantity"),
        number("unitPriceCopper", "unit_price_copper"),
        number("totalPriceCopper", "total_price_copper"),
        number("depositCopper", "deposit_copper"),
        number("consignmentFeeCopper", "consignment_fee_copper"),
        number("netProceedsCopper", "net_proceeds_copper"),
        number("marketRefCopper", "market_ref_copper"),
        text("realm"), text("character"), text("counterparty"),
        text("source"), text("provenance"), text("confidence"),
        number("schemaVersion", "schema_version"),
        number("capturedAt", "captured_at"),
        imported_at,
    )


INSERT_SQL = """
INSERT OR IGNORE INTO auction_intel_events (
    event_id, event_type, ts, item_id, item_name, quantity, unit_price_copper,
    total_price_copper, deposit_copper, consignment_fee_copper,
    net_proceeds_copper, market_ref_copper, realm, character, counterparty,
    source, provenance, confidence, schema_version, captured_at, imported_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def build_name_index(db_path: Path | str) -> Dict[str, int]:
    """item name -> item id, straight from the catalogue tables.

    Kept here, and not borrowed from the ML pipeline, because the importer has
    no CatBoost or pandas to load just to read three SELECTs.
    """
    from woweconomy.dbcompat import connect_store, missing_table_errors

    index: Dict[str, int] = {}
    con = connect_store(db_path, readonly=True)
    try:
        queries = ("SELECT itemId, name FROM items",
                   "SELECT item_id, name FROM tsm_realm_price",
                   "SELECT key_id, name FROM tsm_region_price")
        for query in queries:
            try:
                for item_id, name in con.execute(query):
                    if name and item_id:
                        index.setdefault(str(name), int(item_id))
            except missing_table_errors():
                continue
    finally:
        con.close()
    return index


def fill_item_ids(events: Iterable[dict], name_to_id: Dict[str, int]) -> int:
    """Add the item id to events that only carry an item name.

    Journalator's invoices are stored without an item link -- the pipeline
    says so in :mod:`woweconomy.ml.journalator` -- so the addon exports the
    name and no id.  Without an id the row cannot be joined to any price, so
    the name is resolved against the item catalogue here.  Returns how many
    rows were completed.
    """
    filled = 0
    for event in events:
        if event.get("itemId"):
            continue
        name = event.get("itemName") or event.get("item_name") or ""
        item_id = name_to_id.get(str(name))
        if item_id:
            event["itemId"] = int(item_id)
            filled += 1
    return filled


def import_events(con: sqlite3.Connection, events: Sequence[dict],
                  imported_at: str = "") -> Tuple[int, int]:
    """Insert the events; returns ``(inserted, already_present)``."""
    ensure_table(con)
    inserted = 0
    present = 0
    for event in events:
        row = event_row(event, imported_at)
        cursor = con.execute(INSERT_SQL, row)
        if cursor.rowcount:
            inserted += 1
        else:
            present += 1
    con.commit()
    return inserted, present


# --- the measurements ------------------------------------------------------


@dataclass
class Coverage:
    """What the event log actually contains."""

    total: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    with_item_id: int = 0
    with_price: int = 0
    with_market_ref: int = 0
    with_deposit: int = 0
    with_fee: int = 0
    with_net: int = 0
    realms: Dict[str, int] = field(default_factory=dict)

    @property
    def span_days(self) -> float:
        if not self.first_ts or not self.last_ts:
            return 0.0
        return (self.last_ts - self.first_ts) / 86400.0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "by_type": dict(self.by_type),
            "span_days": round(self.span_days, 2),
            "with_item_id": self.with_item_id,
            "with_price": self.with_price,
            "with_market_ref": self.with_market_ref,
            "with_deposit": self.with_deposit,
            "with_fee": self.with_fee,
            "with_net": self.with_net,
            "realms": dict(self.realms),
        }


def coverage(events: Sequence[dict]) -> Coverage:
    """The coverage of the event log, field by field."""
    result = Coverage()
    for event in events:
        result.total += 1
        event_type = str(event.get("eventType", "unknown"))
        result.by_type[event_type] = result.by_type.get(event_type, 0) + 1
        ts = int(event.get("timestamp") or 0)
        if ts:
            result.first_ts = ts if result.first_ts is None else min(result.first_ts, ts)
            result.last_ts = ts if result.last_ts is None else max(result.last_ts, ts)
        if event.get("itemId"):
            result.with_item_id += 1
        if event.get("unitPriceCopper") or event.get("totalPriceCopper"):
            result.with_price += 1
        if event.get("marketRefCopper"):
            result.with_market_ref += 1
        if event.get("depositCopper"):
            result.with_deposit += 1
        if event.get("consignmentFeeCopper"):
            result.with_fee += 1
        if event.get("netProceedsCopper"):
            result.with_net += 1
        realm = str(event.get("realm") or "")
        result.realms[realm] = result.realms.get(realm, 0) + 1
    return result


@dataclass
class ClearanceSample:
    """One realised sale, with the market reference standing at that moment."""

    item_id: Optional[int]
    realm: str
    ts: int
    realised: int
    reference: int
    band: str = ""

    @property
    def ratio(self) -> float:
        return self.realised / self.reference if self.reference else 0.0


def clearance_samples(events: Sequence[dict],
                      band_of=None) -> List[ClearanceSample]:
    """The realised price against the market reference **at the time of the sale**.

    This is the "E2" measurement: unlike the region-wide average sale price,
    it is contemporaneous, it is per realm, and it is a ratio of two things
    measured at the same moment.
    """
    out = []
    for event in events:
        if str(event.get("eventType")) != "sale":
            continue
        reference = int(event.get("marketRefCopper") or 0)
        realised = int(event.get("unitPriceCopper") or 0)
        if not reference or not realised:
            continue
        sample = ClearanceSample(
            item_id=int(event.get("itemId") or 0) or None,
            realm=str(event.get("realm") or ""),
            ts=int(event.get("timestamp") or 0),
            realised=realised,
            reference=reference,
        )
        if band_of is not None and sample.item_id:
            sample.band = band_of(sample.item_id)
        out.append(sample)
    return out


def clearance_by_band(samples: Sequence[ClearanceSample]) -> Dict[str, dict]:
    """The clearance distribution per band: the measured sale/ask ratio."""
    grouped: Dict[str, List[float]] = {}
    for sample in samples:
        if sample.band:
            grouped.setdefault(sample.band, []).append(sample.ratio)
    out = {}
    for band, ratios in grouped.items():
        ratios = sorted(ratios)
        n = len(ratios)
        out[band] = {
            "n": n,
            "p25": ratios[n // 4],
            "p50": ratios[n // 2],
            "p75": ratios[3 * n // 4],
            "mean": statistics.mean(ratios),
        }
    return out


def time_to_sale(events: Sequence[dict]) -> List[dict]:
    """The hours between a listing and the sale that closed it.

    A listing is matched to the first sale of the same item and realm that
    happens after it.  Only the *first* pending listing is used, so a relist
    does not create a second, shorter interval.
    """
    listings: Dict[Tuple, dict] = {}
    sales: List[dict] = []
    for event in events:
        key = (event.get("itemId"), event.get("realm"))
        event_type = str(event.get("eventType"))
        if event_type == "listing":
            if key not in listings:
                listings[key] = event
        elif event_type == "sale":
            sales.append(event)
    out = []
    for sale in sales:
        key = (sale.get("itemId"), sale.get("realm"))
        listing = listings.get(key)
        if not listing:
            continue
        posted = int(listing.get("timestamp") or 0)
        sold = int(sale.get("timestamp") or 0)
        if not posted or not sold or sold < posted:
            continue
        out.append({
            "item_id": sale.get("itemId"),
            "realm": sale.get("realm"),
            "hours": (sold - posted) / 3600.0,
            "asked_copper": int(listing.get("unitPriceCopper") or 0),
            "realised_copper": int(sale.get("unitPriceCopper") or 0),
        })
    return out


def funnel(events: Sequence[dict]) -> Dict[str, dict]:
    """Listings, sales and expiries per item and realm: the sell-through."""
    listings: Dict[Tuple, int] = defaultdict(int)
    sales: Dict[Tuple, int] = defaultdict(int)
    expires: Dict[Tuple, int] = defaultdict(int)
    cancels: Dict[Tuple, int] = defaultdict(int)
    for event in events:
        key = (event.get("itemId"), event.get("realm"))
        event_type = str(event.get("eventType"))
        if event_type == "listing":
            listings[key] += 1
        elif event_type == "sale":
            sales[key] += 1
        elif event_type == "expire":
            expires[key] += 1
        elif event_type == "cancel":
            cancels[key] += 1
    out = {}
    for key in set(listings) | set(sales) | set(expires):
        posted = listings.get(key, 0)
        sold = sales.get(key, 0)
        out[key] = {
            "listings": posted,
            "sales": sold,
            "expires": expires.get(key, 0),
            "cancels": cancels.get(key, 0),
            "sell_through": (sold / (sold + expires.get(key, 0))) if (sold + expires.get(key, 0)) else None,
        }
    return out


def measured_costs(events: Sequence[dict]) -> Dict[str, object]:
    """The AH cut and the deposit, **measured** instead of assumed."""
    fees: List[float] = []
    deposits: List[float] = []
    for event in events:
        total = int(event.get("totalPriceCopper") or 0)
        fee = int(event.get("consignmentFeeCopper") or 0)
        deposit = int(event.get("depositCopper") or 0)
        if event_type_is_sale(event) and total and fee:
            fees.append(fee / total)
        if event_type_is_listing(event) and total and deposit:
            deposits.append(deposit / total)
    return {
        "fee_samples": len(fees),
        "fee_median": statistics.median(fees) if fees else None,
        "deposit_samples": len(deposits),
        "deposit_median": statistics.median(deposits) if deposits else None,
    }


def event_type_is_sale(event: dict) -> bool:
    return str(event.get("eventType")) == "sale"


def event_type_is_listing(event: dict) -> bool:
    return str(event.get("eventType")) == "listing"
