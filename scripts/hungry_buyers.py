"""Where the hungry buyers are -- DfA bitmaps joined with TSM snapshots.

Reads, in this order:

* the DfA database (``item_slot``, ``owned``, ``account``, ``collector``) for
  the appearances each high-score account still lacks;
* the TSM snapshot tables (``tsm_realm_price``, ``tsm_region_price``,
  ``tsm_realm_group``) for what those appearances cost on the account's
  connected-realm auction house.

No network request.  Prices are copper in the database and gold in the report.

Examples
--------
    python scripts/hungry_buyers.py
    python scripts/hungry_buyers.py --min-score 1050000 --top 15
    python scripts/hungry_buyers.py --boe-only --sort items
    python scripts/hungry_buyers.py --details Ravencrest
    python scripts/hungry_buyers.py --csv reports/hungry_buyers.csv
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

from woweconomy.analytics.hungry_buyers import (  # noqa: E402
    COPPER_PER_GOLD, HungerRow, compute_hunger, price_of, rank,
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
    ap.add_argument("--min-score", type=int, default=1_000_000,
                    help="só contas com este score de colecção (default 1M)")
    ap.add_argument("--min-buyers", type=int, default=5)
    ap.add_argument("--price-cap", type=float, default=1_000_000,
                    help="tecto em ouro por item (default 1M; 0 desliga)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--sort", choices=("gold", "items"), default="items",
                    help="items = fome (default); gold = valor de mercado da lista em falta")
    ap.add_argument("--boe-only", action="store_true",
                    help="só aparências que têm pelo menos um item BoE retail")
    ap.add_argument("--details", default="", help="reino para o detalhe dos itens")
    ap.add_argument("--csv", default="", help="escrever a tabela para CSV")
    return ap.parse_args()


def load_slots(dfa: sqlite3.Connection, kind: str) -> tuple[list[int], dict[int, str]]:
    rows = dfa.execute("SELECT slot, item_id FROM item_slot WHERE kind=? ORDER BY slot",
                       (kind,)).fetchall()
    if not rows:
        raise SystemExit(f"error: sem item_slot para {kind}; corre `collections` primeiro")
    width = max(int(s) for s, _ in rows) + 1
    by_slot = [0] * width
    for slot, item_id in rows:
        by_slot[int(slot)] = int(item_id)
    names = dict(dfa.execute("SELECT item_id, name FROM catalogue WHERE kind=?",
                             (kind,)).fetchall())
    return by_slot, names


def load_boe_slots(tsm: sqlite3.Connection, dfa: sqlite3.Connection,
                   kind: str) -> set[int]:
    if kind != "appearances":
        return set()
    boe = {int(r[0]) for r in tsm.execute(
        "SELECT DISTINCT a.appearanceId FROM db2_item_appearance a "
        "JOIN db2_items i ON i.itemId=a.itemId "
        "WHERE i.binding=1 AND i.era=0")}
    return {int(slot) for slot, item_id in dfa.execute(
        "SELECT slot, item_id FROM item_slot WHERE kind=?", (kind,)) if int(item_id) in boe}


def load_accounts(dfa: sqlite3.Connection, kind: str, min_score: int
                  ) -> dict[str, list[bytes]]:
    rows = dfa.execute(
        "SELECT a.realm, COALESCE(c.score, 0), o.bits "
        "FROM owned o JOIN account a ON a.account=o.account "
        "LEFT JOIN collector c ON c.region=a.region AND c.realm=a.realm "
        "AND c.name=a.name "
        "WHERE o.kind=? AND a.region='EU' AND COALESCE(c.score, 0) >= ?",
        (kind, min_score)).fetchall()
    by_realm: dict[str, list[bytes]] = defaultdict(list)
    for realm, _score, bits in rows:
        by_realm[realm].append(bits)
    return by_realm


def count_missing(bitmaps: list[bytes], width: int, slots: int) -> np.ndarray:
    owned = np.zeros((len(bitmaps), width), dtype=np.uint8)
    for pos, blob in enumerate(bitmaps):
        raw = zlib.decompress(blob)
        take = min(width, len(raw))
        if take:
            owned[pos, :take] = np.frombuffer(raw[:take], dtype=np.uint8)
    bits = np.unpackbits(owned, axis=1, bitorder="little")[:, :slots]
    return (1 - bits).sum(axis=0, dtype=np.int64)


def prices_for(tsm: sqlite3.Connection, scope: str) -> dict[int, tuple[int, int]]:
    return {int(i): (int(m or 0), int(b or 0)) for i, m, b in tsm.execute(
        "SELECT item_id, market_value, min_buyout FROM tsm_realm_price WHERE scope=?",
        (scope,))}


def region_prices(tsm: sqlite3.Connection) -> dict[int, int]:
    return {int(i): int(m or 0) for i, m in tsm.execute(
        "SELECT key_id, market_value FROM tsm_region_price")}


def print_table(rows: list[HungerRow], top: int) -> None:
    print(f"{'realm':26s} {'compradores':>11s} {'itens/comp.':>11s} "
          f"{'ouro/comp. (M)':>14s} {'índice':>7s} {'cobertura':>9s}")
    print("-" * 84)
    for r in rows[:top]:
        index = f"{r.price_index:.2f}x" if r.price_index else "—"
        print(f"{r.realm[:26]:26s} {r.buyers:>11,} {r.items_per_buyer:>11,.0f} "
              f"{r.gold_per_buyer / 1e6:>14,.1f} {index:>7s} "
              f"{100 * r.coverage:>8.0f}%")


def detail(tsm: sqlite3.Connection, dfa: sqlite3.Connection, kind: str,
           realm: str, canonical: str, bitmaps: list[bytes], width: int,
           by_slot: list[int], names: dict[int, str], region: dict[int, int],
           price_cap: int = 0, top: int = 15) -> None:
    counts = count_missing(bitmaps, width, len(by_slot))
    rp = prices_for(tsm, canonical)
    scored = []
    for slot in range(len(by_slot)):
        n = int(counts[slot])
        if n <= 0:
            continue
        item_id = by_slot[slot]
        unit = price_of(rp.get(item_id), region.get(item_id))
        if unit <= 0:
            continue
        capped = bool(price_cap and unit > price_cap)
        if capped:
            unit = price_cap
        scored.append((n, n * unit, item_id, unit, capped))
    # Most wanted first; expensive only breaks ties.  Sorting by gold alone
    # lets one 10M-gold placeholder outrank an item every buyer still lacks.
    scored.sort(key=lambda x: (-x[0], -x[1]))
    print(f"\n== {realm} / {canonical}: {len(bitmaps)} compradores, "
          f"{int(counts.sum()):,} aparências em falta ==")
    print(f"{'item':>9s} {'querem':>7s} {'reino':>11s} {'região':>11s} "
          f"{'ratio':>6s} {'ouro':>12s}  nome")
    for n, _score, item_id, unit, capped in scored[:top]:
        reino = rp.get(item_id, (0, 0))
        reg = region.get(item_id, 0)
        ratio = f"{reino[0] / reg:.2f}x" if reino[0] and reg else "—"
        reino_txt = f"{reino[0]:>11,}" if reino[0] else f"{'—':>11s}"
        mark = "*" if capped else " "
        print(f"{item_id:>9} {n:>7,} {reino_txt} {reg:>11,} {ratio:>6s} "
              f"{n * unit / COPPER_PER_GOLD:>12,.0f}{mark} {(names.get(item_id) or '')[:40]}")
    shown = len(scored[:top])
    if not shown:
        print("  (nenhum item em falta com preço no TSM)")


def main() -> int:
    args = parse_args()
    dfa = sqlite3.connect(f"file:{args.dfa_db}?mode=ro", uri=True)
    tsm = sqlite3.connect(f"file:{args.tsm_db}?mode=ro", uri=True)
    realm_map = load_realm_map(args.realms_json)

    by_slot, names = load_slots(dfa, args.kind)
    width = len(by_slot) // 8 + (1 if len(by_slot) % 8 else 0)
    boe = load_boe_slots(tsm, dfa, args.kind) if args.boe_only else set()
    accounts = load_accounts(dfa, args.kind, args.min_score)
    region = region_prices(tsm)
    price_cap = int(args.price_cap * COPPER_PER_GOLD) if args.price_cap > 0 else 0
    latest = tsm.execute(
        "SELECT MAX(updated_at) FROM tsm_snapshot WHERE kind='realm_items'").fetchone()[0]

    rows: list[HungerRow] = []
    skipped: list[str] = []
    price_cache: dict[str, dict[int, tuple[int, int]]] = {}
    for realm, bitmaps in sorted(accounts.items()):
        if len(bitmaps) < args.min_buyers:
            continue
        try:
            group = realm_map.resolve(realm)
        except KeyError:
            skipped.append(realm)
            continue
        counts = count_missing(bitmaps, width, len(by_slot))
        if boe:
            mask = np.zeros(len(by_slot), dtype=bool)
            mask[list(boe)] = True
            counts = np.where(mask, counts, 0)
        if group.canonical_slug not in price_cache:
            price_cache[group.canonical_slug] = prices_for(tsm, group.canonical_slug)
        rows.append(compute_hunger(
            realm=realm, canonical_slug=group.canonical_slug,
            counts=counts, item_ids=by_slot,
            realm_prices=price_cache[group.canonical_slug],
            region_prices=region, buyers=len(bitmaps),
            price_cap=price_cap))

    rows = rank(rows, args.sort)
    total_buyers = sum(r.buyers for r in rows)
    print(f"# {args.kind}: {total_buyers:,} contas com bitmap e score >= "
          f"{args.min_score:,} em {len(rows)} mercados")
    print(f"# TSM: snapshots de reino com updatedAt {latest or '—'}")
    if skipped:
        print(f"# {len(skipped)} reinos fora do índice EU: {', '.join(skipped[:8])}"
              + (" …" if len(skipped) > 8 else ""))
    print("# preço: minBuyout > marketValue > marketValue da região")
    print()
    print_table(rows, args.top)

    if args.details:
        realm = args.details
        match = next((r for r in rows if r.realm.lower() == realm.lower()), None)
        if not match:
            print(f"\n(sem dados para {realm})", file=sys.stderr)
        else:
            detail(tsm, dfa, args.kind, match.realm, match.canonical_slug,
                   accounts[match.realm], width, by_slot, names, region,
                   price_cap=price_cap)

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["realm", "canonical_slug", "buyers", "gap_items",
                        "items_per_buyer", "gap_gold_copper", "gold_per_buyer",
                        "price_index", "coverage", "wanted_items", "priced_items",
                        "capped_items"])
            for r in rows:
                w.writerow([r.realm, r.canonical_slug, r.buyers, r.gap_items,
                            round(r.items_per_buyer, 1), round(r.gap_gold, 0),
                            round(r.gold_per_buyer, 0),
                            round(r.price_index, 4) if r.price_index else "",
                            round(r.coverage, 4), r.wanted_items, r.priced_items,
                            r.capped_items])
        print(f"\ncsv -> {out}")

    print("\nnotas: compradores = contas com bitmap e score >= limiar, não a "
          "população do reino;")
    print("       ouro/comprador (M) = soma do valor de mercado dos itens em falta, "
          "não o que o comprador vai gastar;")
    print(f"       preço cortado a {args.price_cap:,.0f}g/item; * marca os itens "
          "cortados no detalhe;")
    print("       cobertura = fracção dos itens em falta com preço no reino ou "
          "região; ouro/comprador só conta esses;")
    print("       cada grupo connected tem um preço só; o TSM não publica "
          "numAuctions nos CSV de reino.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
