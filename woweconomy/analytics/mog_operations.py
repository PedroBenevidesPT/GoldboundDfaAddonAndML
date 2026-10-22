"""Turn the mog groups into a TSM export string: groups + auctioning operations.

The item lists come from ``reports/mog_groups/*.txt``; the price policies are the
ones in ``reports/mog_groups.md`` (section D).  The result is one AceSerializer
line that the TSM addon imports as named groups with an Auctioning Operation
already attached to each.

The Auctioning operation field set mirrors a real TSM export
(``operations.Auctioning.<name>``) so the addon reads it back without missing
keys.  Only the price strings, duration, undercut and max-expires change per
group; the rest are the conservative values used by published transmog
operations: post cap 1 (one auction per item; 0 for the refusal groups), no
automatic cancel/repost, "post at max price" when competition is below minimum
or above maximum.

Prices are anchored on the **regional market value** (``dbregionmarketavg`` →
``dbregionhistorical`` → the realm 60-day ``dbhistorical``), never on the realm
``dbmarket``/``dbminbuyout`` (on a realm with no competition your own auction
*is* the realm market, so it freezes the price at whatever was posted once), and
never on ``dbregionsaleavg`` (a region-wide *sale* average, contaminated by
snipes).

Pricing therefore has two independent dimensions:

* the **band** (``BAND_MULTIPLIERS``) -- how far below the reference a listing
  sits, estimated per price band;
* the **realm** (``mog_realm.RealmProfile``) -- which reference that is (the
  measured ``factor``: local market / regional market) and the posture
  (multiplier, duration, holding window) derived from the measured competition
  depth.

``build_mog_export(..., realm=...)`` applies the realm's ``factor x posture``
uniformly to the three price fields of every band and writes its duration and
holding window into ``maxExpires``; without a realm the neutral reference
(factor 1.00, 30 days) is used.

The multiplier is **banded on the value anchor** instead of being a single
number per behaviour.  Every price string is a nested ``ifgt`` chain, so one
operation still covers a whole group while five thousand gold of stock and
fifty thousand gold of stock do not share the same factor.  This follows the
measurements in ``reports/mog_groups.md``: turnover rises with the price band
(Silvermoon: 2.8%/day at 50-500g, 5.7%/day above 5k), while inside a single band
the behaviour labels hardly order at all; and the region market is asymmetric
(walls above 120% of it fall to ~0.75x in 27h, walls below it stay flat), so it
works as a ceiling and not as a floor.  ``liquid`` is the only group whose ask
decayed over 78 days (0.59x), which is why it gets the lowest factors and 24h;
``slow``/``very_slow``/``collector`` are flat, so they are held rather than
discounted.

WARNING: this was never run inside the addon.  Import into a **test profile**
and check the Price Tester / a post scan before trusting it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

from .tsm_export import build_export, build_group_export
from .mog_realm import RealmProfile, resolve as resolve_realm

# group slug -> name shown in the TSM group tree (as in the 7-day plan)
GROUP_NAMES = {
    "liquid": "Liquid",
    "collector": "Collector",
    "slow": "Slow",
    "very_slow": "Very_Slow",
    "unproven": "Unproven",
    "unknown": "Unknown",
    "conflict": "Conflict",
}

# one pricing policy per behaviour (md section D).  slow/very_slow/unproven are
# now distinct: the discount grows as the observed movement shrinks.
GROUP_POLICY = {
    "liquid": "liquid",
    "collector": "collector",
    "slow": "slow",
    "very_slow": "very_slow",
    "unproven": "unproven",
    "unknown": "refusal",
    "conflict": "conflict",
}

# Value anchor.  Two traps are avoided on purpose:
#   * the realm ``dbmarket``/``dbminbuyout``: on a realm with no competition the
#     player's own auction *is* the realm market and TSM never undercuts your own
#     auction (``MakePostDecision`` matches it when the lowest is ``isPlayer``),
#     so anchoring on it locks the price at whatever was posted once;
#   * ``dbregionsaleavg``: the region-wide *sale* average, contaminated by snipes
#     (section B of reports/mog_groups.md), so it is not a value either.
# The region-wide **market** (ask) average is the score, the region historical is
# the fallback and the 60-day realm historical is the last resort; the floor
# ``1.5 * vendorsell`` keeps the listing above a few deposits.
VALUE = "first(dbregionmarketavg, dbregionhistorical, dbhistorical)"
FLOOR = "1.5 * vendorsell"

# --- price bands ---------------------------------------------------------
# ``ifgt(a, b, x, y)`` is TSM's "if a > b then x else y" (Types.FUNCTION_INFO),
# so a descending list of thresholds compiles to one nested expression.
# Thresholds are in gold and are compared against VALUE, which TSM returns in
# copper -- the ``g`` suffix makes the comparison unit-safe.
BAND_THRESHOLDS_G = (50_000, 5_000, 500)

# behaviour -> one (normal, minimum, maximum) triple per band, **highest band
# first**; the last triple is the fallback used below the smallest threshold.
# Every triple must satisfy 0 < minimum <= normal <= maximum -- there is a test.
BAND_MULTIPLIERS = {
    # decaying stock: sell it before it loses more (0.59x over 78 days)
    "liquid": (
        (0.75, 0.40, 1.20),
        (0.65, 0.35, 1.10),
        (0.55, 0.30, 1.00),
        (0.45, 0.25, 0.90),
    ),
    # collector demand: hold the premium, widest ceiling
    "collector": (
        (0.85, 0.50, 1.60),
        (0.78, 0.45, 1.45),
        (0.68, 0.40, 1.30),
        (0.58, 0.35, 1.15),
    ),
    # flat ask + modest movement: match the band, do not discount
    "slow": (
        (0.80, 0.45, 1.35),
        (0.72, 0.40, 1.25),
        (0.62, 0.35, 1.15),
        (0.52, 0.30, 1.05),
    ),
    # flat ask and barely any movement: hold, but below the collector premium
    "very_slow": (
        (0.82, 0.45, 1.50),
        (0.74, 0.42, 1.35),
        (0.64, 0.38, 1.25),
        (0.55, 0.32, 1.15),
    ),
    # a price but no observed movement: probe low, the band still applies
    "unproven": (
        (0.75, 0.40, 1.25),
        (0.68, 0.36, 1.18),
        (0.58, 0.32, 1.10),
        (0.48, 0.28, 1.00),
    ),
    # the anchor itself is unreliable: widest spread of all
    "conflict": (
        (0.90, 0.50, 1.80),
        (0.82, 0.46, 1.60),
        (0.72, 0.42, 1.40),
        (0.62, 0.36, 1.25),
    ),
}


def _num(value: float) -> str:
    """Render a multiplier without trailing zeros (``1.0`` -> ``1``)."""
    return f"{value:g}"


def banded_expression(value: str, multipliers: Sequence[tuple[float, ...]]) -> str:
    """Compile one column of a band table into a nested ``ifgt`` string."""
    fallback = f"{_num(multipliers[-1])} * {value}"
    picks = [
        (threshold, f"{_num(multiplier)} * {value}")
        for threshold, multiplier in zip(BAND_THRESHOLDS_G, multipliers[:-1])
    ]
    for threshold, pick in reversed(picks):
        fallback = f"ifgt({value}, {threshold}g, {pick}, {fallback})"
    return fallback


def banded_prices(
    behaviour: str,
    value: str = VALUE,
    bands: Mapping[str, Sequence[tuple[float, float, float]]] | None = None,
) -> tuple[str, str, str]:
    """Return ``(normal, minimum, maximum)`` banded expressions for a behaviour."""
    table = (bands or BAND_MULTIPLIERS)[behaviour]
    if len(table) != len(BAND_THRESHOLDS_G) + 1:
        raise ValueError(f"{behaviour}: {len(table)} bands, expected {len(BAND_THRESHOLDS_G) + 1}")
    for band, (normal, minimum, maximum) in zip(BAND_THRESHOLDS_G + (0,), table):
        if not 0 < minimum <= normal <= maximum:
            raise ValueError(f"{behaviour} @ {band}g: need 0 < min <= normal <= max, got {(normal, minimum, maximum)}")
    more = list(zip(*table))
    return tuple(banded_expression(value, column) for column in more)


def scaled_bands(realm: RealmProfile) -> Dict[str, tuple]:
    """The band table with the realm's ``factor x posture`` folded in.

    One scalar for every band and every group: a realm is allowed to move the
    whole price level, not to reshape the bands (there is no per-realm sales
    history to estimate realm x band coefficients).  Uniform scaling keeps
    ``min <= normal <= max`` and the bands' own descending order.
    """
    scale = realm.scale
    if abs(scale - 1.0) < 1e-9:
        return dict(BAND_MULTIPLIERS)
    return {
        behaviour: tuple(tuple(round(c * scale, 3) for c in triple) for triple in table)
        for behaviour, table in BAND_MULTIPLIERS.items()
    }


@dataclass(frozen=True)
class AuctioningPolicy:
    normal: str
    minimum: str
    maximum: str
    duration: int          # 12, 24 or 48
    undercut: str          # money string, e.g. "1c"
    max_expires: int       # posting cycles since the last sale, 0 = never stop
    cancel: bool = False   # cancel undercut/repost automatically
    post_cap: int = 1      # 0 = never post this group
    price_reset: str = "none"  # what to do when the wall is below the minimum


# --- holding window ------------------------------------------------------
# ``maxExpires`` is not a duration: TSM compares it with the "NumExpires" custom
# source, which is the number of expired units of that item **since its last
# sale** (``ValidateForPosting`` -> ``CustomString.GetValue("NumExpires")`` ->
# ``Accounting.Auctions.GetNumExpiresSinceSale``), so a sale resets the count.
# With ``postCap`` 1 it is therefore a holding window in posting cycles, and the
# window is a per-realm decision (``mog_realm.CLASS_POLICY``): 60 days on a quiet
# realm, 14 on a full one.  TSM profiles are per character
# (``_currentProfile[char]`` in ``SettingsDB``) and hold their own
# groups/items/operations (``profile.userData``); custom price sources are
# global (``global.userData.customPriceSources``) and cannot separate realms and
# ``globalOperations`` would share the operations between profiles, so the
# intended use is one profile per realm, each importing its own export.

# A behaviour is never posted more often than the realm's base duration, but a
# long-tail group is never shortened below 48 h: fast repricing is for stock
# that actually has competitors (liquid / unproven / conflict).
BEHAVIOUR_MIN_DURATION = {
    "liquid": 12,
    "collector": 48,
    "slow": 48,
    "very_slow": 48,
    "unproven": 24,
    "conflict": 24,
}


def duration_for(behaviour: str, realm: RealmProfile) -> int:
    return max(realm.duration, BEHAVIOUR_MIN_DURATION[behaviour])


def max_expires_for(duration: int, hold_days: int) -> int:
    """Posting cycles that fit in the holding window (a sale resets the count)."""
    return max(1, round(hold_days * 24 / duration) - 1)


def _policy(
    behaviour: str,
    *,
    realm: RealmProfile,
    undercut: str,
    cancel: bool = False,
    post_cap: int = 1,
    bands: Mapping[str, Sequence[tuple[float, float, float]]] | None = None,
) -> AuctioningPolicy:
    """Build a policy whose three price strings are banded ``ifgt`` chains."""
    duration = duration_for(behaviour, realm)
    normal, minimum, maximum = banded_prices(behaviour, bands=bands)
    return AuctioningPolicy(
        normal=f"max({normal}, {FLOOR})",
        minimum=f"max({minimum}, {FLOOR})",
        maximum=f"max({maximum}, {FLOOR})",
        duration=duration,
        undercut=undercut,
        max_expires=max_expires_for(duration, realm.hold_days),
        cancel=cancel,
        post_cap=post_cap,
    )


# undercut per behaviour; the price strings come from the bands.  ``undercut`` is
# written as "1c" but on Retail TSM coerces anything below one silver to "0c"
# (``private.SanitizeUndercut``: ``defaultZeroUndercut`` is true whenever the AH
# is LIFO), so 1c and 0c behave the same in game: the listing matches the wall
# instead of starting a price war.
_POLICY_SPECS = {
    "liquid": "1c",
    "collector": "0c",
    "slow": "1c",
    "very_slow": "1c",
    "unproven": "1c",
    "conflict": "0c",
}


def policies(realm: "str | RealmProfile | None" = None) -> Dict[str, AuctioningPolicy]:
    """All seven operations for one realm profile (one holding window per realm)."""
    profile = resolve_realm(realm)
    bands = scaled_bands(profile)
    built = {
        slug: _policy(slug, realm=profile, undercut=undercut, bands=bands)
        for slug, undercut in _POLICY_SPECS.items()
    }
    # Unknown: no appearance with any usable price -> never post (postCap 0).
    # The strings are constants so nothing is evaluated / logged as invalid.
    built["refusal"] = AuctioningPolicy(
        normal="1000000000g",
        minimum="1000000000g",
        maximum="1000000000g",
        duration=24,
        undercut="0c",
        max_expires=max_expires_for(24, profile.hold_days),
        post_cap=0,
    )
    return built


POLICIES = policies()


# --- token rush (liquidation) export --------------------------------------
# A *separate* kind of export, with its own naming convention:
# ``liquidity_mogs_groups_<realm>.tsm``.  It does not touch the normal
# pricing: each item gets its own group and its own operation, so two items of
# the same price band can be posted at different prices.  The operation is
# deliberately short-lived (rush duration, few expires) and bounded:
# ``minPrice`` is the floor the model computed (loss limit included) and
# ``maxPrice`` is the local reference, so the live TSM clamp keeps the price
# inside the band the engine recommended.
LIQUIDITY_PREFIX = "liquidity_mogs_groups"


@dataclass(frozen=True)
class LiquidityOperation:
    """One liquidation operation: a recommended price and the bounds around it."""

    normal: str
    minimum: str
    maximum: str
    duration: int = 12          # rush: the item is reposted often
    undercut: str = "1c"
    max_expires: int = 2        # give up quickly; a 12 day campaign has no patience
    post_cap: int = 1
    stack_size: int = 1         # materials post in stacks
    stack_size_is_cap: bool = False
    price_reset: str = "none"   # a wall under the floor: do not post
    above_max: str = "maxPrice"  # a wall over the cap: post at the cap
    cancel: bool = False


def liquidity_operation(policy: LiquidityOperation) -> Dict:
    """Build one ``operations.Auctioning.<name>`` table for a rush export."""
    return {
        "aboveMax": policy.above_max,
        "bidPercent": 1,
        "blacklist": "",
        "cancelRepost": policy.cancel,
        "cancelRepostThreshold": "1000g",
        "cancelUndercut": policy.cancel,
        "duration": policy.duration,
        "ignoreLowDuration": 0,
        "keepPosted": 0,
        "keepQuantity": 0,
        "keepQtySources": {},
        "matchStackSize": False,
        "maxExpires": policy.max_expires,
        "maxPrice": policy.maximum,
        "minPrice": policy.minimum,
        "normalPrice": policy.normal,
        "postCap": policy.post_cap,
        "priceReset": policy.price_reset,
        "stackSize": policy.stack_size,
        "stackSizeIsCap": policy.stack_size_is_cap,
        "undercut": policy.undercut,
    }


def build_liquidity_export(
    groups: Mapping[str, Sequence[int]],
    operations: Mapping[str, LiquidityOperation],
    *,
    operation_prefix: str = "Rush ",
) -> str:
    """Assemble a rush import string: one group and one operation per item.

    ``groups`` maps the group name (shown in the TSM tree) to the item ids;
    ``operations`` maps the same name to its operation.  Empty groups are
    dropped.  The group operations carry ``override = true`` so TSM actually
    attaches the operation to the group.
    """
    names = [name for name, ids in groups.items() if ids]
    group_export = build_group_export({name: [int(i) for i in groups[name]] for name in names})
    auctioning: Dict[str, Dict] = {}
    group_operations: Dict[str, Dict] = {}
    for name in names:
        operation = operation_prefix + name
        auctioning[operation] = liquidity_operation(operations[name])
        group_operations[name] = {"Auctioning": {1: operation, "override": True}}
    return build_export({"Auctioning": auctioning}, group_operations, group_export)


def auctioning_operation(policy: AuctioningPolicy) -> Dict:
    """Build one ``operations.Auctioning.<name>`` table for a TSM export."""
    return {
        "aboveMax": "maxPrice",
        "bidPercent": 1,
        "blacklist": "",
        "cancelRepost": policy.cancel,
        "cancelRepostThreshold": "1000g",
        "cancelUndercut": policy.cancel,
        "duration": policy.duration,
        "ignoreLowDuration": 0,
        "keepPosted": 0,
        "keepQuantity": 0,
        "keepQtySources": {},
        "matchStackSize": False,
        "maxExpires": policy.max_expires,
        "maxPrice": policy.maximum,
        "minPrice": policy.minimum,
        "normalPrice": policy.normal,
        "postCap": policy.post_cap,
        "priceReset": policy.price_reset,
        "stackSize": 1,
        "stackSizeIsCap": False,
        "undercut": policy.undercut,
    }


def build_mog_export(
    items: Mapping[str, Sequence[int]],
    *,
    with_operations: bool = True,
    conflict_inline: bool = False,
    operation_prefix: str = "Mogs ",
    group_names: Mapping[str, str] | None = None,
    realm: "str | RealmProfile | None" = None,
) -> str:
    """Assemble the import string from ``{group slug: [item_id, ...]}``.

    ``conflict_inline`` keeps the conflict items in their primary group (TSM
    allows an item in only one group, so by default they move to a dedicated
    ``Conflict`` group that carries the historical-override operation).

    ``realm`` selects the measured realm profile (name, slug or connected-realm
    alias, e.g. ``"Dragonmaw"`` resolves to the Spinebreaker group): its
    ``factor x posture`` scales every band and its duration / holding window go
    into the operation.  ``None`` is the neutral reference, factor 1.00.
    """
    names = dict(group_names or GROUP_NAMES)
    order = [slug for slug in names if slug != "conflict"] + ["conflict"]

    conflict_ids = set(int(i) for i in items.get("conflict", []))
    groups: Dict[str, List[int]] = {}
    for slug in order:
        if slug == "conflict":
            if conflict_inline:
                continue
            groups[names[slug]] = [int(i) for i in items.get("conflict", [])]
            continue
        ids = [int(i) for i in items.get(slug, [])]
        if not conflict_inline:
            # TSM keeps an item in one group only: conflict wins its own group.
            ids = [i for i in ids if i not in conflict_ids]
        groups[names[slug]] = ids

    # an empty group adds nothing to the import
    groups = {name: ids for name, ids in groups.items() if ids}
    group_export = build_group_export(groups)
    if not with_operations:
        return build_export({}, {}, group_export)

    operations = {"Auctioning": {}}
    group_operations: Dict[str, Dict] = {}
    built = policies(realm)
    for slug in order:
        if slug == "conflict" and conflict_inline:
            continue
        name = names[slug]
        if not groups.get(name):
            continue
        operation = operation_prefix + name
        operations["Auctioning"][operation] = auctioning_operation(
            built[GROUP_POLICY[slug]]
        )
        # TSM only attaches a group's operations when the module table carries
        # ``override = true`` (GroupOperation.Import: ``if operations.override``);
        # without it the group just inherits the profile root and the operation
        # is silently dropped.  ``ignoreItemVariations`` is a legacy field the
        # importer wipes, so we don't emit it (the group editor's "show ungrouped
        # items as" setting governs how item variants are matched).
        group_operations[name] = {
            "Auctioning": {1: operation, "override": True},
        }
    return build_export(operations, group_operations, group_export)
