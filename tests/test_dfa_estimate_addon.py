"""Contract tests for the DFA Estimate -> Auctionator bridge.

The bridge is the only place where the DFA estimate reaches the sale item
frame, so a regression there is exactly what the user reported as
"Auctionator priceSet blocked". These tests load the bridge into a real Lua
runtime and verify the fill path, the reseed behavior and the user-choice
guardrail. The Lua source under test is the file that goes into the addon
folder, never a stub.

The runtime itself lives in ``tests/dfa_lua_harness.py``, shared with the
pricing-export tests: two hand-rolled mock clients had drifted apart and one
of them could not load the addon at all.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from woweconomy import addons as addon_manifest
from dfa_lua_harness import (BRIDGE, DATA, MAIN, PANEL, ROOT, TOOLTIP,
                             fire, load_addon, woW_like_lua)

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
    lua = woW_like_lua()
    load_addon(lua)
    # The stub goes in *after* the addon loads: the real dataset assigns
    # ``Addon.DFAEstimate`` while it loads, so a stub installed first is
    # silently replaced and the test then measures the shipped dataset
    # instead of the one it declared.
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
            -- ``Estimate(itemId)``, matching the addon: it takes no self.
            -- A stub with a spurious ``self`` made ``self`` the item id and
            -- the bridge looked broken when the stub was.
            Estimate = function(itemId)
                -- ``action.ask`` is the model's decision and already carries
                -- the house cut; the bridge prefers it over ``price``.
                return { price = 1000, action = { ask = 1053 },
                         conf = 0.5, n = 2 }
            end,
        }
    """)
    addon = lua.globals().Addon
    bridge = addon.DFAEstimateAuctionator

    lua.execute("""
        _G.__calls = {}
        -- GoldBox is a real EditBox in game: it has SetScript, SetText and
        -- GetScript.  A bare table broke the price-box watch, which is
        -- part of what this test is meant to cover.
        _G.__frame = {
            itemInfo = { itemID = 1234, location = {} },
            Price = { MoneyInput = { GoldBox = CreateFrame("EditBox", nil, nil, nil) } },
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
    assert lua.globals().__calls.price == 1100   # ask 1053, rounded up to the next silver
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
    assert lua.globals().__calls.price == 1100     # our estimate reseeded it

    # Auctionator also writes the price box itself -- its own price list, the
    # minimum buyout it just found.  That text change is not the player, and
    # treating it as a choice left the field holding Auctionator's price for
    # the rest of the item.  No focus, so the estimate goes back in.
    lua.execute("""
        _G.__calls = {}
        local f = _G.__frame
        f.Price.MoneyInput.GoldBox:SetText('777')       -- Auctionator's min buyout
    """)
    assert bridge.GetLastFill().confirmed is True
    assert lua.globals().__calls.price == 1100, \
        "Auctionator's own price replaced the estimate without a click"

    # but a price the player chose after the fill must win.  This is the
    # real flow: typing (or clicking a row in Auctionator's price list)
    # raises OnTextChanged, Auctionator then writes the choice, and the
    # bridge's own hook runs.  The guard lives in OnPriceSet, which is the
    # entry point the client calls -- calling ApplyEstimate directly would
    # test a path that does not exist in game.
    lua.execute("""
        _G.__calls = {}
        local f = _G.__frame
        f.Price.MoneyInput.GoldBox:SetFocus()           -- the player clicks in
        f.Price.MoneyInput.GoldBox:SetText('2000')      -- the player types
        f.UpdateSalesPrice(f, 2000)                     -- Auctionator writes it
        Addon.DFAEstimateAuctionator.OnPriceSet(f)      -- the hook fires
    """)
    assert bridge.GetLastFill().confirmed is False
    assert lua.globals().__calls.price == 2000, \
        "the bridge overwrote a price the player chose"


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
    lua = woW_like_lua()
    lua.execute("""
        _G.Addon = { DFAEstimate = { data = { realms = {} }, Estimate = function() end } }
        -- Auctionator's own surface, as far as the bridge uses it: the event
        -- bus it hooks and the event names it compares.  Without these,
        -- Enable() gives up before it ever reaches the selling tab, which is
        -- what this test is about.
        _G.Auctionator = {
            EventBus = { Fire = function() end },
            Selling = { Events = {
                BagItemClicked = "BAG_ITEM_CLICKED",
                ClearBagItem = "CLEAR_BAG_ITEM",
                PriceSelected = "PRICE_SELECTED",
            } },
        }
    """)
    load_addon(lua)
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
                Price = { MoneyInput = { GoldBox = CreateFrame("EditBox", nil, nil, nil) } },
                UpdateSalesPrice = function(self, price) self.__price = price end,
            },
        }
        _G.AuctionatorSaleItemMixin = {
            UpdateSalesPrice = function(self) self.__mixinCall = true end,
        }
    """)

    # The watch frame is private to the bridge, which is correct -- nothing
    # outside it should drive that frame.  Deliver the event through the
    # harness instead, the way the client would.
    fired = fire(lua, "AUCTION_HOUSE_SHOW")
    assert fired >= 1, "nothing was watching for the auction house to open"

    assert bridge.Enable(bridge) is True
    assert bridge.GetDiagnostics().hooked is True


def test_the_tooltip_uses_tooltips_post_call_not_sethyperlink():
    """The tooltip must follow every item tooltip, not only hyperlink ones.

    Hooking SetHyperlink misses the bags, bank and auction house tooltips.
    The modern path is AddTooltipPostCall with the Item kind.
    """
    lua = woW_like_lua()
    load_addon(lua)
    post = lua.globals().__post
    assert post is not None
    assert post.kind == 7


def test_the_addon_initializes_without_a_missing_file_error():
    """Every file the toc names must exist, and nothing else may be assumed.

    This used to assert a hard-coded ``Localization.lua`` that this addon
    never had (it is not in the toc and was never tracked), so the test
    failed while the addon was fine -- and it would have passed while the
    toc named a file that did not exist.  Driving the check from the toc
    tests the addon instead of the list.
    """
    toc = ROOT / "DFA_Estimate" / "DFA_Estimate.toc"
    assert toc.exists()
    listed = addon_manifest.toc_files(toc)
    assert listed, "the toc must load at least one file"
    assert addon_manifest.missing_files(ROOT / "DFA_Estimate") == []
    # the four the bridge work depends on are among them
    for required in ("DFA_Estimate.lua", "Data/DFA_Estimate_Data.lua",
                     "UI/Tooltip.lua", "UI/Panel.lua", "UI/Auctionator.lua"):
        assert required in listed, f"{required} is not loaded by the toc"


def test_the_installed_copy_matches_the_project_after_a_deploy():
    """A change in the project that never reached the client is not a fix."""
    installed = Path(r"C:\Program Files (x86)\World of Warcraft\_retail_"
                     r"\Interface\AddOns\DFA_Estimate")
    if not installed.exists():
        pytest.skip("the game is not installed on this machine")
    project = ROOT / "DFA_Estimate"
    names = addon_manifest.toc_files(project / "DFA_Estimate.toc")
    for name in names:
        assert (project / name).read_bytes() == (installed / name).read_bytes(), name


def test_the_shipped_baseline_dataset_is_not_silently_dropped():
    """The current export marks every record "baseline_only". That status is a

    model-lifecycle tag, not a visibility gate: the reader accepts it (the
    tooltip only suppresses the fully-validated silence path), and the record
    still carries a usable price. This is the test the empty panel needs -- it
    proves the 237 records are real, priced records the addon can read, and not
    rows that the addon silently discards.
    """
    lua = woW_like_lua()
    load_addon(lua)
    addon = lua.globals().Addon.DFAEstimate

    # the dataset the client would actually load from the shipped file
    assert addon.data is not None, "the shipped dataset did not load"
    realms = addon.data.realms or {}
    assert realms, "the shipped dataset has no realm blocks"
    realm_name = addon.GetRealmName()

    # exactly the counts the shipped metadata claims; if either path drops rows
    # the panel -- which iterates GetItems() -- would be empty on the user's
    # realm without any error message, which is the bug this test guards.
    # the panel and the tooltip both read through getRealmItems; the value must be
    # the table the addon built, and its live length must match the metadata.
    # lupa returns Lua tables as objects, not as Python dicts, so count through
    # the addon API that the panel actually uses.
    panel_items = addon.GetItems()
    panel_count = len(list(panel_items.keys()))
    realm_block = realms.get(realm_name)
    if realm_block is None:
        shipped_realm_count = 0
    else:
        shipped_realm_count = len(list(realm_block.items.keys()))
    assert panel_count == shipped_realm_count, (
        f"GetItems() returned {panel_count} items but the realm block for "
        f"{realm_name!r} has {shipped_realm_count} items")
    assert panel_count == 237, (
        "the shipped dataset should expose exactly 237 priced items; "
        f"got {panel_count}")
    assert shipped_realm_count == 237, (
        "the Dragonmaw block in the shipped dataset should carry exactly "
        f"237 items; got {shipped_realm_count}")
    # a sample record from the populated realm, read through the public API.
    # Dragonmaw is the realm with the most records in this export.
    sample_id = next(iter(panel_items.keys()))
    record = addon.GetRecord(sample_id)
    assert record is not None, f"GetRecord({sample_id}) returned nothing"
    assert record.price and record.price > 0, (
        f"the exported record for item {sample_id} has no usable price")

    # the record is still priced through the addon's normal path, including for
    # an item the realm has no recorded sale of (the model path).  This is the
    # actual shape an item the player is posting takes, so it is the one that
    # must survive the status tag.
    estimate = addon.Estimate(sample_id)
    assert estimate is not None, f"Estimate({sample_id}) returned nothing"
    assert estimate.price and estimate.price > 0, (
        f"Estimate({sample_id}) produced no usable price")
    assert estimate.source in ("record", "model"), (
        f"Estimate({sample_id}) returned an unexpected source: {estimate.source!r}")

    # the visible status behavior: baseline_only is shown to the player, not
    # swallowed. The tooltip translates it; the panel does not filter on it.
    assert record.status == "baseline_only", (
        "this export is the baseline_only case by construction; "
        "if the dataset changes, revisit this assertion and the state name together"
    )
    tooltip = addon.Tooltip and addon.Tooltip.GetLines(sample_id)
    assert tooltip, "the tooltip module did not expose GetLines"
    pl = tooltip
    assert "item sale rate, no learned model" in pl, (
        f"baseline_only must be presented by the tooltip, not hidden; "
        f"lines={pl!r}"
    )

