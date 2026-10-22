"""Token Rush: a deadline-driven liquidation engine for the auction house.

What this module is
    The mog pricing model (``mog_operations``) answers "what is this item
    worth over months".  A token campaign asks a different question: *given 12
    days and a gold deficit, what do I sell, at what price, in what order, and
    what do I accept as a loss?*  This module answers that, and only that.  It
    never touches the normal pricing coefficients.

The four independent layers
    1. **Item classification** (A/B/C/D/unknown) -- from *observed* evidence:
       the account's own sales and expirations (``journal_stats``), the
       regional reference quality, and whether the price is supported by
       anything but an asking price.
    2. **Realm posture** -- the local market structure of one realm: the local
       price level relative to the region, the listing pressure, and how much
       of it is actually measured.  Realm and item are separate dimensions: a
       quiet realm does not make a stale item liquid, and a full realm does not
       make a wanted item stale.
    3. **Rush pricing** -- a *temporary* modifier on the reference price, built
       from three non-overlapping inputs (item evidence, realm competition,
       calendar) and bounded by a floor and a maximum acceptable loss.
    4. **Prioritisation** -- the queue, ordered by contribution to the gold
       deficit per hour of management, with checkpoints for days 3/6/9/12.

What is observed and what is assumed
    Observed: the account's realized sales and expirations, the local market
    value and cheapest local listing per realm, the regional reference, the
    vendor sell price.
    Assumed (configurable, flagged in the report): the AH fee, the deposit
    rule, the discount bands per competition level, the loss limit and the
    heuristic weights.  No sale probability is invented: an expected value is
    produced only when the account's own history gives a sell-through for that
    item; otherwise the row says *heuristic* and the ranking uses a transparent
    evidence weight, not a probability.
"""
from __future__ import annotations

import csv
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

COPPER_PER_GOLD = 10_000

# --- item profiles --------------------------------------------------------
PROFILE_LIQUID = "A"        # realized sales, keep selling
PROFILE_PROVEN = "B"        # demand exists but weakly / estimated
PROFILE_SPECULATIVE = "C"   # no sales evidence, price may be inflated
PROFILE_STALE = "D"         # relisted repeatedly, never sold
PROFILE_UNKNOWN = "U"       # not enough evidence to say anything

PROFILE_LABEL = {
    PROFILE_LIQUID: "líquido",
    PROFILE_PROVEN: "procura provada / pouca concorrência",
    PROFILE_SPECULATIVE: "especulativo",
    PROFILE_STALE: "estoque parado",
    PROFILE_UNKNOWN: "desconhecido",
}

# Heuristic weight used ONLY to order the queue when no sell-through can be
# observed.  It is not a probability and is never presented as one.
EVIDENCE_WEIGHT = {
    PROFILE_LIQUID: 1.00,
    PROFILE_PROVEN: 0.70,
    PROFILE_SPECULATIVE: 0.35,
    PROFILE_STALE: 0.30,
    PROFILE_UNKNOWN: 0.20,
}

# Multiplier applied to the price, per profile.  The item dimension carries
# demand evidence; the realm dimension never enters here.
ITEM_PRICE_FACTOR = {
    PROFILE_LIQUID: 1.00,
    PROFILE_PROVEN: 1.00,
    PROFILE_SPECULATIVE: 0.97,
    PROFILE_STALE: 0.92,
    PROFILE_UNKNOWN: 1.00,
}

# --- competition levels ---------------------------------------------------
COMP_HIGH = "alta"
COMP_MID = "media"
COMP_LOW = "baixa"
COMP_UNKNOWN = "desconhecida"

# Initial test parameters for the undercut against a valid comparable listing.
# Configurable; they are hypotheses to validate, not rules.
UNDERCUT_BANDS = {
    COMP_HIGH: (0.15, 0.25),
    COMP_MID: (0.10, 0.20),
    COMP_LOW: (0.05, 0.15),
    COMP_UNKNOWN: (0.10, 0.15),
}

COMP_LABEL = {
    COMP_HIGH: "concorrência alta",
    COMP_MID: "concorrência média",
    COMP_LOW: "concorrência baixa",
    COMP_UNKNOWN: "concorrência desconhecida",
}


@dataclass(frozen=True)
class CampaignConfig:
    """Everything the campaign is allowed to assume.  All of it is tunable."""

    # calendar and target
    days: int = 12
    hours_per_day: float = 8.0
    current_gold: int = 213_246 * COPPER_PER_GOLD
    token_price: int = 398_720 * COPPER_PER_GOLD
    token_price_safe: int = 420_000 * COPPER_PER_GOLD
    target_gold: int = 200_000 * COPPER_PER_GOLD

    # limits
    min_price_copper: int = 1     # the floor when the vendor sell price is unknown
    max_loss_pct: float = 0.35           # vs a known cost basis, when there is one
    max_discount_pct: float = 0.50       # never discount more than this vs the reference
    comparable_min_ratio: float = 0.30   # a listing below 30% of the reference is not comparable
    comparable_max_ratio: float = 3.00   # ... nor above 300%

    # auction house costs (assumed, flagged in the report)
    ah_fee_pct: float = 0.05             # cut taken from the sale price
    deposit_pct: float = 0.05            # deposit = 5% of the sale price ...
    deposit_min_gold: int = 1            # ... at least 1 g ...
    deposit_vendor_cap_pct: float = 3.00  # ... and at most 300% of the vendor sell price
    costs_verified: bool = False         # the report says so when this is False

    # classification thresholds
    liquid_sell_through: float = 0.50   # observed sell-through that counts as liquid
    stale_min_expirations: int = 2      # expirations without a sale = stale
    credible_sold_per_day: float = 0.30  # regional estimate that counts as credible demand
    min_attempts_for_rate: int = 3      # attempts needed before a rate is quoted

    # deadline
    deadline_price_factor: float = 0.90  # multiplier on the price at the deadline
    liquidation_discount: float = 0.25  # discount vs the reference when there is no comparable

    # management effort
    hours_per_item: float = 0.25
    hours_per_competitive_item: float = 0.50
    # a realm with no account activity cannot be posted to: it is evaluated,
    # but it does not compete for the management budget
    inactive_realm_weight: float = 0.25

    def token_price_used(self) -> int:
        """The price the campaign budgets against (the safe one)."""
        return max(self.token_price, self.token_price_safe) if self.token_price_safe else self.token_price


def gold(copper: int | float) -> float:
    return float(copper) / COPPER_PER_GOLD


# =========================================================================
# 1. Item classification
# =========================================================================

@dataclass(frozen=True)
class ItemEvidence:
    """Everything observed about one item in one realm.  Missing means missing."""

    item_id: int
    name: str = ""
    qty: int = 1
    realm: str = ""
    quality: str = ""

    # the account's own history (observed)
    sales: int = 0
    expired: int = 0
    attempts: int = 0
    sold_copper: int = 0
    last_sale_copper: int = 0

    # regional reference
    region_market: int = 0
    region_historical: int = 0
    region_avg_sale: int = 0
    region_sold_per_day: float = 0.0
    region_sale_rate: float = 0.0

    # local state
    realm_market: int = 0
    realm_min_buyout: int = 0
    realm_recent: int = 0
    realm_historical: int = 0
    listings: int | None = None          # num_auctions, only for the scanned realms
    is_commodity: bool = False
    vendor_sell: int | None = None

    @property
    def sell_through(self) -> float | None:
        if not self.attempts:
            return None
        return self.sales / self.attempts

    @property
    def has_reference(self) -> bool:
        return bool(self.region_market or self.realm_market)


@dataclass(frozen=True)
class ItemClassification:
    profile: str
    reasons: tuple[str, ...]
    confidence: str            # "observada" | "estimada" | "nenhuma"
    coverage: float           # 0..1, share of the evidence fields that exist
    sell_through: float | None

    @property
    def label(self) -> str:
        return PROFILE_LABEL[self.profile]


def classify_item(evidence: ItemEvidence, config: CampaignConfig) -> ItemClassification:
    """Classify one item from evidence.  Never from the price alone.

    A high market value is not demand.  A listing that disappeared is not a
    sale.  Missing data stays missing: without a single observation the item
    is ``unknown`` (U) unless the region itself reports demand, and even then
    the confidence is ``estimada``, not ``observada``.
    """
    ev = evidence
    reasons: list[str] = []
    fields_present = 0
    fields_total = 6

    if ev.sales or ev.expired:
        fields_present += 1
    if ev.attempts:
        fields_present += 1
    if ev.region_market:
        fields_present += 1
    if ev.realm_market or ev.realm_min_buyout:
        fields_present += 1
    if ev.listings is not None:
        fields_present += 1
    if ev.vendor_sell is not None:
        fields_present += 1
    coverage = fields_present / fields_total

    rate = ev.sell_through
    if ev.sales > 0:
        if ev.expired == 0:
            profile = PROFILE_LIQUID
            reasons.append(f"{ev.sales} venda(s) realizada(s) sem expirações")
        elif rate is not None and rate >= config.liquid_sell_through:
            profile = PROFILE_LIQUID
            reasons.append(f"{ev.sales} venda(s) em {ev.attempts} publicação(ões) "
                           f"(sell-through observado {rate:.0%})")
        else:
            profile = PROFILE_PROVEN
            reasons.append(f"{ev.sales} venda(s) mas {ev.expired} expirações "
                           f"(sell-through observado {rate:.0%}): procura existe, preço não")
        if ev.sold_copper:
            reasons.append(f"preço médio realizado {gold(ev.sold_copper / max(1, ev.sales)):,.0f} g")
        confidence = "observada"
    elif ev.expired >= config.stale_min_expirations:
        profile = PROFILE_STALE
        reasons.append(f"{ev.expired} expirações sem nenhuma venda neste item")
        reasons.append("anúncio desaparecido não é venda: só contam as vendas registadas")
        confidence = "observada"
    elif ev.region_sold_per_day >= config.credible_sold_per_day and ev.has_reference:
        profile = PROFILE_PROVEN
        reasons.append(f"sem vendas próprias; a região estima "
                       f"{ev.region_sold_per_day:.2f} vendas/dia (estimativa TSM, não observação)")
        confidence = "estimada"
    elif ev.has_reference:
        profile = PROFILE_SPECULATIVE
        reasons.append("nenhuma venda observada; a procura não está demonstrada")
        reasons.append(f"referência regional {gold(ev.region_market):,.0f} g é um *ask*, não uma venda")
        confidence = "estimada" if ev.region_sold_per_day > 0 else "nenhuma"
    else:
        profile = PROFILE_UNKNOWN
        reasons.append("sem preço de referência e sem histórico: nada a concluir")
        confidence = "nenhuma"

    if ev.listings is not None:
        reasons.append(f"{ev.listings} anúncio(s) ativo(s) no reino")
    elif ev.realm_min_buyout:
        reasons.append("há pelo menos um anúncio local (contagem indisponível)")

    return ItemClassification(profile, tuple(reasons), confidence, coverage, rate)


# =========================================================================
# 2. Realm posture
# =========================================================================

@dataclass(frozen=True)
class RealmPosture:
    """How a realm's market is structured, and how much of that is measured."""

    realm: str
    canonical: str = ""
    klass: str = "balanced"
    factor: float = 1.0          # local market / regional market (measured)
    competition: str = COMP_UNKNOWN
    listings_median: float | None = None
    has_listing_counts: bool = False
    confidence: str = "partilhada"
    warnings: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return COMP_LABEL[self.competition]


@dataclass(frozen=True)
class RealmEvidence:
    realm: str
    canonical: str = ""
    klass: str = "balanced"
    factor: float = 1.0
    listings_median: float | None = None
    has_listing_counts: bool = False
    confidence: str = "partilhada"
    duplicate_of: tuple[str, ...] = ()   # realms that share the same auction house


def build_realm_posture(evidence: RealmEvidence) -> RealmPosture:
    """Turn measured realm evidence into a posture, with its warnings."""
    warnings: list[str] = []
    depth = evidence.listings_median
    if depth is None:
        competition = COMP_UNKNOWN
        warnings.append("contagem de anúncios por item indisponível neste reino: "
                        "a concorrência é inferida, não observada")
    elif depth >= 3:
        competition = COMP_HIGH
    elif depth >= 1.5:
        competition = COMP_MID
    else:
        competition = COMP_LOW

    if evidence.duplicate_of:
        others = ", ".join(evidence.duplicate_of)
        warnings.append(f"métricas idênticas a {others}: é o mesmo mercado (casa de "
                        "leilão partilhada), não mercados independentes")

    if evidence.klass == "thin" and competition == COMP_LOW:
        warnings.append("pouca concorrência observada: NÃO é prova de procura; "
                        "exigir evidência de venda antes de pedir mais")
    if evidence.klass == "deep" and competition == COMP_HIGH:
        warnings.append("mercado cheio: reagir depressa, mas entrar numa guerra "
                        "de preços só se a venda melhorar o resultado")

    if evidence.confidence == "inferida":
        warnings.append("classe do reino inferida do nível de preço: a contagem de "
                        "anúncios não foi observada neste reino")

    return RealmPosture(
        realm=evidence.realm,
        canonical=evidence.canonical or evidence.realm,
        klass=evidence.klass,
        factor=evidence.factor,
        competition=competition,
        listings_median=depth,
        has_listing_counts=evidence.has_listing_counts,
        confidence=evidence.confidence,
        warnings=tuple(warnings),
    )


# =========================================================================
# 3. Rush pricing
# =========================================================================

@dataclass(frozen=True)
class RushPrice:
    item_id: int
    realm: str
    profile: str
    reference: int
    reference_source: str
    comparable: int | None
    comparable_valid: bool
    undercut_pct: float
    base_price: int
    item_factor: float
    deadline_factor: float
    recommended: int
    min_price: int
    max_price: int
    net_if_sold: int
    deposit: int | None
    cost_basis: int | None
    flags: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()

    @property
    def loss_vs_cost(self) -> float | None:
        if not self.cost_basis:
            return None
        return 1 - self.recommended / self.cost_basis


def deposit_for(price: int, vendor_sell: int | None, config: CampaignConfig) -> int | None:
    """Auction deposit for a listing, or None when the vendor price is unknown.

    The rule (retail): the deposit is 5% of the sale price, at least 1 g, and at
    most 300% of the item's vendor sell price.  When the vendor sell price is
    not in the database the cap cannot be applied, so the cost is reported as
    unavailable instead of being guessed.
    """
    if vendor_sell is None:
        return None
    raw = price * config.deposit_pct
    cap = vendor_sell * config.deposit_vendor_cap_pct
    return int(max(config.deposit_min_gold * COPPER_PER_GOLD, min(raw, cap)))


def _competition_for(posture: RealmPosture, evidence: ItemEvidence) -> str:
    """Item-level competition, refined by the realm posture."""
    if evidence.listings is not None:
        if evidence.listings >= 3:
            return COMP_HIGH
        if evidence.listings >= 1.5:
            return COMP_MID
        return COMP_LOW
    if evidence.realm_min_buyout:
        # one listing is known; more may exist
        return COMP_UNKNOWN if posture.competition == COMP_UNKNOWN else posture.competition
    return COMP_UNKNOWN


def rush_price(
    evidence: ItemEvidence,
    classification: ItemClassification,
    posture: RealmPosture,
    config: CampaignConfig,
    *,
    days_remaining: int | None = None,
    cost_basis: int | None = None,
) -> RushPrice:
    """The temporary liquidation price.  Never replaces the normal pricing.

    The composition is deliberately free of double counting:

    * the **reference** is the local level (measured) or the regional level
      corrected by the realm factor -- the realm's contribution to the *level*;
    * the **competition band** decides the undercut against a valid comparable
      -- the realm's contribution to the *discount*;
    * the **item profile** decides how much of that discount is given up --
      the item's contribution;
    * the **deadline factor** decides how much more is given away as the
      calendar runs out -- the calendar's contribution.
    """
    flags: list[str] = []
    adjustments: list[str] = []

    # --- 1. reference (the local level) --------------------------------
    if evidence.realm_market:
        reference = evidence.realm_market
        reference_source = "mercado local (medido)"
    elif evidence.region_market:
        reference = int(evidence.region_market * posture.factor)
        reference_source = f"mercado regional x factor do reino ({posture.factor:.2f})"
        flags.append("sem preço local neste reino: a referência é a regional corrigida")
    elif evidence.region_historical:
        reference = int(evidence.region_historical * posture.factor)
        reference_source = "histórico regional x factor do reino"
        flags.append("referência fraca: só há histórico regional")
    else:
        reference = 0
        reference_source = "indisponível"
        flags.append("sem referência de preço: nenhuma preço pode ser recomendado")

    # --- 2. comparable listing -----------------------------------------
    comparable = evidence.realm_min_buyout or None
    comparable_valid = False
    if comparable and reference:
        ratio = comparable / reference
        if config.comparable_min_ratio <= ratio <= config.comparable_max_ratio:
            comparable_valid = True
        else:
            flags.append(f"anúncio mais barato ({gold(comparable):,.0f} g) não é um "
                         f"comparável válido ({ratio:.0%} da referência): ignorado")

    competition = _competition_for(posture, evidence)
    low, high = UNDERCUT_BANDS[competition]
    undercut = (low + high) / 2

    # --- 3. base price --------------------------------------------------
    if comparable_valid:
        base = int(comparable * (1 - undercut))
        adjustments.append(f"comparável local {gold(comparable):,.0f} g "
                           f"- undercut {undercut:.0%} ({COMP_LABEL[competition]})")
    elif reference:
        base = int(reference * (1 - config.liquidation_discount))
        adjustments.append(f"referência {gold(reference):,.0f} g "
                           f"- {config.liquidation_discount:.0%} (sem comparável válido)")
        undercut = config.liquidation_discount
    else:
        base = 0
        adjustments.append("sem base: nenhum preço")

    # --- 4. item factor and deadline factor ----------------------------
    item_factor = ITEM_PRICE_FACTOR[classification.profile]
    if item_factor != 1.0:
        adjustments.append(f"perfil {classification.profile} x{item_factor:.2f}")
    deadline_factor = config.deadline_price_factor
    if days_remaining is not None and config.days > 0:
        progress = 1 - min(1.0, max(0.0, days_remaining / config.days))
        deadline_factor = 1 - (1 - config.deadline_price_factor) * progress
        adjustments.append(f"prazo ({days_remaining}d restantes) x{deadline_factor:.2f}")

    recommended = int(base * item_factor * deadline_factor)

    # --- 5. floors and caps --------------------------------------------
    # The floor is the configured minimum *or* the vendor sell price (you would
    # rather vendor the item than post it below what the vendor pays), and it
    # is never allowed above the reference: a floor the market cannot reach
    # means the item is not for sale, not a higher price.
    floor = config.min_price_copper
    if evidence.vendor_sell:
        floor = max(floor, evidence.vendor_sell)
    if cost_basis:
        loss_floor = int(cost_basis * (1 - config.max_loss_pct))
        if loss_floor > floor:
            floor = loss_floor
            adjustments.append(f"piso de perda ({config.max_loss_pct:.0%} de {gold(cost_basis):,.0f} g)")

    max_price = reference or recommended
    if reference and floor > reference:
        flags.append("o piso (perda limitada / valor de venda ao fornecedor) está "
                     "acima da referência local: não vale a pena publicar")
        recommended = 0
    if recommended > max_price:
        adjustments.append("limitado à referência local")
        recommended = max_price
    if recommended and recommended < floor:
        recommended = floor
    if reference and recommended and recommended < reference * (1 - config.max_discount_pct):
        recommended = int(reference * (1 - config.max_discount_pct))
        flags.append(f"desconto limitado a {config.max_discount_pct:.0%} da referência")

    net = int(recommended * (1 - config.ah_fee_pct))
    dep = deposit_for(recommended, evidence.vendor_sell, config)
    if dep is None:
        flags.append("preço de venda ao fornecedor desconhecido: depósito não calculado")

    return RushPrice(
        item_id=evidence.item_id,
        realm=evidence.realm,
        profile=classification.profile,
        reference=reference,
        reference_source=reference_source,
        comparable=comparable,
        comparable_valid=comparable_valid,
        undercut_pct=undercut,
        base_price=base,
        item_factor=item_factor,
        deadline_factor=deadline_factor,
        recommended=recommended,
        min_price=floor,
        max_price=max_price,
        net_if_sold=net,
        deposit=dep,
        cost_basis=cost_basis,
        flags=tuple(flags),
        adjustments=tuple(adjustments),
    )


# =========================================================================
# 4. Prioritisation
# =========================================================================

@dataclass
class RushRow:
    """One line of the queue: an item in a realm, with everything that led there."""

    item_id: int
    name: str
    realm: str
    qty: int
    evidence: ItemEvidence
    classification: ItemClassification
    posture: RealmPosture
    price: RushPrice
    hours: float = 0.25
    priority: float = 0.0
    expected: float | None = None       # observed sell-through based estimate (gold)
    expected_basis: str = "sem estimativa"
    action: str = ""
    warnings: tuple[str, ...] = ()
    realm_weight: float = 1.0            # 1.0 active realm, lower otherwise
    aliases: tuple[str, ...] = ()        # connected realms covered by this line

    @property
    def potential_gold(self) -> float:
        return gold(self.price.recommended) * self.qty


def prioritise(
    rows: Iterable[RushRow],
    config: CampaignConfig,
    *,
    days_remaining: int | None = None,
) -> list[RushRow]:
    """Order the queue by contribution to the deficit per hour of management.

    The expected value is only produced when the account's own history gives a
    sell-through for that item; otherwise the row carries a *potential* and an
    evidence weight, and says so.
    """
    out = []
    for row in rows:
        weight = EVIDENCE_WEIGHT[row.classification.profile]
        if (row.evidence.attempts >= config.min_attempts_for_rate
                and row.evidence.sell_through is not None):
            rate = row.evidence.sell_through
            # gold, like ``potential_gold``: the two are compared and summed
            row.expected = gold(row.price.net_if_sold) * rate * row.qty
            row.expected_basis = f"observada ({rate:.0%} de {row.evidence.attempts} publicações)"
        else:
            row.expected = None
            row.expected_basis = "heurística (sem observação de venda)"
        hours = config.hours_per_item
        if row.price.comparable_valid:
            hours = config.hours_per_competitive_item
        row.hours = hours
        contribution = row.expected if row.expected is not None else row.potential_gold * weight
        row.priority = contribution / hours * row.realm_weight if hours else 0.0
        out.append(row)

    out.sort(key=lambda r: (-r.priority, r.item_id))
    for position, row in enumerate(out, 1):
        row.action = _action_for(row, position, days_remaining)
    return out


def _action_for(row: RushRow, position: int, days_remaining: int | None) -> str:
    if row.classification.profile == PROFILE_STALE:
        return ("repor abaixo do preço actual, até ao limite de perda; "
                "depois vender por destruction/vendor")
    if row.classification.profile == PROFILE_LIQUID:
        return "postar já no preço recomendado e repor se expirar"
    if row.classification.profile == PROFILE_PROVEN:
        return "postar no preço recomendado; vigiar se expira sem vender"
    if row.classification.profile == PROFILE_SPECULATIVE:
        return "postar com desconto inicial e reavaliar em 2 dias"
    if row.classification.profile == PROFILE_UNKNOWN:
        return "não postar: sem evidência; usar como materiais/vendor"
    return "avaliar"


# =========================================================================
# 5. Campaign arithmetic and checkpoints
# =========================================================================

@dataclass(frozen=True)
class CampaignState:
    gold_now: int
    token_price: int
    gold_needed: int          # shortfall to buy the token
    target_gold: int          # operational target: net gold the campaign must make
    deficit: int              # what the campaign has to net (the binding number)
    days_remaining: int
    hours_remaining: float
    realized_gold: int = 0
    projected: float = 0.0
    projected_basis: str = "heurística"

    @property
    def coverage(self) -> float:
        return self.projected / self.deficit if self.deficit else 0.0


def campaign_state(
    config: CampaignConfig,
    *,
    days_remaining: int | None = None,
    realized_gold: int = 0,
    projected: float = 0.0,
    projected_basis: str = "heurística",
) -> CampaignState:
    """Recompute the deficit from the *current* token price and current gold.

    Two numbers matter and they are not the same:

    * the **shortfall to the token** -- ``token - gold`` (the "initial gold
      deficit" of the campaign brief);
    * the **operational target** -- the net gold the campaign must produce.

    What the campaign has to net is the bigger of the two.  Both are reported
    so the arithmetic is never hidden.
    """
    token = config.token_price_used()
    gold_now = config.current_gold + realized_gold
    needed = max(0, token - gold_now)
    required = max(needed, config.target_gold)
    days = config.days if days_remaining is None else max(0, days_remaining)
    return CampaignState(
        gold_now=gold_now,
        token_price=token,
        gold_needed=needed,
        target_gold=config.target_gold,
        deficit=required,
        days_remaining=days,
        hours_remaining=days * config.hours_per_day,
        realized_gold=realized_gold,
        projected=projected,
        projected_basis=projected_basis,
    )


def checkpoints(config: CampaignConfig, days=(3, 6, 9, 12)) -> list[dict]:
    """The campaign checkpoints (day -> what has to be true by then)."""
    out = []
    for day in days:
        days_left = max(0, config.days - day)
        state = campaign_state(config, days_remaining=days_left)
        required_per_day = state.deficit / days_left if days_left else state.deficit
        out.append({
            "day": day,
            "days_remaining": days_left,
            "gold_now": state.gold_now,
            "token_price": state.token_price,
            "deficit": state.deficit,
            "required_per_day": required_per_day,
            "hours_per_day": config.hours_per_day,
            "action": (
                f"até ao dia {day} precisas de {gold(state.deficit):,.0f} g de mais "
                f"({gold(required_per_day):,.0f} g/dia) a {config.hours_per_day:.0f} h/dia"
            ),
        })
    return out


# =========================================================================
# 6. Feedback loop
# =========================================================================

@dataclass(frozen=True)
class FeedbackEntry:
    item_id: int
    name: str
    realm: str
    sales: int
    expired: int
    sold_copper: int

    @property
    def sell_through(self) -> float | None:
        attempts = self.sales + self.expired
        if not attempts:
            return None
        return self.sales / attempts


def feedback_from_journal(rows: Iterable[Mapping]) -> list[FeedbackEntry]:
    """Realized results, straight from the journal.  Listings that merely
    disappeared are *not* in this table, so they cannot be mistaken for sales."""
    out = []
    for row in rows:
        out.append(FeedbackEntry(
            item_id=int(row.get("itemId") or 0),
            name=row.get("name") or "",
            realm=row.get("realm") or "",
            sales=int(row.get("sold_count") or 0),
            expired=int(row.get("failures") or 0),
            sold_copper=int(row.get("sold_copper") or 0),
        ))
    return out


def observed_sell_through(entries: Iterable[FeedbackEntry]) -> dict[int, float]:
    """Observed sell-through per item, from the account's own history only."""
    agg: dict[int, list[int]] = {}
    for entry in entries:
        if not entry.sales and not entry.expired:
            continue
        bucket = agg.setdefault(entry.item_id, [0, 0])
        bucket[0] += entry.sales
        bucket[1] += entry.expired
    out = {}
    for item, (sales, expired) in agg.items():
        attempts = sales + expired
        if attempts:
            out[item] = sales / attempts
    return out
