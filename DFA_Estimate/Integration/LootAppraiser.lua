-- LootAppraiser integration: value recipes, weapons and armor with the DfA
-- estimate.
--
-- LootAppraiser already supports a price source per item class.  Its own
-- resolver compares localized item-type names, so the class decision is
-- made by item class id here, and the three classes are answered from the
-- dataset's frozen table.  The TSM custom-string path is deliberately not
-- used for them: on this client LootAppraiser could not turn `dfaestimate`
-- into a number, while the frozen table needs no TSM at all.
--
-- The three classes are written into LootAppraiser's own profile, so the
-- change is visible in its options and survives a reload.  The previous
-- values are saved here, and `/dfaestimate lootappraiser off` puts them
-- back.

local ADDON_NAME, Addon = ...

local SOURCE = "dfaestimate"
local LABEL = "DfA: Estimate"

-- the profile keys LootAppraiser reads in PriceSources.ResolveForItem
local CLASS_KEYS = {
	"classTypeArmor", "classTypeArmorPriceSource",
	"classTypeWeapon", "classTypeWeaponPriceSource",
	"classTypeRecipe", "classTypeRecipePriceSource",
}

-- Blizzard item class ids: 2 weapon, 4 armor, 9 recipe.  LootAppraiser's
-- own class table is keyed on the English item-type string, which is
-- localized on a Portuguese client; the ids are not.
-- the profile key for each class id
local CLASS_PROFILE = {
	[2] = "classTypeWeapon",
	[4] = "classTypeArmor",
	[9] = "classTypeRecipe",
}

-- The wrapper is a Lua function, not a setting: it cannot survive a
-- reload, so it must never be remembered in SavedVariables.  An earlier
-- build saved `resolverInstalled = true`; on the next login the wrapper
-- was skipped and LootAppraiser fell back to its own resolver, which
-- compares localized class names ("Arma", "Armadura") with English keys.
local runtime = {
	resolverInstalled = false,
	wrappedResolver = nil,
	originalResolver = nil,
	valueInstalled = false,
	wrappedGetValue = nil,
	originalGetValue = nil,
}

---The Blizzard item class id, or nil when the item is not cached.
---@param itemID number
---@return number?
local function ClassID(itemID)
	if C_Item and C_Item.GetItemInfoInstant then
		local _, _, _, _, _, classID = C_Item.GetItemInfoInstant(itemID)
		if classID then
			return classID
		end
	end
	if GetItemInfo then
		return select(12, GetItemInfo(itemID))
	end
	return nil
end

local function Saved()
	-- DFA_ESTIMATE_DB is declared in the TOC; the game creates it before
	-- the files run, but a test harness or a damaged SavedVariables file
	-- may leave it nil
	local saved = _G.DFA_ESTIMATE_DB
	if type(saved) ~= "table" then
		saved = {}
		_G.DFA_ESTIMATE_DB = saved
	end
	if type(saved.lootappraiser) ~= "table" then
		saved.lootappraiser = {}
	end
	-- these described the running Lua state, not the profile, and an
	-- older build persisted them; a persisted true made the next login
	-- skip installing the wrapper
	saved.lootappraiser.resolverInstalled = nil
	saved.lootappraiser.originalResolver = nil
	return saved.lootappraiser
end

local function GetLootAppraiser()
	local AceAddon = LibStub and LibStub("AceAddon-3.0", true)
	if not AceAddon or not AceAddon.GetAddon then
		return nil
	end
	-- LootAppraiser registers the AceAddon under its display name; the
	-- folder name is kept as a fallback for older builds
	return AceAddon:GetAddon("LootAppraiser Reloaded", true)
		or AceAddon:GetAddon("LootAppraiserReloaded", true)
end

local function SaveOriginals(la, state)
	if state.originals then
		return
	end
	local profile = la.db.profile
	state.originals = {
		useSubClasses = profile.general and profile.general.useSubClasses,
	}
	for _, key in ipairs(CLASS_KEYS) do
		state.originals[key] = profile[key]
	end
end

---The key TSM actually registered, which may keep the source's capitals.
local function SourceKey()
	if TSM_API and TSM_API.GetPriceSourceKeys then
		local keys = {}
		TSM_API.GetPriceSourceKeys(keys)
		for _, key in ipairs(keys) do
			if type(key) == "string" and string.lower(key) == SOURCE then
				return key
			end
		end
	end
	return SOURCE
end

local function PublishSource(la)
	-- LootAppraiser filters TSM sources through its own table; without this
	-- entry `dfaestimate` would not appear in its dropdown, even though the
	-- class fields accept it as free text.  The class fields use the lower
	-- case spelling; the dropdown uses whatever TSM registered.
	local key = SourceKey()
	if la.CONST and la.CONST.PRICE_SOURCE then
		la.CONST.PRICE_SOURCE[SOURCE] = LABEL
		la.CONST.PRICE_SOURCE[key] = LABEL
	end
	if type(la.availablePriceSources) == "table" then
		la.availablePriceSources[key] = LABEL
	end
end

---Make the class decision by item class id, not by the localized type name.
---
---LootAppraiser compares `select(6, GetItemInfo(itemID))` with "Armor",
---"Weapon" and "Recipe".  That string is localized, so on a Portuguese
---client the class overrides never match.  The wrapper uses the class id
---instead, and then reads LootAppraiser's own profile values: if the user
---turns a class off, or turns the class overrides off, the wrapper passes
---the item straight through.
---
---The wrapper is reinstalled on every login.  It is a Lua function and
---cannot be saved, so a persisted "installed" flag would make the second
---session silently fall back to the localized resolver.
local function InstallResolver(la, state)
	local sources = la.PriceSources
	if type(sources) ~= "table" or type(sources.ResolveForItem) ~= "function" then
		return
	end
	if runtime.resolverInstalled and sources.ResolveForItem == runtime.wrappedResolver then
		return
	end
	local original = sources.ResolveForItem
	runtime.originalResolver = original
	local wrapped
	wrapped = function(itemID, basePriceSource)
		if state.enabled then
			local profile = la.db and la.db.profile
			if profile and profile.general and profile.general.useSubClasses then
				local key = CLASS_PROFILE[ClassID(itemID)]
				if key and profile[key] == true then
					local source = profile[key .. "PriceSource"]
					if source then
						return source
					end
				end
			end
		end
		return original(itemID, basePriceSource)
	end
	sources.ResolveForItem = wrapped
	runtime.wrappedResolver = wrapped
	runtime.resolverInstalled = true
end

---The dataset's price for an item, in copper, without TSM.
---
---The realm's own record wins: it is the per-realm estimate the tooltip
---shows.  When this realm has no record, the frozen table covers every item
---the region knows.  Neither path asks TSM for a price.  The live model is
---skipped because it reads TSM custom references, and on this client
---LootAppraiser cannot turn a custom source into a number.
---@param itemID number
---@return number?
local function DatasetPrice(itemID)
	local addon = Addon.DFAEstimate
	local id = tonumber(itemID)
	if not id then
		return nil
	end
	if addon and addon.GetRecord then
		local record = addon.GetRecord(id)
		-- Loot value is what the sale pays, not the price it is posted at.
		-- `net` is the realised price less the 5 % cut and the expected
		-- deposit; falling back to `price` keeps an older dataset working.
		if record and type(record.net) == "number" and record.net > 0 then
			return record.net
		end
		if record and type(record.price) == "number" and record.price > 0 then
			return record.price
		end
	end
	local data = addon and addon.data
	if not (data and data.prices) then
		data = _G.DFA_ESTIMATE
	end
	-- The regional table's net (price less the 5 % cut) covers every item
	-- the dataset prices, including the ones with no realm record.
	local nets = data and data.nets
	local net = nets and tonumber(nets[id])
	if net and net > 0 then
		return net
	end
	local prices = data and data.prices
	local price = prices and prices[id]
	if type(price) == "number" and price > 0 then
		return price
	end
	return nil
end

---Answers the three classes from the dataset, without TSM.
---
---`ResolveForItem` has already made the class decision by id; this wrapper
---is what turns that decision into a number.  If the dataset has no price,
---it returns nil: an explicit "no estimate" is better than silently asking
---TSM for a source the user cannot use.
local function InstallValueResolver(la, state)
	local sources = la.PriceSources
	if type(sources) ~= "table" or type(sources.GetItemValue) ~= "function" then
		return
	end
	if runtime.valueInstalled and sources.GetItemValue == runtime.wrappedGetValue then
		return
	end
	local original = sources.GetItemValue
	runtime.originalGetValue = original
	local wrapped
	wrapped = function(itemID, priceSource)
		if state.enabled and type(priceSource) == "string"
			and string.lower(priceSource) == SOURCE
			and CLASS_PROFILE[ClassID(itemID)] then
			return DatasetPrice(itemID)
		end
		return original(itemID, priceSource)
	end
	sources.GetItemValue = wrapped
	runtime.wrappedGetValue = wrapped
	runtime.valueInstalled = true
end

local Integration = Addon.DFAEstimateLootAppraiser or {}
Addon.DFAEstimateLootAppraiser = Integration

---Points LootAppraiser's Recipe, Weapon and Armor classes at `dfaestimate`.
---@param quiet boolean?
---@return boolean
function Integration:Enable(quiet)
	local la = GetLootAppraiser()
	if not la or not la.db or not la.db.profile then
		if not quiet then
			Addon.DFAEstimatePanel.Say(
				"LootAppraiser Reloaded is not loaded; nothing to configure.")
		end
		return false
	end
	local state = Saved()
	SaveOriginals(la, state)
	local profile = la.db.profile
	local already = profile.classTypeArmor == true
		and profile.classTypeArmorPriceSource == SOURCE
		and profile.classTypeWeapon == true
		and profile.classTypeWeaponPriceSource == SOURCE
		and profile.classTypeRecipe == true
		and profile.classTypeRecipePriceSource == SOURCE
	profile.general = profile.general or {}
	PublishSource(la)
	profile.general.useSubClasses = true
	profile.classTypeArmor = true
	profile.classTypeArmorPriceSource = SOURCE
	profile.classTypeWeapon = true
	profile.classTypeWeaponPriceSource = SOURCE
	profile.classTypeRecipe = true
	profile.classTypeRecipePriceSource = SOURCE
	state.enabled = true
	InstallResolver(la, state)
	InstallValueResolver(la, state)
	if la.UI and la.UI.RefreshStatusText then
		pcall(la.UI.RefreshStatusText)
	end
	if not quiet and not already then
		Addon.DFAEstimatePanel.Say(
			("LootAppraiser: Recipe, Weapon and Armor now use %q."):format(SOURCE))
	end
	return true
end

---Puts the values that were there before the first Enable back.
---@param quiet boolean?
---@return boolean
function Integration:Disable(quiet)
	local la = GetLootAppraiser()
	local state = Saved()
	if not la or not la.db or not la.db.profile or not state.originals then
		if not quiet then
			Addon.DFAEstimatePanel.Say("LootAppraiser: nothing to restore.")
		end
		return false
	end
	local profile = la.db.profile
	profile.general = profile.general or {}
	profile.general.useSubClasses = state.originals.useSubClasses
	for _, key in ipairs(CLASS_KEYS) do
		profile[key] = state.originals[key]
	end
	state.originals = nil
	state.enabled = false
	if la.UI and la.UI.RefreshStatusText then
		pcall(la.UI.RefreshStatusText)
	end
	if not quiet then
		Addon.DFAEstimatePanel.Say(
			"LootAppraiser: the previous class price sources were restored.")
	end
	return true
end

---What is in LootAppraiser's profile right now.
---@return string
function Integration:Status()
	local la = GetLootAppraiser()
	if not la or not la.db or not la.db.profile then
		return "LootAppraiser Reloaded is not loaded."
	end
	local profile = la.db.profile
	-- LootAppraiser ignores loot entirely when no session is running; the
	-- overrides can be perfect and nothing will be counted.
	local session = la.Session and la.Session.IsRunning
		and la.Session.IsRunning() or false
	local general = profile.general or {}
	return ("LootAppraiser: armor=%s (%s), weapon=%s (%s), recipe=%s (%s), "
		.. "class overrides=%s, resolver=%s, value=%s, session=%s, "
		.. "ignoreSoulbound=%s, autoStart=%s"):format(
		tostring(profile.classTypeArmor),
		tostring(profile.classTypeArmorPriceSource),
		tostring(profile.classTypeWeapon),
		tostring(profile.classTypeWeaponPriceSource),
		tostring(profile.classTypeRecipe),
		tostring(profile.classTypeRecipePriceSource),
		tostring(general.useSubClasses),
		runtime.resolverInstalled and "installed" or "not installed",
		runtime.valueInstalled and "installed" or "not installed",
		session and "running" or "stopped",
		tostring(general.ignoreSoulboundItems),
		tostring(general.autoStartLA))
end

function Integration:IsEnabled()
	local la = GetLootAppraiser()
	if not la or not la.db or not la.db.profile then
		return false
	end
	local profile = la.db.profile
	-- the profile alone is not enough: on a localized client the resolver
	-- is what makes the class ids match the three source fields, and the
	-- value wrapper is what turns the decision into the frozen price
	return runtime.resolverInstalled and runtime.valueInstalled
		and profile.classTypeArmor == true
		and profile.classTypeArmorPriceSource == SOURCE
		and profile.classTypeWeapon == true
		and profile.classTypeWeaponPriceSource == SOURCE
		and profile.classTypeRecipe == true
		and profile.classTypeRecipePriceSource == SOURCE
end

---Called once per login from DFA_Estimate.lua.
---@param quiet boolean?
function Integration:OnLogin(quiet)
	-- no LootAppraiser, no message: the integration is optional
	if not GetLootAppraiser() then
		return
	end
	local state = Saved()
	-- an explicit "off" is remembered; a first run applies the integration
	if state.enabled == nil then
		state.enabled = true
	end
	if state.enabled then
		self:Enable(quiet)
	end
end

-- A separate command, so the LootAppraiser settings can be changed without
-- remembering the /dfaestimate subcommand.
SLASH_DFALOOTAPPRAISER1 = "/dfala"
SlashCmdList["DFALOOTAPPRAISER"] = function(msg)
	msg = tostring(msg or ""):lower()
	msg = string.match(msg, "^%s*(.-)%s*$") or ""
	if msg == "off" or msg == "revert" then
		Integration:Disable(false)
	elseif msg == "status" then
		Addon.DFAEstimatePanel.Say(Integration:Status())
	else
		Integration:Enable(false)
		Addon.DFAEstimatePanel.Say(Integration:Status())
	end
end
