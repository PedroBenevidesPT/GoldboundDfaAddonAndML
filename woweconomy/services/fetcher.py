"""Data fetching service — downloads TSM public data CSVs."""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from woweconomy.config import WowEconomyConfig
from woweconomy.models import SnapshotRecord

logger = logging.getLogger(__name__)

TSM_ENDPOINTS = {
    "realm_items": "{base}/{gameType}/{regionSlug}/realm/{realmSlug}/items.csv",
    "realm_pets": "{base}/{gameType}/{regionSlug}/realm/{realmSlug}/pets.csv",
    "commodities": "{base}/{gameType}/{regionSlug}/commodities.csv",
    "region_items": "{base}/{gameType}/{regionSlug}/region/items.csv",
    "region_pets": "{base}/{gameType}/{regionSlug}/region/pets.csv",
}


class TSMFetcher:
    """Download and parse TSM public data CSVs."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "WowEconomyEngine/0.1 (market analysis bot)",
        })

    def url_for(self, kind: str, realm_slug: Optional[str] = None) -> str:
        """Build the public-data URL for one of the five CSV kinds.

        ``realm_slug`` is percent-encoded because EU has slugs such as
        ``aggra-português`` and ``pozzo-delleternità``; the TSM CDN serves
        them, but only when the path is UTF-8 encoded.
        """
        if kind not in TSM_ENDPOINTS:
            raise ValueError(f"unknown TSM csv kind {kind!r}; "
                             f"known: {', '.join(TSM_ENDPOINTS)}")
        realm = realm_slug or self.config.realm_slug
        return TSM_ENDPOINTS[kind].format(
            base=self.config.tsm_base_url,
            gameType=self.config.game_type,
            regionSlug=self.config.region_slug,
            realmSlug=quote(realm, safe=""),
        )

    def fetch_items_csv(self) -> Tuple[str, List[Dict]]:
        """Fetch realm items CSV. Returns (checksum, rows)."""
        return self.fetch_kind("realm_items")

    def fetch_realm_items_csv(self, realm_slug: Optional[str] = None
                              ) -> Tuple[str, List[Dict]]:
        """Fetch one realm's items CSV (connected realms share one file)."""
        return self.fetch_kind("realm_items", realm_slug)

    def fetch_realm_pets_csv(self, realm_slug: Optional[str] = None
                             ) -> Tuple[str, List[Dict]]:
        """Fetch one realm's pets CSV (keyed by ``petSpeciesId``)."""
        return self.fetch_kind("realm_pets", realm_slug)

    def fetch_commodities_csv(self) -> Tuple[str, List[Dict]]:
        """Fetch the region-wide commodities CSV."""
        return self.fetch_kind("commodities")

    def fetch_region_items_csv(self) -> Tuple[str, List[Dict]]:
        """Fetch region-level items CSV."""
        return self.fetch_kind("region_items")

    def fetch_region_pets_csv(self) -> Tuple[str, List[Dict]]:
        """Fetch region-level pets CSV."""
        return self.fetch_kind("region_pets")

    def fetch_kind(self, kind: str, realm_slug: Optional[str] = None
                   ) -> Tuple[str, List[Dict]]:
        """Download and parse one CSV kind, returning (checksum, rows)."""
        checksum, content = self.fetch_raw(self.url_for(kind, realm_slug))
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        return checksum, list(reader)

    def fetch_raw(self, url: str) -> Tuple[str, bytes]:
        """Download a CSV body, returning (sha256, bytes) with retries."""
        last_error = None
        for attempt in range(self.config.max_retries):
            try:
                resp = self._session.get(url, timeout=self.config.fetch_timeout_seconds)
                resp.raise_for_status()
                content = resp.content
                checksum = hashlib.sha256(content).hexdigest()
                logger.info(f"Fetched {url}: {len(content):,} bytes, "
                            f"checksum={checksum[:16]}...")
                return checksum, content

            except requests.RequestException as e:
                last_error = e
                logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay_seconds)

        raise ConnectionError(f"Failed to fetch {url} after {self.config.max_retries} attempts: {last_error}")

    def _fetch_csv(self, url: str) -> Tuple[str, List[Dict]]:
        """Backward-compatible helper for callers that already built a URL."""
        checksum, content = self.fetch_raw(url)
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        return checksum, list(reader)

    def rows_to_snapshot_records(self, rows: List[Dict],
                                 snapshot_id: int) -> List[SnapshotRecord]:
        """Convert CSV rows to SnapshotRecord objects."""
        records = []
        for r in rows:
            try:
                records.append(SnapshotRecord(
                    snapshot_id=snapshot_id,
                    item_id=int(r.get("itemId", 0)),
                    market_value=float(r.get("marketValue", 0) or 0),
                    historical=float(r.get("historical", 0) or 0),
                    avg_sale_price=float(r.get("avgSalePrice", 0) or 0),
                    sale_rate=float(r.get("saleRate", 0) or 0),
                    sold_per_day=float(r.get("soldPerDay", 0) or 0),
                    snapshot_timestamp=r.get("updatedAt", datetime.now(timezone.utc).isoformat()),
                ))
            except (ValueError, TypeError) as e:
                logger.debug(f"Skipping row: {e}")
        return records

    def close(self):
        self._session.close()
