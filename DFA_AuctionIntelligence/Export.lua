-- DFA Auction Intelligence - Structured Versioned Export
local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.Export = {}
local Export = DFA_AI.Export

-- Lightweight JSON serializer for JSONL lines in SavedVariables
local function EscapeJSON(str)
    if not str then return '""' end
    str = tostring(str)
    str = str:gsub('\\', '\\\\')
    str = str:gsub('"', '\\"')
    str = str:gsub('\n', '\\n')
    str = str:gsub('\r', '\\r')
    str = str:gsub('\t', '\\t')
    return '"' .. str .. '"'
end

local function ToJSON(val)
    local t = type(val)
    if t == "number" then
        return tostring(val)
    elseif t == "boolean" then
        return val and "true" or "false"
    elseif t == "string" then
        return EscapeJSON(val)
    elseif t == "table" then
        -- Check if it's an array
        local isArray = (#val > 0)
        local parts = {}
        if isArray then
            for _, v in ipairs(val) do
                table.insert(parts, ToJSON(v))
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            for k, v in pairs(val) do
                table.insert(parts, EscapeJSON(k) .. ":" .. ToJSON(v))
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    else
        return "null"
    end
end

function Export:GenerateExport(sinceTimestamp)
    if not DFA_AI.DataStore then
        return 0
    end
    local events = DFA_AI.DataStore:GetEvents()
    local exportedEvents = {}
    local jsonLines = {}

    for _, ev in ipairs(events) do
        if not sinceTimestamp or (ev.timestamp and ev.timestamp >= sinceTimestamp) then
            table.insert(exportedEvents, ev)
            table.insert(jsonLines, ToJSON(ev))
        end
    end

    local buildInfo = (GetBuildInfo and {GetBuildInfo()}) or {"unknown"}
    local inventoryJSON = ""
    local sellerLines = ""
    local mailLines = ""
    local undercutLines = ""
    local inventoryTable = nil
    local sellerRows = {}
    local mailRows = {}
    local undercutRows = {}
    if DFA_AI.Inventory and DFA_AI.Inventory.LatestJSON then
        local ok, value = pcall(DFA_AI.Inventory.LatestJSON, DFA_AI.Inventory)
        if ok and type(value) == "string" then inventoryJSON = value end
        if DFA_AI.Inventory.Latest then
            local okLatest, latest = pcall(DFA_AI.Inventory.Latest, DFA_AI.Inventory)
            if okLatest then inventoryTable = latest end
        end
    end
    if DFA_AI.Sellers and DFA_AI.Sellers.ToJSONLines then
        local ok, value = pcall(DFA_AI.Sellers.ToJSONLines, DFA_AI.Sellers)
        if ok and type(value) == "string" then sellerLines = value end
    end
    if DFA_AI.Mail and DFA_AI.Mail.ToJSONLines then
        local ok, value = pcall(DFA_AI.Mail.ToJSONLines, DFA_AI.Mail)
        if ok and type(value) == "string" then mailLines = value end
    end
    if DFA_AI.Undercut and DFA_AI.Undercut.ToJSONLines then
        local ok, value = pcall(DFA_AI.Undercut.ToJSONLines, DFA_AI.Undercut)
        if ok and type(value) == "string" then undercutLines = value end
    end
    if DFA_AUCTION_INTELLIGENCE_DB
            and type(DFA_AUCTION_INTELLIGENCE_DB.seller_observations) == "table" then
        sellerRows = DFA_AUCTION_INTELLIGENCE_DB.seller_observations
    end
    if DFA_AUCTION_INTELLIGENCE_DB
            and type(DFA_AUCTION_INTELLIGENCE_DB.mail_invoices) == "table" then
        mailRows = DFA_AUCTION_INTELLIGENCE_DB.mail_invoices
    end
    if DFA_AUCTION_INTELLIGENCE_DB
            and type(DFA_AUCTION_INTELLIGENCE_DB.undercut_observations) == "table" then
        undercutRows = DFA_AUCTION_INTELLIGENCE_DB.undercut_observations
    end
    -- Every HUD press, with its result.  The daemon imports this and runs
    -- the follow-up the button implies (a scan -> relist advice + alert).
    local actionRows = {}
    if DFA_AUCTION_INTELLIGENCE_DB
            and type(DFA_AUCTION_INTELLIGENCE_DB.actions) == "table" then
        actionRows = DFA_AUCTION_INTELLIGENCE_DB.actions
    end
    local actionLines = {}
    for _, row in ipairs(actionRows) do
        actionLines[#actionLines + 1] = ToJSON(row)
    end
    -- Kills, loot and copper per mob.  The flush turns the pending
    -- counters into one immutable batch; the export carries every batch
    -- still retained, so an import that happens late loses nothing and
    -- one that happens twice stores nothing twice.
    local killRows = {}
    if DFA_AI.Kills then
        local ok = pcall(function() DFA_AI.Kills:Flush() end)
        if ok then
            local okRows, rows = pcall(function() return DFA_AI.Kills:Rows() end)
            if okRows and type(rows) == "table" then killRows = rows end
        end
    end
    local killLines = {}
    for _, row in ipairs(killRows) do
        killLines[#killLines + 1] = ToJSON(row)
    end

    DFA_AUCTION_INTELLIGENCE_EXPORT = {
        schema_version = DFA_AI.SCHEMA_VERSION or 1,
        addon_version = DFA_AI.VERSION or "1.0.0",
        exported_at = time(),
        realm = GetRealmName() or "UnknownRealm",
        character = UnitName("player") or "UnknownPlayer",
        client_build = buildInfo[1] or "unknown",
        total_events = #exportedEvents,
        jsonl = table.concat(jsonLines, "\n"),
        events = exportedEvents,
        -- inventory picture and seller observations, both as a Lua table
        -- (easy to read in SavedVariables) and as JSON (easy for Python)
        inventory = inventoryTable,
        inventory_json = inventoryJSON,
        sellers = sellerRows,
        sellers_jsonl = sellerLines,
        mail_invoices = mailRows,
        mail_jsonl = mailLines,
        -- who posted the listings below my price, captured from the
        -- Auctionator undercut scan's own results
        undercut_observations = undercutRows,
        undercuts_jsonl = undercutLines,
        actions = actionRows,
        actions_jsonl = table.concat(actionLines, "\n"),
        -- what the player killed and looted; the backend prices it
        kills = killRows,
        kills_jsonl = table.concat(killLines, "\n"),
        -- proof of what the last sync read, next to the events themselves
        last_sync = DFA_AI.DataStore.GetLastSync and DFA_AI.DataStore:GetLastSync() or nil,
    }

    return #exportedEvents
end

function Export:GetLastExportStatus()
    if not DFA_AUCTION_INTELLIGENCE_EXPORT then
        return { has_export = false }
    end
    return {
        has_export = true,
        exported_at = DFA_AUCTION_INTELLIGENCE_EXPORT.exported_at,
        total_events = DFA_AUCTION_INTELLIGENCE_EXPORT.total_events,
        realm = DFA_AUCTION_INTELLIGENCE_EXPORT.realm,
    }
end
