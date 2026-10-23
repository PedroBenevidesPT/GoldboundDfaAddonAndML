"""Probability that a listing sells -- no 48 h window, buyer pools per realm.

The 48 h model needed a sale rate per realm, which the TSM only publishes per
region.  This one starts from the other end: `dfa.db` holds a bitmap per scanned
account, so for every (item, realm) we can count **how many local collectors
still lack it**.  That pool, divided by the price competition, is what decides a
sale -- not a fixed number of hours.

    lambda_i,r = lambda_i,reg * (buyers_i,r / buyers_i,reg)
    P(30d)     = 1 - exp(-30 * lambda_i,r / (N_i + 1))
    E[gold]    = P * price

``lambda_i,reg`` is TSM's regional ``sold_per_day`` for the item.  Scaling it by
the realm's share of the buyers is unbiased even though the bitmap sample is only
a slice of the population: the ratio of two sample counts estimates the ratio of
two populations.  ``N_i`` is 1 when the realm already has a listing, so posting
into an empty shelf doubles the probability -- the only edge realm choice gives.

    python scripts/sale_probability.py
    python scripts/sale_probability.py --top 20
    python scripts/sale_probability.py --items Silvermoon --items-top 15
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

DFA_DB = Path(r"C:\economics\woweconomy\dfa\dfa.db")
TSM_DB = ROOT / "tsm_markets.db"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dfa-db", default=str(DFA_DB))
    ap.add_argument("--tsm-db", default=str(TSM_DB))
    ap.add_argument("--min-score", type=int, default=600_000)
    ap.add_argument("--days", type=int, default=30, help="janela da probabilidade")
    ap.add_argument("--cap", type=float, default=3.0,
                    help="tecto do preco local em multiplos da mediana entre reinos")
    ap.add_argument("--min-realms", type=int, default=5,
                    help="o item tem de ter mercado em N reinos para contar")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--bands", action="store_true",
                    help="tabela por banda de score (amostra vs populacao)")
    ap.add_argument("--where", type=int, default=0,
                    help="os N melhores pares (reino, item) com prateleira vazia")
    ap.add_argument("--min-buyers", type=int, default=3,
                    help="compradores locais minimos para entrar no --where")
    ap.add_argument("--items", default="", help="lista os melhores itens deste reino")
    ap.add_argument("--items-top", type=int, default=15)
    ap.add_argument("--csv", default="")
    a = ap.parse_args(argv)

    dfa = sqlite3.connect(f"file:{a.dfa_db}?mode=ro", uri=True)
    tsm = sqlite3.connect(a.tsm_db)
    wants, by_slot, realms = load_bitmaps(dfa, "appearances", a.min_score)
    scores = np.array([r[0] for r in dfa.execute(
        "SELECT COALESCE(c.score,0) FROM owned o JOIN account a ON a.account=o.account "
        "LEFT JOIN collector c ON c.region=a.region AND c.realm=a.realm AND c.name=a.name "
        "WHERE o.kind='appearances' AND a.region='EU' AND COALESCE(c.score,0)>=?",
        (a.min_score,))])
    print(f"amostra: {len(scores)} contas com bitmap, score >= {a.min_score:,}")

    # appearance -> BoE items.  An item is wanted when any of its appearances is.
    boe = {r[0] for r in tsm.execute("SELECT itemId FROM db2_items WHERE binding=2")}
    amap: dict[int, list[int]] = defaultdict(list)
    for iid, aid in tsm.execute("SELECT itemId, appearanceId FROM db2_item_appearance"):
        if iid in boe:
            amap[aid].append(iid)
    names = {r[0]: r[1] for r in tsm.execute("SELECT itemId, name FROM db2_items")}
    region = {r[0]: r[1:] for r in tsm.execute(
        "SELECT key_id, sold_per_day, market_value FROM tsm_region_price")}

    def item_pool(counts: np.ndarray) -> dict[int, int]:
        out: dict[int, int] = {}
        for pos, aid in enumerate(by_slot):
            n = int(counts[pos])
            if not n:
                continue
            for iid in amap.get(aid, ()):
                if n > out.get(iid, 0):
                    out[iid] = n
        return out

    if a.bands:
        bands = [(600_000, 700_000), (700_000, 800_000),
                 (800_000, 900_000), (900_000, 10 ** 9)]
        print(f"\n{'banda':12s} {'amostra':>8s} {'populacao':>10s} {'%medido':>8s} "
              f"{'faltam/conta':>13s} {'uniao':>7s} {'massa':>12s}")
        for lo, hi in bands:
            m = (scores >= lo) & (scores < hi)
            pop = dfa.execute("SELECT COUNT(*) FROM collector WHERE score>=? AND score<?",
                              (lo, hi)).fetchone()[0]
            if not m.any():
                print(f"{lo//1000:>4d}-{hi//1000:<6d}k {0:8d} {pop:10d} ")
                continue
            per = wants[m].sum(1)
            union = int(wants[m].any(0).sum())
            print(f"{lo//1000:>4d}-{hi//1000:<6d}k {int(m.sum()):8d} {pop:10d} "
                  f"{100*m.sum()/max(pop,1):7.1f}% {int(np.median(per)):13,d} "
                  f"{union:7,d} {int(per.sum()):12,d}")
        print("  massa = soma das faltas da amostra; populacao = linhas em collector")

    eu = item_pool(wants.sum(0))
    # Two guards against a rate read off a single observation.  TSM flags an item
    # as sale_rate 1.0 when every listing it saw disappeared, which is the same
    # as "this item was listed once" -- 15 items region-wide, and they were
    # leading every realm ranking before this.  Breadth is the second guard: an
    # item priced in one realm out of 92 has no regional market to speak of.
    saturated = {r[0] for r in tsm.execute(
        "SELECT key_id FROM tsm_region_price WHERE sale_rate>=1")}
    breadth = dict(tsm.execute(
        "SELECT item_id, COUNT(*) FROM tsm_realm_price WHERE market_value>0 "
        "GROUP BY item_id"))
    thin = 0
    priced = []
    for i in eu:
        if i not in region or region[i][1] <= 0 or region[i][0] <= 0 or i in saturated:
            continue
        if breadth.get(i, 0) < a.min_realms:
            thin += 1
            continue
        priced.append(i)
    allowed = set(priced)
    print(f"universo: {len(eu):,} itens BoE com comprador; {len(allowed):,} com vendas "
          f"e preco medidos ({len(saturated)} saturados e {thin} finos fora, "
          f"< {a.min_realms} reinos)")

    # cross-realm consensus, to keep one stale listing out of the gold column
    listings: dict[int, list[int]] = defaultdict(list)
    for iid, mb in tsm.execute("SELECT item_id, min_buyout FROM tsm_realm_price WHERE min_buyout>0"):
        if iid in eu:
            listings[iid].append(mb)
    consensus = {i: statistics.median(v) for i, v in listings.items() if len(v) >= 8}

    canon = {s: n for s, n, c in tsm.execute(
        "SELECT canonical_slug, canonical_name, is_canonical FROM tsm_realm_group") if c}
    by_realm: dict[str, list[int]] = defaultdict(list)
    for pos, r in enumerate(realms):
        by_realm[r].append(pos)

    # Reinos ligados partilham casa de leilão: o ficheiro de 'spinebreaker' serve
    # Spinebreaker, Dragonmaw, Vashj, Stormreaver e Haomarush.  O mercado é o grupo.
    canon_of = {s: c for s, c in tsm.execute(
        "SELECT realm_slug, canonical_slug FROM tsm_realm_group")}
    group_of: dict[str, list[str]] = defaultdict(list)
    for slug, rname in tsm.execute("SELECT realm_slug, realm_name FROM tsm_realm_group"):
        group_of[canon_of.get(slug, slug)].append(rname)

    rows = []
    spots: list[tuple] = []
    for scope, name in canon.items():
        membros = group_of.get(scope) or [name]
        idx = [p for m in membros for p in by_realm.get(m, [])]
        if not idx:
            continue
        pool = item_pool(wants[idx].sum(0))
        shelf = [i for i in pool if i in allowed]
        local = {r[0]: r[1:] for r in tsm.execute(
            "SELECT item_id, market_value, min_buyout FROM tsm_realm_price WHERE scope=?",
            (scope,))}
        probs, gold, empty, sales = [], 0.0, 0, 0.0
        for i in shelf:
            buyers, buyers_eu = pool[i], eu[i]
            lam = region[i][0] * buyers / buyers_eu
            mv, mb = local.get(i, (0, 0))
            n = 1 if mb > 0 else 0
            empty += 0 if n else 1
            p = 1 - math.exp(-a.days * lam / (n + 1))
            price = mb or mv or region[i][1]
            med = consensus.get(i)
            if a.cap and med:
                price = min(price, a.cap * med)
            probs.append(p)
            sales += p
            gold += p * price
            if a.where and n == 0 and buyers >= a.min_buyers:
                thin = i not in consensus
                spots.append((p * price, p, name, names.get(i) or str(i), buyers,
                              price, i, thin))
        if not shelf:
            continue
        rows.append({
            "realm": name, "slug": scope, "accounts": len(idx),
            "shelf": len(shelf), "empty": empty,
            "empty_pct": 100 * empty / len(shelf),
            "p_median": 100 * statistics.median(probs),
            "p_20": 100 * sum(1 for p in probs if p >= 0.20) / len(probs),
            "p_50": 100 * sum(1 for p in probs if p >= 0.50) / len(probs),
            "sales": sales, "gold": gold / 10_000,
        })

    rows.sort(key=lambda r: -r["gold"])
    hdr = (f"{'reino':22s} {'contas':>6s} {'prateleira':>10s} {'vazia':>6s} "
           f"{'P med':>6s} {'P>=20%':>7s} {'P>=50%':>7s} {'vendas':>7s} {'ouro':>9s}")
    print(f"\njanela: {a.days} dias | N = listagem existente no reino")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        print(f"{r['realm'][:22]:22s} {r['accounts']:6d} {r['shelf']:10,d} "
              f"{r['empty_pct']:5.0f}% {r['p_median']:5.1f}% {r['p_20']:6.0f}% "
              f"{r['p_50']:6.0f}% {r['sales']:7.1f} {r['gold']:9,.0f}")

    if a.items:
        want = a.items.lower()
        row = next((r for r in rows if r["realm"].lower() == want), None)
        if row is None:
            print(f"\n!! reino desconhecido: {a.items}")
            return 1
        idx = by_realm.get(row["realm"]) or []
        pool = item_pool(wants[idx].sum(0))
        local = {r[0]: r[1:] for r in tsm.execute(
            "SELECT item_id, market_value, min_buyout FROM tsm_realm_price WHERE scope=?",
            (row["slug"],))}
        best = []
        for i in pool:
            if i not in allowed:
                continue
            mv, mb = local.get(i, (0, 0))
            if mb > 0:
                continue                      # ja esta a venda: sem vantagem
            lam = region[i][0] * pool[i] / eu[i]
            p = 1 - math.exp(-a.days * lam)
            price = min(mv or region[i][1], a.cap * consensus.get(i, math.inf))
            best.append((p * price, p, i, pool[i], price))
        best.sort(reverse=True)
        print(f"\n{row['realm']}: itens com compradores, sem listagem local")
        print(f"{'item':38s} {'compradores':>11s} {'P':>7s} {'preco':>10s} {'ouro':>9s}")
        for g, p, i, b, price in best[:a.items_top]:
            print(f"{(names.get(i) or str(i))[:38]:38s} {b:11d} {100*p:6.1f}% "
                  f"{price/1e4:9,.0f}g {g/1e4:8,.0f}g")

    if a.where:
        spots.sort(reverse=True)
        print(f"\n{len(spots):,} pares (reino, item) com prateleira vazia e "
              f">= {a.min_buyers} compradores locais; melhores por ouro esperado:")
        print(f"{'reino':16s} {'item':30s} {'id':>7s} {'comp':>5s} {'P':>7s} "
              f"{'preco':>11s} {'ouro':>9s}")
        for g, p, realm, item, buyers, price, iid, thin in spots[:a.where]:
            print(f"{realm[:16]:16s} {item[:30]:30s} {iid:7d} {buyers:5d} {100*p:6.1f}% "
                  f"{price/1e4:10,.0f}g{g/1e4:8,.0f}g{' !' if thin else ''}")
        # probabilidade, nao ouro: so itens que pagam o esforco
        floor = 1_000 * 10_000
        good = [s for s in spots if s[5] >= floor]
        good.sort(key=lambda s: -s[1])
        print(f"\nmesmos pares por probabilidade (preco >= 1 000 g):")
        print(f"{'reino':16s} {'item':30s} {'id':>7s} {'comp':>5s} {'P':>7s} "
              f"{'preco':>11s} {'ouro':>9s}")
        for g, p, realm, item, buyers, price, iid, thin in good[:a.where]:
            print(f"{realm[:16]:16s} {item[:30]:30s} {iid:7d} {buyers:5d} {100*p:6.1f}% "
                  f"{price/1e4:10,.0f}g{g/1e4:8,.0f}g{' !' if thin else ''}")
        best: dict[str, tuple] = {}
        for s in spots:
            if s[2] not in best or s[0] > best[s[2]][0]:
                best[s[2]] = s
        print(f"\nmelhor oportunidade de cada reino ({len(best)} reinos com alguma):")
        for s in sorted(best.values(), reverse=True)[:a.where]:
            print(f"  {s[2][:18]:18s} {s[3][:30]:30s} {s[4]:3d} comprad. "
                  f"P {100*s[1]:4.1f}% {s[5]/1e4:9,.0f}g")
        print("  ! = sem consenso entre reinos: preco de fonte unica, nao verificado")

    if a.csv:
        with open(a.csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n{len(rows)} reinos -> {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
