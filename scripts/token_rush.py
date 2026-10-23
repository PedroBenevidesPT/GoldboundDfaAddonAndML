"""Token Rush: 12 dias para transformar inventário em ouro.

    python scripts/token_rush.py --show
    python scripts/token_rush.py --write
    python scripts/token_rush.py --write --export --flipqueue
    python scripts/token_rush.py --feedback

Lê o inventário (`inventory.csv`), o estado local de cada reino
(`tsm_realm_price`), a referência regional (`tsm_region_price`), a contagem de
anúncios (`tsm_market`, 12 reinos), o histórico próprio de vendas e expirações
(`journal_stats`) e o perfil medido do reino (`mog_realm`).

Saídas (tudo em modo de *dry run*, nada é posto automaticamente):

    reports/token_rush/token_rush.md        o relatório
    reports/token_rush/rush_queue.csv       a fila, uma linha por item×reino
    reports/token_rush/flipqueue_<realm>.csv lista para importar no FlipQueue
    reports/mog_groups/liquidity_mogs_groups_<realm>.tsm  import do TSM
    reports/token_rush/feedback.md          o que foi realmente vendido

O ficheiro `liquidity_mogs_groups_<realm>.tsm` é um import do TSM com **um
grupo e uma operação por item**, cada um com o preço que o modelo recomenda e
os limites (piso de perda, tecto da referência).  Não mexe nas operações
normais: são ficheiros novos, para um perfil novo.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics import mog_realm  # noqa: E402
from woweconomy.analytics.mog_operations import (  # noqa: E402
    LIQUIDITY_PREFIX, LiquidityOperation, build_liquidity_export,
)
from woweconomy.analytics.token_rush import (  # noqa: E402
    COPPER_PER_GOLD, COMP_HIGH, COMP_LOW, COMP_MID, COMP_UNKNOWN, EVIDENCE_WEIGHT,
    PROFILE_LABEL, PROFILE_LIQUID, PROFILE_PROVEN, PROFILE_SPECULATIVE,
    PROFILE_STALE, PROFILE_UNKNOWN, CampaignConfig, ItemEvidence, RealmEvidence,
    build_realm_posture, campaign_state, checkpoints, classify_item,
    feedback_from_journal, gold, observed_sell_through, prioritise, rush_price,
)
from woweconomy.analytics.token_rush_data import (  # noqa: E402
    duplicate_realms, load_commodity_ids, load_journal, load_market_counts,
    load_realm_prices, load_region_prices, load_vendor_sell, read_inventory,
    realm_groups,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
OUT_DIR = ROOT / "reports" / "token_rush"
EXPORT_DIR = ROOT / "reports" / "mog_groups"

# The realms the campaign was asked to evaluate.  ``--realms`` overrides it.
DEFAULT_REALMS = [
    "Dragonmaw", "Spinebreaker", "Stormreaver", "Haomarush",
    "Twisting Nether", "Argent Dawn", "Silvermoon", "Draenor",
    "Aggra (Português)", "Pozzo dell'Eternità", "Shattered Halls", "Uldaman",
]


def slugify(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "realm"


def money(copper: int) -> str:
    """TSM money string: whole gold, silver and copper (no decimals allowed)."""
    copper = max(0, int(copper))
    g, rest = divmod(copper, COPPER_PER_GOLD)
    s, c = divmod(rest, 100)
    return f"{g}g {s}s {c}c"


def gold_text(copper: int | float) -> str:
    """Format a value that is in copper."""
    return f"{gold(copper):,.0f} g"


def g_text(value_gold: float) -> str:
    """Format a value that is already in gold."""
    return f"{value_gold:,.0f} g"


# ---------------------------------------------------------------------------
# building the queue
# ---------------------------------------------------------------------------

def build_rows(args, config: CampaignConfig) -> tuple[list, dict, dict]:
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    inventory = read_inventory(args.inventory)
    if not inventory:
        raise SystemExit(f"inventário vazio: {args.inventory}")
    item_ids = sorted(inventory)

    region = load_region_prices(con, item_ids)
    vendor = load_vendor_sell(con, item_ids)
    commodities = load_commodity_ids(con, item_ids)
    journal = load_journal(con, item_ids)
    market_counts = load_market_counts(con)
    groups = realm_groups(con)
    duplicates = duplicate_realms(con)
    account_realms = {r[0] for r in con.execute("SELECT DISTINCT realm FROM journal_stats")}

    # the TSM export loses some names ("Unknown"); the DB2 always has them
    names = {int(i): n for i, n in con.execute(
        f"SELECT itemId, name FROM db2_items WHERE itemId IN ({','.join('?' * len(item_ids))})",
        item_ids)}
    for item_id, item in list(inventory.items()):
        if not item.name or item.name.strip().lower() in ("unknown", "?"):
            if names.get(item_id):
                inventory[item_id] = dataclasses.replace(item, name=names[item_id])

    # --- realm postures -------------------------------------------------
    # Connected realms share one auction house: they are evaluated once, and
    # the aliases are kept so the report says which realms a line covers.
    postures: dict[str, object] = {}
    realm_scopes: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    for name in args.realms:
        profile = mog_realm.resolve(name)
        row = groups.get(name) or groups.get(profile.realm) or {}
        scope = row.get("canonical_slug") or mog_realm._key(profile.realm)
        realm_scopes[name] = scope
        counts = market_counts.get(profile.realm) or market_counts.get(name) or {}
        listed = [n for n in counts.values() if n >= 1]
        depths = sorted(listed)
        median_depth = depths[len(depths) // 2] if depths else None
        evidence = RealmEvidence(
            realm=name,
            canonical=profile.realm,
            klass=profile.klass,
            factor=profile.factor,
            listings_median=median_depth,
            has_listing_counts=bool(counts),
            confidence=profile.confidence,
            duplicate_of=tuple(sorted({d for d in
                                       (duplicates.get(profile.realm, ())
                                        + duplicates.get(name, ()))
                                       if d != name and d != profile.realm})),
        )
        posture = build_realm_posture(evidence)
        if name not in account_realms:
            posture = dataclasses.replace(posture, warnings=posture.warnings + (
                "sem actividade desta conta neste reino: avaliado, "
                "mas não pode ser publicado sem personagem",))
        postures[name] = posture
        aliases.setdefault(profile.realm, []).append(name)

    # one line per connected group: the first requested name represents it
    seen: set[str] = set()
    markets: list[tuple[str, str, tuple[str, ...]]] = []
    for name in args.realms:
        canonical = mog_realm.resolve(name).realm
        if canonical in seen:
            continue
        seen.add(canonical)
        markets.append((name, realm_scopes[name], tuple(aliases.get(canonical, [name]))))

    # --- rows -----------------------------------------------------------
    rows = []
    for name, scope, group_aliases in markets:
        posture = postures[name]
        prices = load_realm_prices(con, scope, item_ids)
        counts = market_counts.get(posture.canonical) or market_counts.get(name) or {}
        for item_id, item in inventory.items():
            local = prices.get(item_id)
            reg = region.get(item_id)
            own = journal.get((item_id, name)) or journal.get((item_id, posture.canonical)) or {}
            evidence = ItemEvidence(
                item_id=item_id,
                name=item.name,
                qty=item.qty,
                realm=name,
                quality=item.quality,
                sales=int(own.get("sales", 0)),
                expired=int(own.get("expired", 0)),
                attempts=int(own.get("attempts", 0)),
                sold_copper=int(own.get("sold_copper", 0)),
                region_market=reg.market if reg else 0,
                region_historical=reg.historical if reg else 0,
                region_avg_sale=reg.avg_sale if reg else 0,
                region_sold_per_day=reg.sold_per_day if reg else 0.0,
                region_sale_rate=reg.sale_rate if reg else 0.0,
                realm_market=local.market if local else 0,
                realm_min_buyout=local.min_buyout if local else 0,
                realm_recent=local.recent if local else 0,
                realm_historical=local.historical if local else 0,
                listings=counts.get(item_id) if item_id in counts else None,
                is_commodity=item_id in commodities,
                vendor_sell=vendor.get(item_id),
            )
            classification = classify_item(evidence, config)
            cost_basis = evidence.sold_copper // max(1, evidence.sales) if evidence.sales else None
            price = rush_price(evidence, classification, posture, config,
                               days_remaining=args.days_remaining, cost_basis=cost_basis)
            row = _row(evidence, classification, posture, price, config)
            row.aliases = group_aliases
            if name not in account_realms:
                row.realm_weight = config.inactive_realm_weight
            rows.append(row)
    con.close()
    return rows, postures, realm_scopes


def _row(evidence, classification, posture, price, config):
    from woweconomy.analytics.token_rush import RushRow

    row = RushRow(
        item_id=evidence.item_id,
        name=evidence.name,
        realm=evidence.realm,
        qty=evidence.qty,
        evidence=evidence,
        classification=classification,
        posture=posture,
        price=price,
    )
    row.warnings = tuple(posture.warnings) + tuple(price.flags)
    return row


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------

QUEUE_FIELDS = [
    "rank", "item_id", "name", "realm", "qty", "profile", "profile_label",
    "confidence", "coverage", "sales", "expired", "attempts", "sell_through",
    "competition", "listings", "reference_g", "reference_source", "comparable_g",
    "comparable_valid", "undercut", "recommended_g", "min_g", "max_g",
    "recommended_copper", "min_copper", "max_copper", "is_commodity",
    "net_if_sold_g", "deposit_g", "loss_vs_cost", "expected_g", "expected_basis",
    "priority", "hours", "action", "reasons", "warnings", "adjustments",
]


def write_queue_csv(path: Path, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            price = row.price
            writer.writerow({
                "rank": index,
                "item_id": row.item_id,
                "name": row.name,
                "realm": row.realm,
                "qty": row.qty,
                "profile": row.classification.profile,
                "profile_label": PROFILE_LABEL[row.classification.profile],
                "confidence": row.classification.confidence,
                "coverage": f"{row.classification.coverage:.0%}",
                "sales": row.evidence.sales,
                "expired": row.evidence.expired,
                "attempts": row.evidence.attempts,
                "sell_through": (f"{row.evidence.sell_through:.0%}"
                                 if row.evidence.sell_through is not None else ""),
                "competition": row.posture.competition,
                "listings": row.evidence.listings if row.evidence.listings is not None else "",
                "reference_g": f"{gold(price.reference):,.0f}",
                "reference_source": price.reference_source,
                "comparable_g": (f"{gold(price.comparable):,.0f}" if price.comparable else ""),
                "comparable_valid": "sim" if price.comparable_valid else "nao",
                "undercut": f"{price.undercut_pct:.0%}",
                "recommended_g": f"{gold(price.recommended):,.0f}",
                "min_g": f"{gold(price.min_price):,.0f}",
                "max_g": f"{gold(price.max_price):,.0f}",
                "recommended_copper": price.recommended,
                "min_copper": price.min_price,
                "max_copper": price.max_price,
                "is_commodity": "sim" if row.evidence.is_commodity else "nao",
                "net_if_sold_g": f"{gold(price.net_if_sold):,.0f}",
                "deposit_g": (f"{gold(price.deposit):,.0f}" if price.deposit is not None else ""),
                "loss_vs_cost": (f"{price.loss_vs_cost:.0%}"
                                 if price.loss_vs_cost is not None else ""),
                "expected_g": (f"{row.expected:,.0f}" if row.expected is not None else ""),
                "expected_basis": row.expected_basis,
                "priority": f"{row.priority:,.0f}",
                "hours": f"{row.hours:.2f}",
                "action": row.action,
                "reasons": " | ".join(row.classification.reasons),
                "warnings": " | ".join(row.warnings),
                "adjustments": " | ".join(row.price.adjustments),
            })


def write_flipqueue_csv(path: Path, rows) -> None:
    """A list in the format FlipQueue's import understands (FP comma CSV).

    The addon maps "Sell Price" to the expected price and "Sell Realm" to the
    target realm, so the queue arrives already priced and aimed.
    """
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Item ID", "Item Name", "Quality", "iLvl", "Sell Price",
                         "Sell Realm", "Sell Rate", "Category"])
        for row in rows:
            writer.writerow([
                row.item_id,
                row.name,
                row.evidence.quality or "",
                0,
                f"{gold(row.price.recommended):,.0f}g",
                row.realm,
                f"{row.evidence.region_sale_rate:.3f}" if row.evidence.region_sale_rate else "",
                "Reagent" if row.evidence.is_commodity else "",
            ])


def write_liquidity_export(path: Path, rows) -> int:
    """One group and one operation per item, with the model's price."""
    groups = {}
    operations = {}
    for row in rows:
        if not row.price.recommended:
            continue
        label = row.name or f"item {row.item_id}"
        name = f"{row.item_id} {label[:36]}"
        groups[name] = [row.item_id]
        operations[name] = LiquidityOperation(
            normal=money(row.price.recommended),
            minimum=money(row.price.min_price),
            maximum=money(row.price.max_price or row.price.recommended),
            duration=12,
            undercut="1c",
            max_expires=2,
            stack_size=999 if row.evidence.is_commodity else 1,
        )
    if not groups:
        return 0
    text = build_liquidity_export(groups, operations)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text + "\n")
    return len(groups)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def write_report(path: Path, args, config: CampaignConfig, rows, postures, realm_scopes,
                 state, points) -> None:
    by_profile = defaultdict(list)
    for row in rows:
        by_profile[row.classification.profile].append(row)

    top = rows[:args.top]
    observed = [r for r in rows if r.expected is not None]
    upper_bound = sum(gold(r.price.net_if_sold) * r.qty for r in rows)
    heuristic_total = sum((r.expected if r.expected is not None
                           else r.potential_gold * EVIDENCE_WEIGHT[r.classification.profile])
                          for r in rows)
    observed_total = sum(r.expected for r in observed)
    hours = state.hours_remaining
    affordable = []
    spent = 0.0
    for row in rows:
        if spent + row.hours > hours:
            break
        affordable.append(row)
        spent += row.hours
    affordable_gold = sum((r.expected if r.expected is not None
                           else r.potential_gold * EVIDENCE_WEIGHT[r.classification.profile])
                          for r in affordable)

    lines = [
        "# Token Rush — relatório de liquidação (dry run)",
        "",
        f"Gerado por `python scripts/token_rush.py` a partir de `{Path(args.db).name}` "
        f"e `{Path(args.inventory).name}`. Nada foi posto: este ficheiro é uma "
        "recomendação, e cada valor tem a sua base escrita ao lado.",
        "",
        "## 1. Estado da campanha",
        "",
        "| medida | valor |",
        "|---|---:|",
        f"| ouro actual | {gold_text(state.gold_now)} |",
        f"| preço do token usado | {gold_text(state.token_price)} |",
        f"| falta para o token | {gold_text(state.gold_needed)} |",
        f"| alvo operacional (ouro líquido a fazer) | {gold_text(state.target_gold)} |",
        f"| **a liquidar** (o maior dos dois) | **{gold_text(state.deficit)}** |",
        f"| dias restantes | {state.days_remaining} |",
        f"| horas de gestão restantes | {state.hours_remaining:.0f} h |",
        f"| linhas na fila | {len(rows)} |",
        "",
        f"O défice é recalculado a cada execução com o preço do token e o ouro "
        f"actuais. A campanha tem de liquidar **o maior** entre a falta para o "
        f"token e o alvo operacional: {gold_text(state.deficit)}. Se o objectivo "
        f"for ter as duas coisas (token comprado *e* {gold_text(state.target_gold)} "
        f"de reserva), o número sobe para {gold_text(state.gold_needed + state.target_gold)}.",
        "",
        "## 2. O que o inventário vale — e o que é realizável",
        "",
        "Três números, e só um deles é uma previsão com base em observações:",
        "",
        f"| número | valor | o que é |",
        f"|---|---:|---|",
        f"| tecto | {g_text(upper_bound)} | se **tudo** vendesse ao preço recomendado, sem taxas nem depósitos. Não é uma previsão |",
        f"| heurística | {g_text(heuristic_total)} | valor potencial ponderado pelo peso de evidência nas linhas sem histórico. **Heurística**: o peso não é uma probabilidade |",
        f"| observada | {g_text(observed_total)} | o que a evidência da própria conta sustenta: {len(observed)} de {len(rows)} linhas têm vendas registadas |",
        "",
        f"> O valor de mercado estimado do inventário (~600 k g) **não** é o que se "
        f"vai realizar. A diferença entre o tecto ({g_text(upper_bound)}) e o "
        f"número observado ({g_text(observed_total)}) é o espaço onde esta "
        "campanha opera, e é muito maior do que o objectivo de "
        f"{gold_text(state.target_gold)}.",
        "",
        f"**O orçamento de gestão decide quantas linhas cabem.** Com "
        f"{hours:.0f} h e {config.hours_per_item:.2f}-"
        f"{config.hours_per_competitive_item:.2f} h por linha cabem "
        f"**{len(affordable)} linhas** ({g_text(affordable_gold)} pela estimativa "
        f"heurística). As {len(rows) - len(affordable)} restantes só entram se "
        "houver tempo ou se forem materiais a vender ao fornecedor.",
        "",
        "## 3. Postura por reino",
        "",
        "| reino | classe | factor | concorrência | anúncios/item | confiança | avisos |",
        "|---|---|---:|---|---:|---|---|",
    ]
    for name in args.realms:
        posture = postures[name]
        depth = (f"{posture.listings_median:g}" if posture.listings_median is not None else "—")
        warnings = "<br>".join(posture.warnings) or "—"
        lines.append(f"| {name} | {posture.klass} | {posture.factor:.2f} | "
                     f"{posture.competition} | {depth} | {posture.confidence} | {warnings} |")

    lines += [
        "",
        "Factor = mediana de `mercado local / mercado regional` medido no catálogo. "
        "A classe (`thin`/`balanced`/`deep`) vem da concorrência observada; onde não "
        "há contagem de anúncios é inferida do preço e diz isso.",
        "",
        "## 4. Distribuição por perfil",
        "",
        "| perfil | o que significa | itens (linhas) | ouro potencial | peso de evidência |",
        "|---|---|---:|---:|---:|",
    ]
    for profile in (PROFILE_LIQUID, PROFILE_PROVEN, PROFILE_SPECULATIVE, PROFILE_STALE, PROFILE_UNKNOWN):
        group = by_profile.get(profile, [])
        if not group:
            continue
        potential = sum(r.potential_gold for r in group)
        lines.append(f"| {profile} | {PROFILE_LABEL[profile]} | {len(group)} | "
                     f"{g_text(potential)} | {EVIDENCE_WEIGHT[profile]:.2f} |")

    lines += [
        "",
        "## 5. A fila",
        "",
        "Ordenada por contribuição para o défice por hora de gestão. `esperado` só "
        "aparece quando há histórico de venda do próprio item; caso contrário a "
        "linha usa o valor potencial e diz *heurística*.",
        "",
        "| # | item | reino | perfil | evidência | concorrência | referência | comparável | preço | piso | líquido | esperado | acção |",
        "|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for index, row in enumerate(top, 1):
        price = row.price
        comparable = (f"{gold(price.comparable):,.0f}" if price.comparable else "—")
        expected = (f"{gold(row.expected):,.0f} ({row.expected_basis.split(' ')[0]})"
                    if row.expected is not None else "— (heurística)")
        lines.append(
            f"| {index} | {row.name[:34]} ({row.item_id}) | {row.realm} | "
            f"{row.classification.profile} | {row.classification.confidence} | "
            f"{row.posture.competition} | {gold(price.reference):,.0f} | {comparable} | "
            f"**{gold(price.recommended):,.0f}** | "
            f"{gold(price.min_price):,.0f} | "
            f"{gold(price.net_if_sold):,.0f} | {expected} | {row.action[:40]} |"
        )

    lines += [
        "",
        "## 5.1 Onde publicar, por reino",
        "",
        "| reino | linhas | potencial | observadas | activo na conta |",
        "|---|---:|---:|---:|---|",
    ]
    for name in args.realms:
        subset = [r for r in rows if r.realm == name]
        if not subset:
            continue
        active = ("não" if any("sem actividade" in w for w in subset[0].posture.warnings)
                  else "sim")
        potential = sum(r.potential_gold for r in subset)
        obs = sum(1 for r in subset if r.expected is not None)
        aliases = ", ".join(subset[0].aliases)
        lines.append(f"| {name}{' (' + aliases + ')' if len(subset[0].aliases) > 1 else ''} | "
                     f"{len(subset)} | {g_text(potential)} | {obs} | {active} |")

    lines += [
        "",
        "## 5.2 Os itens que decidem a campanha",
        "",
        "Ordenados pelo ouro em jogo, não pela prioridade: são os que podem, sozinhos, "
        "cobrir o défice. Todos sem histórico de venda — por isso o risco é grande e o "
        "preço agressivo é a aposta, não a evidência.",
        "",
        "| item | reino | perfil | referência | preço recomendado | potencial | risco |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in sorted(rows, key=lambda r: -r.potential_gold)[:12]:
        risk = ("sem vendas registadas" if not row.evidence.sales and not row.evidence.expired
                else f"{row.evidence.sales} vendas / {row.evidence.expired} expirações")
        lines.append(f"| {row.name[:32]} ({row.item_id}) | {row.realm} | "
                     f"{row.classification.profile} | {gold(row.price.reference):,.0f} | "
                     f"{gold(row.price.recommended):,.0f} | {g_text(row.potential_gold)} | {risk} |")

    lines += [
        "",
        "## 5.3 Como cada preço foi construído",
        "",
    ]
    for index, row in enumerate(top[:6], 1):
        lines.append(f"**{index}. {row.name} ({row.item_id}) em {row.realm}**")
        lines.append("")
        lines.append(f"- Perfil **{row.classification.profile}** ({PROFILE_LABEL[row.classification.profile]}), "
                     f"confiança **{row.classification.confidence}**, cobertura da evidência "
                     f"{row.classification.coverage:.0%}.")
        for reason in row.classification.reasons:
            lines.append(f"  - {reason}")
        for adjustment in row.price.adjustments:
            lines.append(f"  - {adjustment}")
        lines.append(f"  - referência {gold(row.price.reference):,.0f} g → preço recomendado "
                     f"**{gold(row.price.recommended):,.0f} g**, piso "
                     f"{gold(row.price.min_price):,.0f} g, tecto {gold(row.price.max_price):,.0f} g, "
                     f"líquido depois da taxa de {config.ah_fee_pct:.0%} "
                     f"{gold(row.price.net_if_sold):,.0f} g"
                     + (f", depósito {gold(row.price.deposit):,.0f} g por tentativa"
                        if row.price.deposit is not None else ", depósito desconhecido"))
        if row.expected is not None:
            lines.append(f"  - esperado (observado): {row.expected:,.0f} g "
                         f"— {row.expected_basis}")
        else:
            lines.append("  - esperado: sem estimativa (heurística)")
        for warning in row.price.flags:
            lines.append(f"  - ⚠ {warning}")
        lines.append("")

    lines += [
        "## 6. Marcos da campanha",
        "",
        "| dia | dias que restam | défice | por dia | acção |",
        "|---:|---:|---:|---:|---|",
    ]
    for point in points:
        lines.append(f"| {point['day']} | {point['days_remaining']} | "
                     f"{gold_text(point['deficit'])} | "
                     f"{gold_text(point['required_per_day'])} | {point['action']} |")

    lines += [
        "",
        "## 7. O que é dado e o que é heurística",
        "",
        "**Dado (observado):** as vendas e expirações do próprio inventário "
        "(`journal_stats`), o mercado local e o anúncio mais barato de cada reino "
        "(`tsm_realm_price`), a referência regional (`tsm_region_price`), a contagem "
        "de anúncios nos 12 reinos descarregados (`tsm_market`) e o preço de venda ao "
        "fornecedor (`blizzard_items`).",
        "",
        "**Estimado (não observado):** `sold_per_day` e `sale_rate` regionais da TSM "
        "(estimativas da Blizzard, não vendas medidas). Usados apenas para classificar "
        "a procura, e sempre com confiança *estimada*.",
        "",
        "**Heurística (decisão, não medição):** as bandas de undercut 15-25 % / "
        "10-20 % / 5-15 %, o factor de prazo (1,00 → "
        f"{config.deadline_price_factor:.2f}), o peso de evidência da fila, o limite "
        f"de perda ({config.max_loss_pct:.0%} face ao custo conhecido), o preço "
        f"mínimo ({config.min_price_copper / COPPER_PER_GOLD:.2f} g, ou o valor de "
        f"venda ao fornecedor quando existe), a taxa de "
        f"casa de auction ({config.ah_fee_pct:.0%}) e a regra de depósito "
        f"({config.deposit_pct:.0%}, mínimo {config.deposit_min_gold} g, máximo "
        f"{config.deposit_vendor_cap_pct:.0%} do valor de venda ao fornecedor). "
        + ("Estes custos estão **verificados**." if config.costs_verified
           else "Estes custos estão **assumidos, não verificados** nesta base de dados."),
        "",
        "**Nunca garantido:** nenhuma linha promete venda. O modelo nunca inventa "
        "probabilidades: onde não há histórico, a linha é marcada *heurística* e a "
        "ordem usa um peso de evidência, não uma probabilidade de venda.",
        "",
        "## 8. Bloqueios",
        "",
        "- **Sem custo de acquisition:** o inventário não traz o preço de compra. "
        "O piso de perda só pode ser calculado contra o preço a que o item vendeu no "
        "passado (`sold_copper`), e é esse que é usado; quando não há vendas, o piso "
        "fica no mínimo configurado e a perda fica *desconhecida*.",
        "- **Sem histórico por realm além do observado:** o `journal_stats` é "
        "agregado por item e reino, sem datas, o que impede medir tempo até à venda "
        "e taxa de venda por item com precisão de dias.",
        "- **Contagem de anúncios em 12 reinos:** nos outros 80 a concorrência é "
        "inferida do nível de preço e o preço de venda fica *sem comparável*.",
        "- **Sem backtest de lucro por reino:** não há série histórica de vendas por "
        "reino; a validação tem de ser feita com as vendas desta campanha.",
        "",
        "## 9. Ficheiros gerados",
        "",
        f"- `reports/token_rush/rush_queue.csv` — a fila completa "
        f"({len(rows)} linhas).",
    ]
    if args.flipqueue:
        lines.append("- `reports/token_rush/flipqueue_<reino>.csv` — lista para o "
                     "FlipQueue (Import → colar): `Sell Price` é o preço recomendado "
                     "e `Sell Realm` é o reino alvo.")
    if args.export:
        lines.append(f"- `reports/mog_groups/{LIQUIDITY_PREFIX}_<reino>.tsm` — import do "
                     "TSM, **um grupo e uma operação por item**, com o preço "
                     "recomendado, o piso e o tecto. Importa-se num perfil novo; não "
                     "altera as operações normais.")
    lines += [
        "",
        "> Nenhum ficheiro foi importado no TSM e nada foi configurado em jogo. "
        "Importa primeiro num perfil de teste.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_feedback(path: Path, args, rows) -> None:
    """What the campaign actually produced, from the journal only."""
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    raw = con.execute(
        "SELECT itemId, name, realm, sold_count, failures, sold_copper "
        "FROM journal_stats").fetchall()
    con.close()
    entries = feedback_from_journal([
        {"itemId": r[0], "name": r[1], "realm": r[2], "sold_count": r[3],
         "failures": r[4], "sold_copper": r[5]}
        for r in raw
    ])
    rates = observed_sell_through(entries)
    sold = sum(e.sold_copper for e in entries)
    expired = sum(e.expired for e in entries)
    sales = sum(e.sales for e in entries)

    by_item: dict[int, list] = defaultdict(list)
    for row in rows:
        by_item[row.item_id].append(row)

    lines = [
        "# Token Rush — feedback",
        "",
        "Fonte: `journal_stats` (vendas e expirações reais desta conta). "
        "Anúncios que desapareceram **não** contam como venda: só entram as "
        "entradas com venda registada.",
        "",
        f"- vendas registadas: **{sales:,}**",
        f"- expirações registadas: **{expired:,}**",
        f"- ouro realizado (bruto, sem taxas): **{gold_text(sold)}**",
        f"- itens com taxa de venda observada: **{len(rates)}**",
        "",
        "## Preço recomendado vs preço a que vendeu",
        "",
        "| item | reino | perfil | recomendado | vendeu a | diferença |",
        "|---|---|---|---:|---:|---:|",
    ]
    matched = 0
    for entry in entries:
        if not entry.sales:
            continue
        for row in by_item.get(entry.item_id, []):
            if row.realm != entry.realm and not entry.realm:
                continue
            realised = entry.sold_copper / entry.sales
            delta = (realised - row.price.recommended) / row.price.recommended if row.price.recommended else 0
            lines.append(f"| {entry.name[:30]} ({entry.item_id}) | {entry.realm} | "
                         f"{row.classification.profile} | "
                         f"{gold(row.price.recommended):,.0f} g | {gold(realised):,.0f} g | "
                         f"{delta:+.0%} |")
            matched += 1
            break
    if not matched:
        lines.append("| — | — | — | — | — | nenhuma venda do inventário cruzou a fila |")
    lines += [
        "",
        "## Como usar isto",
        "",
        "- Se o preço a que os itens vendem ficar **acima** do recomendado, o undercut "
        "está a ser mais agressivo do que a evidência justifica: alargar a banda.",
        "- Se ficar **abaixo** do piso, a perda está a ser real: reduzir o horizonte "
        "da campanha ou vender por destruição.",
        "- A taxa de venda observada por item é a única coisa que pode alimentar uma "
        "estimativa; enquanto um item tiver menos de "
        f"{CampaignConfig().min_attempts_for_rate} publicações, continua heurístico.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inventory", default=str(ROOT / "inventory.csv"))
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--realms", default=",".join(DEFAULT_REALMS),
                    help="reinos a avaliar, separados por vírgula")
    ap.add_argument("--days", type=int, default=12, help="duração da campanha")
    ap.add_argument("--days-remaining", type=int, default=None,
                    help="dias que faltam (para simular o meio da campanha)")
    ap.add_argument("--gold", type=int, default=213_246, help="ouro actual, em ouro")
    ap.add_argument("--token", type=int, default=398_720, help="preço do token, em ouro")
    ap.add_argument("--token-safe", type=int, default=420_000,
                    help="preço do token a usar no cálculo (o mais alto)")
    ap.add_argument("--target", type=int, default=200_000, help="alvo de ouro extra")
    ap.add_argument("--max-loss", type=float, default=0.35,
                    help="perda máxima admitida face ao custo conhecido")
    ap.add_argument("--min-price", type=float, default=0.0,
                    help="preço mínimo em ouro (0 = usa o valor de venda ao fornecedor)")
    ap.add_argument("--deadline-factor", type=float, default=0.90,
                    help="multiplicador do preço no último dia")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--export-dir", type=Path, default=EXPORT_DIR)
    ap.add_argument("--top", type=int, default=25, help="linhas na tabela do relatório")
    ap.add_argument("--write", action="store_true", help="grava o relatório e o CSV")
    ap.add_argument("--export", action="store_true",
                    help="grava os imports do TSM (liquidity_mogs_groups_<reino>.tsm)")
    ap.add_argument("--flipqueue", action="store_true",
                    help="grava a lista para importar no FlipQueue")
    ap.add_argument("--feedback", action="store_true", help="grava o feedback")
    args = ap.parse_args(argv)

    args.realms = [r.strip() for r in args.realms.split(",") if r.strip()]
    config = CampaignConfig(
        days=args.days,
        current_gold=args.gold * COPPER_PER_GOLD,
        token_price=args.token * COPPER_PER_GOLD,
        token_price_safe=args.token_safe * COPPER_PER_GOLD,
        target_gold=args.target * COPPER_PER_GOLD,
        min_price_copper=max(1, int(args.min_price * COPPER_PER_GOLD)),
        max_loss_pct=args.max_loss,
        deadline_price_factor=args.deadline_factor,
    )

    rows, postures, realm_scopes = build_rows(args, config)
    rows = prioritise(rows, config, days_remaining=args.days_remaining)

    observed_total = sum(r.expected for r in rows if r.expected is not None)
    heuristic_total = sum((r.expected if r.expected is not None
                           else r.potential_gold * EVIDENCE_WEIGHT[r.classification.profile])
                          for r in rows)
    state = campaign_state(config, days_remaining=args.days_remaining,
                           projected=heuristic_total)
    points = checkpoints(config)

    print(f"fila: {len(rows)} linhas em {len(args.realms)} reinos")
    print(f"défice: {gold_text(state.deficit)} | Projectado (heurístico): "
          f"{g_text(heuristic_total)} | com observação: {g_text(observed_total)}")
    for index, row in enumerate(rows[:5], 1):
        print(f"  {index}. {row.name[:36]:36s} {row.realm:16s} {row.classification.profile} "
              f"{gold(row.price.recommended):>12,.0f} g  {row.action[:40]}")

    if not (args.write or args.export or args.flipqueue or args.feedback):
        print("\n(modo verificação: usa --write/--export/--flipqueue/--feedback)",
              file=sys.stderr)
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.write:
        write_queue_csv(args.out_dir / "rush_queue.csv", rows)
        write_report(args.out_dir / "token_rush.md", args, config, rows, postures,
                     realm_scopes, state, points)
        print(f"-> {args.out_dir / 'token_rush.md'}", file=sys.stderr)
        print(f"-> {args.out_dir / 'rush_queue.csv'}", file=sys.stderr)
    if args.export:
        args.export_dir.mkdir(parents=True, exist_ok=True)
        for name in args.realms:
            subset = [r for r in rows if r.realm == name]
            if not subset:
                # an alias of a realm that was already written: same auction house
                continue
            path = args.export_dir / f"{LIQUIDITY_PREFIX}_{slugify(name)}.tsm"
            count = write_liquidity_export(path, subset)
            aliases = ", ".join(subset[0].aliases)
            print(f"-> {path} ({count} grupos/ops; cobre {aliases})", file=sys.stderr)
    if args.flipqueue:
        for name in args.realms:
            subset = [r for r in rows if r.realm == name]
            if not subset:
                continue
            path = args.out_dir / f"flipqueue_{slugify(name)}.csv"
            write_flipqueue_csv(path, subset)
            print(f"-> {path} ({len(subset)} linhas)", file=sys.stderr)
    if args.feedback:
        write_feedback(args.out_dir / "feedback.md", args, rows)
        print(f"-> {args.out_dir / 'feedback.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
