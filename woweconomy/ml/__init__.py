"""The DfA CatBoost learning engine.

Four models, four different jobs, four different kinds of honesty:

``Model A`` -- the **realizable price**.  Trained on what buyers actually
paid, never on ``dbmarket`` or ``dbregionmarketavg``, which are asking
prices and are inputs here.

``Model B`` -- the **probability of selling** inside 1, 3, 7 or 14 days.
Censored listings and seller cancellations are not failures.

``Model C`` -- the **time to sell**, with right censoring, fitted only
when the outcome linkage can be trusted.

``Model D`` -- the **listing-price strategy**, which is the decision layer
on top of the three, not another model.
"""
from __future__ import annotations

from .config import MLConfig
from .dataset import build_dataset
from .demand import DemandBook, load_demand, load_realm_groups
from .features import FeatureBuilder
from .journalator import JournalArchive, build_archive
from .linking import ListingOutcome, link_postings
from .models import (
    CATBOOST_AVAILABLE,
    PriceModel,
    SaleProbabilityModel,
    TimeToSaleModel,
)
from .pipeline import DfaLearningEngine, PipelineResult
from .strategy import ListingStrategy, StrategyConfig
from .tsm_history import TSMHistory
from .validate import make_split

__all__ = [
    "MLConfig",
    "JournalArchive",
    "build_archive",
    "ListingOutcome",
    "link_postings",
    "TSMHistory",
    "DemandBook",
    "load_demand",
    "load_realm_groups",
    "FeatureBuilder",
    "build_dataset",
    "PriceModel",
    "SaleProbabilityModel",
    "TimeToSaleModel",
    "CATBOOST_AVAILABLE",
    "ListingStrategy",
    "StrategyConfig",
    "DfaLearningEngine",
    "PipelineResult",
    "make_split",
]
