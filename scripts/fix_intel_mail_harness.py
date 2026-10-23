#!/usr/bin/env python3
"""Fix intel_addon_harness.py mail mock + sync installed Mail.lua.

The harness had GetInboxItemLink but no GetInboxInvoiceItemLink, and the new
Mail.lua now checks for GetInboxInvoiceItemLink before scanning.  The harness
also stubbed MailboxButton out, so the addon's ``MailboxButton()`` definition
(the one that triggers the ``Trim`` call in the direct repro) never ran.

This script:
1) inserts GetInboxInvoiceItemLink into the mock, right after GetInboxInvoiceInfo,
   without clobbering GetInboxItemLink (which other parts of the mock may call).
2) replaces the MailboxButton no-op with the real frame pattern the addon uses,
   so the MockedRun path exercises the same code path as the direct repro.
3) copies the project Mail.lua over the installed copy so the failing installed-copy
   test reflects the real project change.
"""
from pathlib import Path

H = Path("scripts/intel_addon_harness.py")
txt = H.read_text(encoding="utf-8", errors="replace")

# 1) ensure GetInboxInvoiceItemLink exists
if "function GetInboxInvoiceItemLink" not in txt:
    anchor = "function GetInboxInvoiceInfo(index)\n    return unpackValues(inbox[index].invoice, 1, 12)\nend\n"
    injection = (
        "function GetInboxInvoiceInfo(index)\n"
        "    return unpackValues(inbox[index].invoice, 1, 12)\n"
        "end\n"
        "function GetInboxInvoiceItemLink(index)\n"
        "    return inbox[index].link\n"
        "end\n"
    )
    if anchor in txt:
        txt = txt.replace(anchor, injection, 1)
        print("inserted GetInboxInvoiceItemLink via anchor")
    else:
        import re
        m = re.search(
            r"function GetInboxInvoiceInfo\(index\)\n    return unpackValues\(inbox\[index\]\.invoice, 1, 12\)\nend\n",
            txt)
        if not m:
            raise SystemExit("cannot locate GetInboxInvoiceInfo def")
        ins = (
            m.group(0)
            + "function GetInboxInvoiceItemLink(index)\n"
            + "    return inbox[index].link\n"
            + "end\n"
        )
        txt = txt[: m.start()] + ins + txt[m.end():]
        print("inserted GetInboxInvoiceItemLink via regex")
else:
    print("GetInboxInvoiceItemLink already present")

# 2) replace the MailboxButton no-op stub with the real pattern
old_stub = "function MailboxButton()\n    return nil\nend\n"
real_stub = (
    "function MailboxButton()\n"
    "    if MailboxButtonCreated then\n"
    "        return nil\n"
    "    end\n"
    "    MailboxButtonCreated = true\n"
    "    local anchor = CreateFrame(\"Button\", \"DFA_AI_MailboxAnchor\", UIParent, \"UIPanelButtonTemplate\")\n"
    "    anchor:SetText(\"Open Mailbox\")\n"
    "    anchor:SetSize(140, 22)\n"
    "    anchor:SetPoint(\"TOP\", 0, -180)\n"
    "    anchor:SetScript(\"OnClick\", function()\n"
    "        ClearCursor()\n"
    "        if PlaySoundFile(\"Sound\\\\Interface\\\\RifleScopeTargetAcquired.wav\", \"SFX\") then end\n"
    "        StaticPopup_Hide(\"MAIL_INBOX_WAIT_FOR_ITEMS\")\n"
    "        if Mail:IsAutoScanEnabled() then\n"
    "            Mail:ScanInbox(\"mailbox_button\", true)\n"
    "        end\n"
    "    end)\n"
    "    return anchor\n"
    "end\n"
)
if old_stub in txt:
    txt = txt.replace(old_stub, real_stub, 1)
    print("replaced MailboxButton no-op with real pattern")
else:
    print("MailboxButton no-op stub not found; leaving as-is")

# normalize line endings just in case and write back
txt = txt.replace("\r\n", "\n").replace("\r", "\n")
H.write_text(txt, encoding="utf-8")
print("rewrote", H)

# 3) sync Mail.lua to installed copy
proj = Path("DFA_AuctionIntelligence/Mail.lua")
installed = Path(
    r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_AuctionIntelligence\Mail.lua"
)
if not installed.exists():
    print("installed Mail.lua missing; skipping sync")
else:
    installed.write_bytes(proj.read_bytes())
    print("synced project Mail.lua -> installed Mail.lua")
