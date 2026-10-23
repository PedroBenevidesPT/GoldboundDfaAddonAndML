"""Diario de vendas/reposts: dados REAIS para calibrar a heuristica.

Duas fontes, e a segunda e opcional:

1) Journalator (addon) — SavedVariables/JOURNALATOR_STATISTICS tem, por item e
   reino, o que ACONTECEU nas tuas vendas:
       successes  = anuncios que venderam
       failures   = anuncios que expiraram sem vender
       totalSold  = {count, value}   (copper -> guardamos gold)
       totalBought= {count, value}   (o que compraste)
   Daqui sai o teu sell-through REAL (successes/(successes+failures)), o teu
   preco realizado e os teus POSTS POR VENDA reais — os numeros que a heuristica
   adivinha com --target-posts/--undercount/--elasticity.

2) Registo manual (para o que o addon nao apanhar ou para apontar a mao):
       python scripts/journal.py log-sale --item "Wool Cloth" --price 5 --qty 99
       python scripts/journal.py log-post --item 1351 --price 12632
       python scripts/journal.py log-fail --item 1351 --note "expirou 48h"
       python scripts/journal.py log-buy  --item 124640 --price 800 --qty 3
   Ou preenche um CSV e importa-o (aceita ';' ou ','):
       date;kind;item;price_gold;qty;note
       2026-10-16;sale;1351;12632;1;vendeu ao 2o post

   COMO CONTAR AS TENTATIVAS (importa para o racio posts/venda):
   uma tentativa = UM desfecho, log-sale (vendeu) ou log-fail (expirou).  Um
   REPOST e outra tentativa: se voltar a expirar, e outro log-fail.  Assim os
   reposts CONTAM no racio em vez de o inflacionarem com posts pendentes.
   log-post/log-cancel nao entram no racio (seria contar 2x o mesmo anuncio).

   O reino e inferido (o do addon; senao o mais frequente da base), para os teus
   registos fundirem com os do addon em vez de criarem uma linha paralela.  Se
   registares a mao algo que o addon ja viu, o report avisa que esta a contar 2x.

USO
    python scripts/journal.py import                 # Journalator -> tsm_markets.db
    python scripts/journal.py report                  # calibracao vs heuristica
    python scripts/journal.py report --realm Dragonmaw --min-sales 3
    python scripts/journal.py import-csv journal.csv
    python scripts/journal.py stats                    # so o que esta guardado

O report usa as DUAS fontes: o manual conta para o sell-through, para os posts
por venda e para o preco realizado, tal como os dados do addon.

Se os dados forem poucos, o report DIZ que sao poucos e a heuristica fica como
esta; nao inventa calibracao com 2 vendas.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = "tsm_markets.db"

# Onde o WoW guarda o Journalator (conta/reino/char variaveis -> procuramos)
WTF_GLOBS = [
    r"C:/Program Files (x86)/World of Warcraft/*/WTF/Account/*/SavedVariables/"
    r"Journalator_Statistics.lua",
    r"C:/Program Files/World of Warcraft/*/WTF/Account/*/SavedVariables/"
    r"Journalator_Statistics.lua",
    r"C:/Users/*/World of Warcraft/*/WTF/Account/*/SavedVariables/"
    r"Journalator_Statistics.lua",
]

KINDS = ("sale", "post", "fail", "cancel", "buy")
MIN_ITEMS_FOR_CALIBRATION = 3     # com menos itens, mantem a heuristica
MIN_SALES_FOR_CALIBRATION = 5

# Dinheiro em COPPER (convencao do repo: 1 gold = 10000 copper); a CLI e o
# relatorio falam em gold.  Guardar copper evita perder centavos em itens baratos.
SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_stats (
    itemId       INTEGER NOT NULL,
    name         TEXT    NOT NULL,
    realm        TEXT    NOT NULL,
    successes    INTEGER NOT NULL DEFAULT 0,
    failures     INTEGER NOT NULL DEFAULT 0,
    sold_count   INTEGER NOT NULL DEFAULT 0,
    sold_copper  INTEGER NOT NULL DEFAULT 0,
    bought_count INTEGER NOT NULL DEFAULT 0,
    bought_copper INTEGER NOT NULL DEFAULT 0,
    source       TEXT    NOT NULL DEFAULT 'journalator',
    imported_at  TEXT    NOT NULL,
    PRIMARY KEY (itemId, realm)
);
CREATE TABLE IF NOT EXISTS journal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    itemId       INTEGER,
    name         TEXT    NOT NULL,
    realm        TEXT    NOT NULL DEFAULT '',
    qty          INTEGER NOT NULL DEFAULT 1,
    price_copper INTEGER NOT NULL DEFAULT 0,   -- por unidade
    note         TEXT    NOT NULL DEFAULT '',
    UNIQUE (ts, kind, name, realm, qty, price_copper)
);
"""


def conn(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    # migracao: a 1a versao guardava gold arredondado e perdia centavos; as
    # tabelas so tem dados re-importaveis do Journalator, logo recria-se.
    for tbl, ref in (("journal_stats", "sold_copper"), ("journal", "price_copper")):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})")]
        if cols and ref not in cols:
            con.execute(f"DROP TABLE {tbl}")
            print(f"  (migracao: {tbl} passou a copper — corre 'import' outra vez)")
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- #
# Journalator
# --------------------------------------------------------------------------- #
def find_journalator() -> str | None:
    import glob
    hits = [p for g in WTF_GLOBS for p in glob.glob(g)]
    return max(hits, key=lambda p: Path(p).stat().st_mtime) if hits else None


def parse_statistics(txt: str) -> dict[tuple[str, str], dict]:
    """Le o lua do Journalator (tabelas regulares) sem interpretar Lua.

    Estrutura: STATS["Items"]["<nome>"]["<reino>"] = { successes, failures,
    totalBought{count,value}, totalSold{count,value} }.
    """
    stack: list[str] = []
    out: dict[tuple[str, str], dict] = {}
    pending: str | None = None
    for line in txt.splitlines():
        s = line.strip()
        m = re.match(r'^\["(.*?)"\] = \{', s)
        if m:
            stack.append(m.group(1))
            pending = m.group(1) if m.group(1) in ("totalSold", "totalBought") else None
            continue
        m = re.match(r'^\["(.*?)"\] = (-?[\d.]+),', s)
        if m and len(stack) >= 3:
            blk = out.setdefault((stack[1], stack[2]), {})
            key = m.group(1)
            if pending in ("totalSold", "totalBought"):
                blk[f"{pending}_{key}"] = float(m.group(2))
            elif key in ("successes", "failures"):
                blk[key] = float(m.group(2))
            continue
        if s.startswith("},") or s == "}":
            if stack:
                stack.pop()
            if not stack or stack[-1] not in ("totalSold", "totalBought"):
                pending = None
    return out


def import_journalator(con: sqlite3.Connection, path: str | None, verbose=True) -> int:
    path = path or find_journalator()
    if not path:
        print("  Journalator: SavedVariables nao encontrado (ja usaste o addon?)")
        return 0
    txt = Path(path).read_text(encoding="utf-8", errors="replace")
    blocks = parse_statistics(txt)
    names = dict(con.execute("SELECT name, itemId FROM items").fetchall())
    dup = {n: i for i, n in con.execute("SELECT itemId, name FROM items")}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for (name, realm), b in blocks.items():
        iid = dup.get(name) or names.get(name)
        if not iid:
            continue
        sold_c = int(b.get("totalSold_count", 0))
        bought_c = int(b.get("totalBought_count", 0))
        con.execute(
            "INSERT INTO journal_stats (itemId,name,realm,successes,failures,"
            "sold_count,sold_copper,bought_count,bought_copper,source,imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'journalator', ?) "
            "ON CONFLICT(itemId,realm) DO UPDATE SET "
            "successes=excluded.successes, failures=excluded.failures, "
            "sold_count=excluded.sold_count, sold_copper=excluded.sold_copper, "
            "bought_count=excluded.bought_count, bought_copper=excluded.bought_copper, "
            "imported_at=excluded.imported_at",
            (iid, name, realm, int(b.get("successes", 0)), int(b.get("failures", 0)),
             sold_c, int(b.get("totalSold_value", 0)),
             bought_c, int(b.get("totalBought_value", 0)), now))
        n += 1
    con.commit()
    if verbose:
        s = sum(b.get("successes", 0) for b in blocks.values())
        f = sum(b.get("failures", 0) for b in blocks.values())
        print(f"  Journalator: {path}")
        print(f"  {n} itens/reinos importados | vendas reais {int(s)} | "
              f"expiracoes {int(f)}")
    return n


# --------------------------------------------------------------------------- #
# Registo manual
# --------------------------------------------------------------------------- #
def resolve_item(con: sqlite3.Connection, token: str) -> tuple[int | None, str]:
    t = token.strip()
    if t.isdigit():
        iid = int(t)
        row = con.execute("SELECT name FROM items WHERE itemId=?", (iid,)).fetchone()
        return iid, (row[0] if row else "")
    row = con.execute("SELECT itemId FROM items WHERE name=? COLLATE NOCASE",
                      (t,)).fetchone()
    return (row[0] if row else None), t


def default_realm(con: sqlite3.Connection) -> str:
    """O reino do utilizador, inferido em vez de assumido.

    Sem isto o registo manual ficava com reino vazio e NUNCA fundia com o do
    addon: o mesmo item aparecia duas vezes no relatorio e a calibracao via-os
    como itens diferentes.  Ordem: o reino que o addon ja registou (journal_stats)
    e senao o mais frequente em tsm_market.
    """
    for sql in ("SELECT realm, COUNT(*) c FROM journal_stats WHERE realm<>'' "
                "GROUP BY realm ORDER BY c DESC LIMIT 1",
                "SELECT realm, COUNT(*) c FROM tsm_market WHERE realm<>'' "
                "GROUP BY realm ORDER BY c DESC LIMIT 1"):
        try:
            row = con.execute(sql).fetchone()
        except sqlite3.OperationalError:      # tabela ainda nao existe
            continue
        if row:
            return row[0]
    return ""


def add_entry(con: sqlite3.Connection, kind: str, item: str, price: float,
              qty: int, realm: str, when: str, note: str) -> None:
    if kind not in KINDS:
        sys.exit(f"erro: kind tem de ser um de {', '.join(KINDS)}")
    realm = realm or default_realm(con)       # para fundir com os dados do addon
    iid, name = resolve_item(con, item)
    copper = round(float(price or 0) * 10000)     # a CLI fala gold, guarda copper
    # auto-ts ao minuto: repetir o mesmo registo no mesmo minuto e o mesmo evento
    # (dedupe), mas vendas repetidas mais tarde contam como eventos separados
    ts = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    gold = copper / 10000
    try:
        con.execute("INSERT INTO journal (ts,kind,itemId,name,realm,qty,price_copper,"
                    "note) VALUES (?,?,?,?,?,?,?,?)",
                    (ts, kind, iid, name, realm, qty, copper, note))
        con.commit()
        print(f"  + {kind} {name} x{qty} @ {gold:,.2f}g "
              f"{('[' + realm + ']') if realm else ''}"
              f"{('- ' + note) if note else ''}")
    except sqlite3.IntegrityError:
        print(f"  = ja estava registado: {kind} {name} x{qty} @ {gold:,.2f}g")


def import_csv(con: sqlite3.Connection, path: str) -> None:
    raw = Path(path).read_bytes().decode("utf-8-sig")
    sample = raw[:4096]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    rows = list(csv.DictReader(io.StringIO(raw), delimiter=delim))
    if not rows:
        sys.exit(f"erro: {path} vazio")
    def col(r, *names):
        for n in names:
            for k in r:
                if k and k.strip().lower() == n.lower():
                    return (r[k] or "").strip()
        return ""
    n = 0
    for r in rows:
        item = col(r, "item", "itemid", "item_id", "name", "nome")
        if not item:
            continue
        add_entry(con, (col(r, "kind", "tipo") or "sale").lower(), item,
                  col(r, "price_gold", "price", "preco", "preço") or 0,
                  int(float(col(r, "qty", "quantity", "quantidade") or 1)),
                  col(r, "realm", "reino"), col(r, "date", "ts", "data"),
                  col(r, "note", "nota"))
        n += 1
    print(f"  {n} linhas lidas de {path}")


# --------------------------------------------------------------------------- #
# Relatorio / calibracao
# --------------------------------------------------------------------------- #
def load_priced(path: str = "inventory_priced.csv") -> dict[int, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    raw = p.read_bytes().decode("utf-8-sig")
    delim = ";" if raw[:4096].count(";") >= raw[:4096].count(",") else ","
    out = {}
    for r in csv.DictReader(io.StringIO(raw), delimiter=delim):
        try:
            out[int(r["ItemID"])] = r
        except (KeyError, ValueError):
            continue
    return out


def report(db: str, realm: str, min_sales: int, brief: bool = False) -> int:
    con = sqlite3.connect(db)
    where, params = ("", [])
    if realm:
        where, params = ("WHERE realm=?", [realm])
    stats = list(con.execute(
        f"SELECT itemId,name,realm,successes,failures,sold_count,sold_copper,"
        f"bought_count,bought_copper FROM journal_stats {where} "
        f"ORDER BY successes DESC", params))
    manual = list(con.execute(
        "SELECT kind,name,realm,qty,price_copper,itemId FROM journal"
        + (" WHERE realm=?" if realm else ""), params))
    # copper na base -> gold aqui (mesma unidade do inventory_priced.csv)
    tsm = {r[0]: r for r in con.execute(
        "SELECT itemId, region_sale_rate, region_sold_per_day, region_sale_avg "
        "FROM tsm_region")}
    priced = load_priced()
    con.close()

    # 1) O REGISTO MANUAL entra na calibracao (antes so era contado, nunca usado).
    #    Uma tentativa = UM desfecho: log-sale (vendeu) ou log-fail (expirou).
    #    Um REPOST e outra tentativa, logo outro log-fail se voltar a expirar ->
    #    o racio posts/venda passa a contar os reposts em vez de os esconder.
    #    (log-post/log-cancel nao entram no racio: se um post e o seu fail
    #    contassem, a mesma tentativa seria contada duas vezes.)
    man: dict[tuple[int, str], dict] = {}
    unresolved = 0
    for kind, name, rlm, qty, price_copper, iid in manual:
        if iid is None:                  # nome que nao bate com a base: sem id
            unresolved += 1
            continue
        b = man.setdefault((iid, rlm or ""), dict(name=name, succ=0, fail=0,
                                                  sc=0, sco=0))
        b["name"] = b["name"] or name
        if kind == "sale":               # vendeu -> tentativa bem sucedida
            b["succ"] += 1
            b["sc"] += qty
            b["sco"] += qty * price_copper
        elif kind == "fail":             # expirou -> tentativa falhada
            b["fail"] += 1

    # 2) funde addon + manual na MESMA forma, para o resto do relatorio ser um so
    #    caminho de codigo (sem duplicar a logica de metricas).
    merged: dict[tuple[int, str], dict] = {}
    for iid, name, rlm, succ, fail, sc, sco, bc, bco in stats:
        merged[(iid, rlm)] = dict(iid=iid, name=name, realm=rlm, succ=succ,
                                  fail=fail, sc=sc, sco=sco, auto=True, man=False)
    for key, b in man.items():
        m = merged.get(key)
        if m is None:
            m = merged[key] = dict(iid=key[0], name=b["name"] or str(key[0]),
                                   realm=key[1], succ=0, fail=0, sc=0, sco=0,
                                   auto=False, man=False)
        m["succ"] += b["succ"]
        m["fail"] += b["fail"]
        m["sc"] += b["sc"]
        m["sco"] += b["sco"]
        m["man"] = True
    rows = sorted(merged.values(), key=lambda r: (-r["succ"], -r["fail"]))
    both = [r for r in rows if r["auto"] and r["man"]]

    print("=" * 78)
    print("DADOS REAIS (Journalator + registo manual)")
    print("=" * 78)
    tot_s = sum(r["succ"] for r in rows)
    tot_f = sum(r["fail"] for r in rows)
    n_auto = sum(1 for r in rows if r["auto"])
    n_man = sum(1 for r in rows if r["man"])
    print(f"  {len(rows)} itens com historico | vendas {tot_s} | expiracoes {tot_f}"
          f" | do addon {n_auto} | do registo manual {n_man}")
    if unresolved:
        print(f"  ({unresolved} registos manuais sem itemId: o nome nao bate com"
              f" nenhum item da base — usa o id)")
    if not rows:
        print("\n  Sem dados reais ainda.")
        print("  -> inferencia mantida (heuristica): --target-posts, --undercount, "
              "--elasticity continuam a ser as suposicoes documentadas.")
        return 0
    if both:
        print(f"  AVISO: {len(both)} itens tem addon E registo manual — se"
              f" registaste a mao o mesmo evento que o addon ja viu, esta a contar"
              f" 2x: {', '.join(r['name'][:18] for r in both[:3])}")

    if not brief:
        print(f"\n  {'item':<28} {'reino':<12} {'vend':>4} {'exp':>4} "
              f"{'sell-thr':>8} {'posts/vend':>10} {'realizado':>11} "
              f"{'TSM venda':>10} {'posta':>10} {'bias':>6}")
        print("  " + "-" * 106)
    rows_cal = []
    zero: list[tuple] = []
    for r in rows:
        iid, name, rlm = r["iid"], r["name"], r["realm"]
        succ, fail, sc, sco = r["succ"], r["fail"], r["sc"], r["sco"]
        posts = succ + fail
        thr = succ / posts if posts else 0.0
        pps = (posts / succ) if succ else 0.0
        real = (sco / sc / 10000) if sc else 0.0          # gold por unidade
        p = priced.get(iid, {})
        try:
            posta = float(p.get("posta") or 0)
        except ValueError:
            posta = 0.0
        bias = (real / posta) if (real and posta) else 0.0
        t = tsm.get(iid)
        rate = (t[1] or 0) if t else 0
        tsm_avg = (t[3] / 10000) if t and t[3] else 0.0   # copper -> gold
        imp_under = (thr / rate) if (thr and rate) else 0.0
        rows_cal.append((name, thr, pps, real, posta, bias, imp_under, succ))
        if succ <= 0:                       # nunca vendeu -> ver-seccao propria
            zero.append((name, rlm, int(fail), tsm_avg, posta, iid))
            continue
        if not brief:
            print(f"  {name[:28]:<28} {rlm[:12]:<12} {int(succ):>4} {int(fail):>4} "
                  f"{thr:>7.0%} {pps:>10.1f} {real:>10,.2f}g "
                  f"{tsm_avg:>9,.2f}g {posta:>9,.0f}g {bias:>6.2f}")

    # Nunca vendeu mas eu recomendo um preco: e aqui que a inflacao aparece.
    inflado = [z for z in zero if z[4] > 0 and z[3] > 0 and z[4] / z[3] >= 3]
    if not brief:
        print(f"\n  JA EXPIRARAM SEM VENDER (e estao no meu output): {len(zero)} itens")
        print(f"  {'item':<28} {'exp':>4} {'TSM venda':>11} {'minha posta':>12} "
              f"{'posta/venda':>12}")
        print("  " + "-" * 72)
        for name, rlm, fails, tsm_avg, posta, iid in sorted(
                zero, key=lambda z: -(z[4] / z[3] if z[3] else 0))[:20]:
            ratio = (posta / tsm_avg) if tsm_avg else 0
            flag = "  <-- inflacao" if ratio >= 3 else ""
            print(f"  {name[:28]:<28} {fails:>4} {tsm_avg:>10,.2f}g {posta:>11,.0f}g "
                  f"{ratio:>11.1f}x{flag}")
        if len(zero) > 20:
            print(f"  ... +{len(zero) - 20} itens")
    if inflado:
        print(f"\n  AVISO: {len(inflado)} itens que ja expiraram estao com posta >= 3x o"
              f" que o TSM diz que vende")
        print("        (expirar uma vez nao prova que o preco e meu, mas a inflacao e real)")

    if brief:
        print("  (usa 'report' sem --brief para a tabela completa)")
    print("\n" + "=" * 78)
    print("CALIBRACAO DA HEURISTICA")
    print("=" * 78)

    live = [r for r in rows_cal if r[7] >= 1]
    if len(live) < MIN_ITEMS_FOR_CALIBRATION or tot_s < min_sales:
        print(f"  Dados insuficientes ({len(live)} itens com venda, {tot_s} vendas).")
        print(f"  Regra: preciso de >={MIN_ITEMS_FOR_CALIBRATION} itens e "
              f">={min_sales} vendas.")
        print("  -> HEURISTICA MANTIDA: --target-posts 10, --undercount 1.25, "
              "--elasticity 1.0")
        return 0

    pps = [r[2] for r in live]
    thr = [r[1] for r in live]
    under = [r[6] for r in live if 0 < r[6] <= 10]
    bias = [r[5] for r in live if r[5] > 0]
    med_pps = st.median(pps)
    med_thr = st.median(thr)
    print(f"  itens com venda: {len(live)} | vendas: {tot_s}")
    print("  AVISO: estes itens com venda sao "
          + ", ".join(r[0].split()[-1].lower() for r in live[:4])
          + " — commodities que escoam no 1o post.  O --target-posts so age no"
          " caminho \"sem oferta\" (mogs iliquidos), onde ainda nao tens vendas:"
          " os mogs so tem expiracoes.  Nao uses isto para apertar o alvo sem olhar")
    print(f"  posts por venda REAL (mediana): {med_pps:.1f}   "
          f"-> --target-posts {med_pps:.0f}  (default 10)")
    print(f"  sell-through REAL (mediana):    {med_thr:.0%}")
    if under:
        print(f"  undercount implicito (sell-through real / DBRegionSaleRate): "
              f"mediana {st.median(under):.2f}x  (default 1.25)")
    else:
        print("  undercount implicito: sem DBRegionSaleRate para comparar")
    if bias:
        med_bias = st.median(bias)
        print(f"  preco realizado / a minha 'posta': {med_bias:.2f}x  "
              f"({'>' if med_bias > 1 else '<'}1 = eu estou a pedir "
              f"{'abaixo' if med_bias > 1 else 'acima'} do que vendes de facto)")
    print("\n  Aplica (se concordares) com:")
    print(f"    --target-posts {med_pps:.0f}"
          + (f" --undercount {st.median(under):.2f}" if under else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Diario de vendas/reposts + calibracao.")
    ap.add_argument("--db", default=DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="Journalator Statistics -> base")
    p.add_argument("--path", default="", help="Journalator_Statistics.lua explicito")

    log_parent = argparse.ArgumentParser(add_help=False)
    log_parent.add_argument("--item", required=True,
                            help="id ou nome (ex. 1351 ou \"Wool Cloth\")")
    log_parent.add_argument("--price", type=float, default=0,
                            help="gold POR UNIDADE")
    log_parent.add_argument("--qty", type=int, default=1)
    log_parent.add_argument("--realm", default="")
    log_parent.add_argument("--when", default="", help="ISO; default = agora")
    log_parent.add_argument("--note", default="")
    for kind, helptxt in (("sale", "vendeu"), ("post", "postei"),
                          ("fail", "expirou sem vender"),
                          ("cancel", "cancelei"), ("buy", "comprei")):
        sub.add_parser(f"log-{kind}", parents=[log_parent], help=helptxt)
    sub.add_parser("stats", help="o que esta guardado")

    p = sub.add_parser("import-csv", help="importar um CSV de registos")
    p.add_argument("csv")

    p = sub.add_parser("report", help="calibracao da heuristica com dados reais")
    p.add_argument("--realm", default="")
    p.add_argument("--min-sales", type=int, default=MIN_SALES_FOR_CALIBRATION)
    p.add_argument("--brief", action="store_true")

    a = ap.parse_args()
    if a.cmd.startswith("log-"):
        con = conn(a.db)
        add_entry(con, a.cmd[4:], a.item, a.price, a.qty, a.realm, a.when, a.note)
        return 0
    if a.cmd == "import":
        con = conn(a.db)
        import_journalator(con, a.path or None)
        return 0
    if a.cmd == "import-csv":
        con = conn(a.db)
        import_csv(con, a.csv)
        return 0
    if a.cmd == "stats":
        con = conn(a.db)
        for row in con.execute(
                "SELECT name,realm,successes,failures,sold_count,sold_copper "
                "FROM journal_stats ORDER BY successes DESC LIMIT 40"):
            print("  %-30s %-12s vend=%d exp=%d realizado=%s" %
                  (row[0][:30], row[1], row[2], row[3],
                   f"{row[5] / row[4] / 10000:,.2f}g" if row[4] else "-"))
        n = con.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        print(f"  registos manuais: {n}")
        return 0
    return report(a.db, a.realm, a.min_sales, a.brief)


if __name__ == "__main__":
    sys.exit(main())
