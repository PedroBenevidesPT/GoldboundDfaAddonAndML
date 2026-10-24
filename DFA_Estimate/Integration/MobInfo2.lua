-- MobInfo2 integration: the item values MobInfo2 records are DfA prices.
--
-- MobInfo2 stores a per-mob `itemValue` and shows a "price" line on the item
-- tooltip, both through `MI2.FindItemValue(itemLinkOrID)`.  Left alone that
-- function answers with the vendor sell price, or with MobInfo2's own frozen
-- price table -- a second pricing system that cannot agree with ours.  This
-- file points it at the dataset the engine ships, so a mob's `itemValue`,
-- its average per loot and the tooltip line are the same money the rest of
-- the account sees.
--
-- The wrapper is a Lua function, so it is never remembered in
-- SavedVariables: an older session's `installed = true` would skip the
-- install and silently fall back to MobInfo2's prices.  Only the player's
-- preference (`mobinfo = false`) is saved.
--
-- What MobInfo2 gets is a farmer's value, in this order: junk (poor quality)
-- that is not a transmog is worth the vendor price, a commodity is worth
-- TSM's cheapest listing (`minbuyout`), and everything else is worth the DfA
-- market estimate.  An item the dataset cannot price falls through to
-- MobInfo2's own answer, which is what keeps its history comparable and is
-- why nothing here ever returns zero.
--
-- The rules live in two places, and only one of them is used at a time:
--
--   * `MobInfo2.lua` itself is patched in place (it raises
--     `MI2.dfaFindItemValuePatched`).  That is the real implementation and
--     the one that gets the junk and commodity rules.
--   * this file wraps the function at runtime for a *stock* MobInfo2 -- a
--     patch lost to an addon update.  A wrapper cannot re-implement the
--     rules without duplicating them, so it answers with the market
--     estimate only, and it steps aside entirely when it sees the marker:
--     wrapping a patched MobInfo2 would answer first with a market-only
--     value and undo the rules (a grey would be worth its model price
--     instead of what a vendor pays).

local ADDON_NAME, Addon = ...

local MobInfo2 = {
	enabled = false,
	--: MobInfo2's own file already applies the rules; nothing to wrap
	patched = false,
	--: the function we installed; runtime only, never saved
	wrapper = nil,
	original = nil,
	served = 0,
	fallbacks = 0,
	error = nil,
}

Addon.DFAEstimateMobInfo2 = MobInfo2

local function API()
	-- luacheck: globals DFA_ESTIMATE_API
	return _G.DFA_ESTIMATE_API
end

---The DfA market value of an item, in copper, or nil.
---@param itemLinkOrID number|string
---@return number?
local function DfAValue(itemLinkOrID)
	local api = API()
	if not api or not api.BaseItemId or not api.GetLootValues then
		return nil
	end
	local id = api.BaseItemId(itemLinkOrID)
	if not id then
		return nil
	end
	local values = api.GetLootValues(id)
	if values and values.market and values.market > 0 then
		return values.market
	end
	return nil
end

---Whether the player wants MobInfo2 valued with the dataset.
---@return boolean
local function Preferred()
	local saved = _G.DFA_ESTIMATE_DB
	if type(saved) ~= "table" then
		return true
	end
	return saved.mobinfo ~= false
end

local function Remember(enabled)
	local saved = _G.DFA_ESTIMATE_DB
	if type(saved) ~= "table" then
		return
	end
	saved.mobinfo = enabled and true or false
end

---Installs the wrapper, if MobInfo2 is loaded and it is not installed yet.
---@return boolean installed
---@return string? reason
function MobInfo2:Install()
	local mi2 = _G.MI2
	if type(mi2) ~= "table" then
		return false, "MobInfo2 is not loaded"
	end
	if type(mi2.FindItemValue) ~= "function" then
		return false, "MobInfo2 exposes no FindItemValue"
	end
	-- already ours (a second login, a retried event, or a second copy of
	-- this file loaded by an addon that ships it twice)
	if mi2.FindItemValue == self.wrapper
			or mi2.FindItemValue == _G.DFA_ESTIMATE_MOBINFO2_WRAPPER then
		self.wrapper = mi2.FindItemValue
		self.enabled = true
		return true
	end
	-- MobInfo2 is patched in place: it already applies every rule, and a
	-- wrapper here would answer first with a market-only value and undo
	-- them.  Stand down and report success -- the values are ours.
	--
	-- The flag lives on the MI2 table because a Lua function has no table
	-- of its own; writing it onto the function would raise.
	if mi2.dfaFindItemValuePatched then
		self.patched = true
		self.wrapper = nil
		self.original = nil
		self.enabled = true
		self.error = nil
		return true
	end
	self.patched = false
	self.original = mi2.FindItemValue
	local original = self.original
	local counter = self
	local wrapper = function(itemLinkOrID)
		local value = DfAValue(itemLinkOrID)
		if value then
			counter.served = counter.served + 1
			return value
		end
		counter.fallbacks = counter.fallbacks + 1
		return original(itemLinkOrID)
	end
	-- a global, not a field on the function: a second copy of this file has
	-- its own `self`, and without a shared marker it would wrap the wrapper
	_G.DFA_ESTIMATE_MOBINFO2_WRAPPER = wrapper
	self.wrapper = wrapper
	mi2.FindItemValue = wrapper
	self.enabled = true
	self.error = nil
	return true
end

---Turns the integration on.
---@param remember boolean? store the preference
---@return boolean installed
function MobInfo2:Enable(remember)
	local installed, reason = self:Install()
	if installed and remember then
		Remember(true)
	end
	if not installed then
		self.error = reason
		self.enabled = false
	end
	return installed
end

---Puts MobInfo2's own function back (the preference is kept unless told).
---@param remember boolean?
function MobInfo2:Disable(remember)
	local mi2 = _G.MI2
	-- a patched MobInfo2 has nothing to unwrap: it reads the preference
	-- itself and falls back to its own prices when `mobinfo` is false
	if type(mi2) == "table" and self.wrapper and mi2.FindItemValue == self.wrapper
			and type(self.original) == "function" then
		mi2.FindItemValue = self.original
	end
	self.enabled = false
	if remember then
		Remember(false)
	end
end

---Called once at PLAYER_LOGIN.  MobInfo2 loads after us alphabetically, so
---this is the first moment its table is guaranteed to exist.
function MobInfo2:OnLogin()
	if not Preferred() then
		self.enabled = false
		return false
	end
	return self:Enable(false)
end

---One line for `/dfaestimate mobinfo`.
---@return string
function MobInfo2:Status()
	local installed = self.enabled and true or false
	local text
	if self.patched and installed then
		text = "MobInfo2: priced in place (junk -> vendor, commodities -> minbuyout)"
	else
		text = ("MobInfo2: %s (served %d value(s), %d fallback(s))")
			:format(installed and "priced by DfA" or "off",
				self.served or 0, self.fallbacks or 0)
	end
	if self.error then
		text = text .. "; " .. tostring(self.error)
	end
	return text
end

local eventFrame = CreateFrame("Frame")
eventFrame:RegisterEvent("ADDON_LOADED")
eventFrame:RegisterEvent("PLAYER_LOGIN")
eventFrame:SetScript("OnEvent", function(_, event, loadedName)
	if event == "ADDON_LOADED" then
		-- MobInfo2 may load after us; retry there and again at login
		if loadedName == "MobInfo2" and Preferred() then
			pcall(function() MobInfo2:Enable(false) end)
		end
		return
	end
	pcall(function() MobInfo2:OnLogin() end)
end)
