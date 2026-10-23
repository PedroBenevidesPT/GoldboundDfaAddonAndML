"""Tests for DFA Auction Intelligence services and analytics."""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from woweconomy.analytics.auction_intelligence import (
    calculate_confidence, link_listings_and_sales,
    load_item_auction_stats, load_overall_auction_summary,
)
from woweconomy.services.auction_intelligence import (
    export_events_jsonl, extract_item_id_from_link,
    generate_event_signature, ingest_journalator_archive, ingest_jsonl,
    init_auction_events_db, insert_normalized_event, normalize_event_dict,
    validate_event,
)


@pytest.fixture
def mem_db():
    con = sqlite3.connect(":memory:")
    init_auction_events_db(con)
    con.execute("CREATE TABLE items (itemId INTEGER PRIMARY KEY, name TEXT, createdAt TEXT)")
    con.execute("INSERT INTO items VALUES (12037, 'Mystery Meat', '2026-10-19')")
    con.execute("INSERT INTO items VALUES (2299, 'Burning War Axe', '2026-10-19')")
    con.commit()
    yield con
    con.close()


def test_extract_item_id_from_link():
    assert extract_item_id_from_link("|cnIQ1:|Hitem:12037::::::::80:103:::::::::|h[Mystery Meat]|h|r") == 12037
    assert extract_item_id_from_link("item:2299") == 2299
    assert extract_item_id_from_link("|Hbattlepet:82800:1:3:100:20:20|h") == 82800
    assert extract_item_id_from_link(None) is None
    assert extract_item_id_from_link("invalid string") is None


def test_validate_event_success():
    valid = {
        "eventType": "sale",
        "timestamp": 1792249652,
        "itemName": "Burning War Axe",
        "quantity": 1,
        "totalPriceCopper": 27130000,
    }
    ok, errors = validate_event(valid)
    assert ok is True
    assert len(errors) == 0


def test_validate_event_invalid_fields():
    # Bad type
    ok, errs = validate_event({"eventType": "unknown", "timestamp": 100, "itemName": "Item"})
    assert ok is False
    assert any("eventType" in e for e in errs)

    # Missing timestamp
    ok, errs = validate_event({"eventType": "sale", "itemName": "Item"})
    assert ok is False
    assert any("timestamp" in e for e in errs)

    # Zero/negative quantity
    ok, errs = validate_event({"eventType": "sale", "timestamp": 100, "itemName": "Item", "quantity": 0})
    assert ok is False
    assert any("Quantity" in e for e in errs)

    # Negative price
    ok, errs = validate_event({"eventType": "sale", "timestamp": 100, "itemName": "Item", "totalPriceCopper": -10})
    assert ok is False
    assert any("Total price" in e for e in errs)

    # Missing item name
    ok, errs = validate_event({"eventType": "sale", "timestamp": 100, "itemName": ""})
    assert ok is False
    assert any("itemName" in e for e in errs)


def test_normalize_event_net_proceeds_and_item_id():
    raw_sale = {
        "eventType": "sale",
        "timestamp": 1792249652,
        "itemLink": "|Hitem:2299|h",
        "itemName": "Burning War Axe",
        "quantity": 1,
        "totalPriceCopper": 100000,
        "depositCopper": 500,
        "consignmentFeeCopper": 5000,
        "realm": "Dragonmaw",
    }
    norm = normalize_event_dict(raw_sale)
    assert norm["item_id"] == 2299
    assert norm["event_type"] == "sale"
    assert norm["unit_price_copper"] == 100000
    assert norm["fee_copper"] == 5000
    assert norm["deposit_copper"] == 500
    # Net proceeds = 100000 - 5000 + 500 = 95500
    assert norm["net_proceeds_copper"] == 95500

    raw_purchase = {
        "eventType": "purchase",
        "timestamp": 1792249652,
        "itemName": "Mystery Meat",
        "quantity": 5,
        "totalPriceCopper": 25000,
        "realm": "Dragonmaw",
    }
    norm_purch = normalize_event_dict(raw_purchase)
    assert norm_purch["event_type"] == "purchase"
    assert norm_purch["unit_price_copper"] == 5000
    assert norm_purch["net_proceeds_copper"] == -25000


def test_insert_and_deduplication(mem_db):
    event = {
        "event_id": "test_ev_1",
        "schema_version": 1,
        "event_type": "sale",
        "ts": "2026-10-19T22:00:00Z",
        "timestamp_unix": 1792249652,
        "item_id": 2299,
        "item_name": "Burning War Axe",
        "item_link": None,
        "qty": 1,
        "unit_price_copper": 100000,
        "total_price_copper": 100000,
        "deposit_copper": 500,
        "fee_copper": 5000,
        "net_proceeds_copper": 95500,
        "realm": "Dragonmaw",
        "character": "Bullbound",
        "faction": "Horde",
        "counterparty": "Buyer1",
        "source": "test",
        "provenance": "unit_test",
        "confidence": "verified",
        "market_ref_copper": None,
        "raw_json": "{}",
    }

    ok, reason = insert_normalized_event(mem_db, event)
    assert ok is True
    assert reason == "inserted"

    # Insert identical event again (same ID)
    ok2, reason2 = insert_normalized_event(mem_db, event)
    assert ok2 is False
    assert reason2 == "duplicate"

    # Insert with different ID but identical semantic tuple
    event_alt = dict(event)
    event_alt["event_id"] = "different_id_same_data"
    ok3, reason3 = insert_normalized_event(mem_db, event_alt)
    assert ok3 is False
    assert reason3 == "duplicate"


def test_jsonl_ingestion_and_export(mem_db):
    sample_lines = [
        json.dumps({
            "eventType": "sale",
            "timestamp": 1792249000,
            "itemName": "Mystery Meat",
            "quantity": 10,
            "totalPriceCopper": 50000,
            "realm": "Dragonmaw"
        }),
        json.dumps({
            "eventType": "listing",
            "timestamp": 1792248000,
            "itemName": "Burning War Axe",
            "quantity": 1,
            "totalPriceCopper": 200000,
            "depositCopper": 1000,
            "realm": "Dragonmaw"
        }),
        # Corrupted line
        "{ not valid json",
        # Missing fields
        json.dumps({"eventType": "invalid_type", "itemName": "Bad"}),
    ]

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
        tf.write("\n".join(sample_lines) + "\n")
        temp_path = tf.name

    try:
        res = ingest_jsonl(mem_db, temp_path)
        assert res.total == 4
        assert res.inserted == 2
        assert res.invalid == 2
        assert res.duplicates == 0

        # Export test
        with tempfile.NamedTemporaryFile("r", encoding="utf-8", delete=False) as out_tf:
            out_path = out_tf.name

        count = export_events_jsonl(mem_db, out_path)
        assert count == 2

        with open(out_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) == 2
        assert lines[0]["item_name"] in ("Mystery Meat", "Burning War Axe")
    finally:
        Path(temp_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def test_analytics_metrics(mem_db):
    # Insert a sequence: 1 listing, 1 sale, 1 expire
    events = [
        {
            "event_id": "ev_list",
            "schema_version": 1,
            "event_type": "listing",
            "ts": "2026-10-19T10:00:00Z",
            "timestamp_unix": 1000000,
            "item_id": 2299,
            "item_name": "Burning War Axe",
            "item_link": None,
            "qty": 1,
            "unit_price_copper": 100000,
            "total_price_copper": 100000,
            "deposit_copper": 5000,
            "fee_copper": 0,
            "net_proceeds_copper": 0,
            "realm": "Dragonmaw",
            "character": "Bullbound",
            "faction": "Horde",
            "counterparty": "",
            "source": "test",
            "provenance": "test",
            "confidence": "verified",
            "market_ref_copper": 90000,
            "raw_json": "{}",
        },
        {
            "event_id": "ev_sale",
            "schema_version": 1,
            "event_type": "sale",
            "ts": "2026-10-19T14:00:00Z",
            "timestamp_unix": 1014400,  # 4 hours later
            "item_id": 2299,
            "item_name": "Burning War Axe",
            "item_link": None,
            "qty": 1,
            "unit_price_copper": 100000,
            "total_price_copper": 100000,
            "deposit_copper": 5000,
            "fee_copper": 5000,
            "net_proceeds_copper": 100000,
            "realm": "Dragonmaw",
            "character": "Bullbound",
            "faction": "Horde",
            "counterparty": "Buyer",
            "source": "test",
            "provenance": "test",
            "confidence": "verified",
            "market_ref_copper": 90000,
            "raw_json": "{}",
        },
        {
            "event_id": "ev_expire",
            "schema_version": 1,
            "event_type": "expire",
            "ts": "2026-10-19T20:00:00Z",
            "timestamp_unix": 1036000,
            "item_id": 2299,
            "item_name": "Burning War Axe",
            "item_link": None,
            "qty": 1,
            "unit_price_copper": 0,
            "total_price_copper": 0,
            "deposit_copper": 0,
            "fee_copper": 0,
            "net_proceeds_copper": 0,
            "realm": "Dragonmaw",
            "character": "Bullbound",
            "faction": "Horde",
            "counterparty": "",
            "source": "test",
            "provenance": "test",
            "confidence": "verified",
            "market_ref_copper": None,
            "raw_json": "{}",
        },
    ]

    for ev in events:
        insert_normalized_event(mem_db, ev)

    summary = load_overall_auction_summary(mem_db, realm="Dragonmaw")
    assert summary["counts"]["sale"] == 1
    assert summary["counts"]["listing"] == 1
    assert summary["counts"]["expire"] == 1
    assert summary["units_sold"] == 1
    assert summary["gross_sales_copper"] == 100000
    assert summary["total_fees_copper"] == 5000
    # Denominator: 1 sale + 1 expire = 2 => STR = 50%
    assert summary["sell_through_rate"] == pytest.approx(0.50)

    # Time to sale linking: listing was at 1000000, sale at 1014400 (14400 sec = 4.0 hours)
    durations = link_listings_and_sales(mem_db, realm="Dragonmaw")
    assert (2299, "Dragonmaw") in durations
    assert durations[(2299, "Dragonmaw")][0] == pytest.approx(4.0)

    stats = load_item_auction_stats(mem_db, realm="Dragonmaw")
    axe_stat = stats[(2299, "Dragonmaw")]
    assert axe_stat.units_sold == 1
    assert axe_stat.avg_realized_copper == 100000
    assert axe_stat.sell_through_rate == pytest.approx(0.50)
    assert axe_stat.avg_time_to_sale_hours == pytest.approx(4.0)


def test_confidence_tiers():
    assert calculate_confidence(35) == "alta"
    assert calculate_confidence(15) == "média"
    assert calculate_confidence(5) == "baixa"
    assert calculate_confidence(1) == "nenhuma"


def test_real_journalator_archive_integration():
    """Verify integration against the actual Journalator SavedVariables on this system."""
    real_path = Path(r"C:\Program Files (x86)\World of Warcraft\_retail_\WTF\Account\1186541918#1\SavedVariables\Journalator.lua")
    if not real_path.exists():
        pytest.skip("Journalator.lua not found in WoW WTF directory")

    con = sqlite3.connect(":memory:")
    init_auction_events_db(con)

    res = ingest_journalator_archive(con, real_path)
    assert res.total > 1000
    assert res.inserted > 1000
    assert res.invalid == 0

    # Verify events in DB
    row = con.execute("SELECT COUNT(*) FROM auction_events").fetchone()
    assert row[0] == res.inserted

    # Verify at least sales and postings exist
    sales_cnt = con.execute("SELECT COUNT(*) FROM auction_events WHERE event_type='sale'").fetchone()[0]
    listings_cnt = con.execute("SELECT COUNT(*) FROM auction_events WHERE event_type='listing'").fetchone()[0]
    assert sales_cnt > 0
    assert listings_cnt > 0

    con.close()
