-- DFA Auction Intelligence - Seller observations
--
-- The public Game Data API does not carry a seller field.  The client's
-- Auction House Browse tooltip does show one for non-commodity auctions,
-- so that is what this module reads.  It never invents a name:
--
--   * only a line whose label is a known "Seller" label is accepted;
--   * the raw line is stored next to the parsed value, so the Python
--     side can audit what was actually on screen;
--   * a commodity book has no seller line, so nothing is stored for it;
--   * an auction-info table is probed for an owner-like field, and the
--     field name is recorded rather than assumed.

local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.Sellers = {}
local Sellers = DFA_AI.Sellers

local MAX_OBSERVATIONS = 20000
local DEDUP_WINDOW_SECONDS = 600

-- enUS first; the rest are the client's own labels for the common
-- locales.  A label that is not in this list is not guessed at.
local BASE_LABELS = {
    "Seller", "Verkäufer", "Vendeur", "Vendedor", "Venditore",
    "Продавец", "판매자", "卖家", "賣家",
}

local function StripColors(text)
    if type(text) ~= "string" then return "" end
    text = text:gsub("|c%x%x%x%x%x%x%x%x", "")
    text = text:gsub("|r", "")
    return text
end

local function Trim(text)
    return (text or ""):match("^%s*(.-)%s*$")
end

local function Labels()
    local labels = {}
    for _, value in ipairs(BASE_LABELS) do labels[value] = true end
    local global_label = _G["AUCTION_HOUSE_SELLER"] or _G["SELLER"]
    if type(global_label) == "string" and global_label ~= "" then
        labels[global_label] = true
    end
    return labels
end

local function SplitSeller(value)
    local text = Trim(value)
    local name, realm = text:match("^([^%-]+)%-(.+)$")
    if name and name ~= "" then
        return Trim(name), Trim(realm)
    end
    return text, ""
end

function Sellers:ParseSellerLine(line)
    local text = Trim(StripColors(line))
    if text == "" then return nil end
    local label, value = text:match("^([^:]+):%s*(.-)$")
    if not label or not value or value == "" then return nil end
    label = Trim(label)
    value = Trim(value)
    if value == "" or #value > 64 then return nil end
    -- a player name never contains a pipe or an angle bracket; those
    -- belong to a link or a UI annotation, not a name
    if value:find("|", 1, true) or value:find("<", 1, true) then return nil end
    local known = Labels()
    if not known[label] and not known[string.lower(label)] then return nil end
    return { seller = value, label = label,
             sellerName = SplitSeller(value), confidence = "verified" }
end

local function ItemFromTooltip(tooltip)
    if not tooltip or not tooltip.GetItem then return nil, nil end
    local ok, name, link = pcall(tooltip.GetItem, tooltip)
    if not ok then return nil, nil end
    local itemId = nil
    if type(link) == "string" then
        itemId = tonumber(link:match("item:(%d+)"))
    end
    return itemId, name
end

local function Signature(observation)
    local bucket = math.floor((observation.observedAt or 0) / DEDUP_WINDOW_SECONDS)
    return string.format("%s|%s|%s|%s", tostring(observation.seller),
        tostring(observation.realm), tostring(observation.itemId), tostring(bucket))
end

-- Our own auction is shown as the literal "You" on the shop tooltip, not
-- as the character name, so comparing with UnitName() alone let it through.
local function IsSelfLabel(name)
    if type(name) ~= "string" or name == "" then return false end
    local lower = string.lower(name)
    local player = UnitName("player") or ""
    if player ~= "" and lower == string.lower(player) then return true end
    local you = _G["YOU"] or "You"
    return type(you) == "string" and lower == string.lower(you)
end

function Sellers:Record(observation)
    if type(observation) ~= "table" or not observation.seller then
        return false, "no seller"
    end
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) ~= "table" then return false, "store not initialized" end

    local player = UnitName("player") or ""
    local sellerName = observation.sellerName or SplitSeller(observation.seller)
    if IsSelfLabel(sellerName) or IsSelfLabel(observation.seller) then
        return false, "own auction"
    end

    observation.observedAt = observation.observedAt or time()
    db.seller_observations = db.seller_observations or {}
    local signature = Signature(observation)
    db.seller_index = db.seller_index or {}
    if db.seller_index[signature] then
        db.seller_stats = db.seller_stats or {}
        db.seller_stats.duplicates = (db.seller_stats.duplicates or 0) + 1
        return false, "duplicate"
    end

    observation.schemaVersion = DFA_AI.SCHEMA_VERSION or 1
    observation.character = observation.character or player
    observation.realm = observation.realm or GetRealmName() or ""
    table.insert(db.seller_observations, observation)
    db.seller_index[signature] = true
    db.seller_stats = db.seller_stats or {}
    db.seller_stats.total = #db.seller_observations
    db.seller_stats.last_capture = observation.observedAt
    db.seller_stats.by_source = db.seller_stats.by_source or {}
    local source = tostring(observation.source or "unknown")
    db.seller_stats.by_source[source] = (db.seller_stats.by_source[source] or 0) + 1
    while #db.seller_observations > MAX_OBSERVATIONS do
        table.remove(db.seller_observations, 1)
    end
    -- keep the dedupe index from outliving the rows it indexes
    if #db.seller_observations % 100 == 0 then
        db.seller_index = {}
        for _, row in ipairs(db.seller_observations) do
            db.seller_index[Signature(row)] = true
        end
    end
    return true
end

function Sellers:CaptureFromInfo(info, itemId, itemName)
    if type(info) ~= "table" then return false, "no info" end
    -- Detect the field instead of assuming a name.  The field that the
    -- client actually returns is recorded in ``label``/``source``.
    local candidates = { "ownerFullName", "ownerName", "owner", "sellerName", "seller" }
    for _, field in ipairs(candidates) do
        local value = info[field]
        if type(value) == "string" and value ~= "" then
            local name, realm = SplitSeller(value)
            return self:Record({
                observedAt = time(),
                realm = GetRealmName() or "",
                seller = value, sellerName = name, sellerRealm = realm,
                itemId = itemId, itemName = itemName or "",
                auctionId = nil,
                source = "auction_info", confidence = "verified",
                label = field, rawLine = field .. "=" .. value,
            })
        end
    end
    return false, "no owner field"
end

function Sellers:CaptureTooltip(tooltip, reason)
    if not tooltip or not tooltip.GetRegions then return false, "no tooltip" end
    -- Outside the Auction House an item tooltip can still contain a
    -- "Seller" line (a trade window or a mail); only the AH frame is
    -- accepted automatically.  The manual command overrides this.
    if reason ~= "manual" and AuctionHouseFrame
            and AuctionHouseFrame.IsShown and not AuctionHouseFrame:IsShown() then
        return false, "not in the auction house"
    end
    local itemId, itemName = ItemFromTooltip(tooltip)
    if not itemId then return false, "no item on the tooltip" end

    -- pack every return value: GetRegions() returns several regions and
    -- ``local ok, regions = pcall(...)`` would keep only the first
    local ok, regions = pcall(function() return { tooltip:GetRegions() } end)
    if not ok or not regions then return false, "no regions" end
    local found = nil
    for _, region in ipairs(regions) do
        if region and region.GetText then
            local okText, text = pcall(region.GetText, region)
            if okText and type(text) == "string" then
                local parsed = self:ParseSellerLine(text)
                if parsed then
                    found = parsed
                    break
                end
            end
        end
    end
    if not found then return false, "no seller line" end

    local name, sellerRealm = SplitSeller(found.seller)
    return self:Record({
        observedAt = time(),
        realm = GetRealmName() or "",
        seller = found.seller,
        sellerName = name,
        sellerRealm = sellerRealm,
        itemId = itemId,
        itemName = itemName or "",
        auctionId = nil,
        source = "tooltip",
        confidence = found.confidence,
        label = found.label,
        rawLine = StripColors(found.label .. ": " .. found.seller),
    })
end

local function Schedule(tooltip)
    if C_Timer and C_Timer.After then
        C_Timer.After(0, function() Sellers:CaptureTooltip(tooltip, "tooltip") end)
    else
        Sellers:CaptureTooltip(tooltip, "tooltip")
    end
end

function Sellers:InstallHooks()
    if self.hooked then return end
    self.hooked = true
    if not GameTooltip or not GameTooltip.HookScript then return end
    -- The lines are already populated when the tooltip is cleared, and
    -- OnShow covers a fresh tooltip.  SetAuctionItem is the method the
    -- Auction House browse button uses to fill the tooltip; when it does
    -- not exist in this client, SetItem is the fallback.  The capture
    -- itself refuses anything that is not an Auction House tooltip with
    -- a seller line, so a frequent hook is not a frequent write.
    --
    -- Each hook is installed independently: some clients do not expose
    -- every tooltip script, and a failure in the first hook must not stop
    -- OnShow from being installed.
    local function safeHook(script, handler)
        local ok = pcall(GameTooltip.HookScript, GameTooltip, script, handler)
        return ok
    end
    self.hookedCleared = safeHook("OnTooltipCleared", function(tt)
        Sellers:CaptureTooltip(tt, "tooltip")
    end)
    self.hookedShow = safeHook("OnShow", function(tt)
        Schedule(tt)
    end)
    local hookedMethod = false
    for _, method in ipairs({ "SetAuctionItem", "SetItem" }) do
        if type(GameTooltip[method]) == "function" then
            local ok = pcall(hooksecurefunc, GameTooltip, method, function(tt)
                Schedule(tt or GameTooltip)
            end)
            if ok then
                hookedMethod = true
                if method == "SetAuctionItem" then break end
            end
        end
    end
    self.hookedMethod = hookedMethod
end

local function EscapeJSON(value)
    local text = tostring(value or "")
    text = text:gsub("\\", "\\\\")
    text = text:gsub('"', '\\"')
    text = text:gsub("\n", "\\n")
    text = text:gsub("\r", "\\r")
    text = text:gsub("\t", "\\t")
    return '"' .. text .. '"'
end

function Sellers:ToJSONLines()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    local rows = db and db.seller_observations or {}
    local lines = {}
    for _, obs in ipairs(rows) do
        local fields = {
            "observedAt", "realm", "seller", "sellerName", "sellerRealm",
            "itemId", "itemName", "auctionId", "unitPriceCopper", "source",
            "confidence", "label", "rawLine", "character",
        }
        local parts = {}
        for _, key in ipairs(fields) do
            local value = obs[key]
            if type(value) == "number" then
                parts[#parts + 1] = EscapeJSON(key) .. ":" .. tostring(value)
            elseif type(value) == "string" then
                parts[#parts + 1] = EscapeJSON(key) .. ":" .. EscapeJSON(value)
            end
        end
        lines[#lines + 1] = "{" .. table.concat(parts, ",") .. "}"
    end
    return table.concat(lines, "\n")
end

function Sellers:GetStats()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    local stats = db and db.seller_stats or {}
    return {
        total = (db and db.seller_observations and #db.seller_observations) or 0,
        duplicates = stats.duplicates or 0,
        by_source = stats.by_source or {},
        last_capture = stats.last_capture,
        hooked = self.hooked and true or false,
    }
end
