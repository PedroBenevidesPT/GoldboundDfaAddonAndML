"""ZoneTimerRedux's gold calculation, run in a real Lua interpreter.

The addon used to count cash and nothing else.  It now values loot with the
DfA pricing dataset, so the links that can break silently are covered here:

* the harness itself (``scripts/zone_timer_harness.py``) drives the real
  dataset, the real loot feed and the real addon files;
* the addon must read prices from the engine's exported dataset through
  ``DFA_ESTIMATE_API`` -- not from TSM, not from a price of its own;
* the installed copy must be the one that was tested.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "zone_timer_harness.py"
ADDON = ROOT / "ZoneTimerRedux"
INSTALLED = Path(r"C:\Program Files (x86)\World of Warcraft\_retail_"
                 r"\Interface\AddOns\ZoneTimerRedux")


def test_the_pricing_engine_values_the_loot_and_the_ui_shows_the_split():
    result = subprocess.run([sys.executable, str(HARNESS)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    # every check that ran must have passed; the total moves as checks are
    # added, so it is the equality that matters, not the number
    summary = re.search(r"(\d+)/(\d+) checks passed", result.stdout)
    assert summary, result.stdout[-400:]
    assert summary.group(1) == summary.group(2), result.stdout[-400:]
    assert "FAIL" not in result.stdout


def test_prices_come_from_the_engine_and_not_from_a_second_source():
    """One pricing system: the dataset DFA_Estimate publishes.

    A TSM price, a hardcoded table or a local formula would disagree with
    the tooltip and with the backend, which is exactly what this integration
    replaced.
    """
    pricing = (ADDON / "Pricing.lua").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in pricing.splitlines())
    assert "DFA_ESTIMATE_API" in code
    for foreign in ("TSM_API", "dbmarket", "DBMarket", "Auctionator"):
        assert foreign not in code, foreign


def test_the_vendor_value_is_not_derived_from_the_market_value():
    """Vendor and market are separate facts, kept in separate fields."""
    pricing = (ADDON / "Pricing.lua").read_text(encoding="utf-8")
    assert "ClientVendorPrice" in pricing
    assert "GetLootValues" in pricing
    ui = (ADDON / "UI.lua").read_text(encoding="utf-8")
    assert "Valuation(" in ui
    assert "valuation.cash" in ui
    assert "FormatLoot" in ui


def test_the_toc_declares_the_integrations_it_uses():
    toc = (ADDON / "ZoneTimerRedux.toc").read_text(encoding="utf-8")
    assert "DFA_Estimate" in toc
    assert "DFA_AuctionIntelligence" in toc
    assert "MobInfo2" in toc
    assert "ZoneLootDB" in toc
    files = [line.strip() for line in toc.splitlines()
             if line.strip() and not line.startswith("##")]
    assert files.index("Pricing.lua") < files.index("UI.lua")
    assert files.index("Loot.lua") < files.index("UI.lua")


def test_the_reader_publishes_the_api_zone_timer_reads():
    reader = (ROOT / "DFA_Estimate" / "DFA_Estimate.lua").read_text(encoding="utf-8")
    for name in ("DFA_ESTIMATE_API = public", "GetLootValues",
                 "GetVendorPrice", "GetNetValue", "GetMaxAgeHours"):
        assert name in reader, name


def test_the_installed_copy_is_the_one_that_was_tested():
    if not INSTALLED.exists():
        pytest.skip("the game is not installed on this machine")
    for name in ("Core.lua", "UI.lua", "Settings.lua", "Pricing.lua",
                 "Loot.lua", "ZoneTimerRedux.toc"):
        assert (ADDON / name).read_bytes() == (INSTALLED / name).read_bytes(), name
