-- ZoneTimerRedux/Pricing.lua
--
-- What the loot was worth, priced by the DfA pricing engine.
--
-- The prices are not computed here.  DFA_Estimate ships the dataset the
-- engine generated and publishes it as DFA_ESTIMATE_API; every number below
-- comes from that table, in the precedence the engine itself defines --
-- this realm's own sale record first, then the regional net price scaled by
-- the realm's measured factor.  ZoneTimerRedux re-deriving a price from
-- market references would be a second, disagreeing pricing system.
--
-- Two values are reported and are never added together:
--
--   vendor -- what a vendor pays, from the client's item table (the same
--             number the dataset ships).  Guaranteed money.
--   market -- what the auction house is expected to pay for a unit, net of
--             the 5 % cut.  An estimate: shown with a "~", never counted
--             as earned.
--
-- Cash the player picked up is neither, and stays in ZoneGoldDB.  An item
-- the dataset cannot price is *unpriced*: it is counted, and it adds
-- nothing, because a zero would be a price this addon made up.

local ZTR = ZoneTimerRedux

--: Used when the dataset predates the field; it is the engine's own
--: default (PricingConfig.max_age_hours, fourteen days).
local DEFAULT_MAX_AGE_HOURS = 24 * 14

--: MobInfo2 is probed for a mob id in bands, not level by level: its API
--: takes a level and combines the levels around it (combine = true).
local MOBINFO_MAX_LEVEL = 90
local MOBINFO_STEP = 9

local function API()
    return _G.DFA_ESTIMATE_API
end

---Whether the pricing engine has a dataset loaded at all.
---@return boolean
function ZTR:HasPrices()
    local api = API()
    return api ~= nil and api.data ~= nil
end

---The age of the pricing dataset and the point at which it is stale.
---@return table
function ZTR:GetPriceState()
    local api = API()
    if not api or not api.data then
        return {
            available = false,
            reason = "the DFA Estimate dataset is not loaded",
            maxAgeHours = DEFAULT_MAX_AGE_HOURS,
        }
    end
    local maxAge = DEFAULT_MAX_AGE_HOURS
    if api.GetMaxAgeHours then
        maxAge = tonumber(api.GetMaxAgeHours()) or maxAge
    end
    local age = api.GetAgeHours and tonumber(api.GetAgeHours()) or nil
    return {
        available = true,
        generatedAt = tonumber(api.generatedAt) or 0,
        version = api.dataVersion,
        ageHours = age,
        maxAgeHours = maxAge,
        stale = age ~= nil and age > maxAge,
    }
end

---Whether the evidence behind the prices is older than the engine allows.
---@return boolean
function ZTR:IsPriceStale()
    return self:GetPriceState().stale == true
end

---The client's own sell price for an item, in copper, or nil.
---
---This is the Blizzard item table, the same source the dataset's vendor
---column comes from, so the two can never disagree about what a vendor
---pays.  A client that has not cached the item yet returns nothing.
local function ClientVendorPrice(itemId)
    local get = GetItemInfo or (C_Item and C_Item.GetItemInfo)
    if not get or ZTR:IsSecret(itemId) then return nil end
    local ok, _, _, _, _, _, _, _, _, _, _, sell = pcall(get, itemId)
    -- the sell price can be a restricted value on this client: a value we
    -- may not read is unknown, never zero and never guessed
    if not ok or ZTR:IsSecret(sell) then return nil end
    local copper = tonumber(sell)
    if copper and copper > 0 then return copper end
    return nil
end

---One item, priced for a loot ledger.
---
---Returns nil when neither a vendor value nor a market estimate exists.
---@param itemId number|string
---@return table? { vendor, market, source, stale, ageHours, confidence }
function ZTR:PriceItem(itemId)
    local id = tonumber(itemId)
    if not id or id <= 0 then return nil end
    local api = API()
    local values = api and api.GetLootValues and api.GetLootValues(id) or nil
    local vendor = ClientVendorPrice(id) or (values and values.vendor) or nil
    local market = values and values.market or nil
    if not vendor and not market then return nil end
    return {
        vendor = vendor,
        market = market,
        source = (values and values.source) or (vendor and "client") or nil,
        stale = values and values.stale or false,
        ageHours = values and values.ageHours,
        confidence = values and values.confidence,
    }
end

---The loot ledger the tally is showing: account-wide or this character's.
---@param charView boolean
---@return table zone -> { [itemId] = count }
function ZTR:GetLootLedger(charView)
    if charView then
        ZoneTimerCharDB.loot = ZoneTimerCharDB.loot or {}
        return ZoneTimerCharDB.loot
    end
    ZoneLootDB = ZoneLootDB or {}
    return ZoneLootDB
end

local function PriceEpoch()
    if type(time) ~= "function" then return 0 end
    return math.floor(time() / 3600)
end

local function EmptyValuation(zone)
    return {
        zone = zone, cash = 0, vendor = 0, market = 0,
        priced = 0, unpriced = 0, quantity = 0,
        hasLoot = false, stale = false, state = {},
    }
end

---What a zone's loot and cash are worth.
---
---``cash`` is money picked up (ZoneGoldDB), ``vendor`` what a vendor would
---pay for the looted items, ``market`` the estimated auction proceeds.
---``unpriced`` counts the distinct items no price covers; they are left out
---of both totals instead of being valued at zero.
---@param zone string?
---@param charView boolean?
---@return table
function ZTR:Valuation(zone, charView)
    zone = zone or self.currentZone
    if not zone then return EmptyValuation(zone) end
    charView = charView == true

    local state = self:GetPriceState()
    local items = self:GetLootLedger(charView)[zone]

    -- The main window asks every frame, so the answer is cached.  The key
    -- carries the loot revision, the dataset identity and the hour: a new
    -- dataset, a looted item or a price going stale all recompute it.
    local key = table.concat({
        zone, charView and "char" or "account",
        tostring(self.ledgerRevision or 0),
        tostring(state.generatedAt or 0),
        tostring(PriceEpoch()),
    }, "|")
    self._valuations = self._valuations or {}
    local cached = self._valuations[key]
    if cached then return cached end

    local valuation = EmptyValuation(zone)
    valuation.state = state
    valuation.cash = self:GetZoneCash(zone, charView)
    valuation.stale = state.stale == true
    valuation.hasLoot = items ~= nil and next(items) ~= nil

    for itemId, rawCount in pairs(items or {}) do
        local count = tonumber(rawCount) or 0
        if count > 0 then
            local price = self:PriceItem(itemId)
            valuation.quantity = valuation.quantity + count
            if price then
                valuation.vendor = valuation.vendor + (price.vendor or 0) * count
                valuation.market = valuation.market + (price.market or 0) * count
                valuation.priced = valuation.priced + 1
            else
                valuation.unpriced = valuation.unpriced + 1
            end
        end
    end

    -- one entry per zone and view is all the UI asks for; a very long
    -- session would otherwise keep every hour's answer around
    local cachedEntries = 0
    for _ in pairs(self._valuations) do cachedEntries = cachedEntries + 1 end
    if cachedEntries > 40 then
        self._valuations = {}
    end
    self._valuations[key] = valuation
    return valuation
end

---The same numbers per hour spent in the zone.
---@param zone string?
---@param charView boolean?
---@return table { seconds, hours, cash, vendor, market, valuation }
function ZTR:ZoneRates(zone, charView)
    zone = zone or self.currentZone
    local valuation = self:Valuation(zone, charView)
    local seconds = self:GetZoneTime(zone, charView)
    local hours = seconds > 0 and seconds / 3600 or nil
    local function rate(value)
        if not hours or not value or value <= 0 then return nil end
        return value / hours
    end
    return {
        seconds = seconds,
        hours = hours,
        cash = rate(valuation.cash),
        vendor = rate(valuation.vendor),
        market = rate(valuation.market),
        valuation = valuation,
    }
end

---A gold amount short enough for one line: 950g, 1.2kg, 34g.
---@param copper number?
---@return string
function ZTR:ShortGold(copper)
    copper = tonumber(copper) or 0
    if copper >= 10000000 then
        return string.format("%.1fkg", copper / 1000000)
    end
    return string.format("%dg", math.floor(copper / 10000))
end

---The loot line: vendor value, then the market estimate marked with "~".
---@param valuation table?
---@return string
function ZTR:FormatLoot(valuation)
    if not valuation or (valuation.vendor == 0 and valuation.market == 0) then
        return "|cff9d9d9d---|r"
    end
    local vendor = self:ShortGold(valuation.vendor)
    if not valuation.market or valuation.market <= 0 then
        -- no market estimate: say so by showing only what is certain
        return string.format("|cffFFD700%s|r", vendor)
    end
    return string.format("|cffFFD700%s|r |cff808080/|r |cff66ddff~%s|r",
        vendor, self:ShortGold(valuation.market))
end

---The body of the loot tooltip: every number, and where it came from.
---@param zone string?
---@param charView boolean?
---@return string? title
---@return string? text
function ZTR:LootTooltip(zone, charView)
    zone = zone or self.currentZone
    if not zone then return nil end
    local valuation = self:Valuation(zone, charView)
    local rates = self:ZoneRates(zone, charView)
    local lines = {}
    lines[#lines + 1] = "Cash (picked up): " .. self:FormatGold(valuation.cash)
    lines[#lines + 1] = "Vendor: " .. self:FormatGold(valuation.vendor)
    lines[#lines + 1] = "Market: ~" .. self:FormatGold(valuation.market)
        .. " (estimated, after the 5% cut)"
    if rates.hours then
        lines[#lines + 1] = string.format(
            "Per hour: cash %s, vendor %s, market ~%s",
            self:ShortGold(rates.cash), self:ShortGold(rates.vendor),
            self:ShortGold(rates.market))
    end
    if valuation.unpriced > 0 then
        lines[#lines + 1] = string.format(
            "%d item type(s) with no price: counted, not valued",
            valuation.unpriced)
    end
    local state = valuation.state or {}
    if not state.available then
        lines[#lines + 1] = "No prices: " .. tostring(state.reason)
    elseif state.stale then
        lines[#lines + 1] = string.format(
            "|cffff7f00Prices are stale|r: dataset %s old (limit %s)",
            self:FormatAge(state.ageHours), self:FormatAge(state.maxAgeHours))
    else
        lines[#lines + 1] = string.format("Prices: DFA Estimate dataset %s old",
            self:FormatAge(state.ageHours))
    end
    return zone .. " -- gold", table.concat(lines, "\n")
end

---"3h 12m", "18m", "just now".
---@param hours number?
---@return string
function ZTR:FormatAge(hours)
    if not hours then return "age unknown" end
    if hours < 1 then return "under an hour" end
    local whole = math.floor(hours)
    if whole < 48 then
        local h = math.floor(whole)
        local m = math.floor((hours - h) * 60)
        if m > 0 then return string.format("%dh %dm", h, m) end
        return string.format("%dh", h)
    end
    return string.format("%dd", math.floor(whole / 24))
end

---The mobs this session saw, with MobInfo2's history where it has any.
---@param npcId number
---@return table? { source, name, kills, loots, copper, vendor, market,
---                  unpriced, vendorPerKill, marketPerKill, stale }
function ZTR:GetMobGold(npcId)
    npcId = tonumber(npcId)
    if not npcId then return nil end
    local state = self:GetPriceState()
    local mob = self.mobs and self.mobs[npcId] or nil
    local info = self:GetMobInfo(npcId)

    local source, name, kills, loots, copper, items
    if info then
        source = "mobinfo"
        name = info.name
        kills = tonumber(info.kills) or 0
        loots = tonumber(info.loots) or 0
        copper = tonumber(info.copper) or 0
        items = info.itemList
    elseif mob then
        source = "session"
        name = mob.name
        kills = tonumber(mob.kills) or 0
        loots = tonumber(mob.loots) or 0
        copper = tonumber(mob.copper) or 0
        items = mob.items
    else
        return nil
    end

    local result = {
        npcId = npcId, source = source, name = name,
        kills = kills, loots = loots, copper = copper,
        vendor = 0, market = 0, unpriced = 0, stale = state.stale == true,
    }
    for itemId, rawCount in pairs(items or {}) do
        local count = tonumber(rawCount) or 0
        if count > 0 then
            local price = self:PriceItem(itemId)
            if price then
                result.vendor = result.vendor + (price.vendor or 0) * count
                result.market = result.market + (price.market or 0) * count
            else
                result.unpriced = result.unpriced + 1
            end
        end
    end
    if kills > 0 then
        result.vendorPerKill = result.vendor / kills
        result.marketPerKill = result.market / kills
        result.copperPerKill = copper / kills
    end
    return result
end

---MobInfo2's documented per-mob record, or nil.
---
---Only the public API is used (ReadMe_MobInfo_API.txt): the saved table is
---LibSerialize-compressed and is not a stable interface.  The probe walks
---the level bands once per mob and remembers the answer, including "no
---data", because the UI asks on every tooltip.
---@param npcId number
---@return table?
function ZTR:GetMobInfo(npcId)
    npcId = tonumber(npcId)
    if not npcId then return nil end
    self._mobInfo = self._mobInfo or {}
    local cached = self._mobInfo[npcId]
    if cached ~= nil then
        return cached or nil
    end
    if type(MI2_GetMobData) ~= "function" then
        self._mobInfo[npcId] = false
        return nil
    end
    local found = nil
    for level = 1, MOBINFO_MAX_LEVEL, MOBINFO_STEP do
        local ok, data = pcall(MI2_GetMobData, npcId, level, nil, true)
        if ok and type(data) == "table" then
            found = data
            break
        end
    end
    self._mobInfo[npcId] = found or false
    return found
end

---The text `/zt gold` prints: every zone, then the mobs seen this session.
function ZTR:PrintPriceReport()
    local state = self:GetPriceState()
    print("|cff33ff99ZoneTimerRedux|r gold -- cash is money picked up, "
        .. "vendor is what a vendor pays, market is an estimate after the 5% cut.")
    if not state.available then
        print("  prices: |cffff0000" .. tostring(state.reason)
            .. "|r -- vendor values still shown, nothing estimated")
    else
        print(string.format("  prices: DFA Estimate dataset %s old (limit %s), %s",
            self:FormatAge(state.ageHours), self:FormatAge(state.maxAgeHours),
            state.stale and "|cffff7f00stale|r" or "current"))
    end

    local zones = self:GetSortedZones()
    if #zones == 0 then
        print("  no zones recorded yet")
    end
    for _, entry in ipairs(zones) do
        local rates = self:ZoneRates(entry.zone, self.charView)
        local valuation = rates.valuation
        local line = string.format(
            "  %-24s %-12s cash %s  vendor %s  market ~%s",
            entry.zone, self:FormatTime(rates.seconds),
            self:FormatGold(valuation.cash), self:FormatGold(valuation.vendor),
            self:FormatGold(valuation.market))
        if valuation.unpriced > 0 then
            line = line .. string.format("  (%d unpriced)", valuation.unpriced)
        end
        print(line)
        if rates.hours then
            print(string.format("      per hour: cash %s, vendor %s, market ~%s",
                self:FormatGold(rates.cash), self:FormatGold(rates.vendor),
                self:FormatGold(rates.market)))
        end
    end

    local shown = 0
    for npcId in pairs(self.mobs or {}) do
        local mob = self:GetMobGold(npcId)
        if mob and shown < 10 then
            shown = shown + 1
            local line = string.format(
                "  %s %s: %d kill(s), %d looted -- vendor %s, market ~%s",
                mob.source == "mobinfo" and "MobInfo2" or "session",
                tostring(mob.name or npcId), mob.kills, mob.loots,
                self:FormatGold(mob.vendor), self:FormatGold(mob.market))
            if mob.marketPerKill then
                line = line .. string.format(", market ~%s/kill",
                    self:FormatGold(mob.marketPerKill))
            end
            print(line)
        end
    end
end
