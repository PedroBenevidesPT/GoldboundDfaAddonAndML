"""The relist advice, written down before the outcome is known.

``relist.advise`` ranks open listings today; the next scan produces a new
ranking and the old one is gone.  That is enough to decide what to look at
now and not enough to answer the only question that matters later: *did the
advice work?*  Remembering the ranking is not evidence, because the ranking
that looks good is the one rewritten after the sales are seen.

So every recommendation is stored here with its inputs, at the time it was
made, and the outcome is attached afterwards from the confirmed events
(``auction_events`` / ``auction_intel_events``).  Two properties are
deliberate:

* **append-only in spirit.**  A new recommendation for the same auction
  supersedes the old one; it does not edit it.  Rows are never deleted, and
  an outcome is written once (``WHERE outcome = 'open'``).
* **saying what cannot be concluded.**  A ``cancel_and_relist`` followed by
  a cancel event is the advice being *followed*, not a failure; the relisted
  auction is a different auction id and has its own row.  Only ``hold`` and
  ``watch`` have an unconfounded natural outcome.  :func:`summary` states
  that instead of pooling everything into a sale rate.

Matching an outcome is the same problem as matching a sale to a posting:
the journal carries no auction id, only item, quantity, price, character
and time.  The price identifies the listing exactly in almost every case
(99.4 % measured, ``reports/phase_report_2026-10-22.md`` §2.2).  When two
open listings share the item, character and unit price, this module refuses
to guess and leaves both open with an ``ambiguous`` evidence line.

This is analysis only: nothing here cancels, posts or undercuts.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from woweconomy.dbcompat import connect_store, missing_table_errors, store_exists

OPEN = "open"
SOLD = "sold"
EXPIRED = "expired"
CANCELLED = "cancelled"
OUTCOMES = (OPEN, SOLD, EXPIRED, CANCELLED)

#: journal event type -> ledger outcome
_EVENT_OUTCOME = {"sale": SOLD, "expire": EXPIRED, "cancel": CANCELLED}

#: a sale's net proceeds are the gross minus the auction house cut; used
#: only when the event does not carry its own net figure
AH_CUT = 0.05

#: an outcome further than this from the scan is not this listing's
DEFAULT_WINDOW_DAYS = 14


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


def decision_id(row: Mapping[str, Any], realm: str) -> str:
    """A stable id for one recommendation about one observation.

    Re-recording the same scan is a no-op; a later scan of the same auction
    gets a different id and supersedes the earlier one.  An observation
    without an auction id (a price-only row) is keyed by the scan and the
    ask, because there is no listing to follow.
    """
    own = _as_int(row.get("own_auction_id"))
    if own > 0:
        key = "|".join((str(realm).lower(), str(own),
                        str(row.get("scan_id") or row.get("observed_at") or "")))
    else:
        key = "|".join((str(realm).lower(), "item",
                        str(_as_int(row.get("item_id"))),
                        str(_as_int(row.get("observed_at"))),
                        str(_as_int(row.get("own_unit_price_copper")))))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def record(con, advice: Mapping[str, Any], realm: str = "",
           now: Optional[int] = None) -> Dict[str, int]:
    """Store every row of a ``relist.advise`` result, idempotently.

    ``advice`` is the whole payload (or just a mapping with an ``advice``
    list).  The realm defaults to the payload's ``realm``; ``"all"`` is not
    a realm and is treated as empty.
    """
    now = int(time.time() if now is None else now)
    target = str(realm or advice.get("realm") or "").strip()
    if target.lower() == "all":
        target = ""
    rows = advice.get("advice") or []
    recorded = present = superseded = 0
    for row in rows:
        did = decision_id(row, target)
        own_id = _as_int(row.get("own_auction_id"))
        market = row.get("market")
        values = (
            did, _as_int(row.get("decided_at"), now),
            _as_int(row.get("observed_at")), target,
            str(row.get("character") or ""), str(row.get("scan_id") or ""),
            own_id, _as_int(row.get("item_id")),
            str(row.get("item_name") or ""), str(row.get("advice") or ""),
            str(row.get("reason") or ""),
            str(row.get("reference_status") or ""),
            str(row.get("relation") or ""),
            _as_int(row.get("own_unit_price_copper")),
            _as_int(row.get("lowest_unit_price_copper")),
            _as_int(row.get("own_quantity"), 1),
            float(_as_float(row.get("gap_pct"), 0.0) or 0.0),
            _as_int(row.get("items_ahead")),
            _as_float(row.get("hours_left")),
            (None if row.get("suggested_ask_copper") is None
             else _as_int(row.get("suggested_ask_copper"))),
            (json.dumps(market, sort_keys=True, default=str)
             if isinstance(market, (dict, list)) else ""),
            1 if row.get("review_required") else 0,
        )
        cur = con.execute(
            "INSERT INTO relist_decision (decision_id, decided_at, observed_at,"
            " realm, character, scan_id, own_auction_id, item_id, item_name,"
            " advice, reason, reference_status, relation,"
            " own_unit_price_copper, lowest_unit_price_copper, own_quantity,"
            " gap_pct, items_ahead, hours_left, suggested_ask_copper,"
            " market_json, review_required)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (decision_id) DO NOTHING", values)
        if cur.rowcount:
            recorded += 1
        else:
            present += 1
        if own_id > 0:
            if cur.rowcount:
                # The latest advice is the one evaluated.  The earlier row is
                # kept, with its outcome if it already had one, but it is no
                # longer the standing recommendation for this auction.
                cur = con.execute(
                    "UPDATE relist_decision SET superseded = 1"
                    " WHERE LOWER(realm) = LOWER(?) AND own_auction_id = ?"
                    " AND decision_id <> ? AND superseded = 0",
                    (target, own_id, did))
                superseded += max(0, int(cur.rowcount or 0))
            else:
                # Replaying the same scan must not move the standing
                # recommendation.  Without this branch, a second backfill
                # processed the oldest scan first and marked the *current*
                # row superseded before the replay reached it: 219 auctions
                # ended with no recommendation in force.  Recomputing the
                # winner from the rows is idempotent and repairs rows already
                # inverted by an earlier replay.
                con.execute(
                    "UPDATE relist_decision SET superseded = CASE WHEN"
                    " decision_id = (SELECT d2.decision_id FROM relist_decision d2"
                    " WHERE LOWER(d2.realm) = LOWER(?) AND d2.own_auction_id = ?"
                    " ORDER BY d2.observed_at DESC, d2.decided_at DESC,"
                    " d2.decision_id DESC LIMIT 1) THEN 0 ELSE 1 END"
                    " WHERE LOWER(realm) = LOWER(?) AND own_auction_id = ?",
                    (target, own_id, target, own_id))
    con.commit()
    return {"recorded": recorded, "already_present": present,
            "superseded": superseded, "realm": target}


def _events(tsm_db: Any, realm: str, start: int,
            end: int) -> List[Dict[str, Any]]:
    """Confirmed journal events in a window, normalised across both tables.

    Expiries and cancellations are stored with no price at all (measured on
    the live journal: every ``expire``/``cancel`` row has
    ``unit_price_copper = 0``), so a price cannot be required to match
    them.  They are still returned, with ``unit = 0`` left as the marker
    that the event says nothing about which price it was.
    """
    if not store_exists(tsm_db):
        return []
    con = connect_store(tsm_db, readonly=True)
    out: List[Dict[str, Any]] = []
    try:
        where = " WHERE ts >= ? AND ts <= ?"
        params: List[Any] = [start, end]
        if realm:
            where += " AND LOWER(realm) = LOWER(?)"
            params.append(realm)
        try:
            rows = con.execute(
                "SELECT event_type, ts, item_id, quantity,"
                " unit_price_copper, total_price_copper, net_proceeds_copper,"
                " realm, character FROM auction_intel_events" + where,
                params).fetchall()
            for row in rows:
                out.append({
                    "event_type": str(row[0] or "").lower(),
                    "ts": _as_int(row[1]), "item_id": _as_int(row[2]),
                    "qty": _as_int(row[3], 1), "unit": _as_int(row[4]),
                    "total": _as_int(row[5]),
                    "net": _as_int(row[6]) if row[6] is not None else None,
                    "realm": str(row[7] or ""), "character": str(row[8] or ""),
                    "source": "auction_intel_events",
                })
        except missing_table_errors():
            pass
        # ``ts`` is an ISO string in the legacy table, so the window is
        # filtered on ``timestamp_unix`` there.
        where = " WHERE timestamp_unix >= ? AND timestamp_unix <= ?"
        params = [start, end]
        if realm:
            where += " AND LOWER(realm) = LOWER(?)"
            params.append(realm)
        try:
            rows = con.execute(
                "SELECT event_type, timestamp_unix, item_id, qty,"
                " unit_price_copper, total_price_copper, net_proceeds_copper,"
                " realm, character FROM auction_events" + where,
                params).fetchall()
            for row in rows:
                out.append({
                    "event_type": str(row[0] or "").lower(),
                    "ts": _as_int(row[1]), "item_id": _as_int(row[2]),
                    "qty": _as_int(row[3], 1), "unit": _as_int(row[4]),
                    "total": _as_int(row[5]),
                    "net": _as_int(row[6]) if row[6] is not None else None,
                    "realm": str(row[7] or ""), "character": str(row[8] or ""),
                    "source": "auction_events",
                })
        except missing_table_errors():
            pass
    finally:
        try:
            con.close()
        except Exception:  # pragma: no cover - defensive
            pass
    # A sale always carries a unit price; the legacy table occasionally
    # keeps only the total, so derive the unit from it when it can.
    for event in out:
        if event["unit"] <= 0 and event["total"] > 0:
            event["unit"] = int(round(event["total"] / max(1, event["qty"])))
    # The addon import feeds one table and the legacy importer the other;
    # the same sale can therefore appear twice.  Deduplicate on the facts
    # this module matches on, preferring the priced copy.
    seen = set()
    unique: List[Dict[str, Any]] = []
    for event in sorted(out, key=lambda e: (e["ts"], e["event_type"])):
        key = (event["event_type"], event["ts"], event["item_id"],
               event["qty"], event["unit"], event["character"].casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


def link_outcomes(con, tsm_db: Any, realm: str = "",
                  window_days: int = DEFAULT_WINDOW_DAYS,
                  now: Optional[int] = None) -> Dict[str, int]:
    """Attach confirmed events to the recommendation standing at the time.

    An auction observed on three scans has three recommendations.  The
    event belongs to the **latest one taken before it**: that is the advice
    that was in force when the listing sold or expired, so it is the one
    judged.  Earlier rows were already superseded and are not evidence for
    anything.

    Only an event *after* that scan can be its outcome.  When two open
    auctions share item, character and unit price, the event cannot be
    attributed without guessing; both stay open and are counted as
    ambiguous.
    """
    now = int(time.time() if now is None else now)
    sql = ("SELECT * FROM relist_decision WHERE outcome = 'open'"
           " AND own_auction_id > 0")
    params: List[Any] = []
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        params.append(realm)
    decisions = [dict(row) for row in con.execute(sql, params).fetchall()]
    linked = ambiguous = 0
    by_realm: Dict[str, List[Dict[str, Any]]] = {}
    for decision in decisions:
        by_realm.setdefault(str(decision.get("realm") or ""), []).append(decision)

    for target, rows in by_realm.items():
        start = min(_as_int(row.get("observed_at"), now) for row in rows)
        span = max(1, int(window_days)) * 86400
        end = min(now, max(_as_int(row.get("observed_at"), now) for row in rows) + span)
        events = _events(tsm_db, target, start, end)
        exact: Dict[Tuple[str, int, int], List[Dict[str, Any]]] = {}
        unpriced: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        all_priced: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        all_unpriced: Dict[int, List[Dict[str, Any]]] = {}
        wild: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        wild_unpriced: Dict[int, List[Dict[str, Any]]] = {}
        for event in events:
            if event["event_type"] not in _EVENT_OUTCOME:
                continue
            character = event["character"].casefold()
            if event["unit"] > 0:
                all_priced.setdefault(
                    (event["item_id"], event["unit"]), []).append(event)
                key = (character, event["item_id"], event["unit"])
                if event["character"]:
                    exact.setdefault(key, []).append(event)
                else:
                    wild.setdefault((event["item_id"], event["unit"]), []).append(event)
            else:
                # expire/cancel: the journal does not record the price the
                # listing had, so it can only be attributed to the one open
                # auction of that item (and character, when known).
                all_unpriced.setdefault(event["item_id"], []).append(event)
                key = (character, event["item_id"])
                if event["character"]:
                    unpriced.setdefault(key, []).append(event)
                else:
                    wild_unpriced.setdefault(event["item_id"], []).append(event)

        # One auction can have several open recommendations (a rescan);
        # the episode is the auction, not the row.
        by_auction: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            by_auction.setdefault(_as_int(row.get("own_auction_id")), []).append(row)

        # A price shared by two of our open auctions (different characters
        # included) cannot identify which one an event belonged to.  These
        # are counts of auctions, not of scans.
        price_auctions: Dict[Tuple[int, int], set] = {}
        char_price_auctions: Dict[Tuple[str, int, int], set] = {}
        char_item_auctions: Dict[Tuple[str, int], set] = {}
        item_auctions: Dict[int, set] = {}
        for auction_id, group in by_auction.items():
            row = group[-1]
            price_key = (_as_int(row.get("item_id")),
                         _as_int(row.get("own_unit_price_copper")))
            char_key = (str(row.get("character") or "").casefold(),
                        _as_int(row.get("item_id")))
            item = _as_int(row.get("item_id"))
            price_auctions.setdefault(price_key, set()).add(auction_id)
            char_price_auctions.setdefault(
                (char_key[0], item, price_key[1]), set()).add(auction_id)
            char_item_auctions.setdefault(char_key, set()).add(auction_id)
            item_auctions.setdefault(item, set()).add(auction_id)

        for auction_id, group in by_auction.items():
            group.sort(key=lambda row: _as_int(row.get("observed_at"), now))
            last = group[-1]
            item_id = _as_int(last.get("item_id"))
            unit = _as_int(last.get("own_unit_price_copper"))
            character = str(last.get("character") or "").casefold()
            item_price = (item_id, unit)
            char_item = (character, item_id)
            anonymous = not character
            priced_auctions = (price_auctions if anonymous
                               else char_price_auctions)
            priced_key = (item_price if anonymous
                          else (character, item_id, unit))
            if len(priced_auctions.get(priced_key, ())) > 1:
                if exact.get((character, item_id, unit)) or all_priced.get(item_price):
                    for row in group:
                        _mark_ambiguous(con, row,
                                        len(priced_auctions.get(priced_key, ())))
                    ambiguous += 1
                    continue
            # Most Blizzard-scan rows carry no character at all (1687 of
            # 1773 on the live store), so an empty character cannot narrow
            # the match; the item and price do.
            candidates = list(exact.get((character, item_id, unit), []))
            if len(price_auctions.get(item_price, ())) == 1:
                candidates.extend(wild.get(item_price, []))
                if anonymous:
                    candidates.extend(all_priced.get(item_price, []))
            if not candidates:
                unique = (len(item_auctions.get(item_id, ())) == 1
                          if anonymous else
                          len(char_item_auctions.get(char_item, ())) == 1)
                if unique:
                    candidates.extend(unpriced.get(char_item, []))
                    candidates.extend(wild_unpriced.get(item_id, []))
                    if anonymous:
                        candidates.extend(all_unpriced.get(item_id, []))
                elif (unpriced.get(char_item) or wild_unpriced.get(item_id)):
                    for row in group:
                        _mark_ambiguous(con, row, len(
                            item_auctions.get(item_id, ()) or [0]))
                    ambiguous += 1
                    continue
            first_scan = _as_int(group[0].get("observed_at"), now)
            candidates = [event for event in candidates
                          if first_scan <= event["ts"]
                          <= min(now, first_scan + span)]
            if not candidates:
                continue
            event = min(candidates, key=lambda e: e["ts"])
            # The recommendation in force when the event happened: the last
            # scan at or before it.
            in_force = [row for row in group
                        if _as_int(row.get("observed_at"), now) <= event["ts"]]
            if not in_force:
                continue
            _apply_outcome(con, in_force[-1], event)
            linked += 1
    con.commit()
    remaining = con.execute(
        "SELECT COUNT(*) FROM relist_decision WHERE outcome = 'open'"
        + (" AND LOWER(realm) = LOWER(?)" if realm else ""),
        ([realm] if realm else [])).fetchone()[0]
    return {"linked": linked, "ambiguous_auctions": ambiguous,
            "still_open": int(remaining or 0)}


def _mark_ambiguous(con, row: Mapping[str, Any], count: int) -> None:
    con.execute(
        "UPDATE relist_decision SET evidence = ? WHERE decision_id = ?"
        " AND outcome = 'open'",
        (f"ambiguous: {count} open listings share this item"
         + (" and character" if str(row.get("character") or "") else "")
         + "; the event cannot choose between them",
         str(row.get("decision_id"))))


def _apply_outcome(con, decision: Mapping[str, Any],
                   event: Mapping[str, Any]) -> None:
    outcome = _EVENT_OUTCOME[event["event_type"]]
    total = _as_int(event.get("total"))
    if total <= 0:
        total = _as_int(event.get("unit")) * max(1, _as_int(event.get("qty"), 1))
    net = event.get("net")
    realised = 0
    if outcome == SOLD:
        realised = (int(net) if net else int(round(total * (1 - AH_CUT))))
        realised = max(0, realised)
    if _as_int(event.get("unit")) > 0:
        evidence = "matched on realm, item, character and unit price"
        if not str(decision.get("character") or ""):
            evidence = ("matched on realm, item and unit price; the scan"
                        " carries no character")
    else:
        evidence = ("matched on realm, item and character; the event carries"
                    " no price (expire/cancel)")
        if not str(decision.get("character") or ""):
            evidence = ("matched on realm and item only; the scan carries no"
                        " character and the event carries no price")
    con.execute(
        "UPDATE relist_decision SET outcome = ?, outcome_at = ?,"
        " outcome_source = ?, outcome_price_copper = ?, outcome_qty = ?,"
        " realised_copper = ?, evidence = ?"
        " WHERE decision_id = ? AND outcome = 'open'",
        (outcome, _as_int(event.get("ts")), str(event.get("source") or ""),
         total, _as_int(event.get("qty"), 1), realised,
         evidence, str(decision.get("decision_id"))))


def backfill(con, realm: str = "", days: int = 30,
             now: Optional[int] = None) -> Dict[str, int]:
    """Replay stored scans at the time they were taken.

    The advice in the ledger must be the advice that could have been given
    then, not a ranking recomputed with today's clock.  ``relist.decide_row``
    takes the observation's own time for that reason.  The measured market
    context (noise, trend, own share) is not reconstructible from history,
    so a backfilled row is decided by the gap/queue/time rules and its
    ``market_json`` is empty -- a weaker record, and labelled by that fact.
    """
    from woweconomy.intel import relist

    now = int(time.time() if now is None else now)
    cutoff = now - max(1, int(days)) * 86400
    sql = "SELECT * FROM undercut_observation WHERE observed_at >= ?"
    params: List[Any] = [cutoff]
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        params.append(realm)
    sql += " ORDER BY observed_at, observation_id"
    rows = con.execute(sql, params).fetchall()
    recorded = present = superseded = 0
    for raw in rows:
        row = dict(raw)
        target = str(row.get("realm") or realm or "")
        decision = relist.decide_row(row, now=_as_int(row.get("observed_at"), now))
        # The recommendation is dated at the scan, not at the replay: a
        # summary window and an outcome both read ``decided_at``.
        result = record(con, {"advice": [decision]}, target,
                        now=_as_int(row.get("observed_at"), now))
        recorded += result["recorded"]
        present += result["already_present"]
        superseded += result["superseded"]
    return {"scans": len(rows), "recorded": recorded,
            "already_present": present, "superseded": superseded}


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def _median_hours(rows: Iterable[Mapping[str, Any]]) -> Optional[float]:
    hours = []
    for row in rows:
        ended = _as_int(row.get("outcome_at"))
        started = _as_int(row.get("observed_at"))
        if ended and started and ended >= started:
            hours.append((ended - started) / 3600.0)
    return round(statistics.median(hours), 2) if hours else None


def summary(con, realm: str = "", days: int = 30,
            now: Optional[int] = None) -> Dict[str, Any]:
    """What the recommendations led to, by advice, with the caveats stated."""
    now = int(time.time() if now is None else now)
    cutoff = now - max(1, int(days)) * 86400
    # A resolved row is evidence even when a later scan superseded it: it
    # was the advice in force when the listing sold, expired or was
    # cancelled.  An *open* superseded row was replaced before anything
    # happened and is not evidence for anything.
    sql = ("SELECT * FROM relist_decision"
           " WHERE (superseded = 0 OR outcome <> 'open')"
           " AND decided_at >= ?")
    params: List[Any] = [cutoff]
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        params.append(realm)
    rows = [dict(row) for row in con.execute(sql, params).fetchall()]

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("advice") or ""), []).append(row)

    by_advice: Dict[str, Dict[str, Any]] = {}
    for advice, group in sorted(buckets.items()):
        sold = sum(1 for row in group if row.get("outcome") == SOLD)
        expired = sum(1 for row in group if row.get("outcome") == EXPIRED)
        cancelled = sum(1 for row in group if row.get("outcome") == CANCELLED)
        open_rows = [row for row in group if row.get("outcome") == OPEN]
        resolved = sold + expired + cancelled
        realised = sum(_as_int(row.get("realised_copper")) for row in group
                       if row.get("outcome") == SOLD)
        by_advice[advice] = {
            "recommendations": len(group),
            "sold": sold, "expired": expired, "cancelled": cancelled,
            "open": len(open_rows),
            "ambiguous": sum(1 for row in open_rows
                             if str(row.get("evidence") or "").startswith("ambiguous")),
            "resolved": resolved,
            "sale_rate": _rate(sold, resolved),
            "gold_per_resolved": (round(realised / resolved, 2)
                                  if resolved else None),
            "median_hours_to_outcome": _median_hours(
                row for row in group if row.get("outcome") != OPEN),
        }

    hold_rows = [row for row in rows
                 if row.get("advice") in ("hold", "watch")
                 and row.get("outcome") != OPEN]
    relist_rows = [row for row in rows
                   if row.get("advice") == "cancel_and_relist"
                   and row.get("outcome") != OPEN]
    readings = {
        "hold_or_watch_sale_rate": _rate(
            sum(1 for row in hold_rows if row.get("outcome") == SOLD),
            len(hold_rows)),
        "hold_or_watch_resolved": len(hold_rows),
        "relist_followed_share": _rate(
            sum(1 for row in relist_rows if row.get("outcome") == CANCELLED),
            len(relist_rows)),
        "relist_resolved": len(relist_rows),
        "note": ("these are recommendations, not orders. A cancel after"
                 " cancel_and_relist is the advice being followed (the"
                 " relisted auction is a new row); only hold/watch have an"
                 " unconfounded natural outcome, and only over resolved"
                 " listings. No conclusion is drawn from a single listing."),
    }
    return {
        "realm": realm or "all",
        "days": int(days),
        "recommendations": len(rows),
        "by_advice": by_advice,
        "readings": readings,
        "outcome_counts": {
            "open": sum(1 for row in rows if row.get("outcome") == OPEN),
            "sold": sum(1 for row in rows if row.get("outcome") == SOLD),
            "expired": sum(1 for row in rows if row.get("outcome") == EXPIRED),
            "cancelled": sum(1 for row in rows if row.get("outcome") == CANCELLED),
        },
    }
