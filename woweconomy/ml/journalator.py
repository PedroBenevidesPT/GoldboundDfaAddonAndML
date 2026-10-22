"""Raw Journalator extraction.

The Journalator saved-variable file is the only place in the installation
that records *what the account actually did*: what it posted, what it
bought, what it sold and what failed.  The aggregate tables
(``journal_stats``) keep only counts per item and throw away the
timestamps, the prices and the quantities -- exactly the information a
supervised model needs.

The archive is compressed with ``LibDeflate`` and serialised with
``LibSerialize``; the only way to read it is to run the addon's own
libraries, which is what :func:`extract_archive` does through Lupa.

Field semantics (verified against ``Journalator/Source/Monitor``):

``Posting``
    written by a hook on ``C_AuctionHouse.PostItem``.  ``buyout`` is the
    **per-unit** buyout price, ``bid`` the per-unit opening bid, ``count``
    the stack size, ``time`` the moment the item was posted.

``Invoices``
    written when the player opens a mail that carries an invoice.
    ``invoiceType`` is ``"seller"`` (we sold) or ``"buyer"`` (we bought).
    ``value`` is the gross total, ``consignment`` the house cut,
    ``deposit`` the refunded deposit.  The mail has **no** ``itemLink``
    in this installation, so the item has to be resolved by name.

``Failures``
    written when the player opens the "your auction expired" or "your
    auction was cancelled" mail.  ``failedType`` is ``"expired"`` or
    ``"cancelled"``.

    *An expiry is evidence the ask was not met.  A cancellation is the
    seller changing their mind and says nothing about demand*, so the two
    are kept apart everywhere downstream.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Posting:
    time: int
    realm: str
    character: str
    faction: str
    item_name: str
    item_id: Optional[int]
    qty: int
    unit_buyout: int
    unit_bid: int
    deposit: int


@dataclass
class Invoice:
    time: int
    realm: str
    character: str
    faction: str
    item_name: str
    item_id: Optional[int]
    qty: int
    invoice_type: str
    value: int
    consignment: int
    deposit: int
    counterparty: str


@dataclass
class Failure:
    time: int
    realm: str
    character: str
    faction: str
    item_name: str
    item_id: Optional[int]
    qty: int
    failed_type: str


@dataclass
class JournalArchive:
    postings: List[Posting] = field(default_factory=list)
    invoices: List[Invoice] = field(default_factory=list)
    failures: List[Failure] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def first_ts(self) -> int:
        stamps = [p.time for p in self.postings] + [i.time for i in self.invoices] \
            + [f.time for f in self.failures]
        return min(stamps) if stamps else 0

    @property
    def last_ts(self) -> int:
        stamps = [p.time for p in self.postings] + [i.time for i in self.invoices] \
            + [f.time for f in self.failures]
        return max(stamps) if stamps else 0

    def summary(self) -> Dict[str, int]:
        return {
            "postings": len(self.postings),
            "invoices_seller": sum(1 for i in self.invoices if i.invoice_type == "seller"),
            "invoices_buyer": sum(1 for i in self.invoices if i.invoice_type == "buyer"),
            "failures_expired": sum(1 for f in self.failures if f.failed_type == "expired"),
            "failures_cancelled": sum(1 for f in self.failures if f.failed_type == "cancelled"),
        }


_LUA_BOOTSTRAP = """
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
  if not libs[major] and not silent then error("Library not found: " .. major) end
  return libs[major], minors[major]
end
setmetatable(LibStub, { __call = function(self, major, silent)
  return self:GetLibrary(major, silent)
end })
"""

_LUA_DECOMPRESS = """
local LibDeflate = LibStub("LibDeflate")
local LibSerialize = LibStub("LibSerialize")
function DeArchive(encoded)
  if type(encoded) ~= "string" then return nil end
  local ok, compressed = pcall(function() return LibDeflate:DecodeForPrint(encoded) end)
  if not ok or not compressed then return nil end
  local ok2, serialized = pcall(function() return LibDeflate:DecompressDeflate(compressed) end)
  if not ok2 or not serialized then return nil end
  local success, data = LibSerialize:Deserialize(serialized)
  if not success then return nil end
  return data
end
function Collect()
  local out = {Posting = {}, Failures = {}, Invoices = {}}
  local stores = (JOURNALATOR_ARCHIVE and JOURNALATOR_ARCHIVE.stores) or {}
  local sl = stores["SometimesLocked"] or {}
  for store_id, store_obj in pairs(sl) do
    local d = DeArchive(store_obj and store_obj.data)
    if d then
      if d.Posting then
        for _, r in ipairs(d.Posting) do out.Posting[#out.Posting + 1] = r end
      end
      if d.Failures then
        for _, r in ipairs(d.Failures) do out.Failures[#out.Failures + 1] = r end
      end
      if d.Invoices then
        for _, r in ipairs(d.Invoices) do out.Invoices[#out.Invoices + 1] = r end
      end
    end
  end
  return out
end
"""


def _lua_to_py(value):
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    try:
        keys = list(value.keys())
    except Exception:
        return None
    if not keys:
        return None
    return {k: _lua_to_py(value[k]) for k in keys}


def extract_archive(lua_path: Path | str,
                    addons_dir: Path | str) -> Tuple[List[dict], List[dict], List[dict], List[str]]:
    """Return ``(postings, invoices, failures, errors)`` as plain dicts."""
    errors: List[str] = []
    lua_path = Path(lua_path)
    addons = Path(addons_dir)
    if not lua_path.exists():
        return [], [], [], [f"Journalator.lua not found at {lua_path}"]

    try:
        from lupa import LuaRuntime
    except ImportError:
        return [], [], [], ["Lupa is not installed; cannot read the Journalator archive."]

    deflate = addons / "Journalator/Embeds/Archivist/libs/LibDeflate/LibDeflate.lua"
    serialize = addons / "Journalator/Embeds/Archivist/libs/LibSerialize/LibSerialize.lua"
    if not deflate.exists() or not serialize.exists():
        return [], [], [], [f"Journalator libraries not found under {addons}"]

    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(_LUA_BOOTSTRAP)
    try:
        lua.execute(deflate.read_text(encoding="utf-8"))
        lua.execute(serialize.read_text(encoding="utf-8"))
        lua.execute(f"dofile([[{lua_path}]])")
        lua.execute(_LUA_DECOMPRESS)
    except Exception as exc:  # pragma: no cover - depends on the addon build
        return [], [], [], [f"Failed to prepare the Journalator archive: {exc}"]

    data = lua.globals().Collect()
    out = []
    for key in ("Posting", "Invoices", "Failures"):
        rows = []
        table = getattr(data, key)
        for i in range(1, len(table) + 1):
            row = _lua_to_py(table[i])
            if row:
                rows.append(row)
        out.append(rows)
    return out[0], out[1], out[2], errors


def _source_of(row: dict) -> Tuple[str, str, str]:
    src = row.get("source") or {}
    return (str(src.get("realm") or ""), str(src.get("character") or ""),
            str(src.get("faction") or ""))


def _item_id_from_link(link: str) -> Optional[int]:
    if not link:
        return None
    for marker in ("item:", "Hitem:"):
        idx = link.find(marker)
        if idx >= 0:
            tail = link[idx + len(marker):]
            digits = ""
            for ch in tail:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                return int(digits)
    return None


def build_archive(lua_path: Path | str,
                   addons_dir: Path | str,
                   name_to_id: Optional[Dict[str, int]] = None) -> JournalArchive:
    """Read the archive and normalise it into typed records.

    ``name_to_id`` resolves the invoices, which carry no item link.
    """
    postings, invoices, failures, errors = extract_archive(lua_path, addons_dir)
    archive = JournalArchive(errors=errors)
    names = name_to_id or {}

    for row in postings:
        realm, character, faction = _source_of(row)
        link = str(row.get("itemLink") or "")
        name = str(row.get("itemName") or "")
        archive.postings.append(Posting(
            time=int(row.get("time") or 0), realm=realm, character=character,
            faction=faction, item_name=name,
            item_id=_item_id_from_link(link) or names.get(name),
            qty=int(row.get("count") or 1),
            unit_buyout=int(row.get("buyout") or 0),
            unit_bid=int(row.get("bid") or 0),
            deposit=int(row.get("deposit") or 0)))

    for row in invoices:
        realm, character, faction = _source_of(row)
        name = str(row.get("itemName") or "")
        archive.invoices.append(Invoice(
            time=int(row.get("time") or 0), realm=realm, character=character,
            faction=faction, item_name=name,
            item_id=_item_id_from_link(str(row.get("itemLink") or "")) or names.get(name),
            qty=int(row.get("count") or 1),
            invoice_type=str(row.get("invoiceType") or ""),
            value=int(row.get("value") or 0),
            consignment=int(row.get("consignment") or 0),
            deposit=int(row.get("deposit") or 0),
            counterparty=str(row.get("playerName") or "")))

    for row in failures:
        realm, character, faction = _source_of(row)
        link = str(row.get("itemLink") or "")
        name = str(row.get("itemName") or "")
        archive.failures.append(Failure(
            time=int(row.get("time") or 0), realm=realm, character=character,
            faction=faction, item_name=name,
            item_id=_item_id_from_link(link) or names.get(name),
            qty=int(row.get("count") or 1),
            failed_type=str(row.get("failedType") or "")))
    return archive
