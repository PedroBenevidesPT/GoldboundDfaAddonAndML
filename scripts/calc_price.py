"""Preco justo de BoE / transmog, direto do TSM AppData (zero typing).

O QUE MUDOU
-----------
O dump antigo (tsm_markets.db -> market_snapshots) so tinha 5 campos:
marketValue, historical, avgSalePrice, saleRate, soldPerDay.  Faltava
DBRegionMarketAvg, DBRegionHistorical e DBMinBuyout, por isso tinhas de colar o
tooltip a mao.  Agora le-se o ficheiro que a App do TSM ja escreve no PC:

    TradeSkillMaster_AppHelper/AppData.lua

e ele tem TUDO, por realm (Twisting Nether, Dragonmaw, ...) e por regiao (EU):
    per realm : minBuyout, marketValue(DBMarket), numAuctions, historical
    per region: regionMarketValue, regionHistorical, regionSaleAvg,
                regionSoldPerDay, regionSaleRate

COMO SE CALCULA (o "cruzamento")
-------------------------------
1. PRECO  -> core = mediana(DBMarket, DBHistorical, DBRegionMarketAvg,
                           DBRegionHistorical).  A mediana resiste a 1 post
   inflacionado (o problema do DBMarket de 14 dias).  min() nao basta porque
   uma listagem barata velha tambem arrasta.
2. VENDA  -> DBRegionSaleAvg so entra quando ha prova (rate/daily suficientes).
   Se estiver corrompido (ex. 522g numa peca de 170k) e DESCARTA, nao se usa.
3. LIQUIDEZ -> rate/daily decidem a paciencia, mas NAO mudam o valor.  O preco
   conclui-se SEMPRE a partir da oferta (a mediana) mais a venda SO quando ela
   bate certo com a oferta.  Nao ha "incerto": da-se um preco e um grau de
   confianca ALTA/MEDIA/BAIXA consoante os dados.
4. UNICIDADE (0-100) -> junta competicao (numAuctions), fluxo (auctions/dia) e
   raridade (drop rate).  Modelo unico + pouca oferta = podes pedir mais.
5. PISO -> vendor (nunca abaixo) + custo de relist esperado
   (1/rate - 1) * 60% vendor.  Se o valor nao chega -> LIQUIDAR.
6. VELOCIDADE -> se NAO houver oferta atual (ninguem a vender), o "valor de
   mercado" e uma referencia morta; aí o preco e ajustado ao SALE RATE real
   para se vender em ~1 a cada 10 posts (--target-posts), em vez de pedir um
   valor que so sairia ao fim de milhares de reposts.
7. ANCORA (escolha do dono do farm, 2026-10-16, com dados reais): com parede
   local manda a parede (--realm-cap); SEM parede local, manda a VENDA real:
   teto = DBRegionSaleAvg x --sale-cap (3.0).  Quando este teto morde, o
   desconto por velocidade NAO se aplica por cima (era duplo castigo: o
   Fingerbone saia a 15g em vez de 1.568g).
8. SNIPES -> DBRegionSaleAvg e a media de COMPRAS, nao de posts: inclui quem
   comprou barato para revender (snipes/flips) e nao inclui quem nao usa TSM.
   Se cair ABAIXO do MinBuyout atual, nao e preco: e prova de compra barata e
   e DESCARTADO como ancora (nunca arrasta o valor para baixo).
9. TSM SUBCONTABILIZA -> rate/daily so contam vendas de utilizadores do TSM.
   --undercount 2.20 (calibrado em 204 vendas reais); 1.0 = TSM puro.
10. CAP DO REINO -> a mediana regional pode estar inflacionada para o teu reino
   (pouca oferta local, posts de outros servidores).  Com oferta local, o preco
   fica limitado a max(mediana local, MinBuyout) x --realm-cap (default 2.0),
   porque um preco que nao se alcanca no teu reino nao vende.
11. RELIST -> o custo de relist esperado nunca passa --relist-cap (0.5) do valor:
   se repostar custa mais do que o item vale, a conclusao e "nao vale repostar",
   nao "pede 800x o mercado".  (Real: 59 itens expirados a >3x a venda.)
12. LIQUIDAR -> "valor baixo" devolve o tier Min (preco de despacho real), nao
   o piso de vendor: antes dava 1g em itens cujo mercado era 471g.

MODO DUMP (zero typing; --realm escolhe o servidor)
    python scripts/calc_price.py --item 1351 --realm "Twisting Nether"
    python scripts/calc_price.py --item "Glorious Breastplate" --realm Dragonmaw
    python scripts/calc_price.py --item 14966 --drop-rate 0.02 --vendor 5000

POLITICAS (afinal os numeros sao teus, nao do addon)
    --target-posts 1    alvo de posts por venda no caso "sem oferta"
    --elasticity 1.0    desconto por velocidade (>1 corta mais)
    --undercount 2.20   quanto o TSM subcontabiliza as vendas (1.0 = TSM puro)
    --realm-cap 2.0     teto local = max(mediana local, MinBuyout) x isto
    --sale-cap 3.0      sem parede local, teto = DBRegionSaleAvg x isto
    --relist-cap 0.5    custo de relist esperado no maximo isto do valor

MODO TOOLTIP (override manual, como antes)
    python scripts/calc_price.py --market 46636 --historical 47378 \
        --region-market 46806 --region-hist 48207 --saleavg 8970 \
        --rate 0.012 --daily 0.024 --vendor 20 --minbuyout 46723 --name Glorious

INVENTARIO (por defeito: TODO o BoE [binding=ON_EQUIP] + recipes/patterns)
    python scripts/calc_price.py --inventory inventory.csv --out priced.csv
    python scripts/calc_price.py --inventory inventario.csv --audit audit.csv
    python scripts/calc_price.py --inventory inventario.csv --min-quality UNCOMMON
    python scripts/calc_price.py --inventory inventario.csv --no-recipes
    python scripts/calc_price.py --inventory inventario.csv --all-items
    # sem bind/qualidade para alguns itens? enriquece primeiro:
    python scripts/enrich_inventory.py inventario.csv --db tsm_markets.db

    O criterio de BoE e o BINDING, nao a qualidade: cinzas e brancos tambem sao
    BoE e entram (o modelo da-lhes LIQUIDAR).  Usa --min-quality UNCOMMON para os
    cortar, e --audit FICHEIRO para ver linha a linha porque algo ficou de fora.

CSV DE SAIDA (abre direto no Excel, sem assistente):
    separador ';' (--sep para mudar, ex. --sep , ou --sep tab) e UTF-8 COM BOM,
    que e o que o Excel precisa para ler acentos (confianca, unicidade).
    ORDEM das colunas: Name, posta (a quanto postar), confianca, mode, Qty, ...
    e so no fim a evidencia de mercado (market/minBuyout/saleavg/rate/note).

BONUS IGNORADO: o preco e sempre do item base.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import re
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DEFAULT_DB = "tsm_markets.db"
DEFAULT_REALM = "Twisting Nether"
DEFAULT_MARKET_CSV = "csv/items.csv"

APPDATA_GLOBS = [
    r"C:/Program Files (x86)/World of Warcraft/*/Interface/AddOns/"
    r"TradeSkillMaster_AppHelper/AppData.lua",
    r"C:/Program Files/World of Warcraft/*/Interface/AddOns/"
    r"TradeSkillMaster_AppHelper/AppData.lua",
    os.path.expanduser(
        "~/World of Warcraft/*/Interface/AddOns/"
        "TradeSkillMaster_AppHelper/AppData.lua"
    ),
]
PREFERRED_DIRS = ["_retail_", "_anniversary_", "_classic_era_", "_classic_"]

# Liquidez: (sold/dia minimo, sale rate minimo) -> confianca na venda.
BUCKETS = [
    ("LIQUIDO", 1.0, 0.20, 1.00),
    ("semi", 0.10, 0.05, 0.50),
    ("thin", 0.02, 0.01, 0.25),
    ("dead", 0.0, 0.0, 0.00),
]
# Fatores Min/Normal/Max por bucket.
TIERS = {
    "LIQUIDO": (0.90, 0.98, 1.15),
    "semi": (0.72, 1.00, 1.54),
    "thin": (0.72, 1.00, 1.54),
    "dead": (0.55, 1.00, 1.60),
}
SALE_BAND = (0.5, 2.0)         # DBRegionSaleAvg so conta se cair nesta banda do core
VALUABLE_MIN_GOLD = 1000.0     # abaixo disto nao vale um slot -> LIQUIDAR
DUMP_VENDOR_MULT = 1.05        # piso = vendor * isto
REALM_SHARE = 1.0              # 1.0 = preco do servidor (regiao so como ancora)
TARGET_POSTS = 1.0             # CALIBRADO nas 204 vendas reais: vendem no 1o post
ELASTICITY = 1.0               # 1.0 = preco proporcional a velocidade de venda
# O TSM so contabiliza vendas de quem usa TSM (Accounting), logo rate/daily sao
# um PISO, nao a verdade.  Ajuste assumido para nao castigar quem nao usa addon.
TSM_UNDERCOUNT = 2.20          # CALIBRADO: sell-through real / DBRegionSaleRate
# Sem parede local nao ha concorrencia a limitar, e a mediana regional pode estar
# dezenas de vezes acima do que se transaciona.  Teto pela VENDA real (xisto),
# so nesse caso: com parede local manda a parede (--realm-cap).
SALE_CAP = 3.0
# O custo de relist esperado nao pode ser razao para pedir MAIS do que o mercado:
# se repostar custa isto (ou mais) do valor do item, a conclusao certa e "nao vale
# repostar", nao "pede 800x o mercado".  Real: 59 itens expirados a >3x a venda.
RELIST_CAP = 0.5
# A mediana regional pode estar muito acima da parede do teu reino; se ha oferta
# local, o preco tem de ser alcancavel aqui.  Cap = max(mediana local, MinBuyout)*isto.
REALM_CAP = 2.0

# Bindings que nunca chegam ao AH (BoP / conta).
SOULBOUND = frozenset({
    "ON_ACQUIRE", "TO_ACCOUNT", "TO_BNETACCOUNT",
    "BIND_ON_PICKUP", "BIND_TO_ACCOUNT", "BIND_TO_BNETACCOUNT"})
# Recipes/patterns/plans: o Blizzard da-lhes binding vazio, logo deteta-se pelo
# nome (o \b evita falsos como "Enchanted Sea Kelp").
RECIPE_RE = re.compile(
    r"^(Recipe|Pattern|Plans|Schematic|Formula|Design|Enchant|Technique|"
    r"Glyph|Blueprint)\b", re.I)


# --------------------------------------------------------------------------- #
# AppData.lua
# --------------------------------------------------------------------------- #
_LOADDATA_RE = re.compile(
    r'LoadData\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*\[\[(.*?)\]\]\s*\)', re.S
)
_ROW_RE = re.compile(r"\{([^{}]+)\}")
_FIELDS_RE = re.compile(r"fields=\{([^}]*)\}")
_DL_RE = re.compile(r"downloadTime=(\d+)")

REALM_TAGS = {
    "AUCTIONDB_NON_COMMODITY_DATA",
    "AUCTIONDB_NON_COMMODITY_SCAN_STAT",
    "AUCTIONDB_NON_COMMODITY_HISTORICAL",
}
REGION_TAGS = {
    "AUCTIONDB_REGION_STAT",
    "AUCTIONDB_REGION_HISTORICAL",
    "AUCTIONDB_REGION_SALE",
}
COMMODITY_TAGS = {
    "AUCTIONDB_COMMODITY_DATA",
    "AUCTIONDB_COMMODITY_SCAN_STAT",
    "AUCTIONDB_COMMODITY_HISTORICAL",
}


def b32(token: str) -> int:
    token = token.strip()
    return int(token, 32) if token else 0


def _item_id(raw: str) -> Optional[int]:
    raw = raw.strip().strip('"')
    if raw.isdigit():
        return int(raw)
    if raw.startswith("i:"):
        head = raw.split(":", 2)[1]
        return int(head) if head.isdigit() else None
    return None


def _parse_stream(body: str):
    inner = body.strip()
    inner = inner[inner.index("return") + len("return"):].strip()
    inner = inner[1:-1]
    idx = inner.index(",data={")
    meta = inner[:idx]
    data = inner[idx + len(",data={"):]
    fm = _FIELDS_RE.search(meta + "}")
    fields = re.findall(r'"([^"]+)"', fm.group(1)) if fm else []
    dm = _DL_RE.search(body)
    dl = int(dm.group(1)) if dm else 0
    rows = {}
    for rr in _ROW_RE.findall(data):
        parts = rr.split(",")
        iid = _item_id(parts[0])
        if iid is None:
            continue
        rows[iid] = [b32(p) for p in parts[1:]]
    return fields, rows, dl


def find_appdata(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found: list[str] = []
    for pat in APPDATA_GLOBS:
        found.extend(glob.glob(pat))
    if not found:
        return None

    def rank(p: str):
        n = p.replace("\\", "/")
        for i, d in enumerate(PREFERRED_DIRS):
            if f"/{d}/" in n:
                return (i, -os.path.getmtime(p))
        return (len(PREFERRED_DIRS), -os.path.getmtime(p))

    return min(found, key=rank)


def build_signals(appdata_path: str, db_path: str, verbose: bool = True) -> dict:
    """Le AppData.lua e escreve as tabelas tsm_market/tsm_region/tsm_meta."""
    raw = Path(appdata_path).read_text(encoding="utf-8", errors="replace")
    market: dict[tuple[int, str], dict] = {}
    region: dict[int, dict] = {}
    meta = {"appdata_path": appdata_path}

    def mrow(iid: int, realm: str) -> dict:
        return market.setdefault((iid, realm), {
            "min_buyout": 0, "market": 0, "num_auctions": 0,
            "market_recent": 0, "historical": 0})

    for tag, realm, body in _LOADDATA_RE.findall(raw):
        if tag == "APP_INFO":
            vm = re.search(r"version=(\d+)", body)
            if vm:
                meta["app_version"] = vm.group(1)
            continue
        if ",data={" not in body:
            continue
        fields, rows, dl = _parse_stream(body)
        col = {f: i for i, f in enumerate(fields[1:])}
        meta[f"dl_{tag}_{realm}"] = str(dl)

        if tag == "AUCTIONDB_NON_COMMODITY_DATA":
            for iid, v in rows.items():
                r = mrow(iid, realm)
                r["min_buyout"] = v[col["minBuyout"]]
                r["num_auctions"] = v[col["numAuctions"]]
                r["market_recent"] = v[col["marketValueRecent"]]
        elif tag == "AUCTIONDB_NON_COMMODITY_SCAN_STAT":
            for iid, v in rows.items():
                mrow(iid, realm)["market"] = v[col["marketValue"]]
        elif tag == "AUCTIONDB_NON_COMMODITY_HISTORICAL":
            for iid, v in rows.items():
                mrow(iid, realm)["historical"] = v[col["historical"]]
        elif tag == "AUCTIONDB_REGION_STAT":
            for iid, v in rows.items():
                region.setdefault(iid, {})["region_market"] = v[col["regionMarketValue"]]
        elif tag == "AUCTIONDB_REGION_HISTORICAL":
            for iid, v in rows.items():
                region.setdefault(iid, {})["region_historical"] = v[col["regionHistorical"]]
        elif tag == "AUCTIONDB_REGION_SALE":
            for iid, v in rows.items():
                r = region.setdefault(iid, {})
                r["region_sale_avg"] = v[col["regionSale"]]
                r["region_sold_per_day"] = v[col["regionSoldPerDay"]] / 1000.0
                r["region_sale_rate"] = v[col["regionSalePercent"]] / 1000.0
        elif tag in COMMODITY_TAGS:
            crealm = f"{realm} (commodity)"
            if tag.endswith("_DATA"):
                for iid, v in rows.items():
                    r = mrow(iid, crealm)
                    r["min_buyout"] = v[col["minBuyout"]]
                    r["num_auctions"] = v[col["numAuctions"]]
                    r["market_recent"] = v[col["marketValueRecent"]]
            elif tag.endswith("_SCAN_STAT"):
                for iid, v in rows.items():
                    mrow(iid, crealm)["market"] = v[col["marketValue"]]
            elif tag.endswith("_HISTORICAL"):
                for iid, v in rows.items():
                    mrow(iid, crealm)["historical"] = v[col["historical"]]

    con = sqlite3.connect(db_path)
    con.executescript(
        """
        DROP TABLE IF EXISTS tsm_market;
        DROP TABLE IF EXISTS tsm_region;
        DROP TABLE IF EXISTS tsm_meta;
        CREATE TABLE tsm_market(
            itemId INTEGER, realm TEXT, min_buyout INTEGER, market INTEGER,
            num_auctions INTEGER, market_recent INTEGER, historical INTEGER,
            PRIMARY KEY(itemId, realm));
        CREATE TABLE tsm_region(
            itemId INTEGER PRIMARY KEY, region_market INTEGER,
            region_historical INTEGER, region_sale_avg INTEGER,
            region_sold_per_day REAL, region_sale_rate REAL);
        CREATE TABLE tsm_meta(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    con.executemany(
        "INSERT INTO tsm_market VALUES(?,?,?,?,?,?,?)",
        [(iid, r, v["min_buyout"], v["market"], v["num_auctions"],
          v["market_recent"], v["historical"]) for (iid, r), v in market.items()])
    full = {"region_market": 0, "region_historical": 0, "region_sale_avg": 0,
            "region_sold_per_day": 0.0, "region_sale_rate": 0.0}
    con.executemany(
        "INSERT INTO tsm_region VALUES(?,?,?,?,?,?)",
        [(iid, {**full, **v}["region_market"], {**full, **v}["region_historical"],
          {**full, **v}["region_sale_avg"], {**full, **v}["region_sold_per_day"],
          {**full, **v}["region_sale_rate"]) for iid, v in region.items()])
    con.executemany("INSERT INTO tsm_meta VALUES(?,?)", meta.items())
    con.commit()
    realms = sorted({r for _, r in market})
    con.close()
    stats = {"market_rows": len(market), "region_rows": len(region), "realms": realms}
    if verbose:
        print(f"[build] {appdata_path}")
        print(f"[build] {db_path}: {stats['market_rows']} linhas de mercado, "
              f"{stats['region_rows']} de regiao")
        print(f"[build] realms: {', '.join(realms)}")
    return stats


def ensure_signals(db_path: str, appdata: Optional[str], rebuild: bool) -> None:
    need = rebuild
    if not need and Path(db_path).exists():
        con = sqlite3.connect(db_path)
        try:
            con.execute("SELECT 1 FROM tsm_market LIMIT 1")
        except sqlite3.Error:
            need = True
        con.close()
    else:
        need = True
    if need:
        src = find_appdata(appdata)
        if not src:
            sys.exit("erro: nao achei AppData.lua; passa --appdata CAMINHO")
        build_signals(src, db_path)
    # vista com os BoE (bind on equip) — separa equipamento de commodities
    try:
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE VIEW IF NOT EXISTS boe AS SELECT itemId,name,quality,level,"
            "sellPrice FROM blizzard_items WHERE binding='ON_EQUIP'")
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------- #
# Sinais (unidade interna = OURO, float)
# --------------------------------------------------------------------------- #
@dataclass
class Signals:
    item_id: int
    realm: str
    name: str = ""
    market: float = 0.0
    historical: float = 0.0
    market_recent: float = 0.0
    min_buyout: float = 0.0
    num_auctions: int = 0
    region_market: float = 0.0
    region_historical: float = 0.0
    region_sale_avg: float = 0.0
    sold_per_day: float = 0.0
    sale_rate: float = 0.0
    vendor: float = 0.0


def _tbl(con: sqlite3.Connection, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def load_signals(con: sqlite3.Connection, iid: int, realm: str) -> Signals:
    s = Signals(item_id=iid, realm=realm)
    has_tsm = _tbl(con, "tsm_market")
    if has_tsm:
        row = con.execute(
            "SELECT min_buyout,market,num_auctions,market_recent,historical "
            "FROM tsm_market WHERE itemId=? AND realm=?", (iid, realm)).fetchone()
        if row:
            (s.min_buyout, s.market, s.num_auctions,
             s.market_recent, s.historical) = [x or 0 for x in row]
            s.min_buyout /= 10000.0
            s.market /= 10000.0
            s.market_recent /= 10000.0
            s.historical /= 10000.0
        rr = con.execute(
            "SELECT region_market,region_historical,region_sale_avg,"
            "region_sold_per_day,region_sale_rate FROM tsm_region WHERE itemId=?",
            (iid,)).fetchone()
        if rr:
            (s.region_market, s.region_historical, s.region_sale_avg,
             s.sold_per_day, s.sale_rate) = [x or 0 for x in rr]
            s.region_market /= 10000.0
            s.region_historical /= 10000.0
            s.region_sale_avg /= 10000.0
    # fallback: dumps antigos
    if s.market == 0 and _tbl(con, "market_snapshots"):
        r = con.execute(
            "SELECT m.marketValue,m.historical,m.avgSalePrice,m.saleRate,m.soldPerDay "
            "FROM market_snapshots m WHERE m.itemId=? "
            "ORDER BY m.snapshotId DESC LIMIT 1", (iid,)).fetchone()
        if r:
            s.market = (r[0] or 0) / 10000.0
            s.historical = (r[1] or 0) / 10000.0
            s.region_sale_avg = s.region_sale_avg or (r[2] or 0) / 10000.0
            s.sale_rate = s.sale_rate or float(r[3] or 0)
            s.sold_per_day = s.sold_per_day or float(r[4] or 0)
    # nome + vendor
    if _tbl(con, "items"):
        r = con.execute("SELECT name FROM items WHERE itemId=?", (iid,)).fetchone()
        if r:
            s.name = r[0]
    if _tbl(con, "blizzard_items"):
        r = con.execute("SELECT sellPrice FROM blizzard_items WHERE itemId=?",
                        (iid,)).fetchone()
        if r and r[0]:
            s.vendor = r[0] / 10000.0
    return s


def item_bind_quality(con: sqlite3.Connection,
                      iid: int) -> tuple[Optional[str], str, str]:
    """(binding, quality, name); (None, '', '') se ainda nao foi enriquecido."""
    if not _tbl(con, "blizzard_items"):
        return None, "", ""
    r = con.execute("SELECT binding, quality, name FROM blizzard_items "
                    "WHERE itemId=?", (iid,)).fetchone()
    if not r:
        return None, "", ""
    return (r[0] or ""), (r[1] or ""), (r[2] or "")


def is_recipe(name: str) -> bool:
    return bool(RECIPE_RE.match(name or ""))


def resolve_item(con: sqlite3.Connection, token: str) -> tuple[int, str]:
    token = token.strip()
    if token.isdigit():
        iid = int(token)
        name = ""
        if _tbl(con, "items"):
            r = con.execute("SELECT name FROM items WHERE itemId=?", (iid,)).fetchone()
            name = r[0] if r else ""
        return iid, name
    if not _tbl(con, "items"):
        sys.exit("erro: nome precisa de --db com tabela items; usa o id.")
    r = con.execute("SELECT itemId,name FROM items WHERE lower(name)=lower(?)",
                    (token,)).fetchone()
    if r:
        return r[0], r[1]
    cands = con.execute("SELECT itemId,name FROM items WHERE lower(name) LIKE ? LIMIT 15",
                        (f"%{token.lower()}%",)).fetchall()
    if len(cands) == 1:
        return cands[0][0], cands[0][1]
    if not cands:
        sys.exit(f"erro: nada para {token!r}. Usa o id, ou --market-csv.")
    print(f"Varios candidatos para {token!r}:")
    for iid, nm in cands:
        print(f"  {iid}: {nm}")
    sys.exit("refina com --item <id> ou o nome exato.")


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #
@dataclass
class Valuation:
    name: str
    item_id: int
    mode: str
    core: float
    fair: float
    mn: float
    norm: float
    mx: float
    confidence: str
    uniqueness: float
    trust: float
    bucket: str
    flags: dict
    notes: list = field(default_factory=list)


def _median(vals) -> float:
    v = [x for x in vals if x and x > 0]
    return statistics.median(v) if v else 0.0


def liquidity_bucket(sold_per_day: float, rate: float) -> tuple[str, float]:
    for name, min_d, min_r, trust in BUCKETS:
        if sold_per_day >= min_d or rate >= min_r:
            return name, trust
    return "dead", 0.0


def uniqueness(s: Signals, drop_rate: Optional[float]) -> tuple[float, dict]:
    """0..1 -> quao 'unico' e o item (oferta rara + procura sem prova)."""
    parts = {}
    # numAuctions=0 + ha DBMinBuyout => ha pelo menos 1 listagem (modo tooltip)
    auctions = s.num_auctions or (1 if s.min_buyout > 0 else 0)
    parts["competicao"] = 1.0 if auctions == 0 else 1.0 / (1.0 + auctions)
    glut = auctions / max(s.sold_per_day, 0.01)
    parts["fluxo"] = 1.0 if glut <= 1 else max(0.0, 1.0 - math.log10(glut) / 2.0)
    if drop_rate and drop_rate > 0:
        r = (math.log10(1.0 / drop_rate) - 1.0) / 2.0
        parts["raridade"] = min(1.0, max(0.0, r))
    score = sum(parts.values()) / len(parts)
    return score, parts


def value(s: Signals, *, drop_rate: Optional[float] = None, own: bool = False,
          realm_share: float = REALM_SHARE, target_posts: float = TARGET_POSTS,
          elasticity: float = ELASTICITY, undercount: float = TSM_UNDERCOUNT,
          realm_cap: float = REALM_CAP,
          relist_cap: float = RELIST_CAP,
          sale_cap: float = SALE_CAP,
          price_model: str = "gated") -> tuple[Valuation, float, dict]:
    """Conclui SEMPRE um preco.  Nunca devolve 'incerto'.

    gated (default): valor sai da oferta (mediana de Market/Hist/RegionMarket/
    RegionHist); a venda (DBRegionSaleAvg) so entra se for CONSISTENTE com a
    oferta (banda 0.5x..2x); senao e descartada.

    tsm-avg: replica a tua string max(avg(dbregionmarketavg, dbmarket,
    dbhistorical, dbregionhistorical, 500% dbregionsaleavg)) — o max() de um
    so argumento e a propria media, e o avg() do TSM usa as fontes validas
    (>0). Sem bandas nem tetos: a media manda, mesmo com outlier.
    """
    notes: list[str] = []
    if price_model == "tsm-avg":
        parts = [x for x in (s.market, s.historical, s.region_market,
                             s.region_historical,
                             5 * s.region_sale_avg if s.region_sale_avg > 0 else 0)
                 if x > 0]
        if not parts:
            v0 = Valuation(name=s.name, item_id=s.item_id, mode="NO_PRICE",
                           core=0, fair=0, mn=0, norm=0, mx=0, confidence="BAIXA",
                           uniqueness=0.0, trust=0.0, bucket="dead", flags={},
                           notes=["sem fontes validas para a media TSM"])
            return v0, 0, {}
        tsm = sum(parts) / len(parts)
        notes.append(f"tsm-avg: media de {len(parts)} fontes "
                     f"(= tua string max(avg(...))) -> {g(tsm)}")
        vendor_floor = s.vendor * DUMP_VENDOR_MULT if s.vendor else 0.0
        floor = max(vendor_floor, 1.0)
        rate_eff = min(1.0, s.sale_rate * undercount) if s.sale_rate > 0 else 0.0
        spd_eff = s.sold_per_day * undercount
        bucket, trust = liquidity_bucket(spd_eff, rate_eff)
        uni, uni_parts = uniqueness(s, drop_rate)
        f_quick, f_norm, f_greed = TIERS[bucket]
        val = max(tsm * realm_share, floor)
        mn, norm, mx = max(val * f_quick, floor), val * f_norm, val * f_greed
        mode = "LIQUIDO" if bucket == "LIQUIDO" else (
            "LIQUIDAR" if val < VALUABLE_MIN_GOLD else "ILIQUIDO-BASE")
        flags = {}
        for k, v in (("DBMarket", s.market), ("DBHistorical", s.historical),
                     ("DBRegionMarketAvg", s.region_market),
                     ("DBRegionHistorical", s.region_historical),
                     ("DBMinBuyout", s.min_buyout),
                     ("DBRegionSaleAvg", s.region_sale_avg)):
            if v > 0:
                div = v / tsm if tsm else 0
                flags[k] = (div, "OUTLIER" if not (0.5 <= div <= 2.0) else "OK")
        conclusion = mn if mode == "LIQUIDAR" else norm
        return Valuation(name=s.name, item_id=s.item_id, mode=mode, core=tsm,
                         fair=val, mn=mn, norm=norm, mx=mx,
                         confidence="MEDIA", uniqueness=uni,
                         trust=trust, bucket=bucket, flags=flags,
                         notes=notes), conclusion, uni_parts
    asks = [s.market, s.historical, s.region_market, s.region_historical]
    core = _median(asks)
    if core <= 0:
        core = s.market or s.historical or s.region_market or s.region_sale_avg or 0.0

    # rate/daily efetivos (TSM subcontabiliza: so ve vendas de quem usa TSM)
    rate_eff = min(1.0, s.sale_rate * undercount) if s.sale_rate > 0 else 0.0
    spd_eff = s.sold_per_day * undercount
    bucket, trust = liquidity_bucket(spd_eff, rate_eff)

    # DBRegionSaleAvg e uma media de COMPRAS: inclui snipes/flips, por isso pode
    # ficar abaixo do preco mais barato que existe a venda.  Nesse caso nao e
    # preco, e sinal de que houve compra barata -> nunca ancora o valor.
    snipe = s.min_buyout > 0 and s.region_sale_avg > 0 and s.region_sale_avg < s.min_buyout
    sale_ratio = (s.region_sale_avg / core) if (s.region_sale_avg and core) else 0.0
    sale_ok = (s.region_sale_avg > 0 and SALE_BAND[0] <= sale_ratio <= SALE_BAND[1]
               and not snipe)
    if sale_ok:
        val = (1 - trust) * core + trust * s.region_sale_avg
        notes.append(f"DBRegionSaleAvg dentro da banda ({sale_ratio:.2f}x) -> usado")
    else:
        val = core
        if snipe:
            notes.append(f"DBRegionSaleAvg {g(s.region_sale_avg)} < MinBuyout "
                         f"{g(s.min_buyout)} -> snipe/flip, DESCARTADO como preco")
        elif s.region_sale_avg > 0:
            notes.append(f"DBRegionSaleAvg {sale_ratio:.3f}x do core -> DESCARTADO "
                         f"(nao bate com a oferta)")

    # A mediana REGIONAL pode estar dezenas de vezes acima da parede do teu reino
    # (posts de outros reinos, pouca oferta la).  Com oferta local, o preco tem de
    # ser alcancavel aqui, senao fica a apodrecer no AH.
    if s.min_buyout > 0:
        realm_core = _median([s.market, s.historical]) or s.min_buyout
        cap = max(realm_core, s.min_buyout) * realm_cap
        if val > cap:
            notes.append(f"capa do teu reino: {g(val)} -> {g(cap)} (parede "
                         f"{g(s.min_buyout)}, mediana local {g(realm_core)})")
            val = cap
    # Sem parede local: ninguem esta a vender no teu reino, logo a mediana regional
    # nao e um preco alcancavel.  Teto pela venda real (x o fator).
    # Se este teto morde, ele JA e a evidencia de transacao: nao se aplica depois
    # o desconto por velocidade (era duplo castigo: 12.632g -> 15g no Fingerbone).
    sale_capped = False
    if s.min_buyout <= 0 and sale_cap > 0 and s.region_sale_avg > 0:
        cap = s.region_sale_avg * sale_cap
        if val > cap:
            notes.append(f"sem parede local -> teto pela venda real: {g(val)} -> "
                         f"{g(cap)} (DBRegionSaleAvg {g(s.region_sale_avg)} "
                         f"x{sale_cap:g})")
            val = cap
            sale_capped = True

    # A mediana regional pode estar dezenas... (parede local) — ver abaixo

    # piso: vendor + relist esperado (deposito perdido ate vender)
    vendor_floor = s.vendor * DUMP_VENDOR_MULT if s.vendor else 0.0
    relist_raw = ((1.0 / rate_eff - 1.0) * 0.60 * s.vendor
                  if (rate_eff > 0 and s.vendor) else 0.0)
    relist = relist_raw
    if relist_raw and relist_cap >= 0:
        # limitado ao valor de mercado: o deposito esperado nao dita o preco
        relist = min(relist_raw, val * relist_cap) if val > 0 else relist_raw
    floor = max(vendor_floor, relist, 1.0 if val > 0 else 0.0)
    if relist_raw:
        notes.append(f"~{1.0 / rate_eff:,.0f} posts p/ 1 venda (relist ~{relist_raw:,.0f}g)")
        if relist < relist_raw:
            notes.append(f"relist acima de {relist_cap:.0%} do valor -> nao vale "
                         f"repostar; piso travado em {g(relist)} (era {g(relist_raw)})")

    uni, uni_parts = uniqueness(s, drop_rate)
    if drop_rate and drop_rate < 0.01:
        notes.append(f"drop {drop_rate * 100:.3f}% -> raro, nao despejar")

    # Sem oferta atual, o "valor de mercado" e uma referencia morta: ninguem
    # esta a vender, logo o preco tem de refletir a VELOCIDADE real de venda.
    has_supply = s.num_auctions >= 1 or (s.min_buyout > 0 and not own)
    velocity = False
    if not has_supply and rate_eff > 0 and not sale_capped:
        target_rate = 1.0 / max(target_posts, 1.0)
        if rate_eff < target_rate:
            adj = max(floor, val * (rate_eff / target_rate) ** elasticity)
            if adj < val:
                notes.append(
                    f"sem oferta atual -> preco pela velocidade: rate "
                    f"{rate_eff:.3%} (~{1 / rate_eff:,.0f} posts/venda), "
                    f"alvo ~1/{target_posts:.0f} posts")
                val = adj
                velocity = True

    val = max(val * realm_share, floor)
    f_quick, f_norm, f_greed = TIERS[bucket]
    mn, norm, mx = max(val * f_quick, floor), val * f_norm, val * f_greed
    if uni >= 0.66 and val > 0:
        mx = max(mx, val * 1.8)
        notes.append(f"unicidade {uni * 100:.0f}% -> podes pedir acima do normal")

    # conclusao: 4 classes, sem "incerto"
    if val <= 0 and s.min_buyout <= 0:
        mode = "NO_PRICE"
        notes.append("sem dados para este realm/item")
    elif bucket == "LIQUIDO":
        mode = "LIQUIDO"
        notes.append("vende rapido -> poe ao valor de mercado e baixa ~2% se houver competicao")
    elif val < VALUABLE_MIN_GOLD:
        mode = "LIQUIDAR"
        notes.append("valor baixo -> despacha rapido (Min) ou vendor, nao gastes slot")
    else:
        mode = "ILIQUIDO-BASE"

    # LIQUIDAR sugere postar rapido, nao dar o item: a conclusao e o tier Min
    # (valor x fator de despacho), nunca o piso de vendor (que era 1g).
    conclusion = mn if mode == "LIQUIDAR" else norm

    # confianca da conclusao (quao sustentada esta pelos dados que temos)
    pos = [x for x in asks if x > 0]
    if not own and s.min_buyout > 0:
        pos.append(s.min_buyout)
    spread = (max(pos) - min(pos)) / core if (core > 0 and len(pos) > 1) else 0.0
    if sale_ok and rate_eff >= 0.02:
        conf = "ALTA"
    elif spread <= 0.5 and (rate_eff >= 0.01 or s.num_auctions >= 3):
        conf = "MEDIA"
    else:
        conf = "BAIXA"
        notes.append("confianca baixa: valor vem da oferta (sem prova de venda)")
    if velocity:
        notes.append("preco ajustado ao que o colecionador paga, nao a parede de asks")
    elif mode == "ILIQUIDO-BASE" and s.num_auctions == 0:
        notes.append("sem concorrencia -> tu defines o preco; segura no valor de mercado")

    flags = {}
    for k, v in (("DBMarket", s.market), ("DBHistorical", s.historical),
                 ("DBRegionMarketAvg", s.region_market),
                 ("DBRegionHistorical", s.region_historical),
                 ("DBMinBuyout", s.min_buyout), ("DBRegionSaleAvg", s.region_sale_avg)):
        if v > 0:
            div = v / core if core else 0
            flags[k] = (div, "OUTLIER" if not (0.5 <= div <= 2.0) else "OK")

    return Valuation(name=s.name, item_id=s.item_id, mode=mode, core=core, fair=val,
                     mn=mn, norm=norm, mx=mx, confidence=conf, uniqueness=uni,
                     trust=trust, bucket=bucket, flags=flags,
                     notes=notes), conclusion, uni_parts


# --------------------------------------------------------------------------- #
# Saida
# --------------------------------------------------------------------------- #
def g(v: float) -> str:
    return f"{v:,.0f}g"


def render(val: Valuation, recommend: float, uni_parts: dict, s: Signals,
           realm: str) -> str:
    out = ["=" * 70]
    head = f"  {val.item_id}"
    if val.name:
        head += f"  {val.name}"
    out.append(f"{head}   [{realm}]")
    out.append("=" * 70)
    # a conclusao primeiro: nome -> a quanto postar -> confianca
    out.append(f"  >>> A QUANTO POSTAR: {g(recommend)}   confianca={val.confidence}")
    out.append(f"  Min={g(val.mn)}   Normal={g(val.norm)}   Max={g(val.mx)}"
               f"   modo={val.mode}")
    out.append(f"  core={g(val.core)}   valor={g(val.fair)}")
    out.append(f"  bucket={val.bucket}  trust={val.trust:.2f}  "
               f"unicidade={val.uniqueness * 100:.0f}%")
    for n in val.notes:
        out.append(f"    - {n}")
    out.append("-" * 70)
    out.append("  OFERTA (o que pedem)            VENDA (o que pagam)")
    out.append(f"    DBMinBuyout   {g(s.min_buyout):>12}    "
               f"DBRegionSaleAvg  {g(s.region_sale_avg):>12}")
    out.append(f"    DBMarket      {g(s.market):>12}    "
               f"SoldPerDay       {s.sold_per_day:>12.3f}")
    out.append(f"    DBHistorical  {g(s.historical):>12}    "
               f"DBRegionSaleRate {s.sale_rate:>12.3%}")
    out.append(f"    DBRecent      {g(s.market_recent):>12}    "
               f"numAuctions      {s.num_auctions:>12}")
    out.append(f"    RegMarketAvg  {g(s.region_market):>12}")
    out.append(f"    RegHistorical {g(s.region_historical):>12}")
    if s.vendor:
        out.append(f"    Vendor        {g(s.vendor):>12}")
    out.append("-" * 70)
    out.append("  unicidade: " + ", ".join(f"{k}={v:.2f}" for k, v in uni_parts.items()))
    out.append("=" * 70)
    return "\n".join(out)


def tsm_strings() -> str:
    return (
        "  Sugestao de string TSM (raro / transmog):\n"
        "  Normal: ifgt(DBRegionSaleRate, 0.02, "
        "min(DBMarket, DBHistorical, DBRegionMarketAvg, 500% DBRegionSaleAvg), "
        "avg(DBMarket, DBHistorical, DBRegionHistorical))\n"
        "  Max: ifgt(DBRegionSaleRate, 0.02, "
        "150% min(DBMarket, DBRegionMarketAvg), "
        "180% avg(DBMarket, DBHistorical, DBRegionHistorical))\n"
        "  Achar subvalorizados: "
        "iflt(DBMinBuyout, 50% avg(DBMarket, DBRegionMarketAvg), DBMinBuyout, 0c)"
    )


# --------------------------------------------------------------------------- #
# Inventario
# --------------------------------------------------------------------------- #
def run_inventory(args) -> None:
    con = sqlite3.connect(args.db)
    with open(args.inventory, encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        rows = list(csv.DictReader(f, delimiter=delim))

    def col(r, *names):
        for n in names:
            for k in r:
                if k and k.strip().lower() == n.lower():
                    return r[k]
        return ""

    QUALITY_ORDER = {"POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3,
                     "EPIC": 4, "LEGENDARY": 5, "ARTIFACT": 6}
    min_q = (None if (args.min_quality or "").upper() in ("ANY", "")
             else QUALITY_ORDER.get((args.min_quality or "").upper(), 2))
    out_rows = []
    counts: dict[str, int] = {}
    skipped: dict[str, int] = {}
    audit: list[dict] = []          # TODAS as linhas do input + motivo

    def _skip(iid, nm, key: str) -> None:
        skipped[key] = skipped.get(key, 0) + 1
        audit.append({"ItemID": iid, "Name": nm, "status": "ignorado",
                      "motivo": key})

    for r in rows:
        iid_s = col(r, "itemid", "item_id", "id").strip()
        if not iid_s:
            _skip("", col(r, "name", "nome").strip(), "sem ItemID na linha")
            continue
        try:
            iid = int(float(iid_s))
        except ValueError:
            _skip(iid_s, col(r, "name", "nome").strip(), "ItemID invalido")
            continue
        nm = col(r, "name", "nome").strip()
        # filtro: BoE (ON_EQUIP) + recipes/patterns; fora commodities/BoP/greys
        bind, qual, dbname = item_bind_quality(con, iid)
        if not args.all_items:
            if bind is None:
                _skip(iid, nm, "sem bind/qualidade (corre enrich_inventory.py)")
                continue
            if bind in SOULBOUND:
                _skip(iid, nm, f"soulbound ({bind})")
                continue
            if bind == "ON_EQUIP":
                if min_q is not None and QUALITY_ORDER.get(qual, 0) < min_q:
                    _skip(iid, nm, f"BoE qualidade {qual or '?'} < {args.min_quality}"
                                       f" (usa --min-quality ANY)")
                    continue
            elif is_recipe(dbname):
                if args.no_recipes:
                    _skip(iid, nm, "recipe (--no-recipes)")
                    continue
            else:
                _skip(iid, nm, f"nao-BoE/nao-recipe (binding={bind or 'nenhum'})")
                continue
        qty = col(r, "quantity", "qty", "quantidade").strip() or "1"
        bonus = col(r, "bonusids", "bonus", "bonus_ids").strip()
        dr = col(r, "drop_rate", "droprate", "drop").strip()
        drop = float(dr.rstrip("%")) / 100.0 if dr else None
        s = load_signals(con, iid, args.realm)
        if nm:
            s.name = nm
        val, rec, uparts = value(s, drop_rate=drop, own=args.own,
                                 target_posts=args.target_posts,
                                 elasticity=args.elasticity,
                                 undercount=args.undercount, realm_cap=args.realm_cap,
                                 relist_cap=args.relist_cap, sale_cap=args.sale_cap,
                                 price_model=args.price_model)
        counts[val.mode] = counts.get(val.mode, 0) + 1
        audit.append({"ItemID": iid, "Name": s.name, "status": "precificado",
                      "motivo": f"{val.mode} ({bind or 'vazio'}/{qual or '?'})"})
        note = ("bonus ignorado: preco e do base" if bonus else "")
        if val.notes:
            note = (note + " | " if note else "") + " | ".join(val.notes)
        out_rows.append({
            "Name": s.name, "posta": round(rec), "confianca": val.confidence,
            "mode": val.mode, "quality": qual or "", "binding": bind or "",
            "Qty": qty, "ItemID": iid,
            "Min": round(val.mn), "Normal": round(val.norm), "Max": round(val.mx),
            "minBuyout": round(s.min_buyout), "market": round(s.market),
            "saleavg": round(s.region_sale_avg), "hist": round(s.historical),
            # quantas vezes a minha 'posta' e a venda media do TSM (dados reais
            # dizem: commodities ~1x; mogs 6-25x -> e aqui que se decide a ancora)
            "venda_x": (round(rec / s.region_sale_avg, 1)
                        if s.region_sale_avg > 0 else ""),
            "regMarket": round(s.region_market), "rate": s.sale_rate,
            "daily": s.sold_per_day, "unicidade": round(val.uniqueness * 100),
            "BonusIDs": bonus, "note": note})

    # Ordem pensada para uso: nome -> a quanto postar -> confianca,
    # e so no fim a evidencia de mercado (volta a evoluir daqui).
    fields = ["Name", "posta", "confianca", "mode", "quality", "binding",
              "Qty", "ItemID", "Min", "Normal", "Max", "minBuyout", "market",
              "saleavg", "venda_x", "hist", "regMarket", "rate", "daily",
              "unicidade", "BonusIDs", "note"]
    sep = {"tab": "\t", "t": "\t", "\\t": "\t"}.get((args.sep or ";").lower(),
                                            args.sep or ";")
    if len(sep) != 1:
        sys.exit(f"erro: --sep tem de ser 1 caracter (recebi {args.sep!r})")
    # a nota nao pode conter o separador nem virgulas (ver csv_safe): sem isso o
    # Sheets/Excel adivinham mal o separador e a importacao parte-se.
    for r in out_rows:
        r["note"] = csv_safe(r["note"], sep)
    for r in audit:
        r["motivo"] = csv_safe(r["motivo"], sep)
    # utf-8-sig = BOM, sem o qual o Excel le a codepage ANSI e parte os acentos
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=sep)
        w.writeheader()
        w.writerows(out_rows)
    if args.audit:
        with open(args.audit, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ItemID", "Name", "status", "motivo"],
                               delimiter=sep)
            w.writeheader()
            w.writerows(audit)
    con.close()

    priced = [r for r in out_rows if r["mode"] != "NO_PRICE"]
    qual_txt = ("todas as qualidades" if min_q is None
                else f"qualidade>={args.min_quality}")
    filt = ("TODOS" if args.all_items else
            f"BoE (ON_EQUIP, {qual_txt})"
            + (" + recipes" if not args.no_recipes else ""))
    print(f"{len(rows)} linhas | filtro={filt} | {len(out_rows)} precificadas | "
          f"{sum(skipped.values())} ignoradas -> {args.out}")
    print(f"politica: undercount x{args.undercount:g} (rate/daily), cap do reino "
          f"x{args.realm_cap:g}, teto de venda x{args.sale_cap:g}, relist "
          f"x{args.relist_cap:g}, alvo {args.target_posts:g} posts/venda, "
          f"elasticidade {args.elasticity:g}")
    for m, n in sorted(counts.items()):
        print(f"  {m:14s} {n}")
    if not args.audit:
        print("dica: --audit inventory_audit.csv escreve TODAS as linhas do input "
              "com precificado/ignorado + motivo")
    if skipped:
        print("ignoradas:")
        for k, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  {k}")
    priced.sort(key=lambda r: r["posta"] * float(r["Qty"] or 1), reverse=True)
    print("Top valor em stock (posta x qty):")
    print(f"  {'Name':<36} {'posta':>7}  {'conf':<6} {'modo':<14} xQty")
    for r in priced[:15]:
        print(f"  {(r['Name'] or '')[:36]:36s} {r['posta']:>7}g  "
              f"{r['confianca']:<6} {r['mode']:<14} x{r['Qty']:<4} "
              f"= {r['posta'] * int(r['Qty'] or 1):>9,}g  (id {r['ItemID']})")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_gold(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    t = str(x).strip().lower().replace(",", "")
    if not t:
        return 0.0
    try:
        return float(t)
    except ValueError:
        pass
    g_ = s_ = c_ = 0.0
    for amt, unit in re.findall(r"([\d.]+)\s*([gsc]?)", t):
        v = float(amt)
        g_ += v if unit == "g" else 0
        s_ += v if unit == "s" else 0
        c_ += v if unit == "c" else 0
    if g_ or s_ or c_:
        return g_ + s_ / 100.0 + c_ / 10000.0
    return float(t) if t else 0.0


def csv_safe(text: str, sep: str) -> str:
    """Nota humana -> texto que nao precisa de aspas no CSV.

    O Excel e o Google Sheets ADIVINHAM o separador.  Se a nota trouxer o
    proprio separador (ex.: 'a; b') ou virgulas de milhares (ex.: '14,250g'), a
    deteccao falha e o ficheiro entra desmanchado — foi o que aconteceu: 167
    notas com ';' e 205 com ',' fizeram o Sheets escolher a virgula.
    A nota e decorativa (a evidencia numerica esta nas colunas proprias), logo
    aqui troca-se a pontuacao ambigua em vez de a deixar partir a importacao.
    """
    t = text.replace(";", " | ").replace(", ", " / ").replace(",", "")
    t = t.replace("\r", " ").replace("\n", " ")
    if sep != ";":
        t = t.replace(sep, " ")
    return " ".join(t.split())


def main() -> None:
    ap = argparse.ArgumentParser(description="Preco justo BoE/transmog via TSM AppData.")
    ap.add_argument("--item", default="", help="id ou nome")
    ap.add_argument("--inventory", default="", help="CSV do inventario")
    ap.add_argument("--out", default="inventory_priced.csv")
    ap.add_argument("--sep", default=";",
                    help="separador do CSV de saida: ';' (default, Excel pt-PT), "
                         "',' ou 'tab' (a leitura detecta os dois automaticamente)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--realm", default=DEFAULT_REALM)
    ap.add_argument("--all-items", action="store_true",
                    help="precifica tudo (nao filtra por BoE)")
    ap.add_argument("--min-quality", default="ANY",
                    help="qualidade minima para o BoE: POOR/COMMON/UNCOMMON/RARE/"
                         "EPIC/ANY. Default ANY = TODO o BoE (ON_EQUIP), porque cinzas "
                         "e brancos tambem sao BoE; usa UNCOMMON para os cortar")
    ap.add_argument("--audit", default="",
                    help="escreve um CSV com TODAS as linhas do input: status "
                         "(precificado/ignorado) + motivo")
    ap.add_argument("--no-recipes", action="store_true",
                    help="excluir recipes/patterns/plans")
    ap.add_argument("--appdata", default="", help="AppData.lua explicito")
    ap.add_argument("--rebuild", action="store_true", help="reconstroi tabelas tsm_*")
    ap.add_argument("--qty", type=float, default=1)
    ap.add_argument("--vendor", default="")
    ap.add_argument("--drop-rate", default="", help="drop em %%, ex. 0.02 = 0.02%%")
    ap.add_argument("--own", action="store_true", help="tu es o MinBuyout (nao conta como competicao)")
    ap.add_argument("--target-posts", type=float, default=TARGET_POSTS,
                    help="alvo de posts por venda quando nao ha oferta (default 10)")
    ap.add_argument("--elasticity", type=float, default=ELASTICITY,
                    help="desconto por velocidade; >1 = mais agressivo (default 1.0)")
    ap.add_argument("--undercount", type=float, default=TSM_UNDERCOUNT,
                    help="ajuste de rate/daily: o TSM so ve vendas de quem usa TSM "
                         "(default 1.25; 1.0 = TSM puro)")
    ap.add_argument("--realm-cap", type=float, default=REALM_CAP,
                    help="teto do preco no teu reino = max(mediana local, MinBuyout) x "
                         "isto, quando a regiao esta inflacionada (default 2.0)")
    ap.add_argument("--sale-cap", type=float, default=SALE_CAP,
                    help="sem parede local, teto = DBRegionSaleAvg x isto "
                         "(default 3.0; 0 desliga)")
    ap.add_argument("--relist-cap", type=float, default=RELIST_CAP,
                    help="o custo de relist esperado nunca passa isto do valor do "
                         "item (default 0.5; -1 desliga)")
    ap.add_argument("--price-model", default="gated", choices=("gated", "tsm-avg"),
                    help="gated (default): mediana + bandas + tetos; tsm-avg: "
                         "replica max(avg(dbregionmarketavg, dbmarket, "
                         "dbhistorical, dbregionhistorical, 500%% dbregionsaleavg))")
    # overrides tooltip
    ap.add_argument("--name", default="")
    ap.add_argument("--market", default="")
    ap.add_argument("--historical", default="")
    ap.add_argument("--region-market", default="")
    ap.add_argument("--region-hist", default="")
    ap.add_argument("--saleavg", default="")
    ap.add_argument("--rate", default="")
    ap.add_argument("--daily", default="")
    ap.add_argument("--minbuyout", default="")
    a = ap.parse_args()

    drop = float(a.drop_rate) / 100.0 if a.drop_rate else None
    tooltip = any([a.market, a.historical, a.region_market, a.region_hist,
                   a.saleavg, a.rate, a.daily, a.minbuyout])

    if a.item or a.inventory:
        ensure_signals(a.db, a.appdata or None, a.rebuild)
    con = sqlite3.connect(a.db) if Path(a.db).exists() else None

    if a.inventory:
        if con:
            con.close()
        run_inventory(a)
        return

    if a.item:
        iid, name = resolve_item(con, a.item)
        s = load_signals(con, iid, a.realm)
        if not tooltip and s.market == 0 and s.historical == 0 and s.min_buyout == 0 \
                and s.region_market == 0 and s.region_sale_avg == 0:
            realms = sorted({r[0] for r in con.execute(
                "SELECT DISTINCT realm FROM tsm_market ORDER BY realm")})
            sys.exit(f"erro: sem dados para {iid} em {a.realm!r}.\n"
                     f"       realms: {', '.join(realms)}")
    elif tooltip:
        s = Signals(item_id=0, realm=a.realm, name=a.name or "item")
    else:
        sys.exit("erro: usa --item <id|nome>, --inventory ou os overrides de tooltip.")

    # overrides
    if a.market:
        s.market = parse_gold(a.market)
    if a.historical:
        s.historical = parse_gold(a.historical)
    if a.region_market:
        s.region_market = parse_gold(a.region_market)
    if a.region_hist:
        s.region_historical = parse_gold(a.region_hist)
    if a.saleavg:
        s.region_sale_avg = parse_gold(a.saleavg)
    if a.rate:
        s.sale_rate = float(a.rate)
    if a.daily:
        s.sold_per_day = float(a.daily)
    if a.minbuyout:
        s.min_buyout = parse_gold(a.minbuyout)
    if a.vendor:
        s.vendor = parse_gold(a.vendor)
    if a.name:
        s.name = a.name

    val, rec, uparts = value(s, drop_rate=drop, own=a.own,
                             target_posts=a.target_posts, elasticity=a.elasticity,
                             undercount=a.undercount, realm_cap=a.realm_cap,
                             relist_cap=a.relist_cap, sale_cap=a.sale_cap,
                             price_model=a.price_model)
    print(render(val, rec, uparts, s, a.realm))
    print(f"  politica: undercount x{a.undercount:g} (rate/daily), cap do reino "
          f"x{a.realm_cap:g}, alvo {a.target_posts:g} posts/venda, "
          f"elasticidade {a.elasticity:g}")
    print(tsm_strings())
    if con:
        con.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {e}")
        sys.exit(1)
