-- DFA Auction Intelligence - Inventory snapshots (bags, bank, posted stock)
--
-- The Python reconciler needs a full picture at a known instant, not a
-- stream of guesses.  This module takes that picture and records which
-- locations it could actually read:
--
--   * a bank the character is nowhere near is *unknown*, not empty;
--   * a posted-auction API that is unavailable is *unknown*, not zero.
--
-- Writing an empty list for an unreadable location would make the
-- reconciler report a disappearance that never happened.

local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.Inventory = {}
local Inventory = DFA_AI.Inventory

local MAX_SNAPSHOTS = 48

local function EscapeJSON(value)
    if value == nil then return "null" end
    local text = tostring(value)
    text = text:gsub("\\", "\\\\")
    text = text:gsub('"', '\\"')
    text = text:gsub("\n", "\\n")
    text = text:gsub("\r", "\\r")
    text = text:gsub("\t", "\\t")
    return '"' .. text .. '"'
end

local function Encode(value)
    local kind = type(value)
    if kind == "number" then
        return tostring(value)
    elseif kind == "boolean" then
        return value and "true" or "false"
    elseif kind == "string" then
        return EscapeJSON(value)
    elseif kind == "table" then
        local parts = {}
        if #value > 0 then
            for _, row in ipairs(value) do
                parts[#parts + 1] = Encode(row)
            end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        for key, row in pairs(value) do
            parts[#parts + 1] = EscapeJSON(key) .. ":" .. Encode(row)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return "null"
end

local function AddUnit(map, order, itemId, name, quantity)
    itemId = tonumber(itemId)
    quantity = tonumber(quantity)
    if not itemId or itemId <= 0 or not quantity or quantity <= 0 then return end
    local row = map[itemId]
    if not row then
        row = { itemId = itemId, itemName = name or "", quantity = 0 }
        map[itemId] = row
        order[#order + 1] = row
    end
    if (row.itemName == nil or row.itemName == "") and name and name ~= "" then
        row.itemName = name
    end
    row.quantity = row.quantity + quantity
end

local function ScanContainers(firstBag, lastBag, map, order)
    if not (C_Container and C_Container.GetContainerNumSlots
            and C_Container.GetContainerItemInfo) then
        return false, 0
    end
    local readable = false
    local slotsRead = 0
    for bag = firstBag, lastBag do
        local okCount, count = pcall(C_Container.GetContainerNumSlots, bag)
        if okCount and type(count) == "number" and count > 0 then
            readable = true
            for slot = 1, count do
                local okInfo, info = pcall(C_Container.GetContainerItemInfo, bag, slot)
                if okInfo and type(info) == "table" and info.itemID then
                    local name = info.itemName
                    if (not name or name == "") and info.hyperlink then
                        name = info.hyperlink:match("%[(.-)%]")
                    end
                    AddUnit(map, order, info.itemID, name,
                            info.stackCount or info.quantity or 1)
                    slotsRead = slotsRead + 1
                end
            end
        end
    end
    return readable, slotsRead
end

local function ScanPosted(map, order, listings)
    if not (C_AuctionHouse and C_AuctionHouse.GetNumOwnedAuctions
            and C_AuctionHouse.GetOwnedAuctionInfo) then
        return false, 0
    end
    -- A closed Auction House keeps the owned-auction list unloaded, and
    -- GetNumOwnedAuctions() then answers 0.  That is not an empty shelf:
    -- without a completed owned-auction query the list is unknown.  The
    -- logout snapshot used to record 0 there, eleven seconds after a
    -- manual snapshot had read 310 posted units.
    local hasFull = C_AuctionHouse.HasFullOwnedAuctionResults
    if type(hasFull) == "function" then
        local okFull, full = pcall(hasFull)
        if not okFull or not full then return false, 0 end
    end
    local okCount, count = pcall(C_AuctionHouse.GetNumOwnedAuctions)
    if not okCount or type(count) ~= "number" then return false, 0 end
    if count == 0 and type(hasFull) ~= "function" then
        -- no completion signal to distinguish "none" from "not loaded"
        return false, 0
    end
    for index = 1, count do
        local okInfo, info = pcall(C_AuctionHouse.GetOwnedAuctionInfo, index)
        if okInfo and type(info) == "table" then
            local itemId = nil
            local name = info.itemName
            if type(info.itemKey) == "table" then
                itemId = info.itemKey.itemID
            end
            if not itemId and type(info.itemLink) == "string" then
                itemId = tonumber(info.itemLink:match("item:(%d+)"))
            end
            local quantity = tonumber(info.quantity) or 1
            local buyout = tonumber(info.buyoutAmount) or 0
            AddUnit(map, order, itemId, name, quantity)
            -- The aggregate above answers "how much do I have posted".
            -- The per-listing row answers "at what price, next to whom":
            -- without the price the out-of-game Blizzard scan cannot match
            -- a listing to its competitors.
            listings[#listings + 1] = {
                auctionId = tonumber(info.auctionID) or 0,
                itemId = itemId or 0,
                itemName = name or "",
                quantity = quantity,
                unitPriceCopper = quantity > 0
                    and math.floor(buyout / quantity + 0.5) or 0,
                buyoutCopper = buyout,
                timeLeft = tonumber(info.timeLeft) or 0,
            }
        end
    end
    return true, count
end

-- Bank containers changed in 11.2: the character bank and the warband bank
-- are tab containers, not a fixed bag range.  The purchased tab ids are
-- container ids the client already reads; the old range stays only as a
-- fallback, because a client that exposes the tabs but not their contents
-- would otherwise report the bank as unknown when it is right there.
local function PurchasedTabs(bankType)
    if not (C_Bank and C_Bank.FetchPurchasedBankTabIDs and Enum
            and Enum.BankType and Enum.BankType[bankType]) then
        return nil
    end
    local ok, tabs = pcall(C_Bank.FetchPurchasedBankTabIDs,
        Enum.BankType[bankType])
    if ok and type(tabs) == "table" and #tabs > 0 then
        return tabs
    end
    return nil
end

local function ScanTabContainers(tabs, map, order)
    local readable, slots = false, 0
    for _, container in ipairs(tabs) do
        local ok, tabRead, tabSlots = pcall(ScanContainers, container,
            container, map, order)
        if ok and tabRead then readable = true end
        if ok then slots = slots + (tonumber(tabSlots) or 0) end
    end
    return readable, slots
end

local function ScanBank(map, order)
    local tabs = PurchasedTabs("Character")
    if tabs then
        local readable, slots = ScanTabContainers(tabs, map, order)
        if readable then return true, slots end
    end
    local first = (_G.NUM_BAG_SLOTS or 4) + 1
    return ScanContainers(first, first + ((_G.NUM_BANKBAGS or 7) - 1),
        map, order)
end

-- Account-wide storage: never reconciled as personal stock (an alt can move
-- it), but it is stock the player owns and can post from, so it is read and
-- kept apart from the character bank.
local function ScanWarband(map, order)
    local tabs = PurchasedTabs("Account")
    if tabs then
        local readable, slots = ScanTabContainers(tabs, map, order)
        if readable then return true, slots end
    end
    -- Warband tab bags are containers 13..17; an unpurchased tab answers 0
    -- slots, so this stays "unknown" instead of "empty".
    return ScanContainers(13, 17, map, order)
end

-- Guild bank: shared storage, so it is recorded but never reconciled as
-- the player's own stock.  Item data only exists while the frame is open and
-- the tab is viewable; without that it stays unknown, not empty.
local GUILD_BANK_SLOTS = 98

local function ScanGuildBank(map, order)
    if type(GetNumGuildBankTabs) ~= "function"
            or type(GetGuildBankItemInfo) ~= "function" then
        return false, 0
    end
    local okTabs, tabs = pcall(GetNumGuildBankTabs)
    if not okTabs or type(tabs) ~= "number" or tabs <= 0 then return false, 0 end
    local readable = false
    local slotsRead = 0
    for tab = 1, tabs do
        local okTab, _, _, viewable = pcall(GetGuildBankTabInfo, tab)
        if okTab and viewable then
            readable = true
            for slot = 1, GUILD_BANK_SLOTS do
                local ok, _texture, count, _locked, _filtered, _quality,
                    itemId, _usable, link =
                    pcall(GetGuildBankItemInfo, tab, slot)
                if ok and itemId and itemId > 0 and (count or 0) > 0 then
                    local name = ""
                    if type(link) == "string" then
                        name = link:match("%[(.-)%]") or ""
                    end
                    AddUnit(map, order, itemId, name, count)
                    slotsRead = slotsRead + 1
                end
            end
        end
    end
    return readable, slotsRead
end

local function BuildSnapshot(reason)
    local bags, bagOrder = {}, {}
    local bank, bankOrder = {}, {}
    local posted, postedOrder = {}, {}
    local postedListings = {}
    local warband, warbandOrder = {}, {}
    local guildbank, guildbankOrder = {}, {}

    local bagsOk, _ = ScanContainers(0, _G.NUM_BAG_SLOTS or 4, bags, bagOrder)
    local bankOk = ScanBank(bank, bankOrder)
    local warbandOk = ScanWarband(warband, warbandOrder)
    local postedOk, postedCount = ScanPosted(posted, postedOrder, postedListings)
    local guildOk = ScanGuildBank(guildbank, guildbankOrder)

    -- If the bag API is missing there is no picture at all.  Refusing to
    -- store one is safer than storing zeros.
    if not bagsOk then
        return nil, "bag API unavailable"
    end

    return {
        capturedAt = time(),
        reason = reason or "manual",
        character = UnitName("player") or "",
        realm = GetRealmName() or "",
        bagSlots = _G.NUM_BAG_SLOTS or 4,
        available = { bags = true, bank = bankOk and true or false,
                      posted = postedOk and true or false,
                      warband = warbandOk and true or false,
                      guildbank = guildOk and true or false },
        locations = {
            bags = bagOrder,
            bank = bankOk and bankOrder or nil,
            posted = postedOk and postedOrder or nil,
            warband = warbandOk and warbandOrder or nil,
            guildbank = guildOk and guildbankOrder or nil,
        },
        postedListings = postedOk and postedListings or nil,
        postedCount = postedCount or 0,
    }
end

function Inventory:Snapshot(reason)
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) ~= "table" then
        return nil, "the store is not initialized"
    end
    local snapshot, problem = BuildSnapshot(reason)
    if not snapshot then
        db.inventory_stats = db.inventory_stats or {}
        db.inventory_stats.last_error = problem
        return nil, problem
    end
    db.inventory_snapshots = db.inventory_snapshots or {}
    local last = db.inventory_snapshots[#db.inventory_snapshots]
    if last and last.capturedAt == snapshot.capturedAt
            and last.reason == snapshot.reason then
        return last
    end
    table.insert(db.inventory_snapshots, snapshot)
    while #db.inventory_snapshots > MAX_SNAPSHOTS do
        table.remove(db.inventory_snapshots, 1)
    end
    db.inventory_latest = snapshot
    db.inventory_stats = db.inventory_stats or {}
    db.inventory_stats.snapshots = #db.inventory_snapshots
    db.inventory_stats.last_capture = snapshot.capturedAt
    db.inventory_stats.last_error = nil
    return snapshot
end

-- At login the owned-auction list is not loaded, so `posted` is unknown.
-- The moment the client finishes an owned-auction query (the Auctions tab,
-- Auctionator or TSM), the picture becomes readable: take it then, not only
-- at logout.  Throttled so browsing the AH cannot flood the history.
local lastAuctionsSnapshot = 0
local lastGuildSnapshot = 0
local lastBankSnapshot = 0

function Inventory:SnapshotOnAuctions(reason)
    if (time() - lastAuctionsSnapshot) < 120 then
        return nil, "throttled"
    end
    lastAuctionsSnapshot = time()
    return self:Snapshot(reason or "auctions")
end

-- The bank is only readable while its frame is open (and the warband tabs
-- only once bought).  Opening it is the moment the picture is complete.
function Inventory:SnapshotOnBank(reason)
    if (time() - lastBankSnapshot) < 120 then
        return nil, "throttled"
    end
    lastBankSnapshot = time()
    return self:Snapshot(reason or "bank")
end

-- The guild bank is shared: record it when it is open, but never treat an
-- unreadable tab as empty, and never reconcile it as personal stock.
function Inventory:SnapshotOnGuildBank(reason)
    if (time() - lastGuildSnapshot) < 120 then
        return nil, "throttled"
    end
    lastGuildSnapshot = time()
    return self:Snapshot(reason or "guildbank")
end

function Inventory:Latest()
    return DFA_AUCTION_INTELLIGENCE_DB
        and DFA_AUCTION_INTELLIGENCE_DB.inventory_latest or nil
end

function Inventory:LatestJSON()
    local snapshot = self:Latest()
    if not snapshot then return "" end
    return Encode(snapshot)
end

function Inventory:GetStats()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    local latest = db and db.inventory_latest or nil
    if not latest then
        return { available = false, snapshots = 0 }
    end
    local counts = { bags = 0, bank = 0, posted = 0, warband = 0, guildbank = 0 }
    for _, location in ipairs({ "bags", "bank", "posted", "warband", "guildbank" }) do
        for _, row in ipairs((latest.locations and latest.locations[location]) or {}) do
            counts[location] = counts[location] + (row.quantity or 0)
        end
    end
    return {
        available = true,
        captured_at = latest.capturedAt,
        reason = latest.reason,
        locations = counts,
        listings = latest.postedListings and #latest.postedListings or 0,
        readable = latest.available or {},
        snapshots = (db and db.inventory_stats and db.inventory_stats.snapshots) or 0,
        last_error = db and db.inventory_stats and db.inventory_stats.last_error,
    }
end
