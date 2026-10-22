"""Configuration for the DfA pricing engine and its generated dataset."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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

    tsm_db: Path = Path(r"C:\FarmAnalysis\tsm_markets.db")
    dfa_db: Path = Path(r"C:\economics\woweconomy\dfa\dfa.db")
    #: the intelligence database: observed supply (Blizzard snapshots),
    #: undercut scans and our own posted stock.  It is a separate file on
    #: purpose -- the engine reads it and never writes it.
    intel_db: Path = Path(r"C:\FarmAnalysis\data\intel.sqlite")

    #: the trained models of the learning engine
    models_dir: Path = Path(r"C:\FarmAnalysis\models\dfa_ml")

    #: the per-realm price models (one folder per realm), written by
    #: `intel.py realm-ml`; used only when a realm has been trained
    realm_models_dir: Path = Path(r"C:\FarmAnalysis\models\intel")
    realm_model_region: str = "eu"

    #: the generated dataset and the addon that carries it
    dataset_path: Path = Path(
        r"C:\FarmAnalysis\DFA_Estimate\Data\DFA_Estimate_Data.lua")
    addon_dir: Path = Path(r"C:\FarmAnalysis\DFA_Estimate")
    #: the game's Interface/AddOns, where the addon copy of the dataset is
    #: deployed; the same path the learning engine already configures
    addons_dir: Path = Path(
        r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns")
    reports_dir: Path = Path(r"C:\FarmAnalysis\reports\dfa_pricing")

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
