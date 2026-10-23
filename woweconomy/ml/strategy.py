"""Model D -- the listing-price strategy.

This is deliberately **not** a fourth model.  It is the decision layer that
sits on top of models A, B and C:

* the **price model** says what the market is likely to pay for a unit;
* the **sale-probability model** says how likely a listing at a given ask
  is to sell inside the deadline;
* the **time-to-sale model** says how long that is likely to take, when
  the data supports it at all.

The strategy evaluates a grid of candidate asks and picks one.  It does
*not* claim that the best predicted price maximises realised gold: the
counterfactual "what if I had asked that" cannot be observed, and the
evaluation below states the assumption it rests on instead of hiding it.

The assumption is **monotone demand**: a lower ask is at least as
attractive to a buyer as a higher one.  Under that assumption the outcome
of a listing gives a usable, checkable regret:

* a listing that **sold** at ``A`` would also have sold at any
  ``P <= A`` -- the strategy would have earned ``P`` instead of ``A``, so
  the regret is ``A - P``;
* a listing that **expired** at ``A`` would also have expired at any
  ``P >= A``, and *might* have sold at any ``P < A`` -- the regret is
  bounded by the price that was left on the table.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import AH_CONSIGNMENT_RATE, COPPER_PER_GOLD

logger = logging.getLogger(__name__)


@dataclass
class StrategyConfig:
    """What the strategy is allowed to assume."""

    #: the house cut
    fee_rate: float = AH_CONSIGNMENT_RATE
    #: the deposit is NOT a share of the ask: it is a share of the item's
    #: vendor sell value, refunded only when the listing sells.  Use
    #: :func:`woweconomy.ml.posting.deposit_copper` -- there is deliberately
    #: no rate here, because a single rate cannot express it.
    #: the user's deadline, in hours
    deadline_h: int = 72
    #: the objective: "ev" maximises expected gold, "ev_deadline" assumes an
    #: unsold item is worthless at the deadline, "probability" maximises the
    #: chance of selling in time
    objective: str = "ev"
    #: a floor on the acceptable sale probability
    min_sale_probability: float = 0.0
    #: how far around the anchor the grid may reach
    min_price_multiple: float = 0.35
    max_price_multiple: float = 3.0
    #: how many candidate prices to evaluate
    grid_points: int = 41
    #: the grid is also bounded by the range of asks the probability model
    #: was actually trained on: a candidate outside it is an
    #: extrapolation, not a measurement.  The bounds are the observed
    #: quantiles of ``ask / regional sale average`` and they are computed
    #: from the training rows, not chosen by hand.
    ask_ratio_bounds: Optional[Tuple[float, float]] = None
    #: what an unsold item is still worth, as a fraction of the expected
    #: proceeds of listing it again.  The seller keeps the item, so it is
    #: not worthless -- and treating it as worthless would push every
    #: recommendation upward, which is the classic mistake of an auction
    #: bot that only counts sales.
    hold_value_ratio: float = 1.0
    #: the cost of listing again and waiting longer, in gold per unit
    relist_cost_gold: float = 0.0
    #: how far the item's own lift (what the model says about *this*
    #: item) may move the measured sale rate
    lift_bounds: Tuple[float, float] = (0.2, 5.0)


@dataclass
class Candidate:
    """One candidate ask and what the models say about it."""

    ask_unit: int
    sale_probability: float
    expected_net_gold: float
    p10_net_gold: float = 0.0
    p50_net_gold: float = 0.0
    p90_net_gold: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "ask_unit": self.ask_unit,
            "sale_probability": self.sale_probability,
            "expected_net_gold": self.expected_net_gold,
            "p10_net_gold": self.p10_net_gold,
            "p50_net_gold": self.p50_net_gold,
            "p90_net_gold": self.p90_net_gold,
        }


def reprice(base: Dict[str, object], ask_unit: int, qty: int,
            bid_unit: Optional[int] = None) -> Dict[str, object]:
    """A copy of a feature row with a different ask.

    The ask feeds back into the model through three ratios, so they have
    to be recomputed; everything else is a property of the market and
    stays as it was.  Every key the models expect is written, even when
    the answer is unknown, so the rows of a candidate grid all have the
    same columns.
    """
    row = dict(base)
    ask = float(ask_unit)
    row["ask_unit_log"] = math.log1p(ask) if ask > 0 else None

    def ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
        if num is None or den is None or den <= 0:
            return None
        return num / den

    def from_log(value) -> Optional[float]:
        if value is None:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
            return None
        return math.expm1(v)

    local_market = from_log(row.get("local_market_log"))
    region_sale_avg = from_log(row.get("region_sale_avg_log"))
    region_market = from_log(row.get("region_market_log"))
    row["ask_over_local_market"] = ratio(ask, local_market)
    row["ask_over_region_sale_avg"] = ratio(ask, region_sale_avg)
    row["ask_over_region_market"] = ratio(ask, region_market)
    if bid_unit:
        row["bid_to_buyout"] = ratio(float(bid_unit), ask)
    row.setdefault("bid_to_buyout", None)
    row.setdefault("unit_bid_log", None)
    # the deposit ratio moves with the ask too: leaving it behind would
    # feed the model a row that mixes one ask with another row's ratio
    deposit = from_log(row.get("deposit_log"))
    if deposit is not None and ask > 0:
        row["deposit_ratio"] = ratio(deposit, ask * max(1, qty))
    else:
        row.setdefault("deposit_ratio", None)
    row["qty"] = float(qty)
    row["qty_log"] = math.log1p(qty)
    return row


def _anchor_prices(base: Dict[str, object], predicted_unit: Optional[float]) -> List[int]:
    """The prices worth testing around."""
    out: List[int] = []
    if predicted_unit and predicted_unit > 0:
        out.append(int(round(predicted_unit)))
    for key in ("region_sale_avg_log", "region_market_log", "local_market_log",
                "local_recent_log", "region_historical_log", "local_historical_log"):
        value = base.get(key)
        if value is None:
            continue
        try:
            out.append(int(round(math.expm1(float(value)))))
        except (TypeError, ValueError, OverflowError):
            continue
    return [p for p in out if p > 0]


def candidate_grid(base: Dict[str, object],
                   predicted_unit: Optional[float],
                   config: StrategyConfig) -> List[int]:
    """A grid of candidate asks, anchored on the model's own estimate."""
    anchors = _anchor_prices(base, predicted_unit)
    if not anchors:
        return []
    centre = predicted_unit if (predicted_unit and predicted_unit > 0) else anchors[0]
    lo = centre * config.min_price_multiple
    hi = centre * config.max_price_multiple
    grid = {int(round(v)) for v in np.geomspace(max(1.0, lo), max(2.0, hi),
                                                config.grid_points)}
    for anchor in anchors:
        grid.add(int(round(anchor)))
    # the bound: a price outside the range the probability model was
    # trained on is an extrapolation, and the grid must not offer one
    sale_avg = base.get("region_sale_avg_log")
    bounds = config.ask_ratio_bounds
    if sale_avg is not None and bounds:
        try:
            reference = math.expm1(float(sale_avg))
            ceiling = reference * bounds[1]
            floor = reference * bounds[0]
            grid = {p for p in grid if floor <= p <= ceiling}
        except (TypeError, ValueError, OverflowError):
            pass
    return sorted(p for p in grid if p > 0)


class ListingStrategy:
    """Turns the models into a recommended ask for one item."""

    def __init__(self,
                 price_model=None,
                 probability_model=None,
                 time_model=None,
                 config: Optional[StrategyConfig] = None,
                 quantiles: Optional[Dict[float, object]] = None,
                 curve: Optional[Callable[[float], float]] = None) -> None:
        self.price_model = price_model
        self.probability_model = probability_model
        self.time_model = time_model
        self.config = config or StrategyConfig()
        #: the quantile models of the price, when they are available
        self.quantiles = quantiles or {}
        #: the measured sale rate as a function of
        #: ``ask / regional sale average``.  It is an observation, not a
        #: model, and it is the only trustworthy thing about the *ask*
        #: dimension of the question.
        self.curve = curve

    # -- the ask dimension ------------------------------------------------
    def curve_probability(self, base: Dict[str, object], ask_unit: int) -> Optional[float]:
        """The measured sale rate for a listing at this ask."""
        if self.curve is None:
            return None
        reference = base.get("ref_region_sale_avg")
        if not reference or reference <= 0:
            return None
        return float(self.curve(float(ask_unit) / float(reference)))

    def item_lift(self, base: Dict[str, object], qty: int) -> Optional[float]:
        """What the model says about *this* item, relative to the average.

        The measured curve says how the sale rate falls with the ask; it
        says nothing about whether *this* item sells better or worse than
        the average one.  The model does know that, but only reliably at
        the prices the account actually used.  So the model's probability
        at the item's reference price is divided by the curve's value
        there: what is left is the item's own lift.
        """
        if self.probability_model is None or not getattr(self.probability_model, "trained", False):
            return None
        reference = base.get("ref_region_sale_avg")
        if not reference or reference <= 0:
            return None
        row = reprice(base, int(round(reference)), qty)
        model_prob = self.sale_probability(pd.DataFrame([row]))
        if model_prob is None or not len(model_prob):
            return None
        curve_prob = self.curve_probability(base, int(round(reference)))
        if curve_prob is None or curve_prob <= 0:
            return None
        lift = float(model_prob[0]) / curve_prob
        lo, hi = self.config.lift_bounds
        return float(min(hi, max(lo, lift)))

    # -- the pieces ------------------------------------------------------
    def predicted_price(self, base: Dict[str, object]) -> Optional[float]:
        """Model A's estimate of the price a unit will fetch."""
        if self.price_model is None or not getattr(self.price_model, "trained", False):
            return None
        frame = pd.DataFrame([base])
        try:
            return float(self.price_model.predict_net_unit(frame)[0])
        except Exception as exc:
            logger.warning("the price model could not predict: %s", exc)
            return None

    def predicted_interval(self, base: Dict[str, object]) -> Optional[Dict[str, float]]:
        """The spread of the price, when the quantile models are available.

        A recommendation that does not say how wide its own error is asks
        for more trust than the data can give.
        """
        return self._interval(pd.DataFrame([base]))

    def _interval(self, frame: pd.DataFrame) -> Optional[Dict[str, float]]:
        if not self.quantiles:
            return None
        from .models import make_pool
        if self.price_model is None:
            return None
        feats = self.price_model.features
        cats = self.price_model.categoricals
        out: Dict[str, float] = {}
        try:
            pool = make_pool(frame, feats, cats)
            for alpha, model in self.quantiles.items():
                out[f"p{int(alpha * 100)}"] = float(np.exp(model.predict(pool)[0]))
        except Exception as exc:
            logger.warning("the quantile models could not predict: %s", exc)
            return None
        return out or None

    def sale_probability(self, frame: pd.DataFrame) -> Optional[np.ndarray]:
        if self.probability_model is None or not getattr(self.probability_model, "trained", False):
            return None
        try:
            return np.asarray(self.probability_model.predict(frame), dtype=float)
        except Exception as exc:
            logger.warning("the probability model could not predict: %s", exc)
            return None

    def expected_time_hours(self, frame: pd.DataFrame) -> Optional[np.ndarray]:
        if self.time_model is None or not getattr(self.time_model, "trained", False):
            return None
        try:
            return np.asarray(self.time_model.predict_median_hours(frame), dtype=float)
        except Exception as exc:
            logger.warning("the time-to-sale model could not predict: %s", exc)
            return None

    # -- the recommendation ---------------------------------------------
    def recommend(self,
                  base: Dict[str, object],
                  qty: int = 1,
                  deadline_h: Optional[int] = None,
                  stock: Optional[int] = None,
                  bid_unit: Optional[int] = None) -> Dict[str, object]:
        """The recommended ask for one item, with the whole grid behind it.

        The probability model is the model of *one* horizon -- the one it
        was fitted on.  Asking it about a different deadline would be
        asking a model a question it was not asked, so the horizon is
        checked and the answer is labelled with the horizon it really
        answers.
        """
        cfg = self.config
        deadline = deadline_h or cfg.deadline_h
        available = stock if stock is not None else qty
        if available <= 0:
            return {"error": "no stock", "candidates": []}

        model_horizon = getattr(self.probability_model, "horizon_h", 0) or 0
        horizon_note = ""
        if model_horizon and model_horizon != deadline:
            horizon_note = (f"the probability model answers the {model_horizon}h "
                            f"horizon, not {deadline}h")

        predicted = self.predicted_price(base)
        prices = candidate_grid(base, predicted, cfg)
        if not prices:
            return {"error": "no candidate price: the model has no market reference",
                    "candidates": [], "predicted_unit_price": predicted}

        rows = []
        for price in prices:
            row = reprice(base, price, qty, bid_unit)
            rows.append(row)
        frame = pd.DataFrame(rows)

        # --- the probability of each candidate ask -------------------------
        # Two sources, each used where it is strongest.  The *ask*
        # dimension comes from the measured demand curve: it is an
        # observation of how often a listing at that ask sold, and the
        # model cannot be trusted outside the range of asks it was
        # trained on.  The *item* dimension comes from the model, which
        # ranks items well (it separates a seller who will sell from one
        # who will not), scaled into the curve as a lift.
        model_probs = self.sale_probability(frame)
        curve_probs = np.array([
            self.curve_probability(base, p) if self.curve_probability(base, p)
            is not None else np.nan for p in prices], dtype=float)
        lift = self.item_lift(base, qty)

        if self.curve is not None and not np.all(np.isnan(curve_probs)):
            probs = curve_probs * (lift if lift is not None else 1.0)
            if np.all(np.isnan(probs)):
                probs = model_probs
            source = "the measured demand curve, scaled by the model's lift for this item"
        else:
            probs = model_probs
            source = "the sale-probability model"

        if probs is None:
            # without a probability model the only honest thing is to report
            # the price the market model expects and say that the sale
            # probability is unknown
            return {
                "recommended_ask": int(round(predicted)) if predicted else None,
                "predicted_unit_price": predicted,
                "sale_probability": None,
                "note": ("no sale-probability model is available, so the "
                         "recommendation is the expected price only"),
                "candidates": [Candidate(ask_unit=p, sale_probability=float("nan"),
                                         expected_net_gold=float("nan")).as_dict()
                               for p in prices],
            }
        probs = np.clip(np.asarray(probs, dtype=float), 0.0, 1.0)

        # the value of holding the item if it does not sell here is **not**
        # its asking price: the seller keeps the item and has to list it
        # again, and the measured behaviour says most listings do not sell.
        # The honest value of holding is therefore the *expected proceeds*
        # of listing it again -- the probability of selling at that price,
        # times the price.
        hold_value = 0.0
        market_probability = None
        if cfg.objective == "ev" and predicted:
            market_row = reprice(base, int(round(predicted)), qty, bid_unit)
            market_frame = pd.DataFrame([market_row])
            market_curve = self.curve_probability(base, int(round(predicted)))
            market_model = self.sale_probability(market_frame)
            if market_curve is not None:
                market_probability = float(min(1.0, max(0.0, market_curve * (lift if lift is not None else 1.0))))
            elif market_model is not None and len(market_model):
                market_probability = float(min(1.0, max(0.0, market_model[0])))
            if market_probability is not None:
                hold_value = (market_probability * predicted * (1 - cfg.fee_rate)
                              * cfg.hold_value_ratio)

        net_per_unit = np.array([p * (1 - cfg.fee_rate) for p in prices])
        units = min(qty, available)
        expected_gold = (probs * net_per_unit * units
                         + (1 - probs) * hold_value * units
                         - (1 - probs) * cfg.relist_cost_gold * units)
        expected_gold = expected_gold / COPPER_PER_GOLD

        if cfg.objective == "probability":
            objective = probs
        else:
            objective = expected_gold
        admissible = probs >= cfg.min_sale_probability
        if admissible.any():
            best = int(np.argmax(np.where(admissible, objective, -np.inf)))
        else:
            best = int(np.argmax(objective))

        candidates = []
        intervals = self._interval(frame) or {}
        for i in range(len(prices)):
            entry = Candidate(ask_unit=int(prices[i]),
                              sale_probability=float(probs[i]),
                              expected_net_gold=float(expected_gold[i])).as_dict()
            if intervals:
                per = self._interval(frame.iloc[[i]]) or {}
                if per:
                    entry["price_p10"] = per.get("p10")
                    entry["price_p90"] = per.get("p90")
            candidates.append(entry)

        result: Dict[str, object] = {
            "recommended_ask": int(prices[best]),
            "sale_probability": float(probs[best]),
            "expected_net_gold": float(expected_gold[best]),
            "predicted_unit_price": predicted,
            "hold_value_gold": float(hold_value * min(qty, available) / COPPER_PER_GOLD),
            "market_sale_probability": market_probability,
            "item_lift": lift,
            "probability_source": source,
            "deadline_h": deadline,
            "candidates": candidates,
            "objective": cfg.objective,
        }
        interval = self.predicted_interval(base)
        if interval:
            result["price_interval"] = interval
            result["price_interval_note"] = (
                "the spread of what a unit of this item fetched, over the "
                "transactions the model has seen")
        reference = base.get("ref_region_sale_avg")
        if reference and reference > 0:
            result["recommended_over_regional_sale_avg"] = float(
                int(prices[best]) / float(reference))
            result["predicted_over_regional_sale_avg"] = float(
                predicted / float(reference)) if predicted else None
        if horizon_note:
            result["note"] = horizon_note
        result["caveat"] = (
            "the probability model has only seen the prices this account "
            "actually asked; a candidate far outside that behaviour is an "
            "extrapolation, not a measurement")
        if self.time_model is not None and getattr(self.time_model, "trained", False):
            times = self.expected_time_hours(frame.iloc[[best]])
            if times is not None:
                result["expected_time_hours"] = float(times[0])
        return result

    # -- the evaluation ---------------------------------------------------
    def evaluate(self,
                 frame: pd.DataFrame,
                 features: Sequence[str],
                 categoricals: Sequence[str]) -> Dict[str, object]:
        """Replay the strategy over the observed listings.

        For every listing whose outcome is known, the strategy is asked
        what it would have recommended using only the information
        available at the posting, and the recommendation is compared with
        what actually happened under the monotone-demand assumption.
        """
        from .models import make_pool

        if self.price_model is None or not getattr(self.price_model, "trained", False):
            return {"error": "no price model"}
        if self.probability_model is None and self.curve is None:
            return {"error": "no sale-probability model and no demand curve"}

        rows = []
        for _, row in frame.iterrows():
            # the base row must carry every column the models expect, or
            # the candidate grid would not have the same shape as the
            # training data
            base = {name: row.get(name) for name in features}
            base.update({name: row.get(name) for name in categoricals})
            base = {k: (None if (isinstance(v, float) and math.isnan(v)) else v)
                    for k, v in base.items()}
            actual_ask = int(row.get("ask_unit") or 0)
            qty = int(row.get("qty") or 1)
            outcome = row.get("outcome")
            if actual_ask <= 0 or outcome in (None, "censored"):
                continue
            rec = self.recommend(base, qty=qty, bid_unit=None)
            if "recommended_ask" not in rec or rec["recommended_ask"] is None:
                continue
            rec_ask = int(rec["recommended_ask"])
            if outcome == "sale":
                if rec_ask <= actual_ask:
                    # a cheaper ask would have sold as well: we left money
                    # on the table
                    regret_gold = (actual_ask - rec_ask) * qty * (1 - self.config.fee_rate) \
                        / COPPER_PER_GOLD
                else:
                    # a dearer ask might not have sold at all
                    regret_gold = 0.0
            else:  # expired
                if rec_ask >= actual_ask:
                    regret_gold = 0.0
                else:
                    # a cheaper ask might have sold: an unknown gain
                    regret_gold = 0.0
            rows.append({
                "item_id": row.get("item_id"),
                "realm": row.get("realm"),
                "outcome": outcome,
                "qty": qty,
                "actual_ask": actual_ask,
                "recommended_ask": rec_ask,
                "ask_ratio": rec_ask / actual_ask if actual_ask else None,
                "sale_probability": rec.get("sale_probability"),
                "regret_gold": regret_gold,
            })
        if not rows:
            return {"error": "no listing with a known outcome"}
        out = pd.DataFrame(rows)
        sold = out[out["outcome"] == "sale"]
        expired = out[out["outcome"] == "expired"]
        gold_gap = ((out["recommended_ask"] - out["actual_ask"]).abs()
                    * out.get("qty", 1) * (1 - self.config.fee_rate) / COPPER_PER_GOLD)

        def ratios(frame: pd.DataFrame) -> Dict[str, object]:
            if frame.empty:
                return {}
            return {
                "n": int(len(frame)),
                "median_ask_ratio": float(frame["ask_ratio"].median()),
                "p25_ask_ratio": float(frame["ask_ratio"].quantile(0.25)),
                "p75_ask_ratio": float(frame["ask_ratio"].quantile(0.75)),
                "recommended_below_rate": float((frame["ask_ratio"] < 0.95).mean()),
                "recommended_above_rate": float((frame["ask_ratio"] > 1.05).mean()),
            }

        return {
            "listings_evaluated": int(len(out)),
            "sold": int(len(sold)),
            "expired": int(len(expired)),
            "mean_ask_ratio": float(out["ask_ratio"].mean()),
            "median_ask_ratio": float(out["ask_ratio"].median()),
            "recommended_below_actual_rate": float((out["ask_ratio"] < 0.95).mean()),
            "recommended_above_actual_rate": float((out["ask_ratio"] > 1.05).mean()),
            "mean_regret_gold": float(out["regret_gold"].mean()),
            "total_regret_gold": float(out["regret_gold"].sum()),
            "mean_abs_ask_gold": float(
                ((out["recommended_ask"] - out["actual_ask"]).abs()
                 * out.get("qty", 1) * (1 - self.config.fee_rate) / COPPER_PER_GOLD).mean()),
            "median_abs_ask_gold": float(
                ((out["recommended_ask"] - out["actual_ask"]).abs()
                 * out.get("qty", 1) * (1 - self.config.fee_rate) / COPPER_PER_GOLD).median()),
            "mean_predicted_sale_probability": float(out["sale_probability"].mean()),
            "sold_listings": ratios(sold),
            "expired_listings": ratios(expired),
            "note": ("regret is computed under the monotone-demand assumption: "
                     "a lower ask is at least as likely to sell.  It is an upper "
                     "bound on what the strategy gives up, not proof that the "
                     "strategy maximises realised gold -- the counterfactual ask "
                     "was never observed.  The probability model has also only "
                     "seen the prices this account actually asked, so a candidate "
                     "far outside that behaviour is an extrapolation"),
        }
