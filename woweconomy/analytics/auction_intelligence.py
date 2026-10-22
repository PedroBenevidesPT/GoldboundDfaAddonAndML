"""Auction Intelligence Analytics.

Empirical evaluation of realized auction house performance, sell-through,
fee impact, and pricing clearance from captured events.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

COPPER_PER_GOLD = 10_000


@dataclass(frozen=True)
class ItemAuctionStats:
    """Empirical auction metrics for one item on one realm."""

    item_id: Optional[int]
    name: str
    realm: str
    units_sold: int = 0
    gross_sales_copper: int = 0
    net_proceeds_copper: int = 0
    total_fees_copper: int = 0
    deposit_paid_copper: int = 0
    deposit_lost_copper: int = 0
    listings_count: int = 0
    sales_count: int = 0
    cancellations_count: int = 0
    expirations_count: int = 0
    purchases_count: int = 0
    units_bought: int = 0
    purchase_copper: int = 0
    avg_realized_copper: Optional[int] = None
    sell_through_rate: Optional[float] = None
    expiration_rate: Optional[float] = None
    avg_time_to_sale_hours: Optional[float] = None
    time_to_sale_samples: int = 0
    market_ref_copper: Optional[int] = None
    market_ref_error: Optional[float] = None
    e2_clearance_ratio: Optional[float] = None
    confidence: str = "nenhuma"

    @property
    def gross_gold(self) -> float:
        return self.gross_sales_copper / COPPER_PER_GOLD

    @property
    def net_gold(self) -> float:
        return self.net_proceeds_copper / COPPER_PER_GOLD

    @property
    def avg_realized_gold(self) -> Optional[float]:
        return (self.avg_realized_copper / COPPER_PER_GOLD) if self.avg_realized_copper else None


def calculate_confidence(sample_size: int) -> str:
    """Confidence tier based on observation count."""
    if sample_size >= 30:
        return "alta"
    elif sample_size >= 10:
        return "média"
    elif sample_size >= 3:
        return "baixa"
    return "nenhuma"


def load_overall_auction_summary(
    con: sqlite3.Connection,
    realm: Optional[str] = None
) -> Dict[str, Any]:
    """Summary of all recorded events and aggregate metrics."""
    where = "WHERE 1=1"
    params: List[Any] = []
    if realm:
        where += " AND realm = ?"
        params.append(realm)

    counts: Dict[str, int] = defaultdict(int)
    for ev_type, c in con.execute(f"SELECT event_type, COUNT(*) FROM auction_events {where} GROUP BY 1", params):
        counts[ev_type] = c

    sales_row = con.execute(
        f"SELECT COALESCE(SUM(qty), 0), COALESCE(SUM(total_price_copper), 0), "
        f"COALESCE(SUM(fee_copper), 0), COALESCE(SUM(net_proceeds_copper), 0) "
        f"FROM auction_events {where} AND event_type='sale'", params
    ).fetchone()
    units_sold, gross_sales, total_fees, net_proceeds = sales_row

    deposit_row = con.execute(
        f"SELECT COALESCE(SUM(deposit_copper), 0) FROM auction_events {where} AND event_type='listing'", params
    ).fetchone()
    total_listing_deposits = deposit_row[0]

    n_sales = counts.get("sale", 0)
    n_expires = counts.get("expire", 0)
    n_cancels = counts.get("cancel", 0)
    n_listings = counts.get("listing", 0)

    # Denominator: Sales + Expirations (completed attempts)
    denom = n_sales + n_expires
    sell_through = (n_sales / denom) if denom > 0 else None
    expire_rate = (n_expires / denom) if denom > 0 else None

    # Earliest & latest timestamp
    ts_row = con.execute(
        f"SELECT MIN(timestamp_unix), MAX(timestamp_unix) FROM auction_events {where}", params
    ).fetchone()
    min_ts, max_ts = ts_row

    return {
        "realm": realm or "todos",
        "counts": dict(counts),
        "total_events": sum(counts.values()),
        "units_sold": units_sold,
        "gross_sales_copper": gross_sales,
        "total_fees_copper": total_fees,
        "net_proceeds_copper": net_proceeds,
        "listing_deposits_copper": total_listing_deposits,
        "sell_through_rate": sell_through,
        "expiration_rate": expire_rate,
        "earliest_timestamp": min_ts,
        "latest_timestamp": max_ts,
    }


def link_listings_and_sales(
    con: sqlite3.Connection,
    realm: Optional[str] = None,
    max_window_hours: float = 72.0
) -> Dict[Tuple[int, str], List[float]]:
    """Match listing events to subsequent sale events to estimate time to sale."""
    where = "WHERE 1=1"
    params: List[Any] = []
    if realm:
        where += " AND realm = ?"
        params.append(realm)

    rows = con.execute(
        f"SELECT item_id, realm, event_type, timestamp_unix, qty "
        f"FROM auction_events {where} AND event_type IN ('listing', 'sale') AND item_id IS NOT NULL "
        f"ORDER BY item_id, realm, timestamp_unix ASC", params
    ).fetchall()

    # Track open listings per (item_id, realm): list of [timestamp, qty]
    open_listings: Dict[Tuple[int, str], List[List[Any]]] = defaultdict(list)
    sale_durations: Dict[Tuple[int, str], List[float]] = defaultdict(list)

    max_sec = max_window_hours * 3600

    for item_id, r_name, ev_type, ts, qty in rows:
        key = (item_id, r_name)
        if ev_type == "listing":
            open_listings[key].append([ts, qty])
        elif ev_type == "sale":
            candidates = open_listings[key]
            # Match against earliest listing that occurred before this sale within window
            matched = False
            for idx, listing in enumerate(candidates):
                l_ts, l_qty = listing
                if 0 <= (ts - l_ts) <= max_sec:
                    elapsed_hours = (ts - l_ts) / 3600.0
                    sale_durations[key].append(elapsed_hours)
                    # Deduct matched quantity
                    if l_qty <= qty:
                        candidates.pop(idx)
                    else:
                        listing[1] -= qty
                    matched = True
                    break

    return sale_durations


def load_item_auction_stats(
    con: sqlite3.Connection,
    realm: Optional[str] = None,
    min_events: int = 1
) -> Dict[Tuple[Optional[int], str], ItemAuctionStats]:
    """Calculate detailed empirical statistics per item and realm."""
    where = "WHERE 1=1"
    params: List[Any] = []
    if realm:
        where += " AND realm = ?"
        params.append(realm)

    # Gather durations
    durations = link_listings_and_sales(con, realm=realm)

    # Load wall baselines if available
    walls: Dict[Tuple[int, str], int] = {}
    try:
        for scope, iid, mb in con.execute("SELECT scope, item_id, min_buyout FROM tsm_realm_price WHERE min_buyout > 0"):
            walls[(int(iid), scope)] = int(mb)
    except sqlite3.OperationalError:
        pass

    items_sql = f"""
    SELECT
        item_id,
        item_name,
        realm,
        COUNT(*) as total_ev,
        SUM(CASE WHEN event_type='sale' THEN qty ELSE 0 END) as units_sold,
        SUM(CASE WHEN event_type='sale' THEN total_price_copper ELSE 0 END) as gross_sales,
        SUM(CASE WHEN event_type='sale' THEN fee_copper ELSE 0 END) as total_fees,
        SUM(CASE WHEN event_type='sale' THEN net_proceeds_copper ELSE 0 END) as net_proceeds,
        SUM(CASE WHEN event_type='listing' THEN deposit_copper ELSE 0 END) as deposit_paid,
        SUM(CASE WHEN event_type='listing' THEN 1 ELSE 0 END) as listings_count,
        SUM(CASE WHEN event_type='sale' THEN 1 ELSE 0 END) as sales_count,
        SUM(CASE WHEN event_type='cancel' THEN 1 ELSE 0 END) as cancels_count,
        SUM(CASE WHEN event_type='expire' THEN 1 ELSE 0 END) as expires_count,
        SUM(CASE WHEN event_type='purchase' THEN 1 ELSE 0 END) as purchases_count,
        SUM(CASE WHEN event_type='purchase' THEN qty ELSE 0 END) as units_bought,
        SUM(CASE WHEN event_type='purchase' THEN total_price_copper ELSE 0 END) as purchase_copper,
        AVG(CASE WHEN market_ref_copper > 0 THEN market_ref_copper ELSE NULL END) as avg_market_ref
    FROM auction_events
    {where}
    GROUP BY item_id, item_name, realm
    HAVING total_ev >= ?
    """
    params.append(min_events)

    out: Dict[Tuple[Optional[int], str], ItemAuctionStats] = {}

    for row in con.execute(items_sql, params):
        (iid, name, r_name, total_ev, units_sold, gross_sales, fees, net_proc,
         dep_paid, n_list, n_sale, n_cancel, n_expire, n_purch, u_bought, purch_copper,
         avg_ref) = row

        avg_realized = (gross_sales // units_sold) if units_sold > 0 else None

        denom = n_sale + n_expire
        str_rate = (n_sale / denom) if denom > 0 else None
        exp_rate = (n_expire / denom) if denom > 0 else None

        # Approximate deposit lost on expirations
        avg_dep_per_listing = (dep_paid / n_list) if n_list > 0 else 0
        dep_lost = int(avg_dep_per_listing * n_expire)

        # Time to sale
        d_list = durations.get((iid, r_name)) if iid else None
        avg_time = (sum(d_list) / len(d_list)) if d_list else None
        n_time = len(d_list) if d_list else 0

        # Market reference error & E2 clearance
        ref_copper = int(avg_ref) if avg_ref else None
        ref_error = None
        if avg_realized and ref_copper and ref_copper > 0:
            ref_error = (avg_realized - ref_copper) / ref_copper

        # E2 Clearance: realized price vs local wall
        wall = walls.get((iid, r_name)) if iid else None
        e2_clearance = (avg_realized / wall) if (avg_realized and wall and wall > 0) else None

        conf = calculate_confidence(n_sale + n_expire)

        key = (iid, r_name)
        out[key] = ItemAuctionStats(
            item_id=iid,
            name=name,
            realm=r_name,
            units_sold=units_sold,
            gross_sales_copper=gross_sales,
            net_proceeds_copper=net_proc,
            total_fees_copper=fees,
            deposit_paid_copper=dep_paid,
            deposit_lost_copper=dep_lost,
            listings_count=n_list,
            sales_count=n_sale,
            cancellations_count=n_cancel,
            expirations_count=n_expire,
            purchases_count=n_purch,
            units_bought=u_bought,
            purchase_copper=purch_copper,
            avg_realized_copper=avg_realized,
            sell_through_rate=str_rate,
            expiration_rate=exp_rate,
            avg_time_to_sale_hours=avg_time,
            time_to_sale_samples=n_time,
            market_ref_copper=ref_copper,
            market_ref_error=ref_error,
            e2_clearance_ratio=e2_clearance,
            confidence=conf,
        )

    return out


def generate_auction_intelligence_report(
    con: sqlite3.Connection,
    realm: Optional[str] = None
) -> str:
    """Generate a comprehensive markdown intelligence report from captured events."""
    summary = load_overall_auction_summary(con, realm=realm)
    item_stats = load_item_auction_stats(con, realm=realm)

    lines = [
        "# Relatório de Inteligência de Leilão — DFA",
        "",
        f"**Reino**: {summary['realm']} | **Total de Eventos**: {summary['total_events']:,}",
        "",
        "## 1. Resumo Executivo da Atividade",
        "",
        "| Métrica | Valor Observado |",
        "|---|---:|",
        f"| Vendas Concluídas | **{summary['counts'].get('sale', 0):,}** |",
        f"| Unidades Vendidas | **{summary['units_sold']:,}** |",
        f"| Facturação Bruta | **{summary['gross_sales_copper'] / COPPER_PER_GOLD:,.1f} g** |",
        f"| Comissões do Leilão (Consignment 5%) | **{summary['total_fees_copper'] / COPPER_PER_GOLD:,.1f} g** |",
        f"| Facturação Líquida Realizada | **{summary['net_proceeds_copper'] / COPPER_PER_GOLD:,.1f} g** |",
        f"| Publicações Feitas (Listings) | **{summary['counts'].get('listing', 0):,}** |",
        f"| Depósitos Pagos em Publicações | **{summary['listing_deposits_copper'] / COPPER_PER_GOLD:,.1f} g** |",
        f"| Expirações Registadas | **{summary['counts'].get('expire', 0):,}** |",
        f"| Cancelamentos Deliberados | **{summary['counts'].get('cancel', 0):,}** |",
    ]

    str_pct = f"{summary['sell_through_rate']:.1%}" if summary["sell_through_rate"] is not None else "—"
    exp_pct = f"{summary['expiration_rate']:.1%}" if summary["expiration_rate"] is not None else "—"

    lines += [
        f"| Sell-Through Rate (Vendas / Desfechos) | **{str_pct}** |",
        f"| Taxa de Expiração (Expirações / Desfechos) | **{exp_pct}** |",
        "",
        "> [!NOTE]",
        "> O Sell-Through Rate utiliza o denominador exacto `Vendas / (Vendas + Expirações)`. ",
        "> Cancelamentos manuais não são falhas de mercado e são contabilizados separadamente.",
        "",
        "## 2. Top Vendas Realizadas (Ouro Realizado)",
        "",
        "| Item | ID | Reino | Qtd | Bruto | Líquido | Preço Médio | STR | Confiança |",
        "|---|---:|---|---:|---:|---:|---:|---:|---|",
    ]

    # Sort items by gross sales descending
    sorted_sales = sorted(
        [s for s in item_stats.values() if s.gross_sales_copper > 0],
        key=lambda x: x.gross_sales_copper,
        reverse=True
    )

    for s in sorted_sales[:25]:
        str_val = f"{s.sell_through_rate:.0%}" if s.sell_through_rate is not None else "—"
        avg_g = f"{s.avg_realized_gold:,.0f} g" if s.avg_realized_gold else "—"
        lines.append(
            f"| {s.name[:32]} | {s.item_id or '—'} | {s.realm} | {s.units_sold} | "
            f"{s.gross_gold:,.0f} g | {s.net_gold:,.0f} g | {avg_g} | {str_val} | {s.confidence} |"
        )

    if not sorted_sales:
        lines.append("| — | — | — | — | — | — | — | — | nenhuma venda observada |")

    lines += [
        "",
        "## 3. Itens com Maior Atrito / Desperdício de Depósitos (Expirações Repetidas)",
        "",
        "| Item | ID | Reino | Expirações | Cancelamentos | Vendas | Depósito Perdido Est. |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]

    sorted_expires = sorted(
        [s for s in item_stats.values() if s.expirations_count > 0],
        key=lambda x: (x.expirations_count, x.deposit_lost_copper),
        reverse=True
    )

    for s in sorted_expires[:20]:
        dep_g = f"{s.deposit_lost_copper / COPPER_PER_GOLD:,.1f} g"
        lines.append(
            f"| {s.name[:32]} | {s.item_id or '—'} | {s.realm} | {s.expirations_count} | "
            f"{s.cancellations_count} | {s.sales_count} | {dep_g} |"
        )

    if not sorted_expires:
        lines.append("| — | — | — | — | — | — | nenhuma expiração registada |")

    lines += [
        "",
        "## 4. Recomendações e Liquidação Token Rush (Modo Análise)",
        "",
        "- **Itens de Alta Rotação (STR >= 50%)**: Manter preço normal ou alargar margem; a procura aceita o preço actual.",
        "- **Itens Mortos (>= 3 expirações sem vendas)**: Reduzir duração de postagem para 12h para poupar depósitos, ou aplicar liquidação Token Rush com desconto agressivo da parede local.",
        "- **Desvios de Referência**: Acompanhar itens onde o preço realizado difere substancialmente do `dbmarket` da TSM.",
        "",
    ]

    return "\n".join(lines)
