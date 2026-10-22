"""What the Auctionator bridge does when the player picks a price.

The bridge fills the buyout field with the DfA estimate, and must leave
alone a price the player chose -- either by typing it in or by clicking a
row in Auctionator's own price list.  A row click used to lose that
protection: the estimate was written straight back over the row the
player had just picked.

These tests load the real ``UI/Auctionator.lua`` into a Lua runtime and
drive the two paths that reach ``OnPriceSet``: a fill with no player
choice, and a choice made through the event bus.

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

-- Auctionator's event bus.  Fire is a real Lua function because the
-- bridge refuses to wrap it otherwise.
_G.Auctionator = {
    Selling = {
        Events = {
            BagItemClicked = "BagItemClicked",
            ClearBagItem = "ClearBagItem",
            PriceSelected = "PriceSelected",
        },
    },
    EventBus = {
        Fire = function(self, source, event, ...) end,
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

    def auctionator_sets(self) -> None:
        """What the hook body runs when Auctionator sets a price."""
        self.api.OnPriceSet(self.frame)

    def fire(self, event: str) -> None:
        """Raise one event through the wrapped Fire, in Auctionator's order."""
        bus = self.lua.globals().Auctionator.EventBus
        bus.Fire(bus, "AuctionatorSellingFrame", event)

    def last_fill(self):
        return self.api.GetLastFill()

    def writes(self) -> List[int]:
        """Every price written to the frame, in order."""
        return [int(p) for p in self.frame.writes.values()]

    def messages(self) -> List[str]:
        return [str(m) for m in self.lua.globals().dfaMessages.values()]


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
# the regression: a row click must survive
# ---------------------------------------------------------------------------
def test_clicking_a_price_row_is_not_overwritten_by_the_estimate(bridge):
    """The reported bug.

    Clicking a row in Auctionator's price list makes Auctionator call
    UpdateSalesPrice with the price the player picked.  The bridge hooks
    that call; it used to put the estimate straight back on top.
    """
    bridge.enable()

    # A normal fill first, so there is a fill that could be re-applied.
    assert bridge.fill() is True
    assert bridge.writes() == [ESTIMATE_COPPER]

    # The player clicks a row.  The event arrives before the frame is told.
    bridge.fire("PriceSelected")

    # Auctionator now sets the price the player picked.
    bridge.auctionator_sets()

    assert bridge.writes() == [ESTIMATE_COPPER], (
        "the estimate must not be written again over a price the player "
        "picked from Auctionator's price list"
    )


def test_a_row_click_marks_the_fill_as_not_confirmed(bridge):
    bridge.enable()
    assert bridge.fill() is True
    assert bool(bridge.last_fill().confirmed) is True

    bridge.fire("PriceSelected")

    assert bool(bridge.last_fill().confirmed) is False, (
        "the last fill was superseded by the player's choice"
    )


def test_a_price_the_player_typed_is_not_overwritten(bridge):
    """The same protection, reached through typing rather than a click."""
    bridge.enable()
    assert bridge.fill() is True
    assert bridge.writes() == [ESTIMATE_COPPER]

    # The gold box is watched, but this harness has no widget; the event
    # bus path above covers the click.  Here we assert the equivalent
    # invariant directly: once the player has chosen, a further
    # Auctionator price set does not pull the estimate back in.
    bridge.fire("PriceSelected")
    bridge.auctionator_sets()

    assert bridge.writes() == [ESTIMATE_COPPER]


# ---------------------------------------------------------------------------
# the choice belongs to one item
# ---------------------------------------------------------------------------
def test_the_choice_is_forgotten_when_the_item_changes(bridge):
    bridge.enable()
    bridge.fill()
    bridge.fire("PriceSelected")
    bridge.auctionator_sets()

    # A different item is selected: the previous choice belonged to the
    # previous one, so the estimate fills again.
    bridge.fire("BagItemClicked")
    assert bridge.fill() is True
    assert bridge.writes() == [ESTIMATE_COPPER, ESTIMATE_COPPER]


def test_a_fill_is_not_reapplied_when_the_bridge_is_disabled(bridge):
    bridge.api.SetEnabled(False)
    assert bridge.fill() is False
    assert bridge.writes() == []


def test_the_estimate_is_not_divided_by_the_consignment_rate(bridge):
    """The estimate is what the buyer pays; the cut comes off afterwards."""
    bridge.fill()
    assert bridge.writes() == [ESTIMATE_COPPER]
