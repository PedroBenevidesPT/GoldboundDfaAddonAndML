"""BoE targets — the sellable items whose look collectors still lack.

What it joins
    db2_items              binding=2 (BoE), era=0 (retail)          -> the universe
    db2_item_appearance    itemId -> appearanceId                  -> what it looks like
    demand_appearances.csv appearanceId -> missing_pct, owned_pct  -> who still needs it
    latest TSM snapshot    price / saleRate / soldPerDay           -> evidence it moves

The join is by ID on both sides, which is why it can be exact: the client's
`ItemAppearance.ID` is the same `visualid` the Data for Azeroth catalogue uses,
so an item is "missing" when any of its appearances is still missing for the
sampled collectors -- no name matching, no 18% ceiling.

Status per item
    falta          pelo menos uma aparência ainda falta aos coleccionadores
    comum          tem aparência obtível, mas nenhuma falta: já todos têm
    inobtenivel    todas as aparências são de fonte morta no catálogo do DfA
                   (o BoE em si ainda vende -- quem o compra passa a ter a
                   aparência -- mas o DfA não lhe chama procura)
    fora_catalogo  tem aparência, mas nenhuma delas está no catálogo do DfA
    sem_aparencia  não tem linha em db2_item_appearance (receitas, anéis,
                   trinkets, tokens: BoE sem transmog)

Direcção do dado
    O endpoint de colecções do DfA devolve o que a conta TEM.  O scan deste
    projecto subtrai isso ao catálogo (`obtainable - owned`) e grava o
    complemento em `dfa.db :: missing`; `demand_appearances.csv` é escrito a
    partir dessa tabela.  Logo o CSV lido aqui já é "o que falta", não "o que
    se tem" -- confirmado nos dados (missing_pct x owned_pct, r = -0.98).

What this does NOT claim
    `missing_pct` é a fatia da amostra que ainda não tem a aparência, não uma
    promessa de venda.  Preço e liquidez (TSM) vão ao lado de cada linha; a
    coluna `valor` é só uma chave de ordenação (preço x falta), não um EV.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from woweconomy.analytics.collector_demand import (
    default_dfa_dir,
    item_appearances,
    load_appearance_demand,
)
from woweconomy.analytics.farm_ranking import ItemMarket, _g, load_latest_market
from woweconomy.services.db2_items import Db2Item, QUALITY_NAMES, load_map

logger = logging.getLogger(__name__)

DEFAULT_DB = "tsm_markets.db"
DEFAULT_OUT = "reports/boe_targets.csv"
DEFAULT_FALTAM_OUT = "reports/boe_faltam.csv"

STATUS_FALTA = "falta"
STATUS_COMUM = "comum"
STATUS_INOBTENIVEL = "inobtenivel"
STATUS_FORA_CATALOGO = "fora_catalogo"
STATUS_SEM = "sem_aparencia"

# The DfA catalogue says whether an appearance can still be got: 1 and 2 mean
# gone, 3 means obtainable but time/region gated.  Same rule as the scanner.
UNOBTAINABLE_CODES = (1, 2)

# Same reading of ExpansionID used by the binding report; labels only.
EXPANSIONS = {
    0: "vanilla", 1: "tbc", 2: "wotlk", 3: "cata", 4: "mop", 5: "wod",
    6: "legion", 7: "bfa", 8: "sl", 9: "df", 10: "tww", 11: "midnight",
}


def expansion_name(expansion: Optional[int]) -> str:
    return EXPANSIONS.get(int(expansion or 0), f"exp{expansion}")


@dataclass
class BoeTarget:
    """One BoE retail item, with its best still-missing appearance."""
    item_id: int
    name: str
    quality: str = ""
    expansion: str = ""
    item_level: int = 0
    required_level: int = 0
    appearances: int = 0          # appearance ids the item grants
    missing_appearances: int = 0  # ... of which the sample still lacks
    missing_count: int = 0        # sampled accounts lacking the best of them
    collectors: int = 0           # size of the sample that answered
    missing_pct: Optional[float] = None  # best (highest) missing share
    owned_pct: Optional[float] = None
    demand_value: Optional[float] = None
    price: float = 0.0            # copper (TSM avgSalePrice)
    sale_rate: float = 0.0
    sold_per_day: float = 0.0
    status: str = STATUS_SEM

    @property
    def missing(self) -> bool:
        return self.status == STATUS_FALTA

    @property
    def value(self) -> float:
        """Sort key only: price x missing share.  Not an expected value."""
        if not self.missing or self.price <= 0:
            return 0.0
        return self.price * (self.missing_pct or 0.0) / 100.0


# ----------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------

def appearance_catalog(dfa_dir: Path) -> Optional[Dict[int, bool]]:
    """appearanceId -> obtainable?, from the DfA catalogue cache.

    The demand CSV only lists what is *obtainable* and still missing, so
    without this an item whose looks are dead would be silently called
    "comum".  Returns None when the cache is absent (statuses fall back).
    """
    path = Path(dfa_dir) / "cache" / "appearancesources.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = data.get("appearancesources", []) if isinstance(data, dict) else data
    out: Dict[int, bool] = {}
    for row in rows:
        raw = row.get("visualid", row.get("id"))
        try:
            key = int(raw)
        except (TypeError, ValueError):
            continue
        out[key] = (not row.get("excluded")
                    and row.get("unobtainable") not in UNOBTAINABLE_CODES)
    return out


def boe_items(db2: Dict[int, Db2Item]) -> List[Db2Item]:
    """The sellable universe this report is about: BoE and retail."""
    return [i for i in db2.values() if i.binding == 2 and not i.era]


# ----------------------------------------------------------------------
# Join
# ----------------------------------------------------------------------

def lookup_demand(item_id: int, db_path: str = DEFAULT_DB,
                  dfa_dir: Optional[Path] = None) -> tuple:
    """(number of appearances, best still-missing wishlist row or None).

    Cheap enough for a single-item lookup: reads only that item's appearances
    and then the demand CSV.  Never raises when the DfA folder is absent.
    """
    apps = item_appearances(db_path, [item_id]).get(item_id, [])
    if not apps:
        return 0, None
    folder = Path(dfa_dir) if dfa_dir else default_dfa_dir()
    if folder is None or not folder.exists():
        return len(apps), None
    return len(apps), load_appearance_demand(folder).row_for(apps)


def build_targets(db_path: str = DEFAULT_DB, dfa_dir: Optional[Path] = None,
                  market: Optional[Dict[int, ItemMarket]] = None,
                  catalog: Optional[Dict[int, bool]] = None
                  ) -> List[BoeTarget]:
    """Every BoE retail item, with its collection status."""
    dfa_dir = dfa_dir or default_dfa_dir()
    if dfa_dir is None or not Path(dfa_dir).exists():
        raise FileNotFoundError(
            "pasta do Data for Azeroth nao encontrada: passa --dfa-dir "
            "(ou define DFA_DIR)")
    demand = load_appearance_demand(dfa_dir)
    if catalog is None:
        catalog = appearance_catalog(dfa_dir)
    market = market if market is not None else load_latest_market(db_path)
    items = boe_items(load_map(db_path))
    looks = item_appearances(db_path, [i.item_id for i in items])

    targets: List[BoeTarget] = []
    for item in items:
        apps = looks.get(item.item_id, [])
        best = demand.row_for(apps) if apps else None
        lacking = [demand.missing.get(a, 0) for a in apps]
        m = market.get(item.item_id)
        target = BoeTarget(
            item_id=item.item_id, name=item.name,
            quality=QUALITY_NAMES.get(item.quality, f"Q{item.quality}"),
            expansion=expansion_name(item.expansion),
            item_level=item.item_level, required_level=item.required_level,
            appearances=len(apps),
            missing_appearances=sum(1 for n in lacking if n > 0),
            missing_count=max(lacking) if lacking else 0,
            collectors=demand.sample,
            missing_pct=best.missing_pct if best else None,
            owned_pct=best.owned_pct if best else None,
            demand_value=best.value if best else None,
            price=m.price if m is not None else 0.0,
            sale_rate=m.sale_rate if m is not None else 0.0,
            sold_per_day=m.sold_per_day if m is not None else 0.0,
        )
        if not apps:
            target.status = STATUS_SEM
        elif best is not None:
            target.status = STATUS_FALTA
        elif catalog is None:
            target.status = STATUS_COMUM  # no catalogue: cannot tell further
        elif not any(a in catalog for a in apps):
            target.status = STATUS_FORA_CATALOGO
        elif any(catalog.get(a) for a in apps):
            target.status = STATUS_COMUM  # gettable and nobody lacks it
        else:
            target.status = STATUS_INOBTENIVEL
        targets.append(target)
    return targets


def sort_targets(targets: Iterable[BoeTarget], key: str = "falta") -> List[BoeTarget]:
    """falta = most-wanted first, preco = price, valor = price x missing."""
    rows = list(targets)
    if key == "preco":
        return sorted(rows, key=lambda t: -t.price)
    if key == "valor":
        return sorted(rows, key=lambda t: -t.value)
    return sorted(rows, key=lambda t: (-(t.missing_pct or 0.0), -t.price))


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------

def print_report(targets: Sequence[BoeTarget], top_n: int = 30,
                 sort_key: str = "falta", with_price: bool = False) -> None:
    falta = [t for t in targets if t.status == STATUS_FALTA]
    comum = [t for t in targets if t.status == STATUS_COMUM]
    inobt = [t for t in targets if t.status == STATUS_INOBTENIVEL]
    fora = [t for t in targets if t.status == STATUS_FORA_CATALOGO]
    sem = [t for t in targets if t.status == STATUS_SEM]
    print(f"\n=== BoE retail vs coleccionadores ===")
    print(f"itens BoE retail: {len(targets)} | falta: {len(falta)} "
          f"({100.0 * len(falta) / len(targets):.1f}%) | comum: {len(comum)} | "
          f"inobtenivel: {len(inobt)} | fora do catalogo: {len(fora)} | "
          f"sem aparencia: {len(sem)}")
    if falta:
        com_procura = [t for t in falta if t.missing_pct is not None]
        strong = [t for t in com_procura if (t.missing_pct or 0) >= 50.0]
        print(f"   destes, {len(strong)} faltam a >= 50% da amostra")
        priced = [t for t in falta if t.price > 0]
        print(f"   com preco TSM: {len(priced)} | com volume (spd>0): "
              f"{sum(1 for t in priced if t.sold_per_day > 0)}")
    print("   ('comum' = aparencia obtivel e ja todos a tem; 'inobtenivel' = a")
    print("    fonte morreu no catalogo do DfA -- o BoE em si ainda vende, mas a")
    print("    amostra nunca a pode 'faltar'; 'sem_aparencia' = receita/anel/trinket)")

    print(f"\n-- TOP {top_n} EM FALTA (ordem: {sort_key}) --")
    print("   falta%  own%   preco      sr    spd  item                              "
          "aparencias  ilvl/exp" + ("  (com preco)" if with_price else ""))
    shown = 0
    for t in sort_targets([t for t in falta if not with_price or t.price > 0],
                          sort_key):
        miss = f"{t.missing_pct:6.1f}" if t.missing_pct is not None else "     ?"
        own = f"{t.owned_pct:5.2f}" if t.owned_pct is not None else "    ?"
        price = _g(t.price) if t.price > 0 else "-"
        print(f"  {miss}% {own}% {price:>9s} {t.sale_rate:6.2%} "
              f"{t.sold_per_day:5.1f}  {t.name[:32]:32s} "
              f"{t.missing_appearances:>3}/{t.appearances:<3} "
              f"{t.item_level:>4}/{t.expansion}")
        shown += 1
        if shown >= top_n:
            break
    if not shown:
        print("  (nada: sem preco ou sem procura)")


def print_missing_items(targets: Sequence[BoeTarget], top_n: int = 40,
                        only_priced: bool = False, sort_key: str = "valor") -> None:
    """One row per BoE ITEM the sample does not own (no appearance columns)."""
    rows = [t for t in targets if t.status == STATUS_FALTA and t.missing_count > 0]
    if only_priced:
        rows = [t for t in rows if t.price > 0]
    sample = max((t.collectors for t in targets), default=0)
    print(f"\n=== BoE que a amostra ainda NAO tem ===")
    escopo = "BoE retail com preco" if only_priced else "BoE retail"
    print(f"{len(rows)} de {len(targets)} {escopo} | amostra: {sample} contas "
          f"(DfA: a conta nao trouxe a aparencia na resposta)")
    order = {"valor": lambda t: -t.value,
             "falta": lambda t: (-t.missing_count, -t.price),
             "preco": lambda t: -t.price}.get(sort_key, lambda t: -t.value)
    print("\n   sem/amostra    %      preco      sr    spd  item"
          "                                  ilvl/exp")
    for t in sorted(rows, key=order)[:top_n]:
        pct = (100.0 * t.missing_count / sample) if sample else 0.0
        price = _g(t.price) if t.price > 0 else "-"
        print(f"{t.missing_count:>5}/{sample:<4} {pct:5.1f}%  {price:>9s} "
              f"{t.sale_rate:6.2%} {t.sold_per_day:5.1f}  {t.name[:44]:44s} "
              f"{t.item_level:>4}/{t.expansion}")
    if not rows:
        print("  (nada: nenhum BoE em falta com esse filtro)")


CSV_COLUMNS = (
    "item_id", "name", "status", "appearances", "missing_appearances",
    "missing_accounts", "collectors", "missing_pct", "owned_pct",
    "demand_value", "price_gold", "sale_rate", "sold_per_day", "quality",
    "expansion", "item_level", "required_level",
)


def write_csv(targets: Sequence[BoeTarget], path: str = DEFAULT_OUT,
              only_falta: bool = False, only_priced: bool = False,
              sort_key: str = "falta") -> Path:
    """The table.  `only_falta` keeps the same rows `--faltam` prints."""
    rows = list(targets)
    if only_falta:
        rows = [t for t in rows if t.status == STATUS_FALTA and t.missing_count > 0]
    if only_priced:
        rows = [t for t in rows if t.price > 0]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        w.writeheader()
        for t in sort_targets(rows, sort_key):
            w.writerow({
                "item_id": t.item_id, "name": t.name, "status": t.status,
                "appearances": t.appearances,
                "missing_appearances": t.missing_appearances,
                "missing_accounts": t.missing_count,
                "collectors": t.collectors,
                "missing_pct": "" if t.missing_pct is None else f"{t.missing_pct:.2f}",
                "owned_pct": "" if t.owned_pct is None else f"{t.owned_pct:.2f}",
                "demand_value": "" if t.demand_value is None else f"{t.demand_value:.2f}",
                "price_gold": round(t.price / 1e4, 2),
                "sale_rate": t.sale_rate, "sold_per_day": t.sold_per_day,
                "quality": t.quality, "expansion": t.expansion,
                "item_level": t.item_level,
                "required_level": t.required_level,
            })
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dfa-dir", default="",
                    help="pasta com demand_appearances.csv (default: $DFA_DIR)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--sort", choices=("falta", "preco", "valor"), default="falta")
    ap.add_argument("--only-priced", action="store_true",
                    help="so' linhas com avgSalePrice no TSM")
    ap.add_argument("--faltam", action="store_true",
                    help="tabela por ITEM: quantas contas da amostra ainda o nao"
                         " tem (sem colunas de aparencia)")
    ap.add_argument("--csv", default=DEFAULT_OUT, help="tabela completa")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    dfa_dir = Path(args.dfa_dir) if args.dfa_dir else default_dfa_dir()
    if dfa_dir is None or not dfa_dir.exists():
        print("pasta do Data for Azeroth nao encontrada: passa --dfa-dir "
              "(ou define DFA_DIR)")
        return 2
    targets = build_targets(args.db, dfa_dir)
    if args.faltam:
        print_missing_items(targets, args.top, args.only_priced, args.sort)
    else:
        print_report(targets, args.top, args.sort, with_price=args.only_priced)
    if not args.no_csv:
        path = args.csv
        if args.faltam and path == DEFAULT_OUT:
            path = DEFAULT_FALTAM_OUT
        written = write_csv(targets, path, only_falta=args.faltam,
                            only_priced=args.only_priced, sort_key=args.sort)
        print(f"\ntabela -> {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
