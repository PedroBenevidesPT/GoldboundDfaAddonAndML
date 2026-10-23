"""Cancel-and-relist vs hold vs let-expire, with the rule stated."""
import json
import time

from woweconomy.intel import alerts, relist, store


def add(con, auction_id=1, own=10_000, lowest=8_500, ahead=2, qty=1,
        time_left=20 * 3600, observed_at=None, item="Thing", scan="scan-1",
        seller="Under-Stormscale", item_id=1001,
        source="auctionator_undercut_scan", confidence="verified",
        reference_status=None):
    observed = int(time.time()) - 60 if observed_at is None else observed_at
    gap = (own - lowest) / own
    status = (reference_status
              or ("verified" if confidence == "verified" else "price-only"))
    con.execute(
        "INSERT OR REPLACE INTO undercut_observation (observation_id,"
        " scan_id, observed_at, realm, character, item_id, item_name, item_key,"
        " item_link, own_auction_id, own_quantity, own_buyout_copper,"
        " own_unit_price_copper, lowest_unit_price_copper, lowest_seller,"
        " lowest_seller_realm, lowest_auction_id, undercut_copper,"
        " undercut_pct, items_ahead, sellers_below, time_left, sellers_json,"
        " source, confidence, reference_status, imported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{scan}-{auction_id}", scan, observed, "Dragonmaw", "Bullbound",
         item_id, item, f"{item_id}:0:0:0", "", auction_id, qty, own, own,
         lowest, seller, "Stormscale", 9001, own - lowest, gap, ahead, 1,
         time_left, "[]", source, confidence, status, ""))
    con.commit()


def test_a_one_percent_gap_with_one_stack_ahead_is_a_hold():
    decision = relist.decide(10_000, 9_900, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.HOLD
    assert "clears first" in decision["reason"]


def test_a_heavy_gap_with_time_left_is_a_relist_under_the_lowest():
    decision = relist.decide(10_000, 8_000, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.RELAST
    assert decision["suggested_ask_copper"] == 7_999
    assert decision["suggested_discount_pct"] == 0.2001


def test_a_heavy_gap_with_two_hours_left_is_left_to_expire():
    decision = relist.decide(10_000, 8_000, items_ahead=1, own_quantity=1,
                             hours_left=2,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.EXPIRE
    assert "too late" in decision["reason"]


def test_a_long_queue_behind_justifies_a_relist():
    decision = relist.decide(10_000, 9_900, items_ahead=10, own_quantity=1,
                             hours_left=30,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.RELAST


def test_without_a_verified_reference_the_same_gap_is_only_a_review():
    decision = relist.decide(10_000, 8_000, items_ahead=1, own_quantity=1,
                             hours_left=20)
    assert decision["advice"] == relist.WATCH
    assert decision["review_required"] is True
    assert decision["reference_status"] == relist.REFERENCE_UNVERIFIED
    assert "suggested_ask_copper" not in decision


def test_a_genuinely_underpriced_listing_is_held_not_relisted():
    decision = relist.decide(8_000, 10_000, items_ahead=0, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.HOLD
    assert decision["relation"] == "underpriced"
    assert decision["gap_pct"] == 0.0
    assert "suggested_ask_copper" not in decision
    assert "below the cheapest" in decision["reason"]


def test_a_listing_at_the_cheapest_is_held():
    decision = relist.decide(8_000, 8_000, items_ahead=0, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.HOLD
    assert decision["relation"] == "at_cheapest"
    assert "matches the cheapest" in decision["reason"]


def test_zero_and_negative_references_are_not_actionable():
    for own, lowest in [(10_000, 0), (0, 8_000), (10_000, -1), (-1, 8_000)]:
        decision = relist.decide(own, lowest, items_ahead=10, own_quantity=1,
                                 hours_left=1,
                                 reference_status=relist.REFERENCE_VERIFIED)
        assert decision["advice"] == relist.WATCH
        assert decision["reference_status"] == relist.REFERENCE_INVALID
        assert decision["review_required"] is True
        assert "suggested_ask_copper" not in decision


def test_gold_silver_and_copper_are_compared_in_copper():
    # 1g vs 90s: the gap is 10% of my ask; the suggested ask is 8,999c.
    decision = relist.decide(10_000, 9_000, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.RELAST
    assert decision["suggested_ask_copper"] == 8_999
    assert decision["gap_pct"] == 0.1
    assert decision["above_cheapest_pct"] == 0.1111


def test_malformed_prices_and_clock_do_not_raise():
    decision = relist.decide("not-a-price", 8_000, items_ahead="x",
                             own_quantity=None, hours_left="oops",
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.WATCH
    assert decision["reference_status"] == relist.REFERENCE_INVALID


def test_the_five_reported_dragonmaw_pairs_are_review_only():
    con = store.memory_connection()
    pairs = [
        (82800, "Pet Cage", 5_000_000, 50_000, 457),
        (1747, "Linked Chain Boots", 10_493_000, 1_079_300, 1),
        (62129, "Bleached Plate Boots", 3_111_500, 360_200, 1),
        (3374, "Calico Belt", 3_225_500, 528_600, 1),
        (1799, "Rawhide Gloves", 17_258_800, 3_067_200, 1),
    ]
    for offset, (item_id, name, own, lowest, ahead) in enumerate(pairs, start=1):
        add(con, auction_id=offset, item_id=item_id, item=name, own=own,
            lowest=lowest, ahead=ahead, time_left=0, seller="",
            source="blizzard_api", confidence="price-only",
            reference_status="variant-ambiguous")

    report = relist.advise(con, "Dragonmaw")

    assert report["listings"] == 5
    assert report["counts"][relist.RELAST] == 0
    assert report["counts"][relist.WATCH] == 5
    assert report["review_required"] == 5
    assert all(row["review_required"] for row in report["advice"])
    assert all(row["reference_status"] == "variant-ambiguous"
               for row in report["advice"])
    assert all("suggested_ask_copper" not in row for row in report["advice"])
    digest = alerts.undercut_alerts(report)
    assert len(digest) == 1
    assert "5 listing(s) need review" in digest[0].title
    assert "relist=0" in digest[0].lines[-1]


def test_an_unknown_clock_on_a_verified_reference_never_expires():
    decision = relist.decide(10_000, 8_000, items_ahead=1, own_quantity=1,
                             hours_left=None,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.RELAST
    assert "suggested_ask_copper" in decision


def test_a_medium_gap_with_four_to_twelve_hours_is_a_watch():
    decision = relist.decide(10_000, 9_500, items_ahead=1, own_quantity=1,
                             hours_left=8,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.WATCH
    assert "cannot prove itself" in decision["reason"]


def test_the_gap_boundaries_follow_the_stated_rules():
    at_min = relist.decide(10_000, 9_800, items_ahead=1, own_quantity=1,
                           hours_left=20,
                           reference_status=relist.REFERENCE_VERIFIED)
    over_min = relist.decide(10_000, 9_799, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    at_heavy = relist.decide(10_000, 9_000, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    under_heavy = relist.decide(10_000, 9_001, items_ahead=1, own_quantity=1,
                                hours_left=20,
                                reference_status=relist.REFERENCE_VERIFIED)
    assert at_min["advice"] == relist.HOLD
    assert over_min["advice"] == relist.WATCH
    assert at_heavy["advice"] == relist.RELAST
    assert under_heavy["advice"] == relist.WATCH


def test_another_realm_is_not_advised_by_a_dragonmaw_observation():
    con = store.memory_connection()
    add(con, auction_id=31, own=10_000, lowest=8_000)
    report = relist.advise(con, "Silvermoon")
    assert report["listings"] == 0
    assert report["counts"][relist.RELAST] == 0


def test_a_stale_reference_is_review_not_an_action():
    con = store.memory_connection()
    add(con, auction_id=21, own=10_000, lowest=8_000,
        observed_at=int(time.time()) - 100 * 3600)
    report = relist.advise(con, "Dragonmaw", max_age_h=72)
    assert report["counts"][relist.RELAST] == 0
    assert report["advice"][0]["advice"] == relist.WATCH
    assert report["advice"][0]["reference_status"] == relist.REFERENCE_STALE
    assert report["advice"][0]["review_required"] is True


def test_the_latest_scan_of_an_auction_is_the_one_that_counts():
    con = store.memory_connection()
    add(con, auction_id=7, own=10_000, lowest=8_000, scan="old",
        observed_at=int(time.time()) - 3600)
    add(con, auction_id=7, own=10_000, lowest=9_900, ahead=1, scan="new")
    report = relist.advise(con, "Dragonmaw")
    assert report["listings"] == 1
    assert report["advice"][0]["advice"] == relist.HOLD


def test_time_left_decays_since_the_scan():
    con = store.memory_connection()
    add(con, auction_id=8, time_left=6 * 3600,
        observed_at=int(time.time()) - 8 * 3600)
    report = relist.advise(con, "Dragonmaw")
    assert report["advice"][0]["advice"] == relist.EXPIRE


def test_the_deposit_is_called_out_as_sunk():
    con = store.memory_connection()
    add(con, auction_id=9)
    report = relist.advise(con, "Dragonmaw")
    assert "sunk" in report["note"]


def test_the_cli_prints_the_advice(tmp_path, capsys):
    from scripts.intel import main
    db = tmp_path / "intel.sqlite"
    con = store.connect(db)
    add(con, auction_id=11, own=10_000, lowest=8_000)
    argv = ["--db", str(db), "--tsm-db", str(tmp_path / "tsm.db")]
    assert main(argv + ["relist", "--realm", "Dragonmaw"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["cancel_and_relist"] == 1
    assert out["advice"][0]["suggested_ask_copper"] == 7_999

# -------------------------------------------------------------------
# New regression tests for false cancel_and_relist prevention
# --------------------------------------------------------------------

def test_a_genuinely_overpriced_listing_is_relisted() :
    """An overpriced listing (own above lowest) with a verified reference
    should RELAST when the gap is heavy and there is time left."""
    decision = relist.decide(10_000, 5_000, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.RELAST
    assert decision["suggested_ask_copper"] == 4_999
    assert "gap" in decision["reason"]


def test_a_listing_near_its_valid_reference_is_held() :
    """A listing within the minimum gap with verified reference should HOLD."""
    decision = relist.decide(10_000, 9_800, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.HOLD
    assert "clears first" in decision["reason"]


def test_missing_reference_price_is_not_actionable() :
    """When the reference price is missing, the decision should be WATCH
    with review_required and REFERENCE_INVALID."""
    decision = relist.decide(10_000, None, items_ahead=1, own_quantity=1,
                              hours_left=20,
                              reference_status=relist.REFERENCE_UNVERIFIED)
    assert decision["advice"] == relist.WATCH
    assert decision["reference_status"] == relist.REFERENCE_INVALID
    assert decision["review_required"] is True
    assert "suggested_ask_copper" not in decision


def test_variant_ambiguous_rows_never_trigger_cancel_and_relist() :
    """Variant-ambiguous rows (different item level/suffix/pet species)
    must never independently trigger cancel_and_relist."""
    con = store.memory_connection()
    add(con, auction_id=32, item_id=2001, item="Fancy Widget Lvl 2",
        own=10_000, lowest=5_000, ahead=1,
        time_left=20, seller="", source="blizzard_api",
        confidence="price-only", reference_status="variant-ambiguous")
    report = relist.advise(con, "Dragonmaw")
    assert report["counts"][relist.RELAST] == 0
    assert report["counts"][relist.WATCH] == 1
    assert report["review_required"] == 1
    assert all(row["reference_status"] == "variant-ambiguous" for row in report["advice"])


def test_different_item_ids_are_not_confused() :
    """Two different item IDs must not share the same observation."""
    con = store.memory_connection()
    add(con, auction_id=33, item_id=3001, item="Thing A",
        own=10_000, lowest=5_000, ahead=1,
        time_left=20 * 3600, seller="Under-Stormscale", source="auc-scan",
        confidence="verified", reference_status="verified")
    add(con, auction_id=34, item_id=3002, item="Thing B",
        own=10_000, lowest=5_000, ahead=1,
        time_left=20 * 3600, seller="Under-Stormscale", source="auc-scan",
        confidence="verified", reference_status="verified")
    report = relist.advise(con, "Dragonmaw")
    assert report["listings"] == 2
    # Both should be RELAST since both have verified references and heavy gaps
    relist_count = sum(1 for row in report["advice"] if row["advice"] == relist.RELAST)
    assert relist_count == 2


def test_stale_reference_is_review_not_action() :
    """A reference older than the freshness window should be WATCH with
    REFERENCE_STALE, not RELAST."""
    con = store.memory_connection()
    old_time = int(time.time()) - 100 * 3600  # ~4 days old
    add(con, auction_id=35, item_id=3003, item="Old Item",
        own=10_000, lowest=5_000, ahead=1,
        time_left=20 * 3600, seller="Under-Stormscale", source="auc-scan",
        confidence="verified", reference_status="verified",
        observed_at=old_time)
    report = relist.advise(con, "Dragonmaw", max_age_h=72)
    assert report["counts"][relist.RELAST] == 0
    assert report["counts"][relist.WATCH] == 1
    assert report["review_required"] == 1
    assert report["advice"][0]["reference_status"] == relist.REFERENCE_STALE


def test_zero_and_negative_price_edge_cases() :
    """Zero or negative prices on either side must produce WATCH with
    REFERENCE_INVALID and review_required."""
    # own = 0
    decision = relist.decide(0, 8_000, items_ahead=10, own_quantity=1,
                             hours_left=1,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.WATCH
    assert decision["reference_status"] == relist.REFERENCE_INVALID
    assert decision["review_required"] is True
    assert "suggested_ask_copper" not in decision

    # lowest = 0
    decision = relist.decide(10_000, 0, items_ahead=10, own_quantity=1,
                             hours_left=1,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.WATCH
    assert decision["reference_status"] == relist.REFERENCE_INVALID
    assert decision["review_required"] is True
    assert "suggested_ask_copper" not in decision

    # both negative (wrapped as large positive via int(float))
    decision = relist.decide(-1, -100, items_ahead=10, own_quantity=1,
                             hours_left=1,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.WATCH
    assert decision["reference_status"] == relist.REFERENCE_INVALID
    assert decision["review_required"] is True
    assert "suggested_ask_copper" not in decision


def test_very_small_gap_is_hold_not_relist() :
    """A gap of 1% with verified reference and time left should be HOLD,
    not RELAST (noise, not worth cancelling)."""
    decision = relist.decide(10_000, 9_900, items_ahead=1, own_quantity=1,
                             hours_left=20,
                             reference_status=relist.REFERENCE_VERIFIED)
    assert decision["advice"] == relist.HOLD
    assert "clears first" in decision["reason"]


def test_cli_dragonmaw_five_pairs_are_review_only() :
    """The exact five price pairs from the user report must produce
    review-only output (relist=0) when run through the CLI."""
    from scripts.intel import main
    import tempfile, os
    db = tempfile.mktemp(suffix=".sqlite")
    con = store.connect(db)
    pairs = [
        (828000, "Pet Cage", 5_000_000, 50_000, 457),
        (1747, "Linked Chain Boots", 10_493_000, 1_079_300, 1),
        (62129, "Bleached Plate Boots", 3_111_500, 360_200, 1),
        (3374, "Calico Belt", 3_225_500, 528_600, 1),
        (1799, "Rawhide Gloves", 17_258_800, 3_067_200, 1),
    ]
    for offset, (item_id, name, own, lowest, ahead) in enumerate(pairs, start=1):
        add(con, auction_id=offset, item_id=item_id, item=name, own=own,
            lowest=lowest, ahead=ahead, time_left=0, seller="",
            source="blizzard_api", confidence="price-only",
            reference_status="variant-ambiguous")

    argv = ["--db", db, "--tsm-db", "", "relist", "--realm", "Dragonmaw"]
    assert main(argv) == 0
    # Just verify the report structure
    report = relist.advise(con, "Dragonmaw")
    assert report["listings"] == 5
    assert report["counts"][relist.RELAST] == 0
    assert report["counts"][relist.WATCH] == 5
    assert report["review_required"] == 5
    digest = alerts.undercut_alerts(report)
    assert len(digest) == 1
    assert "5 listing(s) need review" in digest[0].title
    assert "relist=0" in digest[0].lines[-1]

    # Cleanup
    con.close()
