-- The Auctionator bridge.
--
-- When the seller selects an item in Auctionator's selling tab, the DfA
-- estimate for that item goes into the buyout price field.  It is a
-- fill, not a lock: the player can type another price, or pick one from
-- Auctionator's price list, and the bridge leaves it alone.
--
-- What it does:
--   * reads the estimate for the item the frame is showing;
--   * puts it in the price field, and puts it back when Auctionator
--     overwrites it with a price of its own;
--   * says, in the chat, what it did and what it saw.
--
-- What it deliberately does not do:
--   * it does not post.  The user still clicks Post;
--   * it does not change Auctionator's undercut preference, and it does
--     not cancel or undercut anything;
--   * it never overrides a price the user chose afterwards.
--
-- Three things had to be right for the fill to be visible at all:
--
--   * Auctionator sets the price more than once for one item: from its
--     own database, from the market it just searched, and from the
--     vendor price of a piece of equipment.  Those arrive *after* the
--     item is selected, so a fill that happens once is gone a second
--     later.  Every price Auctionator sets is therefore checked, and the
--     estimate wins -- unless the player chose a price.
--
--   * The estimate is the price a buyer pays.  The 5 % cut comes off it
--     afterwards, so dividing it by 0.95 would ask for more than the
--     model says the item is worth.
--
--   * The event that says "the player picked a price" arrives before the
--     frame is told about it.  The event bus is one table for the whole
--     addon, so wrapping its Fire is the one place where the order is
--     right: the flag is set before the frame handles the event, and the
--     estimate does not win over a price the player chose.

local ADDON_NAME, Addon = ...

local Auctionator = Addon.DFAEstimateAuctionator or {}
Addon.DFAEstimateAuctionator = Auctionator

local private = {
	hooked = false,        -- the mixin hook is in place
	fireHooked = false,    -- the event bus wrapper is in place
	filling = false,       -- the re-entrancy guard
	enabled = true,        -- a fill is the point of the bridge
	itemId = nil,          -- the item the frame is showing
	price = nil,           -- the price we put in the field
	userChose = false,     -- the player chose a price for this item
	programmatic = false,  -- a price is being set by code, not by a player
	lastFill = nil,
	error = nil,
	watch = nil,
	debug = false,         -- verbose chat logging of every step
	seq = 0,               -- how many fills have been attempted this session
	lastError = nil,       -- the last failure: stage, message, stack, verdict
	lastWarning = nil,     -- de-duplication key for the always-on failure line
}


---A chat line for the debug trace.  Off by default; /dfaestimate fill debug on turns it on.
---@param text string
local function Log(text)
	if not private.debug then
		return
	end
	if not DEFAULT_CHAT_FRAME then
		return
	end
	DEFAULT_CHAT_FRAME:AddMessage("|cff44ddffDFA Estimate|r |cffffff00[auctionator]|r: " .. tostring(text))
end

---An always-on failure line, tagged so a pasted chat log is unambiguous.
---It prints only when something actually failed and only when the failure
---is new, so a price field updated several times a second cannot flood
---the chat.
---@param text string
local function Warn(text)
	local signature = tostring(text)
	if private.lastWarning == signature then
		return
	end
	private.lastWarning = signature
	if not DEFAULT_CHAT_FRAME then
		return
	end
	DEFAULT_CHAT_FRAME:AddMessage("|cff44ddffDFA Estimate|r |cffff5050[auctionator]|r " .. text)
end

---Combat state, as context for a blocked call.  Never raises: a client
---without the API just reports "unknown".
---@return string
local function Combat()
	if type(InCombatLockdown) == "function" then
		local ok, value = pcall(InCombatLockdown)
		if ok then
			return value and "in-combat" or "out-of-combat"
		end
	end
	return "combat-unknown"
end

---A name for the frame, so two sale item frames are distinguishable.
---@param frame table
---@return string
local function FrameId(frame)
	if type(frame) == "table" and type(frame.GetName) == "function" then
		local ok, name = pcall(frame.GetName, frame)
		if ok and type(name) == "string" and name ~= "" then
			return name
		end
	end
	return "sale-item-frame"
end

---Which side refused the price, from the stage it failed in and the error
---text.  "taint / protected / blocked / forbidden / secure" wording is
---Blizzard's protected-action rule (which is never bypassed here); a raise
---inside the price call is the API itself; anything earlier never reached
---Auctionator at all.  The verdict is what a bug report should quote.
---@param stage string
---@param err any
---@return string
local function Verdict(stage, err)
	local text = string.lower(tostring(err))
	local blocked = string.find(text, "taint", 1, true)
		or string.find(text, "protected", 1, true)
		or string.find(text, "blocked", 1, true)
		or string.find(text, "forbidden", 1, true)
		or string.find(text, "secure", 1, true)
	if stage == "price_call" and blocked then
		return "BLIZZARD PROTECTED-ACTION / TAINT BLOCK (not bypassed)"
	elseif stage == "price_call" then
		return "the price API call itself raised (Auctionator/Blizzard side)"
	end
	return "failed before the price call (our code, stage " .. tostring(stage) .. ")"
end

---Calls one price API under xpcall so a failure keeps the stack of the
---failing frame, not only its message.  Lua 5.1-compatible: the call runs
---inside a closure, so no arguments are passed through xpcall itself.
---@param fn function
---@param receiver table
---@param arg number
---@return boolean ok
---@return table? detail { message, stack }
local function CallPrice(fn, receiver, arg)
	local ok, problem = xpcall(function()
		return fn(receiver, arg)
	end, function(err)
		local stack = ""
		if type(debugstack) == "function" then
			local okStack, text = pcall(debugstack)
			if okStack then
				stack = tostring(text)
			end
		end
		return { message = tostring(err), stack = stack }
	end)
	if ok then
		return true, nil
	end
	local detail = problem
	if type(detail) ~= "table" then
		detail = { message = tostring(detail), stack = "" }
	end
	return false, detail
end

---Remembers the last failure for /dfaestimate fill status and prints it
---once with its verdict.  The stack goes to the debug trace only, so a
---failure costs one line by default and a full trace on demand
---(/dfaestimate fill debug on).
---@param stage string
---@param itemId number?
---@param detail table { message, stack }
---@param api string? the API that was being called
local function RecordFailure(stage, itemId, detail, api)
	local verdict = Verdict(stage, detail.message)
	private.lastError = {
		stage = tostring(stage),
		api = tostring(api or ""),
		itemId = itemId,
		message = tostring(detail.message),
		stack = tostring(detail.stack or ""),
		verdict = verdict,
	}
	Warn(("|cffffff00step=%s api=%s item=%s combat=%s -- %s: %s"):format(
		tostring(stage), tostring(api or "-"), tostring(itemId),
		Combat(), verdict, tostring(detail.message)))
	if private.debug and type(detail.stack) == "string"
			and detail.stack ~= "" then
		for line in detail.stack:gmatch("[^\r\n]+") do
			Log("  stack: " .. line)
		end
	end
end

---Auctionator's own events, read from Auctionator itself, so that a
---renamed event is a missing feature rather than a silent one.
---@return table?
local function Events()
	local addon = _G.Auctionator
	return addon and addon.Selling and addon.Selling.Events or nil
end

---The item the frame is showing, as an item id.
---
---Auctionator's itemInfo carries `itemID` directly.  The old code asked
---`C_Item.GetItemInfo(location, "itemLink")`, which returns the *name*,
---not the link, so the match below never found an id and nothing was
---ever filled.
---@param frame table
---@return number?
local function GetItemId(frame)
	local itemInfo = frame and frame.itemInfo
	if not itemInfo then
		return nil
	end
	local id = tonumber(itemInfo.itemID)
	if id and id > 0 then
		return id
	end
	local link = itemInfo.itemLink
	if type(link) ~= "string" and itemInfo.location then
		if C_Item and C_Item.GetItemLink then
			link = C_Item.GetItemLink(itemInfo.location)
		end
		if type(link) ~= "string" and C_Item and C_Item.GetItemInfo then
			-- GetItemInfo returns name, link, ...; the second value is the
			-- link.  The first value is kept as a last resort for a client
			-- (or a test) that answers with the link itself.
			local name, itemLink = C_Item.GetItemInfo(itemInfo.location)
			link = itemLink or name
		end
	end
	if type(link) ~= "string" then
		return nil
	end
	-- a number, not a string: the dataset is keyed by the numeric id and
	-- a Lua table does not treat "1234" and 1234 as the same key
	local match = link:match("item:(%d+)") or link:match("Hitem:(%d+)")
	return match and tonumber(match) or nil
end

---Auctionator's own price normalisation: the auction house does not take
---copper, so a price is rounded up to a whole silver and is at least one
---silver.  The same rule is applied here, so the field shows exactly
---what would be posted.
---@param price number
---@return number
local function NormalizePrice(price)
	local normalized = math.floor(tonumber(price) or 0)
	if normalized % 100 ~= 0 then
		normalized = normalized + (100 - normalized % 100)
	end
	if normalized < 100 then
		normalized = 100
	end
	return normalized
end

---The price to post at for an item, or nil.
---
---The action is the model's own decision: "post now at X", or "match the
---cheapest at Y".  Its ask is what it decided to post at, so it is used
---first; the plain estimate is the fallback for an item the model has no
---action for.
---@param record table
---@return number? price
---@return string? source
local function AskPrice(record)
	local action = record and record.action
	local ask = tonumber(action and action.ask)
	if ask and ask > 0 then
		return ask, "action"
	end
	-- a "wait" action says what it would post at once the market clears
	local projected = tonumber(action and action.projected_ask)
	if projected and projected > 0 then
		return projected, "action"
	end
	local price = tonumber(record and record.price)
	if price and price > 0 then
		return price, record.source
	end
	return nil
end

---The widget the buyout price lives in.
---@param frame table
---@return table?
local function PriceWidget(frame)
	return frame and frame.Price or nil
end

---The box the player types a price into.
---@param frame table
---@return table?
local function GoldBox(frame)
	local widget = PriceWidget(frame)
	local money = widget and widget.MoneyInput
	if not money then
		return nil
	end
	if money.GoldBox then
		return money.GoldBox
	end
	if money.Gold and money.Gold.GoldBox then
		return money.Gold.GoldBox
	end
	return nil
end

---Watches the price box, so a price the player types wins over ours.
---
---The box is watched for a text change: a change that is not one of ours
---is the player typing.  The widget's SetAmount is wrapped to tell the
---two apart, because Auctionator sets the price from code as well.
---@param frame table
local function WatchPriceBox(frame)
	local widget = PriceWidget(frame)
	if not widget then
		return
	end
	if not widget.__dfaSetAmount and type(widget.SetAmount) == "function" then
		local original = widget.SetAmount
		widget.__dfaSetAmount = original
		Log(("watch: wrapping Price:SetAmount frame=%s"):format(FrameId(frame)))
		widget.SetAmount = function(self, amount)
			private.programmatic = true
			local ok, err = pcall(original, self, amount)
			private.programmatic = false
			if not ok then
				-- Auctionator's own write failing is reported, not hidden
				RecordFailure("auctionator_setamount", private.itemId,
					{ message = tostring(err), stack = "" }, "Price:SetAmount")
				error(err, 0)
			end
		end
	end
	local box = GoldBox(frame)
	if box and not box.__dfaWatched then
		box.__dfaWatched = true
		local previous = box:GetScript("OnTextChanged")
		Log(("watch: OnTextChanged installed on the gold box frame=%s"):format(FrameId(frame)))
		box:SetScript("OnTextChanged", function(self, text)
			if not private.programmatic then
				-- the player is typing: their price wins
				Log(("text change from the player: %s (their price wins)"):format(tostring(text)))
				private.userChose = true
				if private.lastFill then
					private.lastFill.confirmed = false
				end
			end
			if previous then
				previous(self, text)
			end
		end)
	end
end

---What is in the price field right now, or nil when the widget cannot
---answer.  The read-back is what tells "our call raised" from "the field
---kept Auctionator's value anyway" (a write the client refused, or a
---price Auctionator set again straight after ours).
---@param frame table
---@return number?
local function FieldValue(frame)
	local widget = PriceWidget(frame)
	local getter = widget and widget.GetAmount
	if type(getter) == "function" then
		local ok, value = pcall(getter, widget)
		if ok and type(value) == "number" then
			return value
		end
	end
	return nil
end

---Puts the estimate in the price field.
---@param frame table
---@return boolean filled
function Auctionator.ApplyEstimate(frame)
	private.error = nil
	private.seq = private.seq + 1
	Log(("apply #%d: entry enabled=%s filling=%s userChose=%s combat=%s frame=%s"):format(
		private.seq, tostring(private.enabled), tostring(private.filling),
		tostring(private.userChose), Combat(), FrameId(frame)))
	if not private.enabled or private.filling then
		Log("apply: skipped (disabled or filling)")
		return false
	end
	if not frame then
		Log("apply: skipped (no frame)")
		return false
	end
	local addon = Addon.DFAEstimate
	if not addon or not addon.data then
		Log("apply: skipped (no dataset)")
		return false
	end
	local itemId = GetItemId(frame)
	if not itemId then
		-- an empty frame is normal; a frame with an item but no id is not
		if frame.itemInfo then
			private.error = "the selected item has no item id"
			Log("apply: skipped (no item id)")
		end
		return false
	end
	-- Estimate, not GetRecord: the dataset only carries the items this
	-- realm has seen sold, and the model prices the rest from the market
	-- references TSM already has.
	local record = addon.Estimate(itemId)
	if not record then
		Log(("apply: skipped (no estimate for item %d)"):format(itemId))
		return false
	end
	local price, source = AskPrice(record)
	if not price then
		Log(("apply: skipped (no ask price for item %d)"):format(itemId))
		return false
	end
	-- The estimate as the model produced it, before normalisation: this is
	-- the line that shows a wrong number was already wrong in our own data,
	-- before Auctionator ever saw it.
	Log(("apply #%d: estimate item=%d link=%s price=%s source=%s conf=%s n=%s ask=%s"):format(
		private.seq, itemId, tostring(frame.itemInfo and frame.itemInfo.itemLink),
		tostring(price), tostring(source or record.source), tostring(record.conf),
		tostring(record.n), tostring(record.action and record.action.ask)))
	price = NormalizePrice(price)
	if price <= 0 then
		Log(("apply: skipped (price <= 0 after normalize for item %d)"):format(itemId))
		return false
	end
	WatchPriceBox(frame)
	local widget = PriceWidget(frame)
	local ok, err
	local api
	private.filling = true
	if widget and type(widget.SetAmount) == "function" then
		api = "Price:SetAmount"
	elseif type(frame.UpdateSalesPrice) == "function" then
		-- a client without the money widget: the mixin still sets it
		api = "frame:UpdateSalesPrice"
	else
		private.filling = false
		private.error = "the sale item frame has no price field"
		Log("apply: skipped (no price field on frame)")
		return false
	end
	Log(("apply #%d: calling %s item=%d price=%d combat=%s"):format(
		private.seq, api, itemId, price, Combat()))
	if api == "Price:SetAmount" then
		ok, err = CallPrice(widget.SetAmount, widget, price)
	else
		ok, err = CallPrice(frame.UpdateSalesPrice, frame, price)
	end
	private.filling = false
	if not ok then
		private.error = tostring(err and err.message)
		Log(("apply #%d: %s failed item=%d err=%s"):format(
			private.seq, api, itemId, tostring(err and err.message)))
		RecordFailure("price_call", itemId, err, api)
		return false
	end
	private.itemId = itemId
	private.price = price
	private.lastFill = {
		itemId = itemId,
		price = price,
		confidence = record.conf,
		sampleSize = record.n,
		source = source or record.source,
		confirmed = not private.userChose,
	}
	-- Did the write land?  Reading the field back separates "our call
	-- raised" from "the client or Auctionator kept its own value".  A
	-- mismatch is a failure line, not a silent wrong price.
	local readBack = FieldValue(frame)
	Log(("apply #%d: FILLED item=%d price=%d source=%s field reads back %s"):format(
		private.seq, itemId, price, tostring(source or record.source),
		tostring(readBack)))
	if readBack ~= nil and readBack ~= price then
		RecordFailure("read_back", itemId, {
			message = ("wrote %d, the field still reads %s"):format(price, tostring(readBack)),
			stack = "",
		}, api)
	end
	return true
end

---Auctionator set the price.  The estimate goes back in, unless the
---player chose a price of their own.
---@param frame table
function Auctionator.OnPriceSet(frame)
	Log(("onpriceset: entry enabled=%s filling=%s userChose=%s frame=%s"):format(
		tostring(private.enabled), tostring(private.filling), tostring(private.userChose),
		tostring(type(frame) == "table")))
	if not private.enabled or private.filling then
		Log("onpriceset: skipped (disabled or filling)")
		return
	end
	if not frame then
		Log("onpriceset: skipped (no frame)")
		return
	end
	if private.userChose then
		-- The player picked this price, either by typing it in or by
		-- clicking a row in Auctionator's price list.  Auctionator is
		-- writing that choice now; putting the estimate back would
		-- silently undo it, so the fill stops here until the item changes.
		Log("onpriceset: skipped (player chose a price for this item)")
		return
	end
	if not GetItemId(frame) then
		-- an empty frame: there is nothing to price
		Log("onpriceset: skipped (no item id)")
		return
	end

	Log("onpriceset: calling ApplyEstimate")
	Auctionator.ApplyEstimate(frame)
end

---Whether the bridge is filling the price field.
---@return boolean
function Auctionator.IsEnabled()
	return private.enabled
end

---Turns the bridge on or off.
---@param enabled boolean
function Auctionator.SetEnabled(enabled)
	private.enabled = enabled and true or false
	Auctionator.SaveSettings()
end

---Turns verbose chat logging of the bridge on or off.
---@param enabled boolean
function Auctionator.SetDebug(enabled)
	private.debug = enabled and true or false
end

---Whether verbose chat logging is on.
---@return boolean
function Auctionator.GetDebug()
	return private.debug == true
end

---The last fill, for the status line.
---@return table?
function Auctionator.GetLastFill()
	return private.lastFill
end

---What the bridge sees, for the panel and for a bug report.
---@return table
function Auctionator.GetDiagnostics()
	local mixin = _G.AuctionatorSaleItemMixin
	local tab = _G.AuctionatorSellingFrame
	return {
		enabled = private.enabled,
		hooked = private.hooked,
		fireHooked = private.fireHooked,
		mixin = type(mixin) == "table",
		updateSalesPrice = type(mixin) == "table"
			and type(mixin.UpdateSalesPrice) == "function" or false,
		tab = type(tab) == "table",
		saleItemFrame = type(tab) == "table" and tab.SaleItemFrame ~= nil,
		itemId = private.itemId,
		userChose = private.userChose,
		lastFill = private.lastFill,
		debug = private.debug == true,
		error = private.error,
		attempts = private.seq,
		lastError = private.lastError,
	}
end

---Watches what happens in Auctionator's selling tab.
---
---The event bus is one table for the whole addon, so wrapping its Fire is
---the one place where the order is right: the flag for "the player chose
---a price" has to be set *before* the frame handles the event, or the
---estimate would win over a price the player chose.
---@return boolean hooked
local function HookEventBus()
	if private.fireHooked then
		return true
	end
	local addon = _G.Auctionator
	local bus = addon and addon.EventBus
	Log(("hookbus: Auctionator=%s EventBus=%s"):format(
		tostring(type(addon) == "table"), tostring(type(bus) == "table")))
	if type(bus) ~= "table" or type(bus.Fire) ~= "function" then
		Log("hookbus: EventBus.Fire missing, not hooked yet")
		return false
	end
	local original = bus.Fire
	bus.Fire = function(self, source, eventName, ...)
		Log(("bus: event=%s source=%s"):format(tostring(eventName), tostring(type(source))))
		local events = Events()
		if events then
			if eventName == events.BagItemClicked
					or eventName == events.ClearBagItem then
				-- a new item: a price the player chose belonged to the
				-- previous one
				private.itemId = nil
				private.price = nil
				private.userChose = false
				if private.lastFill then
					private.lastFill.confirmed = false
				end
			elseif eventName == events.PriceSelected then
				-- the player picked a row from Auctionator's price list.
				-- That is a choice of theirs, exactly like typing in the
				-- box: Auctionator is about to set the price they picked,
				-- and our fill must not put the estimate back on top of it.
				private.userChose = true
				if private.lastFill then
					private.lastFill.confirmed = false
				end
			end
		end
		return original(self, source, eventName, ...)
	end
	private.fireHooked = true
	Log("hookbus: EventBus.Fire wrapped")
	return true
end

---Waits for Auctionator to build the selling tab.
---
---Auctionator creates its tabs with the auction-house frame, which only
---exists once the player opens the auction house -- long after login.
function Auctionator.WatchForFrame()
	if private.watch then
		return
	end
	local watch = CreateFrame("Frame")
	watch:RegisterEvent("AUCTION_HOUSE_SHOW")
	watch:SetScript("OnEvent", function()
		Auctionator:Enable()
	end)
	private.watch = watch
end

---Hooks the selling tab, without replacing its method.
---@return boolean hooked
function Auctionator:Enable()
	Log(("enable: enabled=%s hooked=%s fireHooked=%s"):format(
		tostring(private.enabled), tostring(private.hooked), tostring(private.fireHooked)))
	if not HookEventBus() then
		-- Auctionator is not loaded yet: the watcher retries
		Log("enable: HookEventBus failed (Auctionator not ready), watching AUCTION_HOUSE_SHOW")
		Auctionator.WatchForFrame()
		return false
	end
	Log("enable: HookEventBus ok")
	if private.hooked then
		Log("enable: already hooked")
		return true
	end
	local mixin = _G.AuctionatorSaleItemMixin
	Log(("enable: mixin=%s UpdateSalesPrice=%s"):format(
		tostring(type(mixin) == "table"), tostring(type(mixin) == "table" and type(mixin.UpdateSalesPrice) == "function")))
	-- A post-hook is safe: it runs after Auctionator's own update and does
	-- not replace anything, so an error here cannot stop the selling tab.
	-- The frames hold the same function the mixin has, so the hook reaches
	-- the frames that already exist as well as the ones built later.
	if type(mixin) == "table" and type(mixin.UpdateSalesPrice) == "function"
			and type(hooksecurefunc) == "function" then
		-- The arguments are logged so the chat trace shows the price
		-- Auctionator set, in order, around every reseed of ours: that
		-- sequence is what distinguishes "Auctionator overwrote us" from
		-- "our fill never arrived".
		hooksecurefunc(mixin, "UpdateSalesPrice",
			function(self, salesPrice, bidPrice, preserveBidPrice)
			Log(("hook: Auctionator UpdateSalesPrice item=%s price=%s bid=%s preserve=%s combat=%s"):format(
				tostring(GetItemId(self)), tostring(salesPrice),
				tostring(bidPrice), tostring(preserveBidPrice), Combat()))
			local ok, err = pcall(Auctionator.OnPriceSet, self)
			if not ok then
				private.error = tostring(err)
				RecordFailure("onpriceset", GetItemId(self),
					{ message = tostring(err), stack = "" },
					"hooksecurefunc(UpdateSalesPrice)")
			end
		end)
		private.hooked = true
		Log("enable: hooksecurefunc installed on mixin.UpdateSalesPrice")
		return true
	end
	-- Fallback for a client with no mixin: hook the one frame.
	local tab = _G.AuctionatorSellingFrame
	local frame = tab and (tab.SaleItemFrame or tab)
	if not frame or type(frame.UpdateSalesPrice) ~= "function" then
		-- the tab is not built yet: wait for it instead of giving up
		Log("enable: no sell frame yet, watching AUCTION_HOUSE_SHOW")
		Auctionator.WatchForFrame()
		return false
	end
	local original = frame.UpdateSalesPrice
	frame.UpdateSalesPrice = function(self, salesPrice, bidPrice, preserveBidPrice)
		local a, b, c = original(self, salesPrice, bidPrice, preserveBidPrice)
		local ok, err = pcall(Auctionator.OnPriceSet, self)
		if not ok then
			private.error = tostring(err)
		end
		return a, b, c
	end
	private.hooked = true
	Log("enable: fallback hook installed on sale frame")
	return true
end

function Auctionator:Disable()
	private.enabled = false
	Auctionator.SaveSettings()
end


---The settings, in the addon's own saved variables.
function Auctionator.LoadSettings()
	local db = _G.DFA_ESTIMATE_DB
	if type(db) ~= "table" then
		db = {}
		_G.DFA_ESTIMATE_DB = db
	end
	-- A fill is what the bridge is for, so it starts on; `/dfaestimate
	-- fill off` is the explicit opt-out, and it is remembered.
	private.enabled = db.fillAuctionatorPrice ~= false
end

function Auctionator.SaveSettings()
	local db = _G.DFA_ESTIMATE_DB
	if type(db) ~= "table" then
		db = {}
		_G.DFA_ESTIMATE_DB = db
	end
	db.fillAuctionatorPrice = private.enabled
end
