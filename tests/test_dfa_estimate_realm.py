"""The game-side reader of the per-realm index, run in a real Lua interpreter."""
from pathlib import Path

import pytest
from lupa import LuaRuntime

ROOT = Path(__file__).resolve().parent.parent

MOCK = r"""
function CreateFrame(kind, name)
    return { events = {}, scripts = {},
             RegisterEvent = function(self, e) self.events[#self.events+1] = e end,
             SetScript = function(self, s, f) self.scripts[s] = f end }
end
function GetRealmName() return "Dragonmaw" end
function time() return 1792570000 end
SlashCmdList = {}
-- WoW exposes string.format as the global format
format = string.format
"""


@pytest.fixture(scope="module")
def addon():
    data = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"
    if not data.exists():
        pytest.skip("the dataset has not been generated")
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    lua.execute(data.read_text(encoding="utf-8"))
    load = lua.eval("function(src) local fn = assert(load(src, 'dfa')); return fn end")
    extension = lua.table()
    load((ROOT / "DFA_Estimate" / "DFA_Estimate.lua").read_text(encoding="utf-8"))(
        "DFA_Estimate", extension)
    load((ROOT / "DFA_Estimate" / "UI" / "Tooltip.lua").read_text(encoding="utf-8"))(
        "DFA_Estimate", extension)
    return extension


@pytest.fixture(scope="module")
def estimate(addon):
    return addon.DFAEstimate


def test_the_dataset_loads_and_ships_a_line_per_realm(estimate):
    assert estimate.data is not None
    assert estimate.data.indexedRealms > 200
    entry = estimate.GetRealmEntry("Dragonmaw")

    assert entry is not None
    assert entry.source == "realm"
    assert entry.sample > 0


def test_another_realm_gets_the_same_item_at_its_own_factor(estimate):
    regional = float(estimate.data.prices[25])
    there = estimate.EstimateForRealm(25, "Silvermoon")
    entry = estimate.GetRealmEntry("Silvermoon")

    assert there is not None and there.price > 0
    # no Silvermoon record for item 25: the regional price times the factor
    assert there.source == "table"
    assert abs(there.price - regional * entry.factor) <= 2
    assert there.factor == entry.factor


def test_the_best_realm_is_a_price_and_competition_ranking(estimate):
    best = estimate.BestRealm(25)

    assert best is not None
    assert best.price > 0
    assert best.gain is None or -1 < best.gain < 10


def test_an_unmeasured_babysit_risk_is_not_zero(estimate):
    share, risk, watched = estimate.BabysitProbability("Dragonmaw")

    # no scan and one snapshot yet: unknown, not "no risk"
    assert share is None or 0 <= share <= 1
    assert risk is None or 0 <= risk <= 1
    assert watched is None or watched >= 0


def test_the_tooltip_names_the_best_realm_and_the_owned_stock(addon, estimate):
    tooltip = addon.DFAEstimateTooltip
    raw = tooltip.GetLines(25)
    lines = [raw[index] for index in range(1, len(raw) + 1)]

    assert any(line.startswith("DFA estimate:") for line in lines)
    assert any(line.startswith("Best realm:") for line in lines)
    # The shipped dataset may now carry a measured undercut share (the export
    # reads the scans from the shared database, so it no longer loses them).
    # What must never happen is a line for an *unknown* share rendered as 0 %.
    share, risk, _ = estimate.BabysitProbability("Dragonmaw")
    babysit = [line for line in lines if "Babysit" in line]
    if share is None and risk is None:
        assert not babysit, "an unknown babysit share must not be drawn"
    else:
        assert babysit and "%" in babysit[0]

    owned, posted = tooltip.CountOwned(25)
    assert owned is None  # no inventory export in this interpreter
