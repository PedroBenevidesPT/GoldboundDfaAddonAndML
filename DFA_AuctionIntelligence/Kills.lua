-- DFA Auction Intelligence - kills, loot and copper per mob
--
-- What is recorded, and why it is counted this way:
--
-- * a **kill** is a creature this player (or their pet) damaged and which
--   then died.  MobInfo2 tracks the same pair -- "attacked" and "died" can
--   arrive in either order -- because UNIT_DIED and PARTY_KILL both report
--   one death.  The GUID is remembered until the fight ends, so the second
--   signal is not a second kill.
-- * **loot** is the corpse's loot window, read once per corpse GUID.  A
--   corpse reopened for leftovers is the same corpse, not a second drop,
--   and the rolling list of looted GUIDs is persisted, so a /reload does
--   not count it again either.
-- * **copper** is the money slots of that window, kept apart from the item
--   counts so a valuation can price the items without it and add it back.
--
-- Nothing here prices anything.  The value of a drop is the backend's
-- question, and it owns the price sources; this file only records what
-- happened in the game.
local _, addon = ...

DFA_AI = DFA_AI or {}
DFA_AI.Kills = DFA_AI.Kills or {}
local Kills = DFA_AI.Kills

--: how many flushed batches stay in SavedVariables.  A batch is cleared from
--: the pending table when it is flushed, so this is the window in which the
--: daemon can still import it (it deduplicates by batch and NPC).
local MAX_BATCHES = 20
--: corpses remembered across reloads, so a reopened one is not counted twice
local MAX_CORPSES = 200
--: loot slot types (Enum.LootSlotType where the client exposes it)
local SLOT_MONEY = 2

local playerGUID, petGUID
local ours, dead, recorded = {}, {}, {}
local sessionStart = nil
local pendingLoot = nil

-- Listeners see what was committed, once per corpse and once per kill.
-- They run *after* the counters are stored, inside a pcall: a listener from
-- another addon must never be able to cost the observation, and a broken
-- one must not take the loot window down with it.  The announcement carries
-- copies of the loot, so a listener cannot edit our counters.
local lootListeners, killListeners = {}, {}
--: how many listener errors are remembered for /dfaai status
local MAX_LISTENER_ERRORS = 20

local function Remember(errorMessage)
    DFA_AI.listenerErrors = DFA_AI.listenerErrors or {}
    local errors = DFA_AI.listenerErrors
    errors[#errors + 1] = tostring(errorMessage)
    while #errors > MAX_LISTENER_ERRORS do
        table.remove(errors, 1)
    end
end

local function Announce(listeners, payload)
    if #listeners == 0 then return end
    for _, listener in ipairs(listeners) do
        local ok, reason = pcall(listener, payload)
        if not ok then Remember(reason) end
    end
end

---Subscribe to committed loot.  Returns false when the argument is not a
---function; subscribing the same function twice is a no-op.
function Kills:RegisterLootListener(listener)
    if type(listener) ~= "function" then return false end
    for _, existing in ipairs(lootListeners) do
        if existing == listener then return true end
    end
    lootListeners[#lootListeners + 1] = listener
    return true
end

---Subscribe to committed kills (same contract as RegisterLootListener).
function Kills:RegisterKillListener(listener)
    if type(listener) ~= "function" then return false end
    for _, existing in ipairs(killListeners) do
        if existing == listener then return true end
    end
    killListeners[#killListeners + 1] = listener
    return true
end

local function Now()
    return time and time() or 0
end

-- "Creature-0-1465-2552-1234-224654-00004A2B1C" -> 224654
local function NpcIdFromGUID(guid)
    -- a secret GUID cannot be matched: the client is keeping the unit to
    -- itself, and a string operation on it would raise inside the loot path
    if type(guid) ~= "string" or DFA_AI.IsSecret(guid) then return nil end
    local kind = guid:match("^([%a]+)%-")
    if kind ~= "Creature" then return nil end
    local fields = {}
    for field in guid:gmatch("[^-]+") do
        fields[#fields + 1] = field
        if #fields > 7 then break end
    end
    return tonumber(fields[6])
end

local function Store()
    DFA_AUCTION_INTELLIGENCE_DB = DFA_AUCTION_INTELLIGENCE_DB or {}
    local db = DFA_AUCTION_INTELLIGENCE_DB
    db.kills = db.kills or {}
    local store = db.kills
    store.pending = store.pending or {}
    store.batches = store.batches or {}
    store.order = store.order or {}
    store.corpses = store.corpses or {}
    store.corpse_order = store.corpse_order or {}
    store.stats = store.stats or {}
    local stats = store.stats
    stats.kills = stats.kills or 0
    stats.loots = stats.loots or 0
    stats.copper = stats.copper or 0
    stats.batches = stats.batches or 0
    return store
end

local function Entry(store, npcId, name)
    local entry = store.pending[npcId]
    if not entry then
        entry = {
            npcId = npcId, name = name or ("NPC " .. tostring(npcId)),
            kills = 0, loots = 0, empty = 0, copper = 0, items = {},
            firstAt = Now(), lastAt = Now(),
        }
        store.pending[npcId] = entry
    end
    return entry
end

function Kills:Initialize()
    playerGUID = UnitGUID and UnitGUID("player") or nil
    petGUID = UnitGUID and UnitGUID("pet") or nil
    if DFA_AI.IsSecret(playerGUID) then playerGUID = nil end
    if DFA_AI.IsSecret(petGUID) then petGUID = nil end
    sessionStart = Now()
    ours, dead, recorded = {}, {}, {}
    Store()
end

function Kills:OnCombatEnd()
    ours, dead, recorded = {}, {}, {}
end

function Kills:RecordKill(guid, name)
    local npcId = NpcIdFromGUID(guid)
    if not npcId or recorded[guid] then return false end
    if DFA_AI.IsSecret(name) then name = nil end
    recorded[guid] = true
    local store = Store()
    local entry = Entry(store, npcId, name)
    entry.kills = entry.kills + 1
    entry.name = name or entry.name
    entry.lastAt = Now()
    store.stats.kills = store.stats.kills + 1
    Announce(killListeners, {
        npcId = npcId, name = entry.name, guid = guid, at = entry.lastAt,
    })
    return true
end

-- A death is written only once both facts are known: this unit was hit by
-- us (or the pet), and this unit died.  Either can be learned first.
local function TryRecord(guid)
    if not guid or not ours[guid] or not dead[guid] then return end
    Kills:RecordKill(guid)
    ours[guid] = nil
    dead[guid] = nil
end

local function IsOurs(guid)
    if not guid or DFA_AI.IsSecret(guid) then return false end
    if guid == playerGUID then return true end
    petGUID = petGUID or (UnitGUID and UnitGUID("pet") or nil)
    if DFA_AI.IsSecret(petGUID) then petGUID = nil end
    return petGUID ~= nil and guid == petGUID
end

function Kills:OnCombatLog()
    if not CombatLogGetCurrentEventInfo then return end
    local info = { CombatLogGetCurrentEventInfo() }
    local subevent = info[2]
    local sourceGUID = info[4]
    local destGUID = info[8]
    -- combat log fields are restricted in this client; a secret value cannot
    -- even be compared, so an event we are not allowed to read is skipped
    if not subevent or not destGUID then return end
    if DFA_AI.IsSecret(subevent) or DFA_AI.IsSecret(destGUID)
            or DFA_AI.IsSecret(sourceGUID) then return end

    -- DAMAGE / SPELL_DAMAGE / SWING_DAMAGE / SPELL_PERIODIC_DAMAGE ...
    if subevent:match("DAMAGE$") and not subevent:match("_MISSED") then
        if IsOurs(sourceGUID) then ours[destGUID] = true end
        return
    end
    if subevent == "PARTY_KILL" then
        if IsOurs(sourceGUID) then ours[destGUID] = true end
        dead[destGUID] = true
        TryRecord(destGUID)
        return
    end
    if subevent == "UNIT_DIED" then
        dead[destGUID] = true
        TryRecord(destGUID)
    end
end

function Kills:RememberCorpse(store, guid)
    if not guid or store.corpses[guid] then return end
    store.corpses[guid] = true
    store.corpse_order[#store.corpse_order + 1] = guid
    while #store.corpse_order > MAX_CORPSES do
        local oldest = table.remove(store.corpse_order, 1)
        store.corpses[oldest] = nil
    end
end

function Kills:OnLootOpened()
    local total = (GetNumLootItems and GetNumLootItems()) or 0
    local loot = nil
    for slot = 1, total do
        local guid = GetLootSourceInfo and GetLootSourceInfo(slot) or nil
        local npcId = NpcIdFromGUID(guid)
        if npcId then
            loot = loot or {}
            local entry = loot[guid]
            if not entry then
                entry = { npcId = npcId, items = {}, copper = 0, slots = 0 }
                loot[guid] = entry
            end
            local slotType = GetLootSlotType and GetLootSlotType(slot) or nil
            local link = GetLootSlotLink and GetLootSlotLink(slot) or nil
            local _, _, quantity = GetLootSlotInfo(slot)
            -- the loot window can carry secret values: read them as "no
            -- slot" rather than match or compare a value Blizzard hid
            if DFA_AI.IsSecret(slotType) then slotType = nil end
            if DFA_AI.IsSecret(link) then link = nil end
            if DFA_AI.IsSecret(quantity) then quantity = 0 end
            quantity = tonumber(quantity) or 0
            if slotType == SLOT_MONEY or (slotType == nil and not link) then
                if quantity > 0 then
                    entry.copper = entry.copper + quantity
                    entry.slots = entry.slots + 1
                end
            else
                local itemId = link and tonumber(link:match("item:(%d+)")) or nil
                if itemId and quantity > 0 then
                    entry.items[itemId] = (entry.items[itemId] or 0) + quantity
                    entry.slots = entry.slots + 1
                end
            end
        end
    end
    -- no loot window means no commit: LOOT_OPENED for a chest or a
    -- GameObject carries a GUID that is not a creature, so nothing is stored
    pendingLoot = loot
end

function Kills:OnLootClosed()
    local loot = pendingLoot
    pendingLoot = nil
    if not loot then return end
    local store = Store()
    for guid, drop in pairs(loot) do
        -- a corpse is counted once: reopening it for leftovers is the same
        -- drop, not a second one
        if not store.corpses[guid] then
            local entry = Entry(store, drop.npcId)
            if drop.slots > 0 then
                entry.loots = entry.loots + 1
                store.stats.loots = store.stats.loots + 1
            else
                entry.empty = entry.empty + 1
            end
            entry.copper = entry.copper + drop.copper
            store.stats.copper = store.stats.copper + drop.copper
            for itemId, quantity in pairs(drop.items) do
                entry.items[itemId] = (entry.items[itemId] or 0) + quantity
            end
            entry.lastAt = Now()
            self:RememberCorpse(store, guid)

            -- Announce the copy, not the stored tables: a listener that
            -- kept a reference would otherwise see our counters change
            -- under it.
            local items = {}
            for itemId, quantity in pairs(drop.items) do
                items[itemId] = quantity
            end
            Announce(lootListeners, {
                npcId = drop.npcId,
                name = entry.name,
                guid = guid,
                copper = drop.copper,
                items = items,
                at = entry.lastAt,
            })
        end
    end
end

local function BatchId(store, realm, character)
    local ordinal = (store.stats.batches or 0) + 1
    store.stats.batches = ordinal
    return string.format("%s:%s:%d:%d", tostring(character or "?"),
                         tostring(realm or "?"), sessionStart or Now(), ordinal)
end

---Turn the pending counters into one immutable batch and keep the newest
---few.  Returns the batch's rows; the caller exports every retained batch,
---so an import can happen late without losing anything.
function Kills:Flush()
    local store = Store()
    local rows = {}
    local pending = {}
    for _, entry in pairs(store.pending) do
        if entry.kills > 0 or entry.loots > 0 or entry.empty > 0
                or entry.copper > 0 or next(entry.items) then
            pending[#pending + 1] = entry
        end
    end
    if #pending == 0 then
        store.pending = {}
        return rows
    end
    local realm = GetRealmName and GetRealmName() or "UnknownRealm"
    local character = UnitName and UnitName("player") or "UnknownPlayer"
    local batchId = BatchId(store, realm, character)
    table.sort(pending, function(a, b) return a.npcId < b.npcId end)
    for _, entry in ipairs(pending) do
        local items = {}
        for itemId, quantity in pairs(entry.items) do
            if quantity > 0 then
                items[#items + 1] = { itemId = itemId, quantity = quantity }
            end
        end
        table.sort(items, function(a, b) return a.itemId < b.itemId end)
        rows[#rows + 1] = {
            batchId = batchId,
            realm = realm,
            character = character,
            npcId = entry.npcId,
            npcName = entry.name,
            kills = entry.kills,
            loots = entry.loots,
            empty = entry.empty,
            moneyCopper = entry.copper,
            firstAt = entry.firstAt,
            lastAt = entry.lastAt,
            items = items,
        }
    end
    store.batches[batchId] = { id = batchId, at = Now(), rows = rows }
    store.order[#store.order + 1] = batchId
    while #store.order > MAX_BATCHES do
        local oldest = table.remove(store.order, 1)
        store.batches[oldest] = nil
    end
    store.pending = {}
    return rows
end

---Every retained row, oldest batch first: what the export carries.
function Kills:Rows()
    local store = Store()
    local rows = {}
    for _, batchId in ipairs(store.order) do
        local batch = store.batches[batchId]
        for _, row in ipairs(batch and batch.rows or {}) do
            rows[#rows + 1] = row
        end
    end
    return rows
end

function Kills:GetStats()
    local store = Store()
    local pending, npcs = 0, 0
    for _, entry in pairs(store.pending) do
        pending = pending + entry.kills
        npcs = npcs + 1
    end
    return {
        kills = store.stats.kills or 0,
        loots = store.stats.loots or 0,
        copper = store.stats.copper or 0,
        batches = #store.order,
        pending_kills = pending,
        pending_npcs = npcs,
    }
end
