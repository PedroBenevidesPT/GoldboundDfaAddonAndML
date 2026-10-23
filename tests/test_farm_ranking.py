"""Unit tests for the Goldbound farm-ranking model.

Fixtures only — no network, no browser, no live API calls.
"""
import math

import pytest

from woweconomy.analytics.farm_ranking import (
    BlizzardItem,
    FarmRankingConfig,
    ItemMarket,
    NpcDrop,
    _g,
    blizz_excluded,
    build_rotation,
    build_sessions,
    liquidity_weight,
    load_zone_drops,
    mat_class,
    price_age_days,
    rank_zone,
    realization_weight,
    score_npcs,
    top_jackpots,
    top_mats,
    top_mogs,
)
from woweconomy.config import WowEconomyConfig
from woweconomy.services.blizzard_items import BlizzardItemsClient
from woweconomy.services.wowhead import WowheadZoneScraper


def mkdrop(npc=1, item=100, rate=0.5, qty=1.0, name="Fine Sword", type_=""):
    return NpcDrop(npc, "Mob", 331, "ash", "drops", item, name, rate, qty, type_)


def mkmarket(item=100, price=10000.0, sr=0.5, spd=50.0):
    return {item: ItemMarket(item, f"Item-{item}", price, sr, spd)}


# --- drop-rate normalization: % column vs decimal ---------------------

def test_rate_from_pct_column():
    W = WowheadZoneScraper
    assert W._rate_from_row("44 out of 65 ... 68", "68") == pytest.approx(0.68)
    assert W._rate_from_row("727 out of 267 710 ... 0.3", "0.3") == pytest.approx(0.003)
    # fraction fallback (normalized thousands separators), never clamped
    assert W._rate_from_row("44 out of 65", "") == pytest.approx(44 / 65)
    assert W._rate_from_row("8 191 out of 267 710", "") == pytest.approx(8191 / 267710)
    assert W._rate_from_row("no numbers here", "") is None
    # misparse (num > den) falls through instead of clamping to 100%
    assert W._rate_from_row("727 out of 267", "") is None or \
        W._rate_from_row("727 out of 267", "") <= 1.0


def test_qty_ranges():
    W = WowheadZoneScraper
    assert W._qty_from_text("1-4") == 2.5
    assert W._qty_from_text("2") == 2.0
    assert W._qty_from_text("") == 1.0  # documented midpoint/default assumption


def test_item_id_from_href():
    W = WowheadZoneScraper
    assert W._item_id_from_el("https://www.wowhead.com/item=14047/runecloth") == 14047
    assert W._item_id_from_el("https://x/nothing") is None


# --- money -------------------------------------------------------------

def test_copper_to_gold():
    assert _g(10_000) == "1g"
    assert _g(266_486_020) == "26,649g"
    assert _g(0) == "0g"


def test_auction_commission():
    m = ItemMarket(1, "X", 10_000.0, 0.5, 50.0)
    assert m.net_value(0.05) == 9_500.0
    assert ItemMarket(1, "X", 0.0).net_value(0.05) == 0.0


def test_missing_price_excluded_not_zero():
    farms = score_npcs([mkdrop()], {}, WowEconomyConfig.default())
    assert farms[0].n_drops_valued == 0
    assert farms[0].n_drops_unpriced == 1
    assert farms[0].ev_per_kill == 0.0


# --- liquidity: DBRegionSaleRate is per-listing sell-through (0-1) --------

def test_liquidity_weight_scale():
    # decimal scale used as-is: NO percent normalization, NO time compounding
    assert liquidity_weight(0.541) == pytest.approx(0.541)
    assert liquidity_weight(0.0) == 0.0
    assert liquidity_weight(1.0) == 1.0
    assert liquidity_weight(2.0) == 1.0  # clamped, never >100%
    assert liquidity_weight(-0.5) == 0.0
    assert liquidity_weight(float("nan")) == 0.0
    # signature has NO horizon: daily compounding is gone
    import inspect
    assert list(inspect.signature(liquidity_weight).parameters) == ["sale_rate"]


def test_no_daily_probability_formula():
    # 1-(1-sr)^N must not appear anywhere in the implementation
    src = open("woweconomy/analytics/farm_ranking.py", encoding="utf-8").read()
    assert "(1.0 - sr) **" not in src
    assert "(1 - sale_rate)^N" not in src


def test_reference_price_unscaled():
    # avgSalePrice is the reference: NO base+rate multiplier inflation.
    # net = price x (1 - cut) exactly.
    m = ItemMarket(1, "X", 10000.0, 0.9, 50.0)
    assert m.net_value(0.05) == 9500.0  # not 10000*(2+10*0.9)*0.95


def test_thin_market_haircut():
    m_nosales = ItemMarket(1, "X", 10_000.0, 0.5, 0.0)
    m_sales = ItemMarket(1, "X", 10_000.0, 0.5, 5.0)
    assert realization_weight(m_sales) == pytest.approx(0.5)
    assert realization_weight(m_nosales) == pytest.approx(0.05)


def test_days_to_sale_na():
    import tempfile, json
    from pathlib import Path
    zone = [{"npc_id": 1, "zone": 331, "name": "ash", "npc_name": "A",
             "category": "drops", "items": [
                 {"item_id": 1, "item_name": "Fine Sword",
                  "drop_rate": 0.01, "qty": 1.0}]}]
    p = Path(tempfile.mkdtemp()) / "z.json"
    p.write_text(json.dumps(zone), encoding="utf-8")
    mkt = {1: ItemMarket(1, "Fine Sword", 50000.0, 0.02, 1.0)}
    r = rank_zone([p], mkt)
    assert r.mog_farms, "mog farm expected"
    assert r.mog_farms[0].days_to_sale is None  # N/A, not invented
    assert 0.0 < r.mog_farms[0].p_cycle_sale <= 1.0


def test_mog_p_acquisition_aware():
    # 100 ultra-rare mogs must NOT saturate near 100%: you won't loot them.
    from woweconomy.analytics.farm_ranking import top_mog_farms
    drops = [NpcDrop(1, "Mob", 331, "ash", "drops", 1000 + i,
                     f"Rare Robe {i}", 0.000001, 1.0, "") for i in range(100)]
    mkt = {1000 + i: ItemMarket(1000 + i, f"Robe {i}", 500000.0, 0.05, 5.0)
           for i in range(100)}
    farms = top_mog_farms(drops, mkt)
    assert farms, "expected a mog farm"
    assert farms[0].p_cycle_sale < 0.5, farms[0].p_cycle_sale
    # ...while a common mog portfolio scores high
    drops2 = [NpcDrop(2, "Mob2", 331, "ash", "drops", 2000 + i,
                      f"Common Cloak {i}", 0.05, 1.0, "") for i in range(20)]
    mkt2 = {2000 + i: ItemMarket(2000 + i, f"Cloak {i}", 50000.0, 0.10, 5.0)
            for i in range(20)}
    farms2 = top_mog_farms(drops2, mkt2)
    assert farms2[0].p_cycle_sale > farms[0].p_cycle_sale


def test_conservative_never_guarantees():
    # conservative = vendor + fast-mover weighted EV: a subset estimate,
    # always <= vendor + full auction expectation (never inflated).
    from woweconomy.analytics.farm_ranking import NpcFarm, build_sessions
    farms = [NpcFarm(1, "M", 331, "ash", 1000.0, 500.0, 50.0, 0, 1, 0)]
    plans = build_sessions(
        build_rotation(farms, stops=1), farms, [], {},
        WowEconomyConfig.default(), {}, FarmRankingConfig(),
        minutes_list=(30,))
    for s in plans:
        assert s.conservative_gold <= s.vendor_gold + s.auction_est
        assert s.speculative_gold >= 0.0


def test_separation_vendor_auction_liquidity():
    from woweconomy.services.blizzard_items import BlizzardItem as BI
    blizz = {1: BI(1, "Sword", "UNCOMMON", "ON_EQUIP", sell_price=2000)}
    mkt = {1: ItemMarket(1, "Sword", 10000.0, 0.5, 10.0)}
    farms = score_npcs([mkdrop(item=1, rate=1.0)], mkt, WowEconomyConfig.default(), blizz)
    f = farms[0]
    assert f.ev_per_kill == pytest.approx(1.0 * 9500.0)          # gross
    assert f.realizable_ev == pytest.approx(9500.0 * 0.5)        # x liquidity
    assert f.vendor_per_kill == pytest.approx(2000.0)            # separate


def test_jackpot_stable_without_probability_info():
    mkt = {100: ItemMarket(100, "Epic", 1_000_000.0, 0.001, 0.5)}
    hits = top_jackpots([mkdrop(rate=0.001)], mkt,
                        min_rate=0.0005, max_rate=0.10, blizz={})
    assert len(hits) == 1  # works with empty blizzard map
    assert hits[0].expected == pytest.approx(0.001 * 950_000.0 * 0.001)
    assert hits[0].p_drop_100 == pytest.approx(1 - 0.999 ** 100)


def test_cli_flags_compatible():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "woweconomy.analytics.farm_ranking",
                        "--help"], capture_output=True, text=True, cwd=".")
    assert r.returncode == 0
    for flag in ("--top", "--stops", "--kpm", "--sort", "--horizon",
                 "--travel-sec", "--session", "--mog-kills", "--out",
                 "--price-base", "--price-rate", "--price-floor",
                 "--db", "--blizz-db", "--market-csv"):
        assert flag in r.stdout, flag


def test_freshness_factor():
    from woweconomy.analytics.farm_ranking import freshness_factor
    assert freshness_factor(0) == pytest.approx(1.0)
    assert freshness_factor(1) == pytest.approx(1 / (1 + 1 / 30))
    assert freshness_factor(30) == pytest.approx(0.5)
    assert freshness_factor(79) < 0.3
    assert freshness_factor(None) == 0.5  # unknown provenance penalized


def test_confidence_reflects_age():
    import tempfile, json
    from pathlib import Path
    zone = [{"npc_id": 1, "zone": 331, "name": "ash", "npc_name": "A",
             "category": "drops", "items": [
                 {"item_id": 1, "item_name": "Fine Sword",
                  "drop_rate": 0.5, "qty": 1.0}]}]
    p = Path(tempfile.mkdtemp()) / "z.json"
    p.write_text(json.dumps(zone), encoding="utf-8")
    mkt = {1: ItemMarket(1, "Fine Sword", 10000.0, 0.5, 50.0)}
    fresh = rank_zone([p], mkt, r_cfg=FarmRankingConfig(price_age_days=1))
    stale = rank_zone([p], mkt, r_cfg=FarmRankingConfig(price_age_days=79))
    assert fresh.farms[0].confidence == pytest.approx(1 / (1 + 1 / 30))
    assert stale.farms[0].confidence < fresh.farms[0].confidence


def test_pricing_multiplier_defaults_identity():
    from woweconomy.analytics.farm_ranking import eff_net, list_price
    m = ItemMarket(1, "X", 10000.0, 0.5, 50.0)
    cfg, rcfg = WowEconomyConfig.default(), FarmRankingConfig()
    assert list_price(m, 500.0, rcfg) == 10000.0  # base=1, rate=0: unchanged
    assert eff_net(m, None, cfg, rcfg) == pytest.approx(9500.0)
    rcfg2 = FarmRankingConfig(price_base_mult=2.0, price_rate_mult=10.0)
    assert list_price(m, 0.0, rcfg2) == pytest.approx(10000.0 * (2 + 10 * 0.5))
    rcfg3 = FarmRankingConfig(price_vendor_floor_mult=2.0)
    assert list_price(m, 6000.0, rcfg3) == pytest.approx(12000.0)  # floor binds
    assert list_price(m, 100.0, rcfg3) == pytest.approx(10000.0)  # floor slack


def test_jackpot_evidence_fields():
    mkt = {100: ItemMarket(100, "Epic", 1_000_000.0, 0.02, 3.0)}
    from woweconomy.services.blizzard_items import BlizzardItem as BI
    blizz = {100: BI(100, "Epic", "EPIC", "ON_EQUIP", sell_price=5000)}
    hits = top_jackpots([mkdrop(rate=0.01)], mkt, blizz=blizz)
    assert hits and hits[0].sale_rate == 0.02
    assert hits[0].sold_per_day == 3.0
    assert hits[0].vendor_value == 5000


def test_mog_realizable_present():
    import tempfile, json
    from pathlib import Path
    zone = [{"npc_id": 7, "zone": 331, "name": "ash", "npc_name": "M",
             "category": "drops", "items": [
                 {"item_id": 5, "item_name": "Fine Robe",
                  "drop_rate": 0.02, "qty": 1.0}]}]
    p = Path(tempfile.mkdtemp()) / "z.json"
    p.write_text(json.dumps(zone), encoding="utf-8")
    mkt = {5: ItemMarket(5, "Fine Robe", 200000.0, 0.05, 2.0)}
    r = rank_zone([p], mkt)
    assert r.mog_farms and r.mog_farms[0].realizable_ev > 0


# --- vendor fallback ----------------------------------------------------

def test_vendor_gold_per_kill():
    blizz = {100: BlizzardItem(100, "Sword", "UNCOMMON", "ON_EQUIP", sell_price=500)}
    farms = score_npcs([mkdrop(rate=0.5, qty=2.0)], mkmarket(),
                       WowEconomyConfig.default(), blizz)
    assert farms[0].vendor_per_kill == pytest.approx(0.5 * 2.0 * 500)
    # no blizzard data -> vendor 0, never fabricated
    farms2 = score_npcs([mkdrop()], mkmarket(), WowEconomyConfig.default(), {})
    assert farms2[0].vendor_per_kill == 0.0


# --- jackpots -------------------------------------------------------------

def test_rare_jackpot_weighted_but_listed():
    mkt = {100: ItemMarket(100, "Epic", 1_000_000.0, 0.001, 0.5)}
    drops = [mkdrop(rate=0.001)]
    hits = top_jackpots(drops, mkt, min_rate=0.0005, max_rate=0.10)
    assert len(hits) == 1
    assert hits[0].expected == pytest.approx(0.001 * 1_000_000 * 0.95 * 0.001)
    assert hits[0].p_drop_100 == pytest.approx(1 - 0.999 ** 100)
    # too common -> not a jackpot
    assert top_jackpots([mkdrop(rate=0.5)], mkt) == []


# --- multi-drop aggregation + determinism ----------------------------------

def test_multi_drop_npc_and_determinism(tmp_path):
    import json
    zone = [{"npc_id": 1, "zone": 331, "name": "ash", "npc_name": "A",
             "category": "drops", "items": [
                 {"item_id": 1, "item_name": "I1", "drop_rate": 0.5, "qty": 1.0},
                 {"item_id": 2, "item_name": "I2", "drop_rate": 0.1, "qty": 2.0}]},
            {"npc_id": 2, "zone": 331, "name": "ash", "npc_name": "B",
             "category": "drops", "items": [
                 {"item_id": 1, "item_name": "I1", "drop_rate": 0.95, "qty": 1.0}]}]
    p = tmp_path / "z.json"
    p.write_text(json.dumps(zone), encoding="utf-8")
    mkt = {1: ItemMarket(1, "I1", 10000.0, 0.5, 50.0),
           2: ItemMarket(2, "I2", 20000.0, 0.5, 50.0)}
    r1 = rank_zone([p], mkt)
    r2 = rank_zone([p], mkt)
    assert [f.npc_id for f in r1.farms] == [f.npc_id for f in r2.farms]
    assert r1.farms[0].npc_id == 2  # 0.95*9500=9025 > 0.5*9500+0.1*2*19000=8550
    assert len(load_zone_drops([p])) == 3


# --- tradeability / binding ---------------------------------------------------

def test_binding_and_quality_filters():
    blizz = {
        1: BlizzardItem(1, "BoP", "RARE", "ON_ACQUIRE"),
        2: BlizzardItem(2, "Grey", "POOR", ""),
        3: BlizzardItem(3, "BoE", "RARE", "ON_EQUIP"),
    }
    rcfg = FarmRankingConfig()
    assert blizz_excluded(NpcDrop(1, "", 0, "", "", 1, "", 0.5), blizz, rcfg)
    assert blizz_excluded(NpcDrop(1, "", 0, "", "", 2, "", 0.5), blizz, rcfg)
    assert not blizz_excluded(NpcDrop(1, "", 0, "", "", 3, "", 0.5), blizz, rcfg)
    assert not blizz_excluded(NpcDrop(1, "", 0, "", "", 99, "", 0.5), blizz, rcfg)


def test_blizzard_parse_binding_enum():
    b = BlizzardItemsClient.parse_item(
        9, {"id": 9, "name": "Q", "quality": {"type": "COMMON"},
            "preview_item": {"binding": {"type": "ON_ACQUIRE"}}})
    assert b.is_soulbound and b.excluded


# --- route timing ---------------------------------------------------------------

def test_rotation_and_session_timing():
    from woweconomy.analytics.farm_ranking import NpcFarm
    farms = [NpcFarm(i, f"M{i}", 331, "ash", 100.0 * i, 10.0 * i, 1, 0) for i in (1, 2, 3)]
    rot = build_rotation(farms, stops=3)
    assert len(rot.stops) == 3 and rot.revisit_after_stops == 3
    # session math: 60min, 20kpm, 3 stops x 60s travel = 3min travel
    from woweconomy.analytics.farm_ranking import build_sessions
    plans = build_sessions(rot, farms, [], {}, WowEconomyConfig.default(), {},
                           FarmRankingConfig(kills_per_minute=20.0,
                                             travel_sec_per_stop=60.0),
                           minutes_list=(60,))
    route = [s for s in plans if s.scope.startswith("route")][0]
    assert route.kills == pytest.approx((60 - 3) * 20)
    solo = [s for s in plans if s.scope.startswith("solo")][0]
    assert solo.kills == pytest.approx(60 * 20)  # no travel, optimistic


# --- price freshness ---------------------------------------------------------------

def test_price_age():
    assert price_age_days("2026-10-13T00:21:32Z",
                          __import__("datetime").datetime(
                              2026, 10, 16, tzinfo=__import__(
                                  "datetime").timezone.utc)) == 2
    assert price_age_days("garbage") is None


def test_mat_class():
    assert mat_class("Light Leather") == "LEATHER"
    assert mat_class("Runecloth") == "CLOTH"
    assert mat_class("Fine Sword") == "OTHER"


def test_farm_profile():
    from woweconomy.analytics.farm_ranking import NpcFarm, farm_profile
    assert farm_profile(NpcFarm(1, "A", 331, "ash", 100.0, 0.0)) == "THIN"
    assert farm_profile(NpcFarm(1, "A", 331, "ash", 100.0, 100.0,
                                jackpot_value=80.0)) == "JACKPOT"
    assert farm_profile(NpcFarm(1, "A", 331, "ash", 100.0, 100.0,
                                jackpot_value=10.0)) == "STEADY"
    assert farm_profile(NpcFarm(1, "A", 331, "ash", 100.0, 100.0,
                                jackpot_value=30.0)) == "MIXED"


def test_parse_spawns():
    from woweconomy.services.wowhead import WowheadZoneScraper as W
    assert W.parse_spawns("Ashenvale (44)", "ashenvale") == 44
    assert W.parse_spawns("Ashenvale (44) Azshara (3)", "azshara") == 3
    assert W.parse_spawns("Ashenvale (44) Azshara (3)", "tanaris") == 47
    assert W.parse_spawns("", "ashenvale") == 0
    assert W.parse_spawns("no counts here", "ashenvale") == 0


def test_top_mats_units():
    mkt = {100: ItemMarket(100, "Light Leather", 14250.0, 0.308, 6820.0)}
    leaders = top_mats([mkdrop(rate=0.5, qty=4.0, name="Light Leather")], mkt)
    assert leaders[0].units_per_kill == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# The command-line entry point is a thin shell over this module
# ---------------------------------------------------------------------------

def test_an_unknown_zone_id_is_refused_rather_than_skipped():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "farm_ranking.py"
    spec = importlib.util.spec_from_file_location("farm_ranking_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit, match="no zone file"):
        module.zone_paths([10 ** 9])


def test_the_cli_will_not_rank_against_an_empty_market(tmp_path):
    """Zero prices would make every EV zero and read as "nothing pays".

    ``--price-source region`` is the point of the test: with the trained model
    available, an empty market table is not an empty market, because the model
    still prices the items.  The refusal is about having *no* prices at all.
    """
    import importlib.util
    import sqlite3
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "farm_ranking.py"
    spec = importlib.util.spec_from_file_location("farm_ranking_cli2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    empty = tmp_path / "tsm.db"
    con = sqlite3.connect(empty)
    # the tables the region source reads, present but holding no rows: this is
    # "a market that has been initialised and never filled", not "no database"
    con.execute("CREATE TABLE tsm_region_price (key_id INTEGER, name TEXT,"
                " avg_sale_price REAL, sale_rate REAL, sold_per_day REAL)")
    con.execute("CREATE TABLE items (itemId INTEGER, name TEXT)")
    con.commit()
    con.close()
    assert module.main(["mats", "--price-source", "region",
                        "--db", str(empty)]) == 2


def test_a_missing_market_database_is_an_error_not_an_empty_ranking(tmp_path):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "farm_ranking.py"
    spec = importlib.util.spec_from_file_location("farm_ranking_cli3", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["mats", "--price-source", "region",
                        "--db", str(tmp_path / "absent.db")]) == 2


def test_the_leaderboards_read_the_trained_model_not_the_asking_prices(tmp_path):
    """One price source for the zone report and the leaderboards.

    The leaderboards used to read ``tsm_region`` directly, so the same item
    had an asking price in one report and a realizable price in the other.
    """
    from woweconomy.analytics import farm_ranking as fr

    assert fr._model_artifact() is not None, (
        "this test needs the trained price model to be present")

    source = fr.resolve_market([2589, 2592], source="auto", db_path="")
    assert source.source == "model"
    assert "model (" in source.label
    assert source.market, "the model priced nothing"
    for item in source.market.values():
        assert item.price > 0
        # a model price carries the chance of selling; an asking price does not
        assert item.sale_rate >= 0


def test_resolve_market_auto_prefers_the_model_over_the_table():
    """``auto`` must not silently fall back to asking prices while a model exists."""
    from woweconomy.analytics import farm_ranking as fr

    if fr._model_artifact() is None:
        pytest.skip("no trained price model in this workspace")
    source = fr.resolve_market([2589], source="auto", db_path="tsm_markets.db")
    assert source.source == "model"
