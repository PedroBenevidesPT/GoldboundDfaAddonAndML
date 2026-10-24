"""Auction House mail invoices, and the buyer link they make possible.

The journal already records the sale.  What it often lacks is the buyer:
a chat event or an invoice without a cached player name arrives with an
empty counterparty.  The mailbox invoice has that name.

This module therefore does **not** create a sale.  It stores the invoice
as evidence, then updates the existing sale row whose item, quantity and
value match, inside the mail's lifetime.  A sale is never counted twice,
and a sale that already has a buyer is never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalise_item(value: Any) -> str:
    """Item names in a mail and in the journal must compare equal.

    Colour codes, surrounding brackets and repeated spaces are presentation,
    not identity.
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\u00a0", " ")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class MailInvoice:
    mail_id: str
    seen_at: int
    estimated_sale_at: int
    realm: str
    character: str
    invoice_type: str
    item_name: str
    player_name: str
    quantity: int
    value_copper: int
    buyout_copper: int = 0
    deposit_copper: int = 0
    consignment_copper: int = 0
    days_left: float = 0.0
    subject: str = ""
    sender: str = ""
    item_link: str = ""
    source: str = "mail_invoice"
    confidence: str = "verified"


def _invoice_kind(value: Any) -> str:
    """One sale, one kind: the "Sale Pending" mail becomes a seller mail.

    The client keeps a ``seller_temp_invoice`` for about an hour before the
    money arrives, then rewrites the same mail as ``seller``.  Treating the
    two as one kind means the second scan does not add a second invoice.
    """
    kind = str(value or "").casefold()
    return "seller" if kind == "seller_temp_invoice" else kind


def make_mail_id(invoice_type: str, realm: str, character: str,
                 item_name: str, player_name: str, value_copper: int,
                 quantity: int, estimated_sale_at: int) -> str:
    """A stable id for one mail, independent of when it was scanned.

    Content, not time: repeated inbox scans must never add a row.  Two
    genuinely identical sales to the same buyer collapse into one
    invoice; that loses a duplicate rather than inventing one, and the
    linker refuses to attach one invoice to two sales.
    """
    raw = "|".join((
        _invoice_kind(invoice_type),
        str(realm or "").casefold(),
        str(character or "").casefold(),
        normalise_item(item_name),
        str(player_name or "").casefold(),
        str(int(value_copper or 0)),
        str(int(quantity or 1)),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def parse_invoice(row: Dict[str, Any]) -> Optional[MailInvoice]:
    if not isinstance(row, dict):
        return None
    invoice_type = str(row.get("invoiceType") or row.get("invoice_type") or "").strip()
    item_name = str(row.get("itemName") or row.get("item_name") or "").strip()
    player_name = str(row.get("playerName") or row.get("player_name") or "").strip()
    if not invoice_type or not item_name or not player_name:
        return None
    seen_at = _int(row.get("seenAt") or row.get("seen_at"))
    estimated = _int(row.get("estimatedSaleAt") or row.get("estimated_sale_at"))
    if estimated <= 0:
        estimated = seen_at
    realm = str(row.get("realm") or "")
    character = str(row.get("character") or "")
    quantity = max(1, _int(row.get("quantity") or row.get("count"), 1))
    value = max(0, _int(row.get("valueCopper") or row.get("value_copper")
                        or row.get("value")))
    return MailInvoice(
        mail_id=str(row.get("mailId") or row.get("mail_id")
                    or make_mail_id(invoice_type, realm, character, item_name,
                                    player_name, value, quantity, estimated)),
        seen_at=seen_at,
        estimated_sale_at=estimated,
        realm=realm,
        character=character,
        invoice_type=invoice_type,
        item_name=item_name,
        player_name=player_name,
        quantity=quantity,
        value_copper=value,
        buyout_copper=max(0, _int(row.get("buyoutCopper")
                                  or row.get("buyout_copper"))),
        deposit_copper=max(0, _int(row.get("depositCopper")
                                   or row.get("deposit_copper"))),
        consignment_copper=max(0, _int(row.get("consignmentCopper")
                                       or row.get("consignment_copper"))),
        days_left=_float(row.get("daysLeft") or row.get("days_left")),
        subject=str(row.get("subject") or ""),
        sender=str(row.get("sender") or ""),
        item_link=str(row.get("itemLink") or row.get("item_link") or ""),
        source=str(row.get("source") or "mail_invoice"),
        confidence=str(row.get("confidence") or "verified"),
    )


def parse_mail_jsonl(text: str) -> List[MailInvoice]:
    out: List[MailInvoice] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = parse_invoice(row)
        if parsed is not None:
            out.append(parsed)
    return out


def parse_export_file(path: Path | str) -> List[MailInvoice]:
    from woweconomy.analytics.auction_intel import extract_lua_string
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    raw = extract_lua_string(content, "mail_jsonl")
    if raw is None:
        raise ValueError(
            f"no mail_jsonl block found in {path}"
            " (the 1.3.0+ addon must be loaded and the game reloaded before"
            " the inbox can be read)")
    # An empty block is valid: the addon ran, the inbox had no invoice.
    return parse_mail_jsonl(raw)


def import_invoices(con: sqlite3.Connection,
                    invoices: Iterable[MailInvoice]) -> int:
    added = 0
    for inv in invoices:
        cursor = con.execute(
            "INSERT OR IGNORE INTO mail_invoice (mail_id, seen_at,"
            " estimated_sale_at, realm, character, invoice_type, item_name,"
            " player_name, quantity, value_copper, buyout_copper,"
            " deposit_copper, consignment_copper, days_left, subject, sender,"
            " item_link, source, confidence, imported_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (inv.mail_id, inv.seen_at, inv.estimated_sale_at, inv.realm,
             inv.character, inv.invoice_type, inv.item_name, inv.player_name,
             inv.quantity, inv.value_copper, inv.buyout_copper,
             inv.deposit_copper, inv.consignment_copper, inv.days_left,
             inv.subject, inv.sender, inv.item_link, inv.source,
             inv.confidence, _utcnow()))
        added += cursor.rowcount
    con.commit()
    return added


def load_invoices(con: sqlite3.Connection, realm: str = "",
                  invoice_type: str = "seller") -> List[MailInvoice]:
    # Rows written by the Lua addon keep the raw type, so a pending sale
    # invoice is still a sell-side invoice here.
    sql = ("SELECT * FROM mail_invoice WHERE invoice_type IN (?, ?)"
           " AND player_name <> ''")
    params: List[Any] = [invoice_type, "seller_temp_invoice"]
    if realm:
        sql += " AND LOWER(realm) = LOWER(?)"
        params.append(realm)
    sql += " ORDER BY seen_at, mail_id"
    from woweconomy.dbcompat import missing_table_errors

    try:
        rows = con.execute(sql, params).fetchall()
    except missing_table_errors():
        return []
    return [MailInvoice(
        mail_id=row["mail_id"], seen_at=int(row["seen_at"]),
        estimated_sale_at=int(row["estimated_sale_at"]),
        realm=row["realm"], character=row["character"],
        invoice_type=row["invoice_type"], item_name=row["item_name"],
        player_name=row["player_name"], quantity=int(row["quantity"]),
        value_copper=int(row["value_copper"]),
        buyout_copper=int(row["buyout_copper"]),
        deposit_copper=int(row["deposit_copper"]),
        consignment_copper=int(row["consignment_copper"]),
        days_left=float(row["days_left"]), subject=row["subject"],
        sender=row["sender"], item_link=row["item_link"],
        source=row["source"], confidence=row["confidence"],
    ) for row in rows]


def _sale_rows(tsm_db: Path | str, realm: str) -> Tuple[List[Dict[str, Any]], int]:
    from woweconomy.dbcompat import connect_store, missing_table_errors

    con = connect_store(tsm_db, readonly=True)
    try:
        columns = {row[1] for row in con.execute(
            "PRAGMA table_info(auction_intel_events)")}
        if "event_id" not in columns:
            return [], 0
        wanted = [c for c in (
            "event_id", "ts", "item_id", "item_name", "quantity",
            "unit_price_copper", "total_price_copper", "realm", "character",
            "counterparty") if c in columns]
        sql = f"SELECT {', '.join(wanted)} FROM auction_intel_events" \
              " WHERE event_type = 'sale'"
        params: List[Any] = []
        if realm and "realm" in columns:
            sql += " AND LOWER(realm) = LOWER(?)"
            params.append(realm)
        rows = []
        total = 0
        for row in con.execute(sql, params):
            total += 1
            buyer = row["counterparty"] if "counterparty" in row.keys() else ""
            if buyer:
                continue
            rows.append({key: row[key] for key in row.keys()})
        return rows, total
    except missing_table_errors():
        return [], 0
    finally:
        con.close()


def _candidate_ok(inv: MailInvoice, sale: Dict[str, Any],
                  sale_ts: int, window_days: int) -> bool:
    if inv.realm and sale.get("realm"):
        if inv.realm.casefold() != str(sale["realm"]).casefold():
            return False
    if inv.character and sale.get("character"):
        if inv.character.casefold() != str(sale["character"]).casefold():
            return False
    if inv.estimated_sale_at and abs(inv.estimated_sale_at - sale_ts) <= 3 * 86400:
        return True
    return (sale_ts - 300
            <= inv.seen_at
            <= sale_ts + max(1, window_days) * 86400)


def link_buyers(con: sqlite3.Connection, tsm_db: Path | str,
                realm: str = "", window_days: int = 35,
                dry_run: bool = False) -> Dict[str, Any]:
    """Attach a mailbox buyer to a sale that is already in the journal.

    Only sales with an empty counterparty are considered.  If two different
    buyers could explain one sale, or one invoice could explain two sales,
    the sale is left alone and reported as ambiguous.  That is deliberate:
    a wrong buyer is worse than a missing one.
    """
    sales, total_sales = _sale_rows(tsm_db, realm)
    invoices = load_invoices(con, realm, "seller")
    # An invoice explains one sale, now and in later runs: a mail already
    # attached to a sale must not be reused for a second sale that happens
    # to share its item, quantity and price.
    used_mail = set()
    from woweconomy.dbcompat import missing_table_errors

    try:
        for row in con.execute("SELECT mail_id FROM sale_buyer_link"):
            used_mail.add(str(row[0]))
    except missing_table_errors():
        used_mail = set()
    already_linked = 0
    by_key: Dict[Tuple[str, int, int], List[MailInvoice]] = {}
    for inv in invoices:
        if inv.mail_id in used_mail:
            already_linked += 1
            continue
        # A sale can be recorded at the winning bid or at the buyout.  The
        # invoice carries both, so either can identify the same sale.
        for value in {inv.value_copper, inv.buyout_copper}:
            if value <= 0:
                continue
            key = (normalise_item(inv.item_name), inv.quantity, value)
            by_key.setdefault(key, []).append(inv)

    report: Dict[str, Any] = {
        "realm": realm or "all",
        "sales_total": total_sales,
        "sales_without_buyer": len(sales),
        "already_had_buyer": max(0, total_sales - len(sales)),
        "mail_invoices": len(invoices),
        "mail_already_linked": already_linked,
        "linked": 0,
        "linked_high": 0,
        "linked_medium": 0,
        "no_mail": 0,
        "ambiguous_buyer": 0,
        "ambiguous_mail": 0,
        "dry_run": bool(dry_run),
        "links": [],
        "ambiguous": [],
        "note": ("the journal sale is updated in place; no sale row is"
                 " inserted, and an existing buyer is never overwritten"),
    }
    if not sales:
        return report
    if not by_key:
        report["no_mail"] = len(sales)
        return report

    tentative: List[Tuple[Dict[str, Any], MailInvoice, str, str]] = []
    for sale in sales:
        sale_ts = _int(sale.get("ts"))
        quantity = max(1, _int(sale.get("quantity"), 1))
        unit = max(0, _int(sale.get("unit_price_copper")))
        total = _int(sale.get("total_price_copper"))
        values = {v for v in (total, unit * quantity) if v > 0}
        if sale_ts <= 0 or not values:
            report["no_mail"] += 1
            continue
        item_key = normalise_item(sale.get("item_name"))
        candidates: Dict[str, MailInvoice] = {}
        for value in values:
            for inv in by_key.get((item_key, quantity, value), ()):
                if _candidate_ok(inv, sale, sale_ts, window_days):
                    candidates[inv.mail_id] = inv
        if not candidates:
            report["no_mail"] += 1
            continue
        groups: Dict[str, List[MailInvoice]] = {}
        for inv in candidates.values():
            groups.setdefault(inv.player_name.casefold(), []).append(inv)
        if len(groups) > 1:
            report["ambiguous_buyer"] += 1
            if len(report["ambiguous"]) < 50:
                report["ambiguous"].append({
                    "sale_event_id": sale.get("event_id"),
                    "item_name": sale.get("item_name"),
                    "quantity": quantity,
                    "value_copper": sorted(values),
                    "buyers": sorted({inv.player_name for inv in candidates.values()}),
                    "reason": "more than one buyer could explain this sale",
                })
            continue
        _, group = next(iter(groups.items()))
        chosen = min(group, key=lambda inv: (
            abs(inv.estimated_sale_at - sale_ts) if inv.estimated_sale_at else 10 ** 12,
            inv.seen_at,
        ))
        buyer = chosen.player_name
        # ``bid`` is the money the invoice says was paid; ``buyout`` is only
        # the listing's buyout, which a bid win never reached.  A match on
        # the bid is therefore stronger evidence than one on the buyout.
        if chosen.value_copper in values:
            confidence = "high"
        elif chosen.buyout_copper in values:
            confidence = "medium"
        else:
            continue
        tentative.append((sale, chosen, buyer, confidence))

    mail_use: Dict[str, int] = {}
    for _sale, inv, _buyer, _confidence in tentative:
        mail_use[inv.mail_id] = mail_use.get(inv.mail_id, 0) + 1

    # A write, so not read-only: on the stack the journal table is in the one
    # Postgres database, where the workstation file does not exist.
    from woweconomy.dbcompat import connect_store

    journal = connect_store(tsm_db)
    try:
        for sale, inv, buyer, confidence in tentative:
            if mail_use.get(inv.mail_id, 0) > 1:
                report["ambiguous_mail"] += 1
                continue
            if dry_run:
                report["linked"] += 1
                report["linked_" + confidence] += 1
                if len(report["links"]) < 100:
                    report["links"].append({
                        "sale_event_id": sale.get("event_id"),
                        "item_name": sale.get("item_name"),
                        "quantity": inv.quantity,
                        "value_copper": inv.value_copper,
                        "buyer": buyer,
                        "mail_id": inv.mail_id,
                        "matched_on": "bid" if confidence == "high" else "buyout",
                        "confidence": confidence,
                    })
                continue
            cursor = journal.execute(
                "UPDATE auction_intel_events SET counterparty = ?"
                " WHERE event_id = ? AND (counterparty IS NULL"
                " OR counterparty = '')", (buyer, sale.get("event_id")))
            if cursor.rowcount == 0:
                continue
            con.execute(
                "INSERT OR REPLACE INTO sale_buyer_link (sale_event_id,"
                " mail_id, realm, buyer, method, confidence, linked_at, note)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sale.get("event_id"), inv.mail_id, sale.get("realm") or realm,
                 buyer, "mail_invoice_exact_item_qty_value", confidence,
                 _utcnow(),
                 f"mail seen at {inv.seen_at}; estimated sale "
                 f"{inv.estimated_sale_at}; value {inv.value_copper}"))
            report["linked"] += 1
            report["linked_" + confidence] += 1
            if len(report["links"]) < 100:
                report["links"].append({
                    "sale_event_id": sale.get("event_id"),
                    "item_name": sale.get("item_name"),
                    "quantity": inv.quantity,
                    "value_copper": inv.value_copper,
                    "buyer": buyer,
                    "mail_id": inv.mail_id,
                    "matched_on": "bid" if confidence == "high" else "buyout",
                    "confidence": confidence,
                })
        journal.commit()
    finally:
        journal.close()
    con.commit()
    return report
