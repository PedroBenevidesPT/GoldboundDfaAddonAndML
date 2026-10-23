"""Converte listas de item IDs para o formato de import do TSM 4.

    python scripts/tsm_item_list.py reports/mog_groups/liquid.txt
    python scripts/tsm_item_list.py --in-place reports/mog_groups
    python scripts/tsm_item_list.py --check reports/mog_groups

O TSM 4 espera cada item como ``i:<itemID>`` e os itens separados por
vírgulas, numa só linha:

    i:12345,i:67890,i:11223,i:44556

Aceita como entrada IDs ``i:12345``, ``item:12345`` ou só ``12345``,
separados por vírgula, ponto e vírgula, espaço ou linha. Isto cobre os TXT
antigos (um ID por linha) e as listas copiadas do Wowhead. Duplicados são
removidos e a ordem original é mantida.

Sem ``--in-place`` o resultado vai para o stdout (um cabeçalho por ficheiro
vai para o stderr), para poderes copiar a linha directamente. ``--check``
não escreve nada e sai com código 1 se algum ficheiro não estiver já no
formato canónico.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woweconomy.analytics.tsm_ids import parse_item_ids, tsm_item_string  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def _files(paths: list[Path], pattern: str) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.glob(pattern)))
        elif path.is_file():
            found.append(path)
        else:
            raise SystemExit(f"não existe: {path}")
    # de-duplicate while preserving order
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    if not unique:
        raise SystemExit(f"nenhum ficheiro {pattern} encontrado")
    return unique


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Converte listas de IDs para `i:<id>,i:<id>,...` (TSM 4)."
    )
    ap.add_argument("paths", nargs="+", type=Path,
                    help="ficheiros ou pastas com listas de IDs")
    ap.add_argument("--in-place", action="store_true",
                    help="reescreve os ficheiros em vez de imprimir no stdout")
    ap.add_argument("--check", action="store_true",
                    help="só verifica; sai com 1 se algo não estiver convertido")
    ap.add_argument("--glob", default="*.txt",
                    help="padrão usado ao percorrer pastas (default: *.txt)")
    ap.add_argument("--quiet", action="store_true",
                    help="não imprime o cabeçalho por ficheiro no stderr")
    args = ap.parse_args(argv)

    if args.in_place and args.check:
        ap.error("--in-place e --check são exclusivos")

    status = 0
    for path in _files(args.paths, args.glob):
        try:
            text = path.read_text(encoding="utf-8-sig")
            ids = parse_item_ids(text)
        except (OSError, ValueError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            status = 1
            continue

        line = tsm_item_string(ids)
        canonical = line + "\n" if line else ""
        if not args.quiet:
            print(f"{path}: {len(ids)} ids", file=sys.stderr)

        if not ids:
            print(f"{path}: lista vazia, nada a escrever", file=sys.stderr)
            continue

        if args.check:
            if text.strip() != line:
                print(f"{path}: não está no formato TSM", file=sys.stderr)
                status = 1
            continue

        if args.in_place:
            if text == canonical:
                if not args.quiet:
                    print(f"{path}: já convertido", file=sys.stderr)
                continue
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(canonical)
            if not args.quiet:
                print(f"{path}: convertido", file=sys.stderr)
        else:
            print(line)

    return status


if __name__ == "__main__":
    raise SystemExit(main())
