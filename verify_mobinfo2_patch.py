# Verify the MobInfo2 <- DfA Estimate patch.
#
# MobInfo2's MI2.FindItemValue was patched in place (see MI2_TsmMinBuyout and
# MI2_DfaItemValue in MobInfo2.lua) so item values follow the farmer's rules:
#   * junk (poor quality) that is not a transmog  -> vendor price
#   * a commodity (trade goods, reagents, ...)     -> TSM minbuyout
#   * everything else                              -> DfA market estimate
# with a safe fall-through to MobInfo2's own answer when nothing can price it.
#
# Run after patching, and again after any MobInfo2 update that wiped the
# edit:
#   C:/Users/petru/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe verify_mobinfo2_patch.py
#
# Requires the `lupa` package (LuaJIT == WoW's Lua 5.1).

import re
import sys

import lupa

MOBINFO2 = r"C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns\MobInfo2\MobInfo2.lua"

lua = lupa.LuaRuntime()
lines = open(MOBINFO2, "r", encoding="utf-8").read().splitlines(keepends=True)

start = next(i for i, l in enumerate(lines) if "local function MI2_TsmMinBuyout" in l)
fn_start = next(i for i, l in enumerate(lines[start:], start) if "function MI2.FindItemValue" in l)
end = next(i for i, l in enumerate(lines[fn_start:], fn_start) if l.rstrip() == "end")
region = "".join(lines[start:end + 1])
assert "function MI2.FindItemValue" in region, "patched region not found"
assert "MI2_TsmMinBuyout" in region, "minbuyout helper not in the region"

harness = (
    "MI2 = { BasePrices = {} }\n"
    "COPPER_PER_GOLD, COPPER_PER_SILVER = 10000, 100\n"
    # vendor price unknown -> the original -1 / BasePrices fallback path
    "C_Item = { GetItemInfo = function() return nil end }\n"
    + region
    + """
FACTS = {
    [1001] = { quality = 3, commodity = false, appearance = false },
    [1002] = { quality = 0, commodity = false, appearance = false },
    [1003] = { quality = 0, commodity = false, appearance = true },
    [1004] = { quality = 0, commodity = false, appearance = false, vendor = 66 },
    [2001] = { quality = 1, commodity = true, appearance = false },
    [2002] = { quality = 1, commodity = true, appearance = false },
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
    BaseItemId = function(x)
        if type(x) == "number" then return x end
        if type(x) == "string" then
            local id = string.match(x, "item:(%d+)") or string.match(x, "^i:(%d+)")
            return id and tonumber(id) or nil
        end
        return nil
    end,
    ItemFacts = function(id) return FACTS[id] end,
    GetVendorPrice = function(id) return VENDOR[id] end,
    GetLootValues = function(id)
        if id == 999 then return nil end
        return { vendor = 100, market = 4242 }
    end,
}
"""
)
lua.execute(harness)
call = lua.eval("function(...) return MI2.FindItemValue(...) end")

cases = [
    ("gear -> market", 1001, 4242),
    ("junk -> vendor", 1002, 55),
    ("junk mog -> market", 1003, 4242),
    ("junk, client vendor", 1004, 66),
    ("commodity -> minbuyout", 2001, 7777),
    ("commodity, no TSM", 2002, 4242),
    ("nothing can price it", 999, 1),
    ("item link", "|Hitem:1001::::::::60:::::::|h[x]|h|r", 4242),
]

failed = 0
for name, arg, expected in cases:
    got = call(arg)
    ok = got == expected
    failed += not ok
    print("%-24s -> %-8s (expect %s) %s" % (name, got, expected, "OK" if ok else "FAIL"))

lua.execute("DFA_ESTIMATE_DB = { mobinfo = false }")
got = call(1001)
ok = got == 1
failed += not ok
print("%-24s -> %-8s (expect 1) %s" % ("opt-out mobinfo=false", got, "OK" if ok else "FAIL"))
lua.execute("DFA_ESTIMATE_DB = nil")

lua.execute("DFA_ESTIMATE_API = nil")
got = call(1001)
ok = got == 1
failed += not ok
print("%-24s -> %-8s (expect 1) %s" % ("DfA addon absent", got, "OK" if ok else "FAIL"))

lua.execute('DFA_ESTIMATE_API = { BaseItemId = function(x) return tonumber(x) end, GetLootValues = function() error("boom") end }')
got = call(1001)
ok = got == 1
failed += not ok
print("%-24s -> %-8s (expect 1) %s" % ("DfA API errors", got, "OK" if ok else "FAIL"))

# --------------------------------------------------- the marker, and its shape
# Integration/MobInfo2.lua passes every answer straight through when it sees
# `MI2.dfaFindItemValuePatched`.  It has to be a field on the MI2 table: a Lua
# function has no table of its own, so writing the flag onto
# `MI2.FindItemValue` raises "attempt to index a function value" and would
# take MobInfo2 down at load.  This check keeps that from coming back.
src = open(MOBINFO2, "r", encoding="utf-8").read()
marker = re.search(r"^MI2\.dfaFindItemValuePatched\s*=\s*true", src, re.M) is not None
on_function = re.search(r"^MI2\.FindItemValue\.\w+\s*=", src, re.M) is not None
ok = marker and not on_function
failed += not ok
print("%-24s -> %-8s %s" % ("marker on the MI2 table", "yes" if marker else "no",
                            "OK" if ok else "FAIL"))

print("PATCH", "OK" if not failed else "BROKEN")
sys.exit(1 if failed else 0)
