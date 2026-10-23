"""The DfA pricing engine -- an independent estimator for TSM.

This is **not** a re-implementation of the learning engine in
:mod:`woweconomy.ml`.  That engine learns from the observed outcomes; this
one turns what it learned plus the live market state into a single,
explainable answer for one item:

    price            what a unit will really fetch
    confidence       how much that answer is worth
    sale probability the chance of selling inside the horizon
    time to sale     only when the outcome data supports it
    evidence         the numbers the answer was built from
    flags            the warnings

Nothing here posts, cancels, undercuts or reprices anything.  It answers
questions; a human (or a tool that asks for consent) acts.
"""
from __future__ import annotations

from .config import PricingConfig
from .dataset import DatasetBuilder, DatasetRecord, build_dataset, validate_dataset
from .engine import Estimate, PricingEngine
from .interests import AppearanceInterest, load_interests
from .mapping import ItemMapping, build_item_mapping

__all__ = [
    "PricingConfig",
    "DatasetBuilder",
    "DatasetRecord",
    "build_dataset",
    "validate_dataset",
    "Estimate",
    "PricingEngine",
    "AppearanceInterest",
    "load_interests",
    "ItemMapping",
    "build_item_mapping",
]
