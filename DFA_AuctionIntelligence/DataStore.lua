-- DFA Auction Intelligence - Versioned Local Event Storage & Deduplication
local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.DataStore = {}
local DataStore = DFA_AI.DataStore

local DB_VERSION = 1
local MAX_STORED_EVENTS = 25000 -- safety guard against unbounded SavedVariables growth
local MAX_SYNC_REPORTS = 10    -- a short history is enough to prove what was read

local function GenerateSignature(ev)
    local evType = ev.eventType or "unknown"
    local itemId = ev.itemId or 0
    local ts = ev.timestamp or 0
    local qty = ev.quantity or 1
    local price = ev.totalPriceCopper or 0
    local realm = ev.realm or "global"
    local counter = ev.counterparty or ""
    return string.format("%s:%s:%s:%s:%s:%s:%s", evType, itemId, ts, qty, price, realm, counter)
end

function DataStore:Initialize()
    if type(DFA_AUCTION_INTELLIGENCE_DB) ~= "table" then
        DFA_AUCTION_INTELLIGENCE_DB = {}
    end
    local db = DFA_AUCTION_INTELLIGENCE_DB

    if not db.schema_version or db.schema_version < DB_VERSION then
        self:Migrate(db.schema_version or 0, DB_VERSION)
        db.schema_version = DB_VERSION
    end

    db.events = db.events or {}
    db.index = db.index or {}
    db.sync_log = db.sync_log or {}
    -- A stats table written by an older build only had duplicates/total.
    -- Without this every StoreEvent indexed a nil ``by_type`` and died -- the
    -- archive was read and then thrown away, which is why the log stayed at a
    -- few dozen sales.
    db.stats = db.stats or {}
    db.stats.duplicates = db.stats.duplicates or 0
    db.stats.last_capture_time = db.stats.last_capture_time or 0
    db.stats.by_type = db.stats.by_type or {}
    for _, eventType in ipairs({ "sale", "purchase", "listing", "cancel", "expire" }) do
        db.stats.by_type[eventType] = db.stats.by_type[eventType] or 0
    end

    -- Rebuild in-memory index if size mismatch
    if #db.events > 0 and (not db.index or next(db.index) == nil) then
        self:RebuildIndex()
    end

    db.stats.total = #db.events
    return db
end

function DataStore:Migrate(fromVer, toVer)
    -- Reserved for forward migrations
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if fromVer == 0 then
        db.events = db.events or {}
        db.index = db.index or {}
        db.stats = db.stats or {}
    end
end

function DataStore:RebuildIndex()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    db.index = {}
    local by_type = { sale = 0, purchase = 0, listing = 0, cancel = 0, expire = 0 }
    for i, ev in ipairs(db.events) do
        local sig = GenerateSignature(ev)
        db.index[sig] = i
        ev.eventId = ev.eventId or sig
        local t = ev.eventType
        if by_type[t] ~= nil then
            by_type[t] = by_type[t] + 1
        end
    end
    db.stats.by_type = by_type
    db.stats.total = #db.events
end

function DataStore:StoreEvent(event)
    if not event or type(event) ~= "table" then return false, "invalid_event" end
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if not db or not db.events then
        self:Initialize()
        db = DFA_AUCTION_INTELLIGENCE_DB
    end

    local sig = GenerateSignature(event)
    if db.index[sig] then
        db.stats.duplicates = (db.stats.duplicates or 0) + 1
        return false, "duplicate"
    end

    event.eventId = sig
    event.schemaVersion = DB_VERSION
    event.capturedAt = event.capturedAt or time()

    table.insert(db.events, event)
    db.index[sig] = #db.events

    local t = event.eventType
    db.stats.by_type = db.stats.by_type or {}
    if db.stats.by_type[t] ~= nil then
        db.stats.by_type[t] = db.stats.by_type[t] + 1
    end
    db.stats.total = #db.events
    db.stats.last_capture_time = event.timestamp or time()

    -- Trim oldest if exceeding safety bound
    if #db.events > MAX_STORED_EVENTS then
        self:Prune(MAX_STORED_EVENTS)
    end

    return true, event.eventId
end

function DataStore:StoreBatch(events)
    local added = 0
    local dups = 0
    for _, ev in ipairs(events) do
        local ok, res = self:StoreEvent(ev)
        if ok then
            added = added + 1
        elseif res == "duplicate" then
            dups = dups + 1
        end
    end
    return added, dups
end

function DataStore:Prune(maxCount)
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if not db or not db.events or #db.events <= maxCount then return end
    local toRemove = #db.events - maxCount
    local newEvents = {}
    for i = toRemove + 1, #db.events do
        table.insert(newEvents, db.events[i])
    end
    db.events = newEvents
    self:RebuildIndex()
end

---Keeps the last few sync reports, so "it synced" is a fact and not a hope.
---@param report table
function DataStore:RecordSync(report)
    local db = self:Initialize()
    db.sync_log = db.sync_log or {}
    table.insert(db.sync_log, report)
    while #db.sync_log > MAX_SYNC_REPORTS do
        table.remove(db.sync_log, 1)
    end
    db.last_sync = report
    return report
end

function DataStore:GetLastSync()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    return db and db.last_sync or nil
end

function DataStore:GetEvents()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    return db and db.events or {}
end

function DataStore:GetStats()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    return db and db.stats or { total = 0, duplicates = 0, by_type = {} }
end

function DataStore:GetCount()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    return db and db.events and #db.events or 0
end
