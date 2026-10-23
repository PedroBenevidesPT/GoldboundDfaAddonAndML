"""Binding authority — is this item BoE, BoP, or sellable at all?

Why this exists
---------------
Everything downstream hangs on one bit per item: can it reach an auction
house?  The farm EV, the collector report and the BoE targets all read it.
There is now exactly one source that answers it for the whole game -- the
client DB2 (``db2_items``, see ``scripts/import_db2.py``) -- plus the Blizzard
API for the few hundred ids that were enriched.  This script is where those two
are confronted, counted and exported, so the answer is auditable instead of
being buried inside a filter:

    python scripts/item_binding.py                  # auditoria + CSV em reports/
    python scripts/item_binding.py 25 229155        # consulta pontual (id)
    python scripts/item_binding.py "Worn Shortsword"  # consulta pontual (nome)

What the report separates
-------------------------
    vendavel    binding 0 (sem binding), 2 (BoE) ou 3 (bind on use) e era 0
    ligado      1 (BoP), 4 (quest), 7-9 (conta/warband): nunca chega a AH
    era-Classic a linha e' do mesmo espaco de ids mas de outro jogo
    sem dado    id que nao existe no DB2 nem na API (nao se adivinha)

E duas coisas que o relatorio NAO esconde:

1. divergencias entre o DB2 e a API da Blizzard (mesmo id, binding diferente);
2. "contradicoes": item que o cliente diz BoP mas que tem preco/volume no TSM.
   Sao itens modernos (TWW/DF) -- a hipotese e' warbound/atualizacao de
   binding, nao uma certeza -- e ficam FORA do EV (conservador) mas listados,
   porque um BoP com 190k de preco e' precisamente o que nao pode passar em
   silencio.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from woweconomy.analytics.farm_ranking import (  # noqa: E402
    ItemMarket,
    load_latest_market,
)
from woweconomy.services.blizzard_items import load_map as load_api_map  # noqa: E402
from woweconomy.services.db2_items import (  # noqa: E402
    BINDING_NAMES,
    QUALITY_NAMES,
    Db2Item,
    load_map as load_db2_map,
)

DEFAULT_DB = "tsm_markets.db"
DEFAULT_OUT = "reports"

# Binding values that can be sold on an auction house.  3 = binds when used:
# still sellable before you use it (potions, enchants, recipes).
SELLABLE = frozenset({0, 2, 3})
BOUND = frozenset({1, 4, 7, 8, 9})

# ExpansionID -> name.  Our reading of the enum, not an official table: it is
# only here to make the report readable, no rule depends on it.
EXPANSIONS = {
    0: "vanilla", 1: "tbc", 2: "wotlk", 3: "cata", 4: "mop", 5: "wod",
    6: "legion", 7: "bfa", 8: "sl", 9: "df", 10: "tww", 11: "midnight",
}


def expansion_name(expansion: Optional[int]) -> str:
    return EXPANSIONS.get(int(expansion or 0), f"exp{expansion}")


def verdict(binding: Optional[int]) -> Tuple[str, bool]:
    """(readable label, can reach the AH?) for a DB2 Bonding value."""
    if binding is None:
        return "sem dado", False
    if binding == 0:
        return "negociavel (sem binding)", True
    if binding == 2:
        return "BoE (bind on equip)", True
    if binding == 3:
        return "bind on use", True
    if binding == 1:
        return "BoP (bind on pickup)", False
    if binding == 4:
        return "item de quest", False
    if binding in (7, 8, 9):
        return "conta/warband", False
    return f"binding {binding}", False


def _binding_name(binding: Optional[int]) -> str:
    if binding is None:
        return ""
    return BINDING_NAMES.get(binding, f"BIND_{binding}")


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------

@dataclass
class Audit:
    total: int = 0
    by_binding: Counter = field(default_factory=Counter)
    by_era: Counter = field(default_factory=Counter)
    boe_by_expansion: Counter = field(default_factory=Counter)
    boe_by_quality: Counter = field(default_factory=Counter)
    api_overlap: int = 0
    conflicts: List[Dict] = field(default_factory=list)     # DB2 vs API binding
    contradictions: List[Dict] = field(default_factory=list)  # BoP with a price
    unknown: List[Dict] = field(default_factory=list)      # in TSM, not in DB2

    @property
    def sellable_retail(self) -> int:
        return sum(self.sellable_breakdown().values())

    def sellable_breakdown(self) -> Dict[int, int]:
        return {b: self.by_era[(b, 0)] for b in sorted(SELLABLE)}


def _market_rows(db_path: str) -> Dict[int, ItemMarket]:
    return load_latest_market(db_path)


def _tsm_ids(db_path: str) -> Dict[int, str]:
    con = sqlite3.connect(db_path)
    try:
        return {int(i): n or "" for i, n in con.execute("SELECT itemId, name FROM items")}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def build_audit(db_path: str = DEFAULT_DB) -> Audit:
    """Read the DB, compare the two binding sources and collect the anomalies."""
    db2 = load_db2_map(db_path)
    api = load_api_map(db_path)
    market = _market_rows(db_path)
    audit = Audit(total=len(db2))
    for item in db2.values():
        audit.by_binding[item.binding] += 1
        audit.by_era[(item.binding, 0 if not item.era else 1)] += 1
        if item.binding == 2 and not item.era:
            audit.boe_by_expansion[item.expansion] += 1
            audit.boe_by_quality[item.quality] += 1

    for item_id, api_item in api.items():
        d = db2.get(item_id)
        if d is None:
            continue
        audit.api_overlap += 1
        ours = d.binding_name if d.binding is not None else ""
        theirs = (api_item.binding or "").upper()
        if ours != theirs:
            audit.conflicts.append({
                "item_id": item_id,
                "name": d.name or api_item.name,
                "db2": _binding_name(d.binding) or "(vazio)",
                "blizzard": theirs or "(vazio)",
            })

    for item_id, d in db2.items():
        m = market.get(item_id)
        if d.binding == 1 and not d.era and m is not None and m.price > 0:
            audit.contradictions.append({
                "item_id": item_id, "name": d.name,
                "price": m.price, "sale_rate": m.sale_rate,
                "sold_per_day": m.sold_per_day,
                "expansion": d.expansion, "item_level": d.item_level,
                "required_level": d.required_level,
            })
    audit.contradictions.sort(key=lambda r: -r["price"])

    for item_id, name in _tsm_ids(db_path).items():
        if item_id in db2 or item_id in api:
            continue
        m = market.get(item_id)
        audit.unknown.append({
            "item_id": item_id,
            # items.name is empty for a few of these; the snapshot still has it
            "name": name or (m.name if m is not None else ""),
            "price": m.price if m is not None else 0.0,
        })
    audit.unknown.sort(key=lambda r: -r["price"])
    return audit


def print_audit(audit: Audit, db_path: str = DEFAULT_DB) -> None:
    print(f"\n== db2_items {audit.total} linhas ({db_path}) ==")
    print("binding:")
    for binding, n in audit.by_binding.most_common():
        label, sellable = verdict(binding)
        pct = 100.0 * n / audit.total if audit.total else 0.0
        print(f"   {label:<26} {_binding_name(binding) or '-':<22} "
              f"{n:>7} {pct:5.1f}%  {'vende' if sellable else 'nao vende'}")

    print("\nera (0 = retail; != 0 = linha de era-Classic):")
    retail = sum(n for (_b, flag), n in audit.by_era.items() if flag == 0)
    era = audit.total - retail
    print(f"   retail {retail:>7} | era-Classic {era:>7}")

    print(f"\n== vendavel na AH (era 0, binding 0/2/3): {audit.sellable_retail} ==")
    for binding, n in audit.sellable_breakdown().items():
        if n:
            print(f"   {verdict(binding)[0]:<26} {n:>7}")

    if audit.boe_by_expansion:
        print("\nBoE retail por expansao (leitura do ExpansionID):")
        for exp, n in audit.boe_by_expansion.most_common():
            print(f"   {expansion_name(exp):<10} {n:>6}")
        print("   qualidade:", ", ".join(
            f"{QUALITY_NAMES.get(q, q)}={n}"
            for q, n in sorted(audit.boe_by_quality.items())))

    print(f"\n== cruzamento DB2 vs API da Blizzard ({audit.api_overlap} ids) ==")
    print(f"   concordam {audit.api_overlap - len(audit.conflicts)}"
          f" | divergem {len(audit.conflicts)}")
    for c in audit.conflicts:
        print(f"   [{c['item_id']}] {c['name'][:36]:36s} "
              f"DB2={c['db2']:<14} API={c['blizzard']}")

    print(f"\n== contradicoes: BoP retail com preco TSM ({len(audit.contradictions)}) ==")
    with_volume = sum(1 for r in audit.contradictions if r["sold_per_day"] > 0)
    if audit.contradictions:
        by_exp = Counter(expansion_name(r["expansion"]) for r in audit.contradictions)
        print(f"   {with_volume} com volume registado (soldPerDay > 0); "
              f"expansao: {dict(by_exp.most_common())}")
        print("   ficam FORA do EV (conservador) -- lista completa em CSV")
        for r in audit.contradictions[:5]:
            print(f"   [{r['item_id']}] {r['name'][:38]:38s} "
                  f"{r['price'] / 1e4:>9,.0f}g spd={r['sold_per_day']:.2f} "
                  f"({expansion_name(r['expansion'])}, ilvl {r['item_level']})")

    print("\n== procura dos BoE ==")
    print("   quais tem aparencia em falta: "
          "python -m woweconomy.analytics.boe_targets")

    print(f"\n== sem dado nenhum ({len(audit.unknown)} ids do TSM) ==")
    priced = sum(1 for r in audit.unknown if r["price"] > 0)
    print(f"   {priced} com preco -- nao se classificam: nao estao no DB2 nem na API")
    for r in audit.unknown[:5]:
        print(f"   [{r['item_id']}] {r['name'][:40]:40s} {r['price'] / 1e4:>9,.0f}g")


def write_reports(audit: Audit, db2, out_dir: str = DEFAULT_OUT) -> List[Path]:
    """The four CSVs that make the audit checkable outside this tool."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    boe = out / "boe_retail.csv"
    with boe.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "name", "quality", "expansion", "item_level",
                    "required_level"])
        for item in sorted(db2.values(), key=lambda i: i.item_id):
            if item.binding == 2 and not item.era:
                w.writerow([item.item_id, item.name,
                            QUALITY_NAMES.get(item.quality, item.quality),
                            expansion_name(item.expansion), item.item_level,
                            item.required_level])
    written.append(boe)

    priced = out / "bop_priced.csv"
    with priced.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "item_id", "name", "price_gold", "sale_rate", "sold_per_day",
            "expansion", "item_level", "required_level"])
        w.writeheader()
        for r in audit.contradictions:
            row = dict(r)
            # TSM money is copper; the CSV says so in the column name.
            row["price_gold"] = round(r["price"] / 1e4, 2)
            del row["price"]
            w.writerow(row)
    written.append(priced)

    conflicts = out / "binding_conflicts.csv"
    with conflicts.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["item_id", "name", "db2", "blizzard"])
        w.writeheader()
        w.writerows(audit.conflicts)
    written.append(conflicts)

    unknown = out / "binding_unknown.csv"
    with unknown.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["item_id", "name", "price_gold"])
        w.writeheader()
        for r in audit.unknown:
            w.writerow({"item_id": r["item_id"], "name": r["name"],
                        "price_gold": round(r["price"] / 1e4, 2)})
    written.append(unknown)
    return written


# ----------------------------------------------------------------------
# One item, asked by id or name
# ----------------------------------------------------------------------

def _find(queries: Sequence[str], db_path: str) -> Iterable[Tuple[str, Optional[Db2Item], str]]:
    """Yield (query, item or None, display name) for ids and names."""
    db2 = load_db2_map(db_path)
    api = load_api_map(db_path)
    by_name: Dict[str, Db2Item] = {}
    for item in db2.values():
        if item.name:
            by_name.setdefault(item.name.casefold(), item)
    for query in queries:
        q = (query or "").strip()
        if not q:
            continue
        if q.isdigit():
            item = db2.get(int(q))
            name = item.name if item else (api.get(int(q)).name if int(q) in api else "")
            yield q, item, name
        else:
            item = by_name.get(q.casefold())
            yield q, item, item.name if item else q


def lookup(queries: Sequence[str], db_path: str = DEFAULT_DB) -> int:
    """Print the binding verdict for each query; 1 when nothing was found."""
    market = _market_rows(db_path)
    api = load_api_map(db_path)
    found = 0
    for query, item, name in _find(queries, db_path):
        if item is None:
            print(f"\n=== {query} ===")
            print("  nao esta no DB2 "
                  + ("nem na API." if not name else f"(a API diz '{name}')"))
            print("  sem binding conhecido: nao se adivinha; ver "
                  "reports/binding_unknown.csv")
            continue
        found += 1
        label, sellable = verdict(item.binding)
        m = market.get(item.item_id)
        print(f"\n=== {item.name} ===")
        print(f"id        : {item.item_id}")
        print(f"binding   : {label} -- DB2 Bonding={item.binding} "
              f"({_binding_name(item.binding) or 'vazio'})")
        print(f"era       : {'retail' if not item.era else f'era-Classic (era={item.era})'}")
        print(f"vende AH  : {'SIM' if sellable and not item.era else 'NAO'}"
              + ("" if sellable else "  (nunca chega a AH)")
              + ("  (linha de outro jogo)" if item.era else ""))
        print(f"qualidade : {QUALITY_NAMES.get(item.quality, item.quality)} | "
              f"{expansion_name(item.expansion)} | ilvl {item.item_level} | "
              f"req {item.required_level}")
        if item.binding == 2 and not item.era:
            # The other half of the answer: a BoE is only a target if its look
            # is still missing.  Same join as `woweconomy.analytics.boe_targets`.
            from woweconomy.analytics.boe_targets import lookup_demand
            n_apps, best = lookup_demand(item.item_id, db_path)
            if not n_apps:
                print("transmog  : sem aparencia (receita/anel/trinket)")
            elif best is None:
                print(f"transmog  : {n_apps} aparencia(s), nenhuma em falta na amostra")
            else:
                own = (f"{best.owned_pct:.2f}% dos perfis"
                       if best.owned_pct is not None else "owned n/d")
                print(f"transmog  : {n_apps} aparencia(s) | falta a "
                      f"{best.missing_pct:.1f}% da amostra ({own})")
        a = api.get(item.item_id)
        if a is not None and (a.binding or "").upper() != (item.binding_name or ""):
            print(f"API       : {a.binding or '(vazio)'} "
                  f"<-- divergencia, ver reports/binding_conflicts.csv")
        if m is not None and m.price > 0:
            print(f"mercado   : {m.price / 1e4:,.0f}g  sr={m.sale_rate:.1%}  "
                  f"spd={m.sold_per_day:.2f}/dia")
            if item.binding == 1 and not item.era:
                print("            ATENCAO: BoP com preco no TSM -- contradicao, "
                      "fica fora do EV (reports/bop_priced.csv)")
    return 0 if found else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("queries", nargs="*",
                    help="ids ou nomes a consultar (sem isto: auditoria completa)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out-dir", default=DEFAULT_OUT,
                    help="onde escrever os CSV da auditoria")
    ap.add_argument("--no-csv", action="store_true",
                    help="auditoria so para o ecra, sem escrever CSV")
    args = ap.parse_args(argv)

    if args.queries:
        return lookup(args.queries, args.db)

    audit = build_audit(args.db)
    print_audit(audit, args.db)
    if not args.no_csv:
        db2 = load_db2_map(args.db)
        for path in write_reports(audit, db2, args.out_dir):
            print(f"relatorio -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
