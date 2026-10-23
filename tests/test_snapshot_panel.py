"""The market panel, and the two things it must not get wrong.

The panel is the evidence behind the risk targets, so its failure modes are
quiet ones: a market split across two names produces a smaller series and no
error, and a variant pooled with its siblings produces a number that belongs
to neither.  Both are asserted here against a database built for the test.
"""
from __future__ import annotations

import sqlite3
import sys
import zlib
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from woweconomy.intel import store                      # noqa: E402
from woweconomy.ml import sentinel                      # noqa: E402
from woweconomy.ml import snapshot_panel as sp          # noqa: E402

HOUR = 3600
T0 = 1_792_600_000


def _db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    return con


def _realm(con, cr, name, slug, region="eu"):
    con.execute(
        "INSERT INTO ah_realm (region, connected_realm_id, realm_name,"
        " realm_slug) VALUES (?,?,?,?)", (region, cr, name, slug))


def _snapshot(con, sid, cr, realm, slug, ts, region="eu"):
    con.execute(
        "INSERT INTO ah_snapshot (snapshot_id, region, connected_realm_id,"
        " realm, realm_slug, captured_at, source, payload_sha256,"
        " auction_count, commodity_count, rejected_rows)"
        " VALUES (?,?,?,?,?,?,'test',?,0,0,0)",
        (sid, region, cr, realm, slug, ts, "sha-" + sid))


def _auction(con, sid, key, item_id, price, qty=1, status="ok",
             band="LONG", variant=None):
    con.execute(
        "INSERT INTO ah_auction (snapshot_id, auction_key, item_id, quantity,"
        " unit_price_copper, total_price_copper, unit_price_source,"
        " time_left_band, is_commodity, price_status, price_reason,"
        " variant_key) VALUES (?,?,?,?,?,?,'unit',?,0,?,'',?)",
        (sid, key, item_id, qty, price, price * qty, band, status, variant))


@pytest.fixture()
def market(tmp_path):
    """Two books: one observed twice, one observed once."""
    path = tmp_path / "intel.sqlite"
    con = _db(path)
    _realm(con, 3656, "Dragonmaw", "dragonmaw")
    _realm(con, 3656, "Haomarush", "haomarush")
    _realm(con, 1305, "Kazzak", "kazzak")

    # Book 3656, two snapshots.  The first is stored under the empty slug,
    # which is what the single-realm fetch path produced historically.
    _snapshot(con, "s1", 3656, "Dragonmaw", "", T0)
    _snapshot(con, "s2", 3656, "Dragonmaw", "dragonmaw", T0 + 24 * HOUR)
    # Book 1305, one snapshot: cannot contribute a change.
    _snapshot(con, "s3", 1305, "Kazzak", "kazzak", T0)

    for sid, price in (("s1", 1000), ("s2", 900), ("s3", 500)):
        _auction(con, sid, sid + "-a", item_id=100, price=price, qty=1)
        _auction(con, sid, sid + "-b", item_id=100, price=price * 2, qty=3)
    con.commit()
    con.close()
    return path


def test_the_same_book_under_two_names_is_one_scope(market):
    """The regression: '' and 'dragonmaw' are the same market.

    Historical snapshots carry an empty `realm_slug`.  Keying the panel on
    the stored name split one book into two series, which silently halved
    the observations available to a temporal target.
    """
    panel, report = sp.load_market_panel(market)
    assert report.books == 1
    assert set(panel["scope"]) == {"dragonmaw"}
    assert panel["ts"].nunique() == 2


def test_a_book_seen_once_is_dropped_and_counted(market):
    panel, report = sp.load_market_panel(market)
    assert report.books_dropped_single_snapshot == 1
    assert "kazzak" not in set(panel["scope"])


def test_a_book_seen_once_is_kept_when_asked_for(market):
    panel, report = sp.load_market_panel(market, min_snapshots=1)
    assert report.books == 2
    assert "kazzak" in set(panel["scope"])


def test_market_is_quantity_weighted_and_min_is_the_cheapest(market):
    panel, _ = sp.load_market_panel(market)
    first = panel[panel["ts"] == T0].iloc[0]
    # 1000x1 + 2000x3 over 4 units = 1750, not the 1500 a plain mean gives.
    assert first["market"] == pytest.approx(1750.0)
    assert first["min_buyout"] == 1000
    assert first["listings"] == 2
    assert first["supply"] == 4


def test_recent_only_counts_short_time_left(market):
    con = sqlite3.connect(market)
    _auction(con, "s1", "s1-c", item_id=100, price=700, band="SHORT")
    con.commit()
    con.close()
    panel, _ = sp.load_market_panel(market)
    first = panel[panel["ts"] == T0].iloc[0]
    assert first["recent"] == 700


def test_a_rejected_price_never_becomes_the_cheapest_listing(market):
    con = sqlite3.connect(market)
    _auction(con, "s1", "s1-bad", item_id=100, price=1, status="sentinel")
    _auction(con, "s1", "s1-out", item_id=100, price=2, status="outlier")
    con.commit()
    con.close()
    panel, _ = sp.load_market_panel(market)
    first = panel[panel["ts"] == T0].iloc[0]
    assert first["min_buyout"] == 1000


def test_two_variants_of_one_item_stay_two_markets(market):
    con = sqlite3.connect(market)
    for sid in ("s1", "s2"):
        _auction(con, sid, sid + "-v1", item_id=82800, price=400,
                 variant="82800:pet-a")
        _auction(con, sid, sid + "-v2", item_id=82800, price=900_000,
                 variant="82800:pet-b")
    con.commit()
    con.close()
    panel, report = sp.load_market_panel(market)
    assert report.variants_pooled is False
    pets = panel[panel["item_id"] == 82800]
    assert pets["variant_key"].nunique() == 2
    # A pooled row would have min_buyout 400 and a market near 450k; neither
    # variant's own price may be contaminated by the other.
    assert set(pets["min_buyout"]) == {400, 900_000}


def test_variants_are_pooled_when_asked_for(market):
    con = sqlite3.connect(market)
    for sid in ("s1", "s2"):
        _auction(con, sid, sid + "-v1", item_id=82800, price=400,
                 variant="82800:pet-a")
        _auction(con, sid, sid + "-v2", item_id=82800, price=900_000,
                 variant="82800:pet-b")
    con.commit()
    con.close()
    panel, report = sp.load_market_panel(market, group_by_variant=False)
    assert report.variants_pooled is True
    pets = panel[panel["item_id"] == 82800]
    assert pets["variant_key"].nunique() == 1
    assert pets["min_buyout"].max() == 400


def test_a_missing_database_is_reported_not_raised(tmp_path):
    panel, report = sp.load_market_panel(tmp_path / "nope.sqlite")
    assert panel.empty
    assert list(panel.columns) == list(sp.PANEL_COLUMNS)
    assert report.notes and "not found" in report.notes[0]


def test_a_single_snapshot_book_says_so_rather_than_returning_silence(tmp_path):
    path = tmp_path / "intel.sqlite"
    con = _db(path)
    _realm(con, 1305, "Kazzak", "kazzak")
    _snapshot(con, "s3", 1305, "Kazzak", "kazzak", T0)
    _auction(con, "s3", "a", item_id=100, price=500)
    con.commit()
    con.close()
    panel, report = sp.load_market_panel(path)
    assert panel.empty
    assert report.books_dropped_single_snapshot == 1
    assert any("observed twice" in note for note in report.notes)


def test_coverage_counts_observations_per_book(market):
    panel, _ = sp.load_market_panel(market)
    table = sp.coverage_by_scope(panel)
    assert list(table.index) == ["dragonmaw"]
    assert table.loc["dragonmaw", "snapshots"] == 2
    assert table.loc["dragonmaw", "items"] == 1


# ---------------------------------------------------------------------------
# The risk examples built on top of the panel
# ---------------------------------------------------------------------------

def _panel(rows) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(sp.PANEL_COLUMNS))
    frame["ts"] = frame["ts"].astype("int64")
    return frame


def test_the_target_is_built_per_variant(market):
    """Item 82800 pooled is two markets; the target must not mix them."""
    con = sqlite3.connect(market)
    for sid, ts in (("s1", T0), ("s2", T0 + 24 * HOUR)):
        _auction(con, sid, sid + "-v1", item_id=82800, price=400,
                 variant="82800:pet-a")
        _auction(con, sid, sid + "-v2", item_id=82800, price=900_000,
                 variant="82800:pet-b")
    con.commit()
    con.close()
    panel, _ = sp.load_market_panel(market)
    examples = sentinel.build_risk_examples(panel, horizons_h=[24])
    pets = examples[examples["item_id"] == 82800]
    assert set(pets["variant_key"]) == {"82800:pet-a", "82800:pet-b"}
    # Neither pet changed price, so neither is undercut or declining.
    assert pets["undercut_pressure"].sum() == 0
    assert pets["price_decline"].sum() == 0


def test_a_horizon_that_never_closed_produces_no_row():
    panel = _panel([
        (T0, "dragonmaw", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
        (T0 + 2 * HOUR, "dragonmaw", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
    ])
    examples = sentinel.build_risk_examples(panel, horizons_h=[24])
    assert examples.empty


def test_a_drop_beyond_the_horizon_window_is_not_evidence():
    """A snapshot three days later says nothing about a 24h horizon."""
    panel = _panel([
        (T0, "dragonmaw", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
        (T0 + 72 * HOUR, "dragonmaw", 100, "100:x", 500.0, 500.0, None, 1, 1),
    ])
    examples = sentinel.build_risk_examples(panel, horizons_h=[24])
    assert examples.empty


def test_a_cheaper_listing_later_is_undercut_pressure():
    panel = _panel([
        (T0, "dragonmaw", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
        (T0 + 24 * HOUR, "dragonmaw", 100, "100:x", 900.0, 800.0, None, 1, 1),
    ])
    examples = sentinel.build_risk_examples(panel, horizons_h=[24])
    assert len(examples) == 1
    assert examples.iloc[0]["undercut_pressure"] == 1
    assert examples.iloc[0]["price_decline"] == 1


def test_the_examples_are_ordered_by_time():
    """A chronological split over an unordered frame is a different split."""
    panel = _panel([
        (T0 + 24 * HOUR, "zzz", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
        (T0, "zzz", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
        (T0 + 48 * HOUR, "zzz", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
        (T0 + 24 * HOUR, "aaa", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
        (T0, "aaa", 100, "100:x", 1000.0, 1000.0, None, 1, 1),
    ])
    examples = sentinel.build_risk_examples(panel, horizons_h=[24])
    assert list(examples["ts"]) == sorted(examples["ts"])


# ---------------------------------------------------------------------------
# A feature that has to be the same number twice
# ---------------------------------------------------------------------------

def test_the_scope_code_is_the_same_in_every_process():
    """`hash()` is salted per process; a model feature cannot be.

    The old form produced 210 for 'dragonmaw' in one run and 493 in the
    next, so two training runs over identical data saw different features.
    """
    expected = zlib.crc32(b"dragonmaw") % 997
    assert sentinel._scope_code("dragonmaw") == float(expected)
    assert sentinel._scope_code("dragonmaw") == sentinel._scope_code("dragonmaw")


def test_the_scope_code_differs_between_realms():
    codes = {sentinel._scope_code(name)
             for name in ("dragonmaw", "kazzak", "silvermoon", "draenor")}
    assert len(codes) == 4


def test_a_missing_scope_is_not_a_number():
    assert pd.isna(sentinel._scope_code(None))
    assert pd.isna(sentinel._scope_code(""))


# ---------------------------------------------------------------------------
# Choosing the source, and saying which one was used
# ---------------------------------------------------------------------------

def _cfg(tmp_path, source, **overrides):
    from woweconomy.ml.sentinel import SentinelConfig
    kwargs = dict(
        intel_db=tmp_path / "intel.sqlite",
        tsm_db=tmp_path / "tsm.db",
        models_root=tmp_path / "models",
        reports_dir=tmp_path / "reports",
        panel_source=source,
    )
    kwargs.update(overrides)
    return SentinelConfig(**kwargs)


def test_a_thin_blizzard_panel_falls_back_to_tsm_and_says_why(market, tmp_path):
    cfg = _cfg(tmp_path, "auto", intel_db=market, min_blizzard_panel_rows=10**9,
               min_blizzard_span_h=0)
    panel, note = sentinel.load_panel(cfg)
    assert panel.empty                       # the TSM db does not exist here
    assert note.startswith("panel=tsm")
    assert "too short" in note and "rows" in note


def test_a_panel_shorter_than_the_horizon_falls_back_even_when_large(
        market, tmp_path):
    """Row count is not the gate; span is.

    The fixture spans 24h.  A panel that cannot close a 48h horizon cannot
    teach it, so preferring it because it has many rows would quietly train
    the risk models on labels that never close.
    """
    cfg = _cfg(tmp_path, "auto", intel_db=market, min_blizzard_panel_rows=1,
               min_blizzard_span_h=72)
    panel, note = sentinel.load_panel(cfg)
    assert note.startswith("panel=tsm")
    assert "span" in note


def test_an_explicit_blizzard_source_does_not_fall_back(market, tmp_path):
    cfg = _cfg(tmp_path, "blizzard", intel_db=market,
               min_blizzard_panel_rows=10**9)
    panel, note = sentinel.load_panel(cfg)
    assert not panel.empty
    assert note.startswith("panel=blizzard")


def test_a_thick_blizzard_panel_is_preferred_over_tsm(market, tmp_path):
    cfg = _cfg(tmp_path, "auto", intel_db=market, min_blizzard_panel_rows=1,
               min_blizzard_span_h=0)
    panel, note = sentinel.load_panel(cfg)
    assert not panel.empty
    assert note.startswith("panel=blizzard")


def test_an_unknown_panel_source_is_refused(tmp_path):
    cfg = _cfg(tmp_path, "telepathy")
    with pytest.raises(ValueError, match="panel_source"):
        sentinel.load_panel(cfg)
