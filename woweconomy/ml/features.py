"""Point-in-time feature assembly.

Every feature in this module is computed from a state that was already
true at the prediction timestamp, and every feature says where it came
from.  Two rules drive the code:

**A reference is only a reference.**  ``dbmarket`` and
``dbregionmarketavg`` are asking prices, not what a buyer paid.  They
enter the model as *features* and never as labels, and the ratios between
the reference families are the features that let CatBoost work out when
an asking price is inflated and when a low sale average is genuine.

**Missing is not zero.**  TSM writes a sentinel when it has no data; a
reference that is absent gets a missing indicator rather than a value that
would read as "free".  The freshness of each reference is a feature too:
a market average from three days ago is a different kind of evidence from
one from an hour ago.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config import SENTINEL_COPPER
from .demand import DemandBook
from .journalator import Posting
from .tsm_history import TSMHistory

logger = logging.getLogger(__name__)

#: Features that describe the listing itself.  They are excluded from the
#: price model, which has to answer "what will the market pay" without
#: being told the ask.  Every feature derived from the ask belongs here;
#: leaving one out would let the ask back in through the back door.
ASK_FEATURES = ("ask_unit_log", "ask_over_local_market", "ask_over_region_sale_avg",
                "ask_over_region_market", "ask_over_tsm_normal", "bid_to_buyout",
                "unit_bid_log", "deposit_ratio")

#: What each feature is, where it comes from, and whether it was true at
#: the moment the listing was made.  Rendered into the data-readiness
#: report so that no number in a model is a mystery.
FEATURE_DOCS: Dict[str, Tuple[str, str, str]] = {
    # -- the listing itself ---------------------------------------------
    "qty": ("the stack size", "Journalator posting record", "yes"),
    "qty_log": ("log of the stack size", "Journalator posting record", "yes"),
    "ask_unit_log": ("log of the per-unit buyout asked for",
                     "Journalator posting record", "yes"),
    "unit_bid_log": ("log of the per-unit opening bid",
                     "Journalator posting record (empty in this archive)", "yes"),
    "bid_to_buyout": ("the bid as a fraction of the buyout",
                      "Journalator posting record (empty in this archive)", "yes"),
    "deposit_ratio": ("the deposit as a fraction of the asked total",
                      "Journalator posting record", "yes"),
    "deposit_log": ("log of the deposit; the deposit is 15 % of the item's "
                    "vendor price, so it carries the item's value without "
                    "carrying the ask",
                    "Journalator posting record", "yes"),
    "is_stack": ("whether the listing is a stack", "derived", "yes"),
    "hour_of_day": ("the hour of the posting, UTC", "derived", "yes"),
    "day_of_week": ("the day of the posting", "derived", "yes"),
    # -- the local references -------------------------------------------
    "local_market_log": ("log of dbmarket, the local market average",
                         "tsm_price_change:realm_items", "yes"),
    "local_min_buyout_log": ("log of dbminbuyout, the cheapest buyout listing",
                              "tsm_price_change:realm_items", "yes"),
    "local_recent_log": ("log of dbrecent, the decayed recent average",
                         "tsm_price_change:realm_items", "yes"),
    "local_historical_log": ("log of dbhistorical, the long-run average",
                              "tsm_price_change:realm_items", "yes"),
    "local_present": ("whether a local reference existed at that moment",
                      "derived", "yes"),
    "local_age_h": ("hours since that local snapshot", "derived", "yes"),
    "local_stale": ("whether the local reference is older than the freshness cap",
                    "derived", "yes"),
    "local_has_snapshot": ("whether any local snapshot covered that moment",
                           "derived", "yes"),
    # -- the regional references ----------------------------------------
    "region_market_log": ("log of dbregionmarketavg",
                          "tsm_price_change:region_items", "yes"),
    "region_historical_log": ("log of dbregionhistorical",
                              "tsm_price_change:region_items", "yes"),
    "region_sale_avg_log": ("log of dbregionsaleavg, the average a sale closed at",
                            "tsm_price_change:region_items", "yes"),
    "region_sold_per_day": ("dbregionsoldperday, the estimated daily volume",
                            "tsm_price_change:region_items", "yes"),
    "region_sale_rate": ("dbregionsalerate, the estimated sale frequency",
                         "tsm_price_change:region_items", "yes"),
    "region_present": ("whether a regional reference existed at that moment",
                       "derived", "yes"),
    "region_age_h": ("hours since that regional snapshot", "derived", "yes"),
    "region_stale": ("whether the regional reference is older than the cap",
                     "derived", "yes"),
    "region_has_snapshot": ("whether any regional snapshot covered that moment",
                            "derived", "yes"),
    # -- the ratios between the families ---------------------------------
    "region_sale_avg_over_market": ("the transaction family over the asking "
                                    "family: how far the asking price runs ahead "
                                    "of what buyers pay",
                                    "derived", "yes"),
    "region_market_over_historical": ("the regional asking price over its own history",
                                      "derived", "yes"),
    "region_sale_avg_over_historical": ("the regional sale average over its own history",
                                        "derived", "yes"),
    "local_market_over_historical": ("the local asking price over its own history",
                                     "derived", "yes"),
    "local_recent_over_market": ("the local recent average over the local market average",
                                 "derived", "yes"),
    "local_recent_over_region_sale_avg": ("the local recent average over the regional "
                                          "sale average", "derived", "yes"),
    "ask_over_local_market": ("the ask over the local market average",
                              "derived", "yes"),
    "ask_over_region_sale_avg": ("the ask over the regional sale average",
                                  "derived", "yes"),
    "ask_over_region_market": ("the ask over the regional market average",
                               "derived", "yes"),
    "spread_market_vs_sale": ("the gap between the regional asking and sale averages",
                              "derived", "yes"),
    "spread_local_vs_region": ("the gap between the local and regional asking prices",
                               "derived", "yes"),
    "ref_dispersion": ("the relative spread of every reference that existed",
                       "derived", "yes"),
    "ref_count": ("how many references existed at all", "derived", "yes"),
    # -- the TSM auctioning strings -------------------------------------
    "tsm_normalmarket_log": ("log of the wall: the local ask capped by the regional "
                              "one and floored at 85 % of it",
                              "woweconomy.pricing.tsm_strings", "yes"),
    "tsm_normal_log": ("log of the Normal level, after the sale-rate discount",
                       "woweconomy.pricing.tsm_strings", "yes"),
    "tsm_normal_over_region_market": ("the Normal level against the regional ask: "
                                       "how far the policy sits from the market",
                                       "derived", "yes"),
    "tsm_maximum_over_normal": ("the width of the posting band", "derived", "yes"),
    "ask_over_tsm_normal": ("the ask over the Normal level", "derived", "yes"),
    # -- the changes over time ------------------------------------------
    "region_market_chg": ("the change in the regional asking price since the "
                          "previous snapshot", "derived", "yes"),
    "region_sale_avg_chg": ("the change in the regional sale average since the "
                            "previous snapshot", "derived", "yes"),
    "local_market_chg": ("the change in the local asking price since the previous "
                         "snapshot", "derived", "yes"),
    # -- the demand --------------------------------------------------------
    "dfa_appearances_realm_rate": ("the share of the realm's collectors that still "
                                   "lack the transmog",
                                   "dfa.db::missing_realm", "**no** -- a live aggregate"),
    "dfa_appearances_global_rate": ("the share of all collectors that lack the transmog",
                                     "dfa.db::missing", "**no** -- a live aggregate"),
    "dfa_appearances_realm_missing": ("how many collectors of the realm lack it",
                                      "dfa.db::missing_realm", "**no** -- a live aggregate"),
    "dfa_appearances_known": ("whether the transmog demand is known at all",
                               "derived", "**no**"),
    "dfa_mounts_realm_rate": ("the same for mounts", "dfa.db::missing_realm",
                               "**no** -- a live aggregate"),
    "dfa_mounts_global_rate": ("the same for mounts", "dfa.db::missing",
                                "**no** -- a live aggregate"),
    "dfa_mounts_realm_missing": ("the same for mounts", "dfa.db::missing_realm",
                                 "**no** -- a live aggregate"),
    "dfa_mounts_known": ("whether the mount demand is known", "derived", "**no**"),
    "dfa_recipes_realm_rate": ("the same for recipes", "dfa.db::missing_realm",
                                "**no** -- a live aggregate"),
    "dfa_recipes_global_rate": ("the same for recipes", "dfa.db::missing",
                                 "**no** -- a live aggregate"),
    "dfa_recipes_realm_missing": ("the same for recipes", "dfa.db::missing_realm",
                                   "**no** -- a live aggregate"),
    "dfa_recipes_known": ("whether the recipe demand is known", "derived", "**no**"),
    "dfa_known": ("whether any demand counter applies to the item", "derived", "**no**"),
}


def _clean(value: Optional[float]) -> Optional[float]:
    """Turn a TSM value into a usable number, or ``None``."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v >= SENTINEL_COPPER:
        return None
    if not math.isfinite(v):
        return None
    return v


def _log1p(value: Optional[float]) -> Optional[float]:
    v = _clean(value)
    if v is None:
        return None
    return math.log1p(v)


def _ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    n, d = _clean(numerator), _clean(denominator)
    if n is None or d is None or d == 0:
        return None
    r = n / d
    if not math.isfinite(r) or r > 1e6:
        return None
    return r


def _ratio_log(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    n, d = _clean(numerator), _clean(denominator)
    if n is None or d is None or d == 0:
        return None
    r = math.log(n / d)
    if not math.isfinite(r):
        return None
    return max(-20.0, min(20.0, r))


def _spread(values: Sequence[Optional[float]]) -> Optional[float]:
    """Relative dispersion of the references that exist."""
    vals = sorted(v for v in (_clean(x) for x in values) if v is not None)
    if len(vals) < 2:
        return None
    lo, hi = vals[0], vals[-1]
    mid = _median(vals)
    if mid <= 0:
        return None
    return (hi - lo) / mid


def _median(sorted_values: Sequence[float]) -> float:
    """The median, averaging the two middle values when there are even counts."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n % 2:
        return float(sorted_values[n // 2])
    return (float(sorted_values[n // 2 - 1]) + float(sorted_values[n // 2])) / 2.0


@dataclass
class FeatureRow:
    """One prediction row: the features, plus where they came from."""

    features: Dict[str, Optional[float]] = field(default_factory=dict)
    categorical: Dict[str, str] = field(default_factory=dict)
    provenance: Dict[str, str] = field(default_factory=dict)

    def get(self, name: str, default: Optional[float] = None) -> Optional[float]:
        value = self.features.get(name)
        return default if value is None else value


class FeatureBuilder:
    """Builds the feature row of a listing at the moment it was listed."""

    def __init__(self, history: TSMHistory,
                 demand: Optional[DemandBook] = None,
                 realm_group: Optional[Dict[str, List[str]]] = None,
                 freshness_cap_h: float = 72.0) -> None:
        self.history = history
        self.demand = demand
        self.freshness_cap_h = freshness_cap_h
        #: realm -> the realms that share its auction house (DFA side)
        self.realm_group = realm_group or {}
        self._cache: Dict[Tuple[str, str, int], Dict[int, tuple]] = {}

    # -- helpers ---------------------------------------------------------
    def _realm_state(self, scope: str, kind: str, ts: int) -> Dict[int, tuple]:
        key = (scope, kind, ts)
        hit = self._cache.get(key)
        if hit is None:
            hit = self.history.state_at(scope, kind, ts)
            self._cache[key] = hit
        return hit

    def _previous_state(self, scope: str, kind: str, ts: int) -> Dict[int, tuple]:
        """The state at the snapshot before the one in force at ``ts``."""
        times = self.history.snapshot_times(scope, kind)
        if not times:
            return {}
        current = self.history.snapshot_time_at(scope, kind, ts)
        earlier = [t for t in times if current and t < current]
        if not earlier:
            return {}
        return self.history.state_at(scope, kind, earlier[-1])

    # -- the row ---------------------------------------------------------
    def build(self, posting: Posting, ts: int) -> FeatureRow:
        row = FeatureRow()
        f = row.features
        realm = posting.realm
        scope = self.history.canonical_scope(realm)
        canon_name, group_name, group_id = self.history.realm_group(realm)

        local = self._realm_state(scope, "realm_items", ts) if scope else {}
        local_prev = self._previous_state(scope, "realm_items", ts) if scope else {}
        region = self._realm_state("region", "region_items", ts)
        region_prev = self._previous_state("region", "region_items", ts)

        item_id = posting.item_id
        lrow = local.get(item_id) if item_id is not None else None
        lrow_prev = local_prev.get(item_id) if item_id is not None else None
        rrow = region.get(item_id) if item_id is not None else None
        rrow_prev = region_prev.get(item_id) if item_id is not None else None

        # --- local references -------------------------------------------
        local_market = _clean(lrow[0]) if lrow else None
        local_min_buyout = _clean(lrow[1]) if lrow else None
        local_recent = _clean(lrow[2]) if lrow else None
        local_historical = _clean(lrow[3]) if lrow else None
        local_market_prev = _clean(lrow_prev[0]) if lrow_prev else None

        f["local_market_log"] = _log1p(local_market)
        f["local_min_buyout_log"] = _log1p(local_min_buyout)
        f["local_recent_log"] = _log1p(local_recent)
        f["local_historical_log"] = _log1p(local_historical)
        f["local_present"] = 1.0 if lrow else 0.0
        row.provenance["local"] = f"tsm_price_change:realm_items:{scope}"

        # --- regional references ----------------------------------------
        region_market = _clean(rrow[0]) if rrow else None
        region_historical = _clean(rrow[3]) if rrow else None
        region_sale_avg = _clean(rrow[4]) if rrow else None
        region_sale_rate = _clean(rrow[5]) if rrow else None
        region_sold_per_day = _clean(rrow[6]) if rrow else None
        region_market_prev = _clean(rrow_prev[0]) if rrow_prev else None
        region_sale_avg_prev = _clean(rrow_prev[4]) if rrow_prev else None

        f["region_market_log"] = _log1p(region_market)
        f["region_historical_log"] = _log1p(region_historical)
        f["region_sale_avg_log"] = _log1p(region_sale_avg)
        f["region_sale_rate"] = region_sale_rate
        f["region_sold_per_day"] = region_sold_per_day
        f["region_present"] = 1.0 if rrow else 0.0
        row.provenance["region"] = "tsm_price_change:region_items:region"

        # --- freshness ---------------------------------------------------
        local_snap_ts = self.history.snapshot_time_at(scope, "realm_items", ts) if scope else 0
        region_snap_ts = self.history.snapshot_time_at("region", "region_items", ts)
        local_age = (ts - local_snap_ts) / 3600.0 if local_snap_ts else None
        region_age = (ts - region_snap_ts) / 3600.0 if region_snap_ts else None
        f["local_age_h"] = local_age
        f["region_age_h"] = region_age
        f["local_stale"] = 1.0 if (local_age is not None
                                  and local_age > self.freshness_cap_h) else 0.0
        f["region_stale"] = 1.0 if (region_age is not None
                                    and region_age > self.freshness_cap_h) else 0.0
        f["local_has_snapshot"] = 1.0 if local_snap_ts else 0.0
        f["region_has_snapshot"] = 1.0 if region_snap_ts else 0.0

        # --- the listing --------------------------------------------------
        ask = float(posting.unit_buyout) if posting.unit_buyout > 0 else None
        f["qty"] = float(posting.qty)
        f["qty_log"] = math.log1p(posting.qty)
        f["ask_unit_log"] = _log1p(ask)
        f["unit_bid_log"] = _log1p(posting.unit_bid)
        f["bid_to_buyout"] = _ratio(posting.unit_bid, posting.unit_buyout)
        f["deposit_ratio"] = _ratio(posting.deposit,
                                    posting.unit_buyout * posting.qty) if ask else None
        # the deposit is 15 % of the item's vendor price, capped: it is a
        # property of the *item*, not of the ask, and it is the only value
        # signal the account's own record carries
        f["deposit_log"] = _log1p(posting.deposit)
        f["hour_of_day"] = float((ts // 3600) % 24)
        f["day_of_week"] = float(((ts // 86400) + 4) % 7)  # 1970-01-01 was a Thursday
        f["is_stack"] = 1.0 if posting.qty > 1 else 0.0

        # --- the auctioning policy, evaluated ------------------------------
        # The three TSM strings are the listing policy the player actually
        # follows.  They are computed from references only, so they are
        # legitimate features: the ask itself never enters them.
        from ..pricing.tsm_strings import levels_for
        policy = levels_for(local_market, region_market, region_sale_rate)
        f["tsm_normalmarket_log"] = _log1p(policy.normalmarket)
        f["tsm_normal_log"] = _log1p(policy.normal)
        f["tsm_normal_over_region_market"] = _ratio(policy.normal, region_market)
        f["tsm_maximum_over_normal"] = _ratio(policy.maximum, policy.normal)
        row.provenance["tsm_strings"] = "woweconomy.pricing.tsm_strings"

        # --- ratios between the reference families ------------------------
        # The heart of the "speculative reference" problem: how far the
        # asking families sit above the transaction family.
        f["region_sale_avg_over_market"] = _ratio(region_sale_avg, region_market)
        f["region_market_over_historical"] = _ratio(region_market, region_historical)
        f["region_sale_avg_over_historical"] = _ratio(region_sale_avg, region_historical)
        f["local_market_over_historical"] = _ratio(local_market, local_historical)
        f["local_recent_over_market"] = _ratio(local_recent, local_market)
        f["local_recent_over_region_sale_avg"] = _ratio(local_recent, region_sale_avg)
        f["ask_over_local_market"] = _ratio(ask, local_market)
        f["ask_over_region_sale_avg"] = _ratio(ask, region_sale_avg)
        f["ask_over_region_market"] = _ratio(ask, region_market)
        f["ask_over_tsm_normal"] = _ratio(ask, policy.normal)
        f["spread_market_vs_sale"] = (
            (region_market - region_sale_avg) / region_market
            if region_market and region_sale_avg else None)
        f["spread_local_vs_region"] = (
            (local_market - region_market) / region_market
            if local_market and region_market else None)
        f["ref_dispersion"] = _spread([region_market, region_historical, region_sale_avg,
                                       local_market, local_recent, local_historical])
        f["ref_count"] = float(sum(1 for v in (region_market, region_historical,
                                               region_sale_avg, local_market,
                                               local_recent, local_historical)
                                   if v is not None))

        # --- change over the previous snapshot ----------------------------
        f["region_market_chg"] = (
            (region_market - region_market_prev) / region_market_prev
            if region_market and region_market_prev else None)
        f["region_sale_avg_chg"] = (
            (region_sale_avg - region_sale_avg_prev) / region_sale_avg_prev
            if region_sale_avg and region_sale_avg_prev else None)
        f["local_market_chg"] = (
            (local_market - local_market_prev) / local_market_prev
            if local_market and local_market_prev else None)

        # --- DfA demand ----------------------------------------------------
        if self.demand is not None and self.demand.available and item_id is not None:
            for kind in ("appearances", "mounts", "recipes"):
                sample = self._demand_for(kind, item_id, realm)
                f[f"dfa_{kind}_realm_rate"] = sample.realm_rate or None
                f[f"dfa_{kind}_global_rate"] = sample.global_rate or None
                f[f"dfa_{kind}_realm_missing"] = (
                    float(sample.realm_missing) if sample.realm_missing else None)
                f[f"dfa_{kind}_known"] = 1.0 if (sample.realm_missing
                                                or sample.global_missing) else 0.0
            f["dfa_known"] = 1.0
            row.provenance["dfa"] = self.demand.source
        else:
            for kind in ("appearances", "mounts", "recipes"):
                f[f"dfa_{kind}_known"] = 0.0
            f["dfa_known"] = 0.0

        # --- identity ------------------------------------------------------
        row.categorical["item_id"] = str(item_id) if item_id is not None else "unknown"
        row.categorical["realm"] = realm or "unknown"
        row.categorical["realm_group"] = str(group_name or "unknown")
        row.categorical["item_name_bucket"] = _name_bucket(posting.item_name)

        # --- the raw references -------------------------------------------
        # Kept in copper as well as in logs: the credibility analysis has
        # to compare a reference with a realised price, and comparing a
        # logarithm with a logarithm of a different kind of thing is how
        # a report ends up claiming a reference is 100000 times too small.
        for name, value in (("ref_local_market", local_market),
                            ("ref_local_min_buyout", local_min_buyout),
                            ("ref_local_recent", local_recent),
                            ("ref_local_historical", local_historical),
                            ("ref_region_market", region_market),
                            ("ref_region_historical", region_historical),
                            ("ref_region_sale_avg", region_sale_avg)):
            f[name] = value

        return row

    def _demand_for(self, kind: str, item_id: int, realm: str):
        """Demand for one item, aggregated over the realm's auction house.

        Realms of one connected group share a single auction house, so the
        buyers of a Dragonmaw listing live in Dragonmaw *and* Stormreaver.
        The counters are sliced per realm, so they have to be added over
        the whole group -- otherwise the feature would answer a question
        nobody asked.
        """
        from .demand import DemandSample
        group = self.realm_group.get(realm, [realm])
        total = DemandSample(source="dfa.db::missing_realm")
        found = False
        for member in group:
            for appearance in self.demand.appearances.get(int(item_id), ()):
                sample = self.demand.get(kind, appearance, member)
                if not (sample.realm_missing or sample.global_missing
                        or sample.in_catalogue):
                    continue
                found = True
                total.realm_missing += sample.realm_missing
                total.realm_sample += sample.realm_sample
                total.global_missing += sample.global_missing
                total.global_sample += sample.global_sample
                total.weight = max(total.weight, sample.weight)
                total.in_catalogue = True
        if not found:
            return DemandSample(source="dfa.db (sem linha)")
        return total


def _name_bucket(name: str) -> str:
    """A coarse family for the item, so the model can share strength.

    Two hundred distinct item ids with a dozen rows each teach a tree
    model nothing; the family ('herb', 'ore', 'crafted', ...) at least
    carries a signal about how the item behaves.
    """
    n = (name or "").lower()
    if not n:
        return "unknown"
    families = (
        ("plans:", "plan"), ("recipe:", "recipe"), ("schematic:", "schematic"),
        ("pattern:", "pattern"), ("design:", "design"),
        ("herb", "herb"), ("root", "herb"), ("moss", "herb"), ("vine", "herb"),
        ("lily", "herb"), ("petal", "herb"), ("flower", "herb"), ("seed", "herb"),
        ("ore", "ore"), ("oreand", "ore"), ("bar", "bar"), ("ingot", "bar"),
        ("hide", "hide"), ("leather", "hide"), ("pelt", "hide"),
        ("spirit", "spirit"), ("essence", "spirit"), ("shard", "shard"),
        ("essence", "spirit"),
        ("meat", "meat"), ("flesh", "meat"), ("fish", "meat"),
        ("cloth", "cloth"), ("silk", "cloth"), ("wool", "cloth"), ("linen", "cloth"),
        ("essence", "spirit"),
    )
    for needle, family in families:
        if needle in n:
            return family
    return "other"
