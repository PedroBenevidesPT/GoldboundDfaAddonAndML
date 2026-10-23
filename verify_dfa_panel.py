# Headless check of the DfA Estimate addon: the panel and the LootAppraiser
# integration.
#
# Loads the real files -- the generated dataset, DFA_Estimate.lua,
# UI/Panel.lua and Integration/LootAppraiser.lua -- under LuaJIT (WoW's Lua
# 5.1) with only the WoW UI shell stubbed, then exercises every public entry
# point: the panel's text/status/farm-ranking builders and all
# /dfaestimate command paths, and the LootAppraiser class/value wrappers
# including the deposit-aware valuation (record.net less
# record.action.expected_deposit).  A Lua error anywhere fails the run.
#
# Also compile-checks every .lua file in the workspace addon and in the
# installed _retail_ copy, so a syntax slip cannot reach the client.
#
# Run:  <venv>/python.exe verify_dfa_panel.py

import glob
import os
import sys

import lupa

WS = r"C:\FarmAnalysis\DFA_Estimate"
RETAIL = r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\DFA_Estimate"

lua = lupa.LuaRuntime()

# ---------------------------------------------------------------- WoW stubs
lua.execute(r"""
SlashCmdList = {}
chat = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(_, m) chat[#chat + 1] = m end }

local function dummyFrame()
	local f = {}
	f.RegisterEvent = function() end
	f.UnregisterEvent = function() end
	f.SetScript = function() end
	f.Show = function() end
	f.Hide = function() end
	return f
end
CreateFrame = function() return dummyFrame() end
C_Timer = { After = function() end }
time = function() return 1792779530 end

-- item class 4 (armor) for every id but 7777, which is class 7 (trade goods)
C_Item = {
	GetItemInfoInstant = function(id)
		if id == 7777 then return nil, nil, nil, nil, nil, 7 end
		return nil, nil, nil, nil, nil, 4
	end,
	GetItemInfo = function(id)
		if id == 3001 then return "Junk", nil, 0, nil, nil, nil, nil, nil, nil, nil, 100 end
		if id == 3002 then return "Junk Mog", nil, 0, nil, nil, nil, nil, nil, nil, nil, 100 end
		return "Gear", nil, 3, nil, nil, nil, nil, nil, nil, nil, 250 end,
}
-- 3002 is junk with a transmog appearance; 3001 is junk without one
C_TransmogCollection = { GetItemInfo = function(id)
	if id == 3002 then return 12345 end
	return nil
end }

TSM_API = {
	GetCustomPriceValue = function(source, itemString)
		if source == "dbmarket" then return 51234 end
		if source == "dbrecent" then return 48800 end
		if source == "dbregionmarketavg" then return 53000 end
		if source == "dbregionsaleavg" then return 49500 end
		if source == "dfaestimate" then return 47777 end
		return nil
	end,
	GetPriceSourceKeys = function(keys)
		keys[#keys + 1] = "dbmarket"
		keys[#keys + 1] = "dfaestimate"
	end,
}

-- a stand-in for the real LootAppraiser Reloaded addon
LA = {
	db = { profile = { general = {} } },
	CONST = { PRICE_SOURCE = {} },
	availablePriceSources = {},
	GetFromDb = function(_, k) return "Custom" end,
	Session = { IsRunning = function() return false end },
	PriceSources = {
		ResolveForItem = function(itemID, base) return "Localized" end,
		GetItemValue = function(itemID, source) return 999 end,
	},
	UI = {},
}
LibStub = setmetatable({}, {__call = function(_, name, silent)
	if name == "AceAddon-3.0" then
		return { GetAddon = function(_, addonName) return LA end }
	end
	return nil
end})

DFA_ESTIMATE_SOURCE = {
	version = "2.0.0",
	GetError = function() return nil end,
	GetEstimate = function(itemString) return 47777 end,
	GetModelPrice = function(itemString) return 45000 end,
	GetFrozenPrice = function(itemString) return 46000 end,
	GetRealmName = function() return "Stubgaard" end,
}
""")

# ------------------------------------------------- load the real addon files
loader = lua.eval("function(src, ...) return assert(load(src))(...) end")


def load(path, *args):
    src = open(path, "r", encoding="utf-8").read()
    try:
        loader(src, *args)
        return True
    except lupa.LuaError as e:
        print("LOAD ERROR in %s:\n%s" % (path, e))
        return False


Addon = lua.eval("function() return {} end")()  # a real Lua table: missing keys are nil
ok = load(os.path.join(WS, "Data", "DFA_Estimate_Data.lua"))
ok &= load(os.path.join(WS, "DFA_Estimate.lua"), "DFA_Estimate", Addon)
ok &= load(os.path.join(WS, "UI", "Panel.lua"), "DFA_Estimate", Addon)
ok &= load(os.path.join(WS, "Integration", "LootAppraiser.lua"), "DFA_Estimate", Addon)
if not ok:
    sys.exit(1)
lua.globals().Addon = Addon  # expose the shared table the files were loaded with

lua.execute("""
if not GetRealmName then
	local realm = next(_G.DFA_ESTIMATE.realms)
	GetRealmName = function() return realm end
end
-- the sibling modules the panel reports on
Addon.DFAEstimateTooltip = {
	IsEnabled = function() return true end,
	UsesProcessor = function() return true end,
	GetDiagnostics = function() return { hooked = true, active = true, processor = true,
		processorGlobal = true, addPostCall = true, enumItem = true, gameTooltip = true,
		setHyperlink = true, error = nil } end,
	Enable = function() end,
}
Addon.DFAEstimateAuctionator = {
	IsEnabled = function() return true end,
	LoadSettings = function() end,
	Enable = function() end,
	SetDebug = function() end,
	SetEnabled = function() end,
	SetPostingHours = function(h) return true, "posting hours set to " .. tostring(h) end,
	PostingHoursStatus = function() return "posting hours: 24" end,
	GetDebug = function() return false end,
	GetDiagnostics = function() return { enabled = true, hooked = true, mixin = true,
		updateSalesPrice = true, saleItemFrame = true, userChose = nil,
		lastFill = { itemId = 12345, price = 47777, confirmed = true },
		error = nil, lastError = nil, attempts = 1 } end,
}
Addon.DFAEstimateMobInfo2 = {
	Enable = function() return true end,
	Disable = function() end,
	Status = function() return "MobInfo2: priced by DfA (served 3 value(s), 0 fallback(s))" end,
}
""")

g = lua.globals()
Panel = g.Addon.DFAEstimatePanel
Integration = g.Addon.DFAEstimateLootAppraiser

# a real item id from the dataset
item_id = lua.eval("""
function()
	local items = _G.DFA_ESTIMATE.realms[GetRealmName()].items
	for id in pairs(items) do return id end
end
""")()

print("dataset realm: %s, first item id: %s" % (g.GetRealmName(), item_id))

failures = []


def check(name, fn):
    try:
        fn()
        print("OK    %s" % name)
    except lupa.LuaError as e:
        failures.append(name)
        print("FAIL  %s\n      %s" % (name, str(e).strip().splitlines()[0]))


def _head(text, n):
    lines = str(text).splitlines()
    return "\n      ".join(lines[:n]) + ("\n      ... (%d lines total)" % len(lines) if len(lines) > n else "")


# ------------------------------------------- LootAppraiser integration tests
# The wrappers are installed by Enable; DatasetPrice is then driven through
# la.PriceSources.GetItemValue with controlled records.
la_result = lua.eval(r"""
function()
	local failures = {}
	local function assert_eq(name, got, want)
		if got ~= want then
			failures[#failures + 1] = name .. " (got " .. tostring(got)
				.. ", want " .. tostring(want) .. ")"
		end
	end

	local Integration = Addon.DFAEstimateLootAppraiser
	assert_eq("Enable", Integration:Enable(true), true)
	assert(Integration:IsEnabled(), "IsEnabled after Enable")

	-- class decision: armor id -> the DfA source, from the profile
	assert_eq("resolve armor", LA.PriceSources.ResolveForItem(1001, "Localized"), "dfaestimate")
	-- a class LootAppraiser does not override passes through untouched
	assert_eq("resolve trade goods", LA.PriceSources.ResolveForItem(7777, "Localized"), "Localized")
	-- with the class overrides off the wrapper passes through as well
	LA.db.profile.general.useSubClasses = false
	assert_eq("resolve overrides off", LA.PriceSources.ResolveForItem(1001, "Localized"), "Localized")
	LA.db.profile.general.useSubClasses = true

	-- controlled records for the value wrapper
	local records = {
		[1001] = { net = 16362, price = 17000, action = { expected_deposit = 0 } },
		[1002] = { net = 16362, price = 17000, action = { expected_deposit = 500 } },
		[1003] = { net = 0, price = 12000, action = { expected_deposit = 0 } },
		[1004] = nil,  -- no realm record: falls through to the regional table
		-- junk: 3001 has no record and no appearance, 3002 is a junk mog
		[3002] = { net = 5555, action = { expected_deposit = 0 } },
	}
	local real = Addon.DFAEstimate.GetRecord
	Addon.DFAEstimate.GetRecord = function(id) return records[id] end

	local value = LA.PriceSources.GetItemValue
	assert_eq("value net, no deposit", value(1001, "dfaestimate"), 16362)
	assert_eq("value net less deposit", value(1002, "dfaestimate"), 15862)
	assert_eq("value price fallback", value(1003, "dfaestimate"), 12000)
	-- regional net for 1004: whatever the dataset ships for it
	local regional = _G.DFA_ESTIMATE.nets and _G.DFA_ESTIMATE.nets[1004]
	if regional and regional > 0 then
		assert_eq("value regional net", value(1004, "dfaestimate"), regional)
	end
	-- junk that is not a transmog is worth the vendor price, not a model
	assert_eq("value junk -> vendor", value(3001, "dfaestimate"), 100)
	-- ... unless it has an appearance: a junk mog is still a mog
	assert_eq("value junk mog -> model", value(3002, "dfaestimate"), 5555)
	-- unknown everywhere: an explicit nil for the DfA source, by design --
	-- better than silently asking TSM for a source the user cannot use.
	-- Pick an id the dataset has neither a realm record, a regional net
	-- nor a price for.
	local absent = 1
	while _G.DFA_ESTIMATE.nets[absent] or _G.DFA_ESTIMATE.prices[absent]
		or records[absent] do
		absent = absent + 1
	end
	assert_eq("value unknown item", value(absent, "dfaestimate"), nil)
	-- another price source is not ours to answer
	assert_eq("value other source", value(1001, "dbmarket"), 999)
	-- a class LootAppraiser does not override passes through untouched
	assert_eq("value trade goods", value(7777, "dfaestimate"), 999)

	Addon.DFAEstimate.GetRecord = real

	-- status mentions the installed wrappers and the profile
	local status = Integration:Status()
	assert(status:find("armor=true"), "status armor")
	assert(status:find("resolver=installed"), "status resolver")
	assert(status:find("value=installed"), "status value")

	-- disable restores the previous profile values
	assert_eq("Disable", Integration:Disable(true), true)
	assert(not Integration:IsEnabled(), "IsEnabled after Disable")
	assert_eq("resolve after disable", LA.PriceSources.ResolveForItem(1001, "Localized"), "Localized")

	-- OnLogin re-applies it (first run: no remembered choice)
	_G.DFA_ESTIMATE_DB = {}
	Integration:OnLogin(true)
	assert(Integration:IsEnabled(), "IsEnabled after OnLogin")

	return failures
end
""")()
la_failures = list(la_result.values()) if la_result is not None else []
if la_failures:
    for line in la_failures:
        failures.append("LootAppraiser: " + line)
        print("FAIL  LootAppraiser: %s" % line)
else:
    print("OK    LootAppraiser integration (resolve/value/status/disable/login)")

# ------------------------------------------------------------ panel checks
check("BuildText", lambda: print(_head(Panel.BuildText(), 12)))
check("BuildStatus", lambda: print(_head(Panel.BuildStatus(), 10)))
check("BuildFarmRanking", lambda: print(_head("\n".join(Panel.BuildFarmRanking().values()), 14)))
check("DescribeItem(%s)" % item_id, lambda: print("      " + Panel.DescribeItem(item_id)))
check("ReportSource(%s)" % item_id, lambda: Panel.ReportSource(item_id))
check("HandleCommand('')", lambda: Panel.HandleCommand(""))
check("HandleCommand('debug')", lambda: Panel.HandleCommand("debug"))
check("HandleCommand('farm')", lambda: Panel.HandleCommand("farm"))
check("HandleCommand('mobinfo status')", lambda: Panel.HandleCommand("mobinfo status"))
check("HandleCommand('mobinfo off')", lambda: Panel.HandleCommand("mobinfo off"))
check("HandleCommand('lootappraiser status')", lambda: Panel.HandleCommand("lootappraiser status"))
check("HandleCommand('lootappraiser off')", lambda: Panel.HandleCommand("lootappraiser off"))
check("HandleCommand('fill')", lambda: Panel.HandleCommand("fill"))
check("HandleCommand('fill off')", lambda: Panel.HandleCommand("fill off"))
check("HandleCommand('posthours')", lambda: Panel.HandleCommand("posthours"))
check("HandleCommand('posthours 12')", lambda: Panel.HandleCommand("posthours 12"))
check("HandleCommand('source %s')" % item_id, lambda: Panel.HandleCommand("source %s" % item_id))
check("HandleCommand(item id)", lambda: Panel.HandleCommand(str(item_id)))
check("HandleCommand(item link)", lambda: Panel.HandleCommand("|Hitem:%d::::::::60:::::::|h[x]|h|r" % item_id))
check("GetLayout", lambda: Panel.GetLayout())
check("ItemLabel", lambda: (Panel.ItemLabel(1, "Sword"), Panel.ItemLabel(None, "Sword"),
                            Panel.ItemLabel(1, None), Panel.ItemLabel(None, None)))
check("/dfala status", lambda: lua.globals().SlashCmdList.DFALOOTAPPRAISER("status"))

# ------------------------------------------------- MobInfo2 integration checks
# The rules live in the patched MobInfo2.lua itself; Integration/MobInfo2.lua
# only wraps a *stock* MobInfo2 and must stand down when it sees the marker.
ok = load(os.path.join(WS, "Integration", "MobInfo2.lua"), "DFA_Estimate", Addon)
if not ok:
    sys.exit(1)
MobInfo2 = g.Addon.DFAEstimateMobInfo2
g.ITEM_ID = item_id

lua.execute("""
-- a patched MobInfo2 applies every rule itself, so the wrapper must not
-- answer first with a market-only value
function test_mobinfo2_patched_passthrough()
	DFA_ESTIMATE_DB = {}
	local called = 0
	local patched = function() called = called + 1; return 4242 end
	-- the flag lives on MI2: a Lua function cannot carry fields of its own
	MI2 = { FindItemValue = patched, dfaFindItemValuePatched = true }

	local installed, reason = Addon.DFAEstimateMobInfo2:Enable(false)
	assert(installed, "Enable refused a patched MobInfo2: " .. tostring(reason))
	assert(Addon.DFAEstimateMobInfo2.patched == true, "the patched flag was not set")
	assert(Addon.DFAEstimateMobInfo2.wrapper == nil, "a wrapper was installed over the patch")
	assert(MI2.FindItemValue == patched, "the patched function was replaced")
	assert(MI2.FindItemValue(3001) == 4242, "the patched answer did not come through")
	assert(called == 1, "the patched function was not called")

	local status = Addon.DFAEstimateMobInfo2:Status()
	assert(status:find("in place"), "status does not mention the in-place patch: " .. status)
	return status
end

-- a stock MobInfo2 (a patch lost to an addon update) still gets a wrapper,
-- and an item the dataset cannot price still falls back to MobInfo2
function test_mobinfo2_stock_wrapper()
	DFA_ESTIMATE_DB = {}
	MI2 = { FindItemValue = function() return 4242 end }

	assert(Addon.DFAEstimateMobInfo2:Enable(false), "Enable refused a stock MobInfo2")
	assert(Addon.DFAEstimateMobInfo2.patched == false, "a stock MobInfo2 was marked patched")
	assert(Addon.DFAEstimateMobInfo2.wrapper ~= nil, "no wrapper was installed")
	assert(MI2.FindItemValue ~= nil, "the wrapper was not installed on MI2")

	local value = MI2.FindItemValue(ITEM_ID)
	assert(type(value) == "number" and value > 0,
		"the wrapper served no market value for " .. tostring(ITEM_ID))

	local fallback = MI2.FindItemValue(999999)
	assert(fallback == 4242, "an unpriceable item did not fall back: " .. tostring(fallback))
	return value, fallback
end
""")

check("MobInfo2: patched MobInfo2 is passed through",
      lambda: print("      " + str(lua.globals().test_mobinfo2_patched_passthrough())))
check("MobInfo2: stock MobInfo2 is wrapped",
      lambda: print("      served %s, fell back to %s" % tuple(lua.globals().test_mobinfo2_stock_wrapper())))

# ------------------------------------------------------- compile-check trees
print()
for tree in (WS, RETAIL):
    for path in sorted(glob.glob(os.path.join(tree, "**", "*.lua"), recursive=True)):
        src = open(path, "r", encoding="utf-8", errors="replace").read()
        rel = os.path.relpath(path, tree)
        try:
            lua.compile(src)
            print("COMPILES  %-14s %s" % (os.path.basename(tree), rel))
        except lupa.LuaError as e:
            print("LuaJIT-ERR %-14s %s -> %s" % (os.path.basename(tree), rel,
                                                 str(e).strip().splitlines()[0]))

print()
if failures:
    print("PANEL CHECK: %d FAILURE(S): %s" % (len(failures), ", ".join(map(str, failures))))
    sys.exit(1)
print("PANEL CHECK: ALL OK")
