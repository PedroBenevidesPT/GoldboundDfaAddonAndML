"""Cancel and relist, hold, or let it expire?

The undercut scan says who is below my price and by how much.  It does not
say what to do about it, and the answer is not "always undercut back":

* a one-copper undercut with one stack ahead is your turn next -- cancelling
  only pays a second deposit and restarts the queue;
* a 15 % gap with hours left will expire unsold unless the ask moves;
* with two hours left there is no time to earn the deposit back, so the
  listing is better left to expire and reposted next session.

The deposit is treated as **sunk**: cancelling and letting it expire both
lose the posting fee, and only a sale returns it.  So the decision is about
time and queue position, not about the fee.

The reference is the cheapest competing **ask**, not a sale, DBMarket or a
prediction.  That is the right target for an undercut decision, but only
when the reference is comparable and trustworthy.  A price-only API row has
no seller and no item key; it may be a different item level, suffix, bonus
list or battle-pet species.  Those rows are shown as review-required and
never turn into a cancel-and-relist instruction on their own.

This is analysis only.  Nothing here cancels, posts or undercuts.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

#: a gap this small is noise, not an undercut worth reacting to
DEFAULT_MIN_GAP = 0.02
#: at or above this, the current ask is not competitive at all
DEFAULT_HEAVY_GAP = 0.10
#: a queue longer than this multiple of my stack means I am behind
DEFAULT_QUEUE_FACTOR = 2
#: below this many hours there is no time to earn a new deposit back
DEFAULT_SHORT_TIME_H = 4.0
#: at or above this there is time for a repriced listing to sell
DEFAULT_LONG_TIME_H = 12.0
#: an observation older than this is evidence, not a current instruction
DEFAULT_MAX_AGE_H = 72.0
#: a gap smaller than this many times the measured noise is not a signal
DEFAULT_NOISE_MULT = 1.5
#: a market falling this much over the price path justifies a relist even
#: when today's gap is small
DEFAULT_FALL_TREND = 0.15
#: above this share of the visible supply, the cheapest ask is partly ours
DEFAULT_OWN_SHARE = 0.20
#: a supply shrink this large means the cheap stock is clearing
DEFAULT_CLEARING_DROP = 0.15

HOLD = "hold"
RELAST = "cancel_and_relist"
EXPIRE = "let_expire"
WATCH = "watch"

#: only a scan that names the seller and keeps the item key is strong
#: enough to justify an automatic recommendation
REFERENCE_VERIFIED = "verified"
REFERENCE_PRICE_ONLY = "price-only"
REFERENCE_VARIANT_AMBIGUOUS = "variant-ambiguous"
REFERENCE_UNVERIFIED = "unverified"
REFERENCE_STALE = "stale"
REFERENCE_INVALID = "invalid"

TRUSTED_REFERENCES = {REFERENCE_VERIFIED}


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _reference_status(row: Dict[str, Any]) -> str:
    """The stored trust label, with an honest fallback for older rows.

    Rows written before ``reference_status`` existed have no column value.
    A named in-game scan is still verified; a Blizzard API row is not.
    """
    status = str(row.get("reference_status") or "").strip().lower()
    if status:
        return status
    confidence = str(row.get("confidence") or "").strip().lower()
    source = str(row.get("source") or "").strip().lower()
    if confidence == REFERENCE_VERIFIED:
        return REFERENCE_VERIFIED
    if source == "blizzard_api" or confidence == REFERENCE_PRICE_ONLY:
        return REFERENCE_UNVERIFIED
    return REFERENCE_UNVERIFIED


def _reference_reason(status: str) -> str:
    if status == REFERENCE_VARIANT_AMBIGUOUS:
        return ("the cheapest listing carries a different item variant (level,"
                " suffix, bonus list or pet species), and the owned-auction"
                " export does not carry the matching item key: review it in"
                " the Auction House before cancelling")
    if status == REFERENCE_PRICE_ONLY:
        return ("the reference is a price-only API ask with no seller or"
                " item-key confirmation: review it in the Auction House"
                " before cancelling")
    if status == REFERENCE_STALE:
        return ("the reference is older than the freshness window: run a new"
                " undercut scan before cancelling")
    return ("the reference has no verified item-key and seller evidence:"
            " review it in the Auction House before cancelling")


def _relation(own_unit: int, lowest_unit: int) -> str:
    if own_unit < lowest_unit:
        return "underpriced"
    if own_unit == lowest_unit:
        return "at_cheapest"
    return "overpriced"


def _latest_observations(con: sqlite3.Connection, realm: str,
                         days: int) -> List[Dict[str, Any]]:
    import time
    since = int(time.time()) - max(1, days) * 86400
    sql = ("SELECT * FROM undercut_observation WHERE observed_at >= ?")
    params: List[Any] = [since]
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        params.append(realm)
    sql += " ORDER BY observed_at"
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    latest: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        own_id = _as_int(row["own_auction_id"])
        key: Any = own_id if own_id > 0 else row["observation_id"]
        latest[key] = {name: row[name] for name in row.keys()}
    return list(latest.values())


def decide(own_unit: int, lowest_unit: int, items_ahead: int,
           own_quantity: int, hours_left: float | None,
           min_gap: float = DEFAULT_MIN_GAP,
           heavy_gap: float = DEFAULT_HEAVY_GAP,
           queue_factor: float = DEFAULT_QUEUE_FACTOR,
           short_time_h: float = DEFAULT_SHORT_TIME_H,
           long_time_h: float = DEFAULT_LONG_TIME_H,
           reference_status: str = REFERENCE_UNVERIFIED,
           market: Any = None,
           noise_mult: float = DEFAULT_NOISE_MULT,
           fall_trend: float = DEFAULT_FALL_TREND,
           own_share_limit: float = DEFAULT_OWN_SHARE,
           clearing_drop: float = DEFAULT_CLEARING_DROP) -> Dict[str, Any]:
    """One recommendation, with the rule that produced it spelled out.

    ``hours_left`` may be ``None`` when the source does not carry a clock
    (the Blizzard API only sends SHORT/MEDIUM/LONG).  Unknown is not zero:
    the "too late, let it expire" rule must not fire on a missing number.

    ``own_unit`` and ``lowest_unit`` are unit prices in copper.  The gap is
    the fraction of *my* ask that the cheapest competitor is below; it is
    not a percentage by which my ask is below the competitor.  The decision
    defaults to review when the caller cannot say the reference is verified.
    """
    own_unit = _as_int(own_unit)
    lowest_unit = _as_int(lowest_unit)
    items_ahead = max(0, _as_int(items_ahead))
    own_quantity = max(1, _as_int(own_quantity, 1))
    hours_left = _as_float(hours_left)
    if hours_left is not None and hours_left < 0:
        hours_left = 0.0
    min_gap = _as_float(min_gap, DEFAULT_MIN_GAP) or 0.0
    heavy_gap = _as_float(heavy_gap, DEFAULT_HEAVY_GAP) or 0.0
    queue_factor = _as_float(queue_factor, DEFAULT_QUEUE_FACTOR) or 0.0
    short_time_h = _as_float(short_time_h, DEFAULT_SHORT_TIME_H) or 0.0
    long_time_h = _as_float(long_time_h, DEFAULT_LONG_TIME_H) or 0.0
    status = str(reference_status or REFERENCE_UNVERIFIED).strip().lower()

    if own_unit <= 0 or lowest_unit <= 0:
        return {
            "advice": WATCH,
            "reason": "no usable price on either side",
            "gap_pct": 0.0,
            "above_cheapest_pct": 0.0,
            "relation": "unknown",
            "reference_status": REFERENCE_INVALID,
            "review_required": True,
        }

    relation = _relation(own_unit, lowest_unit)
    gap = max(0.0, (own_unit - lowest_unit) / own_unit)
    above = max(0.0, (own_unit - lowest_unit) / lowest_unit)
    common = {
        "gap_pct": round(gap, 4),
        "above_cheapest_pct": round(above, 4),
        "relation": relation,
        "reference_status": status,
    }
    # The measured market, when the caller has one.  It refines thresholds
    # and can overrule the gap rules, but it never turns an unverified
    # reference into an instruction (that check comes next).
    volatility = getattr(market, "volatility", None) if market is not None else None
    trend = getattr(market, "trend_pct", None) if market is not None else None
    own_share = getattr(market, "own_share", None) if market is not None else None
    supply_change = (getattr(market, "units_change_pct", None)
                     if market is not None else None)
    if market is not None and hasattr(market, "as_dict"):
        common["market"] = market.as_dict()
    effective_min_gap = min_gap
    if volatility is not None and float(volatility) > 0:
        # A gap inside the price's own measured noise is not a signal: the
        # market moves that much between two snapshots on its own.
        effective_min_gap = max(min_gap,
                                min(0.5, float(noise_mult) * float(volatility)))

    if status not in TRUSTED_REFERENCES:
        return {
            **common,
            "advice": WATCH,
            "reason": _reference_reason(status),
            "review_required": True,
        }

    suggested = max(1, lowest_unit - 1)
    clock_known = hours_left is not None
    behind = items_ahead > max(1, own_quantity) * queue_factor

    if relation == "underpriced":
        return {
            **common,
            "advice": HOLD,
            "review_required": False,
            "reason": ("your ask is below the cheapest listed competitor;"
                       " it is not underpriced by a trustworthy higher"
                       " reference and needs no relist"),
        }
    if relation == "at_cheapest":
        return {
            **common,
            "advice": HOLD,
            "review_required": False,
            "reason": ("your ask matches the cheapest listed competitor;"
                       " there is no lower comparable ask to undercut"),
        }
    if clock_known and hours_left <= short_time_h:
        return {
            **common,
            "advice": EXPIRE,
            "review_required": False,
            "reason": (f"{hours_left:.1f}h left: too late to pay a new deposit"
                       " and still sell; repost after it expires"),
        }
    # Our own stock is a large part of what the scan saw: the "cheapest ask"
    # below us may be another of our own listings, and cancelling to match
    # it would be paying a deposit to undercut ourselves.
    if own_share is not None and float(own_share) >= own_share_limit:
        return {
            **common,
            "advice": HOLD,
            "review_required": False,
            "reason": (f"{float(own_share):.0%} of the visible supply is our own"
                       " stock: matching the cheapest ask would undercut our"
                       " own listings"),
        }
    # A market falling over the price path makes today's cheapest ask stale
    # by the time a new listing sells, so the relist is justified even when
    # the gap is small.  The trend is measured, the future is not: this says
    # the direction, not the size of the drop.
    if (trend is not None and float(trend) <= -abs(fall_trend)
            and relation == "overpriced"):
        return {
            **common,
            "advice": RELAST,
            "review_required": False,
            "reason": (f"the market is down {float(trend):.0%} over the price"
                       f" path, and the ask is {gap:.1%} above the cheapest:"
                       " matching it now is cheaper than following it down"),
            "suggested_ask_copper": suggested,
            "suggested_discount_pct": round((own_unit - suggested) / own_unit, 4),
        }
    # The cheap stock is clearing and the price is not falling: the queue
    # empties on its own, and matching it now sells into a market that will
    # bear more shortly.  Only said when a snapshot actually showed supply
    # shrinking.
    if (supply_change is not None and float(supply_change) <= -abs(clearing_drop)
            and (trend is None or float(trend) >= 0.0) and not behind):
        return {
            **common,
            "advice": HOLD,
            "review_required": False,
            "reason": (f"the listed supply is down {float(supply_change):.0%}"
                       " and the price is not falling: the cheaper stock is"
                       " clearing, so holding costs nothing and matching it"
                       " would sell into a market that will bear more"),
        }
    if gap <= effective_min_gap and not behind:
        reason = (f"gap {gap:.1%} with {items_ahead} unit(s) ahead:"
                  " the cheaper stock clears first and the ask is already"
                  " competitive")
        if effective_min_gap > min_gap:
            reason += (f" (the gap is inside the measured noise of"
                       f" {float(volatility):.1%} per snapshot)")
        return {
            **common,
            "advice": HOLD,
            "review_required": False,
            "reason": reason,
        }
    if gap >= heavy_gap or behind:
        return {
            **common,
            "advice": RELAST,
            "review_required": False,
            "reason": (f"gap {gap:.1%}, {items_ahead} unit(s) ahead:"
                       " the ask will not clear before it expires"),
            "suggested_ask_copper": suggested,
            "suggested_discount_pct": round((own_unit - suggested) / own_unit, 4),
        }
    if not clock_known:
        return {
            **common,
            "advice": WATCH,
            "review_required": False,
            "reason": (f"gap {gap:.1%} with {items_ahead} unit(s) ahead, but the"
                       " source carries no time left: watch the next scan"),
        }
    if hours_left < long_time_h:
        return {
            **common,
            "advice": WATCH,
            "review_required": False,
            "reason": (f"gap {gap:.1%} but only {hours_left:.1f}h left:"
                       " repricing now cannot prove itself; watch the next scan"),
        }
    return {
        **common,
        "advice": WATCH,
        "review_required": False,
        "reason": (f"gap {gap:.1%}, {items_ahead} unit(s) ahead: not enough to"
                   " justify cancelling yet"),
    }


def decide_row(row: Any, now: float | None = None, market: Any = None,
               max_age_h: float = DEFAULT_MAX_AGE_H,
               **thresholds: Any) -> Dict[str, Any]:
    """One recommendation for one stored observation, at a stated time.

    This is the whole of :func:`advise`'s per-row work, exposed because the
    decision ledger has to replay a stored scan at the time it was taken:
    replaying it at ``now`` would report a five-day-old listing as "two
    hours left" and turn every historical HOLD into a false EXPIRE.

    ``row`` is any mapping with the ``undercut_observation`` columns.  ``now``
    is the clock used to age the scan; it is explicit so the answer does not
    depend on when the report is run.
    """
    import time
    now = time.time() if now is None else float(now)
    observed_at = _as_int(row.get("observed_at"), 0)
    age_h = max(0.0, (now - observed_at) / 3600.0) if observed_at else 0.0
    raw_left = _as_int(row.get("time_left"), 0)
    # 0 is "the source did not say", not "expires now"
    hours_left = (max(0.0, raw_left / 3600.0 - age_h)
                  if raw_left > 0 else None)
    status = _reference_status(row)
    max_age_h = _as_float(max_age_h, DEFAULT_MAX_AGE_H) or 0.0
    if max_age_h and age_h > max_age_h:
        status = REFERENCE_STALE
    item_id = _as_int(row.get("item_id"), 0)
    item_name = (str(row.get("item_name") or "").strip()
                 or (f"item {item_id}" if item_id else "item unknown"))
    item_market = None
    if market is not None:
        try:
            item_market = market.get(item_id)
        except Exception:  # pragma: no cover - defensive
            item_market = None
    decision = decide(
        own_unit=_as_int(row.get("own_unit_price_copper")),
        lowest_unit=_as_int(row.get("lowest_unit_price_copper")),
        items_ahead=_as_int(row.get("items_ahead")),
        own_quantity=_as_int(row.get("own_quantity"), 1),
        hours_left=hours_left,
        reference_status=status,
        market=item_market,
        **thresholds)
    return {
        "item_id": item_id,
        "item_name": item_name,
        "character": str(row.get("character") or ""),
        "own_auction_id": row.get("own_auction_id"),
        "own_quantity": _as_int(row.get("own_quantity"), 1),
        "own_unit_price_copper": row.get("own_unit_price_copper"),
        "lowest_unit_price_copper": row.get("lowest_unit_price_copper"),
        "lowest_seller": row.get("lowest_seller"),
        "lowest_auction_id": row.get("lowest_auction_id"),
        "items_ahead": row.get("items_ahead"),
        "hours_left": round(hours_left, 2) if hours_left is not None else None,
        "observed_at": row.get("observed_at"),
        "scan_id": row.get("scan_id"),
        "source": row.get("source"),
        "confidence": row.get("confidence"),
        **decision,
    }


def advise(con: sqlite3.Connection, realm: str = "", days: int = 14,
           max_age_h: float = DEFAULT_MAX_AGE_H,
           market: Any = None,
           now: float | None = None,
           **thresholds: Any) -> Dict[str, Any]:
    """Rank the open listings by what to look at first.

    ``market`` is an optional :class:`~woweconomy.ml.market_context.RealmMarket`
    (or a plain ``item_id -> item market`` mapping).  When it is given, each
    row is decided with the measured noise, trend and own-share; without it
    the decision is exactly the gap/queue/time one it always was.

    ``now`` is the clock the scans are aged against; it defaults to the
    current time and is explicit so a replay of old observations is
    reproducible.
    """
    import time
    now = time.time() if now is None else float(now)
    rows = _latest_observations(con, realm, days)
    out = []
    counts = {HOLD: 0, RELAST: 0, EXPIRE: 0, WATCH: 0}
    review_required = 0
    for row in rows:
        entry = decide_row(row, now=now, market=market, max_age_h=max_age_h,
                           **thresholds)
        counts[entry["advice"]] += 1
        if entry.get("review_required"):
            review_required += 1
        out.append(entry)
    out.sort(key=lambda r: (r["advice"] != RELAST, -r.get("gap_pct", 0)))
    return {
        "realm": realm or "all",
        "days": days,
        "listings": len(out),
        "counts": counts,
        "review_required": review_required,
        "advice": out,
        "note": ("the deposit is sunk either way: cancelling and expiring both"
                 " lose it, and only a sale returns it. Only a verified,"
                 " comparable ask can produce cancel-and-relist; price-only"
                 " and variant-ambiguous rows are review items. Nothing here"
                 " cancels, posts or undercuts; it only ranks what to look"
                 " at first."),
    }
