"""The farm ranking that ships inside the dataset.

The ranking is built during the export, from the same engine that produced
the prices, which is what makes it "always actual": there is no second
schedule to fall behind and no second price source to disagree with.

What these tests hold down:

* the block renders as Lua the reader can load, with names escaped;
* a ranking that cannot be built ships a reason instead of an empty
  leaderboard that would read as "nothing is worth farming";
* ``top_mogs`` returns its full list by contract, so this caller must slice
  it -- unsliced, 1023 transmog rows went into a 1.4 MB dataset.
"""
from __future__ import annotations

import pytest

from woweconomy.pricing import farm_section as fs


def test_zone_paths_reads_every_zone_file():
    paths = fs.zone_paths()
    assert paths, "no zone files on disk"
    assert all(path.name.startswith("zone_") for path in paths)


def test_a_missing_zone_id_is_an_error_not_a_silent_skip(tmp_path):
    with pytest.raises(fs.FarmRankingUnavailable):
        fs.zone_paths([999999], directory=tmp_path)


def test_no_zone_files_is_reported_as_unavailable(tmp_path):
    with pytest.raises(fs.FarmRankingUnavailable, match="no zone files"):
        fs.build_rankings(directory=tmp_path)


def test_an_empty_block_carries_the_reason():
    block = fs.empty_block("no prices for these zones")
    assert block["reason"] == "no prices for these zones"
    assert block["stops"] == [] and block["mogs"] == [] and block["mats"] == []
    assert block["dataAgeDays"] is None


def test_the_rendered_block_is_a_named_lua_table():
    block = fs.empty_block("")
    text = "\n".join(fs.render(block))
    assert "\tfarmRankings = {" in text
    assert text.rstrip().endswith("},")
    # every line that opens a table closes it: a truncated file would make
    # the whole dataset unloadable in the game
    assert text.count("{") == text.count("}")


def test_names_are_escaped_for_lua():
    block = {"generatedAt": 1, "source": 'he said "no"', "dataAgeDays": None,
             "zones": 1, "items": 1, "npcs": 1,
             "stops": [{"npc": 1, "name": 'Gruk "the Hand"',
                        "zone": "a\\b", "ev": 1, "gross": 1, "vendor": 0,
                        "spawns": 0, "items": []}],
             "mogs": [], "mats": []}
    text = "\n".join(fs.render(block))
    assert r'"Gruk \"the Hand\""' in text
    assert r'"a\\b"' in text
    assert r'"he said \"no\""' in text


def test_every_item_row_carries_an_id_and_a_name():
    """The alerts' rule, in the panel too: both, never one."""
    block = {"generatedAt": 1, "source": "", "dataAgeDays": 1,
             "zones": 1, "items": 1, "npcs": 1,
             "stops": [{"npc": 7, "name": "Gruk", "zone": "z", "ev": 100,
                        "gross": 100, "vendor": 0, "spawns": 2,
                        "items": [{"id": 2589, "name": "Linen Cloth",
                                   "rate": 0.5, "net": 120}]}],
             "mogs": [{"id": 14726, "name": "War Paint Gloves",
                       "price": 2763381, "drop": 0.0009, "rate": 0.0758}],
             "mats": []}
    text = "\n".join(fs.render(block))
    assert "id = 2589, name = \"Linen Cloth\"" in text
    assert "id = 14726, name = \"War Paint Gloves\"" in text


def test_the_summary_states_the_source_and_the_data_age():
    block = fs.empty_block("")
    block.update({"npcs": 257, "zones": 3, "source": "model (1 estimate)",
                  "dataAgeDays": 4,
                  "stops": [{"npc": 1}], "mogs": [], "mats": []})
    line = fs.summary(block)
    assert "1 stop(s)" in line
    assert "model (1 estimate)" in line
    assert "data 4 day(s) old" in line


def test_a_ranking_that_was_not_built_says_so_in_the_summary():
    assert "not built" in fs.summary(fs.empty_block("no zone files"))


def test_blizzard_facts_returns_nothing_rather_than_raising():
    """A filter that lost its reference table must not fail the export."""
    facts = fs.blizzard_facts()
    assert isinstance(facts, dict)
    for item_id, fact in list(facts.items())[:5]:
        assert int(fact.item_id) == int(item_id)


def test_the_price_age_is_unknown_without_an_engine():
    assert fs._price_age_days(None) is None


# -- the regression this work started from --------------------------------

def test_top_mogs_does_not_need_a_ranking_config():
    """``top_mogs`` passed its own ``r_cfg=None`` into ``eff_net``.

    Every other leaderboard defaults the config; this one did not, so any
    caller that omitted it crashed with "'NoneType' object has no attribute
    'price_base_mult'" -- which is how building the panel's ranking failed
    the first time it was run.
    """
    from woweconomy.analytics import farm_ranking as fr

    market = {1: fr.ItemMarket(item_id=1, name="Thing", price=10000.0,
                               sale_rate=0.5, sold_per_day=1.0)}
    drop = fr.NpcDrop(npc_id=1, npc_name="Gruk", zone=1, zone_name="z",
                      category="misc", item_id=1, item_name="Thing",
                      drop_rate=0.01)
    rows = fr.top_mogs(drops=[drop], market=market)
    assert isinstance(rows, list)


def test_eff_net_defaults_its_config():
    from woweconomy.analytics import farm_ranking as fr

    market = fr.ItemMarket(item_id=1, name="Thing", price=10000.0,
                           sale_rate=0.5)
    assert fr.eff_net(market, None, fr.WowEconomyConfig.default()) == pytest.approx(9500.0)
    assert fr.list_price(market, 0.0) == pytest.approx(10000.0)


# -- how the ranking reaches the file the game loads ----------------------

class _FakeEngine:
    """Just enough of the engine for the dataset builder."""

    def __init__(self):
        from woweconomy.pricing.shipped import ShippedModel

        self.shipped_model = ShippedModel(
            intercept=0.1, coefficients=[0.2, 0.3], medians=[1.0, 2.0],
            references=["dbregionmarketavg", "dbregionsaleavg"])
        self._history_cache = {"Dragonmaw": {100: (3, 1792400000, [1000])}}
        self.interests = None
        self.ml_frame = None

    def realm_scope(self, realm):
        return "spinebreaker"


def _builder(block=None):
    from woweconomy.pricing.config import PricingConfig
    from woweconomy.pricing.dataset import DatasetBuilder

    builder = DatasetBuilder(config=PricingConfig(), engine=_FakeEngine())
    builder.generated_at = 1792450000
    builder.realms = {"Dragonmaw": {"auctionHouse": "spinebreaker",
                                    "items": {}, "sampleSize": 0,
                                    "updatedAt": 1792400000}}
    builder.farm_rankings = dict(block or {})
    return builder


SAMPLE = {"generatedAt": 1792449000, "source": "model (1 estimate)",
          "reason": "", "dataAgeDays": 4, "zones": 3, "items": 1653,
          "npcs": 257,
          "stops": [{"npc": 4235, "name": "Guardian of the Deep",
                     "zone": "blackfathom-deeps", "ev": 116482,
                     "gross": 130000, "vendor": 900, "spawns": 4,
                     "items": [{"id": 4235, "name": "Heavy Hide",
                                "rate": 0.42, "net": 1200}]}],
          "mogs": [{"id": 14726, "name": "War Paint Gloves",
                    "price": 2763381, "drop": 0.0009, "rate": 0.0758}],
          "mats": [{"mat": "LEATHER", "npc": "Taerar", "zone": "ashenvale",
                    "units": 5.385, "ev": 28075}]}


def test_the_dataset_carries_the_ranking():
    text = _builder(SAMPLE).to_lua()
    assert "farmRankings = {" in text
    assert "Guardian of the Deep" in text
    assert "War Paint Gloves" in text
    assert "\tfarmStops = 1," in text


def test_a_dataset_without_a_ranking_has_no_block():
    text = _builder({}).to_lua()
    assert "farmRankings" not in text
    assert "\tfarmStops = 0," in text


def test_the_ranking_survives_validation():
    """The block's own rows must not read as malformed price records."""
    from woweconomy.pricing.dataset import validate_dataset

    text = _builder(SAMPLE).to_lua()
    assert validate_dataset(text)["problems"] == []


def test_a_ranking_change_is_a_new_dataset():
    """The ranking ships inside the file, so moving it changes the file."""
    first = _builder(SAMPLE).content_signature()
    moved = dict(SAMPLE)
    moved["stops"] = [dict(SAMPLE["stops"][0], ev=200000)]
    assert _builder(moved).content_signature() != first


def test_rebuilding_the_ranking_alone_is_not_a_new_dataset():
    """The block's own timestamp is not content, exactly like the dataset's."""
    later = dict(SAMPLE, generatedAt=SAMPLE["generatedAt"] + 3600)
    assert _builder(later).content_signature() == _builder(SAMPLE).content_signature()
