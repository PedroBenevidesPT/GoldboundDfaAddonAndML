"""The farm ranking as a webhook message.

The panel carries the whole ranking; this is the same ranking folded into one
embed, so "what should I farm" is answerable without the game.  What matters
is that the stops, their zones and their value survive the folding -- a
message that says only "the ranking was rebuilt" is a heartbeat.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCK = {
    "generatedAt": 1792570000,
    "source": "model (1653 estimate, 0 shipped)",
    "reason": "",
    "zones": 3, "items": 1653, "npcs": 257,
    "stops": [
        {"npc": 4235, "name": "Guardian of the Deep",
         "zone": "blackfathom-deeps", "ev": 116482,
         "items": [{"id": 4235, "name": "Heavy Hide", "rate": 0.42,
                    "net": 1200}]},
        {"npc": 918, "name": "Taerar", "zone": "ashenvale", "ev": 28075,
         "items": [{"id": 2589, "name": "Linen Cloth", "rate": 0.8,
                    "net": 400}]},
    ],
}


def _module():
    path = ROOT / "scripts" / "dfa_pricing_export.py"
    spec = importlib.util.spec_from_file_location("dfa_pricing_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_alert_names_each_stop_with_its_zone_and_value():
    alert = _module().farm_alert(BLOCK)
    assert alert is not None
    assert alert.key == "farm:ranking"
    assert "2 stop(s)" in alert.title
    assert "257 NPC(s)" in alert.title and "3 zone(s)" in alert.title
    first = alert.lines[0]
    assert "Guardian of the Deep" in first
    assert "blackfathom-deeps" in first
    assert "11.6g/kill" in first
    assert "Heavy Hide 42%" in first


def test_the_alert_says_where_the_prices_came_from():
    alert = _module().farm_alert(BLOCK)
    assert any("model (1653 estimate" in line for line in alert.lines)


def test_a_ranking_with_no_stops_posts_nothing():
    """An empty ranking is not news, and must not read as "farm nothing"."""
    block = dict(BLOCK, stops=[])
    assert _module().farm_alert(block) is None
