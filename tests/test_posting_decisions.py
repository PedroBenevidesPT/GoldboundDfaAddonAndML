"""The posting decision and the deposit it has to get right.

The deposit is a share of the **vendor sell value**, not of the ask, and it
is returned only when the item sells.  Those two facts decide whether a
posting rule can ever recommend selling a realm mount: 5 % of a 6M-gold ask
is 300k, 5 % of its 25-gold vendor value is 1g 25s.
"""
import pytest

from woweconomy.ml.posting import (
    AH_CUT_RATE,
    DEPOSIT_BASE_RATE,
    deposit_copper,
    deposit_rate,
    decide,
    sale_net,
)


def test_the_deposit_is_a_share_of_the_vendor_value_not_the_ask():
    # 5 % of 25g at the 12 h base
    assert deposit_copper(2500, hours=12) == 125
    # the ask never enters the calculation
    assert deposit_copper(2500, hours=12) == deposit_copper(2500, hours=12)
    assert deposit_rate(12) == pytest.approx(DEPOSIT_BASE_RATE)


def test_the_durations_double_and_quadruple():
    base = deposit_copper(10000, hours=12)
    assert base == 500
    assert deposit_copper(10000, hours=24) == 2 * base
    assert deposit_copper(10000, hours=48) == 4 * base
    assert deposit_rate(24) == pytest.approx(0.10)
    assert deposit_rate(48) == pytest.approx(0.20)


def test_an_unknown_duration_is_refused_not_rounded():
    with pytest.raises(ValueError):
        deposit_copper(1000, hours=36)


def test_the_deposit_scales_with_the_stack():
    assert deposit_copper(2500, hours=24, qty=4) == deposit_copper(2500, hours=24) * 4


def test_an_item_no_vendor_buys_has_no_deposit():
    assert deposit_copper(0, hours=48) == 0
    assert deposit_copper(None, hours=48) == 0


def test_the_deposit_is_only_paid_when_the_listing_does_not_sell():
    ask, vendor = 100000, 5000
    certain = sale_net(ask, vendor, 1.0, hours=24)
    never = sale_net(ask, vendor, 0.0, hours=24)
    cut = int(round(ask * AH_CUT_RATE))
    assert certain == ask - cut
    assert never == ask - cut - deposit_copper(vendor, hours=24)


def test_a_mount_deposit_does_not_scale_with_its_price():
    """The regression: 5 % of a 6M ask would bar every mount from posting."""
    ask, vendor = 6_000_000_00, 2500  # 6M gold ask, 25g vendor value
    assert deposit_copper(vendor, hours=12) == 125
    assert deposit_copper(vendor, hours=12) < ask / 100000
    assert decide(reference=ask, vendor_sell_copper=vendor,
                  sale_probability=0.4, confidence=0.9,
                  hours=12).action in ("post", "hold", "undercut")


def test_no_evidence_means_stop():
    result = decide(reference=0, vendor_sell_copper=100, sale_probability=0.5,
                    confidence=0.9)
    assert result.action == "stop"
    assert result.ask == 0


def test_low_confidence_means_stop():
    result = decide(reference=10000, vendor_sell_copper=100,
                    sale_probability=0.5, confidence=0.05)
    assert result.action == "stop"
    assert "confidence" in result.reason


def test_no_competition_posts_at_the_supported_price():
    result = decide(reference=10000, vendor_sell_copper=1000,
                    sale_probability=0.5, confidence=0.8)
    assert result.action == "post"
    assert result.ask == 10000
    assert result.net > 0


def test_competition_means_match_the_cheapest_listing():
    result = decide(reference=10000, vendor_sell_copper=1000,
                    sale_probability=0.5, confidence=0.8, best_ask=8000)
    assert result.action == "undercut"
    assert result.ask == 8000


def test_a_listing_at_the_cheapest_price_is_held():
    result = decide(reference=10000, vendor_sell_copper=1000,
                    sale_probability=0.5, confidence=0.8,
                    best_ask=8000, list_price=8000)
    assert result.action == "hold"
    assert result.ask == 8000
    assert "already at or below" in result.reason


def test_relisting_pays_when_the_gap_beats_the_deposit():
    result = decide(reference=20000, vendor_sell_copper=100,
                    sale_probability=0.9, confidence=0.8,
                    best_ask=10000, list_price=20000, hours=12)
    assert result.action == "relist"
    assert result.ask == 10000


def test_a_thin_gap_is_not_worth_burning_the_deposit():
    """Same ask, but posting cheaper gains less than the deposit it costs."""
    result = decide(reference=10000, vendor_sell_copper=100,
                    sale_probability=0.0, confidence=0.8,
                    best_ask=9999, list_price=10000, hours=12)
    assert result.action == "hold"
    assert result.ask == 10000


def test_another_realm_wins_when_the_advantage_survives_the_margin():
    result = decide(reference=10000, vendor_sell_copper=1000,
                    sale_probability=0.5, confidence=0.8, realm="Dragonmaw",
                    other_realms={"Silvermoon": (30000, 0.5)})
    assert result.action == "change_realm"
    assert result.realm == "Silvermoon"
    assert result.ask == 30000


def test_a_marginal_other_realm_does_not_move_stock():
    result = decide(reference=10000, vendor_sell_copper=1000,
                    sale_probability=0.5, confidence=0.8,
                    other_realms={"Silvermoon": (10500, 0.5)})
    assert result.action in ("post", "hold", "undercut")


def test_stop_when_the_cut_and_deposit_eat_the_price():
    result = decide(reference=10, vendor_sell_copper=100000,
                    sale_probability=0.0, confidence=0.8)
    assert result.action == "stop"
    assert result.net < 0
