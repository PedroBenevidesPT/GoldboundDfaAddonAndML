"""Compare what you own with what the 1M+ collectors still lack.

The inventory is a TSM-style export (``ItemID;Name;...;Quantity``); the demand
table is ``reports/item_demand.csv`` (one row per BoE item).  The join is by
base item id, because that is what both files share -- the demand table also
carries the appearance id, but the inventory does not.

The useful outputs are:

* ``matched`` -- demand items you already have, with quantity and stock value;
* ``missing`` -- demand items with quantity 0 (the shopping list);
* ``extra``   -- inventory items that are not on the high-score list, which is
  not necessarily bad, just outside this particular segment.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

COPPER_PER_GOLD = 10_000

# O export do TSM não tem um nome fixo: o mesmo ficheiro aparece como
# ``inventory.csv`` ou ``inventario.csv``, na raiz ou em ``scripts/``.  A ordem
# é a da preferência; a primeira que existir é a usada.
INVENTORY_CANDIDATES = (
    "inventory.csv",
    "inventario.csv",
    "scripts/inventory.csv",
    "scripts/inventario.csv",
    "reports/inventory.csv",
    "reports/inventario.csv",
)


def resolve_inventory(root: str | Path, explicit: str = "") -> Path:
    """Caminho do inventário: o dado à mão, senão o primeiro candidato que existe.

    Levanta ``FileNotFoundError`` com a lista de sítios procurados, para o erro
    dizer o que fazer em vez de rebentar num ``open``.
    """
    root = Path(root)
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(
                f"inventário não encontrado: {path}")
        return path
    for rel in INVENTORY_CANDIDATES:
        path = root / rel
        if path.is_file():
            return path
    tried = ", ".join(INVENTORY_CANDIDATES)
    raise FileNotFoundError(
        f"nenhum inventário encontrado em {root} (procurei: {tried})")


@dataclass(frozen=True)
class InventoryLine:
    item_id: int
    name: str
    qty: int


@dataclass(frozen=True)
class DemandLine:
    item_id: int
    item_name: str
    appearance_id: int
    name: str
    source: str
    grants: int
    wanters: int
    eligible: int
    want_rate: float
    price_copper: int
    gold_gap: float

    @property
    def price_gold(self) -> float:
        return self.price_copper / COPPER_PER_GOLD


@dataclass(frozen=True)
class Match:
    demand: DemandLine
    qty: int

    @property
    def stock_value(self) -> float:
        """Gold the current quantity is worth at the regional market value."""
        return self.qty * self.demand.price_gold

    @property
    def coverage(self) -> float:
        return self.qty / self.demand.wanters if self.demand.wanters else 0.0


def _pick(row: dict, *names: str) -> str:
    for wanted in names:
        for key, value in row.items():
            if key and key.strip().lower() == wanted.lower():
                return (value or "").strip()
    return ""


def load_inventory(path: str | Path) -> dict[int, InventoryLine]:
    """Read a TSM export, auto-detecting ``;`` vs ``,``; duplicates add up."""
    path = Path(path)
    with path.open(encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        rows = list(csv.DictReader(fh, delimiter=delim))
    out: dict[int, InventoryLine] = {}
    for row in rows:
        raw = _pick(row, "ItemID", "item_id", "id")
        if not raw:
            continue
        try:
            item_id = int(float(raw))
        except ValueError:
            continue
        qty_raw = _pick(row, "Quantity", "Qty", "quantity", "qty") or "1"
        try:
            qty = int(float(qty_raw))
        except ValueError:
            qty = 1
        if item_id <= 0 or qty <= 0:
            continue
        name = _pick(row, "Name", "nome")
        old = out.get(item_id)
        if old:
            out[item_id] = InventoryLine(item_id, old.name or name, old.qty + qty)
        else:
            out[item_id] = InventoryLine(item_id, name, qty)
    return out


def realm_wanters(wants: np.ndarray, realms: Sequence[str],
                  columns: Sequence[int], per_realm: int = 3,
                  min_wanters: int = 1) -> list[dict]:
    """Onde falta um item: contas por reino que nao tem nenhuma das aparências.

    ``wants`` é contas x slots com 1 = nao tem.  Um item com varias aparências
    conta o comprador **uma vez** (união), nao uma vez por aparência.
    ``per_realm <= 0`` devolve todos os reinos.
    """
    if not columns or wants.size == 0:
        return []
    cols = np.array(list(columns), dtype=np.int64)
    out = []
    for name in sorted(set(realms)):
        mask = np.array([r == name for r in realms])
        if not mask.any():
            continue
        sub = wants[mask][:, cols]
        n = int(sub.any(axis=1).sum()) if sub.shape[1] > 1 else int(sub[:, 0].sum())
        accounts = int(mask.sum())
        if n >= min_wanters:
            out.append({"realm": name, "wanters": n, "accounts": accounts,
                        "share": n / accounts if accounts else 0.0})
    out.sort(key=lambda d: (-d["wanters"], -d["share"], d["realm"]))
    return out if per_realm <= 0 else out[:per_realm]


def sale_pressure(wanters: int, listings: int) -> tuple[float, str]:
    """Compradores por anúncio: quanto mais alto, mais depressa deve sair.

    Nao é a probabilidade de venda da TSM (essa existe por região, nao por
    reino); é a razão procura/oferta que a base permite calcular em cada reino.
    Sem anúncio nenhum nao há oferta a competir: o valor é a própria procura e a
    etiqueta avisa que nao há lista.
    """
    if wanters <= 0:
        return 0.0, "sem procura"
    if listings <= 0:
        return float(wanters), "sem anúncio"
    return wanters / listings, ""


def missing_names(inventory: dict[int, InventoryLine]) -> list[int]:
    """Ids whose name the export did not carry (``Unknown`` / empty)."""
    return [item_id for item_id, line in inventory.items()
            if not line.name or line.name.strip().lower() in ("unknown", "?")]


def resolve_names(inventory: dict[int, InventoryLine],
                  names: dict[int, str]) -> dict[int, InventoryLine]:
    """Fill in names the export lost, keeping what the export did carry.

    O export do TSM escreve ``Unknown`` quando não tem o nome em cache; a base
    do DB2 tem-no sempre, por isso o mesmo item deixa de aparecer anónimo.
    """
    out = dict(inventory)
    for item_id in missing_names(inventory):
        nome = names.get(item_id)
        if nome:
            out[item_id] = InventoryLine(item_id, nome,
                                         inventory[item_id].qty)
    return out


def load_demand(path: str | Path) -> list[DemandLine]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for row in rows:
        try:
            item_id = int(float(row.get("item_id", "")))
            wanters = int(float(row.get("wanters", 0)))
            eligible = int(float(row.get("eligible", 0)))
            price = int(float(row.get("price_copper", 0)))
        except (TypeError, ValueError):
            continue
        out.append(DemandLine(
            item_id=item_id,
            item_name=row.get("item_name", "") or "",
            appearance_id=int(float(row.get("appearance_id", 0) or 0)),
            name=row.get("name", "") or "",
            source=row.get("source", "") or "",
            grants=int(float(row.get("grants", 1) or 1)),
            wanters=wanters,
            eligible=eligible,
            want_rate=float(row.get("want_rate", 0) or 0),
            price_copper=price,
            gold_gap=float(row.get("gold_gap", 0) or 0),
        ))
    return out


def compare(inventory: dict[int, InventoryLine],
            demand: Iterable[DemandLine]) -> tuple[list[Match], list[DemandLine],
                                                   list[InventoryLine]]:
    """Return (matched, missing, extra).  Missing is the shopping list."""
    demand = list(demand)
    matched: list[Match] = []
    missing: list[DemandLine] = []
    wanted_ids = set()
    for line in demand:
        wanted_ids.add(line.item_id)
        hit = inventory.get(line.item_id)
        if hit and hit.qty > 0:
            matched.append(Match(demand=line, qty=hit.qty))
        else:
            missing.append(line)
    extra = [line for item_id, line in inventory.items()
             if item_id not in wanted_ids]
    return matched, missing, extra
