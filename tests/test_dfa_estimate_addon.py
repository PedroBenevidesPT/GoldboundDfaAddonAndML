"""Contract tests for the DFA Estimate -> Auctionator bridge.

The bridge is the only place where the DFA estimate reaches the sale item
frame, so a regression there is exactly what the user reported as
"Auctionator priceSet blocked". These tests load the bridge into a real Lua
runtime and verify the fill path, the reseed behavior and the user-choice
guardrail. The Lua source under test is the file that goes into the addon
folder, never a stub.
"""
import math
from pathlib import Path

import lupa
import pytest

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "DFA_Estimate" / "UI" / "Auctionator.lua"
PANEL = ROOT / "DFA_Estimate" / "UI" / "Panel.lua"
TOOLTIP = ROOT / "DFA_Estimate" / "UI" / "Tooltip.lua"
MAIN = ROOT / "DFA_Estimate" / "DFA_Estimate.lua"
DATA = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"
LOCALIZATION = ROOT / "DFA_Estimate" / "Localization.lua"


def _woW_like_lua():
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
            if template then
                frame:SetScript = function(self, script, handler)
                    self.scripts[script] = handler
                end
                frame:GetScript = function(self, script)
                    return self.scripts[script]
                end
                frame:SetParent = function(self, parent)
                    self.parent = parent
                end
                frame:SetSize = function(self, w, h)
                end
                frame:SetPoint = function(self, anchor, x, y)
                end
                frame:SetText = function(self, text)
                    self.__text = text
                end
                frame:SetWidth = function(self, w)
                end
                frame:SetHeight = function(self, h)
                end
                frame:IsShown = function(self)
                    return true
                end
                frame:GetRegions = function(self)
                    return { { GetText = function() return self.__text end } }
                end
                frame:GetText = function(self)
                    return self.__text
                end
                frame:HookScript = function(self, script, handler)
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
                frame.HookScript = frame.HookScript
                frame.__index = frame
                setmetatable(frame, frame)
            end
            return frame
        end
        _G.StaticPopup_Hide = function() end
        _G.PlaySoundFile = function() return true end
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


def _load_addon(lua):
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


def test_the_bridge_file_is_complete_and_scanned():
    """Every file the test loads is the one outside this harness.

    If the addon folder drifts from the project, the fix it tests is not the
    file the client will load.  Keep the harness and the rewrite under review
    synchronized so that a regenerated file is tested before it is shipped.
    """
    text = BRIDGE.read_text(encoding="utf-8")
    assert "ApplyEstimate" in text
    assert "OnPriceSet" in text
    assert "Enable" in text
    assert "SetEnabled" in text
    assert "UpdateSalesPrice" in text


def test_the_bridge_fills_the_price_field_and_reseeds_it():
    """Auctionator sets the price more than once per item selection.

    The bridge must not fill once and go quiet: the estimate has to win over
    the database price and the searched-market price that arrive after the
    item is selected, unless the player typed a price themselves.
    """
    lua = _woW_like_lua()
    lua.execute("""
        _G.Addon.DFAEstimate = {
            data = {
                realms = {
                    Dragonmaw = {
                        items = {
                            [1234] = { price = 1000, conf = 0.5, n = 2 },
                        },
                    },
                },
            },
            Estimate = function(self, itemId)
                local realm = self.data.realms.Dragonmaw
                return realm and realm.items[itemId]
            end,
        }
    """)
    _load_addon(lua)
    addon = lua.globals().Addon
    bridge = addon.DFAEstimateAuctionator

    lua.execute("""
        _G.__calls = {}
        _G.__frame = {
            itemInfo = { itemID = 1234, location = {} },
            Price = { MoneyInput = { GoldBox = { GetScript = function() return nil end } } },
            UpdateSalesPrice = function(self, price)
                _G.__calls.price = price
                self.__price = price
            end,
        }
        _G.AuctionatorSellingFrame = { SaleItemFrame = _G.__frame }
    """)
    bridge.SetEnabled(True)
    assert bridge.IsEnabled()

    # first fill: the estimate wins
    frame = lua.globals().__frame
    assert bridge.ApplyEstimate(frame) is True
    assert lua.globals().__calls.price == 1053   # 1000 / 0.95, rounded up to silver
    assert bridge.GetLastFill().itemId == 1234
    assert bridge.GetLastFill().confirmed is True

    # Auctionator refreshes the field from its own market query; our estimate
    # goes back in because the player has not chosen a price
    lua.execute("""
        _G.__calls = {}
    """)
    frame.UpdateSalesPrice(frame, 999)
    assert lua.globals().__calls.price == 999      # Auctionator's own update ran
    bridge.ApplyEstimate(frame)                     # the hook calls OnPriceSet
    assert lua.globals().__calls.price == 1053     # our estimate reseeded it

    # but a price the user typed after the fill must win
    lua.execute("""
        _G.__calls = {}
        _G.__filled = nil
    """)
    frame.Price.MoneyInput.GoldBox:SetText("2000")
    assert bridge.GetLastFill().confirmed is False
    bridge.ApplyEstimate(frame)
    assert lua.globals().__calls.price == 2000


def test_the_bridge_never_touches_undercutting_or_posting():
    """The blocked flow the user saw is "Auctionator priceSet is blocked".

    That phrase is about setting the sale-item price, not about posting the
    auction.  The bridge must stay inside the price field and never call the
    post/undercut APIs directly -- those are the buttons the player still
    clicks themselves.
    """
    text = BRIDGE.read_text(encoding="utf-8")
    forbidden = (
        "PostItem", "RequestPost", "ConfirmPost",
        "AUCTION_SALES_PREFERENCE", "UNDERCUT_PERCENTAGE",
    )
    for name in forbidden:
        assert name not in text, f"bridge must not touch {name}"


def test_the_bridge_waits_for_the_selling_tab_and_then_hooks_it():
    """Auctionator builds the selling tab only when the Auction House opens.

    The bridge must keep trying until the tab exists, and once it exists the
    mixin UpdateSalesPrice hook must be in place so a reseed actually fires.
    """
    lua = _woW_like_lua()
    lua.execute("""
        _G.Addon = { DFAEstimate = { data = { realms = {} }, Estimate = function() end } }
    """)
    _load_addon(lua)
    bridge = lua.globals().Addon.DFAEstimateAuctionator
    bridge.SetEnabled(True)

    # no tab yet: the bridge reports that it is not hooked
    assert bridge.IsEnabled()
    assert bridge.Enable(bridge) is False
    assert not bridge.GetDiagnostics().hooked

    # the auction house opens and the tab is created
    lua.execute("""
        _G.AuctionatorSellingFrame = {
            SaleItemFrame = {
                itemInfo = { itemID = 1234, location = {} },
                Price = { MoneyInput = { GoldBox = { GetScript = function() return nil end } } },
                UpdateSalesPrice = function(self, price) self.__price = price end,
            },
        }
        _G.AuctionatorSaleItemMixin = {
            UpdateSalesPrice = function(self) self.__mixinCall = true end,
        }
    """)

    watch = lua.globals().Addon.DFAEstimateAuctionator.watch
    watch.scripts["OnEvent"](watch, "AUCTION_HOUSE_SHOW")

    assert bridge.Enable(bridge) is True
    assert bridge.GetDiagnostics().hooked is True


def test_the_tooltip_uses_tooltips_post_call_not_sethyperlink():
    """The tooltip must follow every item tooltip, not only hyperlink ones.

    Hooking SetHyperlink misses the bags, bank and auction house tooltips.
    The modern path is AddTooltipPostCall with the Item kind.
    """
    lua = _woW_like_lua()
    _load_addon(lua)
    post = lua.globals().__post
    assert post is not None
    assert post.kind == 7


def test_the_addon_initializes_without_a_localization_file_error():
    """The addon folder must load cleanly from the project, including the
    localization file listed in the toc.
    """
    assert LOCALIZATION.exists(), "Localization.lua must exist for the toc"
    assert DATA.exists(), "DFA_Estimate_Data.lua must exist for the toc"
    for path in MAIN, TOOLTIP, PANEL, BRIDGE:
        assert path.exists(), f"{path.name} must exist"


def test_the_installed_copy_matches_the_project_after_a_deploy():
    """A change in the project that never reached the client is not a fix."""
    installed = Path(r"C:\Program Files (x86)\World of Warcraft\_retail_"
                     r"\Interface\AddOns\DFA_Estimate")
    if not installed.exists():
        pytest.skip("the game is not installed on this machine")
    project = ROOT / "DFA_Estimate"
    names = ["Localization.lua", "DFA_Estimate.lua",
             "Data/DFA_Estimate_Data.lua", "UI/Tooltip.lua",
             "UI/Panel.lua", "UI/Auctionator.lua",
             "Integration/LootAppraiser.lua", "Integration/MobInfo2.lua",
             "DFA_Estimate.toc"]
    for name in names:
        assert (project / name).read_bytes() == (installed / name).read_bytes(), name
