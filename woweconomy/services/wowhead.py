"""Wowhead zone scraper.

Workflow
    1. Discover lootable NPCs via the listing filter
       https://www.wowhead.com/npcs?filter=6:9;<zone>:1;0:0
       (only NPCs that can drop loot; zone page's NPC tab otherwise).
    2. Sweep EVERY listing page and accumulate the union of NPC ids,
       deduped.  A single page_number only covers ~100 NPCs, so
       single-page mode misses NPCs.
    3. For each unique NPC id, open the NPC detail page and inspect only
       the tabs that matter: drops, skinning, pickpocketing — including
       every page inside each tab (drop tables paginate too).
    4. A row counts if it actually carries an item link (<tr class="listview-row">
       with a[href*='item=']).  drop_rate is the pure Wowhead probability
       (e.g. "44 out of 65"); mean units per drop go to qty.  Rows without
       an item link are ignored and that category is skipped for the NPC.
    5. Persist the result to woweconomy/wowhead_zones/<zone>.json.

Run (one or many zones straight on the command line)
    python -m woweconomy.services.wowhead "zone=331/ashenvale"
    python -m woweconomy.services.wowhead "zone=331/ashenvale" "zone=.../azshara"
Flags (any position): "all" = sweep every listing page (default),
    <number> = single listing page only, "all-npcs" = skip lootable filter,
    "headless" = hide the browser window (default shows it: don't close it!),
    "no-cache" = ignore the per-NPC disk cache and re-scrape everything.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# WoWhead rate limits are aggressive.  Give the service breathing room.
REQUEST_DELAY_SECONDS = 2.0

# Per-NPC disk cache: one JSON per NPC so a killed run never loses
# scraped drops.  Entries older than this are re-scraped.
CACHE_DIR = Path("woweconomy/wowhead_zones/cache")
CACHE_MAX_AGE_DAYS = 30

# Lootable-NPC discovery trick: the NPC listing endpoint accepts a filter
# that returns only lootable NPCs for a zone, e.g.
#     https://www.wowhead.com/npcs?filter=6:9;331:1;0:0
# (zone 331 = Ashenvale).  The ";<zone>:1;" segment pins the zone, so we
# build it per zone instead of scraping the zone page's NPC tab.
NPC_FILTER_URL = "https://www.wowhead.com/npcs?filter=6:9;{zone}:1;0:0"

# ---------------------------------------------------------------
# Drop-row recognition
# ---------------------------------------------------------------
# Wowhead renders one <tr class="listview-row"> per drop inside the tab:
#
#     <a class="... listview-cleartext" href=".../item=14047/runecloth">
#     <span>44</span><div>out of 65</div>   -> rate 44/65 (client samples)
#     <span class="wh-icon-text">1-4</span>  -> 1-4 units per drop
#
# A row counts when it carries an item link (href .../item=<id>).  Rows
# without one (abilities, text) are ignored; a tab with no item rows
# means the NPC has nothing in that category and is skipped entirely.
DROP_LINK_RE = re.compile(r'data-item["\'\s]+(\d+)')
ITEM_ID_RE = re.compile(r'(?:item|item-id)["\'\s]*[:=]["\']?\s*(\d+)')


@dataclass
class DropRow:
    zone: int
    zone_name: str
    npc_id: int
    npc_name: str
    category: str  # "drops" | "skinning" | "pickpocketing"
    item_id: Optional[int]
    item_name: str = ""
    drop_rate: float = 0.0  # pure probability 0-1 (the Wowhead % field)
    qty: float = 1.0        # mean units per drop ("1-4" -> 2.5)
    item_type: str = ""     # Wowhead Type cell: Junk, Quest, Cloth Armor, ...
    source_url: str = ""


@dataclass
class ZoneScrape:
    zone: int
    zone_name: str
    npc_total: int
    rows: list[DropRow] = field(default_factory=list)
    spawns: dict[int, int] = field(default_factory=dict)


class WowheadZoneScraper:
    """Headful browser driver for Wowhead zone pages."""

    def __init__(self, delay: float = REQUEST_DELAY_SECONDS, headless: bool = False):
        self.delay = delay
        self.headless = headless
        self.spawn_counts: dict[int, int] = {}  # npc_id -> spawn points

    @staticmethod
    def parse_spawns(text: str, zone_name: str) -> int:
        """Spawn count for our zone from '#locations' text.

        e.g. "Ashenvale (44) Azshara (3)" + "ashenvale" -> 44.
        Falls back to the summed total when the zone is not named.
        """
        pairs = re.findall(r"([A-Za-z'’\- ]+?)\s*\((\d+)\)", text or "")
        if not pairs:
            return 0
        zl = zone_name.lower()
        for name, count in pairs:
            if zl and zl in name.strip().lower():
                return int(count)
        return sum(int(c) for _, c in pairs)

    @staticmethod
    def npc_spawns_from_page(page, zone_name: str) -> int:
        try:
            loc = page.locator("#locations")
            if loc.count() == 0:
                return 0
            return WowheadZoneScraper.parse_spawns(
                loc.first.inner_text() or "", zone_name)
        except Exception:
            return 0

    def scrape_zone(
        self,
        zone_slug: str,
        page_number: Optional[int] = None,
        page_size: int = 100,
        max_pages: int = 20,
        lootable_only: bool = True,
        headless: Optional[bool] = None,
        use_cache: bool = True,
    ) -> ZoneScrape:
        """Open a zone, grab the FULL NPC list across all pages, scrape drops.

        When lootable_only is True (default) NPC discovery uses the lootable
        filter listing (npcs?filter=6:9;<zone>:1;0:0), which returns only
        NPCs that can actually drop loot — no need to walk the zone page's
        NPC tab.  When False, every NPC-list page is visited
        (#npcs;<offset> with offset = 0, 100, 200, ...) and the union of NPC
        ids is scraped.  Pass a page_number for the old single-page behaviour.
        """
        zone = self._parse_zone(zone_slug)
        zone_name = self._parse_zone_name(zone_slug)
        if page_number is None:
            logger.info("Scraping zone %s (%s) ALL pages", zone, zone_name)
        else:
            logger.info("Scraping zone %s (%s) page %s", zone, zone_name, page_number)

        rows: list[DropRow] = []
        npc_ids: list[int] = []
        headless = self.headless if headless is None else headless
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            try:
                if lootable_only:
                    npc_ids = self._collect_lootable_npc_ids(
                        page, zone, max_pages=max_pages
                    )
                    logger.info(
                        "Found %d LOOTABLE NPCs via filter (zone %s)",
                        len(npc_ids), zone,
                    )
                else:
                    self._open_zone(page, zone_slug)
                    self._accept_cookies(page)
                    self._open_npcs_tab(page)

                    if page_number is not None:
                        self._open_npcs_page(page, zone_slug, page_number)
                        npc_ids = self._npc_ids_on_page(page)
                        logger.info("Found %d NPCs on page %s", len(npc_ids), page_number)
                    else:
                        npc_ids = self._collect_all_npc_ids(
                            page, zone_slug, page_size=page_size, max_pages=max_pages
                        )
                        logger.info("Found %d UNIQUE NPCs across all pages", len(npc_ids))

                for idx, npc_id in enumerate(npc_ids, start=1):
                    logger.info(
                        "Scraping NPC %s/%s (id=%s)", idx, len(npc_ids), npc_id
                    )
                    if use_cache:
                        cached = self._load_cached_npc(npc_id, zone)
                        if cached is not None:
                            logger.info(
                                "NPC %s served from cache (%d rows)", npc_id, len(cached)
                            )
                            rows.extend(cached)
                            continue
                    try:
                        fresh = self._scrape_npc(page, npc_id, zone, zone_name)
                    except Exception as exc:
                        # One bad NPC (or a closed browser target) must not
                        # kill the whole zone run.  If the browser itself is
                        # gone there is nothing more we can do — stop.
                        logger.warning("NPC %s failed: %r", npc_id, exc)
                        if "closed" in type(exc).__name__.lower() or "closed" in str(exc).lower():
                            break
                    else:
                        rows.extend(fresh)
                        try:
                            self._save_cached_npc(fresh, npc_id)
                        except Exception as exc:
                            logger.warning("Cache save failed: %r", exc)
                    self._rate_limit()
                    if rows and idx % 25 == 0:
                        # Checkpoint: an interrupt never loses the whole run.
                        try:
                            self.export(rows, Path("woweconomy/wowhead_zones"),
                                        self.spawn_counts)
                        except Exception as exc:
                            logger.warning("Checkpoint failed: %r", exc)
            finally:
                browser.close()

        return ZoneScrape(
            zone=zone,
            zone_name=zone_name,
            npc_total=len(npc_ids),
            rows=rows,
            spawns=dict(self.spawn_counts),
        )

    @staticmethod
    def export(rows: list[DropRow], out_dir: Path,
               spawns: Optional[dict[int, int]] = None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"zone_{rows[0].zone if rows else 0}.json"
        spawns = spawns or {}
        by_npc: dict[int, dict] = {}
        for r in rows:
            by_npc.setdefault(r.npc_id, {
                "npc_id": r.npc_id,
                "zone": r.zone,
                "name": r.zone_name,
                "npc_name": r.npc_name,
                "category": r.category,
                "spawns": spawns.get(r.npc_id, 0),
                "items": [],
            })
            by_npc[r.npc_id]["items"].append({
                "category": r.category,
                "item_id": r.item_id,
                "item_name": r.item_name,
                "item_type": r.item_type,
                "drop_rate": r.drop_rate,
                "qty": r.qty,
                "source_url": r.source_url,
            })
        path.write_text(
            json.dumps([v for v in by_npc.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Wrote %s (%d NPCs, %d rows)", path, len(by_npc), len(rows))
        return path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_path(npc_id: int) -> Path:
        return CACHE_DIR / f"npc_{npc_id}.json"

    @staticmethod
    def _load_cached_npc(npc_id: int, zone: int) -> Optional[list[DropRow]]:
        """Cached rows for this NPC, or None on miss/stale/foreign-zone."""
        path = WowheadZoneScraper._cache_path(npc_id)
        try:
            if not path.exists():
                return None
            age_days = (time.time() - path.stat().st_mtime) / 86400
            if age_days > CACHE_MAX_AGE_DAYS:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("zone") != zone:
                return None
            return [
                DropRow(
                    zone=data["zone"], zone_name=data.get("zone_name", ""),
                    npc_id=npc_id, npc_name=data.get("npc_name", ""),
                    category=it.get("category", "drops"),
                    item_id=it.get("item_id"), item_name=it.get("item_name", ""),
                    drop_rate=float(it.get("drop_rate", 0) or 0),
                    qty=float(it.get("qty", 1) or 1),
                    item_type=it.get("item_type", ""),
                    source_url=it.get("source_url", ""),
                )
                for it in data.get("items", [])
            ]
        except Exception:
            return None

    @staticmethod
    def _save_cached_npc(rows: list[DropRow], npc_id: int) -> None:
        if not rows:
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = WowheadZoneScraper._cache_path(npc_id)
        data = {
            "npc_id": npc_id,
            "zone": rows[0].zone,
            "zone_name": rows[0].zone_name,
            "npc_name": rows[0].npc_name,
            "items": [
                {"category": r.category, "item_id": r.item_id,
                 "item_name": r.item_name, "item_type": r.item_type,
                 "drop_rate": r.drop_rate,
                 "qty": r.qty, "source_url": r.source_url}
                for r in rows
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def _parse_zone(slug: str) -> int:
        m = re.search(r"zone=(\d+)/", slug)
        if not m:
            raise ValueError(f"Cannot parse zone id from {slug!r}")
        return int(m.group(1))

    @staticmethod
    def _parse_zone_name(slug: str) -> str:
        m = re.search(r"zone=\d+/([^/?#]+)", slug)
        return m.group(1) if m else slug

    @staticmethod
    def _accept_cookies(page):
        try:
            page.click("#onetrust-accept-btn-handler", timeout=5000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

    def _open_zone(self, page, zone_slug: str):
        page.goto(f"https://www.wowhead.com/{zone_slug}", timeout=90000)
        page.wait_for_timeout(9000)

    def _open_npcs_tab(self, page):
        tabs = page.locator('button[role="tab"]')
        for i in range(tabs.count()):
            if "npc" in tabs.nth(i).inner_text().lower():
                tabs.nth(i).click()
                page.wait_for_timeout(4000)
                break

    def _open_npcs_page(self, page, zone_slug: str, offset: int):
        page.goto(
            f"https://www.wowhead.com/{zone_slug}#npcs;{offset}",
            timeout=90000,
        )
        page.wait_for_timeout(7000)

    def _open_lootable_filter(self, page, zone: int):
        page.goto(NPC_FILTER_URL.format(zone=zone), timeout=90000)
        page.wait_for_timeout(7000)

    def _collect_lootable_npc_ids(
        self, page, zone: int, max_pages: int = 20
    ) -> list[int]:
        """Union of NPC ids from the lootable-filter listing (with paging)."""
        self._open_lootable_filter(page, zone)
        self._accept_cookies(page)
        seen: set[int] = set()
        ordered: list[int] = []
        for n in range(max_pages):
            ids = self._npc_ids_on_page(page)
            new = [i for i in ids if i not in seen]
            logger.info(
                "Lootable-filter page %s: %d ids (%d new)", n + 1, len(ids), len(new)
            )
            for i in new:
                seen.add(i)
                ordered.append(i)
            if not ids or not new:
                break
            if not self._click_tab_next(page):
                break
            page.wait_for_timeout(2500)
        return ordered

    def _collect_all_npc_ids(
        self, page, zone_slug: str, page_size: int = 100, max_pages: int = 20
    ) -> list[int]:
        """Visit every NPC-list offset and return the deduped union of ids."""
        seen: set[int] = set()
        ordered: list[int] = []
        empty_streak = 0
        for n in range(max_pages):
            offset = n * page_size
            self._open_npcs_page(page, zone_slug, offset)
            self._accept_cookies(page)
            ids = self._npc_ids_on_page(page)
            new = [i for i in ids if i not in seen]
            logger.info(
                "NPC page offset %s: %d ids (%d new)", offset, len(ids), len(new)
            )
            if not ids or not new:
                empty_streak += 1
                if empty_streak >= 2 or not ids:
                    # Two consecutive pages with nothing new (or a truly
                    # empty page) means we walked past the end.
                    if not ids:
                        break
                    # Peek one more page before giving up when the site
                    # repeats the last page instead of going empty.
                    if empty_streak >= 2:
                        break
                    continue
            empty_streak = 0
            for i in new:
                seen.add(i)
                ordered.append(i)
        return ordered

    def _npc_ids_on_page(self, page) -> list[int]:
        ids = page.eval_on_selector_all(
            "a[href*='npc=']", r"""
                els => {
                    const out = [];
                    for (const el of els) {
                        const m = el.href.match(/npc=(\d+)/);
                        if (m) out.push(parseInt(m[1], 10));
                    }
                    return out;
                }
            """
        )
        # dedupe, keep order
        seen: set[int] = set()
        ordered: list[int] = []
        for n in ids:
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        return ordered

    def _scrape_npc(self, page, npc_id: int, zone: int, zone_name: str) -> list[DropRow]:
        """Return every matching item row for this NPC (empty => no drops)."""
        # NPC detail page, e.g. https://www.wowhead.com/npc=66329/flutterby
        page.goto(f"https://www.wowhead.com/npc={npc_id}", timeout=90000)
        page.wait_for_timeout(5000)
        self._accept_cookies(page)
        npc_name = self._npc_name_from_page(page)
        try:
            self.spawn_counts[npc_id] = self.npc_spawns_from_page(page, zone_name)
        except Exception:
            pass

        rows: list[DropRow] = []

        for category in ("drops", "skinning", "pickpocketing"):
            tab = self._find_tab(page, category)
            if tab is None:
                continue
            try:
                self._dismiss_overlays(page)
                tab.scroll_into_view_if_needed(timeout=8000)
                tab.click(timeout=8000, force=True)
            except Exception as exc:
                # One stuck tab must not fail the whole NPC.
                logger.warning("NPC %s tab %s click failed: %r", npc_id, category, exc)
                continue
            page.wait_for_timeout(2500)
            self._accept_cookies(page)

            page_rows = self._rows_from_tab_all_pages(
                page, category, npc_id, zone, zone_name
            )
            if not page_rows:
                # No item rows on this tab -> this NPC has nothing for it.
                # Skip the category but keep going (next tab may still work).
                continue

            rows.extend(page_rows)
            self._rate_limit()

        for r in rows:
            r.npc_name = npc_name
        # One row per (npc, item): same item across categories keeps the
        # best hit, categories merged ("drops+skinning").
        best: dict[int, DropRow] = {}
        for r in rows:
            key = r.item_id or 0
            cur = best.get(key)
            if cur is None or r.drop_rate * r.qty > cur.drop_rate * cur.qty:
                if cur is not None and cur.category not in r.category:
                    r.category = "+".join(sorted(set(
                        (cur.category + "+" + r.category).split("+"))))
                best[key] = r
            elif cur is not None and r.category not in cur.category:
                cur.category = "+".join(sorted(set(
                    (cur.category + "+" + r.category).split("+"))))
        return list(best.values())

    @staticmethod
    def _npc_name_from_page(page) -> str:
        """NPC name from the detail page title ('Name - NPC - World of ...')."""
        for sel in ("h1.heading-size-1", "h1"):
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    name = loc.first.inner_text().strip()
                    if name:
                        return name
            except Exception:
                continue
        try:
            return (page.title() or "").split(" - ")[0].strip()
        except Exception:
            return ""

    @staticmethod
    def _find_tab(page, category: str):
        tabs = page.locator('button[role="tab"]')
        for i in range(tabs.count()):
            try:
                if not tabs.nth(i).is_visible():
                    continue
                t = tabs.nth(i).inner_text().lower()
            except Exception:
                continue
            if category in t:
                return tabs.nth(i)
        return None

    @staticmethod
    def _dismiss_overlays(page):
        """Best-effort removal of popups that intercept tab clicks."""
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        for sel in (
            "#newsletter-cta-wrapper .newsletter-cta-close",
            ".newsletter-cta-overlay",
            "[id*='newsletter'] button",
            ".onetrust-close-btn-handler",
        ):
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=2000, force=True)
            except Exception:
                continue

    @staticmethod
    def _rows_from_tab_all_pages(
        page, category: str, npc_id: int, zone: int, zone_name: str,
        max_tab_pages: int = 10,
    ) -> list[DropRow]:
        """Collect item rows from a tab, walking its inner pagination."""
        seen_ids: set[int] = set()
        merged: list[DropRow] = []
        for _ in range(max_tab_pages):
            chunk = WowheadZoneScraper._rows_from_page(
                page, category, npc_id, zone, zone_name
            )
            fresh = [r for r in chunk if (r.item_id or 0) not in seen_ids]
            for r in fresh:
                seen_ids.add(r.item_id or 0)
            merged.extend(fresh)
            if not WowheadZoneScraper._click_tab_next(page):
                break
            page.wait_for_timeout(2500)
        # Collapse duplicate (item_id) rows: keep the highest rate observed.
        best: dict[int, DropRow] = {}
        for r in merged:
            key = r.item_id or 0
            if key not in best or r.drop_rate > best[key].drop_rate:
                best[key] = r
        return list(best.values())

    @staticmethod
    def _click_tab_next(page) -> bool:
        """Click a 'Next' pagination control inside the active tab, if any."""
        candidates = [
            ".listview-pagination a.next",
            ".listview-nav a.next",
            "a.listview-next",
            "a:has-text('Next »')",
            "a:has-text('Next')",
            "button:has-text('Next')",
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    first = loc.first
                    try:
                        if not first.is_visible() or not first.is_enabled():
                            continue
                    except Exception:
                        pass
                    try:
                        cls = (first.get_attribute("class") or "").lower()
                        aria = (first.get_attribute("aria-disabled") or "").lower()
                        if "disabled" in cls or aria == "true":
                            continue
                    except Exception:
                        pass
                    first.click(timeout=5000)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _rows_from_page(
        page, category: str, npc_id: int, zone: int, zone_name: str
    ) -> list[DropRow]:
        """Return item rows from the active tab.  Empty when the tab has none.

        Wowhead renders one <tr class="listview-row"> per drop:
            <a class="... listview-cleartext" href=".../item=14047/runecloth">
            <span ...>44</span><div ...>out of 65</div>   -> rate 44/65
            <span class="wh-icon-text">1-4</span>          -> 1-4 units per drop
        drop_rate stores the pure Wowhead probability 0-1 (the % field,
        e.g. "44 out of 65" -> 0.677).  Mean units per drop go to qty.
        """
        rows: list[DropRow] = []

        # One entry per listview row in the visible tab body.
        links = page.eval_on_selector_all(
            ".listview-row", """
                els => els.map(row => {
                    const links = [...row.querySelectorAll("a[href*='item=']")];
                    const a = row.querySelector('a.listview-cleartext')
                        || links.find(x => x.innerText.trim())
                        || links[0];
                    const q = row.querySelector('.wh-icon-text');
                    const tds = [...row.querySelectorAll('td')];
                    const ty = row.querySelector("a[href*='/items=']");
                    return {
                        href: a ? a.href : '',
                        name: a ? a.innerText.trim() : '',
                        qty: q ? q.innerText.trim() : '',
                        text: row.innerText,
                        pct: tds.length ? tds[tds.length - 1].innerText.trim() : '',
                        itype: ty ? ty.innerText.trim() : '',
                    };
                }).filter(r => r.href)
            """
        )
        if not links:
            return rows

        for link in links:
            item_id = WowheadZoneScraper._item_id_from_el(link["href"])
            if item_id is None or not link["name"]:
                continue
            rate = WowheadZoneScraper._rate_from_row(link["text"], link.get("pct", ""))
            if rate is None or rate <= 0:
                continue
            qty = WowheadZoneScraper._qty_from_text(link["qty"])
            rows.append(
                DropRow(
                    zone=zone,
                    zone_name=zone_name,
                    npc_id=npc_id,
                    npc_name="",
                    category=category,
                    item_id=item_id,
                    item_name=link["name"],
                    drop_rate=rate,
                    qty=qty,
                    item_type=link.get("itype", ""),
                    source_url=link["href"],
                )
            )

        # Collapse duplicate (item_id) rows: keep the highest rate observed.
        best: dict[int, DropRow] = {}
        for r in rows:
            key = r.item_id or 0
            if key not in best or r.drop_rate > best[key].drop_rate:
                best[key] = r
        return list(best.values())

    @staticmethod
    def _item_id_from_el(href: str) -> Optional[int]:
        m = re.search(r"item=(\d+)", href)
        if m:
            return int(m.group(1))
        m = re.search(r"/items/(\d+)", href)
        return int(m.group(1)) if m else None

    @staticmethod
    def _rate_from_row(text: str, pct: str = "") -> Optional[float]:
        """Rate for a listview row.  Primary: the % column Wowhead already
        computed (e.g. "68" next to "44 out of 65").  Fallbacks: the sample
        fraction itself, then a trailing number.  A fraction with num > den
        is a misparse -> fall through, never clamp.
        """
        if pct and "out of" in (text or ""):
            m = re.search(r"(\d+(?:[.,]\d+)?)", pct)
            if m:
                v = float(m.group(1).replace(",", "."))
                if 0 < v <= 100:
                    return v / 100.0
        norm = re.sub(r"(?<=\d)[\s\u00a0\u2009\u202f]+(?=\d{3}(?!\d))", "", text or "")
        m = re.search(r"(\d+)\s*out of\s*(\d+)", norm)
        if m:
            num, den = int(m.group(1)), int(m.group(2))
            if den > 0 and 0 <= num <= den:
                return num / den
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*%?\s*$", norm.strip())
        if m:
            v = float(m.group(1).replace(",", "."))
            if 0 < v <= 100:
                return v / 100.0
        return None

    @staticmethod
    def _qty_from_text(qty: str) -> float:
        """Mean units per drop from e.g. "1-4" -> 2.5, "2" -> 2, "" -> 1."""
        m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", qty or "")
        if m:
            return (float(m.group(1)) + float(m.group(2))) / 2.0
        m = re.search(r"(\d+(?:\.\d+)?)", qty or "")
        return float(m.group(1)) if m else 1.0

    @staticmethod
    def _rate_from_el(text: str) -> Optional[float]:
        """Parse the drop/spin rate out of a row's link text."""
        text = text.strip()

        # Slash-format rates (e.g. 100/200 -> 50%, 52/100 -> 52%).
        m = re.search(r"(?:^|/)(\d{2,3})/(\d{2,3})(?![/\d])", text)
        if m:
            num, den = int(m.group(1)), int(m.group(2))
            if den:
                rate = num / den
                if 0 < rate <= 1:
                    return rate

        # Direct percent (e.g. 320 turns, 100%).
        m = re.search(r"(\d{2,3})(?:\s*%|\s*turns?)?", text)
        if m:
            v = float(m.group(1))
            if 0 < v <= 100:
                return v / 100.0

        return None

    @staticmethod
    def _item_name(text: str) -> str:
        """Trim a trailing rate from the item display text."""
        return re.split(r"\s+[-–—]\s+|\s+-\s+", text + " ")[0].strip()

    def _rate_limit(self):
        time.sleep(self.delay)


def scrape_zone(
    zone_slug: str,
    page_number: Optional[int] = None,
    page_size: int = 100,
    max_pages: int = 20,
    lootable_only: bool = True,
    headless: bool = False,
    use_cache: bool = True,
) -> ZoneScrape:
    """Synchronous convenience wrapper.  page_number=None sweeps all pages."""
    scraper = WowheadZoneScraper(headless=headless)
    return scraper.scrape_zone(
        zone_slug,
        page_number=page_number,
        page_size=page_size,
        max_pages=max_pages,
        lootable_only=lootable_only,
        headless=headless,
        use_cache=use_cache,
    )


if __name__ == "__main__":
    # Zones go straight on the command line, one or many:
    #   python -m woweconomy.services.wowhead "zone=331/ashenvale"
    #   python -m woweconomy.services.wowhead "zone=331/ashenvale" "zone=.../azshara"
    # Flags (any position): "all" (sweep every listing page, default),
    #   <number> (single listing page only), "all-npcs" (no lootable filter),
    #   "headless" (hide the browser window).
    logging.basicConfig(level=logging.INFO)
    raw = sys.argv[1:] or ["zone=331/ashenvale"]
    slopes = [a for a in raw if "zone=" in a]
    flags = [a.lower() for a in raw if "zone=" not in a]
    page: Optional[int] = None
    for f in flags:
        if f != "all" and f != "all-npcs" and f != "headless" and f != "no-cache":
            page = int(f)  # single-page mode
    lootable = "all-npcs" not in flags
    headless = "headless" in flags
    use_cache = "no-cache" not in flags
    results: list[ZoneScrape] = []
    for slash in slopes:
        result = scrape_zone(slash, page, lootable_only=lootable,
                             headless=headless, use_cache=use_cache)
        results.append(result)
        print(
            f"Zone {result.zone} ({result.zone_name}): "
            f"{result.npc_total} NPCs, {len(result.rows)} rows"
        )
        for r in result.rows[:10]:
            print(
                f"  {r.npc_id} {r.npc_name} [{r.category}] -> item {r.item_id} "
                f"{r.item_name} ({r.drop_rate:.0%})"
            )
        if result.rows:
            WowheadZoneScraper.export(result.rows, Path("woweconomy/wowhead_zones"),
                                      result.spawns)
    print(f"Done: {len(results)} zone(s), "
          f"{sum(r.npc_total for r in results)} NPCs, "
          f"{sum(len(r.rows) for r in results)} rows")
