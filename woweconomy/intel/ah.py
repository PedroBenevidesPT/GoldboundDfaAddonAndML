"""Blizzard Auction House snapshots, with the raw time-left preserved.

The public Game Data API returns active auctions for a connected realm.
The project's existing client (:class:`woweconomy.services.blizzard_items.BlizzardItemsClient`)
already speaks OAuth and the static namespace; this module reuses it and
only changes the namespace to ``dynamic-<region>``.

What is stored, and what is deliberately **not** stored:

* every auction keeps the exact ``time_left`` string the API sent
  (``SHORT`` / ``MEDIUM`` / ``LONG`` / ``VERY_LONG`` today).  An unknown
  value is kept with ``time_left_band = NULL`` instead of being forced
  into a bucket;
* ``unit_price`` is kept when the API sent one.  For a non-commodity
  auction the API sends a total ``buyout``; the per-unit price is then
  derived and ``unit_price_source`` says so.  A missing value stays
  ``NULL`` -- never zero, which would read as "free";
* the seller is **not** in the API payload, so there is no seller column
  here.  Seller identity comes from the client UI and lives in
  :mod:`woweconomy.intel.sellers`.

A listing that is in snapshot A and not in snapshot B is a *departure*.
It may have sold, expired, been cancelled, or moved to another connected
realm.  This module never calls it a sale; :mod:`woweconomy.intel.dynamics`
keeps the distinction.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import store

logger = logging.getLogger(__name__)

#: The categories the API has used.  Anything else is preserved raw and
#: flagged as unknown rather than guessed.
KNOWN_TIME_LEFT: Tuple[str, ...] = ("SHORT", "MEDIUM", "LONG", "VERY_LONG")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def time_left_band(raw: Optional[str]) -> Optional[str]:
    """The category, if the API's string is one we know.

    The value is *not* collapsed into short/medium/long: ``VERY_LONG``
    stays ``VERY_LONG`` so a later analysis can learn its own cut points.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().upper()
    return value if value in KNOWN_TIME_LEFT else None


@dataclass(frozen=True)
class AuctionRecord:
    """One auction, normalised but not interpreted."""

    auction_key: str
    auction_id: Optional[int]
    item_id: int
    quantity: int
    unit_price_copper: Optional[int]
    buyout_copper: Optional[int]
    bid_copper: Optional[int]
    total_price_copper: Optional[int]
    unit_price_source: str
    time_left_raw: Optional[str]
    time_left_band: Optional[str]
    is_commodity: bool
    item_context: Optional[int] = None
    pet_species_id: Optional[int] = None
    bonus_lists: Optional[str] = None
    modifiers: Optional[str] = None

    def as_row(self) -> Tuple:
        return (
            self.auction_key, self.auction_id, self.item_id, self.item_context,
            self.quantity, self.unit_price_copper, self.buyout_copper,
            self.bid_copper, self.total_price_copper, self.unit_price_source,
            self.time_left_raw, self.time_left_band, int(self.is_commodity),
            self.pet_species_id, self.bonus_lists, self.modifiers,
        )


@dataclass
class SnapshotBundle:
    snapshot_id: str
    region: str
    realm: str
    realm_slug: str
    connected_realm_id: Optional[int]
    captured_at: int
    source: str
    payload_sha256: str
    api_last_modified: str = ""
    auctions: List[AuctionRecord] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)

    @property
    def commodity_count(self) -> int:
        return sum(1 for a in self.auctions if a.is_commodity)

    @property
    def auction_count(self) -> int:
        return len(self.auctions) - self.commodity_count


@dataclass
class IngestResult:
    snapshot_id: str
    inserted: bool
    auctions: int = 0
    commodities: int = 0
    rejected: int = 0
    already_present: bool = False
    unknown_time_left: int = 0


def payload_hash(payload: Dict[str, Any]) -> str:
    """A stable hash of the payload, independent of key order."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_snapshot_id(region: str, realm: str, captured_at: int,
                     content_hash: str) -> str:
    raw = f"{region}|{realm}|{captured_at}|{content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _entry_key(entry: Dict[str, Any], index: int, commodity: bool) -> str:
    auction_id = _int_or_none(entry.get("id"))
    if auction_id is not None:
        return str(auction_id)
    # The API normally sends an id.  If a captured fixture (or an edge
    # case) does not, the row still needs a stable key; the ``h:`` prefix
    # makes clear that it is a content hash, not an auction id.
    blob = json.dumps({"entry": entry, "index": index, "commodity": commodity},
                      sort_keys=True, default=str)
    return "h:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def _parse_entry(entry: Dict[str, Any], index: int, commodity: bool) -> Optional[AuctionRecord]:
    if not isinstance(entry, dict):
        return None
    item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
    item_id = _int_or_none(item.get("id"))
    if item_id is None:
        return None
    quantity = _int_or_none(entry.get("quantity"))
    if quantity is None or quantity <= 0:
        quantity = 1

    buyout = _int_or_none(entry.get("buyout"))
    bid = _int_or_none(entry.get("bid"))
    unit = _int_or_none(entry.get("unit_price"))
    if unit is not None:
        unit_source = "api_unit_price"
    elif buyout is not None:
        unit = buyout // quantity
        unit_source = "derived_from_buyout"
    else:
        unit_source = ""

    # Preserve the API's total exactly when it sent one.  Deriving it from a
    # rounded unit price would quietly lose copper on a stack that does not
    # divide evenly.
    if buyout is not None:
        total = buyout
    elif unit is not None:
        total = unit * quantity
    else:
        total = None
    raw_time_left = entry.get("time_left")
    if isinstance(raw_time_left, str):
        raw_time_left = raw_time_left.strip() or None
    else:
        raw_time_left = None

    is_commodity = bool(commodity or "unit_price" in entry)
    bonus = item.get("bonus_lists")
    modifiers = item.get("modifiers")
    return AuctionRecord(
        auction_key=_entry_key(entry, index, is_commodity),
        auction_id=_int_or_none(entry.get("id")),
        item_id=item_id,
        quantity=int(quantity),
        unit_price_copper=unit,
        buyout_copper=buyout,
        bid_copper=bid,
        total_price_copper=total,
        unit_price_source=unit_source,
        time_left_raw=raw_time_left,
        time_left_band=time_left_band(raw_time_left),
        is_commodity=is_commodity,
        item_context=_int_or_none(item.get("context")),
        pet_species_id=_int_or_none(item.get("pet_species_id")),
        bonus_lists=json.dumps(bonus, sort_keys=True) if bonus else None,
        modifiers=json.dumps(modifiers, sort_keys=True) if modifiers else None,
    )


def parse_payload(payload: Dict[str, Any], *,
                  region: str,
                  realm: str,
                  realm_slug: str = "",
                  connected_realm_id: Optional[int] = None,
                  captured_at: Optional[int] = None,
                  source: str = "blizzard_api",
                  api_last_modified: str = "",
                  commodity_endpoint: bool = False) -> SnapshotBundle:
    """Normalise one API payload without writing anything.

    ``commodity_endpoint`` is for ``/data/wow/auctions/commodities``,
    whose ``auctions`` list is entirely commodities.
    """
    if not isinstance(payload, dict):
        raise ValueError("the auction payload is not a JSON object")
    now = int(captured_at if captured_at is not None
              else datetime.now(timezone.utc).timestamp())
    digest = payload_hash(payload)
    bundle = SnapshotBundle(
        snapshot_id=make_snapshot_id(region, realm, now, digest),
        region=region, realm=realm, realm_slug=realm_slug or "",
        connected_realm_id=connected_realm_id, captured_at=now,
        source=source, payload_sha256=digest, api_last_modified=api_last_modified,
    )

    seen: set[str] = set()
    sections: List[Tuple[str, Sequence[Any], bool]] = []
    auctions = payload.get("auctions")
    if isinstance(auctions, list):
        sections.append(("auctions", auctions, commodity_endpoint))
    commodities = payload.get("commodities")
    if isinstance(commodities, list):
        sections.append(("commodities", commodities, True))
    if not sections:
        bundle.rejected.append("payload has no 'auctions' or 'commodities' list")
        return bundle

    for section_name, rows, commodity in sections:
        for index, entry in enumerate(rows):
            record = _parse_entry(entry, index, commodity)
            if record is None:
                bundle.rejected.append(f"{section_name}[{index}]: no item id")
                continue
            key = record.auction_key
            if key in seen:
                # Duplicate keys inside one payload are not two auctions;
                # suffix the duplicate so nothing is silently dropped.
                key = f"{key}#dup{index}"
                record = AuctionRecord(**{**record.__dict__, "auction_key": key})
            seen.add(key)
            bundle.auctions.append(record)
    return bundle


def ingest_bundle(con, bundle: SnapshotBundle) -> IngestResult:
    """Insert a parsed snapshot idempotently.

    The snapshot id is a hash of the region, the realm, the capture time
    and the payload.  Re-ingesting the same file therefore writes nothing
    and says so; a new snapshot at a new time is a new row.
    """
    existing = con.execute(
        "SELECT 1 FROM ah_snapshot WHERE snapshot_id = ?",
        (bundle.snapshot_id,)).fetchone()
    if existing:
        return IngestResult(snapshot_id=bundle.snapshot_id, inserted=False,
                            already_present=True,
                            auctions=len(bundle.auctions),
                            rejected=len(bundle.rejected))

    con.execute(
        "INSERT INTO ah_snapshot (snapshot_id, region, connected_realm_id, realm,"
        " realm_slug, captured_at, api_last_modified, source, payload_sha256,"
        " auction_count, commodity_count, rejected_rows, imported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bundle.snapshot_id, bundle.region, bundle.connected_realm_id,
         bundle.realm, bundle.realm_slug, bundle.captured_at,
         bundle.api_last_modified, bundle.source, bundle.payload_sha256,
         bundle.auction_count, bundle.commodity_count, len(bundle.rejected),
         _utcnow()))
    con.executemany(
        "INSERT OR IGNORE INTO ah_auction (snapshot_id, auction_key, auction_id,"
        " item_id, item_context, quantity, unit_price_copper, buyout_copper,"
        " bid_copper, total_price_copper, unit_price_source, time_left_raw,"
        " time_left_band, is_commodity, pet_species_id, bonus_lists, modifiers)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(bundle.snapshot_id,) + record.as_row() for record in bundle.auctions])
    con.commit()
    return IngestResult(
        snapshot_id=bundle.snapshot_id, inserted=True,
        auctions=bundle.auction_count, commodities=bundle.commodity_count,
        rejected=len(bundle.rejected),
        unknown_time_left=sum(1 for a in bundle.auctions
                              if a.time_left_raw and not a.time_left_band))


def ingest_file(con, path: Path | str, **kwargs) -> IngestResult:
    """Read a JSON file and ingest it.  Extra kwargs go to :func:`parse_payload`."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ingest_bundle(con, parse_payload(payload, **kwargs))


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def latest_snapshot(con, region: str, realm: str) -> Optional[Dict[str, Any]]:
    row = con.execute(
        "SELECT * FROM ah_snapshot WHERE region = ? AND LOWER(realm) = LOWER(?)"
        " ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1",
        (region, realm)).fetchone()
    return dict(row) if row else None


def snapshot_auctions(con, snapshot_id: str) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM ah_auction WHERE snapshot_id = ?"
        " ORDER BY item_id, unit_price_copper IS NULL, unit_price_copper, auction_key",
        (snapshot_id,)).fetchall()
    return [dict(r) for r in rows]


def snapshots_between(con, region: str, realm: str,
                      start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM ah_snapshot WHERE region = ? AND LOWER(realm) = LOWER(?)"
        " AND captured_at >= ? AND captured_at <= ? ORDER BY captured_at",
        (region, realm, int(start_ts), int(end_ts))).fetchall()
    return [dict(r) for r in rows]


def item_ladder(con, snapshot_id: str, item_id: int) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM ah_auction WHERE snapshot_id = ? AND item_id = ?"
        " ORDER BY unit_price_copper IS NULL, unit_price_copper, quantity DESC",
        (snapshot_id, int(item_id))).fetchall()
    return [dict(r) for r in rows]


def summary(con, snapshot_id: str) -> Dict[str, Any]:
    """A compact description of one snapshot, with no interpretation."""
    snap = con.execute("SELECT * FROM ah_snapshot WHERE snapshot_id = ?",
                       (snapshot_id,)).fetchone()
    if not snap:
        return {"error": f"unknown snapshot {snapshot_id}"}
    totals = con.execute(
        "SELECT COUNT(*) AS listings, COALESCE(SUM(quantity),0) AS units,"
        " COUNT(DISTINCT item_id) AS items,"
        " SUM(CASE WHEN unit_price_copper IS NULL THEN 1 ELSE 0 END) AS unpriced,"
        " SUM(CASE WHEN time_left_raw IS NOT NULL AND time_left_band IS NULL"
        "          THEN 1 ELSE 0 END) AS unknown_time_left"
        " FROM ah_auction WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    bands = con.execute(
        "SELECT time_left_raw, COUNT(*) FROM ah_auction WHERE snapshot_id = ?"
        " GROUP BY time_left_raw ORDER BY COUNT(*) DESC", (snapshot_id,)).fetchall()
    return {
        "snapshot": dict(snap),
        "listings": int(totals["listings"]),
        "units": int(totals["units"]),
        "items": int(totals["items"]),
        "unpriced_listings": int(totals["unpriced"] or 0),
        "unknown_time_left": int(totals["unknown_time_left"] or 0),
        "time_left_counts": {str(r[0]): int(r[1]) for r in bands},
    }


# ---------------------------------------------------------------------------
# fetching (network lives here and nowhere else)
# ---------------------------------------------------------------------------

class AuctionHouseClient:
    """Thin subclass of the existing client, with the dynamic namespace.

    Composition rather than import-time construction: no credentials are
    read until the class is instantiated, so importing this module stays
    offline and testable.
    """

    def __init__(self, region: str = "eu", locale: str = "en_US",
                 requests_per_second: float = 1.0):
        from woweconomy.services.blizzard_items import BlizzardItemsClient
        self._inner = BlizzardItemsClient(region=region, locale=locale,
                                          requests_per_second=requests_per_second)
        self.region = region
        self.namespace = f"dynamic-{region}"
        self.locale = locale

    def get_json(self, path: str,
                 modified_since: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
        """A dynamic-namespace GET, with a 304 shortcut.

        The existing client defaults to ``static-<region>``; the connected
        realm index and the auctions are dynamic resources, so they go
        through this method with ``dynamic-<region>`` instead.
        """
        import time
        inner = self._inner
        params = {"namespace": self.namespace, "locale": self.locale}
        headers = {"Authorization": f"Bearer {inner._token()}"}
        if modified_since:
            headers["If-Modified-Since"] = modified_since
        for attempt in range(inner.max_retries + 1):
            inner._throttle()
            response = inner.session.get(inner.api_url + path, params=params,
                                         headers=headers, timeout=120)
            if response.status_code == 304:
                return None, response.headers.get("Last-Modified", modified_since)
            if response.status_code == 401 and attempt == 0:
                inner._token_exp = 0.0
                headers["Authorization"] = f"Bearer {inner._token()}"
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                delay = min(2 ** attempt, 30)
                logger.warning("Blizzard %s -> %s, retry in %ss",
                               path, response.status_code, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json(), response.headers.get("Last-Modified", "")
        raise RuntimeError(f"Blizzard {path} failed after retries")

    def fetch_auctions(self, connected_realm_id: int,
                       modified_since: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
        """Return ``(payload, last_modified)`` or ``(None, last_modified)`` on 304."""
        return self.get_json(
            f"/data/wow/connected-realm/{int(connected_realm_id)}/auctions",
            modified_since)


def sync_realm_index(con, client: AuctionHouseClient) -> int:
    """Populate ``ah_realm`` from the connected-realm index.

    One request for the index plus one per connected realm.  The mapping
    is required to fetch a realm's auctions at all, so it is stored rather
    than recomputed on every run.
    """
    index, _ = client.get_json("/data/wow/connected-realm/index")
    hrefs = [entry.get("href") for entry in ((index or {}).get("connected_realms") or [])
             if isinstance(entry, dict) and entry.get("href")]
    inserted = 0
    for href in hrefs:
        path = href.split(".com", 1)[-1].split("?", 1)[0]
        body, _ = client.get_json(path)
        if not isinstance(body, dict):
            continue
        realm_id = body.get("id")
        for realm in body.get("realms") or []:
            slug = str(realm.get("slug") or "")
            name = str(realm.get("name") or "")
            if not slug or not name:
                continue
            con.execute(
                "INSERT OR REPLACE INTO ah_realm (region, realm_slug, realm_name,"
                " connected_realm_id, refreshed_at) VALUES (?,?,?,?,?)",
                (client.region, slug, name, int(realm_id or 0), _utcnow()))
            inserted += 1
    con.commit()
    return inserted


def connected_realm_id(con, realm: str, region: str = "eu") -> Optional[int]:
    row = con.execute(
        "SELECT connected_realm_id FROM ah_realm WHERE region = ?"
        " AND (LOWER(realm_name) = LOWER(?) OR LOWER(realm_slug) = LOWER(?))",
        (region, realm, realm)).fetchone()
    return int(row[0]) if row else None
