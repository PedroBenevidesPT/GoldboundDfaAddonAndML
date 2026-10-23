"""Importa e mede o registo de eventos do addon DFA Auction Intelligence.

    python scripts/dfa_ai.py --status      # onde está o export, o que há
    python scripts/dfa_ai.py --import      # importa para o tsm_markets.db
    python scripts/dfa_ai.py --measure     # as medições que os eventos permitem
    python scripts/dfa_ai.py --plan        # o protocolo de teste (que postar)

Em jogo:

    /dfaai status     # estado e número de eventos
    /dfaai diag       # cobertura e campos em falta
    /dfaai export     # regenera o export nos SavedVariables
    /dfaai sync       # re-importa o arquivo do Journalator

Nada é postado, cancelado ou repreçado: isto só lê e mede.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics import auction_intel as ai  # noqa: E402
from woweconomy.analytics import pricing_groups as pg  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = ROOT / "tsm_markets.db"
OUT_DIR = ROOT / "reports" / "auction_intel"


def g(value) -> str:
    return f"{value / 10_000:,.0f} g"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_events(path: Path | None = None):
    path = path or ai.find_saved_variables()
    if path is None:
        return None, []
    return path, ai.parse_saved_variables(path)


def cmd_status(args) -> int:
    path = ai.find_saved_variables()
    # the DB and the export live in the same file: WoW writes one file per
    # addon folder, named after the folder
    print("export do addon:", path or "NÃO ENCONTRADO")
    if path is None:
        print("\nO addon ainda não exportou. Em jogo:")
        print("  1. confirma que DFA_AuctionIntelligence e Journalator estão activos")
        print("  2. /dfaai status   -> número de eventos")
        print("  3. /dfaai sync     -> importa o arquivo do Journalator")
        print("  4. /dfaai export   -> regenera o export nos SavedVariables")
        print("  5. sai do jogo (o export é refrescado no logout)")
        return 1
    events = ai.parse_saved_variables(path)
    cover = ai.coverage(events)
    print(f"\neventos no export: {cover.total:,}")
    for event_type, count in sorted(cover.by_type.items()):
        print(f"  {event_type:10s} {count:8,d}")
    if cover.first_ts:
        print(f"  janela: {datetime.fromtimestamp(cover.first_ts, timezone.utc):%Y-%m-%d} "
              f"-> {datetime.fromtimestamp(cover.last_ts, timezone.utc):%Y-%m-%d} "
              f"({cover.span_days:.1f} dias)")
    print(f"  com item id: {cover.with_item_id:,} | com preço: {cover.with_price:,} | "
          f"com referência de mercado: {cover.with_market_ref:,}")
    print(f"  com depósito: {cover.with_deposit:,} | com taxa: {cover.with_fee:,} | "
          f"com líquido: {cover.with_net:,}")
    # what the last import left in the database, so a sync that silently did
    # nothing cannot pass for a sync that worked
    try:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        rows, with_id = con.execute(
            f"select count(*), count(item_id) from {ai.TABLE}").fetchone()
        con.close()
        print(f"\nna base ({ai.TABLE}): {rows:,} eventos, {with_id:,} com item id")
        if rows > cover.total:
            print("  (a base tem mais do que este export: o addon ainda não exportou "
                  "o que já tinha guardado — /dfaai export e sair do jogo)")
    except sqlite3.OperationalError:
        print(f"\nna base: tabela {ai.TABLE} ainda não existe (corre --import)")
    return 0


def cmd_import(args) -> int:
    path, events = load_events(args.export)
    if path is None:
        print("sem export do addon: corre /dfaai export em jogo (e sai do jogo)", file=sys.stderr)
        return 1
    if not events:
        print(f"export sem eventos: {path}", file=sys.stderr)
        return 1
    # journalator's invoices carry no item link, so the id has to come from the
    # catalogue before the row can be joined to a price
    filled = ai.fill_item_ids(events, ai.build_name_index(args.db))
    con = sqlite3.connect(args.db)
    inserted, present = ai.import_events(con, events, now_iso())
    total = con.execute(f"select count(*) from {ai.TABLE}").fetchone()[0]
    print(f"importados {inserted:,} eventos (já existentes: {present:,}) de {path}")
    print(f"item id resolvido pelo nome em {filled:,} eventos")
    print(f"tabela {ai.TABLE}: {total:,} eventos")
    con.close()
    return 0


def load_imported(db_path) -> list:
    """The events already imported, with the ids resolved by ``--import``.

    Measuring from the export instead of this table loses every invoice sale:
    Journalator's invoices carry no item link, so the export has no ``itemId``
    for a sale and ``time_to_sale`` can never pair it with a listing.  The
    import resolves the id by name, and *that* is the table to measure.
    """
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select event_type, ts, item_id, item_name, quantity, unit_price_copper, "
            "total_price_copper, deposit_copper, consignment_fee_copper, "
            "net_proceeds_copper, market_ref_copper, realm, character, counterparty, "
            f"source, provenance, confidence, event_id from {ai.TABLE}").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    events = []
    for row in rows:
        events.append({
            "eventType": row[0], "timestamp": row[1], "itemId": row[2],
            "itemName": row[3], "quantity": row[4], "unitPriceCopper": row[5],
            "totalPriceCopper": row[6], "depositCopper": row[7],
            "consignmentFeeCopper": row[8], "netProceedsCopper": row[9],
            "marketRefCopper": row[10], "realm": row[11], "character": row[12],
            "counterparty": row[13], "source": row[14], "provenance": row[15],
            "confidence": row[16], "eventId": row[17],
        })
    return events


def cmd_measure(args) -> int:
    events = load_imported(args.db)
    origin = f"base ({ai.TABLE})"
    if not events:
        path, events = load_events(args.export)
        origin = str(path) if path else "?"
        if not events:
            print("sem eventos: importa primeiro (--import) ou corre o addon", file=sys.stderr)
            return 1

    # the band of each item, from the regional reference
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    band_by_item = {}
    for item, market in con.execute(
            "SELECT key_id, market_value FROM tsm_region_price "
            "WHERE snapshot_id=(select max(snapshot_id) from tsm_region_price)"):
        if market and market < 99_999_999_000:
            band_by_item[int(item)] = pg.band_of(market / 10_000)
    con.close()

    cover = ai.coverage(events)
    samples = ai.clearance_samples(events,
                                  band_of=lambda item: band_by_item.get(item, ""))
    by_band = ai.clearance_by_band(samples)
    intervals = ai.time_to_sale(events)
    funnel = ai.funnel(events)
    costs = ai.measured_costs(events)

    print(f"eventos: {cover.total:,} | origem: {origin} | tipos: {cover.by_type}")
    if cover.first_ts:
        print(f"janela: {datetime.fromtimestamp(cover.first_ts, timezone.utc):%Y-%m-%d} -> "
              f"{datetime.fromtimestamp(cover.last_ts, timezone.utc):%Y-%m-%d} "
              f"({cover.span_days:.1f} dias)")

    print("\n== clearance medido (preço realizado ÷ referência de mercado NO MOMENTO) ==")
    if by_band:
        print(f"{'banda':10s} {'n':>5s} {'p25':>7s} {'p50':>7s} {'p75':>7s} {'média':>7s}")
        for band, stats in sorted(by_band.items()):
            print(f"{band:10s} {stats['n']:5d} {stats['p25']:7.3f} {stats['p50']:7.3f} "
                  f"{stats['p75']:7.3f} {stats['mean']:7.3f}")
    else:
        print("sem vendas com referência de mercado: o evento 'sale' precisa de "
              "marketRefCopper (o addon lê o dbmarket do TSM no momento)")

    print("\n== tempo até à venda (listagem -> venda) ==")
    if intervals:
        hours = sorted(i["hours"] for i in intervals)
        n = len(hours)
        print(f"  n={n} | p25={hours[n//4]:.1f} h | p50={hours[n//2]:.1f} h | "
              f"p75={hours[3*n//4]:.1f} h")
        for item in intervals[:5]:
            print(f"    item {item['item_id']} {item['realm']}: pediu "
                  f"{g(item['asked_copper'])}, vendeu por {g(item['realised_copper'])} "
                  f"após {item['hours']:.1f} h")
    else:
        print("  sem pares listagem->venda (o addon precisa de eventos 'listing')")

    print("\n== funil (por item e reino) ==")
    posted = sum(f["listings"] for f in funnel.values())
    sold = sum(f["sales"] for f in funnel.values())
    expired = sum(f["expires"] for f in funnel.values())
    if posted:
        print(f"  listagens={posted} vendas={sold} expirações={expired} "
              f"sell-through={(sold / (sold + expired)) if (sold + expired) else 0:.0%}")
    else:
        print("  sem listagens registadas")

    print("\n== custos medidos (em vez de assumidos) ==")
    print(f"  taxa de AH: n={costs['fee_samples']} mediana="
          f"{costs['fee_median']:.2%}" if costs["fee_median"] else
          "  taxa de AH: sem amostras")
    print(f"  depósito:   n={costs['deposit_samples']} mediana="
          f"{costs['deposit_median']:.2%}" if costs["deposit_median"] else
          "  depósito:   sem amostras")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / "measurements.md"
    lines = [
        "# Medições do registo de eventos (DFA Auction Intelligence)",
        "",
        "Gerado por `python scripts/dfa_ai.py --measure`.",
        "",
        f"- eventos: {cover.total:,} — {cover.by_type}",
        f"- janela: {datetime.fromtimestamp(cover.first_ts, timezone.utc):%Y-%m-%d} -> "
        f"{datetime.fromtimestamp(cover.last_ts, timezone.utc):%Y-%m-%d}"
        if cover.first_ts else "- sem janela",
        "",
        "## Clearance medido (venda ÷ referência de mercado no momento)",
        "",
        "| banda | n | p25 | p50 | p75 | média |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for band, stats in sorted(by_band.items()):
        lines.append(f"| {band} | {stats['n']} | {stats['p25']:.3f} | {stats['p50']:.3f} | "
                     f"{stats['p75']:.3f} | {stats['mean']:.3f} |")
    lines += [
        "",
        "## Tempo até à venda",
        "",
        (f"n={len(intervals)}, mediana "
         f"{sorted(i['hours'] for i in intervals)[len(intervals)//2]:.1f} h"
         if intervals else "sem pares listagem->venda"),
        "",
        "## Funil",
        "",
        (f"listagens={posted}, vendas={sold}, expirações={expired}"
         if posted else "sem listagens"),
        "",
        "## Custos medidos",
        "",
        f"- taxa de AH: {costs['fee_median']} (n={costs['fee_samples']})"
        if costs["fee_median"] else "- taxa de AH: sem amostras",
        f"- depósito: {costs['deposit_median']} (n={costs['deposit_samples']})"
        if costs["deposit_median"] else "- depósito: sem amostras",
        "",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n-> {report}")
    return 0


def cmd_plan(args) -> int:
    """The measurement protocol: what to post, at what price, to test what."""
    from woweconomy.analytics import pricing_engine as pe
    from woweconomy.analytics.token_rush_data import read_inventory

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    inventory = read_inventory(args.inventory)
    rows = []
    for item_id, item in inventory.items():
        reg = con.execute(
            "SELECT market_value, avg_sale_price, sold_per_day FROM tsm_region_price "
            "WHERE key_id=?", (item_id,)).fetchone()
        local = con.execute(
            "SELECT market_value, min_buyout FROM tsm_realm_price "
            "WHERE scope='spinebreaker' AND item_id=?", (item_id,)).fetchone()
        if not reg or not reg[0]:
            continue
        market, avg_sale, sold_per_day = reg
        wall = local[1] if local else 0
        rows.append({
            "item_id": item_id,
            "name": item.name,
            "qty": item.qty,
            "market": market,
            "avg_sale": avg_sale or 0,
            "sold_per_day": sold_per_day or 0,
            "wall": wall,
            "band": pg.band_of(market / 10_000),
        })
    con.close()
    rows.sort(key=lambda r: -(r["avg_sale"] or 0))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "test_plan.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        import csv
        writer = csv.writer(fh)
        writer.writerow(["item_id", "name", "band", "qty", "market_g", "avg_sale_g",
                         "wall_g", "sold_per_day", "preco_teste_g", "grupo"])
        for row in rows:
            # the test price: the measured average sale (the price the market
            # actually paid), which is the number the model must beat
            test = row["avg_sale"] or row["wall"] or row["market"]
            group = "A: preco da venda media (controlo)"
            writer.writerow([row["item_id"], row["name"], row["band"], row["qty"],
                             round(row["market"] / 10_000), round((row["avg_sale"] or 0) / 10_000),
                             round(row["wall"] / 10_000), row["sold_per_day"],
                             round(test / 10_000), group])
    print(f"-> {path}")
    print("\nProtocolo:")
    print("  1. posta os itens da primeira coluna de precios a um preço conhecido "
          "(o `preco_teste_g`)")
    print("  2. deixa o addon registar os eventos (listagem, venda, expiração)")
    print("  3. /dfaai export e sai do jogo")
    print("  4. python scripts/dfa_ai.py --import && --measure")
    print("\nO que fica medido: o tempo até à venda, o sell-through, o clearance "
          "realizado contra a referência do momento, e a taxa e o depósito reais.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--inventory", default=str(ROOT / "inventory.csv"))
    ap.add_argument("--export", type=Path, default=None,
                    help="caminho do SavedVariables (por omissão, o mais recente)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--import", dest="do_import", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--plan", action="store_true")
    args = ap.parse_args(argv)

    if args.status or not (args.do_import or args.measure or args.plan):
        return cmd_status(args)
    if args.do_import:
        return cmd_import(args)
    if args.measure:
        return cmd_measure(args)
    if args.plan:
        return cmd_plan(args)
    return cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
