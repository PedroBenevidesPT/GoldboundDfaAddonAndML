"""Should this transmog be posted, and at what price?

The question is **expected net gold**, not sale probability.  A listing at
half the estimated price sells more often and earns less; a listing at four
times it sells rarely and earns less.  The cheapest price is not the best
price, and the highest sale rate is not the highest profit.

So the strategy layer compares candidate prices by

    E[net] = p(sale at price) x (total x (1 - cut))
             - (1 - p(sale at price)) x deposit
             - E[relisting deposits] x expected posting cycles
             - opportunity cost of the tied-up gold, if one is supplied

and reports the uncertainty next to the answer.  Where the evidence is
thin, the recommendation is ``do_not_post`` with the reason, not a
confident number built on nothing.

The cost side comes from :mod:`woweconomy.analytics.ah_costs`, whose
constants were measured from this account's own completed sales rather
than assumed.  The probability side is supplied by the caller and is
labelled with its source, so a measured regional rate is never displayed
as if it were a validated item-specific model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from woweconomy.analytics.ah_costs import (AH_CUT, deposit_copper,
                                           net_proceeds, stack_total)

#: Candidate prices, as fractions of the estimate.  The band that maximises
#: expected net gold is the answer; there is no default.
PRICE_CANDIDATES: Tuple[float, ...] = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)

#: Below this expected net gold, posting is not worth the deposit.
MIN_EXPECTED_NET_GOLD: float = 0.0

#: How many posting cycles a slow item is expected to need before it sells.
#: A policy, not an estimate: it is what "patient listing" costs in deposits.
DEFAULT_CYCLES: int = 3

#: Below this measured chance over the horizon, the look is slow enough that
#: holding the price beats discounting it.  A policy, stated so it can be
#: argued with, not a fitted constant.
PATIENT_P_BELOW: float = 0.5

ACTION_POST = "post"
ACTION_POST_PATIENT = "post_patient"
ACTION_UNDERCUT = "undercut"
ACTION_MAINTAIN = "maintain"
ACTION_RELIST = "relist"
ACTION_HOLD = "hold"
ACTION_VENDOR = "vendor"
ACTION_DO_NOT_POST = "do_not_post"

ACTIONS = (ACTION_POST, ACTION_POST_PATIENT, ACTION_UNDERCUT,
           ACTION_MAINTAIN, ACTION_RELIST, ACTION_HOLD,
           ACTION_VENDOR, ACTION_DO_NOT_POST)

#: Where a sale probability came from.  The UI must show this verbatim.
P_SOURCE_MEASURED = "measured_rate"
P_SOURCE_MODEL = "sentinel_model"
P_SOURCE_NONE = "unavailable"


@dataclass(frozen=True)
class StrategyInput:
    """Everything the strategy needs about one item."""

    item_id: int
    name: str = ""
    estimate_unit_copper: Optional[float] = None
    confidence: str = "none"
    observations: int = 0
    quantity: int = 1
    #: measured regional turnover per day, already validated by the caller
    sold_per_day: Optional[float] = None
    #: optional, caller-supplied model probability at a given horizon
    p_sale: Optional[float] = None
    p_sale_source: str = P_SOURCE_NONE
    horizon_days: float = 30.0
    competition_unit_copper: Optional[float] = None
    competition_count: int = 0
    vendor_unit_copper: Optional[float] = None
    deposit_copper: Optional[float] = None
    expected_cycles: int = DEFAULT_CYCLES
    #: gold tied up, per day.  None means "not measurable", which is
    #: reported as such rather than assumed to be zero.
    opportunity_cost_per_day: Optional[float] = None

    @property
    def effective_p_sale(self) -> Optional[float]:
        if self.p_sale is not None and self.p_sale_source == P_SOURCE_MODEL:
            return _clamp(self.p_sale)
        if self.sold_per_day is not None and self.sold_per_day > 0:
            # exponential turnover over the horizon: the honest reading of a
            # measured rate, with no extra structure invented.
            return 1.0 - math.exp(-self.sold_per_day * self.horizon_days)
        return None

    @property
    def effective_p_source(self) -> str:
        """Where the probability actually came from.

        Derived, never taken on trust: a caller that supplies a measured
        turnover but leaves ``p_sale_source`` at its default must not have
        its number displayed as "unavailable".
        """
        if self.p_sale is not None and self.p_sale_source == P_SOURCE_MODEL:
            return P_SOURCE_MODEL
        if self.sold_per_day is not None and self.sold_per_day > 0:
            return P_SOURCE_MEASURED
        return P_SOURCE_NONE


@dataclass(frozen=True)
class Candidate:
    """One candidate price and what it is expected to earn.

    ``measured`` is the load-bearing field.  The sale probability was
    measured at the level the item has actually been selling at, so it
    describes *that* price and nothing else.  A candidate priced above
    that level has no measured probability, and its expected value is
    therefore ``None`` -- not a number produced by a curve nobody fitted.
    """

    price_unit_copper: float
    p_sale: Optional[float]
    expected_net_copper: Optional[float]
    deposit_copper: int
    gross_if_sold_copper: float
    vendor_copper: int
    relist_deposits_copper: int
    opportunity_cost_copper: float
    measured: bool = True
    #: half-width of a crude interval, from the evidence size
    uncertainty_copper: float = 0.0


@dataclass(frozen=True)
class PostingPlan:
    """The recommendation, with the reason and the evidence size."""

    item_id: int
    name: str
    action: str
    price_unit_copper: Optional[float]
    expected_net_copper: Optional[float]
    p_sale: Optional[float]
    p_sale_source: str
    horizon_days: float
    confidence: str
    observations: int
    candidates: Tuple[Candidate, ...] = ()
    vendor_net_copper: Optional[float] = None
    tied_up_gold: Optional[float] = None
    reason: str = ""
    flags: Tuple[str, ...] = ()

    @property
    def beats_vendor(self) -> Optional[bool]:
        if self.expected_net_copper is None or self.vendor_net_copper is None:
            return None
        return self.expected_net_copper > self.vendor_net_copper


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _measured_probability(p_base: Optional[float], price: float,
                          estimate: float) -> Optional[float]:
    """The sale probability, but only where it was actually measured.

    ``p_base`` describes the price the item has been selling at.  Applying
    it unchanged to a much higher ask would be an invented price-response
    curve -- exactly what the spec forbids.  So the probability is returned
    only for candidates at or below that level; above it the answer is
    ``None`` and the caller must report the upside as unmeasured.
    """
    if p_base is None or estimate <= 0:
        return None
    if price <= estimate:
        return _clamp(p_base)
    return None


def evaluate(inputs: StrategyInput) -> PostingPlan:
    """Choose the candidate price with the highest expected net gold."""
    flags: List[str] = []
    p_base = inputs.effective_p_sale
    if p_base is None:
        flags.append("no sale probability: neither a measured turnover nor a model")

    vendor_unit = inputs.vendor_unit_copper or 0
    deposit = inputs.deposit_copper
    if deposit is None:
        deposit = deposit_copper(vendor_unit, inputs.quantity)

    vendor_net = net_proceeds(stack_total(vendor_unit, inputs.quantity)) if vendor_unit else 0.0
    tied_up = (inputs.estimate_unit_copper * inputs.quantity / 10_000.0
               if inputs.estimate_unit_copper else None)

    # --- nothing to price with -----------------------------------------
    if inputs.estimate_unit_copper is None or inputs.estimate_unit_copper <= 0:
        if vendor_unit > 0:
            return PostingPlan(
                item_id=inputs.item_id, name=inputs.name,
                action=ACTION_VENDOR,
                price_unit_copper=None, expected_net_copper=None,
                p_sale=None, p_sale_source=inputs.effective_p_source,
                horizon_days=inputs.horizon_days, confidence="none",
                observations=inputs.observations, candidates=(),
                vendor_net_copper=vendor_net, tied_up_gold=tied_up,
                reason=("no price evidence at all; the only realised outcome "
                        "is the vendor price"),
                flags=tuple(flags))
        return PostingPlan(
            item_id=inputs.item_id, name=inputs.name,
            action=ACTION_DO_NOT_POST, price_unit_copper=None,
            expected_net_copper=None, p_sale=None,
            p_sale_source=inputs.effective_p_source,
            horizon_days=inputs.horizon_days, confidence="none",
            observations=inputs.observations, candidates=(),
            reason="no price evidence and no vendor price: nothing to decide with",
            flags=tuple(flags))

    # --- a price exists but no probability ------------------------------
    if p_base is None:
        return PostingPlan(
            item_id=inputs.item_id, name=inputs.name,
            action=ACTION_DO_NOT_POST, price_unit_copper=None,
            expected_net_copper=None, p_sale=None,
            p_sale_source=P_SOURCE_NONE,
            horizon_days=inputs.horizon_days, confidence="none",
            observations=inputs.observations, candidates=(),
            vendor_net_copper=vendor_net, tied_up_gold=tied_up,
            reason=("a price exists but no sale probability does; posting "
                    "without one is a guess"),
            flags=tuple(flags))

    # --- score every candidate price ------------------------------------
    cycles = max(1, int(inputs.expected_cycles))
    opportunity_rate = inputs.opportunity_cost_per_day
    candidates: List[Candidate] = []
    for fraction in PRICE_CANDIDATES:
        price = inputs.estimate_unit_copper * fraction
        p = _measured_probability(p_base, price, inputs.estimate_unit_copper)
        gross = net_proceeds(stack_total(price, inputs.quantity))
        # the deposit is lost on every cycle that does not sell
        relist = deposit * (cycles - 1)
        if p is None:
            # Above the measured level.  The gross is known, the chance is
            # not, so the expected value is not a number we can defend.
            candidates.append(Candidate(
                price_unit_copper=price, p_sale=None, expected_net_copper=None,
                deposit_copper=deposit, gross_if_sold_copper=gross,
                vendor_copper=vendor_net, relist_deposits_copper=relist,
                opportunity_cost_copper=0.0, measured=False,
                uncertainty_copper=0.0))
            continue
        expected = p * gross - (1.0 - p) * deposit - (1.0 - p) * relist
        hold_cost = 0.0
        if opportunity_rate is not None and tied_up:
            hold_cost = opportunity_rate * inputs.horizon_days * tied_up
            expected -= (1.0 - p) * hold_cost
        uncertainty = _uncertainty(expected, p, inputs.observations)
        candidates.append(Candidate(
            price_unit_copper=price, p_sale=p, expected_net_copper=expected,
            deposit_copper=deposit, gross_if_sold_copper=gross,
            vendor_copper=vendor_net, relist_deposits_copper=relist,
            opportunity_cost_copper=hold_cost, measured=True,
            uncertainty_copper=uncertainty))

    measured = [c for c in candidates
                if c.measured and c.expected_net_copper is not None]
    if not measured:
        return PostingPlan(
            item_id=inputs.item_id, name=inputs.name,
            action=ACTION_DO_NOT_POST, price_unit_copper=None,
            expected_net_copper=None, p_sale=None,
            p_sale_source=inputs.effective_p_source,
            horizon_days=inputs.horizon_days, confidence="none",
            observations=inputs.observations, candidates=tuple(candidates),
            vendor_net_copper=vendor_net, tied_up_gold=tied_up,
            reason="no candidate price could be scored",
            flags=tuple(flags))

    best = max(measured, key=lambda c: (c.expected_net_copper, -c.price_unit_copper))
    action, reason = _choose_action(best, inputs, vendor_net, flags)
    if any(not c.measured for c in candidates):
        flags.append(
            "prices above the measured level are listed but not scored: their "
            "sale probability has not been measured, so their expected value "
            "would be an extrapolation")

    confidence = _confidence(inputs)
    return PostingPlan(
        item_id=inputs.item_id, name=inputs.name, action=action,
        price_unit_copper=best.price_unit_copper,
        expected_net_copper=best.expected_net_copper,
        p_sale=best.p_sale, p_sale_source=inputs.effective_p_source,
        horizon_days=inputs.horizon_days, confidence=confidence,
        observations=inputs.observations, candidates=tuple(candidates),
        vendor_net_copper=vendor_net, tied_up_gold=tied_up,
        reason=reason, flags=tuple(flags))


def _choose_action(best: Candidate, inputs: StrategyInput,
                   vendor_net: float, flags: List[str]) -> Tuple[str, str]:
    if best.expected_net_copper <= MIN_EXPECTED_NET_GOLD:
        if vendor_net > 0:
            flags.append("posting loses to the vendor price at every candidate price")
            return (ACTION_VENDOR,
                    f"expected net {best.expected_net_copper / 10_000:.2f}g is "
                    f"below the vendor's {vendor_net / 10_000:.2f}g")
        flags.append("no candidate price has a positive expected net gold")
        return (ACTION_DO_NOT_POST,
                f"best expected net is {best.expected_net_copper / 10_000:.2f}g")

    p = best.p_sale or 0.0
    wall = inputs.competition_unit_copper if inputs.competition_count > 0 else None

    # At the wall: a slow rare look is held, not discounted.  Undercutting a
    # collectible by a few silver surrenders gold and buys almost no extra
    # chance, because the buyer is looking for the appearance, not the deal.
    if wall is not None and best.price_unit_copper >= wall * 0.98:
        if p < PATIENT_P_BELOW:
            flags.append("the recommendation sits at the live wall")
            return (ACTION_MAINTAIN,
                    f"at the wall with only a {p:.1%} chance over "
                    f"{inputs.horizon_days:.0f} days; undercutting would "
                    f"surrender gold without adding a sale")
        flags.append("the recommendation sits at the live wall")
        return (ACTION_UNDERCUT,
                f"at the wall with a {p:.1%} chance; being cheapest is what "
                f"converts a fast-moving look")

    if p < PATIENT_P_BELOW:
        return (ACTION_POST_PATIENT,
                f"a {p:.1%} chance over {inputs.horizon_days:.0f} days "
                f"justifies holding the price rather than discounting")
    return (ACTION_POST,
            f"expected net {best.expected_net_copper / 10_000:.2f}g at "
            f"{best.price_unit_copper / 10_000:.2f}g")


def _uncertainty(expected: float, p: float, observations: int) -> float:
    """A crude half-width from the evidence size.

    This is deliberately not a bootstrap: with fewer than a handful of
    observations a formal interval would be false precision.  The width
    scales down as the square root of the evidence and is capped at the
    whole estimate.
    """
    if observations <= 0:
        return abs(expected)
    scale = 1.0 / math.sqrt(max(1, observations))
    return min(abs(expected), abs(expected) * scale * 3.0)


def _confidence(inputs: StrategyInput) -> str:
    if inputs.observations <= 0:
        return "none"
    if inputs.observations < 3:
        return "low"
    if inputs.observations < 10:
        return "medium"
    return "high"
