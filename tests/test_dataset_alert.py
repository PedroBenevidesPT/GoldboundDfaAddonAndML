"""The new-dataset alert, and the sidecar that makes it a change.

The pipeline exports the dataset every six hours.  An alert per export
would be a heartbeat, and a heartbeat is the thing a player mutes, so the
alert fires when the *bytes* differ from the last export and stays quiet
when they do not.

The other half of that rule is honesty about ignorance: on the first run
there is no previous export, and "nothing changed" and "we did not look"
must not print the same way.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from woweconomy.intel import alerts
from woweconomy.pricing import dataset as ds


META = {
    "version": 2,
    "generatedAt": "2026-10-21T09:00:00+00:00",
    "itemCount": 237,
    "priceCount": 31_184,
    "indexedRealms": 267,
    "predictionStatusCounts": {"baseline_only": 237},
}


def meta_with(**overrides) -> dict:
    out = dict(META)
    out.update(overrides)
    return out


# ---------------------------------------------------------------------------
# the alert itself
# ---------------------------------------------------------------------------
def test_the_alert_names_the_counts_and_the_checksum():
    alert = alerts.dataset_alert(meta_with(checksum="a" * 64), None, "x.lua")

    body = "\n".join(alert.lines)
    assert alert.title.startswith("DFA:")
    assert "237 items" in body
    assert "31,184 prices" in body
    assert "267 realms" in body
    assert "a" * 16 in body, "the checksum identifies which dataset this was"
    assert "x.lua" in body


def test_the_alert_key_is_the_dataset_not_the_run():
    """Two exports of the same dataset are one alert, not two."""
    first = alerts.dataset_alert(meta_with(checksum="b" * 64), None)
    second = alerts.dataset_alert(meta_with(checksum="b" * 64), None)
    other = alerts.dataset_alert(meta_with(checksum="c" * 64), None)

    assert first.key == second.key
    assert first.key != other.key


def test_the_alert_reports_what_changed_against_the_previous_export():
    previous = {"checksum": "d" * 64, "itemCount": 200,
                "priceCount": 31_000, "indexedRealms": 267}
    alert = alerts.dataset_alert(meta_with(checksum="e" * 64), previous)

    body = "\n".join(alert.lines)
    assert "+37 items" in body
    assert "+184 prices" in body
    assert "0 realms" in body


def test_a_first_export_says_the_change_is_unknown_not_zero():
    """'We did not look' must not print as 'nothing changed'."""
    alert = alerts.dataset_alert(meta_with(checksum="f" * 64), None)

    body = "\n".join(alert.lines)
    assert "unknown" in body
    assert "change: 0" not in body


def test_a_previous_export_missing_a_count_does_not_become_zero():
    """A partial sidecar is not evidence that a count did not change."""
    previous = {"checksum": "1" * 64}
    alert = alerts.dataset_alert(meta_with(checksum="2" * 64), previous)

    body = "\n".join(alert.lines)
    assert "+237 items" not in body
    assert "? items" in body


def test_the_alert_carries_the_prediction_status_breakdown():
    alert = alerts.dataset_alert(meta_with(checksum="3" * 64), None)
    assert "baseline_only 237" in "\n".join(alert.lines)


# ---------------------------------------------------------------------------
# when to stay quiet
# ---------------------------------------------------------------------------
def test_the_same_bytes_are_not_announced_again():
    meta = meta_with(checksum="4" * 64)
    assert alerts.dataset_unchanged(meta, {"checksum": "4" * 64}) is True


def test_the_comparison_uses_the_content_not_the_file():
    """A new timestamp is a new file but not a new dataset.

    The pipeline exports every six hours; if the build time decided
    "changed", the alert would fire on every run with nothing to say.
    """
    before = {"checksum": "a" * 64, "contentSignature": "s" * 64}
    after = meta_with(checksum="b" * 64, contentSignature="s" * 64)

    assert alerts.dataset_unchanged(after, before) is True


def test_the_comparison_notices_a_content_change():
    before = {"checksum": "a" * 64, "contentSignature": "s" * 64}
    after = meta_with(checksum="b" * 64, contentSignature="t" * 64)

    assert alerts.dataset_unchanged(after, before) is False


def test_the_alert_key_follows_the_content():
    """Two exports of one dataset are one alert, whatever their timestamps."""
    first = alerts.dataset_alert(
        meta_with(checksum="a" * 64, contentSignature="s" * 64), None)
    second = alerts.dataset_alert(
        meta_with(checksum="b" * 64, contentSignature="s" * 64), None)

    assert first.key == second.key


def test_an_old_sidecar_without_a_signature_still_works():
    """A sidecar written before the signature existed must not silence it."""
    assert alerts.dataset_unchanged(meta_with(checksum="4" * 64),
                                    {"checksum": "4" * 64}) is True
    assert alerts.dataset_unchanged(meta_with(checksum="5" * 64),
                                    {"checksum": "4" * 64}) is False


def test_different_bytes_are_announced():
    assert alerts.dataset_unchanged(
        meta_with(checksum="5" * 64), {"checksum": "6" * 64}) is False


def test_a_missing_previous_checksum_is_announced_rather_than_assumed():
    """Silence is the failure that costs something."""
    assert alerts.dataset_unchanged(meta_with(checksum="7" * 64), {}) is False
    assert alerts.dataset_unchanged(meta_with(checksum=""), {"checksum": ""}) is False


# ---------------------------------------------------------------------------
# the sidecar
# ---------------------------------------------------------------------------
def test_the_sidecar_round_trips_beside_the_dataset(tmp_path: Path):
    path = tmp_path / "DFA_Estimate_Data.lua"
    path.write_text("-- the dataset\n", encoding="utf-8")

    side = ds.write_meta_sidecar(path, META, "8" * 64)

    assert side.name == "DFA_Estimate_Data.meta.json"
    assert side.parent == path.parent
    stored = ds.read_meta_sidecar(path)
    assert stored["checksum"] == "8" * 64
    assert stored["itemCount"] == 237


def test_a_missing_sidecar_reads_as_unknown_not_as_empty(tmp_path: Path):
    assert ds.read_meta_sidecar(tmp_path / "nothing.lua") == {}


def test_a_corrupt_sidecar_does_not_stop_the_export(tmp_path: Path):
    path = tmp_path / "DFA_Estimate_Data.lua"
    path.write_text("-- the dataset\n", encoding="utf-8")
    ds.meta_sidecar_path(path).write_text("{not json", encoding="utf-8")

    assert ds.read_meta_sidecar(path) == {}


def test_a_sidecar_holding_a_list_is_not_treated_as_a_mapping(tmp_path: Path):
    path = tmp_path / "DFA_Estimate_Data.lua"
    path.write_text("-- the dataset\n", encoding="utf-8")
    ds.meta_sidecar_path(path).write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert ds.read_meta_sidecar(path) == {}


def test_the_sidecar_write_is_atomic(tmp_path: Path):
    """A reader must never see half a sidecar, same as half a dataset."""
    path = tmp_path / "DFA_Estimate_Data.lua"
    path.write_text("-- the dataset\n", encoding="utf-8")

    ds.write_meta_sidecar(path, META, "9" * 64)
    ds.write_meta_sidecar(path, meta_with(itemCount=1), "a" * 64)

    assert ds.read_meta_sidecar(path)["itemCount"] == 1
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"a temporary file was left behind: {leftovers}"
