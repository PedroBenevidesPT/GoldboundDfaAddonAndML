-- DFA Auction Intelligence - Diagnostics, Health & Field Coverage
local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.Diagnostics = {}
local Diagnostics = DFA_AI.Diagnostics

function Diagnostics:Run()
    local stats = DFA_AI.DataStore:GetStats()
    local events = DFA_AI.DataStore:GetEvents()

    local missingItemId = 0
    local missingDeposit = 0
    local missingFee = 0
    local missingTimestamp = 0
    local zeroPrice = 0

    for _, ev in ipairs(events) do
        if not ev.itemId or ev.itemId == 0 then
            missingItemId = missingItemId + 1
        end
        if not ev.timestamp or ev.timestamp == 0 then
            missingTimestamp = missingTimestamp + 1
        end
        if ev.eventType == "listing" and (not ev.depositCopper or ev.depositCopper == 0) then
            missingDeposit = missingDeposit + 1
        end
        if ev.eventType == "sale" and (not ev.consignmentFeeCopper or ev.consignmentFeeCopper == 0) then
            missingFee = missingFee + 1
        end
        if (ev.eventType == "sale" or ev.eventType == "listing") and (not ev.totalPriceCopper or ev.totalPriceCopper == 0) then
            zeroPrice = zeroPrice + 1
        end
    end

    local jStatus = DFA_AI.JournalatorAdapter:GetStatus()
    local tsmActive = (TSM_API ~= nil or TradeSkillMaster_API ~= nil)

    return {
        total_events = #events,
        duplicates_intercepted = stats.duplicates or 0,
        by_type = stats.by_type or {},
        missing_fields = {
            missing_item_id = missingItemId,
            missing_timestamp = missingTimestamp,
            missing_deposit = missingDeposit,
            missing_consignment_fee = missingFee,
            zero_price = zeroPrice,
        },
        integrations = {
            journalator = jStatus,
            tsm = { available = tsmActive },
            auctionator = { available = (Auctionator ~= nil) },
        },
        mail = DFA_AI.Mail and DFA_AI.Mail:GetStats() or {},
        undercut = DFA_AI.Undercut and DFA_AI.Undercut:GetStats() or {},
        last_capture_time = stats.last_capture_time or 0,
    }
end

function Diagnostics:PrintReport()
    local r = self:Run()
    print("|cff00ccff[DFA AI Diagnostics Report]|r")
    print(string.format("  Total Events: %d (Duplicates filtered: %d)", r.total_events, r.duplicates_intercepted))
    print(string.format("  By Type: Sales=%d, Purchases=%d, Listings=%d, Cancels=%d, Expires=%d",
        r.by_type.sale or 0, r.by_type.purchase or 0, r.by_type.listing or 0,
        r.by_type.cancel or 0, r.by_type.expire or 0))

    local jStr = r.integrations.journalator.available
        and ("|cff00ff00Connected|r (v" .. tostring(r.integrations.journalator.version) .. ")")
        or "|cffff0000Not Active|r (Native Fallback Engaged)"
    print("  Journalator: " .. jStr)

    local tsmStr = r.integrations.tsm.available and "|cff00ff00Active|r" or "|cffffaa00Not Detected|r"
    print("  TradeSkillMaster: " .. tsmStr)

    local mail = r.mail or {}
    print(string.format("  Mail invoices: %d (inbox=%d, last reason=%s, auto scan=%s)",
        mail.total or 0, mail.last_inbox_items or 0,
        tostring(mail.last_reason or "?"), tostring(mail.autoscan)))
    local undercut = r.undercut or {}
    local native = undercut.last_native or {}
    print(string.format("  Undercut observations: %d (hook=%s, last native=%s read=%d no-results=%d)",
        undercut.total or 0, tostring(undercut.hooked),
        tostring(native.reason or "never"), native.scanned or 0,
        native.no_results or 0))

    print("  Data Quality & Missing Fields:")
    print(string.format("    Missing Item ID: %d", r.missing_fields.missing_item_id))
    print(string.format("    Missing Timestamps: %d", r.missing_fields.missing_timestamp))
    print(string.format("    Listings without Deposit: %d", r.missing_fields.missing_deposit))
    print(string.format("    Sales without AH Cut: %d", r.missing_fields.missing_consignment_fee))
    print(string.format("    Zero Price Transactions: %d", r.missing_fields.zero_price))

    if r.last_capture_time > 0 then
        print(string.format("  Last Captured: %s", date("%Y-%m-%d %H:%M:%S", r.last_capture_time)))
    else
        print("  Last Captured: None")
    end
end
