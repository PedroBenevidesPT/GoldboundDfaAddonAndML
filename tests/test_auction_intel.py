"""Tests for the DFA Auction Intelligence ingestion and measurements.

The fixture is a SavedVariables file in the **exact** format the addon writes
(``DFA_AUCTION_INTELLIGENCE_EXPORT`` with a ``jsonl`` string), so the parser,
the importer and the measurements are verified before a single in-game event
exists -- and so a change in the addon's format breaks a test instead of
silently producing empty reports.
"""
import json
import sqlite3

import pytest

from woweconomy.analytics import auction_intel as ai

GOLD = ai.COPPER_PER_GOLD


def event(event_type, ts, item_id, **kwargs):
    payload = {
        "eventType": event_type,
        "timestamp": ts,
        "itemId": item_id,
        "itemName": kwargs.pop("itemName", f"Item {item_id}"),
        "quantity": kwargs.pop("quantity", 1),
        "unitPriceCopper": kwargs.pop("unitPriceCopper", 0),
        "totalPriceCopper": kwargs.pop("totalPriceCopper", 0),
        "depositCopper": kwargs.pop("depositCopper", 0),
        "consignmentFeeCopper": kwargs.pop("consignmentFeeCopper", 0),
        "netProceedsCopper": kwargs.pop("netProceedsCopper", 0),
        "marketRefCopper": kwargs.pop("marketRefCopper", 0),
        "realm": kwargs.pop("realm", "Dragonmaw"),
        "character": kwargs.pop("character", "Tester"),
        "counterparty": kwargs.pop("counterparty", ""),
        "source": kwargs.pop("source", "journalator_live"),
        "provenance": kwargs.pop("provenance", "client_event_inbox"),
        "confidence": kwargs.pop("confidence", "verified"),
    }
    payload.update(kwargs)
    payload["eventId"] = ai_event_id(payload)
    return payload


def ai_event_id(payload):
    return "%s:%s:%s:%s:%s:%s:%s" % (
        payload["eventType"], payload["itemId"], payload["timestamp"],
        payload["quantity"], payload["totalPriceCopper"], payload["realm"],
        payload["counterparty"])


def saved_variables(events, schema_version=1, addon_version="1.0.0"):
    """A SavedVariables file with the addon's export, escapes and all."""
    jsonl = "\n".join(json.dumps(e) for e in events)
    escaped = (jsonl.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'''DFA_AUCTION_INTELLIGENCE_EXPORT = {{
["schema_version"] = {schema_version},
["addon_version"] = "{addon_version}",
["exported_at"] = 1792600000,
["realm"] = "Dragonmaw",
["character"] = "Tester",
["total_events"] = {len(events)},
["jsonl"] = "{escaped}",
}}
DFA_AUCTION_INTELLIGENCE_DB = {{
["schema_version"] = 1,
["events"] = {{
}},
}}
'''


@pytest.fixture()
def sample_events():
    return [
        # a listing, a sale 6 hours later, and an expiry of another item
        event("listing", 1792500000, 1001, unitPriceCopper=50 * GOLD,
              totalPriceCopper=100 * GOLD, depositCopper=5 * GOLD,
              marketRefCopper=100 * GOLD, quantity=2),
        event("sale", 1792521600, 1001, unitPriceCopper=45 * GOLD,
              totalPriceCopper=90 * GOLD, depositCopper=5 * GOLD,
              consignmentFeeCopper=45_000, netProceedsCopper=905_000,
              marketRefCopper=100 * GOLD, quantity=2, counterparty="Buyer"),
        event("listing", 1792500000, 1002, unitPriceCopper=200 * GOLD,
              totalPriceCopper=200 * GOLD, depositCopper=10 * GOLD,
              marketRefCopper=250 * GOLD),
        event("expire", 1792586400, 1002, marketRefCopper=250 * GOLD),
        # a sale with no market reference: it must not enter the clearance
        event("sale", 1792590000, 1003, unitPriceCopper=20 * GOLD,
              totalPriceCopper=20 * GOLD, marketRefCopper=0),
    ]


@pytest.fixture()
def sample_file(tmp_path, sample_events):
    path = tmp_path / "DFA_AUCTION_INTELLIGENCE_EXPORT.lua"
    path.write_text(saved_variables(sample_events), encoding="utf-8")
    return path


# --- the parser ------------------------------------------------------------

def test_the_saved_variables_jsonl_is_parsed(sample_file, sample_events):
    events = ai.parse_saved_variables(sample_file)
    assert len(events) == len(sample_events)
    assert events[0]["eventType"] == "listing"
    assert events[0]["itemId"] == 1001
    assert events[1]["unitPriceCopper"] == 45 * GOLD


def test_the_lua_escapes_are_undone():
    text = '["jsonl"] = "{\\"a\\": 1}\\n{\\"b\\": \\"x\\"}"'
    extracted = ai.extract_lua_string(text, "jsonl")
    assert extracted == '{"a": 1}\n{"b": "x"}'


def test_a_missing_export_is_not_an_error(tmp_path):
    path = tmp_path / "nothing.lua"
    path.write_text("DFA_AUCTION_INTELLIGENCE_DB = {}\n", encoding="utf-8")
    assert ai.parse_saved_variables(path) == []


# --- the file the client actually writes -----------------------------------

def test_the_export_is_found_under_the_addon_folder_name(tmp_path, monkeypatch, sample_events):
    """WoW writes ``<AddOnFolder>.lua``, not ``<SavedVariable>.lua``.

    Searching only for ``DFA_AUCTION_INTELLIGENCE_EXPORT.lua`` reported "no
    export" while a 74 KB file sat in SavedVariables, so nothing was ever
    imported.
    """
    account = tmp_path / "Account" / "1186541918#1" / "SavedVariables"
    account.mkdir(parents=True)
    written = account / "DFA_AuctionIntelligence.lua"
    written.write_text(saved_variables(sample_events), encoding="utf-8")
    monkeypatch.setattr(ai, "WTF_GLOBS", (
        str(tmp_path / "Account" / "*" / "SavedVariables" / "{name}.lua"),))

    found = ai.find_saved_variables()
    assert found == written
    assert len(ai.parse_saved_variables(found)) == len(sample_events)


def test_an_export_without_events_loses_to_one_that_has_them(tmp_path, monkeypatch,
                                                             sample_events):
    account = tmp_path / "Account" / "X" / "SavedVariables"
    account.mkdir(parents=True)
    empty = account / "DFA_AuctionIntelligence.lua"
    empty.write_text(saved_variables([]), encoding="utf-8")
    full = account / "DFA_AuctionIntelligence.lua.bak"
    full.write_text(saved_variables(sample_events), encoding="utf-8")
    monkeypatch.setattr(ai, "WTF_GLOBS", (
        str(tmp_path / "Account" / "*" / "SavedVariables" / "{name}.lua"),
        str(tmp_path / "Account" / "*" / "SavedVariables" / "{name}.lua.bak")))

    assert ai.find_saved_variables() == full


# --- the item id the invoice does not carry ---------------------------------

def test_an_event_without_an_item_id_gets_it_from_the_name():
    events = [{"eventType": "sale", "itemName": "Deepsea Sagefish"},
              {"eventType": "sale", "itemName": "Unknown Thing"},
              {"eventType": "sale", "itemName": "Other", "itemId": 42}]
    filled = ai.fill_item_ids(events, {"Deepsea Sagefish": 202173, "Other": 1})
    assert filled == 1
    assert events[0]["itemId"] == 202173
    assert "itemId" not in events[1]
    assert events[2]["itemId"] == 42      # an id that exists is never overwritten


def test_the_name_index_comes_from_the_catalogue(tmp_path):
    db = tmp_path / "mini.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE items (itemId INTEGER, name TEXT);"
                      "INSERT INTO items VALUES (1001, 'Deepsea Sagefish');")
    con.commit()
    con.close()
    assert ai.build_name_index(db) == {"Deepsea Sagefish": 1001}


# --- the importer ----------------------------------------------------------

def test_the_event_log_has_its_own_table():
    """``auction_events`` belongs to the Journalator import, with another schema.

    Sharing the name made every addon import fail with "table auction_events
    has no column named quantity".
    """
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE auction_events (event_id TEXT PRIMARY KEY, ts TEXT);")
    con.commit()
    inserted, _ = ai.import_events(con, [{"eventType": "sale", "timestamp": 1,
                                          "itemId": 1}], "now")
    assert inserted == 1
    assert con.execute(f"select count(*) from {ai.TABLE}").fetchone()[0] == 1


def test_the_import_is_idempotent(tmp_path, sample_file, sample_events):
    con = sqlite3.connect(":memory:")
    events = ai.parse_saved_variables(sample_file)
    inserted, present = ai.import_events(con, events, "2026-10-20T00:00:00Z")
    assert inserted == len(sample_events)
    assert present == 0
    again, present = ai.import_events(con, events, "2026-10-20T00:00:00Z")
    assert again == 0
    assert present == len(sample_events)
    total = con.execute(f"select count(*) from {ai.TABLE}").fetchone()[0]
    assert total == len(sample_events)


def test_the_imported_rows_keep_the_fields(tmp_path, sample_file):
    con = sqlite3.connect(":memory:")
    ai.import_events(con, ai.parse_saved_variables(sample_file), "2026-10-20T00:00:00Z")
    row = con.execute(
        "select event_type, ts, item_id, unit_price_copper, deposit_copper, "
        "consignment_fee_copper, net_proceeds_copper, market_ref_copper, realm "
        f"from {ai.TABLE} where event_type='sale' and item_id=1001").fetchone()
    assert row[0] == "sale"
    assert row[1] == 1792521600
    assert row[2] == 1001
    assert row[3] == 45 * GOLD
    assert row[4] == 5 * GOLD
    assert row[5] == 45_000
    assert row[6] == 905_000
    assert row[7] == 100 * GOLD
    assert row[8] == "Dragonmaw"


# --- the coverage ----------------------------------------------------------

def test_the_coverage_counts_the_types_and_the_span(sample_events):
    cover = ai.coverage(sample_events)
    assert cover.total == len(sample_events)
    assert cover.by_type["listing"] == 2
    assert cover.by_type["sale"] == 2
    assert cover.by_type["expire"] == 1
    assert cover.span_days == pytest.approx((1792590000 - 1792500000) / 86400.0, rel=1e-6)
    assert cover.with_market_ref == 4      # the sale without a reference is excluded
    assert cover.with_fee == 1


# --- the measurements ------------------------------------------------------

def test_the_clearance_uses_the_reference_of_the_moment(sample_events):
    samples = ai.clearance_samples(sample_events)
    assert len(samples) == 1               # the sale without a reference is out
    assert samples[0].ratio == pytest.approx(0.45)
    assert samples[0].reference == 100 * GOLD


def test_the_clearance_is_grouped_by_band(sample_events):
    samples = ai.clearance_samples(sample_events, band_of=lambda item: "2k-5k")
    by_band = ai.clearance_by_band(samples)
    assert by_band["2k-5k"]["n"] == 1
    assert by_band["2k-5k"]["p50"] == pytest.approx(0.45)


def test_the_time_to_sale_pairs_a_listing_with_the_sale_that_closed_it(sample_events):
    intervals = ai.time_to_sale(sample_events)
    assert len(intervals) == 1
    assert intervals[0]["hours"] == pytest.approx(6.0)
    assert intervals[0]["asked_copper"] == 50 * GOLD
    assert intervals[0]["realised_copper"] == 45 * GOLD


def test_a_relist_does_not_create_a_second_shorter_interval(sample_events):
    events = list(sample_events)
    # the item is listed again, after the sale
    events.append(event("listing", 1792600000, 1001, unitPriceCopper=40 * GOLD,
                        totalPriceCopper=40 * GOLD, marketRefCopper=100 * GOLD))
    intervals = ai.time_to_sale(events)
    assert len(intervals) == 1
    assert intervals[0]["hours"] == pytest.approx(6.0)


def test_the_funnel_separates_sales_from_expiries(sample_events):
    funnel = ai.funnel(sample_events)
    assert funnel[(1001, "Dragonmaw")]["listings"] == 1
    assert funnel[(1001, "Dragonmaw")]["sales"] == 1
    assert funnel[(1002, "Dragonmaw")]["expires"] == 1
    assert funnel[(1002, "Dragonmaw")]["sell_through"] == pytest.approx(0.0)
    assert funnel[(1001, "Dragonmaw")]["sell_through"] == pytest.approx(1.0)


def test_the_costs_are_measured_not_assumed(sample_events):
    costs = ai.measured_costs(sample_events)
    # the sale: a 5 % cut (45 000 copper) on a 900 000 copper sale
    assert costs["fee_samples"] == 1
    assert costs["fee_median"] == pytest.approx(0.05)
    # the listings: two of them, both at 5 %
    assert costs["deposit_samples"] == 2
    assert costs["deposit_median"] == pytest.approx(0.05)


def test_a_missing_field_never_becomes_a_zero_measurement(sample_events):
    # the sale of item 1003 has no market reference: it is excluded, not zeroed
    samples = ai.clearance_samples(sample_events)
    assert all(sample.reference > 0 for sample in samples)
    cover = ai.coverage(sample_events)
    assert cover.with_market_ref == 4
