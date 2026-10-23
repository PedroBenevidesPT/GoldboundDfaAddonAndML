"""Onde é que os coleccionadores de 1M+ ainda não têm cada item.

Lê o detalhe por reino gerado por ``item_demand.py --where --where-csv`` e
responde às duas perguntas de quem compra para revender:

* por item  -- em que reinos há mais compradores a quem falta;
* por reino -- que itens da lista estão em falta e quanto ouro isso representa.

    python scripts/realm_gaps.py
    python scripts/realm_gaps.py --items 10 --realms 3
    python scripts/realm_gaps.py --realm Kazzak
    python scripts/realm_gaps.py --csv reports/realm_gaps.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.realm_gaps import (  # noqa: E402
    cheapest_realms, load_rows, realm_totals, region_value,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DFA_DB = Path(r"C:\economics\woweconomy\dfa\dfa.db")
DEMAND = ROOT / "reports" / "item_demand.csv"
REALMS = ROOT / "reports" / "item_demand_realms.csv"
TSM_DB = ROOT / "tsm_markets.db"


def load_accounts(dfa_db: Path, min_score: int) -> dict[str, int]:
    """Contas com bitmap de aparências e score >= limiar, por reino."""
    con = sqlite3.connect(f"file:{dfa_db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT a.realm, COUNT(*) FROM owned o JOIN account a ON a.account=o.account "
        "LEFT JOIN collector c ON c.region=a.region AND c.realm=a.realm "
        "AND c.name=a.name "
        "WHERE o.kind='appearances' AND a.region='EU' "
        "AND COALESCE(c.score,0)>=? GROUP BY a.realm", (min_score,)).fetchall()
    con.close()
    return {r: int(n) for r, n in rows}


def load_demand(path: Path) -> dict[int, dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return {int(r["item_id"]): r for r in csv.DictReader(fh)}


def print_cheapest(tsm: sqlite3.Connection, demand: dict[int, dict],
                   items: int, per_item: int) -> None:
    """Cheapest realm market value per item, with the cheapest listing if any."""
    print(f"{'item':>9} {'região':>12} {'reino':>18} {'mercado':>12} "
          f"{'listing':>12}  nome")
    print("-" * 118)
    order = sorted(demand.values(), key=lambda d: -float(d.get("gold_gap", 0) or 0))
    for d in order[:items]:
        item_id = int(d["item_id"])
        rows = cheapest_realms(tsm, item_id, per_item)
        if not rows:
            continue
        for pos, r in enumerate(rows):
            head = (f"{item_id:>9} "
                    f"{region_value(tsm, item_id) / 10_000:>12,.0f} "
                    if pos == 0 else " " * 23)
            listing = "—" if not r["listing"] else f"{r['listing'] / 10_000:,.0f}g"
            print(f"{head}{r['realm'][:18]:>18} {r['market'] / 10_000:>12,.0f} "
                  f"{listing:>12}  {d.get('item_name', '')[:44]}")
        print()
    print("notas: 'listing' = min_buyout agora; '—' = sem anúncio, o valor é o")
    print("       mercado estimado do reino e vem de poucas amostras.")
    print()


def print_by_item(rows: list[dict], demand: dict[int, dict],
                  accounts: dict[str, int], items: int, per_item: int,
                  min_wanters: int) -> None:
    by_item: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_item[r["item_id"]].append(r)
    order = sorted(by_item, key=lambda i: -float(demand.get(i, {}).get(
        "gold_gap", 0) or 0))
    print(f"{'item':>9} {'querem':>7} {'%':>6} {'preço':>12} {'ouro em falta':>14}"
          f"  nome")
    print("-" * 118)
    for item_id in order[:items]:
        d = demand.get(item_id, {})
        print(f"{item_id:>9} {int(d.get('wanters', 0) or 0):>7,} "
              f"{100 * float(d.get('want_rate', 0) or 0):>5.1f}% "
              f"{int(d.get('price_copper', 0) or 0) / 10_000:>12,.0f} "
              f"{float(d.get('gold_gap', 0) or 0):>14,.0f}  "
              f"{d.get('item_name', '')[:60]}")
        top = sorted(by_item[item_id], key=lambda r: -r["wanters"])
        for r in top[:per_item]:
            if r["wanters"] < min_wanters:
                break
            n = accounts.get(r["realm"], 0)
            share = f"{100 * r['wanters'] / n:>5.1f}% de {n}" if n else "—"
            print(f"   {r['realm'][:22]:22s} {r['wanters']:>4} querem "
                  f"({share}) | {r['price_copper'] / 10_000:>11,.0f}g "
                  f"({r['price_source']}) | {r['gold_gap']:>13,.0f}g")
        print()


def print_by_realm(rows: list[dict], demand: dict[int, dict],
                   accounts: dict[str, int], realms: int, min_wanters: int,
                   detail: str = "") -> None:
    by_realm: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_realm[r["realm"]].append(r)
    print(f"{'reino':22s} {'contas':>6} {'itens':>6} {'compradores':>11} "
          f"{'ouro em falta':>14}  melhor item")
    print("-" * 118)
    for t in realm_totals(rows, accounts, min_wanters)[:realms]:
        print(f"{t['realm'][:22]:22s} {t['accounts']:>6} {t['items']:>6} "
              f"{t['buyers']:>11} {t['gold_gap']:>14,.0f}  "
              f"{t['best_item'][:40]}")
    if detail and detail in by_realm:
        print(f"\n== {detail}: itens em falta (wanters >= {min_wanters}) ==\n")
        hits = sorted((r for r in by_realm[detail] if r["wanters"] >= min_wanters),
                      key=lambda r: -r["gold_gap"])
        print(f"{'item':>9} {'querem':>7} {'%':>6} {'preço':>12} "
              f"{'ouro em falta':>14}  item (BoE a comprar)")
        print("-" * 118)
        for r in hits:
            print(f"{r['item_id']:>9} {r['wanters']:>7,} "
                  f"{100 * r['want_rate']:>5.1f}% "
                  f"{r['price_copper'] / 10_000:>12,.0f} {r['gold_gap']:>14,.0f}"
                  f"  {r['item_name'][:50]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--realms-csv", default=str(REALMS))
    ap.add_argument("--demand", default=str(DEMAND))
    ap.add_argument("--dfa-db", default=str(DFA_DB))
    ap.add_argument("--min-score", type=int, default=1_000_000)
    ap.add_argument("--items", type=int, default=8,
                    help="quantos itens detalhar por reino")
    ap.add_argument("--per-item", type=int, default=3,
                    help="quantos reinos mostrar por item")
    ap.add_argument("--realms", type=int, default=12,
                    help="quantos reinos listar no resumo")
    ap.add_argument("--min-wanters", type=int, default=3)
    ap.add_argument("--realm", default="", help="detalha um reino (ex: Kazzak)")
    ap.add_argument("--buy", type=int, default=0,
                    help="mostra os N reinos mais baratos por item (0 desliga)")
    ap.add_argument("--tsm-db", default=str(TSM_DB))
    ap.add_argument("--csv", default="", help="grava reino x item")
    args = ap.parse_args()

    accounts = load_accounts(Path(args.dfa_db), args.min_score)
    demand = load_demand(Path(args.demand))
    rows = load_rows(Path(args.realms_csv))
    if not rows:
        print("error: sem linhas; corre primeiro "
              "`item_demand.py --where --where-csv reports/item_demand_realms.csv`")
        return 1

    print(f"# {len(demand)} itens | {len({r['realm'] for r in rows})} reinos | "
          f"{len(accounts)} reinos e {sum(accounts.values()):,} contas com "
          f"score >= {args.min_score:,}")
    print()
    print("== por reino: onde está o dinheiro ==")
    print()
    print_by_realm(rows, demand, accounts, args.realms, args.min_wanters,
                   args.realm)
    print()
    print("== por item: quem falta e onde ==")
    print()
    print_by_item(rows, demand, accounts, args.items, args.per_item,
                  args.min_wanters)

    if args.buy:
        tsm = sqlite3.connect(f"file:{args.tsm_db}?mode=ro", uri=True)
        print("== por item: onde comprar mais barato (mercado do reino) ==")
        print()
        print_cheapest(tsm, demand, args.items, args.buy)

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["realm", "accounts", "item_id", "item_name", "wanters",
                        "realm_share", "price_copper", "price_source",
                        "gold_gap"])
            for r in sorted(rows, key=lambda r: (r["realm"], -r["gold_gap"])):
                n = accounts.get(r["realm"], 0)
                w.writerow([r["realm"], n, r["item_id"], r["item_name"],
                            r["wanters"], round(r["wanters"] / n, 4) if n else "",
                            r["price_copper"], r["price_source"],
                            int(r["gold_gap"])])
        print(f"\ncsv -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
