"""Blizzard item enrichment — quality / binding for farmed drops.

Why
    TSM prices alone can't tell a sellable BoE from a soulbound quest item
    or grey vendor trash.  Blizzard's item record carries both:
        quality.type            POOR | COMMON | UNCOMMON | RARE | EPIC | ...
        preview_item.binding    BIND_ON_PICKUP | BIND_ON_EQUIP | BIND_ON_USE | ...

Credentials (NEVER in code — env only, same names as other projects)
    BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET

Endpoints (static namespace, EU retail by default)
    GET /data/wow/item/{itemId}          canonical record for one item
    GET /data/wow/search/item            universe discovery, _pageSize=1000

Storage
    tsm_markets.db :: blizzard_items(itemId PK, name, quality, binding,
    level, sellPrice, fetchedAt).  Enrichment is resumable: ids already
    stored (and fresh) are skipped.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(".env"))
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    # minimal KEY=VALUE fallback (no quotes handling beyond basics)
    for _p in (Path(".env"), Path(__file__).resolve().parents[2] / ".env"):
        try:
            for _line in _p.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))
        except OSError:
            pass

logger = logging.getLogger(__name__)

REGION_HOSTS = {
    "eu": ("https://eu.battle.net/oauth/token", "https://eu.api.blizzard.com"),
    "us": ("https://us.battle.net/oauth/token", "https://us.api.blizzard.com"),
}

# Bindings that can never reach the Auction House.  Blizzard spells BoP
# "ON_ACQUIRE" ("Binds when picked up"); BIND_* kept as a safety net.
UNSELLABLE_BINDINGS = frozenset({
    "ON_ACQUIRE",
    "TO_ACCOUNT",
    "TO_BNETACCOUNT",
    "BIND_ON_PICKUP",
    "BIND_TO_ACCOUNT",
    "BIND_TO_BNETACCOUNT",
})

# Grey vendor trash.
JUNK_QUALITIES = frozenset({"POOR"})

# Blizzard published limits: 100 requests/second, 36,000/hour.
BURST_PER_SECOND = 100.0
HOURLY_CAP = 36_000
HOURLY_MARGIN = 1_000  # stop this far below the cap

SEARCH_PAGE_SIZE = 1000


@dataclass
class BlizzardItem:
    item_id: int
    name: str = ""
    quality: str = ""    # POOR | COMMON | UNCOMMON | RARE | EPIC | ...
    binding: str = ""    # BIND_ON_PICKUP | BIND_ON_EQUIP | ... ("" = none/tradable)
    level: int = 0
    sell_price: int = 0  # vendor sell price, copper
    fetched_at: str = ""
    era: int = 0         # DB2 ItemSquishEraID: 0 = retail, else era-Classic

    @property
    def is_soulbound(self) -> bool:
        return self.binding.upper() in UNSELLABLE_BINDINGS

    @property
    def is_era_classic(self) -> bool:
        """True for an era-Classic row: its TSM price belongs to another game."""
        return bool(self.era)

    @property
    def is_junk_quality(self) -> bool:
        return self.quality.upper() in JUNK_QUALITIES

    @property
    def excluded(self) -> bool:
        """BoP/account-bound or grey trash: never farmable for gold."""
        return self.is_soulbound or self.is_junk_quality


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BlizzardItemsClient:
    """Minimal sync client: OAuth client-credentials + item endpoints."""

    def __init__(self, region: str = "eu", locale: str = "en_US",
                 requests_per_second: float = 8.0,
                 max_retries: int = 4):
        cid = os.getenv("BLIZZARD_CLIENT_ID", "")
        secret = os.getenv("BLIZZARD_CLIENT_SECRET", "")
        if not cid or not secret:
            raise RuntimeError(
                "Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET env vars.")
        token_url, api_url = REGION_HOSTS[region]
        self.token_url = token_url
        self.api_url = api_url
        self.namespace = f"static-{region}"
        self.locale = locale
        self.min_interval = 1.0 / min(max(requests_per_second, 0.1),
                                      BURST_PER_SECOND)
        self.max_retries = max_retries
        self._cid = cid
        self._secret = secret
        self._access_token = ""
        self._token_exp = 0.0
        self._last_call = 0.0
        self._window: list[float] = []  # call timestamps, rolling hour
        self.session = requests.Session()

    # -- plumbing ----------------------------------------------------

    def _throttle(self) -> None:
        now = time.time()
        dt = now - self._last_call
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
            now = time.time()
        # hourly cap: slide the window, wait if we're at the ceiling
        cutoff = now - 3600.0
        self._window = [t for t in self._window if t > cutoff]
        if len(self._window) >= HOURLY_CAP - HOURLY_MARGIN:
            wait = self._window[0] + 3600.0 - now + 1.0
            if wait > 0:
                logger.warning("Hourly cap reached, sleeping %.0fs", wait)
                time.sleep(wait)
            now = time.time()
            self._window = [t for t in self._window if t > now - 3600.0]
        self._last_call = now
        self._window.append(now)

    def _token(self) -> str:
        if self._access_token and time.time() < self._token_exp - 60:
            return self._access_token
        r = self.session.post(
            self.token_url,
            data={"grant_type": "client_credentials"},
            auth=(self._cid, self._secret),
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        self._access_token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 86399))
        return self._access_token

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        params = dict(params or {})
        params.setdefault("namespace", self.namespace)
        params.setdefault("locale", self.locale)
        for attempt in range(self.max_retries + 1):
            self._throttle()
            r = self.session.get(
                self.api_url + path,
                params=params,
                headers={"Authorization": f"Bearer {self._token()}"},
                timeout=30,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 30)
                logger.warning("Blizzard %s -> %s, retry in %ss",
                               path, r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code == 401 and attempt == 0:
                self._token_exp = 0.0  # force refresh once
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Blizzard {path} failed after retries")

    # -- endpoints ---------------------------------------------------

    @staticmethod
    def parse_item(item_id: int, body: dict) -> BlizzardItem:
        preview = body.get("preview_item") or {}
        binding = (preview.get("binding") or {}).get("type", "") or ""
        quality = (body.get("quality") or {}).get("type", "") or ""
        return BlizzardItem(
            item_id=int(body.get("id", item_id)),
            name=body.get("name", ""),
            quality=quality.upper(),
            binding=binding.upper(),
            level=int(body.get("level") or 0),
            sell_price=int(body.get("sell_price") or 0),
            fetched_at=_utcnow(),
        )

    def fetch_item(self, item_id: int) -> BlizzardItem:
        body = self._get(f"/data/wow/item/{item_id}")
        return self.parse_item(item_id, body)

    def iter_search(self, page_size: int = SEARCH_PAGE_SIZE,
                    orderby: str = "id") -> Iterator[list[dict]]:
        """Yield raw search-result pages (each up to page_size entries)."""
        page = 1
        while True:
            body = self._get("/data/wow/search/item", {
                "orderby": orderby, "_page": page, "_pageSize": page_size,
            })
            results = body.get("results", [])
            if not results:
                return
            yield results
            if page >= int(body.get("pageCount", page)):
                return
            page += 1


# -- storage ----------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS blizzard_items (
    itemId INTEGER PRIMARY KEY,
    name TEXT,
    quality TEXT,
    binding TEXT,
    level INTEGER,
    sellPrice INTEGER,
    fetchedAt TEXT
);
CREATE TABLE IF NOT EXISTS blizzard_missing (
    itemId INTEGER PRIMARY KEY,
    failedAt TEXT
)
"""


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, item: BlizzardItem) -> None:
    conn.execute(
        "INSERT INTO blizzard_items(itemId,name,quality,binding,level,sellPrice,fetchedAt)"
        " VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(itemId) DO UPDATE SET name=excluded.name, quality=excluded.quality,"
        " binding=excluded.binding, level=excluded.level, sellPrice=excluded.sellPrice,"
        " fetchedAt=excluded.fetchedAt",
        (item.item_id, item.name, item.quality, item.binding,
         item.level, item.sell_price, item.fetched_at),
    )


def missing_ids(conn: sqlite3.Connection, ids: list[int],
                max_age_days: float = 90.0) -> list[int]:
    """Ids with no row, or a stale row.  Order preserved, deduped."""
    if not ids:
        return []
    uniq = list(dict.fromkeys(ids))
    q = f"SELECT itemId, fetchedAt FROM blizzard_items WHERE itemId IN ({','.join('?' * len(uniq))})"
    have = {r[0]: r[1] for r in conn.execute(q, uniq).fetchall()}
    try:
        gone = {r[0] for r in conn.execute(
            f"SELECT itemId FROM blizzard_missing WHERE itemId IN ({','.join('?' * len(uniq))})",
            uniq).fetchall()}
    except sqlite3.OperationalError:
        gone = set()
    now = datetime.now(timezone.utc)
    out = []
    for i in uniq:
        if i in gone:
            continue  # known 404: don't retry
        ts = have.get(i)
        if ts is None:
            out.append(i)
            continue
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds() / 86400
        except ValueError:
            age = 1e9
        if age > max_age_days:
            out.append(i)
    return out


def mark_missing(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "INSERT INTO blizzard_missing(itemId,failedAt) VALUES(?,?)"
        " ON CONFLICT(itemId) DO UPDATE SET failedAt=excluded.failedAt",
        (item_id, _utcnow()),
    )


def load_map(db_path: str) -> dict[int, BlizzardItem]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT itemId,name,quality,binding,level,sellPrice,fetchedAt"
            " FROM blizzard_items").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {r[0]: BlizzardItem(r[0], r[1] or "", r[2] or "", r[3] or "",
                               r[4] or 0, r[5] or 0, r[6] or "") for r in rows}
