-- The DfA estimate: a price source that says what a unit will really
-- fetch, and what it is worth.
--
-- This file is part of the DfA fork of TradeSkillMaster.  It is
-- *loaded*, not merged: the fork keeps TSM's own file untouched and
-- adds this one, so a TSM update that changes nothing here cannot
-- break the fork, and a fork that breaks can be removed by deleting
-- one file.
--
-- What it does:
--   * reads the generated dataset published by the DfA_Estimate addon;
--   * registers a custom price source named "DFAEstimate", usable in
--     any TSM custom string as: dfaestimate
--     (bare -- TSM's custom string language has no braces, and an
--     identifier must be letters only);
--   * returns a positive copper value, or nil when there is no estimate;
--   * never touches an operation, a group, a profile or a saved string.
--
-- What it deliberately does not do:
--   * it does not post, cancel, undercut or reprice anything.  A price
--     source is a number; using it is the user's decision.

local TSM = select(2, ...) ---@type TSM

-- The registration must not depend on anything optional.  If one of the
-- helpers below were missing, the whole chunk would die and the source
-- would silently not exist -- which is exactly the failure the user
-- sees as "the source is not available".  So the source is registered
-- first, with the least it can do with, and the rest is added around it.
local CustomString = TSM.LibTSMTypes:Include("CustomString")

local Log = nil
local ChatMessage = nil
local SessionInfo = nil
local L = nil

do
	local ok, err
	ok, Log = pcall(function() return TSM.LibTSMUtil:Include("Util.Log") end)
	if not ok then Log = nil end
	ok, ChatMessage = pcall(function() return TSM.LibTSMService:Include("UI.ChatMessage") end)
	if not ok then ChatMessage = nil end
	ok, SessionInfo = pcall(function() return TSM.LibTSMWoW:Include("Util.SessionInfo") end)
	if not ok then SessionInfo = nil end
	ok, L = pcall(function() return TSM.Locale.GetTable() end)
	if not ok then L = nil end
end

-- The dataset schema this source understands.  A dataset with another
-- version is refused: the reader must not guess at a format it does
-- not know.
local SUPPORTED_DATASET_VERSION = 2
-- The schema-2 fields are additive; reading a schema-1 dataset is safe.
local SUPPORTED_DATASET_VERSIONS = { [1] = true, [2] = true }

-- The fork's own version, so a future change can be recognised.
local SOURCE_VERSION = "2.0.0"

-- The name the source is registered under.  TSM's custom string
-- language has no braces and wants an identifier of letters only, so
-- the source is written bare: dfaestimate
local SOURCE_KEY = "DFAEstimate"

local private = {
	data = nil,          -- the dataset, or nil
	dataVersion = nil,   -- the version of the dataset that was read
	loaded = false,
	error = nil,
}

-- luacheck: globals DFA_ESTIMATE
DFA_ESTIMATE = DFA_ESTIMATE or nil



-- ============================================================================
-- Module Functions
-- ============================================================================

---Reads the dataset the DfA_Estimate addon published.
---@return boolean success
local function LoadData()
	local data = _G.DFA_ESTIMATE
	if type(data) ~= "table" then
		private.error = "the DfA_Estimate addon is not loaded"
		return false
	end
	if not SUPPORTED_DATASET_VERSIONS[data.version] then
		private.error = ("the dataset is version %s and this source understands version %s")
			:format(tostring(data.version), tostring(SUPPORTED_DATASET_VERSION))
		return false
	end
	if type(data.model) ~= "table" or type(data.model.coefficients) ~= "table"
		or #data.model.coefficients == 0 then
		private.error = "the dataset carries no usable model"
		return false
	end
	private.data = data
	private.dataVersion = data.version
	private.loaded = true
	private.error = nil
	return true
end

---The realm the client is on, in the form the dataset uses.
---@return string
local function GetRealmName()
	local name = nil
	if SessionInfo then
		name = SessionInfo.GetRealmName()
	end
	if not name or name == "" then
		return ""
	end
	-- the same sanitising TSM itself applies to realm names
	return (string.gsub(name, "\226", "'"))
end

---The item id in an item string, or nil.
---
---Only ``i:`` is accepted.  The dataset carries no pet data, and a pet's
---species id is *not* an item id: reading one as the other returns the
---price of an unrelated item.  1,296 of the first 3,500 pet species ids
---collide with an item price in this dataset, so the confusion is not
---hypothetical.
---@param itemString string
---@return number?
local function ItemId(itemString)
	return tonumber(string.match(itemString or "", "^i:(%d+)"))
end

---The estimate the dataset holds for an item in the current realm.
---@param itemString string The item string
---@return number? price The price in copper, or nil
---@return number? confidence 0..1
---@return table? record The whole record, when there is one
local function GetEstimate(itemString)
	local data = private.data
	if not data then
		return nil
	end
	local itemId = ItemId(itemString)
	if not itemId then
		return nil
	end
	local realmBlock = data.realms and data.realms[GetRealmName()]
	if not realmBlock then
		return nil
	end
	local record = realmBlock.items and realmBlock.items[itemId]
	if not record or not record.price or record.price <= 0 then
		return nil
	end
	return record.price, record.conf, record
end

---The price the model expects for an item, computed from the live market
---references.  This is what makes the source work for *every* item and
---not only the ones the account has traded.
---@param itemString string
---@return number? price In copper, or nil
local function GetModelPrice(itemString)
	local data = private.data
	if not data or not data.model then
		return nil
	end
	-- items only: the model was fitted on items, and a pet's species id is
	-- not one
	if not ItemId(itemString) then
		return nil
	end
	local model = data.model
	local references = {
		dbregionmarketavg = TSM.AuctionDB.GetRegionItemData(itemString, "regionMarketValue"),
		dbregionhistorical = TSM.AuctionDB.GetRegionItemData(itemString, "regionHistorical"),
		dbregionsaleavg = TSM.AuctionDB.GetRegionItemData(itemString, "regionSale"),
	}
	local total = model.intercept
	local present = 0
	for index, name in ipairs(model.references or {}) do
		local value = references[name]
		if not value or value <= 0 then
			-- the median is already on the log scale: it is the median of
			-- the logarithms, exactly as the model was fitted
			total = total + (model.coefficients or {})[index] * ((model.medians or {})[index] or 0)
		else
			present = present + 1
			total = total + (model.coefficients or {})[index] * math.log(value + 1)
		end
	end
	if present == 0 then
		-- not one reference exists for this item: there is no estimate,
		-- and saying "the price of a typical item" would be a lie
		return nil
	end
	if not total or total ~= total or total <= 0 then
		return nil
	end
	return math.exp(total)
end

---The price the dataset froze for an item at build time.
---
---The model above needs at least one live regional reference.  When TSM has
---none for the item -- a fresh install that has not downloaded the region
---data, a region file that is out of date, or an item the live scan has not
---seen -- the table the dataset ships is the last resort.  It is the same
---model applied to the region's references when the dataset was built, so it
---answers for every item the region covered, in every realm, without
---pretending the number is current.  The companion addon already uses this
---table; the TSM source has to use it too, or the two disagree about which
---items have an estimate at all.
---@param itemString string
---@return number? price In copper, or nil
local function GetFrozenPrice(itemString)
	local data = private.data
	if not data or type(data.prices) ~= "table" then
		return nil
	end
	local itemId = ItemId(itemString)
	if not itemId or itemId <= 0 then
		return nil
	end
	local price = data.prices[itemId]
	if type(price) ~= "number" or price <= 0 then
		return nil
	end
	return price
end

---The sale rate the dataset measured for an ask, as a fraction of the
---regional sale average.
---@param ratio number The ask divided by the regional sale average
---@return number?
local function GetCurveProbability(ratio)
	local data = private.data
	if not data or not data.curve then
		return nil
	end
	local best, bestDistance = nil, nil
	for _, point in ipairs(data.curve) do
		local distance = math.abs(point.ratio - ratio)
		if bestDistance == nil or distance < bestDistance then
			best, bestDistance = point, distance
		end
	end
	return best and best.p or nil
end

---The value of the source: a positive copper number, or nil.
---@param itemString string
---@return number?
local function DFAEstimateFunc(itemString)
	if not private.loaded and not LoadData() then
		return nil
	end
	-- the account's own transactions first: they carry the sample size
	-- and the confidence, and they are the only figure that is about
	-- *this* realm
	local price = GetEstimate(itemString)
	if price and price > 0 then
		return price
	end
	-- otherwise the model, applied to the live market references
	local modelled = GetModelPrice(itemString)
	if modelled and modelled > 0 then
		return math.floor(modelled)
	end
	-- and, when TSM has no live reference at all, the table the dataset
	-- froze at build time -- so the source answers for every item the
	-- region covered, not only the ones the current scan happens to hold
	return GetFrozenPrice(itemString)
end



-- ============================================================================
-- Registration
-- ============================================================================

-- The public face of the source, for the companion addon and for
-- debugging.  It is a plain global, not a TSM module: the fork adds one
-- file and does not extend TSM's own module tree.
-- luacheck: globals DFA_ESTIMATE_SOURCE
DFA_ESTIMATE_SOURCE = {
	version = SOURCE_VERSION,
	supportedDatasetVersion = SUPPORTED_DATASET_VERSION,
	sourceKey = SOURCE_KEY,
	GetData = function() return private.data end,
	GetError = function() return private.error end,
	GetRealmName = GetRealmName,
	GetRealmGroup = function()
		local data = private.data
		if not data then
			return nil
		end
		return data.realmGroups and data.realmGroups[GetRealmName()] or nil
	end,
	GetEstimate = GetEstimate,
	GetModelPrice = GetModelPrice,
	GetFrozenPrice = GetFrozenPrice,
	GetCurveProbability = GetCurveProbability,
	Reload = function()
		private.loaded = false
		private.data = nil
		return LoadData()
	end,
}

-- The label comes from TSM's locale when it is there, and from a plain
-- string when it is not: the registration must never fail.
local label = "DFA - Realizable Price Estimate"
if L then
	local success, resolved = pcall(function()
		return L["DFA - Realizable Price Estimate"]
	end)
	if success and type(resolved) == "string" and resolved ~= "" then
		label = resolved
	end
end

CustomString.RegisterSource("DFA", SOURCE_KEY,
	label, DFAEstimateFunc, CustomString.SOURCE_TYPE.NORMAL)

-- The user has to be able to see that the fork is alive, and why it
-- would not answer if it cannot.  A log line does not reach the chat
-- in a release build, so this is a message the user actually sees.
local function Tell(text)
	if ChatMessage then
		ChatMessage.PrintfUser("|cff00ff00DFAEstimate|r: %s", text)
	end
	if Log then
		Log.Info("DFAEstimate: %s", text)
	end
end

if not LoadData() then
	Tell(("%s.  The source is registered but it will answer nil until the data addon is loaded."):format(tostring(private.error)))
else
	Tell(("loaded, dataset version %s, %d item(s) in %s."):format(
		tostring(private.dataVersion), private.data.itemCount or 0, GetRealmName()))
end
