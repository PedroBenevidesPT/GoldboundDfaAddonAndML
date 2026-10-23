"""Fetch every TSM public CSV for EU (retail) and store a snapshot.

The five endpoints are the ones TSM documents:

    /{game}/{region}/realm/{slug}/items.csv
    /{game}/{region}/realm/{slug}/pets.csv
    /{game}/{region}/commodities.csv
    /{game}/{region}/region/items.csv
    /{game}/{region}/region/pets.csv

A connected realm shares one auction house and TSM serves byte-identical files
under every member slug, so only one canonical slug per group is downloaded and
``tsm_realm_group`` fans it back out to the other members.  Running the script
again is cheap: unchanged files (same SHA-256) are skipped, and only changed
price rows are written to the history table.

Examples
--------
    python scripts/fetch_tsm_snapshots.py --dry-run          # list the URLs
    python scripts/fetch_tsm_snapshots.py --limit 4          # smoke test
    python scripts/fetch_tsm_snapshots.py                    # all EU groups
    python scripts/fetch_tsm_snapshots.py --realms ravencrest,tarren-mill
    python scripts/fetch_tsm_snapshots.py --no-region        # realm files only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.config import WowEconomyConfig  # noqa: E402
from woweconomy.services import tsm_snapshots as ts  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DEFAULT_REALMS_JSON = ROOT / "reports" / "realms_eu.json"
DEFAULT_OUT = ROOT / "data" / "tsm" / "snapshots"


def print_plan(tasks: list, cfg, args, written: int | None = None) -> None:
    groups = len({t.scope for t in tasks if t.kind in ts.REALM_KINDS})
    region_files = len(tasks) - sum(1 for t in tasks if t.kind in ts.REALM_KINDS)
    print(f"{len(tasks)} ficheiros | {groups} mercados (grupos connected) | "
          f"{region_files} ficheiros de região")
    tail = f" | mapa: {written} slugs" if written is not None else ""
    print(f"CSV: {cfg.tsm_base_url}/{cfg.game_type}/{cfg.region_slug} | "
          f"base: {args.db}{tail}")
    for task in tasks:
        print(f"  {task.label:34s} {task.url}")


def main() -> int:
    from woweconomy.dbcompat import load_env

    load_env()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tsm_markets.db")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--realms-json", default=str(DEFAULT_REALMS_JSON))
    ap.add_argument("--realms", default="",
                    help="nomes ou slugs separados por vírgula (default: todos os grupos EU)")
    ap.add_argument("--kinds", default=",".join(ts.ALL_KINDS),
                    help="realm_items,realm_pets,region_items,region_pets,commodities")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--delay", type=float, default=1.0,
                    help="segundos entre inícios de pedido (evita 429)")
    ap.add_argument("--limit", type=int, default=0,
                    help="só N ficheiros: os mais necessários (nunca buscados, "
                         "depois os mais antigos), não os primeiros da lista")
    ap.add_argument("--offset", type=int, default=0,
                    help="salta os primeiros N ficheiros (para correr em blocos)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--max-wait", type=float, default=120.0,
                    help="se o 429 pedir mais do que isto, aborta em vez de dormir")
    ap.add_argument("--no-region", action="store_true",
                    help="não buscar commodities/region (só os ficheiros de reino)")
    ap.add_argument("--missing-only", action="store_true",
                    help="não tentar ficheiros que já têm um snapshot")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = WowEconomyConfig.default()
    realm_map = ts.load_realm_map(args.realms_json)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in ts.ALL_KINDS]
    if unknown:
        print(f"error: kinds desconhecidos: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if args.no_region:
        kinds = [k for k in kinds if k in ts.REALM_KINDS]
    if not kinds:
        print("error: nenhum kind selecionado", file=sys.stderr)
        return 2

    realms = [r.strip() for r in args.realms.split(",") if r.strip()] or None
    try:
        tasks = ts.build_tasks(
            realm_map, kinds, realms,
            base_url=cfg.tsm_base_url, game_type=cfg.game_type,
            region=cfg.region_slug)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # A plain dry-run must not create tables or touch the database; only
    # --missing-only needs to read it, because the answer depends on what is
    # already stored.
    if args.dry_run and not args.missing_only:
        if args.offset:
            tasks = tasks[args.offset:]
        if args.limit:
            tasks = tasks[:args.limit]
        print_plan(tasks, cfg, args)
        return 0

    con = ts.connect(args.db)
    written = ts.sync_realm_groups(con, realm_map)
    wait = ts.cooldown_until(con)
    if wait > 0:
        print(f"CDN em cooldown: faltam {wait / 60:.0f} min. Nada foi pedido.\n"
              f"(o estado está em tsm_meta.ratelimit_until; espera e volta a correr)")
        con.close()
        return 0
    if args.missing_only:
        have = {(scope, kind) for scope, kind in con.execute(
            "SELECT scope, kind FROM tsm_snapshot")}
        before = len(tasks)
        tasks = [t for t in tasks if (t.scope, t.kind) not in have]
        print(f"--missing-only: {before - len(tasks)} já têm snapshot, "
              f"{len(tasks)} por buscar")
    # Ordem de retoma: um run que pare a meio tem de avançar, não repetir a
    # mesma cabeça de lista (os 50 reinos que já têm dados vinham sempre antes).
    tasks = ts.order_for_resume(con, tasks)
    if args.offset:
        tasks = tasks[args.offset:]
    if args.limit:
        tasks = tasks[:args.limit]

    if args.dry_run:
        print_plan(tasks, cfg, args, written)
        con.close()
        return 0
    print_plan(tasks, cfg, args, written)

    def progress(task: ts.Task, result: ts.ImportResult | None, error: str) -> None:
        if args.quiet:
            return
        if error:
            print(f"  !! {task.label:34s} {error}")
        elif result is None:
            print(f"  ?  {task.label:34s} sem resultado")
        elif result.not_modified:
            print(f"  =  {task.label:34s} 304 (TSM não republicou)")
        elif result.skipped:
            print(f"  =  {task.label:34s} inalterado")
        else:
            print(f"  +  {task.label:34s} {result.rows:>7,} linhas | "
                  f"{result.changed:>6,} mudanças | {result.updated_at}")

    stats = ts.sync(con, tasks, args.out, workers=args.workers,
                    timeout=args.timeout, retries=args.retries,
                    delay=args.delay, max_wait=args.max_wait, progress=progress)
    covered = {row[0] for row in con.execute(
        "SELECT DISTINCT scope FROM tsm_snapshot WHERE kind='realm_items'")}
    wait_left = ts.cooldown_until(con)
    con.close()

    print("-" * 78)
    print(f"ficheiros {stats.files} | novos/alterados "
          f"{stats.files - stats.skipped - stats.failed} | 304 {stats.not_modified} | "
          f"inalterados {stats.skipped - stats.not_modified} | falhas {stats.failed}"
          + (" | ABORTADO por rate limit" if stats.aborted else ""))
    print(f"linhas importadas {stats.rows:,} | linhas alteradas {stats.changed:,}")
    print(f"mercados com dados: {len(covered)} | grupos no mapa: {len(realm_map.groups)} "
          f"| faltam {max(0, len(realm_map.groups) - len(covered))}")
    if wait_left > 0:
        print(f"CDN em cooldown: {wait_left / 60:.0f} min. Os ficheiros por buscar ficam "
              f"na fila; correr outra vez depois disso.")
    print(f"snapshots -> {Path(args.out)}")
    if stats.errors:
        print("falhas:")
        for err in stats.errors:
            print(f"  - {err}")
    return 1 if stats.failed and not stats.rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
