"""O teu inventário contra a procura -- e em que reino a procuram mais.

Um comando, duas perguntas:

1. o teu inventário vs a lista de demanda (``reports/item_demand.csv``);
2. com ``--realms``, para cada item que tens: **em que reino** há mais contas a
   quem falta a aparência, e a razão procura/oferta (compradores por anúncio)
   desse reino.

    python scripts/compare_inventory.py --realms
    python scripts/compare_inventory.py --realms --top 100 --per-item 3
    python scripts/compare_inventory.py --realms --min-score 600000 --realms-csv reports/inventory_realms.csv

Sem argumentos procura ``inventory.csv`` / ``inventario.csv`` na raiz e em
``scripts/`` e ``reports/``; o caminho usado aparece na primeira linha.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.inventory_demand import (  # noqa: E402
    INVENTORY_CANDIDATES, compare, load_demand, load_inventory,
    realm_wanters, resolve_inventory, resolve_names, sale_pressure,
)

DFA_DB = Path(r"C:\economics\woweconomy\dfa\dfa.db")
TSM_DB = ROOT / "tsm_markets.db"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DEFAULT_DEMAND = ROOT / "reports" / "item_demand.csv"


def realm_report(inventory: dict, dfa_db: Path, tsm_db: Path, min_score: int,
                 per_item: int, realms_top: int, items: int,
                 csv_out: str = "") -> int:
    """Em que reino querem mais o que tens, item a item."""
    from item_demand import load_bitmaps  # irmão, no mesmo directório

    dfa = sqlite3.connect(f"file:{dfa_db}?mode=ro", uri=True)
    wants, by_slot, realms = load_bitmaps(dfa, "appearances", min_score)
    dfa.close()
    tsm = sqlite3.connect(f"file:{tsm_db}?mode=ro", uri=True)
    ids = list(inventory)
    q = ",".join("?" * len(ids))

    apps: dict[int, list[int]] = {}
    for item_id, app in tsm.execute(
            f"SELECT itemId, appearanceId FROM db2_item_appearance "
            f"WHERE itemId IN ({q})", ids):
        apps.setdefault(int(item_id), []).append(int(app))
    slot_of = {app: pos for pos, app in enumerate(by_slot)}
    listings = {}
    for item_id, realm, n in tsm.execute(
            f"SELECT itemId, LOWER(realm), num_auctions FROM tsm_market "
            f"WHERE itemId IN ({q})", ids):
        listings[(int(item_id), realm)] = int(n or 0)
    region = {int(i): (int(m or 0), float(sr or 0), float(spd or 0))
              for i, m, sr, spd in tsm.execute(
                  f"SELECT key_id, market_value, sale_rate, sold_per_day "
                  f"FROM tsm_region_price WHERE key_id IN ({q})", ids)}
    # Reinos com dados de anúncios (tsm_market); nos outros a oferta é
    # desconhecida, o que é diferente de "não há anúncio nenhum".
    market_realms = {r for (r,) in tsm.execute(
        "SELECT DISTINCT LOWER(realm) FROM tsm_market")}
    tsm.close()

    rows: list[dict] = []
    for item_id, line in inventory.items():
        cols = [slot_of[a] for a in apps.get(item_id, []) if a in slot_of]
        if not cols:
            continue
        # per_realm=0 -> todos os reinos: o resumo por reino não pode depender
        # do corte que serve a tabela por item.
        best = realm_wanters(wants, realms, cols, per_realm=0)
        if not best:
            continue
        market, sale_rate, sold_per_day = region.get(item_id, (0, 0.0, 0.0))
        for pos, hit in enumerate(best):
            n_auctions = listings.get((item_id, hit["realm"].lower()), 0)
            if hit["realm"].lower() not in market_realms:
                pressure, nota = 0.0, "sem dados de anúncios"
            else:
                pressure, nota = sale_pressure(hit["wanters"], n_auctions)
            rows.append({
                "item_id": item_id, "name": line.name, "qty": line.qty,
                "realm": hit["realm"], "wanters": hit["wanters"],
                "accounts": hit["accounts"], "share": round(hit["share"], 4),
                "auctions": n_auctions, "per_listing": round(pressure, 3),
                "note": nota, "price_copper": market,
                "sale_rate": sale_rate, "sold_per_day": sold_per_day,
                "rank": pos + 1})

    total_accounts = sum(1 for _ in realms)
    print(f"# segmento: {total_accounts:,} contas com bitmap e score >= "
          f"{min_score:,} em {len(set(realms))} reinos")
    print(f"# {len(rows)} pares item x reino | 'por anúncio' = compradores "
          f"a quem falta / anúncios no reino")

    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["realm"], {"items": 0, "wanters": 0,
                                        "accounts": r["accounts"]})
        a["items"] += 1
        a["wanters"] += r["wanters"]
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["wanters"])

    print(f"\n== em que reino querem mais o teu inventário "
          f"({len(ranked)} reinos com procura) ==\n")
    print(f"{'reino':22s} {'contas':>6} {'itens':>6} {'compradores':>11} "
          f"{'% médio':>8}  melhor item")
    print("-" * 112)
    for realm, a in ranked[:realms_top]:
        best = max((r for r in rows if r["realm"] == realm),
                   key=lambda r: r["wanters"])
        share = a["wanters"] / (a["items"] * a["accounts"]) if a["accounts"] else 0
        print(f"{realm[:22]:22s} {a['accounts']:>6} {a['items']:>6} "
              f"{a['wanters']:>11,} {100 * share:>7.1f}%  {best['name'][:34]}")

    print(f"\n== item a item: melhor reino e pressão de venda ==\n")
    print(f"{'item':>9} {'qty':>4} {'reino':>16} {'querem':>7} {'do reino':>9} "
          f"{'anúncios':>9} {'por anúncio':>12} {'nota':<22} {'v/dia':>6}  nome")
    print("-" * 128)
    # Um reino com anúncios é onde se pode vender já: escolhe-se o de maior
    # pressão (compradores por anúncio); sem anúncios conhecidos fica o de mais
    # compradores, que a nota deixa claro.
    chosen: dict[int, tuple] = {}
    for r in rows:
        # Com anúncios conhecidos, manda a pressão (compradores por anúncio);
        # só se não houver nenhum se escolhe pelo número de compradores.
        known = r["note"] != "sem dados de anúncios"
        cand = (r["per_listing"], 1) if known else (0.0, 0)
        cur = chosen.get(r["item_id"])
        if cur is None or cand > cur[0]:
            chosen[r["item_id"]] = (cand, r)
    firsts = sorted((v[1] for v in chosen.values()),
                    key=lambda r: (-r["wanters"], r["item_id"]))[:items]
    for r in firsts:
        nota = f" ({r['note']})" if r["note"] else ""
        snap = r["note"] != "sem dados de anúncios"
        auctions = f"{r['auctions']:,}" if snap else "—"
        per_listing = f"{r['per_listing']:,.1f}" if snap else "—"
        print(f"{r['item_id']:>9} {r['qty']:>4} {r['realm'][:16]:>16} "
              f"{r['wanters']:>7,} {100 * r['share']:>8.1f}% "
              f"{auctions:>9} {per_listing:>12}"
              f" {nota:<22} "
              f"{r['sold_per_day']:>8.3f}  {r['name'][:28]}")

    if csv_out:
        out = Path(csv_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "item_id", "name", "qty", "realm", "rank", "wanters",
                "accounts", "share", "auctions", "per_listing", "note",
                "price_copper", "sale_rate", "sold_per_day"])
            w.writeheader()
            keep = [r for r in rows if r["rank"] <= per_item]
            w.writerows(sorted(keep, key=lambda r: (r["item_id"], r["rank"])))
        print(f"\ncsv -> {out} ({len(keep)} linhas, {per_item} reinos por item)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inventory_path", nargs="?", default="",
                    help="export do TSM (por omissão: primeiro candidato que existir)")
    ap.add_argument("--inventory", default="",
                    help="igual ao argumento posicional")
    ap.add_argument("--demand", default=str(DEFAULT_DEMAND))
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--by", choices=("wanters", "gold"), default="wanters")
    ap.add_argument("--show-extra", action="store_true",
                    help="lista todo o inventário com o estado face à demanda")
    ap.add_argument("--no-names", action="store_true",
                    help="não resolver no DB2 os nomes que o export perdeu")
    ap.add_argument("--realms", action="store_true",
                    help="em que reino querem mais cada item que tens")
    ap.add_argument("--min-score", type=int, default=600_000,
                    help="score mínimo do segmento (default 600k)")
    ap.add_argument("--per-item", type=int, default=3,
                    help="reinos por item guardados no --realms-csv")
    ap.add_argument("--realms-top", type=int, default=12,
                    help="reinos mostrados no resumo")
    ap.add_argument("--dfa-db", default=str(DFA_DB))
    ap.add_argument("--tsm-db", default=str(TSM_DB))
    ap.add_argument("--realms-csv", default="",
                    help="grava item x reino com wanters e pressão de venda")
    ap.add_argument("--csv", default="")
    args = ap.parse_args()

    try:
        path = resolve_inventory(ROOT, args.inventory or args.inventory_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("       passa o ficheiro: python scripts/compare_inventory.py "
              "--inventory caminho\\para\\inventory.csv", file=sys.stderr)
        print(f"       candidatos: {', '.join(INVENTORY_CANDIDATES)}",
              file=sys.stderr)
        return 2
    if not Path(args.demand).is_file():
        print(f"error: demand em falta: {args.demand}\n"
              f"       corre primeiro: python scripts/item_demand.py "
              f"--csv reports\\item_demand.csv", file=sys.stderr)
        return 2

    inventory = load_inventory(path)
    if args.realms:
        if not args.no_names:
            tsm_names = sqlite3.connect(f"file:{args.tsm_db}?mode=ro", uri=True)
            q = ",".join("?" * len(inventory))
            inventory = resolve_names(inventory, {
                int(i): n for i, n in tsm_names.execute(
                    f"SELECT itemId, name FROM db2_items WHERE itemId IN ({q})",
                    list(inventory)) if n} if inventory else {})
            tsm_names.close()
        rc = realm_report(inventory, Path(args.dfa_db), Path(args.tsm_db),
                          args.min_score, args.per_item, args.realms_top,
                          args.top, args.realms_csv)
        print("\nnotas: 'querem' = contas do reino com score >= limiar a quem falta "
              "a aparência (união das aparências do item);")
        print("       'por anúncio' = compradores por anúncio no reino (procura "
              "/oferta); a nota diz quando não há snapshot;")
        print("       a linha de cada item prefere um reino com dados de "
              "anúncios; sem eles, o de mais compradores.")
        print("       'vendas/dia' é da região (a TSM não publica velocity por "
              "reino); não é probabilidade de venda individual.")
        return rc
    if not args.no_names:
        tsm = sqlite3.connect(f"file:{ROOT / 'tsm_markets.db'}?mode=ro", uri=True)
        q = ",".join("?" * len(inventory))
        names = {int(i): n for i, n in tsm.execute(
            f"SELECT itemId, name FROM db2_items WHERE itemId IN ({q})",
            list(inventory)) if n} if inventory else {}
        tsm.close()
        inventory = resolve_names(inventory, names)
    demand = load_demand(args.demand)
    wanters = {d.item_id: d for d in demand}
    matched, missing, extra = compare(inventory, demand)
    units = sum(line.qty for line in inventory.values())

    print(f"# inventário: {len(inventory):,} itens | {units:,} unidades | "
          f"{path}")
    print(f"# demand: {len(demand):,} itens BoE | {args.demand}")
    print(f"# nomes do item (ItemID), não da aparência")
    print(f"# tens: {len(matched)} | faltam: {len(missing)} | "
          f"fora da lista: {len(extra)}")
    print()

    if matched:
        print(f"{'item':>9s} {'qty':>5s} {'querem':>7s} {'%':>6s} "
              f"{'preço':>12s} {'stock':>14s} {'cobertura':>9s}  "
              f"item (no teu inventário)")
        print("-" * 112)
        matched.sort(key=lambda m: (-m.demand.wanters, -m.demand.gold_gap))
        for m in matched[:args.top]:
            mine = inventory[m.demand.item_id].name or m.demand.item_name
            print(f"{m.demand.item_id:>9} {m.qty:>5,} {m.demand.wanters:>7,} "
                  f"{100 * m.demand.want_rate:>5.1f}% "
                  f"{m.demand.price_gold:>12,.0f} {m.stock_value:>14,.0f} "
                  f"{100 * m.coverage:>8.1f}%  {mine[:40]}")
    else:
        print("(não tens nenhum dos itens da lista de demanda)")

    if missing:
        print(f"\n== em falta ({len(missing)}) ==\n")
        key = (lambda d: (-d.wanters, -d.gold_gap)) if args.by == "wanters" \
            else (lambda d: (-d.gold_gap, -d.wanters))
        print(f"{'item':>9s} {'querem':>7s} {'%':>6s} {'preço':>12s} "
              f"{'ouro em falta':>14s} {'apar.':>5s}  item")
        print("-" * 112)
        for d in sorted(missing, key=key)[:args.top]:
            print(f"{d.item_id:>9} {d.wanters:>7,} {100 * d.want_rate:>5.1f}% "
                  f"{d.price_gold:>12,.0f} {d.gold_gap:>14,.0f} "
                  f"{d.grants:>5}  {d.item_name[:44]}")

    if args.show_extra:
        print(f"\n== o teu inventário ({len(inventory)}) vs demanda ==\n")
        print(f"{'item':>9s} {'qty':>5s} {'na lista':>9s}  estado | nome")
        print("-" * 112)
        for line in sorted(inventory.values(), key=lambda x: -x.qty)[:args.top]:
            d = wanters.get(line.item_id)
            estado = (f"{d.wanters:,} querem ({100 * d.want_rate:.1f}%)" if d
                      else "fora da lista")
            marca = "sim" if d else "-"
            print(f"{line.item_id:>9} {line.qty:>5,} {marca:>9s}  {estado:22s} | "
                  f"{line.name[:56]}")

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        qty = {line.item_id: line.qty for line in inventory.values()}

        def raw(line):
            return qty.get(line.item_id, 0)

        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["item_id", "item_name", "name", "qty", "wanters",
                        "want_rate", "price_gold", "gold_gap", "stock_value",
                        "coverage"])
            for line in sorted(demand, key=lambda d: (-d.wanters, -d.gold_gap)):
                n = raw(line)
                w.writerow([line.item_id, line.item_name, line.name, n,
                            line.wanters, round(line.want_rate, 4),
                            round(line.price_gold, 0), round(line.gold_gap, 0),
                            round(n * line.price_gold, 0),
                            round(n / line.wanters, 4) if line.wanters else 0])
        print(f"\ncsv -> {out}")

    print("\nnotas: junta por ItemID base; o inventário não traz aparência, só o "
          "item;")
    print("       as linhas mostram o nome do item BoE (o que se compra/vende), "
          "não o da aparência;")
    print("       'cobertura' = qty / wanters (não é procura garantida);")
    print("       'stock' = qty x preço regional, valor de mercado, não receita.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
