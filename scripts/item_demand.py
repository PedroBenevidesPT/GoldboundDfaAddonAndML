"""What do the 1M+ collectors still lack? -- item-level demand, not averages.

The bitmaps answer the seller's question directly:

    wanters(item) = how many accounts with score >= 1M have bit 0
    want_rate     = wanters / accounts in that score band
    gold_gap      = wanters * price of the cheapest priced BoE item

That is different from ``hungry_buyers.py``: that report measures the *size* of
each buyer's missing list (how far from completion they are), while this one
measures *which* appearances are still missing.  A collector with 1M score owns
most of the catalogue, so the useful shelf is the intersection of "still
missing" and "exists as a BoE item with a price".

Examples
--------
    python scripts/item_demand.py
    python scripts/item_demand.py --sort wanters --top 30
    python scripts/item_demand.py --min-rate 0.5 --min-wanters 100
    python scripts/item_demand.py --where --top 10
    python scripts/item_demand.py --csv reports/item_demand.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.hungry_buyers import price_of  # noqa: E402
from woweconomy.analytics.item_demand import (  # noqa: E402
    ItemDemand, ItemShelf, count_wanters, rank_demand, rank_item_union,
)
from woweconomy.services.tsm_snapshots import load_realm_map  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DFA_DB = Path(r"C:\economics\woweconomy\dfa\dfa.db")
TSM_DB = ROOT / "tsm_markets.db"
REALMS_JSON = ROOT / "reports" / "realms_eu.json"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dfa-db", default=str(DFA_DB))
    ap.add_argument("--tsm-db", default=str(TSM_DB))
    ap.add_argument("--realms-json", default=str(REALMS_JSON))
    ap.add_argument("--kind", default="appearances",
                    choices=("appearances", "mounts", "recipes"))
    ap.add_argument("--min-score", type=int, default=600_000,
                    help="limiar de score do segmento (default 600k; o topo da "
                         "amostra e ~1,22M)")
    ap.add_argument("--min-wanters", type=int, default=50)
    ap.add_argument("--min-rate", type=float, default=0.25)
    ap.add_argument("--mode", choices=("item", "appearance"), default="item",
                    help="item = união das aparências de cada BoE (default); "
                         "appearance = uma linha por aparência")
    ap.add_argument("--max-price", type=float, default=0,
                    help="tecto em ouro por item (0 desliga)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--sort", choices=("gold", "wanters", "rate"), default="gold")
    ap.add_argument("--include-bop", action="store_true",
                    help="inclui itens não-BoE (por omissão só BoE retail)")
    ap.add_argument("--exclude-sources", default="vendor,trading post,quest,profession,achievement")
    ap.add_argument("--where", action="store_true",
                    help="para cada item, mostra os reinos com mais ouro em falta")
    ap.add_argument("--where-top", type=int, default=0,
                    help="quantos itens detalhar no --where (0 = top/3)")
    ap.add_argument("--where-csv", default="",
                    help="grava item x reino (wanters, preço, ouro em falta)")
    ap.add_argument("--csv", default="")
    return ap.parse_args()


def load_bitmaps(dfa: sqlite3.Connection, kind: str, min_score: int):
    rows = dfa.execute(
        "SELECT a.realm, COALESCE(c.score, 0), o.bits, o.slots "
        "FROM owned o JOIN account a ON a.account=o.account "
        "LEFT JOIN collector c ON c.region=a.region AND c.realm=a.realm "
        "AND c.name=a.name "
        "WHERE o.kind=? AND a.region='EU' AND COALESCE(c.score, 0) >= ?",
        (kind, min_score)).fetchall()
    slots = dfa.execute(
        "SELECT slot, item_id FROM item_slot WHERE kind=? ORDER BY slot",
        (kind,)).fetchall()
    if not rows or not slots:
        raise SystemExit("error: sem bitmaps ou sem item_slot para este kind")
    width = max(int(s) for s, _ in slots) // 8 + 1
    catalogue_slots = len(slots)
    bad = [r for r in rows if int(r[3]) < catalogue_slots]
    if bad:
        # A short blob would make every later slot look "not owned"; that is a
        # data problem, not demand.
        raise SystemExit(
            f"error: {len(bad)} bitmaps com slots < {catalogue_slots}; "
            "corre `collections` para os reescrever")
    owned = np.zeros((len(rows), width), dtype=np.uint8)
    for pos, (_realm, _score, bits, _slots) in enumerate(rows):
        raw = zlib.decompress(bits)
        take = min(width, len(raw))
        owned[pos, :take] = np.frombuffer(raw[:take], dtype=np.uint8)
    bits = np.unpackbits(owned, axis=1, bitorder="little")[:, :catalogue_slots]
    wants = 1 - bits
    by_slot = [int(item) for _slot, item in slots]
    realms = [r[0] for r in rows]
    return wants, by_slot, realms


def load_sellable(tsm: sqlite3.Connection, include_bop: bool,
                  max_price: int) -> dict[int, tuple[int, int, str]]:
    """appearance -> (cheapest priced item's copper, itemId, item name).

    A single appearance can be granted by several items; the cheapest priced
    one is the price ceiling a buyer sees, so that is the one the report keeps.
    """
    binding = "" if include_bop else "AND i.binding=1"
    sql = (
        "SELECT a.appearanceId, a.itemId, i.name, r.market_value "
        "FROM db2_item_appearance a "
        "JOIN db2_items i ON i.itemId=a.itemId "
        "JOIN tsm_region_price r ON r.key_id=a.itemId "
        f"WHERE i.era=0 {binding} AND r.market_value > 0")
    best: dict[int, tuple[int, int, str]] = {}
    for appearance, item_id, name, price in tsm.execute(sql):
        p = int(price or 0)
        if p <= 0 or (max_price and p > max_price):
            continue
        app = int(appearance)
        if app not in best or p < best[app][0]:
            best[app] = (p, int(item_id), name or "")
    return best


def print_rows(rows: list[ItemDemand | ItemShelf], top: int, mode: str) -> None:
    if mode == "item":
        print(f"{'item':>9s} {'querem':>7s} {'%':>6s} {'preço':>12s} "
              f"{'ouro em falta':>14s} {'apar.':>5s}  item (BoE a comprar) | "
              f"aparência")
        print("-" * 122)
        for r in rows[:top]:
            print(f"{r.item_id:>9} {r.wanters:>7,} {100 * r.want_rate:>5.1f}% "
                  f"{r.price / 10_000:>12,.0f} {r.gold_gap:>14,.0f} "
                  f"{r.grants:>5}  {r.item_name[:44]} | {r.name[:30]}")
    else:
        print(f"{'apar.':>9s} {'querem':>7s} {'%':>6s} {'preço':>12s} "
              f"{'ouro em falta':>14s} {'item BoE':>9s}  aparência | item")
        print("-" * 118)
        for r in rows[:top]:
            print(f"{r.appearance_id:>9} {r.wanters:>7,} {100 * r.want_rate:>5.1f}% "
                  f"{r.price / 10_000:>12,.0f} {r.gold_gap:>14,.0f} "
                  f"{r.item_id:>9}  {r.name[:42]} | {r.item_name[:28]}")


def where_to_sell(rows: list[ItemDemand | ItemShelf], tops: int,
                  wants: np.ndarray, by_slot: list[int], realms: list[str],
                  tsm: sqlite3.Connection, realm_map, sellable,
                  catalogue: dict, excluded: list[str],
                  max_price: int, min_realm_wanters: int = 3,
                  out: list[dict] | None = None) -> None:
    """For the selected items, list the realms with the most missing gold.

    ``wanters_realm * price_realm`` is the addressable gold in that market.  The
    price is the realm's own if it has a snapshot, otherwise the region file;
    the marker says which one was used.  For an item-level row the realm count
    is the union of buyers missing any of the item's appearances, so one sale
    is never counted twice.
    """
    names = sorted(set(realms))
    masks = {name: np.array([r == name for r in realms]) for name in names}
    slot_of = {app: slot for slot, app in enumerate(by_slot)}
    denied = tuple(s.lower() for s in excluded)
    item_apps: dict[int, list[int]] = defaultdict(list)
    for app, (_price, item_id, _name) in sellable.items():
        hit = catalogue.get(int(app))
        if not hit or any(term in hit[1].lower() for term in denied):
            continue
        item_apps[int(item_id)].append(int(app))
    region = {int(i): int(m or 0) for i, m in tsm.execute(
        "SELECT key_id, market_value FROM tsm_region_price")}
    price_cache: dict[str, dict[int, tuple[int, int]]] = {}

    def prices(canonical: str):
        if canonical not in price_cache:
            price_cache[canonical] = {
                int(i): (int(m or 0), int(b or 0)) for i, m, b in tsm.execute(
                    "SELECT item_id, market_value, min_buyout FROM tsm_realm_price "
                    "WHERE scope=?", (canonical,))}
        return price_cache[canonical]

    for row in rows[:tops]:
        if isinstance(row, ItemShelf):
            apps = item_apps.get(row.item_id, [row.appearance_id])
            label = f"{row.name} | {row.item_name}"
        else:
            apps = [row.appearance_id]
            label = row.item_name if row.item_name == row.name \
                else f"{row.item_name} | {row.name}"
        cols = [slot_of[a] for a in apps if a in slot_of]
        if not cols:
            continue
        print(f"\n== {label} | {row.wanters} de {row.eligible} "
              f"({100 * row.want_rate:.1f}%) | região {row.price / 10_000:,.0f}g ==")
        scored = []
        for realm, mask in masks.items():
            n = int(wants[mask][:, cols].any(axis=1).sum()) if len(cols) > 1 \
                else int(wants[mask][:, cols[0]].sum())
            if n < min_realm_wanters:
                continue
            try:
                group = realm_map.resolve(realm)
            except KeyError:
                continue
            rp = prices(group.canonical_slug).get(row.item_id)
            unit = price_of(rp, region.get(row.item_id))
            if max_price and unit > max_price:
                unit = max_price
            if unit <= 0:
                continue
            scored.append((n * unit / 10_000, n, realm, unit,
                           "reino" if rp and (rp[0] or rp[1]) else "região"))
        scored.sort(reverse=True)
        if out is not None:
            for gold, n, realm, unit, src in scored:
                out.append({"item_id": row.item_id, "item_name": row.item_name,
                            "name": row.name, "realm": realm, "wanters": n,
                            "eligible": row.eligible,
                            "want_rate": round(n / row.eligible, 4),
                            "price_copper": unit,
                            "gold_gap": round(gold, 0), "price_source": src})
        for gold, n, realm, unit, src in scored[:5]:
            print(f"   {realm[:24]:24s} {n:>4} querem | {unit / 10_000:>10,.0f}g "
                  f"({src}) | {gold:>12,.0f}g em falta")


def main() -> int:
    args = parse_args()
    dfa = sqlite3.connect(f"file:{args.dfa_db}?mode=ro", uri=True)
    tsm = sqlite3.connect(f"file:{args.tsm_db}?mode=ro", uri=True)
    realm_map = load_realm_map(args.realms_json)

    wants, by_slot, realms = load_bitmaps(dfa, args.kind, args.min_score)
    eligible = wants.shape[0]
    max_price = int(args.max_price * 10_000) if args.max_price else 0
    sellable = load_sellable(tsm, args.include_bop, max_price)
    catalogue = {int(i): (n or "", s or "") for i, n, s in dfa.execute(
        "SELECT item_id, name, source FROM catalogue "
        "WHERE kind=? AND obtainable=1 AND item_id>0", (args.kind,))}
    excluded = [s.strip() for s in args.exclude_sources.split(",") if s.strip()]
    if args.mode == "item":
        rows = rank_item_union(
            wants=wants, slots=by_slot, sellable=sellable, catalogue=catalogue,
            min_wanters=args.min_wanters, min_rate=args.min_rate,
            exclude_sources=excluded, sort=args.sort)
    else:
        rows = rank_demand(
            counts=count_wanters(wants), eligible=eligible, slots=by_slot,
            sellable=sellable, catalogue=catalogue,
            min_wanters=args.min_wanters, min_rate=args.min_rate,
            exclude_sources=excluded, sort=args.sort)

    label = "itens BoE" if args.mode == "item" else "aparências"
    print(f"# {args.kind}: {eligible:,} contas com bitmap e score >= "
          f"{args.min_score:,} | {len(by_slot):,} aparições | "
          f"{len(rows):,} {label} com preço e fonte aceite")
    print(f"# wanters = contas do segmento com bit 0; preço = item BoE mais "
          f"barato com preço regional; {args.min_wanters} wanters / "
          f"{100 * args.min_rate:.0f}% mínimos")
    print()
    print_rows(rows, args.top, args.mode)
    if args.where:
        where_rows: list[dict] = [] if args.where_csv else None
        where_to_sell(rows, args.where_top or max(1, args.top // 3), wants,
                      by_slot, realms, tsm, realm_map, sellable, catalogue,
                      excluded, max_price, out=where_rows)
        if where_rows is not None:
            out = Path(args.where_csv)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=[
                    "item_id", "item_name", "name", "realm", "wanters",
                    "eligible", "want_rate", "price_copper", "gold_gap",
                    "price_source"])
                w.writeheader()
                w.writerows(where_rows)
            print(f"\nwhere csv -> {out} ({len(where_rows)} linhas)")

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["item_id", "item_name", "appearance_id", "name",
                        "source", "grants", "wanters", "eligible", "want_rate",
                        "price_copper", "gold_gap"])
            for r in rows:
                w.writerow([r.item_id, r.item_name, r.appearance_id, r.name,
                            r.source, getattr(r, "grants", 1), r.wanters,
                            r.eligible, round(r.want_rate, 4), r.price,
                            round(r.gold_gap, 0)])
        print(f"\ncsv -> {out}")

    print("\nnotas: o denominador é o segmento >= limiar, não a população do reino;")
    print("       uma aparência pode vir de vários itens — fica o BoE mais barato "
          "com preço;")
    print("       no modo item, wanters é a união de quem falta pelo menos uma "
          "aparência do item;")
    print("       fontes de vendor/Trading Post/quest/profissão ficam de fora.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
