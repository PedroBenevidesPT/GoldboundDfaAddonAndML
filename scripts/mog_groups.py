"""Divide os mogs em grupos de preço a partir da base TSM.

    python scripts/mog_groups.py
    python scripts/mog_groups.py --realm silvermoon --out reports/mog_groups_silvermoon
    python scripts/mog_groups.py --top 10

A classificação é feita por **aparência** (o look), não por item: todas as
variantes que partilham uma aparência ficam no mesmo grupo, mesmo que só uma
delas tenha preço. `--items` escolhe o universo: `all` (default, todos os itens
com aparência; os looks sem preço ficam `unknown`), `variants` (só as variantes
de aparências com preço) ou `priced` (só itens com preço regional, o antigo).

A saída é `reports/mog_groups/mog_groups.csv` (todos) e um CSV/TXT por grupo
(`liquid`, `collector`, `slow`, `very_slow`, `unproven`, `unknown`, `conflict`).
O `.txt` tem uma linha no formato de import do TSM 4
(`i:12345,i:67890,...`), pronta a colar no Import do grupo.

Os cortes são rótulos, não instruções para vender/vendor: `spd >= 0.1/dia` é
movimento estimado ativo; `0.01–0.1` é slow; `0 < spd < 0.01` é very slow, mas
positivo; `spd = 0` é unproven e ausência é unknown. Vendoring é uma decisão
separada baseada no ask, depósito estimado e número de expirações.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.mog_groups import (  # noqa: E402
    GROUPS, classify, load_universe, write_groups,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=ROOT / "tsm_markets.db")
    ap.add_argument("--realm", default="spinebreaker",
                    help="slug do reino (default: spinebreaker)")
    ap.add_argument("--collector", type=Path,
                    default=ROOT / "reports" / "inventory_prob_novo.csv",
                    help="CSV do modelo de coleccionadores")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "mog_groups")
    ap.add_argument("--items", choices=("priced", "variants", "all"),
                    default="all",
                    help="universo: priced (só com preço regional), variants "
                         "(todas as variantes de aparências com preço) ou all "
                         "(todos os itens com aparência; sem preço = unknown)")
    ap.add_argument("--top", type=int, default=5,
                    help="exemplos por grupo no resumo")
    args = ap.parse_args(argv)

    rows = load_universe(args.db, args.realm, args.collector, items=args.items)
    counts = write_groups(rows, args.out)

    appearances = len({m.appearance_id for m in rows if m.appearance_id is not None})
    print(f"{len(rows):,} itens | {appearances:,} aparências | {args.realm} "
          f"| items={args.items} | grupos em {args.out}")
    for group in GROUPS + ("conflict",):
        # empty categories need no example lookup; unknown movement prints as n/a
        n = counts.get(group, 0)
        if not n:
            continue
        print(f"  {group:10s} {n:6,}")
        examples = [m for m in rows if classify(m).group == group
                    or (group == "conflict" and classify(m).conflict)]
        examples.sort(key=lambda m: -(m.price_g or 0))
        for m in examples[: args.top]:
            spd_text = f"{m.spd:.4f}" if m.spd is not None else "n/a"
            print(f"      {m.item_id:>7} {m.name[:44]:44s} "
                  f"regional_spd={spd_text:>7} preço={(m.price_g or 0):9,.0f}g")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
