"""Corre o pipeline completo do FarmAnalysis, por etapas, numa so ordem.

Etapas (ordem real de execucao):

    1. sync       TSM AppData.lua -> tsm_market/tsm_region (precos; --fetch-csv)
    2. scrape     Wowhead -> woweconomy/wowhead_zones/zone_<id>.json   (browser, lento)
    3. repair     limpa/normaliza os zone JSON + semeia a cache
    4. enrich     Blizzard API -> quality/binding/sellPrice (tsm_markets.db)
    5. rank       farm_ranking -> FarmReport.txt (EV por zona, mogs, jackpots)
    6. score      run.py -> MarketReport.txt (Farmscore/flip/investment)
    7. inventory  inventario CSV -> enrich + preco justo (inventory_priced.csv)
    8. journal    Journalator -> vendas reais + calibracao (heuristica se vazio)
    9. intel      SavedVariables -> data/intel.sqlite (eventos, stock, mails, undercuts)
   10. buyers     nome do comprador da mail -> venda que ja existe no journal
   11. pricing    dataset do jogo (modelo do reino + um factor por reino)
   12. tests      pytest tests -q

O CSV do inventario e o primeiro que existir entre ``inventory.csv`` e
``scripts/inventario.csv``; ``--inventory`` escolhe outro.

O mercado carrega-se na etapa 1 e todas as seguintes leem esse retrato; a etapa
inventory revalida o AppData (--rebuild) para o preco nunca sair de dados velhos.

Uso:

    python scripts/run_all.py                  # tudo o que e offline
    python scripts/run_all.py --scrape         # inclui o scrape Wowhead
    python scripts/run_all.py --only rank,inventory
    python scripts/run_all.py --only intel,buyers      # sempre que vou vender
    python scripts/run_all.py --skip enrich,score
    python scripts/run_all.py --dry-run        # so imprime a sequencia
    python scripts/run_all.py --list           # nomes das etapas

O scrape fica de fora por defeito (abre o browser e demora); tudo o resto corre.
Cada comando e mostrado antes de correr, e no fim ha um resumo com exit codes.
"""
from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

DB = "tsm_markets.db"
ZONES_DIR = ROOT / "woweconomy" / "wowhead_zones"
#: O export do TSM mudou de lugar mais do que uma vez; escolher o primeiro
#: que existe evita o pipeline morrer por um caminho antigo.
INVENTORY_CANDIDATES = ("inventory.csv", "scripts/inventario.csv",
                        "scripts/inventario_itemids.csv")


def default_inventory() -> str:
    for candidate in INVENTORY_CANDIDATES:
        if (ROOT / candidate).exists():
            return candidate
    return INVENTORY_CANDIDATES[0]

FARM_REPORT = "FarmReport.txt"
MARKET_REPORT = "MarketReport.txt"
MARKET_WARN = "MarketReport.warnings.log"   # 42k linhas de avisos do run.py
INVENTORY_OUT = "inventory_priced.csv"
INVENTORY_AUDIT = "inventory_audit.csv"    # todas as linhas do input + motivo

# Alvos do scrape: "zone=<id>/<slug>", tal como o scraper aceita na linha de comando.
SCRAPE_TARGETS = [
    "zone=331/ashenvale",
    "zone=719/blackfathom-deeps",
    "zone=16/azshara",
]


@dataclass
class Cmd:
    argv: list[str]
    out: Optional[str] = None          # redirecciona stdout para este ficheiro
    err: Optional[str] = None          # redirecciona stderr (senao herda o terminal)


@dataclass
class Stage:
    name: str
    title: str
    cmds: list[Cmd] = field(default_factory=list)
    opt_in: bool = False               # nao corre sem --scrape
    note: str = ""


def zone_files() -> list[str]:
    """Zone JSONs presentes no disco, ordem estavel."""
    return [str(p.relative_to(ROOT)).replace("\\", "/")
            for p in sorted(ZONES_DIR.glob("zone_*.json"))]


def build_stages(args) -> list[Stage]:
    zones = zone_files()
    targets = args.scrape_zone or SCRAPE_TARGETS
    inventory = getattr(args, "inventory", "") or default_inventory()

    sync_cmd = [PY, "scripts/sync_market.py", "--quiet"]
    if not args.fetch_csv:
        sync_cmd.append("--no-csv")
    stages = [
        Stage(
            "sync",
            "Carregar mercado: TSM AppData -> tsm_*"
            + (" + CSVs publicos" if args.fetch_csv else " (sem rede)"),
            [Cmd(sync_cmd)],
            note="AppData.lua do TSM desktop app (retrato oficial fica para --fetch-csv)",
        ),
        Stage(
            "scrape",
            "Wowhead -> zone JSON",
            [Cmd([PY, "-m", "woweconomy.services.wowhead", *targets, "all"])],
            opt_in=True,
            note="abre browser; usa cache por NPC (podes matar e retomar)",
        ),
        Stage(
            "repair",
            "Reparar zone JSON (nomes, cache, junk)",
            [Cmd([PY, "scripts/repair_zone.py", z, DB]) for z in zones],
        ),
        Stage(
            "enrich",
            "Blizzard: quality/binding/sellPrice",
            [Cmd([PY, "scripts/enrich_items.py", "--db", DB])],
            note="precisa de .env com BLIZZARD_CLIENT_ID/SECRET; resumivel",
        ),
        Stage(
            "rank",
            "Ranking de farms por zona -> " + FARM_REPORT,
            [Cmd([PY, "-m", "woweconomy.analytics.farm_ranking", *zones,
                  "--db", DB, "--blizz-db", DB, "--top", "20",
                  "--out", FARM_REPORT])],
        ),
        Stage(
            "score",
            "Farmscore/flip/investment -> " + MARKET_REPORT,
            [Cmd([PY, "run.py"], out=MARKET_REPORT, err=MARKET_WARN)],
        ),
        Stage(
            "inventory",
            "Inventario: enrich + preco justo -> " + INVENTORY_OUT,
            [
                # --rebuild relê o AppData.lua do TSM e reconstroi tsm_market/tsm_region
                Cmd([PY, "scripts/enrich_inventory.py", inventory, "--db", DB]),
                Cmd([PY, "scripts/calc_price.py", "--inventory", inventory,
                     "--db", DB, "--rebuild", "--out", INVENTORY_OUT,
                     "--audit", INVENTORY_AUDIT]),
            ],
            note="inventario: " + inventory,
        ),
        Stage(
            "journal",
            "Vendas reais (Journalator) + calibracao",
            [Cmd([PY, "scripts/journal.py", "import"]),
             Cmd([PY, "scripts/journal.py", "report", "--brief"])],
            note="le o SavedVariables do Journalator; sem dados, mantem a heuristica",
        ),
        Stage(
            "intel",
            "Addon: eventos, inventario, sellers, mails, undercuts -> data/intel.sqlite",
            [Cmd([PY, "scripts/intel.py", "addon-import"])],
            note="le as SavedVariables; corre depois de sair do jogo ou de /reload",
        ),
        Stage(
            "buyers",
            "Liga o comprador da mail a venda que ja existe (nunca cria vendas)",
            [Cmd([PY, "scripts/intel.py", "mail-link"])],
            note="actualiza auction_intel_events so onde counterparty esta vazio",
        ),
        Stage(
            "pricing",
            "Dataset do jogo: modelo do reino + indice dos 267 reinos",
            [Cmd([PY, "scripts/dfa_pricing_export.py", "--realm-model",
                  "--all-realms"])],
            note="demora ~3 min: refita o modelo que o jogo corre",
        ),
        Stage(
            "tests",
            "Testes (pytest)",
            [Cmd([PY, "-m", "pytest", "tests", "-q"])],
        ),
    ]
    return stages


def select(stages: list[Stage], args) -> list[Stage]:
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()}
    skip = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    known = {s.name for s in stages}
    for bad in (only | skip) - known:
        sys.exit(f"erro: etapa desconhecida {bad!r}; usa --list")

    picked = []
    for s in stages:
        if s.opt_in and not args.scrape and s.name not in only:
            continue
        if only and s.name not in only:
            continue
        if s.name in skip:
            continue
        if not s.cmds:
            continue
        picked.append(s)
    return picked


def run_cmd(cmd: Cmd, dry: bool) -> tuple[int, float]:
    shown = " ".join(cmd.argv)
    if cmd.out:
        shown += f"  >  {cmd.out}"
    if cmd.err:
        shown += f"  2>  {cmd.err}"
    print(f"  $ {shown}")
    if dry:
        return 0, 0.0

    out_cm = (open(ROOT / cmd.out, "w", encoding="utf-8") if cmd.out
              else contextlib.nullcontext(sys.stdout))
    err_cm = (open(ROOT / cmd.err, "w", encoding="utf-8") if cmd.err
              else contextlib.nullcontext())
    t0 = time.time()
    with out_cm as fo, err_cm as fe:
        rc = subprocess.run(cmd.argv, cwd=ROOT, stdout=fo, stderr=fe).returncode
    for name in (cmd.out, cmd.err):
        if name:
            print(f"    -> {name}")
    return rc, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Corre o pipeline completo (scrape -> rank -> score -> inventario -> testes).")
    ap.add_argument("--scrape", action="store_true",
                    help="inclui o scrape Wowhead (browser, lento)")
    ap.add_argument("--fetch-csv", action="store_true",
                    help="a etapa sync tambem baixa os CSVs publicos da TSM")
    ap.add_argument("--scrape-zone", action="append", default=[],
                    metavar="zone=<id>/<slug>",
                    help="alvo do scrape (repetivel; substitui os alvos por defeito)")
    ap.add_argument("--inventory", default="",
                    help="CSV do inventario (por defeito: o primeiro que existir)")
    ap.add_argument("--only", default="", help="corre so estas etapas (virgulas)")
    ap.add_argument("--skip", default="", help="salta estas etapas (virgulas)")
    ap.add_argument("--dry-run", action="store_true", help="imprime a sequencia, nao corre")
    ap.add_argument("--keep-going", action="store_true",
                    help="nao para na primeira etapa que falha")
    ap.add_argument("--list", action="store_true", help="lista as etapas e sai")
    args = ap.parse_args()

    stages = build_stages(args)
    if args.list:
        for s in stages:
            tag = "  (opt-in: --scrape)" if s.opt_in else ""
            print(f"{s.name:<10} {s.title}{tag}")
            if s.note:
                print(f"{'':<10}   nota: {s.note}")
        return 0

    picked = select(stages, args)
    if not picked:
        sys.exit("erro: nenhuma etapa para correr")

    zones = zone_files()
    print("=" * 78)
    print("FarmAnalysis - pipeline completo")
    print("=" * 78)
    print(f"  python   {PY}")
    print(f"  repo     {ROOT}")
    print(f"  db       {DB} ({'existe' if (ROOT / DB).exists() else 'FALTA'})")
    print(f"  zones    {len(zones)} zone JSON: {', '.join(zones) or '(nenhum)'}")
    print(f"  etapas   {', '.join(s.name for s in picked)}")
    if args.dry_run:
        print("  modo     DRY-RUN (nada e executado)")
    print()

    results: list[tuple[str, int, float]] = []
    for s in picked:
        print("-" * 78)
        print(f"[{s.name}] {s.title}")
        if s.note:
            print(f"  nota: {s.note}")
        failed = False
        stage_secs = 0.0
        for cmd in s.cmds:
            rc, secs = run_cmd(cmd, args.dry_run)
            stage_secs += secs
            if rc != 0:
                print(f"  !! exit {rc}")
                failed = True
                break
        results.append((s.name, 1 if failed else 0, stage_secs))
        print(f"  {'FALHOU' if failed else 'OK'}  ({stage_secs:.1f}s)")
        print()
        if failed and not args.keep_going and not args.dry_run:
            print("A parar na primeira falha (usa --keep-going para continuar).")
            break

    print("=" * 78)
    print("RESUMO")
    print("=" * 78)
    for name, rc, secs in results:
        print(f"  {'OK   ' if rc == 0 else 'FALHA'}  {name:<10} {secs:6.1f}s")
    bad = [n for n, rc, _ in results if rc != 0]
    if bad:
        print(f"\nEtapas com falha: {', '.join(bad)}")
        return 1
    print("\nTudo concluido." if not args.dry_run else "\n(dry-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
