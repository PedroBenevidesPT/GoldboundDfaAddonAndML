"""Tests for the TSM auctioning strings, transcribed with TSM's semantics.

What matters here is not that the parser accepts the text -- it is that the
numbers it produces are the ones TSM would produce:

* ``normalmarket`` is a clamp (local capped by the regional ask, floored at
  85 % of it), not a blend;
* the sale-rate ladder discounts in the documented steps and in the
  documented order;
* ``min``/``max`` skip a missing source, and everything else propagates it;
* ``ifgt`` with a missing comparison is *unknown*, never the else branch;
* an item above 50 000 g is priced off its expiry count instead.
"""
import pytest

from woweconomy.pricing import tsm_strings as ts

G = ts.COPPER_PER_GOLD


def levels(dbmarket=None, region=None, rate=None, vendor=None, expires=0.0):
    return ts.levels_for(dbmarket, region, rate, vendor, expires)


# --------------------------------------------------------------------------- #
# normalmarket: the clamp
# --------------------------------------------------------------------------- #

def test_local_above_the_wall_is_capped_at_125_percent_of_the_region():
    got = levels(200 * G, 100 * G, 0.02)
    assert got.normalmarket == pytest.approx(125 * G)


def test_local_below_the_floor_is_lifted_to_85_percent_of_the_region():
    got = levels(50 * G, 100 * G, 0.02)
    assert got.normalmarket == pytest.approx(85 * G)


def test_local_inside_the_band_is_used_as_it_is():
    got = levels(100 * G, 100 * G, 0.02)
    assert got.normalmarket == pytest.approx(100 * G)


def test_a_missing_local_ask_leaves_only_the_125_percent_cap():
    """Documented, and worth knowing: min() *drops* the unknown.

    With no dbmarket the cap has nothing to cap, so the wall becomes 125 %
    of the regional ask -- above the regional ask itself.  That is what TSM
    computes, and a string that wants the regional ask as the fallback has
    to say so (``min(first(dbmarket, 125% dbregionmarketavg),
    125% dbregionmarketavg)``).
    """
    got = levels(None, 100 * G, 0.02)
    assert got.normalmarket == pytest.approx(125 * G)


# --------------------------------------------------------------------------- #
# the sale-rate ladder
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rate,expected", [
    (0.020, 1.00),
    (0.012, 0.90),
    (0.006, 0.80),
    (0.001, 0.60),
])
def test_the_ladder_discounts_in_steps(rate, expected):
    got = levels(100 * G, 100 * G, rate)
    assert got.normal == pytest.approx(expected * 100 * G)


def test_the_ladder_is_an_upper_bound_not_a_lower_one():
    """A healthy sale rate never *lowers* the price below the wall."""
    fast = levels(100 * G, 100 * G, 0.02).normal
    slow = levels(100 * G, 100 * G, 0.001).normal
    assert fast > slow


# --------------------------------------------------------------------------- #
# minimum and maximum
# --------------------------------------------------------------------------- #

def test_minimum_is_sixty_percent_of_the_wall():
    got = levels(100 * G, 100 * G, 0.02)
    assert got.minimum == pytest.approx(60 * G)


def test_vendorsell_is_a_floor_and_not_a_summand():
    # 2 * vendor = 80 g is above 60 % of 100 g, so it wins
    got = levels(100 * G, 100 * G, 0.02, vendor=40 * G)
    assert got.minimum == pytest.approx(80 * G)


def test_maximum_is_one_and_a_half_times_the_wall():
    got = levels(100 * G, 100 * G, 0.02)
    assert got.maximum == pytest.approx(150 * G)


def test_the_band_contains_the_normal_level():
    got = levels(100 * G, 100 * G, 0.006)
    assert got.minimum <= got.normal <= got.maximum


# --------------------------------------------------------------------------- #
# the expensive branch: expiry count instead of sale rate
# --------------------------------------------------------------------------- #

def test_an_expensive_item_uses_the_expiry_count():
    fresh = levels(60_000 * G, 60_000 * G, 0.001, expires=0.0)
    stale = levels(60_000 * G, 60_000 * G, 0.001, expires=40.0)
    assert fresh.normal == pytest.approx(60_000 * G)        # max(70 %, 100 %)
    assert stale.normal == pytest.approx(0.70 * 60_000 * G)  # max(70 %, 60 %)


def test_the_expiry_branch_never_goes_below_seventy_percent():
    floor = levels(60_000 * G, 60_000 * G, 0.001, expires=90.0).normal
    assert floor == pytest.approx(0.70 * 60_000 * G)


def test_a_cheap_item_ignores_the_expiry_count():
    cheap = levels(100 * G, 100 * G, 0.02, expires=90.0)
    assert cheap.normal == pytest.approx(100 * G)


# --------------------------------------------------------------------------- #
# missing data is missing
# --------------------------------------------------------------------------- #

def test_no_references_means_no_levels():
    got = levels()
    assert got.normalmarket is None
    assert got.normal is None
    assert got.maximum is None


def test_ifgt_with_a_missing_comparison_is_not_the_else_branch():
    """The documented rule: an unknown comparison is unknown, not false."""
    ctx = ts.context(dbmarket=None, dbregionmarketavg=None, dbregionsalerate=0.02)
    assert ts.evaluate(ts.STRING_NORMAL, ctx) is None


def test_arithmetic_with_a_missing_operand_is_missing():
    assert ts.evaluate("1 + vendorsell", {"vendorsell": None}) is None
    assert ts.evaluate("100% vendorsell", {"vendorsell": None}) is None


# --------------------------------------------------------------------------- #
# the transcription itself
# --------------------------------------------------------------------------- #

def test_each_string_is_valid_tsm_and_evaluates():
    """The strings the player pastes into TSM are the ones evaluated here."""
    got = ts.levels_for(100 * G, 100 * G, 0.02, vendorsell=1 * G)
    assert got.normalmarket == pytest.approx(100 * G)
    assert got.minimum == pytest.approx(60 * G)
    assert got.normal == pytest.approx(100 * G)
    assert got.maximum == pytest.approx(150 * G)


def test_normalmarket_is_a_source_the_other_strings_can_name():
    ctx = ts.context(dbmarket=100 * G, dbregionmarketavg=100 * G,
                     dbregionsalerate=0.02)
    ctx["normalmarket"] = ts.evaluate(ts.STRING_NORMALMARKET, ctx)
    assert ts.evaluate(ts.STRING_NORMAL, ctx) == pytest.approx(100 * G)
    assert ts.evaluate(ts.STRING_MAXIMUM, ctx) == pytest.approx(150 * G)


def test_percent_before_a_name_is_a_fraction_of_it():
    assert ts.evaluate("125% dbmarket", {"dbmarket": 80.0}) == pytest.approx(100.0)


def test_gold_silver_and_copper_suffixes():
    assert ts.evaluate("1g", {}) == 10_000
    assert ts.evaluate("1s", {}) == 100
    assert ts.evaluate("1c", {}) == 1
