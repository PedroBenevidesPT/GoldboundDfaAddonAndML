"""Repair a zone JSON scraped without NPC names.

- Fills empty npc_name via a fast title-only pass over NPC detail pages.
- Backfills per-item category from the entry when missing.
- One row per (npc, item): keeps the best rate x qty, merges categories.
- Drops junk: rate <= 0 or no market price (avgSalePrice/marketValue <= 0).
- Seeds the per-NPC disk cache so reruns skip scraped NPCs.

Run AFTER the zone scrape finishes (separate browser session):
    python scripts/repair_zone.py woweconomy/wowhead_zones/zone_331.json [--db tsm_markets.db]
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import sys
sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

from woweconomy.services.wowhead import WowheadZoneScraper

logger = logging.getLogger(__name__)


def main(path: str, db: str = "") -> None:
    from woweconomy.analytics.farm_ranking import load_csv_market, load_latest_market
    p = Path(path)
    entries = json.loads(p.read_text(encoding="utf-8"))
    market = load_latest_market(db) if db else load_csv_market("csv/items.csv")
    print(f"Market: {len(market)} priced items")
    scraper = WowheadZoneScraper(delay=1.0)

    need_names = [e for e in entries if not e.get("npc_name") and e.get("npc_id")]
    names: dict[int, str] = {}
    if need_names:
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
                for i, e in enumerate(need_names, 1):
                    nid = int(e["npc_id"])
                    try:
                        page.goto(f"https://www.wowhead.com/npc={nid}", timeout=90000)
                        page.wait_for_timeout(4000)
                        scraper._accept_cookies(page)
                        names[nid] = scraper._npc_name_from_page(page)
                    except Exception as exc:
                        logger.warning("name fetch npc=%s failed: %r", nid, exc)
                        names[nid] = ""
                    if i % 10 == 0:
                        print(f"names {i}/{len(need_names)}")
                    time.sleep(1.0)
            finally:
                browser.close()

    fixed = 0
    for e in entries:
        nid = int(e.get("npc_id", 0))
        if not e.get("npc_name") and nid in names:
            e["npc_name"] = names[nid]
            fixed += 1
        # one row per (npc, item) + drop junk (rate 0 / no market price)
        best: dict[int, dict] = {}
        for it in e.get("items", []):
            if not it.get("category"):
                it["category"] = e.get("category", "drops")
            rate = float(it.get("drop_rate", 0) or 0)
            if rate <= 0:
                continue
            m = market.get(int(it.get("item_id", 0) or 0))
            if m is None or m.price <= 0:
                continue
            key = it.get("item_id")
            score = rate * float(it.get("qty", 1) or 1)
            prev = best.get(key)
            if prev is None:
                best[key] = it
                continue
            prev_score = float(prev.get("drop_rate", 0) or 0) * float(prev.get("qty", 1) or 1)
            if score > prev_score:
                if prev.get("category") not in it.get("category", ""):
                    it["category"] = "+".join(sorted(set(
                        (prev.get("category", "") + "+" + it.get("category", "")).split("+"))))
                best[key] = it
            elif it.get("category") not in prev.get("category", ""):
                prev["category"] = "+".join(sorted(set(
                    (prev.get("category", "") + "+" + it.get("category", "")).split("+"))))
        e["items"] = list(best.values())
        # seed cache
        if nid and e.get("items"):
            WowheadZoneScraper._save_cached_npc(
                [__import__("woweconomy.services.wowhead", fromlist=["DropRow"]).DropRow(
                    zone=int(e.get("zone", 0)), zone_name=e.get("name", ""),
                    npc_id=nid, npc_name=e.get("npc_name", ""),
                    category=it.get("category", "drops"), item_id=it.get("item_id"),
                    item_name=it.get("item_name", ""),
                    drop_rate=float(it.get("drop_rate", 0) or 0),
                    qty=float(it.get("qty", 1) or 1),
                    item_type=it.get("item_type", ""),
                    source_url=it.get("source_url", "")) for it in e["items"]],
                nid)

    p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    # A repaired file must not become a second, better copy of the database.
    # The same rows go to the configured store, replacing the zone, so the
    # ranking reads the repair instead of quietly reading the old scan.
    try:
        from woweconomy.services import wowhead_store

        rows = []
        spawns = {}
        for e in entries:
            nid = int(e.get("npc_id", 0) or 0)
            spawns[nid] = int(e.get("spawns", 0) or 0)
            for it in e.get("items", []):
                rows.append({
                    "npc_id": nid, "npc_name": e.get("npc_name", ""),
                    "zone": int(e.get("zone", 0) or 0),
                    "zone_name": e.get("name", ""),
                    "category": it.get("category", ""),
                    "item_id": it.get("item_id"),
                    "item_name": it.get("item_name", ""),
                    "item_type": it.get("item_type", ""),
                    "drop_rate": it.get("drop_rate", 0),
                    "qty": it.get("qty", 1),
                    "source_url": it.get("source_url", ""),
                })
        saved = wowhead_store.save_zone(
            rows, zone_id=int(entries[0].get("zone", 0) or 0),
            zone_name=entries[0].get("name", ""), npc_total=len(entries),
            spawns=spawns, source="wowhead-repair")
        print(f"Stored zone {saved['zone_id']}: {saved['rows']} repaired rows")
    except Exception as exc:
        logger.warning("could not store the repaired zone in the database: %r",
                       exc)
    print(f"Repaired {p}: {len(entries)} NPCs, {fixed} names filled, cache seeded")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    from woweconomy.dbcompat import load_env

    load_env()
    _path = sys.argv[1] if len(sys.argv) > 1 else "woweconomy/wowhead_zones/zone_331.json"
    _db = sys.argv[2] if len(sys.argv) > 2 else ""
    main(_path, _db)
