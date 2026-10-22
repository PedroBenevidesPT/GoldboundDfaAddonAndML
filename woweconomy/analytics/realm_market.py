"""Which realm pays best for the mogs collectors still lack.

The question
    "Onde vendo isto mais caro?"  The auction house is per realm; the demand
    collected by the Data for Azeroth scan is not.  This command joins the two:
    one basket of wanted transmogs, priced in every realm the TSM snapshot
    knows about.

Inputs
    demand_appearances.csv (the scan's `report`)   one row per appearance
    db2_item_appearance                            appearance -> item ids
    db2_items                                      binding / era (BoE only)
    tsm_market                                     price per REALM
    tsm_region                                     sale rate per REGION

Why the comparison is against the other realms, not against TSM's region price
    `tsm_region.region_market` is an average over the whole region and it is
    dominated by the SAME dead listings this command exists to defuse: one
    68k ask for a 700g item on a low-pop realm drags the regional mean up and
    makes every healthy realm look like it charges a fifth of the "region".
    So the baseline is built from the snapshot itself: per item, the median
    price across the distinct markets.  Every realm is then compared with that
    median, and `--realm-cap` cuts a quote at N x the median before it can
    decide anything.

Connected realms are one market
    Stormreaver, Dragonmaw, Haomarush and Spinebreaker share a single auction
    house, and TSM stores the identical price vector under all four names.
    Counting them four times would both pad the table and bias the per-item
    median towards that one market, so realms with identical quotes are
    collapsed into one row ("Stormreaver +3").

Why the ranking is not a "sales per realm" ranking
    TSM's velocity columns (saleRate, soldPerDay) exist only per REGION: one
    number for all of EU.  The per-realm snapshot carries price, min buyout and
    numAuctions -- what people ASK, and how much is already listed.  So this
    command can say "realm X asks more for the items collectors want, with N
    listings in the way"; it cannot say "realm X sells more units".  Any tool
    that claims the second from this data is inventing it.

Which item represents an appearance
    One appearance can be granted by many items (the same look, different
    armour tokens, crafted vs dropped).  Per appearance the command keeps the
    item with the most listings across all realms -- the version the market
    actually trades -- and says so in `--explain`.  Keeping every item would
    count one look several times.

Binding and era
    Read by ITEM ID from the DB2 (never by name).  A BoP item never reaches the
    AH and an era-Classic row carries another game's price: both are dropped
    before the ranking, so the basket only contains things you can actually
    list in retail.

Run
    python -m woweconomy.analytics.realm_market --db tsm_markets.db
    python -m woweconomy.analytics.realm_market --db tsm_markets.db --min-score 50
    python -m woweconomy.analytics.realm_market --kind mounts --db tsm_markets.db
    python -m woweconomy.analytics.realm_market --db tsm_markets.db --csv realms.csv
    python -m woweconomy.analytics.realm_market --db tsm_markets.db --no-groups
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from woweconomy.analytics.collector_demand import (
    KINDS,
    default_dfa_dir,
    load_demand,
)
from woweconomy.analytics.farm_ranking import (
    FarmRankingConfig,
    item_exclusion_reason,
)
from woweconomy.services.db2_items import load_facts

COPPER_PER_GOLD = 10000.0

DEFAULT_REALM_CAP = 2.0

# Realms TSM stores commodities under ("EU (commodity)"): not a market you post
# a transmog in, and its rows would look like a realm with huge supply.
_NOT_A_REALM = ("commodity",)


# ----------------------------------------------------------------------
# Basket
# ----------------------------------------------------------------------

@dataclass
class BasketItem:
    """One wanted appearance, reduced to the item id that represents it."""
    appearance_id: int
    item_id: int
    name: str
    weight: float          # DfA `value` (0-100): audience x rarity
    gap: float             # share of the sample that still lacks it
    realm_prices: Dict[str, int] = field(default_factory=dict)   # copper
    realm_auctions: Dict[str, int] = field(default_factory=dict)
    # Region-wide, same in every realm: only used for the basket-level note.
    sold_per_day: float = 0.0


def _chunks(values: Sequence[int], size: int = 900) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _demand_rows(dfa_dir: Path, kind: str, min_score: float) \
        -> List[Tuple[int, float, float, str]]:
    """(appearance_id, weight, gap, name) for every wanted entry.

    `min_score` uses the DfA `value` column (0-100), the same cut as
    `collector_demand --min-score`.  The name is only a label: the join below
    is by id.
    """
    book = load_demand(dfa_dir, (kind,))
    seen: Dict[int, Tuple[int, float, float, str]] = {}
    for rows in book.by_name.values():
        for row in rows:
            if row.item_id <= 0:
                continue
            if min_score > 0 and (row.value or 0.0) < min_score:
                continue
            prev = seen.get(row.item_id)
            cand = (row.item_id, row.value or 0.0, row.gap, row.name)
            # One appearance can appear twice when two catalogue entries share
            # a name; keep the strongest, never sum them.
            if prev is None or cand[1] > prev[1]:
                seen[row.item_id] = cand
    return list(seen.values())


def appearance_items(db_path: str, appearance_ids: Sequence[int]) \
        -> Dict[int, List[int]]:
    """appearanceId -> [itemId] (the reverse of collector_demand's bridge)."""
    out: Dict[int, List[int]] = {}
    ids = list(dict.fromkeys(int(a) for a in appearance_ids))
    if not ids:
        return out
    con = sqlite3.connect(db_path)
    try:
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for appearance_id, item_id in con.execute(
                    f"SELECT appearanceId, itemId FROM db2_item_appearance "
                    f"WHERE appearanceId IN ({marks})", chunk):
                out.setdefault(int(appearance_id), []).append(int(item_id))
    except sqlite3.OperationalError:
        return {}  # no DB2 import: nothing to price
    finally:
        con.close()
    return out


def load_realm_market(db_path: str, item_ids: Sequence[int]) \
        -> Dict[Tuple[int, str], Tuple[int, int]]:
    """(item, realm) -> (market copper, auctions).

    `market_recent` is preferred when the snapshot has it: it is the recent
    window, closer to what a listing competes with than the all-time mean.
    """
    out: Dict[Tuple[int, str], Tuple[int, int]] = {}
    ids = list(dict.fromkeys(int(i) for i in item_ids))
    if not ids:
        return out
    con = sqlite3.connect(db_path)
    try:
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for item_id, realm, market, recent, auctions in con.execute(
                    f"SELECT itemId, realm, market, market_recent, num_auctions "
                    f"FROM tsm_market WHERE itemId IN ({marks})", chunk):
                if realm and any(tag in realm for tag in _NOT_A_REALM):
                    continue
                price = int(recent or 0) or int(market or 0)
                if price <= 0:
                    continue
                out[(int(item_id), realm)] = (price, int(auctions or 0))
    finally:
        con.close()
    return out


def load_velocity(db_path: str, item_ids: Sequence[int]) \
        -> Dict[int, Tuple[float, float]]:
    """item -> (region sale rate, region sold per day).

    Region-wide by construction: it is used as a basket-level note, never as a
    per-realm argument.
    """
    out: Dict[int, Tuple[float, float]] = {}
    ids = list(dict.fromkeys(int(i) for i in item_ids))
    if not ids:
        return out
    con = sqlite3.connect(db_path)
    try:
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for item_id, sale_rate, spd in con.execute(
                    f"SELECT itemId, region_sale_rate, region_sold_per_day "
                    f"FROM tsm_region WHERE itemId IN ({marks})", chunk):
                out[int(item_id)] = (float(sale_rate or 0.0),
                                     float(spd or 0.0))
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()
    return out


def build_basket(dfa_dir: Path, db_path: str, kind: str, min_score: float,
                 r_cfg: Optional[FarmRankingConfig] = None) \
        -> Tuple[List[BasketItem], Dict[str, int]]:
    """The tradeable, wanted looks, each with its per-realm price.

    Returns (basket, counters).  The counters are what the header prints: how
    many wanted entries existed and why the rest fell out -- an empty table
    then explains itself instead of looking like "nothing is worth selling".
    """
    counters = {"wanted": 0, "sem_item": 0, "sem_preco": 0, "fora": 0,
                "fora_era": 0, "fora_bop": 0, "fora_junk": 0}
    r_cfg = r_cfg or FarmRankingConfig(exclude_poor=False)
    rows = _demand_rows(dfa_dir, kind, min_score)
    counters["wanted"] = len(rows)
    if not rows:
        return [], counters

    bridge = appearance_items(db_path, [r[0] for r in rows])
    if not bridge:
        return [], counters

    # Every item that grants a wanted appearance, filtered by binding/era.
    candidates: Dict[int, List[int]] = {}   # appearance -> tradeable items
    facts = load_facts(db_path, only=[i for ids in bridge.values() for i in ids])
    for appearance_id, item_ids in bridge.items():
        keep: List[int] = []
        for item_id in item_ids:
            reason = item_exclusion_reason(item_id, facts, r_cfg)
            if reason:
                counters["fora"] += 1
                counters["fora_" + reason] += 1
                continue
            keep.append(item_id)
        if keep:
            candidates[appearance_id] = keep
        else:
            counters["sem_item"] += 1

    all_items = sorted({i for ids in candidates.values() for i in ids})
    market = load_realm_market(db_path, all_items)
    velocity = load_velocity(db_path, all_items)

    # One pass over the quotes: (item, realm) -> (copper, auctions).  The
    # inner loop then only looks up, instead of rescanning the market for
    # every wanted appearance (that is quadratic on a 30k-row table).
    listings: Dict[int, int] = {}
    by_item: Dict[int, Dict[str, Tuple[int, int]]] = {}
    for (iid, realm), quote in market.items():
        listings[iid] = listings.get(iid, 0) + quote[1]
        by_item.setdefault(iid, {})[realm] = quote

    basket: List[BasketItem] = []
    for appearance_id, weight, gap, name in rows:
        item_ids = candidates.get(appearance_id)
        if not item_ids:
            continue
        # The traded version: most listings across all realms, among the ids
        # that actually have a quote.  Keeping every id would count one look
        # several times.
        priced = [i for i in item_ids if i in by_item] or item_ids
        item_id = max(priced, key=lambda i: (listings.get(i, 0), i))
        quotes = by_item.get(item_id, {})
        if not quotes:
            counters["sem_preco"] += 1
            continue
        _sale_rate, spd = velocity.get(item_id, (0.0, 0.0))
        basket.append(BasketItem(
            appearance_id=appearance_id, item_id=item_id,
            name=name or (facts[item_id].name if item_id in facts else str(item_id)),
            weight=weight, gap=gap,
            realm_prices={realm: p for realm, (p, _a) in quotes.items()},
            realm_auctions={realm: a for realm, (_p, a) in quotes.items()},
            sold_per_day=spd,
        ))
    return basket, counters


# ----------------------------------------------------------------------
# Connected realms
# ----------------------------------------------------------------------

def group_realms(basket: Sequence[BasketItem], min_shared: int = 5) \
        -> Dict[str, Tuple[str, ...]]:
    """realm -> the realms that share its auction house.

    A connected group is ONE market: TSM writes the same price under every
    name.  Two realms are merged only when they agree EXACTLY (price and
    auctions) on every item they both quote, and on enough items to make that
    meaningful -- a 99% match would merge realms that merely price alike, and
    a hash of each realm's whole vector would miss A and A2 when one of them
    has a stale row the other lacks.  Merging is transitive (union-find).
    """
    realms = sorted({r for item in basket for r in item.realm_prices})
    parent = {realm: realm for realm in realms}

    def find(realm: str) -> str:
        while parent[realm] != realm:
            parent[realm] = parent[parent[realm]]
            realm = parent[realm]
        return realm

    for i, a in enumerate(realms):
        for b in realms[i + 1:]:
            if find(a) == find(b):
                continue
            shared = [item for item in basket
                      if item.realm_prices.get(a) and item.realm_prices.get(b)]
            if len(shared) < min_shared:
                continue
            if all(item.realm_prices[a] == item.realm_prices[b]
                   and item.realm_auctions.get(a, 0) == item.realm_auctions.get(b, 0)
                   for item in shared):
                parent[find(b)] = find(a)

    merged: Dict[str, List[str]] = {}
    for realm in realms:
        merged.setdefault(find(realm), []).append(realm)
    out: Dict[str, Tuple[str, ...]] = {}
    for members in merged.values():
        label = tuple(sorted(members))
        for realm in members:
            out[realm] = label
    return out


def group_label(members: Sequence[str]) -> str:
    """"Stormreaver +3" -- one row per market, never one per name."""
    if len(members) == 1:
        return members[0]
    return f"{members[0]} +{len(members) - 1}"


# ----------------------------------------------------------------------
# Ranking
# ----------------------------------------------------------------------

@dataclass
class RealmScore:
    realm: str                       # representative (first name of the group)
    realms: Tuple[str, ...] = ()     # every name in that market
    items: int = 0
    cover: float = 0.0               # share of the common basket priced here
    value: float = 0.0               # gold, weighted median price
    premium: float = 0.0             # weighted median of price / cross-realm median
    supply: float = 0.0              # median listings per basket item
    capped: int = 0                  # quotes cut by the realm cap

    @property
    def label(self) -> str:
        return group_label(self.realms or (self.realm,))


def _weight(item: BasketItem) -> float:
    """Demand weight: the DfA value, floored so a 0 still counts a little.

    `value` already multiplies audience by rarity (0-100).  A row at exactly 0
    still got into the basket because the user did not cut it, so it keeps a
    token weight instead of disappearing from every average.
    """
    return max(item.weight, 0.1)


def _weighted_median(pairs: Sequence[Tuple[float, float]]) -> float:
    """Median of (weight, value) pairs, 0.0 when there is no weight.

    A plain mean is useless here: mog markets are thin, and one 7.5M listing
    on a dead appearance moves it by thousands of gold.  The median ignores
    the tail by construction, and the weights keep the wanted looks dominant.
    """
    total = sum(w for w, _ in pairs)
    if total <= 0:
        return 0.0
    running = 0.0
    ordered = sorted(pairs, key=lambda p: p[1])
    for weight, value in ordered:
        running += weight
        if running >= total / 2.0:
            return value
    return ordered[-1][1]


def reference_prices(basket: Sequence[BasketItem],
                     markets: Sequence[str]) -> Dict[int, int]:
    """appearance -> median price across the distinct markets (copper).

    Built from the snapshot, not from `tsm_region`: the regional average is
    dragged by dead-realm asks, and using it as the baseline made the healthy
    realms look 5x cheap.  The median across markets survives one bad quote.
    """
    out: Dict[int, int] = {}
    for item in basket:
        prices = [item.realm_prices[m] for m in markets if item.realm_prices.get(m)]
        if prices:
            out[item.appearance_id] = int(statistics.median(prices))
    return out


def cap_price(price_copper: int, reference_copper: int,
              cap: float = DEFAULT_REALM_CAP) -> int:
    """Cut a realm quote at reference x cap (0 / no reference = no cap).

    Same convention and default as `calc_price.py --realm-cap`: an ask more
    than twice the cross-realm median is not a price, it is one optimistic
    listing.  The cap is what stops a single troll auction from deciding which
    realm is "the best".
    """
    if cap <= 0 or reference_copper <= 0:
        return price_copper
    return min(price_copper, int(reference_copper * cap))


def common_basket(basket: Sequence[BasketItem], markets: Sequence[str],
                  min_share: float = 0.9) -> List[BasketItem]:
    """The items every compared market prices (or nearly every one).

    Without this, medians are not comparable: a realm that only lists the 60%
    most expensive looks becomes "2x more expensive" purely because the cheap,
    common items are absent from its snapshot.  Requiring the same items in
    every market is what makes the columns mean the same thing.
    """
    need = max(1, int(len(markets) * min_share))
    return [item for item in basket
            if sum(1 for m in markets if item.realm_prices.get(m)) >= need]


def score_realms(basket: Sequence[BasketItem], cap: float = DEFAULT_REALM_CAP,
                 groups: Optional[Dict[str, Tuple[str, ...]]] = None) \
        -> Tuple[List[RealmScore], int, Dict[str, Tuple[str, ...]]]:
    """Rank markets by how far above the cross-realm median they price the basket.

    Returns (scores, size of the common basket, realm -> market group).  Every
    market is scored on the SAME items, so "asks more" cannot be an artefact
    of which items that realm happens to have listed.  `premium` is the
    demand-weighted median of price / reference: 1.0 = asks the median.
    """
    groups = groups or group_realms(basket)
    markets = sorted({members[0] for members in groups.values()})
    if not markets:
        return [], 0, groups
    common = common_basket(basket, markets)
    if len(basket) >= 50 and len(common) < 50:
        # On a big basket a tiny intersection would make the ranking noise;
        # widen it to the items most markets have and let `cover` show the
        # difference.  A small basket is already the whole story.
        common = common_basket(basket, markets, min_share=0.5)
    reference = reference_prices(basket, markets)

    out: List[RealmScore] = []
    for realm in markets:
        prices: List[Tuple[float, float, int, float]] = []
        capped = 0
        for item in common:
            raw = item.realm_prices.get(realm)
            if not raw:
                continue
            ref = reference.get(item.appearance_id, 0)
            price = cap_price(raw, ref, cap)
            capped += 1 if price != raw else 0
            prices.append((_weight(item), price / COPPER_PER_GOLD,
                           item.realm_auctions.get(realm, 0),
                           ref / COPPER_PER_GOLD))
        if not prices:
            continue
        value = _weighted_median([(p[0], p[1]) for p in prices])
        ratios = [(p[0], p[1] / p[3]) for p in prices if p[3] > 0]
        premium = _weighted_median(ratios)
        out.append(RealmScore(
            realm=realm, realms=groups[realm],
            items=len(prices),
            cover=100.0 * len(prices) / len(common),
            value=value, premium=premium,
            supply=statistics.median(p[2] for p in prices),
            capped=capped,
        ))
    return sorted(out, key=lambda s: -s.premium), len(common), groups


def best_realm_per_item(basket: Sequence[BasketItem], top: int = 10,
                        cap: float = DEFAULT_REALM_CAP,
                        groups: Optional[Dict[str, Tuple[str, ...]]] = None) \
        -> List[Tuple[BasketItem, str, float, float, int, bool]]:
    """(item, market, price, premium vs reference, auctions, was_capped).

    Items ordered by demand weight, the market picked by the highest capped
    premium over the cross-realm median -- the question the seller asks ("onde
    ponho este item?").  The flag says the raw quote was cut, i.e. it is one
    listing asking a fantasy price.
    """
    groups = groups or group_realms(basket)
    refs = reference_prices(basket, sorted({m[0] for m in groups.values()}))
    out = []
    ordered = sorted(basket, key=lambda i: -_weight(i))[:top]
    for item in ordered:
        ref = refs.get(item.appearance_id, 0)

        def score(kv: Tuple[str, int]) -> Tuple[float, float]:
            _realm, raw = kv
            price = cap_price(raw, ref, cap)
            return ((price / ref) if ref else 0.0, price)

        realm, raw = max(item.realm_prices.items(), key=score)
        price = cap_price(raw, ref, cap)
        premium = (price / ref) if ref else 0.0
        out.append((item, group_label(groups[realm]),
                    price / COPPER_PER_GOLD, premium,
                    item.realm_auctions.get(realm, 0), price != raw))
    return out


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------

def _money(value: float) -> str:
    return f"{value:,.0f}g"


def render(basket: Sequence[BasketItem], scores: Sequence[RealmScore],
           counters: Dict[str, int], kind: str, min_score: float,
           top: int, top_items: int, explain: bool,
           cap: float = DEFAULT_REALM_CAP, common: int = 0,
           groups: Optional[Dict[str, Tuple[str, ...]]] = None) -> str:
    groups = groups or group_realms(basket)
    lines: List[str] = []
    lines.append(f"\n=== onde os {kind} em falta valem mais ===")
    lines.append(
        f"cabaz: {len(basket)} aparencias com preco "
        f"(de {counters['wanted']} em falta com valor >= {min_score:g})")
    dropped = [f"{counters['fora_bop']} BoP", f"{counters['fora_era']} era-Classic"]
    if counters["sem_preco"]:
        dropped.append(f"{counters['sem_preco']} sem preco TSM em nenhum reino")
    if counters["sem_item"]:
        dropped.append(f"{counters['sem_item']} sem item BoE no DB2")
    lines.append("fora do cabaz: " + ", ".join(dropped))
    merged = [m for m in groups.values() if len(m) > 1]
    if merged:
        lines.append("mercados juntos (mesmo leilao): " + "; ".join(
            group_label(m) + f" ({', '.join(m)})" for m in
            {m for m in merged}))
    with_volume = sum(1 for i in basket if i.sold_per_day > 0)
    lines.append(f"cabaz comum comparado em todos os mercados: {common} itens")
    lines.append(f"itens com venda diaria no TSM (regiao): {with_volume}/{len(basket)}"
                 " -- saleRate e' da REGIAO, nao distingue reinos")
    lines.append(f"preco por reino cortado a {cap:g}x a mediana dos reinos "
                 "(--realm-cap; 0 desliga)")
    if explain:
        lines.append("")
        for item in sorted(basket, key=lambda i: -_weight(i))[:top_items]:
            lines.append(
                f"  {item.name[:38]:38s} ap={item.appearance_id} "
                f"item={item.item_id} valor={item.weight:.1f} falta={item.gap:.0f}%")
    if not scores:
        lines.append("\nsem dados: nenhum item do cabaz tem preco por reino no TSM")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{'#':>3}  {'mercado':26s} {'itens':>5} {'cobertura':>9} "
                 f"{'preco mediano':>13} {'vs mediana':>10} {'leiloes':>8} "
                 f"{'cortes':>6}")
    lines.append("-" * 96)
    for idx, s in enumerate(scores[:top], 1):
        prem = f"{s.premium:.2f}x" if s.premium else "-"
        lines.append(
            f"{idx:>3}  {s.label[:26]:26s} {s.items:>5} {s.cover:>8.0f}% "
            f"{_money(s.value):>13} {prem:>10} {s.supply:>8.0f} "
            f"{s.capped:>6}")
    if len(scores) > top:
        lines.append(f"  ... {len(scores) - top} mercados a mais (--top 0 para todos)")

    best = best_realm_per_item(basket, top_items, cap, groups)
    if best:
        lines.append("")
        lines.append(f"{'item':34s} {'melhor mercado':26s} {'preco':>10} "
                     f"{'vs mediana':>10} {'leiloes':>8}  nota")
        lines.append("-" * 96)
        for item, market, price, premium, auctions, was_capped in best:
            prem = f"{premium:.2f}x" if premium else "-"
            note = (f"pedido acima de {cap:g}x (cortado)" if was_capped else "")
            lines.append(f"{item.name[:34]:34s} {market[:26]:26s} "
                         f"{_money(price):>10} {prem:>10} {auctions:>8}  {note}")
    lines.append("")
    lines.append("leitura: preco por reino mede o que se PEDE, nao o que se vende;")
    lines.append("         'vs mediana' > 1 = o mercado pede acima da mediana dos reinos;")
    lines.append("         'leiloes' alto = mais concorrencia; 'cortes' alto = mercado")
    lines.append("         com pedidos irrealistas, logo preco pouco fiavel.")
    return "\n".join(lines)


def write_csv(path: str, scores: Sequence[RealmScore]) -> int:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["market", "realms", "items", "coverage_pct",
                         "median_price_gold", "premium_vs_median",
                         "median_auctions", "capped_quotes"])
        for s in scores:
            writer.writerow([s.label, ",".join(s.realms), s.items,
                             f"{s.cover:.1f}", f"{s.value:.0f}",
                             f"{s.premium:.3f}", f"{s.supply:.0f}", s.capped])
    return len(scores)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Onde os mog em falta valem mais: junta a demanda do DfA "
                    "aos precos POR REINO do TSM.")
    ap.add_argument("--kind", default="appearances", choices=KINDS,
                    help="catalogo do DfA a usar (default: appearances)")
    ap.add_argument("--dfa-dir", default="",
                    help="pasta com demand_*.csv (default: $DFA_DIR ou o "
                         "woweconomy/dfa do scanner)")
    ap.add_argument("--db", default="tsm_markets.db",
                    help="base com tsm_market/tsm_region/db2_* (default: "
                         "tsm_markets.db)")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="valor DfA minimo do item (coluna 'valor', 0-100); "
                         "0 desliga")
    ap.add_argument("--top", type=int, default=25,
                    help="mercados a mostrar (0 = todos; default: 25)")
    ap.add_argument("--top-items", type=int, default=10,
                    help="itens na tabela 'melhor mercado' (0 = nenhum)")
    ap.add_argument("--realm-cap", type=float, default=DEFAULT_REALM_CAP,
                    help="corta o preco de cada reino a N x a mediana dos "
                         "reinos, para uma listagem unica a 7M nao ganhar o "
                         "ranking (0 = sem corte; default: %(default)s)")
    ap.add_argument("--no-groups", action="store_true",
                    help="nao juntar reinos ligados (mostra as 4 copias do "
                         "mesmo leilao de Stormreaver)")
    ap.add_argument("--csv", default="",
                    help="escreve o ranking dos mercados neste CSV (; separado)")
    ap.add_argument("--explain", action="store_true",
                    help="mostra o item escolhido para cada aparencia")
    args = ap.parse_args(argv)

    dfa_dir = Path(args.dfa_dir) if args.dfa_dir else default_dfa_dir()
    if dfa_dir is None:
        print("error: nao encontrei a pasta do DfA (usa --dfa-dir ou DFA_DIR)",
              file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"error: base nao encontrada: {args.db}", file=sys.stderr)
        return 2

    basket, counters = build_basket(dfa_dir, args.db, args.kind,
                                    args.min_score)
    groups = group_realms(basket)
    if args.no_groups:
        groups = {realm: (realm,) for members in groups.values() for realm in members}
    scores, common, groups = score_realms(basket, args.realm_cap, groups)
    print(render(basket, scores, counters, args.kind, args.min_score,
                 args.top or len(scores), args.top_items, args.explain,
                 args.realm_cap, common, groups))
    if args.csv:
        n = write_csv(args.csv, scores)
        print(f"CSV -> {args.csv} ({n} mercados)")
    if not basket:
        print("error: cabaz vazio -- ve os contadores acima", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
