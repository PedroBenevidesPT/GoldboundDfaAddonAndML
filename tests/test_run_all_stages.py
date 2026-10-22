"""The daily pipeline must not die on a moved inventory CSV."""
import sys
from types import SimpleNamespace

from scripts import run_all


def args(**kwargs):
    base = dict(scrape_zone=[], fetch_csv=False, inventory="")
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_the_inventory_default_is_a_file_that_exists():
    path = run_all.default_inventory()
    assert (run_all.ROOT / path).exists()


def test_an_explicit_inventory_wins():
    stages = run_all.build_stages(args(inventory="inventory.csv"))
    inventory = next(s for s in stages if s.name == "inventory")
    assert "inventory.csv" in " ".join(inventory.cmds[0].argv)


def test_the_daily_stages_include_the_addon_import_and_the_buyer_link():
    names = [s.name for s in run_all.build_stages(args())]
    assert names.index("intel") < names.index("buyers") < names.index("tests")


def test_the_pre_selling_run_prints_both_commands(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv",
                        ["run_all.py", "--dry-run", "--only", "intel,buyers"])
    assert run_all.main() == 0
    out = capsys.readouterr().out
    assert "scripts/intel.py addon-import" in out
    assert "scripts/intel.py mail-link" in out
    assert "(dry-run)" in out
