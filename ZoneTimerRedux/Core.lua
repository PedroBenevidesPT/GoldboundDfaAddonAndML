-- ZoneTimerRedux/Core.lua

ZoneTimerRedux = {}
ZoneTimerRedux.VERSION  = "1.0"
-- tally sort: "zone", "time", "gold", or nil for the default order
ZoneTimerRedux.sortMode      = "time"
ZoneTimerRedux.sortAscending = false
ZoneTimerRedux.DEBUG    = AlniDev and AlniDev.debug.ZoneTimerRedux or false

-- Runtime state (not persisted) -----------------------------------------------
ZoneTimerRedux.currentZone = nil
ZoneTimerRedux.enteredTime = nil
ZoneTimerRedux.isPaused    = false

-- Debug helper ----------------------------------------------------------------

local function DebugPrint(...)
    if not ZoneTimerRedux.DEBUG then return end
    print("|cff33ff99ZoneTimerRedux:|r", ...)
end

-- SavedVariable placeholder (file-load-time only) -----------------------------
-- SavedVariables are NOT loaded yet when addon files execute; WoW makes them
-- available only after ADDON_LOADED fires.  This placeholder lets UI.lua create
-- its frames without crashing.  The authoritative initialization happens in the
-- ADDON_LOADED handler below, once the real saved data is accessible.
ZoneTimerSettings                      = ZoneTimerSettings or {}
ZoneTimerSettings.times                = ZoneTimerSettings.times or {}
ZoneTimerSettings.announcedMilestones  = ZoneTimerSettings.announcedMilestones or {}
ZoneTimerSettings.width                = ZoneTimerSettings.width   or 220
ZoneTimerSettings.opacity              = ZoneTimerSettings.opacity or 1.0
ZoneTimerSettings.fontSize             = ZoneTimerSettings.fontSize or 12
if ZoneTimerSettings.trackGold         == nil then ZoneTimerSettings.trackGold         = true  end
if ZoneTimerSettings.showAlerts        == nil then ZoneTimerSettings.showAlerts        = true  end
if ZoneTimerSettings.tallySort         == nil then ZoneTimerSettings.tallySort         = "time" end
if ZoneTimerSettings.goldenTheme       == nil then ZoneTimerSettings.goldenTheme       = true  end
-- the window theme used to be the goldenTheme checkbox; carry it over once
if ZoneTimerSettings.theme == nil then
    ZoneTimerSettings.theme = ZoneTimerSettings.goldenTheme ~= false and "gold" or "standard"
end
if ZoneTimerSettings.showLabels        == nil then ZoneTimerSettings.showLabels        = true  end
if ZoneTimerSettings.globalDataAdopted == nil then ZoneTimerSettings.globalDataAdopted = false end
if ZoneTimerSettings.windowVisible     == nil then ZoneTimerSettings.windowVisible     = true  end
if ZoneTimerSettings.mainPanelCharView == nil then ZoneTimerSettings.mainPanelCharView = false end

ZoneTimerRedux.charView = true  -- tally shows per-character data by default

ZoneGoldDB            = ZoneGoldDB or {}
--: item counts per zone for loot valued by the pricing engine (Loot.lua)
ZoneLootDB            = ZoneLootDB or {}
ZoneTimerCharDB                      = ZoneTimerCharDB or {}
ZoneTimerCharDB.times                = ZoneTimerCharDB.times or {}
ZoneTimerCharDB.gold                 = ZoneTimerCharDB.gold  or {}
ZoneTimerCharDB.loot                 = ZoneTimerCharDB.loot  or {}
ZoneTimerCharDB.announcedMilestones  = ZoneTimerCharDB.announcedMilestones or {}

--: bumped whenever the ledgers change (loot or cash), so the valuation
--: cache knows to recompute instead of answering from the old numbers
ZoneTimerRedux.ledgerRevision = 0

function ZoneTimerRedux:TouchLedger()
    self.ledgerRevision = (self.ledgerRevision or 0) + 1
end

---Whether the client refuses to let addons read this value.
---
---Newer clients mark some data "secret": it looks like a normal string or
---number, but matching, comparing or doing arithmetic on it raises.  The
---timer only displays data, so a secret is treated as unknown instead.
function ZoneTimerRedux:IsSecret(value)
    return type(issecretvalue) == "function" and issecretvalue(value) == true
end

-- ADDON_LOADED: authoritative init on the real SavedVariables ------------------
-- After this event ZoneTimerSettings is the real saved table (WoW replaced the
-- placeholder above with data loaded from disk).  Re-applying defaults here
-- fills in any keys added in newer addon versions for users with older save files.
local _coreInitFrame = CreateFrame("Frame")
_coreInitFrame:RegisterEvent("ADDON_LOADED")
_coreInitFrame:SetScript("OnEvent", function(self, event, addonName)
    if addonName ~= "ZoneTimerRedux" then return end
    self:UnregisterAllEvents()

    ZoneTimerSettings.times                = ZoneTimerSettings.times or {}
    ZoneTimerSettings.announcedMilestones  = ZoneTimerSettings.announcedMilestones or {}
    ZoneTimerSettings.width                = ZoneTimerSettings.width   or 220
    ZoneTimerSettings.opacity              = ZoneTimerSettings.opacity or 1.0
    ZoneTimerSettings.fontSize             = ZoneTimerSettings.fontSize or 12
    if ZoneTimerSettings.trackGold         == nil then ZoneTimerSettings.trackGold         = true  end
    if ZoneTimerSettings.showAlerts        == nil then ZoneTimerSettings.showAlerts        = true  end
    if ZoneTimerSettings.tallySort         == nil then ZoneTimerSettings.tallySort         = "time" end
    if ZoneTimerSettings.goldenTheme       == nil then ZoneTimerSettings.goldenTheme       = true  end
    if ZoneTimerSettings.theme == nil then
        ZoneTimerSettings.theme = ZoneTimerSettings.goldenTheme ~= false and "gold" or "standard"
    end
    if ZoneTimerSettings.showLabels        == nil then ZoneTimerSettings.showLabels        = true  end
    if ZoneTimerSettings.globalDataAdopted == nil then ZoneTimerSettings.globalDataAdopted = false end
    if ZoneTimerSettings.windowVisible     == nil then ZoneTimerSettings.windowVisible     = true  end
    if ZoneTimerSettings.mainPanelCharView == nil then ZoneTimerSettings.mainPanelCharView = false end

    -- "none" saves the unsorted state; tallySort itself defaults to "time"
    ZoneTimerRedux.sortMode      = ZoneTimerSettings.tallySort ~= "none" and ZoneTimerSettings.tallySort or nil
    ZoneTimerRedux.sortAscending = ZoneTimerSettings.tallySortAscending == true

    ZoneGoldDB                           = ZoneGoldDB or {}
    ZoneLootDB                           = ZoneLootDB or {}
    ZoneTimerCharDB                      = ZoneTimerCharDB or {}
    ZoneTimerCharDB.times                = ZoneTimerCharDB.times or {}
    ZoneTimerCharDB.gold                 = ZoneTimerCharDB.gold  or {}
    ZoneTimerCharDB.loot                 = ZoneTimerCharDB.loot  or {}
    ZoneTimerCharDB.announcedMilestones  = ZoneTimerCharDB.announcedMilestones or {}
end)

-- Formatting -------------------------------------------------------------------

function ZoneTimerRedux:FormatTime(seconds)
    seconds = tonumber(seconds) or 0
    local h = math.floor(seconds / 3600)
    local m = math.floor((seconds % 3600) / 60)
    local s = seconds % 60
    if h > 0 then return string.format("%dh %dm %ds", h, m, s) end
    if m > 0 then return string.format("%dm %ds", m, s) end
    return string.format("%ds", s)
end

function ZoneTimerRedux:ColorTime(text)
    local h, m, s = text:match("(%d+)h (%d+)m (%d+)s")
    if h then
        return string.format("|cffffd200%sh|r |cffc7c7cf%sm|r |cff9d9d9d%ss|r", h, m, s)
    end
    local m2, s2 = text:match("(%d+)m (%d+)s")
    if m2 then
        return string.format("|cffc7c7cf%sm|r |cff9d9d9d%ss|r", m2, s2)
    end
    return text
end

function ZoneTimerRedux:FormatGold(copper)
    copper = tonumber(copper) or 0
    local g = math.floor(copper / 10000)
    local s = math.floor((copper % 10000) / 100)
    local c = copper % 100
    return string.format("%dg %ds %dc", g, s, c)
end

function ZoneTimerRedux:ColorGold(text)
    local g, s, c = text:match("(%d+)g (%d+)s (%d+)c")
    if not g then return text end
    return string.format("|cffFFD700%sg|r |cffffffff%ss|r |cffff7f00%sc|r", g, s, c)
end

-- Data accessors ---------------------------------------------------------------

function ZoneTimerRedux:GetActiveTimes()
    return self.charView and ZoneTimerCharDB.times or ZoneTimerSettings.times
end

function ZoneTimerRedux:GetActiveGoldDB()
    return self.charView and ZoneTimerCharDB.gold or ZoneGoldDB
end

-- Cash actually picked up: money the client announced, not a valuation.
function ZoneTimerRedux:GetZoneCash(zone, charView)
    local ledger = charView and ZoneTimerCharDB.gold or ZoneGoldDB
    return ledger[zone] or 0
end

function ZoneTimerRedux:GetZoneGold(zone)
    return self:GetZoneCash(zone, ZoneTimerSettings.mainPanelCharView == true)
end

-- Seconds spent in a zone, including the part of the current visit that is
-- not saved yet.  charView selects the same ledger the tally is showing.
function ZoneTimerRedux:GetZoneTime(zone, charView)
    if not zone then return 0 end
    local times = charView and ZoneTimerCharDB.times or ZoneTimerSettings.times
    local saved = times[zone] or 0
    if zone == self.currentZone and not self.isPaused and self.enteredTime then
        return saved + (time() - self.enteredTime)
    end
    return saved
end

function ZoneTimerRedux:GetCurrentTime()
    if not self.currentZone then return 0 end
    return self:GetZoneTime(self.currentZone, ZoneTimerSettings.mainPanelCharView == true)
end

function ZoneTimerRedux:GetSortedZones()
    local times  = self:GetActiveTimes()
    local goldDB = self:GetActiveGoldDB()
    local list   = {}
    local liveElapsed = (self.currentZone and not self.isPaused and self.enteredTime)
        and (time() - self.enteredTime) or nil
    local sawCurrentZone = false

    for zone, t in pairs(times) do
        if liveElapsed and zone == self.currentZone then
            t = t + liveElapsed
            sawCurrentZone = true
        end
        table.insert(list, { zone = zone, time = t, gold = goldDB[zone] or 0 })
    end

    -- currentZone may have no saved time yet (freshly entered zone), so it
    -- wouldn't otherwise appear in the list until the timer is saved.
    if liveElapsed and not sawCurrentZone then
        table.insert(list, { zone = self.currentZone, time = liveElapsed, gold = goldDB[self.currentZone] or 0 })
    end

    -- A zone can carry loot without a saved time -- entered and left before
    -- the timer ticked, or a ledger restored from an older save -- and the
    -- row list comes from the times table alone, so its loot would never be
    -- shown anywhere.  Anything the loot ledger knows about gets a row.
    local ledger = self.GetLootLedger and self:GetLootLedger(self.charView) or {}
    local listed = {}
    for _, row in ipairs(list) do listed[row.zone] = true end
    for zone in pairs(ledger) do
        if not listed[zone] then
            table.insert(list, { zone = zone, time = times[zone] or 0,
                                 gold = goldDB[zone] or 0 })
        end
    end

    local key, ascending = self.sortMode, self.sortAscending
    table.sort(list, function(a, b)
        -- default order: most time first
        if key == nil then return a.time > b.time end
        local x, y = a[key], b[key]
        if key == "zone" then x, y = x:lower(), y:lower() end
        if x == y then return a.zone < b.zone end
        if ascending then return x < y end
        return x > y
    end)
    return list
end


-- forward declaration - defined in the Milestones section below
local ActiveMilestones

-- Zone lifecycle ---------------------------------------------------------------

function ZoneTimerRedux:SaveCurrentZone()
    if self.currentZone and self.enteredTime and not self.isPaused then
        local elapsed = time() - self.enteredTime
        ZoneTimerSettings.times[self.currentZone] =
            (ZoneTimerSettings.times[self.currentZone] or 0) + elapsed
        ZoneTimerCharDB.times[self.currentZone] =
            (ZoneTimerCharDB.times[self.currentZone] or 0) + elapsed
        DebugPrint(string.format("Saved %q: +%ds (total %ds, char %ds)",
            self.currentZone, elapsed,
            ZoneTimerSettings.times[self.currentZone],
            ZoneTimerCharDB.times[self.currentZone]))
    end
end

function ZoneTimerRedux:EnterZone(zone)
    if not zone or zone == "" then return end
    self:SaveCurrentZone()
    self.currentZone = zone
    self.enteredTime = self.isPaused and nil or time()
    DebugPrint(string.format("Entered zone: %q (paused: %s)", tostring(zone), tostring(self.isPaused)))

    local ann = ActiveMilestones()
    local times = ZoneTimerSettings.mainPanelCharView and ZoneTimerCharDB.times or ZoneTimerSettings.times
    local isNew = not (times[zone] and times[zone] > 0)
               and not (ann[zone] and ann[zone]["discovered"])
    if isNew and ZoneTimerSettings.showAlerts then
        ann[zone] = ann[zone] or {}
        ann[zone]["discovered"] = true
        DebugPrint(string.format("New zone discovered: %q", zone))
        if ZoneTimerRedux_ShowDiscoveredAlert then
            ZoneTimerRedux_ShowDiscoveredAlert(zone)
        end
    end
end

function ZoneTimerRedux:Pause()
    self:SaveCurrentZone()
    self.isPaused    = true
    self.enteredTime = nil
    DebugPrint("Timer paused.")
end

function ZoneTimerRedux:Resume()
    self.isPaused    = false
    self.enteredTime = time()
    DebugPrint("Timer resumed.")
end

-- Milestones -------------------------------------------------------------------

ActiveMilestones = function()
    return ZoneTimerSettings.mainPanelCharView
        and ZoneTimerCharDB.announcedMilestones
        or  ZoneTimerSettings.announcedMilestones
end

-- 30m, 1h, 2h, 3h, 5h, then every 10h up to 200h
do
    local m = { 30, 60, 120, 180, 300 }
    for i = 1, 20 do table.insert(m, i * 600) end
    ZoneTimerRedux.MILESTONES = m
end

function ZoneTimerRedux:CheckMilestones(zone, totalSeconds)
    if not zone then return end
    local ann = ActiveMilestones()
    ann[zone] = ann[zone] or {}
    local minutes = math.floor(totalSeconds / 60)
    for _, m in ipairs(self.MILESTONES) do
        if minutes >= m and not ann[zone][m] then
            ann[zone][m] = true
            DebugPrint(string.format("Milestone %dm reached in %q (alerts: %s)", m, zone, tostring(ZoneTimerSettings.showAlerts)))
            if ZoneTimerSettings.showAlerts then
                ZoneTimerRedux_ShowMilestoneAlert(zone, m)
            end
        end
    end
end

function ZoneTimerRedux:CheckGoldMilestones(zone, totalCopper)
    if not zone then return end
    local ann = ActiveMilestones()
    ann[zone] = ann[zone] or {}
    local goldK = math.floor(totalCopper / 1000000)  -- 1,000g = 1,000,000 copper
    for k = 1, goldK do
        local key = "g" .. k
        if not ann[zone][key] then
            ann[zone][key] = true
            DebugPrint(string.format("Gold milestone %dk reached in %q (alerts: %s)", k, zone, tostring(ZoneTimerSettings.showAlerts)))
            if ZoneTimerSettings.showAlerts then
                ZoneTimerRedux_ShowGoldMilestoneAlert(zone, k * 1000)
            end
        end
    end
end

function ZoneTimerRedux:AdoptGlobalData()
    if ZoneTimerSettings.globalDataAdopted then return end
    for zone, globalTime in pairs(ZoneTimerSettings.times) do
        ZoneTimerCharDB.times[zone] = globalTime
    end
    for zone, globalGold in pairs(ZoneGoldDB) do
        ZoneTimerCharDB.gold[zone] = globalGold
    end
    ZoneTimerSettings.globalDataAdopted = true
    self:TouchLedger()
    DebugPrint("Global data adopted.")
end

-- Gold tracking ----------------------------------------------------------------

local goldFrame = CreateFrame("Frame")
goldFrame:RegisterEvent("CHAT_MSG_MONEY")
goldFrame:SetScript("OnEvent", function(_, _, message)
    local zone = GetRealZoneText()
    if not zone then return end
    -- a restricted message cannot be matched; counting nothing is better
    -- than raising inside the client's chat event
    if ZoneTimerRedux:IsSecret(message) or ZoneTimerRedux:IsSecret(zone) then return end

    local locale    = GetLocale()
    local goldPat   = locale == "ptBR" and "(%d+)[^%d]*[Oo]uro"  or "(%d+)[^%d]*[Gg]old"
    local silverPat = locale == "ptBR" and "(%d+)[^%d]*[Pp]rata" or "(%d+)[^%d]*[Ss]ilver"
    local copperPat = locale == "ptBR" and "(%d+)[^%d]*[Cc]obre" or "(%d+)[^%d]*[Cc]opper"

    local g = tonumber(string.match(message, goldPat))   or 0
    local s = tonumber(string.match(message, silverPat)) or 0
    local c = tonumber(string.match(message, copperPat)) or 0

    local earned = g * 10000 + s * 100 + c
    ZoneGoldDB[zone]          = (ZoneGoldDB[zone] or 0) + earned
    ZoneTimerCharDB.gold[zone] = (ZoneTimerCharDB.gold[zone] or 0) + earned
    -- the valuation caches cash with the loot: this is a change to it
    ZoneTimerRedux:TouchLedger()
    DebugPrint(string.format("Gold in %q: +%dc (total %dc, char %dc)",
        zone, earned, ZoneGoldDB[zone], ZoneTimerCharDB.gold[zone]))
end)
