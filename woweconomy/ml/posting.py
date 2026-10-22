"""The posting decision: undercut, hold, relist, change realm or stop.

The strategy picks an ask.  This module decides what to *do* with a listing
that already exists, which is a different question and the one a gold maker
faces every time they open the auction house:

* **undercut** -- no listing yet, and the market supports this ask;
* **hold** -- listed at or below the competition; cancelling would only
  burn the deposit;
* **relist** -- undercut by enough that the expected gain from a fresh,
  cheaper listing beats the deposit that cancelling forfeits;
* **change_realm** -- the same item is worth materially more on another
  realm the account can reach;
* **stop** -- the expected net proceeds at any supported ask are not
  positive, or there is no evidence to price it at all.

## The deposit, verified rather than assumed

The deposit is **not** a percentage of the ask.  It scales with the item's
**vendor sell value** and the listing duration: 12 h is the base, 24 h is
twice it and 48 h is four times it (Epiccarry, *WoW Auction House Guide
(TWW)*, updated 2026-04-24: "This fee, paid upfront when posting items,
scales with the item's vendor value and the auction duration. 12h = base
cost, 24h = 2x base, 48h = 4x base"; WoWWiki, *Formulas:Auction House*:
"Deposits are based on the Merchant Sell Value").

The base is 5 % of the vendor value, which is the figure the community
uses and the one that reproduces the observed 5 / 10 / 20 % for
12 / 24 / 48 h.  It is recorded here as `DEPOSIT_BASE_RATE` with that
provenance: it is a well-supported convention, not a Blizzard statement,
and the code never presents it as more than that.

The deposit is **refunded only when the item sells**.  So the expected
deposit cost of a listing is ``deposit * (1 - P(sale))``: a listing that
is certain to sell pays nothing, one that is certain to expire pays it
all.  On a realm mount with a 6M-gold ask and a 25-gold vendor value the
deposit is 1g 25s, not 300k -- which is exactly the difference between a
usable posting rule and one that refuses to ever post a mount.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .market_context import ItemMarket

#: The house cut on a completed sale (retail: 5 %).
AH_CUT_RATE = 0.05

#: The deposit base: share of the **vendor sell value** for a 12 h listing.
#: Community-verified (see the module docstring), not an official constant.
DEPOSIT_BASE_RATE = 0.05

#: Duration multipliers on that base.  12 h is the base, 24 h twice, 48 h
#: four times.
DEPOSIT_MULTIPLIERS: Dict[int, float] = {12: 1.0, 24: 2.0, 48: 4.0}

#: The durations the client offers.
DEPOSIT_HOURS = tuple(sorted(DEPOSIT_MULTIPLIERS))


@dataclass(frozen=True)
class PostingEdge:
    """One candidate action, with the money it is expected to make."""

    #: post | undercut | hold | relist | change_realm | wait | stop
    action: str
    ask: int                     # the ask this action posts at (0 for wait/stop)
    gross: int                   # ask x qty
    cut: int                     # the 5 % house cut on gross
    deposit: int                 # the deposit the action pays
    expected_deposit: int        # deposit x (1 - P(sale))
    net: int                     # gross - cut - expected_deposit
    sale_probability: float
    confidence: float
    realm: str = ""
    reason: str = ""
    #: the ask a "wait" would post at once the market clears (None otherwise)
    projected_ask: Optional[int] = None
    #: the market evidence the action rests on, when there was any
    market: Optional[Dict[str, object]] = None

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "action": self.action, "ask": self.ask, "gross": self.gross,
            "cut": self.cut, "deposit": self.deposit,
            "expected_deposit": self.expected_deposit, "net": self.net,
            "sale_probability": round(self.sale_probability, 4),
            "confidence": round(self.confidence, 4), "realm": self.realm,
            "reason": self.reason,
        }
        if self.projected_ask is not None:
            out["projected_ask"] = int(self.projected_ask)
        if self.market:
            out["market"] = dict(self.market)
        return out


def deposit_rate(hours: int) -> float:
    """The deposit as a share of the vendor value, for a duration.

    A duration the client does not offer is refused rather than silently
    rounded: 12 h, 24 h and 48 h are the only listings that exist.
    """
    try:
        multiplier = DEPOSIT_MULTIPLIERS[int(hours)]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"no auction lasts {hours!r} hours; use {DEPOSIT_HOURS}")
    return DEPOSIT_BASE_RATE * multiplier


def deposit_copper(vendor_sell_copper: int, hours: int = 24,
                   qty: int = 1) -> int:
    """What the client charges to post ``qty`` units for ``hours``.

    Zero when the item has no vendor value: the deposit is a share of it,
    and an item a vendor will not buy has nothing to charge against.  The
    ask is not an input on purpose -- that is the error this replaces.
    """
    vendor = max(0, int(vendor_sell_copper or 0))
    units = max(1, int(qty or 1))
    return int(round(vendor * deposit_rate(hours) * units))


def sale_net(ask: int, vendor_sell_copper: int, sale_probability: float,
             hours: int = 24, qty: int = 1) -> int:
    """Expected copper from one listing, deposit included.

    ``ask`` is the unit ask.  The cut comes off every unit sold; the
    deposit is paid once for the posting and is only returned when it
    sells, so its expected cost is weighted by ``1 - P(sale)``.
    """
    units = max(1, int(qty or 1))
    gross = max(0, int(ask)) * units
    cut = int(round(gross * AH_CUT_RATE))
    deposit = deposit_copper(vendor_sell_copper, hours, units)
    p = min(1.0, max(0.0, float(sale_probability or 0.0)))
    return gross - cut - int(round(deposit * (1.0 - p)))


def decide(
    *,
    reference: int,
    vendor_sell_copper: int,
    sale_probability: float,
    hours: int = 24,
    qty: int = 1,
    list_price: Optional[int] = None,
    best_ask: Optional[int] = None,
    confidence: float = 0.0,
    min_confidence: float = 0.15,
    realm: str = "",
    other_realms: Optional[Dict[str, tuple]] = None,
    relist_gain: float = 1.10,
    min_relist_gap: float = 0.05,
    market: Optional["ItemMarket"] = None,
    noise_mult: float = 1.5,
    wait_supply_drop: float = 0.15,
    fall_trend: float = 0.15,
    own_share_limit: float = 0.20,
) -> PostingEdge:
    """What to do with one item, and what it is worth.

    ``reference`` is the price the model supports.  ``best_ask`` is the
    cheapest competing listing (None when nothing competes).  ``list_price``
    is our own live listing, when there is one.  ``other_realms`` maps a
    realm to ``(ask, sale_probability)`` for the same item.

    ``market`` is the measured context around this item (see
    :mod:`woweconomy.ml.market_context`).  It is optional and it only ever
    *refines* the same arithmetic: without it the decision is exactly what
    it was before.  With it, four things change:

    * a gap smaller than the market's own measured noise is not worth a
      deposit -- the price moves that much between two snapshots anyway;
    * a market falling faster than one snapshot is not worth matching: the
      ask that is cheapest today is stale tomorrow, so the relist is
      justified even when the gap is small;
    * when the visible supply is largely our own stock, the "cheapest ask"
      is partly us, and undercutting it is paying a deposit to bid against
      ourselves;
    * when the cheap stock is clearing and the price is not falling, the
      queue empties on its own: waiting is worth more than starting a price
      war, and the decision says so instead of posting.
    """
    def edge(action: str, ask: int, p: float, reason: str,
             at: str = "", projected: Optional[int] = None) -> PostingEdge:
        units = max(1, int(qty or 1))
        gross = max(0, int(ask)) * units
        cut = int(round(gross * AH_CUT_RATE))
        deposit = deposit_copper(vendor_sell_copper, hours, units)
        expected_deposit = int(round(deposit * (1.0 - min(1.0, max(0.0, p)))))
        return PostingEdge(
            action=action, ask=int(ask), gross=gross, cut=cut, deposit=deposit,
            expected_deposit=expected_deposit,
            net=gross - cut - expected_deposit,
            sale_probability=float(p), confidence=float(confidence),
            realm=at or realm, reason=reason, projected_ask=projected,
            market=(market.as_dict() if market is not None else None))

    if reference <= 0 or confidence < min_confidence:
        return edge("stop", 0, 0.0,
                    "no evidence to price this item" if reference <= 0
                    else f"confidence {confidence:.0%} below {min_confidence:.0%}")

    # The cheapest ask we can post at.  A cheaper listing is matched (LIFO
    # rewards the newest listing at the same price, so a 1-copper undercut
    # only starts a race); a listing *above* the supported price changes
    # nothing, because the evidence does not support asking that much and a
    # lone expensive ask is not proof that the item sells there.
    p_target = float(sale_probability or 0.0)
    cheaper = bool(best_ask and 0 < int(best_ask) < int(reference))
    target = int(best_ask) if cheaper else int(reference)

    # "post" is a new listing, "undercut" is matching a cheaper one and
    # "hold" is leaving a listing that is already there alone.  They are
    # different actions with different costs, so they are different names.
    if cheaper:
        here = edge("undercut", target, p_target,
                    "match the cheapest listing, which is below the"
                    " price the evidence supports")
    elif best_ask:
        here = edge("post", target, p_target,
                    "the cheapest listing is above the supported price:"
                    " post at the price the evidence supports")
    else:
        here = edge("post", target, p_target,
                    "no competition: post at the supported price")

    # A live listing at or below the competition: cancelling costs the
    # deposit and gains nothing.
    if list_price and best_ask and list_price <= best_ask:
        return edge("hold", int(list_price), p_target,
                    "already at or below the cheapest listing")

    # -- the market evidence, when there is any ----------------------------
    # Each piece is optional on its own: a realm with a price path but no
    # snapshot still gets the noise and the trend, and a realm with a
    # snapshot but no path still gets the supply.
    volatility = None
    if market is not None and market.volatility and market.volatility > 0:
        volatility = float(market.volatility)
    gap_floor = min_relist_gap
    if volatility is not None:
        # a gap inside the measured noise is not a price signal; cap the
        # requirement so a wild market does not freeze every relist
        gap_floor = max(gap_floor, min(0.5, float(noise_mult) * volatility))

    # Our own stock in the visible supply: the cheapest ask may be ours.
    if market is not None and list_price:
        own_is_cheapest = (market.own_cheapest_ask is not None
                           and best_ask is not None
                           and int(best_ask) <= int(market.own_cheapest_ask))
        big_share = (market.own_share is not None
                     and market.own_share >= own_share_limit)
        if own_is_cheapest or big_share:
            share = (f"{market.own_share:.0%} of the visible supply is our own stock"
                     if big_share else "the cheapest listing is our own")
            return edge(
                "hold", int(list_price), p_target,
                f"{share}: matching it would undercut ourselves, and the"
                " reference is then not independent market evidence")

    # A better realm, when the advantage survives a 10 % margin (moving
    # stock has a cost the data does not measure, so demand headroom).
    best_other = None
    for name, value in (other_realms or {}).items():
        try:
            other_ask, other_p = value
        except (TypeError, ValueError):
            continue
        candidate = edge("change_realm", int(other_ask), float(other_p),
                         f"better net on {name}", at=name)
        if best_other is None or candidate.net > best_other.net:
            best_other = candidate
    if best_other is not None and best_other.net > here.net * relist_gain:
        return best_other

    if here.net <= 0:
        return edge("stop", 0, 0.0,
                    "expected net proceeds are not positive at any supported ask")

    # Undercut and already listed above it.  While cheaper stock is on the
    # board ours is not what a buyer picks, so waiting has no measurable
    # expected value -- the data says nothing about when the competitor
    # leaves.  The choice is to relist at the cheapest ask or leave it, and
    # a 1-copper "undercut" is the pointless race LIFO makes pointless: it
    # has to be a material move to be worth the posting.
    if list_price and best_ask and list_price > best_ask:
        gap = (int(list_price) - target) / max(1, int(list_price))
        falling = (market is not None and market.trend_pct is not None
                   and market.trend_pct <= -abs(fall_trend))
        if here.net > 0 and gap >= gap_floor:
            reason = f"undercut by {gap:.0%}: relisting at the cheapest ask"
            if gap_floor > min_relist_gap:
                reason += (f" (the gap clears the measured noise of"
                           f" {volatility:.1%} per snapshot)")
            return edge("relist", target, p_target, reason)
        if here.net > 0 and falling:
            return edge(
                "relist", target, p_target,
                f"the market is down {market.trend_pct:.0%} over"
                f" {market.path_snapshots} snapshots: a price that is only"
                " 1c cheaper is already behind it, so matching it now is the"
                " cheaper exit")
        if gap < gap_floor:
            if gap_floor > min_relist_gap:
                return edge(
                    "hold", int(list_price), p_target,
                    f"a {gap:.1%} gap is inside the measured noise of"
                    f" {float(volatility):.1%} per snapshot: reposting would"
                    " pay a deposit to chase the market's own moves")
            return edge("hold", int(list_price), p_target,
                        "the gap to the cheapest ask does not pay for a repost")
        return edge("hold", int(list_price), p_target,
                    "no positive net at the cheapest ask")

    # No live listing.  When the cheap stock is clearing and the price is
    # not falling, the queue empties on its own; matching it now would sell
    # into a market that will bear more in a few hours.  This is only said
    # when a scan actually saw the competition, and the reason carries the
    # assumption it rests on.
    if (not list_price and market is not None and best_ask
            and int(best_ask) < int(reference)
            and market.items_ahead is not None and market.items_ahead > 0
            and market.units_change_pct is not None
            and market.units_change_pct <= -abs(wait_supply_drop)
            and (market.trend_pct is None or market.trend_pct >= 0.0)
            and market.confidence >= 0.3):
        projected = int(round(float(reference)))
        return edge(
            "wait", 0, p_target,
            f"supply is down {market.units_change_pct:.0%} and the price is"
            f" not falling; {market.items_ahead} unit(s) ahead of the cheapest"
            f" ask are clearing.  Waiting for them to sell at the supported"
            f" price of {projected} beats matching it now; this assumes the"
            " sale probability holds while waiting, which the data does not"
            " measure",
            projected=projected)

    return here
