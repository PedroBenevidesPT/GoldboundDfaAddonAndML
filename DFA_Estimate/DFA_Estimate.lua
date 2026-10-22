-- DfA Estimate: the reader of the generated dataset.
--
-- The dataset is a *build artefact* that is shipped with the addon.  The
-- addon never reaches out to a website, a database or a file: it reads
-- the Lua table that was generated offline, and refuses a version it
-- does not understand.

local ADDON_NAME, Addon = ...

local DATA_VERSION = 2
-- Schema 1 is still readable: the fields added in schema 2 are optional,
-- so an old dataset must not blank the panel.  A genuinely unknown
-- version is still refused instead of being guessed at.
local SUPPORTED_DATA_VERSIONS = { [1] = true, [2] = true }

local public = {
	version = "2.0.3",
	data = nil,
	dataVersion = nil,
	engineVersion = nil,
	generatedAt = 0,
	error = nil,
	-- why the tooltip hook or the panel could not be started; shown by
	-- /dfaestimate, so a silent "off" always has a reason attached
	tooltipError = nil,
	panelError = nil,
}

-- luacheck: globals DFA_ESTIMATE
DFA_ESTIMATE = DFA_ESTIMATE or nil

Addon.DFAEstimate = public

-- The same table under a stable global, so an addon that is not the one
-- this file belongs to can reach it.  ZoneTimerRedux values looted items
-- with it; a second reader of the dataset would drift from this one.
-- luacheck: globals DFA_ESTIMATE_API
DFA_ESTIMATE_API = public


---Reads the generated dataset.
---@return boolean success
function public.Load()
	local data = _G.DFA_ESTIMATE
	if type(data) ~= "table" then
		public.error = "the dataset was not found"
		return false
	end
	if not SUPPORTED_DATA_VERSIONS[data.version] then
		public.error = ("the dataset is version %s and this addon understands version %s")
			:format(tostring(data.version), tostring(DATA_VERSION))
		return false
	end
	public.data = data
	public.dataVersion = data.version
	public.engineVersion = data.engineVersion
	public.generatedAt = data.generatedAt or 0
	public.error = nil
	return true
end

---How old the dataset is, in hours.
---@return number?
function public.GetAgeHours()
	if not public.generatedAt or public.generatedAt == 0 or type(time) ~= "function" then
		return nil
	end
	return (time() - public.generatedAt) / 3600
end

--: What the engine itself treats as stale when the dataset is older than
--: this: PricingConfig.max_age_hours, fourteen days.  A dataset written
--: before the field existed is read with the same rule, so the addon and
--: the engine never disagree about whether a price is stale.
local DEFAULT_MAX_AGE_HOURS = 24 * 14

---The age after which a price is stale, in hours.
---@return number
function public.GetMaxAgeHours()
	local data = public.data
	local hours = tonumber(data and data.maxAgeHours)
	if hours and hours > 0 then
		return hours
	end
	return DEFAULT_MAX_AGE_HOURS
end

---Whether the dataset is older than the staleness limit.
---@return boolean
function public.IsStale()
	local age = public.GetAgeHours()
	return age ~= nil and age > public.GetMaxAgeHours()
end

---What a vendor pays for an item, in copper, or nil when it is unknown.
---
---The dataset's vendor price is the Blizzard sell price, the same number
---the tooltip reads from the client; a zero is a missing value, not a
---worthless item, so it is reported as nil.
---@param itemId number|string
---@return number?
function public.GetVendorPrice(itemId)
	local data = public.data
	local id = public.BaseItemId(itemId)
	if not data or not id then
		return nil
	end
	local record = public.GetRecord(id)
	local vendor = tonumber(record and record.vendor)
	if not vendor or vendor <= 0 then
		vendor = tonumber(data.vendors and data.vendors[id])
	end
	if vendor and vendor > 0 then
		return vendor
	end
	return nil
end

---What one unit is expected to pay on the auction house, in copper.
---
---The precedence is the engine's own and is not re-derived here: this
---realm's record (already net of the 5 % cut and of the expected deposit)
---wins, then the regional net price scaled by the realm's measured
---factor.  An item neither path can price returns nil.
---@param itemId number|string
---@return number? value
---@return string? source "record" or "regional"
function public.GetNetValue(itemId)
	local data = public.data
	local id = public.BaseItemId(itemId)
	if not data or not id then
		return nil
	end
	local record = public.GetRecord(id)
	local net = tonumber(record and record.net)
	if net and net > 0 then
		return net, "record"
	end
	local regional = tonumber(data.nets and data.nets[id])
	if not regional or regional <= 0 then
		return nil
	end
	local entry = public.GetRealmEntry()
	local factor = entry and tonumber(entry.factor) or 1
	if not factor or factor <= 0 then
		factor = 1
	end
	return math.max(1, math.floor(regional * factor + 0.5)), "regional"
end

---Everything a loot tracker needs about one item, or nil when the dataset
---cannot price it at all.
---
---``vendor`` is money a vendor pays, ``market`` is an estimate of what the
---auction house pays after the cut; they are never added together here.
---``stale`` says the evidence behind the numbers is older than the
---engine's own limit, so a caller can show it as such instead of trusting
---it silently.
---@param itemId number|string
---@return table?
function public.GetLootValues(itemId)
	local data = public.data
	local id = public.BaseItemId(itemId)
	if not data or not id then
		return nil
	end
	local vendor = public.GetVendorPrice(id)
	local market, source = public.GetNetValue(id)
	if not vendor and not market then
		return nil
	end
	local record = public.GetRecord(id)
	local seen = tonumber(record and record.seen) or public.generatedAt
	local age = nil
	if seen and seen > 0 and type(time) == "function" then
		age = (time() - seen) / 3600
	end
	return {
		itemId = id,
		vendor = vendor,
		market = market,
		source = source or (vendor and "vendor"),
		seen = seen,
		ageHours = age,
		stale = age ~= nil and age > public.GetMaxAgeHours(),
		confidence = tonumber(record and record.conf),
	}
end

---The realm the client is on.
---@return string
function public.GetRealmName()
	if not GetRealmName then
		return ""
	end
	return GetRealmName() or ""
end

---The base item id of anything that names an item.
---
---A link or a TSM item string can carry bonus ids, an item level, a
---context and so on -- ``item:1234:0:0:0:0:0:0:0`` and ``i:1234:...`` are
---all the same item.  The dataset is keyed on the base item id, which is
---the first number, so every form is reduced to it: a war-forged variant
---is priced as the item it is a variant of, not as an unknown id.
---
---Battle pets are not items here: ``p:1234`` is a species id, and the
---dataset has no pet prices.
---@param itemId number|string
---@return number?
function public.BaseItemId(itemId)
	-- a restricted value the client hands addons (issecretvalue) cannot be
	-- read, matched or compared; it is not an item id we may use
	-- luacheck: globals issecretvalue
	if type(issecretvalue) == "function" and issecretvalue(itemId) then
		return nil
	end
	if type(itemId) == "number" then
		if itemId > 0 and itemId == math.floor(itemId) then
			return itemId
		end
		return nil
	end
	if type(itemId) ~= "string" then
		return nil
	end
	local id = string.match(itemId, "item:(%d+)") or string.match(itemId, "^i:(%d+)")
	return id and tonumber(id) or nil
end

---The estimate the dataset holds for an item in the current realm.
---@param itemId number
---@return table?
function public.GetRecord(itemId)
	local data = public.data
	local id = public.BaseItemId(itemId)
	if not data or not id then
		return nil
	end
	local realmBlock = data.realms and data.realms[public.GetRealmName()]
	if not realmBlock then
		return nil
	end
	return realmBlock.items and realmBlock.items[id] or nil
end

---How many prices the dataset ships for the whole region.
---@return number
function public.GetPriceCount()
	local data = public.data
	local prices = data and data.prices
	if type(prices) ~= "table" then
		return 0
	end
	return data.priceCount or 0
end

---The items the dataset knows about in the current realm.
---@return table<number, table>
function public.GetItems()
	local data = public.data
	if not data then
		return {}
	end
	local realmBlock = data.realms and data.realms[public.GetRealmName()]
	if not realmBlock then
		return {}
	end
	return realmBlock.items or {}
end

---The Data for Azeroth interest of an appearance.
---@param appearanceId number
---@return table?
function public.GetAppearance(appearanceId)
	local data = public.data
	if not data or not appearanceId then
		return nil
	end
	return data.appearances and data.appearances[appearanceId] or nil
end

-- One TSM reference for an item, through the public API.
-- The model shipped in the dataset is written in terms of these names, so
-- the same names are read here: "dbregionmarketavg" and friends.  TSM's
-- custom price language has no braces -- a braced name is not a valid
-- price source, and GetCustomPriceValue rejects it.
---@param source string
---@param itemId number
---@return number?
local function TSMReference(source, itemId)
	if not TSM_API or not TSM_API.GetCustomPriceValue then
		return nil
	end
	local ok, value = pcall(TSM_API.GetCustomPriceValue, source, "i:" .. itemId)
	if ok and type(value) == "number" and value > 0 then
		return value
	end
	return nil
end

---Attaches the model's calibrated price range to a record.
---
---The width is a logarithm, so the range is multiplicative.  An old
---dataset has no width and simply gets no range: a missing interval is
---not a zero-width one.
---@param record table
---@param model table
---@return table record
local function AddInterval(record, model)
	local half = tonumber(model and model.intervalHalfWidth)
	local price = tonumber(record and record.price)
	if not half or half <= 0 or not price or price <= 0 then
		return record
	end
	record.plow = math.max(1, math.floor(price * math.exp(-half) + 0.5))
	record.phigh = math.max(record.plow,
		math.floor(price * math.exp(half) + 0.5))
	record.plevel = tonumber(model.intervalLevel) or 0.8
	record.pcov = tonumber(model.intervalCoverage)
	record.pintn = tonumber(model.intervalN)
	record.pmethod = model.intervalMethod
	return record
end

---The estimate for an item this realm has no recorded sale of.
---
---This is the whole point of shipping the model: the dataset can only
---carry the items that *were* traded, and the game sees every item.  The
---shipped linear model turns whatever references TSM has for the item
---into a price, using the same medians the Python side fills a missing
---reference with.  No reference at all means no estimate -- "the price
---of a typical item" is a guess, not an estimate of this one.
---@param itemId number
---@return table?
function public.EstimateFromModel(itemId)
	local data = public.data
	local id = public.BaseItemId(itemId)
	if not data or not id then
		return nil
	end
	local model = data.model
	if type(model) ~= "table" then
		return nil
	end
	local names, coefficients, medians = model.references, model.coefficients, model.medians
	if type(names) ~= "table" or type(coefficients) ~= "table"
		or #names == 0 or #coefficients ~= #names then
		return nil
	end
	medians = type(medians) == "table" and medians or {}
	local total = model.intercept or 0
	local present = 0
	for index = 1, #names do
		local value = TSMReference(names[index], id)
		if value and value > 0 then
			present = present + 1
			total = total + coefficients[index] * math.log(value + 1)
		else
			-- the median of the *logarithm*, exactly as the Python side
			-- substitutes it, so it is not transformed a second time
			total = total + coefficients[index] * (medians[index] or 0)
		end
	end
	if present == 0 then
		-- No live reference: fall back to the table the dataset ships, which
		-- is the same model applied to the region's references at build
		-- time.  It is what makes the addon useful without TSM installed.
		local frozen = data.prices and data.prices[id]
		if type(frozen) == "number" and frozen > 0 then
			local hit = model.within25
			if type(hit) ~= "number" or hit <= 0 then
				hit = 0.2
			end
			return AddInterval({
				price = frozen,
				conf = hit,
				psell = public.GetCurveProbability(1.0),
				tts = nil,
				n = 0,
				seen = 0,
				-- not a warning: `source` already says this came from the
				-- model, and the tooltip prints that as the evidence
				flags = {},
				references = 0,
				source = "table",
			}, model)
		end
		return nil
	end
	local price = math.exp(total)
	if not (price > 0) or price == math.huge then
		return nil
	end
	-- the measured hit rate, shipped with the model; a made-up 50 % would
	-- be worse than a low, honest number
	local confidence = model.within25
	if type(confidence) ~= "number" or confidence <= 0 then
		confidence = 0.2
	end
	return AddInterval({
		price = math.floor(price + 0.5),
		conf = confidence,
		psell = public.GetCurveProbability(1.0),
		-- nothing can be said about a time to sale for an item with no
		-- local history, so the field stays empty rather than invented
		tts = nil,
		n = 0,
		seen = 0,
		flags = {},
		references = present,
		source = "model",
	}, model)
end

---The per-realm line the dataset ships: a price factor and the measured
---undercut pressure.  Present for every realm the client knows, even the
---ones the account never traded in.
---@param realm string?
---@return table?
function public.GetRealmEntry(realm)
	local data = public.data
	if not data or type(data.realmIndex) ~= "table" then
		return nil
	end
	return data.realmIndex[realm or public.GetRealmName()]
end

---A record from one realm's own sales, if the dataset has one.
---@param itemId number
---@param realm string
---@return table?
local function RealmRecord(itemId, realm)
	local data = public.data
	local block = data and data.realms and data.realms[realm]
	return block and block.items and block.items[itemId] or nil
end

---The estimate for an item in a given realm.
---
---Order: that realm's own sale record, then the shipped model with the
---realm factor applied.  The factor is measured against the *regional*
---references the model is written in, so applying it to the model output
---does not double count the realm's market.
---@param itemId number
---@param realm string?
---@return table?
function public.EstimateForRealm(itemId, realm)
	realm = realm or public.GetRealmName()
	local id = public.BaseItemId(itemId)
	if not id then
		return nil
	end
	local record = RealmRecord(id, realm)
	if record and record.price and record.price > 0 then
		local copy = {}
		for key, value in pairs(record) do
			copy[key] = value
		end
		copy.source = "record"
		return copy
	end
	local estimate = public.EstimateFromModel(id)
	if not estimate then
		return nil
	end
	local entry = public.GetRealmEntry(realm)
	local factor = entry and tonumber(entry.factor) or 1
	if factor and factor > 0 and factor ~= 1 then
		estimate.price = math.max(1, math.floor(estimate.price * factor + 0.5))
		estimate.factor = factor
		-- the range is multiplicative, so the realm factor scales both
		-- ends with the price instead of widening it
		if estimate.plow then
			estimate.plow = math.max(1, math.floor(estimate.plow * factor + 0.5))
		end
		if estimate.phigh then
			estimate.phigh = math.max(estimate.plow or 1,
				math.floor(estimate.phigh * factor + 0.5))
		end
		-- A factor off two sales is a hint, not evidence: the confidence
		-- shrinks with the sample instead of pretending to be measured.
		local sample = entry and tonumber(entry.sample) or 0
		local trust = math.min(1, 0.5 + 0.5 * math.min(1, sample / 10))
		estimate.conf = (estimate.conf or 0.2) * trust
	end
	return estimate
end

---The best estimate the data can give for an item, in copper.
---
---A record from this realm's own sales wins; otherwise the shipped model.
---@param itemId number
---@return table?
function public.Estimate(itemId)
	return public.EstimateForRealm(itemId, public.GetRealmName())
end

---How much of the market undercuts an ask, in this realm.
---
---Returns ``share`` (0..1, of the listings the addon could see),
---``risk24h`` (0..1, only when two auctions snapshots exist), and the
---number of listings behind the share.  Anything unmeasured is nil.
---@param realm string?
---@return number? share
---@return number? risk24h
---@return number? watched
function public.BabysitProbability(realm)
	local entry = public.GetRealmEntry(realm)
	if not entry then
		return nil, nil, nil
	end
	local share = tonumber(entry.undercutShare)
	local risk = tonumber(entry.babysit24h)
	local watched = tonumber(entry.undercutWatched)
	return share, risk, watched
end

---The realm that would pay most for an item, if it sells there.
---
---Price comes from the realm's record or regional price x factor; the
---score discounts that by the measured undercut share, because a price
---that gets undercut is not collected.  It is a price-and-competition
---ranking, not a probability of sale -- there is no per-realm sale model.
---@param itemId number
---@return table?
function public.BestRealm(itemId)
	local data = public.data
	local id = public.BaseItemId(itemId)
	if not data or not id then
		return nil
	end
	local base = public.EstimateFromModel(id)
	if not base or not base.price or base.price <= 0 then
		return nil
	end
	local current = public.GetRealmName()
	local currentPrice = nil
	local best, bestScore = nil, nil
	for realm, entry in pairs(data.realmIndex or {}) do
		local record = RealmRecord(id, realm)
		local price = record and tonumber(record.price) or 0
		if price <= 0 then
			price = math.floor(base.price * (tonumber(entry.factor) or 1) + 0.5)
		end
		local share = tonumber(entry.undercutShare) or 0
		local score = price * (1 - 0.5 * share)
		if realm == current then
			currentPrice = price
		end
		local measured = entry.source == "realm"
			or tonumber(entry.undercutWatched) ~= nil
		-- 259 realms price at the regional baseline and tie exactly; a
		-- measured realm wins a tie, and the name keeps the answer stable
		-- from one hover to the next (pairs() is unordered).
		local better = bestScore == nil
			or score > bestScore + 1
			or (math.abs(score - bestScore) <= 1 and measured and not best.measured)
			or (math.abs(score - bestScore) <= 1 and measured == best.measured
				and realm < best.realm)
		if price > 0 and better then
			best, bestScore = {
				realm = realm,
				price = price,
				share = share,
				risk24h = tonumber(entry.babysit24h),
				measured = measured,
			}, score
		end
	end
	if not best then
		return nil
	end
	best.current_price = currentPrice
	best.gain = (currentPrice and currentPrice > 0)
		and (best.price - currentPrice) / currentPrice or nil
	return best
end

---The measured sale rate for an ask, as a fraction of the regional sale
---average.
---@param ratio number
---@return number?
function public.GetCurveProbability(ratio)
	local data = public.data
	if not data or not data.curve or not ratio then
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

---The state of the TSM fork's price source, as far as the game can see.
---@return table status
function public.GetSourceState()
	local status = {
		forkLoaded = false,
		registered = false,
		error = nil,
		version = nil,
		datasetVersion = nil,
	}
	-- luacheck: globals DFA_ESTIMATE_SOURCE
	local source = _G.DFA_ESTIMATE_SOURCE
	if type(source) == "table" then
		status.forkLoaded = true
		status.version = source.version
		status.error = source.GetError and source.GetError() or nil
	end
	-- the public API lists the registered sources; the key is lower case
	if TSM_API and TSM_API.GetPriceSourceKeys then
		local keys = {}
		TSM_API.GetPriceSourceKeys(keys)
		for _, key in ipairs(keys) do
			if string.lower(key) == "dfaestimate" then
				status.registered = true
			end
		end
	end
	status.datasetVersion = public.dataVersion
	return status
end

public.Load()

-- The addon has to be told when it is loaded before it can register a
-- slash command, read its settings or hook anything.  Nothing calls a
-- mixin's RegisterEvents by itself, so the addon owns its own event
-- frame -- without this the /dfaestimate command simply does not exist.
local eventFrame = CreateFrame("Frame")
eventFrame:RegisterEvent("ADDON_LOADED")
eventFrame:RegisterEvent("PLAYER_LOGIN")
eventFrame:SetScript("OnEvent", function(_, event, loadedName)
	if event == "ADDON_LOADED" then
		if loadedName ~= ADDON_NAME then
			return
		end
		eventFrame:UnregisterEvent("ADDON_LOADED")
		public.Load()
		-- The tooltip is hooked *first*, and independently of the panel.
		-- The panel builds a frame, and a frame can fail on a client whose
		-- templates changed; when that happened the error aborted this
		-- handler before RegisterEvents, so the hook was never installed
		-- and the addon said "tooltip off" without a reason.
		if Addon.DFAEstimateTooltip and Addon.DFAEstimateTooltip.Enable then
			local ok, err = pcall(Addon.DFAEstimateTooltip.Enable, Addon.DFAEstimateTooltip)
			if not ok then
				public.tooltipError = tostring(err)
			end
		end
		if Addon.DFAEstimatePanel then
			local ok, err = pcall(Addon.DFAEstimatePanel.Initialize, Addon.DFAEstimatePanel)
			if not ok then
				public.panelError = tostring(err)
			end
			local ok2, err2 = pcall(Addon.DFAEstimatePanel.RegisterEvents,
				Addon.DFAEstimatePanel)
			if not ok2 then
				public.panelError = public.panelError or tostring(err2)
			end
		end
	elseif event == "PLAYER_LOGIN" then
		if Addon.DFAEstimateAuctionator then
			-- The Auctionator tab may be created after this addon is loaded, so
			-- retry its bridge after entering the world.
			Addon.DFAEstimateAuctionator:Enable()
		end
		-- LootAppraiser is loaded by then, and its profile is ready; point
		-- its Recipe, Weapon and Armor classes at the DfA source.  An
		-- explicit "off" in /dfaestimate lootappraiser off is remembered.
		if Addon.DFAEstimateLootAppraiser then
			local ok, err = pcall(Addon.DFAEstimateLootAppraiser.OnLogin,
				Addon.DFAEstimateLootAppraiser, false)
			if not ok then
				public.lootappraiserError = tostring(err)
			end
		end
		-- One line, once per login.  Without it there is no way to tell
		-- "the addon is running and this item has no estimate" from
		-- "the addon did not load at all" -- both look like a silent
		-- tooltip.  It goes straight to the chat frame, not through the
		-- panel, so it still appears if a UI file failed.
		if DEFAULT_CHAT_FRAME then
			local tooltip = Addon.DFAEstimateTooltip
			local state
			if not public.data then
				state = "|cffff0000the dataset did not load|r: " .. tostring(public.error)
			elseif tooltip and tooltip.IsEnabled and tooltip.IsEnabled() then
				state = "tooltip on"
			elseif public.tooltipError then
				state = "|cffff0000tooltip off|r: " .. tostring(public.tooltipError)
			else
				state = "|cffff0000tooltip off|r (run /dfaestimate debug)"
			end
			DEFAULT_CHAT_FRAME:AddMessage(("|cff44ddffDFA Estimate|r: loaded -- "
				.. "%d price(s), %s.  /dfaestimate for the panel")
				:format(public.data and public.data.priceCount or 0, state))
		end
	end
end)
