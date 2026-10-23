-- DFA Auction Intelligence - Verified Journalator Integration Adapter
local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.JournalatorAdapter = {}
local JournalatorAdapter = DFA_AI.JournalatorAdapter

local isAvailable = false
local version = "unknown"
local lastBackfillCount = 0
local lastSync = nil
--: how live rows reach us: "eventbus", "hook" or "none"
local integration = "none"

--: what a sync imports, and the field it counts into
local SECTIONS = {
    { name = "Invoices", count = "invoices", normalizer = "NormalizeInvoice" },
    { name = "Posting", count = "postings", normalizer = "NormalizePosting" },
    { name = "Failures", count = "failures", normalizer = "NormalizeFailure" },
}

function JournalatorAdapter:IsAvailable()
    return isAvailable
end

function JournalatorAdapter:GetVersion()
    return version
end

function JournalatorAdapter:GetStatus()
    return {
        available = isAvailable,
        version = version,
        lastBackfillCount = lastBackfillCount,
        lastSync = lastSync,
        integration = integration,
    }
end

local function CaptureEntries(newEntries)
    JournalatorAdapter:OnLogsUpdated(newEntries)
end

function JournalatorAdapter:Initialize()
    local getMeta = (C_AddOns and C_AddOns.GetAddOnMetadata) or GetAddOnMetadata
    if getMeta then
        version = getMeta("Journalator", "Version") or "unknown"
    end

    if not Journalator or not Journalator.Archiving then
        isAvailable = false
        -- Enable native fallback capture
        DFA_AI.AuctionEvents:EnableNativeCapture()
        return false, "Journalator not detected"
    end

    isAvailable = true

    -- One capture path, never two.  Journalator registers *the function object*
    -- ``Journalator.AddToLogs`` as its Auctionator EventBus source, and its own
    -- body calls ``EventBus:Fire(Journalator.AddToLogs, ...)``.  A
    -- ``hooksecurefunc`` on a table field replaces that field with a new
    -- function, so the Fire looks up a source that was never registered and
    -- Auctionator raises "All sources must be registered" for every auction
    -- archived after login.  Listen on the bus instead; only hook when there is
    -- no bus to break.
    local bus = Auctionator and Auctionator.EventBus
    local logsUpdated = Journalator.Events and Journalator.Events.LogsUpdated
    if integration == "none" and bus and type(bus.Register) == "function" and logsUpdated then
        local registered = pcall(bus.Register, bus, {
            ReceiveEvent = function(_, _, newEntries)
                CaptureEntries(newEntries)
            end,
        }, { logsUpdated })
        if registered then
            integration = "eventbus"
        end
    end
    if integration == "none" then
        hooksecurefunc(Journalator, "AddToLogs", CaptureEntries)
        integration = "hook"
        -- the hook replaced the function; put the new object back in the
        -- registry so Journalator's own Fire() keeps working
        if bus and type(bus.RegisterSource) == "function" then
            pcall(bus.RegisterSource, bus, Journalator.AddToLogs, "Journalator.AddToLogs")
        end
    end

    return true
end

---Import every Journalator section and say exactly what happened.
--
-- The old backfill swallowed a failing section in a bare ``pcall``: the
-- archive could be half readable and the addon would still report success
-- with zero events.  This returns a report -- per section, with the errors --
-- and the report is persisted, so the next session can prove what was read.
--@param reason string  who asked: "login", "logout", "manual"
--@return table
function JournalatorAdapter:SyncAll(reason)
    local report = {
        at = time(),
        reason = reason or "manual",
        available = isAvailable,
        -- rows *read* from each section: what the archive held
        invoices = 0,
        postings = 0,
        failures = 0,
        -- rows that were new to the store, and how many were already there
        stored = 0,
        duplicates = 0,
        errors = {},
        -- sections read from the live store instead of the archive (logout)
        fallback = {},
    }

    if not isAvailable then
        table.insert(report.errors, "Journalator not detected")
        lastSync = report
        if DFA_AI.DataStore and DFA_AI.DataStore.RecordSync then
            DFA_AI.DataStore:RecordSync(report)
        end
        return report
    end

    if not (Journalator.Archiving and Journalator.Archiving.GetRange) then
        table.insert(report.errors, "Journalator.Archiving.GetRange missing")
        lastSync = report
        return report
    end

    -- Archivist tears its instance down on PLAYER_LOGOUT (``archive.sv = nil``
    -- in ``Archivist:DeInitialize``), and that teardown can run before this
    -- handler: every logout sync then failed with "attempt to index a nil
    -- value" and stored nothing.  The live store is still in memory, so on
    -- logout read it when the archive can no longer be opened.
    local canReadLive = reason == "logout" and Journalator.State ~= nil

    for _, sec in ipairs(SECTIONS) do
        local ok, entries = pcall(Journalator.Archiving.GetRange, 0, sec.name)
        if (not ok or type(entries) ~= "table") and canReadLive
                and type(Journalator.State.Logs) == "table" then
            local live = Journalator.State.Logs[sec.name]
            if type(live) == "table" then
                entries = live
                ok = true
                report.fallback = report.fallback or {}
                table.insert(report.fallback, sec.name)
            end
        end
        if not ok then
            table.insert(report.errors, sec.name .. ": " .. tostring(entries))
        elseif type(entries) ~= "table" then
            table.insert(report.errors, sec.name .. ": no table returned")
        elseif #entries == 0 then
            -- an empty section is information too: it says the archive has
            -- no postings, which is why the funnel cannot be measured
            table.insert(report.errors, sec.name .. ": empty")
        else
            report[sec.count] = report[sec.count] + #entries
            local normalizer = DFA_AI.AuctionEvents[sec.normalizer]
            for _, rawEntry in ipairs(entries) do
                local ev = normalizer(DFA_AI.AuctionEvents, rawEntry, "journalator_archive")
                local okStore, why = DFA_AI.DataStore:StoreEvent(ev)
                if okStore then
                    report.stored = report.stored + 1
                elseif why == "duplicate" then
                    report.duplicates = report.duplicates + 1
                end
            end
        end
    end

    lastSync = report
    lastBackfillCount = report.stored
    if DFA_AI.DataStore and DFA_AI.DataStore.RecordSync then
        DFA_AI.DataStore:RecordSync(report)
    end
    return report
end

function JournalatorAdapter:BackfillHistorical()
    local report = self:SyncAll("backfill")
    return lastBackfillCount, report
end

function JournalatorAdapter:OnLogsUpdated(newEntries)
    if not newEntries or type(newEntries) ~= "table" then return end

    if newEntries.Invoices then
        for _, item in ipairs(newEntries.Invoices) do
            local ev = DFA_AI.AuctionEvents:NormalizeInvoice(item, "journalator_live")
            DFA_AI.DataStore:StoreEvent(ev)
        end
    end

    if newEntries.Posting then
        for _, item in ipairs(newEntries.Posting) do
            local ev = DFA_AI.AuctionEvents:NormalizePosting(item, "journalator_live")
            DFA_AI.DataStore:StoreEvent(ev)
        end
    end

    if newEntries.Failures then
        for _, item in ipairs(newEntries.Failures) do
            local ev = DFA_AI.AuctionEvents:NormalizeFailure(item, "journalator_live")
            DFA_AI.DataStore:StoreEvent(ev)
        end
    end
end
