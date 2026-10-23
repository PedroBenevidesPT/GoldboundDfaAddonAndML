"""Candidate selection must be deterministic, explicit and auditable."""
from __future__ import annotations

import itertools

import pytest

from woweconomy.ml.sentinel import BASELINE_NAME, Selection, select_candidate


def test_a_candidate_that_beats_the_baseline_by_enough_wins():
    selection = select_candidate(
        0.50,
        {"catboost": {"logloss": 0.40, "brier": 0.12},
         "logistic": {"logloss": 0.45, "brier": 0.13}},
        metric="logloss", tiebreak="brier", min_gain=0.002)
    assert selection.name == "catboost"
    assert selection.margin == pytest.approx(0.10)


def test_a_candidate_that_barely_beats_the_baseline_is_not_eligible():
    selection = select_candidate(
        0.50, {"catboost": {"logloss": 0.4995, "brier": 0.12}},
        metric="logloss", tiebreak="brier", min_gain=0.002)
    assert selection.name == BASELINE_NAME
    assert "below" in selection.reason


def test_an_exact_tie_is_broken_by_name_not_by_dictionary_order():
    candidates = {"zeta": {"logloss": 0.40, "brier": 0.12},
                  "alpha": {"logloss": 0.40, "brier": 0.12}}
    forward = select_candidate(0.50, candidates, min_gain=0.0)
    backward = select_candidate(0.50, dict(reversed(list(candidates.items()))),
                                min_gain=0.0)
    assert forward.name == backward.name == "alpha"


def test_every_permutation_of_the_candidates_gives_the_same_answer():
    candidates = {"a": {"logloss": 0.41, "brier": 0.120},
                  "b": {"logloss": 0.41, "brier": 0.121},
                  "c": {"logloss": 0.39, "brier": 0.130}}
    answers = set()
    for order in itertools.permutations(candidates):
        selection = select_candidate(
            0.50, {name: candidates[name] for name in order}, min_gain=0.0)
        answers.add(selection.name)
    assert answers == {"c"}


def test_a_tie_on_the_primary_metric_is_broken_by_the_secondary():
    selection = select_candidate(
        0.50,
        {"a": {"logloss": 0.40, "brier": 0.130},
         "b": {"logloss": 0.40, "brier": 0.120}},
        metric="logloss", tiebreak="brier", min_gain=0.0)
    assert selection.name == "b"


def test_a_candidate_with_no_finite_score_is_skipped():
    selection = select_candidate(
        0.50,
        {"broken": {"logloss": float("nan"), "brier": 0.1},
         "good": {"logloss": 0.45, "brier": 0.12}},
        min_gain=0.0)
    assert selection.name == "good"


def test_the_baseline_wins_when_no_candidate_is_better():
    selection = select_candidate(
        0.30, {"a": {"logloss": 0.40, "brier": 0.12}}, min_gain=0.0)
    assert selection.name == BASELINE_NAME
    assert selection.chose_baseline is True


def test_the_selection_records_what_it_compared():
    selection = select_candidate(
        0.50, {"a": {"logloss": 0.40, "brier": 0.12}}, min_gain=0.0)
    assert isinstance(selection, Selection)
    assert selection.metric == "logloss"
    assert selection.baseline_value == 0.50
    assert selection.value == 0.40
