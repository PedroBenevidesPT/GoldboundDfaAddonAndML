"""The farm ranking the panel shows.

Two things are being pinned down here, and both are things a user would
notice if they broke:

* the ranking is read out of the dataset, so it is exactly as fresh as the
  prices next to it -- there is no second schedule to fall behind;
* every item it names carries **both** its id and its name.  A name alone
  is ambiguous (two items share one) and an id alone cannot be recognised,
  which is the same rule the Sentinel alerts follow.

The block is injected rather than read from the shipped dataset: the test
is about the renderer, and it must not start failing on the day the export
is skipped.
"""
from pathlib import Path

import pytest
from lupa import LuaRuntime

ROOT = Path(__file__).resolve().parent.parent

#: Enough of a client to load Panel.lua: the file registers its slash
#: command at load time, which is the only global it touches.
MOCK = r"""
SlashCmdList = {}
UIParent = {}
function time() return 1792570000 end
"""


def _panel(block=None, *, data=True):
    """Panel.lua loaded against a hand-made dataset."""
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    load = lua.eval("function(src) local fn = assert(load(src)); return fn end")
    addon = lua.table()
    load((ROOT / "DFA_Estimate" / "UI" / "Panel.lua").read_text(encoding="utf-8"))(
        "DFA_Estimate", addon)
    addon.DFAEstimate = lua.table()
    if data:
        payload = lua.table()
        if block is not None:
            payload.farmRankings = _to_lua(lua, block)
        addon.DFAEstimate.data = payload
        # The parts of the addon core BuildText asks for.  Stubbed rather
        # than loaded so this file tests the renderer and not the loader.
        addon.DFAEstimate.GetAgeHours = lambda: 2.0
        addon.DFAEstimate.GetRealmName = lambda: "Dragonmaw"
        addon.DFAEstimate.GetItems = lambda: lua.table()
        addon.DFAEstimate.dataVersion = 4
        addon.DFAEstimate.engineVersion = "test"
    return lua, addon


def _to_lua(lua, value):
    """A Python dict/list as a Lua table (lupa only converts one level)."""
    if isinstance(value, dict):
        table = lua.table()
        for key, item in value.items():
            table[key] = _to_lua(lua, item)
        return table
    if isinstance(value, (list, tuple)):
        table = lua.table()
        for index, item in enumerate(value, 1):
            table[index] = _to_lua(lua, item)
        return table
    return value


def _lines(lua, addon):
    out = addon.DFAEstimatePanel.BuildFarmRanking()
    return [out[index] for index in range(1, len(out) + 1)]


BLOCK = {
    "generatedAt": 1792570000 - 7200,  # two hours old
    "source": "model (1653 estimate, 0 shipped)",
    "reason": "",
    "dataAgeDays": 4,
    "zones": 3, "items": 1653, "npcs": 257,
    "stops": [{
        "npc": 4235, "name": "Guardian of the Deep",
        "zone": "blackfathom-deeps",
        "ev": 116482, "gross": 130000, "vendor": 900,
        "spawns": 4,
        "items": [{"id": 4235, "name": "Heavy Hide", "rate": 0.42, "net": 1200}],
    }],
    "mogs": [{"id": 14726, "name": "War Paint Gloves",
              "price": 2763381, "drop": 0.0009, "rate": 0.0758}],
    "mats": [{"mat": "LEATHER", "npc": "Taerar", "zone": "ashenvale",
              "units": 5.385, "ev": 28075}],
}


def test_the_ranking_is_read_from_the_dataset():
    lua, addon = _panel(BLOCK)
    lines = _lines(lua, addon)
    assert any("Farm ranking -- 1 stop(s), 257 NPC(s), 3 zone(s)" in line
               for line in lines)
    assert any("Guardian of the Deep" in line for line in lines)


def test_every_item_carries_its_id_and_its_name():
    lua, addon = _panel(BLOCK)
    lines = "\n".join(_lines(lua, addon))
    assert "Heavy Hide (item 4235)" in lines
    assert "War Paint Gloves (item 14726)" in lines


def test_the_ranking_says_how_old_it_is():
    """The panel must not present a two-hour-old ranking as live."""
    lua, addon = _panel(BLOCK)
    lines = _lines(lua, addon)
    assert any("built 2.0 hour(s) ago" in line for line in lines), lines
    assert any("model (1653 estimate" in line for line in lines)
    assert any("market data: 4 day(s) old" in line for line in lines), lines


def test_an_unknown_data_age_is_not_reported_as_fresh():
    block = dict(BLOCK, dataAgeDays=None)
    lua, addon = _panel(block)
    lines = "\n".join(_lines(lua, addon))
    assert "market data: age unknown" in lines


def test_a_small_value_keeps_its_unit():
    """12 silver must not render as 0.12g, nor 40 copper as 0.00g."""
    lua, addon = _panel(BLOCK)
    lines = "\n".join(_lines(lua, addon))
    assert "12s net each" in lines
    assert "0.12g" not in lines


def test_a_rare_drop_does_not_read_as_zero():
    """A 0.09 % transmog drop is not "0%" -- that would say it never drops."""
    lua, addon = _panel(BLOCK)
    lines = "\n".join(_lines(lua, addon))
    assert "0.09%" in lines


def test_a_dataset_without_a_ranking_says_so():
    """Not built is a different answer from "nothing is worth farming"."""
    lua, addon = _panel(None)
    lines = "\n".join(_lines(lua, addon))
    assert "not in this dataset" in lines
    assert "dfa_pricing_export" in lines


def test_a_ranking_that_could_not_be_built_ships_its_reason():
    block = dict(BLOCK, stops=[], mogs=[], mats=[],
                 reason="no prices for these zones (csv:csv/items.csv)")
    lua, addon = _panel(block)
    lines = "\n".join(_lines(lua, addon))
    assert "NOT BUILT" in lines
    assert "no prices for these zones" in lines


def test_the_panel_body_ends_with_the_ranking():
    """The item table is the reason the panel exists; the ranking follows it."""
    lua, addon = _panel(BLOCK)
    text = addon.DFAEstimatePanel.BuildText()
    assert text.index("known in this realm") < text.index("Farm ranking")
    assert "Heavy Hide (item 4235)" in text


def test_the_panel_names_the_ranking_before_the_item_table():
    """The full ranking is past the table; a summary line is not."""
    lua, addon = _panel(BLOCK)
    text = addon.DFAEstimatePanel.BuildText()
    assert "Farm: 1 stop(s) -- top Guardian of the Deep" in text
    assert text.index("Farm: 1 stop") < text.index("Farm ranking --")


def test_a_broken_ranking_does_not_take_the_panel_down():
    """A ranking that raises must leave the price table readable."""
    lua, addon = _panel()
    addon.DFAEstimate.data.farmRankings = "not a table"
    text = addon.DFAEstimatePanel.BuildText()
    assert "not in this dataset" in text
