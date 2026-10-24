-- ZoneTimerRedux/Loot.lua
--
-- The loot ledger: how many of each item, per zone, so Pricing.lua can value
-- it.
--
-- Loot reaches this file from two places, and both are deduplicated against
-- the same corpse so a drop is counted exactly once:
--
--   * this file's own capture, on LOOT_OPENED/LOOT_CLOSED.  It is the one
--     that has to work: DFA_AuctionIntelligence's feed answered for only a
--     handful of corpses in a long session (its own counters read
--     `loots = 11`, `kills = 0`), so a ledger fed by it alone showed almost
--     nothing, and nothing at all in most zones.
--   * DFA_AuctionIntelligence's feed, when it does answer.  It attributes
--     loot to the mob and persists the observation the backend imports for
--     the farm models, so when it speaks it is the better source and its
--     answer is taken first.
--
-- The dedupe is deliberately *time-bounded*.  A corpse GUID identifies a
-- spawn, not a kill: the client hands the same spawn the same GUID again
-- after the mob respawns, so a permanent memo ("this corpse was counted")
-- would silently swallow every kill after the first one of each spawn point.
-- A corpse reopened for leftovers is the same drop; the same spawn killed a
-- minute later is not.
--
-- Copper is deliberately *not* taken from the loot window: CHAT_MSG_MONEY
-- already reports it (ZoneGoldDB), and counting both would double the cash.
-- The loot window's copper is kept only on the per-mob session ledger, for
-- the "gold per kill" line, where it is not added to any zone total.

local ZTR = ZoneTimerRedux

--: account-wide ledgers, the same split as ZoneGoldDB / ZoneTimerCharDB.gold
ZoneLootDB = ZoneLootDB or {}
ZoneTimerCharDB.loot = ZoneTimerCharDB.loot or {}

--: mobs seen this session (not persisted: MobInfo2 keeps the history)
ZTR.mobs = ZTR.mobs or {}

--: how long a corpse GUID counts as "the same corpse".  Long enough to cover
--: a reopened loot window, short enough that a respawned mob is a new drop.
local RECENT_SECONDS = 120

--: guid -> time() it was recorded, plus the order to trim by
local recent, recentOrder = {}, {}
local MAX_RECENT = 300

--: what the client's loot window showed, per corpse, until it closes
local pendingLoot = nil

local subscribed = false

local function Now()
    return (type(time) == "function") and time() or 0
end

---The npc id in a creature GUID, or nil for anything else.
---"Creature-0-1465-0-12345-3717-00005BA170" -> 3717
---@param guid string?
---@return number?
local function NpcIdFromGUID(guid)
    if type(guid) ~= "string" then return nil end
    local kind = guid:match("^([%a]+)%-")
    if kind ~= "Creature" then return nil end
    local fields = {}
    for field in guid:gmatch("[^-]+") do
        fields[#fields + 1] = field
        if #fields > 7 then break end
    end
    return tonumber(fields[6])
end

---Whether this corpse was already counted, within the dedupe window.
---@param guid string?
---@return boolean
local function Seen(guid)
    if not guid then return false end
    local at = recent[guid]
    if not at then return false end
    if (Now() - at) < RECENT_SECONDS then return true end
    -- the spawn's GUID came back around; this is a new kill, not leftovers
    recent[guid] = nil
    return false
end

---Remembers that this corpse was counted.
---@param guid string?
local function Mark(guid)
    if not guid then return end
    if recent[guid] then return end
    recent[guid] = Now()
    recentOrder[#recentOrder + 1] = guid
    while #recentOrder > MAX_RECENT do
        local oldest = table.remove(recentOrder, 1)
        recent[oldest] = nil
    end
end

local function Mob(npcId, name)
    npcId = tonumber(npcId)
    if not npcId then return nil end
    local mob = ZTR.mobs[npcId]
    if not mob then
        mob = { npcId = npcId, name = name, kills = 0, loots = 0, copper = 0, items = {} }
        ZTR.mobs[npcId] = mob
    end
    if name and name ~= "" then mob.name = name end
    return mob
end

local function AddTo(ledger, zone, itemId, count)
    ledger[zone] = ledger[zone] or {}
    local row = ledger[zone]
    row[itemId] = (row[itemId] or 0) + count
end

---One kill, from the loot feed.  It is the denominator of the session's
---gold-per-kill and nothing else.
---@param kill table { npcId, name, guid, at }
function ZTR:RecordKill(kill)
    if type(kill) ~= "table" then return end
    local mob = Mob(kill.npcId, kill.name)
    -- the observer already skips restricted values; the guard here is what
    -- keeps this addon from being the one that raises
    if not mob then return end
    mob.kills = mob.kills + 1
end

---Puts one corpse's items into the ledgers.
---
---The items go into both ledgers -- account-wide and this character's --
---because the drop happened to this character, exactly as cash does.  The
---zone is the one the player is in when the corpse is looted, which is what
---the timer has been running on.
---@param npcId number? the mob, when the source knows it
---@param name string?
---@param items table { [itemId] = count }
---@param copper number? loot-window money, kept per mob only
---@return boolean changed
function ZTR:RecordDrop(npcId, name, items, copper)
    local zone = GetRealZoneText and GetRealZoneText() or nil
    if not zone or zone == "" or self:IsSecret(zone) then return false end

    local changed = false
    for rawId, rawCount in pairs(items or {}) do
        local itemId = tonumber(rawId)
        local count = tonumber(rawCount)
        if itemId and itemId > 0 and count and count > 0 then
            AddTo(ZoneLootDB, zone, itemId, count)
            AddTo(ZoneTimerCharDB.loot, zone, itemId, count)
            changed = true
        end
    end

    local mob = Mob(npcId, name)
    if mob then
        mob.loots = mob.loots + 1
        mob.copper = mob.copper + (tonumber(copper) or 0)
        for rawId, rawCount in pairs(items or {}) do
            local itemId = tonumber(rawId)
            local count = tonumber(rawCount)
            if itemId and itemId > 0 and count and count > 0 then
                mob.items[itemId] = (mob.items[itemId] or 0) + count
            end
        end
    end

    if changed then
        -- the valuation cache is keyed on this
        self:TouchLedger()
    end
    return changed
end

---One looted corpse, from DFA_AuctionIntelligence's feed.
---
---Deduplicated against this file's own capture: whichever path sees the
---corpse first records it, the other one skips.
---@param drop table { npcId, name, guid, copper, items, at }
function ZTR:RecordLoot(drop)
    if type(drop) ~= "table" then return end
    if Seen(drop.guid) then return end
    Mark(drop.guid)
    self:RecordDrop(drop.npcId or NpcIdFromGUID(drop.guid), drop.name,
        drop.items, drop.copper)
end

-- Capturing the loot window ------------------------------------------------
--
-- The same three facts DFA_AuctionIntelligence reads: which creature the slot
-- belongs to, what the slot is, and how many.  Anything the client marks
-- "secret" is read as unknown rather than compared, because comparing a
-- secret value raises.

local SLOT_MONEY = 1

---The loot window's contents, grouped by the creature each slot came from.
function ZTR:OnLootOpened()
    local total = (GetNumLootItems and GetNumLootItems()) or 0
    local loot = nil
    for slot = 1, total do
        local guid = GetLootSourceInfo and GetLootSourceInfo(slot) or nil
        if self:IsSecret(guid) then guid = nil end
        if guid then
            loot = loot or {}
            local entry = loot[guid]
            if not entry then
                entry = { items = {}, copper = 0 }
                loot[guid] = entry
            end

            local slotType = GetLootSlotType and GetLootSlotType(slot) or nil
            local link = GetLootSlotLink and GetLootSlotLink(slot) or nil
            if self:IsSecret(slotType) then slotType = nil end
            if self:IsSecret(link) then link = nil end
            local _, _, quantity = GetLootSlotInfo(slot)
            if self:IsSecret(quantity) then quantity = 0 end
            quantity = tonumber(quantity) or 0

            if quantity > 0 then
                if slotType == SLOT_MONEY or (slotType == nil and not link) then
                    entry.copper = entry.copper + quantity
                else
                    local itemId = link and tonumber(link:match("item:(%d+)"))
                    if itemId then
                        entry.items[itemId] = (entry.items[itemId] or 0) + quantity
                    end
                end
            end
        end
    end
    pendingLoot = loot
end

---The loot window closed: commit what it showed, once per corpse.
function ZTR:OnLootClosed()
    local loot = pendingLoot
    pendingLoot = nil
    if not loot then return end
    for guid, drop in pairs(loot) do
        if not Seen(guid) then
            Mark(guid)
            self:RecordDrop(NpcIdFromGUID(guid), nil, drop.items, drop.copper)
        end
    end
end

---Subscribe to DFA_AuctionIntelligence's loot feed, if it is loaded.
---
---ZTR loads after it (both alphabetical order and OptionalDeps), but a
---disabled-then-enabled addon or a different client can load it later, so
---this is retried on ADDON_LOADED and PLAYER_LOGIN.  The flag is what keeps
---a second subscription from counting the same corpse twice.
---@return boolean
function ZTR:SubscribeLootFeed()
    if subscribed then return true end
    local kills = _G.DFA_AI and _G.DFA_AI.Kills
    if not kills or type(kills.RegisterLootListener) ~= "function" then
        return false
    end
    kills:RegisterLootListener(function(drop) ZTR:RecordLoot(drop) end)
    if type(kills.RegisterKillListener) == "function" then
        kills:RegisterKillListener(function(kill) ZTR:RecordKill(kill) end)
    end
    subscribed = true
    return true
end

---Whether the feed is subscribed and how much has been captured.
---@return boolean subscribed, number corpses, number zones
function ZTR:LootCaptureState()
    local zones, corpses = 0, 0
    for _ in pairs(ZoneLootDB) do zones = zones + 1 end
    for _ in pairs(recent) do corpses = corpses + 1 end
    return subscribed, corpses, zones
end

---One report for `/zt loot`: is the capture alive, and what has it seen?
---
---The client's own answers are printed on purpose.  When loot stops
---registering the question is always the same -- did the event fire, and did
---the window report a source -- and these four lines answer it without
---guessing.
function ZTR:PrintLootReport()
    local subscribed, corpses, zones = self:LootCaptureState()
    print("|cff33ff99ZoneTimerRedux|r loot -- "
        .. (subscribed and "feed subscribed" or "feed not subscribed (own capture only)"))
    print(("  dedupe window: %d corpse(s) in the last %ds, %d zone(s) in the ledger")
        :format(corpses, RECENT_SECONDS, zones))

    local total = (GetNumLootItems and GetNumLootItems()) or 0
    print(("  loot window right now: %s slot(s); GetLootSourceInfo %s; GetLootSlotLink %s")
        :format(tostring(total),
            GetLootSourceInfo and "present" or "|cffff0000MISSING|r",
            GetLootSlotLink and "present" or "|cffff0000MISSING|r"))

    local zone = GetRealZoneText and GetRealZoneText() or nil
    local ledger = self:GetLootLedger(self.charView)
    local row = zone and ledger[zone] or nil
    if row then
        local count, kinds = 0, 0
        for _, n in pairs(row) do
            count = count + (tonumber(n) or 0)
            kinds = kinds + 1
        end
        print(("  this zone (%s): %d item(s) across %d kind(s)")
            :format(zone, count, kinds))
    else
        print("  this zone: nothing looted yet")
    end

    -- the busiest few zones, so a ledger that is filling up is visible
    local rows = {}
    for name, items in pairs(ledger) do
        local count = 0
        for _, n in pairs(items) do count = count + (tonumber(n) or 0) end
        rows[#rows + 1] = { zone = name, count = count }
    end
    table.sort(rows, function(a, b) return a.count > b.count end)
    for i = 1, math.min(#rows, 5) do
        print(("  %-28s %d item(s)"):format(rows[i].zone, rows[i].count))
    end
end

local feedFrame = CreateFrame("Frame")
feedFrame:RegisterEvent("ADDON_LOADED")
feedFrame:RegisterEvent("PLAYER_LOGIN")
feedFrame:RegisterEvent("LOOT_OPENED")
feedFrame:RegisterEvent("LOOT_CLOSED")
feedFrame:SetScript("OnEvent", function(_, event, loadedName)
    if event == "LOOT_OPENED" then
        pcall(function() ZTR:OnLootOpened() end)
        return
    end
    if event == "LOOT_CLOSED" then
        pcall(function() ZTR:OnLootClosed() end)
        return
    end
    if event == "ADDON_LOADED" and loadedName ~= "DFA_AuctionIntelligence" then
        return
    end
    ZTR:SubscribeLootFeed()
end)
