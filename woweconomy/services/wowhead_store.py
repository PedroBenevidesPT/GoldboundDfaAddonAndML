"""Wowhead zone drops in the one shared store.

The scraper used to stop at ``woweconomy/wowhead_zones/zone_<id>.json``.  That
is a fine raw artefact -- it is what a killed run leaves behind -- but it made
the zone files a second source of truth: the farm ranking read them, the
importer read them, and the Postgres database (which every other part of the
pipeline uses) knew nothing about the drops.

This module is the canonical table, on whatever backend is configured:

* ``wowhead_zone``  one row per scraped zone: when, how many NPCs, from where;
* ``wowhead_drop``  one row per (zone, NPC, item): the drop rate, mean units,
                    category and the URL the row was read from.

Postgres owns the schema through Alembic (``007_wowhead_drops``); SQLite
creates the same tables here so a workstation and the test suite keep working
without a migration runner.  A legacy SQLite file with no drop table is not an
error: :func:`load_drops` returns an empty list and the caller can fall back to
the JSON it has always read.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from woweconomy.dbcompat import (
    connect_store, is_postgres, missing_table_errors, project_path,
)

#: Where the TSM/DB2 tables live on a workstation.  Ignored on Postgres.
DEFAULT_DB = project_path("WOW_WOWHEAD_DB", "tsm_markets.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wowhead_zone (
    zone_id     INTEGER NOT NULL PRIMARY KEY,
    zone_name   TEXT NOT NULL DEFAULT '',
    npc_total   INTEGER NOT NULL DEFAULT 0,
    scraped_at  INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'wowhead'
);
CREATE TABLE IF NOT EXISTS wowhead_drop (
    zone_id     INTEGER NOT NULL,
    npc_id      INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    zone_name   TEXT NOT NULL DEFAULT '',
    npc_name    TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    item_name   TEXT NOT NULL DEFAULT '',
    item_type   TEXT NOT NULL DEFAULT '',
    drop_rate   REAL NOT NULL DEFAULT 0,
    qty         REAL NOT NULL DEFAULT 1,
    spawns      INTEGER NOT NULL DEFAULT 0,
    source_url  TEXT NOT NULL DEFAULT '',
    scraped_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (zone_id, npc_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_wowhead_drop_item ON wowhead_drop(item_id);
CREATE INDEX IF NOT EXISTS idx_wowhead_drop_npc ON wowhead_drop(npc_id);
"""


def ensure_schema(con) -> None:
    """Create the tables on SQLite; do nothing on Postgres (Alembic owns it)."""
    if is_postgres():
        return
    con.executescript(SCHEMA)
    con.commit()


def _value(row: Any, name: str, default: Any = None) -> Any:
    """Read a field from a mapping or from a ``DropRow``-like object."""
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def save_zone(rows: Iterable[Any], *, zone_id: Optional[int] = None,
              zone_name: str = "", npc_total: Optional[int] = None,
              spawns: Optional[Mapping[int, int]] = None,
              source: str = "wowhead",
              db_path: Path | str = DEFAULT_DB,
              now: Optional[int] = None) -> Dict[str, int]:
    """Replace one zone's drops with ``rows``.

    Replace, not merge: a rescan that no longer finds an item must remove the
    stale row, or the farm ranking keeps pricing a drop nobody has seen since.
    The whole write is one transaction on Postgres, so a reader sees either
    the previous scan or the new one, never a half-deleted zone.
    """
    rows = list(rows)
    if zone_id is None:
        zone_id = _int(_value(rows[0], "zone", 0)) if rows else 0
    zone_id = _int(zone_id)
    if zone_id <= 0:
        raise ValueError("a wowhead zone needs a positive zone_id")
    if not zone_name:
        zone_name = str(_value(rows[0], "zone_name", "")) if rows else ""
    scraped_at = int(time.time() if now is None else now)
    spawns = spawns or {}

    # One row per (npc, item).  The scraper already collapses these, but a
    # caller importing two JSON files for the same zone must not hit the PK.
    best: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        item_id = _int(_value(row, "item_id"))
        if item_id <= 0:
            continue
        npc_id = _int(_value(row, "npc_id"))
        key = (npc_id, item_id)
        current = best.get(key)
        drop_rate = _float(_value(row, "drop_rate"))
        if current is not None and current["drop_rate"] >= drop_rate:
            continue
        best[key] = {
            "npc_id": npc_id,
            "item_id": item_id,
            "npc_name": str(_value(row, "npc_name", "") or ""),
            "category": str(_value(row, "category", "") or ""),
            "item_name": str(_value(row, "item_name", "") or ""),
            "item_type": str(_value(row, "item_type", "") or ""),
            "drop_rate": drop_rate,
            "qty": _float(_value(row, "qty"), 1.0) or 1.0,
            "spawns": _int(spawns.get(npc_id, _value(row, "spawns", 0))),
            "source_url": str(_value(row, "source_url", "") or ""),
        }

    con = connect_store(db_path)
    try:
        ensure_schema(con)
        con.execute(
            "INSERT INTO wowhead_zone"
            " (zone_id, zone_name, npc_total, scraped_at, source)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(zone_id) DO UPDATE SET"
            " zone_name=excluded.zone_name, npc_total=excluded.npc_total,"
            " scraped_at=excluded.scraped_at, source=excluded.source",
            (zone_id, zone_name, _int(npc_total, len(best)),
             scraped_at, source))
        con.execute("DELETE FROM wowhead_drop WHERE zone_id = ?", (zone_id,))
        con.executemany(
            "INSERT INTO wowhead_drop"
            " (zone_id, npc_id, item_id, zone_name, npc_name, category,"
            "  item_name, item_type, drop_rate, qty, spawns, source_url,"
            "  scraped_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(zone_id, row["npc_id"], row["item_id"], zone_name,
              row["npc_name"], row["category"], row["item_name"],
              row["item_type"], row["drop_rate"], row["qty"], row["spawns"],
              row["source_url"], scraped_at) for row in best.values()])
        con.commit()
    finally:
        con.close()
    return {"zone_id": zone_id, "rows": len(best), "scraped_at": scraped_at}


def load_drops(zone_id: Optional[int] = None,
               db_path: Path | str = DEFAULT_DB) -> List[Dict[str, Any]]:
    """Every stored drop, or one zone's, in the shape the ranking speaks.

    Empty when the table does not exist yet: a legacy SQLite file predates
    this table, and that is not an error.
    """
    try:
        con = connect_store(db_path, readonly=True)
        try:
            sql = ("SELECT zone_id, zone_name, npc_id, npc_name, category,"
                   " item_id, item_name, item_type, drop_rate, qty, spawns,"
                   " source_url FROM wowhead_drop")
            params: tuple = ()
            if zone_id is not None:
                sql += " WHERE zone_id = ?"
                params = (_int(zone_id),)
            sql += " ORDER BY zone_id, npc_id, item_id"
            rows = con.execute(sql, params).fetchall()
        finally:
            con.close()
    except missing_table_errors():
        return []
    keys = ("zone_id", "zone_name", "npc_id", "npc_name", "category",
            "item_id", "item_name", "item_type", "drop_rate", "qty",
            "spawns", "source_url")
    return [dict(zip(keys, row)) for row in rows]


def import_json(paths: Iterable[Path], *,
                db_path: Path | str = DEFAULT_DB,
                now: Optional[int] = None) -> Dict[str, int]:
    """Import existing ``zone_*.json`` files into the store.

    This is the one-time bridge for scrapes that happened before the database
    existed.  It uses the same :func:`save_zone` as a live scrape, so imported
    and freshly scraped rows cannot differ.
    """
    total = {"zones": 0, "rows": 0}
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = list(data.values()) if isinstance(data, dict) else list(data)
        if not entries:
            continue
        zone_id = _int(entries[0].get("zone", 0))
        zone_name = str(entries[0].get("name", entries[0].get("zone_name", "")))
        rows: List[Dict[str, Any]] = []
        spawns: Dict[int, int] = {}
        for entry in entries:
            npc_id = _int(entry.get("npc_id"))
            spawns[npc_id] = _int(entry.get("spawns", 0))
            for item in entry.get("items", []):
                rows.append({
                    "npc_id": npc_id,
                    "npc_name": entry.get("npc_name", ""),
                    "zone": zone_id,
                    "zone_name": zone_name,
                    "category": item.get("category", ""),
                    "item_id": item.get("item_id"),
                    "item_name": item.get("item_name", ""),
                    "item_type": item.get("item_type", ""),
                    "drop_rate": item.get("drop_rate", 0),
                    "qty": item.get("qty", 1),
                    "source_url": item.get("source_url", ""),
                })
        result = save_zone(rows, zone_id=zone_id, zone_name=zone_name,
                           npc_total=len(entries), spawns=spawns,
                           source="wowhead-json-import", db_path=db_path,
                           now=now)
        total["zones"] += 1
        total["rows"] += result["rows"]
    return total
