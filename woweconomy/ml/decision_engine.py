"""DFA Auction House decision engine.

Consumes :class:`ItemEvaluation` objects and produces actionable recommendations.

Every recommendation includes:
- Item identity.
- Recommended action.
- Recommended posting price or price range when supported.
- Quantity to list when inventory and policy inputs support it.
- Expected net proceeds or profit where calculable.
- Estimated uncertainty.
- Evidence and reasons.
- Data freshness and confidence limitations.
- Conditions that would change the decision.
- A clear fallback when required evidence is missing.

Supported actions (matching the specification):
  POST, MATCH, UNDERCUT, MAINTAIN, CANCEL_AND_RELIST, EXPIRE,
  LIQUIDATE, DO_NOT_LIST, REVIEW_REQUIRED.

The engine is deliberately conservative: it never recommends an action when
the evidence is insufficient, and it always identifies the supporting evidence
and the assumptions on which the decision depends.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config import AH_CONSIGNMENT_RATE
from .market_eval import ItemEvaluation, _compute_net_proceeds
from .intervals import CalibratedInterval, MIN_CALIBRATION_ROWS


# ---------------------------------------------------------------------------
# Decision result
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """One recommendation produced by the decision engine."""

    item_id: int
    realm: str
    action: str = "REVIEW_REQUIRED"  # conservative default; set by decide() logic
    suggested_price: Optional[float] = None  # in copper, when supported
    suggested_quantity: Optional[int] = None  # when inventory + policy support it
    net_proceeds: Optional[float] = None  # in copper, after commission + deposit
    uncertainty: str = "none"  # "none", "interval", "insufficient"
    evidence: List[str] = field(default_factory=list)  # human-readable reasons
    limitations: List[str] = field(default_factory=list)  # what's missing / unknown
    conditions_for_change: List[str] = field(default_factory=list)  # what would flip the decision
    model_versions: dict = field(default_factory=dict)  # which models were used and their versions

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "realm": self.realm,
            "action": self.action,
            "suggested_price": self.suggested_price,
            "suggested_quantity": self.suggested_quantity,
            "net_proceeds": self.net_proceeds,
            "uncertainty": self.uncertainty,
            "evidence": self.evidence,
            "limitations": self.limitations,
            "conditions_for_change": self.conditions_for_change,
            "model_versions": self.model_versions,
        }


# ---------------------------------------------------------------------------
# Decision engine — the core policy
# ---------------------------------------------------------------------------

def decide(evaluation: ItemEvaluation) -> Decision:
    """Produce a :class:`Decision` from one :class:`ItemEvaluation`.

    The policy is conservative and evidence-driven:

    1. If we have insufficient evidence → REVIEW_REQUIRED.
    2. If the reference is not verified → REVIEW_REQUIRED.
    3. If we have a verified price and credible competitor data → decide
       between MAINTAIN, UNDERCUT, or POST based on gap, supply, and economics.
    4. Otherwise → DO_NOT_LIST or REVIEW_REQUIRED.

    All reasoning is captured in ``evidence`` and ``limitations``.
    """
    d = Decision(item_id=evaluation.item_id, realm=evaluation.realm)

    # --- Insufficient evidence ---
    if math.isnan(evaluation.current_price) and math.isnan(evaluation.model_price):
        d.evidence.append("no current price or model price available")
        d.limitations.append("missing price data entirely")
        d.conditions_for_change.append("acquire current price or model prediction")
        return d

    if not evaluation.is_verified:
        d.evidence.append(f"reference_status={evaluation.reference_status}; not verified")
        d.limitations.append("reference not verified; only verified references support actionable decisions")
        d.conditions_for_change.append("obtain verified reference status")
        return d

    # --- We have a verified price ---
    d.evidence.append(f"reference verified; current price {evaluation.current_price:.0f} copper")

    # Gather helper values
    current = evaluation.current_price
    competing = evaluation.competing_price
    historical = evaluation.historical_price
    model_price = evaluation.model_price
    model_interval = evaluation.model_interval
    model_coverage = evaluation.model_coverage
    competing_supply = evaluation.competing_supply
    own_listings = evaluation.own_listings
    commission_pct = evaluation.sale_commission_pct or AH_CONSIGNMENT_RATE
    deposit = evaluation.deposit_cost if math.isfinite(evaluation.deposit_cost) else 0.0
    probability = evaluation.sale_probability

    # --- Helper: net proceeds ---
    #
    # This used to be `price * (1 - 0.05 / 100)`: the rate was divided by a
    # hundred as if it were a percentage, so every decision was made against
    # a 0.05 % cut instead of 5 %, and the deposit was not in the arithmetic
    # at all.  Both now come from the one implementation.
    def net(price: float) -> float:
        return _compute_net_proceeds(price, commission_pct, deposit, probability)

    # --- 1. POST a new listing ---
    # We POST when there's no existing listing (own_listings == 0) and we have
    # a credible price target.
    if own_listings == 0 and not math.isnan(current):
        # Use model price if it's credible (interval coverage >= 70% or interval width reasonable)
        price_to_post = current
        if not math.isnan(model_price):
            if model_interval is not None and model_coverage is not None:
                if model_coverage < 0.70 or model_interval.half_width_log > 1.5:
                    price_to_post = current  # stay with known price
                else:
                    price_to_post = model_price
            else:
                price_to_post = model_price

            # Economic check: expected net proceeds must be positive
            expected = net(price_to_post)
            if math.isfinite(expected) and expected > 0:
                d.action = "POST"
                d.suggested_price = price_to_post
                d.net_proceeds = expected
                d.evidence.append("no existing listing; posting at estimated price")
                d.evidence.append(f"expected net proceeds: {expected:.0f} copper (commission {commission_pct}%)")
                d.uncertainty = "interval" if model_interval is not None else "none"
                d.limitations.append("own listing count = 0; price based on model/verified current price")
                d.conditions_for_change.append("if own_quantity changes or competitor price moves")
                return d
            else:
                # Even the model price doesn't cover costs → don't list
                d.action = "DO_NOT_LIST"
                d.evidence.append(f"model price {price_to_post:.0f} copper yields non-positive net proceeds")
                d.limitations.append("economic unviable at modelled price")
                d.conditions_for_change.append("if costs decrease or price increases")
                return d

    # --- 2. MAINTAIN existing listing ---
    # We MAINTAIN when the current ask is already at or below the competitor,
    # or when the gap is small and undercut wouldn't materially improve outcomes.
    if own_listings > 0 and not math.isnan(current):
        if not math.isnan(competing) and current <= competing:
            d.action = "MAINTAIN"
            d.evidence.append(f"own ask {current:.0f} <= competitor {competing:.0f}; maintain")
            d.net_proceeds = net(current)
            d.uncertainty = "none"
            d.evidence.append(f"net proceeds: {net(current):.0f} copper")
            d.conditions_for_change.append("if competitor price drops below own ask")
            return d

        # Small gap: undercut may not be worthwhile
        if not math.isnan(competing):
            gap_pct = (current - competing) / current if current > 0 else 0
            if 0 < gap_pct < 0.10:  # < 10% gap
                d.action = "MAINTAIN"
                d.evidence.append(f"small price gap {gap_pct:.1%}; maintain rather than undercut")
                d.net_proceeds = net(current)
                d.uncertainty = "none"
                d.evidence.append(f"net proceeds: {net(current):.0f} copper")
                d.conditions_for_change.append("if gap widens to >= 10% or competitor price drops")
                return d

    # --- 3. UNDERCUT ---
    # Under cut when:
    #   - Cheapest competitor is credible (verified reference or many listings)
    #   - The gap is material (>= 10% after considering economics)
    #   - Our supply is not already dominating
    #   - Expected net proceeds after undercut exceed current by a meaningful margin
    if not math.isnan(competing) and not math.isnan(current) and own_listings >= 0:
        if current > competing:
            undercut_gap = (current - competing) / current
            if undercut_gap >= 0.10:  # at least 10% undercut
                # Check supply: if competitor has much more supply, undercut may not help
                if competing_supply is not None and competing_supply > 0 and own_listings > 0:
                    supply_ratio = competing_supply / max(own_listings, 1)
                else:
                    supply_ratio = 1.0

                # With a measured sale probability and a vendor value, the
                # economic comparison is the one the posting layer makes:
                # undercut vs hold vs relist vs stop, with the deposit
                # (vendor-based, refunded on sale) and the cut in the
                # arithmetic.  The crude "does a lower ask net more" test
                # below cannot see that a higher ask may never sell, so it
                # is only the fallback when the probability is unknown.
                if math.isfinite(probability) and math.isfinite(evaluation.vendor_price):
                    from .posting import decide as posting_decide

                    edge = posting_decide(
                        reference=model_price if math.isfinite(model_price) else competing,
                        vendor_sell_copper=int(evaluation.vendor_price),
                        sale_probability=probability,
                        hours=evaluation.listing_hours,
                        list_price=int(current),
                        best_ask=int(competing),
                        confidence=1.0,
                    )
                    action = {"relist": "CANCEL_AND_RELIST", "undercut": "UNDERCUT",
                              "hold": "MAINTAIN", "post": "POST",
                              "wait": "MAINTAIN", "stop": "DO_NOT_LIST"}.get(
                                  edge.action, "REVIEW_REQUIRED")
                    if action != "REVIEW_REQUIRED":
                        d.action = action
                        d.suggested_price = float(edge.ask) if edge.ask else None
                        d.net_proceeds = float(edge.net)
                        d.evidence.append(
                            f"posting layer: {edge.action} ({edge.reason})")
                        d.evidence.append(
                            f"expected net after 5% cut and deposit "
                            f"({edge.deposit} copper, refunded on sale): {edge.net} copper")
                        d.uncertainty = "interval" if model_interval is not None else "none"
                        d.conditions_for_change.append("if the sale probability or the cheapest ask moves")
                        return d

                # If competitor supply is not overwhelming, undercut may be worthwhile
                if supply_ratio < 3.0:  # competitor doesn't have 3x our supply
                    # Calculate potential net proceeds from undercut
                    undercut_price = competing - 1  # 1 copper undercut
                    undercut_net = net(undercut_price)
                    current_net = net(current)

                    if math.isfinite(undercut_net) and math.isfinite(current_net):
                        improvement = undercut_net - current_net
                        if improvement > 0 and improvement > 100:  # > 1s copper improvement
                            d.action = "UNDERCUT"
                            d.suggested_price = undercut_price
                            d.net_proceeds = undercut_net
                            d.evidence.append(f"undercut by 1 copper from {current:.0f} to {undercut_price:.0f}")
                            d.evidence.append(f"net improvement: {improvement:.0f} copper")
                            d.uncertainty = "interval" if model_interval is not None else "none"
                            d.limitations.append(f"competitor supply ratio: {supply_ratio:.2f}; actual supply may vary")
                            d.conditions_for_change.append("if competitor relists or supply changes")
                            return d

    # --- 5. EXPIRE ---
    # (In this simplified engine, time-left data is not available; note for relay layer)
    d.evidence.append("time-left data not available in this engine; EXPIRE decision left to relist layer")

    # --- 6. LIQUIDATE ---
    # (Liquidation strategy requires player objectives and acquisition costs;
    # left as limitation here.)
    d.limitations.append("liquidation strategy requires player objectives and acquisition costs not available in this engine")

    # --- 7. DO_NOT_LIST ---
    if math.isnan(net(current)) or net(current) <= 0:
        d.action = "DO_NOT_LIST"
        d.evidence.append(f"price {current:.0f} copper yields non-positive net proceeds after commission")
        d.limitations.append("economic unviable; cost exceeds expected return")
        d.conditions_for_change.append("if commission decreases or price increases")
        return d

    # --- 8. Fallback: REVIEW_REQUIRED ---
    d.action = "REVIEW_REQUIRED"
    d.evidence.append("could not converge on action; insufficient distinguishing evidence")
    d.limitations.append("policy could not decide between MAINTAIN, UNDERCUT, or DO_NOT_LIST")
    d.conditions_for_change.append("acquire more data: competitor prices, supply, player objectives")

    return d


# ---------------------------------------------------------------------------
# Batch decision (process a list of evaluations)
# ---------------------------------------------------------------------------

def batch_decisions(evaluations: List[ItemEvaluation]) -> List[Decision]:
    """Run the decision engine on a list of evaluations.

    Returns a list of :class:`Decision` objects, one per evaluation, in the
    same order.  Unknown/NaN items get a REVIEW_REQUIRED decision.
    """
    return [decide(ev) for ev in evaluations]


# ---------------------------------------------------------------------------
# Backward‑compatible API (do not remove without replacing)
# ---------------------------------------------------------------------------

def quick_decide(item_id: int, realm: str, current_price: float,
                 competing: float = math.nan,
                 own_listings: int = 0,
                 reference_verified: bool = False,
                 model_price: float = math.nan) -> Decision:
    """Convenience: produce a Decision from bare fields.

    Used by callers that cannot yet use the full ItemEvaluation pipeline.
    """
    from .market_eval import ItemEvaluation
    ev = ItemEvaluation(
        item_id=item_id,
        realm=realm,
        current_price=current_price,
        competing=competing,
        is_verified=reference_verified,
        model_price=model_price,
        own_listings=own_listings,
    )
    return decide(ev)


# ---------------------------------------------------------------------------
# Integration with the existing pipeline (Phase 1+2 already produced
# evaluations; now we wire them to decisions)
# ---------------------------------------------------------------------------

def run_decision_pipeline(frame: pd.DataFrame, *,
                          target: str = "y_net_unit_log",
                          record_iterations: int = 600,
                          sentinel_iterations: int = 300,
                          min_proceeds: float = 0.0) -> Tuple[List[ItemEvaluation], List[Decision]]:
    """Full pipeline: evaluate items → produce decisions.

    Returns (evaluations, decisions) aligned by index.
    """
    from .market_eval import evaluate_all_items, decision_pipeline as mp_decision_pipeline

    evaluations, model_results = evaluate_all_items(frame, target=target,
                                                    record_iterations=record_iterations,
                                                    sentinel_iterations=sentinel_iterations)
    decisions = mp_decision_pipeline(evaluations, min_proceeds=min_proceeds)
    return evaluations, decisions