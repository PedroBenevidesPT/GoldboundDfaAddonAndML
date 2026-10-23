"""Per-realm comparison over the 95-item top BoE basket.

Reads the local TSM snapshot (``tsm_realm_price`` / ``tsm_region_price``) and the
DfA demand matrix (``reports/item_demand_realms.csv``) and prints one row per
realm: how much of the basket is actually listed there, how deep the book is
discounted, how the recent price compares to the 60-day one, and the expected
sales/gold over a 48 h cycle for a seller posting the whole basket.

    python scripts/realm_48h.py
    python scripts/realm_48h.py --csv reports/realm_48h.csv

The model (same as reports/analise_reinos_48h.md):

    P_i = 1 - exp(-2 * lambda_i,r / (N_i + 1))
    E[gold/48h] = sum_i P_i * p_i,r

``lambda_i,r`` is the item's regional ``sold_per_day`` scaled by the realm's share
of wanters for that item, ``N_i`` is 1 when the realm already has a listing and 0
when the shelf is empty, and ``p`` is the cheapest listing, else the realm market
value, else the region market value.  Prices are copper everywhere; output is gold.

``p`` is capped at ``--cap`` times the cross-realm median for the item whenever the
item has at least ``--min-realms`` realms of data.  Without that cap a single stale
listing (198k g on 57 realms, 3.6M g on one) decides the whole ranking -- the
dispersion is measurement noise, not an arbitrage the game allows you to take
(items cannot be moved between servers).
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = str(ROOT / "tsm_markets.db")
DEMAND = ROOT / "reports" / "item_demand_realms.csv"


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def canonical_names(con: sqlite3.Connection) -> dict[str, str]:
    return {s: n for s, n, c in con.execute(
        "SELECT canonical_slug, canonical_name, is_canonical FROM tsm_realm_group")
        if c}


def load_demand(path: Path) -> tuple[dict[int, str], dict[int, dict[str, int]], dict[str, int]]:
    """item -> name, item -> realm wanters, realm -> total wanters."""
    names: dict[int, str] = {}
    per_item: dict[int, dict[str, int]] = defaultdict(dict)
    totals: dict[str, int] = defaultdict(int)
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            iid = int(row["item_id"])
            n = int(row["wanters"])
            names[iid] = row["item_name"]
            key = slug(row["realm"])
            per_item[iid][key] = per_item[iid].get(key, 0) + n
            totals[key] += n
    return names, per_item, totals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--demand", default=str(DEMAND))
    ap.add_argument("--csv", default="", help="grava a tabela completa neste CSV")
    ap.add_argument("--top", type=int, default=25, help="linhas a imprimir")
    ap.add_argument("--cap", type=float, default=3.0,
                    help="tecto do preco local em multiplos da mediana entre reinos (0 desliga)")
    ap.add_argument("--min-realms", type=int, default=8,
                    help="so aplica o tecto a itens com esta cobertura de reinos")
    a = ap.parse_args(argv)

    con = sqlite3.connect(a.db)
    canon = canonical_names(con)
    names, per_item, totals = load_demand(Path(a.demand))
    items = sorted(names)
    ph = ",".join("?" * len(items))

    region = {r[0]: r for r in con.execute(
        f"SELECT key_id, market_value, historical, sold_per_day "
        f"FROM tsm_region_price WHERE key_id IN ({ph})", items)}
    universe = sum(1 for i in items if i in region)
    lam_total = sum(region[i][3] or 0 for i in items if i in region)
    print(f"cabaz: {len(items)} itens ({universe} com preco regional), "
          f"lambda regional = {lam_total:.2f} vendas/dia")
    print(f"reinos com precos: "
          f"{con.execute('SELECT COUNT(DISTINCT scope) FROM tsm_realm_price').fetchone()[0]}")

    # realm -> item -> (market_value, min_buyout, recent, historical)
    prices: dict[str, dict[int, tuple]] = defaultdict(dict)
    for scope, iid, mv, mb, rec, hist in con.execute(
            f"SELECT scope, item_id, market_value, min_buyout, recent, historical "
            f"FROM tsm_realm_price WHERE item_id IN ({ph})", items):
        prices[scope][iid] = (mv or 0, mb or 0, rec or 0, hist or 0)

    # cross-realm consensus price per item: the median listing across all realms
    consensus: dict[int, float] = {}
    for iid in items:
        vals = sorted(v[1] for v in (p.get(iid) for p in prices.values())
                      if v and v[1] > 0)
        if len(vals) >= a.min_realms:
            consensus[iid] = statistics.median(vals)

    rows = []
    for scope, per in prices.items():
        realm_slug = slug(canon.get(scope, scope))
        wanters_total = totals.get(realm_slug, 0)
        listed = sum(1 for v in per.values() if v[1] > 0)
        priced = sum(1 for v in per.values() if v[0] > 0)
        dumping = [v[1] for v in per.values()
                   if v[1] > 0 and v[0] > 0 and v[1] < 0.7 * v[0]]
        decay = [v[2] / v[3] for v in per.values() if v[2] > 0 and v[3] > 0]

        gold = sales = 0.0
        capped = 0
        ratios = [v[1] / consensus[iid] for iid, v in per.items()
                  if v[1] > 0 and iid in consensus]
        for iid in items:
            lam = region.get(iid, (0, 0, 0, 0))[3] or 0
            if lam <= 0:
                continue
            share = per_item[iid].get(realm_slug, 0)
            base = sum(per_item[iid].values()) or 1
            lam_r = lam * share / base
            if lam_r <= 0:
                continue
            mv, mb, _rec, _hist = per.get(iid, (0, 0, 0, 0))
            price = mb or mv or region[iid][1]
            med = consensus.get(iid)
            if a.cap and med:
                if price > a.cap * med:
                    capped += 1
                price = min(price, a.cap * med)
            n = 1 if mb > 0 else 0
            p = 1 - math.exp(-2 * lam_r / (n + 1))
            sales += p
            gold += p * price

        rows.append({
            "realm": canon.get(scope, scope),
            "slug": scope,
            "demand_pct": 100 * wanters_total / sum(totals.values()) if totals else 0.0,
            "wanters": wanters_total,
            "listed": listed,
            "priced": priced,
            "empty_pct": 100 * (priced - listed) / priced if priced else 0.0,
            "dumping_pct": 100 * len(dumping) / listed if listed else 0.0,
            "decay": statistics.median(decay) if decay else None,
            "vs_consensus": statistics.median(ratios) if ratios else None,
            "capped": capped,
            "sales48": sales,
            "gold48": gold / 10_000,
        })

    rows.sort(key=lambda r: -r["gold48"])
    total_gold = sum(r["gold48"] for r in rows) or 1

    hdr = (f"{'reino':22s} {'%dem':>5s} {'list':>5s} {'vazio':>6s} "
           f"{'dump':>5s} {'rec/hist':>8s} {'vs med':>6s} {'ouro/48h':>10s} {'vendas/48h':>10s}")
    print()
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        decay = f"{r['decay']:.3f}" if r["decay"] is not None else "   -  "
        vsmed = f"{r['vs_consensus']:.2f}x" if r["vs_consensus"] is not None else "   - "
        print(f"{r['realm'][:22]:22s} {r['demand_pct']:5.1f} {r['listed']:5d} "
              f"{r['empty_pct']:5.0f}% {r['dumping_pct']:4.0f}% {decay:>8s} "
              f"{vsmed:>6s} {r['gold48']:10.0f} {r['sales48']:10.3f}")

    if a.csv:
        out = Path(a.csv)
        with open(out, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n{len(rows)} reinos -> {out}")

    totais = {
        "reinos": len(rows),
        "sem_amostra": sum(1 for r in rows if r["wanters"] == 0),
        "gold48_total": sum(r["gold48"] for r in rows),
        "sales48_total": sum(r["sales48"] for r in rows),
        "top5_share": 100 * sum(r["gold48"] for r in rows[:5]) / total_gold,
    }
    print(f"\n{totais['reinos']} reinos, {totais['sem_amostra']} sem amostra de demanda; "
          f"top 5 = {totais['top5_share']:.0f}% do ouro modelado; "
          f"{sum(r['capped'] for r in rows)} precos acima do tecto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
