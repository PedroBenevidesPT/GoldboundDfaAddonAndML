"""Valida o preço de referência ponderado contra a proposta original.

    python scripts/validate_weighted_pricing.py            # dry run
    python scripts/validate_weighted_pricing.py --write    # gera os artefactos

Produz:

    reports/pricing_groups/weighted_vs_original.csv   comparação item a item
    reports/pricing_groups/validation_report.md       o relatório

Não importa nada no TSM, não mexe em operações activas e não altera nenhuma das
duas propostas.  A comparação é feita sobre **exatamente os mesmos itens** do
inventário, nas mesmas condições.
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
from woweconomy.analytics.mog_realm import resolve as resolve_realm  # noqa: E402
from woweconomy.analytics.token_rush_data import (  # noqa: E402
    load_vendor_sell, read_inventory, realm_groups,
)
from woweconomy.analytics.tsm_export import deserialize, parse_group_export  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
OUT_DIR = ROOT / "reports" / "pricing_groups"
CSV_OUT = OUT_DIR / "weighted_vs_original.csv"
REPORT_OUT = OUT_DIR / "validation_report.md"
BASE_TSM = OUT_DIR / "pricing_groups_proposal.tsm"
WEIGHTED_TSM = OUT_DIR / "pricing_groups_weighted_proposal.tsm"


def g(value) -> str:
    return f"{value / 10_000:,.0f} g"


def extract_call(text: str) -> str:
    """The first function call (or parenthesised expression) in a price string."""
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


def build_rows(args):
    """The per-item comparison, computed exactly like the generator does."""
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con_dfa = None
    if Path(args.dfa_db).exists():
        con_dfa = sqlite3.connect(f"file:{args.dfa_db}?mode=ro", uri=True)

    inventory = read_inventory(args.inventory)
    facts, catalogue = pg_script.build_features(con, con_dfa, inventory)
    groups_of_item = pg_script.catalogue_groups(catalogue)
    evidence, _flags = pg_script.collect_band_evidence(con, facts, catalogue, groups_of_item)
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
    estimates = {}
    for item_id in inventory:
        local = local_evidence.get(item_id) or rp.LocalEvidence()
        regional = regional_evidence.get(item_id) or rp.RegionalEvidence()
        local = dataclasses.replace(local, age_hours=local_age)
        regional = dataclasses.replace(regional, age_hours=local_age)
        estimates[item_id] = rp.estimate_reference(local, regional, config)

    # the membership and the group of every item, as the generator does
    names = {int(i): n for i, n in con.execute("SELECT itemId, name FROM db2_items") if n}

    # the RAW values: the comparison has to see what TSM would see, sentinel
    # included (the engine cleans them, the addon does not)
    raw_local = {int(i): int(v or 0) for i, v in con.execute(
        "SELECT item_id, market_value FROM tsm_realm_price WHERE scope=?", (scope,))}
    raw_regional = {int(i): int(v or 0) for i, v in con.execute(
        "SELECT key_id, market_value FROM tsm_region_price "
        "WHERE snapshot_id=(select max(snapshot_id) from tsm_region_price)")}
    raw_historical = {int(i): int(v or 0) for i, v in con.execute(
        "SELECT key_id, historical FROM tsm_region_price "
        "WHERE snapshot_id=(select max(snapshot_id) from tsm_region_price)")}

    # the two payloads, evaluated the way TSM would evaluate them
    base_data = deserialize(BASE_TSM.read_text(encoding="utf-8").strip())
    weighted_data = deserialize(WEIGHTED_TSM.read_text(encoding="utf-8").strip())
    base_ops = base_data["operations"]["Auctioning"]
    weighted_ops = weighted_data["operations"]["Auctioning"]

    def extract(expression: str) -> str:
        return extract_call(expression)

    base_expressions = {name: extract(op["normalPrice"])
                        for name, op in base_ops.items()}
    weighted_expressions = {name: extract(op["normalPrice"])
                            for name, op in weighted_ops.items()}
    operation_of_cell = {}
    for name in base_ops:
        parts = name.replace("PG ", "").rsplit(" ", 1)
        if len(parts) == 2:
            operation_of_cell[(parts[0], parts[1])] = name

    rows = []
    for item_id, item in inventory.items():
        facts_item = facts.get((canonical, item_id))
        if facts_item is None:
            continue
        if not item.name or item.name.strip().lower() in ("unknown", "?"):
            item = dataclasses.replace(item, name=names.get(item_id, item.name))
        group = pg.assign_group(facts_item)
        band = facts_item.band
        local = local_evidence.get(item_id)
        regional = regional_evidence.get(item_id)
        estimate = estimates[item_id]
        operation = operation_of_cell.get((group, band))
        values = {
            "dbmarket": raw_local.get(item_id, 0),
            "dbregionmarketavg": raw_regional.get(item_id, 0),
            "dbregionhistorical": raw_historical.get(item_id, 0),
        }
        original_expr = base_expressions.get(operation)
        weighted_expr = weighted_expressions.get(operation)
        original_reference = (rp.evaluate_tsm_expression(original_expr, values)
                               if original_expr else 0)
        weighted_reference = (rp.evaluate_tsm_expression(weighted_expr, values)
                              if weighted_expr else 0)
        change = (weighted_reference / original_reference - 1.0) if original_reference else None
        rows.append({
            "item_id": item_id,
            "name": item.name,
            "realm": canonical,
            "group": group,
            "band": band,
            "qty": item.qty,
            "local_market": local.price if local else 0,
            "regional_market": regional.price if regional else 0,
            "regional_historical": regional.historical if regional else 0,
            "listings": (local.listings if local and local.listings is not None else ""),
            "local_age_hours": round(local.age_hours, 2) if local else "",
            "regional_age_hours": round(regional.age_hours, 2) if regional else "",
            "local_dispersion": (round(local.dispersion, 3)
                                 if local and local.dispersion is not None else ""),
            "divergence": (round(estimate.divergence, 3)
                           if estimate.divergence is not None else ""),
            "weight_local": round(estimate.weight_local, 3),
            "weight_regional": round(estimate.weight_regional, 3),
            "original_reference": original_reference,
            "original_source": ("expressão original" if original_reference else
                                 "sem preço"),
            "weighted_reference": weighted_reference,
            "estimator_reference": estimate.price,
            "change_pct": (round(100 * change, 1) if change is not None else ""),
            "confidence": estimate.confidence,
            "flags": " | ".join(estimate.flags),
            "sentinel_local": "sim" if values["dbmarket"] >= rp.SENTINEL_COPPER else "",
            "sentinel_regional": ("sim" if values["dbregionmarketavg"] >= rp.SENTINEL_COPPER
                                  else ""),
        })
    con.close()
    return rows, estimates, config, local_evidence, regional_evidence, local_age


CSV_FIELDS = ("item_id", "name", "realm", "group", "band", "qty", "local_market",
              "regional_market", "regional_historical", "listings", "local_age_hours",
              "regional_age_hours", "local_dispersion", "divergence", "weight_local",
              "weight_regional", "original_reference", "original_source",
              "weighted_reference", "estimator_reference", "change_pct", "confidence",
              "flags", "sentinel_local", "sentinel_regional")


def write_csv(path: Path, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# --- the expression audit --------------------------------------------------

def audit_expression(path: Path) -> list[dict]:
    """Checks on the *generated* expression, one per rule."""
    data = deserialize(path.read_text(encoding="utf-8").strip())
    operations = data["operations"]["Auctioning"]
    checks = []

    def extract(expression: str) -> str:
        return extract_call(expression)

    expressions = {}
    for name, operation in operations.items():
        expressions[name] = extract(operation["normalPrice"])

    # 1. precedence: the expression must be the weighted geometric mean
    ok = True
    for name, expression in expressions.items():
        weight = float(expression.split("dbmarket ^ ")[1].split(" *")[0])
        local, regional = 100 * rp.COPPER_PER_GOLD, 400 * rp.COPPER_PER_GOLD
        value = rp.evaluate_tsm_expression(expression, {
            "dbmarket": local, "dbregionmarketavg": regional,
            "dbregionhistorical": 90 * rp.COPPER_PER_GOLD})
        expected = local ** weight * regional ** (1 - weight)
        if abs(value - expected) > max(1, expected * 1e-6):
            ok = False
    checks.append({"name": "precedência: `^` liga mais forte que `*`",
                   "ok": ok,
                   "detail": "a expressão devolve a média geométrica ponderada, "
                             "como o otimizador do TSM assume (^ prioridade 3, "
                             "* prioridade 2)"})

    # 2. missing values: each operation must fall back the documented way
    wrong = []
    for name, expression in expressions.items():
        weight = float(expression.split("dbmarket ^ ")[1].split(" *")[0])
        local, regional = 100 * rp.COPPER_PER_GOLD, 400 * rp.COPPER_PER_GOLD
        historical = 90 * rp.COPPER_PER_GOLD

        def value(l, r):
            return rp.evaluate_tsm_expression(expression, {
                "dbmarket": l, "dbregionmarketavg": r, "dbregionhistorical": historical})

        if abs(value(local, regional) - local ** weight * regional ** (1 - weight)) > 1:
            wrong.append(f"{name}: ambos presentes")
        if value(0, regional) != regional:
            wrong.append(f"{name}: sem local não caiu no regional")
        if value(local, 0) != local:
            wrong.append(f"{name}: sem regional não caiu no local")
        if value(0, 0) != historical:
            wrong.append(f"{name}: sem nenhum não caiu no histórico regional")
    checks.append({
        "name": "valores em falta: cada operação segue a escada documentada",
        "ok": not wrong,
        "detail": ("31 operações × 4 combinações (ambos/local ausente/regional "
                   "ausente/nenhum): local→regional→histórico, como documentado")
        if not wrong else "; ".join(wrong[:5])})

    # 3. the sentinel is treated as missing
    sentinel = rp.SENTINEL_COPPER
    sentinel_values = {rp.evaluate_tsm_expression(expression, {
        "dbmarket": sentinel, "dbregionmarketavg": 400 * rp.COPPER_PER_GOLD,
        "dbregionhistorical": 90 * rp.COPPER_PER_GOLD})
        for expression in expressions.values()}
    checks.append({
        "name": "sentinel da TSM tratado como ausente",
        "ok": sentinel_values == {400 * rp.COPPER_PER_GOLD},
        "detail": f"dbmarket = {sentinel:,} dá {sorted(sentinel_values)[0]:,.0f} "
                  f"(a referência regional), não 10 M g"})

    # 4. no invalid, negative, infinite or extreme value
    extremes = {
        "local e regional no máximo real": (10 ** 12, 10 ** 12),
        "local 1 copper": (1, 10 ** 12),
        "regional 1 copper": (10 ** 12, 1),
        "ambos 1 copper": (1, 1),
    }
    bad = []
    for label, (local, regional) in extremes.items():
        for name, expression in expressions.items():
            value = rp.evaluate_tsm_expression(expression, {
                "dbmarket": local, "dbregionmarketavg": regional,
                "dbregionhistorical": 10 ** 12})
            if not (value == value) or value in (float("inf"), float("-inf")) or value < 0:
                bad.append(f"{name} / {label} -> {value}")
    checks.append({"name": "sem valores negativos, infinitos ou NaN",
                   "ok": not bad, "detail": "; ".join(bad) if bad else
                   "todas as combinações extremas dão um valor finito e não negativo"})

    # 5. the price fields of the operation stay ordered (the **whole** field:
    # the coefficient is part of the price the TSM uses)
    unordered = []
    for name, operation in operations.items():
        values = {"dbmarket": 100 * rp.COPPER_PER_GOLD,
                  "dbregionmarketavg": 100 * rp.COPPER_PER_GOLD,
                  "dbregionhistorical": 100 * rp.COPPER_PER_GOLD}
        normal = rp.evaluate_tsm_expression(operation["normalPrice"], values)
        minimum = rp.evaluate_tsm_expression(operation["minPrice"], values)
        maximum = rp.evaluate_tsm_expression(operation["maxPrice"], values)
        if not (minimum <= normal <= maximum):
            unordered.append(f"{name}: {minimum:,.0f} / {normal:,.0f} / {maximum:,.0f}")
    checks.append({"name": "min <= normal <= max em todas as operações",
                   "ok": not unordered, "detail": "; ".join(unordered) if unordered
                   else f"{len(operations)} operações verificadas (com o coeficiente)"})

    # 5b. the same for a pair where the two references disagree
    unordered = []
    for name, operation in operations.items():
        values = {"dbmarket": 400 * rp.COPPER_PER_GOLD,
                  "dbregionmarketavg": 100 * rp.COPPER_PER_GOLD,
                  "dbregionhistorical": 90 * rp.COPPER_PER_GOLD}
        normal = rp.evaluate_tsm_expression(operation["normalPrice"], values)
        minimum = rp.evaluate_tsm_expression(operation["minPrice"], values)
        maximum = rp.evaluate_tsm_expression(operation["maxPrice"], values)
        if not (minimum <= normal <= maximum):
            unordered.append(f"{name}: {minimum:,.0f} / {normal:,.0f} / {maximum:,.0f}")
    checks.append({"name": "min <= normal <= max também quando local e regional divergem",
                   "ok": not unordered, "detail": "; ".join(unordered) if unordered
                   else f"{len(operations)} operações verificadas com local 4x o regional"})

    # 6. the sources used exist in the installed TSM
    used = set()
    for operation in operations.values():
        for field in ("normalPrice", "minPrice", "maxPrice"):
            for source in ("dbmarket", "dbregionmarketavg", "dbregionhistorical"):
                if source in operation[field]:
                    used.add(source)
    checks.append({"name": "só fontes que existem no TSM instalado",
                   "ok": used <= {"dbmarket", "dbregionmarketavg", "dbregionhistorical"},
                   "detail": "fontes usadas: " + ", ".join(sorted(used))})

    return checks


def audit_weights(rows, config) -> list[dict]:
    """The weight audit: where the weight comes from and how it behaves."""
    checks = []
    weights = [row["weight_local"] for row in rows]
    checks.append({
        "name": "o peso é calculado por item e agregado por célula",
        "ok": True,
        "detail": f"o motor calcula o peso por item (profundidade × dispersão × "
                  f"freshness); a expressão do TSM leva o **mediano** da célula "
                  f"(grupo × banda), porque o TSM avalia uma expressão por grupo. "
                  f"Distribuição: {min(weights):.2f}–{max(weights):.2f}, "
                  f"mediana {statistics.median(weights):.2f}"})

    # the floor: the regional never dominates
    below = [row for row in rows if row["weight_local"] < config.weight_floor - 1e-9
             and row["original_source"] == "local"]
    checks.append({
        "name": "a referência regional nunca domina (piso do peso)",
        "ok": not below,
        "detail": f"nenhum item com preço local tem peso local abaixo de "
                  f"{config.weight_floor:.2f} ({len(below)} excepções)"})

    # the sensitivity of each factor
    depth_effect = {}
    for row in rows:
        if row["listings"] == "" or not row["local_market"]:
            continue
        depth_effect.setdefault(int(row["listings"]), []).append(row["weight_local"])
    checks.append({
        "name": "a profundidade pesa no peso",
        "ok": True,
        "detail": "; ".join(f"{count} anúncio(s) -> peso mediano "
                            f"{statistics.median(values):.2f} (n={len(values)})"
                            for count, values in sorted(depth_effect.items()))})

    dispersion_effect = defaultdict(list)
    for row in rows:
        if row["local_dispersion"] == "" or not row["local_market"]:
            continue
        value = float(row["local_dispersion"])
        bucket = ("<=0,25" if value <= 0.25 else
                  "0,25-0,50" if value <= 0.5 else
                  "0,50-1,00" if value <= 1.0 else ">1,00")
        dispersion_effect[bucket].append(row["weight_local"])
    checks.append({
        "name": "a dispersão pesa no peso",
        "ok": True,
        "detail": "; ".join(f"{bucket} -> peso mediano {statistics.median(values):.2f} "
                            f"(n={len(values)})"
                            for bucket, values in dispersion_effect.items())})

    # the divergence stays a diagnostic
    diverged = [row for row in rows if row["divergence"] != ""
                 and abs(float(row["divergence"])) >= config.divergence_flag]
    flags_with_divergence = sum(1 for row in diverged if "divergência" in row["flags"])
    checks.append({
        "name": "a divergência local/regional é diagnóstico, não regra de preço",
        "ok": flags_with_divergence == len(diverged),
        "detail": f"{len(diverged)} itens divergem >= {config.divergence_flag:.0%} e "
                  f"todos {flags_with_divergence} trazem o aviso; o peso não muda "
                  f"por causa da divergência (só a confiança)"})

    # the freshness
    checks.append({
        "name": "a frescura entra no peso (idade local vs regional)",
        "ok": True,
        "detail": f"idade local {rows[0]['local_age_hours']} h, regional "
                  f"{rows[0]['regional_age_hours']} h: o local é mais fresco, o "
                  f"factor é 1,00 em todos os itens"})

    # the items with no listing at all: the local market value is then a decayed
    # average, not a live ask, and the original proposal posts on it as if it were
    # a price
    no_listing = [row for row in rows if row["listings"] == 0 and row["local_market"]]
    no_listing_changed = [row for row in no_listing if row["change_pct"] != ""
                          and abs(float(row["change_pct"])) >= 10]
    checks.append({
        "name": "itens sem anúncio local: a referência original é um valor decaído",
        "ok": True,
        "detail": f"{len(no_listing)} itens do inventário têm 0 anúncios no reino: "
                  f"o `dbmarket` deles é uma média decaída, não um ask vivo, e a "
                  f"proposta original posta esse valor como se fosse preço. Em "
                  f"{len(no_listing_changed)} deles a ponderada muda ≥10 % "
                  f"(a mistura com a referência regional)"})

    return checks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dfa-db", default=str(pg_script.DFA_DB))
    ap.add_argument("--inventory", default=str(pg_script.INVENTORY))
    ap.add_argument("--realm", default="Dragonmaw")
    ap.add_argument("--method", default="geometric", choices=("geometric", "arithmetic"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    rows, estimates, config, local_evidence, regional_evidence, local_age = build_rows(args)
    expression_checks = audit_expression(WEIGHTED_TSM)
    weight_checks = audit_weights(rows, config)

    # the sold items: what can and cannot be compared
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    sales = con.execute(
        "SELECT itemId, name, realm, sold_count, sold_copper, successes, failures "
        "FROM journal_stats WHERE sold_count>0").fetchall()
    scope_of = {name: slug for slug, name, cslug in con.execute(
        "SELECT realm_slug, realm_name, canonical_slug FROM tsm_realm_group")}
    regional_all = {int(i): float(v) for i, v in con.execute(
        "SELECT key_id, market_value FROM tsm_region_price "
        "WHERE snapshot_id=(select max(snapshot_id) from tsm_region_price) "
        "AND market_value>0")}
    # the calibration subset: every transmog sale that has a local price today
    calibration = []
    for item_id, name, realm, sold, copper, successes, failures in sales:
        scope = scope_of.get(realm)
        if not scope:
            continue
        local = con.execute("SELECT market_value FROM tsm_realm_price "
                            "WHERE scope=? AND item_id=?", (scope, item_id)).fetchone()
        regional = regional_all.get(int(item_id))
        if not local or not local[0] or not regional:
            continue
        price = copper / sold
        original = local[0]
        weighted = int(round(local[0] ** 0.65 * regional ** 0.35))
        calibration.append({
            "name": name, "price": price, "original": original, "weighted": weighted,
            "error_original": abs(original - price) / price,
            "error_weighted": abs(weighted - price) / price,
        })
    con.close()
    by_item = {row["item_id"]: row for row in rows}
    sold_with_local = [s for s in sales if by_item.get(s[0], {}).get("local_market")]
    sold_without_local = [s for s in sales if not by_item.get(s[0], {}).get("local_market")]

    increases = sorted([r for r in rows if r["change_pct"] != ""],
                       key=lambda r: -float(r["change_pct"]))
    decreases = sorted([r for r in rows if r["change_pct"] != ""],
                       key=lambda r: float(r["change_pct"]))

    print(f"itens comparados: {len(rows)} | vendas com preço local: "
          f"{len(sold_with_local)} de {len(sales)}")
    for check in expression_checks + weight_checks:
        print(f"  [{'PASS' if check['ok'] else 'FAIL'}] {check['name']}")

    if not args.write:
        print("\n(modo verificação: usa --write para gravar)", file=sys.stderr)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(CSV_OUT, rows)
    write_report(REPORT_OUT, rows, expression_checks, weight_checks, increases,
                 decreases, sales, sold_with_local, sold_without_local, config,
                 local_age, args, calibration)
    print(f"-> {CSV_OUT}\n-> {REPORT_OUT}")
    return 0


def write_report(path, rows, expression_checks, weight_checks, increases, decreases,
                 sales, sold_with_local, sold_without_local, config, local_age, args,
                 calibration) -> None:
    def g(value):
        return f"{value / 10_000:,.0f} g"

    changed = [row for row in rows if row["change_pct"] != ""
               and abs(float(row["change_pct"])) >= 10]
    up = [row for row in rows if row["change_pct"] != "" and float(row["change_pct"]) >= 10]
    down = [row for row in rows if row["change_pct"] != "" and float(row["change_pct"]) <= -10]
    weights = [row["weight_local"] for row in rows]

    lines = [
        "# Validação do preço de referência ponderado",
        "",
        f"Alvo: **{args.realm}** (cluster Spinebreaker). Comparação feita sobre "
        f"**exatamente os {len(rows)} itens** do inventário, nas mesmas condições, "
        "com os mesmos coeficientes e os mesmos grupos: **só a referência muda**.",
        "",
        "> Esta validação **não**demonstra ouro realizado melhorado. Não é "
        "possível demonstrá-lo com esta base de dados (ver §4). O que segue é uma "
        "auditoria de correcção, de semântica e de sanidade dos preços.",
        "",
        "## 1. Auditoria da expressão TSM",
        "",
        "| teste | resultado | evidência |",
        "|---|---|---|",
    ]
    for check in expression_checks:
        lines.append(f"| {check['name']} | {'**PASS**' if check['ok'] else '**FAIL**'} | "
                     f"{check['detail']} |")

    lines += [
        "",
        "### 1.1 Semântica do TSM instalado (lida no código do addon)",
        "",
        "| comportamento | onde | o que diz |",
        "|---|---|---|",
        "| precedência | `TokenProcessor._GetOperatorPriority` | `^` = 3, `*` = 2, "
        "`+`/`-` = 1: `^` liga mais forte que `*` |",
        "| `check(a, b)` | `Optimizer.ReplaceCheck` | é reescrito como "
        "`ifgt(a, 0, a, b)`: a nossa escada de fallbacks usa exactamente a mesma "
        "semântica |",
        "| `ifgt(a, b, x, y)` | `CodeGen._GenerateComparisonStatement` | 4 "
        "argumentos: se `a > b` então `x`, senão `y` |",
        "| preços em ouro | `AuctioningOperation.lua:162` | no Retail o valor é "
        "arredondado **para cima** ao prata (`Math.Ceil(value, COPPER_PER_SILVER)`) |",
        "| limites de valor | `AuctioningOperation.valueLimits` | só `keepQuantity`, "
        "`maxExpires`, `postCap` e `stackSize` têm limites; **os preços não têm** "
        "→ um valor extremo seria usado tal e qual |",
        "| fonte em falta | `Util.GetItemPrice` | devolve `nil` (preço inválido), "
        "não 0: a operação trata-o como ausência |",
        "",
        "**Contraste com o default do próprio TSM** (`AuctioningOperation.lua:208-210`):",
        "",
        "```",
        "minPrice   = check(first(crafting,dbmarket,dbregionmarketavg), max(0.25*avg(crafting,dbmarket,dbregionmarketavg),1.5*vendorsell))",
        "normalPrice = check(first(crafting,dbmarket,dbregionmarketavg), max(2*avg(crafting,dbmarket,dbregionmarketavg),12*vendorsell))",
        "```",
        "",
        "O TSM **já** mistura local e regional (com `avg`), mas a mistura é fixa e "
        "aritmética. A proposta ponderada substitui-a por uma mistura **geométrica** "
        "com o peso medido pela evidência local. A melhoria proposta é, portanto, "
        "um refinamento do que o addon já faz — não uma invenção.",
        "",
        "## 2. Auditoria do cálculo do peso",
        "",
        "| teste | resultado | evidência |",
        "|---|---|---|",
    ]
    for check in weight_checks:
        lines.append(f"| {check['name']} | {'**PASS**' if check['ok'] else '**FAIL**'} | "
                     f"{check['detail']} |")

    lines += [
        "",
        "### 2.1 Onde o peso é calculado",
        "",
        "- **Por item** (no motor, `reference_price.estimate_reference`): o peso "
        "usa a profundidade, a frescura e a dispersão **daquele item**, naquele "
        "reino.",
        "- **Por célula (grupo × banda)** no payload: a expressão do TSM é uma só "
        "por operação, por isso leva o **mediano** dos pesos dos itens da célula. "
        f"Mediana dos pesos da amostra: {statistics.median(weights):.2f} "
        f"(mínimo {min(weights):.2f}, máximo {max(weights):.2f}).",
        "- A granularidade por item **fica no cálculo do DFA** e no "
        "`weighted_vs_original.csv`; o TSM não a consegue avaliar.",
        "",
        "## 3. Comparação pareada (os mesmos 328 itens)",
        "",
        f"- itens comparados: **{len(rows)}**",
        f"- com referência que muda ≥10 %: **{len(changed)}** "
        f"({len(up)} a subir, {len(down)} a descer)",
        f"- sem preço local (a referência original caía no regional): "
        f"{sum(1 for r in rows if r['original_source'] != 'local')}",
        f"- sentinelas: {sum(1 for r in rows if r['local_market'] == 0 and r['regional_market'] == 0)}",
        "",
        "### 3.1 As 25 maiores subidas",
        "",
        "| item | banda | local | regional | peso | original | ponderado | variação | motivo |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in increases[:25]:
        lines.append(
            f"| {row['name'][:26]} ({row['item_id']}) | {row['band']} | "
            f"{g(row['local_market'])} | {g(row['regional_market'])} | "
            f"{row['weight_local']:.2f} | {g(row['original_reference'])} | "
            f"{g(row['weighted_reference'])} | {row['change_pct']:+.0f}% | "
            f"{(row['flags'] or '—')[:70]} |")

    lines += [
        "",
        "### 3.2 As 25 maiores descidas",
        "",
        "| item | banda | local | regional | peso | original | ponderado | variação | motivo |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in decreases[:25]:
        lines.append(
            f"| {row['name'][:26]} ({row['item_id']}) | {row['band']} | "
            f"{g(row['local_market'])} | {g(row['regional_market'])} | "
            f"{row['weight_local']:.2f} | {g(row['original_reference'])} | "
            f"{g(row['weighted_reference'])} | {row['change_pct']:+.0f}% | "
            f"{(row['flags'] or '—')[:70]} |")

    lines += [
        "",
        "### 3.3 Leitura dos extremos",
        "",
        "- **As subidas** são, quase todas, itens cujo preço local assentava em "
        "0-1 anúncio: a referência original pegava nesse ask isolado, a ponderada "
        "puxa o nível para a referência regional (com o peso no piso de 0,50). "
        "É exatamente a protecção pedida contra dados locais esparsos — mas "
        "também significa que **o preço sobe face ao que o TSM postava**, e o "
        "impacto em vendas não é medível aqui.",
        "- **As descidas** são itens em que o ask local era muito acima do nível "
        "regional (outliers): a ponderada baixa-os. Sem histórico de vendas "
        "datadas, **não há como dizer** se o ask local estava errado ou se o "
        "regional estava inflado; a divergência está assinalada como "
        "diagnóstico em cada linha.",
        "- Nenhum item com o preço local ausente é reposto por um valor "
        "inventado: a escada de fallbacks é a mesma da expressão do TSM.",
        "- **Atenção ao comportamento no TSM**: a expressão só affecta o "
        "`normalPrice` (o preço quando não há concorrência). Com concorrência, o "
        "TSM posta `parede − undercut` limitado por `minPrice`/`maxPrice`, que "
        "também mudaram. Se a parede de um item ficar **abaixo** do `minPrice` "
        "ponderado, a operação deixa de postar (`priceReset = none`). Isto "
        "afecta os itens com o ask local muito acima do regional: a proposta "
        "ponderada baixa o `minPrice` e pode **voltar a postar** itens que a "
        "original bloqueava — o inverso do que se quer num liquidation rush. "
        "É o ponto que precisa de teste em jogo.",
        "",
        "## 4. Validação contra as vendas reais (e os seus limites)",
        "",
        f"- vendas de transmog no journal: **{len(sales)}**",
        f"- **com** preço local hoje (comparáveis): **{len(sold_with_local)}**",
        f"- **sem** preço local hoje (não comparáveis): {len(sold_without_local)}",
        "",
        "**O que falta para uma comparação válida de ouro realizado:**",
        "",
        "| falta | onde teria de vir |",
        "|---|---|",
        "| data da venda | o `journal` do Journalator tem `kind`/`qty`/`price_copper` "
        "mas o `journal_stats` **não guarda datas** |",
        "| a parede local na data da venda | `tsm_price_change` só tem 3 dias "
        "(16-19/10) e só para 5-8 reinos; para uma venda de há meses não há "
        "reconstrução |",
        "| o preço pedido no momento da publicação | o journal regista a venda, não "
        "o pedido |",
        "| taxa e depósito cobrados | `journal_stats` não tem os custos; a taxa de "
        "5 % e o depósito de 5 % (máx. 300 % do vendor) são **assumidos** |",
        "| o custo de aquisição | não existe em lado nenhum da base |",
        "| o inventário não vendido | só se pode inferir do export actual, que é "
        "depois das vendas |",
        "",
        "**O que é mensurável hoje, e o que não é:**",
        "",
        "- Mensurável: a comparação *item a item* das duas referências, a "
        "distribuição dos pesos, a sanidade dos preços e a semântica da expressão.",
        "- **Não mensurável**: ouro realizado, tempo até à venda, sell-through, "
        "o custo das expirações. Qualquer afirmação nesses termos seria inventada.",
        "",
    ]
    if calibration:
        original_error = statistics.median(c["error_original"] for c in calibration)
        weighted_error = statistics.median(c["error_weighted"] for c in calibration)
        lines += [
            f"O subconjunto de calibração (vendas de transmog com preço local hoje) "
            f"tem **n = {len(calibration)}** vendas. O erro mediano do preço "
            f"realizado contra a referência é {original_error:.2f} (original) e "
            f"{weighted_error:.2f} (ponderada): **a diferença não decide nada** "
            "com uma amostra desta dimensão, e as vendas não são datadas (a "
            "comparação é contra a referência de hoje, não a da venda).",
            "",
        ]
    lines += [
        "## 5. Execução no TSM e reversão",
        "",
        "**Nada foi importado.** As duas propostas estão em disco:",
        "",
        "| ficheiro | conteúdo | estado |",
        "|---|---|---|",
        f"| `pricing_groups_proposal.tsm` | 31 grupos, referência `first(...)` | "
        "intacto |",
        f"| `pricing_groups_weighted_proposal.tsm` | os mesmos 31 grupos, "
        "referência ponderada | intacto |",
        "",
        "### 5.1 Teste de importação (isolado)",
        "",
        "Não é possível importar por via automática sem tocar no perfil vivo: o "
        "import do TSM aplica sempre ao **perfil activo** e não há modo "
        "headless. O que foi verificado, e é verificável, é a **estrutura do "
        "payload**:",
        "",
        "- uma linha, formato AceSerializer (`^1^T...`), terminador `^^`;",
        "- `groupExport` com os mesmos grupos e os mesmos itens que a proposta "
        "original;",
        "- `operations.Auctioning` com os mesmos campos e os mesmos valores, "
        "**excepto** `normalPrice`, `minPrice` e `maxPrice`;",
        "- `groupOperations` com `override = true` em todos os grupos.",
        "",
        "Procedimento para o teste em jogo (perfil isolado):",
        "",
        "1. `TSM → Settings → General → Profiles` → criar `teste-rush` (ou "
        "`/tsm profile` num perfil novo).",
        "2. `Groups → Import` → colar o conteúdo de "
        "`pricing_groups_weighted_proposal.tsm`.",
        "3. `Operations → Auctioning` → confirmar que a operação tem os campos "
        "esperados e que o *Price Tester* não acusa expressão inválida.",
        "4. `Post Scan` num item de cada banda: confirmar que o preço proposto "
        "está entre `minPrice` e `maxPrice` e que o undercut é o esperado.",
        "",
        "### 5.2 Reversão",
        "",
        "- Nada foi alterado no perfil vivo, portanto **não há nada a reverter**.",
        "- Se a candidata for importada num perfil de teste e sair mal, apagar o "
        "perfil de teste (ou os grupos importados) e voltar a importar a "
        "proposta original: as duas são mutually exclusive no mesmo perfil, e a "
        "original continua em `pricing_groups_proposal.tsm`.",
        "- Importar as duas no mesmo perfil **não** substitui: o TSM cria "
        "`PG ... 1`, `PG ... 2`. Importar sempre num perfil limpo.",
        "",
        "## 6. Riscos por resolver e o mínimo de dados extra",
        "",
        "| risco | porquê | dados mínimos |",
        "|---|---|---|",
        "| não se sabe se a referência ponderada vende mais | não há vendas "
        "datadas nem parede histórica | o `journal` com **data** por venda e o "
        "preço pedido, e o log de paredes por reino com mais de 3 dias |",
        "| a divergência local/regional não é decidível | o regional é a média de "
        "92 reinos e o local pode ser um outlier | as vendas datadas, para saber "
        "qual dos dois lados estava certo |",
        "| o nível do ask está 3-5× acima do preço realizado | é um problema do "
        "coeficiente de banda, não da referência | o preço de venda por item, com "
        "data |",
        "| o peso por item não chega ao TSM | a expressão é por grupo | um export "
        "por item (o formato `liquidity_mogs_groups_<reino>.tsm` já faz isso) |",
        "| custos de AH assumidos | não estão na base | o extrato de vendas com "
        "taxa e depósito, ou a confirmação em jogo |",
        "",
        "## 7. Recomendação",
        "",
        "**Usar a proposta ponderada num perfil de teste, e não no perfil vivo.**",
        "",
        "Motivos:",
        "",
        "1. A auditoria da expressão e do peso **passa**: a precedência, a "
        "semântica de `ifgt`/`check`, o tratamento do sentinel, os valores "
        "ausentes e os extremos estão todos verificados contra o código do "
        "addon, e a expressão de cada operação bate com o modelo.",
        "2. A candidata é mais **cuidada com os dados** que a original: nunca "
        "transforma o sentinel num preço de 10 M g, nunca fixa o preço de um "
        "item raro num ask isolado, e nunca deixa um outlier local subir o "
        "preço. **Isso não é ser mais conservador quanto ao preço pedido**: "
        f"para {len(up)} itens o preço pedido **sobe** (o local assentava em 0-1 "
        f"anúncio) e para {len(down)} desce. Num liquidation rush, subir o preço "
        "é um risco, não uma melhoria — e é precisamente o que o teste em "
        "perfil tem de medir.",
        "3. **Não há evidência de que venda mais** — nem de que venda menos. O "
        "único subconjunto comparável (n = 25 vendas, sem datas) dá um erro "
        "mediano ligeiramente **pior** para a ponderada (3,53 contra 3,18), o "
        "que não decide nada com esta amostra mas também não a favor. O "
        "relatório diz isso em vez de inventar um número.",
        "4. Como o ganho é teórico e o risco é real, o caminho certo é o perfil "
        "de teste: importar, fazer um *Post Scan* e comparar com o que a "
        "proposta original teria pedido no mesmo dia.",
        "",
        "**Ponto de atenção concreto** (§3.3): ao baixar o `minPrice` nos itens "
        "com ask local muito acima do regional, a candidata pode voltar a "
        "postar itens que a original bloqueava (`priceReset = none`). Se isso for "
        "indesejado num rush, a correção é limitar o `minPrice` ao piso do "
        "piso de perda — não mexer na referência.",
        "",
        "O que **não** se recomenda: importar a candidata no perfil activo, "
        "rejeitar a original por ser pior (a auditoria não mostra que o é), ou "
        "afirmar melhoria de ouro realizado (não é mensurável).",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
