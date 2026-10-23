"""An evidence-based pricing engine: estimated realizable value, liquidity, and
a recommended listing price -- with the provenance of every number.

The principle
    **Listings show what sellers want to receive; transactions show what
    someone actually paid.**  Every price source in TSM is one of the two, and
    the two must not be averaged as if they measured the same thing.

    * the *asking* references (``dbmarket``, ``dbrecent``, ``dbhistorical``,
      ``dbminbuyout``, ``dbregionmarketavg``, ``dbregionhistorical``) say what
      the market is asking for.  They can be inflated, thin, stale or
      manipulated;
    * the *transactions* (``dbregionsaleavg`` and the account's own realized
      sales) say what a buyer paid.

    So the estimate is built in that order:

    1. the **transaction value**, from the observed sales and their quality;
    2. the **asking level**, from the asking families, each family reduced to
       one value (correlated TSM sources are not independent evidence);
    3. the **compatibility** between the two, which is where the two
       hypotheses live:

       * *the asking price is inflated* -- the sales do not confirm the ask.
         The ask loses confidence and the estimate follows the sales;
       * *the price is high but commercially defensible* -- there are
         recurring sales, enough volume, and the evidence is compatible with
         the high ask.  The model does **not** lower the price because it
         looks expensive.

    The weights are **not** chosen here.  They come from a calibration
    (see :class:`Calibration`) that measures, against the account's own
    realized sales, how well each family predicts a real sale price.  Until
    that calibration exists, the asking families are treated as *speculative
    signals*, not as commercial truth.

Missing data never becomes zero, the TSM sentinel is rejected with a reason,
and every estimate says which sources produced it.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

COPPER_PER_GOLD = 10_000

#: TSM writes this when it has no data.  It is not a price.
SENTINEL_COPPER = 99_999_999_000

# --- the kinds of evidence -------------------------------------------------
ASKING = "asking"            # an asking price: what people list, not what they get
MARKET_REFERENCE = "market_reference"   # a decayed market average
TRANSACTION = "transaction"   # a price someone actually paid
LOCAL = "local"
REGIONAL = "regional"

FAMILY_LOCAL_ASK = "local_ask"
FAMILY_REGIONAL_ASK = "regional_ask"
FAMILY_REGIONAL_SALES = "regional_sales"
FAMILY_OWN_SALES = "own_sales"

TRANSACTION_FAMILIES = (FAMILY_REGIONAL_SALES, FAMILY_OWN_SALES)
ASKING_FAMILIES = (FAMILY_LOCAL_ASK, FAMILY_REGIONAL_ASK)


@dataclass(frozen=True)
class SourceValue:
    """One price observation, with everything known about its quality."""

    name: str
    value: int
    kind: str = ASKING
    scope: str = LOCAL
    family: str = ""
    timestamp: str = ""
    age_hours: float = 0.0
    depth: Optional[int] = None       # competing listings, when the source exposes it
    dispersion: Optional[float] = None  # internal spread of the family, when known
    sample_size: Optional[int] = None   # only when the source genuinely exposes it
    volume: Optional[float] = None      # a volume signal (sold per day), not a price
    note: str = ""

    @property
    def valid(self) -> bool:
        if self.value is None:
            return False
        if not math.isfinite(float(self.value)):
            return False
        if self.value <= 0:
            return False
        if self.value >= SENTINEL_COPPER:
            return False
        return True

    def reject_reason(self) -> str:
        if self.value is None:
            return "ausente"
        if not math.isfinite(float(self.value)):
            return "valor não finito"
        if self.value <= 0:
            return "ausente (zero ou negativo)"
        if self.value >= SENTINEL_COPPER:
            return "sentinel da TSM (sem dados)"
        return ""


@dataclass
class Evidence:
    """Everything known about one item in one realm."""

    item_id: int
    name: str = ""
    realm: str = ""
    band: str = ""
    qty: int = 1
    sources: List[SourceValue] = field(default_factory=list)
    #: the account's own observed history (counts, not rates)
    own_sales: int = 0
    own_expired: int = 0
    own_sold_copper: int = 0
    own_sell_through: Optional[float] = None
    #: the measured demand (the DfA sample that still lacks the look)
    want_rate: Optional[float] = None
    demand_known: bool = False
    #: the measured clearance factor of the market (sale average / ask level),
    #: measured across the catalogue for the item's band
    market_clearance: Optional[float] = None
    market_clearance_n: int = 0
    #: the local competition
    listings: Optional[int] = None
    #: the vendor price (the floor of any listing)
    vendor_sell: Optional[int] = None
    #: the costs (assumed unless proven otherwise)
    ah_fee_pct: float = 0.05
    deposit_pct: float = 0.05
    deposit_vendor_cap_pct: float = 3.0
    costs_verified: bool = False


@dataclass(frozen=True)
class Calibration:
    """The measured quality of each source family.

    ``family_weight`` is the *measured* weight: how much a family moves the
    estimate towards a real sale price.  ``family_error`` is the measured
    relative error (the median of |log(prediction / sale price)|) and
    ``coverage`` the share of the calibrated sample in which the family was
    available at all.  A family that was never available cannot be trusted
    just because it is theoretically the best one.
    """

    family_weight: Mapping[str, float] = field(default_factory=dict)
    family_error: Mapping[str, float] = field(default_factory=dict)
    coverage: Mapping[str, float] = field(default_factory=dict)
    measured_at: str = ""
    sample: int = 0
    source: str = ""
    #: the weight of a family that has no calibration yet
    default_weight: float = 0.5

    def weight_of(self, family: str) -> float:
        return float(self.family_weight.get(family, self.default_weight))

    def as_dict(self) -> dict:
        return {
            "measured_at": self.measured_at,
            "sample": self.sample,
            "source": self.source,
            "family_weight": dict(self.family_weight),
            "family_error": dict(self.family_error),
            "coverage": dict(self.coverage),
        }


@dataclass(frozen=True)
class EngineConfig:
    """Every tunable of the engine.  Nothing here is a hidden constant."""

    #: the prior weight of a family, used only when there is no calibration
    family_prior: Mapping[str, float] = None
    #: the freshness half-life, in hours
    freshness_half_life_h: float = 24.0
    #: the depth that counts as "a real market"
    depth_full: int = 5
    depth_floor: int = 0
    #: the dispersion (relative) that still counts as consistent
    dispersion_full: float = 0.50
    dispersion_floor: float = 0.05
    #: the relative disagreement above which the asking reference is distrusted
    divergence_penalty: float = 0.75
    #: the minimum total weight for an estimate to be trusted
    min_total_weight: float = 0.35
    #: the confidence below which the answer is INSUFFICIENT_EVIDENCE
    min_confidence: float = 0.30
    #: the tolerance (in log terms) for "the ask is compatible with the sales"
    compatibility_tolerance: float = 0.35
    #: the minimum number of sales that makes an item's own sale average usable
    min_own_sample: int = 3
    #: the listing strategy
    horizon_days: int = 12
    hours_per_day: float = 3.0
    #: the maximum discount the recommendation may apply to the estimate
    max_listing_discount: float = 0.15
    ah_fee_pct: float = 0.05
    deposit_pct: float = 0.05
    deposit_vendor_cap_pct: float = 3.0

    def __post_init__(self):
        if self.family_prior is None:
            object.__setattr__(self, "family_prior", {
                FAMILY_LOCAL_ASK: 0.60,        # asking, local: a reference, not truth
                FAMILY_REGIONAL_ASK: 0.40,    # asking, regional: further away
                FAMILY_REGIONAL_SALES: 1.00,  # a transaction: the target quantity
                FAMILY_OWN_SALES: 1.00,       # a local transaction, tiny sample
            })


# --- the quality factors ---------------------------------------------------


def freshness_weight(age_hours: float, config: EngineConfig) -> float:
    """How much a fresh observation is worth relative to a stale one."""
    if age_hours <= 0:
        return 1.0
    if config.freshness_half_life_h <= 0:
        return 1.0
    return max(0.15, 0.5 ** (age_hours / config.freshness_half_life_h))


def depth_weight(depth: Optional[int], config: EngineConfig) -> float:
    """How much a price backed by listings is worth."""
    if depth is None:
        return 0.6          # the count is not exposed: partial trust
    if depth <= config.depth_floor:
        return 0.35
    if depth >= config.depth_full:
        return 1.0
    span = max(1, config.depth_full - config.depth_floor)
    return 0.35 + 0.65 * (depth - config.depth_floor) / span


def dispersion_weight(dispersion: Optional[float], config: EngineConfig) -> float:
    """How much a price backed by a consistent market is worth."""
    if dispersion is None:
        return 0.8
    if dispersion <= config.dispersion_full:
        return 1.0
    if dispersion >= config.dispersion_floor:
        span = max(1e-9, config.dispersion_full - config.dispersion_floor)
        return max(0.25, 1.0 - 0.75 * (dispersion - config.dispersion_full) / span)
    return 0.25


def sample_weight(sample: Optional[int], config: EngineConfig) -> float:
    """How much a price backed by a real number of transactions is worth.

    ``None`` means the source does not expose a sample size (TSM's regional
    average sale price does not) -- that is **not** the same as a sample of
    one, and it is not invented here.
    """
    if sample is None:
        return 0.85
    if sample <= 0:
        return 0.35
    if sample >= 10:
        return 1.0
    return 0.35 + 0.65 * sample / 10.0


# --- the value estimate ----------------------------------------------------


@dataclass
class ValueEstimate:
    """The estimated realizable value, with its provenance."""

    price: Optional[int]
    low: Optional[int]
    high: Optional[int]
    confidence: float
    basis: str = ""                # "transacções" | "referência de mercado" | ""
    contributions: Tuple[dict, ...] = ()
    rejected: Tuple[dict, ...] = ()
    flags: Tuple[str, ...] = ()
    explanation: str = ""
    hypotheses: Tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.price is not None and self.confidence > 0

    def as_dict(self) -> dict:
        return {
            "estimated_price": self.price,
            "low": self.low,
            "high": self.high,
            "confidence": round(self.confidence, 3),
            "basis": self.basis,
            "contributions": "; ".join(
                f"{c['family']}={c['value'] / COPPER_PER_GOLD:,.0f}g(w={c['weight']:.3f})"
                for c in self.contributions),
            "rejected": "; ".join(f"{c['name']}: {c['reason']}" for c in self.rejected),
            "flags": " | ".join(self.flags),
            "hypotheses": " | ".join(self.hypotheses),
            "explanation": self.explanation,
        }


def _family_value(sources: Sequence[SourceValue]) -> Tuple[Optional[int], Optional[float]]:
    """One value per family (a robust median) and the family's internal spread."""
    values = [s.value for s in sources]
    if not values:
        return None, None
    median = statistics.median(values)
    if len(values) == 1:
        return int(median), 0.0
    spread = (max(values) - min(values)) / median if median else None
    return int(median), spread


def _family_weight(family: str, members: Sequence[SourceValue],
                   config: EngineConfig, calibration: Optional[Calibration]) -> float:
    """The weight of one family: the calibrated trust times the quality factors."""
    prior = (calibration.weight_of(family) if calibration
             else config.family_prior.get(family, 0.5))
    age = min(m.age_hours for m in members)
    depth = next((m.depth for m in members if m.depth is not None), None)
    sample = next((m.sample_size for m in members if m.sample_size is not None), None)
    dispersion = next((m.dispersion for m in members if m.dispersion is not None), None)
    if dispersion is None:
        dispersion = _family_value(members)[1]
    return (prior
            * freshness_weight(age, config)
            * depth_weight(depth, config)
            * dispersion_weight(dispersion, config)
            * sample_weight(sample, config))


def estimate_value(evidence: Evidence,
                    config: EngineConfig | None = None,
                    calibration: Optional[Calibration] = None) -> ValueEstimate:
    """The estimated realizable market value of one item.

    The transaction families decide the estimate when they exist (they are the
    quantity being estimated); the asking families are then used to check
    whether the ask is compatible with the sales, and to provide a qualified
    fallback when there is no transaction evidence at all.
    """
    config = config or EngineConfig()
    rejected: List[dict] = []
    families: Dict[str, List[SourceValue]] = {}
    for source in evidence.sources:
        if not source.valid:
            rejected.append({"name": source.name, "reason": source.reject_reason()})
            continue
        families.setdefault(source.family, []).append(source)

    flags: List[str] = []
    hypotheses: List[str] = []
    contributions: List[dict] = []
    family_values: Dict[str, Tuple[int, float, float]] = {}

    for family, members in families.items():
        value, spread = _family_value(members)
        if value is None:
            continue
        weight = _family_weight(family, members, config, calibration)
        if weight <= 0:
            rejected.append({"name": family, "reason": "peso nulo"})
            continue
        family_values[family] = (value, weight, spread if spread is not None else 0.0)
        age = min(m.age_hours for m in members)
        depth = next((m.depth for m in members if m.depth is not None), None)
        sample = next((m.sample_size for m in members if m.sample_size is not None), None)
        contributions.append({
            "family": family,
            "value": value,
            "weight": round(weight, 4),
            "members": [m.name for m in members],
            "kind": members[0].kind,
            "scope": members[0].scope,
            "age_hours": age,
            "depth": depth,
            "sample_size": sample,
            "volume": next((m.volume for m in members if m.volume is not None), None),
            "dispersion": spread,
        })

    if not family_values:
        return ValueEstimate(None, None, None, 0.0, "", (), tuple(rejected),
                              tuple(flags), "nenhuma fonte válida: sem estimativa")

    # --- the transaction value -----------------------------------------
    transaction_families = {f: v for f, v in family_values.items()
                            if f in TRANSACTION_FAMILIES}
    asking_families = {f: v for f, v in family_values.items()
                        if f in ASKING_FAMILIES}

    transaction_value = transaction_price = None
    if transaction_families:
        total = sum(w for _v, w, _s in transaction_families.values())
        transaction_price = int(round(sum(v * w for v, w, _s in transaction_families.values())
                                    / total))
        transaction_value = transaction_price
        basis = "transacções"
    else:
        basis = "referência de mercado"
        flags.append("sem evidência de transacção: a referência de mercado é "
                     "especulativa, não verdade comercial")

    # --- the asking level and the clearance -----------------------------
    ask_value = None
    if asking_families:
        total = sum(w for _v, w, _s in asking_families.values())
        ask_value = int(round(sum(v * w for v, w, _s in asking_families.values()) / total))

    clearance = evidence.market_clearance
    clearance_source = f"banda (n={evidence.market_clearance_n})"
    # the item's own clearance wins when the sample supports it
    own = family_values.get(FAMILY_OWN_SALES)
    if own and evidence.own_sales >= config.min_own_sample and ask_value:
        own_clearance = own[0] / ask_value
        if 0 < own_clearance <= 1.5:
            clearance = own_clearance
            clearance_source = f"próprio ({evidence.own_sales} vendas)"
    if clearance is None:
        flags.append("sem factor de clearance medido: a referência de ask é usada "
                     "tal e qual")

    # --- the two hypotheses ---------------------------------------------
    if transaction_price is not None and ask_value:
        ratio = transaction_price / ask_value
        if ratio < (1 - config.compatibility_tolerance):
            hypotheses.append(
                f"preço anunciado inflacionado: as vendas ficam {ratio:.0%} do ask; "
                f"a referência de announce perde confiança")
        elif ratio > (1 + config.compatibility_tolerance):
            hypotheses.append(
                f"as vendas estão {(ratio - 1):.0%} acima do ask: o item vende "
                f"acima do mercado; o preço não é baixado")
        else:
            hypotheses.append("ask e vendas compatíveis: o preço anunciado é "
                              "comercialmente defensável")
    if transaction_price is not None and evidence.own_sales >= config.min_own_sample:
        hypotheses.append(f"transacções próprias ({evidence.own_sales}) corroboram o "
                          f"nível")

    # --- the estimate ----------------------------------------------------
    if transaction_price is not None:
        price = transaction_price
    elif ask_value is not None:
        price = int(round(ask_value * (clearance if clearance is not None else 1.0)))
        if clearance is not None:
            flags.append(f"preço derivado do ask pelo factor de clearance medido "
                         f"({clearance:.3f}, {clearance_source})")
    else:
        price = None

    if price is None or price <= 0:
        return ValueEstimate(None, None, None, 0.0, "", tuple(contributions),
                              tuple(rejected), tuple(flags), "sem estimativa")

    # the confidence: the evidence that produced it, and the disagreement
    total_weight = sum(w for _v, w, _s in family_values.values())
    confidence = min(1.0, total_weight / 2.0)
    if basis == "referência de mercado":
        confidence *= 0.6      # asking only: speculative
    if transaction_price is not None and ask_value:
        spread = abs(math.log(max(1, transaction_price) / max(1, ask_value)))
        confidence *= max(0.3, 1.0 - spread / 2.0)
        if spread > config.divergence_penalty:
            flags.append(f"discrepância entre ask e vendas: {spread:.0%} em log")
            confidence *= 0.8
    if evidence.market_clearance is not None and 0 < evidence.market_clearance_n < 50:
        confidence *= 0.85
        flags.append("factor de clearance com amostra pequena")

    # the range: the spread of the contributing values
    prices = [v for v, _w, _s in family_values.values()]
    spread = (max(prices) - min(prices)) / price if price else 0.0
    low = int(round(price * (1 - min(0.5, spread / 2 + 0.05))))
    high = int(round(price * (1 + min(0.5, spread / 2 + 0.05))))
    explanation = _explain(contributions, price, confidence, basis)
    return ValueEstimate(price, low, high, confidence, basis, tuple(contributions),
                          tuple(rejected), tuple(flags), explanation,
                          tuple(hypotheses))


def _explain(contributions: Sequence[dict], price: int, confidence: float,
             basis: str) -> str:
    parts = []
    for contribution in sorted(contributions, key=lambda c: -c["weight"]):
        parts.append(f"{contribution['family']} ({contribution['value'] / COPPER_PER_GOLD:,.0f} g, "
                     f"peso {contribution['weight']:.2f})")
    return (f"base: {basis}; {'; '.join(parts)} -> {price / COPPER_PER_GOLD:,.0f} g "
            f"(confiança {confidence:.0%})")


# --- the liquidity model ---------------------------------------------------

LIQUIDITY_HIGH = "alta"
LIQUIDITY_MEDIUM = "média"
LIQUIDITY_LOW = "baixa"
LIQUIDITY_UNKNOWN = "desconhecida"


@dataclass
class LiquidityAssessment:
    """How liquid the item is.  Never multiplied into the price."""

    level: str = LIQUIDITY_UNKNOWN
    demand: str = "desconhecida"
    velocity: Optional[float] = None       # regional sold per day (a regional signal)
    competition: Optional[int] = None      # local listings
    dispersion: Optional[float] = None      # local ask vs the wall
    stock_coverage_days: Optional[float] = None   # regional indicator only
    observed_sell_through: Optional[float] = None  # the account's own observation
    flags: Tuple[str, ...] = ()
    explanation: str = ""

    def as_dict(self) -> dict:
        return {
            "liquidity": self.level,
            "demand": self.demand,
            "velocity_per_day": self.velocity,
            "local_listings": self.competition,
            "local_dispersion": self.dispersion,
            "stock_coverage_days": self.stock_coverage_days,
            "observed_sell_through": self.observed_sell_through,
            "flags": " | ".join(self.flags),
            "explanation": self.explanation,
        }


def assess_liquidity(evidence: Evidence,
                      config: EngineConfig | None = None) -> LiquidityAssessment:
    """The liquidity of an item, from the demand, the velocity and the competition.

    Nothing here touches the price.  The regional volume is reported as a
    *regional* indicator: it says the region moves units of this item, not
    that this realm will.
    """
    config = config or EngineConfig()
    flags: list[str] = []
    velocity = next((s.volume for s in evidence.sources
                      if s.volume is not None), None)
    demand = "desconhecida"
    if evidence.demand_known and evidence.want_rate is not None:
        demand = f"{evidence.want_rate:.1%} da amostra DfA ainda não tem o look"
    else:
        flags.append("procura não medida (a amostra DfA não cobre este item)")

    sell_through = evidence.own_sell_through
    if sell_through is None:
        flags.append("sem histórico de vendas próprio para este item")
    elif evidence.own_sales + evidence.own_expired < 3:
        flags.append(f"histórico próprio pequeno ({evidence.own_sales} vendas, "
                     f"{evidence.own_expired} expirações)")

    coverage = None
    if velocity and evidence.qty and velocity > 0:
        coverage = evidence.qty / velocity
        flags.append("cobertura de stock calculada com o volume **regional**: "
                     "indica a escala, não promete venda local")

    level = LIQUIDITY_UNKNOWN
    score = 0
    if evidence.demand_known and evidence.want_rate is not None:
        score += 2 if evidence.want_rate >= 0.05 else 1
    if sell_through is not None:
        score += 2 if sell_through >= 0.5 else 1
    if velocity is not None:
        score += 1 if velocity >= 0.1 else 0
    if evidence.listings is not None:
        score += 0 if evidence.listings >= 3 else -1
    if score >= 4:
        level = LIQUIDITY_HIGH
    elif score >= 2:
        level = LIQUIDITY_MEDIUM
    elif score >= 1:
        level = LIQUIDITY_LOW
    else:
        flags.append("sem sinais de liquidez suficientes")

    explanation = (f"procura: {demand}; "
                   f"velocidade regional: {velocity if velocity is not None else '—'} vendas/dia; "
                   f"concorrência local: {evidence.listings if evidence.listings is not None else '—'} "
                   f"anúncios; "
                   f"sell-through próprio: {sell_through if sell_through is not None else '—'}")
    return LiquidityAssessment(level, demand, velocity, evidence.listings,
                                next((s.dispersion for s in evidence.sources
                                      if s.dispersion is not None), None),
                                coverage, sell_through, tuple(flags), explanation)


# --- the listing recommendation -------------------------------------------

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass
class ListingRecommendation:
    """What to list at, and why."""

    price: Optional[int]
    low: Optional[int]
    high: Optional[int]
    reason: str
    gross_proceeds: Optional[int] = None
    net_proceeds: Optional[int] = None
    costs_assumed: bool = True
    flags: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "recommended_price": self.price,
            "recommended_low": self.low,
            "recommended_high": self.high,
            "reason": self.reason,
            "gross_proceeds": self.gross_proceeds,
            "net_proceeds": self.net_proceeds,
            "costs_assumed": self.costs_assumed,
            "flags": " | ".join(self.flags),
        }


def recommend_listing(value: ValueEstimate,
                      liquidity: LiquidityAssessment,
                      evidence: Evidence,
                      config: EngineConfig | None = None) -> ListingRecommendation:
    """The price to list at, derived from the value, the liquidity and the costs.

    The recommendation is **not** the estimate: it may be lower (a liquid item
    in a competitive market, where being the cheapest matters) and it is
    floored by the loss limit and by the vendor price.
    """
    config = config or EngineConfig()
    if not value.usable or value.price is None:
        return ListingRecommendation(None, None, None,
                                     INSUFFICIENT_EVIDENCE,
                                     flags=tuple(value.flags))

    price = value.price
    reasons = []
    if value.basis == "transacções":
        reasons.append("preço ancorado em transacções observadas")
    else:
        reasons.append("preço ancorado numa referência de announce (especulativo)")

    if value.confidence < config.min_confidence:
        # The estimate exists but the evidence behind it is thin.  In a
        # liquidation campaign the honest answer is not "do not post" but
        # "ask at the conservative end of the interval and say so": the
        # uncertainty is expressed in the price, not in a missing group.
        if value.low:
            price = value.low
        reasons.append(f"confiança baixa ({value.confidence:.0%}): pede-se o fundo "
                       f"do intervalo para forçar a venda")
    elif liquidity.level == LIQUIDITY_HIGH:
        reasons.append("liquidez alta: lista ao valor estimado")
    elif liquidity.level in (LIQUIDITY_MEDIUM, LIQUIDITY_LOW):
        discount = config.max_listing_discount / 2
        price = int(round(value.price * (1 - discount)))
        reasons.append(f"liquidez {liquidity.level}: {discount:.0%} abaixo do valor "
                       f"estimado para ganhar posição")
    else:
        reasons.append("liquidez desconhecida: lista ao valor estimado e reavalia")

    # the floor: the vendor price.  It is applied *after* the estimate clamp,
    # so that a vendor price above the estimate is not silently discarded.
    floor = evidence.vendor_sell or 0
    if floor and price < floor:
        price = floor
        reasons.append("preço elevado ao valor de venda ao fornecedor")
    if price > value.price and not floor:
        price = value.price

    deposit = int(round(price * config.deposit_pct))
    if evidence.vendor_sell is not None:
        deposit = min(deposit, int(evidence.vendor_sell * config.deposit_vendor_cap_pct))
    deposit = max(int(config.deposit_pct * 100 * COPPER_PER_GOLD), deposit) \
        if price > 0 else 0
    gross = price * evidence.qty
    net = int(round(gross * (1 - config.ah_fee_pct)))
    flags = ["taxa de AH e depósito assumidos, não verificados"]
    if not evidence.costs_verified:
        flags.append("custos não confirmados nesta base de dados")
    return ListingRecommendation(price, value.low, value.high,
                                 "; ".join(reasons), gross, net,
                                 costs_assumed=not evidence.costs_verified,
                                 flags=tuple(flags))
