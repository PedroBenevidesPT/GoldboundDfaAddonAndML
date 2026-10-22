"""Enriquecer os ids de um CSV de inventario via Blizzard API (quality/binding/vendor).

Usa os mesmos servicos do enrich_items.py, mas a fonte e um CSV de inventario em
vez de zone JSONs.  Resumivel: ids ja na cache sao saltados.

    python scripts/enrich_inventory.py scripts/inventario.csv --db tsm_markets.db

Precisa de .env no repo com BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

sys.path.insert(0, ".")

from woweconomy.services.blizzard_items import (  # noqa: E402
    BlizzardItemsClient, mark_missing, missing_ids, open_db, upsert,
)

logger = logging.getLogger(__name__)


def ids_from_csv(path: str) -> list[int]:
    with open(path, encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        rows = list(csv.DictReader(fh, delimiter=delim))
    col = next((k for k in (rows[0].keys() if rows else [])
                if k and k.strip().lower() in ("itemid", "item_id", "id")), None)
    if not col:
        raise SystemExit("erro: nao achei coluna de item id no CSV")
    seen: set[int] = set()
    out: list[int] = []
    for r in rows:
        v = (r.get(col) or "").strip()
        if v.isdigit():
            i = int(v)
            if i not in seen:
                seen.add(i)
                out.append(i)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Enriquecer um CSV via Blizzard API.")
    ap.add_argument("inventory")
    ap.add_argument("--db", default="tsm_markets.db")
    ap.add_argument("--rps", type=float, default=8.0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    ids = ids_from_csv(args.inventory)
    print(f"{len(ids)} ids no CSV")

    conn = open_db(args.db)
    todo = missing_ids(conn, ids)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} por buscar")

    client = BlizzardItemsClient(requests_per_second=args.rps)
    done = 0
    for iid in todo:
        try:
            upsert(conn, client.fetch_item(iid))
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("item %s falhou: %r", iid, exc)
            if "404" in str(exc):
                mark_missing(conn, iid)
        if done and done % 50 == 0:
            conn.commit()
            print(f"  {done}/{len(todo)}")
    conn.commit()
    print(f"enriquecidos {done}/{len(todo)}")


if __name__ == "__main__":
    main()
