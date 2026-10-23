"""Pricing groups for the transmog inventory: discovery, evidence, strategies.

This module answers a *segmentation* question, not a per-item one:

    which items behave alike enough to share one pricing strategy, and what
    should that strategy be?

Four rules keep the analysis honest, and they are enforced by the structure of
the code rather than by convention:

**Observed statistics and operational policy are different objects.**
    ``BandEvidence`` holds what was *measured* (the clearance distribution, the
    sample sizes, the decay).  ``BandPolicy`` holds what we *decide* (the
    normal/minimum/maximum factors, the floors, the margins).  A floor is a
    policy decision and is never described as statistically derived.

**The two clearance estimators stay separate.**
    ``E1`` is the regional average sale against the local wall: a large
    sample, but the average sale includes snipes, so it is a *lower* bound on
    where the market trades.  ``E2`` is the account's realized sale against
    the wall *at the moment of the sale*: the honest measure, but it needs
    dated sales and a reconstructable wall history, which this database does
    not have.  When E2 is unavailable it is reported as unavailable -- the
    current wall is never silently substituted for the wall at sale time.

**Behaviour never changes a band coefficient.**
    The band coefficients are estimated from the band alone.  A group may only
    carry a *separate* adjustment, and that adjustment is estimated from the
    group's own clearance against its band baseline -- with a sample and a
    materiality test.  Otherwise it is 1.0 and labelled heuristic.  The
    classification and the price adjustment are kept apart on purpose.

**The realm factor is applied once.**
    The anchor is local (``dbmarket``); the realm factor belongs to the
    *regional* anchor and to the campaign layer, never to both.

The data behind it (all read-only):

``tsm_realm_price``   the local state of every realm: market value, cheapest
                      local listing (the *wall*), 3-day ask, 60-day ask
``tsm_region_price``  the regional reference: market value, average sale
                      price, sale rate, sold per day
``market_snapshots``  the region-wide series (27/07, 28/07, 13/10)
``journal_stats``     the account's own realized sales and expirations
``dfa.db``            the Data for Azeroth sample: how many sampled accounts
                      still *lack* an appearance (potential demand, nothing
                      more)
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Sequence

COPPER_PER_GOLD = 10_000

# --- candidate price bands ------------------------------------------------
# Nine candidates.  They are *candidates*: the analysis merges or flags the
# sparse ones (see ``merge_sparse_bands``) and may split a band when the
# evidence separates.  Edges in gold, ascending.
BAND_EDGES_G = (100, 500, 2_000, 5_000, 15_000, 50_000, 150_000, 500_000)


def _edge_label(edge: int) -> str:
    return f"{edge // 1000}k" if edge >= 1000 else str(edge)


def band_of(gold_value: float) -> str:
    """The band label of a price level, e.g. ``"2k-5k"``."""
    for index, edge in enumerate(BAND_EDGES_G):
        if gold_value < edge:
            if index == 0:
                return f"<{_edge_label(edge)}"
            return f"{_edge_label(BAND_EDGES_G[index - 1])}-{_edge_label(edge)}"
    return f">{_edge_label(BAND_EDGES_G[-1])}"


def band_order() -> list[str]:
    return [band_of(edge - 1) for edge in BAND_EDGES_G] + [band_of(BAND_EDGES_G[-1] + 1)]


# --- the clearance estimators --------------------------------------------


@dataclass(frozen=True)
class Clearance:
    """One clearance estimate, with everything needed to judge it."""

    key: str                 # "E1" or "E2"
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    n: int = 0
    source: str = ""
    caveat: str = ""
    usable: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.key} (n={self.n})"


E1_SOURCE = "tsm_region_price.avg_sale_price / tsm_realm_price.min_buyout"
E1_CAVEAT = ("a média de venda regional inclui snipes (vendas a 1% do ask): é um "
             "limite inferior de onde o mercado troca, não o preço justo")
E2_SOURCE = "journal_stats.sold_copper/sold_count / parede local na data da venda"
E2_CAVEAT = ("exige vendas datadas e a parede histórica reconstruível; sem datas "
             "no journal, a parede actual NÃO substitui a parede da venda")

E2_UNAVAILABLE_NOTE = (
    "indisponível: o journal não guarda datas das vendas, logo não há parede "
    "histórica na data da venda; usar a parede actual seria substituir uma "
    "medição por outra")


# --- the E2 machinery -----------------------------------------------------
# E2 is the honest measure: the price the account really got, against the
# cheapest listing that existed *at that moment*.  It needs two things this
# database does not have: dated sales, and a wall history to look back into.
# The machinery is here so that the moment the Journalator export carries
# dates, the estimate becomes available -- without ever substituting the
# current wall for the historical one.


def snapshot_id_at(con: sqlite3.Connection, when: str, kind: str = "realm_items") -> int:
    """The last snapshot of a kind at or before a timestamp."""
    row = con.execute(
        "SELECT MAX(snapshot_id) FROM tsm_snapshot WHERE kind=? AND updated_at<=?",
        (kind, when)).fetchone()
    return int(row[0]) if row and row[0] else 0


def wall_at(con: sqlite3.Connection, scope: str, item_id: int,
            when: str) -> int | None:
    """The cheapest local listing of an item at a point in time.

    ``tsm_price_change`` is the delta log of the per-realm fetches: the last
    row at or before the timestamp is the state of that listing then.  A gap
    (no row before that moment) means "unknown", not "zero".
    """
    snapshot = snapshot_id_at(con, when)
    if not snapshot:
        return None
    row = con.execute(
        "SELECT min_buyout FROM tsm_price_change WHERE kind='realm_items' "
        "AND scope=? AND key_id=? AND snapshot_id<=? AND min_buyout>0 "
        "ORDER BY snapshot_id DESC LIMIT 1",
        (scope, int(item_id), snapshot)).fetchone()
    return int(row[0]) if row and row[0] else None


def estimate_e2(con: sqlite3.Connection, sales: Iterable[tuple]) -> Clearance:
    """E2: the account's realized sale against the wall at the moment of sale.

    ``sales`` are ``(item_id, scope, price_copper, when)`` tuples.  A sale
    without a date, or a sale whose wall at that date cannot be found, is
    *skipped* -- never measured against the current wall.
    """
    ratios: list[float] = []
    without_date = 0
    without_wall = 0
    for item_id, scope, price_copper, when in sales:
        if not when:
            without_date += 1
            continue
        wall = wall_at(con, scope, item_id, when)
        if not wall:
            without_wall += 1
            continue
        ratios.append(price_copper / wall)
    if not ratios:
        parts = []
        if without_date:
            parts.append(f"{without_date} venda(s) sem data no journal: a parede na "
                         f"data da venda não pode ser reconstruída")
        if without_wall:
            parts.append(f"{without_wall} venda(s) sem parede reconstruível")
        note = ("; ".join(parts) if parts else E2_UNAVAILABLE_NOTE)
        return Clearance(key="E2", n=0, source=E2_SOURCE, caveat=E2_CAVEAT,
                         usable=False, note=note)
    return Clearance(
        key="E2",
        p25=percentile(ratios, 0.25),
        p50=percentile(ratios, 0.50),
        p75=percentile(ratios, 0.75),
        n=len(ratios),
        source=E2_SOURCE,
        caveat=E2_CAVEAT,
        usable=True,
        note=(f"{without_date + without_wall} venda(s) excluídas por falta de data "
              f"ou de parede"),
    )


# --- the observed evidence of a band -------------------------------------


@dataclass(frozen=True)
class BandEvidence:
    """What was measured for one price band.  No policy, no group labels."""

    band: str
    n_items: int = 0
    e1: Clearance = field(default_factory=lambda: Clearance(key="E1"))
    e2: Clearance = field(default_factory=lambda: Clearance(key="E2"))
    decay_78d: float | None = None       # the ask average over 78 days
    n_decay: int = 0
    wall_ratio_p50: float | None = None  # the market's own ask vs its lowest ask
    sold_per_day_p50: float | None = None
    sale_rate_p50: float | None = None
    demand_rate_p50: float | None = None
    n_demand: int = 0
    #: the account's own realized sales in this band
    own_sales: int = 0
    own_clearance_p50: float | None = None

    @property
    def sample(self) -> int:
        return self.e1.n

    @property
    def confidence(self) -> str:
        if self.e1.n >= 2_000:
            return "alta"
        if self.e1.n >= 300:
            return "média"
        if self.e1.n >= 30:
            return "baixa"
        return "nenhuma"

    def preferred(self) -> Clearance:
        """E2 when it is usable, otherwise E1 -- with the caveat attached."""
        if self.e2.usable and self.e2.n >= 3:
            return self.e2
        return self.e1


# --- the operational policy of a band -------------------------------------


@dataclass(frozen=True)
class BandPolicy:
    """What we decide to do with a band.  Every field says where it comes from."""

    band: str
    normal: float
    minimum: float
    maximum: float
    source: str
    uncertainty: str = ""
    confidence: str = "nenhuma"
    #: the policy floors, kept apart from the statistics on purpose
    floor_normal: float = 0.15
    floor_minimum: float = 0.08

    def as_dict(self) -> dict:
        return {
            "band": self.band,
            "normal": round(self.normal, 3),
            "min": round(self.minimum, 3),
            "max": round(self.maximum, 3),
            "source": self.source,
            "uncertainty": self.uncertainty,
            "confidence": self.confidence,
        }


#: the status quo of the project: the band coefficients of the normal pricing
#: model (``mog_operations.BAND_MULTIPLIERS``).  They are *not* re-derived
#: here; they are the baseline the new evidence is compared against.
STATUS_QUO = {
    "<100": (0.45, 0.25, 0.90),
    "100-500": (0.55, 0.30, 1.00),
    "500-2k": (0.62, 0.35, 1.15),
    "2k-5k": (0.68, 0.40, 1.30),
    "5k-15k": (0.78, 0.45, 1.45),
    "15k-50k": (0.85, 0.50, 1.60),
    "50k-150k": (0.85, 0.50, 1.60),
    "150k-500k": (0.85, 0.50, 1.60),
    ">500k": (0.85, 0.50, 1.60),
}


def status_quo_for(band: str) -> tuple[float, float, float]:
    """The current model's factors for a band (interpolated between edges)."""
    if band in STATUS_QUO:
        return STATUS_QUO[band]
    for index, edge in enumerate(BAND_EDGES_G):
        if band == f"<{_edge_label(edge)}":
            return STATUS_QUO[f"<{_edge_label(edge)}"]
        if band == f">{_edge_label(BAND_EDGES_G[-1])}":
            return STATUS_QUO[f">{_edge_label(BAND_EDGES_G[-1])}"]
    return (0.78, 0.45, 1.45)


#: The policy floors.  They are **decisions**, not statistics: without them a
#: band whose clearance is 0.02 would post at 2 % of the market's own ask,
#: which the sample does not support.  They are parameters, not findings.
FLOOR_NORMAL = 0.15
FLOOR_MINIMUM = 0.08
CEILING_NORMAL = 1.0


def band_coefficients(evidence: BandEvidence, *,
                      floor_normal: float = FLOOR_NORMAL,
                      floor_minimum: float = FLOOR_MINIMUM,
                      ceiling_normal: float = CEILING_NORMAL) -> tuple[float, float, float, str]:
    """Candidate coefficients from the measured clearance, with the spread.

    The result is a **proposal**, not an operating value: the clip is the
    policy floor, and the returned string says what the numbers came from and
    how wide they are.
    """
    clearance = evidence.preferred()
    if not clearance.usable or clearance.p50 is None:
        return (0.0, 0.0, 0.0, "sem evidência de preço")
    normal = max(floor_normal, min(ceiling_normal, clearance.p50))
    minimum = max(floor_minimum, min(normal, clearance.p25 if clearance.p25 is not None
                                     else normal * 0.6))
    maximum = max(normal, clearance.p75 if clearance.p75 is not None else normal * 1.5)
    spread = ""
    if clearance.p25 is not None and clearance.p75 is not None:
        spread = (f"p25 {clearance.p25:.3f} / p50 {clearance.p50:.3f} / "
                  f"p75 {clearance.p75:.3f} (n={clearance.n})")
    return (normal, minimum, maximum, spread)


# --- the groups -----------------------------------------------------------

DEMAND_THRESHOLD = 0.05     # the share of sampled accounts still wanting it
DECAY_FALLING = 0.90        # the ask fell this much over 78 days


@dataclass(frozen=True)
class GroupDefinition:
    """A behaviour group.  The rules are explicit; the price is not."""

    key: str
    name: str
    purpose: str
    inclusion: str
    exclusion: str
    #: liquidation undercut against a valid comparable listing
    undercut: tuple[float, float] = (0.10, 0.20)
    duration_h: int = 24
    hold_days: int = 14
    postable: bool = True
    note: str = ""


GROUPS: Dict[str, GroupDefinition] = {}


def _group(**kwargs) -> GroupDefinition:
    definition = GroupDefinition(**kwargs)
    GROUPS[definition.key] = definition
    return definition


_group(
    key="demand_falling",
    name="Procura medida, ask a descer",
    purpose="a amostra mostra compradores em falta e o ask esta a cair: o preco tem de acompanhar",
    inclusion="procura medida acima do limiar E o ask caiu mais de 10% em 78 dias",
    exclusion="qualquer um dos dois criterios",
    undercut=(0.15, 0.25),
    duration_h=12,
    hold_days=14,
)

_group(
    key="demand_stable",
    name="Procura medida, ask estavel",
    purpose="a amostra mostra compradores em falta e o ask segura: e o grupo que sustenta o preco",
    inclusion="procura medida acima do limiar E o ask caiu 10% ou menos em 78 dias",
    exclusion="procura nao medida, ou o ask a descer",
    undercut=(0.05, 0.15),
    duration_h=48,
    hold_days=30,
    note="o preco pedido precisa de sobreviver a 78 dias: e o unico grupo em que segura",
)

_group(
    key="no_demand_falling",
    name="Sem procura medida, ask a descer",
    purpose="a amostra nao mostra compradores e o ask esta a cair: o preco nao se segura",
    inclusion="procura medida abaixo do limiar E o ask caiu mais de 10%",
    exclusion="procura acima do limiar, ou procura desconhecida",
    undercut=(0.20, 0.30),
    duration_h=12,
    hold_days=7,
    note="desconto maior e perda controlada: aqui o objectivo e sair, nao ganhar",
)

_group(
    key="no_demand_stable",
    name="Sem procura medida, ask estavel",
    purpose="o ask segura mas a amostra nao mostra compradores: o preco e plausivel, a venda nao",
    inclusion="procura medida abaixo do limiar E o ask caiu 10% ou menos",
    exclusion="procura acima do limiar, ou procura desconhecida",
    undercut=(0.10, 0.20),
    duration_h=24,
    hold_days=14,
)

_group(
    key="unknown_falling",
    name="Procura desconhecida, ask a descer",
    purpose="a amostra nao cobre o item e o ask esta a cair: so a velocidade decide",
    inclusion="o item nao esta na amostra DfA E o ask caiu mais de 10%",
    exclusion="procura medida, de qualquer valor",
    undercut=(0.15, 0.25),
    duration_h=12,
    hold_days=7,
)

_group(
    key="unknown_stable",
    name="Procura desconhecida, ask estavel",
    purpose="a amostra nao cobre o item e o ask segura: o preco segue a banda",
    inclusion="o item nao esta na amostra DfA E o ask caiu 10% ou menos",
    exclusion="procura medida, de qualquer valor",
    undercut=(0.10, 0.20),
    duration_h=24,
    hold_days=14,
)

_group(
    key="commodity",
    name="Materiais",
    purpose="mercado unico de commodities: o reino nao muda nada",
    inclusion="o item tem preco de commodity na base",
    exclusion="tudo o resto",
    undercut=(0.0, 0.05),
    duration_h=24,
    hold_days=14,
)

_group(
    key="no_reference",
    name="Sem referencia de preco",
    purpose="sem preco nenhum: nao ha o que postar, ha o que liquidar de outra forma",
    inclusion="sem preco de mercado, sem parede local e sem preco de venda",
    exclusion="qualquer referencia de preco",
    postable=False,
    undercut=(0.0, 0.0),
    duration_h=24,
    hold_days=7,
    note="vendor ou destruicao: um anuncio sem preco e um deposito perdido",
)


# --- the facts of one item ------------------------------------------------


@dataclass
class ItemFacts:
    """The measured behaviour of one item, in one realm."""

    item_id: int
    name: str = ""
    realm: str = ""
    qty: int = 1
    is_commodity: bool = False

    # price levels (copper)
    local_market: int = 0
    local_wall: int = 0
    local_recent: int = 0
    local_historical: int = 0
    region_market: int = 0
    region_avg_sale: int = 0
    #: the commodity market (materials): one market for the whole region
    commodity_market: int = 0

    # potential demand (Data for Azeroth sample) -- not purchase intent
    want_rate: float | None = None
    wanters_eu: int | None = None
    accounts_sampled: int = 0
    #: the DfA sample covers this item's appearance?  When it does not, the
    #: demand is **unknown**, not zero.
    demand_known: bool = False

    # the account's own history
    sales: int = 0
    expired: int = 0
    attempts: int = 0
    sold_copper: int = 0

    # the 78 day behaviour of the ask
    decay_78d: float | None = None

    # reference quality
    vendor_sell: int | None = None
    wall_support: str = "desconhecida"

    @property
    def band(self) -> str:
        return band_of(self.reference_gold)

    @property
    def reference_gold(self) -> float:
        for value in (self.local_market, self.local_wall, self.region_market,
                      self.commodity_market):
            if value:
                return value / COPPER_PER_GOLD
        return 0.0

    @property
    def realized_price(self) -> int | None:
        if self.sales and self.sold_copper:
            return self.sold_copper // self.sales
        return None

    @property
    def has_reference(self) -> bool:
        return bool(self.local_market or self.local_wall or self.region_market
                    or self.local_recent or self.local_historical
                    or self.commodity_market)

    @property
    def demand_measured(self) -> bool:
        return self.want_rate is not None


def assign_group(facts: ItemFacts) -> str:
    """The behaviour group of one item.

    The axes are measurements, never prices:

    * **demand state** -- the sample shows buyers still missing the look
      (measured), shows almost none (measured, low), or does not cover the
      item at all (unknown).  Unknown is *not* "no demand".
    * **ask behaviour** -- the ask fell over 78 days (the price is moving) or
      held (it is not).

    The two axes are candidates: the analysis merges the cells that do not
    differ in turnover (see ``distinctness``).
    """
    if facts.is_commodity:
        return "commodity"
    if not facts.has_reference:
        return "no_reference"
    falling = facts.decay_78d is not None and facts.decay_78d < DECAY_FALLING
    if not facts.demand_known:
        return "unknown_falling" if falling else "unknown_stable"
    rate = facts.want_rate
    if rate is not None and rate >= DEMAND_THRESHOLD:
        return "demand_falling" if falling else "demand_stable"
    return "no_demand_falling" if falling else "no_demand_stable"


# --- group adjustment, estimated ------------------------------------------


@dataclass(frozen=True)
class GroupStatistics:
    """What a group looks like, in a band, on statistics that are not the price.

    The *turnover* (sold per day, sale rate) is the statistic that separates
    the groups without the confound that ruins the clearance ratio: in a
    popular market the average sale is dragged down by snipes, so a lower
    clearance there does **not** mean the market pays less.
    """

    group: str
    band: str
    n_items: int = 0
    n_with_sales: int = 0
    sold_per_day_p50: float | None = None
    sale_rate_p50: float | None = None
    demand_rate_p50: float | None = None
    decay_p50: float | None = None
    clearance_p50: float | None = None
    clearance_n: int = 0

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "band": self.band,
            "n_items": self.n_items,
            "n_with_sales": self.n_with_sales,
            "sold_per_day_p50": self.sold_per_day_p50,
            "sale_rate_p50": self.sale_rate_p50,
            "demand_rate_p50": self.demand_rate_p50,
            "decay_p50": self.decay_p50,
            "clearance_p50": self.clearance_p50,
            "clearance_n": self.clearance_n,
        }


#: two groups in the same band are only *distinct* if their turnover differs
#: by at least this much, with enough items behind it.
TURNOVER_MARGIN = 0.20
MIN_TURNOVER_SAMPLE = 100


def distinctness(a: GroupStatistics, b: GroupStatistics) -> tuple[str, str]:
    """Are two groups different enough to deserve different strategies?

    The test is on the *turnover* (how fast the market turns over), not on the
    price: the clearance ratio is confounded by sniping, and the price level
    is the band itself.

    Returns ``("distinct" | "same" | "unknown", reason)``.  "unknown" is not
    "same": a thin sample means the question could not be answered, and the
    two groups must not be merged on that basis.
    """
    if a.n_with_sales < MIN_TURNOVER_SAMPLE or b.n_with_sales < MIN_TURNOVER_SAMPLE:
        return "unknown", (f"amostra de turnover insuficiente "
                           f"({a.n_with_sales} / {b.n_with_sales})")
    if a.sold_per_day_p50 is None or b.sold_per_day_p50 is None:
        return "unknown", "sem estimativa de velocidade"
    if a.sold_per_day_p50 == 0 and b.sold_per_day_p50 == 0:
        return "same", "ambos sem movimento estimado"
    high = max(a.sold_per_day_p50, b.sold_per_day_p50)
    low = min(a.sold_per_day_p50, b.sold_per_day_p50)
    if high == 0:
        return "same", "ambos sem movimento estimado"
    ratio = (high - low) / high
    if ratio < TURNOVER_MARGIN:
        return "same", (f"velocidade quase igual ({a.sold_per_day_p50:.3f} vs "
                        f"{b.sold_per_day_p50:.3f} vendas/dia, diferença {ratio:.0%})")
    return "distinct", (f"velocidade diferente ({a.sold_per_day_p50:.3f} vs "
                        f"{b.sold_per_day_p50:.3f} vendas/dia, diferença {ratio:.0%})")


# --- group adjustment, estimated ------------------------------------------


@dataclass(frozen=True)
class GroupAdjustment:
    """The price adjustment of a group against its band baseline."""

    group: str
    band: str
    multiplier: float
    basis: str
    n: int
    baseline: float | None
    group_clearance: float | None
    material: bool
    confidence: str
    #: the measured ratio, kept even when it is not applied
    measured_ratio: float | None = None

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "band": self.band,
            "multiplier": round(self.multiplier, 3),
            "basis": self.basis,
            "n": self.n,
            "baseline": self.baseline,
            "group_clearance": self.group_clearance,
            "measured_ratio": (round(self.measured_ratio, 3)
                               if self.measured_ratio is not None else None),
            "material": self.material,
            "confidence": self.confidence,
        }


MIN_GROUP_SAMPLE = 100        # observations needed to estimate an adjustment
MATERIAL_MARGIN = 0.15        # a difference below this is not material

#: why the E1 ratio is *not* applied as a price adjustment on its own.
E1_NOT_IDENTIFIED = (
    "E1 não está identificada para o preço: num mercado popular a média de venda "
    "é puxada para baixo pelos snipes, uma clearance menor NÃO significa que o "
    "mercado pague menos — significa que há mais snipes")


def estimate_adjustment(
    group: str,
    band: str,
    group_clearance: float | None,
    group_n: int,
    baseline: float | None,
    *,
    own_sales: int = 0,
    own_clearance: float | None = None,
) -> GroupAdjustment:
    """The adjustment of one group in one band.

    It is only applied when the group's **own realized sales** support it: the
    account's own sales are the only observations whose price was chosen by
    us, so the sign of the effect is identified.  The E1 ratio is measured and
    reported, but it is not applied -- see ``E1_NOT_IDENTIFIED``.

    Without that evidence the adjustment is 1.0, labelled heuristic: the
    classification survives, the price adjustment does not.
    """
    ratio = None
    if group_clearance is not None and baseline not in (None, 0):
        ratio = group_clearance / baseline
    if own_sales >= 3 and own_clearance and baseline not in (None, 0):
        multiplier = own_clearance / baseline
        material = abs(multiplier - 1.0) >= MATERIAL_MARGIN
        if material:
            return GroupAdjustment(
                group, band, round(multiplier, 3),
                f"vendas da própria conta no grupo (n={own_sales}) ÷ clearance da banda",
                own_sales, baseline, own_clearance, True,
                "baixa" if own_sales < 30 else "média", ratio)
        return GroupAdjustment(
            group, band, 1.0,
            f"vendas da própria conta no grupo (n={own_sales}): diferença não material",
            own_sales, baseline, own_clearance, False, "baixa", ratio)
    if group_n < MIN_GROUP_SAMPLE:
        return GroupAdjustment(
            group, band, 1.0,
            f"sem amostra suficiente (n={group_n} < {MIN_GROUP_SAMPLE}): 1,00 heurístico",
            group_n, baseline, group_clearance, False, "nenhuma", ratio)
    return GroupAdjustment(
        group, band, 1.0,
        f"1,00 heurístico: {E1_NOT_IDENTIFIED}",
        group_n, baseline, group_clearance, False, "nenhuma", ratio)


# --- the strategy of a group ---------------------------------------------


#: The TSM semantics this model relies on (validated against the project's
#: operation definitions and the addon's ``AuctioningOperation.MakePostDecision``):
#:
#: * with a competitor, the posted price is ``lowestAuction.buyout - undercut``,
#:   clamped to ``[minPrice, maxPrice]``; if the result is under ``minPrice``
#:   the operation does not post (``priceReset = "none"``);
#: * with no competitor, the posted price is ``normalPrice``;
#: * ``aboveMax = "maxPrice"`` means a wall above the cap is posted at the cap;
#: * ``maxExpires`` counts expirations *since the last sale*, so it is a
#:   holding window in posting cycles, not a duration;
#: * ``duration`` is the length of a posting; ``undercut`` is the amount below
#:   the wall, and on Retail anything under one silver is coerced to 0c.
TSM_SEMANTICS = {
    "competitive_price": "minPrice <= lowestAuction.buyout - undercut <= maxPrice",
    "no_competition_price": "normalPrice",
    "below_minimum": "não posta (priceReset = none)",
    "above_maximum": "posta no maxPrice (aboveMax = maxPrice)",
    "max_expires": "expirações desde a última venda (janela de retenção)",
    "undercut": "retail sanitiza undercut < 1s para 0c",
    "duration": "horas de cada post",
}

#: The normal anchor.  It is **local**: ``dbmarket`` is the realm's own ask
#: average.  Because the anchor is local, the realm factor (local ÷ regional)
#: is NOT applied again on top of it -- that would count the same adjustment
#: twice.  The realm factor belongs to the regional anchor, and to the
#: campaign layer, where it is applied once.
NORMAL_ANCHOR = "first(dbmarket, dbregionmarketavg, dbregionhistorical)"


@dataclass(frozen=True)
class Strategy:
    """The pricing strategy of one group: normal and liquidation, apart."""

    group: str
    name: str
    normal_formula: str
    rush_formula: str
    band: str
    band_policy: BandPolicy
    adjustment: GroupAdjustment
    undercut: tuple[float, float]
    duration_h: int
    hold_days: int
    postable: bool
    confidence: str
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "name": self.name,
            "normal_formula": self.normal_formula,
            "rush_formula": self.rush_formula,
            "band": self.band,
            "band_normal": self.band_policy.normal,
            "band_min": self.band_policy.minimum,
            "band_max": self.band_policy.maximum,
            "adjustment": self.adjustment.multiplier,
            "adjustment_basis": self.adjustment.basis,
            "undercut_low": self.undercut[0],
            "undercut_high": self.undercut[1],
            "duration_h": self.duration_h,
            "hold_days": self.hold_days,
            "postable": self.postable,
            "confidence": self.confidence,
            "notes": " | ".join(self.notes),
        }


def build_strategy(
    group: str,
    band_policy: BandPolicy,
    adjustment: GroupAdjustment,
    *,
    notes: Iterable[str] = (),
) -> Strategy:
    """The normal and liquidation formulas of one group in one band.

    * **normal** -- the anchor times the band factor times the group
      adjustment.  The band factor comes from the policy; the group
      adjustment from the evidence, and is 1.00 (heuristic) when there is no
      sample.
    * **rush** -- the liquidation target.  It is expressed *relative to the
      wall* when a valid comparable exists (the wall is the market's own
      lowest ask, and undercutting it is how a sale happens), and it falls
      back to the reference times the band factor when there is no comparable.
      It never applies the group adjustment to the wall: the adjustment is
      estimated on the clearance of the *ask*, and there is no evidence that
      it transfers to the wall.
    """
    definition = GROUPS[group]
    normal = band_policy.normal * adjustment.multiplier
    minimum = min(band_policy.minimum * adjustment.multiplier, normal)
    maximum = max(band_policy.maximum * adjustment.multiplier, normal)
    all_notes = list(notes)
    if definition.note:
        all_notes.append(definition.note)
    if not adjustment.material:
        all_notes.append("ajuste do grupo = 1,00 (heurístico): a classificação "
                         "existe, o preço não é ajustado por ela")
    rush = (f"parede_local * (1 - undercut[{definition.undercut[0]:.2f},"
            f"{definition.undercut[1]:.2f}])  # se houver comparável válido\n"
            f"      senão: {normal:.3f} * {NORMAL_ANCHOR}")
    return Strategy(
        group=group,
        name=definition.name,
        normal_formula=f"{normal:.3f} * {NORMAL_ANCHOR}",
        rush_formula=rush,
        band=band_policy.band,
        band_policy=band_policy,
        adjustment=adjustment,
        undercut=definition.undercut,
        duration_h=definition.duration_h,
        hold_days=definition.hold_days,
        postable=definition.postable,
        confidence=adjustment.confidence,
        notes=tuple(all_notes),
    )


# --- helpers ---------------------------------------------------------------


def merge_sparse_bands(evidence: Mapping[str, BandEvidence],
                       minimum: int = 30) -> tuple[Dict[str, BandEvidence], list[str]]:
    """Fold bands with too few observations into their neighbour.

    A band with a handful of observations cannot support its own
    coefficients, so it is folded into the next band that has enough and
    *flagged* in the report.  The flag is part of the output: the reader must
    know which boundaries are evidence and which are candidates.

    The folded band keeps the statistics of the band it joins -- averaging
    percentiles of two different populations would be meaningless.
    """
    order = band_order()
    kept = [band for band in order
            if band in evidence and evidence[band].e1.n >= minimum]
    merged: Dict[str, BandEvidence] = {band: evidence[band] for band in kept}
    flags: list[str] = []
    for band in order:
        item = evidence.get(band)
        if item is None or item.e1.n >= minimum:
            continue
        target = next((other for other in kept
                       if order.index(other) > order.index(band)), None)
        if target is None:
            merged[band] = item
            flags.append(f"{band}: apenas {item.e1.n} observações — mantido, mas a "
                         f"confiança é nula")
            continue
        flags.append(f"{band}: apenas {item.e1.n} observações — fundido em {target} "
                     f"(as estatísticas de {target} são mantidas)")
    return merged, flags


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * q))
    return ordered[index]


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None
