"""Tests for the DfA learning engine.

The important ones are the leakage guards: a test that the state returned
for a moment in the past cannot contain anything that was only true
later, and a test that a censored listing never becomes a failure.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from woweconomy.ml.config import AH_CONSIGNMENT_RATE, SENTINEL_COPPER
from woweconomy.ml.dataset import build_dataset
from woweconomy.ml.features import FeatureBuilder, _clean, _ratio, _spread
from woweconomy.ml.journalator import Failure, Invoice, JournalArchive, Posting
from woweconomy.ml.linking import (
    CANCELLED,
    CENSORED,
    EXPIRED,
    SALE,
    infer_duration_h,
    link_postings,
)
from woweconomy.ml.models import (
    _survival_label,
    concordance_index,
    price_metrics,
    probability_metrics,
    _auc,
)
from woweconomy.ml.strategy import ListingStrategy, StrategyConfig, reprice
from woweconomy.ml.tsm_history import TSMHistory, parse_ts
from woweconomy.ml.validate import make_split


# ---------------------------------------------------------------- helpers

def make_posting(t, item_id=1, qty=1, buyout=1000, realm="Dragonmaw", char="Bullbound"):
    return Posting(time=t, realm=realm, character=char, faction="Horde",
                   item_name=f"item{item_id}", item_id=item_id, qty=qty,
                   unit_buyout=buyout, unit_bid=0, deposit=0)


def make_invoice(t, item_id=1, qty=1, value=1000, realm="Dragonmaw", char="Bullbound"):
    return Invoice(time=t, realm=realm, character=char, faction="Horde",
                   item_name=f"item{item_id}", item_id=item_id, qty=qty,
                   invoice_type="seller", value=value, consignment=0, deposit=0,
                   counterparty="Someone")


def make_failure(t, item_id=1, qty=1, kind="expired", realm="Dragonmaw", char="Bullbound"):
    return Failure(time=t, realm=realm, character=char, faction="Horde",
                   item_name=f"item{item_id}", item_id=item_id, qty=qty,
                   failed_type=kind)


# ---------------------------------------------------------------- the linker

def test_sale_is_matched_to_the_price_compatible_listing():
    """Two listings of the same item and stack size; the sale names the price."""
    archive = JournalArchive()
    archive.postings.append(make_posting(1000, buyout=5500))
    archive.postings.append(make_posting(2000, buyout=1800))
    archive.invoices.append(make_invoice(3000, value=1800))
    outcomes, report = link_postings(archive)
    sales = [o for o in outcomes if o.outcome == SALE]
    assert len(sales) == 1
    assert sales[0].posting.unit_buyout == 1800
    assert report.summary()["price_exact_rate"] == 1.0


def test_the_most_recent_listing_is_used_when_the_price_does_not_match():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000, buyout=5500))
    archive.postings.append(make_posting(2000, buyout=1800))
    archive.invoices.append(make_invoice(3000, value=999))
    outcomes, _ = link_postings(archive)
    sales = [o for o in outcomes if o.outcome == SALE]
    assert len(sales) == 1
    assert sales[0].posting.unit_buyout == 1800      # the most recent one
    assert sales[0].price_exact is False


def test_a_listing_with_no_observed_outcome_is_censored_not_failed():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000))
    outcomes, report = link_postings(archive)
    assert outcomes[0].outcome == CENSORED
    assert report.summary()["censored"] == 1
    assert report.summary()["matched_expired"] == 0


def test_a_cancellation_is_its_own_outcome_and_not_a_failure():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000))
    archive.failures.append(make_failure(2000, kind="cancelled"))
    outcomes, report = link_postings(archive)
    assert outcomes[0].outcome == CANCELLED
    assert report.summary()["matched_cancelled"] == 1
    assert report.summary()["matched_expired"] == 0


def test_the_auction_duration_is_inferred_from_the_expiry_delay():
    assert infer_duration_h(12 * 3600) == 12
    assert infer_duration_h(24 * 3600) == 24
    assert infer_duration_h(48 * 3600 + 600) == 48
    assert infer_duration_h(60) is None


# ---------------------------------------------------------------- the labels

class FakeHistory:
    """A history with no market at all: every reference is missing."""

    def canonical_scope(self, realm):
        return "fake"

    def realm_group(self, realm):
        return ("fake", "fake", 0)

    def state_at(self, scope, kind, ts):
        return {}

    def snapshot_time_at(self, scope, kind, ts):
        return 0

    def snapshot_times(self, scope, kind):
        return []


def _frame_for(outcomes, builder=None, horizon=24):
    builder = builder or FeatureBuilder(FakeHistory())
    frame, report = build_dataset(outcomes, builder, (horizon,), 10_000)
    return frame, report


def test_censored_and_cancelled_rows_carry_no_horizon_label():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000))
    archive.postings.append(make_posting(2000, item_id=2))
    archive.failures.append(make_failure(2000, item_id=2, kind="cancelled"))
    outcomes, _ = link_postings(archive)
    frame, report = _frame_for(outcomes)
    assert frame["label_known_24h"].tolist() == [0, 0]
    assert report.horizon_rows[24] == 0


def test_a_sale_after_the_horizon_is_a_known_negative():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000))
    archive.invoices.append(make_invoice(1000 + 48 * 3600, value=1000))
    outcomes, _ = link_postings(archive)
    frame, report = _frame_for(outcomes)
    assert frame["label_known_24h"].tolist() == [1]
    assert frame["sold_within_24h"].tolist() == [0]


def test_a_sale_inside_the_horizon_is_a_positive():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000))
    archive.invoices.append(make_invoice(1000 + 3600, value=1000))
    outcomes, _ = link_postings(archive)
    frame, _ = _frame_for(outcomes)
    assert frame["sold_within_24h"].tolist() == [1]


def test_an_expiry_inside_the_horizon_is_a_known_negative():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000))
    archive.failures.append(make_failure(1000 + 3600, kind="expired"))
    outcomes, _ = link_postings(archive)
    frame, report = _frame_for(outcomes)
    assert frame["label_known_24h"].tolist() == [1]
    assert frame["sold_within_24h"].tolist() == [0]
    assert report.horizon_positive[24] == 0


# ---------------------------------------------------------------- the money

def test_the_net_proceeds_subtract_the_house_cut_and_add_the_deposit_back():
    archive = JournalArchive()
    archive.postings.append(make_posting(1000, buyout=1000))
    inv = make_invoice(2000, value=1000)
    inv.consignment = 50
    inv.deposit = 10
    archive.invoices.append(inv)
    outcomes, _ = link_postings(archive)
    sale = outcomes[0]
    assert sale.gross_unit == 1000
    assert sale.net_unit == 1000 - 50 + 10


def test_the_house_cut_rate_matches_the_observed_invoices():
    assert abs(AH_CONSIGNMENT_RATE - 5190 / 103800) < 1e-6


# ---------------------------------------------------------------- the TSM state

def _tiny_tsm_db(tmp_path: Path) -> Path:
    path = tmp_path / "tsm.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE tsm_snapshot(snapshot_id INTEGER PRIMARY KEY, scope TEXT,
            kind TEXT, updated_at TEXT, fetched_at TEXT, checksum TEXT,
            rows INTEGER, changed INTEGER, path TEXT, etag TEXT, last_modified TEXT);
        CREATE TABLE tsm_price_change(snapshot_id INTEGER, scope TEXT, kind TEXT,
            key_id INTEGER, market_value INTEGER, min_buyout INTEGER, recent INTEGER,
            historical INTEGER, avg_sale_price INTEGER, sale_rate REAL, sold_per_day REAL);
        CREATE TABLE tsm_realm_group(realm_slug TEXT, realm_name TEXT, group_id INTEGER,
            canonical_slug TEXT, canonical_name TEXT, is_canonical INTEGER);
        CREATE TABLE tsm_realm_price(scope TEXT, item_id INTEGER, name TEXT,
            market_value INTEGER, min_buyout INTEGER, recent INTEGER, historical INTEGER);
        CREATE TABLE tsm_region_price(key_id INTEGER, name TEXT, market_value INTEGER,
            historical INTEGER, avg_sale_price INTEGER, sale_rate REAL, sold_per_day REAL);
    """)
    con.execute("INSERT INTO tsm_realm_group VALUES ('spinebreaker','Dragonmaw',1,"
                "'spinebreaker','Spinebreaker',1)")
    con.execute("INSERT INTO tsm_snapshot VALUES (1,'spinebreaker','realm_items',"
                "'2026-10-16T00:00:00Z','2026-10-16T00:00:00Z','',1,1,'','','')")
    con.execute("INSERT INTO tsm_snapshot VALUES (2,'spinebreaker','realm_items',"
                "'2026-10-18T00:00:00Z','2026-10-18T00:00:00Z','',1,1,'','','')")
    con.execute("INSERT INTO tsm_price_change VALUES (1,'spinebreaker','realm_items',"
                "1,1000,0,0,0,0,0,0)")
    con.execute("INSERT INTO tsm_price_change VALUES (2,'spinebreaker','realm_items',"
                "1,5000,0,0,0,0,0,0)")
    con.commit()
    con.close()
    return path


def test_the_state_at_a_moment_never_contains_a_later_value(tmp_path):
    db = _tiny_tsm_db(tmp_path)
    history = TSMHistory(db)
    early = parse_ts("2026-10-16T12:00:00Z")
    late = parse_ts("2026-10-19T00:00:00Z")
    assert history.state_at("spinebreaker", "realm_items", early)[1][0] == 1000
    assert history.state_at("spinebreaker", "realm_items", late)[1][0] == 5000


def test_before_the_first_snapshot_there_is_no_state_at_all(tmp_path):
    db = _tiny_tsm_db(tmp_path)
    history = TSMHistory(db)
    before = parse_ts("2026-10-15T00:00:00Z")
    assert history.state_at("spinebreaker", "realm_items", before) == {}


def test_a_connected_realm_resolves_to_its_group_canonical_scope(tmp_path):
    db = _tiny_tsm_db(tmp_path)
    history = TSMHistory(db)
    assert history.canonical_scope("Dragonmaw") == "spinebreaker"


def test_every_known_realm_is_mapped_to_its_auction_house(tmp_path):
    """The map is a property of TSM, not of the realms with transactions.

    The dataset ships it so an item in a realm the account never traded in
    can still be named with the auction house it would sell in.
    """
    db = _tiny_tsm_db(tmp_path)
    history = TSMHistory(db)
    assert history.realm_groups() == {"Dragonmaw": "spinebreaker"}


def test_the_availability_clock_can_be_switched_to_the_fetch_time(tmp_path):
    db = _tiny_tsm_db(tmp_path)
    history = TSMHistory(db, availability_clock="fetched")
    moment = parse_ts("2026-10-17T00:00:00Z")
    # the second snapshot was fetched on the 18th, so the 16th value stands
    assert history.state_at("spinebreaker", "realm_items", moment)[1][0] == 1000


# ---------------------------------------------------------------- the features

def test_the_tsm_sentinel_is_not_a_price():
    assert _clean(SENTINEL_COPPER) is None
    assert _clean(0) is None
    assert _clean(1500) == 1500


def test_a_missing_reference_does_not_become_zero():
    assert _ratio(100, None) is None
    assert _ratio(None, 100) is None
    assert _ratio(100, 0) is None
    assert _spread([None, None]) is None
    assert _spread([100, 200, None]) == pytest.approx(200 / 150 - 100 / 150)


def test_the_ask_feeds_back_into_the_ask_ratios():
    import math
    base = {"local_market_log": math.log1p(1000.0),
            "region_sale_avg_log": math.log1p(500.0),
            "region_market_log": math.log1p(800.0),
            "ask_unit_log": math.log1p(1000.0)}
    row = reprice(base, 2000, 1)
    assert row["ask_unit_log"] == pytest.approx(math.log1p(2000.0))
    assert row["ask_over_local_market"] == pytest.approx(2.0)
    assert row["ask_over_region_sale_avg"] == pytest.approx(4.0)
    assert row["ask_over_region_market"] == pytest.approx(2.5)


def test_repricing_keeps_the_shape_of_the_row_even_without_a_market():
    row = reprice({}, 1000, 2)
    for key in ("ask_unit_log", "ask_over_local_market", "ask_over_region_sale_avg",
                "ask_over_region_market", "bid_to_buyout", "qty", "qty_log"):
        assert key in row
    assert row["ask_over_local_market"] is None


def test_the_price_model_never_sees_the_ask():
    """The price model must answer "what will the market pay" unaided.

    Every feature derived from the asking price has to be excluded, or the
    model learns to read the ask and the whole exercise becomes circular.
    """
    from woweconomy.ml.features import ASK_FEATURES
    from woweconomy.ml.models import feature_columns

    frame = pd.DataFrame({
        "ask_unit_log": [1.0, 2.0],
        "ask_over_local_market": [1.0, 2.0],
        "ask_over_region_sale_avg": [1.0, 2.0],
        "ask_over_region_market": [1.0, 2.0],
        "bid_to_buyout": [1.0, 2.0],
        "unit_bid_log": [1.0, 2.0],
        "deposit_ratio": [1.0, 2.0],
        "region_sale_avg_log": [1.0, 2.0],
        "local_market_log": [1.0, 2.0],
    })
    price_features = [c for c in feature_columns(frame) if c not in ASK_FEATURES]
    assert not [c for c in price_features if c.startswith("ask")]
    assert "deposit_ratio" not in price_features
    assert "region_sale_avg_log" in price_features


def test_the_ask_features_are_all_excluded():
    from woweconomy.ml.features import ASK_FEATURES
    for name in ("ask_unit_log", "ask_over_local_market", "ask_over_region_sale_avg",
                 "ask_over_region_market", "bid_to_buyout", "unit_bid_log",
                 "deposit_ratio"):
        assert name in ASK_FEATURES


# ---------------------------------------------------------------- the strategy

class FakeProbabilityModel:
    """A sale probability that falls with the ask, in a known way."""

    def __init__(self, features=None, categoricals=None, horizon_h=24):
        self.features = features or []
        self.categoricals = categoricals or []
        self.horizon_h = horizon_h
        self.trained = True

    def predict(self, frame):
        import numpy as np
        asks = np.exp(frame["ask_unit_log"].to_numpy(dtype=float)) - 1.0
        return np.clip(1.0 / (1.0 + asks / 100.0), 0.0, 1.0)


class FakePriceModel:
    """A price model that always says 100 copper."""

    def __init__(self, features=None, categoricals=None):
        self.features = features or []
        self.categoricals = categoricals or []
        self.trained = True

    def predict_net_unit(self, frame):
        import numpy as np
        return np.full(len(frame), 100.0)


def _base_row():
    import math
    return {"ask_unit_log": math.log1p(100.0),
            "local_market_log": math.log1p(120.0),
            "region_sale_avg_log": math.log1p(110.0),
            "region_market_log": math.log1p(130.0),
            "qty": 1.0, "qty_log": math.log1p(1)}


def test_an_unsold_item_is_not_worthless():
    """The strategy must not treat a listing that did not sell as a loss.

    If the item keeps its value, the expected gold of an ask is flatter and
    the recommendation stays near the market; if the item were worth
    nothing, every recommendation would drift upwards.
    """
    holding = ListingStrategy(FakePriceModel(), FakeProbabilityModel(),
                              config=StrategyConfig(objective="ev"))
    deadline = ListingStrategy(FakePriceModel(), FakeProbabilityModel(),
                               config=StrategyConfig(objective="ev_deadline"))
    base = _base_row()
    with_hold = holding.recommend(base, qty=1)
    without = deadline.recommend(base, qty=1)
    assert with_hold["hold_value_gold"] > 0
    assert without["hold_value_gold"] == 0
    # an item that keeps its value must not be pushed to a higher ask
    assert with_hold["recommended_ask"] <= without["recommended_ask"]


def test_the_recommendation_carries_the_price_interval():
    strategy = ListingStrategy(FakePriceModel(), FakeProbabilityModel())
    result = strategy.recommend(_base_row(), qty=1)
    assert "candidates" in result
    assert result["recommended_ask"] > 0
    assert 0.0 <= result["sale_probability"] <= 1.0


def test_the_recommendation_lands_where_the_measured_curve_peaks():
    """The strategy must land in the band the demand curve says is best.

    The curve measured on this account says the sale rate is around 60 %
    from 1.0x to 1.4x the regional sale average and collapses above 2x.
    A strategy that maximises expected gold with a *realistic* hold value
    has to land there; one that treats an unsold item as worthless (or as
    worth its full asking price) does not.
    """
    import math
    import numpy as np

    class CurveModel:
        """P(sell) measured from the demand curve."""

        def __init__(self, features=None, categoricals=None):
            self.features = features or []
            self.categoricals = categoricals or []
            self.horizon_h = 24
            self.trained = True

        def predict(self, frame):
            asks = np.exp(frame["ask_unit_log"].to_numpy(dtype=float)) - 1.0
            ratio = asks / 100.0          # the sale average is 100 copper
            # the measured shape: flat to 1.4x, then a collapse
            p = np.where(ratio <= 1.4, 0.61,
                         np.where(ratio <= 2.0, 0.39,
                                  np.where(ratio <= 3.0, 0.085, 0.026)))
            return p

    class Price100:
        def __init__(self, features=None, categoricals=None):
            self.features = features or []
            self.categoricals = categoricals or []
            self.trained = True

        def predict_net_unit(self, frame):
            return np.full(len(frame), 100.0)

    base = {"ask_unit_log": math.log1p(100.0),
            "local_market_log": math.log1p(120.0),
            "region_sale_avg_log": math.log1p(100.0),
            "region_market_log": math.log1p(130.0),
            "qty": 1.0, "qty_log": math.log1p(1)}
    strategy = ListingStrategy(Price100(), CurveModel(),
                                config=StrategyConfig(objective="ev"))
    result = strategy.recommend(base, qty=1)
    ratio = result["recommended_ask"] / 100.0
    assert 0.8 <= ratio <= 2.5, f"the recommendation landed at {ratio}x, not in the band"


def test_the_hold_value_is_the_expected_proceeds_of_relisting():
    """Not the asking price: an unsold item is worth what it will fetch."""
    import math
    strategy = ListingStrategy(FakePriceModel(), FakeProbabilityModel(),
                                config=StrategyConfig(objective="ev"))
    base = _base_row()
    result = strategy.recommend(base, qty=1)
    # the hold value can never exceed the item's market value
    assert result["hold_value_gold"] <= result["predicted_unit_price"] / 10000.0 * 1.01
    assert result["hold_value_gold"] > 0


# ---------------------------------------------------------------- the split

def _frame_with_days():
    rows = []
    for day in range(20742, 20746):
        for i in range(50):
            rows.append({"date": day, "ts": day * 86400 + i, "y": i})
    return pd.DataFrame(rows)


def test_the_split_is_chronological_and_the_test_window_is_untouched():
    frame = _frame_with_days()
    split = make_split(frame, n_folds=3, test_fraction=0.25)
    assert len(split.test) == 50                       # the last day only
    assert split.test["date"].min() == 20745
    for fold in split.folds:
        assert fold.train["date"].max() < fold.valid["date"].min()
        assert fold.valid["date"].max() < split.test["date"].min()


def test_a_frame_with_too_little_history_says_so_instead_of_pretending():
    frame = _frame_with_days().query("date < 20743")
    split = make_split(frame, n_folds=3)
    assert not split.folds
    assert split.note


# ---------------------------------------------------------------- the metrics

def test_the_price_metrics_report_the_multiplicative_error():
    import numpy as np
    truth = np.array([np.log(1000.0), np.log(2000.0)])
    pred = np.array([np.log(1100.0), np.log(1800.0)])
    metrics = price_metrics(truth, pred)
    assert metrics["rmse_log"] > 0
    assert 0.9 < metrics["median_ratio"] < 1.1
    assert metrics["within_50pct"] == 1.0


def test_the_auc_handles_a_perfect_ranking():
    import numpy as np
    assert _auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert _auc(np.array([0, 0, 1, 1]), np.array([0.9, 0.8, 0.2, 0.1])) == 0.0


def test_the_probability_metrics_report_the_base_rate():
    import numpy as np
    metrics = probability_metrics(np.array([1, 0, 1, 0]), np.array([0.9, 0.1, 0.8, 0.2]))
    assert metrics["base_rate"] == 0.5
    assert metrics["auc"] == 1.0
    assert metrics["brier"] < 0.05


# ------------------------------------------------------- survival concordance

def test_the_c_index_ignores_pairs_whose_earlier_row_is_censored():
    import numpy as np
    from math import isnan
    y = np.array([1.0, 2.0, 3.0])
    observed = np.array([1, 0, 0])
    # Only row 0 is an observed event, so only the two pairs it forms with
    # the later censored rows are comparable.  The censored-censored pair
    # must not be counted.
    assert concordance_index(y, np.array([1.0, 100.0, 100.0]), observed) == 1.0
    assert concordance_index(y, np.array([100.0, 1.0, 1.0]), observed) == 0.0
    # A constant prediction cannot order anything: 0.5 is the honest score,
    # not the near-zero the old implementation produced.
    assert concordance_index(y, np.full(3, 7.0), observed) == 0.5
    assert isnan(concordance_index(np.array([1.0, 1.0]),
                                   np.array([1.0, 2.0]),
                                   np.array([1, 0])))


def test_the_c_index_handles_tied_event_times_and_tied_predictions():
    import numpy as np
    y = np.array([1.0, 1.0])
    both = np.array([1, 1])
    assert concordance_index(y, np.array([1.0, 1.0]), both) == 1.0
    assert concordance_index(y, np.array([1.0, 2.0]), both) == 0.5
    # Without an event mask every row is treated as an event.
    assert concordance_index(np.array([1.0, 2.0]), np.array([1.0, 2.0])) == 1.0


def test_the_survival_label_is_an_interval_not_an_event_flag():
    import numpy as np
    frame = pd.DataFrame({
        "tts_hours": [2.0, 5.0, 9.0],
        "tts_censored": [0, 1, 0],
    })
    label = _survival_label(frame)
    # observed sale: [duration, duration]; right-censored: [duration, -1]
    assert label.tolist() == [[2.0, 2.0], [5.0, -1.0], [9.0, 9.0]]

