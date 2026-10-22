"""Calibra o peso de cada família de fontes contra as vendas reais da conta.

    python scripts/pricing_calibration.py --write

A calibração é a resposta à pergunta "quanto vale cada fonte?": para cada item
que a conta vendeu, compara-se cada família de fontes com o **preço realmente
realizado** e mede-se o erro relativo.  O peso de cada família sai dai — não de
uma constante escolhida à mão.

Limites, ditos à frente:
* as vendas **não têm data** (o `journal_stats` é um agregado e o arquivo
  bruto do Journalator é comprimido e cobre só os últimos dias), logo a
  comparação é **contemporânea**: os preditores são os de hoje e as vendas são
  as do período.  O mercado mudou entretanto — é uma limitação declarada, não
  um esquecimento;
* a amostra é a da conta (centenas de vendas, sobretudo material), o que dá
  peso aos sinais de preço de itens comuns e menos aos de transmog raro.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pricing_groups as pg_script  # noqa: E402
from woweconomy.analytics import pricing_engine as pe  # noqa: E402
from woweconomy.analytics.mog_realm import resolve as resolve_realm  # noqa: E402
from woweconomy.analytics.token_rush_data import realm_groups  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
OUT_DIR = ROOT / "reports" / "pricing_engine"
CALIB_JSON = OUT_DIR / "calibration.json"
CALIB_MD = OUT_DIR / "calibration.md"


def family_values(con, scope, canonical, items):
    """One value per family, per item, exactly as the engine reduces them."""
    region = {}
    for item, market, historical, avg_sale in con.execute(
            "SELECT key_id, market_value, historical, avg_sale_price FROM tsm_region_price "
            "WHERE snapshot_id=(select max(snapshot_id) from tsm_region_price)"):
        region[int(item)] = (market, historical, avg_sale)
    local = {}
    for item, market, wall, recent, historical in con.execute(
            "SELECT item_id, market_value, min_buyout, recent, historical "
            "FROM tsm_realm_price WHERE scope=?", (scope,)):
        local[int(item)] = (market, wall, recent, historical)
    out = {}
    for item_id in items:
        lm, lw, lr, lh = local.get(item_id, (0, 0, 0, 0))
        rm, rh, ra = region.get(item_id, (0, 0, 0))
        families = {}
        for name, value, family in (
                ("dbmarket", lm, pe.FAMILY_LOCAL_ASK),
                ("dbrecent", lr, pe.FAMILY_LOCAL_ASK),
                ("dbhistorical", lh, pe.FAMILY_LOCAL_ASK),
                ("dbminbuyout", lw, pe.FAMILY_LOCAL_ASK),
                ("dbregionmarketavg", rm, pe.FAMILY_REGIONAL_ASK),
                ("dbregionhistorical", rh, pe.FAMILY_REGIONAL_ASK),
                ("dbregionsaleavg", ra, pe.FAMILY_REGIONAL_SALES),
        ):
            if value and value < pe.SENTINEL_COPPER:
                families.setdefault(family, []).append(int(value))
        out[item_id] = {family: int(statistics.median(values))
                        for family, values in families.items()}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--realm", default="Dragonmaw")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    profile = resolve_realm(args.realm)
    canonical = profile.realm
    realm_rows = realm_groups(con)
    scope = (realm_rows.get(canonical) or {}).get("canonical_slug") or canonical

    # the account's realized sales: the only real prices we have
    sales = defaultdict(list)
    for item, realm, sold, copper in con.execute(
            "SELECT itemId, realm, sold_count, sold_copper FROM journal_stats "
            "WHERE sold_count>0"):
        sales[int(item)].append((realm, int(copper) / int(sold)))
    items = set(sales)
    values = family_values(con, scope, canonical, items)

    # the families as the engine reduces them, against the realized price
    errors = defaultdict(list)
    ratios = defaultdict(list)
    coverage = defaultdict(int)
    items_covered = defaultdict(set)
    for item_id, pairs in sales.items():
        families = values.get(item_id, {})
        for realm, realized in pairs:
            for family, value in families.items():
                if not value or not realized:
                    continue
                errors[family].append(abs(math.log(value / realized)))
                ratios[family].append(value / realized)
                coverage[family] += 1
                items_covered[family].add(item_id)

    sample = sum(len(v) for v in errors.values()) or 0
    n_items = len(items)
    family_error = {f: statistics.median(v) for f, v in errors.items() if v}
    family_ratio = {f: statistics.median(v) for f, v in ratios.items() if v}
    # The weight: the smaller the measured error, the more the family counts;
    # a family that was never available gets nothing.  The scale is the same as
    # the engine's priors (1.0 = the best possible), so the two can be mixed.
    weights = {}
    for family, error in family_error.items():
        weights[family] = round(max(0.0, 1.0 / (1.0 + error))
                            * min(1.0, coverage[family] / 20.0), 4)

    calibration = pe.Calibration(
        family_weight=weights,
        family_error={f: round(v, 4) for f, v in family_error.items()},
        coverage={f: round(len(items_covered[f]) / max(1, n_items), 3) for f in coverage},
        measured_at=con.execute("select max(fetched_at) from tsm_realm_price").fetchone()[0],
        sample=n_items,
        source="journal_stats (vendas da conta) vs fontes TSM de hoje",
    )

    print(f"itens com venda: {n_items} | observações por família: {sample}")
    for family in sorted(family_error):
        print(f"  {family:20s} n={coverage[family]:5d} erro mediano "
              f"{family_error[family]:.3f} (log) | razão mediana "
              f"{family_ratio[family]:.3f} | peso {weights.get(family, 0):.3f}")

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        CALIB_JSON.write_text(json.dumps(calibration.as_dict(), indent=2,
                                         ensure_ascii=False), encoding="utf-8")
        lines = [
            "# Calibração das fontes de preço",
            "",
            "O peso de cada família de fontes **não** é escolhido: é medido contra "
            "as vendas reais da conta.",
            "",
            f"- Amostra: {n_items} itens com venda, {sample} observações por família.",
            f"- Medido em: {calibration.measured_at}.",
            f"- Fonte: {calibration.source}.",
            "",
            "> **Limitação declarada:** as vendas não têm data, logo a comparação é "
            "contemporânea (os preditores são os de hoje e as vendas são as do "
            "período). É uma limitação do dado, não do método.",
            "",
            "| família | observações | cobertura (itens) | erro mediano (log) | razão mediana "
            "(previsão/venda) | peso |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for family in sorted(family_error):
            lines.append(f"| {family} | {coverage[family]:,} | "
                         f"{calibration.coverage.get(family, 0):.0%} | "
                         f"{family_error[family]:.3f} | {family_ratio[family]:.3f} | "
                         f"{weights.get(family, 0):.3f} |")
        lines += [
            "",
            "## Como ler",
            "",
            "- O **erro mediano** é a mediana de |log(previsão / venda)|: quanto mais "
            "perto de zero, melhor a família prevê o preço realizado.",
            "- A **razão mediana** mostra o viés: um valor de 3 significa que a "
            "f systematicallyamente está 3× acima do preço realizado (é o caso "
            "das referências de ask, quando o mercado é inflado).",
            "- O **peso** é 1/(1+erro) escalado pela cobertura: uma família que "
            "nunca esteve disponível não recebe peso por ser teoricamente a melhor. "
            "A escala é a mesma dos priors do motor (1,0 = o melhor possível), "
            "para as duas poderem ser misturadas.",
            "",
        ]
        CALIB_MD.write_text("\n".join(lines), encoding="utf-8")
        print(f"-> {CALIB_JSON}\n-> {CALIB_MD}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
