-- The DfA Estimate panel: the long form.
--
--     /dfaestimate
--
-- It lists the items the dataset knows about in the current realm, with
-- the estimate, the confidence, the TSM reference prices, the cosmetic
-- interest, the age of the data, the sample size and the warnings.
--
-- It is a *reading* panel.  It does not post, cancel, undercut or
-- reprice anything, and it does not write to TSM's settings.

local ADDON_NAME, Addon = ...

local Panel = Addon.DFAEstimatePanel or {}
Addon.DFAEstimatePanel = Panel

local private = {
	frame = nil,
	scroll = nil,
	text = nil,
	visible = false,
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

local FRAME_W, FRAME_H = 640, 480


---A TSM reference price, through the public API.
---@param source string The price source key, bare: "dbmarket" (TSM's custom
---price language has no braces, and a braced name is rejected)
---@param itemId number
---@return number?
local function GetTSMPrice(source, itemId)
	if not TSM_API or not TSM_API.GetCustomPriceValue then
		return nil
	end
	local ok, value = pcall(TSM_API.GetCustomPriceValue, source, "i:" .. itemId)
	if ok and type(value) == "number" and value > 0 then
		return value
	end
	return nil
end

local function Money(copper)
	if not copper or copper <= 0 then
		return "-"
	end
	return string.format("%.2fg", copper / 10000)
end


---The rows of the table.
---@return table<number, table>
local function GetRows()
	local addon = Addon.DFAEstimate
	local rows = {}
	if not addon or not addon.data then
		return rows
	end
	local items = addon.GetItems()
	local ids = {}
	for id in pairs(items) do
		ids[#ids + 1] = id
	end
	table.sort(ids)
	for _, id in ipairs(ids) do
		local record = items[id]
		rows[#rows + 1] = {
			id = id,
			record = record,
			dbmarket = GetTSMPrice("dbmarket", id),
			dbRecent = GetTSMPrice("dbrecent", id),
			dbRegionMarket = GetTSMPrice("dbregionmarketavg", id),
			dbRegionSale = GetTSMPrice("dbregionsaleavg", id),
		}
	end
	return rows
end


---A one-line report of what is and is not working.
---@return string
function Panel.BuildStatus()
	local addon = Addon.DFAEstimate
	local state = addon and addon.GetSourceState and addon.GetSourceState() or {}
	local lines = {}
	lines[#lines + 1] = ("DFA Estimate -- %s"):format(addon and addon.GetRealmName() or "?")
	if addon and addon.data then
		local age = addon.GetAgeHours()
		lines[#lines + 1] = ("dataset: version %s (engine %s), %d item(s), %.0f hour(s) old")
			:format(tostring(addon.dataVersion), tostring(addon.engineVersion),
				addon.data.itemCount or 0, age or 0)
	else
		lines[#lines + 1] = ("dataset: NOT LOADED (%s)")
			:format(tostring(addon and addon.error or "unknown"))
	end
	-- The dataset only carries the items this realm sold; the model is what
	-- lets the addon answer for every other item, so its measured accuracy
	-- is part of the status rather than a footnote.
	local model = addon and addon.data and addon.data.model
	if model and type(model.coefficients) == "table" and #model.coefficients > 0 then
		lines[#lines + 1] = ("the model: %d price(s) shipped, %.0f%% within 25%% of the sale -- "
			.. "/dfaestimate <item> or a tooltip")
			:format(addon.GetPriceCount and addon.GetPriceCount() or 0,
				(model.within25 or 0) * 100)
	else
		lines[#lines + 1] = "the model: NOT SHIPPED -- only the recorded items can be priced"
	end
	if state.forkLoaded then
		lines[#lines + 1] = ("the TSM fork: loaded (source version %s)"):format(tostring(state.version))
	else
		lines[#lines + 1] = ("the TSM fork: NOT LOADED -- run "
			.. "|cffff0000python scripts/dfa_tsm_patch.py apply|r and restart the client")
	end
	if state.registered then
		lines[#lines + 1] = "the price source: registered -- dfaestimate is available"
	else
		lines[#lines + 1] = ("the price source: NOT REGISTERED -- dfaestimate will not "
			.. "be accepted in a custom string until the fork is loaded")
	end
	if state.error then
		lines[#lines + 1] = ("the fork says: %s"):format(tostring(state.error))
	end
	local bridge = Addon.DFAEstimateAuctionator
	if bridge then
		local state_text
		if bridge.IsEnabled() then
			state_text = "FILLING the price field (turn it off with "
				.. "|cffff0000/dfaestimate fill off|r)"
		else
			state_text = "off (turn it on with "
				.. "|cffff0000/dfaestimate fill on|r)"
		end
		lines[#lines + 1] = ("the Auctionator bridge: %s"):format(state_text)
		if bridge.GetDiagnostics then
			local d = bridge.GetDiagnostics()
			lines[#lines + 1] = ("  hook: mixin=%s UpdateSalesPrice=%s "
				.. "hooked=%s, selling frame=%s"):format(
				tostring(d.mixin), tostring(d.updateSalesPrice),
				tostring(d.hooked), tostring(d.saleItemFrame))
			if d.error then
				lines[#lines + 1] = ("  error: %s"):format(tostring(d.error))
			end
			if d.lastFill then
				lines[#lines + 1] = ("  last fill: item %s -> %s"):format(
					tostring(d.lastFill.itemId), Money(d.lastFill.price))
			end
		end
	end
	local lootappraiser = Addon.DFAEstimateLootAppraiser
	if lootappraiser and not lootappraiser:Status():match("not loaded") then
		lines[#lines + 1] = lootappraiser:Status()
		if not lootappraiser:IsEnabled() then
			lines[#lines + 1] = "  turn it on with /dfaestimate lootappraiser"
		end
	end
	local tooltip = Addon.DFAEstimateTooltip
	if tooltip then
		local how
		if not tooltip.IsEnabled() then
			how = "|cffff0000OFF|r -- nothing will be added to any tooltip"
		elseif tooltip.UsesProcessor() then
			how = "ON -- every item tooltip (TooltipDataProcessor)"
		else
			how = "ON -- item tooltips that go through SetHyperlink (fallback)"
		end
		lines[#lines + 1] = ("the item tooltip: %s"):format(how)
		if not tooltip.IsEnabled() and tooltip.GetDiagnostics then
			local d = tooltip.GetDiagnostics()
			lines[#lines + 1] = ("  why: TooltipDataProcessor=%s, AddTooltipPostCall=%s, "
				.. "Enum.Item=%s, GameTooltip=%s, SetHyperlink=%s"):format(
				tostring(d.processorGlobal), tostring(d.addPostCall),
				tostring(d.enumItem), tostring(d.gameTooltip), tostring(d.setHyperlink))
			if d.error then
				lines[#lines + 1] = ("  error: %s"):format(tostring(d.error))
			end
		end
	end
	if addon and addon.panelError then
		lines[#lines + 1] = ("the panel itself failed to build: %s"):format(
			tostring(addon.panelError))
	end
	local layout = Panel.GetLayout()
	lines[#lines + 1] = ("the panel window: built=%s shown=%s strata=%s level=%s"):format(
		tostring(layout.frame ~= nil), tostring(layout.shown),
		tostring(layout.strata), tostring(layout.level))
	return table.concat(lines, "\n")
end


---The text of the panel.
---@return string
function Panel.BuildText()
	local addon = Addon.DFAEstimate
	local lines = {}
	if not addon or not addon.data then
		return "DFA Estimate: " .. tostring(addon and addon.error or "no dataset")
	end
	local age = addon.GetAgeHours()
	lines[#lines + 1] = ("DFA Estimate -- %s"):format(addon.GetRealmName())
	lines[#lines + 1] = ("dataset %s (engine %s), generated %.0f hour(s) ago")
		:format(tostring(addon.dataVersion), tostring(addon.engineVersion), age or 0)
	-- One GetRows call: it asks TSM for four references per item, and asking
	-- twice made the panel slow enough to look like it never opened.
	local rows = GetRows()
	lines[#lines + 1] = ("%d item(s) known in this realm"):format(#rows)
	lines[#lines + 1] = ""
	lines[#lines + 1] = ("%-9s %-11s %-6s %-10s %-10s %-10s %s")
		:format("item", "estimate", "conf", "dbmarket", "dbrecent", "region avg", "warnings")
	for _, row in ipairs(rows) do
		local record = row.record
		local warnings = {}
		for _, flag in ipairs(record.flags or {}) do
			warnings[#warnings + 1] = FLAG_TEXT[flag] or flag
		end
		lines[#lines + 1] = ("%-9d %-11s %-6s %-10s %-10s %-10s %s"):format(
			row.id,
			Money(record.price),
			string.format("%d%%", math.floor((record.conf or 0) * 100)),
			Money(row.dbmarket),
			Money(row.dbRecent),
			Money(row.dbRegionMarket),
			#warnings > 0 and table.concat(warnings, "; ") or "-")
	end
	lines[#lines + 1] = ""
	lines[#lines + 1] = "The estimate is what a unit is expected to fetch, net of the house"
	lines[#lines + 1] = "cut.  It is not an asking price, and it is not a promise."
	return table.concat(lines, "\n")
end


---Creates a frame with a template, then without it.
---
---The name is deliberately nil.  If a named CreateFrame fails after the
---client has already registered the name, retrying the same name raises
---"frame already exists"; an anonymous frame cannot collide with a stale
---one, with another addon, or with a frame left by a previous attempt.
---@param kind string
---@param parent table
---@param template string?
---@return table?
local function CreateFrameSafe(kind, parent, template)
	if template then
		local ok, frame = pcall(CreateFrame, kind, nil, parent, template)
		if ok and frame then
			return frame
		end
	end
	local ok, frame = pcall(CreateFrame, kind, nil, parent)
	if ok then
		return frame
	end
	return nil
end

---A plain, visible background for clients without the backdrop template.
---@param frame table
local function AddBackground(frame)
	local background = frame:CreateTexture(nil, "BACKGROUND")
	if not background then
		return
	end
	background:SetAllPoints(frame)
	if background.SetColorTexture then
		background:SetColorTexture(0.04, 0.05, 0.07, 0.96)
	else
		background:SetTexture("Interface\\Buttons\\WHITE8X8")
		if background.SetVertexColor then
			background:SetVertexColor(0.04, 0.05, 0.07, 0.96)
		end
	end
end

local function AddCloseButton(frame)
	local close = CreateFrameSafe("Button", frame, "UIPanelCloseButton")
	if not close then
		close = CreateFrameSafe("Button", frame)
	end
	if not close then
		return nil
	end
	close:SetSize(22, 22)
	close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -4, -4)
	if close.SetText then
		close:SetText("X")
	else
		local label = close:CreateFontString(nil, "OVERLAY", "GameFontNormal")
		label:SetAllPoints(close)
		label:SetText("X")
	end
	close:SetScript("OnClick", function()
		Panel.Hide()
	end)
	return close
end


function Panel.Show()
	if not private.frame and not Panel:Initialize() then
		Panel.Say("the panel could not be built: "
			.. tostring(Addon.DFAEstimate and Addon.DFAEstimate.panelError or "unknown"))
		return false
	end
	-- Show the frame *before* building the report.  The text asks TSM for a
	-- reference per item; if that is slow or raises, an empty frame is still
	-- visible and the reason is written into it, instead of the command
	-- looking as if it did nothing.
	private.frame:Show()
	if private.frame.Raise then
		private.frame:Raise()
	end
	private.visible = true
	if not private.text then
		Panel.Say("the panel opened without its text: "
			.. tostring(Addon.DFAEstimate and Addon.DFAEstimate.panelError or "unknown"))
		return false
	end
	local ok, err = pcall(function()
		private.text:SetText(Panel.BuildText())
		Panel.ResizeContent()
	end)
	if not ok then
		Addon.DFAEstimate.panelError = tostring(err)
		private.text:SetText("DFA Estimate: the report could not be built.\n"
			.. tostring(err))
		Panel.ResizeContent()
		Panel.Say("the panel opened, but its text failed: " .. tostring(err))
	end
	return true
end

function Panel.Hide()
	if not private.frame then
		return
	end
	private.frame:Hide()
	private.visible = false
end

function Panel.Toggle()
	if private.frame and private.frame.IsShown and private.frame:IsShown() then
		Panel.Hide()
	else
		Panel.Show()
	end
end


function Panel:Initialize()
	if private.frame then
		return true
	end
	-- The panel must not depend on a template whose name changed on a
	-- client: a plain frame, a background texture and a close button are
	-- enough to read the report.
	local frame = CreateFrameSafe("Frame", UIParent, "BackdropTemplate")
		or CreateFrameSafe("Frame", UIParent)
	if not frame then
		Addon.DFAEstimate.panelError = "CreateFrame returned no frame"
		return false
	end
	-- Set the fields before the rest of the build: a later failure must
	-- leave a usable, visible frame behind, not a nil `private.frame` that
	-- makes every later /dfaestimate silently retry the same failure.
	private.frame = frame
	frame:SetSize(FRAME_W, FRAME_H)
	frame:SetPoint("CENTER", UIParent, "CENTER", 0, 0)
	frame:SetMovable(true)
	frame:EnableMouse(true)
	frame:RegisterForDrag("LeftButton")
	frame:SetScript("OnDragStart", function(self)
		self:StartMoving()
	end)
	frame:SetScript("OnDragStop", function(self)
		self:StopMovingOrSizing()
	end)
	frame:SetClampedToScreen(true)
	-- Above TSM's HIGH windows and above ordinary dialogs.  A reading
	-- window that opens underneath the auction house is indistinguishable
	-- from one that never opened.
	frame:SetFrameStrata("FULLSCREEN_DIALOG")
	frame:SetFrameLevel(100)
	if frame.SetToplevel then
		frame:SetToplevel(true)
	end
	if frame.SetBackdrop then
		local ok = pcall(frame.SetBackdrop, frame, {
			bgFile = "Interface\\Buttons\\WHITE8X8",
			edgeFile = "Interface\\Buttons\\WHITE8X8",
			edgeSize = 1,
		})
		if ok then
			if frame.SetBackdropColor then
				frame:SetBackdropColor(0.04, 0.05, 0.07, 0.96)
			end
			if frame.SetBackdropBorderColor then
				frame:SetBackdropBorderColor(0.25, 0.6, 0.9, 0.9)
			end
		else
			AddBackground(frame)
		end
	else
		AddBackground(frame)
	end
	frame:Hide()

	local title = frame:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
	if title then
		title:SetPoint("TOPLEFT", frame, "TOPLEFT", 16, -12)
		title:SetText("DFA Estimate")
	end
	AddCloseButton(frame)

	local scroll = CreateFrameSafe("ScrollFrame", frame, "UIPanelScrollFrameTemplate")
		or CreateFrameSafe("ScrollFrame", frame)
	if not scroll then
		Addon.DFAEstimate.panelError = "the scroll frame could not be created"
		return false
	end
	scroll:SetPoint("TOPLEFT", 12, -36)
	scroll:SetPoint("BOTTOMRIGHT", -12, 12)

	-- SetScrollChild wants a *frame*, not a font string.  Passing the font
	-- string raised "Wrong object type for function" and the panel never
	-- opened; the text now lives inside a content frame that is sized to
	-- it, which is also what makes the scroll bar meaningful.
	local content = CreateFrameSafe("Frame", scroll)
	if not content then
		Addon.DFAEstimate.panelError = "the scroll content could not be created"
		return false
	end
	content:SetSize(FRAME_W - 44, 1)
	local text = content:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
	if not text then
		Addon.DFAEstimate.panelError = "the report text could not be created"
		return false
	end
	text:SetJustifyH("LEFT")
	text:SetWordWrap(false)
	text:SetPoint("TOPLEFT", content, "TOPLEFT", 0, 0)
	text:SetPoint("TOPRIGHT", content, "TOPRIGHT", 0, 0)
	local okChild, childError = pcall(scroll.SetScrollChild, scroll, content)
	if not okChild then
		-- No scroll bar is better than no panel: put the text directly on
		-- the frame, still readable, and record why it is not scrolling.
		Addon.DFAEstimate.panelError = "SetScrollChild: " .. tostring(childError)
		if text.ClearAllPoints then
			text:ClearAllPoints()
		end
		pcall(text.SetParent, text, frame)
		text:SetPoint("TOPLEFT", frame, "TOPLEFT", 16, -40)
		text:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -16, 16)
	end

	private.scroll = scroll
	private.content = content
	private.text = text
	return true
end

---Sizes the scroll child to the text it now holds.
---
---Without this the content frame stays one pixel high and the scroll
---frame has nothing to scroll; with it the bar matches the report.
function Panel.ResizeContent()
	if not private.content or not private.text then
		return
	end
	local width = 0
	if private.scroll and private.scroll.GetWidth then
		local ok, value = pcall(private.scroll.GetWidth, private.scroll)
		if ok and type(value) == "number" then
			width = value
		end
	end
	if width <= 0 then
		width = FRAME_W - 44
	end
	local height = 1
	if private.text.GetStringHeight then
		local ok, value = pcall(private.text.GetStringHeight, private.text)
		if ok and type(value) == "number" and value > 0 then
			height = value
		end
	end
	private.content:SetSize(width, math.max(1, height))
end

---What the panel built, for /dfaestimate debug and for tests.
---@return table
function Panel.GetLayout()
	local frame = private.frame
	local shown = frame and frame.IsShown and frame:IsShown() or false
	local strata = frame and frame.GetFrameStrata and frame:GetFrameStrata() or nil
	local level = frame and frame.GetFrameLevel and frame:GetFrameLevel() or nil
	return { frame = frame, scroll = private.scroll,
		content = private.content, text = private.text,
		shown = shown, strata = strata, level = level }
end

function Panel:RegisterEvents()
	if Addon.DFAEstimateTooltip and Addon.DFAEstimateTooltip.Enable then
		Addon.DFAEstimateTooltip:Enable()
	end
	if Addon.DFAEstimateAuctionator then
		Addon.DFAEstimateAuctionator.LoadSettings()
		Addon.DFAEstimateAuctionator:Enable()
	end
end

---One item, as a line: the price, where it came from and how sure it is.
---@param itemId number
---@return string
function Panel.DescribeItem(itemId)
	local addon = Addon.DFAEstimate
	if not addon then
		return "the addon is not loaded"
	end
	local estimate = addon.Estimate(itemId)
	if not estimate then
		return ("item %d: no estimate -- no sale here and no TSM reference "
			.. "to price it from"):format(itemId)
	end
	local where
	if estimate.source == "record" then
		where = ("%d sale(s) recorded here"):format(estimate.n or 0)
	else
		where = ("the model, from %d market reference(s)"):format(estimate.references or 0)
	end
	local range = ""
	if estimate.plow and estimate.phigh and estimate.phigh > estimate.plow then
		range = (", range %s - %s"):format(
			Money(estimate.plow), Money(estimate.phigh))
	end
	return ("item %d: %s%s (%d%% data confidence, %s)"):format(
		itemId, Money(estimate.price), range,
		math.floor((estimate.conf or 0) * 100), where)
end

---Prints what the TSM source and LootAppraiser actually return for an item.
---
---The useful question is not "is the override true?" but "what number does
---the chain produce, and where does it stop?"  This walks the whole chain:
---dataset -> addon -> TSM custom source (diagnostic only) and the
---LootAppraiser resolver -> frozen value.
---@param itemId number
function Panel.ReportSource(itemId)
	local itemString = "i:" .. tostring(itemId)
	Panel.Say(("source check: item %d"):format(itemId))

	local data = _G.DFA_ESTIMATE
	local source = _G.DFA_ESTIMATE_SOURCE
	Panel.Say(("dataset=%s, source module=%s, TSM_API=%s"):format(
		tostring(data ~= nil), tostring(source ~= nil),
		tostring(_G.TSM_API ~= nil)))

	local addon = Addon.DFAEstimate
	if addon then
		Panel.Say(Panel.DescribeItem(itemId))
	end

	if source then
		local ok, price = pcall(source.GetEstimate, itemString)
		Panel.Say(("source record: %s"):format(
			ok and (price and Money(price) or "nil") or ("error: " .. tostring(price))))
		local ok2, model = pcall(source.GetModelPrice, itemString)
		Panel.Say(("source model: %s"):format(
			ok2 and (model and Money(model) or "nil") or ("error: " .. tostring(model))))
		local ok3, frozen = pcall(source.GetFrozenPrice, itemString)
		Panel.Say(("source frozen: %s"):format(
			ok3 and (frozen and Money(frozen) or "nil") or ("error: " .. tostring(frozen))))
		local realm = source.GetRealmName and source.GetRealmName() or "?"
		local hasRealm = data and data.realms and data.realms[realm] ~= nil
		Panel.Say(("realm=%s, realm in dataset=%s"):format(
			tostring(realm), tostring(hasRealm)))
		if source.GetError and source.GetError() then
			Panel.Say("source error: " .. tostring(source.GetError()))
		end
	end

	if _G.TSM_API and _G.TSM_API.GetCustomPriceValue then
		local ok, value = pcall(_G.TSM_API.GetCustomPriceValue,
			"dfaestimate", itemString)
		Panel.Say(("TSM dfaestimate: %s"):format(
			ok and (value and Money(value) or "nil")
			or ("error: " .. tostring(value))))
	else
		Panel.Say("TSM_API.GetCustomPriceValue is missing")
	end

	local la = Addon.DFAEstimateLootAppraiser
	if la then
		Panel.Say(la:Status())
	end

	local ace = LibStub and LibStub("AceAddon-3.0", true)
	local gameLA = ace and ace.GetAddon
		and ace:GetAddon("LootAppraiser Reloaded", true)
	if gameLA and gameLA.PriceSources
		and gameLA.PriceSources.ResolveForItem then
		local base = gameLA.GetFromDb
			and gameLA.GetFromDb("pricesource", "source") or "Custom"
		local ok, resolved = pcall(gameLA.PriceSources.ResolveForItem,
			itemId, base)
		Panel.Say(("LootAppraiser resolve(%s): %s"):format(
			tostring(base), ok and tostring(resolved)
			or ("error: " .. tostring(resolved))))
		if ok and type(resolved) == "string"
			and string.lower(resolved) == "dfaestimate" then
			Panel.Say("  (recipe/weapon/armor use the frozen table directly, "
				.. "without the TSM custom source)")
		end
		if ok and resolved and gameLA.PriceSources.GetItemValue then
			local ok2, value = pcall(gameLA.PriceSources.GetItemValue,
				itemId, resolved)
			Panel.Say(("LootAppraiser value: %s"):format(
				ok2 and (value and Money(value) or "nil")
				or ("error: " .. tostring(value))))
		end
	else
		Panel.Say("LootAppraiser: not loaded")
	end
end

---Handles /dfaestimate [fill on|off] and /dfaestimate <item>.
---@param msg string
function Panel.HandleCommand(msg)
	local lower = msg and msg:lower() or ""
	if lower == "debug" then
		local addon = Addon.DFAEstimate
		local tooltip = Addon.DFAEstimateTooltip
		if tooltip and tooltip.GetDiagnostics then
			local d = tooltip.GetDiagnostics()
			Panel.Say(("tooltip: hooked=%s active=%s processor=%s global=%s "
				.. "AddTooltipPostCall=%s Enum.Item=%s GameTooltip=%s SetHyperlink=%s"):format(
				tostring(d.hooked), tostring(d.active), tostring(d.processor),
				tostring(d.processorGlobal), tostring(d.addPostCall),
				tostring(d.enumItem), tostring(d.gameTooltip), tostring(d.setHyperlink)))
			if d.error then
				Panel.Say("tooltip error: " .. tostring(d.error))
			end
		else
			Panel.Say("the tooltip module is not loaded (UI/Tooltip.lua)")
		end
		if addon and addon.tooltipError then
			Panel.Say("tooltip enable error: " .. tostring(addon.tooltipError))
		end
		if addon and addon.panelError then
			Panel.Say("panel error: " .. tostring(addon.panelError))
		end
		return
	end
	local mobinfo = Addon.DFAEstimateMobInfo2
	if lower == "mobinfo" or lower:match("^mobinfo%s") then
		if not mobinfo then
			Panel.Say("the MobInfo2 integration is not loaded")
			return
		end
		local action = lower:match("^mobinfo%s+(%S+)") or "status"
		if action == "off" or action == "revert" then
			mobinfo:Disable(true)
		elseif action ~= "status" then
			if not mobinfo:Enable(true) then
				Panel.Say("MobInfo2 is not loaded; it will be picked up when it is")
				return
			end
		end
		Panel.Say(mobinfo:Status())
		return
	end
	local lootappraiser = Addon.DFAEstimateLootAppraiser
	if lower == "lootappraiser" or lower:match("^lootappraiser%s") then
		if not lootappraiser then
			Panel.Say("the LootAppraiser integration is not loaded")
			return
		end
		local action = lower:match("^lootappraiser%s+(%S+)") or "status"
		if action == "off" or action == "revert" then
			lootappraiser:Disable(false)
		elseif action == "status" then
			Panel.Say(lootappraiser:Status())
		else
			lootappraiser:Enable(false)
			Panel.Say(lootappraiser:Status())
		end
		return
	end
	-- the full source chain, for when the overrides look right but the
	-- number does not arrive
	local sourceArg = lower:match("^source%s+(.+)$")
		or lower:match("^tsm%s+(.+)$")
	if sourceArg then
		local id = sourceArg:match("item:(%d+)")
			or sourceArg:match("^i:(%d+)") or sourceArg:match("(%d+)")
		if id then
			Panel.ReportSource(tonumber(id))
			return
		end
	end

	-- an item id, an item link pasted from the chat, or a TSM item string:
	-- the way to ask about an item the dataset has no row for.  All of them
	-- are reduced to the base item id the dataset is keyed on.
	local itemId = msg and (msg:match("item:(%d+)") or msg:match("^i:(%d+)")
		or msg:match("^(%d+)$"))
	if itemId then
		Panel.Say(Panel.DescribeItem(tonumber(itemId)))
		return
	end
	local bridge = Addon.DFAEstimateAuctionator
	if msg and (msg:lower() == "fill" or msg:lower():match("^fill%s+(%w+)")) then
		local argument = msg:lower():match("^fill%s+(%w+)")
		if bridge then
			-- `/dfaestimate fill` with no argument says what the bridge sees:
			-- "it does not work" is answerable only with these fields
			if argument == nil or argument == "status" then
				local d = bridge.GetDiagnostics()
				local fill = d.lastFill and ("item " .. tostring(d.lastFill.itemId)
					.. " -> " .. tostring(d.lastFill.price) .. "c"
					.. (d.lastFill.confirmed and " (confirmed)" or "")) or "none"
				Panel.Say(("fill: enabled=%s hooked=%s mixin=%s UpdateSalesPrice=%s "
					.. "saleItemFrame=%s userChose=%s lastFill=%s error=%s"):format(
					tostring(d.enabled), tostring(d.hooked), tostring(d.mixin),
					tostring(d.updateSalesPrice), tostring(d.saleItemFrame),
					tostring(d.userChose), fill, tostring(d.error or "none")))
				-- the last failure, with its verdict: blocked by Blizzard,
				-- rejected by Auctionator, or our own code before the call
				if d.lastError then
					Panel.Say(("lastError: step=%s api=%s item=%s attempts=%s"):format(
						tostring(d.lastError.stage), tostring(d.lastError.api or "-"),
						tostring(d.lastError.itemId), tostring(d.attempts)))
					Panel.Say("  verdict: " .. tostring(d.lastError.verdict))
					Panel.Say("  message: " .. tostring(d.lastError.message))
					local stack = d.lastError.stack or ""
					if stack ~= "" and bridge.GetDebug and bridge.GetDebug() then
						for line in stack:gmatch("[^\r\n]+") do
							Panel.Say("  stack: " .. line)
						end
					elseif stack ~= "" then
						Panel.Say("  (stack captured -- run /dfaestimate fill debug on to see it)")
					end
				end
				return
			end
			-- the step-by-step price trace: off unless asked for, so the
			-- chat stays quiet; on, it logs every estimate, every API call
			-- and any stack trace as the price is filled
			if argument == "debug" then
				local on = not (msg and msg:lower():match("^fill%s+debug%s+off"))
				bridge.SetDebug(on)
				Panel.Say("Auctionator price trace: "
					.. (on and "|cff00ff00on|r -- select an item in Auctionator's selling tab; "
						.. "each step (estimate, API called, combat state, read-back) is logged"
						or "|cffff4040off|r"))
				return
			end
			local on = argument ~= "off"
			bridge.SetEnabled(on)
			local state = on and "on" or "off"
			Panel.Say(("filling the Auctionator price field is now |cff00ff00%s|r"):format(state))
			if on then
				Panel.Say("the price field is filled when you click an item.  "
					.. "Nothing is posted: you still click Post.")
			end
		end
		return
	end
	-- the status first, in the chat, so that a problem is visible
	-- without opening a window.  Neither step may abort the other: a
	-- failing integration must not stop the panel from opening, and a
	-- failing panel must not hide the reason in chat.
	local ok, message = pcall(Panel.BuildStatus)
	if ok then
		Panel.Say(message)
	else
		Panel.Say("the status could not be built: " .. tostring(message))
	end
	local opened, err = pcall(Panel.Toggle)
	if not opened then
		Panel.Say("the panel could not open: " .. tostring(err))
	end
end

---Prints a line in the chat, without the colour codes.
---@param text string
function Panel.Say(text)
	if not DEFAULT_CHAT_FRAME then
		return
	end
	local stripped = string.gsub(text, "|c%x%x%x%x%x%x%x%x", "")
	stripped = string.gsub(stripped, "|r", "")
	DEFAULT_CHAT_FRAME:AddMessage("|cff44ddffDFA Estimate|r: " .. stripped)
end

-- The command is registered here, at file scope, and not from ADDON_LOADED.
-- A slash command is the only way in, so it must not depend on an event
-- handler that a later error could skip.  `/dfa` is deliberately NOT used:
-- DFA Auction Intelligence already owns that alias and would win the lookup.
SLASH_DFAESTIMATE1 = "/dfaestimate"
SLASH_DFAESTIMATE2 = "/dfae"
SlashCmdList["DFAESTIMATE"] = function(msg)
	Panel.HandleCommand(msg)
end
