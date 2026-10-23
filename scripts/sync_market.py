"""Carrega o mercado da melhor forma: TSM primeiro, Blizzard API so metadados.

PORQUE TSM (e nao Blizzard API) PARA PRECOS
-------------------------------------------
A API de leiloes da Blizzard devolve so um RETRATO das listagens atuais (cadencia
~1h): nao tem historico, nao tem preco medio, nao tem sale rate nem vendas
concluidas.  Alem disso o pedido de commodities consome 25 unidades de quota, o
limite e 36000 chamadas/hora e os termos exigem apagar dados com mais de 30 dias.
Logo: impossivel construir dai um "DBMarket" ou um sale rate.

Quem tem esses sinais e o TSM:
  * AppData.lua local (app desktop TSM escreve-o, tipicamente de hora a hora)
        por reino : minBuyout, marketValue(DBMarket 14d), numAuctions, historical
        por regiao: regionMarketValue, regionHistorical, regionSaleAvg,
                    regionSaleRate, regionSoldPerDay
  * CSVs publicos da TSM (oficiais, sem chave, sem conta)
        realm/items.csv    itemId,name,marketValue,minBuyout,recent,historical
        region/items.csv   itemId,name,marketValue,historical,avgSalePrice,
                           saleRate,soldPerDay,updatedAt
        commodities.csv    itemId,name,marketValue,minBuyout,recent,historical

A Blizzard API fica para o que ela so ela tem: metadados de item
(quality/binding/sellPrice) via scripts/enrich_items.py e scripts/enrich_inventory.py.

USO
    python scripts/sync_market.py --check       # so relatorio de frescura
    python scripts/sync_market.py               # AppData -> tsm_* e CSVs publicos
    python scripts/sync_market.py --no-csv      # so AppData -> tsm_*
    python scripts/sync_market.py --csv-only    # so os CSVs publicos
    python scripts/sync_market.py --force       # reescreve mesmo sem o AppData mudar
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.calc_price as cp  # noqa: E402  (AppData -> tsm_* + find_appdata)
from woweconomy.config import WowEconomyConfig  # noqa: E402
from woweconomy.services.fetcher import TSMFetcher  # noqa: E402

DB = "tsm_markets.db"
CSV_DIR = Path("csv")

try:  # acentos (nomes de reino, 'Português') legiveis no terminal moderno
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# modulos do AppData cuja data de atualizacao aparece em tsm_meta
MODULES = ("AUCTIONDB_NON_COMMODITY_DATA", "AUCTIONDB_NON_COMMODITY_HISTORICAL",
           "AUCTIONDB_NON_COMMODITY_SCAN_STAT", "COMMODITY_DATA")


def ago(seconds: float) -> str:
    h = seconds / 3600.0
    if h < 1:
        return f"{seconds / 60:.0f} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} dias"


def db_meta(db: str) -> dict:
    out: dict[str, str] = {}
    if not Path(db).exists():
        return out
    con = sqlite3.connect(db)
    try:
        out.update(dict(con.execute("SELECT key, value FROM tsm_meta")))
    except sqlite3.Error:
        pass
    con.close()
    return out


def counts(db: str) -> dict:
    out = {}
    if not Path(db).exists():
        return out
    con = sqlite3.connect(db)
    for t in ("tsm_market", "tsm_region", "blizzard_items"):
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = 0
    try:
        out["realms"] = con.execute(
            "SELECT COUNT(DISTINCT realm) FROM tsm_market").fetchone()[0]
    except sqlite3.Error:
        out["realms"] = 0
    con.close()
    return out


def check(db: str) -> None:
    print("=" * 78)
    print("FONTES DE MERCADO - frescura")
    print("=" * 78)

    print("\n[1] TSM AppData.lua (fonte principal, por reino + regiao)")
    p = cp.find_appdata(None)
    if not p:
        print("    NAO encontrado -> corre o TSM desktop app (ou usa os CSVs publicos)")
    else:
        st = os.stat(p)
        print(f"    {p}")
        print(f"    {st.st_size / 1e6:.1f} MB | atualizado ha {ago(time.time() - st.st_mtime)}"
              f"  ({datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M})")

    meta = db_meta(db)
    print("\n    modulos dentro do AppData (epoch -> idade), por reino:")
    for mod in MODULES:
        epochs = [(k.rsplit("_", 1)[-1], int(v)) for k, v in meta.items()
                  if k.startswith(f"dl_{mod}_") and v.isdigit()]
        if not epochs:
            continue
        newest = max(e for _, e in epochs)
        realms = ", ".join(sorted({r for r, _ in epochs})[:4])
        more = f" +{len(epochs) - 4}" if len(epochs) > 4 else ""
        print(f"      {mod:34s} {len(epochs):>3} reinos | mais recente "
              f"{datetime.fromtimestamp(newest):%Y-%m-%d %H:%M} "
              f"({ago(time.time() - newest)}) [{realms}{more}]")

    print(f"\n[2] base local {db} (o que os scripts leem)")
    c = counts(db)
    if not c:
        print("    nao existe (corre sem --check para carregar)")
    else:
        print(f"    tsm_market {c['tsm_market']:>7} linhas | tsm_region "
              f"{c['tsm_region']:>7} | reinos {c['realms']} | blizzard_items "
              f"{c['blizzard_items']:>6}")
        st = os.stat(db)
        print(f"    ficheiro atualizado ha {ago(time.time() - st.st_mtime)}")

    print("\n[3] CSVs publicos da TSM (rota oficial, sem app desktop)")
    for f in ("items.csv", "commodities.csv", "realm_items.csv"):
        path = CSV_DIR / f
        if not path.exists():
            print(f"    {str(path):22s} ausente")
            continue
        rows = 0
        updated = ""
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            head = next(csv.reader([lines[0]]), []) if lines else []
            rows = max(len(lines) - 1, 0)
            if "updatedAt" in head and len(lines) > 1:
                last = next(csv.DictReader(io.StringIO(lines[0] + "\n" + lines[-1])), {})
                updated = f" | updatedAt {last.get('updatedAt', '')}"
        except (OSError, UnicodeDecodeError):
            pass
        print(f"    {str(path):22s} {rows:>7} linhas | "
              f"ficheiro ha {ago(time.time() - path.stat().st_mtime)}{updated}")

    print("\n[4] Blizzard API - so metadados de item (quality/binding/sellPrice)")
    print("    sem historico, sem sale rate; dados limitados a 30 dias pelos termos")
    print("    -> scripts/enrich_items.py, scripts/enrich_inventory.py")
    print("    (retrato horario das listagens: util so como verificacao pontual)")


# --------------------------------------------------------------------------- #
# Carregar
# --------------------------------------------------------------------------- #
def bkp(path: Path) -> str:
    if not path.exists():
        return ""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
    shutil.copy2(path, dest)
    return str(dest)


def fetch_public_csvs() -> None:
    fetcher = TSMFetcher(WowEconomyConfig.default())

    # region/items.csv -> csv/items.csv (reutiliza o script que ja junta nomes)
    print("  [region/items.csv] via scripts/fetch_csv.py")
    rc = subprocess.run([sys.executable, "scripts/fetch_csv.py"], cwd=ROOT).returncode
    if rc != 0:
        print(f"    !! fetch_csv.py saiu com {rc}")

    # commodities.csv e realm/items.csv -> csv/ (mesmo padrao, com backup)
    for kind, path in (("commodities", CSV_DIR / "commodities.csv"),
                       ("realm_items", CSV_DIR / "realm_items.csv")):
        url = _url_for(kind, fetcher)
        print(f"  [{kind}] {url}")
        try:
            rows, fields = _get_csv(url)
        except Exception as exc:  # rede/HTTP
            print(f"    !! falhou: {type(exc).__name__}: {exc}")
            continue
        if not rows:
            print("    !! resposta vazia, ficheiro intacto")
            continue
        b = bkp(path)
        if b:
            print(f"    backup -> {b}")
        path.parent.mkdir(exist_ok=True)
        # sem BOM: espelha o ficheiro da TSM e os leitores existentes abrem com
        # encoding="utf-8" (um BOM estragaria a 1a coluna: '\ufeffitemId')
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"    {len(rows)} linhas -> {path}")


def _url_for(kind: str, fetcher) -> str:
    cfg = fetcher.config
    base = cfg.tsm_base_url
    if kind == "commodities":
        return f"{base}/{cfg.game_type}/{cfg.region_slug}/commodities.csv"
    return (f"{base}/{cfg.game_type}/{cfg.region_slug}/realm/"
            f"{cfg.realm_slug}/items.csv")


def _get_csv(url: str) -> tuple[list[dict], list[str]]:
    import requests
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig")
    rd = csv.DictReader(io.StringIO(text))
    rows = list(rd)
    return rows, list(rd.fieldnames or [])


def load_appdata(db: str, appdata: str, force: bool) -> None:
    p = cp.find_appdata(appdata or None)
    if not p:
        print("  !! AppData.lua nao encontrado; usa --csv-only ou corre o TSM desktop app")
        return
    fresh = True
    if Path(db).exists() and not force:
        fresh = os.stat(p).st_mtime > os.stat(db).st_mtime
    if not fresh:
        print(f"  base ja mais recente que o AppData (usa --force para reescrever)")
        return
    stats = cp.build_signals(p, db, verbose=False)
    print(f"  tsm_market {stats['market_rows']} linhas | tsm_region "
          f"{stats['region_rows']} | reinos {len(stats['realms'])}")
    cp.ensure_signals(db, p, rebuild=False)   # garante a vista boe


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Carrega/precifica a partir do TSM (AppData + CSVs publicos).")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--appdata", default="", help="AppData.lua explicito")
    ap.add_argument("--check", action="store_true", help="so relatorio, nao carrega")
    ap.add_argument("--no-appdata", action="store_true", help="nao mexer no AppData")
    ap.add_argument("--no-csv", action="store_true", help="nao ir buscar os CSVs publicos")
    ap.add_argument("--csv-only", action="store_true", help="so os CSVs publicos")
    ap.add_argument("--force", action="store_true", help="reescreve mesmo sem mudancas")
    ap.add_argument("--quiet", action="store_true", help="sem o relatorio de frescura no fim")
    ap.add_argument("--snapshots", action="store_true",
                    help="tambem descarrega snapshots por reino (fetch_tsm_snapshots.py)")
    ap.add_argument("--snapshot-kinds", default="realm_items",
                    help="kinds para snapshots (default: realm_items)")
    ap.add_argument("--snapshot-workers", type=int, default=1,
                    help="workers para snapshots (default: 1)")
    ap.add_argument("--snapshot-delay", type=float, default=10.0,
                    help="delay entre pedidos de snapshot em segundos (default: 10)")
    ap.add_argument("--snapshot-limit", type=int, default=0,
                    help="limitar a N ficheiros de snapshot (0 = todos)")
    a = ap.parse_args()

    if a.check:
        check(a.db)
        return 0

    do_appdata = not a.no_appdata and not a.csv_only
    do_csv = not a.no_csv

    print("=" * 78)
    print("Carregar mercado - TSM (AppData + CSVs publicos)")
    print("=" * 78)
    if do_appdata:
        print("\n[1] AppData.lua -> " + a.db)
        load_appdata(a.db, a.appdata, a.force)
    if do_csv:
        print("\n[2] CSVs publicos da TSM -> csv/")
        fetch_public_csvs()
    print("\n[3] Blizzard API: nada a carregar aqui (so metadados de item; sem historico)")
    if a.snapshots:
        print("\n[4] Snapshots por reino (TSM public CSVs)")
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "fetch_tsm_snapshots.py"),
            "--db", a.db,
            "--kinds", a.snapshot_kinds,
            "--workers", str(a.snapshot_workers),
            "--delay", str(a.snapshot_delay),
            "--limit", str(a.snapshot_limit),
        ]
        print(" ".join(cmd))
        subprocess.run(cmd, check=False)
    print("\nfeito. proximo: python scripts/calc_price.py --inventory scripts/inventario.csv")
    if not a.quiet:
        check(a.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
