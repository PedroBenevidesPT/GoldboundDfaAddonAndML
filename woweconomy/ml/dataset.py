"""Building the training rows.

One row per *listing*.  The features describe the market at the moment the
item was posted; the labels describe what became of it.

The labels are where most of the care goes.

**Model A -- the price.**  The target is the price a buyer actually paid
per unit, and the money the seller actually pocketed per unit.  It is
never ``dbmarket`` or ``dbregionmarketavg``: those are asking prices and
they are inputs, not answers.  The rows are the observed sales; a listing
that never sold contributes no price label, because it has none.

**Model B -- the probability of selling.**  For a horizon ``H`` a listing
either sold by ``t0 + H`` or it did not.  A listing whose outcome was
never observed is *censored*: the auction may still be on the market, or
the mail may simply not have been opened yet.  It is neither a success nor
a failure and it is dropped from that horizon's training set -- and
counted, so the report can say how much was dropped and why.

A *cancellation* is also dropped: the seller changed their mind, which
says nothing about whether anybody wanted to buy.

**Model C -- the time to sell.**  A sale has a duration; an expiry and an
unobserved listing are right-censored.  The censoring time is the moment
the account *knew* the item had not sold, which is an upper bound on the
truth -- a conservative choice that keeps the model from promising fast
sales it cannot support.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .config import AH_CONSIGNMENT_RATE, COPPER_PER_GOLD
from .demand import DemandBook
from .features import FeatureBuilder
from .linking import CANCELLED, CENSORED, EXPIRED, SALE, ListingOutcome
from .tsm_history import TSMHistory

logger = logging.getLogger(__name__)


@dataclass
class DatasetReport:
    """What the dataset builder produced and what it had to throw away."""

    rows: int = 0
    rows_with_local_reference: int = 0
    rows_with_region_reference: int = 0
    rows_without_any_reference: int = 0
    price_rows: int = 0
    horizon_rows: Dict[int, int] = field(default_factory=dict)
    horizon_censored: Dict[int, int] = field(default_factory=dict)
    horizon_cancelled: Dict[int, int] = field(default_factory=dict)
    horizon_positive: Dict[int, int] = field(default_factory=dict)
    time_to_sale_rows: int = 0
    time_to_sale_censored: int = 0
    items: int = 0
    realms: int = 0
    first_ts: int = 0
    last_ts: int = 0
    #: the rolling window the raw stores are trimmed to, in days (0 = no trim)
    retention_days: int = 0
    #: postings older than this were dropped, because their market features
    #: may no longer exist in the operational stores
    cutoff_ts: int = 0
    rows_dropped_before_cutoff: int = 0
    notes: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, object]:
        return {
            "rows": self.rows,
            "items": self.items,
            "realms": self.realms,
            "rows_with_local_reference": self.rows_with_local_reference,
            "rows_with_region_reference": self.rows_with_region_reference,
            "rows_without_any_reference": self.rows_without_any_reference,
            "price_rows": self.price_rows,
            "horizon_rows": dict(self.horizon_rows),
            "horizon_censored": dict(self.horizon_censored),
            "horizon_cancelled": dict(self.horizon_cancelled),
            "horizon_positive": dict(self.horizon_positive),
            "time_to_sale_rows": self.time_to_sale_rows,
            "time_to_sale_censored": self.time_to_sale_censored,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "retention_days": self.retention_days,
            "cutoff_ts": self.cutoff_ts,
            "rows_dropped_before_cutoff": self.rows_dropped_before_cutoff,
            "notes": list(self.notes),
        }


def _safe_log(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0 or not math.isfinite(v):
        return None
    return math.log(v)


def build_dataset(outcomes: Sequence[ListingOutcome],
                  builder: FeatureBuilder,
                  horizons_h: Sequence[int],
                  observation_end: int) -> Tuple[pd.DataFrame, DatasetReport]:
    """Turn linked listings into a feature/label table."""
    report = DatasetReport()
    records: List[Dict[str, object]] = []

    for out in outcomes:
        post = out.posting
        ts = post.time
        if post.item_id is None:
            continue
        row = builder.build(post, ts)
        record: Dict[str, object] = {}
        record.update(row.features)
        for key, value in row.categorical.items():
            record[f"cat_{key}"] = value
        record["item_id"] = post.item_id
        record["realm"] = post.realm
        record["item_name"] = post.item_name
        record["ts"] = ts
        record["date"] = ts // 86400
        record["outcome"] = out.outcome
        record["elapsed_h"] = out.elapsed / 3600.0
        record["inferred_duration_h"] = out.inferred_duration_h
        record["ask_unit"] = float(post.unit_buyout)
        record["qty"] = float(post.qty)
        record["ambiguous"] = float(out.ambiguous)
        #: whether the *specific* listing behind a sale is identified.
        #: ``price_exact`` says the total matched one outstanding posting;
        #: ``price_ambiguous`` says two or more postings shared that total,
        #: so the price is right but the elapsed time is attached to an
        #: arbitrary one of them.
        record["price_exact"] = float(out.price_exact)
        record["price_ambiguous"] = float(out.price_ambiguous)

        # ---- Model A: the price -----------------------------------------
        if out.outcome == SALE:
            gross = out.gross_unit
            net = out.net_unit
            record["y_gross_unit"] = float(gross)
            record["y_net_unit"] = float(net)
            record["y_net_unit_log"] = _safe_log(net)
            record["y_gross_unit_log"] = _safe_log(gross)
            record["y_net_gold"] = net / COPPER_PER_GOLD
            record["fee_ratio_observed"] = (
                out.consignment / float(out.posting.unit_buyout * post.qty)
                if post.unit_buyout and post.qty else None)
        else:
            record["y_gross_unit"] = None
            record["y_net_unit"] = None
            record["y_net_unit_log"] = None
            record["y_gross_unit_log"] = None
            record["y_net_gold"] = None
            record["fee_ratio_observed"] = None

        # ---- Model B: did it sell within the horizon? --------------------
        for horizon in horizons_h:
            limit = ts + horizon * 3600
            label: Optional[int] = None
            known = False
            if out.outcome == SALE:
                label = 1 if out.outcome_time <= limit else 0
                known = True
            elif out.outcome == EXPIRED:
                # it expired without selling, so it certainly did not sell
                # inside the horizon
                label = 0
                known = True
            elif out.outcome == CANCELLED:
                label = None
                known = False
            else:  # censored
                label = None
                known = False
            record[f"sold_within_{horizon}h"] = label
            record[f"label_known_{horizon}h"] = int(known)

        # ---- Model C: time to sale, with censoring -----------------------
        if out.outcome == SALE:
            record["tts_hours"] = out.elapsed / 3600.0
            record["tts_censored"] = 0
        elif out.outcome == EXPIRED:
            record["tts_hours"] = out.elapsed / 3600.0
            record["tts_censored"] = 1
        elif out.outcome == CENSORED:
            record["tts_hours"] = max(0.0, (observation_end - ts) / 3600.0)
            record["tts_censored"] = 1
        else:  # cancelled: the seller withdrew it, no information
            record["tts_hours"] = None
            record["tts_censored"] = 1

        records.append(record)

    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        report.notes.append("no rows could be built")
        return frame, report

    report.rows = len(frame)
    report.items = int(frame["item_id"].nunique())
    report.realms = int(frame["realm"].nunique())
    report.first_ts = int(frame["ts"].min())
    report.last_ts = int(frame["ts"].max())
    report.rows_with_local_reference = int((frame.get("local_present", pd.Series(dtype=float)) == 1).sum())
    report.rows_with_region_reference = int((frame.get("region_present", pd.Series(dtype=float)) == 1).sum())
    report.rows_without_any_reference = int(
        ((frame.get("local_present", pd.Series(0, index=frame.index)) == 0)
         & (frame.get("region_present", pd.Series(0, index=frame.index)) == 0)).sum())

    report.price_rows = int(frame["y_net_unit_log"].notna().sum())
    for horizon in horizons_h:
        known = frame[f"label_known_{horizon}h"] == 1
        report.horizon_rows[horizon] = int(known.sum())
        report.horizon_censored[horizon] = int(
            ((frame["outcome"] == CENSORED) & ~known).sum())
        report.horizon_cancelled[horizon] = int(
            ((frame["outcome"] == CANCELLED) & ~known).sum())
        report.horizon_positive[horizon] = int(
            frame.loc[known, f"sold_within_{horizon}h"].fillna(0).sum())

    tts = frame["tts_hours"].notna()
    report.time_to_sale_rows = int(tts.sum())
    report.time_to_sale_censored = int((frame.loc[tts, "tts_censored"] == 1).sum())

    return frame, report
