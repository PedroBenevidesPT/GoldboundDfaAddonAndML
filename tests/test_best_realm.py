"""Tests for the cluster ranking (scripts/best_realm.py).

Fixtures only: two synthetic clusters, two appearances, one item per cluster.
Nothing reads the real DfA/TSM snapshot, so every number below is hand-checkable.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.best_realm import (  # noqa: E402
    Cluster, Shelf, band_lambda, explain_items, missing_matrix, norm, rank,
)


def test_norm_strips_accents_and_punctuation():
    assert norm("Pozzo dell'Eternit\u00e0") == "pozzodelleternita"
    assert norm("Mar\u00e9cage de Zangar") == norm("marecage-de-zangar")
    assert norm("Al'Akir") == norm("alakir")


def test_band_lambda_boundaries():
    assert band_lambda(99) == 0.0165
    assert band_lambda(100) == 0.011
    assert band_lambda(999) == 0.011
    assert band_lambda(1_000) == 0.009
    assert band_lambda(10_000) == 0.007
    assert band_lambda(100_000) == 0.005
    assert band_lambda(10_000_000) == 0.005


def fixture_world():
    # Two accounts, two appearances (10, 20).  Account 0 owns 10 only.
    bits = np.array([[True, False], [False, False]], dtype=bool)
    clusters = ["A", "B"]
    basket = [
        Shelf(1, "alpha", 1, [10], 100e4),
        Shelf(2, "beta", 1, [20], 200e4),
    ]
    names, counts, n_top = missing_matrix(bits, clusters, basket, {10: 0, 20: 1})
    return names, counts, n_top, basket


def test_missing_matrix_counts_per_cluster_and_item():
    names, counts, n_top, _basket = fixture_world()
    assert names == ["A", "B"]
    assert n_top.tolist() == [1.0, 1.0]
    # Account A owns alpha and misses beta; B misses both.
    assert counts.tolist() == [[0.0, 1.0], [1.0, 1.0]]


def test_empty_shelf_doubles_the_probability_and_gold():
    names, counts, n_top, basket = fixture_world()
    clusters = {"A": Cluster("A", "a", ["A"]), "B": Cluster("B", "b", ["B"])}
    # Both sell alpha at the same price; only B has beta listed (buyout > 0).
    prices = {
        "A": {1: (100e4, 100e4, 100e4, 100e4), 2: (200e4, 0.0, 0.0, 0.0)},
        "B": {1: (100e4, 100e4, 100e4, 100e4), 2: (200e4, 200e4, 200e4, 200e4)},
    }
    region = {1: (100e4, 0.01), 2: (200e4, 0.01)}
    rows = rank(clusters, names, counts, n_top, prices, region, basket)
    by = {r.cluster: r for r in rows}
    # A has the empty shelf for beta -> higher probability for the same buyer.
    assert by["A"].gold30 > by["B"].gold30
    assert by["A"].sales30 > by["B"].sales30


def test_price_is_capped_to_twice_the_cross_cluster_median():
    names, counts, n_top, basket = fixture_world()
    clusters = {"A": Cluster("A", "a", ["A"]), "B": Cluster("B", "b", ["B"])}
    # A asks 10x the median for alpha; that wall must not set the expected gold.
    prices = {
        "A": {1: (1000e4, 1000e4, 1000e4, 1000e4), 2: (200e4, 200e4, 200e4, 200e4)},
        "B": {1: (100e4, 100e4, 100e4, 100e4), 2: (200e4, 200e4, 200e4, 200e4)},
    }
    region = {1: (100e4, 0.01), 2: (200e4, 0.01)}
    rows = rank(clusters, names, counts, n_top, prices, region, basket, price_cap=2.0)
    a = next(r for r in rows if r.cluster == "A")
    # alpha is priced at 2x the 150g median (median of 100 and 1000 is 550; capped
    # at 1100, still far below 10000), so gold stays in the same order of magnitude.
    assert a.gold30 < 1000


def test_explain_matches_rank_for_the_same_cluster():
    names, counts, n_top, basket = fixture_world()
    clusters = {"A": Cluster("A", "a", ["A"]), "B": Cluster("B", "b", ["B"])}
    prices = {
        "A": {1: (100e4, 100e4, 100e4, 100e4), 2: (200e4, 200e4, 200e4, 200e4)},
        "B": {1: (100e4, 100e4, 100e4, 100e4), 2: (200e4, 200e4, 200e4, 200e4)},
    }
    region = {1: (100e4, 0.01), 2: (200e4, 0.01)}
    rows = rank(clusters, names, counts, n_top, prices, region, basket)
    a = next(r for r in rows if r.cluster == "A")
    items = explain_items(basket, names, counts, n_top, prices, region, "A", 10.0)
    assert len(items) == 2
    total = sum(item["p30"] * item["price_gold"] * 0.95 * item["qty"] for item in items)
    assert total == pytest.approx(a.gold30, rel=1e-9)
