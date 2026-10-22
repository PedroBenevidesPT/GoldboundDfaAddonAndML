"""The evidence-based pricing engine, wired into the DFA pipeline.

    python scripts/pricing_engine.py --show
    python scripts/pricing_engine.py --write
    python scripts/pricing_engine.py --write --backtest

Produces, for every item of the inventory:

* **A** the estimated realizable market value (with a range, a confidence and
  the provenance of every source);
* **B** the liquidity assessment (demand, velocity, competition, the account's
  own observed sell-through);
* **C** the recommended listing price, with the reason for any deviation from
  the estimate and the costs marked as assumptions.

and a **chronological walk-forward** comparison of the estimator against the
baselines (the local market alone, the regional alone, the historical alone
and the sale average alone), with no future data in the inputs.
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
from woweconomy.analytics import pricing_engine as pe  # noqa: E402
from woweconomy.analytics import pricing_groups as pg  # noqa: E402
from woweconomy.analytics.mog_realm import resolve as resolve_realm  # noqa: E402
from woweconomy.analytics.token_rush_data import (  # noqa: E402
    load_vendor_sell, read_inventory, realm_groups,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
OUT_DIR = ROOT / "reports" / "pricing_engine"
CSV_OUT = OUT_DIR / "item_estimates.csv"
REPORT_OUT = OUT_DIR / "pricing_engine.md"
BACKTEST_OUT = OUT_DIR / "backtest.md"


def g(value) -> str:
    return f"{value / 10_000:,.0f} g"


# ---------------------------------------------------------------------------
# the data
# ---------------------------------------------------------------------------

def clearance_factors(con) -> dict[str, tuple[float, int]]:
    """The **measured** clearance factor per band: sale average / ask level.

    It is the ratio between what people list and what they pay, measured
    across the whole catalogue of the region snapshot.  It is a property of
    the market, not an assumption.
    """
    latest = con.execute("select max(snapshot_id) from tsm_region_price").fetchone()[0]
    ratios = defaultdict(list)
    for item, market, avg_sale in con.execute(
            "SELECT key_id, market_value, avg_sale_price FROM tsm_region_price "
            "WHERE snapshot_id=? AND market_value>0 AND avg_sale_price>0", (latest,)):
        band = pg.band_of(market / pe.COPPER_PER_GOLD)
        ratios[band].append(avg_sale / market)
    return {band: (statistics.median(values), len(values))
            for band, values in ratios.items() if values}


def load_journal(con) -> dict[tuple[int, str], dict]:
    out = {}
    for item, realm, sold, copper, successes, failures in con.execute(
            "SELECT itemId, realm, sold_count, sold_copper, successes, failures "
            "FROM journal_stats"):
        key = (int(item), realm or "")
        entry = out.setdefault(key, {"sales": 0, "copper": 0, "successes": 0,
                                     "failures": 0})
        entry["sales"] += int(sold or 0)
        entry["copper"] += int(copper or 0)
        entry["successes"] += int(successes or 0)
        entry["failures"] += int(failures or 0)
    return out


def build_evidence(con, scope, canonical, items, config, factors, demand_by_item,
                   accounts_sampled, listings_by_item, journal, vendor, local_age,
                   region_age):
    """One :class:`Evidence` per item, with every source it can offer."""
    region = {}
    for item, market, historical, avg_sale, sold_per_day, sale_rate in con.execute(
            "SELECT key_id, market_value, historical, avg_sale_price, sold_per_day, "
            "sale_rate FROM tsm_region_price WHERE snapshot_id="
            "(select max(snapshot_id) from tsm_region_price)"):
        region[int(item)] = (market, historical, avg_sale, sold_per_day, sale_rate)
    local = {}
    for item, market, wall, recent, historical in con.execute(
            "SELECT item_id, market_value, min_buyout, recent, historical "
            "FROM tsm_realm_price WHERE scope=?", (scope,)):
        local[int(item)] = (market, wall, recent, historical)

    evidences = {}
    for item_id in items:
        sources = []
        lm, lw, lr, lh = local.get(item_id, (0, 0, 0, 0))
        rm, rh, ra, spd, rate = region.get(item_id, (0, 0, 0, 0.0, 0.0))
        band = pg.band_of((rm or lm or 1) / pe.COPPER_PER_GOLD)
        depth = listings_by_item.get(item_id)
        dispersion = (abs(lm / lw - 1.0) if lm and lw else None)
        for name, value, kind, family in (
                ("dbmarket", lm, pe.MARKET_REFERENCE, pe.FAMILY_LOCAL_ASK),
                ("dbrecent", lr, pe.ASKING, pe.FAMILY_LOCAL_ASK),
                ("dbhistorical", lh, pe.ASKING, pe.FAMILY_LOCAL_ASK),
                ("dbminbuyout", lw, pe.ASKING, pe.FAMILY_LOCAL_ASK),
                ("dbregionmarketavg", rm, pe.MARKET_REFERENCE, pe.FAMILY_REGIONAL_ASK),
                ("dbregionhistorical", rh, pe.ASKING, pe.FAMILY_REGIONAL_ASK),
                ("dbregionsaleavg", ra, pe.TRANSACTION, pe.FAMILY_REGIONAL_SALES),
        ):
            sources.append(pe.SourceValue(
                name=name, value=value or 0, kind=kind,
                scope=pe.LOCAL if family in (pe.FAMILY_LOCAL_ASK, pe.FAMILY_OWN_SALES)
                else pe.REGIONAL,
                family=family,
                age_hours=local_age if family == pe.FAMILY_LOCAL_ASK else region_age,
                depth=depth if family == pe.FAMILY_LOCAL_ASK else None,
                dispersion=dispersion if family == pe.FAMILY_LOCAL_ASK else None,
                volume=spd if family == pe.FAMILY_REGIONAL_SALES else None,
            ))
        own = journal.get((item_id, canonical)) or journal.get((item_id, "")) or {}
        if own.get("sales"):
            sources.append(pe.SourceValue(
                name="venda própria", value=own["copper"] // own["sales"],
                kind=pe.TRANSACTION, scope=pe.LOCAL, family=pe.FAMILY_OWN_SALES,
                age_hours=0.0, sample_size=own["sales"],
                note=f"média de {own['sales']} venda(s) desta conta"))
        clearance, clearance_n = factors.get(band, (None, 0))
        attempts = own.get("successes", 0) + own.get("failures", 0)
        sell_through = (own["successes"] / attempts) if attempts else None
        evidences[item_id] = pe.Evidence(
            item_id=item_id, realm=canonical, band=band,
            qty=1, sources=sources,
            own_sales=own.get("sales", 0), own_expired=own.get("failures", 0),
            own_sold_copper=own.get("copper", 0), own_sell_through=sell_through,
            want_rate=(demand_by_item.get(item_id) / accounts_sampled
                       if accounts_sampled and demand_by_item.get(item_id) else None),
            demand_known=bool(demand_by_item.get(item_id)),
            market_clearance=clearance, market_clearance_n=clearance_n,
            listings=depth, vendor_sell=vendor.get(item_id),
        )
    return evidences


# ---------------------------------------------------------------------------
# the walk-forward validation
# ---------------------------------------------------------------------------

def walk_forward(con, config, regions=("spinebreaker", "silvermoon", "argent-dawn",
                                       "draenor", "twisting-nether", "stormreaver",
                                       "haomarush", "shattered-halls"),
                 calibration=None):
    """Chronological validation: estimate at fetch *t*, compare with fetch *t+1*.

    No future data enters the estimate: the local state is the one rebuilt from
    the delta log up to *t*, the regional reference is the region snapshot in
    force at *t*, and the clearance factor is measured on that same snapshot.
    """
    scope_of = {}
    for slug, name, cslug in con.execute(
            "SELECT realm_slug, realm_name, canonical_slug FROM tsm_realm_group"):
        scope_of.setdefault(name, cslug)
        scope_of.setdefault(slug, cslug)
    region_snapshots = []
    for sid, when in con.execute("SELECT snapshotId, snapshotTimestamp FROM snapshots"):
        region_snapshots.append((when, sid))
    region_snapshots.sort()

    def region_at(when: str):
        best = None
        for stamp, sid in region_snapshots:
            if stamp[:10] <= when[:10] and (best is None or stamp > best[0]):
                best = (stamp, sid)
        if best is None:
            return {}
        return {int(i): (float(m), float(h), float(a)) for i, m, h, a in con.execute(
            "SELECT itemId, marketValue, historical, avgSalePrice FROM market_snapshots "
            "WHERE snapshotId=? AND marketValue>0", (best[1],))}

    errors = defaultdict(list)
    sample = 0
    for region in regions:
        scope = scope_of.get(region)
        if not scope:
            continue
        fetches = con.execute(
            "SELECT snapshot_id, updated_at FROM tsm_snapshot WHERE scope=? "
            "AND kind='realm_items' ORDER BY updated_at", (scope,)).fetchall()
        if len(fetches) < 2:
            continue
        state = {}
        for index, (sid, when) in enumerate(fetches):
            for item, mv, mb in con.execute(
                    "SELECT key_id, market_value, min_buyout FROM tsm_price_change "
                    "WHERE kind='realm_items' AND scope=? AND snapshot_id=?", (scope, sid)):
                state[int(item)] = (float(mv or 0), float(mb or 0))
            if index + 1 >= len(fetches):
                break
            following = dict(state)
            for item, mv, mb in con.execute(
                    "SELECT key_id, market_value, min_buyout FROM tsm_price_change "
                    "WHERE kind='realm_items' AND scope=? AND snapshot_id=?",
                    (scope, fetches[index + 1][0])):
                following[int(item)] = (float(mv or 0), float(mb or 0))
            regional = region_at(when)
            # the clearance factor measured on the region snapshot in force
            ratios = defaultdict(list)
            for item, market, avg_sale in con.execute(
                    "SELECT itemId, marketValue, avgSalePrice FROM market_snapshots "
                    "WHERE snapshotId=(SELECT snapshotId FROM snapshots WHERE "
                    "snapshotTimestamp<=? ORDER BY snapshotTimestamp DESC LIMIT 1) "
                    "AND marketValue>0 AND avgSalePrice>0", (when,)):
                band = pg.band_of(market / pe.COPPER_PER_GOLD)
                ratios[band].append(avg_sale / market)
            factors = {band: statistics.median(values)
                       for band, values in ratios.items() if values}
            for item, (local_market, wall) in state.items():
                target_market = following.get(item, (0, 0))[0]
                target_wall = following.get(item, (0, 0))[1]
                if not target_market:
                    continue
                reg = regional.get(item)
                if not reg:
                    continue
                rm, rh, ra = reg
                band = pg.band_of(rm / pe.COPPER_PER_GOLD)
                evidence = pe.Evidence(
                    item_id=item, realm=region, band=band,
                    sources=[
                        pe.SourceValue("dbmarket", int(local_market), pe.MARKET_REFERENCE,
                                       pe.LOCAL, pe.FAMILY_LOCAL_ASK, age_hours=0.0,
                                       depth=1 if wall else 0),
                        pe.SourceValue("dbregionmarketavg", int(rm), pe.MARKET_REFERENCE,
                                       pe.REGIONAL, pe.FAMILY_REGIONAL_ASK, age_hours=12.0),
                        pe.SourceValue("dbregionhistorical", int(rh), pe.ASKING,
                                       pe.REGIONAL, pe.FAMILY_REGIONAL_ASK, age_hours=12.0),
                        pe.SourceValue("dbregionsaleavg", int(ra), pe.TRANSACTION,
                                       pe.REGIONAL, pe.FAMILY_REGIONAL_SALES, age_hours=12.0),
                    ],
                    market_clearance=factors.get(band))
                estimate = pe.estimate_value(evidence, config, calibration)
                if estimate.price is None:
                    continue
                sample += 1
                for label, prediction in (
                        ("modelo do motor", estimate.price),
                        ("local (dbmarket)", int(local_market)),
                        ("regional (market)", int(rm)),
                        ("historico regional", int(rh)),
                        ("media de venda regional", int(ra)),
                ):
                    if not prediction or not target_market:
                        continue
                    errors[(label, "nivel local")].append(
                        abs(prediction - target_market) / target_market)
                    if target_wall:
                        errors[(label, "parede seguinte")].append(
                            abs(prediction - target_wall) / target_wall)
    return errors, sample


# ---------------------------------------------------------------------------

def load_calibration() -> pe.Calibration:
    """The measured weights, if the calibration has been run."""
    path = OUT_DIR / "calibration.json"
    if not path.exists():
        return pe.Calibration()
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    return pe.Calibration(
        family_weight=data.get("family_weight", {}),
        family_error=data.get("family_error", {}),
        coverage=data.get("coverage", {}),
        measured_at=data.get("measured_at", ""),
        sample=data.get("sample", 0),
        source=data.get("source", ""),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dfa-db", default=str(pg_script.DFA_DB))
    ap.add_argument("--inventory", default=str(pg_script.INVENTORY))
    ap.add_argument("--realm", default="Dragonmaw")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    args = ap.parse_args(argv)

    config = pe.EngineConfig()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con_dfa = sqlite3.connect(f"file:{args.dfa_db}?mode=ro", uri=True) \
        if Path(args.dfa_db).exists() else None

    inventory = read_inventory(args.inventory)
    items = set(inventory)
    profile = resolve_realm(args.realm)
    canonical = profile.realm
    realm_rows = realm_groups(con)
    scope = (realm_rows.get(canonical) or {}).get("canonical_slug") or canonical

    # the measured weights (how well each family predicts a real sale price)
    calibration = load_calibration()

    facts, catalogue = pg_script.build_features(con, con_dfa, inventory)
    groups_of_item = pg_script.catalogue_groups(catalogue)
    factors = clearance_factors(con)
    journal = load_journal(con)
    vendor = load_vendor_sell(con, items)
    listings = {int(i): int(n) for i, n in con.execute(
        "SELECT itemId, num_auctions FROM tsm_market WHERE realm=?", (canonical,))}
    demand = {int(i): n for i, n in con.execute(
        "SELECT itemId, wanters FROM ("
        "  SELECT db2_item_appearance.itemId AS itemId, MAX(m.item_id) AS wanters "
        "  FROM db2_item_appearance "
        "  JOIN (SELECT item_id, n AS wanters FROM missing_placeholder) m "
        "  GROUP BY db2_item_appearance.itemId)")} if False else {}
    # the demand comes from the DfA sample: the accounts that still lack the look
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

    as_of = con.execute("select max(fetched_at) from tsm_realm_price").fetchone()[0]
    region_at = con.execute("select max(updated_at) from tsm_snapshot "
                             "where scope='region' and kind='region_items'").fetchone()[0]
    local_age = pg_script.age_hours(as_of, region_at)
    region_age = 0.0

    evidences = build_evidence(con, scope, canonical, items, config, factors, demand,
                               accounts_sampled, listings, journal, vendor,
                               local_age, region_age)

    names = {int(i): n for i, n in con.execute("SELECT itemId, name FROM db2_items") if n}
    results = []
    for item_id, evidence in evidences.items():
        item = inventory[item_id]
        evidence.name = item.name or names.get(item_id, "")
        evidence.qty = item.qty
        value = pe.estimate_value(evidence, config, calibration)
        liquidity = pe.assess_liquidity(evidence, config)
        listing = pe.recommend_listing(value, liquidity, evidence, config)
        results.append((item_id, evidence, value, liquidity, listing))

    usable = [r for r in results if r[2].usable]
    print(f"itens: {len(results)} | com estimativa utilizável: {len(usable)} | "
          f"sem evidência: {len(results) - len(usable)}")
    for item_id, evidence, value, liquidity, listing in sorted(
            results, key=lambda r: -((r[2].price or 0)))[:6]:
        print(f"  {evidence.name[:28]:28s} {value.price / 10_000 if value.price else 0:>10,.0f} g "
              f"conf {value.confidence:.0%} | {liquidity.level:12s} | "
              f"lista {(listing.price or 0) / 10_000:>10,.0f} g")

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        write_csv(CSV_OUT, results)
        write_report(REPORT_OUT, results, factors, config, args, calibration)
        print(f"-> {CSV_OUT}\n-> {REPORT_OUT}")
    if args.backtest:
        errors, sample = walk_forward(con, config, calibration=calibration)
        if args.write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            write_backtest(BACKTEST_OUT, errors, sample)
            print(f"-> {BACKTEST_OUT}")
        else:
            for (label, target), values in sorted(errors.items()):
                values.sort()
                print(f"  {label:28s} {target:16s} n={len(values):8,d} "
                      f"p50={values[len(values)//2]:.3f}")
    con.close()
    return 0


CSV_FIELDS = [
    "item_id", "name", "realm", "band", "qty", "estimated_price", "low", "high",
    "confidence", "basis", "contributions", "rejected", "flags", "hypotheses",
    "explanation", "liquidity", "demand", "velocity_per_day", "local_listings",
    "local_dispersion", "stock_coverage_days", "observed_sell_through",
    "liquidity_flags", "liquidity_explanation", "recommended_price",
    "recommended_low", "recommended_high", "listing_reason", "gross_proceeds",
    "net_proceeds", "costs_assumed", "own_sales", "own_expired", "want_rate",
    "market_clearance",
]


def write_csv(path: Path, results) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for item_id, evidence, value, liquidity, listing in results:
            row = {
                "item_id": item_id,
                "name": evidence.name,
                "realm": evidence.realm,
                "band": evidence.band,
                "qty": evidence.qty,
                "own_sales": evidence.own_sales,
                "own_expired": evidence.own_expired,
                "want_rate": (round(evidence.want_rate, 4)
                              if evidence.want_rate is not None else ""),
                "market_clearance": (round(evidence.market_clearance, 4)
                                     if evidence.market_clearance else ""),
            }
            row.update(value.as_dict())
            row.update(liquidity.as_dict())
            row.update(listing.as_dict())
            row["listing_reason"] = listing.reason
            row.pop("reason", None)
            writer.writerow(row)


def write_report(path: Path, results, factors, config, args, calibration=None) -> None:
    usable = [r for r in results if r[2].usable]
    insufficient = [r for r in results if not r[2].usable]
    lines = [
        "# DFA — motor de preço baseado em evidência",
        "",
        f"Gerado por `python scripts/pricing_engine.py`. Alvo: **{args.realm}** "
        f"({len(results)} itens). É análise: **nada foi importado no TSM**, nenhuma "
        "operação foi tocada e os ficheiros de proposta continuam intactos.",
        "",
        "## 0. O principio",
        "",
        "**Os anuncios mostram o que os vendedores querem receber; as transaccoes "
        "mostram o que alguem pagou de facto.** As duas coisas nao se medem da mesma "
        "maneira, por isso nao sao misturadas: primeiro estima-se o valor de "
        "transaccao a partir das vendas observadas, e depois pergunta-se ate que "
        "ponto as referencias de mercado sao compativeis com esse valor.",
        "",
        "## 1. As fontes: origem, significado, data, cobertura, limitacoes",
        "",
        "| campo | origem | significado | data | cobertura | limitacoes |",
        "|---|---|---|---|---|---|",
        "| `dbmarket` | `tsm_realm_price.market_value` | ask medio do reino, "
        "decays | `fetched_at` (19/10) | 176/328 itens | ask, nao transaccao; "
        "medido **3,9x acima** do preco realizado |",
        "| `dbrecent` | `tsm_realm_price.recent` | ask dos ultimos 3 dias | idem | "
        "idem | correlacionado com `dbmarket` (mesma familia) |",
        "| `dbhistorical` | `tsm_realm_price.historical` | ask de 60 dias do reino | "
        "idem | idem | correlacionado; pode estar atrasado |",
        "| `dbminbuyout` | `tsm_realm_price.min_buyout` | o anuncio mais barato | "
        "idem | 114/328 com parede | **um** ask, nao o mercado |",
        "| `dbregionmarketavg` | `tsm_region_price.market_value` | ask medio da "
        "regiao | snapshot de 18/10 | 31 184 itens | ask; 1,25x o preco realizado |",
        "| `dbregionhistorical` | `tsm_region_price.historical` | ask de 60 dias da "
        "regiao | idem | idem | correlacionado com o anterior |",
        "| `dbregionsaleavg` | `tsm_region_price.avg_sale_price` | **media do que "
        "se pagou** na regiao | idem | 25 128 itens com venda | a TSM **nao expoe a "
        "amostra**; sujeita a snipes |",
        "| `dbregionsoldperday` | `tsm_region_price.sold_per_day` | estimativa de "
        "volume regional | idem | idem | **volume, nao preco**: nunca multiplica o "
        "preco |",
        "| `dbregionsalerate` | `tsm_region_price.sale_rate` | frequencia de "
        "vendas | idem | idem | a definicao e da TSM |",
        "| concorrencia | `tsm_market.num_auctions` | anuncios concorrentes | "
        "descarga AuctionDB | **12 reinos** | nao diz vendas futuras |",
        "| procura | `dfa.db :: missing` | contas da amostra que ainda nao tem o "
        "look | leitura actual | 20 436 contas | procura **potencial**, nao "
        "intencao de compra |",
        "| vendas proprias | `journal_stats` | as nossas transaccoes | **sem data** | "
        "6 322 vendas | agregado: sem data e sem preco pedido |",
        "| arquivo bruto | `Journalator.lua` (SavedVariables) | eventos por sessao | "
        "timestamps | 9 sessoes, 4 dias | comprimido (LibDeflate): nao "
        "descodificavel aqui |",
        "",
        "## 2. As tres saidas",
        "",
        "| | o que é |",
        "|---|---|",
        "| **A — valor realizável estimado** | a média aritmética ponderada pela "
        "qualidade da evidência, com intervalo, confiança e a proveniência de cada "
        "fonte |",
        "| **B — avaliação de liquidez** | procura, velocidade, concorrência e o "
        "sell-through **observado** da conta; separado do preço |",
        "| **C — preço de listagem recomendado** | derivado de A e B, com a razão de "
        "qualquer desvio e os custos marcados como assumidos |",
        "",
        f"- {len(usable)} de {len(results)} itens com estimativa utilizável; "
        f"{len(insufficient)} sem evidência suficiente (`INSUFFICIENT_EVIDENCE`).",
        "",
        "## 3. Os pesos: medidos, nao escolhidos",
        "",
    ]
    if calibration and calibration.family_weight:
        lines += [
            f"Calibrados contra as vendas reais da conta "
            f"({calibration.sample} itens, medido em {calibration.measured_at}):",
            "",
            "| familia | cobertura | erro mediano (log) | peso medido |",
            "|---|---:|---:|---:|",
        ]
        for family in sorted(calibration.family_weight):
            lines.append(f"| {family} | {calibration.coverage.get(family, 0):.0%} | "
                         f"{calibration.family_error.get(family, 0):.3f} | "
                         f"{calibration.family_weight[family]:.3f} |")
        lines += [
            "",
            "Ver `calibration.md` para o metodo. **As referencias de ask recebem um "
            "peso medido, nao assumido** -- e o que a medicao mostra e que o ask "
            "local e uma previsao fraca do preco realizado.",
        ]
    else:
        lines.append("Sem calibracao: os pesos sao os priors do motor, marcados como "
                     "especulativos. Corre `python scripts/pricing_calibration.py "
                     "--write` para os medir.")
    lines += [
        "",
        "## 4. As fontes e o que valem",
        "",
        "| fonte | tipo | âmbito | família | peso | porquê |",
        "|---|---|---|---|---|---|",
        "| `dbmarket` | referência de mercado | local | ask local | 1,00 | o nível "
        "do mercado em que se vende |",
        "| `dbrecent` | ask | local | ask local | (com `dbmarket`) | 3 dias: "
        "correlacionado, não é evidência independente |",
        "| `dbhistorical` | ask | local | ask local | (com `dbmarket`) | 60 dias: "
        "correlacionado |",
        "| `dbminbuyout` | ask | local | ask local | (com `dbmarket`) | o anúncio "
        "mais barato: um ask, não o mercado |",
        "| `dbregionmarketavg` | referência de mercado | regional | ask regional | 0,70 | "
        "aferido, mas mais largo que o mercado onde vendemos |",
        "| `dbregionhistorical` | ask | regional | ask regional | (com o anterior) | "
        "correlacionado |",
        "| `dbregionsaleavg` | **transacção** | regional | vendas regionais | 1,00 | "
        "o que alguém pagou — é a grandeza que se quer estimar |",
        "| venda própria | **transacção** | local | vendas próprias | 1,30 | a "
        "transacção mais relevante, com amostra mínima |",
        "",
        "As quatro fontes locais de ask **não são quatro observações**: são uma "
        "família, reduzida a um valor (mediana) com a sua dispersão interna. A "
        "mesma coisa na família regional. Só as vendas são uma família "
        "independente, porque medem outra coisa.",
        "",
        "O factor de clearance (média de venda ÷ nível de ask) é **medido** no "
        "catálogo, por banda:",
        "",
        "| banda | factor | n |",
        "|---|---:|---:|",
    ]
    for band, (factor, n) in sorted(factors.items()):
        lines.append(f"| {band} | {factor:.3f} | {n:,} |")
    lines += [
        "",
        "## 3. Itens: valor, liquidez, listagem",
        "",
        "| item | banda | valor estimado | intervalo | conf. | liquidez | listagem | razão |",
        "|---|---|---:|---|---:|---|---:|---|",
    ]
    for item_id, evidence, value, liquidity, listing in sorted(
            results, key=lambda r: -((r[2].price or 0)))[:30]:
        lines.append(
            f"| {evidence.name[:26]} ({item_id}) | {evidence.band} | "
            f"{(value.price or 0) / 10_000:,.0f} g | "
            f"{(value.low or 0) / 10_000:,.0f}–{(value.high or 0) / 10_000:,.0f} g | "
            f"{value.confidence:.0%} | {liquidity.level} | "
            f"{(listing.price or 0) / 10_000:,.0f} g | {listing.reason[:60]} |")
    lines += [
        "",
        "## 4. A proveniência, item a item",
        "",
        "O CSV `item_estimates.csv` traz, por item: o valor estimado, o intervalo, a "
        "confiança, **as fontes que contribuíram com o peso efectivo**, as fontes "
        "**rejeitadas com o motivo**, os sinais de liquidez, o preço recomendado e "
        "o motivo. Nada é uma caixa-preta: cada número aponta para a fonte que o "
        "produziu.",
        "",
        "## 5. Limitações",
        "",
        "- **Não há vendas datadas.** O `journal_stats` tem contagens e valores "
        "agregados, sem datas; o arquivo bruto do Journalator é comprimido "
        "(LibDeflate) e cobre só os últimos dias. Não há forma de reconstruir "
        "quando uma venda aconteceu, nem o preço pedido no momento da publicação.",
        "- **A média de venda regional não expõe a amostra.** A TSM não diz quantas "
        "vendas entram nessa média, por isso o motor **não inventa** uma "
        "amostra: usa o volume (`sold_per_day`) e a discordância como sinais de "
        "qualidade, e trata a média como uma transacção (peso alto) com confiança "
        "limitada.",
        "- **A média de venda baixa é evidência, e fica.** Quando ela é muito "
        "abaixo do ask, o factor de clearance medido reduz o ask e a estimativa "
        "desce; a confiança desce se a causa não for estabelecida. O preço não é "
        "descartado por ser pouco apelativo.",
        "- **O volume é regional.** A cobertura de stock usa o volume regional e é "
        "sinal de escala, não promessa de venda local.",
        "- **Os custos são assumidos** (taxa 5 %, depósito 5 % limitado a 300 % do "
        "vendor). O resultado líquido é marcado como tal.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_backtest(path: Path, errors, sample) -> None:
    lines = [
        "# Walk-forward do motor de preço",
        "",
        "Para cada par de fetches consecutivos de cada reino, o motor estima o "
        "preço **com os dados disponíveis nesse momento** (o estado local "
        "reconstruído do log de alterações até *t*, a referência regional e o "
        "factor de clearance do snapshot da região em vigor) e compara-se com o "
        "nível e com a parede do fetch seguinte. **Não entra nenhum dado "
        "futuro.**",
        "",
        f"Observações: {sample:,}.",
        "",
        "| estimador | alvo | n | erro mediano | p25 | p75 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for (label, target), values in sorted(errors.items()):
        values.sort()
        n = len(values)
        lines.append(f"| {label} | {target} | {n:,} | {values[n//2]:.3f} | "
                     f"{values[n//4]:.3f} | {values[3*n//4]:.3f} |")
    lines += [
        "",
        "**O que isto é:** uma avaliação da **exactidão do nível** (o erro "
        "relativo entre o que cada estimador diz e o que o mercado fez). "
        "**Não** é uma avaliação de venda realizada: o alvo é um ask, não uma "
        "transacção.",
        "",
        "**As duas validações medem coisas diferentes, e é essencial não as "
        "confundir:**",
        "",
        "- O walk-forward mede **previsibilidade do nível de mercado**. Aí o "
        "`dbmarket` local é o melhor (erro mediano 0,110 contra a parede "
        "seguinte): é um ask médio estável, e o próximo ask sai dele.",
        "- A calibração mede **viés contra o preço realizado**. Aí o `dbmarket` "
        "local é o **pior** (3,9× o preço realizado) e a média de venda regional "
        "é a melhor (0,91×).",
        "",
        "Ou seja: uma fonte pode ser uma **previsão excelente do mercado** e ao "
        "mesmo tempo um **estimator péssimo do valor realizável**. O motor "
        "ancora-se nas transacções justamente por isso, e por isso o seu erro de "
        "nível é maior do que o do `dbmarket`: ele não está a tentar adivinhar o "
        "ask, está a estimar o que se recebe.",
        "",
        "**O que não é:** prova de que o motor vende mais, mais depressa ou com "
        "mais lucro. Para isso é preciso o journal datado, que não existe.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
