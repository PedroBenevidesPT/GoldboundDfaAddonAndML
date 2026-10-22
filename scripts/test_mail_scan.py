"""Focused test for the Auction Intelligence mail scanner.

Runs the real Mail.lua in a Lua interpreter with the mailbox APIs stubbed.
It proves the two cases the scan log previously contradicted:

1. zero inbox items -> zero invoices, zero stored, empty JSONL;
2. one valid invoice plus one non-invoice -> one detected, one stored,
   and one parseable JSON line.

    python scripts/test_mail_scan.py [addon-dir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lupa import LuaRuntime

ROOT = Path(__file__).resolve().parent.parent
ADDON = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "DFA_AuctionIntelligence")

CLIENT = r"""
PRINTED = {}
function print(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
    PRINTED[#PRINTED + 1] = table.concat(parts, " ")
end

NOW = 1792521600
function time() return NOW end
function GetRealmName() return "Dragonmaw" end
function UnitName() return "Bullbound" end

SECRETS = {}
function issecretvalue(value) return SECRETS[value] == true end

C_Timer = { queue = {} }
function C_Timer.After(delay, fn) C_Timer.queue[#C_Timer.queue + 1] = fn end
function RunTimers()
    local pending = C_Timer.queue
    C_Timer.queue = {}
    for _, fn in ipairs(pending) do fn() end
end

local unpackValues = table.unpack or unpack
inbox = {}
function SetInbox(rows) inbox = rows end
function AddRow(row) inbox[#inbox + 1] = row end
function AddInvoiceRow(item, buyer, value, quantity, invoiceType, attachmentCount)
    AddRow({
        subject = "Auction successful: [" .. item .. "]",
        sender = "Auction House",
        daysLeft = 29.5,
        isInvoice = true,
        invoice = {
            invoiceType or "invoice", item, buyer, value, value, 100,
            math.floor(value * 0.9), 0, 0, 0, quantity, 0,
        },
        attachmentCount = attachmentCount,
    })
end
function AddLetterRow(subject)
    AddRow({ subject = subject, sender = "A friend", daysLeft = 20.5,
             isInvoice = false, invoice = nil })
end

function GetInboxNumItems() return #inbox end
function GetInboxText(index)
    local row = inbox[index]
    if not row then return "", "", "", "", false end
    return "", "", "", "", row.isInvoice == true
end
function GetInboxHeaderInfo(index)
    local row = inbox[index]
    return false, false, row.sender, row.subject, 0, 0, row.daysLeft,
        true, false, false, false, false
end
function GetInboxInvoiceInfo(index)
    local row = inbox[index]
    if not row or not row.invoice then return nil end
    return unpackValues(row.invoice, 1, 12)
end
function GetInboxItemLink(index, attachment)
    local row = inbox[index]
    return row and row.itemLink or nil
end
function GetInboxItem(index, attachment)
    local row = inbox[index]
    if not row or not row.attachmentCount then return nil end
    return "Attachment", 2001, "icon", row.attachmentCount, 1, true
end
"""


def load_lua():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(CLIENT)
    lua.execute("DFA_AUCTION_INTELLIGENCE_DB = {}")
    for name in ("Localization.lua", "Mail.lua"):
        lua.execute((ADDON / name).read_text(encoding="utf-8"))
    return lua


def main() -> int:
    lua = load_lua()
    g = lua.globals()
    mail = g.DFA_AI.Mail
    db = g.DFA_AUCTION_INTELLIGENCE_DB
    checks = []

    def scan(reason="manual"):
        return mail.ScanInbox(mail, reason, True)

    # 1. zero inbox items
    lua.execute("inbox = {}")
    report = scan()
    checks.append(("zero inbox: items=0", int(report.inbox_items) == 0,
                   str(report.inbox_items)))
    checks.append(("zero inbox: invoices=0", int(report.invoices) == 0,
                   str(report.invoices)))
    checks.append(("zero inbox: stored=0", int(report.stored) == 0,
                   str(report.stored)))
    checks.append(("zero inbox: nothing persisted",
                   len(db.mail_invoices or {}) == 0,
                   str(db.mail_invoices)))
    checks.append(("zero inbox: JSONL empty",
                   str(mail.ToJSONLines(mail)) == "",
                   repr(str(mail.ToJSONLines(mail)))))
    checks.append(("zero inbox: report says mailbox closed",
                   bool(report.mailbox_closed), str(report.mailbox_closed)))

    # 2. one valid invoice + one non-invoice
    lua.execute('''
        SetInbox({})
        AddInvoiceRow("Thing", "Buyer-Silvermoon", 9000, 2)
        AddLetterRow("Thanks for the trade")
    ''')
    report = scan()
    checks.append(("mixed inbox: both mails read", int(report.inbox_items) == 2,
                   str(report.inbox_items)))
    checks.append(("mixed inbox: one invoice", int(report.invoices) == 1,
                   str(report.invoices)))
    checks.append(("mixed inbox: one stored", int(report.stored) == 1,
                   str(report.stored)))
    checks.append(("mixed inbox: one row persisted",
                   len(db.mail_invoices) == 1, str(len(db.mail_invoices))))
    row = db.mail_invoices[1] if len(db.mail_invoices) == 1 else None
    checks.append(("mixed inbox: buyer, item, value, quantity",
                   row is not None
                   and row.playerName == "Buyer-Silvermoon"
                   and row.itemName == "Thing"
                   and int(row.valueCopper) == 9000
                   and int(row.quantity) == 2,
                   f"{row and row.itemName}/{row and row.playerName}/"
                   f"{row and row.valueCopper}/{row and row.quantity}"))

    # 3. export is valid JSON, not "table: 0x..."
    text = str(mail.ToJSONLines(mail))
    lines = [line for line in text.splitlines() if line.strip()]
    parsed = []
    parse_error = ""
    try:
        parsed = [json.loads(line) for line in lines]
    except json.JSONDecodeError as exc:  # pragma: no cover - failure detail
        parse_error = str(exc)
    checks.append(("export: one parseable JSON line",
                   len(parsed) == 1 and not parse_error,
                   parse_error or str(parsed)))
    checks.append(("export: JSON carries the buyer and price",
                   bool(parsed) and parsed[0].get("playerName") == "Buyer-Silvermoon"
                   and parsed[0].get("valueCopper") == 9000,
                   str(parsed)))

    # 4. repeat scan must not duplicate
    again = scan()
    checks.append(("repeat scan: invoice found again", int(again.invoices) == 1,
                   str(again.invoices)))
    checks.append(("repeat scan: stored=0 duplicates=1",
                   int(again.stored) == 0 and int(again.duplicates) == 1,
                   f"{again.stored}/{again.duplicates}"))
    checks.append(("repeat scan: still one row", len(db.mail_invoices) == 1,
                   str(len(db.mail_invoices))))

    # 5. invoice without a buyer is not stored
    lua.execute('''
        SetInbox({})
        AddInvoiceRow("Other", "", 5000, 1)
    ''')
    no_buyer = scan()
    checks.append(("missing buyer: counted, not stored",
                   int(no_buyer.without_buyer) == 1
                   and int(no_buyer.stored) == 0
                   and len(db.mail_invoices) == 1,
                   f"{no_buyer.without_buyer}/{no_buyer.stored}"))

    # 6. attachment count is the quantity fallback
    lua.execute('''
        SetInbox({})
        AddInvoiceRow("Stack", "Buyer-Stack", 7000, 0, "invoice", 7)
    ''')
    counted = scan()
    stack_row = next((r for r in db.mail_invoices.values()
                      if r.playerName == "Buyer-Stack"), None)
    checks.append(("attachment count fills the quantity",
                   int(counted.stored) == 1 and stack_row is not None
                   and int(stack_row.quantity) == 7,
                   str(stack_row and stack_row.quantity)))

    # 7. the reported mailbox: three sales and two expired auctions
    before = len(db.mail_invoices)
    lua.execute('''
        SetInbox({})
        AddInvoiceRow("Sale A", "Buyer-A", 1000, 1)
        AddInvoiceRow("Sale B", "Buyer-B", 2000, 1)
        AddInvoiceRow("Sale C", "Buyer-C", 3000, 1)
        AddLetterRow("Auction expired: [Sale D]")
        AddLetterRow("Auction cancelled: [Sale E]")
    ''')
    sales = scan()
    checks.append(("5 mails: all 5 are read", int(sales.inbox_items) == 5,
                   str(sales.inbox_items)))
    checks.append(("5 mails: the 3 sales are invoices",
                   int(sales.invoices) == 3, str(sales.invoices)))
    checks.append(("5 mails: the 3 sales are stored",
                   int(sales.stored) == 3
                   and len(db.mail_invoices) == before + 3,
                   f"{sales.stored}/{len(db.mail_invoices)}"))
    checks.append(("5 mails: the 2 expired are not invoices and not retried",
                   int(sales.duplicates) == 0
                   and not sales.retry_scheduled,
                   f"{sales.duplicates}/{sales.retry_scheduled}"))

    # 8. an invoice the client has not cached is retried
    lua.execute('''
        SetInbox({ { subject = "Auction successful: [Thing]",
                     sender = "Auction House", daysLeft = 29.5,
                     isInvoice = true, invoice = nil } })
    ''')
    pending = scan("inbox_update")
    checks.append(("uncached invoice: no invoice yet",
                   int(pending.invoices) == 0
                   and int(pending.pending_invoice) == 1,
                   f"{pending.invoices}/{pending.pending_invoice}"))
    checks.append(("uncached invoice: retry scheduled",
                   bool(pending.retry_scheduled), str(pending.retry_scheduled)))
    lua.execute('''
        SetInbox({})
        AddInvoiceRow("Thing", "Buyer-Retry", 9000, 2)
        RunTimers()
    ''')
    retry_row = next((r for r in db.mail_invoices.values()
                      if r.playerName == "Buyer-Retry"), None)
    checks.append(("uncached invoice: retry stores the buyer",
                   retry_row is not None, str(retry_row is not None)))

    failed = 0
    for label, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
