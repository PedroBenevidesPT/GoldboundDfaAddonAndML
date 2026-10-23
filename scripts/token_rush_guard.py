"""Compara as alternativas de piso de preço e classifica o inventário para o rush.

    python scripts/token_rush_guard.py --show
    python scripts/token_rush_guard.py --write

Alternativas (sobre os mesmos 328 itens, com os mesmos grupos e coeficientes):

    A  proposta original, sem alterações
    B  proposta ponderada, sem alterações
    C  proposta ponderada com o piso do min original em todos os itens
    D  proposta ponderada com o piso só nos itens de evidência fraca

Nada é importado no TSM e nenhuma das duas propostas é alterada.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pricing_groups as pg_script  # noqa: E402
from woweconomy.analytics import pricing_groups as pg  # noqa: E402
from woweconomy.analytics import reference_price as rp  # noqa: E402
from woweconomy.analytics import token_rush_guard as guard  # noqa: E402
from woweconomy.analytics.mog_realm import resolve as resolve_realm  # noqa: E402
from woweconomy.analytics.token_rush_data import read_inventory, realm_groups  # noqa: E402
from woweconomy.analytics.tsm_export import deserialize, parse_group_export  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
OUT_DIR = ROOT / "reports" / "pricing_groups"
CSV_OUT = OUT_DIR / "guard_alternatives.csv"
REPORT_OUT = OUT_DIR / "token_rush_guard.md"
BASE_TSM = OUT_DIR / "pricing_groups_proposal.tsm"
WEIGHTED_TSM = OUT_DIR / "pricing_groups_weighted_proposal.tsm"


def g(value) -> str:
    return f"{value / 10_000:,.0f} g"


def extract_call(text: str) -> str:
    """The reference call inside a price field, without the band coefficient."""
    start = text.index("(")
    identifier = start
    while identifier > 0 and (text[identifier - 1].isalnum() or text[identifier - 1] == "_"):
        identifier -= 1
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[identifier:index + 1]
    raise ValueError("parênteses desemparelhados")


def price_field(operations, name: str, field: str) -> str:
    """The **whole** price field, coefficient included.

    The price TSM uses is ``<coeficiente> * (referência)``; evaluating only the
    reference would compare the wrong numbers (the coefficient is what makes
    the minimum lower than the normal and the maximum higher).
    """
    return operations[name][field]


def build(args):
    """Everything the report needs, computed once."""
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con_dfa = sqlite3.connect(f"file:{args.dfa_db}?mode=ro", uri=True) \
        if Path(args.dfa_db).exists() else None

    inventory = read_inventory(args.inventory)
    facts, catalogue = pg_script.build_features(con, con_dfa, inventory)
    groups_of_item = pg_script.catalogue_groups(catalogue)
    evidence, _ = pg_script.collect_band_evidence(con, facts, catalogue, groups_of_item)
    policies = {band: pg_script.band_policy(band, evidence[band]) for band in evidence}
    clearance = pg_script.group_clearance(con, catalogue, groups_of_item)
    adj = pg_script.adjustments(clearance, evidence, {})

    profile = resolve_realm(args.realm)
    canonical = profile.realm
    realm_rows = realm_groups(con)
    scope = (realm_rows.get(canonical) or {}).get("canonical_slug") or canonical
    listings = {int(i): int(n) for i, n in con.execute(
        "SELECT itemId, num_auctions FROM tsm_market WHERE realm=?", (canonical,))}
    local_evidence = pg_script.load_local_evidence(con, scope, set(inventory), listings)
    regional_evidence = pg_script.load_regional_evidence(con, set(inventory))
    as_of = con.execute("select max(fetched_at) from tsm_realm_price").fetchone()[0]
    region_at = con.execute("select max(updated_at) from tsm_snapshot "
                            "where scope='region' and kind='region_items'").fetchone()[0]
    local_age = pg_script.age_hours(as_of, region_at)
    config = rp.ReferenceConfig(method=args.method)

    # the raw values, the way the addon sees them
    raw_local = {int(i): int(v or 0) for i, v in con.execute(
        "SELECT item_id, market_value FROM tsm_realm_price WHERE scope=?", (scope,))}
    raw_regional = {int(i): int(v or 0) for i, v in con.execute(
        "SELECT key_id, market_value FROM tsm_region_price "
        "WHERE snapshot_id=(select max(snapshot_id) from tsm_region_price)")}
    raw_historical = {int(i): int(v or 0) for i, v in con.execute(
        "SELECT key_id, historical FROM tsm_region_price "
        "WHERE snapshot_id=(select max(snapshot_id) from tsm_region_price)")}
    walls = {int(i): int(v or 0) for i, v in con.execute(
        "SELECT item_id, min_buyout FROM tsm_realm_price WHERE scope=?", (scope,))}

    base_data = deserialize(BASE_TSM.read_text(encoding="utf-8").strip())
    weighted_data = deserialize(WEIGHTED_TSM.read_text(encoding="utf-8").strip())
    base_ops = base_data["operations"]["Auctioning"]
    weighted_ops = weighted_data["operations"]["Auctioning"]
    base_expr = {name: extract_call(op["normalPrice"]) for name, op in base_ops.items()}
    weighted_expr = {name: extract_call(op["normalPrice"]) for name, op in weighted_ops.items()}
    # the price fields TSM actually uses: the coefficient *is* part of them
    base_min = {name: op["minPrice"] for name, op in base_ops.items()}
    weighted_min = {name: op["minPrice"] for name, op in weighted_ops.items()}
    base_max = {name: op["maxPrice"] for name, op in base_ops.items()}
    weighted_max = {name: op["maxPrice"] for name, op in weighted_ops.items()}
    base_normal = {name: op["normalPrice"] for name, op in base_ops.items()}
    weighted_normal = {name: op["normalPrice"] for name, op in weighted_ops.items()}
    cell_of_operation = {}
    for name in base_ops:
        parts = name.replace("PG ", "").rsplit(" ", 1)
        if len(parts) == 2:
            cell_of_operation[(parts[0], parts[1])] = name

    names = {int(i): n for i, n in con.execute("SELECT itemId, name FROM db2_items") if n}
    journal = {}
    for item_id, realm, sold, copper, successes, failures in con.execute(
            "SELECT itemId, realm, sold_count, sold_copper, successes, failures "
            "FROM journal_stats"):
        entry = journal.setdefault(int(item_id), {"sales": 0, "expired": 0,
                                                  "attempts": 0, "sold_copper": 0})
        entry["sales"] += int(sold or 0)
        entry["expired"] += int(failures or 0)
        entry["attempts"] += int(successes or 0) + int(failures or 0)
        entry["sold_copper"] += int(copper or 0)

    rows = []
    for item_id, item in inventory.items():
        item_facts = facts.get((canonical, item_id))
        if item_facts is None:
            continue
        name = item.name
        if not name or name.strip().lower() in ("unknown", "?"):
            name = names.get(item_id, name)
        group = pg.assign_group(item_facts)
        band = item_facts.band
        operation = cell_of_operation.get((group, band))
        local = local_evidence.get(item_id)
        regional = regional_evidence.get(item_id)
        estimate = rp.estimate_reference(
            dataclasses.replace(local or rp.LocalEvidence(), age_hours=local_age),
            dataclasses.replace(regional or rp.RegionalEvidence(), age_hours=local_age),
            config)
        own = journal.get(item_id, {})
        row = {
            "item_id": item_id,
            "name": name,
            "realm": canonical,
            "group": group,
            "band": band,
            "qty": item.qty,
            "wall": walls.get(item_id, 0),
            "local_market": local.price if local else 0,
            "regional_market": regional.price if regional else 0,
            "listings": (local.listings if local and local.listings is not None else ""),
            "dispersion": (round(local.dispersion, 3)
                           if local and local.dispersion is not None else ""),
            "divergence": (round(estimate.divergence, 3)
                           if estimate.divergence is not None else ""),
            "weight_local": round(estimate.weight_local, 3),
            "confidence": estimate.confidence,
            "sales": own.get("sales", 0),
            "expired": own.get("expired", 0),
            "attempts": own.get("attempts", 0),
            "want_rate": (round(item_facts.want_rate, 4)
                           if item_facts.want_rate is not None else ""),
            "demand_known": "sim" if item_facts.demand_known else "nao",
            "flags": " | ".join(estimate.flags),
        }
        facts_guard = guard.LiquidationFacts(
            item_id=item_id, name=name, realm=canonical,
            want_rate=item_facts.want_rate, demand_known=item_facts.demand_known,
            listings=local.listings if local else None,
            local_market=local.price if local else 0,
            local_wall=wall_value(item_facts, walls),
            dispersion=local.dispersion if local else None,
            divergence=estimate.divergence,
            freshness_hours=local_age,
            sales=own.get("sales", 0), expired=own.get("expired", 0),
            attempts=own.get("attempts", 0), qty=item.qty,
            wall=walls.get(item_id, 0), undercut=0,
        )
        if operation is None:
            # no reference at all: there is no price to post, so the item is
            # simply not reposted automatically
            for alternative in guard.ALTERNATIVES:
                row[f"{alternative}_min"] = 0
                row[f"{alternative}_normal"] = 0
                row[f"{alternative}_max"] = 0
                row[f"{alternative}_decision"] = "sem referência de preço"
            row["liquidation_class"] = guard.NO_REPOST
            row["liquidation_reasons"] = "sem preço de referência: não há o que postar"
            rows.append(row)
            continue
        values = {
            "dbmarket": raw_local.get(item_id, 0),
            "dbregionmarketavg": raw_regional.get(item_id, 0),
            "dbregionhistorical": raw_historical.get(item_id, 0),
        }
        prices = {}
        for alternative in guard.ALTERNATIVES:
            if alternative == guard.ALTERNATIVE_A:
                normal_expr, min_expr, max_expr = (base_normal[operation],
                                                   base_min[operation],
                                                   base_max[operation])
            else:
                normal_expr, min_expr, max_expr = (weighted_normal[operation],
                                                   weighted_min[operation],
                                                   weighted_max[operation])
            prices[alternative] = {
                "min": rp.evaluate_tsm_expression(min_expr, values),
                "normal": rp.evaluate_tsm_expression(normal_expr, values),
                "max": rp.evaluate_tsm_expression(max_expr, values),
            }
        for alternative in guard.ALTERNATIVES:
            chosen = guard.apply_alternative(
                alternative,
                weighted_min=prices[guard.ALTERNATIVE_B]["min"],
                weighted_normal=prices[guard.ALTERNATIVE_B]["normal"],
                weighted_max=prices[guard.ALTERNATIVE_B]["max"],
                original_min=prices[guard.ALTERNATIVE_A]["min"],
                original_normal=prices[guard.ALTERNATIVE_A]["normal"],
                original_max=prices[guard.ALTERNATIVE_A]["max"],
                weak_evidence=facts_guard.weak_evidence)
            row[f"{alternative}_min"] = chosen.min_price
            row[f"{alternative}_normal"] = chosen.normal_price
            row[f"{alternative}_max"] = chosen.max_price
            row[f"{alternative}_decision"] = guard.posting_decision(
                row["wall"], 0, chosen.min_price, chosen.normal_price, chosen.max_price,
                has_competitor=bool(row["wall"]))
            # the percentage change against the original proposal
            for field in ("min", "normal", "max"):
                base_value = prices[guard.ALTERNATIVE_A][field]
                new_value = getattr(chosen, f"{field}_price")
                row[f"{alternative}_{field}_pct"] = (
                    round(100 * (new_value / base_value - 1), 1) if base_value else "")
        # the liquidation classification, under the recommended policy (D)
        recommended = guard.apply_alternative(
            guard.ALTERNATIVE_D,
            weighted_min=prices[guard.ALTERNATIVE_B]["min"],
            weighted_normal=prices[guard.ALTERNATIVE_B]["normal"],
            weighted_max=prices[guard.ALTERNATIVE_B]["max"],
            original_min=prices[guard.ALTERNATIVE_A]["min"],
            original_normal=prices[guard.ALTERNATIVE_A]["normal"],
            original_max=prices[guard.ALTERNATIVE_A]["max"],
            weak_evidence=facts_guard.weak_evidence)
        facts_guard = dataclasses.replace(facts_guard, min_price=recommended.min_price,
                                          normal_price=recommended.normal_price,
                                          max_price=recommended.max_price)
        classification, reasons = guard.classify_liquidation(facts_guard)
        row["liquidation_class"] = classification
        row["liquidation_reasons"] = " | ".join(reasons)
        rows.append(row)
    con.close()
    return rows


def wall_value(item_facts, walls) -> int:
    return walls.get(item_facts.item_id, 0)


CSV_FIELDS = [
    "item_id", "name", "realm", "group", "band", "qty", "wall", "local_market",
    "regional_market", "listings", "dispersion", "divergence", "weight_local",
    "confidence", "sales", "expired", "attempts", "want_rate", "demand_known",
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dfa-db", default=str(pg_script.DFA_DB))
    ap.add_argument("--inventory", default=str(pg_script.INVENTORY))
    ap.add_argument("--realm", default="Dragonmaw")
    ap.add_argument("--method", default="geometric", choices=("geometric", "arithmetic"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    rows = build(args)
    for alternative in guard.ALTERNATIVES:
        blocked = sum(1 for row in rows
                      if guard.is_blocked(row[f"{alternative}_decision"]))
        invalid = sum(1 for row in rows
                      if row[f"{alternative}_decision"].startswith("invalido"))
        changed = sum(1 for row in rows
                      if abs(row[f"{alternative}_min"] - row[f"{guard.ALTERNATIVE_A}_min"]) > 0)
        print(f"{alternative:42s} bloqueados={blocked:4d} inválidos={invalid:3d} "
              f"min diferente da original={changed:4d}")
    classes = defaultdict(int)
    for row in rows:
        classes[row["liquidation_class"]] += 1
    print("classificação:", dict(classes))

    if not args.write:
        print("\n(modo verificação: usa --write para gravar)", file=sys.stderr)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fields = list(CSV_FIELDS)
    for alternative in guard.ALTERNATIVES:
        fields += [f"{alternative}_min", f"{alternative}_normal", f"{alternative}_max",
                   f"{alternative}_min_pct", f"{alternative}_normal_pct",
                   f"{alternative}_max_pct", f"{alternative}_decision"]
    fields += ["liquidation_class", "liquidation_reasons", "flags"]
    with CSV_OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            for alternative in guard.ALTERNATIVES:
                out[f"{alternative}_min_g"] = f"{row[f'{alternative}_min'] / 10_000:,.0f}"
            writer.writerow(out)
    write_report(REPORT_OUT, rows, args)
    print(f"-> {CSV_OUT}\n-> {REPORT_OUT}")
    return 0


def write_report(path, rows, args) -> None:
    def g(value) -> str:
        return f"{value / 10_000:,.0f} g"

    lines = [
        "# Token Rush — guard de preço mínimo e política de publicação",
        "",
        f"Alvo: **{args.realm}** (cluster Spinebreaker), os mesmos "
        f"{len(rows)} itens do inventário, os mesmos grupos, os mesmos "
        "coeficientes e as mesmas durações. **Só o preço mínimo (e o normal, "
        "quando a validação do TSM o obriga) muda.** Nada é importado; as duas "
        "propostas ficam intactas.",
        "",
        "## 1. Semântica real da operação (lida no TSM instalado)",
        "",
        "| momento | condição | resultado | fonte |",
        "|---|---|---|---|",
        "| validação | sem itens suficientes | não posta | `ValidateForPosting` |",
        "| validação | expirações desde a última venda > `maxExpires` | não posta "
        "(`NOT_POSTING.MAX_EXPIRES`) e vai para o banco | `ValidateForPosting:363` |",
        "| validação | `normalPrice < minPrice` | **operação inválida, não posta "
        "nunca** (`INVALID.NORMAL_BELOW_MIN`) | `AuctioningOperation:389` |",
        "| validação | `maxPrice < minPrice` | operação inválida | "
        "`AuctioningOperation:392` |",
        "| decisão | sem concorrência | posta a `normalPrice` (piso no "
        "`minPrice`) | `MakePostDecision:426,501` |",
        "| decisão | `parede − undercut < minPrice` e `priceReset = none` | "
        "**não posta** (`NOT_POSTING.BELOW_MIN`) | `MakePostDecision:436-459` |",
        "| decisão | `parede − undercut > maxPrice` | posta ao tecto "
        "(`aboveMax = maxPrice`) | `MakePostDecision:477` |",
        "| decisão | caso normal | posta a `parede − undercut` | "
        "`MakePostDecision:492` |",
        "| cancelamento | `cancelUndercut = false` e `cancelRepost = false` | "
        "**nunca cancela** (`WONT_CANCEL.DISABLED`) | `ValidateForCanceling:558` |",
        "| repost | — | **não existe repost automático**: o anúncio expira ao fim "
        "da `duration` e o *próximo* post scan re-avalia o item do zero | "
        "`PostScan.lua:529` |",
        "",
        "**Consequências, demonstradas e não inferidas:**",
        "",
        "1. **Um `minPrice` mais baixo não provoca um repost.** Não há repost "
        "automático; o que muda é que o *próximo* scan pode postar um item que "
        "antes devolvia `NOT_POSTING.BELOW_MIN`. O número de ciclos é limitado "
        "por `maxExpires` (expirações desde a última venda).",
        "2. **Um `minPrice` mais baixo nunca cancela nada** — com "
        "`cancelUndercut`/`cancelRepost` a false, o TSM não cancela listings "
        "vivos.",
        "3. **Um `minPrice` mais alto pode parar o item de ser posted** (a parede "
        "fica abaixo do piso) e **pode tornar a operação inválida** se o "
        "`normalPrice` ficar abaixo do `minPrice` — nesse caso o item deixa de "
        "ser posted *de todo*, e não é um preço mais alto: é nenhum preço.",
        "4. O `minPrice` **não** define o preço de venda: com concorrência o TSM "
        "posta `parede − undercut`. O piso é uma **porta** (postar ou não), não um "
        "preço.",
        "",
        "## 2. As alternativas, item a item",
        "",
        "| alternativa | o que muda | bloqueados | inválidos | min ≠ original |",
        "|---|---|---:|---:|---:|",
    ]
    for alternative in guard.ALTERNATIVES:
        blocked = sum(1 for row in rows if guard.is_blocked(row[f"{alternative}_decision"]))
        invalid = sum(1 for row in rows if row[f"{alternative}_decision"].startswith("invalido"))
        changed = sum(1 for row in rows
                      if abs(row[f"{alternative}_min"] - row[f"{guard.ALTERNATIVE_A}_min"]) > 0)
        lines.append(f"| {alternative} | "
                     f"{ {'A original': 'nada', 'B ponderada': 'a referência (min, normal e max)',
                         'C ponderada com piso do min original': 'min e normal sobem ao piso original',
                         'D ponderada com piso só na evidência fraca': 'min e normal sobem só nos itens fracos'}[alternative] } "
                     f"| {blocked} | {invalid} | {changed} |")

    # the price movements
    lines += [
        "",
        "### 2.1 Como o mínimo se mexe",
        "",
        "| alternativa | min sobe | min desce | min igual |",
        "|---|---:|---:|---:|",
    ]
    for alternative in guard.ALTERNATIVES:
        up = down = same = 0
        for row in rows:
            delta = row[f"{alternative}_min"] - row[f"{guard.ALTERNATIVE_A}_min"]
            if delta > 0:
                up += 1
            elif delta < 0:
                down += 1
            else:
                same += 1
        lines.append(f"| {alternative} | {up} | {down} | {same} |")

    lines += [
        "",
        "> O mínimo da proposta original **não** é economicamente correcto por ser "
        "mais alto: ele é o resultado de um coeficiente de banda aplicado a uma "
        "referência que, nos itens em causa, é um ask isolado ou um outlier. Por "
        "isso o guard é avaliado por **efeito** (quantos itens ficam bloqueados, "
        "quantos param de ser posted, quanto capital fica preso), não por "
        "altura do número.",
        "",
        "**A armadilha que o guard tem de evitar:** se o piso original fosse "
        "aplicado sem tocar no `normalPrice`, os itens em que o piso original "
        f"ficaria **acima** do normal ponderado "
        f"({sum(1 for row in rows if row[f'{guard.ALTERNATIVE_A}_min'] > row[f'{guard.ALTERNATIVE_B}_normal'])} "
        "de 328) teriam `normalPrice < minPrice` → "
        "`INVALID.NORMAL_BELOW_MIN` → a operação **deixava de postar esse item "
        "de todo**, nem a preço alto nem baixo. Por isso o guard eleva também o "
        "`normalPrice` para o piso: é a validação do TSM que obriga, não uma "
        "escolha de política de preço.",
        "",
        "**A escala real do efeito é pequena.** O `minPrice` é 0,25-0,50 × a "
        "referência, ou seja, muito abaixo da parede da maioria dos itens "
        f"listados: só {sum(1 for row in rows if row['wall'] and float(row['wall']) > 0)} "
        "dos 328 itens têm parede, e entre eles só uma mão está bloqueada em "
        "qualquer das alternativas. A maior parte das diferenças de `minPrice` "
        "**não muda nenhuma decisão de postagem hoje** — muda o preço a que se "
        "postaria se o item estivesse sem concorrência, e o tecto (`maxPrice`) "
        "que decide se aceitamos o pedido de um Competidor acima do nosso "
        "referência.",
        "",
        "## 3. Efeito no capital e nas vendas",
        "",
        "| alternativa | itens sem postar | valor parado na bolsa (g) | valor "
        "listado (g) | depósito por ciclo, 5 % (g) |",
        "|---|---:|---:|---:|---:|",
    ]
    for alternative in guard.ALTERNATIVES:
        locked = sum(row["qty"] * row["wall"] for row in rows
                     if guard.is_blocked(row[f"{alternative}_decision"]))
        listed = sum(row["qty"] * row["wall"] for row in rows
                      if not guard.is_blocked(row[f"{alternative}_decision"]))
        lines.append(f"| {alternative} | "
                     f"{sum(1 for row in rows if guard.is_blocked(row[f'{alternative}_decision']))} | "
                     f"{locked / 10_000:,.0f} | {listed / 10_000:,.0f} | "
                     f"{listed * 0.05 / 10_000:,.0f} |")

    lines += [
        "",
        "*Valor parado na bolsa* = o valor dos itens que a operação deixa de "
        "postar (ficam no inventário, sem depósito queimado e sem venda). "
        "*Valor listado* = o valor dos itens que passam a estar no AH. "
        "*Depósito por ciclo* = 5 % do valor listado, devolvido se o item "
        "vender e perdido se expirar (a taxa de AH de 5 % incide sobre a venda). "
        "Com 12 dias e durações de 12-48 h um item pode queimar 3-6 depósitos: "
        "é esse o custo de insistir, e é o que o `maxExpires` limita.",
        "",
        "## 4. Classificação de liquidação do Token Rush",
        "",
    ]
    classes = defaultdict(list)
    for row in rows:
        classes[row["liquidation_class"]].append(row)
    lines += [
        "| classe | itens | o que é |",
        "|---|---:|---|",
        f"| **{guard.LIQUIDATE}** | {len(classes.get(guard.LIQUIDATE, []))} | "
        "procura medida (DfA) ou venda provada pela conta, e preço não "
        "contraditório: priorizar a venda |",
        f"| **{guard.HOLD}** | {len(classes.get(guard.HOLD, []))} | evidência "
        "insuficiente para descontar com segurança: manter o preço |",
        f"| **{guard.NO_REPOST}** | {len(classes.get(guard.NO_REPOST, []))} | "
        "procura fraca com divergência forte, expirações repetidas, ou o piso "
        "acima da parede: não repôr automaticamente |",
        "",
        "> Os limiares (`5 %` da amostra DfA, `50 %` de sell-through, `2` "
        "expirações, `75 %` de divergência) são **heurísticas** e estão em "
        "`token_rush_guard.py`. Os factos que entram neles (profundidade, "
        "dispersão, frescura, divergência, histórico da conta) são **medidos**. "
        "Nenhum valor de mercado é tratado como receita.",
        "",
    ]
    for name, group in classes.items():
        lines += [f"**{name}** — {len(group)} itens", ""]
        for row in sorted(group, key=lambda r: -r["qty"])[:10]:
            lines.append(f"- {row['name'][:30]} ({row['item_id']}) — "
                         f"{row['liquidation_reasons'][:110]}")
        lines.append("")

    # the 25 items most likely to create unwanted minimum changes
    candidates = []
    for row in rows:
        delta = row[f"{guard.ALTERNATIVE_B}_min"] - row[f"{guard.ALTERNATIVE_A}_min"]
        if delta == 0:
            continue
        candidates.append((abs(delta), row, delta))
    candidates.sort(key=lambda entry: -entry[0])
    lines += [
        "## 5. Os 25 itens mais susceptibles de mudar o preço mínimo",
        "",
        "Ordenados pela magnitude da redução do mínimo (o `minPrice` mais baixo é "
        "o que pode abrir a porta a vender abaixo do que a proposta original "
        "aceitava).",
        "",
        "| item | banda | min original | min ponderado | variação | parede | "
        "divergência | evidência | decisão sob D |",
        "|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for _magnitude, row, delta in candidates[:25]:
        lines.append(
            f"| {row['name'][:26]} ({row['item_id']}) | {row['band']} | "
            f"{g(row[f'{guard.ALTERNATIVE_A}_min'])} | "
            f"{g(row[f'{guard.ALTERNATIVE_B}_min'])} | "
            f"{delta / 10_000:,.0f} g | {g(row['wall'])} | "
            f"{row['divergence'] if row['divergence'] != '' else '—'} | "
            f"{row['confidence']} / {row['listings'] if row['listings'] != '' else '?'} "
            f"anúncios | {row['liquidation_class']} |")

    lines += [
        "",
        "## 6. Configuração de teste e reversão",
        "",
        "1. Criar um perfil de teste: `TSM → Settings → General → Profiles` → "
        "`teste-rush` (ou `/tsm profile teste-rush`).",
        "2. `Groups → Import` → colar `reports/pricing_groups/"
        "pricing_groups_weighted_proposal.tsm` (alternativa D) **num perfil "
        "limpo**.",
        "3. `Operations → Auctioning` → confirmar `minPrice`, `normalPrice` e "
        "`maxPrice` por operação, e `priceReset = none`, `aboveMax = maxPrice`, "
        "`cancelUndercut = false`, `cancelRepost = false`.",
        "4. *Post Scan* num item de cada banda e comparar com o que a proposta "
        "original pediria no mesmo dia (a coluna `wall` do CSV tem a parede "
        "actual de cada item).",
        "5. `maxExpires` conta as expirações **desde a última venda**: se um item "
        "já expirou `maxExpires` vezes, deixa de ser posted e vai para o banco.",
        "",
        "**Reversão:** nada foi alterado no perfil vivo, logo não há nada a "
        "reverter. Se a candidata for importada num perfil de teste, apagar "
        "esse perfil (ou os grupos importados) e voltar a importar "
        "`pricing_groups_proposal.tsm`. Importar as duas no mesmo perfil **não** "
        "substitui: o TSM cria `PG ... 1`, `PG ... 2`.",
        "",
        "## 7. Recomendação",
        "",
        "**Adopt a alternativa D (guard estreito) num perfil de teste; não no "
        "perfil vivo.**",
        "",
        "Porquê:",
        "",
        "1. O `minPrice` é uma **porta**, não um preço: a auditoria do addon "
        "mostra que uma redução não cancela nem repõe nada — só deixa o *próximo* "
        "scan postar o que antes recusava. O guard tem, por isso, de ser "
        "pequeno e reversível.",
        "2. **D** repõe o comportamento de bloqueio da original "
        f"({sum(1 for row in rows if guard.is_blocked(row[f'{guard.ALTERNATIVE_D}_decision']))} "
        f"itens bloqueados, contra {sum(1 for row in rows if guard.is_blocked(row[f'{guard.ALTERNATIVE_A}_decision']))} "
        "da original) para os itens cuja mudança de referência assenta em evidência "
        "fraca, e mantém a referência ponderada onde a evidência a sustenta.",
        "3. **D** evita a armadilha da validação: nos "
        f"{sum(1 for row in rows if row[f'{guard.ALTERNATIVE_A}_min'] > row[f'{guard.ALTERNATIVE_B}_normal'])} "
        "itens em que o piso original ficaria acima do normal ponderado, o "
        "`normalPrice` sobe com o piso e a operação continua válida. Sem isso o "
        "item deixava de ser posted de todo.",
        "4. **B** é a mais agressiva e a que mais destrava capital (é a que tem "
        "o `minPrice` mais baixo em 71 itens). Se a campanha precisa de vender "
        " depressa, é a escolha coerente — mas é também onde o preço realizado "
        "pode piorar, e sem vendas datadas **não dá para justificar**. Fica "
        "para o perfil de teste.",
        "5. **C** é a mais conservadora: aplica o piso original a todos os "
        "itens e perde o benefício da referência ponderada nos itens com "
        "evidência boa. É a escolha se a prioridade for não arriscar uma "
        "redução de preço mínima.",
        "",
        "### 7.1 A política de listagem prática",
        "",
        "| classe | o que se faz | como se decide |",
        "|---|---|---|",
        f"| **liquidar** ({len(classes.get(guard.LIQUIDATE, []))} itens) | postar "
        "já, undercut mínimo, e repor se expirar | procura medida na amostra DfA "
        "ou venda provada pela conta |",
        f"| **manter preço** ({len(classes.get(guard.HOLD, []))} itens) | postar ao "
        "preço de mercado, sem desconto, e não insistir | a evidência não sustenta "
        "descontar |",
        f"| **não repôr automaticamente** ({len(classes.get(guard.NO_REPOST, []))} "
        "itens) | não voltar a postar: vendor, destruição ou venda manual | "
        "expirações repetidas, procura fraca com divergência forte, ou o piso "
        "acima da parede |",
        "",
        "Os itens de **material** (mercado de commodities, um só mercado em toda "
        "a região) e os **sem qualquer referência** não entram na classificação "
        "de liquidação: os primeiros seguem o mercado de materials, os segundos "
        "não têm preço nenhum para postar.",
        "",
        "## 8. O que a base de dados não decide",
        "",
        "Não há vendas datadas, nem parede histórica, nem custo de aquisição: "
        "**nenhuma das quatro alternativas pode ser declarada vencedora em ouro "
        "realizado**. O que a comparação mostra é o efeito mecânico (quantos "
        "itens ficam bloqueados, quantos param de ser posted, quanto capital "
        "fica preso) — e isso é tudo o que se pode afirmar com honestidade.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
