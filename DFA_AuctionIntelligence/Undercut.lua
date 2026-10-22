-- DFA Auction Intelligence - who is undercutting me, by name
--
-- Auctionator's cancel/undercut scan already queries the Auction House once
-- per owned item.  The answer it gets back carries the owners of every
-- listing (``resultInfo.owners``; the player's own auctions say "player"),
-- which is exactly the seller identity the tooltip capture could not give
-- us.  This module hooks that scan and reads the results that are already
-- loaded: it does not send a single extra query, so it cannot fight the
-- Auction House throttle with Auctionator.
--
-- Nothing here is a sale.  It records a price fact: at time T, item I had
-- these listings below my unit price, posted by these sellers.

local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.Undercut = {}
local Undercut = DFA_AI.Undercut

local MAX_OBSERVATIONS = 20000
local MAX_SELLERS_PER_ROW = 10
local MAX_CACHED_LISTINGS = 60
local WOW_TOKEN_ID = 122284

Undercut.hooked = false
Undercut.errors = 0
Undercut.scanId = nil
Undercut.scanSellers = {}
Undercut.emitted = {}

local function Trim(text)
    return (text or ""):match("^%s*(.-)%s*$")
end

local function SplitSeller(owner)
    local name, realm = tostring(owner or ""):match("^(.-)%-(.+)$")
    if name and name ~= "" then return name, realm end
    return tostring(owner or ""), ""
end

local function ItemKeyString(itemKey)
    if type(itemKey) ~= "table" then return "" end
    return table.concat({
        tostring(itemKey.itemID or 0),
        tostring(itemKey.itemLevel or 0),
        tostring(itemKey.itemSuffix or 0),
        tostring(itemKey.battlePetSpeciesID or 0),
    }, ":")
end

local function NewScanId()
    return tostring(time()) .. "-" .. tostring(math.random(1000, 9999))
end

function Undercut:BeginScan(source)
    self.scanId = NewScanId()
    self.scanSource = source or self.scanSource or "auctionator_undercut_scan"
    self.scanSellers = {}
    self.emitted = {}
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) == "table" then
        db.undercut_stats = db.undercut_stats or {}
        db.undercut_stats.last_scan = time()
        db.undercut_stats.last_scan_id = self.scanId
    end
end

local function FirstOwner(info)
    local owners = type(info) == "table" and info.owners or nil
    if type(owners) ~= "table" then return nil end
    for _, owner in ipairs(owners) do
        local text = Trim(owner)
        if text ~= "" and text ~= "player" then return text end
    end
    return nil
end

local function AddListing(listings, owner, unit, quantity, buyout,
                          auctionId, timeLeft, itemLink)
    unit = tonumber(unit) or 0
    if not owner or unit <= 0 then return end
    local seller, realm = SplitSeller(owner)
    listings[#listings + 1] = {
        seller = seller,
        sellerRealm = realm,
        unitPriceCopper = unit,
        buyoutCopper = tonumber(buyout) or (unit * (tonumber(quantity) or 1)),
        quantity = tonumber(quantity) or 1,
        auctionId = tonumber(auctionId) or 0,
        timeLeft = tonumber(timeLeft) or 0,
        itemLink = itemLink or "",
    }
end

local function ReadItemResults(key, auction)
    local count = 0
    local ok, value = pcall(function()
        return C_AuctionHouse.GetNumItemSearchResults(key)
    end)
    if ok and type(value) == "number" then count = value end
    if count <= 0 then return nil end
    local listings = {}
    for index = 1, count do
        local okInfo, info = pcall(function()
            return C_AuctionHouse.GetItemSearchResultInfo(key, index)
        end)
        if okInfo and type(info) == "table" and auction then
            -- Equipment results can mix item levels.  Use Auctionator's own
            -- matcher when it is there, so a lower item level is not
            -- reported as an undercut of the same item.
            if Auctionator and Auctionator.Selling
                    and Auctionator.Selling.DoesItemMatchFromKey then
                local okMatch, matches = pcall(
                    Auctionator.Selling.DoesItemMatchFromKey,
                    auction.itemKey, auction.itemLink,
                    info.itemKey, info.itemLink)
                if okMatch and matches == false then info = nil end
            end
        end
        if okInfo and type(info) == "table" and info.buyoutAmount then
            local quantity = tonumber(info.quantity) or 1
            local buyout = tonumber(info.buyoutAmount) or 0
            AddListing(listings, FirstOwner(info),
                       math.floor(buyout / math.max(1, quantity)), quantity,
                       buyout, info.auctionID,
                       info.timeLeftSeconds or info.timeLeft, info.itemLink)
        end
        if #listings >= MAX_CACHED_LISTINGS then break end
    end
    return listings
end

local function ReadCommodityResults(itemID)
    local count = 0
    local ok, value = pcall(function()
        return C_AuctionHouse.GetNumCommoditySearchResults(itemID)
    end)
    if ok and type(value) == "number" then count = value end
    if count <= 0 then return nil end
    local listings = {}
    for index = 1, count do
        local okInfo, info = pcall(function()
            return C_AuctionHouse.GetCommoditySearchResultInfo(itemID, index)
        end)
        if okInfo and type(info) == "table" then
            local unit = tonumber(info.unitPrice) or 0
            local quantity = tonumber(info.quantity) or 1
            AddListing(listings, FirstOwner(info), unit, quantity,
                       unit * quantity, info.auctionID,
                       info.timeLeftSeconds or info.timeLeft, info.itemLink)
        end
        if #listings >= MAX_CACHED_LISTINGS then break end
    end
    return listings
end

-- Read the current search results.  Item and commodity results use different
-- Blizzard APIs; older code only read the item one and silently skipped every
-- commodity undercut.  `kind` comes from the event when we run our own scan;
-- the Auctionator hook passes nil and we detect it from the loaded result set.
function Undercut:ReadListings(key, auction, kind)
    if type(key) ~= "table" then return nil end
    local itemID = tonumber(key.itemID) or 0
    if not kind and C_AuctionHouse
            and type(C_AuctionHouse.GetItemKeyInfo) == "function" then
        -- Synchronous when the key is already cached (it is for an owned
        -- auction), and it tells us which result API the client filled.
        local okInfo, info = pcall(C_AuctionHouse.GetItemKeyInfo, key)
        if okInfo and type(info) == "table" then
            kind = info.isCommodity and "commodity" or "item"
        end
    end
    local listings, isCommodity = nil, false
    if kind == "item" then
        listings = ReadItemResults(key, auction)
    elseif kind == "commodity" then
        listings = ReadCommodityResults(itemID)
        isCommodity = true
    else
        listings = ReadItemResults(key, auction)
        if listings and #listings > 0 then
            isCommodity = false
        else
            listings = ReadCommodityResults(itemID)
            isCommodity = listings ~= nil and #listings > 0
        end
    end
    if not listings or #listings == 0 then return nil, isCommodity end
    table.sort(listings, function(a, b)
        if a.unitPriceCopper == b.unitPriceCopper then
            return (a.auctionId or 0) < (b.auctionId or 0)
        end
        return a.unitPriceCopper < b.unitPriceCopper
    end)
    return listings, isCommodity
end

function Undercut:ReadCurrentResults(scan)
    return self:ReadListings(scan and scan.expectedItemKey,
                             scan and scan.currentAuction)
end

function Undercut:CaptureResults(scan, auctionInfo)
    local auction = auctionInfo or (scan and scan.currentAuction)
    if type(auction) ~= "table" or type(auction.itemKey) ~= "table" then
        return
    end
    -- Auctionator's ProcessSearchResults has already called NextStep by the
    -- time the post-hook runs, so scan.currentAuction can already point at the
    -- *next* item.  Always prefer the auctionInfo argument from this call.
    local listings = self:ReadListings(
        (scan and scan.expectedItemKey) or auction.itemKey, auction)
    if not listings or #listings == 0 then
        listings = self:ReadListings(auction.itemKey, auction)
    end
    if not listings or #listings == 0 then return end
    self.scanSellers[ItemKeyString(auction.itemKey)] = listings
    self:Record(scan, auction)
end

function Undercut:CaptureCached(scan, auctionInfo)
    local auction = auctionInfo or (scan and scan.currentAuction)
    if type(auction) ~= "table" then return end
    local listings = self.scanSellers[ItemKeyString(auction.itemKey)]
    if not listings then return end
    self:Record(scan, auction)
end

local function AuctionName(auction)
    local name = Trim(auction and auction.searchName)
    if name ~= "" then return name end
    local link = auction and auction.itemLink
    if type(link) == "string" then
        local fromLink = link:match("%[(.-)%]")
        if fromLink and fromLink ~= "" then return fromLink end
    end
    local itemID = tonumber(auction and auction.itemKey
                            and auction.itemKey.itemID) or 0
    if itemID > 0 then
        if C_Item and type(C_Item.GetItemNameByID) == "function" then
            local ok, itemName = pcall(C_Item.GetItemNameByID, itemID)
            if ok and type(itemName) == "string" and itemName ~= "" then
                return itemName
            end
        end
        if type(GetItemInfo) == "function" then
            local ok, itemName = pcall(GetItemInfo, itemID)
            if ok and type(itemName) == "string" and itemName ~= "" then
                return itemName
            end
        end
    end
    return ""
end

-- Turn one owned auction plus the competing listings into one observation.
function Undercut:Record(scan, auction)
    if type(auction) ~= "table" then return false, "no auction" end
    local auctionId = tonumber(auction.auctionID) or 0
    if auctionId <= 0 then return false, "no auction id" end
    if self.emitted[auctionId] then return false, "already recorded" end
    local listings = self.scanSellers[ItemKeyString(auction.itemKey)]
    if not listings then return false, "no results" end

    local quantity = math.max(1, tonumber(auction.quantity) or 1)
    local buyout = tonumber(auction.buyoutAmount) or 0
    local ownUnit = math.floor(buyout / quantity)
    if ownUnit <= 0 then return false, "no buyout" end

    local below = {}
    local itemsAhead = 0
    local lowest = nil
    for _, listing in ipairs(listings) do
        if listing.unitPriceCopper < ownUnit then
            itemsAhead = itemsAhead + listing.quantity
            if not lowest or listing.unitPriceCopper < lowest.unitPriceCopper then
                lowest = listing
            end
            if #below < MAX_SELLERS_PER_ROW then
                below[#below + 1] = listing
            end
        end
    end
    self.emitted[auctionId] = true
    local db = DFA_AUCTION_INTELLIGENCE_DB
    db.undercut_stats = db.undercut_stats or {}
    if not lowest then
        db.undercut_stats.not_undercut =
            (db.undercut_stats.not_undercut or 0) + 1
        return false, "not undercut"
    end

    local itemName = AuctionName(auction)
    local itemId = auction.itemKey and tonumber(auction.itemKey.itemID) or 0
    local observedAt = time()
    local source = (scan and scan.source) or self.scanSource
        or "auctionator_undercut_scan"
    local record = {
        observedAt = observedAt,
        scanId = self.scanId or "",
        realm = GetRealmName() or "",
        character = UnitName("player") or "",
        itemId = itemId,
        itemName = itemName,
        itemKey = ItemKeyString(auction.itemKey),
        itemLink = auction.itemLink or "",
        ownAuctionId = auctionId,
        ownQuantity = quantity,
        ownBuyoutCopper = buyout,
        ownUnitPriceCopper = ownUnit,
        lowestUnitPriceCopper = lowest.unitPriceCopper,
        lowestSeller = lowest.seller,
        lowestSellerRealm = lowest.sellerRealm,
        lowestAuctionId = lowest.auctionId,
        undercutCopper = ownUnit - lowest.unitPriceCopper,
        undercutPct = (ownUnit - lowest.unitPriceCopper) / ownUnit,
        itemsAhead = itemsAhead,
        sellersBelow = #below,
        sellers = below,
        timeLeft = tonumber(auction.timeLeftSeconds or auction.timeLeft) or 0,
        source = source,
        confidence = "verified",
    }

    db.undercut_observations = db.undercut_observations or {}
    db.undercut_index = db.undercut_index or {}
    local recordKey = tostring(record.scanId) .. "|" .. tostring(auctionId)
    if db.undercut_index[recordKey] then return false, "duplicate" end
    db.undercut_index[recordKey] = true
    table.insert(db.undercut_observations, record)
    db.undercut_stats.total = #db.undercut_observations
    db.undercut_stats.last_capture = observedAt
    db.undercut_stats.by_seller = db.undercut_stats.by_seller or {}
    if lowest.seller ~= "" then
        db.undercut_stats.by_seller[lowest.seller] =
            (db.undercut_stats.by_seller[lowest.seller] or 0) + 1
    end

    while #db.undercut_observations > MAX_OBSERVATIONS do
        table.remove(db.undercut_observations, 1)
    end
    if #db.undercut_observations % 100 == 0 then
        db.undercut_index = {}
        for _, row in ipairs(db.undercut_observations) do
            db.undercut_index[tostring(row.scanId) .. "|"
                .. tostring(row.ownAuctionId)] = true
        end
    end
    return true
end

-- ---------------------------------------------------------------------------
-- Our own scan, for when Auctionator is not the tool being used
--
-- The default Auction House UI never shows the seller, but the search
-- results it receives do: ``resultInfo.owners[1]`` is the seller, and our
-- own listings say "player".  This scan asks the same questions Auctionator
-- asks -- the owned list, then one search per owned item -- and reads the
-- owners from the answers.  It is slower than the hook (one query per item,
-- rate limited by the client), so it runs only when asked with
-- ``/dfaai undercut``.

local function Later(delay, fn)
    if C_Timer and C_Timer.After then
        C_Timer.After(delay, fn)
    else
        fn()
    end
end

Undercut.SORTS = {
    { sortOrder = (_G.Enum and Enum.AuctionHouseSortOrder
                   and Enum.AuctionHouseSortOrder.Name) or 1,
      reverseSort = false },
    { sortOrder = (_G.Enum and Enum.AuctionHouseSortOrder
                   and Enum.AuctionHouseSortOrder.Price) or 2,
      reverseSort = false },
}

local function IsCommodityKey(key)
    if C_AuctionHouse and type(C_AuctionHouse.GetItemKeyInfo) == "function" then
        local ok, info = pcall(C_AuctionHouse.GetItemKeyInfo, key)
        if ok and type(info) == "table" and info.isCommodity ~= nil then
            return info.isCommodity
        end
    end
    return nil
end

-- Auctionator's own constants: commodity and item searches need different
-- sort orders.  When the key's commodity status is not cached yet, sort by
-- Price (0), which is valid for both result types; Buyout (4) is not a
-- commodity sort and can make the client return no results.
local function SearchSorts(isCommodity)
    local constants = Auctionator and Auctionator.Constants
    if isCommodity == true then
        if constants and constants.CommodityResultsSorts then
            return { constants.CommodityResultsSorts }
        end
        return { { sortOrder = 0, reverseSort = false } }
    end
    if isCommodity == false then
        if constants and constants.ItemResultsSorts then
            return { constants.ItemResultsSorts }
        end
        return { { sortOrder = 4, reverseSort = false } }
    end
    return { { sortOrder = 0, reverseSort = false } }
end

function Undercut:NativeState()
    self.native = self.native or {
        scanning = false, awaitingOwned = false, awaitingResults = false,
        index = 0, scanned = 0, skipped = 0, noResults = 0, timeouts = 0,
        token = 0,
    }
    return self.native
end

function Undercut:AtAuctionHouse()
    local frame = _G.AuctionHouseFrame
    if frame and type(frame.IsShown) == "function" then
        local ok, shown = pcall(frame.IsShown, frame)
        if ok then return shown end
    end
    return true
end

function Undercut:StartScan()
    local state = self:NativeState()
    if state.scanning then return false, "a scan is already running" end
    if not (C_AuctionHouse and C_AuctionHouse.QueryOwnedAuctions
            and C_AuctionHouse.SendSearchQuery) then
        return false, "the Auction House API is unavailable"
    end
    if not self:AtAuctionHouse() then
        return false, "open the Auction House first"
    end
    self:BeginScan("native_undercut_scan")
    state.scanning = true
    state.awaitingOwned = true
    state.awaitingResults = false
    state.index = 0
    state.scanned = 0
    state.skipped = 0
    state.noResults = 0
    state.timeouts = 0
    state.startedAt = time()
    local ok, qErr = xpcall(function()
        C_AuctionHouse.QueryOwnedAuctions(self.SORTS)
    end, function(err)
        local msg = tostring(err)
        local stack = ""
        if type(debugstack) == "function" then
            local okS, s = pcall(debugstack)
            if okS then stack = tostring(s) end
        end
        local lower = msg:lower()
        if lower:find("blocked", 1, true) or lower:find("forbidden", 1, true)
                or lower:find("taint", 1, true) or lower:find("secure", 1, true) then
            if DFA_AI and DFA_AI.RecordBlocked then
                DFA_AI.RecordBlocked("undercut_scan",
                    "C_AuctionHouse.QueryOwnedAuctions", msg, stack)
            end
        end
        return msg
    end)
    if not ok then
        state.scanning = false
        return false, "the owned-auction query failed: " .. tostring(qErr or "")
    end
    -- If the update event never arrives, use whatever list is loaded.
    Later(10, function()
        if state.scanning and state.awaitingOwned then
            state.awaitingOwned = false
            self:NextStep()
        end
    end)
    return true, "scan started"
end

function Undercut:OnOwnedAuctionsUpdated()
    local state = self.native
    if not state or not state.scanning or not state.awaitingOwned then return end
    state.awaitingOwned = false
    self:NextStep()
end

function Undercut:NextStep()
    local state = self:NativeState()
    if not state.scanning then return end
    state.index = state.index + 1
    local okCount, count = pcall(C_AuctionHouse.GetNumOwnedAuctions)
    if not okCount or type(count) ~= "number" or state.index > count then
        self:FinishScan("done")
        return
    end
    local okInfo, auction = pcall(C_AuctionHouse.GetOwnedAuctionInfo, state.index)
    if not okInfo or type(auction) ~= "table" then
        state.skipped = state.skipped + 1
        Later(0.05, function() self:NextStep() end)
        return
    end
    local key = auction.itemKey
    local quantity = tonumber(auction.quantity) or 0
    local buyout = tonumber(auction.buyoutAmount) or 0
    local itemID = type(key) == "table" and tonumber(key.itemID) or 0
    local status = tonumber(auction.status) or 0
    local tokenID = (Auctionator and Auctionator.Constants
                     and Auctionator.Constants.WOW_TOKEN_ID) or WOW_TOKEN_ID
    if type(key) ~= "table" or itemID <= 0 or itemID == tokenID
            or status == 1 or buyout <= 0 or quantity <= 0
            or auction.bidder ~= nil then
        state.skipped = state.skipped + 1
        Later(0.05, function() self:NextStep() end)
        return
    end

    state.current = auction
    state.awaitingResults = true
    state.awaitingItemID = itemID
    state.token = state.token + 1
    local token = state.token
    local sorts = SearchSorts(IsCommodityKey(key))
    local ok, sqErr = xpcall(function()
        C_AuctionHouse.SendSearchQuery(key, sorts, true)
    end, function(err)
        local msg = tostring(err)
        local stack = ""
        if type(debugstack) == "function" then
            local okS, s = pcall(debugstack)
            if okS then stack = tostring(s) end
        end
        local lower = msg:lower()
        if lower:find("blocked", 1, true) or lower:find("forbidden", 1, true)
                or lower:find("taint", 1, true) or lower:find("secure", 1, true) then
            if DFA_AI and DFA_AI.RecordBlocked then
                DFA_AI.RecordBlocked("undercut_scan",
                    "C_AuctionHouse.SendSearchQuery", msg, stack)
            end
        end
        return msg
    end)
    if not ok then
        state.awaitingResults = false
        state.skipped = state.skipped + 1
        Later(0.3, function() self:NextStep() end)
        return
    end
    -- The client is rate limited; if no answer arrives, move on instead of
    -- stalling the whole scan on one item.
    Later(6, function()
        if state.scanning and state.awaitingResults
                and state.token == token then
            state.awaitingResults = false
            state.timeouts = state.timeouts + 1
            self:NextStep()
        end
    end)
end

function Undercut:OnSearchResults(kind, eventArg)
    local state = self.native
    if not state or not state.scanning or not state.awaitingResults then return end
    -- The AH events are global.  Ignore a result event for the item the user
    -- happened to browse while our query was in flight; otherwise one manual
    -- search consumes the observation for the owned item.
    local expectedID = tonumber(state.awaitingItemID) or 0
    local eventID = 0
    if kind == "item" then
        if type(eventArg) ~= "table" then return end
        eventID = tonumber(eventArg.itemID) or 0
    elseif kind == "commodity" then
        eventID = tonumber(eventArg) or 0
    else
        return
    end
    if expectedID > 0 and eventID ~= expectedID then return end
    if eventID <= 0 then return end
    local auction = state.current
    if type(auction) ~= "table" then
        state.awaitingResults = false
        state.awaitingItemID = nil
        self:NextStep()
        return
    end
    state.awaitingResults = false
    state.awaitingItemID = nil
    state.token = state.token + 1
    local listings = self:ReadListings(auction.itemKey, auction, kind)
    if listings and #listings > 0 then
        self.scanSellers[ItemKeyString(auction.itemKey)] = listings
        state.scanned = state.scanned + 1
        self:Record(state, auction)
    else
        state.noResults = state.noResults + 1
    end
    Later(0.3, function() self:NextStep() end)
end

function Undercut:FinishScan(reason)
    local state = self:NativeState()
    if not state.scanning then return end
    state.scanning = false
    state.awaitingOwned = false
    state.awaitingResults = false
    state.awaitingItemID = nil
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) == "table" then
        db.undercut_stats = db.undercut_stats or {}
        db.undercut_stats.last_native = {
            at = time(), reason = reason or "done",
            scanned = state.scanned, skipped = state.skipped,
            no_results = state.noResults, timeouts = state.timeouts,
        }
    end
    pcall(function() DFA_AI.Export:GenerateExport() end)
    print((DFA_AI.L and DFA_AI.L.PREFIX or "DFA: ")
        .. string.format("Undercut scan %s: %d read, %d skipped, "
            .. "%d without results, %d timed out",
            tostring(reason or "done"), state.scanned, state.skipped,
            state.noResults, state.timeouts))
end

function Undercut:AbortScan(reason)
    local state = self.native
    if state and state.scanning then self:FinishScan(reason or "aborted") end
end

function Undercut:InstallHooks()
    if self.hooked then return true end
    local mixin = _G.AuctionatorUndercutScanMixin
    if type(mixin) ~= "table" or type(mixin.ProcessSearchResults) ~= "function" then
        return false
    end
    if type(hooksecurefunc) ~= "function" then return false end

    hooksecurefunc(mixin, "StartScan", function()
        pcall(function() Undercut:BeginScan("auctionator_undercut_scan") end)
    end)
    -- Auctionator has already fetched the results; read them while they are
    -- still the current result set.  The auctionInfo argument is the item this
    -- result belongs to, not scan.currentAuction (NextStep may already have
    -- moved that on).
    hooksecurefunc(mixin, "ProcessSearchResults", function(scan, auctionInfo)
        local ok = pcall(function()
            Undercut:CaptureResults(scan, auctionInfo)
        end)
        if not ok then Undercut.errors = Undercut.errors + 1 end
    end)
    -- A second auction of the same item reuses the cached results without a
    -- new query; it still deserves its own observation.
    hooksecurefunc(mixin, "ProcessUndercutResult", function(scan, auctionInfo)
        pcall(function() Undercut:CaptureCached(scan, auctionInfo) end)
    end)
    self.hooked = true
    return true
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

local function SellersJSON(sellers)
    local parts = {}
    for _, listing in ipairs(sellers or {}) do
        parts[#parts + 1] = "{" .. table.concat({
            '"seller":' .. EscapeJSON(listing.seller),
            '"sellerRealm":' .. EscapeJSON(listing.sellerRealm),
            '"unitPriceCopper":' .. tostring(listing.unitPriceCopper or 0),
            '"buyoutCopper":' .. tostring(listing.buyoutCopper or 0),
            '"quantity":' .. tostring(listing.quantity or 0),
            '"auctionId":' .. tostring(listing.auctionId or 0),
            '"timeLeft":' .. tostring(listing.timeLeft or 0),
        }, ",") .. "}"
    end
    return "[" .. table.concat(parts, ",") .. "]"
end

function Undercut:ToJSONLines()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    local rows = db and db.undercut_observations or {}
    local lines = {}
    for _, row in ipairs(rows) do
        lines[#lines + 1] = "{" .. table.concat({
            '"observedAt":' .. tostring(row.observedAt or 0),
            '"scanId":' .. EscapeJSON(row.scanId),
            '"realm":' .. EscapeJSON(row.realm),
            '"character":' .. EscapeJSON(row.character),
            '"itemId":' .. tostring(row.itemId or 0),
            '"itemName":' .. EscapeJSON(row.itemName),
            '"itemKey":' .. EscapeJSON(row.itemKey),
            '"itemLink":' .. EscapeJSON(row.itemLink),
            '"ownAuctionId":' .. tostring(row.ownAuctionId or 0),
            '"ownQuantity":' .. tostring(row.ownQuantity or 0),
            '"ownBuyoutCopper":' .. tostring(row.ownBuyoutCopper or 0),
            '"ownUnitPriceCopper":' .. tostring(row.ownUnitPriceCopper or 0),
            '"lowestUnitPriceCopper":' .. tostring(row.lowestUnitPriceCopper or 0),
            '"lowestSeller":' .. EscapeJSON(row.lowestSeller),
            '"lowestSellerRealm":' .. EscapeJSON(row.lowestSellerRealm),
            '"lowestAuctionId":' .. tostring(row.lowestAuctionId or 0),
            '"undercutCopper":' .. tostring(row.undercutCopper or 0),
            '"undercutPct":' .. string.format("%.6f", tonumber(row.undercutPct) or 0),
            '"itemsAhead":' .. tostring(row.itemsAhead or 0),
            '"sellersBelow":' .. tostring(row.sellersBelow or 0),
            '"timeLeft":' .. tostring(row.timeLeft or 0),
            '"sellers":' .. SellersJSON(row.sellers),
            '"source":' .. EscapeJSON(row.source),
            '"confidence":' .. EscapeJSON(row.confidence),
        }, ",") .. "}"
    end
    return table.concat(lines, "\n")
end

function Undercut:GetStats()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    local stats = db and db.undercut_stats or {}
    return {
        total = (db and db.undercut_observations
                 and #db.undercut_observations) or 0,
        last_capture = stats.last_capture,
        last_scan = stats.last_scan,
        last_scan_id = stats.last_scan_id,
        last_native = stats.last_native,
        not_undercut = stats.not_undercut or 0,
        by_seller = stats.by_seller or {},
        hooked = self.hooked,
        errors = self.errors,
        scan_id = self.scanId,
        source = self.scanSource,
    }
end
