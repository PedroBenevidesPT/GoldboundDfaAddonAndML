"""Gera as strings de import do TSM: grupos de mog + operações, por reino.

    python scripts/tsm_groups_export.py --show
    python scripts/tsm_groups_export.py --write
    python scripts/tsm_groups_export.py --write --realm Dragonmaw
    python scripts/tsm_groups_export.py --write --realms "Dragonmaw,Argent Dawn,Silvermoon"
    python scripts/tsm_groups_export.py --write --all-realms
    python scripts/tsm_groups_export.py --no-operations --write

Lê `reports/mog_groups/<grupo>.txt` (formato `i:<id>,...`) e o perfil medido de
cada reino (`reports/mog_groups/realm_profiles.csv`, gerado por
`scripts/mog_realm_profiles.py`). Sem `--realm` a referência é a regional
(factor 1,00); com `--realm` o export usa o factor local, a postura, a duração e
a janela de retenção desse reino.

Cada ficheiro é uma linha AceSerializer que o TSM importa como grupos com nome
(Liquid, Collector, Slow, Very_Slow, Unproven, Unknown, Conflict) já com a
Auctioning Operation de cada um. O TSM não tem perfil no formato de import e o
import aplica ao perfil **activo**: cria o perfil na UI e importa o ficheiro
desse reino nele (ver `reports/mog_groups.md`, secção D).

Por omissão os itens de `conflict` saem do grupo primário e passam para o grupo
`Conflict` (no TSM um item só pode estar num grupo); `--conflict-inline` mantém
o grupo `Conflict` de fora e deixa os itens onde estavam.

O `--show` não escreve nada: imprime os grupos, as contagens e as operações
para revisão. Sem `--write` e sem `--stdout` fica em modo de verificação.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.mog_operations import (  # noqa: E402
    GROUP_NAMES, LIQUIDITY_PREFIX, LiquidityOperation, build_liquidity_export,
    build_mog_export,
)
from woweconomy.analytics.mog_realm import (  # noqa: E402
    known_realms, resolve as resolve_realm,
)
from woweconomy.analytics.tsm_export import (  # noqa: E402
    deserialize, parse_group_export,
)
from woweconomy.analytics.tsm_ids import parse_item_ids  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "realm"


def money(copper: int) -> str:
    """TSM money string: whole gold, silver and copper (decimals are invalid)."""
    copper = max(0, int(copper))
    g, rest = divmod(copper, 10_000)
    s, c = divmod(rest, 100)
    return f"{g}g {s}s {c}c"


def load_items(directory: Path) -> dict[str, list[int]]:
    items: dict[str, list[int]] = {}
    for slug in GROUP_NAMES:
        path = directory / f"{slug}.txt"
        if path.exists():
            items[slug] = parse_item_ids(path.read_text(encoding="utf-8-sig"))
    return items


def show(text: str, realm=None) -> None:
    data = deserialize(text)
    groups = parse_group_export(data.get("groupExport", ""))
    operations = data.get("operations", {}).get("Auctioning", {})
    attached = data.get("groupOperations", {})
    total = 0
    if realm is not None:
        print(realm.describe())
        print(f"  escala {realm.scale:.2f} = factor {realm.factor:.2f} x postura "
              f"{realm.posture:.2f}; {realm.duration}h base, {realm.hold_days} dias de retenção")
    print(f"{len(groups)} grupos, {len(operations)} operações, "
          f"{len(text):,} caracteres")
    for name, ids in groups.items():
        total += len(ids)
        op = attached.get(name, {}).get("Auctioning", {}).get(1, "-")
        policy = operations.get(op, {})
        print(f"  {name:10s} {len(ids):5d} itens  op={op!r} "
              f"dur={policy.get('duration', '-')}h "
              f"undercut={policy.get('undercut', '-')} "
              f"maxExpires={policy.get('maxExpires', '-')}")
    print(f"  total {total:,} itens")
    for op, policy in operations.items():
        print(f"\n[{op}]")
        for field in ("minPrice", "normalPrice", "maxPrice"):
            print(f"  {field:9s} {policy.get(field)}")


def _liquidity(args) -> int:
    """Write the token rush imports: one file per realm in the queue."""
    import csv

    queue = args.liquidity_queue
    if not queue.exists():
        raise SystemExit(f"fila não encontrada: {queue}")
    with queue.open(encoding="utf-8-sig", newline="") as fh:
        realms = []
        for row in csv.DictReader(fh):
            realm = (row.get("realm") or "").strip()
            if realm and realm not in realms:
                realms.append(realm)
    if args.liquidity_realm:
        realms = [r for r in realms if r == args.liquidity_realm]
        if not realms:
            raise SystemExit(f"o reino {args.liquidity_realm!r} não está na fila")
    out_dir = args.dir
    for realm in realms:
        text = liquidity_from_queue(queue, realm)
        out = out_dir / f"{LIQUIDITY_PREFIX}_{slugify(realm)}.tsm"
        if args.show or not (args.write or args.stdout):
            show(text)
        if args.stdout:
            print(text)
            continue
        if args.write:
            with out.open("w", encoding="utf-8", newline="") as fh:
                fh.write(text + "\n")
            print(f"-> {out} ({len(text):,} caracteres)", file=sys.stderr)
        else:
            print("\n(modo verificação: usa --write para gravar)", file=sys.stderr)
    return 0


def build(args, items, realm_name: str | None):
    realm = resolve_realm(realm_name) if realm_name else None
    text = build_mog_export(
        items,
        with_operations=not args.no_operations,
        conflict_inline=args.conflict_inline,
        operation_prefix=args.operation_prefix,
        realm=realm,
    )
    return text, realm


def liquidity_from_queue(queue_path: Path, realm: str) -> str:
    """Build a rush export from a token rush queue.

    The queue (``reports/token_rush/rush_queue.csv``) holds one line per item
    and realm with the price the model recommended and the bounds around it.
    Each item becomes its own group and its own operation, so two items of the
    same price band are posted at different prices.
    """
    import csv

    groups: dict[str, list[int]] = {}
    operations: dict[str, LiquidityOperation] = {}
    with queue_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("realm") != realm:
                continue
            try:
                item_id = int(row["item_id"])
                normal = int(row["recommended_copper"])
                minimum = int(row["min_copper"])
                maximum = int(row["max_copper"])
            except (KeyError, TypeError, ValueError):
                continue
            if normal <= 0:
                continue
            label = (row.get("name") or f"item {item_id}").strip()
            name = f"{item_id} {label[:36]}"
            groups[name] = [item_id]
            operations[name] = LiquidityOperation(
                normal=money(normal),
                minimum=money(minimum),
                maximum=money(maximum or normal),
                duration=12,
                undercut="1c",
                max_expires=2,
                stack_size=999 if row.get("is_commodity") == "sim" else 1,
            )
    if not groups:
        raise SystemExit(
            f"nenhuma linha usável do reino {realm!r} em {queue_path} "
            "(a fila precisa de recommended_copper, min_copper e max_copper)")
    return build_liquidity_export(groups, operations)


def emit(args, text: str, realm, out: Path, neutral: bool = False) -> bool:
    """Show, print or write one export.  Returns False in verify-only mode."""
    if args.show or not (args.write or args.stdout):
        show(text, realm)
    if args.stdout:
        print(text)
        return True
    if args.write:
        with out.open("w", encoding="utf-8", newline="") as fh:
            fh.write(text + "\n")
        label = "referência regional (factor 1,00)" if neutral else realm.describe()
        print(f"-> {out} ({len(text):,} caracteres) | {label}", file=sys.stderr)
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=ROOT / "reports" / "mog_groups")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: mogs_groups.tsm, ou mogs_groups_<reino>.tsm com --realm")
    ap.add_argument("--realm", default=None,
                    help="perfil medido do reino (nome, slug ou reino conectado, ex. Dragonmaw)")
    ap.add_argument("--realms", default=None,
                    help="lista separada por vírgulas; grava um ficheiro por reino")
    ap.add_argument("--all-realms", action="store_true",
                    help="um ficheiro para cada reino com perfil")
    ap.add_argument("--write", action="store_true", help="grava o ficheiro em --out")
    ap.add_argument("--stdout", action="store_true", help="imprime a string em vez de gravar")
    ap.add_argument("--show", action="store_true", help="decodifica e mostra grupos/operações")
    ap.add_argument("--no-operations", action="store_true",
                    help="só grupos, sem operações de auctioning")
    ap.add_argument("--conflict-inline", action="store_true",
                    help="não cria grupo Conflict; itens ficam no grupo primário")
    ap.add_argument("--operation-prefix", default="Mogs ",
                    help="prefixo do nome das operações (default: 'Mogs ')")
    ap.add_argument("--liquidity-queue", type=Path, default=None,
                    help="fila do token rush (CSV) para gerar o import de liquidação")
    ap.add_argument("--liquidity-realm", default=None,
                    help="reino da fila a exportar (default: todos os que houver)")
    args = ap.parse_args(argv)

    if args.liquidity_queue:
        return _liquidity(args)

    if args.all_realms and (args.realm or args.realms):
        ap.error("--all-realms não combina com --realm/--realms")
    if args.out and (args.realms or args.all_realms):
        ap.error("--out só faz sentido com um único reino (--realm)")

    items = load_items(args.dir)
    if not items:
        raise SystemExit(f"nenhum grupo em {args.dir} (esperava liquid.txt, ...)")

    if args.all_realms:
        targets = [p.realm for p in known_realms()]
    elif args.realms:
        targets = [name.strip() for name in args.realms.split(",") if name.strip()]
    elif args.realm:
        targets = [args.realm]
    else:
        targets = []

    if not targets:
        text, _ = build(args, items, None)
        out = args.out or args.dir / "mogs_groups.tsm"
        if not emit(args, text, None, out, neutral=True):
            print("\n(modo verificação: usa --write para gravar ou --stdout para imprimir)",
                  file=sys.stderr)
        return 0

    for name in targets:
        text, realm = build(args, items, name)
        if realm.realm == "referência regional":
            print(f"aviso: {name!r} sem perfil medido; a usar a referência regional "
                  f"(factor 1,00)", file=sys.stderr)
        out = args.out if (args.out and len(targets) == 1) else args.dir / f"mogs_groups_{slugify(name)}.tsm"
        if not emit(args, text, realm, out):
            print("\n(modo verificação: usa --write para gravar ou --stdout para imprimir)",
                  file=sys.stderr)
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
