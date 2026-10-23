"""Mail invoices fill a missing buyer without creating a second sale."""
import json
import sqlite3

import pytest

from woweconomy.intel import mail, store


def journal_db(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE auction_intel_events (event_id TEXT PRIMARY KEY,"
        " event_type TEXT, ts INTEGER, item_id INTEGER, item_name TEXT,"
        " quantity INTEGER, unit_price_copper INTEGER,"
        " total_price_copper INTEGER, realm TEXT, character TEXT,"
        " counterparty TEXT, source TEXT, confidence TEXT)")
    con.commit()
    con.close()
    return path


def add_sale(path, event_id, ts=1000, item_name="Thing", quantity=2,
             unit=10_000, total=20_000, realm="Dragonmaw",
             character="Bullbound", counterparty=""):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO auction_intel_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, "sale", ts, 1, item_name, quantity, unit, total, realm,
         character, counterparty, "journalator", "verified"))
    con.commit()
    con.close()


def invoice(buyer="Buyer-Silvermoon", item_name="Thing", quantity=2,
            value=20_000, seen_at=2000, estimated=1000, realm="Dragonmaw",
            character="Bullbound", invoice_type="seller", buyout=0):
    return mail.MailInvoice(
        mail_id=mail.make_mail_id(invoice_type, realm, character, item_name,
                                  buyer, value, quantity, estimated),
        seen_at=seen_at, estimated_sale_at=estimated, realm=realm,
        character=character, invoice_type=invoice_type, item_name=item_name,
        player_name=buyer, quantity=quantity, value_copper=value,
        buyout_copper=buyout,
        source="mail_invoice", confidence="verified")


def test_mail_jsonl_round_trip_and_idempotent_import():
    text = json.dumps({
        "seenAt": 2000, "estimatedSaleAt": 1000, "daysLeft": 29.5,
        "realm": "Dragonmaw", "character": "Bullbound",
        "invoiceType": "seller", "itemName": "Thing",
        "playerName": "Buyer-Silvermoon", "quantity": 2,
        "valueCopper": 20_000, "buyoutCopper": 20_000,
    })
    parsed = mail.parse_mail_jsonl(text)
    assert len(parsed) == 1 and parsed[0].player_name == "Buyer-Silvermoon"
    con = store.memory_connection()
    assert mail.import_invoices(con, parsed) == 1
    assert mail.import_invoices(con, parsed) == 0
    assert len(mail.load_invoices(con, "Dragonmaw")) == 1


def test_no_invoices_reports_the_unlinked_sales_as_unmatched(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    report = mail.link_buyers(store.memory_connection(), db, "Dragonmaw")
    assert report["mail_invoices"] == 0
    assert report["no_mail"] == 1
    assert report["linked"] == 0


def test_a_matching_mail_fills_the_buyer_without_adding_a_sale(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice()])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 1
    assert report["sales_without_buyer"] == 1
    assert report["already_had_buyer"] == 0
    assert report["links"][0]["buyer"] == "Buyer-Silvermoon"
    check = sqlite3.connect(db)
    rows = check.execute(
        "SELECT event_id, counterparty, quantity, total_price_copper"
        " FROM auction_intel_events").fetchall()
    assert rows == [("sale-1", "Buyer-Silvermoon", 2, 20_000)]
    assert len(check.execute("SELECT * FROM auction_intel_events").fetchall()) == 1
    check.close()
    assert con.execute("SELECT * FROM sale_buyer_link").fetchone() is not None


def test_an_existing_buyer_is_never_overwritten(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1", counterparty="Original-Buyer")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice()])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 0
    assert report["already_had_buyer"] == 1
    check = sqlite3.connect(db)
    assert check.execute(
        "SELECT counterparty FROM auction_intel_events").fetchone()[0] == "Original-Buyer"
    check.close()


def test_two_possible_buyers_leave_the_sale_alone(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice(), invoice(buyer="Other-Silvermoon")])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 0
    assert report["ambiguous_buyer"] == 1
    check = sqlite3.connect(db)
    assert check.execute(
        "SELECT counterparty FROM auction_intel_events").fetchone()[0] == ""
    check.close()


def test_a_mail_outside_the_window_is_not_used(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1", ts=1000)
    con = store.memory_connection()
    mail.import_invoices(con, [invoice(seen_at=1000 + 40 * 86400,
                                      estimated=0)])
    report = mail.link_buyers(con, db, "Dragonmaw", window_days=35)
    assert report["linked"] == 0
    assert report["no_mail"] == 1


def test_a_different_quantity_or_value_is_not_used(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice(quantity=3),
                               invoice(buyer="Other", value=19_999)])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 0
    assert report["no_mail"] == 1


def test_one_invoice_cannot_explain_two_sales(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    add_sale(db, "sale-2")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice()])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 0
    assert report["ambiguous_mail"] == 2
    check = sqlite3.connect(db)
    assert check.execute(
        "SELECT count(*) FROM auction_intel_events WHERE counterparty <> ''"
    ).fetchone()[0] == 0
    check.close()


def test_one_invoice_is_never_reused_for_a_second_sale(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice(estimated=1000)])
    assert mail.link_buyers(con, db, "Dragonmaw")["linked"] == 1
    # Same item, quantity and price: without the guard, the same mail
    # would explain this second sale too.
    add_sale(db, "sale-2", ts=1500)
    again = mail.link_buyers(con, db, "Dragonmaw")
    assert again["linked"] == 0
    assert again["mail_already_linked"] == 1
    check = sqlite3.connect(db)
    rows = dict(check.execute(
        "SELECT event_id, counterparty FROM auction_intel_events"))
    check.close()
    assert rows == {"sale-1": "Buyer-Silvermoon", "sale-2": ""}


def test_a_dry_run_never_writes_the_buyer(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice()])
    report = mail.link_buyers(con, db, "Dragonmaw", dry_run=True)
    assert report["linked"] == 1 and report["dry_run"] is True
    check = sqlite3.connect(db)
    assert check.execute(
        "SELECT counterparty FROM auction_intel_events").fetchone()[0] == ""
    check.close()


def test_a_buyout_amount_can_identify_the_sale(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice(value=0, buyout=20_000)])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 1
    assert report["links"][0]["buyer"] == "Buyer-Silvermoon"


def test_the_estimated_sale_time_can_link_a_late_scan(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1", ts=1000)
    con = store.memory_connection()
    mail.import_invoices(con, [invoice(seen_at=1000 + 40 * 86400,
                                      estimated=1000 + 3600)])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 1
    assert report["links"][0]["confidence"] == "high"


def test_the_same_mail_scanned_twice_is_one_invoice():
    first = invoice(seen_at=2000, estimated=1000)
    second = invoice(seen_at=2600, estimated=1000)
    assert first.mail_id == second.mail_id
    con = store.memory_connection()
    assert mail.import_invoices(con, [first, second]) == 1


def test_a_mail_seen_on_another_day_is_still_one_invoice():
    first = invoice(seen_at=2000, estimated=1000)
    second = invoice(seen_at=1000 + 86400, estimated=1000 + 86400)
    assert first.mail_id == second.mail_id


def test_a_pending_sale_invoice_is_the_same_mail_as_the_seller_one():
    pending = invoice(invoice_type="seller_temp_invoice")
    final = invoice(invoice_type="seller")
    assert pending.mail_id == final.mail_id
    con = store.memory_connection()
    assert mail.import_invoices(con, [pending, final]) == 1
    assert len(mail.load_invoices(con)) == 1


def test_a_buyout_only_match_is_medium_confidence(tmp_path):
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    con = store.memory_connection()
    mail.import_invoices(con, [invoice(value=0, buyout=20_000)])
    report = mail.link_buyers(con, db, "Dragonmaw")
    assert report["linked"] == 1
    assert report["linked_medium"] == 1 and report["linked_high"] == 0
    assert report["links"][0]["matched_on"] == "buyout"
    assert report["links"][0]["confidence"] == "medium"


def test_the_cli_says_so_when_the_addon_has_no_mail_block(tmp_path, capsys):
    from scripts.intel import main
    sv = tmp_path / "sv.lua"
    sv.write_text('DFA_AUCTION_INTELLIGENCE_DB = {\n}\n', encoding="utf-8")
    argv = ["--db", str(tmp_path / "intel.sqlite"),
            "--tsm-db", str(journal_db(tmp_path / "tsm.db"))]
    assert main(argv + ["mail-import", "--path", str(sv)]) == 2
    assert "no mail_jsonl block" in capsys.readouterr().err


def test_an_empty_inbox_block_is_not_a_missing_block(tmp_path):
    sv = tmp_path / "sv.lua"
    sv.write_text('DFA_AUCTION_INTELLIGENCE_DB = {\n["mail_jsonl"] = ""\n}\n',
                  encoding="utf-8")
    assert mail.parse_export_file(sv) == []
    empty = tmp_path / "other.lua"
    empty.write_text('DFA_AUCTION_INTELLIGENCE_DB = {\n}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        mail.parse_export_file(empty)


def test_the_cli_imports_and_links_mail(tmp_path, capsys):
    from scripts.intel import main
    db = journal_db(tmp_path / "tsm.db")
    add_sale(db, "sale-1")
    row = {
        "seenAt": 2000, "estimatedSaleAt": 1000, "daysLeft": 29.5,
        "realm": "Dragonmaw", "character": "Bullbound",
        "invoiceType": "seller", "itemName": "Thing",
        "playerName": "Buyer-Silvermoon", "quantity": 2,
        "valueCopper": 20_000,
    }
    lua = ("DFA_AUCTION_INTELLIGENCE_DB = {\n[" + json.dumps("mail_jsonl")
           + "] = " + json.dumps(json.dumps(row)) + "\n}\n")
    sv = tmp_path / "sv.lua"
    sv.write_text(lua, encoding="utf-8")
    intel_db = tmp_path / "intel.sqlite"
    argv = ["--db", str(intel_db), "--tsm-db", str(db)]
    assert main(argv + ["mail-import", "--path", str(sv)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["parsed"] == 1 and imported["inserted"] == 1
    assert main(argv + ["mail-link", "--realm", "Dragonmaw"]) == 0
    linked = json.loads(capsys.readouterr().out)
    assert linked["linked"] == 1
    check = sqlite3.connect(db)
    assert check.execute(
        "SELECT counterparty FROM auction_intel_events").fetchone()[0] == "Buyer-Silvermoon"
    check.close()
