"""Configuration for the DfA pricing engine and its generated dataset."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..dbcompat import PROJECT_ROOT, project_path as _path

#: The schema version of the generated dataset.  The in-game loader
#: refuses a version it does not understand, which is what stops an old
#: dataset from being read as a new one.
DATASET_VERSION = 2

#: The version of the engine that produced the dataset.
ENGINE_VERSION = "2.0.0"

COPPER_PER_GOLD = 10_000

#: The house cut on a sale.
AH_CONSIGNMENT_RATE = 0.05


@dataclass
class PricingConfig:
    """Where the engine reads from and where it writes."""

    tsm_db: Path = field(
        default_factory=lambda: _path("WOW_TSM_DB", "tsm_markets.db"))
    #: the scan database.  It lives outside the repo on this workstation,
    #: which is why it keeps an absolute default and an override rather
    #: than being resolved against the project root.
    dfa_db: Path = field(default_factory=lambda: _path(
        "WOW_DFA_DB", str(Path(r"C:\economics\woweconomy\dfa\dfa.db"))))
    #: the intelligence database: observed supply (Blizzard snapshots),
    #: undercut scans and our own posted stock.  It is a separate file on
    #: purpose -- the engine reads it and never writes it.
    intel_db: Path = field(
        default_factory=lambda: _path("WOW_INTEL_DB", "data/intel.sqlite"))

    #: the trained models of the learning engine
    models_dir: Path = field(
        default_factory=lambda: _path("WOW_MODELS_DIR", "models/dfa_ml"))

    #: the per-realm price models (one folder per realm), written by
    #: `intel.py realm-ml`; used only when a realm has been trained
    realm_models_dir: Path = field(
        default_factory=lambda: _path("WOW_REALM_MODELS_DIR", "models/intel"))
    realm_model_region: str = "eu"

    #: the generated dataset and the addon that carries it
    dataset_path: Path = field(default_factory=lambda: _path(
        "WOW_DATASET_PATH", "DFA_Estimate/Data/DFA_Estimate_Data.lua"))
    addon_dir: Path = field(
        default_factory=lambda: _path("WOW_ADDON_DIR", "DFA_Estimate"))
    #: the game's Interface/AddOns, where the addon copy of the dataset is
    #: deployed; the same path the learning engine already configures
    addons_dir: Path = field(default_factory=lambda: _path(
        "WOW_ADDONS_DIR",
        r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns"))
    reports_dir: Path = field(
        default_factory=lambda: _path("WOW_REPORTS_DIR", "reports/dfa_pricing"))

    #: the horizon the sale probability is quoted for, in hours
    horizon_h: int = 24

    #: an estimate older than this is stale and says so
    max_age_hours: float = 24.0 * 14

    #: a sample below this is thin and says so
    min_sample_size: int = 3

    #: the confidence a single observation is worth, on its own
    base_confidence: float = 0.25

    #: the number of observations that make an estimate as good as the
    #: model's own typical accuracy
    confidence_reference_n: int = 8

    extra: dict = field(default_factory=dict)

    def ensure_dirs(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.dataset_path.parent.mkdir(parents=True, exist_ok=True)
