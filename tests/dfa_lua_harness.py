"""A Lua runtime that behaves enough like the WoW client to load the addon.

Two test files load ``DFA_Estimate``'s Lua to check the Auctionator bridge
and the pricing export.  They used to build their own runtimes, which meant
two mock clients that drifted apart -- one of them lacked ``CreateFrame``
entirely, so its tests failed before reaching an assertion.  There is one
harness now, and both use it.

What it models, and why each part is needed:

* ``CreateFrame`` returns a table carrying the frame methods the addon calls
  (``SetScript``/``GetScript``, the event methods, ``SetText``).  The methods
  live on the frame itself: a self-referential ``__index`` would make every
  *missing* key recurse until Lua raises "index chain too long", which is
  exactly what ``box.__dfaWatched`` hit.
* ``SetText`` fires ``OnTextChanged``, because the client does.  Without it
  the bridge's "the player typed a price, their choice wins" guard could
  never run, and the test for it would pass by never executing the code.
* ``fire()`` delivers an event to every frame that registered for it, the way
  the client dispatches.  The addon installs its tooltip hook and its panel
  from ``ADDON_LOADED`` / ``PLAYER_LOGIN``, so loading the file is not enough
  -- a loaded addon is not yet a running one.

Everything here is a stub for a WoW API, never for the addon: the Lua under
test is always the file in the addon folder.
"""
from __future__ import annotations

from pathlib import Path

import lupa

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "DFA_Estimate" / "UI" / "Auctionator.lua"
PANEL = ROOT / "DFA_Estimate" / "UI" / "Panel.lua"
TOOLTIP = ROOT / "DFA_Estimate" / "UI" / "Tooltip.lua"
MAIN = ROOT / "DFA_Estimate" / "DFA_Estimate.lua"
DATA = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"


def woW_like_lua():
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        _G.Enum = { TooltipDataType = { Item = 7 } }
        _G.TooltipDataProcessor = {
            AddTooltipPostCall = function(kind, callback)
                _G.__post = { kind = kind, callback = callback }
            end,
        }
        function CreateFrame(name, template, parent, showOnLoad)
            local frame = { name = name, scripts = {}, events = {}, OnEvent = function() end }
            -- The client knows every frame it created; the harness needs
            -- the same list to deliver events, because the addon's event
            -- frame is a local and cannot be reached any other way.
            _G.__frames = _G.__frames or {}
            table.insert(_G.__frames, frame)
            -- Every WoW frame has these methods whether or not a template
            -- was named; guarding them on ``template`` made the mock return
            -- a bare table for the addon's own frames, which is why loading
            -- it died on the first ``:RegisterEvent``.
            frame.SetScript = function(self, script, handler)
                self.scripts[script] = handler
            end
            frame.GetScript = function(self, script)
                return self.scripts[script]
            end
            frame.SetParent = function(self, parent)
                self.parent = parent
            end
            frame.SetSize = function(self, w, h)
            end
            frame.SetPoint = function(self, anchor, x, y)
            end
            frame.SetText = function(self, text)
                self.__text = text
                -- The client fires OnTextChanged when the text changes.
                -- Without it the bridge's "the player typed a price" guard
                -- could never be exercised, and the test that checks it
                -- would pass by never running the code at all.
                local handler = self.scripts["OnTextChanged"]
                if handler then handler(self, text) end
            end
            -- A real EditBox has focus, and the bridge uses it to tell the
            -- player typing from Auctionator writing the box itself.
            frame.HasFocus = function(self)
                return self.__focused == true
            end
            frame.SetFocus = function(self)
                self.__focused = true
            end
            frame.ClearFocus = function(self)
                self.__focused = false
            end
            frame.SetWidth = function(self, w)
            end
            frame.SetHeight = function(self, h)
            end
            frame.IsShown = function(self)
                return true
            end
            frame.GetRegions = function(self)
                return { { GetText = function() return self.__text end } }
            end
            frame.GetText = function(self)
                return self.__text
            end
            -- The frame API the addon actually calls while loading.
            -- These are stubs, not behaviour: they exist so the file can
            -- execute, and each test below asserts the one interaction it
            -- cares about.  Anything that changes state keeps it on the
            -- frame so an assertion can read it back.
            frame.RegisterEvent = function(self, event)
                self.events[event] = true
            end
            frame.UnregisterEvent = function(self, event)
                self.events[event] = nil
            end
            frame.RegisterAllEvents = function(self)
                self.__allEvents = true
            end
            frame.Show = function(self) self.__shown = true end
            frame.Hide = function(self) self.__shown = false end
            frame.SetShown = function(self, shown) self.__shown = shown end
            frame.IsVisible = function(self) return self.__shown ~= false end
            frame.Enable = function(self) self.__enabled = true end
            frame.AddMessage = function(self, text)
                self.__messages = self.__messages or {}
                table.insert(self.__messages, text)
            end
            frame.CreateTexture = function(self)
                return { SetTexture = function() end, SetSize = function() end,
                         SetPoint = function() end, Hide = function() end,
                         Show = function() end, SetColorTexture = function() end }
            end
            frame.CreateFontString = function(self)
                return { SetText = function() end, SetPoint = function() end,
                         SetFont = function() end, Show = function() end,
                         Hide = function() end }
            end
            frame.HookScript = function(self, script, handler)
                local current = self.scripts[script]
                if type(current) ~= "function" then
                    self.scripts[script] = handler
                else
                    self.scripts[script] = function(...)
                        handler(...)
                        return current(...)
                    end
                end
            end
            -- No metatable.  The methods live on the frame itself, so a
            -- plain lookup finds them; and a self-referential ``__index``
            -- (``setmetatable(frame, {__index = frame})``) makes every
            -- *missing* key -- ``box.__dfaWatched``, the first thing the
            -- bridge reads -- recurse until Lua raises "index chain too long".
            return frame
        end
        _G.StaticPopup_Hide = function() end
        _G.PlaySoundFile = function() return true end
        -- Globals the addon touches at load time.  Stubs, so the file can be
        -- executed and its real logic exercised; each test asserts the
        -- interaction it cares about.
        _G.SlashCmdList = {}
        -- The addon namespace object, created empty: ``_load_addon`` fills it,
        -- and a test that only wants the bridge sets its own stub on it.
        _G.Addon = _G.Addon or {}
        _G.GameTooltip = CreateFrame("GameTooltip", "GameTooltip", nil, nil)
        _G.UIParent = CreateFrame("Frame", "UIParent", nil, nil)
        _G.GetTime = function() return 1792450000 end
        _G.wipe = function(t) for k in pairs(t) do t[k] = nil end return t end
        _G.strsplit = function(sep, text)
            local out = {}
            for piece in string.gmatch(text, "([^" .. sep .. "]+)") do
                table.insert(out, piece)
            end
            return table.unpack(out)
        end
        _G.Mixin = function(obj, ...)
            for _, mixin in ipairs({...}) do
                for name, value in pairs(mixin) do
                    if type(value) == "function" then
                        obj[name] = value
                    end
                end
            end
            return obj
        end
        _G.ClearCursor = function() end
        _G.DFA_ESTIMATE = nil
        _G.DFA_ESTIMATE_SOURCE = nil
        _G.GetRealmName = function() return _G.DFA_TEST_REALM end
        _G.time = function() return 1792450000 end
        _G.C_Item = { GetItemInfo = function() return "item:1234", "item:1234" end }
        _G.issecretvalue = function() return false end
        _G.AuctionHouseFrame = { IsShown = function() return true end }
        _G.hooksecurefunc = function() end
    """)
    return lua


def fire(lua, event, *args):
    """Deliver a client event to every frame that registered for it.

    Returns how many handlers ran, so a test can tell "the addon ignored the
    event" from "nothing was listening".
    """
    call = lua.eval("""
        function(event, ...)
            local fired = 0
            for _, frame in ipairs(_G.__frames or {}) do
                if frame.events and frame.events[event] then
                    local handler = frame.scripts and frame.scripts["OnEvent"]
                    if handler then
                        handler(frame, event, ...)
                        fired = fired + 1
                    end
                end
            end
            return fired
        end
    """)
    return call(event, *args)


def load_addon(lua):
    addon_dir = ROOT / "DFA_Estimate"
    lua.execute("""
        _G.Addon = _G.Addon or {}
        _G.DFA_ESTIMATE_DB = _G.DFA_ESTIMATE_DB or {}
        _G.Addon.DFAEstimateAuctionator = {}
    """)
    lua.eval("function(src) return assert(load(src, 'addon.lua')) end")(
        DATA.read_text(encoding="utf-8"))("DFA_Estimate", lua.globals().Addon)
    lua.eval("function(src) return assert(load(src, 'addon.lua')) end")(
        MAIN.read_text(encoding="utf-8"))("DFA_Estimate", lua.globals().Addon)
    lua.eval("function(src) return assert(load(src, 'addon.lua')) end")(
        TOOLTIP.read_text(encoding="utf-8"))("DFA_Estimate", lua.globals().Addon)
    lua.eval("function(src) return assert(load(src, 'addon.lua')) end")(
        PANEL.read_text(encoding="utf-8"))("DFA_Estimate", lua.globals().Addon)
    lua.eval("function(src) return assert(load(src, 'addon.lua')) end")(
        BRIDGE.read_text(encoding="utf-8"))("DFA_Estimate", lua.globals().Addon)
    # A loaded addon is not yet a *running* one: the tooltip hook and the
    # panel are installed from ADDON_LOADED / PLAYER_LOGIN, exactly as the
    # client does it.  Without firing them the harness tested the file
    # before the code that does the work had run.
    fire(lua, "ADDON_LOADED", "DFA_Estimate")
    fire(lua, "PLAYER_LOGIN")


