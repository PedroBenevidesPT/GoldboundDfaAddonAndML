"""Run DFA_Estimate in a real Lua interpreter with a mocked client.

The AuctionIntelligence addon has had a harness for a while; this one covers
the reader, which is the half a player actually looks at.  The questions it
answers are the ones a silent in-game failure hides:

1. does the shipped dataset load, and at the version the reader understands?
2. does the tooltip hook install *without* an error string (``tooltipError``)?
3. does the panel build (``panelError``), and does it show a row for an item
   the dataset prices?
4. does the item tooltip actually get a line for a real item?
5. does ``/dfaestimate`` answer?
6. does a second load (``/reload``, or the client re-firing ADDON_LOADED)
   install the tooltip hook a second time?

    python scripts/dfa_estimate_harness.py [addon-dir]

Exit status is 0 only when every check holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

from lupa import LuaError, LuaRuntime

ADDON = Path(sys.argv[1] if len(sys.argv) > 1
             else "C:/FarmAnalysis/DFA_Estimate")
TOC = ADDON / "DFA_Estimate.toc"

MOCK = r"""
-- ---------------------------------------------------------------- the client
local printed = {}
PRINTED = printed

local function record(text)
    printed[#printed + 1] = tostring(text)
end

function print(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
    record(table.concat(parts, " "))
end

NOW = 1792623900
function time() return NOW end
function date(fmt, ts) return tostring(ts) end
function GetTime() return 1 end
function GetRealmName() return "Dragonmaw" end
function GetLocale() return "enUS" end
function GetBuildInfo() return "12.0.5", "60000", "2026-10-01", 120005 end
function InCombatLockdown() return false end
function GetItemInfo(id)
    return "Item " .. tostring(id), "|cff0070dd|Hitem:" .. tostring(id) .. "|h[Item]|h|r",
        3, 0, 4, "Armor", "Plate", 1, "INVTYPE_CHEST", 12345
end

C_Item = {
    GetItemInfoInstant = function(id) return id, "Armor", "Plate", 4, 3, 1 end,
    GetItemName = function(id) return "Item " .. tostring(id) end,
    GetItemInfo = function(id) return GetItemInfo(id) end,
    RequestLoadItemDataByID = function() end,
}

C_Timer = { After = function(_, fn) fn() end, NewTimer = function() return {} end }

Enum = { TooltipDataType = { Item = 1, Spell = 2 } }
POSTCALLS = {}
TooltipDataProcessor = {
    AddTooltipPostCall = function(kind, fn) POSTCALLS[#POSTCALLS + 1] = fn end,
    AddTooltipPreCall = function(kind, fn) end,
}

local function Widget(kind, name)
    local w = {
        kind = kind, name = name, scripts = {}, events = {}, lines = {},
        shown = true, text = "", width = 200, height = 20, children = {},
    }
    -- Explicit members first: the addon depends on these actually working.
    w.SetScript = function(self, script, fn) self.scripts[script] = fn end
    w.GetScript = function(self, script) return self.scripts[script] end
    w.HookScript = function(self, script, fn) self.scripts[script] = fn end
    w.RegisterEvent = function(self, event) self.events[#self.events + 1] = event end
    w.UnregisterEvent = function(self, event)
        for i = #self.events, 1, -1 do
            if self.events[i] == event then table.remove(self.events, i) end
        end
    end
    w.Show = function(self) self.shown = true return self end
    w.Hide = function(self) self.shown = false return self end
    w.SetShown = function(self, v) self.shown = v and true or false return self end
    w.IsShown = function(self) return self.shown end
    w.IsVisible = function(self) return self.shown end
    w.SetText = function(self, text) self.text = tostring(text or "") return self end
    w.GetText = function(self) return self.text end
    w.SetFormattedText = function(self, text) self.text = tostring(text or "") return self end
    w.AddLine = function(self, text)
        self.lines[#self.lines + 1] = tostring(text or "")
        return self
    end
    w.AddDoubleLine = function(self, left, right)
        self.lines[#self.lines + 1] = tostring(left or "") .. " " .. tostring(right or "")
        return self
    end
    w.NumLines = function(self) return #self.lines end
    w.GetLine = function(self, i) return self.lines[i] end
    w.ClearLines = function(self) self.lines = {} return self end
    w.GetItem = function(self) return nil, self.link end
    w.SetHyperlink = function(self, link) self.link = link return self end
    w.GetWidth = function(self) return self.width end
    w.GetHeight = function(self) return self.height end
    w.SetWidth = function(self, v) self.width = v return self end
    w.SetHeight = function(self, v) self.height = v return self end
    w.SetSize = function(self, w, h) self.width = w self.height = h return self end
    w.GetParent = function(self) return self.parent end
    w.SetParent = function(self, p) self.parent = p return self end
    w.CreateFontString = function(self, name, layer, template)
        local child = Widget("FontString", name)
        child.parent = self
        self.children[#self.children + 1] = child
        return child
    end
    w.CreateTexture = function(self, name, layer, template)
        local child = Widget("Texture", name)
        child.parent = self
        self.children[#self.children + 1] = child
        return child
    end
    w.GetScrollChild = function(self) return self.scrollChild end
    w.SetScrollChild = function(self, c) self.scrollChild = c return self end
    -- Everything else is a no-op that keeps the chain: a client stub has
    -- hundreds of setters and none of them change what is being tested.
    return setmetatable(w, {
        __index = function(t, key)
            local fn = function(first) return first end
            rawset(t, key, fn)
            return fn
        end,
    })
end

FRAMES = {}
function CreateFrame(kind, name, parent, template)
    local frame = Widget(kind, name)
    frame.parent = parent
    frame.template = template
    FRAMES[#FRAMES + 1] = frame
    return frame
end

UIParent = CreateFrame("Frame", "UIParent")
GameTooltip = CreateFrame("GameTooltip", "GameTooltip")
ItemRefTooltip = CreateFrame("GameTooltip", "ItemRefTooltip")
GameTooltip_SetDefaultAnchor = function() end

SLASH_DFAESTIMATE1 = nil
SlashCmdList = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(self, msg) record(msg) end }
LibStub = nil

HOOKED = {}
function hooksecurefunc(target, name, fn)
    target = target or _G
    if type(target[name]) ~= "function" then
        error(("attempt to hook a nonexistent function %s"):format(tostring(name)), 2)
    end
    HOOKED[#HOOKED + 1] = name
    local original = target[name]
    target[name] = function(...)
        local a, b, c, d = original(...)
        fn(...)
        return a, b, c, d
    end
end

function FIRE(event, ...)
    for _, frame in ipairs(FRAMES) do
        local registered = false
        for _, e in ipairs(frame.events) do
            if e == event then registered = true break end
        end
        local handler = frame.scripts.OnEvent
        if registered and handler then handler(frame, event, ...) end
    end
end

-- ------------------------------------------------------------- Auctionator
-- The selling tab, as far as the bridge can see it.  The frames copy the
-- mixin when they are made, so the frame looks the mixin up through a
-- metatable: that is what makes a hook on the mixin reach the frame.
AUCTION_EVENTS = {
    BagItemClicked = "bag_item_clicked",
    ClearBagItem = "clear_bag_item",
    PriceSelected = "price_selected",
}

Auctionator = {
    Selling = { Events = AUCTION_EVENTS },
    EventBus = { sources = {}, listeners = {} },
}

function Auctionator.EventBus:RegisterSource(source, name)
    self.sources[source] = name
end

function Auctionator.EventBus:Register(listener, eventNames)
    for _, name in ipairs(eventNames) do
        self.listeners[name] = self.listeners[name] or {}
        table.insert(self.listeners[name], listener)
    end
end

function Auctionator.EventBus:Fire(source, eventName, ...)
    if self.sources[source] == nil then
        error("All sources must be registered (" .. eventName .. ")")
    end
    local listeners = self.listeners[eventName]
    if listeners then
        for _, listener in ipairs(listeners) do
            listener:ReceiveEvent(eventName, ...)
        end
    end
    return self
end

-- An edit box that behaves like the client's: a text change calls the
-- OnTextChanged script, which is how the bridge hears the player typing.
function NewEditBox()
    local box = { scripts = {}, text = "" }
    box.SetScript = function(self, script, fn) self.scripts[script] = fn end
    box.GetScript = function(self, script) return self.scripts[script] end
    box.SetText = function(self, text)
        self.text = tostring(text or "")
        local handler = self.scripts.OnTextChanged
        if handler then handler(self, self.text) end
        return self
    end
    box.GetText = function(self) return self.text end
    box.IsFocused = function(self) return self.focused == true end
    return box
end

-- The money input: the amount is what the boxes say, so a price the
-- player types is a price the addon reads.
function NewMoneyInput()
    local gold, silver, copper = NewEditBox(), NewEditBox(), NewEditBox()
    local input = { GoldBox = gold, SilverBox = silver, CopperBox = copper }
    input.SetAmount = function(self, amount)
        local value = math.floor(tonumber(amount) or 0)
        gold:SetText(tostring(math.floor(value / 10000)))
        silver:SetText(tostring(math.floor((value % 10000) / 100)))
        copper:SetText(tostring(value % 100))
    end
    input.GetAmount = function(self)
        return (tonumber(gold:GetText()) or 0) * 10000
            + (tonumber(silver:GetText()) or 0) * 100
            + (tonumber(copper:GetText()) or 0)
    end
    return input
end

function NewSaleItemFrame()
    local money = NewMoneyInput()
    local price = { MoneyInput = money }
    price.SetAmount = function(self, amount) self.MoneyInput:SetAmount(amount) end
    price.GetAmount = function(self) return self.MoneyInput:GetAmount() end
    local frame = { Price = price, dbPrice = 0 }
    setmetatable(frame, { __index = AuctionatorSaleItemMixin })
    return frame
end

AuctionatorSaleItemMixin = {}

function AuctionatorSaleItemMixin:UpdateSalesPrice(salesPrice, bidPrice, preserveBidPrice)
    self.Price:SetAmount(salesPrice or 0)
end

function AuctionatorSaleItemMixin:Update()
    if self.itemInfo then
        -- what the client's own database holds for the item
        self:UpdateSalesPrice(self.dbPrice or 0)
    else
        self:UpdateSalesPrice(0)
    end
end

function AuctionatorSaleItemMixin:ReceiveEvent(event, ...)
    if event == AUCTION_EVENTS.BagItemClicked then
        local itemInfo = ...
        self.itemInfo = itemInfo
        self:Update()
    elseif event == AUCTION_EVENTS.PriceSelected then
        local selected = ...
        self:UpdateSalesPrice(selected and selected.buyout or 0)
    end
end
"""


def load_addon(lua, addon_dir: Path) -> dict:
    """Load the files the TOC lists, in TOC order, with (name, table)."""
    toc = addon_dir / "DFA_Estimate.toc"
    files = [line.strip() for line in toc.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("##")]
    addon = lua.table()
    loader = lua.eval("loadfile")
    for name in files:
        path = addon_dir / name.replace("\\", "/")
        if not path.exists():
            raise SystemExit(f"the TOC lists a missing file: {name}")
        chunk = loader(str(path).replace("\\", "/"))
        chunk("DFA_Estimate", addon)
    return addon


def main() -> int:
    if not TOC.exists():
        print(f"no TOC at {TOC}")
        return 2
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    try:
        addon = load_addon(lua, ADDON)
    except LuaError as exc:
        print(f"FAIL the addon does not load: {exc}")
        return 1

    lua.globals().DFAEST = addon["DFAEstimate"]
    est = lua.eval("DFAEST")
    check("the addon exposes its reader", est is not None)
    if est is None:
        print("FAIL no reader table")
        return 1

    check("the dataset loaded", bool(est["Load"]() is True
                                      or est["dataVersion"] is not None),
          str(est["error"]))
    check("the dataset version is the supported one", int(est["dataVersion"] or 0) == 2,
          str(est["dataVersion"]))
    prices = int(est["GetPriceCount"]() or 0)
    check("the dataset ships regional prices", prices > 1000, f"{prices} prices")

    # the item the tooltip is asked about: one the dataset actually prices
    # An item the dataset prices *and* has a vendor value for, so the probe
    # exercises the vendor and net lines rather than only the ask.
    item_id = lua.eval(
        "(function() local d = DFAEST.data"
        " if type(d) ~= 'table' or type(d.prices) ~= 'table' then return nil end"
        " local vendors = d.vendors or {}"
        " local best, bestId = -1, nil"
        " for k, v in pairs(d.prices) do"
        "   if type(v) == 'number' and vendors[k] and v > best then best, bestId = v, k end"
        " end"
        " if bestId then return bestId end"
        " for k, v in pairs(d.prices) do"
        "   if type(v) == 'number' and v > best then best, bestId = v, k end"
        " end"
        " return bestId end)()")
    check("a priced item can be found", item_id is not None, str(item_id))

    # ADDON_LOADED is what installs the tooltip and builds the panel
    lua.execute("FIRE('ADDON_LOADED', 'DFA_Estimate')")
    check("the tooltip hook installed", est["tooltipError"] is None, str(est["tooltipError"]))
    check("the panel initialised", est["panelError"] is None, str(est["panelError"]))
    check("the tooltip processor was registered", int(lua.eval("#POSTCALLS")) >= 1,
          str(lua.eval("#POSTCALLS")))

    if item_id is not None:
        # The addon only decorates the game's own tooltips (its ValidateTooltip
        # rejects a named frame that is not GameTooltip/ItemRefTooltip), so the
        # probe has to be GameTooltip itself, which is what the player hovers.
        lua.execute(
            "GameTooltip:ClearLines()\n"
            "GameTooltip.__dfaEstimateItem = nil\n"
            f"POSTCALLS[1](GameTooltip, {{ hyperlink = 'item:{int(item_id)}' }})\n"
            "PROBE = GameTooltip")
        lines = int(lua.eval("#PROBE.lines"))
        check("the item tooltip gets a line", lines > 0, f"{lines} lines")
        text = " ".join(str(x) for x in lua.eval("PROBE.lines").values())
        check("the tooltip line carries a price", "g" in text.lower() or "s" in text.lower(),
              text[:80])
        check("the tooltip shows the vendor value", "Vendor:" in text, text[:80])
        check("the tooltip shows the net after the cut",
              "Net after" in text, text[-80:])

    # A realm record carries the action and, when a snapshot covered the
    # item, the observed market.  The tooltip has to show what the dataset
    # says, and nothing more.
    action_item = lua.eval(
        "(function() local d = DFAEST.data"
        " local block = d and d.realms and d.realms[GetRealmName()]"
        " if not block or type(block.items) ~= 'table' then return nil end"
        " for id, record in pairs(block.items) do"
        "   if type(record.action) == 'table' and record.action.action then return id end"
        " end return nil end)()")
    check("a record carries an action", action_item is not None, str(action_item))
    if action_item is not None:
        lua.execute(
            "GameTooltip:ClearLines()\n"
            "GameTooltip.__dfaEstimateItem = nil\n"
            f"POSTCALLS[1](GameTooltip, {{ hyperlink = 'item:{int(action_item)}' }})\n"
            "PROBE = GameTooltip")
        action_text = " ".join(str(x) for x in lua.eval("PROBE.lines").values())
        check("the tooltip shows the action", "Action:" in action_text,
              action_text[:100])

    before = int(lua.eval("#PRINTED"))
    lua.execute("if SlashCmdList['DFAESTIMATE'] then SlashCmdList['DFAESTIMATE']('') end")
    check("/dfaestimate answers", int(lua.eval("#PRINTED")) > before,
          f"{int(lua.eval('#PRINTED')) - before} lines")

    # /reload: the client re-fires ADDON_LOADED on the same frames
    hooks = int(lua.eval("#POSTCALLS"))
    lua.execute("FIRE('ADDON_LOADED', 'DFA_Estimate')")
    check("a reload does not hook the tooltip twice", int(lua.eval("#POSTCALLS")) == hooks,
          f"{hooks} -> {int(lua.eval('#POSTCALLS'))}")

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
