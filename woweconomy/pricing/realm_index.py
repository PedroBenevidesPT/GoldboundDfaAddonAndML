"""One line per realm, so the game can answer for *every* realm.

The dataset carries a record only for the items the account traded in a
realm.  That leaves 259 of the 267 known realms with a name and no price,
which is why "best realm for this item" was impossible to answer.

This module compresses each realm into what the game actually needs:

* a **price factor** against the regional model price, estimated from the
  realm's own sales and shrunk towards 1 when the sample is thin (a realm
  with four sales must not claim a 3x market);
* the **undercut share**: of the listings the addon could see on that
  realm, the share that had somebody cheaper -- the direct measure of
  babysitting;
* a **24h babysit risk**, but only when two Blizzard snapshots exist: the
  rate at which the cheapest price drops per item-hour.  With one
  snapshot it is ``None``; a missing measurement is not a zero risk.

What the game does with it: ``regional price x factor`` is the realm's
estimate, and ``best realm`` ranks realms on price discounted by the
measured undercut share.  It is a ranking on price and competition, not a
probability of sale -- the sale probability model is per price, not per
realm, and pretending otherwise would be a nicer number with no evidence.
"""
from __future__ import annotations

import math
import sqlite3
import time
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: the shrink target: a realm with no evidence is the regional market
PRIOR_SAMPLE = 10
#: below this many priced items, a factor is reported but flagged
MIN_FACTOR_SAMPLE = 3
#: how many days of scans define "current" undercut share
SHARE_WINDOW_DAYS = 14


def price_factors(engine, region_prices: Dict[int, int],
                  realms: Iterable[str],
                  prior_sample: int = PRIOR_SAMPLE) -> Dict[str, Dict[str, Any]]:
    """realm -> factor with the sample it was estimated from."""
    out: Dict[str, Dict[str, Any]] = {}
    for realm in realms:
        items = getattr(engine, "_history_cache", {}).get(realm, {})
        ratios: List[float] = []
        for item_id, entry in items.items():
            regional = int(region_prices.get(item_id) or 0)
            if regional <= 0:
                continue
            # The observed sale price, not the model's output: feeding the
            # regional model back in would return a factor of 1 for every
            # realm the model has no own model for, which is no measurement.
            prices = entry[2] if isinstance(entry, (tuple, list)) and len(entry) > 2 else []
            observed = [float(p) for p in prices if float(p or 0) > 0]
            if not observed:
                continue
            price = median(observed)
            if price <= 0:
                continue
            ratios.append(math.log(price / regional))
        if not ratios:
            out[realm] = {"factor": 1.0, "sample": 0, "source": "regional"}
            continue
        raw = median(ratios)
        n = len(ratios)
        shrunk = raw * (n / (n + prior_sample))
        out[realm] = {
            "factor": round(math.exp(shrunk), 4),
            "sample": n,
            "source": "realm" if n >= MIN_FACTOR_SAMPLE else "thin",
            "median_ratio": round(math.exp(raw), 4),
        }
    return out


def undercut_share(con: sqlite3.Connection, realm: str,
                   days: int = SHARE_WINDOW_DAYS) -> Optional[Dict[str, Any]]:
    """Of my listings, how many had somebody cheaper?

    Every stored observation *is* an undercut, so the denominator cannot
    come from the same table: it is the owned-auction list the addon read
    (``inventory_posted_listing``) around the scan.  Without that picture
    the share is unknown (``None``), not 100 %.
    """
    since = int(time.time()) - max(1, days) * 86400
    try:
        scan = con.execute(
            "SELECT scan_id, MAX(observed_at) AS at FROM undercut_observation"
            " WHERE LOWER(realm) = LOWER(?) AND observed_at >= ?"
            " GROUP BY scan_id ORDER BY at DESC LIMIT 1",
            (realm, since)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not scan:
        return None
    scan_id, at = scan[0], int(scan[1] or 0)
    undercut_rows = con.execute(
        "SELECT COUNT(DISTINCT own_auction_id), AVG(undercut_pct),"
        " AVG(items_ahead) FROM undercut_observation"
        " WHERE scan_id = ?", (scan_id,)).fetchone()
    undercut_count = int(undercut_rows[0] or 0)
    try:
        posted = con.execute(
            "SELECT COUNT(*) FROM inventory_posted_listing l"
            " JOIN inventory_snapshot s ON s.snapshot_id = l.snapshot_id"
            " WHERE LOWER(s.realm) = LOWER(?) AND s.captured_at BETWEEN ? AND ?",
            (realm, at - 86400, at + 3600)).fetchone()[0]
    except sqlite3.OperationalError:
        posted = 0
    if not posted:
        return None
    return {
        "watched": int(posted),
        "undercut": undercut_count,
        "share": round(min(1.0, undercut_count / max(1, posted)), 4),
        "avg_gap_pct": round(float(undercut_rows[1] or 0), 4),
        "avg_items_ahead": round(float(undercut_rows[2] or 0), 2),
        "days": days,
        "scan_id": scan_id,
    }


def _states(con: sqlite3.Connection, realm: str,
            limit: int = 24) -> List[Tuple[int, Dict[int, int]]]:
    """The last snapshots' cheapest price per item, oldest first."""
    try:
        heads = con.execute(
            "SELECT snapshot_id, captured_at FROM ah_snapshot"
            " WHERE LOWER(realm) = LOWER(?) ORDER BY captured_at DESC LIMIT ?",
            (realm, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for head in reversed(heads):
        rows = con.execute(
            "SELECT item_id, MIN(unit_price_copper) FROM ah_auction"
            " WHERE snapshot_id = ? AND unit_price_copper > 0"
            " GROUP BY item_id", (head["snapshot_id"],)).fetchall()
        out.append((int(head["captured_at"]), {int(r[0]): int(r[1]) for r in rows}))
    return out


def babysit_risk(con: sqlite3.Connection, realm: str,
                 horizon_h: float = 24.0) -> Optional[Dict[str, Any]]:
    """P(the cheapest price drops within the horizon), from snapshot pairs.

    Each consecutive pair contributes one exposure per item present in
    both: a drop is the event, hours between the snapshots is the time.
    The rate is then turned into a probability with the exponential
    survival form.  One snapshot, or none, means no measurement.
    """
    states = _states(con, realm)
    if len(states) < 2:
        return None
    drops = 0
    exposures = 0
    hours_total = 0.0
    for (t0, first), (t1, second) in zip(states, states[1:]):
        hours = max(0.0, (t1 - t0) / 3600.0)
        if hours <= 0:
            continue
        hours_total += hours
        for item_id, price in second.items():
            before = first.get(item_id)
            if not before or before <= 0:
                continue
            exposures += 1
            if price < before:
                drops += 1
    if not exposures or hours_total <= 0:
        return None
    rate = drops / exposures / (hours_total / len(states[1:]))  # per hour
    probability = 1.0 - math.exp(-rate * horizon_h)
    return {
        "probability": round(min(0.99, max(0.0, probability)), 4),
        "drops": drops,
        "exposures": exposures,
        "snapshots": len(states),
        "hours": round(hours_total, 2),
    }


def build_index(engine, region_prices: Dict[int, int],
                con: Optional[sqlite3.Connection],
                realms: Optional[Sequence[str]] = None,
                horizon_h: float = 24.0) -> Dict[str, Dict[str, Any]]:
    """The compact per-realm block the dataset ships."""
    names = list(realms) if realms else sorted(getattr(engine, "_history_cache", {}))
    factors = price_factors(engine, region_prices, names)
    index: Dict[str, Dict[str, Any]] = {}
    for realm, data in factors.items():
        share = undercut_share(con, realm) if con is not None else None
        risk = babysit_risk(con, realm, horizon_h) if con is not None else None
        entry = {
            "factor": data["factor"],
            "sample": data["sample"],
            "source": data["source"],
        }
        if data.get("median_ratio") is not None:
            entry["medianRatio"] = data["median_ratio"]
        if share is not None:
            entry["undercutShare"] = share["share"]
            entry["undercutWatched"] = share["watched"]
            entry["undercutGapPct"] = share["avg_gap_pct"]
            entry["itemsAhead"] = share["avg_items_ahead"]
        if risk is not None:
            entry["babysit24h"] = risk["probability"]
            entry["babysitSamples"] = risk["exposures"]
        index[realm] = entry
    return index


def best_realms(item_id: int, regional_price: int, index: Dict[str, Dict[str, Any]],
                records: Optional[Dict[str, Dict[int, Any]]] = None,
                limit: int = 3) -> List[Dict[str, Any]]:
    """Realms ranked by estimated price discounted by undercut share.

    ``records`` (realm -> item -> estimate) wins where it exists; every
    other realm is ``regional price x factor``.  The score is not a sale
    probability: it is what the listing is worth if it sells, discounted
    by how often the realm undercuts the ask.
    """
    if regional_price <= 0:
        return []
    ranked = []
    for realm, entry in index.items():
        record = ((records or {}).get(realm) or {}).get(item_id)
        price = int(getattr(record, "price", 0) or 0) if record else 0
        if price <= 0:
            price = int(round(regional_price * float(entry.get("factor") or 1.0)))
        if price <= 0:
            continue
        share = entry.get("undercutShare")
        risk = share if isinstance(share, (int, float)) else None
        score = price * (1.0 - 0.5 * float(risk or 0.0))
        ranked.append({
            "realm": realm,
            "price_copper": price,
            "factor": entry.get("factor"),
            "undercut_share": risk,
            "babysit24h": entry.get("babysit24h"),
            "score": round(score),
            "measured": bool(entry.get("undercutWatched"))
                        or entry.get("source") == "realm",
        })
    # Ties are the norm for the 259 realms without their own sales: they all
    # price at the regional baseline.  A measured realm wins a tie, and the
    # rest are ordered by name so the same item always gives the same answer.
    ranked.sort(key=lambda row: (row["score"], row["measured"], row["realm"]),
                reverse=True)
    return ranked[:max(1, limit)]
