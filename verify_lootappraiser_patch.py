# Verify the LootAppraiser <- DfA Estimate patch.
#
# LootAppraiser's PriceSources.GetItemValue was patched in place (see
# DfaItemValue in Core/PriceSources.lua) so the value it records for a looted
# item follows the same rules as the tooltip and MobInfo2:
#   * junk (poor quality) that is not a transmog  -> vendor price
#   * a commodity (trade goods, reagents, ...)     -> TSM minbuyout
#   * everything else                              -> DfA market estimate
# with a fall-through to LootAppraiser's own answer when nothing can price it.
#
# Run after patching, and again after any LootAppraiser update that wiped the
# edit:
#   C:/Users/petru/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe verify_lootappraiser_patch.py
#
# Requires the `lupa` package (LuaJIT == WoW's Lua 5.1).

import re
import sys

import lupa

PRICES = (r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns"
          r"\LootAppraiserReloaded\Core\PriceSources.lua")

src = open(PRICES, "r", encoding="utf-8").read()

lua = lupa.LuaRuntime(unpack_returned_tuples=True)

# the WoW surface the file touches while it loads: C_Item is read at file
# scope, and the popup helpers are captured as locals (nil is fine, they are
# only called from the config path)
lua.execute("""
COPPER_PER_GOLD, COPPER_PER_SILVER = 10000, 100
OKAY = "OKAY"
StaticPopupDialogs = {}
StaticPopup_Show = function() end
C_Item = {
    GetItemInfo = function(id)
        if id == 1001 then return "Gear", nil, 3, nil, nil, nil, nil, nil, nil, nil, 250 end
        if id == 1002 then return "Junk", nil, 0, nil, nil, nil, nil, nil, nil, nil, 100 end
        if id == 1003 then return "Junk Mog", nil, 0, nil, nil, nil, nil, nil, nil, nil, 100 end
        if id == 2001 then return "Ore", nil, 1, nil, nil, nil, nil, nil, nil, nil, 25 end
        return nil
    end,
}
Auctionator = { API = { v1 = { GetAuctionPriceByItemID = function() return 888 end } } }
""")

# the addon table the file reads: LA.TSM answers 999 for every source, so a
# DfA answer and a fall-through answer can never be confused
la = lua.eval("""
function()
    return {
        TSM = {
            IsTSMLoaded = function() return true end,
            GetAvailablePriceSources = function() return {} end,
            GetItemValue = function(itemID, source) return 999 end,
        },
        db = { profile = { general = {} } },
        Debug = { Log = function() end },
        GetFromDb = function() return "dbmarket" end,
    }
end
""")()
lua.globals().LA = la

loader = lua.eval(
    "function(src, name, la) local f = assert(load(src, name)); return f(name, la) end")
try:
    loader(src, "LootAppraiserReloaded", la)
except lupa.LuaError as exc:
    print("LOAD ERROR in %s:\n%s" % (PRICES, exc))
    sys.exit(1)

sources = la.PriceSources
if not sources:
    print("FAIL  the file did not expose LA.PriceSources")
    sys.exit(1)

lua.execute("""
FACTS = {
    [1001] = { quality = 3, commodity = false, appearance = false },
    [1002] = { quality = 0, commodity = false, appearance = false },
    [1003] = { quality = 0, commodity = false, appearance = true },
    [1004] = { quality = 0, commodity = false, appearance = false, vendor = 66 },
    [2001] = { quality = 1, commodity = true,  appearance = false },
    [999]  = { quality = 1, commodity = false, appearance = false },
}
VENDOR = { [1002] = 55, [1003] = 55 }
TSM_API = {
    GetCustomPriceValue = function(source, itemString)
        if source == "minbuyout" and itemString == "i:2001" then return 7777 end
        return nil
    end,
}
DFA_ESTIMATE_API = {
    BaseItemId = function(x) return tonumber(x) end,
    ItemFacts = function(id) return FACTS[id] end,
    GetVendorPrice = function(id) return VENDOR[id] end,
    GetLootValues = function(id)
        if id == 999 then return nil end
        return { vendor = 100, market = 4242 }
    end,
}
DFA_ESTIMATE_DB = {}
""")

call = lua.eval("function(id, source) return LA.PriceSources.GetItemValue(id, source) end")

failed = 0


def case(name, item, source, expect):
    global failed
    got = call(item, source)
    ok = got == expect
    failed += not ok
    print("%-28s %-12s -> %-8s (expect %-8s) %s"
          % (name, source, got, expect, "OK" if ok else "FAIL"))


# the rules, in the order a farmer would use them
case("gear -> market", 1001, "dbmarket", 4242)
case("junk -> vendor", 1002, "dbmarket", 55)
case("junk mog -> market", 1003, "dbmarket", 4242)
case("junk, dataset vendor", 1004, "dbmarket", 66)
case("commodity -> minbuyout", 2001, "dbmarket", 7777)
case("unpriceable -> own answer", 999, "dbmarket", 999)

# the two sources the patch must not touch: the vendor panel asks what a
# vendor pays, and the disenchant value is not modelled at all
case("VendorValue untouched", 1001, "VendorValue", 250)
case("Destroy untouched", 1001, "Destroy", 999)
# every other source is a market opinion, so DfA wins -- that is the point of
# overwriting LootAppraiser rather than leaving its source choice in charge
case("Auctionator -> DfA", 1001, "Auctionator", 4242)

# the opt-out, and the two ways the addon can be missing
lua.execute("DFA_ESTIMATE_DB = { lootappraiser = false }")
case("opt-out lootappraiser=false", 1001, "dbmarket", 999)
lua.execute("DFA_ESTIMATE_DB = {}")
lua.execute("DFA_ESTIMATE_API = nil")
case("DfA addon absent", 1001, "dbmarket", 999)
lua.execute("""
DFA_ESTIMATE_API = {
    BaseItemId = function(x) return tonumber(x) end,
    ItemFacts = function() error("boom") end,
    GetLootValues = function() error("boom") end,
}
""")
case("DfA API errors", 1001, "dbmarket", 999)

# --------------------------------------------------- the marker, and its shape
# Integration/LootAppraiser.lua stands down when it sees
# `PriceSources.dfaGetItemValuePatched`.  It has to be a field on the table: a
# Lua function has no table of its own, so writing the flag onto
# `PriceSources.GetItemValue` raises "attempt to index a function value" and
# would take LootAppraiser down at load.
marker = re.search(r"^PriceSources\.dfaGetItemValuePatched\s*=\s*true", src, re.M) is not None
on_function = re.search(r"^PriceSources\.GetItemValue\.\w+\s*=", src, re.M) is not None
live = sources.dfaGetItemValuePatched == True
ok = marker and live and not on_function
failed += not ok
print("%-28s %-12s -> %-8s %s"
      % ("marker on the table", "yes" if live else "no", "", "OK" if ok else "FAIL"))

print("PATCH", "OK" if not failed else "BROKEN")
sys.exit(1 if failed else 0)
