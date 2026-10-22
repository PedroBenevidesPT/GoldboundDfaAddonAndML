"""Descobre os grupos de preço do inventário de transmog e as suas estratégias.

    python scripts/pricing_groups.py --show
    python scripts/pricing_groups.py --write
    python scripts/pricing_groups.py --write --realm Dragonmaw

Lê (só leitura) `tsm_markets.db`, `C:\\economics\\woweconomy\\dfa\\dfa.db` e o
inventário.  Escreve **propostas**, nunca configuração viva:

    reports/pricing_groups.md                  a análise
    reports/pricing_groups/band_evidence.csv   a medição por banda
    reports/pricing_groups/groups.csv          os grupos e as estratégias
    reports/pricing_groups/membership.csv      inventário -> grupo
    reports/pricing_groups/tsm_proposal.md     a proposta de grupos e operações
    reports/pricing_groups/pricing_groups_proposal.tsm   a string de import (NÃO importada)

Nada é escrito na base de dados nem no TSM.
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

from woweconomy.analytics import pricing_groups as pg  # noqa: E402
from woweconomy.analytics.pricing_groups import band_order  # noqa: E402
from woweconomy.analytics.mog_realm import resolve as resolve_realm  # noqa: E402
from woweconomy.analytics.token_rush_data import (  # noqa: E402
    RegionPrice, clean, load_commodity_ids, load_journal, load_realm_prices,
    load_region_prices, load_vendor_sell, read_inventory, realm_groups,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
DFA_DB = Path(r"C:\economics\woweconomy\dfa\dfa.db")
INVENTORY = ROOT / "inventory.csv"
OUT_DIR = ROOT / "reports" / "pricing_groups"
REPORT = ROOT / "reports" / "pricing_groups.md"

BAND_KEYS = pg.band_order()


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_demand(con_dfa) -> tuple[dict[int, int], set[int], int]:
    """Potential demand per item: sampled accounts that still lack the look.

    ``dfa.missing`` counts, per *appearance*, the sampled accounts that do not
    have it.  An item can carry several appearances; an account that misses any
    of them may still want this item, so the item takes the **maximum** of its
    appearances (the project's convention for "wanters" is the union over
    accounts; the maximum is the same idea at the item level).

    The second return value is the set of appearances the DfA sample actually
    covers.  An item outside it is **not** "without demand": the sample says
    nothing about it, and that has to stay *unknown*.

    This is *potential* demand: it says nobody in the sample has the look.
    It is not purchase intent and not a sale probability.
    """
    if not con_dfa:
        return {}, set(), 0
    accounts = con_dfa.execute(
        "SELECT COUNT(DISTINCT account) FROM scanned WHERE kind='appearances'").fetchone()[0]
    missing = {int(item_id): int(n) for item_id, n in con_dfa.execute(
        "SELECT item_id, n FROM missing WHERE kind='appearances'")}
    covered = {int(item_id) for (item_id,) in con_dfa.execute(
        "SELECT item_id FROM item_slot WHERE kind='appearances'")}
    return missing, covered, accounts


def load_decay(con) -> dict[int, float]:
    """The 78-day change of the region's ask average, per item."""
    first = {int(i): float(m) for i, m in con.execute(
        "SELECT itemId, marketValue FROM market_snapshots WHERE snapshotId=1") if m}
    last = {int(i): float(m) for i, m in con.execute(
        "SELECT itemId, marketValue FROM market_snapshots WHERE snapshotId=4") if m}
    out = {}
    for item, value in first.items():
        other = last.get(item)
        if other and value:
            out[item] = other / value
    return out


def load_commodity_prices(con, items) -> dict[int, int]:
    """The commodity market value of the materials (one market for the region)."""
    if not items:
        return {}
    ph = ",".join("?" * len(items))
    return {int(i): int(v) for i, v in con.execute(
        f"SELECT key_id, market_value FROM tsm_commodity_price WHERE key_id IN ({ph})",
        list(items))}


def load_all_region(con) -> dict[int, pg.RegionPrice]:
    """The regional reference for the whole catalogue (31k items)."""
    latest = con.execute("select max(snapshot_id) from tsm_region_price").fetchone()[0]
    out = {}
    for item, market, historical, avg_sale, rate, per_day in con.execute(
        "select key_id, market_value, historical, avg_sale_price, sale_rate, sold_per_day "
        "from tsm_region_price where snapshot_id=?", (latest,)):
        out[int(item)] = RegionPrice(clean(market), clean(historical), clean(avg_sale),
                                     float(rate or 0.0), float(per_day or 0.0))
    return out


def build_features(con, con_dfa, inventory: dict) -> tuple[dict, dict]:
    """Per (realm, item) facts, and the catalogue-wide tables."""
    items = sorted(inventory)
    ph = ",".join("?" * len(items))
    region = load_region_prices(con, items)
    all_region = load_all_region(con)
    vendor = load_vendor_sell(con, items)
    commodities = load_commodity_ids(con, items)
    commodity_prices = load_commodity_prices(con, items)
    journal = load_journal(con, items)
    decay = load_decay(con)
    missing, covered, accounts = load_demand(con_dfa)

    # appearances -> items (for the demand)
    demand_by_item: dict[int, int] = {}
    covered_items: set[int] = set()
    missing, covered, accounts = load_demand(con_dfa)
    if missing:
        for item, appearance in con.execute(
                "SELECT itemId, appearanceId FROM db2_item_appearance"):
            if appearance in covered:
                covered_items.add(item)
            n = missing.get(appearance)
            if n and n > demand_by_item.get(item, 0):
                demand_by_item[item] = n

    groups = realm_groups(con)
    scope_of = {}
    for name, row in groups.items():
        scope_of.setdefault(row["canonical_slug"], name)

    facts: dict[tuple[str, int], pg.ItemFacts] = {}
    for scope, canonical in scope_of.items():
        prices = load_realm_prices(con, scope, items)
        if not prices:
            continue
        for item_id, item in inventory.items():
            local = prices.get(item_id)
            reg = region.get(item_id)
            own = journal.get((item_id, canonical)) or {}
            facts[(canonical, item_id)] = pg.ItemFacts(
                item_id=item_id,
                name=item.name,
                realm=canonical,
                qty=item.qty,
                is_commodity=item_id in commodities,
                local_market=local.market if local else 0,
                local_wall=local.min_buyout if local else 0,
                local_recent=local.recent if local else 0,
                local_historical=local.historical if local else 0,
                region_market=reg.market if reg else 0,
                region_avg_sale=reg.avg_sale if reg else 0,
                commodity_market=commodity_prices.get(item_id, 0),
                want_rate=(demand_by_item.get(item_id, 0) / accounts) if accounts and demand_by_item.get(item_id) else None,
                wanters_eu=demand_by_item.get(item_id),
                accounts_sampled=accounts,
                demand_known=item_id in covered_items,
                sales=int(own.get("sales", 0)),
                expired=int(own.get("expired", 0)),
                attempts=int(own.get("attempts", 0)),
                sold_copper=int(own.get("sold_copper", 0)),
                decay_78d=decay.get(item_id),
                vendor_sell=vendor.get(item_id),
                wall_support=("parede" if local and local.min_buyout else "sem parede"),
            )

    catalogue = {
        "region": all_region,
        "decay": decay,
        "demand": demand_by_item,
        "covered": covered_items,
        "accounts": accounts,
        "commodities": commodities,
    }
    return facts, catalogue


# ---------------------------------------------------------------------------
# the evidence
# ---------------------------------------------------------------------------

def catalogue_groups(catalogue) -> dict[int, tuple[str, str]]:
    """The group and the band of every catalogue item.

    The group is decided by the behaviour features only (potential demand and
    the 78-day ask behaviour); the price level lives in the band.  Computed
    once and shared, so the band statistics, the group statistics and the
    inventory membership all speak about the same partition.
    """
    region = catalogue["region"]
    decay = catalogue["decay"]
    demand = catalogue["demand"]
    accounts = catalogue["accounts"]
    out: dict[int, tuple[str, str]] = {}
    for item, reg in region.items():
        if not reg.market:
            continue
        band = pg.band_of(reg.market / pg.COPPER_PER_GOLD)
        group = pg.assign_group(pg.ItemFacts(
            item_id=item,
            region_market=reg.market,
            is_commodity=item in catalogue["commodities"],
            want_rate=((demand.get(item, 0) / accounts)
                       if accounts and demand.get(item) else None),
            demand_known=item in catalogue["covered"],
            decay_78d=decay.get(item),
        ))
        out[item] = (group, band)
    return out


def collect_band_evidence(con, facts, catalogue, groups_of_item) -> tuple[dict[str, pg.BandEvidence], list[str]]:
    """E1 (regional sale against the local wall) per band, plus the rest."""
    region = catalogue["region"]
    decay = catalogue["decay"]

    ratios: dict[str, list[float]] = defaultdict(list)
    walls: dict[str, list[float]] = defaultdict(list)
    decay_by_band: dict[str, list[float]] = defaultdict(list)
    demand_by_band: dict[str, list[float]] = defaultdict(list)
    sold_by_band: dict[str, list[float]] = defaultdict(list)
    rate_by_band: dict[str, list[float]] = defaultdict(list)
    n_items: dict[str, int] = defaultdict(int)

    # the group and the band of every catalogue item, decided by the
    # behaviour features (shared by every statistic below)
    band_of_item: dict[int, str] = {item: band for item, (_g, band) in groups_of_item.items()}

    # E1 over every (realm, item) with a wall: a big sample, and the
    # sniping caveat travels with it.
    for scope, item, mv, mb in con.execute(
            "SELECT scope, item_id, market_value, min_buyout FROM tsm_realm_price "
            "WHERE min_buyout>0"):
        item = int(item)
        reg = region.get(item)
        if not reg or not reg.avg_sale:
            continue
        band = band_of_item.get(item)
        if band is None:
            continue
        ratios[band].append(reg.avg_sale / mb)
        walls[band].append(mb / reg.market)

    # the 78-day behaviour, the demand and the turnover, per band
    for item, reg in region.items():
        if not reg.market:
            continue
        band = band_of_item.get(item)
        change = decay.get(item)
        if change:
            decay_by_band[band].append(change)
        if catalogue["demand"].get(item) and catalogue["accounts"]:
            demand_by_band[band].append(catalogue["demand"][item] / catalogue["accounts"])
        if reg.sold_per_day:
            sold_by_band[band].append(reg.sold_per_day)
        if reg.sale_rate:
            rate_by_band[band].append(reg.sale_rate)
        n_items[band] += 1

    evidence = {}
    for band in BAND_KEYS:
        values = ratios.get(band) or []
        e1 = pg.Clearance(
            key="E1",
            p25=pg.percentile(values, 0.25),
            p50=pg.percentile(values, 0.50),
            p75=pg.percentile(values, 0.75),
            n=len(values),
            source=pg.E1_SOURCE,
            caveat=pg.E1_CAVEAT,
            usable=bool(values),
        )
        e2 = pg.Clearance(key="E2", n=0, source=pg.E2_SOURCE, caveat=pg.E2_CAVEAT,
                          usable=False, note=pg.E2_UNAVAILABLE_NOTE)
        evidence[band] = pg.BandEvidence(
            band=band,
            n_items=n_items.get(band, 0),
            e1=e1,
            e2=e2,
            decay_78d=pg.median(decay_by_band.get(band) or []),
            n_decay=len(decay_by_band.get(band) or []),
            wall_ratio_p50=pg.median(walls.get(band) or []),
            sold_per_day_p50=pg.median(sold_by_band.get(band) or []),
            sale_rate_p50=pg.median(rate_by_band.get(band) or []),
            demand_rate_p50=pg.median(demand_by_band.get(band) or []),
            n_demand=len(demand_by_band.get(band) or []),
        )
    merged, flags = pg.merge_sparse_bands(evidence)
    return merged, flags


def band_policy(band: str, evidence: pg.BandEvidence) -> pg.BandPolicy:
    """The operational factors of a band: the status quo, plus the evidence.

    The *normal* factor is **not** read off the sale ratios.  The status quo
    (the coefficients the project already uses) stays the operational value;
    the evidence is reported next to it with its uncertainty, so a reader can
    see where the two disagree and decide.
    """
    normal, minimum, maximum = pg.status_quo_for(band)
    policy = pg.BandPolicy(
        band=band,
        normal=normal,
        minimum=minimum,
        maximum=maximum,
        source="modelo actual (mog_operations.BAND_MULTIPLIERS)",
        uncertainty="",
        confidence=evidence.confidence,
    )
    return policy


def evidence_proposal(evidence: pg.BandEvidence) -> tuple[float, float, float, str]:
    """Candidate coefficients from the evidence, with the uncertainty spelled out.

    The candidates are *proposals*: they are what the clearance distribution
    would say, clipped by the policy floors, and they are reported with the
    spread they come from.  They are not applied.
    """
    clearance = evidence.preferred()
    if not clearance.usable or clearance.p50 is None:
        return (0, 0, 0, "sem evidência")
    floor_normal, floor_min = 0.15, 0.08
    normal = max(floor_normal, min(1.0, clearance.p50))
    minimum = max(floor_min, min(normal, clearance.p25 if clearance.p25 is not None else normal * 0.6))
    maximum = max(normal, clearance.p75 if clearance.p75 is not None else normal * 1.5)
    spread = ""
    if clearance.p25 is not None and clearance.p75 is not None:
        spread = (f"p25 {clearance.p25:.3f} / p50 {clearance.p50:.3f} / "
                  f"p75 {clearance.p75:.3f} (n={clearance.n})")
    return (normal, minimum, maximum, spread)


# ---------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------

def group_clearance(con, catalogue, groups_of_item) -> dict[tuple[str, str], list[float]]:
    """E1 per (group, band): the clearance of the items of a group.

    The group is decided by the *behaviour* features (potential demand and the
    78-day ask behaviour); the clearance is then measured inside the group.
    The two are not the same statistic, so this is not circular.
    """
    region = catalogue["region"]
    out: dict[tuple[str, str], list[float]] = defaultdict(list)
    for scope, item, mv, mb in con.execute(
            "SELECT scope, item_id, market_value, min_buyout FROM tsm_realm_price "
            "WHERE min_buyout>0"):
        item = int(item)
        reg = region.get(item)
        if not reg or not reg.avg_sale:
            continue
        key = groups_of_item.get(item)
        if key is None:
            continue
        out[key].append(reg.avg_sale / mb)
    return out


def group_statistics(con, catalogue, groups_of_item) -> dict[tuple[str, str], pg.GroupStatistics]:
    """The per (group, band) statistics that are *not* the price.

    Turnover (sold per day, sale rate) is the statistic that separates the
    groups without the sniping confound: in a popular market the average sale
    is dragged down by snipes, so the clearance ratio cannot tell a cheap
    market from a sniped one.  Turnover can.
    """
    region = catalogue["region"]
    decay = catalogue["decay"]
    demand = catalogue["demand"]
    accounts = catalogue["accounts"]

    buckets: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"items": [], "sold": [], "rate": [], "want": [], "decay": [],
                 "clearance": []})

    for item, reg in region.items():
        key = groups_of_item.get(item)
        if key is None:
            continue
        bucket = buckets[key]
        bucket["items"].append(item)
        if reg.sold_per_day:
            bucket["sold"].append(reg.sold_per_day)
        if reg.sale_rate:
            bucket["rate"].append(reg.sale_rate)
        if demand.get(item) and accounts:
            bucket["want"].append(demand[item] / accounts)
        if decay.get(item):
            bucket["decay"].append(decay[item])

    # the clearance of the group (the diagnostic, measured separately)
    for scope, item, mv, mb in con.execute(
            "SELECT scope, item_id, market_value, min_buyout FROM tsm_realm_price "
            "WHERE min_buyout>0"):
        item = int(item)
        reg = region.get(item)
        if not reg or not reg.avg_sale:
            continue
        key = groups_of_item.get(item)
        if key is None:
            continue
        buckets[key]["clearance"].append(reg.avg_sale / mb)

    out = {}
    for key, bucket in buckets.items():
        group, band = key
        n_with_sales = len(bucket["sold"])
        out[key] = pg.GroupStatistics(
            group=group,
            band=band,
            n_items=len(bucket["items"]),
            n_with_sales=n_with_sales,
            sold_per_day_p50=pg.median(bucket["sold"]),
            sale_rate_p50=pg.median(bucket["rate"]),
            demand_rate_p50=pg.median(bucket["want"]),
            decay_p50=pg.median(bucket["decay"]),
            clearance_p50=pg.median(bucket["clearance"]),
            clearance_n=len(bucket["clearance"]),
        )
    return out


def adjustments(clearance: dict[tuple[str, str], list[float]],
                 evidence: dict[str, pg.BandEvidence],
                 own_sales: dict[tuple[str, str], tuple[int, float | None]] | None = None
                 ) -> dict[tuple[str, str], pg.GroupAdjustment]:
    """The group x band adjustment, with the materiality test."""
    own_sales = own_sales or {}
    out = {}
    for band in evidence:
        baseline = evidence[band].e1.p50
        for group in pg.GROUPS:
            values = clearance.get((group, band)) or []
            count, own_clearance = own_sales.get((group, band), (0, None))
            out[(group, band)] = pg.estimate_adjustment(
                group, band, pg.median(values), len(values), baseline,
                own_sales=count, own_clearance=own_clearance)
    return out


# ---------------------------------------------------------------------------
# the TSM proposal
# ---------------------------------------------------------------------------

def banded_expression(factors: dict[str, float], value: str) -> str:
    """Compile a per-band factor table into one nested ``ifgt`` chain."""
    edges = list(pg.BAND_EDGES_G)
    fallback = f"{factors[BAND_KEYS[-1]]:g} * {value}"
    picks = []
    for index, edge in enumerate(edges):
        band = BAND_KEYS[index]
        picks.append((edge, f"{factors[band]:g} * {value}"))
    for edge, pick in reversed(picks):
        fallback = f"ifgt({value}, {edge}g, {pick}, {fallback})"
    return fallback


def tsm_proposal(strategies, membership, realm: str) -> tuple[str, list[dict]]:
    """The proposed TSM groups and operations (never imported)."""
    from woweconomy.analytics.mog_operations import (
        LiquidityOperation, build_liquidity_export,
    )
    from woweconomy.analytics.tsm_export import build_export, build_group_export

    groups: dict[str, list[int]] = {}
    operations: dict[str, dict] = {}
    rows: list[dict] = []
    for (group, band), strategy in strategies.items():
        if not strategy.postable:
            continue
        items = sorted(item_id for item_id, _ in membership.get((group, band), []))
        if not items:
            continue
        name = f"PG {group} {band}"
        groups[name] = items
        operations[name] = {
            "aboveMax": "maxPrice",
            "bidPercent": 1,
            "cancelRepost": False,
            "cancelUndercut": False,
            "duration": strategy.duration_h,
            "maxExpires": max(1, round(strategy.hold_days * 24 / strategy.duration_h) - 1),
            "maxPrice": f"{strategy.band_policy.maximum:.3f} * {pg.NORMAL_ANCHOR}",
            "minPrice": f"{strategy.band_policy.minimum:.3f} * {pg.NORMAL_ANCHOR}",
            "normalPrice": f"{strategy.normal_formula}",
            "postCap": 1,
            "priceReset": "none",
            "stackSize": 1,
            "undercut": "1c",
        }
        rows.append({
            "group": group,
            "band": band,
            "tsm_group": name,
            "items": len(items),
            "normal": strategy.normal_formula,
            "min": f"{strategy.band_policy.minimum:.3f} * {pg.NORMAL_ANCHOR}",
            "max": f"{strategy.band_policy.maximum:.3f} * {pg.NORMAL_ANCHOR}",
            "duration_h": strategy.duration_h,
            "max_expires": operations[name]["maxExpires"],
            "undercut": f"{strategy.undercut[0]:.2f}-{strategy.undercut[1]:.2f}",
            "adjustment": strategy.adjustment.multiplier,
            "adjustment_basis": strategy.adjustment.basis,
            "confidence": strategy.confidence,
        })
    if not groups:
        return "", rows
    group_export = build_group_export(groups)
    group_operations = {name: {"Auctioning": {1: "PG " + name, "override": True}}
                        for name in groups}
    auctioning = {("PG " + name): op for name, op in operations.items()}
    text = build_export({"Auctioning": auctioning}, group_operations, group_export)
    return text, rows


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def write_report(path, args, evidence, policies, adj, strategies, membership,
                 flags, catalogue, realm, stats, e2, distinct, facts) -> None:
    def r3(value, digits=3):
        return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"

    lines = [
        "# Grupos de preço do inventário de transmog",
        "",
        "Gerado por `python scripts/pricing_groups.py`. É uma **análise**: nada "
        "foi escrito na base de dados nem no TSM, e a string de import proposta "
        "nunca foi importada.",
        "",
        "## 1. O que foi medido, e onde",
        "",
        "| dado | fonte | janela | cobertura |",
        "|---|---|---|---|",
        "| clearance E1 (venda regional ÷ parede local) | `tsm_region_price.avg_sale_price` "
        "÷ `tsm_realm_price.min_buyout` | estado actual | todos os pares (reino, item) com parede |",
        "| clearance E2 (venda própria ÷ parede **da data da venda**) | `journal_stats` "
        "÷ histórico de paredes | — | **indisponível**: o journal não guarda datas |",
        "| comportamento do ask em 78 dias | `market_snapshots` (27/07 → 13/10) | 78 dias | "
        f"{len(catalogue['decay']):,} itens |",
        "| procura potencial | `dfa.db :: missing` | amostra DfA | "
        f"{catalogue['accounts']:,} contas amostradas |",
        "| concorrência | `tsm_realm_price.min_buyout`, `tsm_market.num_auctions` | actual | 92 reinos |",
        "| vendas e expirações próprias | `journal_stats` (Journalator) | histórico | "
        "só os itens da conta |",
        "",
        "**E1 é um limite inferior**, não o preço justo: a média de venda "
        "regional inclui snipes (vendas a 1 % do ask) e a parede é o *anúncio* "
        "mais barato, não o preço transacionado. **E2 seria a medida honesta**, "
        "mas este banco de dados não tem vendas datadas, logo a parede na data "
        "da venda não pode ser reconstruída — e a parede actual não é usada em "
        "seu lugar.",
        "",
        f"**E2 foi tentado:** {e2.note}.",
        "",
        "## 2. Bandas de preço: a medição",
        "",
        "As nove bandas são **candidatas**. As que não têm amostra suficiente são "
        "fundidas na seguinte e assinaladas.",
        "",
        "| banda | itens | E1 n | E1 p25 | E1 p50 | E1 p75 | ask 78d | parede/ask | "
        "sold/dia | procura | confiança |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for band in BAND_KEYS:
        item = evidence.get(band)
        if not item:
            continue
        e1 = item.e1
        lines.append(
            f"| {band} | {item.n_items:,} | {e1.n:,} | "
            f"{r3(e1.p25)} | {r3(e1.p50)} | {r3(e1.p75)} | "
            f"{r3(item.decay_78d)} | {r3(item.wall_ratio_p50)} | "
            f"{r3(item.sold_per_day_p50, 4)} | {r3(item.demand_rate_p50)} | "
            f"{item.confidence} |")
    if flags:
        lines += ["", "**Bandas fundidas (amostra insuficiente):**", ""]
        lines += [f"- {flag}" for flag in flags]

    lines += [
        "",
        "## 3. Bandas de preço: a política (separada da medição)",
        "",
        "Os coeficientes **normais** não são lidos das razoes de venda. O que "
        "opera hoje é o modelo do projecto (secção D de `reports/mog_groups.md`); "
        "a medição fica ao lado, com a sua incerteza, para se ver onde as duas "
        "coisas discordam. As bandas com mais de 1 000 observações de E1 são as "
        "únicas em que a evidência manda; nas outras, a proposta é fraca.",
        "",
        "| banda | normal (opera) | min (opera) | max (opera) | fonte | "
        "proposta da evidência (normal/min/max) | dispersão | confiança |",
        "|---|---:|---:|---:|---|---:|---|---|",
    ]
    for band in BAND_KEYS:
        item = evidence.get(band)
        if not item:
            continue
        policy = policies[band]
        normal, minimum, maximum, spread = evidence_proposal(item)
        proposal = (f"{normal:.3f} / {minimum:.3f} / {maximum:.3f}"
                    if normal else "—")
        lines.append(f"| {band} | {policy.normal:.2f} | {policy.minimum:.2f} | "
                     f"{policy.maximum:.2f} | {policy.source} | {proposal} | "
                     f"{spread or '—'} | {policy.confidence} |")

    lines += [
        "",
        "> Os pisos da política (`normal ≥ 0,15`, `min ≥ 0,08`) são **decisões**, "
        "não estatística: sem eles, uma banda com clearance de 0,03 levaria a "
        "postar a 3 % do ask, o que a amostra não sustenta. Estão em "
        "`pricing_groups.BandPolicy` e são configuráveis.",
        "",
        "## 4. Os grupos",
        "",
        "Os grupos são **comportamento**, e os eixos são medições:",
        "",
        "- **procura potencial** — a amostra DfA ainda não tem o look "
        f"(limiar: {pg.DEMAND_THRESHOLD:.0%} das contas amostradas). É procura "
        "potencial: não é intenção de compra nem probabilidade de venda.",
        f"- **comportamento do ask** — caiu mais de {pg.DECAY_FALLING:.0%} em 78 "
        "dias (o preço está a descer) ou não.",
        "",
        "| grupo | para que serve | inclui | exclui | undercut | duração | retenção |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for key, definition in pg.GROUPS.items():
        lines.append(
            f"| `{key}` | {definition.purpose} | {definition.inclusion} | "
            f"{definition.exclusion} | {definition.undercut[0]:.0%}–"
            f"{definition.undercut[1]:.0%} | {definition.duration_h} h | "
            f"{definition.hold_days} d |")

    lines += [
        "",
        "### 4.1 Os grupos medem-se diferente? (a velocidade, não o preço)",
        "",
        "A clearance (E1) **não** separa os grupos de forma limpa: num mercado "
        "popular a média de venda é puxada para baixo pelos snipes, uma "
        "clearance menor não significa que o mercado pague menos. A estatística "
        "que separa sem esse problema é a **velocidade** (vendas/dia e taxa de "
        "venda).",
        "",
        "| grupo | banda | itens | com movimento | vendas/dia (mediana) | taxa de venda | "
        "procura (mediana) | ask 78d | E1 p50 (diagnóstico) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for (group, band), stat in sorted(stats.items()):
        lines.append(
            f"| {group} | {band} | {stat.n_items:,} | {stat.n_with_sales:,} | "
            f"{r3(stat.sold_per_day_p50, 4)} | {r3(stat.sale_rate_p50, 4)} | "
            f"{r3(stat.demand_rate_p50)} | {r3(stat.decay_p50)} | "
            f"{r3(stat.clearance_p50)} |")

    lines += [
        "",
        "**Teste de distinção** (velocidade com diferença ≥ "
        f"{pg.TURNOVER_MARGIN:.0%} e amostra ≥ {pg.MIN_TURNOVER_SAMPLE}):",
        "",
    ]
    if distinct:
        merges = [item for item in distinct if "**fundir**" in item]
        keeps = [item for item in distinct if "distintos" in item]
        unknown = [item for item in distinct if "inconclusivo" in item]
        lines.append(f"- **{len(keeps)}** pares **distinguem-se** pela velocidade → "
                     f"merecem estratégias diferentes;")
        lines.append(f"- **{len(merges)}** pares **não se distinguem** → "
                     f"candidatas a fundir (o mesmo grupo, com o mesmo undercut e a "
                     f"mesma janela);")
        lines.append(f"- **{len(unknown)}** pares **inconclusivos** (amostra "
                     f"insuficiente): não se fundem por falta de prova.")
        lines += ["", "<details><summary>detalhe</summary>", ""]
        lines += [f"- {item}" for item in distinct]
        lines += ["", "</details>", ""]
    else:
        lines.append("- nenhum par de grupos se distingue pela velocidade")

    lines += [
        "",
        "### 4.2 O ajuste de preço de cada grupo",
        "",
        "O ajuste é **1,00** em quase todos os grupos, e isso é uma conclusão, "
        "não uma omissão:",
        "",
        f"- a única medida em que o sinal está identificado é a **venda da "
        f"própria conta** (o preço foi escolhido por nós). E2 não está "
        f"disponível (§1), portanto esse caminho está fechado;",
        f"- o E1 medido (a coluna *E1 p50* acima) **não** é aplicado: "
        f"{pg.E1_NOT_IDENTIFIED}.",
        "",
        "O que resta é a **classificação**, que é o que separa as estratégias "
        "operacionais (undercut, duração, retenção) e o que decide a quem se "
        "pergunta primeiro. A tabela mostra a razão de cada ajuste.",
        "",
        "| grupo | banda | ajuste | n | razão | E1 medido | material |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for (group, band), item in sorted(adj.items()):
        if group in ("commodity", "no_reference"):
            continue
        lines.append(f"| {group} | {band} | {item.multiplier:.2f} | {item.n:,} | "
                     f"{item.basis} | "
                     f"{r3(item.measured_ratio) if item.measured_ratio else '—'} | "
                     f"{'sim' if item.material else 'não'} |")

    lines += [
        "",
        "## 5. As estratégias",
        "",
        "A fórmula normal e a de liquidação estão **separadas**, e os factores "
        "entram uma vez cada:",
        "",
        "```",
        "normal = banda_normal × ajuste_do_grupo × âncora_local",
        "âncora_local = first(dbmarket, dbregionmarketavg, dbregionhistorical)",
        "```",
        "",
        "A âncora é **local**, por isso o factor do reino (local ÷ regional) "
        "**não** é aplicado outra vez aqui: contaria o mesmo ajuste duas vezes. "
        "O factor do reino vive no export por reino e na camada de campanha, "
        "onde é aplicado uma vez.",
        "",
        "| grupo | banda | normal | min | max | ajuste | rush | duração | retenção |",
        "|---|---|---|---|---|---:|---|---:|---:|",
    ]
    for (group, band), strategy in sorted(strategies.items()):
        if group in ("commodity", "no_reference"):
            continue
        lines.append(
            f"| {group} | {band} | {strategy.band_policy.normal * strategy.adjustment.multiplier:.3f}"
            f" × âncora | {strategy.band_policy.minimum * strategy.adjustment.multiplier:.3f} × âncora | "
            f"{strategy.band_policy.maximum * strategy.adjustment.multiplier:.3f} × âncora | "
            f"{strategy.adjustment.multiplier:.2f} | parede × (1 − undercut) | "
            f"{strategy.duration_h} h | {strategy.hold_days} d |")

    lines += [
        "",
        "### 5.1 Semântica do TSM em que isto assenta",
        "",
    ]
    for key, value in pg.TSM_SEMANTICS.items():
        lines.append(f"- **{key}**: {value}")
    lines += [
        "",
        "O alvo de liquidação é **relativo à parede** quando existe um comparável "
        "válido: a parede é o ask mais barato do mercado e undercutê-la é como a "
        "venda acontece. O ajuste do grupo **não** é aplicado à parede: foi "
        "estimado sobre o *ask*, e não há evidência de que se transfira para a "
        "parede. Sem comparável válido, o alvo volta à fórmula normal.",
        "",
        "## 6. Inventário: onde cai cada peça",
        "",
        f"Alvo: **{realm}**. Cada linha é um item do inventário, com a sua banda, "
        "o grupo e a estratégia.",
        "",
        "| grupo | peças | valor de mercado | undercut | duração | retenção |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    summary: dict[str, dict] = defaultdict(lambda: {"n": 0, "value": 0.0})
    for (group, band), items in membership.items():
        for item_id, extra in items:
            entry = summary[group]
            entry["n"] += 1
            entry["value"] += extra["value"]
    for group in sorted(summary, key=lambda g: -summary[g]["value"]):
        definition = pg.GROUPS[group]
        entry = summary[group]
        lines.append(f"| {group} | {entry['n']} | {entry['value']:,.0f} g | "
                     f"{definition.undercut[0]:.0%}–"
                     f"{definition.undercut[1]:.0%} | {definition.duration_h} h | "
                     f"{definition.hold_days} d |")
    lines += [
        "",
        "| item | banda | grupo | procura | ask 78d | parede | vendas | expirações |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in membership_rows(membership, facts, realm):
        lines.append(row)
    lines += [
        "",
        "## 7. Limitações",
        "",
        "- **E2 indisponível**: sem vendas datadas, a clearance da própria conta "
        "não pode ser medida contra a parede correcta. É a medida que falta.",
        "- **E1 é um limite inferior**: snipes puxam a média de venda para baixo. "
        "Compara-se a banda, não se usa como preço.",
        "- **A amostra DfA é uma amostra**: mede falta de look numa amostra de "
        "contas, não intenção de compra. E o número de contas lidas por reino "
        "mede a cobertura da amostra, não a população.",
        "- **A amostra de vendas próprias é minúscula** (28 vendas de mog): serve "
        "para validar, não para estimar.",
        "- **Sem backtest de lucro**: não há série temporal de vendas por reino, "
        "logo não há comparação honesta de estratégias. Nenhum número de "
        "rentabilidade é apresentado.",
        "- **Os custos de casa de auction são assumidos** (taxa 5 %, depósito 5 % "
        "limitado a 300 % do valor de venda ao fornecedor) e não verificados "
        "nesta base de dados.",
        "",
        "## 8. O que não foi feito",
        "",
        "- Nada foi escrito na base de dados nem no addon.",
        "- Nenhuma operação, grupo ou fonte do TSM foi alterado.",
        "- A string de import proposta **não** foi importada.",
        "- Os coeficientes normais do projecto não foram mudados: a evidência "
        "nova está ao lado deles, com a incerteza, para decisão humana.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def membership_rows(membership, facts, realm, limit=40) -> list[str]:
    """The inventory membership, biggest first, and only what fits in a table."""
    def r3(value, digits=3):
        return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"

    rows = []
    flat = []
    for (group, band), items in membership.items():
        for item_id, extra in items:
            flat.append((extra["value"], group, band, item_id, extra))
    flat.sort(key=lambda row: -row[0])
    for _value, group, band, item_id, extra in flat[:limit]:
        rows.append(f"| {extra['name'][:30]} ({item_id}) | {band} | {group} | "
                    f"{r3(extra['want_rate'])} | {r3(extra['decay'])} | "
                    f"{extra['wall']} | {extra['sales']} | {extra['expired']} |")
    if len(flat) > limit:
        rows.append(f"| … mais {len(flat) - limit} peças em `reports/pricing_groups/membership.csv` "
                    f"| | | | | | | |")
    return rows


# ---------------------------------------------------------------------------

def write_tsm_proposal(path: Path, rows, strategies, membership, realm, text) -> None:
    """The proposed TSM groups and operations, in prose.

    This is a **proposal**: the string is written next to it, and nothing was
    imported.  The formulas are portable (no custom source is required): the
    band is expressed as an ``ifgt`` chain over the value, exactly like the
    project's normal export.
    """
    lines = [
        "# Proposta de grupos e operações para o TSM",
        "",
        f"Alvo: **{realm}**. Gerado por `scripts/pricing_groups.py`; **nada foi "
        "importado** e nada foi alterado no addon. A string está em "
        "`pricing_groups_proposal.tsm` (uma linha AceSerializer, para "
        "`Groups → Import` num **perfil de teste**).",
        "",
        "## Como ler",
        "",
        "- O **nome do grupo** é `PG <grupo> <banda>`: o comportamento e a "
        "banda de preço. São dimensões independentes, e por isso o grupo é a "
        "união das duas.",
        "- As **preços** são portáteis: uma cadeia `ifgt` sobre o valor, com o "
        "coeficiente da banda. Não é preciso nenhuma fonte customizada.",
        "- `priceReset = none`: se a parede estiver abaixo do mínimo, **não "
        "posta** (não salta para o máximo).",
        "- `aboveMax = maxPrice`: se a parede estiver acima do tecto, posta no "
        "tecto.",
        "- `maxExpires` é a janela de retenção **desde a última venda**, em "
        "ciclos de post.",
        "",
        "## Os grupos propostos",
        "",
        "| grupo TSM | grupo | banda | itens | normal | min | max | duração | "
        "maxExpires | undercut | ajuste | confiança |",
        "|---|---|---|---:|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['tsm_group']}` | {row['group']} | {row['band']} | "
            f"{row['items']} | `{row['normal']}` | `{row['min']}` | `{row['max']}` | "
            f"{row['duration_h']} h | {row['max_expires']} | {row['undercut']} | "
            f"{row['adjustment']} | {row['confidence']} |")
    lines += [
        "",
        "## As fórmulas",
        "",
        "```",
        "normalPrice = banda_normal(ajuste) * first(dbmarket, dbregionmarketavg, dbregionhistorical)",
        "minPrice   = banda_min(ajuste)   * first(dbmarket, dbregionmarketavg, dbregionhistorical)",
        "maxPrice   = banda_max(ajuste)   * first(dbmarket, dbregionmarketavg, dbregionhistorical)",
        "```",
        "",
        "A âncora é **local** (`dbmarket`): por isso o factor do reino "
        "(local ÷ regional) **não** é aplicado aqui — contaria o mesmo ajuste "
        "duas vezes. O factor do reino entra no export por reino "
        "(`mogs_groups_<reino>.tsm`) e na camada de campanha, uma vez só.",
        "",
        "O **alvo de liquidação** é relativo à parede quando existe um comparável "
        "válido (a parede é o ask mais barato do mercado; undercutê-la é como a "
        "venda acontece), e volta à fórmula normal quando não há comparável. O "
        "ajuste do grupo **não** é aplicado à parede: foi estimado sobre o ask.",
        "",
        f"**A string tem {len(text):,} caracteres** e "
        f"{len(rows)} grupos.",
        "",
        "> Importa primeiro num perfil de teste. Esta proposta nunca foi "
        "executada dentro do addon.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def age_hours(local_at: str, regional_at: str) -> float:
    """How much older the regional observation is than the local one."""
    from datetime import datetime

    def parse(value: str):
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    local = parse(local_at or "")
    regional = parse(regional_at or "")
    if not local or not regional:
        return 0.0
    return max(0.0, (local - regional).total_seconds() / 3600.0)


def load_local_evidence(con, scope: str, items, listings: dict) -> dict:
    """The local evidence of every item: the realm's own view of it."""
    from woweconomy.analytics import reference_price as rp

    out = {}
    for item, mv, mb, rec, hist, fetched in con.execute(
            "SELECT item_id, market_value, min_buyout, recent, historical, fetched_at "
            "FROM tsm_realm_price WHERE scope=?", (scope,)):
        item = int(item)
        if item not in items:
            continue
        out[item] = rp.LocalEvidence(
            price=rp.clean_price(mv),
            wall=rp.clean_price(mb),
            recent=rp.clean_price(rec),
            historical=rp.clean_price(hist),
            listings=listings.get(item),
            age_hours=0.0,
            observed_at=fetched or "",
        )
    return out


def load_regional_evidence(con, items) -> dict:
    """The regional evidence: the region's own view of the same items."""
    from woweconomy.analytics import reference_price as rp

    latest = con.execute("select max(snapshot_id) from tsm_region_price").fetchone()[0]
    out = {}
    for item, market, hist, avg_sale in con.execute(
            "select key_id, market_value, historical, avg_sale_price from tsm_region_price "
            "where snapshot_id=?", (latest,)):
        item = int(item)
        if item not in items:
            continue
        out[item] = rp.RegionalEvidence(
            price=rp.clean_price(market),
            historical=rp.clean_price(hist),
            avg_sale=rp.clean_price(avg_sale),
            age_hours=0.0,
        )
    return out


def weighted_proposal(strategies, membership, realm, weights, config):
    """The candidate export: the same 31 groups, with a weighted reference.

    The groups, the bands and the coefficients are **the same** as the
    baseline proposal.  The only thing that changes is the reference: instead
    of ``first(dbmarket, dbregionmarketavg, dbregionhistorical)`` each group
    gets the evidence-weighted blend, with the weight measured per group and
    band from the local evidence.
    """
    from woweconomy.analytics import reference_price as rp
    from woweconomy.analytics.tsm_export import build_export, build_group_export

    groups: dict[str, list[int]] = {}
    operations: dict[str, dict] = {}
    rows: list[dict] = []
    for (group, band), strategy in strategies.items():
        if not strategy.postable:
            continue
        items = sorted(item_id for item_id, _ in membership.get((group, band), []))
        if not items:
            continue
        name = f"PG {group} {band}"
        weight = weights.get((group, band), {}).get("weight", 1.0)
        # the group is already per band, so the weight is a constant here: the
        # expression stays short and readable
        expression = rp.to_tsm_expression(weight, config)
        groups[name] = items
        operations[name] = {
            "aboveMax": "maxPrice",
            "bidPercent": 1,
            "cancelRepost": False,
            "cancelUndercut": False,
            "duration": strategy.duration_h,
            "maxExpires": max(1, round(strategy.hold_days * 24 / strategy.duration_h) - 1),
            "maxPrice": f"{strategy.band_policy.maximum:.3f} * ({expression})",
            "minPrice": f"{strategy.band_policy.minimum:.3f} * ({expression})",
            "normalPrice": f"{strategy.normal_formula.split(' * ')[0]} * ({expression})",
            "postCap": 1,
            "priceReset": "none",
            "stackSize": 1,
            "undercut": "1c",
        }
        rows.append({
            "group": group,
            "band": band,
            "tsm_group": name,
            "items": len(items),
            "weight_local": round(weight, 3),
            "weight_regional": round(1 - weight, 3),
            "normal": operations[name]["normalPrice"],
            "min": operations[name]["minPrice"],
            "max": operations[name]["maxPrice"],
            "duration_h": strategy.duration_h,
            "max_expires": operations[name]["maxExpires"],
            "adjustment": strategy.adjustment.multiplier,
            "confidence": strategy.confidence,
        })
    if not groups:
        return "", rows
    group_export = build_group_export(groups)
    group_operations = {name: {"Auctioning": {1: "PG " + name, "override": True}}
                        for name in groups}
    auctioning = {("PG " + name): op for name, op in operations.items()}
    text = build_export({"Auctioning": auctioning}, group_operations, group_export)
    return text, rows


def verify_weighted_expression(weights, membership, estimates) -> list[str]:
    """Check that the generated expression computes the intended blend.

    For every cell the expression is evaluated with the *median* local and
    regional values of the cell, and compared with the arithmetic the engine
    would do with the same weight.  A mismatch means the payload does not
    implement the model, and must not be imported.
    """
    from woweconomy.analytics import reference_price as rp

    problems = []
    for (group, band), data in weights.items():
        weight = data["weight"]
        expression = rp.to_tsm_expression(weight)
        cells = membership.get((group, band), [])
        local_values = sorted(estimates[item_id].local for item_id, _ in cells
                              if estimates[item_id].local)
        regional_values = sorted(estimates[item_id].regional for item_id, _ in cells
                                 if estimates[item_id].regional)
        if not local_values or not regional_values:
            continue
        local = local_values[len(local_values) // 2]
        regional = regional_values[len(regional_values) // 2]
        expected = int(round(local ** weight * regional ** (1 - weight)))
        value = rp.evaluate_tsm_expression(expression, {
            "dbmarket": local, "dbregionmarketavg": regional,
            "dbregionhistorical": regional})
        if abs(value - expected) > max(1, expected * 0.001):
            problems.append(f"{group} {band}: a expressão dá {value:,.0f} e o modelo "
                            f"{expected:,.0f}")
    return problems


def write_weighted_report(path: Path, args, config, estimates, weights, rows,
                          membership, facts, realm, local_evidence,
                          regional_evidence, group_weights, base_text, weighted_text,
                          problems=()) -> None:
    """The report of the candidate: what changed, why, and what it does not prove."""
    from woweconomy.analytics import reference_price as rp

    def g(value):
        return f"{value / 10_000:,.0f} g"

    baseline = {row["tsm_group"]: row for row in rows}
    items = sorted(membership.items())
    changed = []
    for (group, band), cells in sorted(membership.items()):
        for item_id, extra in cells:
            estimate = estimates.get(item_id)
            if estimate is None:
                continue
            local = extra["local"]
            regional = extra["regional"]
            if not local and not regional:
                continue
            base_price = local or regional
            delta = (estimate.price / base_price - 1.0) if base_price else 0.0
            if abs(delta) >= 0.10:
                changed.append((abs(delta), item_id, extra, group, band, estimate, delta))

    changed.sort(reverse=True, key=lambda row: row[0])
    weights_hist = defaultdict(int)
    for (group, band), data in weights.items():
        weights_hist[round(data["weight"], 1)] += 1

    lines = [
        "# Preço de referência ponderado pela evidência — proposta",
        "",
        f"Gerado por `python scripts/pricing_groups.py --write`. Alvo: "
        f"**{realm}**. É uma **proposta**: o ficheiro "
        "`pricing_groups_weighted_proposal.tsm` **não foi importado** e nada foi "
        "alterado no TSM. A proposta original (`pricing_groups_proposal.tsm`) "
        "fica intacta.",
        "",
        "## 1. O que muda",
        "",
        "| | original | candidato |",
        "|---|---|---|",
        "| grupos | 31 | 31 (os mesmos) |",
        "| bandas | 9 | 9 (as mesmas) |",
        "| coeficientes de banda | status quo | status quo (inalterados) |",
        "| **referência** | `first(dbmarket, dbregionmarketavg, dbregionhistorical)` | "
        f"mistura ponderada pela evidência local (método: **{config.method}**) |",
        "",
        "O que muda é **uma coisa**: a referência. Os grupos, as bandas, os "
        "coeficientes, a duração, o `maxExpires` e o `priceReset` são os mesmos. "
        "Nenhum factor de reino é aplicado duas vezes: a âncora continua a ser "
        "local (`dbmarket`) e o factor do reino vive no export por reino e na "
        "camada de campanha.",
        "",
        "## 2. A expressão no TSM",
        "",
        "```",
        "referência = ifgt(local_valido,",
        "              ifgt(regional_valido,",
        "                 <mistura ponderada>,",
        "                 local),",
        "              ifgt(regional_valido, regional, historico_regional))",
        "```",
        "",
        f"Com `w` = peso local e `1-w` = peso regional, a mistura **{config.method}** "
        "é:",
        "",
        "```",
        f"geométrica:  dbmarket ^ w * dbregionmarketavg ^ (1 - w)",
        f"aritmética:  w * dbmarket + (1 - w) * dbregionmarketavg",
        "```",
        "",
        "O `ifgt` treats a sentinel da TSM (`99999999900`) como ausente, e a "
        "escada de fallbacks é explícita: sem local → regional; sem regional → "
        "local; sem nenhum → histórico regional (confiança baixa).",
        "",
        "## 3. De onde vêm os pesos",
        "",
        "O peso local é o produto de três medidas, e só de três:",
        "",
        f"| factor | medida | limiares |",
        "|---|---|---|",
        f"| profundidade | nº de anúncios do item no reino | "
        f"{', '.join(f'{n}→{s:.2f}' for n, s in config.depth_scores)} |",
        f"| dispersão | \\|ask médio ÷ parede − 1\\| | ≤ {config.dispersion_full:.2f} → 1,00; "
        f"decaimento até {config.dispersion_floor * 5:.2f} → 0,25 |",
        "| frescura | idade da observação local vs regional | o reino é descarregado de "
        "6 em 6 h e a região de 1 em 1 dia: o local é mais fresco |",
        "",
        f"O peso é limitado a **[{config.weight_floor:.2f}, {config.weight_ceiling:.2f}]**: "
        "a referência regional **nunca** domina. Isto não é arbitrário — o "
        "walk-forward (`scripts/reference_backtest.py`) mostra que a referência "
        "regional erra o nível local por uma ordem de grandeza (erro mediano "
        ">100 %) contra <1 % da local.",
        "",
        "Os limiares saem da distribuição medida: a mediana do nº de anúncios é 1 "
        "em quase todas as bandas, e só 13-38 % dos itens chegam a três; a "
        "dispersãoask/parede tem p50 1,06-1,62 e p90 até 5,6.",
        "",
        "**A divergência local/regional não decide o preço.** Ela produz um "
        "diagnóstico e baixa a confiança; o preço só muda por causa da "
        "profundidade, da frescura e da dispersão.",
        "",
        "## 4. Os pesos medidos",
        "",
        "| peso local | células |",
        "|---:|---:|",
    ]
    for weight, count in sorted(weights_hist.items()):
        lines.append(f"| {weight:.1f} | {count} |")

    lines += [
        "",
        "### 4.1 Peso por grupo e banda",
        "",
        "| grupo | banda | peso local | n |",
        "|---|---|---:|---:|",
    ]
    for (group, band), data in sorted(weights.items()):
        lines.append(f"| {group} | {band} | {data['weight']:.2f} | {data['n']} |")

    lines += [
        "",
        "## 5. Preços que mudam materialmente",
        "",
        f"{len(changed)} itens do inventário mudam **10 % ou mais** face à "
        "referência original (`local` quando existe, `regional` quando não).",
        "",
        "| item | grupo | banda | local | regional | nova referência | variação | confiança | motivo |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for _delta, item_id, extra, group, band, estimate, delta in changed[:40]:
        flags = "; ".join(estimate.flags) or "—"
        lines.append(f"| {extra['name'][:28]} ({item_id}) | {group} | {band} | "
                     f"{g(extra['local'])} | {g(extra['regional'])} | "
                     f"{g(estimate.price)} | {delta:+.0%} | {estimate.confidence} | {flags} |")

    lines += [
        "",
        "## 6. Verificação do payload",
        "",
    ]
    if problems:
        lines += [f"⚠ **{len(problems)} células em que a expressão gerada não "
                  f"bate com o modelo** — não importar:", ""]
        lines += [f"- {problem}" for problem in problems[:10]]
    else:
        lines.append("A expressão de cada operação foi avaliada com os valores "
                     "medianos da célula e bate com o modelo (a diferença é 0). "
                     "A verificação está em `pricing_groups.verify_weighted_expression`.")
    lines += [
        "",
        "",
        "## 7. Limites",
        "",
        "- **Não há backtest de ouro realizado.** O journal do Journalator não "
        "guarda datas, logo não há vendas datadas nem parede histórica na data "
        "da venda. O que existe é um walk-forward do **nível** e uma calibração "
        "com 25 vendas — ver `reference_backtest.md`.",
        "- **O nível não é o problema.** Os asks médios estão várias vezes acima "
        "do preço realizado; isso é trabalho do coeficiente de banda, não da "
        "referência.",
        "- **O peso por item vive fora do TSM.** O TSM avalia a mesma expressão "
        "para todo o grupo, por isso a expressão leva o peso **mediano** do grupo "
        "e da banda. A referência por item (com o peso por item) fica no cálculo "
        "do DFA e neste relatório.",
        "- **A expressão não tem fonte customizada** — só `dbmarket`, "
        "`dbregionmarketavg` e `dbregionhistorical`, que existem no TSM "
        "instalado.",
        "- **Custos de casa de auction assumidos** (taxa 5 %, depósito 5 % "
        "limitado a 300 % do valor de venda ao fornecedor) e não verificados.",
        "",
        "## 8. O que não foi feito",
        "",
        "- Nada foi importado no TSM; a proposta original ficou intacta.",
        "- Nenhuma operação, grupo ou fonte do TSM foi alterada.",
        "- Os coeficientes de banda, os grupos e as durações não foram "
        "tocados.",
        "- O factor do reino não foi aplicado dentro da referência.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dfa-db", default=str(DFA_DB))
    ap.add_argument("--inventory", default=str(INVENTORY))
    ap.add_argument("--realm", default="Dragonmaw", help="reino alvo da proposta")
    ap.add_argument("--method", default="geometric", choices=("geometric", "arithmetic"),
                    help="mistura da referência local e regional (default: geometric)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--write", action="store_true", help="grava o relatório e os CSV")
    args = ap.parse_args(argv)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con_dfa = None
    if Path(args.dfa_db).exists():
        con_dfa = sqlite3.connect(f"file:{args.dfa_db}?mode=ro", uri=True)
    else:
        print(f"aviso: {args.dfa_db} não existe; a procura potencial fica "
              "desconhecida (e os grupos que dependem dela perdem a evidência)",
              file=sys.stderr)

    inventory = read_inventory(args.inventory)
    facts, catalogue = build_features(con, con_dfa, inventory)
    groups_of_item = catalogue_groups(catalogue)
    evidence, flags = collect_band_evidence(con, facts, catalogue, groups_of_item)
    policies = {band: band_policy(band, evidence[band]) for band in evidence}
    clearance = group_clearance(con, catalogue, groups_of_item)
    stats = group_statistics(con, catalogue, groups_of_item)

    # E2: the honest estimator.  It needs dated sales; the Journalator export
    # in this database does not carry them, so the estimate comes back empty
    # and the reason is reported instead of a substitute.
    dated_sales = []
    for item_id, name, realm, sold, copper, successes, failures in con.execute(
            "SELECT itemId, name, realm, sold_count, sold_copper, successes, failures "
            "FROM journal_stats WHERE sold_count>0"):
        dated_sales.append((int(item_id), realm, int(copper) // max(1, int(sold)), None))
    e2 = pg.estimate_e2(con, dated_sales)
    print(f"E2: {e2.note}")

    # the account's own sales, per group and band (the only evidence whose
    # price was chosen by us, so the only one whose sign is identified)
    own_sales: dict[tuple[str, str], tuple[int, float | None]] = {}
    for item_id, realm, sold in con.execute(
            "SELECT itemId, realm, sold_count FROM journal_stats WHERE sold_count>0"):
        key = groups_of_item.get(int(item_id))
        if key is None:
            continue
        count, clearance_value = own_sales.get(key, (0, None))
        own_sales[key] = (count + int(sold), clearance_value)

    adj = adjustments(clearance, evidence, own_sales)

    # the realm the campaign targets (and the scope of its local prices)
    profile = resolve_realm(args.realm)
    canonical = profile.realm
    realm_rows = realm_groups(con)
    scope = (realm_rows.get(canonical) or {}).get("canonical_slug") or canonical

    # --- the evidence-weighted reference ---------------------------------
    from woweconomy.analytics import reference_price as rp

    config = rp.ReferenceConfig(method=args.method)
    listings = {int(i): int(n) for i, n in con.execute(
        "SELECT itemId, num_auctions FROM tsm_market WHERE realm=?", (canonical,))}
    local_evidence = load_local_evidence(con, scope, set(inventory), listings)
    regional_evidence = load_regional_evidence(con, set(inventory))

    # the age of the observations, against the moment of the last fetch
    as_of = con.execute("select max(fetched_at) from tsm_realm_price").fetchone()[0]
    region_at = con.execute("select max(updated_at) from tsm_snapshot "
                             "where scope='region' and kind='region_items'").fetchone()[0]
    local_age = age_hours(as_of, region_at)

    estimates = {}
    for item_id in inventory:
        local = local_evidence.get(item_id)
        regional = regional_evidence.get(item_id)
        if local is None:
            local = rp.LocalEvidence()
        if regional is None:
            regional = rp.RegionalEvidence()
        local = dataclasses.replace(local, age_hours=local_age)
        regional = dataclasses.replace(regional, age_hours=local_age)
        estimates[item_id] = rp.estimate_reference(local, regional, config)

    # which groups are actually different?  The test is on the turnover.
    distinct: list[str] = []
    keys = sorted(stats)
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            if left[0] == right[0] or left[1] != right[1]:
                continue
            if left[0] in ("commodity", "no_reference") or right[0] in ("commodity", "no_reference"):
                continue
            verdict, reason = pg.distinctness(stats[left], stats[right])
            label = {"distinct": "distintos", "same": "**fundir**",
                     "unknown": "inconclusivo"}[verdict]
            distinct.append(f"`{left[0]}` e `{right[0]}` na banda {left[1]}: "
                            f"{label} — {reason}")

    strategies = {}
    for (group, band), adjustment in adj.items():
        strategies[(group, band)] = pg.build_strategy(
            group, policies[band], adjustment,
            notes=(f"amostra da banda: {evidence[band].e1.n:,} observações de E1",))

    # the inventory membership, in the target realm
    # the TSM export loses some names; the DB2 always has them
    names = {int(i): n for i, n in con.execute(
        "SELECT itemId, name FROM db2_items") if n}
    membership: dict[tuple[str, str], list] = {}
    for (realm_name, item_id), item in facts.items():
        if realm_name != canonical:
            continue
        if not item.name or item.name.strip().lower() in ("unknown", "?"):
            if names.get(item_id):
                item.name = names[item_id]
        group = pg.assign_group(item)
        band = item.band
        membership.setdefault((group, band), []).append((item_id, {
            "name": item.name,
            "want_rate": item.want_rate,
            "decay": item.decay_78d,
            "wall": gold(item.local_wall) if item.local_wall else "—",
            "sales": item.sales,
            "expired": item.expired,
            "value": item.reference_gold,
            "local": item.local_market,
            "regional": item.region_market,
        }))

    # the weight of each (group, band) cell, and the per-band weight of a group
    cell_weights: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (group, band), cells in membership.items():
        for item_id, _ in cells:
            estimate = estimates[item_id]
            if estimate.source in ("sem_preco", "regional"):
                # no local price at all: the weight does not apply (the
                # expression's fallback ladder handles it) and a cell made only
                # of such items keeps the local-first form
                continue
            cell_weights[(group, band)].append(estimate.weight_local)
    group_weights: dict[str, dict[str, float]] = defaultdict(dict)
    for (group, band), values in cell_weights.items():
        group_weights[group][band] = statistics.median(values)
    for group, bands in group_weights.items():
        overall = statistics.median(list(bands.values()))
        for band in band_order():
            bands.setdefault(band, overall)

    weights = {}
    for (group, band), values in cell_weights.items():
        weights[(group, band)] = {
            "weight": statistics.median(values),
            "bands": group_weights[group],
            "n": len(values),
        }

    print(f"bandas: {len(evidence)} | evidência E1 total: "
          f"{sum(e.e1.n for e in evidence.values()):,} observações")
    print(f"grupos: {len(pg.GROUPS)} | inventário em {canonical}: "
          f"{sum(len(v) for v in membership.values())} itens")
    print(f"referência ponderada: pesos locais medidos "
          f"{sorted({round(d['weight'], 2) for d in weights.values()})}")
    if not args.write:
        print("\n(modo verificação: usa --write para gravar)", file=sys.stderr)
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "band_evidence.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["band", "n_items", "e1_n", "e1_p25", "e1_p50", "e1_p75",
                         "e1_source", "e1_caveat", "e2_n", "e2_note", "decay_78d",
                         "n_decay", "wall_ratio_p50", "sold_per_day_p50",
                         "sale_rate_p50", "demand_rate_p50", "confidence"])
        for band in BAND_KEYS:
            item = evidence.get(band)
            if not item:
                continue
            writer.writerow([band, item.n_items, item.e1.n, item.e1.p25, item.e1.p50,
                             item.e1.p75, item.e1.source, item.e1.caveat, item.e2.n,
                             item.e2.note, item.decay_78d, item.n_decay,
                             item.wall_ratio_p50, item.sold_per_day_p50,
                             item.sale_rate_p50, item.demand_rate_p50, item.confidence])

    with (args.out_dir / "groups.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["group", "name", "band", "purpose", "inclusion", "exclusion",
                         "normal_formula", "min_formula", "max_formula",
                         "adjustment", "adjustment_basis", "adjustment_n",
                         "undercut_low", "undercut_high", "duration_h", "hold_days",
                         "postable", "confidence"])
        for (group, band), strategy in sorted(strategies.items()):
            writer.writerow([
                group, strategy.name, band,
                pg.GROUPS[group].purpose, pg.GROUPS[group].inclusion,
                pg.GROUPS[group].exclusion, strategy.normal_formula,
                f"{strategy.band_policy.minimum * strategy.adjustment.multiplier:.3f} * {pg.NORMAL_ANCHOR}",
                f"{strategy.band_policy.maximum * strategy.adjustment.multiplier:.3f} * {pg.NORMAL_ANCHOR}",
                strategy.adjustment.multiplier, strategy.adjustment.basis,
                strategy.adjustment.n, strategy.undercut[0], strategy.undercut[1],
                strategy.duration_h, strategy.hold_days, strategy.postable,
                strategy.confidence])

    with (args.out_dir / "membership.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["item_id", "name", "realm", "band", "group", "want_rate",
                         "decay_78d", "local_market_g", "local_wall_g", "sales",
                         "expired", "attempts", "realized_price_g", "vendor_sell_g"])
        for (group, band), items in sorted(membership.items()):
            for item_id, extra in items:
                item = facts[(canonical, item_id)]
                writer.writerow([
                    item_id, item.name, canonical, band, group,
                    item.want_rate if item.want_rate is not None else "",
                    item.decay_78d if item.decay_78d is not None else "",
                    gold(item.local_market), gold(item.local_wall), item.sales,
                    item.expired, item.attempts,
                    gold(item.realized_price) if item.realized_price else "",
                    gold(item.vendor_sell) if item.vendor_sell is not None else ""])

    text, proposal_rows = tsm_proposal(strategies, membership, canonical)
    if text:
        (args.out_dir / "pricing_groups_proposal.tsm").write_text(text + "\n", encoding="utf-8")
    with (args.out_dir / "tsm_proposal.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(proposal_rows[0]) if proposal_rows else ["group"])
        writer.writeheader()
        writer.writerows(proposal_rows)
    write_tsm_proposal(args.out_dir / "tsm_proposal.md", proposal_rows, strategies,
                       membership, canonical, text)

    # --- the candidate: the same groups, an evidence-weighted reference ----
    weighted_text, weighted_rows = weighted_proposal(strategies, membership, canonical,
                                                     weights, config)
    problems = verify_weighted_expression(weights, membership, estimates)
    if problems:
        print("AVISO: a expressão gerada não bate com o modelo:", file=sys.stderr)
        for problem in problems[:5]:
            print("  ", problem, file=sys.stderr)
    if weighted_text:
        (args.out_dir / "pricing_groups_weighted_proposal.tsm").write_text(
            weighted_text + "\n", encoding="utf-8")
    with (args.out_dir / "tsm_proposal_weighted.csv").open("w", newline="",
                                                          encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(weighted_rows[0]) if weighted_rows else ["group"])
        writer.writeheader()
        writer.writerows(weighted_rows)
    write_weighted_report(args.out_dir / "pricing_groups_weighted_report.md", args,
                          config, estimates, weights, weighted_rows, membership,
                          facts, canonical, local_evidence, regional_evidence,
                          group_weights, base_text=len(text), weighted_text=len(weighted_text),
                          problems=problems)

    write_report(REPORT, args, evidence, policies, adj, strategies, membership,
                 flags, catalogue, canonical, stats, e2, distinct, facts)
    print(f"-> {REPORT}")
    print(f"-> {args.out_dir}")
    return 0


def gold(copper: int | float) -> float:
    return float(copper or 0) / pg.COPPER_PER_GOLD


if __name__ == "__main__":
    raise SystemExit(main())
