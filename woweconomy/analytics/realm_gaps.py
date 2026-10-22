"""Agregação reino × item da lista de demanda.

O ``item_demand.py --where --where-csv`` produz uma linha por item e reino.
Aqui ficam as duas leituras que interessam a quem quer comprar para vender:

* ``realm_totals`` -- por reino, quantos compradores e quanto ouro em falta;
* ``cheapest_realms`` -- por item, os reinos mais baratos, preferindo os que
  têm um anúncio real (``min_buyout``) em vez de uma média de mercado.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    """Lê o CSV do ``--where-csv`` e normaliza os números."""
    with Path(path).open(encoding="utf-8", newline="") as fh:
        out = []
        for r in csv.DictReader(fh):
            for key in ("item_id", "wanters", "eligible", "price_copper",
                        "gold_gap"):
                r[key] = int(float(r[key] or 0))
            r["want_rate"] = float(r["want_rate"] or 0)
            out.append(r)
        return out


def realm_totals(rows: list[dict], accounts: dict[str, int],
                 min_wanters: int = 3) -> list[dict]:
    """Por reino: itens com procura, compradores e ouro em falta.

    ``buyers`` soma os ``wanters`` das linhas que passam o mínimo, por isso é
    maior que o número de contas: a mesma conta pode faltar a vários itens.
    """
    by_realm: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["wanters"] >= min_wanters:
            by_realm[r["realm"]].append(r)
    out = []
    for realm, hits in by_realm.items():
        best = max(hits, key=lambda r: r["gold_gap"])
        out.append({
            "realm": realm,
            "accounts": accounts.get(realm, 0),
            "items": len(hits),
            "buyers": sum(r["wanters"] for r in hits),
            "gold_gap": sum(r["gold_gap"] for r in hits),
            "best_item": best["name"],
            "best_item_id": best["item_id"],
        })
    out.sort(key=lambda d: (-d["gold_gap"], d["realm"]))
    return out


def cheapest_realms(tsm: sqlite3.Connection, item_id: int,
                    limit: int = 3) -> list[dict]:
    """Reinos mais baratos para um item.

    Linhas com anúncio (``min_buyout > 0``) vêm primeiro: sem anúncio o preço
    é uma média de mercado feita com poucas amostras, não algo que se compre.
    """
    rows = tsm.execute(
        "SELECT scope, market_value, min_buyout FROM tsm_realm_price "
        "WHERE item_id=? AND market_value > 0 "
        "ORDER BY CASE WHEN min_buyout > 0 THEN 0 ELSE 1 END, "
        "market_value LIMIT ?", (item_id, limit)).fetchall()
    return [{"realm": scope, "market": int(market or 0),
             "listing": int(listing or 0),
             "cost": int(listing or 0) or int(market or 0)}
            for scope, market, listing in rows]


def region_value(tsm: sqlite3.Connection, item_id: int) -> int:
    row = tsm.execute("SELECT market_value FROM tsm_region_price "
                      "WHERE key_id=?", (item_id,)).fetchone()
    return int(row[0] or 0) if row else 0
