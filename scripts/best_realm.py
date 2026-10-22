"""Em que cluster vender o inventario -- ranking com demanda, preco e concorrencia.

A pergunta nao e "que reino tem mais jogadores", e sim onde o teu stock concreto
encontra mais compradores por unidade de esforco.  O modelo tem tres pecas:

    demanda   m_i,c  = contas do segmento de topo no cluster c que ainda nao tem
                       a aparencia do item i (bitmaps DfA, census >= 900k)
    preco     p_i,c  = market value do cluster, limitado a [0.5, 2] x mediana
                       entre clusters (uma listagem de parede nao decide nada)
    venda     P30    = 1 - exp(-30 * lambda_i * (B_i,c/B_i,EU) / (N_i,c+1))

``lambda_i`` e o sold_per_day regional da TSM (queda para a mediana da faixa de
preco quando a TSM nao registou vendas); ``B_i,c`` e a contagem de compradores
com shrinkage empirico-Bayes para o cluster; ``N`` e 1 quando o item ja esta a
venda no cluster e 0 quando a prateleira esta vazia.  O preco de venda e o
market value do cluster (nao o min_buyout, que e uma parede), descontado dos 5 %
da casa de leilao.

Duas amostras DfA, de proposito:
  * ``eu`` -- a escada regional, todos os reinos, so o topo (>= 680k).  E a
    unica frame comparavel entre os 92 clusters.
  * ``local`` -- varredura profunda do cluster Dragonmaw (3 108 contas, ate 92k),
    usada para medir a inclinacao da escada, nao para comparar com os outros.
O segmento >= 900k esta 100 % bitmapped (1 904 contas); abaixo disso a cobertura
cai para 40 % (800-900k) e 2 % (680-800k), por isso nao entra no modelo.

    python scripts/best_realm.py
    python scripts/best_realm.py --basket farm --csv reports/melhor_reino.csv
    python scripts/best_realm.py --basket rare --shrinkage 5
    python scripts/best_realm.py --sensitivity

Nao escreve nas bases: abre-as em ``mode=ro``.
"""
from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import statistics
import sys
import unicodedata
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DFA_DB = Path(r"C:\economics\woweconomy\dfa\dfa.db")
TSM_DB = ROOT / "tsm_markets.db"
INVENTORY = ROOT / "inventory.csv"

SEGMENT = 900_000
DENIED_SOURCES = ("vendor", "trading post", "quest", "profession", "achievement")
# sold_per_day mediano por faixa de preco, medido em reports/analise_reinos_48h.md
BAND_LAMBDA = ((100, 0.0165), (1_000, 0.011), (10_000, 0.009),
               (100_000, 0.007), (float("inf"), 0.005))


try:                      # consola Windows com acentos
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def norm(value: str) -> str:
    """Chave tolerante a mojibake e acentos (as duas bases nao codificam igual)."""
    s = (value or "").replace("\ufffd", "?")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return s.replace("'", "").replace(" ", "").replace("-", "")


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


@dataclass
class Cluster:
    canonical: str
    slug: str
    members: list[str] = field(default_factory=list)
    n_top: int = 0
    depth100: int = 0


@dataclass
class Shelf:
    item_id: int
    name: str
    qty: int
    appearances: list[int]
    region_price: float          # copper
    source: str = ""


def load_clusters(tsm: sqlite3.Connection) -> tuple[dict[str, Cluster], dict[str, str]]:
    clusters: dict[str, Cluster] = {}
    realm2cluster: dict[str, str] = {}
    for slug, realm, canonical, is_canonical in tsm.execute(
            "SELECT realm_slug, realm_name, canonical_name, is_canonical "
            "FROM tsm_realm_group"):
        key = norm(realm)
        realm2cluster[key] = canonical
        node = clusters.setdefault(canonical, Cluster(canonical, slug))
        node.members.append(realm)
        if is_canonical:
            node.slug = slug
    for node in clusters.values():
        node.members.sort()
    return clusters, realm2cluster


def load_segment(dfa: sqlite3.Connection, realm2cluster: dict[str, str],
                 threshold: int):
    """Bitmaps do segmento de topo + o cluster de cada conta.

    Junta o score por conta (maximo entre personagens): a tabela ``collector``
    tem uma linha por personagem e a mesma colecao Bnet aparece em varias.
    """
    slots = dfa.execute(
        "SELECT slot, item_id FROM item_slot WHERE kind='appearances' ORDER BY slot"
    ).fetchall()
    if not slots:
        raise SystemExit("erro: item_slot vazio para appearances")
    width = max(slot for slot, _ in slots) // 8 + 1
    nslots = len(slots)

    rows = dfa.execute(
        "SELECT o.account, a.realm, COALESCE(sc.s, 0), o.bits, o.slots "
        "FROM owned o JOIN account a ON a.account=o.account "
        "LEFT JOIN (SELECT account, MAX(score) AS s FROM collector GROUP BY account) sc "
        "  ON sc.account=o.account "
        "WHERE o.kind='appearances'").fetchall()
    chosen = [r for r in rows if int(r[2]) >= threshold]
    if not chosen:
        raise SystemExit(f"erro: nenhuma conta com score >= {threshold}")
    short = [r for r in chosen if int(r[4]) < nslots]
    if short:
        raise SystemExit(f"erro: {len(short)} bitmaps com slots < {nslots}")

    raw = np.zeros((len(chosen), width), dtype=np.uint8)
    clusters: list[str] = []
    for pos, (_acc, realm, _score, blob, _slots) in enumerate(chosen):
        data = zlib.decompress(blob)
        take = min(width, len(data))
        raw[pos, :take] = np.frombuffer(data[:take], dtype=np.uint8)
        clusters.append(realm2cluster.get(norm(realm), "?"))
    bits = np.unpackbits(raw, axis=1, bitorder="little")[:, :nslots].astype(bool)
    return bits, clusters, {int(app): i for i, (_slot, app) in enumerate(slots)}


def depth_of(dfa: sqlite3.Connection, realm2cluster: dict[str, str], rank: int = 100):
    """Score do N-esimo melhor colector do cluster (medida de profundidade).

    Deduplica por conta: varias personagens da mesma Bnet partilham a colecao.
    """
    best: dict[str, tuple[str, int]] = {}
    for account, realm, score in dfa.execute("SELECT account, realm, score FROM collector"):
        cluster = realm2cluster.get(norm(realm))
        if cluster is None:
            continue
        if account not in best or score > best[account][1]:
            best[account] = (cluster, score)
    by_cluster: dict[str, list[int]] = defaultdict(list)
    for cluster, score in best.values():
        by_cluster[cluster].append(score)
    out = {}
    for cluster, scores in by_cluster.items():
        scores.sort(reverse=True)
        out[cluster] = scores[rank - 1] if len(scores) >= rank else scores[-1]
    return out


def load_inventory(tsm: sqlite3.Connection) -> dict[int, int]:
    if not INVENTORY.exists():
        return {}
    qty: dict[int, int] = {}
    with INVENTORY.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        next(reader, None)
        for row in reader:
            try:
                qty[int(row[0])] = int(row[6])
            except (ValueError, IndexError):
                continue
    return qty


def item_meta(tsm: sqlite3.Connection, ids: set[int] | None = None):
    where = ""
    params: list = []
    if ids:
        where = " WHERE i.itemId IN (%s)" % ",".join("?" * len(ids))
        params = list(ids)
    sql = ("SELECT i.itemId, i.name, i.binding, i.era, "
           "(SELECT COUNT(*) FROM db2_item_appearance a WHERE a.itemId=i.itemId) "
           "FROM db2_items i" + where)
    return {r[0]: r for r in tsm.execute(sql, params)}


def appearances_of(tsm: sqlite3.Connection, ids: set[int] | None = None):
    where = ""
    params: list = []
    if ids:
        where = " WHERE itemId IN (%s)" % ",".join("?" * len(ids))
        params = list(ids)
    out: dict[int, list[int]] = defaultdict(list)
    for item_id, appearance in tsm.execute(
            "SELECT itemId, appearanceId FROM db2_item_appearance" + where, params):
        out[item_id].append(appearance)
    return out


def load_sources(dfa: sqlite3.Connection) -> dict[int, str]:
    return {int(app): (src or "") for app, src in dfa.execute(
        "SELECT item_id, source FROM catalogue WHERE kind='appearances' AND obtainable=1")}


def build_baskets(tsm: sqlite3.Connection, dfa: sqlite3.Connection,
                  inventory: dict[int, int]) -> dict[str, list[Shelf]]:
    """Inventario + dois cabazes de aquisicao, com preco regional da TSM."""
    meta = item_meta(tsm)
    apps = appearances_of(tsm)
    region = {int(i): float(m or 0) for i, m in tsm.execute(
        "SELECT key_id, market_value FROM tsm_region_price")}
    names = {r[0]: r[1] for r in tsm.execute("SELECT itemId, name FROM db2_items")}

    def shelf(item_id: int, qty: int) -> Shelf | None:
        row = meta.get(item_id)
        if not row or row[2] != 2 or not apps.get(item_id):
            return None
        return Shelf(item_id, row[1], qty, apps[item_id], region.get(item_id, 0.0))

    inv: list[Shelf] = []
    for item_id, qty in inventory.items():
        s = shelf(item_id, qty)
        if s:
            inv.append(s)
    inv.sort(key=lambda s: -s.region_price)

    sources = load_sources(dfa)
    inv_ids = {s.item_id for s in inv}
    candidates: list[Shelf] = []
    for item_id, row in meta.items():
        if row[2] != 2 or row[3] != 0 or item_id in inv_ids:
            continue
        if not apps.get(item_id):
            continue
        price = region.get(item_id, 0.0)
        if price <= 0:
            continue
        srcs = [sources.get(a, "") for a in apps[item_id]]
        if not any(src and not any(t in src.lower() for t in DENIED_SOURCES) for src in srcs):
            continue
        candidates.append(Shelf(item_id, row[1], 1, apps[item_id], price,
                                next((s for s in srcs if s), "")))

    return {"inventory": inv, "farm": [], "rare": [],
            "candidates": candidates}


def select_by_demand(candidates: list[Shelf], bits: np.ndarray,
                     slot_of: dict[int, int], farm_top: int, rare_top: int):
    """Escolhe os cabazes de aquisicao por ouro em falta (wanters x preco).

    Ordenar so por preco trazia as pecas mais caras, nao as que tem compradores.
    O ouro em falta no segmento de topo e a medida que interessa a quem vai
    farmar: quantos colectores ainda querem a peca, vezes o preco a que ela sai.
    """
    scored: list[tuple[float, Shelf]] = []
    for shelf in candidates:
        cols = [slot_of[a] for a in shelf.appearances if a in slot_of]
        if not cols:
            continue
        wanters = int((~bits[:, cols].any(axis=1)).sum())
        scored.append((wanters * shelf.region_price, shelf))
    scored.sort(key=lambda pair: -pair[0])
    farm = [s for _g, s in scored if 100e4 <= s.region_price <= 100_000e4][:farm_top]
    rare = [s for _g, s in scored if s.region_price > 100_000e4][:rare_top]
    return farm, rare


def missing_matrix(bits: np.ndarray, clusters: list[str], basket: list[Shelf],
                   slot_of: dict[int, int]) -> tuple[list[str], np.ndarray, np.ndarray]:
    names = sorted(set(clusters))
    index = {c: i for i, c in enumerate(names)}
    indicator = np.zeros((len(names), len(clusters)), dtype=np.float32)
    for col, cluster in enumerate(clusters):
        indicator[index[cluster], col] = 1.0
    counts = np.zeros((len(names), len(basket)), dtype=np.float32)
    for j, shelf in enumerate(basket):
        cols = [slot_of[a] for a in shelf.appearances if a in slot_of]
        if not cols:
            continue
        missing = ~bits[:, cols].any(axis=1)
        counts[:, j] = indicator @ missing.astype(np.float32)
    return names, counts, indicator.sum(axis=1)


def load_prices(tsm: sqlite3.Connection, realm2cluster: dict[str, str],
                basket: list[Shelf]):
    """(market, min_buyout, recent, historical) por (cluster, item)."""
    wanted = {s.item_id for s in basket}
    cluster_of = {}
    for slug, canonical in tsm.execute("SELECT realm_slug, canonical_name FROM tsm_realm_group"):
        cluster_of[slug] = canonical
    prices: dict[str, dict[int, tuple[float, float, float, float]]] = defaultdict(dict)
    for scope, item_id, market, buyout, recent, hist in tsm.execute(
            "SELECT scope, item_id, market_value, min_buyout, recent, historical "
            "FROM tsm_realm_price"):
        if item_id not in wanted:
            continue
        canonical = cluster_of.get(scope)
        if canonical is None:
            continue
        prices[canonical][item_id] = (float(market or 0), float(buyout or 0),
                                      float(recent or 0), float(hist or 0))
    return prices


def band_lambda(gold: float) -> float:
    for ceiling, value in BAND_LAMBDA:
        if gold < ceiling:
            return value
    return BAND_LAMBDA[-1][1]


@dataclass
class Row:
    cluster: str
    members: str
    n_top: int
    depth100: int
    priced: int
    listed: int
    listed_pct: float
    gold30: float
    sales30: float
    gold_per_100: float
    addressable: float
    undercut: float
    decay: float
    auc_per_item: float
    confidence: str


def rank(clusters: dict[str, Cluster], names: list[str], counts: np.ndarray,
         n_top: np.ndarray, prices, region: dict[int, tuple[float, float]],
         basket: list[Shelf], shrinkage: float = 10.0, price_cap: float = 2.0,
         force_listed: bool = False) -> list[Row]:
    total_missing = counts.sum(axis=0)
    total_accounts = float(n_top.sum())

    anchors = {}
    for j, shelf in enumerate(basket):
        vals = [prices.get(c, {}).get(shelf.item_id, (0, 0, 0, 0))[0]
                for c in names]
        vals = [v for v in vals if v > 0]
        anchors[shelf.item_id] = statistics.median(vals) if vals else shelf.region_price

    out: list[Row] = []
    for ci, cluster in enumerate(names):
        node = clusters.get(cluster)
        n = float(n_top[ci]) or 1.0
        gold = sales = addressable = 0.0
        priced = listed = 0
        undercuts: list[float] = []
        decays: list[float] = []
        for j, shelf in enumerate(basket):
            anchor = anchors[shelf.item_id]
            if anchor <= 0:
                continue
            market, buyout, recent, hist = prices.get(cluster, {}).get(
                shelf.item_id, (0.0, 0.0, 0.0, 0.0))
            if market <= 0 and buyout <= 0 and anchor <= 0:
                continue
            # Sem linha da TSM no cluster, o item nao esta a venda la: entra com
            # o preco-ancora entre clusters e N=0.  Ignora-lo por completo
            # favorecia os clusters com pior cobertura de precos.
            has_data = market > 0 or buyout > 0
            if has_data:
                priced += 1
            is_listed = buyout > 0
            listed += int(is_listed)
            price = market if market > 0 else (buyout if buyout > 0 else anchor)
            if anchor > 0:
                price = min(price, price_cap * anchor)
                price = max(price, 0.5 * anchor)
            if buyout > 0 and market > 0:
                undercuts.append(buyout / market)
            if recent > 0 and hist > 0:
                decays.append(recent / hist)

            missing = float(counts[ci, j])
            prior = float(total_missing[j]) / total_accounts if total_accounts else 0.0
            rate = (missing + shrinkage * prior) / (n + shrinkage)
            buyers = rate * n
            lam = region.get(shelf.item_id, (0.0, 0.0))[1]
            if lam <= 0:
                lam = band_lambda(anchor / 10_000)
            share = buyers / max(float(total_missing[j]), 1e-9)
            # Sem dados da TSM nao se sabe se a prateleira esta vazia: entra com
            # N=1 (nao dar bonus de "sem concorrencia" a quem nao foi lido).
            ncomp = 1.0 if (is_listed or not has_data or force_listed) else 0.0
            p30 = 1.0 - math.exp(-30.0 * lam * share / (ncomp + 1.0))
            p30 = min(max(p30, 0.0), 0.95)
            gold += p30 * (price / 10_000) * 0.95 * shelf.qty
            sales += p30 * shelf.qty
            addressable += buyers * (price / 10_000) * shelf.qty

        confidence = "alta" if (n >= 30 and priced >= 0.9 * len(basket)) else \
                     "media" if (n >= 15 and priced >= 0.75 * len(basket)) else "baixa"
        out.append(Row(
            cluster=cluster,
            members=", ".join(node.members) if node and len(node.members) > 1 else cluster,
            n_top=int(n_top[ci]),
            depth100=node.depth100 if node else 0,
            priced=priced,
            listed=listed,
            listed_pct=(listed / priced * 100 if priced else 0.0),
            gold30=gold,
            sales30=sales,
            gold_per_100=(gold / 15.0 / priced * 100 if priced else 0.0),
            addressable=addressable,
            undercut=statistics.median(undercuts) if undercuts else 0.0,
            decay=statistics.median(decays) if decays else 0.0,
            auc_per_item=0.0,
            confidence=confidence,
        ))
    out.sort(key=lambda r: (-r.gold30, -r.n_top))
    return out


def add_auction_depth(rows: list[Row], clusters: dict[str, Cluster],
                      tsm: sqlite3.Connection, basket: list[Shelf]) -> None:
    """num_auctions por item, so nos clusters onde o AppData da TSM o tem.

    E um retrato antigo (a tabela tsm_market nao e refrescada pelo CSV diario),
    por isso serve de sinal de profundidade, nunca de preco.
    """
    wanted = {s.item_id for s in basket}
    per_cluster: dict[str, list[int]] = defaultdict(list)
    realm_to_cluster = {}
    for node in clusters.values():
        for member in node.members:
            realm_to_cluster[member] = node.canonical
    for realm, item_id, auctions in tsm.execute(
            "SELECT realm, itemId, num_auctions FROM tsm_market"):
        canonical = realm_to_cluster.get(realm)
        if canonical and item_id in wanted and auctions is not None:
            per_cluster[canonical].append(int(auctions))
    for row in rows:
        vals = per_cluster.get(row.cluster, [])
        row.auc_per_item = sum(vals) / len(vals) if vals else 0.0


def explain_items(basket: list[Shelf], names: list[str], counts: np.ndarray,
                  n_top: np.ndarray, prices, region, cluster: str,
                  shrinkage: float) -> list[dict]:
    """Tabela item a item do cabaz no cluster escolhido.

    Serve para a decisao operacional: o que listar primeiro, o que segurar e o
    que nao vale a pena comprar.  ``P30`` e a probabilidade de venda em 30 dias
    do modelo, nao uma taxa observada.
    """
    ci = names.index(cluster)
    total = counts.sum(axis=0)
    total_accounts = float(n_top.sum())
    n = float(n_top[ci]) or 1.0
    anchors = {}
    for shelf in basket:
        vals = [prices.get(c, {}).get(shelf.item_id, (0, 0, 0, 0))[0] for c in names]
        vals = [v for v in vals if v > 0]
        anchors[shelf.item_id] = statistics.median(vals) if vals else shelf.region_price
    out: list[dict] = []
    for j, shelf in enumerate(basket):
        anchor = anchors[shelf.item_id]
        market, buyout, _recent, _hist = prices.get(cluster, {}).get(
            shelf.item_id, (0.0, 0.0, 0.0, 0.0))
        has_data = market > 0 or buyout > 0
        price = market if market > 0 else (buyout if buyout > 0 else anchor)
        if price > 0 and anchor > 0:
            price = min(max(price, 0.5 * anchor), 2.0 * anchor)
        missing = float(counts[ci, j])
        prior = float(total[j]) / total_accounts if total_accounts else 0.0
        rate = (missing + shrinkage * prior) / (n + shrinkage)
        buyers = rate * n
        lam = region.get(shelf.item_id, (0.0, 0.0))[1]
        if lam <= 0:
            lam = band_lambda(anchor / 10_000)
        p30 = 1.0 - math.exp(-30.0 * lam * (buyers / max(float(total[j]), 1e-9))
                             / (2.0 if (buyout > 0 or not has_data) else 1.0))
        p30 = min(max(p30, 0.0), 0.95)
        out.append({
            "item_id": shelf.item_id, "name": shelf.name, "qty": shelf.qty,
            "eu_wanters": int(total[j]), "eu_rate": (float(total[j]) / total_accounts
                                                     if total_accounts else 0.0),
            "cluster_missing": int(missing), "cluster_n": int(n),
            "price_gold": price / 10_000, "region_gold": shelf.region_price / 10_000,
            "p30": p30, "listed": buyout > 0, "source": shelf.source,
        })
    return out


def combined_scores(rows_by_basket: dict[str, list[Row]],
                    weights: dict[str, float]) -> list[dict]:
    """Indice de decisao entre cabazes (nao e uma saida do modelo de venda).

    Cada cabaz e normalizado pelo seu maximo e entra com um peso declarado; o
    cabaz de raras, que e o menos fiavel, leva meio peso quando a confianca e
    baixa.  Serve para desempatar clusters que o modelo nao separa -- a decisao
    fica reproduzivel em vez de ser uma conta de cabeca no relatorio.
    """
    maximo = {k: max((r.gold30 for r in rows), default=0.0) or 1.0
              for k, rows in rows_by_basket.items()}
    ranks = {}
    for k, rows in rows_by_basket.items():
        for pos, row in enumerate(rows, 1):
            ranks[(k, row.cluster)] = pos
    table: dict[str, dict] = {}
    for k, rows in rows_by_basket.items():
        for row in rows:
            node = table.setdefault(row.cluster, {
                "cluster": row.cluster, "members": row.members, "score": 0.0})
            node[k] = row.gold30
            node[f"{k}_rank"] = ranks[(k, row.cluster)]
            mult = 0.5 if (k == "rare" and row.confidence == "baixa") else 1.0
            node["score"] += weights[k] * row.gold30 / maximo[k] * mult
    return sorted(table.values(), key=lambda r: -r["score"])


def print_table(rows: list[Row], top: int) -> None:
    print(f"{'#':>3} {'cluster':30s} {'n':>4} {'d100':>8} {'preco':>3} "
          f"{'lista%':>6} {'ouro/30d':>9} {'vendas':>6} {'/100l':>7} {'conf':>5}")
    print("-" * 100)
    for pos, r in enumerate(rows[:top], 1):
        print(f"{pos:>3} {r.cluster[:30]:30s} {r.n_top:>4} {r.depth100:>8,} "
              f"{r.priced:>3} {r.listed_pct:>5.0f}% {r.gold30:>9,.0f} "
              f"{r.sales30:>6.2f} {r.gold_per_100:>7,.0f} {r.confidence:>5}")


def write_csv(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["cluster", "members", "n_top", "depth100", "priced",
                         "listed", "listed_pct", "gold30", "sales30",
                         "gold_per_100_listings", "addressable_gold",
                         "undercut_ratio", "decay_ratio", "auctions_per_item",
                         "confidence"])
        for r in rows:
            writer.writerow([r.cluster, r.members, r.n_top, r.depth100, r.priced,
                             r.listed, round(r.listed_pct, 1), round(r.gold30, 1),
                             round(r.sales30, 3), round(r.gold_per_100, 1),
                             round(r.addressable, 0), round(r.undercut, 3),
                             round(r.decay, 3), round(r.auc_per_item, 2),
                             r.confidence])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dfa-db", default=str(DFA_DB))
    ap.add_argument("--tsm-db", default=str(TSM_DB))
    ap.add_argument("--basket", choices=("inventory", "farm", "rare"), default="inventory")
    ap.add_argument("--min-score", type=int, default=SEGMENT)
    ap.add_argument("--shrinkage", type=float, default=10.0,
                    help="peso do prior EU no rate por cluster (default 10)")
    ap.add_argument("--price-cap", type=float, default=2.0,
                    help="tecto do preco do cluster face a mediana entre clusters")
    ap.add_argument("--farm-top", type=int, default=150)
    ap.add_argument("--rare-top", type=int, default=50)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--csv", default="")
    ap.add_argument("--explain", default="",
                    help="cluster a detalhar item a item (ex.: Draenor)")
    ap.add_argument("--items-csv", default="")
    ap.add_argument("--all", action="store_true", help="imprime os 92 clusters")
    ap.add_argument("--combined", action="store_true",
                    help="corre os tres cabazes e imprime o indice de decisao")
    ap.add_argument("--weights", default="0.15,0.60,0.25",
                    help="pesos inventario,aquisicao,raras (default 0.15,0.60,0.25)")
    ap.add_argument("--sensitivity", action="store_true",
                    help="varre shrink/price-cap/basket e diz se o vencedor muda")
    args = ap.parse_args(argv)

    dfa = open_ro(Path(args.dfa_db))
    tsm = open_ro(Path(args.tsm_db))
    clusters, realm2cluster = load_clusters(tsm)
    depth = depth_of(dfa, realm2cluster)
    for name, node in clusters.items():
        node.depth100 = depth.get(name, 0)

    bits, account_clusters, slot_of = load_segment(dfa, realm2cluster, args.min_score)
    inventory = load_inventory(tsm)
    baskets = build_baskets(tsm, dfa, inventory)
    farm, rare = select_by_demand(baskets["candidates"], bits, slot_of,
                                  args.farm_top, args.rare_top)
    baskets["farm"], baskets["rare"] = farm, rare
    wanted = ("inventory", "farm", "rare") if args.combined else (args.basket,)
    for key in wanted:
        if not baskets[key]:
            raise SystemExit(f"erro: cabaz '{key}' vazio")

    region = {int(i): (float(m or 0), float(s or 0)) for i, m, s in tsm.execute(
        "SELECT key_id, market_value, sold_per_day FROM tsm_region_price")}
    computed: dict[str, tuple] = {}
    for key in wanted:
        shelf = baskets[key]
        names_k, counts_k, n_top_k = missing_matrix(bits, account_clusters, shelf, slot_of)
        prices_k = load_prices(tsm, realm2cluster, shelf)
        rows_k = rank(clusters, names_k, counts_k, n_top_k, prices_k, region, shelf,
                      shrinkage=args.shrinkage, price_cap=args.price_cap)
        add_auction_depth(rows_k, clusters, tsm, shelf)
        computed[key] = (shelf, names_k, counts_k, n_top_k, prices_k, rows_k)

    basket, names, counts, n_top, prices, rows = computed[args.basket]
    if args.combined:
        weights = dict(zip(("inventory", "farm", "rare"),
                           (float(x) for x in args.weights.split(","))))
        table = combined_scores({k: v[5] for k, v in computed.items()}, weights)
        print(f"# indice combinado | pesos inv={weights['inventory']:.2f} "
              f"farm={weights['farm']:.2f} rare={weights['rare']:.2f} "
              f"(raras baixa confianca x0.5) | {bits.shape[0]} contas")
        print(f"{'#':>3} {'cluster':28} {'score':>6} {'inv':>7} {'farm':>9} "
              f"{'rare':>11} {'r(inv/farm/rare)':>17}")
        for pos, item in enumerate(table[:len(table) if args.all else args.top], 1):
            print(f"{pos:>3} {item['cluster'][:28]:28} {item['score']:>6.3f} "
                  f"{item.get('inventory', 0):>7,.0f} {item.get('farm', 0):>9,.0f} "
                  f"{item.get('rare', 0):>11,.0f} "
                  f"{item.get('inventory_rank', 0):>5}/{item.get('farm_rank', 0):<5}/"
                  f"{item.get('rare_rank', 0):<5}")
        if args.csv:
            path = Path(args.csv)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["cluster", "members", "score", "inventory_gold",
                                 "inventory_rank", "farm_gold", "farm_rank",
                                 "rare_gold", "rare_rank"])
                for item in table:
                    writer.writerow([item["cluster"], item["members"],
                                     round(item["score"], 4),
                                     round(item.get("inventory", 0), 1),
                                     item.get("inventory_rank", 0),
                                     round(item.get("farm", 0), 1),
                                     item.get("farm_rank", 0),
                                     round(item.get("rare", 0), 1),
                                     item.get("rare_rank", 0)])
            print(f"\ncsv -> {path}")
    else:
        covered = int((n_top > 0).sum())
        print(f"# cabaz={args.basket} ({len(basket)} itens) | segmento >= {args.min_score:,} "
              f"| {bits.shape[0]} contas em {covered} clusters | TSM {args.tsm_db}")
        missing_any = int((counts.sum(axis=0) > 0).sum())
        print(f"# itens com pelo menos um comprador no segmento: {missing_any}/{len(basket)}")
        print()
        print_table(rows, len(rows) if args.all else args.top)

    if args.sensitivity:
        print("\n## sensibilidade")
        seen = {}
        for shrink in (5.0, 10.0, 20.0):
            for cap in (1.5, 2.0, 3.0):
                alt = rank(clusters, names, counts, n_top, prices, region, basket,
                           shrinkage=shrink, price_cap=cap)
                seen.setdefault((shrink, cap), alt[0].cluster)
        for (shrink, cap), winner in sorted(seen.items()):
            print(f"  shrink={shrink:<4} cap={cap:<3} -> {winner}")
        winners = sorted(set(seen.values()))
        print(f"  vencedores distintos: {len(winners)} -> {', '.join(winners)}")

    if args.explain:
        items = explain_items(basket, names, counts, n_top, prices, region,
                              args.explain, args.shrinkage)
        items.sort(key=lambda r: (-r["cluster_missing"] * r["price_gold"], -r["eu_wanters"]))
        print(f"\n## itens em {args.explain} (n={items[0]['cluster_n'] if items else 0})")
        print(f"{'item':>8} {'qty':>3} {'EU':>5} {'EU%':>5} {'falta':>5} "
              f"{'preco':>10} {'P30':>6} {'L':>1}  nome")
        for r in items:
            print(f"{r['item_id']:>8} {r['qty']:>3} {r['eu_wanters']:>5} "
                  f"{100 * r['eu_rate']:>4.0f}% {r['cluster_missing']:>5} "
                  f"{r['price_gold']:>10,.0f} {100 * r['p30']:>5.1f}% "
                  f"{'S' if r['listed'] else '-'}  {r['name'][:44]}")
        if args.items_csv:
            path = Path(args.items_csv)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(items[0]))
                writer.writeheader()
                writer.writerows(items)
            print(f"itens csv -> {path}")

    if args.csv and not args.combined:
        write_csv(rows, Path(args.csv))
        print(f"\ncsv -> {args.csv}")

    dfa.close()
    tsm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
