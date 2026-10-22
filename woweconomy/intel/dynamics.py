"""Per-realm price dynamics, competition, liquidity and cancel response.

Every metric here is computed for one (region, realm).  Nothing is
averaged across realms, and nothing is averaged across *items* without
saying so.

The departure rule
------------------
Two snapshots show a listing in the first and not the second.  That is a
**departure**.  It could have sold, expired, been cancelled, or been
moved; the snapshots cannot tell the difference.  Departures are reported
as ``unresolved_departures`` with their units, and only the journal's
confirmed sale events feed the liquidity numbers.

Undercuts and competition
-------------------------
The listing ladder gives an undercut measurement without needing seller
names: the cheapest price, the next price, and how many units sit at the
floor.  When seller observations exist, the distinct-seller count and its
concentration are added; when they do not (commodities, or a UI that did
not show a name) the field is ``None``, never zero.

Cancel scans
------------
Our cancel events come from the journal.  The response is measured as the
change in the item's ladder between the snapshot before the cancel and
the first snapshot after it.  It is a *description*, not a causal claim:
another seller may have moved first.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from woweconomy.ml.journalator import Failure, Invoice, JournalArchive, Posting
from woweconomy.ml.linking import CENSORED, CANCELLED, EXPIRED, SALE, link_postings

from . import ah as ah_module
from . import survival

HORIZONS_H = (24, 72, 168, 336)


def _price(row: Dict[str, Any]) -> Optional[int]:
    value = row.get("unit_price_copper")
    return int(value) if value is not None else None


def _median(values: Sequence[int]) -> Optional[int]:
    return int(statistics.median(values)) if values else None


def _item_groups(rows: Iterable[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(int(row["item_id"]), []).append(row)
    for group in groups.values():
        group.sort(key=lambda r: (_price(r) is None, _price(r), -int(r["quantity"])))
    return groups


def market_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate ladder metrics for one snapshot."""
    prices = [p for p in (_price(r) for r in rows) if p is not None]
    items = _item_groups(rows)
    at_floor_units = 0
    undercut_gaps: List[float] = []
    for group in items.values():
        priced = [r for r in group if _price(r) is not None]
        if not priced:
            continue
        floor = _price(priced[0])
        at_floor_units += sum(int(r["quantity"]) for r in priced
                              if _price(r) == floor)
        if len(priced) > 1:
            second = _price(priced[1])
            if second:
                undercut_gaps.append((second - floor) / second * 100.0)
    return {
        "listings": len(rows),
        "items": len(items),
        "units": sum(int(r["quantity"]) for r in rows),
        "priced_listings": len(prices),
        "min_unit_price": min(prices) if prices else None,
        "median_unit_price": _median(prices),
        "p25_unit_price": _percentile(prices, 25),
        "p75_unit_price": _percentile(prices, 75),
        "median_undercut_gap_pct": (round(statistics.median(undercut_gaps), 3)
                                    if undercut_gaps else None),
        "share_units_at_floor": (round(at_floor_units / max(1, sum(
            int(r["quantity"]) for r in rows)), 4)),
        "time_left": _time_left_counts(rows),
    }


def _percentile(values: Sequence[int], pct: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return int(ordered[index])


def _time_left_counts(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        key = row.get("time_left_raw")
        key = str(key) if key is not None else "missing"
        out[key] = out.get(key, 0) + 1
    return out


def item_metrics(rows: Sequence[Dict[str, Any]], item_id: int,
                 seller_rows: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The ladder of one item, including the undercut measurement."""
    group = [r for r in rows if int(r["item_id"]) == int(item_id)]
    priced = sorted((r for r in group if _price(r) is not None),
                    key=lambda r: _price(r))
    floor = _price(priced[0]) if priced else None
    second = _price(priced[1]) if len(priced) > 1 else None
    units = sum(int(r["quantity"]) for r in group)
    floor_units = sum(int(r["quantity"]) for r in priced if _price(r) == floor)
    sellers = None
    seller_share = None
    if seller_rows is not None:
        names = [r.get("seller") for r in seller_rows
                 if int(r.get("item_id") or 0) == int(item_id) and r.get("seller")]
        if names:
            sellers = len(set(names))
            counts: Dict[str, int] = {}
            for name in names:
                counts[name] = counts.get(name, 0) + 1
            seller_share = round(max(counts.values()) / len(names), 4)
    return {
        "item_id": int(item_id),
        "listings": len(group),
        "units": units,
        "min_unit_price": floor,
        "second_unit_price": second,
        "undercut_gap_pct": (round((second - floor) / second * 100.0, 3)
                             if floor and second else None),
        "units_at_floor": floor_units,
        "share_units_at_floor": round(floor_units / units, 4) if units else None,
        "distinct_sellers": sellers,
        "top_seller_share": seller_share,
        "seller_basis": ("observed seller names" if sellers is not None
                         else "unavailable: no seller identity for this book"),
    }


def compare_snapshots(prev_rows: Sequence[Dict[str, Any]],
                      cur_rows: Sequence[Dict[str, Any]],
                      gap_hours: Optional[float] = None) -> Dict[str, Any]:
    """What changed between two snapshots -- with departures kept neutral."""
    prev_items = _item_groups(prev_rows)
    cur_items = _item_groups(cur_rows)
    items: List[Dict[str, Any]] = []
    for item_id in sorted(set(prev_items) | set(cur_items)):
        before = prev_items.get(item_id, [])
        after = cur_items.get(item_id, [])
        before_prices = [p for p in (_price(r) for r in before) if p is not None]
        after_prices = [p for p in (_price(r) for r in after) if p is not None]
        before_min = min(before_prices) if before_prices else None
        after_min = min(after_prices) if after_prices else None
        change = None
        if before_min and after_min:
            change = (after_min - before_min) / before_min
        gone = len([r for r in before if r.get("auction_key")
                    not in {x.get("auction_key") for x in after}])
        new = len([r for r in after if r.get("auction_key")
                   not in {x.get("auction_key") for x in before}])
        items.append({
            "item_id": item_id,
            "prev_listings": len(before),
            "cur_listings": len(after),
            "prev_min_unit_price": before_min,
            "cur_min_unit_price": after_min,
            "min_price_change_pct": (round(change * 100.0, 3)
                                     if change is not None else None),
            "new_listings": new,
            "unresolved_departures": gone,
            "departed_units": sum(int(r["quantity"]) for r in before
                                  if r.get("auction_key")
                                  not in {x.get("auction_key") for x in after}),
        })
    return {
        "gap_hours": round(gap_hours, 2) if gap_hours is not None else None,
        "items": items,
        "new_listings": sum(i["new_listings"] for i in items),
        "unresolved_departures": sum(i["unresolved_departures"] for i in items),
        "departed_units": sum(i["departed_units"] for i in items),
        "note": ("a departure is not a sale: the snapshots cannot tell a sale"
                 " from an expiry, a cancel or a move"),
    }


# ---------------------------------------------------------------------------
# journal -> survival
# ---------------------------------------------------------------------------

def events_to_archive(events: Sequence[Dict[str, Any]]) -> JournalArchive:
    """Adapt the addon/legacy event rows to the existing journal types."""
    archive = JournalArchive()
    for event in events:
        kind = str(event.get("event_type") or "").lower()
        ts = int(event.get("ts") or event.get("timestamp_unix") or 0)
        item_id = event.get("item_id")
        if item_id is None:
            continue
        realm = str(event.get("realm") or "")
        character = str(event.get("character") or "")
        item_name = str(event.get("item_name") or "")
        qty = max(1, int(event.get("quantity") or event.get("qty") or 1))
        unit = int(event.get("unit_price_copper") or 0)
        total = int(event.get("total_price_copper") or unit * qty)
        if kind == "listing":
            archive.postings.append(Posting(
                time=ts, realm=realm, character=character, faction="",
                item_name=item_name, item_id=int(item_id), qty=qty,
                unit_buyout=unit, unit_bid=int(event.get("bid_copper") or 0),
                deposit=int(event.get("deposit_copper") or 0)))
        elif kind == "sale":
            archive.invoices.append(Invoice(
                time=ts, realm=realm, character=character, faction="",
                item_name=item_name, item_id=int(item_id), qty=qty,
                invoice_type="seller", value=total,
                consignment=int(event.get("consignment_fee_copper")
                                or event.get("fee_copper") or 0),
                deposit=int(event.get("deposit_copper") or 0),
                counterparty=str(event.get("counterparty") or "")))
        elif kind in ("cancel", "expire"):
            archive.failures.append(Failure(
                time=ts, realm=realm, character=character, faction="",
                item_name=item_name, item_id=int(item_id), qty=qty,
                failed_type="cancelled" if kind == "cancel" else "expired"))
    return archive


def liquidity_from_events(events: Sequence[Dict[str, Any]],
                          realm: str,
                          horizons: Sequence[int] = HORIZONS_H) -> Dict[str, Any]:
    """Confirmed sales vs expiries, plus the censored time-to-sale curve."""
    realm_events = [e for e in events
                    if str(e.get("realm") or "").casefold() == realm.casefold()]
    sales = sum(1 for e in realm_events if str(e.get("event_type")).lower() == "sale")
    expiries = sum(1 for e in realm_events if str(e.get("event_type")).lower() == "expire")
    cancels = sum(1 for e in realm_events if str(e.get("event_type")).lower() == "cancel")
    archives = events_to_archive(realm_events)
    outcomes, link_report = link_postings(archives)
    durations: List[float] = []
    flags: List[int] = []
    for out in outcomes:
        if out.outcome == SALE:
            durations.append(out.elapsed / 3600.0)
            flags.append(1)
        elif out.outcome in (EXPIRED, CENSORED):
            durations.append(out.elapsed / 3600.0)
            flags.append(0)
        # a cancellation is the seller changing their mind: no information
    km = survival.kaplan_meier(durations, flags)
    observed = sales + expiries
    return {
        "realm": realm,
        "confirmed_sales": sales,
        "expiries": expiries,
        "cancellations": cancels,
        "sale_rate": round(sales / observed, 4) if observed else None,
        "sale_rate_note": ("sales / (sales + expiries); cancellations are"
                           " excluded because they are not demand"),
        "censored_listings": int(link_report.censored),
        "time_to_sale": km.as_dict(),
        "sale_probability_by_h": survival.sale_probability_by(durations, flags, horizons),
        "hazard_by_h": survival.hazard_curve(durations, flags, horizons),
        "linked": link_report.summary(),
    }


def cancel_scan_response(events: Sequence[Dict[str, Any]],
                         snapshots: Sequence[Tuple[int, Sequence[Dict[str, Any]]]],
                         realm: str, response_window_h: float = 6.0) -> List[Dict[str, Any]]:
    """For each cancel, compare the item's ladder before and after.

    ``snapshots`` is a list of ``(captured_at, rows)`` in any order.  A
    cancel with no snapshot on both sides is reported as unobserved
    instead of being dropped.
    """
    ordered = sorted(snapshots, key=lambda pair: pair[0])
    out: List[Dict[str, Any]] = []
    cancels = [e for e in events
               if str(e.get("event_type")).lower() == "cancel"
               and str(e.get("realm") or "").casefold() == realm.casefold()]
    for event in cancels:
        ts = int(event.get("ts") or 0)
        item_id = event.get("item_id")
        if item_id is None:
            continue
        before = [s for s in ordered if s[0] <= ts]
        after = [s for s in ordered
                 if ts < s[0] <= ts + response_window_h * 3600]
        entry: Dict[str, Any] = {
            "ts": ts, "item_id": int(item_id),
            "quantity": int(event.get("quantity") or 0),
            "observed": bool(before and after),
            "response_window_h": response_window_h,
        }
        if not before or not after:
            entry["note"] = "no snapshot on both sides of the cancel"
            out.append(entry)
            continue
        prev_ts, prev_rows = before[-1]
        cur_ts, cur_rows = after[0]
        prev_item = [r for r in prev_rows if int(r["item_id"]) == int(item_id)]
        cur_item = [r for r in cur_rows if int(r["item_id"]) == int(item_id)]
        entry.update({
            "prev_snapshot_at": prev_ts, "next_snapshot_at": cur_ts,
            "prev_listings": len(prev_item), "next_listings": len(cur_item),
            "prev_min_unit_price": min((p for p in (_price(r) for r in prev_item)
                                        if p is not None), default=None),
            "next_min_unit_price": min((p for p in (_price(r) for r in cur_item)
                                        if p is not None), default=None),
            "note": ("describes the ladder around the cancel; other sellers may"
                     " have moved first, so this is not a causal effect"),
        })
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# the assembled per-realm view
# ---------------------------------------------------------------------------

def realm_dynamics(con, region: str, realm: str,
                   events: Sequence[Dict[str, Any]] = (),
                   seller_rows: Sequence[Dict[str, Any]] = (),
                   horizons: Sequence[int] = HORIZONS_H) -> Dict[str, Any]:
    """One realm, one report.  No cross-realm pooling anywhere."""
    latest = ah_module.latest_snapshot(con, region, realm)
    if not latest:
        return {"region": region, "realm": realm, "available": False,
                "reason": "no auction snapshot ingested for this realm"}
    rows = ah_module.snapshot_auctions(con, latest["snapshot_id"])
    prev = con.execute(
        "SELECT * FROM ah_snapshot WHERE region = ? AND LOWER(realm) = LOWER(?)"
        " AND captured_at < ? ORDER BY captured_at DESC LIMIT 1",
        (region, realm, latest["captured_at"])).fetchone()
    prev_rows = ah_module.snapshot_auctions(con, prev["snapshot_id"]) if prev else []
    gap = ((latest["captured_at"] - prev["captured_at"]) / 3600.0
           if prev else None)

    groups = _item_groups(rows)
    items = [item_metrics(rows, item_id, seller_rows) for item_id in sorted(groups)]
    seller_attributed = None
    attributed = {int(r["item_id"]) for r in seller_rows if r.get("item_id")}
    if attributed:
        seller_attributed = round(
            len(attributed & set(groups)) / max(1, len(groups)), 4)

    return {
        "region": region,
        "realm": realm,
        "available": True,
        "snapshot_id": latest["snapshot_id"],
        "captured_at": latest["captured_at"],
        "market": market_metrics(rows),
        "items": items,
        "change": compare_snapshots(prev_rows, rows, gap) if prev else None,
        "seller_identity_coverage_by_item": seller_attributed,
        "seller_identity_note": ("coverage is the share of items for which at"
                                 " least one seller was observed; it is not a"
                                 " share of listings"),
        "liquidity": liquidity_from_events(events, realm, horizons) if events else None,
        "cancel_response": cancel_scan_response(events, [
            (latest["captured_at"], rows)] + ([(prev["captured_at"], prev_rows)]
                                              if prev else []), realm)
        if events else [],
    }
