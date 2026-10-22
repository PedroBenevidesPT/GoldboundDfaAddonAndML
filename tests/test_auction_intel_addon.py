"""The addon's sync and its logout flush, run in a real Lua interpreter.

The harness (``scripts/auction_intel_harness.py``) is the test; this wraps it
so a broken sync breaks the suite instead of waiting to be noticed in game.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from woweconomy import addons as addon_manifest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "auction_intel_harness.py"


def test_the_addon_syncs_and_saves_everything_on_logout():
    result = subprocess.run([sys.executable, str(HARNESS)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "27/27 checks passed" in result.stdout
    assert "FAIL" not in result.stdout


def test_an_unknown_client_event_cannot_kill_the_addon():
    """A single bad RegisterEvent used to abort Core.lua's main chunk.

    The client reported ``Attempt to register unknown event
    PLAYERBANKTABS_CHANGED`` at Core.lua:16, so no slash command and no
    login sync were ever installed.  Registration must stay defensive.
    """
    text = (ROOT / "DFA_AuctionIntelligence" / "Core.lua").read_text(encoding="utf-8")
    assert "PLAYERBANKTABS_CHANGED" not in text
    assert "pcall(frame.RegisterEvent" in text


def test_the_addon_does_not_hook_blizzards_mailbox():
    """Hooking Blizzard's own mail frame got the action blocked at login.

    The client answers an addon installed into Blizzard's frame with
    "action blocked by Blizzard" — the mailbox became unusable.  The addon
    may still read the inbox on demand, never by hooking the frame and not
    by default from the mailbox's own events.
    """
    mail = (ROOT / "DFA_AuctionIntelligence" / "Mail.lua").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in mail.splitlines())
    assert "OpenAllMail" not in code
    assert "StartOpening" not in code
    assert "hooksecurefunc" not in code
    core = (ROOT / "DFA_AuctionIntelligence" / "Core.lua").read_text(encoding="utf-8")
    assert "InstallOpenAllHook" not in core
    assert "Mail:OnMailboxEvent" in core


def test_the_installed_copy_is_the_one_that_was_tested():
    """A change in the project that never reached the client is not a fix."""
    installed = Path(r"C:\Program Files (x86)\World of Warcraft\_retail_"
                     r"\Interface\AddOns\DFA_AuctionIntelligence")
    if not installed.exists():
        pytest.skip("the game is not installed on this machine")
    project = ROOT / "DFA_AuctionIntelligence"
    for name in addon_manifest.toc_files(
            project / "DFA_AuctionIntelligence.toc"):
        assert (project / name).read_bytes() == (installed / name).read_bytes(), name
