"""Tests for the item-level demand join (bit 0 among high-score accounts)."""
import numpy as np
import pytest

from woweconomy.analytics.item_demand import (
    ItemDemand, count_wanters, rank_demand, rank_item_union,
)


def test_count_wanters_sums_zero_bits():
    wants = np.array([
        [1, 0, 0],
        [1, 1, 0],
        [0, 0, 0],
    ], dtype=np.uint8)
    assert count_wanters(wants).tolist() == [2, 1, 0]


def test_rank_filters_source_price_and_rate():
    rows = rank_demand(
        counts=np.array([4, 2, 1], dtype=np.int64),
        eligible=4,
        slots=[101, 102, 103],
        sellable={101: (1000, 1, "A"), 102: (0, 2, "B"), 103: (500, 3, "C")},
        catalogue={101: ("A", "World Drop"), 102: ("B", "Drop"),
                   103: ("C", "Vendor")},
        min_wanters=1, min_rate=0.25,
        exclude_sources=("vendor", "trading post"))
    # 102 has no price; 103 is a vendor appearance.
    assert [r.appearance_id for r in rows] == [101]
    assert rows[0].want_rate == 1.0
    assert rows[0].gold_gap == 0.4
    assert rows[0].gold_per_wanter == 0.1


def test_rank_min_rate_and_sort_wanters():
    counts = np.array([4, 3, 1], dtype=np.int64)
    slots = [101, 102, 103]
    sellable = {101: (1000, 1, "A"), 102: (2000, 2, "B"), 103: (500, 3, "C")}
    catalogue = {101: ("A", "Drop"), 102: ("B", "Drop"), 103: ("C", "Drop")}
    gold = rank_demand(counts, 4, slots, sellable, catalogue,
                       min_wanters=1, min_rate=0.1, sort="gold")
    assert [r.appearance_id for r in gold] == [102, 101, 103]
    by_wanters = rank_demand(counts, 4, slots, sellable, catalogue,
                             min_wanters=1, min_rate=0.1, sort="wanters")
    assert [r.appearance_id for r in by_wanters] == [101, 102, 103]
    only_high = rank_demand(counts, 4, slots, sellable, catalogue,
                            min_wanters=1, min_rate=0.5, sort="rate")
    assert [r.appearance_id for r in only_high] == [101, 102]


def test_item_union_counts_each_buyer_once():
    # Three accounts, two appearances granted by the same BoE item.
    wants = np.array([
        [1, 1],   # misses both -> one buyer
        [0, 1],   # misses one  -> one buyer
        [0, 0],   # owns both  -> not a buyer
    ], dtype=np.uint8)
    rows = rank_item_union(
        wants=wants, slots=[11, 12],
        sellable={11: (1000, 500, "Cape"), 12: (1000, 500, "Cape")},
        catalogue={11: ("Cape A", "Drop"), 12: ("Cape B", "Drop")},
        min_wanters=1, min_rate=0.1)
    assert len(rows) == 1
    assert rows[0].item_id == 500
    assert rows[0].grants == 2
    assert rows[0].wanters == 2          # not 3 (one per appearance)
    assert rows[0].want_rate == pytest.approx(2 / 3)


def test_item_union_drops_excluded_source():
    wants = np.array([[1, 1], [0, 0]], dtype=np.uint8)
    rows = rank_item_union(
        wants=wants, slots=[11, 12],
        sellable={11: (1000, 500, "Cape"), 12: (1000, 500, "Cape")},
        catalogue={11: ("Cape A", "Vendor"), 12: ("Cape B", "Drop")},
        min_wanters=1, min_rate=0.1)
    # The vendor appearance is ignored; the drop appearance has one buyer.
    assert len(rows) == 1
    assert rows[0].grants == 1
    assert rows[0].wanters == 1


def test_item_demand_properties():
    row = ItemDemand(appearance_id=1, name="A", source="Drop",
                     wanters=3, eligible=4, price=10_000,
                     item_id=9, item_name="A item")
    assert row.want_rate == 0.75
    assert row.gold_gap == 3.0
    assert row.gold_per_wanter == 1.0
