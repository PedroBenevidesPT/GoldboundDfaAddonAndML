"""Per-realm market profile for the mog catalog.

Reads three sources:

    tsm_realm_price   price vector of every realm the TSM API knows (92 realms,
                      one row per item per fetch, whole catalogue)
    tsm_market        live auction count per item -- only the 12 realms the TSM
                      app downloaded
    tsm_region_price  the regional reference (denominator of the price ratio)
    tsm_realm_group   which realms share one auction house

and writes

    reports/mog_groups/realm_profiles.csv   one row per connected realm
    reports/mog_groups/realm_aliases.csv    realm name/slug -> canonical realm
    reports/mog_groups_realm_profiles.md    the readable profile

The profile has two independent parts: the **measurement** (price level, depth,
wall, solo share, coverage) and the **policy** (posture, duration, holding
window), which lives in ``woweconomy/analytics/mog_realm.py`` so that the export
and this report cannot disagree.

Usage:
    python scripts/mog_realm_profiles.py            # writes the three files
    python scripts/mog_realm_profiles.py --show     # only prints the table
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.mog_realm import (  # noqa: E402
    CLASS_LABEL, CLASS_POLICY, MAX_FACTOR, MIN_FACTOR,
)

DB = ROOT / "tsm_markets.db"
CATALOG = ROOT / "reports" / "mog_groups" / "mog_groups.csv"
CSV_OUT = ROOT / "reports" / "mog_groups" / "realm_profiles.csv"
ALIAS_OUT = ROOT / "reports" / "mog_groups" / "realm_aliases.csv"
MD_OUT = ROOT / "reports" / "mog_groups_realm_profiles.md"

# --- classification ------------------------------------------------------
# Competition depth is the median number of live auctions per listed catalogue
# item.  Thin: most items have at most one competing listing (you set the
# price).  Deep: three or more, or a wall (>=3 auctions) on most items.
THIN_DEPTH = 1.5
DEEP_DEPTH = 3.5
DEEP_WALL_SHARE = 0.60
# When only the price ratio is known, it is a proxy: on the eight realms where
# both are measured the rule below agrees with the depth on seven (Grim Batol,
# 1.04x with two listings per item, is the exception).
THIN_PRICE_RATIO = 0.95
DEEP_PRICE_RATIO = 0.45

MIN_SAMPLE = 500


def classify(depth: float | None, wall_share: float | None, ratio: float | None) -> str:
    if depth is not None:
        if depth < THIN_DEPTH:
            return "thin"
        if depth >= DEEP_DEPTH or (wall_share or 0) >= DEEP_WALL_SHARE:
            return "deep"
        return "balanced"
    if ratio is None:
        return "balanced"
    if ratio >= THIN_PRICE_RATIO:
        return "thin"
    if ratio < DEEP_PRICE_RATIO:
        return "deep"
    return "balanced"


def _median(values):
    return statistics.median(values) if values else None


def _quantile(values, q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def load_catalog() -> set[int]:
    with CATALOG.open(encoding="utf-8") as fh:
        return {int(row["item_id"]) for row in csv.DictReader(fh)}


def load_region(con) -> dict[int, float]:
    latest = con.execute("select max(snapshot_id) from tsm_region_price").fetchone()[0]
    out = {}
    for item, value in con.execute(
        "select key_id, market_value from tsm_region_price where snapshot_id=?", (latest,)
    ):
        if value and value > 0:
            out[int(item)] = float(value)
    return out


def load_groups(con):
    """Return (rows, by_name) with one entry per realm, canonical included."""
    rows = []
    for slug, name, group_id, cslug, cname, is_canonical in con.execute(
        "select realm_slug, realm_name, group_id, canonical_slug, canonical_name, is_canonical "
        "from tsm_realm_group"
    ):
        rows.append({
            "slug": slug, "name": name, "group_id": int(group_id),
            "canonical_slug": cslug, "canonical": cname, "canonical_flag": bool(is_canonical),
        })
    by_name = {}
    for row in rows:
        by_name[row["name"]] = row
        by_name[row["slug"]] = row
    return rows, by_name


def load_price_vector(con, scope: str) -> dict[int, float]:
    """Latest known market value per item for one realm."""
    out = {}
    for item, value in con.execute(
        """select item_id, market_value from (
             select item_id, market_value,
                    row_number() over (
                      partition by item_id order by fetched_at desc, snapshot_id desc
                    ) rn
             from tsm_realm_price where scope=?
           ) where rn=1""",
        (scope,),
    ):
        if value and value > 0:
            out[int(item)] = float(value)
    return out


def load_depth(con, catalog: set[int]):
    """num_auctions per item per connected group, deduplicated across realms."""
    out = defaultdict(dict)
    for realm, item, na in con.execute("select realm, itemId, num_auctions from tsm_market"):
        item = int(item)
        if item not in catalog:
            continue
        out[realm][item] = max(out[realm].get(item, 0), int(na or 0))
    return out


def load_sales(con) -> dict[str, tuple[int, int]]:
    out = {}
    for realm, items, sold, copper in con.execute(
        "select realm, count(*), coalesce(sum(sold_count), 0), coalesce(sum(sold_copper), 0) "
        "from journal_stats group by 1"
    ):
        out[realm] = (int(items), int(sold), int(copper))
    return out


def build_profiles(con) -> tuple[list[dict], dict[str, list[str]]]:
    catalog = load_catalog()
    region = load_region(con)
    groups, by_name = load_groups(con)
    depth_by_realm = load_depth(con, catalog)
    sales = load_sales(con)

    by_group: dict[int, list[dict]] = defaultdict(list)
    for row in groups:
        by_group[row["group_id"]].append(row)

    profiles = []
    aliases: dict[str, list[str]] = defaultdict(list)
    for group_id, members in by_group.items():
        canonical = next((m for m in members if m["canonical_flag"]), members[0])
        aliases[canonical["name"]] = [m["name"] for m in members] + [m["slug"] for m in members]

        scope = canonical["slug"]
        prices = load_price_vector(con, scope)
        if not prices:
            # some groups may be filed under another member's slug
            for member in members:
                prices = load_price_vector(con, member["slug"])
                if prices:
                    scope = member["slug"]
                    break

        ratios = []
        for item, value in prices.items():
            if item not in catalog:
                continue
            ref = region.get(item)
            if ref:
                ratios.append(value / ref)
        ratio = _median(ratios)

        depth_rows = None
        for member in members:
            if member["name"] in depth_by_realm:
                depth_rows = depth_by_realm[member["name"]]
                break
        # connected realms share one auction house; the app may report the same
        # vector under several names, so only one member's counts are used
        listed_basis = len(depth_rows) if depth_rows is not None else 0
        listed = sum(1 for na in (depth_rows or {}).values() if na >= 1)
        depth = _median([float(na) for na in (depth_rows or {}).values() if na >= 1]) if listed else None
        wall = sum(1 for na in (depth_rows or {}).values() if na >= 3) / listed if listed else None
        solo = sum(1 for na in (depth_rows or {}).values() if na == 1) / listed if listed else None

        klass = classify(depth, wall, ratio)
        factor = round(ratio, 2) if ratio else 1.0
        factor = max(MIN_FACTOR, min(MAX_FACTOR, factor))
        sample = len(ratios)
        if depth is not None:
            confidence = "alta" if sample >= 2000 else "média"
        else:
            confidence = "inferida" if sample >= MIN_SAMPLE else "partilhada"

        sold = (0, 0, 0)
        for member in members:
            hit = sales.get(member["name"]) or sales.get(member["slug"])
            if hit:
                sold = (sold[0] + hit[0], sold[1] + hit[1], sold[2] + hit[2])

        profiles.append({
            "realm": canonical["name"],
            "group_id": group_id,
            "class": klass,
            "factor": factor,
            "items": len(prices),
            "listed_share": round(listed / listed_basis, 3) if listed_basis else "",
            "depth_med": round(depth, 2) if depth is not None else "",
            "wall_share": round(wall, 3) if wall is not None else "",
            "solo_share": round(solo, 3) if solo is not None else "",
            "price_vs_region": round(ratio, 3) if ratio else "",
            "p25": round(_quantile(ratios, 0.25), 3) if ratio else "",
            "p75": round(_quantile(ratios, 0.75), 3) if ratio else "",
            "sample": sample,
            "confidence": confidence,
            # not exported to the CSV; used by the report
            "_sold": sold[1],
            "_copper": sold[2],
            "_scope": scope,
            "_aliases": [m["name"] for m in members],
            "_sales": {m["name"]: sales[m["name"]][1] for m in members if m["name"] in sales},
        })

    profiles.sort(key=lambda p: (float(p["depth_med"] or 99), p["realm"]))
    return profiles, aliases


def print_table(profiles: list[dict]) -> None:
    print(f"{'realm':22s} {'group':>5s} {'class':8s} {'factor':>6s} {'depth':>5s} "
          f"{'wall%':>6s} {'solo%':>6s} {'list%':>6s} {'px/reg':>7s} {'n':>6s} {'conf':>9s} {'sold':>6s}")
    for p in profiles:
        print(f"{p['realm'][:22]:22s} {p['group_id']:5d} {p['class']:8s} {p['factor']:6.2f} "
              f"{str(p['depth_med'] or '-'):>5} "
              f"{(p['wall_share'] * 100 if p['wall_share'] != '' else 0):5.1f}% "
              f"{(p['solo_share'] * 100 if p['solo_share'] != '' else 0):5.1f}% "
              f"{(p['listed_share'] * 100 if p['listed_share'] != '' else 0):5.1f}% "
              f"{str(p['price_vs_region']):>7} {p['sample']:6d} {p['confidence']:>9s} {p['_sold']:6d}")


CSV_FIELDS = ("realm", "group_id", "class", "factor", "items", "listed_share",
              "depth_med", "wall_share", "solo_share", "price_vs_region",
              "p25", "p75", "sample", "confidence")


def write_csv(profiles: list[dict], aliases: dict[str, list[str]]) -> None:
    with CSV_OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(profiles)
    by_realm = {p["realm"]: p for p in profiles}
    with ALIAS_OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("realm", "canonical", "group_id"))
        for canonical, names in sorted(aliases.items()):
            row = by_realm.get(canonical)
            writer.writerow(("|".join(dict.fromkeys(names)), canonical,
                             row["group_id"] if row else ""))


def _pct(value) -> str:
    return f"{value * 100:.1f}%" if value != "" and value is not None else "—"


def slugify(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "realm"


def _hold_table() -> list[str]:
    from woweconomy.analytics.mog_operations import BEHAVIOUR_MIN_DURATION

    rows = ["| classe | liquid | collector / slow / very_slow | unproven / conflict | retenção |",
            "|---|---:|---:|---:|---:|"]
    groups = {
        "liquid": ("liquid",),
        "liquidez curta": ("unproven", "conflict"),
        "cauda longa": ("collector", "slow", "very_slow"),
    }
    for klass, policy in CLASS_POLICY.items():
        hold = int(policy["hold_days"])
        cells = []
        for key in ("liquid", "cauda longa", "liquidez curta"):
            behaviour = groups[key][0]
            duration = max(int(policy["duration"]), BEHAVIOUR_MIN_DURATION[behaviour])
            cycles = max(1, round(hold * 24 / duration) - 1)
            cells.append(f"{duration}h ({cycles})")
        rows.append(f"| {klass} ({CLASS_LABEL[klass]}) | {cells[0]} | {cells[1]} | {cells[2]} | {hold} dias |")
    return rows


def write_markdown(profiles: list[dict], aliases: dict[str, list[str]]) -> None:
    measured = [p for p in profiles if p["confidence"] == "alta"]
    sold = [p for p in profiles if p["_sold"]]
    lines = [
        "# Perfil de mercado por reino",
        "",
        "Gerado por `python scripts/mog_realm_profiles.py`. Este ficheiro é o "
        "**resultado obrigatório por reino** (classificação, indicadores, "
        "estratégia, reatividade, regras, qualidade da evidência e critérios de "
        "revisão). Os números de política vivem em "
        "`woweconomy/analytics/mog_realm.py`; o export lê-os de lá, para o "
        "relatório e o `.tsm` não poderem divergir.",
        "",
        "## O que é observado e em que janela",
        "",
        "| dado | fonte | janela | cobertura |",
        "|---|---|---|---|",
        "| preço local (`market_value`) | `tsm_realm_price` | último valor por item, descarregado a 19/10/2026 | 92 reinos, 13k-18k itens cada |",
        "| referência regional | `tsm_region_price` (último snapshot) | idem | 31 184 itens |",
        "| nº de anúncios por item | `tsm_market` | última descarga AuctionDB (`tsm_meta`) | **12 reinos** |",
        "| vendas realizadas | `journal_stats` | histórico da conta | só os reinos onde vendeste |",
        "",
        "**Não observado:** velocidade de venda por reino, tempo até à venda, "
        "frequência de undercut e reaparecimento de anúncios. Os snapshots de "
        "`tsm_realm_price` guardam apenas as linhas que **mudaram** em cada "
        "descarga (é um log de alterações, não um retrato completo), portanto "
        "não servem para medir turnover; o desaparecimento de um anúncio não é "
        "uma venda confirmada. A classificação abaixo é estrutural (oferta "
        "presente), não uma medida de liquidez.",
        "",
        "## Classificação",
        "",
        f"- **thin** (pouco competitivo): mediana de anúncios por item listado < {THIN_DEPTH}"
        " — a maior parte dos itens tem no máximo um concorrente.",
        f"- **deep** (competitivo): mediana ≥ {DEEP_DEPTH} ou parede (≥3 anúncios) em "
        f"≥ {DEEP_WALL_SHARE:.0%} dos itens listados.",
        "- **balanced** (intermédio): o resto; não é automaticamente líquido nem "
        "pouco competitivo — a estratégia é a da banda, sem prémio nem desconto de reino.",
        "",
        f"Sem contagem de anúncios ({len(profiles) - len(measured)} dos {len(profiles)} reinos) a classe é **inferida** do "
        f"nível de preço: ≥ {THIN_PRICE_RATIO:.0%} da região → thin, < {DEEP_PRICE_RATIO:.0%} → deep. "
        "Nos oito reinos em que ambas são conhecidas a regra de preço acerta em "
        "sete (falha em Grim Batol, 1,04× com 2 anúncios por item).",
        "",
        "## Tabela",
        "",
        "| reino | grupo | classe | factor | itens | % listados | anúncios/item | parede ≥3 | solo (=1) | preço/região | amostra | confiança | vendas |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for p in profiles:
        lines.append(
            f"| {p['realm']} | {p['group_id']} | {p['class']} | {p['factor']:.2f} | "
            f"{p['items']} | {_pct(p['listed_share'])} | {p['depth_med'] if p['depth_med'] != '' else '—'} | "
            f"{_pct(p['wall_share'])} | {_pct(p['solo_share'])} | "
            f"{p['price_vs_region'] if p['price_vs_region'] != '' else '—'} | {p['sample']} | "
            f"{p['confidence']} | {p['_sold'] or '—'} |"
        )

    lines += [
        "",
        "`factor` = mediana de `mercado do reino / mercado regional`, já limitada a "
        f"[{MIN_FACTOR:.2f}, {MAX_FACTOR:.2f}]. É multiplicado por **todas** as bandas "
        "e **todos** os grupos: não há coeficientes reino×banda×grupo (não há "
        "amostra de vendas para os estimar). A dispersão da razão dentro do reino "
        "está em p25-p75:",
        "",
        "| reino | p25 | mediana | p75 |",
        "|---|---:|---:|---:|",
    ]
    for p in profiles:
        if p["price_vs_region"] != "":
            lines.append(f"| {p['realm']} | {p['p25']} | {p['price_vs_region']} | {p['p75']} |")

    lines += [
        "",
        "## Estratégia por classe",
        "",
        "A banda decide **quanto abaixo** do valor de referência se posta; o reino "
        "decide **que valor de referência é esse** (o factor), **quanto se pede** "
        "(postura) e **quanto se espera** (duração e janela).",
        "",
        "| classe | postura | efeito no preço |",
        "|---|---:|---|",
        "| thin | ×1,20 | um anúncio sozinho pode pedir mais; não persegue quem baixe |",
        "| balanced | ×1,00 | só a correção para o nível local |",
        "| deep | ×1,00 | sem desconto: com parede, o preço já é `parede − undercut`; a alavanca é a velocidade |",
        "",
        "Duração e retenção (`maxExpires` = expirações **desde a última venda**):",
        "",
        *_hold_table(),
        "",
        "Regras iguais em todos os reinos: `priceReset = none` (parede abaixo do "
        "mínimo → **não postar**, nunca saltar para o máximo), `aboveMax = maxPrice` "
        "(parede acima do tecto → postar no tecto), sem cancelamento automático, "
        "um anúncio por item.",
        "",
        "## Perfis medidos (com contagem de anúncios)",
        "",
    ]
    for p in measured:
        scale = p["factor"] * CLASS_POLICY[p["class"]]["posture"]
        lines += [
            f"### {p['realm']} (grupo {p['group_id']}) — {p['class']}, {CLASS_LABEL[p['class']]}",
            "",
            f"- **Indicadores** ({p['sample']} itens com preço local e regional): "
            f"mediana de {p['depth_med']} anúncios por item listado; parede em "
            f"{_pct(p['wall_share'])} dos itens listados; um único anúncio em "
            f"{_pct(p['solo_share'])}; {_pct(p['listed_share'])} dos itens com preço "
            f"estão listados; preço local a **{p['price_vs_region']:.3f}×** a região "
            f"(p25 {p['p25']}, p75 {p['p75']}).",
            f"- **Estratégia**: factor **{p['factor']:.2f}** × postura "
            f"**{CLASS_POLICY[p['class']]['posture']:.2f}** = escala **{scale:.2f}**. "
            f"Um `Collector` na banda 500 g-5 k, cujo coeficiente é 0,68, posta a "
            f"**{0.68 * scale:.2f}×V** (V = mercado regional).",
            f"- **Reatividade**: {int(CLASS_POLICY[p['class']]['duration'])} h de base "
            f"(o `Liquid` neste reino posta a "
            f"{max(int(CLASS_POLICY[p['class']]['duration']), 12)} h) e retenção de "
            f"{int(CLASS_POLICY[p['class']]['hold_days'])} dias.",
            f"- **Evidência**: {'medida (nº de anúncios observado)' if p['confidence'] == 'alta' else 'parcial'}"
            f", {p['sample']} itens; a razão varia dentro do reino (p25-p75), por isso "
            "o factor é um compromisso.",
            "",
        ]

    if sold:
        lines += [
            "## Reinos com vendas e ficheiros de import",
            "",
            "O ficheiro de cada reino é o que se importa no perfil TSM desse reino "
            "(o perfil é por personagem; cria-o antes na UI). Reinos sem ficheiro "
            "gerado aparecem como `—`; gera-o com "
            "`python scripts/tsm_groups_export.py --write --realm \"<reino>\"`.",
            "",
            "| reino | perfil medido | classe | factor | vendas | receita | ficheiro |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        for p in sold:
            first = True
            for name, count in sorted(p["_sales"].items(), key=lambda kv: -kv[1]):
                if not count:
                    continue
                path = ROOT / "reports" / "mog_groups" / f"mogs_groups_{slugify(name)}.tsm"
                filename = f"`{path.name}`" if path.exists() else "—"
                revenue = f"{p['_copper'] / 10000:,.0f} g" if first else "(no grupo)"
                lines.append(f"| {name} | {p['realm']} | {p['class']} | {p['factor']:.2f} | "
                             f"{count:,} | {revenue} | {filename} |")
                first = False
        lines.append("")

    lines += [
        "## Limitações",
        "",
        "- O `factor` é uma mediana de todo o catálogo e a razão varia com a banda "
        "(ver p25-p75): em Pozzo vai de ~1,0 acima de 50 k a ~2,5 abaixo de 500 g. "
        "Aplicar um escalar é um compromisso deliberado, não um ajuste fino.",
        "- O nível regional é uma média inflacionada por anúncios mortos de reinos "
        "vazios; é por isso que reinos cheios aparecem a 0,2-0,3× e reinos vazios a "
        "1,0-1,3×. O factor corrige exactamente isso.",
        "- `tsm_market` não tem timestamp; a idade da contagem de anúncios é a da "
        "última descarga de AuctionDB em `tsm_meta`.",
        "- Não há backtesting de lucro por reino: sem série de vendas por reino, "
        "qualquer comparação de lucro realizado seria inventada.",
        "",
        "## Critérios para rever",
        "",
        "1. **Mudança de classe**: a mediana de anúncios por item passa de <1,5 para "
        "≥3,5 (ou o contrário) — refazer o export desse reino.",
        "2. **Desvio do factor**: se a mediana `preço/região` se afastar mais de 20 % "
        "do valor na tabela, recalcular e reimportar.",
        "3. **Cobertura**: se `% listados` ou `parede ≥3` mudar mais de 10 pontos "
        "percentuais, a classe inferida deixa de ser fiável.",
        "4. **Resultado**: após 30 dias, comparar vendas realizadas por reino "
        "(`journal_stats`) com o esperado; se um reino `deep` vender ao mesmo ritmo "
        "que um `thin`, o problema não é o preço — é a selecção de itens.",
        "5. **Idade**: se a última descarga de AuctionDB tiver mais de 7 dias, os "
        "indicadores de concorrência estão velhos; usar só o factor.",
        "",
    ]
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", action="store_true", help="só imprime a tabela")
    args = ap.parse_args(argv)

    if not DB.exists():
        raise SystemExit(f"base de dados em falta: {DB}")
    con = sqlite3.connect(DB)
    profiles, aliases = build_profiles(con)
    if not profiles:
        raise SystemExit("sem dados de tsm_realm_group")
    print_table(profiles)
    if not args.show:
        write_csv(profiles, aliases)
        write_markdown(profiles, aliases)
        print(f"\n-> {CSV_OUT}\n-> {ALIAS_OUT}\n-> {MD_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
