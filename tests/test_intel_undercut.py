"""Undercut observations: a seller name attached to a price, never a sale."""
import json
import time

import pytest

from woweconomy.intel import store, undercut


def row(scan_id="scan-1", auction_id=501, seller="Under-Stormscale",
        item="Thing", own_unit=10_000, lowest=7_000, pct=0.3,
        observed_at=None, sellers_below=2, items_ahead=3):
    observed = int(time.time()) - 60 if observed_at is None else observed_at
    return {
        "observedAt": observed,
        "scanId": scan_id,
        "realm": "Dragonmaw",
        "character": "Bullbound",
        "itemId": 1001,
        "itemName": item,
        "itemKey": "1001:0:0:0",
        "itemLink": "|Hitem:1001|h[Thing]|h|r",
        "ownAuctionId": auction_id,
        "ownQuantity": 1,
        "ownBuyoutCopper": own_unit,
        "ownUnitPriceCopper": own_unit,
        "lowestUnitPriceCopper": lowest,
        "lowestSeller": seller,
        "lowestSellerRealm": "Stormscale",
        "lowestAuctionId": 9001,
        "undercutCopper": own_unit - lowest,
        "undercutPct": pct,
        "itemsAhead": items_ahead,
        "sellersBelow": sellers_below,
        "timeLeft": 3600,
        "sellers": [
            {"seller": seller, "sellerRealm": "Stormscale",
             "unitPriceCopper": lowest, "buyoutCopper": lowest,
             "quantity": 2, "auctionId": 9001, "timeLeft": 3600},
            {"seller": "Rival-Stormscale", "sellerRealm": "Stormscale",
             "unitPriceCopper": 8_000, "buyoutCopper": 8_000,
             "quantity": 1, "auctionId": 9002, "timeLeft": 7200},
        ],
        "source": "auctionator_undercut_scan",
        "confidence": "verified",
    }


def test_a_row_keeps_the_sellers_and_their_prices():
    parsed = undercut.parse_observation(row())
    assert parsed["lowest_seller"] == "Under-Stormscale"
    assert parsed["undercut_copper"] == 3_000
    assert [s["seller"] for s in parsed["sellers"]] == [
        "Under-Stormscale", "Rival-Stormscale"]
    assert parsed["sellers"][1]["unit_price_copper"] == 8_000


def test_a_verified_row_is_verified_and_a_price_only_row_is_not():
    verified = undercut.parse_observation(row())
    price_only = undercut.parse_observation({
        **row(), "source": "blizzard_api", "confidence": "price-only"})
    assert verified["reference_status"] == "verified"
    assert price_only["reference_status"] == "price-only"


def test_a_row_without_a_seller_is_not_stored():
    bad = row()
    bad["lowestSeller"] = ""
    assert undercut.parse_observation(bad) is None


def test_the_same_scan_and_auction_is_one_observation():
    con = store.memory_connection()
    first = undercut.parse_observation(row())
    again = undercut.parse_observation(row())
    assert undercut.import_observations(con, [first, again]) == 1


def test_two_own_auctions_are_two_observations():
    con = store.memory_connection()
    rows = [undercut.parse_observation(row(auction_id=501)),
            undercut.parse_observation(row(auction_id=502, own_unit=7_500,
                                           lowest=7_000, pct=0.0667))]
    assert undercut.import_observations(con, rows) == 2
    assert len(undercut.load_observations(con, "Dragonmaw")) == 2


def test_the_summary_counts_each_seller_and_item():
    con = store.memory_connection()
    rows = [
        undercut.parse_observation(row(auction_id=501)),
        undercut.parse_observation(row(auction_id=502, item="Other",
                                       own_unit=5_000, lowest=4_000,
                                       pct=0.2)),
        undercut.parse_observation(row(scan_id="scan-2", auction_id=503,
                                       own_unit=9_000, lowest=6_000,
                                       pct=0.3333)),
    ]
    undercut.import_observations(con, rows)
    summary = undercut.summarise(con, "Dragonmaw")
    assert summary["scans"] == 2
    assert summary["observations"] == 3
    assert summary["sellers_total"] == 1
    seller = summary["sellers"][0]
    assert seller["observations"] == 3
    assert seller["items_affected"] == 2
    assert seller["avg_undercut_pct"] == pytest.approx(0.2778, abs=0.001)
    assert summary["items_total"] == 2
    assert summary["items"][0]["sellers"][0][0] == "Under-Stormscale"


def test_old_observations_are_outside_the_window():
    con = store.memory_connection()
    old = undercut.parse_observation(row(observed_at=int(time.time()) - 40 * 86400))
    undercut.import_observations(con, [old])
    assert undercut.summarise(con, "Dragonmaw", days=30)["observations"] == 0


def test_an_empty_block_is_valid_and_a_missing_one_is_not(tmp_path):
    sv = tmp_path / "sv.lua"
    sv.write_text('DFA_AUCTION_INTELLIGENCE_DB = {\n["undercuts_jsonl"] = ""\n}\n',
                  encoding="utf-8")
    assert undercut.parse_export_file(sv) == []
    empty = tmp_path / "other.lua"
    empty.write_text('DFA_AUCTION_INTELLIGENCE_DB = {\n}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        undercut.parse_export_file(empty)


def test_the_cli_imports_and_summarises(tmp_path, capsys):
    from scripts.intel import main
    line = row()
    lua = ('DFA_AUCTION_INTELLIGENCE_DB = {\n["undercuts_jsonl"] = '
           + json.dumps(json.dumps(line)) + "\n}\n")
    sv = tmp_path / "sv.lua"
    sv.write_text(lua, encoding="utf-8")
    argv = ["--db", str(tmp_path / "intel.sqlite"),
            "--tsm-db", str(tmp_path / "tsm.db")]
    assert main(argv + ["undercut-import", "--path", str(sv)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["parsed"] == 1 and imported["inserted"] == 1
    assert main(argv + ["undercut", "--realm", "Dragonmaw"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["observations"] == 1
    assert report["sellers"][0]["seller"] == "Under-Stormscale"
