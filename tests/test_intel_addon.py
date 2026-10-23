"""The inventory/seller Lua harness and a CLI smoke test."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "intel_addon_harness.py"


def test_inventory_and_seller_capture_runs_in_a_real_lua_interpreter():
    result = subprocess.run([sys.executable, str(HARNESS)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    summary = result.stdout.strip().splitlines()[-1]
    passed, total = summary.split()[0].split("/")
    assert passed == total, summary


def test_the_cli_can_append_and_verify_a_ledger(tmp_path, capsys):
    from scripts.intel import main
    path = tmp_path / "ledger.jsonl"
    assert main(["ledger-append", str(path), "--kind", "note",
                 "--json", json.dumps({"claim": "a"})]) == 0
    capsys.readouterr()
    assert main(["ledger-verify", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["records"] == 1
