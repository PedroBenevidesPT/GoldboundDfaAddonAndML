"""An evidence-weighted reference price for the TSM auctioning operations.

The problem
    ``first(dbmarket, dbregionmarketavg, dbregionhistorical)`` picks the local
    ask average whenever the realm has *any* price for the item, and the
    regional average otherwise.  Both halves of that are wrong in opposite
    directions:

    * the local ask average of a rare item rests on **one listing** (the
      measured median for the catalogue is 1, and for the >500k band only 13 %
      of the items have three or more) -- and one listing is not a market;
    * the regional average is the mean of 92 realms, including the ones whose
      listings are dead: for the inventory of this account the local/regional
      ratio reaches 4.1x at the top decile, so the regional can be four times
      the local.

The estimator
    A weighted mean of the two references, with the weight on the local
    determined by the **quality of the local evidence** (how many listings it
    rests on, how fresh it is, how consistent it is with the cheapest local
    listing), and the weight on the regional never allowed to dominate.

Why the geometric mean
    The walk-forward over the per-realm fetches (see
    ``scripts/reference_backtest.py``) says three things:

    * the local reference predicts the next local level far better than the
      regional one (median relative error 0.5 % against 122 %) -- so the local
      must keep the weight;
    * where a blend is needed, the geometric mean is the better blend
      (0.222 against 0.258 median error for the same weights) -- and it is the
      one that bounds the damage of an inflated regional reference, because a
      regional ten times the local multiplies the result by ten^0.5 instead of
      five-and-a-half;
    * the *level* is not the problem: the ask averages sit several times above
      the realised sale prices.  That is the band coefficient's job, not the
      reference's.

    The arithmetic mean is implemented too (``method="arithmetic"``) and is
    reported next to the geometric one, but it is not the default: it lets an
    inflated regional reference move the result proportionally to its size.

What is *not* done here
    * a divergence between the local and the regional never decides the price:
      it produces a diagnostic and lowers the confidence;
    * the cheapest local listing never defines the value -- it is only a
      consistency signal for the local reference, and it is what the TSM
      undercut uses at posting time (a different stage);
    * no sale probability is invented: the weights come from listing depth,
      freshness and dispersion, never from an assumed sell-through.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Sequence, Tuple

COPPER_PER_GOLD = 10_000

#: TSM's "no data" sentinel, in copper (9 999 999,99 g).  Anything at or
#: above it is a missing value, never a price.
SENTINEL_COPPER = 99_999_999_000

#: the local/regional ratio below and above which the two disagree enough to
#: need a human look (a diagnostic, not a price rule).
DIVERGENCE_FLAG = 0.75


def clean_price(value: int | float | None) -> int:
    """Zero for a missing price or for TSM's sentinel.

    A sentinel is not a price: 9 999 999,99 g is what TSM writes when it has
    no data, and treating it as a price would turn a recipe with no market
    into a ten million gold asset.
    """
    if not value:
        return 0
    value = int(value)
    if value >= SENTINEL_COPPER:
        return 0
    return value


@dataclass(frozen=True)
class LocalEvidence:
    """What the realm says about an item."""

    price: int = 0                 # dbmarket: the realm's ask average
    wall: int = 0                  # dbminbuyout: the cheapest local listing
    recent: int = 0                # dbrecent: the 3-day ask
    historical: int = 0            # dbhistorical: the 60-day ask
    listings: int | None = None    # the number of competing listings, if known
    age_hours: float = 0.0         # the age of the observation
    observed_at: str = ""

    @property
    def valid(self) -> bool:
        return 0 < self.price < SENTINEL_COPPER

    @property
    def dispersion(self) -> float | None:
        """How far the ask average sits from the cheapest local listing."""
        if not self.wall or not self.valid:
            return None
        return abs(self.price / self.wall - 1.0)


@dataclass(frozen=True)
class RegionalEvidence:
    """What the region says about an item."""

    price: int = 0                 # dbregionmarketavg
    historical: int = 0            # dbregionhistorical
    avg_sale: int = 0              # dbregionsaleavg
    age_hours: float = 0.0
    observed_at: str = ""

    @property
    def valid(self) -> bool:
        return 0 < self.price < SENTINEL_COPPER

    @property
    def historical_valid(self) -> bool:
        return 0 < self.historical < SENTINEL_COPPER


@dataclass(frozen=True)
class ReferenceConfig:
    """Everything the estimator is allowed to assume.  All of it is a parameter."""

    #: "geometric" (the default, see the module docstring) or "arithmetic"
    method: str = "geometric"
    #: the weight on the local is never below this: the regional reference is
    #: a demonstrably worse level predictor (median error 122 % against 0.5 %)
    weight_floor: float = 0.50
    weight_ceiling: float = 1.00
    #: listing count -> score.  The thresholds come from the measured depth
    #: distribution: the median is 1 listing and only 13-38 % of the items
    #: reach three.  The walk-forward shows the local reference still predicts
    #: the local level well with no wall at all (median error 0.2 %), so a thin
    #: local market lowers the weight, it does not void it.
    depth_scores: Tuple[Tuple[int, float], ...] = (
        (0, 0.55), (1, 0.65), (2, 0.80), (3, 0.90), (5, 1.00),
    )
    #: the dispersion (|ask average / cheapest listing - 1|) that still counts
    #: as a consistent local observation
    dispersion_full: float = 0.50
    dispersion_floor: float = 0.10
    #: the local observation older than this loses its freshness score
    stale_hours: float = 48.0
    #: the regional/local ratio outside this band raises a diagnostic
    divergence_flag: float = DIVERGENCE_FLAG
    sentinel: int = SENTINEL_COPPER

    def depth_score(self, listings: int | None) -> float:
        if listings is None:
            # the count is unknown: half trust, and say so
            return 0.5
        best = 0.0
        for threshold, score in self.depth_scores:
            if listings >= threshold:
                best = score
        return best

    def dispersion_score(self, dispersion: float | None) -> float:
        if dispersion is None:
            return 0.75      # no wall to compare with: partial trust
        if dispersion <= self.dispersion_full:
            return 1.0
        if dispersion >= self.dispersion_floor * 5:
            return 0.25
        # linear decay between "full" and "floor"
        span = self.dispersion_floor * 5 - self.dispersion_full
        if span <= 0:
            return 0.25
        return max(0.25, 1.0 - (dispersion - self.dispersion_full) / span * 0.75)

    def freshness_score(self, local: LocalEvidence, regional: RegionalEvidence) -> float:
        """How much fresher the local observation is than the regional one.

        The local fetch runs every 6-24 h and the regional one every 1-2
        days, so in practice the local is the fresher of the two and this
        score is 1.  It exists for the case where the realm stops being
        scanned: then the local observation ages and loses the weight.
        """
        if regional.age_hours <= 0:
            return 1.0
        local_age = max(local.age_hours, 0.25)   # 15 minutes: "fetched now"
        return min(1.0, regional.age_hours / local_age)


@dataclass(frozen=True)
class ReferenceEstimate:
    """The reference price and everything that produced it."""

    price: int
    weight_local: float
    weight_regional: float
    method: str
    source: str
    confidence: str
    local: int
    regional: int
    divergence: float | None = None
    flags: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "price": self.price,
            "weight_local": round(self.weight_local, 3),
            "weight_regional": round(self.weight_regional, 3),
            "method": self.method,
            "source": self.source,
            "confidence": self.confidence,
            "local": self.local,
            "regional": self.regional,
            "divergence": (round(self.divergence, 3)
                           if self.divergence is not None else None),
            "flags": " | ".join(self.flags),
        }


def _blend(local: int, regional: int, weight_local: float, method: str) -> int:
    if method == "arithmetic":
        return int(round(weight_local * local + (1 - weight_local) * regional))
    if method == "geometric":
        if local <= 0 or regional <= 0:
            return int(round(weight_local * local + (1 - weight_local) * regional))
        return int(round(local ** weight_local * regional ** (1 - weight_local)))
    raise ValueError(f"método desconhecido: {method!r}")


def estimate_reference(local: LocalEvidence,
                       regional: RegionalEvidence,
                       config: ReferenceConfig | None = None) -> ReferenceEstimate:
    """The reference price of one item in one realm.

    The ladder, in order:

    1. both references present -> the evidence-weighted blend;
    2. the local missing -> the regional;
    3. the regional missing -> the local;
    4. both missing -> the regional 60-day historical (low confidence);
    5. nothing -> no price at all (the operation does not post).
    """
    config = config or ReferenceConfig()
    flags: list[str] = []

    if local.price >= config.sentinel:
        flags.append("preço local é o sentinel da TSM: tratado como ausente")
    if regional.price >= config.sentinel:
        flags.append("preço regional é o sentinel da TSM: tratado como ausente")

    local_ok = local.valid
    regional_ok = regional.valid
    divergence = None
    if local_ok and regional_ok:
        divergence = local.price / regional.price - 1.0

    if not local_ok and not regional_ok:
        if regional.historical_valid:
            flags.append("sem preço local nem regional: usa o histórico regional "
                         "(confiança baixa)")
            return ReferenceEstimate(
                price=regional.historical, weight_local=0.0, weight_regional=0.0,
                method=config.method, source="regional_historical", confidence="baixa",
                local=local.price, regional=regional.price, divergence=None,
                flags=tuple(flags))
        flags.append("sem nenhum preço de referência")
        return ReferenceEstimate(
            price=0, weight_local=0.0, weight_regional=0.0, method=config.method,
            source="sem_preco", confidence="nenhuma", local=local.price,
            regional=regional.price, divergence=None, flags=tuple(flags))

    if not local_ok:
        flags.append("sem preço local: usa o regional")
        return ReferenceEstimate(
            price=regional.price, weight_local=0.0, weight_regional=1.0,
            method=config.method, source="regional", confidence="média",
            local=local.price, regional=regional.price, divergence=None,
            flags=tuple(flags))

    if not regional_ok:
        flags.append("sem preço regional: usa o local")
        return ReferenceEstimate(
            price=local.price, weight_local=1.0, weight_regional=0.0,
            method=config.method, source="local", confidence="média",
            local=local.price, regional=regional.price, divergence=None,
            flags=tuple(flags))

    # --- the weight on the local, from the quality of its evidence --------
    depth = config.depth_score(local.listings)
    dispersion = config.dispersion_score(local.dispersion)
    freshness = config.freshness_score(local, regional)
    raw = depth * dispersion * freshness
    weight = max(config.weight_floor, min(config.weight_ceiling, raw))
    if weight != raw:
        flags.append(f"peso limitado a {weight:.2f} (bruto {raw:.2f})")

    if local.listings is None:
        flags.append("contagem de anúncios desconhecida: profundidade assumida")
    elif local.listings <= 1:
        flags.append(f"preço local assenta em {local.listings} anúncio(s)")
    if local.age_hours > config.stale_hours:
        flags.append(f"observação local antiga ({local.age_hours:.0f} h)")
    if divergence is not None and abs(divergence) >= config.divergence_flag:
        # a diagnostic, never a price rule
        flags.append(f"divergência local/regional de {divergence:+.0%}: "
                     f"confiança reduzida, o preço não muda por isto")

    price = _blend(local.price, regional.price, weight, config.method)

    confidence = "alta"
    if weight < 0.75 or (divergence is not None and abs(divergence) >= config.divergence_flag):
        confidence = "média"
    if weight <= config.weight_floor + 1e-9 or local.listings in (0, None):
        confidence = "baixa"

    return ReferenceEstimate(
        price=price,
        weight_local=weight,
        weight_regional=1 - weight,
        method=config.method,
        source="ponderado(local, regional)",
        confidence=confidence,
        local=local.price,
        regional=regional.price,
        divergence=divergence,
        flags=tuple(flags),
    )


# --- the TSM expression ---------------------------------------------------

#: the sources the expression uses.  All of them are AuctionDB sources of the
#: installed TSM (see ``TradeSkillMaster.lua``: DBMarket, DBMinBuyout, DBRecent,
#: DBHistorical, DBRegionMarketAvg, DBRegionHistorical).
TSM_LOCAL_SOURCE = "dbmarket"
TSM_REGIONAL_SOURCE = "dbregionmarketavg"
TSM_REGIONAL_FALLBACK = "dbregionhistorical"


def evaluate_tsm_expression(expression: str, sources: Dict[str, float]) -> float:
    """Evaluate a price expression the way the installed TSM does.

    Only the subset the generator uses is supported: the numbers, the named
    sources, ``+ - * / ^``, the parentheses and the functions ``ifgt``,
    ``iflte``, ``min``, ``max``, ``first``, ``round``.  The precedence is the
    one TSM's own optimiser assumes (``^`` binds tighter than ``*``, which
    binds tighter than ``+``/``-``), so ``a ^ 0.6 * b ^ 0.4`` is the geometric
    mean and not ``a ^ (0.6 * b) ^ 0.4``.

    It exists so the generated expression can be *checked* instead of trusted:
    the tests compare it with the estimator's own arithmetic, and the report
    can print what the TSM will actually compute.
    """
    tokens = _tokenize(expression)
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take():
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def parse_expression():
        value = parse_term()
        while peek() in ("+", "-"):
            operator = take()
            right = parse_term()
            value = value + right if operator == "+" else value - right
        return value

    def parse_term():
        value = parse_power()
        while peek() in ("*", "/"):
            operator = take()
            right = parse_power()
            value = value * right if operator == "*" else value / right
        return value

    def parse_power():
        value = parse_atom()
        if peek() == "^":
            take()
            value = value ** parse_power()
        return value

    def parse_atom():
        token = take()
        if token == "(":
            value = parse_expression()
            assert take() == ")", "parênteses desemparelhados"
            return value
        if token in ("-", "+"):
            value = parse_atom()
            return -value if token == "-" else value
        if token[0].isdigit() or token[0] == ".":
            if token.endswith("g") and token[:-1].replace(".", "", 1).isdigit():
                # a TSM money token: "50000g" is 50 000 gold in copper
                return float(token[:-1]) * COPPER_PER_GOLD
            return float(token)
        if token in sources:
            return float(sources[token])
        if token in ("ifgt", "iflte", "min", "max", "first", "round"):
            args = []
            assert take() == "("
            args.append(parse_expression())
            while peek() == ",":
                take()
                args.append(parse_expression())
            assert take() == ")", f"função {token} mal fechada"
            if token == "ifgt":
                if len(args) != 4:
                    raise AssertionError("ifgt precisa de 4 argumentos "
                                         "(a, b, se-maior, se-menor)")
                return args[2] if args[0] > args[1] else args[3]
            if token == "iflte":
                if len(args) != 4:
                    raise AssertionError("iflte precisa de 4 argumentos")
                return args[2] if args[0] <= args[1] else args[3]
            if token == "min":
                return min(args)
            if token == "max":
                return max(args)
            if token == "first":
                for value in args:
                    if value:
                        return value
                return 0
            if token == "round":
                return round(args[0])
        raise AssertionError(f"token desconhecido: {token!r}")

    result = parse_expression()
    assert position == len(tokens), "sobrou lixo na expressão"
    return result


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        if char in "+-*/^(),":
            tokens.append(char)
            index += 1
            continue
        start = index
        while index < len(expression) and (expression[index].isalnum()
                                          or expression[index] in "._"):
            index += 1
        if index == start:
            raise AssertionError(f"carácter inesperado: {char!r}")
        tokens.append(expression[start:index])
    return tokens


def to_tsm_expression(weight_local: float,
                      config: ReferenceConfig | None = None) -> str:
    """The reference as a valid TSM price expression.

    The expression is explicit about the fallbacks, because TSM evaluates it
    per item at posting time:

    * a missing (or sentinel) local value falls back to the regional;
    * a missing regional falls back to the local;
    * both missing fall back to the regional 60-day historical;
    * the blend is the geometric mean when the method is ``geometric`` and the
      arithmetic mean otherwise.

    Nothing here needs a custom source: the weights are constants, decided by
    the DFA calculation and reviewed in the report.
    """
    config = config or ReferenceConfig()
    weight = max(0.0, min(1.0, weight_local))
    regional_weight = 1.0 - weight

    if config.method == "arithmetic":
        blend = f"{weight:.4f} * {TSM_LOCAL_SOURCE} + {regional_weight:.4f} * {TSM_REGIONAL_SOURCE}"
    elif config.method == "geometric":
        blend = (f"{TSM_LOCAL_SOURCE} ^ {weight:.4f} * "
                 f"{TSM_REGIONAL_SOURCE} ^ {regional_weight:.4f}")
    else:
        raise ValueError(f"método desconhecido: {config.method!r}")

    # the sentinel guard: a sentinel is not a price.  The comparison is
    # strictly greater, so the limit sits one copper below the sentinel.
    sentinel_limit = config.sentinel - 1
    local_guard = (f"ifgt({TSM_LOCAL_SOURCE}, {sentinel_limit}, 0, {TSM_LOCAL_SOURCE})")
    regional_guard = (f"ifgt({TSM_REGIONAL_SOURCE}, {sentinel_limit}, 0, "
                      f"{TSM_REGIONAL_SOURCE})")
    return (f"ifgt({local_guard}, 0, "
            f"ifgt({regional_guard}, 0, {blend}, {TSM_LOCAL_SOURCE}), "
            f"ifgt({regional_guard}, 0, {TSM_REGIONAL_SOURCE}, "
            f"{TSM_REGIONAL_FALLBACK}))")


def banded_reference_expression(weights: Dict[str, float],
                                config: ReferenceConfig | None = None) -> str:
    """A per-band weight table compiled into one nested ``ifgt`` chain.

    The band thresholds are in gold, the same way the project's normal export
    does it, and the value being compared is the local market value.
    """
    from .pricing_groups import BAND_EDGES_G, band_order

    config = config or ReferenceConfig()
    order = band_order()
    fallback = to_tsm_expression(weights.get(order[-1], 1.0), config)
    picks = []
    for index, edge in enumerate(BAND_EDGES_G):
        band = order[index]
        picks.append((edge, to_tsm_expression(weights.get(band, 1.0), config)))
    for edge, pick in reversed(picks):
        fallback = f"ifgt({TSM_LOCAL_SOURCE}, {edge}g, {pick}, {fallback})"
    return fallback
