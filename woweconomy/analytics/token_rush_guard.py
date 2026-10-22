"""Token Rush liquidation guard: minimum-price floors and posting decisions.

Why a guard at all
    The weighted reference lowers the *minimum* price of the items whose local
    ask was far above the regional level.  A lower minimum does **not** sell
    anything: it only changes whether TSM is allowed to post.  The exact
    semantics were read from the installed addon
    (``LibTSMSystem/Source/Operation/AuctioningOperation.lua`` and
    ``Core/Service/Auctioning/PostScan.lua``) and are reproduced here so the
    alternatives can be compared on the same footing:

    **Validation** (``ValidateForPosting``)
      * not enough items -> not posted;
      * ``maxExpires > 0`` and the expiries since the last sale exceed it ->
        ``NOT_POSTING.MAX_EXPIRES``: not posted (and the banking module moves
        the item to the bank);
      * a missing ``minPrice``/``maxPrice``/``normalPrice``/``undercut`` ->
        the operation is invalid, nothing is posted;
      * ``normalPrice < minPrice`` -> ``INVALID.NORMAL_BELOW_MIN``: **the
        operation is invalid and the item is never posted**;
      * ``maxPrice < minPrice`` -> ``INVALID.MAX_BELOW_MIN``: same.

    **Decision** (``MakePostDecision``)
      * no competitor -> post at ``normalPrice`` (floored at ``minPrice``);
      * a competitor with ``wall - undercut < minPrice`` and
        ``priceReset = "none"`` -> ``NOT_POSTING.BELOW_MIN``: **not posted**;
      * a competitor above ``maxPrice + undercut`` -> ``aboveMax = maxPrice``:
        post at the cap;
      * otherwise -> undercut: post at ``wall - undercut``.

    **Canceling** (``ValidateForCanceling``)
      * with ``cancelUndercut = false`` and ``cancelRepost = false`` (what the
        proposals use) -> ``WONT_CANCEL.DISABLED``: **TSM never cancels**, so a
        price change cannot cancel a live listing.

    **Reposting**
      * there is no automatic repost: the auction lasts ``duration`` hours and
        expires; the *next* post scan re-evaluates the item from scratch.  A
        lower minimum therefore does not "cause a repost" -- it can only let a
        *subsequent* scan post an item that the previous one refused.

    So the real effect of a lower minimum is: (1) more items become postable,
    which means more listing cycles and more deposits burned; (2) the no
    competition price (``normalPrice``) is floored at the minimum, so a floor
    raise can *lift* the asking price; and (3) an operation whose
    ``normalPrice`` falls below its ``minPrice`` stops posting altogether --
    which is the trap the guard has to avoid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

COPPER_PER_GOLD = 10_000

# --- the posting outcomes, named after the addon's reasons ----------------
POST_UNDERCUT = "posta undercut"
POST_NORMAL = "posta ao preço normal"
POST_MAX = "posta ao tecto (maxPrice)"
BLOCKED_BELOW_MIN = "bloqueado: parede abaixo do min"
INVALID_NORMAL_BELOW_MIN = "invalido: normal abaixo do min"
INVALID_MAX_BELOW_MIN = "invalido: max abaixo do min"
INVALID_MISSING = "invalido: fonte de preço em falta"
BLOCKED_MAX_EXPIRES = "bloqueado: expirações acima do maxExpires"


def posting_decision(wall: Optional[int],
                      undercut: int,
                      min_price: Optional[int],
                      normal_price: Optional[int],
                      max_price: Optional[int],
                      *,
                      has_competitor: bool = True,
                      num_expires: Optional[int] = None,
                      max_expires: Optional[int] = None) -> str:
    """What TSM would do with these prices, following the addon exactly."""
    if None in (min_price, normal_price, max_price) or undercut is None:
        return INVALID_MISSING
    if normal_price < min_price:
        return INVALID_NORMAL_BELOW_MIN
    if max_price < min_price:
        return INVALID_MAX_BELOW_MIN
    if max_expires and num_expires is not None and num_expires > max_expires:
        return BLOCKED_MAX_EXPIRES
    if not has_competitor:
        return POST_NORMAL
    if wall is None:
        # no listing at all: the same branch as "no competitor"
        return POST_NORMAL
    if wall - undercut < min_price:
        return BLOCKED_BELOW_MIN
    if wall - undercut > max_price:
        return POST_MAX
    return POST_UNDERCUT


def is_posted(outcome: str) -> bool:
    return outcome in (POST_UNDERCUT, POST_NORMAL, POST_MAX)


def is_blocked(outcome: str) -> bool:
    return outcome in (BLOCKED_BELOW_MIN, BLOCKED_MAX_EXPIRES)


# --- the alternatives ------------------------------------------------------

ALTERNATIVE_A = "A original"
ALTERNATIVE_B = "B ponderada"
ALTERNATIVE_C = "C ponderada com piso do min original"
ALTERNATIVE_D = "D ponderada com piso só na evidência fraca"
ALTERNATIVES = (ALTERNATIVE_A, ALTERNATIVE_B, ALTERNATIVE_C, ALTERNATIVE_D)


@dataclass(frozen=True)
class Alternative:
    """One policy for the minimum price of an item."""

    key: str
    min_price: int
    normal_price: int
    max_price: int
    note: str = ""


def guard_minimum(weighted_min: int,
                  original_min: int,
                  *,
                  weak_evidence: bool,
                  apply_floor: bool) -> int:
    """The minimum price under a guard policy.

    ``apply_floor`` decides whether the original minimum is a floor at all;
    ``weak_evidence`` decides whether the narrow floor (alternative D) applies
    to this item.
    """
    if not apply_floor:
        return weighted_min
    if weak_evidence:
        return max(weighted_min, original_min)
    return weighted_min


def apply_alternative(alternative: str,
                       weighted_min: int,
                       weighted_normal: int,
                       weighted_max: int,
                       original_min: int,
                       original_normal: int,
                       original_max: int,
                       *,
                       weak_evidence: bool) -> Alternative:
    """The three prices of one item under one alternative.

    The normal price is floored at the minimum: without it, a raised floor
    would push ``normalPrice`` below ``minPrice`` and the operation would
    become **invalid** (``INVALID.NORMAL_BELOW_MIN``), i.e. the item would stop
    being posted altogether.  That is the only place where the normal-price
    policy is touched, and it is forced by the addon's own validation.
    """
    if alternative == ALTERNATIVE_A:
        return Alternative(alternative, original_min, original_normal, original_max,
                           "proposta original, sem alterações")
    if alternative == ALTERNATIVE_B:
        return Alternative(alternative, weighted_min, weighted_normal, weighted_max,
                           "proposta ponderada, sem alterações")
    if alternative == ALTERNATIVE_C:
        minimum = max(weighted_min, original_min)
        normal = max(weighted_normal, minimum)
        return Alternative(alternative, minimum, normal, max(weighted_max, normal),
                           "piso do min original em todos os itens")
    if alternative == ALTERNATIVE_D:
        if weak_evidence:
            minimum = max(weighted_min, original_min)
            note = "piso aplicado: evidência fraca"
        else:
            minimum = weighted_min
            note = "piso não aplicado: evidência suficiente"
        normal = max(weighted_normal, minimum)
        return Alternative(alternative, minimum, normal, max(weighted_max, normal), note)
    raise ValueError(f"alternativa desconhecida: {alternative!r}")


# --- the Token Rush liquidation classification ----------------------------

LIQUIDATE = "liquidar"
HOLD = "manter preço"
NO_REPOST = "não repôr automaticamente"

#: the thresholds, all of them heuristics unless noted
DEMAND_THRESHOLD = 0.05        # share of the DfA sample that still wants the look
SELL_THROUGH_THRESHOLD = 0.50   # observed sell-through of the account
STALE_EXPIRATIONS = 2          # expiries without a sale
DIVERGENCE_SEVERE = 0.75        # |local/regional - 1|


@dataclass(frozen=True)
class LiquidationFacts:
    """The measured facts of one item, for the liquidation classification."""

    item_id: int
    name: str = ""
    realm: str = ""
    want_rate: Optional[float] = None      # DfA: share of the sample that wants it
    demand_known: bool = False
    listings: Optional[int] = None         # competing listings in the realm
    local_market: int = 0
    local_wall: int = 0
    dispersion: Optional[float] = None      # |market/wall - 1|
    divergence: Optional[float] = None      # local/regional - 1
    freshness_hours: float = 0.0
    sales: int = 0
    expired: int = 0
    attempts: int = 0
    qty: int = 1
    min_price: int = 0                      # the minimum under the chosen policy
    wall: int = 0
    undercut: int = 0
    normal_price: int = 0
    max_price: int = 0

    @property
    def sell_through(self) -> Optional[float]:
        if not self.attempts:
            return None
        return self.sales / self.attempts

    @property
    def demand_measured(self) -> bool:
        return self.demand_known and self.want_rate is not None

    @property
    def weak_evidence(self) -> bool:
        """The evidence for the reference change is weak.

        Two of the three conditions are measured (a shallow market, a severe
        divergence); the third (an unknown demand) is a *coverage* fact, not a
        demand fact.
        """
        shallow = self.listings is not None and self.listings <= 1
        severe = self.divergence is not None and abs(self.divergence) >= DIVERGENCE_SEVERE
        return bool(shallow or severe or not self.demand_known)


def classify_liquidation(facts: LiquidationFacts) -> Tuple[str, Tuple[str, ...]]:
    """Classify one item for the Token Rush, with the reasons.

    * **não repôr automaticamente** -- the evidence says another cycle would
      not help: the item expired repeatedly without selling, or the demand is
      unmeasured *and* the price disagreement is severe, or the floor under the
      chosen policy is above the current wall (the item cannot be listed
      without going below it).
    * **liquidar** -- the demand is measured (or proven by the account's own
      sales) and the price evidence is not contradictory.
    * **manter preço** -- everything else: not enough evidence to discount.
    """
    reasons: List[str] = []

    if facts.sales == 0 and facts.expired >= STALE_EXPIRATIONS:
        reasons.append(f"{facts.expired} expirações sem venda: repetir o ciclo não "
                       f"produz nada")
        return NO_REPOST, tuple(reasons)

    if facts.min_price and facts.wall and facts.wall < facts.min_price:
        reasons.append(f"a parede actual ({facts.wall / COPPER_PER_GOLD:,.0f} g) está "
                       f"abaixo do piso ({facts.min_price / COPPER_PER_GOLD:,.0f} g): "
                       f"publicar abaixo do piso não é aceitável")
        return NO_REPOST, tuple(reasons)

    if not facts.demand_measured and facts.divergence is not None \
            and abs(facts.divergence) >= DIVERGENCE_SEVERE:
        reasons.append(f"procura não medida e divergência local/regional de "
                       f"{facts.divergence:+.0%}: não há base para escolher um lado")
        return NO_REPOST, tuple(reasons)

    demand_known = facts.demand_measured
    rate = facts.sell_through
    if demand_known and facts.want_rate is not None:
        reasons.append(f"procura medida: {facts.want_rate:.0%} da amostra DfA ainda "
                       f"não tem o look")
    if rate is not None and facts.sales:
        reasons.append(f"a conta vendeu {facts.sales} de {facts.attempts} publicações "
                       f"({rate:.0%})")
    if facts.listings is not None:
        reasons.append(f"{facts.listings} anúncio(s) concorrente(s) no reino")
    if facts.freshness_hours:
        reasons.append(f"observação local com {facts.freshness_hours:.0f} h")
    if facts.dispersion is not None:
        reasons.append(f"ask local {facts.dispersion:+.0%} face à parede")

    if (demand_known and facts.want_rate is not None
            and facts.want_rate >= DEMAND_THRESHOLD) or (rate is not None and rate >= SELL_THROUGH_THRESHOLD):
        return LIQUIDATE, tuple(reasons)
    return HOLD, tuple(reasons)
