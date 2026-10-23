"""What the tooltip says about the status of a prediction.

The dataset carries a model-lifecycle token ("baseline_only",
"validated_ranking_only", ...).  It is a vocabulary for the reports and the
artifact; a player reading an item tooltip must get a sentence.  These
tests load the real ``UI/Tooltip.lua`` into a Lua runtime and check the
line that is actually built, for every status the pipeline can emit, for a
token added upstream, and for the two ways a status can be absent.

The addon's own harness in ``test_dfa_estimate_addon.py`` cannot be reused
here: it fails to compile on a ``frame:SetScript = ...`` assignment, so
this module loads only the file under test.
"""
from __future__ import annotations

import re
from pathlib import Path

import lupa
import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLTIP = ROOT / "DFA_Estimate" / "UI" / "Tooltip.lua"
DATASET = ROOT / "DFA_Estimate" / "Data" / "DFA_Estimate_Data.lua"

#: Every status the sentinel can write into ``prediction_status``.  The
#: token list mirrors the literals in ``woweconomy/ml/sentinel.py``; the
#: label itself is asserted by behaviour, not by copying the strings.
STATUS_TOKENS = [
    "baseline_only",
    "trained_no_measured_gain",
    "validated_ranking_only",
    "unsupported_signal",
    "insufficient_data",
    "stale",
    "unknown",
]


def load_tooltip():
    """The real Tooltip.lua, with only the globals it reads at load time."""
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        _G.Enum = { TooltipDataType = { Item = 7 } }
        _G.TooltipDataProcessor = { AddTooltipPostCall = function() end }
        _G.C_Item = { GetItemInfo = function() return "item:1234" end }
        _G.hooksecurefunc = function() end
        _G.Addon = {}
    """)
    loader = lua.eval("function(src) return assert(load(src, 'Tooltip.lua')) end")
    loader(TOOLTIP.read_text(encoding="utf-8"))("DFA_Estimate", lua.globals().Addon)
    return lua


def lines_for(lua, record):
    """The tooltip lines for one record, as a Python list of strings."""
    lua.execute("""
        _G.__record = nil
        _G.Addon.DFAEstimate = {
            data = { realms = {}, vendors = {}, nets = {} },
            GetRealmName = function() return "Dragonmaw" end,
            BabysitProbability = function() return nil, nil, 0 end,
            GetAgeHours = function() return 2 end,
            -- the addon calls this as addon.Estimate(itemId): one argument,
            -- not a method call.  The fixture must match that contract or a
            -- test can pass on a record the real addon would never return.
            Estimate = function(itemId) return _G.__record end,
        }
    """)
    lua.globals().__record = lua.table_from(record) if record is not None else None
    lines = lua.globals().Addon.DFAEstimateTooltip.GetLines(1234)
    return [str(value) for value in lines.values()]


def status_line(lines):
    """The single line that talks about the prediction status, or None."""
    found = [line for line in lines if line.startswith("Prediction: ")]
    assert len(found) <= 1, f"more than one status line: {found}"
    return found[0] if found else None


BASE = {"price": 1000, "conf": 0.5}


def test_a_validated_record_says_nothing_about_status():
    lua = load_tooltip()
    lines = lines_for(lua, dict(BASE, status="validated"))
    assert status_line(lines) is None
    assert any(line.startswith("DFA estimate:") for line in lines)


@pytest.mark.parametrize("token", STATUS_TOKENS)
def test_every_known_status_is_rendered_as_a_sentence(token):
    lua = load_tooltip()
    line = status_line(lines_for(lua, dict(BASE, status=token)))

    assert line is not None, f"{token} produced no status line"
    label = line[len("Prediction: "):]
    assert label != token, f"{token} reached the player as a raw token"
    assert "_" not in label, f"{token} is still machine vocabulary: {label!r}"
    assert label.strip() == label and label, "the label must be a readable phrase"


def test_baseline_only_says_what_the_number_is():
    """The token that ships today: the item rate, with no learned model."""
    lua = load_tooltip()
    line = status_line(lines_for(lua, dict(BASE, status="baseline_only")))

    assert line is not None
    lowered = line.lower()
    assert "baseline" not in lowered
    assert "sale rate" in lowered
    assert "model" in lowered


def test_a_status_added_upstream_is_visible_not_swallowed():
    """An unmapped token is shown as it arrived: silence would hide a bug."""
    lua = load_tooltip()
    line = status_line(lines_for(lua, dict(BASE, status="blocked")))

    assert line == "Prediction: blocked"


def test_a_missing_status_adds_no_line():
    """A dataset from before schema 2 carries no status at all."""
    lua = load_tooltip()
    lines = lines_for(lua, dict(BASE))
    assert status_line(lines) is None


def test_an_empty_status_adds_no_line():
    lua = load_tooltip()
    lines = lines_for(lua, dict(BASE, status=""))
    assert status_line(lines) is None


def test_the_fallback_reason_is_kept_beside_the_label():
    lua = load_tooltip()
    line = status_line(lines_for(
        lua, dict(BASE, status="unknown", fallback="no price model")))

    assert line is not None
    assert line.endswith("(no price model)")
    assert line.startswith("Prediction: ")


def test_no_shipped_record_reaches_the_player_as_a_token():
    """The dataset that is actually deployed, not a fixture.

    Whatever statuses the export wrote, none of them may be shown raw.
    """
    if not DATASET.exists():
        pytest.skip("the dataset has not been exported on this machine")
    text = DATASET.read_text(encoding="utf-8", errors="replace")
    shipped = sorted(set(re.findall(r'status = "([^"]*)"', text)))
    assert shipped, "the dataset carries no status field at all"

    lua = load_tooltip()
    for token in shipped:
        if token == "validated":
            continue
        line = status_line(lines_for(lua, dict(BASE, status=token)))
        assert line is not None, f"{token} is shipped but renders no line"
        assert "_" not in line, f"{token} ships to the player as a token: {line!r}"
