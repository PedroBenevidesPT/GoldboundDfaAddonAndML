"""The panel must hand SetScrollChild a frame, not a font string.

``SetScrollChild(fontstring)`` raises "Wrong object type for function" and
the panel never opens; this runs the real Panel.lua against a mock client
whose SetScrollChild asserts the argument type.
"""
from pathlib import Path

import pytest
from lupa import LuaRuntime

ROOT = Path(__file__).resolve().parent.parent

MOCK = r"""
local function New(kind, parent, template)
    local obj = { kind = kind, parent = parent, template = template }
    setmetatable(obj, { __index = function(t, key)
        if key == "SetScrollChild" then
            return function(self, child)
                assert(type(child) == "table" and child.kind == "Frame",
                       "bad scroll child: " .. tostring(child and child.kind))
                self.scrollChild = child
            end
        end
        if key == "GetWidth" then return function() return 420 end end
        if key == "GetStringHeight" then return function() return 240 end end
        if key == "CreateFontString" then
            return function(self) return New("FontString", self) end
        end
        if key == "CreateTexture" then
            return function(self) return New("Texture", self) end
        end
        if key == "SetText" then
            return function(self, text) self.text = text end
        end
        if key == "GetText" then
            return function(self) return self.text end
        end
        if key == "SetSize" then
            return function(self, width, height)
                self.width, self.height = width, height
            end
        end
        return function() return t end
    end })
    return obj
end
function CreateFrame(kind, name, parent, template)
    return New(kind, parent, template)
end
function GetRealmName() return "Dragonmaw" end
function time() return 1792570000 end
SlashCmdList = {}
format = string.format
UIParent = New("Frame")
DFA_ESTIMATE_DB = {}
"""


@pytest.fixture(scope="module")
def panel():
    data = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"
    if not data.exists():
        pytest.skip("the dataset has not been generated")
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    lua.execute(data.read_text(encoding="utf-8"))
    load = lua.eval("function(src) local fn = assert(load(src)); return fn end")
    addon = lua.table()
    for name in ("DFA_Estimate.lua", "UI/Tooltip.lua", "UI/Panel.lua"):
        load((ROOT / "DFA_Estimate" / name).read_text(encoding="utf-8"))(
            "DFA_Estimate", addon)
    return addon


def test_the_scroll_child_is_a_frame_and_the_panel_opens(panel):
    view = panel.DFAEstimatePanel
    view.Initialize(view)

    assert panel.DFAEstimate.panelError is None
    view.Show(view)

    assert panel.DFAEstimate.panelError is None
    layout = view.GetLayout()
    # the content frame the panel handed to the scroll frame
    assert layout.content.kind == "Frame"
    assert layout.scroll.scrollChild.kind == "Frame"
    assert layout.content.width == 420
    # The content is sized to the report, not to a fixed 240.  A fixed
    # height clipped everything past the header, which is why the panel
    # looked empty; the viewport is the floor, never the ceiling.
    assert layout.content.height >= 240
    assert layout.shown


def test_a_missing_template_or_scroll_child_still_shows_a_panel():
    """A changed UI must degrade to a visible panel, not an invisible one.

    This is the failure the user sees: the frame is created, the template
    or ``SetScrollChild`` raises, and the command looks as if it did
    nothing.  The fallback must keep the frame, show it, and put the text
    directly on it.
    """
    strict = r"""
    local function New(kind, parent, template)
        local obj = { kind = kind, parent = parent, template = template,
                      shown = false }
        setmetatable(obj, { __index = function(t, key)
            if key == "SetScrollChild" then
                return function(self, child)
                    error("Wrong object type for function")
                end
            end
            if key == "CreateFontString" then
                return function(self) return New("FontString", self) end
            end
            if key == "CreateTexture" then
                return function(self) return New("Texture", self) end
            end
            if key == "SetText" then
                return function(self, text) self.text = text end
            end
            if key == "GetText" then
                return function(self) return self.text end
            end
            if key == "SetSize" then
                return function(self, width, height)
                    self.width, self.height = width, height
                end
            end
            if key == "GetWidth" then return function() return 420 end end
            if key == "GetStringHeight" then return function() return 240 end end
            if key == "Show" then
                return function(self) self.shown = true end
            end
            if key == "Hide" then
                return function(self) self.shown = false end
            end
            if key == "IsShown" then
                return function(self) return self.shown end
            end
            if key == "GetFrameStrata" then
                return function() return "FULLSCREEN_DIALOG" end
            end
            if key == "GetFrameLevel" then return function() return 100 end end
            return function() return t end
        end })
        return obj
    end
    function CreateFrame(kind, name, parent, template)
        if name then error("named frame: " .. tostring(name)) end
        if template and template ~= "BackdropTemplate" then
            error("unknown template: " .. tostring(template))
        end
        return New(kind, parent, template)
    end
    function GetRealmName() return "Dragonmaw" end
    function time() return 1792570000 end
    SlashCmdList = {}
    UIParent = New("Frame")
    DEFAULT_CHAT_FRAME = { messages = {}, AddMessage = function(self, text)
        self.messages[#self.messages + 1] = text end }
    DFA_ESTIMATE_DB = {}
    """
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(strict)
    data = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"
    lua.execute(data.read_text(encoding="utf-8"))
    load = lua.eval("function(src) local fn = assert(load(src)); return fn end")
    addon = lua.table()
    for name in ("DFA_Estimate.lua", "UI/Tooltip.lua", "UI/Panel.lua"):
        load((ROOT / "DFA_Estimate" / name).read_text(encoding="utf-8"))(
            "DFA_Estimate", addon)

    view = addon.DFAEstimatePanel
    assert view.Show(view) is True
    layout = view.GetLayout()
    assert layout.shown is True
    assert layout.strata == "FULLSCREEN_DIALOG"
    assert layout.text is not None
    assert "could not be built" not in (addon.DFAEstimate.panelError or "")
    # the scroll child failure is recorded, but the report is still readable
    assert "SetScrollChild" in (addon.DFAEstimate.panelError or "")
    assert "DFA Estimate" in layout.text.GetText(layout.text)
