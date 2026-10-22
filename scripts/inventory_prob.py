"""Onde cada peça do inventário tem mais probabilidade de venda.

Responde a "item X vende melhor aqui ou acolá?" com o modelo de
`scripts/sale_probability.py`, mas só para os itens que existem no inventário --
o que torna a corrida rápida em vez de varrer 10 mil itens x 92 reinos.

    python scripts/inventory_prob.py --inventory inventory.csv
    python scripts/inventory_prob.py --inventory inventory.csv --realm Draenor
    python scripts/inventory_prob.py --inventory inventory.csv --csv reports/inventory_prob.csv

O inventário é o export do TSM (`ItemID;Name;Quality;iLvl;BonusIDs;Modifiers;Quantity`),
o mesmo que `scripts/calc_price.py --inventory` já lê. Itens sem aparência (receitas,
material) são listados à parte: para esses o modelo de transmog não tem nada a dizer.

Modelo, igual ao de sale_probability.py:

    P(30d) = 1 - exp(-30 * lambda_regiao * (compradores_reino / compradores_EU) / (N+1))

`lambda_regiao` é o `sold_per_day` da TSM para a região, `compradores_reino` conta as
contas da amostra DfA que ainda não têm a aparência, e `N` é 1 quando o reino já tem a
peça à venda e 0 quando a prateleira está vazia.  Preços em cobre; saída em ouro.
"""
from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from item_demand import load_bitmaps  # noqa: E402
from woweconomy.analytics.inventory_demand import (  # noqa: E402
    INVENTORY_CANDIDATES, resolve_inventory,
)

try:                # consolas de Windows: acentos nas mensagens de erro
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DFA_DB = Path(r"C:\economics\woweconomy\dfa\dfa.db")
TSM_DB = ROOT / "tsm_markets.db"


def read_inventory(path: Path) -> dict[int, int]:
    """item_id -> quantidade.  Aceita o export do TSM (``;``) e CSV com ``,``."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        sep = ";" if sample.count(";") >= sample.count(",") else ","
        rows = list(csv.DictReader(fh, delimiter=sep))
    head = {k.strip().lower(): k for k in (rows[0] if rows else {})}
    id_col = next((head[k] for k in ("itemid", "item_id", "id") if k in head), None)
    qty_col = next((head[k] for k in ("quantity", "qty", "count") if k in head), None)
    if not id_col:
        raise SystemExit(f"erro: sem coluna de item id em {path}")
    out: dict[int, int] = {}
    for row in rows:
        raw = (row.get(id_col) or "").strip()
        if not raw.isdigit():
            continue
        qty = 1
        if qty_col:
            try:
                qty = max(1, int(float((row.get(qty_col) or "1").replace(",", "."))))
            except ValueError:
                qty = 1
        iid = int(raw)
        out[iid] = out.get(iid, 0) + qty
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inventory", default="",
                    help="CSV do inventario; sem isto procura nos sitios habituais")
    ap.add_argument("--dfa-db", default=str(DFA_DB))
    ap.add_argument("--tsm-db", default=str(TSM_DB))
    ap.add_argument("--min-score", type=int, default=600_000)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--cap", type=float, default=3.0)
    ap.add_argument("--min-realms", type=int, default=5)
    ap.add_argument("--per-item", action="store_true",
                    help="lista também o top de reinos de cada peça")
    ap.add_argument("--realm", default="", help="detalha só este reino")
    ap.add_argument("--top", type=int, default=15, help="quantos itens listar")
    ap.add_argument("--top-realms", type=int, default=3,
                    help="quantos reinos mostrar por item (default 3)")
    ap.add_argument("--simulate", type=int, default=0,
                    help="simula N meses (cada listagem vende com prob P) e diz "
                         "a probabilidade de cada reino ficar no top 3")
    ap.add_argument("--sort", choices=("gold", "p"), default="gold",
                    help="ordena os reinos do item por ouro esperado ou por probabilidade")
    ap.add_argument("--csv", default="")
    a = ap.parse_args(argv)

    # O mesmo resolvedor que o compare_inventory.py usa: sem --inventory, o
    # primeiro candidato que existir; com caminho dado, esse mesmo.
    try:
        path = resolve_inventory(ROOT, a.inventory)
    except FileNotFoundError as exc:
        raise SystemExit(f"erro: {exc}\n"
                         f"       passa --inventory caminho\\para\\inventory.csv\n"
                         f"       (a pasta actual é {Path.cwd()})") from None
    if a.inventory:
        print(f"inventário: {path}")
    else:
        print(f"inventário: {path} (encontrado sozinho; candidatos: "
              f"{', '.join(INVENTORY_CANDIDATES)})")
    inv = read_inventory(path)
    if not inv:
        raise SystemExit(f"erro: nenhuma linha com item id em {path}")
    print(f"inventário: {len(inv)} itens distintos, {sum(inv.values())} unidades")

    dfa = sqlite3.connect(f"file:{a.dfa_db}?mode=ro", uri=True)
    tsm = sqlite3.connect(a.tsm_db)
    wants, by_slot, realms = load_bitmaps(dfa, "appearances", a.min_score)

    # colunas do bitmap por item, e os nomes vindos do inventario
    col_of = {aid: pos for pos, aid in enumerate(by_slot)}
    item_cols: dict[int, list[int]] = defaultdict(list)
    for iid, aid in tsm.execute(
            f"SELECT itemId, appearanceId FROM db2_item_appearance "
            f"WHERE itemId IN ({','.join('?' * len(inv))})", list(inv)):
        pos = col_of.get(aid)
        if pos is not None:
            item_cols[iid].append(pos)
    sem_aparencia = [i for i in inv if not item_cols.get(i)]
    print(f"com aparência no bitmap: {len(inv) - len(sem_aparencia)} | sem: {len(sem_aparencia)}")

    ids = [i for i in inv if item_cols.get(i)]
    ph = ",".join("?" * len(ids))

    # por aparência: quantas contas da amostra a não têm (em toda a EU)
    eu = wants.sum(0)

    # por reino com preços: linhas do bitmap, e o total por aparência
    canon_name = {s: n for s, n, c in tsm.execute(
        "SELECT canonical_slug, canonical_name, is_canonical FROM tsm_realm_group") if c}
    by_realm_account: dict[str, list[int]] = defaultdict(list)
    for pos, realm in enumerate(realms):
        by_realm_account[realm].append(pos)

    # Os reinos ligados partilham casa de leilão: o ficheiro de 'auchindoun' serve
    # Auchindoun, Dunemaul, Sylvanas e Jaedenar.  Quem vende lá vende aos colecionadores
    # dos quatro, por isso o grupo inteiro é que é o mercado -- usar só o nome
    # canónico subestimava o pool (37 contas em vez de 217, neste caso).
    canon_of = {s: c for s, c in tsm.execute(
        "SELECT realm_slug, canonical_slug FROM tsm_realm_group")}
    group_of: dict[str, list[str]] = defaultdict(list)
    for slug, name in tsm.execute("SELECT realm_slug, realm_name FROM tsm_realm_group"):
        group_of[canon_of.get(slug, slug)].append(name)

    region = {r[0]: r[1:] for r in tsm.execute(
        f"SELECT key_id, sold_per_day, market_value FROM tsm_region_price "
        f"WHERE key_id IN ({ph})", ids)}
    saturated = {r[0] for r in tsm.execute(
        "SELECT key_id FROM tsm_region_price WHERE sale_rate>=1")}
    breadth = dict(tsm.execute(
        "SELECT item_id, COUNT(*) FROM tsm_realm_price WHERE market_value>0 "
        "GROUP BY item_id"))
    listings: dict[int, list[int]] = defaultdict(list)
    for iid, mb in tsm.execute(
            f"SELECT item_id, min_buyout FROM tsm_realm_price "
            f"WHERE min_buyout>0 AND item_id IN ({ph})", ids):
        listings[iid].append(mb)
    consensus = {i: statistics.median(v) for i, v in listings.items() if len(v) >= 8}
    names = {r[0]: r[1] for r in tsm.execute(
        f"SELECT itemId, name FROM db2_items WHERE itemId IN ({ph})", ids)}

    scopes = [r[0] for r in tsm.execute("SELECT DISTINCT scope FROM tsm_realm_price")]
    local: dict[str, dict[int, tuple]] = {s: {} for s in scopes}
    for scope, iid, mv, mb in tsm.execute(
            f"SELECT scope, item_id, market_value, min_buyout FROM tsm_realm_price "
            f"WHERE item_id IN ({ph})", ids):
        local.setdefault(scope, {})[iid] = (mv or 0, mb or 0)

    rows = []
    for scope in scopes:
        membros = group_of.get(scope) or [canon_name.get(scope, scope)]
        idx = [p for m in membros for p in by_realm_account.get(m, [])]
        if not idx:
            continue
        sub = wants[idx]
        for iid in ids:
            if iid in saturated or iid not in region:
                continue
            lam, rmv = region[iid]
            if lam <= 0 or rmv <= 0 or breadth.get(iid, 0) < a.min_realms:
                continue
            cols = item_cols[iid]
            buyers = max(int(sub[:, c].sum()) for c in cols)
            b_eu = max(int(eu[c]) for c in cols)
            if buyers <= 0 or b_eu <= 0:
                continue
            mv, mb = local.get(scope, {}).get(iid, (0, 0))
            n = 1 if mb > 0 else 0
            p = 1 - math.exp(-a.days * lam * buyers / b_eu / (n + 1))
            price = mb or mv or rmv
            med = consensus.get(iid)
            if a.cap and med:
                price = min(price, a.cap * med)
            rows.append({
                "item_id": iid, "item": names.get(iid, str(iid)), "qty": inv[iid],
                "realm": canon_name.get(scope, scope), "accounts": len(idx),
                "buyers": buyers,
                "P30": 100 * p, "price_g": price / 10_000,
                "gold_g": p * price / 10_000, "listed": "sim" if n else "nao",
                "thin": "!" if iid not in consensus else "",
            })

    per_item: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        per_item[r["item_id"]].append(r)
    key = (lambda r: -r["P30"]) if a.sort == "p" else (lambda r: -r["gold_g"])
    for v in per_item.values():
        v.sort(key=key)

    # --- material: sem reino, o mercado de commodities é um só em toda a região
    all_ph = ",".join("?" * len(inv))
    commodity = {r[0]: r[1:] for r in tsm.execute(
        f"SELECT key_id, market_value, min_buyout FROM tsm_commodity_price "
        f"WHERE key_id IN ({all_ph})", list(inv))}
    mat_gold = sum(inv[i] * ((c[1] or c[0]) or 0) for i, c in commodity.items()) / 10_000
    mat_units = sum(inv[i] for i in commodity)
    respondidos = {r["item_id"] for r in rows}

    # --- ao todo: quanto este inventário rende em cada reino, não peça a peça
    tot: dict[str, dict] = defaultdict(lambda: {"gold": 0.0, "items": 0,
                                                "empty": 0, "sales": 0.0, "accounts": 0,
                                                "top": 0.0, "top_item": ""})
    for r in rows:
        t = tot[r["realm"]]
        t["gold"] += r["gold_g"]
        t["items"] += 1
        t["sales"] += r["P30"] / 100
        t["empty"] += 1 if r["listed"] == "nao" else 0
        t["accounts"] = max(t["accounts"], r["accounts"])
        if r["gold_g"] > t["top"]:
            t["top"], t["top_item"] = r["gold_g"], r["item"]
    print(f"\nao todo, sem ser peça a peça: o inventário inteiro em cada reino, "
          f"{a.days} dias ({len(respondidos)} das {len(inv)} entradas entram no modelo)")
    hdr = (f"{'reino':20s} {'itens':>5s} {'prateleira':>10s} {'vendas':>7s} "
           f"{'ouro transmog':>13s} {'1 item':>7s} {'+ material':>11s} {'total':>11s}")
    print(hdr)
    print("-" * len(hdr))
    ordem = sorted(tot.items(), key=lambda kv: -kv[1]["gold"])
    for name, t in ordem[:a.top_realms]:
        peso = 100 * t["top"] / t["gold"] if t["gold"] else 0
        print(f"{name[:20]:20s} {t['items']:5d} {t['empty']:10d} {t['sales']:7.2f} "
              f"{t['gold']:13,.0f} {peso:6.0f}% {mat_gold:11,.0f} "
              f"{t['gold'] + mat_gold:11,.0f}")
    if ordem:
        print("  1 item = fatia do total que vem de uma única peça; acima de 50% "
              "a ordem destes reinos é decidida por essa peça")
        for name, t in ordem[:a.top_realms]:
            if t["top"] and t["top"] / t["gold"] > 0.5:
                print(f"     {name}: {t['top_item'][:40]} sozinho vale "
                      f"{t['top']:,.0f} dos {t['gold']:,.0f} g")
    if ordem:
        melhor, pior = ordem[0][1]["gold"], ordem[-1][1]["gold"]
        meio = sorted(t["gold"] for t in tot.values())[len(tot) // 2]
        print(f"  em {len(tot)} reinos: melhor {melhor:,.0f} g, mediano {meio:,.0f} g, "
              f"pior {pior:,.0f} g -- a escolha de reino vale {melhor - pior:,.0f} g em "
              f"{a.days} dias; o material é igual em todos")
        # O ouro de um reino vem dos compradores que lhe conhecemos, e conhecemos
        # só as contas que foram lidas lá.  Se a ordem acompanhar o tamanho da
        # amostra, ela está a medir a cobertura e não o reino.
        xs = [math.log(t["accounts"] + 1) for _, t in ordem]
        ys = [math.log(t["gold"] + 1) for _, t in ordem]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs)) or 1.0
        sy = math.sqrt(sum((y - my) ** 2 for y in ys)) or 1.0
        r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)
        contas = sorted(t["accounts"] for _, t in ordem)
        print(f"  AVISO: a ordem dos reinos acompanha o numero de contas lidas em "
              f"cada um (corr {r:.2f}); mediana {contas[len(contas) // 2]} contas por "
              f"reino, maximo {contas[-1]}")
        print(f"  Isso mede cobertura da amostra, nao populacao do reino. Com "
              f"corr acima de 0,6 nao escolhas reino por esta tabela.")

    if a.simulate:
        # A diferenca entre o 1.o e o 5.o reino e menor do que uma venda por mes:
        # simular as vendas diz se a ordem aguenta ou se e' sorte de uma amostra.
        import random
        rnd = random.Random(7)
        pares: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for r in rows:
            pares[r["realm"]].append((r["P30"] / 100, r["price_g"]))
        vitorias: dict[str, int] = defaultdict(int)
        media: dict[str, float] = defaultdict(float)
        for _ in range(a.simulate):
            mes = {k: sum(pr for p, pr in v if rnd.random() < p)
                   for k, v in pares.items()}
            for k, v in mes.items():
                media[k] += v
            for k, _v in sorted(mes.items(), key=lambda kv: -kv[1])[:3]:
                vitorias[k] += 1
        print(f"\n{a.simulate} meses simulados (cada listagem vende com prob P): "
              f"probabilidade de ficar no top 3")
        for k, c in sorted(vitorias.items(), key=lambda kv: -kv[1])[:a.top_realms]:
            print(f"  {k[:20]:20s} top 3 em {100*c/a.simulate:5.1f}% dos meses | "
                  f"media {media[k]/a.simulate:8,.0f} g")

    # --- cobertura: por que só parte do inventário tem resposta
    so_com = [i for i in inv if i in commodity and i not in respondidos]
    nada = [i for i in inv if i not in commodity and i not in respondidos]
    print(f"\ncobertura do inventário ({len(inv)} itens, {sum(inv.values())} unidades):")
    print(f"  com resposta por reino   {len(respondidos):4d} itens "
          f"{sum(inv[i] for i in respondidos):5d} unidades")
    print(f"  só material (commodity)  {len(so_com):4d} itens {mat_units:5d} unidades "
          f"-> {mat_gold:,.0f} g, mercado único da região")
    print(f"  fora dos dois            {len(nada):4d} itens "
          f"{sum(inv[i] for i in nada):5d} unidades  (receitas, itens sem preço)")
    if a.per_item:
        print(f"\ntop {a.top_realms} reinos de cada peça (ordenados por "
              f"{'probabilidade' if a.sort == 'p' else 'ouro esperado'}, {a.days} dias):")
        ranking = sorted(per_item.values(), key=lambda v: key(v[0]))
        for v in ranking[:a.top]:
            print(f"\n{v[0]['item'][:60]} ({v[0]['qty']}x){v[0]['thin']}")
            for pos, s in enumerate(v[:a.top_realms], 1):
                nota = "prateleira vazia" if s["listed"] == "nao" else "já há listagem"
                quem = "comprador " if s["buyers"] == 1 else "compradores"
                print(f"   {pos}. {s['realm'][:18]:18s} {s['buyers']:4d} {quem} "
                      f"{s['P30']:5.1f}%  {s['price_g']:12,.0f}g -> {s['gold_g']:8,.0f}g"
                      f"   ({nota})")

    if a.realm:
        want = a.realm.lower()
        sub = [r for r in rows if r["realm"].lower() == want]
        if not sub:
            print(f"\n!! sem dados para {a.realm}")
        else:
            sub.sort(key=lambda r: -r["gold_g"])
            print(f"\n{a.realm}: melhores peças do inventário "
                  f"({sum(1 for r in sub if not r['listed'])} sem listagem local)")
            print(f"{'item':32s} {'comp':>5s} {'P':>7s} {'preco':>11s} "
                  f"{'ouro':>9s} {'lista':>6s}")
            for r in sub[:a.top]:
                print(f"{r['item'][:32]:32s} {r['buyers']:5d} {r['P30']:6.1f}% "
                      f"{r['price_g']:10,.0f}g {r['gold_g']:8,.0f}g {r['listed']:>6s}{r['thin']}")

    if nada:
        nomes = [names.get(i) or tsm.execute(
            "SELECT name FROM db2_items WHERE itemId=?", (i,)).fetchone()
            for i in nada[:8]]
        print(f"  exemplos fora dos dois: {', '.join(str(n) for n in nomes[:5])}")
        print("  para material e receitas o preço é o caminho: "
              "python scripts/calc_price.py --inventory inventory.csv")

    print("\n'!' no item = preço sem mediana entre reinos (fonte única, não verificado)")
    print("  'já há listagem' = a tua divide a venda com ela; 'prateleira vazia' = N=0")

    if a.csv:
        # a mesma ordem que o ecra: rank 1 e' o melhor reino do item
        rank = {}
        for v in per_item.values():
            for pos, s in enumerate(v, 1):
                rank[(s["item_id"], s["realm"])] = pos
        for r in rows:
            r["rank"] = rank[(r["item_id"], r["realm"])]
        with open(a.csv, "w", encoding="utf-8", newline="") as fh:
            fields = ["rank"] + [f for f in rows[0] if f != "rank"]
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: (r["item_id"], r["rank"])))
        print(f"{len(rows)} pares (item, reino) -> {a.csv} "
              f"(coluna 'rank': 1 = melhor reino do item)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
