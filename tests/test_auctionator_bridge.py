"""What the Auctionator bridge does when the player picks a price.

The bridge fills the buyout field with the DfA estimate, and must leave
alone a price the player chose -- either by typing it in or by clicking a
row in Auctionator's own price list.  A row click used to lose that
protection: the estimate was written straight back over the row the
player had just picked.

Clicking a row is the case the seller cares about, so it is tested as an
outcome and not only as an absence: the field must end up holding the
clicked row's price, and the addon must remember which price that was.

These tests load the real ``UI/Auctionator.lua`` into a Lua runtime and
drive the three paths that reach a price: a fill with no player choice, a
choice made through the event bus, and a fill deferred past a frame
boundary.

The harness is written in Lua, not Python.  lupa exposes a Python callable
as userdata, so ``type(frame.UpdateSalesPrice) == "function"`` -- a check
the bridge really makes -- would fail for a Python lambda.

``test_dfa_pricing.py`` cannot be reused: its harness has no ``CreateFrame``
and no Auctionator mixin, so the bridge never loads there.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import lupa
import pytest

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "DFA_Estimate" / "UI" / "Auctionator.lua"

ITEM_ID = 1234
ESTIMATE_COPPER = 250_000  # 25 gold
PICKED_COPPER = 199_999    # the price the player clicked


HARNESS = """
-- The parts of the client the bridge touches, all in Lua so that
-- type(x) == "function" holds where the bridge checks it.

local function FakeFrame()
    local f = {}
    function f:RegisterEvent() end
    function f:UnregisterEvent() end
    function f:SetScript() end
    function f:GetScript() return nil end
    function f:Show() end
    function f:Hide() end
    return f
end

_G.CreateFrame = function() return FakeFrame() end
_G.hooksecurefunc = function() end
_G.dfaMessages = {}
_G.DEFAULT_CHAT_FRAME = {
    AddMessage = function(self, message)
        table.insert(_G.dfaMessages, message)
    end,
}

-- C_Timer.After is how the bridge defers a fill by one frame, so that a
-- click's PriceSelected event lands before the estimate is written.  The
-- stub queues the callback instead of running it, which is what a real
-- frame boundary does; runTimers() is the frame boundary.
_G.dfaTimers = {}
_G.C_Timer = {
    After = function(seconds, fn)
        table.insert(_G.dfaTimers, fn)
    end,
}
_G.runTimers = function()
    local pending = _G.dfaTimers
    _G.dfaTimers = {}
    for _, fn in ipairs(pending) do
        fn()
    end
end

-- Auctionator's event bus.  Fire is a real Lua function because the
-- bridge refuses to wrap it otherwise.  It dispatches to the sale frame,
-- the way Auctionator's own subscription does: on a row click the frame
-- is told to write the row's buyout.
local function copperOf(value)
    if type(value) == "number" then return value end
    if type(value) ~= "table" then return nil end
    if type(value.buyout) == "number" then return value.buyout end
    local gold = value.gold or 0
    local silver = value.silver or 0
    local copper = value.copper or 0
    return gold * 10000 + silver * 100 + copper
end

_G.Auctionator = {
    Selling = {
        Events = {
            BagItemClicked = "BagItemClicked",
            ClearBagItem = "ClearBagItem",
            PriceSelected = "PriceSelected",
        },
    },
    EventBus = {
        Fire = function(self, source, event, ...)
            local target = _G.dfaFrame
            if not target then return end
            if event == "PriceSelected" then
                -- Auctionator's own handler: it writes the price of the
                -- row the player clicked.  No row, no write.
                local row = ...
                local price = copperOf(row and row.buyout)
                if price then
                    target:UpdateSalesPrice(price)
                end
            end
        end,
    },
}

-- The sale frame: one item, and a price field the bridge can write to.
_G.dfaFrame = {
    itemInfo = { itemID = 1234, itemLink = "item:1234" },
    writes = {},
}
function _G.dfaFrame:UpdateSalesPrice(salesPrice, bidPrice, preserve)
    table.insert(self.writes, salesPrice)
end

-- Auctionator's duration setting, with the shape the real addon has:
-- Config.Options holds string keys, Config.Get/Set read and write them,
-- and the value is a dropdown *index* (1 = 12 h, 2 = 24 h, 3 = 48 h),
-- not a number of hours.  See Auctionator/Source_ModernAH/Config/Main.lua.
_G.dfaAuctionatorConfig = {
    auction_duration = 2,          -- Auctionator's own default: 24 h
    save_last_duration_as_default = false,
}
_G.Auctionator.Config = {
    Options = {
        AUCTION_DURATION = "auction_duration",
        SAVE_LAST_DURATION_AS_DEFAULT = "save_last_duration_as_default",
    },
    Get = function(option)
        return _G.dfaAuctionatorConfig[option]
    end,
    Set = function(option, value)
        _G.dfaAuctionatorConfig[option] = value
    end,
}

-- The duration dropdown on the sale frame: SetSelectedValue/GetValue.
_G.dfaDuration = { value = 2 }
function _G.dfaDuration:SetSelectedValue(index)
    self.value = index
end
function _G.dfaDuration:GetValue()
    return self.value
end
_G.dfaFrame.Duration = _G.dfaDuration

-- Publish the sale frame as the selling tab's, so the bridge's "the frame
-- on screen" lookup finds it.  Called after Enable(), which is when a real
-- client builds the tab.
_G.dfaShowSaleFrame = function()
    _G.AuctionatorSellingFrame = { SaleItemFrame = _G.dfaFrame }
end
"""


class Bridge:
    """The real bridge, a fake sale frame, and a one-item dataset."""

    def __init__(self) -> None:
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(HARNESS)
        globals_ = self.lua.globals()

        # The addon object the bridge is loaded with.  Estimate is called
        # from Lua but only ever compared and unpacked, never type-checked.
        record = self.lua.table_from({
            "itemId": ITEM_ID,
            "conf": 0.9,
            "n": 12,
            "source": "sentinel",
            "action": self.lua.table_from({"ask": ESTIMATE_COPPER}),
        })
        self.addon = self.lua.table_from({
            "DFAEstimate": self.lua.table_from({
                "data": self.lua.table_from({}),
                "Estimate": lambda item_id: record,
            }),
        })
        globals_.Addon = self.addon

        loader = self.lua.eval(
            "function(src) return assert(load(src, 'Auctionator.lua')) end"
        )
        loader(BRIDGE.read_text(encoding="utf-8"))(
            "DFA_EstimateAuctionator", self.addon
        )

        self.api = globals_.Addon.DFAEstimateAuctionator
        self.frame = globals_.dfaFrame
        # The bridge only writes to chat when debug is on; the messages are
        # how these tests see which path it took.
        self.api.SetDebug(True)

    # -- driving ------------------------------------------------------
    def enable(self) -> None:
        """Install the event-bus wrapper.

        ``Enable()`` returns False here because this harness has no
        Auctionator mixin or selling frame to hook; that is expected.  What
        these tests need is the bus wrapper, which is installed first and
        independently, and which ``GetDiagnostics`` reports.
        """
        self.api.Enable()
        assert self.api.GetDiagnostics().fireHooked is True, (
            "the event bus must be wrapped, or a row click cannot be seen"
        )

    def fill(self) -> bool:
        return bool(self.api.ApplyEstimate(self.frame))

    def auctionator_sets(self, price=None) -> None:
        """Only the post-hook body, as it runs after Auctionator's write."""
        if price is None:
            self.api.OnPriceSet(self.frame)
        else:
            self.api.OnPriceSet(self.frame, price)

    def auctionator_reseeds(self, price: int) -> None:
        """Auctionator writes a price of its own, then the post-hook runs.

        Its database, its market search and the vendor value of a piece of
        equipment all reach the frame this way.
        """
        self.frame.UpdateSalesPrice(self.frame, price, None, None)
        self.api.OnPriceSet(self.frame, price)

    def fire(self, event: str, *args) -> None:
        """Raise one event through the wrapped Fire, in Auctionator's order."""
        bus = self.lua.globals().Auctionator.EventBus
        bus.Fire(bus, "AuctionatorSellingFrame", event, *args)

    def click_row(self, buyout: int = PICKED_COPPER) -> None:
        """Click a row in Auctionator's price list.

        The event carries the row, the row carries its buyout, and
        Auctionator's own handler writes that buyout into the price field.
        """
        row = self.lua.table_from({"buyout": buyout})
        self.fire("PriceSelected", row)

    def run_timers(self) -> None:
        """Cross a frame boundary, running anything C_Timer queued."""
        self.lua.globals().runTimers()

    def chosen_price(self):
        value = self.api.GetChosenPrice()
        return None if value is None else int(value)

    def last_fill(self):
        return self.api.GetLastFill()

    def writes(self) -> List[int]:
        """Every price written to the frame, in order."""
        return [int(p) for p in self.frame.writes.values()]

    def messages(self) -> List[str]:
        return [str(m) for m in self.lua.globals().dfaMessages.values()]

    # -- hours of posting ---------------------------------------------
    def show_sale_frame(self) -> None:
        """Publish the sale frame as the selling tab's, as the client does."""
        self.lua.globals().dfaShowSaleFrame()

    def posting_hours(self) -> int:
        return int(self.api.GetPostingHours())

    def set_posting_hours(self, hours):
        ok, message = self.api.SetPostingHours(hours)
        return bool(ok), str(message)

    def auctionator_duration(self) -> int:
        """The index in Auctionator's own config (1 = 12 h, 2 = 24 h, 3 = 48 h)."""
        return int(self.lua.globals().dfaAuctionatorConfig.auction_duration)

    def duration_dropdown(self) -> int:
        """The index the open frame's dropdown shows."""
        return int(self.lua.globals().dfaDuration.value)


@pytest.fixture
def bridge() -> Bridge:
    return Bridge()


# ---------------------------------------------------------------------------
# the plain case
# ---------------------------------------------------------------------------
def test_a_fill_with_no_player_choice_uses_the_estimate(bridge):
    assert bridge.fill() is True

    last = bridge.last_fill()
    assert last is not None
    assert int(last.price) == ESTIMATE_COPPER
    assert int(last.itemId) == ITEM_ID
    assert bridge.writes() == [ESTIMATE_COPPER]


def test_the_bridge_writes_through_the_frames_own_price_call(bridge):
    """No price widget here, so the fallback path is the one exercised."""
    assert bridge.fill() is True
    assert "UpdateSalesPrice" in " ".join(bridge.messages())


# ---------------------------------------------------------------------------
# the regression: a row click must put the clicked price in the field
# ---------------------------------------------------------------------------
def test_clicking_a_price_row_puts_the_clicked_price_in_the_field(bridge):
    """The reported bug, stated as the outcome the seller expects.

    Clicking a row in Auctionator's price list must leave that row's
    price in the buyout field.  The bridge used to put the estimate
    straight back on top of it.
    """
    bridge.enable()

    # A normal fill first, so there is an estimate that could be re-applied.
    assert bridge.fill() is True
    assert bridge.writes() == [ESTIMATE_COPPER]

    # The player clicks a row: the event carries the row, and Auctionator
    # writes its buyout into the field.
    bridge.click_row(PICKED_COPPER)

    assert bridge.writes()[-1] == PICKED_COPPER, (
        "the field must end up holding the price of the row the player "
        "clicked"
    )


def test_the_clicked_price_is_not_overwritten_by_the_estimate(bridge):
    bridge.enable()
    assert bridge.fill() is True

    bridge.click_row(PICKED_COPPER)
    # The hook body runs after Auctionator's write, as it does in the game,
    # and queues a fill for the next frame.  Running it is what proves the
    # queued fill sees the click: without that, the click is only safe
    # until the next frame boundary.
    bridge.auctionator_sets(PICKED_COPPER)
    bridge.run_timers()

    assert bridge.writes() == [ESTIMATE_COPPER, PICKED_COPPER], (
        "the estimate must not be written again over a price the player "
        "picked from Auctionator's price list"
    )


def test_a_row_click_is_recorded_as_the_chosen_price(bridge):
    """The addon has to know *what* was clicked, not only that something was."""
    bridge.enable()
    assert bridge.fill() is True
    assert bridge.chosen_price() is None

    bridge.click_row(PICKED_COPPER)
    bridge.auctionator_sets(PICKED_COPPER)

    assert bridge.chosen_price() == PICKED_COPPER
    assert bridge.api.GetDiagnostics().chosenPrice == PICKED_COPPER


def test_clicking_a_second_row_moves_the_chosen_price(bridge):
    """Every click is a fresh choice, so the record follows the newest one."""
    bridge.enable()
    bridge.fill()
    bridge.click_row(PICKED_COPPER)
    bridge.auctionator_sets(PICKED_COPPER)

    second = 150_000
    bridge.click_row(second)
    bridge.auctionator_sets(second)

    assert bridge.chosen_price() == second
    assert bridge.writes()[-1] == second


def test_a_clicked_price_survives_a_later_auctionator_reseed(bridge):
    """Auctionator sets the price more than once for one item.

    Its database, its market search and the vendor value of a piece of
    equipment all write a price after the item is selected.  Once the
    player has clicked, none of those may pull the estimate back in.
    """
    bridge.enable()
    bridge.fill()
    bridge.click_row(PICKED_COPPER)

    bridge.auctionator_sets(PICKED_COPPER)   # the click itself
    bridge.auctionator_reseeds(120_000)      # a reseed from its own database

    assert bridge.writes() == [ESTIMATE_COPPER, PICKED_COPPER, 120_000]
    assert bridge.chosen_price() == 120_000, (
        "the reseed is what the field holds now, and it is not ours to "
        "replace either"
    )


def test_a_row_click_marks_the_fill_as_not_confirmed(bridge):
    bridge.enable()
    assert bridge.fill() is True
    assert bool(bridge.last_fill().confirmed) is True

    bridge.click_row(PICKED_COPPER)

    assert bool(bridge.last_fill().confirmed) is False, (
        "the last fill was superseded by the player's choice"
    )


# ---------------------------------------------------------------------------
# the ordering: a deferred fill must not race the click
# ---------------------------------------------------------------------------
def test_a_deferred_fill_does_not_overwrite_the_click_it_lost_the_race_to(bridge):
    """The fill is deferred one frame so the click's event lands first.

    Without the deferral the estimate is written synchronously, inside
    Auctionator's own price call, before ``PriceSelected`` has been seen.
    """
    bridge.enable()
    bridge.fill()

    # Auctionator writes a price; the bridge schedules a fill for the next
    # frame instead of writing immediately.
    bridge.auctionator_reseeds(0)
    assert bridge.writes() == [ESTIMATE_COPPER, 0], (
        "the deferred fill must not write inside Auctionator's own call"
    )

    # The player's click is seen before the frame boundary.
    bridge.click_row(PICKED_COPPER)
    bridge.run_timers()

    assert bridge.writes() == [ESTIMATE_COPPER, 0, PICKED_COPPER], (
        "the queued fill must see the click and stand down"
    )


def test_a_deferred_fill_still_lands_when_nobody_chose(bridge):
    """The deferral must not quietly turn the fill off."""
    bridge.enable()
    bridge.auctionator_reseeds(0)

    assert bridge.writes() == [0], "nothing of ours is written inside the hook"
    bridge.run_timers()

    assert bridge.writes() == [0, ESTIMATE_COPPER]


# ---------------------------------------------------------------------------
# a price read out of the event
# ---------------------------------------------------------------------------
def test_a_money_table_buyout_is_read_as_copper(bridge):
    """A buyout is sometimes a money table, not a number."""
    bridge.enable()
    bridge.fill()

    row = bridge.lua.table_from({
        "buyout": bridge.lua.table_from({"gold": 20, "silver": 0, "copper": 0}),
    })
    bridge.fire("PriceSelected", row)

    assert bridge.chosen_price() == 200_000


def test_an_unreadable_click_still_stops_the_fill(bridge):
    """Not knowing the price is not a reason to overwrite the click."""
    bridge.enable()
    bridge.fill()

    bridge.fire("PriceSelected")   # no row at all
    bridge.auctionator_sets()

    assert bridge.chosen_price() is None
    assert bridge.writes() == [ESTIMATE_COPPER], (
        "an unreadable click is still a choice: the estimate stays out"
    )


# ---------------------------------------------------------------------------
# the choice belongs to one item
# ---------------------------------------------------------------------------
def test_the_choice_is_forgotten_when_the_item_changes(bridge):
    bridge.enable()
    bridge.fill()
    bridge.click_row(PICKED_COPPER)
    bridge.auctionator_sets(PICKED_COPPER)
    assert bridge.chosen_price() == PICKED_COPPER

    # A different item is selected: the previous choice belonged to the
    # previous one, so the estimate fills again.
    bridge.fire("BagItemClicked")
    assert bridge.chosen_price() is None

    assert bridge.fill() is True
    assert bridge.writes()[-1] == ESTIMATE_COPPER


def test_a_fill_is_not_reapplied_when_the_bridge_is_disabled(bridge):
    bridge.api.SetEnabled(False)
    assert bridge.fill() is False
    assert bridge.writes() == []


def test_the_estimate_is_not_divided_by_the_consignment_rate(bridge):
    """The estimate is what the buyer pays; the cut comes off afterwards."""
    bridge.fill()
    assert bridge.writes() == [ESTIMATE_COPPER]


# ---------------------------------------------------------------------------
# hours of posting
# ---------------------------------------------------------------------------
def test_the_posting_duration_starts_at_auctionators_own_default(bridge):
    """An untouched setting must agree with what the selling tab shows."""
    assert bridge.posting_hours() == 24
    assert bridge.auctionator_duration() == 2


def test_setting_the_posting_duration_reaches_auctionators_config(bridge):
    bridge.enable()
    bridge.show_sale_frame()

    ok, message = bridge.set_posting_hours(48)

    assert ok is True
    assert bridge.auctionator_duration() == 3, (
        "Auctionator's config is what its SetDuration() reads, so the "
        "setting has to land there and not only in the dropdown"
    )
    assert bridge.duration_dropdown() == 3, "the open frame must follow too"
    assert "48 hours" in message


def test_a_duration_between_the_three_is_refused(bridge):
    """Posting for 18 hours is not a thing; rounding to 24 is not ours to do."""
    ok, message = bridge.set_posting_hours(18)

    assert ok is False
    assert "12, 24 or 48" in message
    assert bridge.posting_hours() == 24, "the setting is unchanged"
    assert bridge.auctionator_duration() == 2, "Auctionator is untouched"


def test_the_duration_is_written_where_auctionator_reseeds_it_from(bridge):
    """A new item reseeds the dropdown from config, so config must hold."""
    bridge.enable()
    bridge.show_sale_frame()
    bridge.set_posting_hours(12)

    # What Auctionator does for the next item: SetDuration() reads its
    # config and pushes it into the dropdown.
    bridge.lua.execute(
        "dfaDuration:SetSelectedValue(dfaAuctionatorConfig.auction_duration)"
    )

    assert bridge.duration_dropdown() == 1


def test_the_posting_duration_survives_a_reload(bridge):
    bridge.api.LoadSettings()
    assert bridge.posting_hours() == 24

    bridge.set_posting_hours(48)
    bridge.api.LoadSettings()      # a reload

    assert bridge.posting_hours() == 48


def test_a_saved_value_that_is_not_a_duration_is_ignored(bridge):
    """A hand-edited saved variable must not become a broken setting."""
    bridge.lua.execute("DFA_ESTIMATE_DB = { postingHours = 30 }")

    bridge.api.LoadSettings()

    assert bridge.posting_hours() == 24


def test_the_setting_is_not_pushed_to_auctionator_before_the_player_chose(bridge):
    """Changing another addon's setting on login, unasked, is not a default."""
    bridge.lua.globals().dfaAuctionatorConfig.auction_duration = 2

    bridge.api.LoadSettings()

    assert bridge.auctionator_duration() == 2, (
        "a default we invented must not overwrite Auctionator's setting"
    )

    # Once the player chooses, it is pushed -- and stays pushed.
    bridge.set_posting_hours(12)
    assert bridge.auctionator_duration() == 1


def test_auctionator_overwriting_the_default_is_reported(bridge):
    """SAVE_LAST_DURATION_AS_DEFAULT turns the setting into a one-shot.

    A setting that quietly stops holding is worse than one that says so.
    """
    bridge.lua.globals().dfaAuctionatorConfig.save_last_duration_as_default = True

    ok, message = bridge.set_posting_hours(12)

    assert ok is True
    assert "save the last duration" in message


def test_the_status_line_names_the_duration(bridge):
    bridge.enable()
    bridge.show_sale_frame()
    bridge.set_posting_hours(48)

    assert "48 hours" in str(bridge.api.PostingHoursStatus())


def test_the_status_line_reports_a_drifted_setting(bridge):
    """If Auctionator holds a different duration, say which one."""
    bridge.enable()
    bridge.show_sale_frame()
    bridge.set_posting_hours(12)
    bridge.lua.globals().dfaAuctionatorConfig.auction_duration = 2  # drifted back

    status = str(bridge.api.PostingHoursStatus())

    assert "12 hours" in status
    assert "posthours 12" in status, "the line must say how to put it back"
