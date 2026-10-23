"""The farm report and the DfA estimate must quote one price source.

The estimate reads the live regional TSM state (``tsm_region_price``),
and its ``avg_sale_price`` is exactly the ``dbregionsaleavg`` reference
the shipped model consumes.  The farm ranking used to read a *frozen
copy* of the same feed (``market_snapshots``), so the two pipelines
disagreed on the price of the same item.

These tests pin the unification: the farm loader reads the estimate's
table, the frozen snapshot stays reachable as an explicit fallback, and
the numbers are provably the same ones the model is fed.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from woweconomy.analytics.farm_ranking import (
    load_latest_market,
    load_region_market,
    load_snapshot_market,
    market_asof_db,
)

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "tsm_markets.db"


def _make_db(path: Path, *, region: bool, snapshot: bool) -> Path:
    """A minimal market DB with the requested tables."""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE items (itemId INTEGER PRIMARY KEY, name TEXT)")
        con.executemany("INSERT INTO items VALUES (?, ?)",
                         [(1, "Worn Shortsword"), (2, "Bent Staff")])
        if region:
            con.execute("""CREATE TABLE tsm_region_price (
                key_id INTEGER PRIMARY KEY, name TEXT, market_value INTEGER,
                historical INTEGER, avg_sale_price REAL, sale_rate REAL,
                sold_per_day REAL, updated_at TEXT)""")
            con.executemany(
                "INSERT INTO tsm_region_price VALUES (?,?,?,?,?,?,?,?)",
                [(1, "Worn Shortsword", 9000, 8000, 5000.0, 0.25, 3.0,
                  "2026-10-17T00:26:55Z"),
                 (2, "", 7000, 6000, 2500.0, 0.10, 0.0,
                  "2026-10-17T00:26:55Z")])
        if snapshot:
            con.execute("""CREATE TABLE snapshots (
                snapshotId INTEGER PRIMARY KEY, snapshotTimestamp TEXT)""")
            con.execute("INSERT INTO snapshots VALUES (1, '2026-10-13T00:21:32Z')")
            con.execute("""CREATE TABLE market_snapshots (
                snapshotId INTEGER, itemId INTEGER, marketValue INTEGER,
                historical INTEGER, avgSalePrice REAL, saleRate REAL,
                soldPerDay REAL)""")
            con.executemany(
                "INSERT INTO market_snapshots VALUES (1,?,?,?,?,?,?)",
                [(1, 9000, 8000, 4000.0, 0.20, 2.0),
                 (2, 7000, 6000, 2000.0, 0.08, 0.0)])
        con.commit()
    finally:
        con.close()
    return path


# --- the loader reads the estimate's table ------------------------------

def test_region_loader_reads_the_live_regional_table(tmp_path):
    db = _make_db(tmp_path / "m.db", region=True, snapshot=False)
    market = load_region_market(str(db))
    assert set(market) == {1, 2}
    # the price is avg_sale_price, never market_value
    assert market[1].price == 5000.0
    assert market[1].sale_rate == 0.25
    assert market[1].sold_per_day == 3.0
    # the name falls back to the items table when the region row has none
    assert market[2].name == "Bent Staff"


def test_latest_prefers_the_live_regional_table(tmp_path):
    db = _make_db(tmp_path / "m.db", region=True, snapshot=True)
    market = load_latest_market(str(db))
    # the frozen snapshot says 4000; the live regional table says 5000
    assert market[1].price == 5000.0
    assert market == load_region_market(str(db))


def test_latest_falls_back_to_the_frozen_snapshot(tmp_path):
    db = _make_db(tmp_path / "m.db", region=False, snapshot=True)
    market = load_latest_market(str(db))
    assert market[1].price == 4000.0
    assert market == load_snapshot_market(str(db))


def test_an_empty_region_table_falls_back_to_the_frozen_snapshot(tmp_path):
    db = _make_db(tmp_path / "m.db", region=True, snapshot=True)
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM tsm_region_price")
        con.commit()
    finally:
        con.close()

    market = load_latest_market(str(db))

    assert market[1].price == 4000.0
    assert market == load_snapshot_market(str(db))


def test_the_frozen_snapshot_stays_reachable(tmp_path):
    db = _make_db(tmp_path / "m.db", region=True, snapshot=True)
    legacy = load_snapshot_market(str(db))
    assert legacy[1].price == 4000.0
    assert legacy[1].price != load_region_market(str(db))[1].price


# --- the vintage the report prints --------------------------------------

def test_asof_reports_the_region_vintage(tmp_path):
    db = _make_db(tmp_path / "m.db", region=True, snapshot=True)
    assert market_asof_db(str(db)) == "2026-10-17T00:26:55Z"
    # the legacy source reports the vintage of the snapshot it reads
    assert market_asof_db(str(db), "legacy") == "2026-10-13T00:21:32Z"


def test_asof_falls_back_to_the_snapshot_timestamp(tmp_path):
    db = _make_db(tmp_path / "m.db", region=False, snapshot=True)
    assert market_asof_db(str(db)) == "2026-10-13T00:21:32Z"


# --- the numbers the model is fed ---------------------------------------

@pytest.mark.skipif(not DB.exists(), reason="the market DB is not here")
def test_farm_price_is_the_reference_the_estimate_consumes():
    """farm_ranking's price IS the estimate's ``dbregionsaleavg``.

    The shipped model is fed three regional references; the sale average
    is one of them.  If the farm loader and the model disagree about
    that number, the report and the tooltip cannot both be right.
    """
    from woweconomy.ml.tsm_history import TSMHistory

    # The region series on its own: loading the realm series as well
    # costs 50 seconds and changes nothing here (the two digests are
    # identical), because the region state is accumulated per scope.
    history = TSMHistory(str(DB), kinds=("region_items",))
    state = history.latest_state("region", "region_items")
    assert state, "the regional state is empty"

    market = load_region_market(str(DB))
    assert market, "the regional market is empty"

    # the region row is (market_value, min_buyout, recent, historical,
    # avg_sale_price, sale_rate, sold_per_day): index 4 is the sale
    # average, the reference the model calls dbregionsaleavg.
    checked = 0
    for item_id, row in state.items():
        entry = market.get(item_id)
        assert entry is not None, f"item {item_id} is missing from the market"
        assert entry.price == float(row[4]), f"item {item_id} disagrees"
        checked += 1
    assert checked == len(state)


@pytest.mark.skipif(not DB.exists(), reason="the market DB is not here")
def test_the_two_pipelines_agree_on_every_drop_item():
    """Every item a zone drops is priced, and priced the same way."""
    from woweconomy.analytics.farm_ranking import load_zone_drops

    zones = sorted((ROOT / "woweconomy" / "wowhead_zones").glob("zone_*.json"))
    if not zones:
        pytest.skip("no zone files here")
    drops = load_zone_drops(zones)
    if not drops:
        pytest.skip("no drops here")
    market = load_region_market(str(DB))
    missing = [d.item_id for d in drops if d.item_id not in market]
    assert not missing, f"{len(missing)} drop items have no regional price"


# --- the CLI ------------------------------------------------------------

def test_cli_exposes_the_price_source():
    r = subprocess.run([sys.executable, "-m", "woweconomy.analytics.farm_ranking",
                        "--help"], capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0
    assert "--price-source" in r.stdout
    for flag in ("region", "legacy", "csv"):
        assert flag in r.stdout, flag
