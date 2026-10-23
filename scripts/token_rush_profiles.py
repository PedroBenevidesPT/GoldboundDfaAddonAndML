"""Gera os grupos de Token Rush para o TSM: um export e um perfil por reino.

    python scripts/token_rush_profiles.py --show
    python scripts/token_rush_profiles.py --write
    python scripts/token_rush_profiles.py --write --realms "Dragonmaw,Argent Dawn"

**Apertar o grupo é o mesmo do export mog** (``tsm_groups_export.py
--all-realms``): os mesmos 127 155 itens nos mesmos sete grupos de
comportamento, com o `conflict` no grupo próprio. O que muda é a **política de
preço**: em vez dos coeficientes de banda do modelo normal, cada grupo recebe
o preço recomendado pelo **motor de evidência** (``pricing_engine``), escrito
como uma fórmula **viva**::

    normalPrice = max( ifgt(dbmarket, banda, r × dbmarket, …), 1.5 × vendorsell )

onde ``r`` é a **razão medida** (preço recomendado ÷ mercado local) dos itens
desse grupo e dessa banda. O preço acompanha o mercado local; a calibragem
vem do motor, não de uma constante.

A operação é de **rush**: 12 h de duração, 7 dias de retenção,
``priceReset = none``, ``aboveMax = maxPrice``, sem cancelamento automático.

Cada export é `token_rush_<reino>.tsm`, para importar num perfil TSM
**desse** reino (``mogs-<reino>``). Nada é importado automaticamente.
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
sys.path.insert(0, str(ROOT / "scripts"))

import pricing_engine as engine_script  # noqa: E402
import tsm_groups_export as mog_export  # noqa: E402
from woweconomy.analytics import auction_intel as ai  # noqa: E402
from woweconomy.analytics import mog_realm  # noqa: E402
from woweconomy.analytics import pricing_engine as pe  # noqa: E402
from woweconomy.analytics import pricing_groups as pg  # noqa: E402
from woweconomy.analytics.mog_operations import (  # noqa: E402
    GROUP_NAMES, LiquidityOperation, build_liquidity_export,
)
from woweconomy.analytics.tsm_export import parse_group_export  # noqa: E402
from woweconomy.analytics.token_rush_data import (  # noqa: E402
    load_vendor_sell, realm_groups,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
OUT_DIR = ROOT / "reports" / "mog_groups"
REPORT = ROOT / "reports" / "token_rush_groups.md"

#: the rush operational parameters (short: the campaign has days, not months)
RUSH_DURATION_H = 12
RUSH_HOLD_DAYS = 7
#: the floor under any listing: the vendor price, as in the normal model
VENDOR_FLOOR = "1.5 * vendorsell"


def g(value) -> str:
    return f"{value / 10_000:,.0f} g"


def slugify(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "realm"


def fmt_ratio(value: float) -> str:
    """A ratio as a plain decimal: ``:g`` would write 1.2e-05, which is not
    a valid TSM number token."""
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def banded_ratio_expression(ratios: dict[str, float]) -> str:
    """``ifgt(V, edge, r * V, ...)`` with one measured ratio per band."""
    order = pg.band_order()
    fallback = f"{fmt_ratio(ratios.get(order[-1], 1.0))} * dbmarket"
    picks = []
    for index, edge in enumerate(pg.BAND_EDGES_G):
        band = order[index]
        picks.append((edge, f"{fmt_ratio(ratios.get(band, 1.0))} * dbmarket"))
    for edge, pick in reversed(picks):
        fallback = f"ifgt(dbmarket, {edge}g, {pick}, {fallback})"
    return fallback


def load_catalogue(con) -> dict[int, str]:
    """item id -> the behaviour group it belongs to (the mog groups)."""
    out: dict[int, str] = {}
    for group, ids in mog_export.load_items(OUT_DIR).items():
        for item_id in ids:
            out[item_id] = group
    return out


def group_membership(con) -> dict[str, list[int]]:
    """The TSM groups, built by the **mog export itself**.

    Same builder, same item lists, same order, same ``conflict`` handling: the
    rush export differs from the mog export in the *price*, not in the items.
    """
    from woweconomy.analytics.mog_operations import build_mog_export
    from woweconomy.analytics.tsm_export import deserialize

    items = mog_export.load_items(OUT_DIR)
    if not items:
        raise SystemExit(f"nenhum grupo em {OUT_DIR} (esperava liquid.txt, ...)")
    text = build_mog_export(items, with_operations=False)
    return parse_group_export(deserialize(text)["groupExport"])


def build_realm(con, canonical, scope, catalogue, config, calibration, factors,
                demand, accounts_sampled, journal, vendor, local_age):
    """The engine's recommended price for every priced item of a realm."""
    region = {}
    for item, market, historical, avg_sale, sold_per_day in con.execute(
            "SELECT key_id, market_value, historical, avg_sale_price, sold_per_day "
            "FROM tsm_region_price WHERE snapshot_id="
            "(select max(snapshot_id) from tsm_region_price)"):
        region[int(item)] = (market, historical, avg_sale, sold_per_day)
    local = {}
    for item, market, wall, recent, historical in con.execute(
            "SELECT item_id, market_value, min_buyout, recent, historical "
            "FROM tsm_realm_price WHERE scope=?", (scope,)):
        local[int(item)] = (market, wall, recent, historical)

    rows = []
    for item_id, (market, wall, recent, historical) in local.items():
        reg = region.get(item_id)
        if not reg or not market:
            continue
        rm, rh, ra, spd = reg
        sources = [
            pe.SourceValue("dbmarket", int(market), pe.MARKET_REFERENCE, pe.LOCAL,
                            pe.FAMILY_LOCAL_ASK, age_hours=local_age, depth=1 if wall else 0),
            pe.SourceValue("dbrecent", int(recent or 0), pe.ASKING, pe.LOCAL,
                            pe.FAMILY_LOCAL_ASK, age_hours=local_age,
                            depth=1 if wall else 0),
            pe.SourceValue("dbhistorical", int(historical or 0), pe.ASKING, pe.LOCAL,
                            pe.FAMILY_LOCAL_ASK, age_hours=local_age,
                            depth=1 if wall else 0),
            pe.SourceValue("dbminbuyout", int(wall or 0), pe.ASKING, pe.LOCAL,
                            pe.FAMILY_LOCAL_ASK, age_hours=local_age,
                            depth=1 if wall else 0),
            pe.SourceValue("dbregionmarketavg", int(rm or 0), pe.MARKET_REFERENCE,
                            pe.REGIONAL, pe.FAMILY_REGIONAL_ASK, age_hours=0.0),
            pe.SourceValue("dbregionhistorical", int(rh or 0), pe.ASKING, pe.REGIONAL,
                            pe.FAMILY_REGIONAL_ASK, age_hours=0.0),
            pe.SourceValue("dbregionsaleavg", int(ra or 0), pe.TRANSACTION, pe.REGIONAL,
                            pe.FAMILY_REGIONAL_SALES, age_hours=0.0, volume=spd),
        ]
        own = journal.get((item_id, canonical)) or {}
        if own.get("sales"):
            sources.append(pe.SourceValue(
                "venda própria", own["copper"] // own["sales"], pe.TRANSACTION,
                pe.LOCAL, pe.FAMILY_OWN_SALES, age_hours=0.0, sample_size=own["sales"]))
        attempts = own.get("successes", 0) + own.get("failures", 0)
        evidence = pe.Evidence(
            item_id=item_id, realm=canonical, band=pg.band_of((rm or market) / 10_000),
            sources=sources,
            own_sales=own.get("sales", 0), own_expired=own.get("failures", 0),
            own_sold_copper=own.get("copper", 0),
            own_sell_through=(own["successes"] / attempts) if attempts else None,
            want_rate=(demand.get(item_id) / accounts_sampled
                       if accounts_sampled and demand.get(item_id) else None),
            demand_known=bool(demand.get(item_id)),
            market_clearance=factors.get(pg.band_of((rm or market) / 10_000), (None, 0))[0],
            market_clearance_n=factors.get(pg.band_of((rm or market) / 10_000), (None, 0))[1],
            listings=1 if wall else 0,
            vendor_sell=vendor.get(item_id),
        )
        value = pe.estimate_value(evidence, config, calibration)
        liquidity = pe.assess_liquidity(evidence, config)
        listing = pe.recommend_listing(value, liquidity, evidence, config)
        if not listing.price:
            continue
        rows.append({
            "item_id": item_id,
            "group": catalogue.get(item_id, "unknown"),
            "band": evidence.band,
            "local_market": int(market),
            "recommended": listing.price,
            "low": value.low or listing.price,
            "high": value.high or listing.price,
            "confidence": value.confidence,
            "liquidity": liquidity.level,
        })
    return rows


def groups_for_realm(membership, rows) -> tuple[dict, list[dict], dict]:
    """The TSM groups of a realm: the items, and the measured ratios per band."""
    by_group: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        # the catalogue stores the slug ("liquid"), the TSM group the name
        # ("Liquid"): they have to meet
        by_group[GROUP_NAMES.get(row["group"], row["group"])][row["band"]].append(row)

    groups: dict[str, list[int]] = {}
    operations: dict[str, LiquidityOperation] = {}
    report: list[dict] = []

    for group, ids in membership.items():
        name = GROUP_NAMES.get(group, group)
        bands = by_group.get(group, {})
        all_items = [item for items in bands.values() for item in items]

        def ratio_of(field, items):
            values = [item[field] / item["local_market"]
                      for item in items if item["local_market"]]
            return statistics.median(values) if values else None

        normal_ratio = ratio_of("recommended", all_items) or 1.0
        band_ratios: dict[str, float] = {}
        min_ratios: dict[str, float] = {}
        max_ratios: dict[str, float] = {}
        for band, items in bands.items():
            band_ratios[band] = ratio_of("recommended", items) or normal_ratio
            min_ratios[band] = ratio_of("low", items) or band_ratios[band]
            max_ratios[band] = ratio_of("high", items) or band_ratios[band]
        for band in pg.band_order():
            band_ratios.setdefault(band, normal_ratio)
        default_min = min(min_ratios.values()) if min_ratios else normal_ratio
        default_max = max(max_ratios.values()) if max_ratios else normal_ratio
        for band in pg.band_order():
            min_ratios.setdefault(band, default_min)
            max_ratios.setdefault(band, default_max)

        # the same order as the mog export, so the two payloads carry the
        # same item lists, item for item
        groups[name] = list(ids)
        operations[name] = LiquidityOperation(
            normal=f"max({banded_ratio_expression(band_ratios)}, {VENDOR_FLOOR})",
            minimum=f"max({banded_ratio_expression(min_ratios)}, {VENDOR_FLOOR})",
            maximum=f"max({banded_ratio_expression(max_ratios)}, {VENDOR_FLOOR})",
            duration=RUSH_DURATION_H,
            undercut="1c",
            max_expires=max(1, round(RUSH_HOLD_DAYS * 24 / RUSH_DURATION_H) - 1),
            price_reset="none",
            above_max="maxPrice",
        )
        report.append({
            "tsm_group": name,
            "group": group,
            "items": len(ids),
            "priced": len(all_items),
            "ratio": round(normal_ratio, 4),
            "min_ratio": round(min(min_ratios.values()), 4),
            "max_ratio": round(max(max_ratios.values()), 4),
            "median_price_g": round(statistics.median(
                [item["recommended"] for item in all_items]) / 10_000) if all_items else 0,
            "liquidity": ", ".join(sorted({item["liquidity"] for item in all_items})) or "—",
            "bands": {band: round(band_ratios[band], 4) for band in sorted(band_ratios)},
        })
    return groups, report, operations


def build_rush_export(items, profile, duration_h=RUSH_DURATION_H,
                      hold_days=RUSH_HOLD_DAYS) -> tuple[str, list[dict]]:
    """The mog export of a realm with the **rush timing**.

    Nothing else changes: the same groups, the same items, the same price
    expressions (the band coefficients of the normal model times the realm
    factor) and the same demand signals that built the groups.  Only the
    operational timing is the campaign's: a short duration and a short holding
    window, because the campaign has days and not months.
    """
    from woweconomy.analytics.mog_operations import build_mog_export
    from woweconomy.analytics.tsm_export import deserialize, serialize

    text = build_mog_export(items, with_operations=True, conflict_inline=False,
                             operation_prefix="Mogs ", realm=profile)
    data = deserialize(text)
    max_expires = max(1, round(hold_days * 24 / duration_h) - 1)
    for operation in data.get("operations", {}).get("Auctioning", {}).values():
        operation["duration"] = duration_h
        operation["maxExpires"] = max_expires
    rows = [{
        "tsm_group": name,
        "duration_h": duration_h,
        "max_expires": max_expires,
        "priceReset": operation.get("priceReset"),
        "aboveMax": operation.get("aboveMax"),
    } for name, operation in sorted(data.get("operations", {}).get("Auctioning", {}).items())]
    return serialize(data), rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dfa-db", default=str(engine_script.pg_script.DFA_DB))
    ap.add_argument("--realms", default="", help="reinos (por omissão, os da conta)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    config = pe.EngineConfig()
    calibration = engine_script.load_calibration()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con_dfa = sqlite3.connect(f"file:{args.dfa_db}?mode=ro", uri=True) \
        if Path(args.dfa_db).exists() else None
    catalogue = load_catalogue(con)
    membership = group_membership(con)
    total_items = sum(len(v) for v in membership.values())
    print(f"catálogo: {total_items:,} itens em {len(membership)} grupos "
          f"({', '.join(sorted(membership))})")

    factors = engine_script.clearance_factors(con)
    journal = engine_script.load_journal(con)
    vendor = load_vendor_sell(con, set(catalogue))
    as_of = con.execute("select max(fetched_at) from tsm_realm_price").fetchone()[0]
    region_at = con.execute("select max(updated_at) from tsm_snapshot "
                             "where scope='region' and kind='region_items'").fetchone()[0]
    local_age = engine_script.pg_script.age_hours(as_of, region_at)

    demand = {}
    accounts_sampled = 0
    if con_dfa:
        accounts_sampled = con_dfa.execute(
            "SELECT COUNT(DISTINCT account) FROM scanned WHERE kind='appearances'").fetchone()[0]
        missing = {int(i): int(n) for i, n in con_dfa.execute(
            "SELECT item_id, n FROM missing WHERE kind='appearances'")}
        for item, appearance in con.execute("SELECT itemId, appearanceId FROM db2_item_appearance"):
            n = missing.get(appearance)
            if n and n > demand.get(int(item), 0):
                demand[int(item)] = n

    realm_rows = realm_groups(con)
    names = [r.strip() for r in args.realms.split(",") if r.strip()] \
        or [row[0] for row in con.execute("SELECT DISTINCT realm FROM journal_stats ORDER BY realm")]
    written = []
    for name in names:
        profile = mog_realm.resolve(name)
        canonical = profile.realm
        row = realm_rows.get(canonical) or {}
        scope = row.get("canonical_slug") or canonical
        rows = build_realm(con, canonical, scope, catalogue, config, calibration,
                           factors, demand, accounts_sampled, journal, vendor, local_age)
        groups, report, operations = groups_for_realm(membership, rows)
        text = build_liquidity_export(groups, operations, operation_prefix="")
        path = args.out_dir / f"token_rush_{slugify(canonical)}.tsm"
        written.append({
            "realm": name, "canonical": canonical,
            "profile": f"mogs-{slugify(canonical)}",
            "groups": len(groups),
            "items": total_items,
            "priced": len(rows),
            "path": path, "rows": report, "chars": len(text), "text": text,
        })
        print(f"{name:18s} -> {canonical:18s} {len(groups)} grupos, "
              f"{total_items:,} itens, {len(text):,} caracteres")

    if not args.write:
        print("\n(modo verificação: usa --write para gravar)", file=sys.stderr)
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for entry in written:
        entry["path"].write_text(entry["text"] + "\n", encoding="utf-8")
    write_report(REPORT, written, args)
    print(f"-> {REPORT}")
    for entry in written:
        print(f"-> {entry['path']}")
    con.close()
    return 0


def write_report(path, written, args) -> None:
    lines = [
        "# Token Rush — grupos TSM por reino",
        "",
        "Gerado por `python scripts/token_rush_profiles.py --write`. **Nada foi "
        "importado**: cada ficheiro é uma proposta para um perfil TSM **desse** "
        "reino, criado à mão (ver §4).",
        "",
        "## 1. O que há em cada export",
        "",
        "- **Os mesmos grupos e os mesmos itens do export mog** "
        "(`tsm_groups_export.py --all-realms`): os 127 155 itens de transmog nos "
        "sete grupos de comportamento, com o `conflict` no grupo próprio. Um drop "
        "novo de transmog cai logo num grupo com preço.",
        "- **O preço é o do motor de evidência** (`pricing_engine`), escrito como "
        "fórmula viva: `ifgt(dbmarket, banda, r × dbmarket, …)`, com `r` a **razão "
        "medida** (preço recomendado ÷ mercado local) dos itens desse grupo e "
        "banda. O preço acompanha o mercado local; a calibragem vem do motor, "
        "não de uma constante.",
        "- **A operação é de rush**: 12 h de duração, 7 dias de retenção "
        "(`maxExpires` 13), `priceReset = none`, `aboveMax = maxPrice`, sem "
        "cancelamento automático.",
        "- O piso é `max(razão × dbmarket, 1.5 × vendorsell)`: nunca abaixo do "
        "que o fornecedor paga.",
        "",
        "> **Atenção operacional:** com concorrência, o TSM posta "
        "`parede − undercut` limitado por `[minPrice, maxPrice]`, ou seja, quem "
        "decide o preço é a **parede** (o anúncio mais barato do reino), não a "
        "nossa fórmula. A fórmula só entra quando **não há** concorrência — e "
        "nesse caso a razão medida pode estar muito abaixo da parede, porque o "
        "ask médio é inflacionado. Aí estamos a vender abaixo do mercado "
        "visível, de propósito (é uma liquidação), mas vale a pena confirmar "
        "item a item no *Price Tester* antes de deixar o TSM postar sozinho.",
        "",
        "## 2. Os reinos",
        "",
        "| reino | cluster | perfil TSM | grupos | itens | com preço | ficheiro |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for entry in written:
        lines.append(f"| {entry['realm']} | {entry['canonical']} | "
                     f"`{entry['profile']}` | {entry['groups']} | {entry['items']:,} | "
                     f"{entry['priced']:,} | `{entry['path'].name}` |")

    lines += [
        "",
        "## 3. Os grupos e as suas razões",
        "",
        "| reino | grupo TSM | itens | com preço | razão (preço/ask local) | "
        "preço mediano | liquidez |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for entry in written:
        for row in entry["rows"]:
            lines.append(f"| {entry['canonical']} | `{row['tsm_group']}` | "
                         f"{row['items']:,} | {row['priced']:,} | {row['ratio']:.3f} | "
                         f"{row['median_price_g']:,.0f} g | {row['liquidity']} |")

    lines += [
        "",
        "## 4. Perfil TSM por reino (uma vez por conta)",
        "",
        "1. `TSM → Settings → General → Profiles` → escreve o nome do perfil "
        "(`mogs-<reino>` na tabela) e Enter. O TSM cria-o e passa a usá-lo.",
        "2. `Groups → Import` → cola o conteúdo de `token_rush_<reino>.tsm` "
        "**num perfil limpo** (importar duas vezes no mesmo perfil cria "
        "`… 1`).",
        "3. Repete por personagem: o perfil é por personagem (`_currentProfile`), "
        "logo um alt novo precisa de `/tsm profile mogs-<reino>`.",
        "4. Confirma com *Post Scan* num item de cada banda: o preço proposto "
        "tem de estar entre o piso e o tecto, e o undercut tem de ser o esperado.",
        "",
        "**Reversão:** nada foi alterado no perfil activo. Para desfazer, apaga o "
        "perfil de teste (ou os grupos importados) e importa a proposta "
        "original (`pricing_groups_proposal.tsm`). As propostas nunca devem "
        "coexistir no mesmo perfil.",
        "",
        "## 5. Limites",
        "",
        "- A razão por grupo é a mediana dos itens, por isso a variação **dentro** "
        "do grupo não é respeitada — um TSM group tem uma só operação. Para "
        "respeitar item a item há o export por item "
        "(`liquidity_mogs_groups_<reino>.tsm`).",
        "- Os custos (taxa e depósito) continuam a ser os **medidos** pelo addon, "
        "quando há eventos; até lá, são os 5 % assumidos.",
        "- A calibração do motor está feita contra as vendas **sem data**; o addon "
        "`DFA_AuctionIntelligence` é o que fecha essa falta.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
