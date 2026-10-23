-- ZoneTimerRedux/Loot.lua
--
-- The loot ledger: how many of each item, per zone, so Pricing.lua can value
-- it.
--
-- Loot is observed exactly once, by DFA_AuctionIntelligence: it reads the
-- corpse's slots, attributes them to the mob, deduplicates a reopened
-- corpse, and persists the observation the backend imports for the farm
-- models.  This file subscribes to that feed instead of opening a second
-- one; two capture paths would disagree about a corpse and the totals would
-- stop matching the observations the ML sees.
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

local subscribed = false

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

---One looted corpse, from the loot feed.
---
---The items go into both ledgers -- account-wide and this character's --
---because the drop happened to this character, exactly as cash does.  The
---zone is the one the player is in when the corpse is looted, which is what
---the timer has been running on.
---@param drop table { npcId, name, guid, copper, items, at }
function ZTR:RecordLoot(drop)
    if type(drop) ~= "table" then return end
    local zone = GetRealZoneText and GetRealZoneText() or nil
    if not zone or zone == "" or self:IsSecret(zone) then return end

    local changed = false
    for rawId, rawCount in pairs(drop.items or {}) do
        local itemId = tonumber(rawId)
        local count = tonumber(rawCount)
        if itemId and itemId > 0 and count and count > 0 then
            AddTo(ZoneLootDB, zone, itemId, count)
            AddTo(ZoneTimerCharDB.loot, zone, itemId, count)
            changed = true
        end
    end

    local mob = Mob(drop.npcId, drop.name)
    if mob then
        mob.loots = mob.loots + 1
        mob.copper = mob.copper + (tonumber(drop.copper) or 0)
        for rawId, rawCount in pairs(drop.items or {}) do
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

local feedFrame = CreateFrame("Frame")
feedFrame:RegisterEvent("ADDON_LOADED")
feedFrame:RegisterEvent("PLAYER_LOGIN")
feedFrame:SetScript("OnEvent", function(_, event, loadedName)
    if event == "ADDON_LOADED" and loadedName ~= "DFA_AuctionIntelligence" then
        return
    end
    ZTR:SubscribeLootFeed()
end)
