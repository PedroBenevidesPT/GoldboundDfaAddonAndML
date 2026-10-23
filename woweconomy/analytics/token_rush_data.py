"""Data access for the token rush engine.

Everything here is read-only and comes from the same sources the rest of the
project already uses:

    inventory.csv            the account's inventory (TSM export)
    tsm_realm_price         the local state of every realm (market value,
                            cheapest local listing, recent, historical)
    tsm_region_price        the regional reference (market, historical,
                            average sale, sale rate, sold per day)
    tsm_market              the live auction count, for the 12 realms the TSM
                            app downloaded
    tsm_commodity_price     the materials market (region wide, one market)
    journal_stats           the account's own realized sales and expirations
    blizzard_items          the vendor sell price (for the deposit cap)

Nothing here interprets the data: the interpretation lives in
``token_rush`` and the realm posture lives in ``mog_realm``.
"""
from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

COPPER_PER_GOLD = 10_000

# TSM writes 99999999999 copper (9 999 999,99 g) as its "no data" sentinel.
# Such a value is not a price: treating it as one turns a recipe with no
# market into a 10 million gold asset and the whole campaign collapses into
# it.  Everything at or above this value is read as *missing*.
SENTINEL_COPPER = 99_999_999_000


def clean(value: int | float | None) -> int:
    """Zero for a missing value or for TSM's sentinel price."""
    if not value:
        return 0
    value = int(value)
    if value >= SENTINEL_COPPER:
        return 0
    return value


@dataclass(frozen=True)
class InventoryItem:
    item_id: int
    name: str
    qty: int
    quality: str = ""


@dataclass(frozen=True)
class RealmPrice:
    market: int = 0
    min_buyout: int = 0
    recent: int = 0
    historical: int = 0


@dataclass(frozen=True)
class RegionPrice:
    market: int = 0
    historical: int = 0
    avg_sale: int = 0
    sale_rate: float = 0.0
    sold_per_day: float = 0.0


def read_inventory(path: str | Path) -> dict[int, InventoryItem]:
    """Read the TSM inventory export (``;`` or ``,`` separated)."""
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        rows = list(csv.DictReader(fh, delimiter=delim))

    head = {k.strip().lower(): k for k in (rows[0] if rows else {})}

    def pick(*names: str) -> str:
        for name in names:
            if name in head:
                return head[name]
        return ""

    id_col = pick("itemid", "item_id", "id")
    qty_col = pick("quantity", "qty", "count")
    name_col = pick("name", "nome")
    quality_col = pick("quality")
    if not id_col:
        raise ValueError(f"inventário sem coluna de id: {path}")

    out: dict[int, InventoryItem] = {}
    for row in rows:
        raw = (row.get(id_col) or "").strip()
        try:
            item_id = int(float(raw))
        except (TypeError, ValueError):
            continue
        if item_id <= 0:
            continue
        qty = 1
        if qty_col:
            try:
                qty = max(1, int(float((row.get(qty_col) or "1").replace(",", "."))))
            except ValueError:
                qty = 1
        name = (row.get(name_col) or "").strip() if name_col else ""
        quality = (row.get(quality_col) or "").strip() if quality_col else ""
        old = out.get(item_id)
        if old:
            out[item_id] = InventoryItem(item_id, old.name or name, old.qty + qty,
                                         old.quality or quality)
        else:
            out[item_id] = InventoryItem(item_id, name, qty, quality)
    return out


def load_realm_prices(con: sqlite3.Connection, scope: str,
                      items: Sequence[int] | None = None) -> dict[int, RealmPrice]:
    """The local state of one realm for the given items."""
    sql = ("SELECT item_id, market_value, min_buyout, recent, historical "
           "FROM tsm_realm_price WHERE scope=?")
    params: list = [scope]
    if items:
        sql += f" AND item_id IN ({','.join('?' * len(items))})"
        params.extend(items)
    out = {}
    for item, market, min_buyout, recent, historical in con.execute(sql, params):
        out[int(item)] = RealmPrice(clean(market), clean(min_buyout),
                                     clean(recent), clean(historical))
    return out


def load_region_prices(con: sqlite3.Connection, items: Sequence[int]) -> dict[int, RegionPrice]:
    """The regional reference for the given items (latest snapshot)."""
    if not items:
        return {}
    ph = ",".join("?" * len(items))
    latest = con.execute("select max(snapshot_id) from tsm_region_price").fetchone()[0]
    out = {}
    for item, market, historical, avg_sale, rate, per_day in con.execute(
        f"SELECT key_id, market_value, historical, avg_sale_price, sale_rate, sold_per_day "
        f"FROM tsm_region_price WHERE snapshot_id=? AND key_id IN ({ph})",
        [latest, *items],
    ):
        out[int(item)] = RegionPrice(clean(market), clean(historical),
                                     clean(avg_sale), float(rate or 0.0),
                                     float(per_day or 0.0))
    return out


def load_market_counts(con: sqlite3.Connection) -> dict[str, dict[int, int]]:
    """Live auction counts per realm name (only the scanned realms have them)."""
    out: dict[str, dict[int, int]] = defaultdict(dict)
    for realm, item, num in con.execute("SELECT realm, itemId, num_auctions FROM tsm_market"):
        out[realm][int(item)] = int(num or 0)
    return dict(out)


def load_commodity_ids(con: sqlite3.Connection, items: Sequence[int]) -> set[int]:
    if not items:
        return set()
    ph = ",".join("?" * len(items))
    return {int(r[0]) for r in con.execute(
        f"SELECT key_id FROM tsm_commodity_price WHERE key_id IN ({ph})", list(items))}


def load_vendor_sell(con: sqlite3.Connection, items: Sequence[int]) -> dict[int, int]:
    if not items:
        return {}
    items = list(items)
    if len(items) > 900:
        # SQLite limits the number of placeholders: for the whole catalogue it
        # is cheaper to read the table (1.8k rows) than to split the query.
        return {int(i): int(v) for i, v in con.execute(
            "SELECT itemId, sellPrice FROM blizzard_items WHERE sellPrice>0")}
    ph = ",".join("?" * len(items))
    return {int(i): int(v) for i, v in con.execute(
        f"SELECT itemId, sellPrice FROM blizzard_items WHERE itemId IN ({ph})", items)}


def load_journal(con: sqlite3.Connection, items: Sequence[int]) -> dict[tuple[int, str], dict]:
    """The account's own realized sales and expirations, per item and realm."""
    if not items:
        return {}
    ph = ",".join("?" * len(items))
    out: dict[tuple[int, str], dict] = {}
    for item, realm, successes, failures, sold_count, sold_copper in con.execute(
        f"SELECT itemId, realm, successes, failures, sold_count, sold_copper "
        f"FROM journal_stats WHERE itemId IN ({ph})", list(items),
    ):
        key = (int(item), realm or "")
        out[key] = {
            "sales": int(sold_count or 0),
            "expired": int(failures or 0),
            "attempts": int(successes or 0) + int(failures or 0),
            "sold_copper": int(sold_copper or 0),
        }
    return out


def realm_groups(con: sqlite3.Connection) -> dict[str, dict]:
    """realm name/slug -> canonical name, group id, member names."""
    members: dict[int, list[str]] = defaultdict(list)
    canonical: dict[int, str] = {}
    by_name: dict[str, dict] = {}
    for slug, name, group_id, cslug, cname, is_canonical in con.execute(
        "SELECT realm_slug, realm_name, group_id, canonical_slug, canonical_name, is_canonical "
        "FROM tsm_realm_group"
    ):
        members[int(group_id)].append(name)
        if is_canonical:
            canonical[int(group_id)] = cname
        by_name[name] = {"slug": slug, "name": name, "group_id": int(group_id),
                          "canonical": cname, "canonical_slug": cslug}
        by_name[slug] = by_name[name]
    for row in by_name.values():
        row["members"] = members.get(row["group_id"], [row["name"]])
    return by_name


def duplicate_realms(con: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Realms whose AuctionDB vectors are identical -- one auction house.

    The TSM app stores the same vector under every name of a connected realm
    group.  Treating them as independent markets would multiply the evidence
    for a single market, so the caller is warned.
    """
    vectors: dict[str, dict[int, tuple]] = defaultdict(dict)
    for realm, item, market, min_buyout, num in con.execute(
        "SELECT realm, itemId, market, min_buyout, num_auctions FROM tsm_market"
    ):
        vectors[realm][int(item)] = (int(market or 0), int(min_buyout or 0), int(num or 0))
    fingerprint: dict[tuple, list[str]] = defaultdict(list)
    for realm, vec in vectors.items():
        key = tuple(sorted(vec.items()))
        fingerprint[key].append(realm)
    out: dict[str, tuple[str, ...]] = {}
    for names in fingerprint.values():
        if len(names) < 2:
            continue
        for name in names:
            out[name] = tuple(sorted(n for n in names if n != name))
    return out
