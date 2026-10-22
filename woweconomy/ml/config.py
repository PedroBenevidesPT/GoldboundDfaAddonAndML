"""Configuration for the DfA CatBoost learning engine.

Everything the engine needs to find its inputs lives here, so the whole
pipeline can be pointed at a different machine without touching code.

The defaults describe *this* installation:

* the TSM mirror in the project root (``tsm_markets.db``), which holds the
  snapshot history of every realm and of the region;
* the Data for Azeroth scan database (``C:\\economics\\woweconomy\\dfa\\dfa.db``),
  which holds the demand counters;
* the Journalator saved variables, which hold the account's own transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

#: TSM writes this when it has no data.  It is not a price.
SENTINEL_COPPER = 99_999_999_000

COPPER_PER_GOLD = 10_000

#: The auction house consignment cut.  Verified against the Journalator
#: invoices: 5190 copper on a 103800 copper sale.
AH_CONSIGNMENT_RATE = 0.05

#: WoW auction durations.  The Journalator posting record does not carry the
#: duration, but the *expiry* delays cluster on these three values, so they
#: are used to infer it (see :mod:`woweconomy.ml.linking`).
AUCTION_DURATIONS_H = (12, 24, 48)

#: The horizons Model B is allowed to claim, in hours.  48 is included
#: because it is a real auction duration and the Sentinel export quotes
#: 24/48/168; 72 and 336 remain for the original walk-forward report.
DEFAULT_HORIZONS_H: Tuple[int, ...] = (24, 48, 72, 168, 336)


@dataclass
class MLConfig:
    """Paths and knobs for the learning engine."""

    # --- inputs -------------------------------------------------------
    tsm_db: Path = Path(r"C:\FarmAnalysis\tsm_markets.db")
    dfa_db: Path = Path(r"C:\economics\woweconomy\dfa\dfa.db")
    journalator_lua: Path = Path(
        r"C:\Program Files (x86)\World of Warcraft\_retail_\WTF\Account"
        r"\1186541918#1\SavedVariables\Journalator.lua")
    addons_dir: Path = Path(
        r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns")

    # --- outputs ------------------------------------------------------
    models_dir: Path = Path(r"C:\FarmAnalysis\models\dfa_ml")
    reports_dir: Path = Path(r"C:\FarmAnalysis\reports\dfa_ml")
    cache_dir: Path = Path(r"C:\FarmAnalysis\data\ml")

    # --- behaviour ----------------------------------------------------
    horizons_h: Tuple[int, ...] = DEFAULT_HORIZONS_H
    #: A TSM reference is only trusted when it is at most this old.  Beyond
    #: it the reference is stale and the freshness feature says so.
    reference_freshness_cap_h: float = 72.0
    #: Minimum number of observed sales in the training window before Model A
    #: is considered trainable at all.
    min_sales_for_price_model: int = 40
    #: Random seed.  Every split and every model uses it.
    seed: int = 20261019
    #: Which availability clock decides when a TSM snapshot became usable.
    #:   "updated" -> the TSM data's own timestamp (the market truth);
    #:   "fetched"  -> when this project downloaded it (the pessimistic view).
    availability_clock: str = "updated"
    #: Reuse the DfA demand counters as a feature.  They are a live snapshot
    #: with no timestamp, so they are not point-in-time; the audit reports
    #: what including them does to the walk-forward scores.
    use_dfa_demand: bool = True
    #: How many trees the models get.  A small number makes a run quick
    #: enough to iterate on; it is only for wiring checks.
    iterations: int = 300
    #: The price model is the one that gains from more trees: on the
    #: walk-forward it went from rmse_log 0.94 at 300 to 0.93 at 600, while
    #: the classifier did not improve (auc 0.947 either way) and the
    #: survival model is limited by its rows, not by its iterations.
    price_iterations: int = 600

    extra: dict = field(default_factory=dict)

    def ensure_dirs(self) -> None:
        for path in (self.models_dir, self.reports_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)
