-- The estimate on the item tooltip.
--
-- The tooltip is the *appropriate* place for the short form: what the
-- item is worth, how much that is worth, and a warning when the data
-- behind it is thin or stale.  The panel has the long form.
--
-- How the lines get onto the tooltip follows Auctionator, not TSM: TSM
-- exposes a custom price source and draws its own UI, it does not inject
-- into the game's item tooltip.  Auctionator registers a TooltipDataProcessor
-- post-call and, inside it, reads the tooltip's own `info` (getter name and
-- arguments) to find the item.  That covers the bags, the bank, the auction
-- house and the merchant, where `tooltip:GetItem()` is often nil.  When the
-- processor is unavailable it hooks the GameTooltip setters directly.

local ADDON_NAME, Addon = ...

local Tooltip = Addon.DFAEstimateTooltip or {}
Addon.DFAEstimateTooltip = Tooltip

local private = {
	hook = nil,
	processor = nil,
	-- the hook stays installed once it is in place (registering the same
	-- post-call twice would append the lines twice); this is what turns the
	-- lines on and off
	active = true,
}

local FLAG_TEXT = {
	["stale"] = "the data is stale",
	["thin-sample"] = "a thin sample",
	["speculative-reference"] = "the asking reference has run away from the transactions",
	["contradictory-references"] = "the market references disagree",
	["no-local-reference"] = "no local reference",
	["shared-appearance"] = "the cosmetic set is worn by several items",
	["time-to-sale-unsupported"] = "no defensible time to sale",
	["low-confidence"] = "low confidence",
}

-- The dataset's status is a model-lifecycle token: it says which estimator
-- produced the number and how much of it was measured.  That vocabulary
-- belongs to the reports and the artifact, not to a tooltip, so the token
-- is translated here and nowhere else.  The dataset still carries
-- "baseline_only" unchanged -- only the sentence a player reads changes.
-- A token with no entry is shown as it arrived rather than swallowed, so a
-- status added upstream is visible instead of silently disappearing.
local STATUS_TEXT = {
	["baseline_only"] = "item sale rate, no learned model",
	["trained_no_measured_gain"] = "no better than a simple rate",
	["validated_ranking_only"] = "ranking only, no duration",
	["unsupported_signal"] = "no signal the data can support",
	["insufficient_data"] = "too little evidence",
	["stale"] = "out of date",
	["unknown"] = "not available",
}

local unpack = _G.unpack or table.unpack


---The copper value of an item, or nil.
---@param itemId number
---@return number?
local function GetPrice(itemId)
	-- Estimate, not GetRecord: an item this realm never sold still gets a
	-- price, from the model the dataset ships, as long as TSM knows it.
	local record = Addon.DFAEstimate and Addon.DFAEstimate.Estimate(itemId)
	if record and record.price and record.price > 0 then
		return record.price
	end
	return nil
end

---How many of this item the inventory snapshot holds, and how many are
---posted.  It reads the other addon's SavedVariables directly: both are
---ours, and a wrong count is worse than no line, so anything unusual is
---ignored instead of guessed.
---@param itemId number
---@return number? owned
---@return number? posted
function Tooltip.CountOwned(itemId)
	local db = _G.DFA_AUCTION_INTELLIGENCE_DB
	local latest = type(db) == "table" and db.inventory_latest or nil
	if type(latest) ~= "table" or type(latest.locations) ~= "table" then
		return nil, nil
	end
	local owned, posted = 0, 0
	for location, rows in pairs(latest.locations) do
		if type(rows) == "table" then
			for _, row in ipairs(rows) do
				if tonumber(row.itemId) == itemId then
					local quantity = tonumber(row.quantity) or 0
					owned = owned + quantity
					if location == "posted" then
						posted = posted + quantity
					end
				end
			end
		end
	end
	return owned, posted
end

---The price, followed by its calibrated range when one exists.
---
---The range is the model's measured uncertainty, not a decoration: the
---coverage is what the interval actually delivered on a holdout, so it
---is printed next to the width.  A record from an old dataset has no
---range and gets no line -- "missing" is never printed as "zero".
---@param lines table
---@param label string
---@param price number?
---@param low number?
---@param high number?
---@param level number?
---@param coverage number?
---@param samples number?
local function AddPrice(lines, label, price, low, high, level, coverage, samples)
	if not price or price <= 0 then
		return
	end
	lines[#lines + 1] = ("%s: %s"):format(label, string.format("%.2fg", price / 10000))
	if not low or not high or low <= 0 or high <= low then
		return
	end
	local text = ("  %d%% range: %s - %s"):format(
		math.floor((tonumber(level) or 0.8) * 100),
		string.format("%.2fg", low / 10000),
		string.format("%.2fg", high / 10000))
	local measured = tonumber(coverage)
	if measured then
		local count = tonumber(samples)
		if count and count > 0 then
			text = text .. (" (%d%% of %d sales)"):format(
				math.floor(measured * 100), count)
		else
			text = text .. (" (%d%% measured)"):format(math.floor(measured * 100))
		end
	end
	lines[#lines + 1] = text
end

---Builds the tooltip lines for an item.
---@param itemId number
---@return table<string, string> lines
function Tooltip.GetLines(itemId)
	local lines = {}
	local addon = Addon.DFAEstimate
	if not addon or not addon.data then
		return lines
	end
	local record = addon.Estimate(itemId)
	if not record then
		return lines
	end
	local price = record.price
	if not price or price <= 0 then
		return lines
	end
	AddPrice(lines, "DFA estimate", price, record.plow, record.phigh,
		record.plevel, record.pcov, record.pintn)
	-- What the item is worth to a vendor, and what is already in stock.
	-- Baganator shows the same pair; here it is the context for the ask,
	-- not a second price.
	local itemInfo = GetItemInfo
		or (C_Item and C_Item.GetItemInfo) or nil
	local okInfo, _, _, _, _, _, _, _, _, _, _, vendorPrice = false
	if itemInfo then
		okInfo, _, _, _, _, _, _, _, _, _, _, vendorPrice =
			pcall(itemInfo, itemId)
	end
	-- The client's sell price is the tenth return after pcall adds its own
	-- success flag, and some clients return nothing for it when the item is
	-- not cached yet.  The dataset ships the same number from the Blizzard
	-- table, so the line appears either way instead of vanishing.
	if not (type(vendorPrice) == "number" and vendorPrice > 0) then
		local dataset = (Addon.DFAEstimate and Addon.DFAEstimate.data)
			or _G.DFA_ESTIMATE
		vendorPrice = tonumber(record.vendor)
			or tonumber(dataset and dataset.vendors and dataset.vendors[itemId])
	end
	if type(vendorPrice) == "number" and vendorPrice > 0 then
		lines[#lines + 1] = ("Vendor: %s"):format(
			string.format("%.2fg", vendorPrice / 10000))
	end
	-- What the auction actually pays: the realised price less the 5 % cut
	-- and the deposit that a sale refunds and an expiry does not.  The ask
	-- above is a price to post at; this is the money.
	local netPrice = tonumber(record.net)
	if not netPrice then
		local dataset = (Addon.DFAEstimate and Addon.DFAEstimate.data)
			or _G.DFA_ESTIMATE
		netPrice = tonumber(dataset and dataset.nets and dataset.nets[itemId])
	end
	if netPrice and netPrice > 0 then
		lines[#lines + 1] = ("Net after 5%% cut: %s"):format(
			string.format("%.2fg", netPrice / 10000))
	end
	-- What to do, when the dataset carries an action.  The dataset is the
	-- only thing that decides it: the tooltip never re-derives a price.
	local action = record.action
	if type(action) == "table" and type(action.action) == "string" then
		local labels = {
			post = "post now", undercut = "match the cheapest",
			hold = "hold", relist = "cancel and relist",
			change_realm = "move realm", wait = "wait",
			stop = "do not post",
		}
		local text = "Action: " .. (labels[action.action] or action.action)
		if action.action == "wait" and tonumber(action.projected_ask) then
			text = text .. (" until %.2fg"):format(action.projected_ask / 10000)
		elseif tonumber(action.ask) and tonumber(action.ask) > 0 then
			text = text .. (" at %.2fg"):format(action.ask / 10000)
		end
		lines[#lines + 1] = text
	end
	-- The observed market: only the fields a snapshot or a scan produced.
	local market = record.market
	if type(market) == "table" then
		local parts = {}
		if tonumber(market.listings) then
			parts[#parts + 1] = ("%d listed"):format(market.listings)
		end
		if tonumber(market.units_change_pct) then
			parts[#parts + 1] = ("supply %+d%%"):format(
				math.floor(market.units_change_pct * 100 + 0.5))
		end
		if tonumber(market.trend_pct) then
			parts[#parts + 1] = ("trend %+d%%"):format(
				math.floor(market.trend_pct * 100 + 0.5))
		end
		if tonumber(market.own_share) and market.own_share >= 0.2 then
			parts[#parts + 1] = ("%d%% ours"):format(
				math.floor(market.own_share * 100 + 0.5))
		end
		if #parts > 0 then
			lines[#lines + 1] = "Market: " .. table.concat(parts, ", ")
		end
	end
	local owned, posted = Tooltip.CountOwned(itemId)
	if owned and owned > 0 then
		lines[#lines + 1] = ("In stock: %d (posted %d)"):format(owned, posted or 0)
	end
	lines[#lines + 1] = ("Data confidence: %d%%"):format(math.floor((record.conf or 0) * 100))
	-- The validated prediction layer.  Every field is optional: a dataset
	-- from before schema 2, or a target the data could not support, simply
	-- has no line.  A nil probability is never shown as 0 %.
	if record.ereal then
		AddPrice(lines, "Expected realized", record.ereal, record.elow,
			record.ehigh, record.elevel, record.ecov, record.eintn)
	end
	local byHorizon = record.psellByHorizon
	if type(byHorizon) == "table" then
		local parts = {}
		for _, horizon in ipairs({ 24, 48, 168 }) do
			local probability = byHorizon[horizon]
			if type(probability) == "number" then
				parts[#parts + 1] = ("%dh %d%%"):format(
					horizon, math.floor(probability * 100))
			end
		end
		if #parts > 0 then
			lines[#lines + 1] = "Sale probability: " .. table.concat(parts, "  ")
		end
	end
	if record.undercut then
		lines[#lines + 1] = ("Market undercut pressure (24h): %d%%"):format(
			math.floor(record.undercut * 100))
	end
	if record.decline then
		lines[#lines + 1] = ("Price-decline risk (24h): %d%%"):format(
			math.floor(record.decline * 100))
	end
	-- The status is a lifecycle token, not a sentence.  A record from a
	-- dataset that predates schema 2 has none and gets no line, and the
	-- fully validated case -- the normal one -- stays silent.
	local status = record.status
	if status ~= nil and status ~= "validated" then
		local token = tostring(status)
		if token ~= "" then
			local text = ("Prediction: %s"):format(STATUS_TEXT[token] or token)
			if record.fallback and record.fallback ~= "" then
				text = text .. " (" .. record.fallback .. ")"
			end
			lines[#lines + 1] = text
		end
	end
	if record.n and record.n > 0 then
		lines[#lines + 1] = ("Evidence: %d sale(s) of this item here"):format(record.n)
	elseif record.source == "model" then
		-- say where it comes from: an estimate off the regional references
		-- is not the same claim as one off a sale in this realm
		lines[#lines + 1] = ("Evidence: the market model (%d reference(s)), "
			.. "no sale of this item here yet"):format(record.references or 0)
	else
		lines[#lines + 1] = "Evidence: the market model, no sale of this item here yet"
	end
	-- The realm answer: where else the item would pay more, and how much
	-- babysitting this realm costs.  Both are measured, not modelled; a
	-- realm with no scan and no second snapshot simply gets no line.
	local best = addon.BestRealm and addon.BestRealm(itemId)
	-- 5 %: below that the 5 % cut and the deposit eat the difference
	if best and best.realm ~= addon.GetRealmName()
			and (best.gain or 0) >= 0.05 then
		lines[#lines + 1] = ("Best realm: %s  %s (+%d%%)%s"):format(
			best.realm, string.format("%.2fg", best.price / 10000),
			math.floor((best.gain or 0) * 100),
			best.measured and "" or " (regional baseline)")
		if best.share and best.share > 0 then
			lines[#lines + 1] = ("  undercut there: %d%% of listings"):format(
				math.floor(best.share * 100))
		end
	end
	local share, risk, watched = addon.BabysitProbability(addon.GetRealmName())
	if risk then
		lines[#lines + 1] = ("Babysit risk here: %d%% in the next 24h"):format(
			math.floor(risk * 100))
	elseif share then
		lines[#lines + 1] = ("Babysit here: %d%% of listings undercut (n=%d)"):format(
			math.floor(share * 100), watched or 0)
	end
	local age = addon.GetAgeHours()
	if age and age >= 0 then
		lines[#lines + 1] = ("Dataset age: %.0f hour(s)"):format(age)
	end
	for _, flag in ipairs(record.flags or {}) do
		lines[#lines + 1] = ("! %s"):format(FLAG_TEXT[flag] or flag)
	end
	return lines
end

---The base item id in a link, an item string or a bare number.
---
---A link can carry bonus ids, an item level and a context; the dataset is
---keyed on the base item id, the first number.  Battle pets are rejected:
---their id is a species id, and the dataset has no pet prices.
---@param value any
---@return number?
local function BaseItemId(value)
	if type(value) == "number" then
		if value > 0 then
			return value
		end
		return nil
	end
	if type(value) ~= "string" then
		return nil
	end
	if string.match(value, "^battlepet:") then
		return nil
	end
	local id = string.match(value, "item:(%d+)")
	if id then
		return tonumber(id)
	end
	id = string.match(value, "^i:(%d+)")
	if id then
		return tonumber(id)
	end
	if string.match(value, "^%d+$") then
		return tonumber(value)
	end
	return nil
end

---The container item link, modern or old API.
---@param bag number
---@param slot number
---@return string?
local function BagItemLink(bag, slot)
	if ItemLocation and C_Item and C_Item.GetItemLink and C_Item.DoesItemExist then
		local ok, link = pcall(function()
			local location = ItemLocation:CreateFromBagAndSlot(bag, slot)
			if location and C_Item.DoesItemExist(location) then
				return C_Item.GetItemLink(location)
			end
		end)
		if ok and link then
			return link
		end
	end
	if GetContainerItemLink then
		return GetContainerItemLink(bag, slot)
	end
	return nil
end

-- The getters named by the tooltip's own info.  Auctionator maps
-- `GetBagItem` to `SetBagItem` and passes the same arguments; this is the
-- same table, reduced to what we need: the item, not the price.
local HANDLERS = {}

HANDLERS.SetBagItem = function(_, bag, slot)
	return BaseItemId(BagItemLink(bag, slot))
end

HANDLERS.SetBuybackItem = function(_, index)
	return BaseItemId(GetBuybackItemLink and GetBuybackItemLink(index))
end

HANDLERS.SetMerchantItem = function(_, index)
	return BaseItemId(GetMerchantItemLink and GetMerchantItemLink(index))
end

HANDLERS.SetInventoryItem = function(_, unit, slot)
	return BaseItemId(GetInventoryItemLink and GetInventoryItemLink(unit, slot))
end

HANDLERS.SetGuildBankItem = function(_, tab, slot)
	return BaseItemId(GetGuildBankItemLink and GetGuildBankItemLink(tab, slot))
end

HANDLERS.SetLootItem = function(_, slot)
	return BaseItemId(GetLootSlotLink and GetLootSlotLink(slot))
end

HANDLERS.SetLootRollItem = function(_, slot)
	return BaseItemId(GetLootRollItemLink and GetLootRollItemLink(slot))
end

HANDLERS.SetQuestItem = function(_, kind, index)
	return BaseItemId(GetQuestItemLink and GetQuestItemLink(kind, index))
end

HANDLERS.SetQuestLogItem = function(_, kind, index)
	return BaseItemId(GetQuestLogItemLink and GetQuestLogItemLink(kind, index))
end

HANDLERS.SetSendMailItem = function(_, index)
	return BaseItemId(GetSendMailItemLink and GetSendMailItemLink(index))
end

HANDLERS.SetInboxItem = function(_, index)
	return BaseItemId(GetInboxItemLink and GetInboxItemLink(index))
end

HANDLERS.SetTradePlayerItem = function(_, index)
	return BaseItemId(GetTradePlayerItemLink and GetTradePlayerItemLink(index))
end

HANDLERS.SetTradeTargetItem = function(_, index)
	return BaseItemId(GetTradeTargetItemLink and GetTradeTargetItemLink(index))
end

HANDLERS.SetAuctionItem = function(_, viewType, index)
	return BaseItemId(GetAuctionItemLink and GetAuctionItemLink(viewType, index))
end

HANDLERS.SetItemKey = function(_, itemID)
	return BaseItemId(itemID)
end

HANDLERS.SetItemByID = function(_, itemID)
	return BaseItemId(itemID)
end

HANDLERS.SetHyperlink = function(_, link)
	return BaseItemId(link)
end

---The item behind a tooltip, using the same sources as Auctionator.
---
---`data` (the post-call argument) is tried first because it is the
---cheapest; then the tooltip's own `info`, which names the getter and its
---arguments; then `GetItem()`, which is nil in the auction house and the
---bank.  A battle pet is not an item and is rejected at every step.
---@param tooltip table
---@param data table?
---@return number?
local function ItemIdOf(tooltip, data)
	if type(data) == "table" then
		-- a battle pet has an id too, and it is a species id; it must not
		-- be priced as if it were an item, even when the id is present
		if type(data.hyperlink) == "string"
			and string.match(data.hyperlink, "^battlepet:") then
			return nil
		end
		local id = BaseItemId(data.hyperlink) or BaseItemId(data.id)
		if id then
			return id
		end
	end
	local info = tooltip and (tooltip.info or tooltip.processingInfo)
	if type(info) == "table" then
		if type(info.hyperlink) == "string"
			and string.match(info.hyperlink, "^battlepet:") then
			return nil
		end
		local id = BaseItemId(info.hyperlink) or BaseItemId(info.id)
		if id then
			return id
		end
		local getter = info.getterName
		local args = info.getterArgs
		if type(getter) == "string" and type(args) == "table" then
			local handler = HANDLERS[string.gsub(getter, "^Get", "Set")]
			if handler then
				local ok, result = pcall(handler, tooltip, unpack(args))
				if ok then
					id = BaseItemId(result)
					if id then
						return id
					end
				end
			end
		end
	end
	if tooltip and tooltip.GetItem then
		local _, link = tooltip:GetItem()
		local id = BaseItemId(link)
		if id then
			return id
		end
	end
	return nil
end

---A tooltip we are allowed to touch, the way Auctionator decides it.
---
---Writing into another addon's private tooltip can taint or duplicate
---lines, so only the game's own tooltips and the known "NotGameTooltip*"
---convention are accepted.  A tooltip without a name (a test harness, or
---an unnamed frame) is allowed: there is nothing to distinguish it by.
---@param tooltip table
---@return boolean
local function ValidateTooltip(tooltip)
	if not tooltip then
		return false
	end
	if tooltip == GameTooltip or tooltip == GameTooltipTooltip
		or tooltip == ItemRefTooltip then
		return true
	end
	if tooltip.IsForbidden and tooltip:IsForbidden() then
		return false
	end
	local name = tooltip.GetName and tooltip:GetName() or nil
	if not name then
		return true
	end
	return string.match(name, "^NotGameTooltip") ~= nil
		or string.match(name, "^RSMap") ~= nil
end

---Adds the estimate lines to a tooltip that is already built.
---@param tooltip table
---@param itemId number?
local function AppendLines(tooltip, itemId)
	if not private.active or not itemId then
		return
	end
	-- the post-call and the fallback hooks can both see the same tooltip;
	-- adding the block twice is worse than adding it once
	if tooltip.__dfaEstimateItem == itemId then
		return
	end
	local lines = Tooltip.GetLines(itemId)
	if #lines == 0 then
		return
	end
	tooltip.__dfaEstimateItem = itemId
	tooltip:AddLine(" ")
	for _, line in ipairs(lines) do
		tooltip:AddLine(line, 1, 1, 1)
	end
end

---Hook a method without replacing it when the client offers hooksecurefunc.
---@param object table
---@param name string
---@param handler function
local function HookMethod(object, name, handler)
	if not object or type(object[name]) ~= "function" then
		return
	end
	if hooksecurefunc then
		hooksecurefunc(object, name, handler)
		return
	end
	local original = object[name]
	object[name] = function(...)
		local result = original(...)
		handler(...)
		return result
	end
end

---Forgets the last item when the tooltip is cleared, so the same item can be
---hovered again after the tooltip is reused.
---@param tooltip table
local function ResetTooltip(tooltip)
	tooltip.__dfaEstimateItem = nil
end

---Hooks the item tooltip.
---
---The modern path is TooltipDataProcessor, registered the way Auctionator
---registers it: the callback reads `tooltip.info`, maps its getter name to a
---handler and lets the handler find the item from the getter's arguments.
---That is what makes the bags, the bank, the auction house and a merchant
---work, even when `tooltip:GetItem()` is nil.  Without the processor, the
---GameTooltip setters are hooked directly, which is Auctionator's fallback.
function Tooltip:Enable()
	private.active = true
	if private.hook then
		return
	end
	private.error = nil
	local processor = _G.TooltipDataProcessor
	local itemType = _G.Enum and Enum.TooltipDataType and Enum.TooltipDataType.Item
	if processor and processor.AddTooltipPostCall and itemType then
		-- the call itself can fail on a client whose tooltip API changed;
		-- a failure here used to fall through to the SetHyperlink branch
		-- without a word, so the reason was lost
		local ok, err = pcall(processor.AddTooltipPostCall, itemType,
			function(tooltip, data)
				-- This runs inside Blizzard's own tooltip call.  A restricted
				-- value (issecretvalue) in `data` must not raise from here:
				-- the error would surface in the client's frame, not ours.
				local read, problem = pcall(function()
					if not ValidateTooltip(tooltip) then
						return
					end
					AppendLines(tooltip, ItemIdOf(tooltip, data))
				end)
				if not read then
					private.error = "tooltip post-call: " .. tostring(problem)
				end
			end)
		if ok then
			private.processor = true
			private.hook = true
			if GameTooltip and GameTooltip.HookScript then
				GameTooltip:HookScript("OnTooltipCleared", ResetTooltip)
			end
			if ItemRefTooltip and ItemRefTooltip.HookScript then
				ItemRefTooltip:HookScript("OnTooltipCleared", ResetTooltip)
			end
			return
		end
		private.error = "TooltipDataProcessor.AddTooltipPostCall: " .. tostring(err)
	end
	local gameTooltip = _G.GameTooltip
	if not gameTooltip then
		private.error = (private.error and private.error .. "; " or "")
			.. "no GameTooltip to hook"
		return
	end
	-- Auctionator's fallback: hook the setters themselves.  It reaches the
	-- same places, one method at a time.
	local hooked = 0
	for name, handler in pairs(HANDLERS) do
		if type(gameTooltip[name]) == "function" then
			HookMethod(gameTooltip, name, function(tip, ...)
				local ok, id = pcall(handler, tip, ...)
				if ok then
					AppendLines(tip, id)
				end
			end)
			hooked = hooked + 1
		end
	end
	if ItemRefTooltip and type(ItemRefTooltip.SetHyperlink) == "function" then
		HookMethod(ItemRefTooltip, "SetHyperlink", function(tip, link)
			AppendLines(tip, BaseItemId(link))
		end)
		hooked = hooked + 1
	end
	if hooked == 0 then
		private.error = (private.error and private.error .. "; " or "")
			.. "no GameTooltip setter to hook"
		return
	end
	private.hook = true
	private.error = nil
end

---What the client actually offers, for the panel and for a bug report.
---
---"tooltip off" alone is useless: it can mean the addon did not load, the
---API is missing, or the client refused the registration.  This says which.
---@return table
function Tooltip.GetDiagnostics()
	local processor = _G.TooltipDataProcessor
	local dataType = _G.Enum and _G.Enum.TooltipDataType
	local gameTooltip = _G.GameTooltip
	return {
		hooked = private.hook == true,
		active = private.active ~= false,
		processor = private.processor == true,
		processorGlobal = type(processor) == "table",
		addPostCall = type(processor) == "table"
			and type(processor.AddTooltipPostCall) == "function" or false,
		enumItem = dataType and dataType.Item or nil,
		gameTooltip = type(gameTooltip) == "table",
		setHyperlink = type(gameTooltip) == "table"
			and type(gameTooltip.SetHyperlink) == "function" or false,
		error = private.error,
	}
end

---Whether the tooltip hook is in place.  Used by the panel, so the user
---can tell "the addon is not loaded" from "this item has no estimate".
---@return boolean
function Tooltip.IsEnabled()
	return private.hook == true and private.active == true
end

---Whether the modern (TooltipDataProcessor) path is the one in use.
---@return boolean
function Tooltip.UsesProcessor()
	return private.processor == true
end

---Stops adding the lines, without undoing the hook.
function Tooltip:Disable()
	private.active = false
end
