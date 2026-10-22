"""Snapshots of TSM's five public CSVs, one file per connected-realm market.

Why connected realms are stored once
------------------------------------
TSM publishes ``realm/{slug}/items.csv`` for every realm slug, but a connected
group shares one auction house and the files are byte-identical across the
group.  Fetching 267 EU realm slugs would download the same 92 markets three
times; the store keeps one *canonical* slug per group and maps the rest onto it
in ``tsm_realm_group``.  A price row for ``draenor`` is therefore also the price
row for every realm connected to Draenor.

What is stored
--------------
* ``tsm_snapshot`` -- one row per downloaded file: scope, kind, upstream
  ``updatedAt``, fetch time, SHA-256, row count, changed-row count, gzip path.
* ``tsm_realm_price`` / ``tsm_realm_pet_price`` -- the latest price per item or
  pet species, for each market.
* ``tsm_region_price`` / ``tsm_region_pet_price`` / ``tsm_commodity_price`` --
  the latest region-wide files.
* ``tsm_price_change`` -- only the rows whose prices changed in that snapshot.
  The first snapshot is all rows; later ones are deltas, which keeps history
  usable without copying ~3.5M unchanged rows every few hours.

The raw CSV of every changed file is also kept gzipped under
``data/tsm/snapshots/<timestamp>/`` so a snapshot can be re-imported or audited
without asking TSM again.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

REALM_KINDS = ("realm_items", "realm_pets")
REGION_KINDS = ("region_items", "region_pets", "commodities")
ALL_KINDS = REALM_KINDS + REGION_KINDS

# kind -> (table, key column, is_realm)
TABLES: dict[str, tuple[str, str, bool]] = {
    "realm_items": ("tsm_realm_price", "item_id", True),
    "realm_pets": ("tsm_realm_pet_price", "pet_species_id", True),
    "region_items": ("tsm_region_price", "key_id", False),
    "region_pets": ("tsm_region_pet_price", "key_id", False),
    "commodities": ("tsm_commodity_price", "key_id", False),
}

# The columns each current table keeps, in insert order after the key.
COLUMNS: dict[str, tuple[str, ...]] = {
    "realm_items": ("name", "market_value", "min_buyout", "recent", "historical"),
    "realm_pets": ("name", "market_value", "min_buyout", "recent", "historical"),
    "region_items": ("name", "market_value", "historical", "avg_sale_price",
                     "sale_rate", "sold_per_day"),
    "region_pets": ("name", "market_value", "historical", "avg_sale_price",
                    "sale_rate", "sold_per_day"),
    "commodities": ("name", "market_value", "min_buyout", "recent", "historical"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tsm_snapshot(
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    rows INTEGER NOT NULL,
    changed INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL,
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    UNIQUE(scope, kind, checksum)
);

CREATE TABLE IF NOT EXISTS tsm_meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tsm_realm_group(
    realm_slug TEXT PRIMARY KEY,
    realm_name TEXT,
    group_id INTEGER,
    canonical_slug TEXT NOT NULL,
    canonical_name TEXT,
    is_canonical INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tsm_realm_group_name ON tsm_realm_group(realm_name);

CREATE TABLE IF NOT EXISTS tsm_realm_price(
    scope TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    name TEXT,
    market_value INTEGER,
    min_buyout INTEGER,
    recent INTEGER,
    historical INTEGER,
    updated_at TEXT,
    fetched_at TEXT,
    snapshot_id INTEGER,
    PRIMARY KEY(scope, item_id)
);
CREATE INDEX IF NOT EXISTS ix_tsm_realm_price_item ON tsm_realm_price(item_id);

CREATE TABLE IF NOT EXISTS tsm_realm_pet_price(
    scope TEXT NOT NULL,
    pet_species_id INTEGER NOT NULL,
    name TEXT,
    market_value INTEGER,
    min_buyout INTEGER,
    recent INTEGER,
    historical INTEGER,
    updated_at TEXT,
    fetched_at TEXT,
    snapshot_id INTEGER,
    PRIMARY KEY(scope, pet_species_id)
);

CREATE TABLE IF NOT EXISTS tsm_region_price(
    key_id INTEGER PRIMARY KEY,
    name TEXT,
    market_value INTEGER,
    historical INTEGER,
    avg_sale_price INTEGER,
    sale_rate REAL,
    sold_per_day REAL,
    updated_at TEXT,
    fetched_at TEXT,
    snapshot_id INTEGER
);

CREATE TABLE IF NOT EXISTS tsm_region_pet_price(
    key_id INTEGER PRIMARY KEY,
    name TEXT,
    market_value INTEGER,
    historical INTEGER,
    avg_sale_price INTEGER,
    sale_rate REAL,
    sold_per_day REAL,
    updated_at TEXT,
    fetched_at TEXT,
    snapshot_id INTEGER
);

CREATE TABLE IF NOT EXISTS tsm_commodity_price(
    key_id INTEGER PRIMARY KEY,
    name TEXT,
    market_value INTEGER,
    min_buyout INTEGER,
    recent INTEGER,
    historical INTEGER,
    updated_at TEXT,
    fetched_at TEXT,
    snapshot_id INTEGER
);

CREATE TABLE IF NOT EXISTS tsm_price_change(
    snapshot_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    key_id INTEGER NOT NULL,
    market_value INTEGER,
    min_buyout INTEGER,
    recent INTEGER,
    historical INTEGER,
    avg_sale_price INTEGER,
    sale_rate REAL,
    sold_per_day REAL,
    PRIMARY KEY(snapshot_id, key_id)
);
CREATE INDEX IF NOT EXISTS ix_tsm_price_change_key
    ON tsm_price_change(kind, scope, key_id);
"""


# --------------------------------------------------------------------------- #
# Connected-realm map
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RealmGroup:
    group_id: int
    canonical_slug: str
    canonical_name: str
    members: tuple[dict, ...]

    @property
    def member_slugs(self) -> tuple[str, ...]:
        return tuple(m["slug"] for m in self.members)


@dataclass
class RealmMap:
    groups: list[RealmGroup]
    slug_to_group: dict[str, RealmGroup]
    name_to_slug: dict[str, str]

    @property
    def canonical_groups(self) -> list[RealmGroup]:
        return self.groups

    def resolve(self, token: str) -> RealmGroup:
        """Resolve a realm name, slug or group id to its connected group."""
        raw = (token or "").strip()
        if not raw:
            raise KeyError("empty realm")
        low = raw.lower()
        if low in self.slug_to_group:
            return self.slug_to_group[low]
        if raw in self.name_to_slug:
            return self.slug_to_group[self.name_to_slug[raw]]
        # Accents and apostrophes differ between the Blizzard name and what a
        # shell user types; compare a folded form as the last resort.
        folded = _fold(raw)
        for name, slug in self.name_to_slug.items():
            if _fold(name) == folded:
                return self.slug_to_group[slug]
        raise KeyError(f"realm not in the Blizzard index: {raw}")


def _fold(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def load_realm_map(path: str | Path) -> RealmMap:
    """Read ``reports/realms_eu.json`` produced by ``scripts/blizzard_realms.py``.

    Only realms that belong to a connected group are kept: the index also
    carries internal shards (``eu3a-account-realm``, ``zzzrdb-eu``) with no
    players and no TSM file.  A group of one is a normal solo realm.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    realms = data.get("realms") or []
    name_by_slug = {r["slug"]: r.get("name", r["slug"]) for r in realms}
    slug_by_name = {r.get("name", r["slug"]): r["slug"] for r in realms}
    groups: list[RealmGroup] = []
    for raw in data.get("connected_realms") or []:
        members = tuple(raw.get("members") or ())
        if not members:
            continue
        canonical = members[0]
        groups.append(RealmGroup(
            group_id=int(raw.get("id") or canonical.get("id") or 0),
            canonical_slug=canonical["slug"],
            canonical_name=canonical.get("name", canonical["slug"]),
            members=members,
        ))
    slug_to_group: dict[str, RealmGroup] = {}
    for group in groups:
        for slug in group.member_slugs:
            slug_to_group[slug] = group
    return RealmMap(groups=groups, slug_to_group=slug_to_group,
                    name_to_slug=slug_by_name)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Record:
    key_id: int
    name: str = ""
    market_value: int = 0
    min_buyout: int = 0
    recent: int = 0
    historical: int = 0
    avg_sale_price: int = 0
    sale_rate: float = 0.0
    sold_per_day: float = 0.0

    def values(self, kind: str) -> tuple:
        cols = COLUMNS[kind]
        out = []
        for col in cols:
            out.append(getattr(self, col))
        return tuple(out)


@dataclass
class Task:
    scope: str
    kind: str
    url: str
    realm_name: str = ""

    @property
    def label(self) -> str:
        return f"{self.scope}/{self.kind}"


@dataclass
class Download:
    task: Task
    content: bytes = b""
    checksum: str = ""
    error: str = ""
    status: int = 0
    not_modified: bool = False
    etag: str = ""
    last_modified: str = ""
    retry_after: float = 0.0


@dataclass
class ImportResult:
    task: Task
    checksum: str = ""
    rows: int = 0
    changed: int = 0
    updated_at: str = ""
    path: str = ""
    skipped: bool = False
    error: str = ""
    not_modified: bool = False


@dataclass
class SyncStats:
    files: int = 0
    skipped: int = 0
    failed: int = 0
    rows: int = 0
    changed: int = 0
    aborted: bool = False
    not_modified: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Schema / helpers
# --------------------------------------------------------------------------- #

def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # Bases criadas antes do ETag existirem: o CREATE TABLE IF NOT EXISTS acima
    # nao acrescenta colunas a uma tabela que ja la esta.
    have = {row[1] for row in con.execute("PRAGMA table_info(tsm_snapshot)")}
    for column in ("etag", "last_modified"):
        if column not in have:
            con.execute(
                f"ALTER TABLE tsm_snapshot ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    con.commit()


def sync_realm_groups(con: sqlite3.Connection, realm_map: RealmMap) -> int:
    """Write the slug -> canonical-group map; returns realms written."""
    rows = []
    for group in realm_map.groups:
        for member in group.members:
            rows.append((
                member["slug"], member.get("name", member["slug"]),
                group.group_id, group.canonical_slug, group.canonical_name,
                1 if member["slug"] == group.canonical_slug else 0,
            ))
    con.executemany(
        "INSERT INTO tsm_realm_group(realm_slug, realm_name, group_id, "
        "canonical_slug, canonical_name, is_canonical) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(realm_slug) DO UPDATE SET realm_name=excluded.realm_name, "
        "group_id=excluded.group_id, canonical_slug=excluded.canonical_slug, "
        "canonical_name=excluded.canonical_name, "
        "is_canonical=excluded.is_canonical",
        rows)
    con.commit()
    return len(rows)


def build_tasks(realm_map: RealmMap, kinds: Sequence[str],
                realms: Optional[Iterable[str]] = None,
                base_url: str = "https://public-data.tradeskillmaster.com",
                game_type: str = "retail", region: str = "eu",
                include_region: bool = True) -> list[Task]:
    """One task per market/kind; connected groups are fetched once."""
    if realms is None:
        groups = realm_map.canonical_groups
    else:
        seen: set[str] = set()
        groups = []
        for token in realms:
            group = realm_map.resolve(token)
            if group.canonical_slug not in seen:
                seen.add(group.canonical_slug)
                groups.append(group)
    tasks: list[Task] = []
    for kind in kinds:
        if kind in REALM_KINDS:
            for group in groups:
                if kind == "realm_items":
                    url = f"{base_url}/{game_type}/{region}/realm/{_quote(group.canonical_slug)}/items.csv"
                else:
                    url = f"{base_url}/{game_type}/{region}/realm/{_quote(group.canonical_slug)}/pets.csv"
                tasks.append(Task(scope=group.canonical_slug, kind=kind, url=url,
                                  realm_name=group.canonical_name))
        elif kind == "region_items":
            tasks.append(Task(scope="region", kind=kind,
                              url=f"{base_url}/{game_type}/{region}/region/items.csv"))
        elif kind == "region_pets":
            tasks.append(Task(scope="region", kind=kind,
                              url=f"{base_url}/{game_type}/{region}/region/pets.csv"))
        elif kind == "commodities":
            tasks.append(Task(scope="region", kind=kind,
                              url=f"{base_url}/{game_type}/{region}/commodities.csv"))
        else:
            raise ValueError(f"unknown kind {kind!r}")
    if not include_region:
        tasks = [t for t in tasks if t.kind in REALM_KINDS]
    return tasks


def _quote(slug: str) -> str:
    from urllib.parse import quote

    return quote(slug, safe="")


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Global spacing between request starts, shared by all workers.

    TSM answers 429 after a burst even though its docs say there is no rate
    limit.  A one-second gate keeps a 187-file run under that ceiling while
    still letting the workers overlap their downloads.
    """

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = max(0.0, min_interval)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = time.monotonic()
            self.next_at = max(now, self.next_at) + self.min_interval


def _retry_after(resp) -> float:
    raw = (resp.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return float(raw)
    return 0.0


def download(task: Task, timeout: int = 120, retries: int = 4,
             limiter: Optional[RateLimiter] = None, max_wait: float = 120.0,
             abort: Optional[threading.Event] = None,
             etag: str = "", last_modified: str = "") -> Download:
    """Download one CSV body.  Errors are returned, never raised, so one bad
    realm does not abort a 180-file run.

    ``etag``/``last_modified`` make this a conditional GET: the CDN answers 304
    with no body when TSM has not republished the file, which is most of the
    time because realm files are rebuilt ~every 3 h and cached for 4 h.  That
    removes ~99% of the bytes from a re-run without changing what is stored.

    A 429 is never retried more than once here: the old loop fired up to four
    requests per file straight into the cooldown, which is how a burst turned
    into a one-hour ban.
    """
    import requests

    headers = {"User-Agent": "WowEconomyEngine/0.1 (market snapshot)"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    last = ""
    for attempt in range(retries):
        if abort is not None and abort.is_set():
            return Download(task=task, error="aborted")
        if limiter:
            limiter.wait()
        try:
            resp = requests.get(task.url, timeout=timeout, headers=headers)
            if resp.status_code == 304:
                return Download(task=task, status=304, not_modified=True,
                                etag=resp.headers.get("ETag") or etag,
                                last_modified=(resp.headers.get("Last-Modified")
                                               or last_modified))
            if resp.status_code == 404:
                return Download(task=task, status=404,
                                error="404 (TSM has no file for this slug)")
            if resp.status_code == 429:
                wait = _retry_after(resp) or min(60.0, 8.0 * (2 ** attempt))
                # A short throttle is worth one sleep; a long one is a ban, and
                # every extra request only extends it.  Stop the whole run and
                # let the next one resume from the files still missing.
                if wait > 15.0 or attempt >= 1:
                    if abort is not None:
                        abort.set()
                    return Download(task=task, status=429, retry_after=wait,
                                    error=f"429 cooldown de {wait:.0f}s; tenta mais tarde")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            content = resp.content
            if not content:
                return Download(task=task, status=resp.status_code,
                                error="empty response")
            return Download(task=task, content=content, status=resp.status_code,
                            checksum=hashlib.sha256(content).hexdigest(),
                            etag=resp.headers.get("ETag", ""),
                            last_modified=resp.headers.get("Last-Modified", ""))
        except Exception as exc:  # noqa: BLE001 - reported per file
            last = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return Download(task=task, error=last)


# --------------------------------------------------------------------------- #
# Parsing / importing
# --------------------------------------------------------------------------- #

def parse_rows(kind: str, content: bytes) -> tuple[list[Record], str]:
    """Parse one CSV body; returns records and the upstream ``updatedAt``."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    key_field = "petSpeciesId" if kind in ("realm_pets", "region_pets") else "itemId"
    records: list[Record] = []
    updated_at = ""
    for row in reader:
        raw_key = row.get(key_field) or row.get(key_field.lower()) or ""
        try:
            key_id = int(float(raw_key))
        except (TypeError, ValueError):
            continue
        stamp = (row.get("updatedAt") or row.get("updatedat") or "").strip()
        if stamp and not updated_at:
            updated_at = stamp
        records.append(Record(
            key_id=key_id,
            name=(row.get("name") or "").strip(),
            market_value=_int(row.get("marketValue") or row.get("marketvalue")),
            min_buyout=_int(row.get("minBuyout") or row.get("minbuyout")),
            recent=_int(row.get("recent")),
            historical=_int(row.get("historical")),
            avg_sale_price=_int(row.get("avgSalePrice") or row.get("avgsaleprice")),
            sale_rate=_float(row.get("saleRate") or row.get("salerate")),
            sold_per_day=_float(row.get("soldPerDay") or row.get("soldperday")),
        ))
    return records, updated_at


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def snapshot_exists(con: sqlite3.Connection, task: Task, checksum: str) -> bool:
    return con.execute(
        "SELECT 1 FROM tsm_snapshot WHERE scope=? AND kind=? AND checksum=?",
        (task.scope, task.kind, checksum)).fetchone() is not None


def import_content(con: sqlite3.Connection, task: Task, content: bytes,
                   fetched_at: str, out_dir: str | Path,
                   write_file: bool = True, etag: str = "",
                   last_modified: str = "") -> ImportResult:
    """Store one downloaded CSV: gzip, metadata, latest values, deltas."""
    checksum = hashlib.sha256(content).hexdigest()
    if snapshot_exists(con, task, checksum):
        row = con.execute(
            "SELECT updated_at, rows FROM tsm_snapshot WHERE scope=? AND kind=? "
            "AND checksum=?", (task.scope, task.kind, checksum)).fetchone()
        # Bytes iguais mas ETag novo (raro): guarda o novo para a proxima
        # condicional acertar no 304.
        if etag or last_modified:
            con.execute(
                "UPDATE tsm_snapshot SET etag=?, last_modified=? "
                "WHERE scope=? AND kind=? AND checksum=?",
                (etag, last_modified, task.scope, task.kind, checksum))
            con.commit()
        return ImportResult(task=task, checksum=checksum, skipped=True,
                            updated_at=row[0] if row else "",
                            rows=row[1] if row else 0)

    records, updated_at = parse_rows(task.kind, content)
    if not records:
        return ImportResult(task=task, checksum=checksum, error="no parsable rows")
    updated_at = updated_at or fetched_at

    path = ""
    if write_file:
        stamp = fetched_at.replace(":", "").replace("-", "")
        dest = Path(out_dir) / stamp / f"{_safe(task.scope)}.{task.kind}.csv.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(dest, "wb", compresslevel=6) as fh:
            fh.write(content)
        path = str(dest)

    cur = con.execute(
        "INSERT INTO tsm_snapshot(scope, kind, updated_at, fetched_at, checksum, "
        "rows, changed, path, etag, last_modified) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (task.scope, task.kind, updated_at, fetched_at, checksum,
         len(records), 0, path, etag, last_modified))
    snapshot_id = int(cur.lastrowid)

    changed = _upsert_current(con, task, records, updated_at, fetched_at,
                              snapshot_id)
    con.execute("UPDATE tsm_snapshot SET changed=? WHERE snapshot_id=?",
                (changed, snapshot_id))
    con.commit()
    return ImportResult(task=task, checksum=checksum, rows=len(records),
                        changed=changed, updated_at=updated_at, path=path)


def import_csv_file(con: sqlite3.Connection, path: str | Path, scope: str,
                    kind: str, out_dir: str | Path,
                    fetched_at: Optional[str] = None) -> ImportResult:
    """Import a CSV already on disk (e.g. ``csv/items.csv``) as a snapshot.

    This is the no-request fallback for the region files and for a realm file
    that was downloaded before the store existed.
    """
    content = Path(path).read_bytes()
    task = Task(scope=scope, kind=kind, url=f"file://{Path(path).resolve()}")
    stamp = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return import_content(con, task, content, stamp, out_dir, write_file=True)


def import_appdata(con: sqlite3.Connection) -> list[ImportResult]:
    """Seed realm prices from ``tsm_market`` (the AppData already in the DB).

    The public CSV fetch can be rate-limited for an hour; the AppData covers the
    realms TSM has scanned and carries the same ``min_buyout``/``DBMarket``
    semantics.  A connected group is stored once, under its canonical slug, and
    only when it is not older than a CSV snapshot already present.
    """
    groups = {}
    for slug, name, canonical in con.execute(
            "SELECT realm_slug, realm_name, canonical_slug FROM tsm_realm_group"):
        groups[_fold(name or slug)] = (slug, canonical)
    names = dict(con.execute("SELECT itemId, name FROM items"))
    by_realm: dict[str, list[tuple]] = {}
    for row in con.execute(
            "SELECT realm, itemId, min_buyout, market, market_recent, historical "
            "FROM tsm_market"):
        by_realm.setdefault(row[0], []).append(row)

    # One representative per canonical group: the member with the most rows.
    best: dict[str, tuple[str, list[tuple]]] = {}
    for realm, rows in by_realm.items():
        hit = groups.get(_fold(realm))
        if not hit:
            logger.warning("appdata: realm %r fora do mapa connected", realm)
            continue
        _slug, canonical = hit
        if canonical not in best or len(rows) > len(best[canonical][1]):
            best[canonical] = (realm, rows)

    results: list[ImportResult] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for canonical, (realm, rows) in best.items():
        epoch = con.execute(
            "SELECT value FROM tsm_meta WHERE key=?",
            (f"dl_AUCTIONDB_NON_COMMODITY_DATA_{realm}",)).fetchone()
        if epoch and str(epoch[0]).isdigit():
            updated_at = datetime.fromtimestamp(
                int(epoch[0]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            updated_at = now
        latest = con.execute(
            "SELECT MAX(updated_at) FROM tsm_snapshot WHERE scope=? AND kind='realm_items'",
            (canonical,)).fetchone()[0]
        if latest and latest >= updated_at:
            continue
        records = [Record(
            key_id=int(r[1]), name=names.get(int(r[1]), ""),
            market_value=_int(r[3]), min_buyout=_int(r[2]),
            recent=_int(r[4]), historical=_int(r[5]),
        ) for r in rows if r[1] is not None]
        if not records:
            continue
        checksum = hashlib.sha256(
            f"appdata:{canonical}:{updated_at}:{len(records)}".encode()).hexdigest()
        if snapshot_exists(con, Task(canonical, "realm_items", "appdata"), checksum):
            continue
        cur = con.execute(
            "INSERT INTO tsm_snapshot(scope, kind, updated_at, fetched_at, checksum, "
            "rows, changed, path) VALUES(?,?,?,?,?,?,?,?)",
            (canonical, "realm_items", updated_at, now, checksum,
             len(records), 0, "appdata:tsm_market"))
        snapshot_id = int(cur.lastrowid)
        changed = _upsert_current(
            con, Task(canonical, "realm_items", "appdata"), records,
            updated_at, now, snapshot_id)
        con.execute("UPDATE tsm_snapshot SET changed=? WHERE snapshot_id=?",
                    (changed, snapshot_id))
        con.commit()
        results.append(ImportResult(
            task=Task(canonical, "realm_items", "appdata"),
            checksum=checksum, rows=len(records), changed=changed,
            updated_at=updated_at, path="appdata:tsm_market"))
    return results


def _current_key(kind: str) -> tuple[str, str, bool]:
    return TABLES[kind]


def _upsert_current(con: sqlite3.Connection, task: Task, records: Sequence[Record],
                    updated_at: str, fetched_at: str, snapshot_id: int) -> int:
    table, key_col, is_realm = TABLES[task.kind]
    cols = COLUMNS[task.kind]
    key_prefix = ("scope",) if is_realm else ()
    pk = (task.scope,) if is_realm else ()

    select_cols = (("scope, " if is_realm else "") + key_col + ", " +
                   ", ".join(cols))
    before = {
        (row[1] if is_realm else row[0]): tuple(row[(2 if is_realm else 1):])
        for row in con.execute(
            f"SELECT {select_cols} FROM {table} WHERE " +
            ("scope=?" if is_realm else "1=1"), pk)
    }

    insert_cols = key_prefix + (key_col,) + tuple(cols) + (
        "updated_at", "fetched_at", "snapshot_id")
    placeholders = ",".join("?" * len(insert_cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in
                        tuple(cols) + ("updated_at", "fetched_at", "snapshot_id"))
    conflict = "scope, " + key_col if is_realm else key_col
    sql = (f"INSERT INTO {table}({', '.join(insert_cols)}) "
           f"VALUES({placeholders}) ON CONFLICT({conflict}) DO UPDATE SET {updates}")

    changed_rows: list[tuple] = []
    batch: list[tuple] = []
    for rec in records:
        values = rec.values(task.kind)
        old = before.get(rec.key_id)
        if old != values:
            changed_rows.append((
                snapshot_id, task.scope, task.kind, rec.key_id,
                rec.market_value, rec.min_buyout, rec.recent, rec.historical,
                rec.avg_sale_price, rec.sale_rate, rec.sold_per_day))
        batch.append(pk + (rec.key_id,) + values + (updated_at, fetched_at,
                                                    snapshot_id))
    if batch:
        con.executemany(sql, batch)
    if changed_rows:
        con.executemany(
            "INSERT OR REPLACE INTO tsm_price_change(snapshot_id, scope, kind, "
            "key_id, market_value, min_buyout, recent, historical, "
            "avg_sale_price, sale_rate, sold_per_day) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", changed_rows)
    return len(changed_rows)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def stored_validators(con: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, str]]:
    """``(scope, kind) -> (etag, last_modified)`` do snapshot mais recente.

    É isto que torna a segunda passagem barata: com o ETag guardado o CDN
    responde 304 e o corpo nunca chega a ser transferido.
    """
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for scope, kind, etag, lm in con.execute(
            "SELECT scope, kind, etag, last_modified FROM tsm_snapshot "
            "WHERE COALESCE(etag, '') <> '' OR COALESCE(last_modified, '') <> '' "
            "ORDER BY fetched_at"):
        out[(scope, kind)] = (etag or "", lm or "")
    return out


def order_for_resume(con: sqlite3.Connection, tasks: Sequence[Task]) -> list[Task]:
    """Ficheiros nunca buscados primeiro, depois os buscados há mais tempo.

    Sem isto um run que morra a meio volta sempre a começar nos mesmos vinte e
    nunca chega aos 42 que faltam.
    """
    seen: dict[tuple[str, str], str] = {}
    for scope, kind, fetched in con.execute(
            "SELECT scope, kind, MAX(fetched_at) FROM tsm_snapshot "
            "GROUP BY scope, kind"):
        seen[(scope, kind)] = fetched or ""
    return sorted(tasks, key=lambda t: (seen.get((t.scope, t.kind), ""),
                                        t.scope, t.kind))


def cooldown_until(con: sqlite3.Connection, now: float | None = None) -> float:
    """Quando é que o CDN volta a aceitar pedidos (epoch), 0 se não há espera."""
    row = con.execute("SELECT value FROM tsm_meta WHERE key='ratelimit_until'").fetchone()
    if not row:
        return 0.0
    try:
        until = float(row[0])
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - (now if now is not None else time.time()))


def _set_cooldown(con: sqlite3.Connection, seconds: float) -> None:
    until = time.time() + max(0.0, seconds)
    con.execute("INSERT OR REPLACE INTO tsm_meta(key, value) VALUES('ratelimit_until', ?)",
                (str(int(until)),))
    con.commit()


def sync(con: sqlite3.Connection, tasks: Sequence[Task], out_dir: str | Path,
         workers: int = 2, timeout: int = 120, retries: int = 4,
         delay: float = 1.0, max_wait: float = 120.0, progress=None) -> SyncStats:
    """Download tasks concurrently, import sequentially (one SQLite writer)."""
    stats = SyncStats(files=len(tasks))
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    workers = max(1, workers)
    limiter = RateLimiter(delay)
    abort = threading.Event()
    validators = stored_validators(con)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for t in tasks:
            etag, lm = validators.get((t.scope, t.kind), ("", ""))
            futures[pool.submit(download, t, timeout, retries, limiter,
                                max_wait, abort, etag, lm)] = t
        for future in as_completed(futures):
            dl = future.result()
            task = dl.task
            if dl.not_modified:
                # 304: TSM não republicou o ficheiro.  Nada a importar, zero bytes.
                stats.skipped += 1
                stats.not_modified += 1
                if progress:
                    progress(task, ImportResult(task=task, skipped=True,
                                                not_modified=True), "")
                continue
            if dl.error:
                if dl.status == 429:
                    _set_cooldown(con, dl.retry_after or 3600.0)
                stats.failed += 1
                stats.errors.append(f"{task.label}: {dl.error}")
                if abort.is_set():
                    stats.aborted = True
                if progress:
                    progress(task, None, dl.error)
                continue
            try:
                result = import_content(con, task, dl.content, fetched_at, out_dir,
                                        etag=dl.etag, last_modified=dl.last_modified)
            except sqlite3.Error as exc:
                stats.failed += 1
                stats.errors.append(f"{task.label}: {exc}")
                if progress:
                    progress(task, None, str(exc))
                continue
            if result.error:
                stats.failed += 1
                stats.errors.append(f"{task.label}: {result.error}")
            elif result.skipped:
                stats.skipped += 1
            else:
                stats.rows += result.rows
                stats.changed += result.changed
            if progress:
                progress(task, result, result.error)
    return stats


def _safe(text: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z._-]+", "_", text) or "scope"


# --------------------------------------------------------------------------- #
# Read helpers used by the analysis layer
# --------------------------------------------------------------------------- #

def latest_snapshot(con: sqlite3.Connection, scope: str, kind: str) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM tsm_snapshot WHERE scope=? AND kind=? "
        "ORDER BY fetched_at DESC LIMIT 1", (scope, kind)).fetchone()


def canonical_for(con: sqlite3.Connection, realm_name_or_slug: str) -> Optional[str]:
    """Resolve a realm name or slug to the canonical slug stored in prices."""
    row = con.execute(
        "SELECT canonical_slug FROM tsm_realm_group WHERE realm_slug=? "
        "OR realm_name=? COLLATE NOCASE LIMIT 1",
        (realm_name_or_slug, realm_name_or_slug)).fetchone()
    return row[0] if row else None


def realm_price_rows(con: sqlite3.Connection, scope: str
                     ) -> Iterator[tuple[int, int, int, int, int]]:
    """``(item_id, market_value, min_buyout, recent, historical)`` for a market."""
    yield from con.execute(
        "SELECT item_id, market_value, min_buyout, recent, historical "
        "FROM tsm_realm_price WHERE scope=?", (scope,))


def region_price_rows(con: sqlite3.Connection
                      ) -> Iterator[tuple[int, int, int]]:
    """``(item_id, market_value, historical)`` for the region file."""
    yield from con.execute(
        "SELECT key_id, market_value, historical FROM tsm_region_price")
