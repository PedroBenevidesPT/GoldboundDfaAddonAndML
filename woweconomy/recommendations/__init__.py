"""Recommendation engine — FarmScore, FlipScore, InvestmentScore, Confidence."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from woweconomy.analytics.classifier import MarketClassifier
from woweconomy.analytics.risk import RiskAnalyzer
from woweconomy.config import WowEconomyConfig
from woweconomy.models import (
    Forecast,
    MarketSignal,
    MarketState,
    PriceHistory,
    Recommendation,
    RiskProfile,
)
from woweconomy.services.metadata import MetadataService

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Confidence
# ──────────────────────────────────────────────
@dataclass
class Confidence:
    label: str = "Low"       # Low, Medium, High
    score: float = 0.0       # 0-1
    reasons: List[str] = field(default_factory=list)


def compute_confidence(state: MarketState, forecast: Optional[Forecast],
                       record_count: int) -> Confidence:
    reasons = []
    factors = []

    # History length
    if record_count >= 30:
        factors.append(1.0)
    elif record_count >= 14:
        factors.append(0.7)
    elif record_count >= 7:
        factors.append(0.5)
    elif record_count >= 3:
        factors.append(0.3)
    else:
        factors.append(0.1)
        reasons.append("pouco historico")

    # Price stability
    stab = state.price_stability if state.price_stability else 0.5
    factors.append(stab)
    if stab < 0.3:
        reasons.append("preco volatil")

    # Volatility
    vol = state.volatility if state.volatility else 0
    if vol < 0.05:
        factors.append(1.0)
    elif vol < 0.15:
        factors.append(0.7)
    else:
        factors.append(0.3)
        reasons.append("alta volatilidade")

    # Forecast confidence
    if forecast and forecast.forecast_available:
        ci = forecast.confidence_interval
        if ci < 0.1:
            factors.append(1.0)
        elif ci < 0.3:
            factors.append(0.8)
        else:
            factors.append(0.5)
    else:
        factors.append(0.3)
        if record_count < 14:
            reasons.append("sem previsao")

    # Data completeness
    complete = all([
        state.sold_per_day > 0,
        state.sale_rate > 0,
        state.avg_sale_price > 0,
        state.historical_mean > 0,
    ])
    factors.append(1.0 if complete else 0.5)
    if not complete:
        reasons.append("dados incompletos")

    score = sum(factors) / len(factors)
    score = max(0.0, min(1.0, score))

    if score >= 0.75:
        label = "Alta"
    elif score >= 0.45:
        label = "Media"
    else:
        label = "Baixa"

    return Confidence(label=label, score=round(score, 3), reasons=reasons)


# ──────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────
@dataclass
class ValidationIssue:
    severity: str  # warning, error
    message: str


def validate_recommendations(recs: List[Recommendation],
                             metadata: MetadataService,
                             snap_count: int) -> List[ValidationIssue]:
    issues = []

    for i, rec in enumerate(recs):
        rank = i + 1
        spd = rec.key_indicators.get("spd", 0)
        sr = rec.key_indicators.get("sr", 0)
        meta = metadata.get(rec.item_id, rec.item_name)

        # Vendor in top farm
        if meta.is_vendor_item and rec.stars >= 4:
            issues.append(ValidationIssue(
                severity="error",
                message=f"VENDOR #{rank}: {rec.item_name} (ID={rec.item_id}) no Top FarmScore. "
                        f"Preco vendedor={meta.vendor_price}g",
            ))

        # SPD=0 in farm
        if spd == 0 and rec.stars >= 4:
            issues.append(ValidationIssue(
                severity="error",
                message=f"SPD_ZERO #{rank}: {rec.item_name} (ID={rec.item_id}) SPD=0 no topo",
            ))

        # SR=0 in farm
        if sr == 0 and rec.stars >= 4:
            issues.append(ValidationIssue(
                severity="error",
                message=f"SR_ZERO #{rank}: {rec.item_name} (ID={rec.item_id}) SR=0 no topo",
            ))

        # InvestmentScore without history
        if rec.investment_score is not None and snap_count < 30:
            issues.append(ValidationIssue(
                severity="warning",
                message=f"INV_HIST: InvestmentScore gerado com apenas {snap_count} snapshot(s)",
            ))

        # FlipScore with no profit
        if rec.flip_score is not None and rec.flip_score > 0:
            s = rec.score_breakdown
            roi = s.get("expected_roi", 0)
            if roi <= 0:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=f"FLIP_ROI #{rank}: {rec.item_name} FlipScore>0 mas ROI={roi:.3f}",
                ))

    return issues


# ──────────────────────────────────────────────
# Normalizer
# ──────────────────────────────────────────────
class PercentileNormalizer:
    """Dynamic percentile-based normalizer using a reference population."""

    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()

    def fit(self, states: List[MarketState]) -> None:
        log_spds = [math.log(1 + max(s.sold_per_day, 0)) for s in states]
        self.demand_cap = float(np.percentile(log_spds, self.config.demand_percentile_cap))

        srs = [max(s.sale_rate, 0) for s in states]
        self.liquidity_cap = float(np.percentile(srs, self.config.liquidity_percentile_cap))

        log_prices = [math.log(1 + max(s.avg_sale_price, 0)) for s in states]
        self.value_cap = float(np.percentile(log_prices, self.config.value_percentile_cap))

        trend_devs = []
        for s in states:
            if s.historical_mean > 0 and s.current_price > 0:
                trend_devs.append(abs(math.log(s.current_price / s.historical_mean)))
        self.trend_dev_cap = float(np.percentile(trend_devs, self.config.trend_dev_percentile_cap)) if trend_devs else 1.0

        zs = [abs(s.z_score) for s in states if s.z_score != 0]
        self.z_score_cap = float(np.percentile(zs, 95)) if zs else 1.0

        vols = [s.volatility for s in states if s.volatility > 0]
        self.vol_cap = float(np.percentile(vols, 95)) if vols else 1.0

        moms = [abs(s.momentum) for s in states if s.momentum != 0]
        self.mom_cap = float(np.percentile(moms, 95)) if moms else 1.0

    def demand_score(self, spd: float) -> float:
        log_spd = math.log(1 + max(spd, 0))
        if self.demand_cap > 0:
            return min(1.0, log_spd / self.demand_cap)
        return 0.0

    def liquidity_score(self, sale_rate: float) -> float:
        if self.liquidity_cap > 0:
            return min(1.0, max(sale_rate, 0) / self.liquidity_cap)
        return 0.0

    def item_value_score(self, price: float) -> float:
        log_p = math.log(1 + max(price, 0))
        if self.value_cap > 0:
            return min(1.0, log_p / self.value_cap)
        return 0.0

    def stability_score(self, stability: Optional[float]) -> float:
        return max(0.0, min(1.0, stability if stability is not None else 0.5))

    def trend_score(self, cp: float, hm: float) -> float:
        if hm <= 0 or cp <= 0:
            return 0.5
        log_dev = abs(math.log(cp / hm))
        if self.trend_dev_cap > 0:
            return max(0.0, min(1.0, 1.0 - log_dev / self.trend_dev_cap))
        return 0.5

    def momentum_score(self, momentum: float) -> float:
        abs_mom = abs(momentum)
        if self.mom_cap > 0:
            return min(1.0, abs_mom / self.mom_cap)
        return min(1.0, abs_mom / 0.1)

    def reversion_score(self, z_score: float) -> float:
        abs_z = abs(z_score)
        if abs_z <= 0.5:
            return 1.0  # close to mean, high reversion potential
        elif abs_z >= 2.0:
            return 0.1  # extreme, low confidence in reversion
        else:
            return max(0.0, 1.0 - (abs_z - 0.5) / 1.5)

    def volatility_score(self, vol: float) -> float:
        if self.vol_cap > 0:
            return max(0.0, min(1.0, 1.0 - vol / self.vol_cap))
        return max(0.0, min(1.0, 1.0 - vol / 0.3))

    def historical_ratio_score(self, cur: float, hist: float) -> float:
        if hist <= 0:
            return 0.5
        ratio = cur / hist
        if ratio < 0.5:
            return 0.0  # way below historical — bad for investment
        elif ratio < 0.8:
            return 0.5  # below average
        elif ratio <= 1.2:
            return 1.0  # near historical — ideal
        elif ratio <= 1.5:
            return 0.6  # above average
        else:
            return 0.2  # way above historical — risky entry

    def roi_score(self, roi: float) -> float:
        """ROI normalised: 50% ROI -> 1.0, capped at 50%."""
        return min(1.0, max(0.0, roi / 0.50))

    def time_score(self, days: float) -> float:
        """Faster is better. 1 day = 1.0, 30 days = 0.0."""
        cap = self.config.flip_max_time_to_sell_days
        return max(0.0, min(1.0, 1.0 - days / cap))


# ──────────────────────────────────────────────
# Gates
# ──────────────────────────────────────────────
class FarmGates:
    def __init__(self, config: WowEconomyConfig):
        self.cfg = config

    def evaluate(self, spd: float, sr: float, is_vendor: bool) -> Tuple[float, str]:
        if spd == 0:
            return 0.0, "bloqueado: SPD=0"
        if sr <= 0:
            return 0.0, "bloqueado: SR<=0"
        if is_vendor and self.cfg.exclude_vendor_items:
            return 0.0, "bloqueado: item de vendedor NPC"
        penalty = 1.0
        reasons = []
        if spd < self.cfg.min_spd_for_farm:
            penalty *= self.cfg.gate_penalty
            reasons.append(f"SPD={spd:.1f}<{self.cfg.min_spd_for_farm}")
        if sr < self.cfg.min_sr_for_farm:
            penalty *= self.cfg.gate_penalty
            reasons.append(f"SR={sr:.4f}<{self.cfg.min_sr_for_farm}")
        if reasons:
            return penalty, "penalizado: " + ", ".join(reasons)
        return 1.0, ""


# ──────────────────────────────────────────────
# FarmScore
# ──────────────────────────────────────────────
class FarmScorer:
    def __init__(self, config: WowEconomyConfig, normalizer: PercentileNormalizer):
        self.cfg = config
        self.norm = normalizer
        self.gates = FarmGates(config)

    def compute(self, state: MarketState, is_vendor: bool) -> Tuple[float, Dict[str, float], str]:
        w = self.cfg.farm_score_weights
        dims = {
            "demand": self.norm.demand_score(state.sold_per_day),
            "liquidity": self.norm.liquidity_score(state.sale_rate),
            "item_value": self.norm.item_value_score(state.avg_sale_price),
            "stability": self.norm.stability_score(state.price_stability),
            "trend": self.norm.trend_score(state.current_price, state.historical_mean),
        }
        gate_val, gate_reason = self.gates.evaluate(state.sold_per_day, state.sale_rate, is_vendor)
        if gate_val == 0:
            return 0.0, dims, gate_reason
        weighted = sum(w[k] * dims[k] for k in w)
        return max(0.0, min(1.0, gate_val * weighted)), dims, gate_reason


# ──────────────────────────────────────────────
# FlipScore (redesigned around expected profit)
# ──────────────────────────────────────────────
class FlipScorer:
    def __init__(self, config: WowEconomyConfig, normalizer: PercentileNormalizer):
        self.cfg = config
        self.norm = normalizer

    def compute(self, state: MarketState, is_vendor: bool, vp: int) -> Tuple[float, Dict[str, float]]:
        w = self.cfg.flip_score_weights
        cur = state.current_price
        hist = state.historical_mean
        asp = state.avg_sale_price

        # Expected sale price: use AVG sale price if available, else historical
        expected_sale = max(asp, hist) if asp > 0 or hist > 0 else cur
        if expected_sale <= 0:
            expected_sale = cur

        # AH cut
        ah_fee = expected_sale * self.cfg.ah_cut_pct
        net_sale = expected_sale - ah_fee

        # Vendor item logic
        if is_vendor and vp > 0:
            cost_price = vp
        else:
            cost_price = cur

        # Expected profit & ROI
        profit = net_sale - cost_price
        roi = profit / cost_price if cost_price > 0 else 0.0

        # Time to sell (days) — rough: 1/SR, capped
        sr = max(state.sale_rate, 0.0001)
        days_to_sell = 1.0 / sr

        # Discount from historical
        discount = (hist - cur) / hist if hist > 0 else 0.0

        # ROI score: gate — hard zero below min ROI
        if roi <= self.cfg.flip_min_roi:
            return 0.0, {
                "expected_roi": 0.0, "demand": 0.0, "liquidity": 0.0,
                "time_to_sell": 0.0, "price_discount": 0.0,
                "_roi_raw": round(roi, 4),
                "_profit_raw": round(profit, 0),
                "_days_to_sell": round(days_to_sell, 1),
                "_expected_sale": round(expected_sale, 0),
                "_cost_price": round(cost_price, 0),
            }
        roi_score = self.norm.roi_score(roi)

        dims = {
            "expected_roi": roi_score,
            "demand": self.norm.demand_score(state.sold_per_day),
            "liquidity": self.norm.liquidity_score(state.sale_rate),
            "time_to_sell": self.norm.time_score(days_to_sell),
            "price_discount": min(1.0, max(0.0, discount / 0.5)),
            "_roi_raw": round(roi, 4),
            "_profit_raw": round(profit, 0),
            "_days_to_sell": round(days_to_sell, 1),
            "_expected_sale": round(expected_sale, 0),
            "_cost_price": round(cost_price, 0),
        }

        score = sum(w[k] * dims[k] for k in w if not k.startswith("_"))
        return max(0.0, min(1.0, score)), dims


# ──────────────────────────────────────────────
# InvestmentScore
# ──────────────────────────────────────────────
class InvestmentScorer:
    def __init__(self, config: WowEconomyConfig, normalizer: PercentileNormalizer):
        self.cfg = config
        self.norm = normalizer

    def compute(self, state: MarketState, record_count: int | list[int]
                ) -> Tuple[Optional[float], Optional[Dict[str, float]], str]:
        # `record_count` is typed `int` in single-item evaluation, but
        # evaluate_batch() passes a list (one count per state). Coerce the
        # single-element list before the integer comparison.
        if isinstance(record_count, list):
            record_count = len(record_count)
        if record_count < self.cfg.min_history_for_investment:
            return None, None, f"historico insuficiente ({record_count}<{self.cfg.min_history_for_investment})"

        w = self.cfg.investment_score_weights
        dims = {
            "trend": self.norm.trend_score(state.current_price, state.historical_mean),
            "momentum": self.norm.momentum_score(state.momentum),
            "mean_reversion": self.norm.reversion_score(state.z_score),
            "volatility": self.norm.volatility_score(state.volatility),
            "historical_ratio": self.norm.historical_ratio_score(state.current_price, state.historical_mean),
        }
        score = sum(w[k] * dims[k] for k in w)
        return max(0.0, min(1.0, score)), dims, ""


# ──────────────────────────────────────────────
# RecommendationEngine
# ──────────────────────────────────────────────
class RecommendationEngine:
    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()
        self.normalizer = PercentileNormalizer(self.config)
        self.farm_scorer = FarmScorer(self.config, self.normalizer)
        self.inv_scorer = InvestmentScorer(self.config, self.normalizer)
        self.flip_scorer = FlipScorer(self.config, self.normalizer)
        self.classifier = MarketClassifier(self.config)
        self.risk_analyzer = RiskAnalyzer(self.config)
        self.metadata = MetadataService()
        if self.config.metadata_csv_path:
            self.metadata.load_csv(self.config.metadata_csv_path)

    def fit(self, states: List[MarketState]) -> None:
        self.normalizer.fit(states)

    def evaluate(self, state: MarketState,
                 record_count: int | list[int] = 1,
                 signals: Optional[List[MarketSignal]] = None,
                 forecast: Optional[Forecast] = None,
                 risk: Optional[RiskProfile] = None) -> Recommendation:
        # record_count may be a list (batch) or a single int; normalise it so
        # every downstream consumer sees an integer.
        if isinstance(record_count, list):
            record_count = len(record_count)

        if risk is None:
            risk = self.risk_analyzer.assess(state)

        meta = self.metadata.get(state.item_id, state.name)
        is_vendor = meta.is_vendor_item
        vendor_price = meta.vendor_price

        # FarmScore
        farm_score, farm_dims, farm_gate_reason = self.farm_scorer.compute(state, is_vendor)

        # FlipScore
        flip_score, flip_dims = self.flip_scorer.compute(state, is_vendor, vendor_price)

        # InvestmentScore (gated by history)
        inv_score, inv_dims, inv_status = self.inv_scorer.compute(state, record_count)

        # Confidence
        conf = compute_confidence(state, forecast, record_count)

        # Stars
        stars = self._score_to_stars(farm_score)
        classification = self.classifier.classify(state)

        # Structured explanation
        explanation_lines = self._build_explanation(
            state, farm_dims, farm_gate_reason, farm_score,
            flip_score, flip_dims, inv_score, inv_status,
            conf, is_vendor, risk, signals, forecast)

        sb = {
            "farm_score": farm_score,
            "farm_gate": farm_gate_reason,
            "demand": farm_dims.get("demand", 0),
            "liquidity": farm_dims.get("liquidity", 0),
            "item_value": farm_dims.get("item_value", 0),
            "stability": farm_dims.get("stability", 0),
            "trend": farm_dims.get("trend", 0),
            "flip_score": flip_score,
            "flip_expected_sale": round(flip_dims.get("_expected_sale", 0), 0),
            "flip_cost_price": round(flip_dims.get("_cost_price", 0), 0),
            "flip_roi": flip_dims.get("_roi_raw", 0),
            "flip_profit": flip_dims.get("_profit_raw", 0),
            "flip_days": flip_dims.get("_days_to_sell", 0),
            "investment_score": inv_score if inv_score is not None else 0.0,
            "inv_status": inv_status,
            "confidence_label": conf.label,
            "confidence_score": conf.score,
            "is_vendor": int(is_vendor),
            "vendor_price": vendor_price,
        }
        rec = Recommendation(
            item_id=state.item_id,
            item_name=state.name,
            stars=stars,
            category=self._map_to_category(stars, classification),
            explanation=explanation_lines,
            farm_score=farm_score,
            investment_score=inv_score if inv_score is not None else 0.0,
            flip_score=flip_score,
            risk=risk.risk_level,
            market_classification=classification,
            key_indicators={
                "spd": state.sold_per_day,
                "sr": state.sale_rate,
                "price": state.avg_sale_price,
            },
            score_breakdown=sb,
        )
        return rec

    @staticmethod
    def _score_to_stars(score: float) -> int:
        if score >= 0.80:
            return 5
        elif score >= 0.60:
            return 4
        elif score >= 0.40:
            return 3
        elif score >= 0.20:
            return 2
        return 1

    def _build_explanation(self, state, farm_dims, gate_reason, farm_score,
                           flip_score, flip_dims, inv_score, inv_status,
                           conf, is_vendor, risk, signals, forecast):
        lines = []
        if gate_reason:
            lines.append(f"Gate: {gate_reason}")
            if farm_score == 0:
                lines.append("Item nao e farmavel (score=0)")
                return lines

        w = self.config.farm_score_weights
        lines.append(f"FarmScore={farm_score:.3f}")
        for dim in ["demand", "liquidity", "item_value", "stability", "trend"]:
            s = farm_dims.get(dim, 0)
            lines.append(f"  {dim}: {s:.3f} (peso={w[dim]:.2f}, contrib={s*w[dim]:.3f})")

        lines.append(f"Confianca: {conf.label} ({conf.score:.2f})")
        if conf.reasons:
            lines.append(f"  Motivos: {', '.join(conf.reasons)}")

        if is_vendor:
            lines.append(f"Item de Vendedor: SIM (preco={self.metadata.get(state.item_id, state.name).vendor_price}g)")

        if flip_score > 0:
            roi = flip_dims.get("_roi_raw", 0)
            profit = flip_dims.get("_profit_raw", 0)
            days = flip_dims.get("_days_to_sell", 0)
            exp_sale = flip_dims.get("_expected_sale", 0)
            cost = flip_dims.get("_cost_price", 0)
            lines.append(f"FlipScore={flip_score:.3f}")
            lines.append(f"  ROI={roi:.1%} Lucro={profit:,.0f}g Custo={cost:,.0f}g")
            lines.append(f"  Venda esperada={exp_sale:,.0f}g Tempo={days:.1f}d")

        if inv_score is not None:
            lines.append(f"InvestmentScore={inv_score:.3f}")
        else:
            lines.append(f"InvestmentScore: N/A ({inv_status})")

        if state.classification:
            lines.append(f"Mercado: {state.classification}")

        if risk.risk_factors:
            for f in risk.risk_factors[:2]:
                lines.append(f"Risco: {f}")

        if signals:
            for sig in signals:
                if sig.severity in ("high", "critical"):
                    lines.append(f"Sinal: {sig.description}")

        if forecast and forecast.forecast_available:
            lines.append(f"Previsao: {forecast.trend_direction} "
                         f"(7d: {forecast.next_7d_avg:,.0f}g)")

        return lines

    @staticmethod
    def _map_to_category(stars, classification):
        if stars >= 4:
            if "Farming" in classification:
                return "farm"
            return "opportunity"
        elif stars == 3:
            return "average"
        elif stars == 2:
            return "avoid"
        return "dead"

    def evaluate_batch(self, states: List[MarketState],
                       record_counts: Optional[List[int]] = None,
                       signals_list: Optional[List[List[MarketSignal]]] = None,
                       forecasts: Optional[List[Optional[Forecast]]] = None,
                       risks: Optional[List[RiskProfile]] = None) -> List[Recommendation]:
        self.fit(states)
        recs = []
        for i, state in enumerate(states):
            nrecs = record_counts[i] if record_counts else 1
            sigs = signals_list[i] if signals_list and i < len(signals_list) else None
            fcst = forecasts[i] if forecasts and i < len(forecasts) else None
            rsk = risks[i] if risks and i < len(risks) else None
            recs.append(self.evaluate(state, nrecs, sigs, fcst, rsk))
        recs.sort(key=lambda r: r.farm_score, reverse=True)
        return recs


class FarmAdvisor:
    def __init__(self, config: Optional[WowEconomyConfig] = None):
        self.config = config or WowEconomyConfig.default()
        self.engine = RecommendationEngine(self.config)

    def rank_items(self, states: List[MarketState],
                   record_counts: Optional[List[int]] = None,
                   signals_list: Optional[List[List[MarketSignal]]] = None,
                   forecasts: Optional[List[Optional[Forecast]]] = None,
                   risks: Optional[List[RiskProfile]] = None) -> List[Recommendation]:
        return self.engine.evaluate_batch(states, record_counts, signals_list, forecasts, risks)
