"""Backfill per-NPC spawn counts (#locations) into zone JSONs.

Fast pass: one page load per NPC, reads the #locations element only
(e.g. "Ashenvale (44)"), no tab scraping.  Run once per zone file;
entries that already have spawns are skipped (resumable).

    python scripts/backfill_spawns.py
    python scripts/backfill_spawns.py woweconomy/wowhead_zones/zone_16.json
"""

from __future__ import annotations

import glob
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

from woweconomy.services.wowhead import WowheadZoneScraper

logger = logging.getLogger(__name__)


def main(paths: list[str]) -> None:
    files = [Path(p) for p in paths] or \
        [Path(p) for p in sorted(glob.glob("woweconomy/wowhead_zones/zone_*.json"))]
    scraper = WowheadZoneScraper(delay=0.5)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"))
        try:
            for path in files:
                entries = json.loads(path.read_text(encoding="utf-8"))
                todo = [e for e in entries if not e.get("spawns")
                        and e.get("npc_id")]
                print(f"{path}: {len(todo)}/{len(entries)} missing spawns")
                for i, e in enumerate(todo, 1):
                    nid = int(e["npc_id"])
                    try:
                        page.goto(f"https://www.wowhead.com/npc={nid}",
                                  timeout=90000)
                        page.wait_for_timeout(4000)
                        scraper._accept_cookies(page)
                        e["spawns"] = scraper.npc_spawns_from_page(
                            page, e.get("name", ""))
                    except Exception as exc:
                        logger.warning("spawns npc=%s failed: %r", nid, exc)
                    if i % 25 == 0:
                        path.write_text(json.dumps(entries, ensure_ascii=False,
                                                   indent=2), encoding="utf-8")
                        print(f"  checkpoint {i}/{len(todo)}")
                    time.sleep(0.5)
                path.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                print(f"Done {path}")
        finally:
            browser.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main(sys.argv[1:])
