"""Tests for the TSM export string codec and the mog groups/operations builder."""
from woweconomy.analytics.mog_operations import build_mog_export
from woweconomy.analytics.tsm_export import (
    build_group_export, deserialize, parse_group_export, serialize,
)


def test_serialize_matches_aceserializer_encoding():
    # space -> "~`", table framing, revision prefix and "^^" terminator
    assert serialize({"a": "b c"}) == "^1^T^Sa^Sb~`c^t^^"
    assert serialize({"n": 48, "f": 1.5}) == "^1^T^Sn^N48^Sf^N1.5^t^^"
    assert serialize(True) == "^1^B^^"
    assert serialize(False) == "^1^b^^"


def test_serialize_escapes_caret_and_tilde():
    assert deserialize(serialize("x^y~z")) == "x^y~z"
    assert deserialize(serialize({"a b": "c\nd"})) == {"a b": "c\nd"}


def test_round_trip_nested_table():
    value = {
        "operations": {"Auctioning": {"Op": {"postCap": 1, "cancel": False}}},
        "groupOperations": {"G": {"Auctioning": {1: "Op"},
                                  "ignoreItemVariations": True}},
        "groupExport": "group:G,i:1,i:2",
    }
    assert deserialize(serialize(value)) == value


def test_group_export_round_trip():
    groups = {"Liquid": [10, 20], "Very_Slow": [30]}
    payload = build_group_export(groups)
    assert payload == "group:Liquid,i:10,i:20,group:Very_Slow,i:30"
    assert parse_group_export(payload) == groups


def test_build_mog_export_creates_named_groups_and_operations():
    items = {
        "liquid": [1, 2, 3],
        "collector": [4],
        "slow": [5],
        "very_slow": [6],
        "unproven": [7],
        "unknown": [8],
        "conflict": [3, 4],
    }
    text = build_mog_export(items)
    data = deserialize(text)
    groups = parse_group_export(data["groupExport"])
    # conflict items move out of the primary groups (TSM: one group per item)
    assert groups["Liquid"] == [1, 2]
    assert "Collector" not in groups          # empty groups are not exported
    assert groups["Conflict"] == [3, 4]
    assert data["groupOperations"]["Liquid"]["Auctioning"][1] == "Mogs Liquid"
    assert data["groupOperations"]["Liquid"]["Auctioning"]["override"] is True
    liquid = data["operations"]["Auctioning"]["Mogs Liquid"]
    assert liquid["duration"] == 24 and liquid["undercut"] == "1c"
    assert liquid["postCap"] == 1
    # 30-day window at 24h posts -> 29 allowed expiries
    assert liquid["maxExpires"] == 29
    assert liquid["priceReset"] == "none"
    assert "dbregionmarketavg" in liquid["normalPrice"]
    refusal = data["operations"]["Auctioning"]["Mogs Unknown"]
    assert refusal["postCap"] == 0
    assert refusal["minPrice"] == "1000000000g"
    assert refusal["maxExpires"] == 29
    assert refusal["priceReset"] == "none"


def test_each_group_gets_its_own_banded_prices():
    from woweconomy.analytics.mog_operations import (
        BAND_MULTIPLIERS, GROUP_POLICY, POLICIES, VALUE,
    )

    assert len(set(GROUP_POLICY.values())) == len(GROUP_POLICY)  # no sharing
    normals = [p.normal for slug, p in POLICIES.items() if slug != "refusal"]
    assert len(set(normals)) == len(normals)  # no two groups share a price string
    for slug, policy in POLICIES.items():
        if slug == "refusal":
            assert policy.post_cap == 0
            continue
        for field in (policy.normal, policy.minimum, policy.maximum):
            # one ifgt per threshold, on the value anchor, plus the vendorsell floor
            assert field.startswith("max(ifgt("), slug
            assert field.count("ifgt(") == 3, slug
            assert field.count("(") == field.count(")"), slug
            assert field.endswith(", 1.5 * vendorsell)"), slug
            assert VALUE in field, slug
        assert BAND_MULTIPLIERS[slug]


def test_banded_expression_is_a_nested_ifgt_on_the_value():
    from woweconomy.analytics.mog_operations import banded_expression

    assert banded_expression("V", (0.75, 0.65, 0.55, 0.45)) == (
        "ifgt(V, 50000g, 0.75 * V, "
        "ifgt(V, 5000g, 0.65 * V, "
        "ifgt(V, 500g, 0.55 * V, 0.45 * V)))"
    )


def test_bands_are_ordered_and_rise_with_price():
    from woweconomy.analytics.mog_operations import BAND_MULTIPLIERS, BAND_THRESHOLDS_G

    for slug, table in BAND_MULTIPLIERS.items():
        assert len(table) == len(BAND_THRESHOLDS_G) + 1, slug
        for normal, minimum, maximum in table:
            assert 0 < minimum <= normal <= maximum, (slug, normal, minimum, maximum)
        # the factor never falls as the price band rises: cheap stock is
        # discounted hardest, expensive stock is held (table is highest first)
        normals = [band[0] for band in table]
        assert normals == sorted(normals, reverse=True), slug


def test_operations_never_anchor_on_the_realm_market():
    from woweconomy.analytics.mog_operations import POLICIES

    for name, policy in POLICIES.items():
        for field in (policy.normal, policy.minimum, policy.maximum):
            # dbregionmarketavg is regional and safe; the realm dbmarket/dbminbuyout
            # are the player's own auction on a realm with no competition.
            assert "dbmarket" not in field.replace("dbregionmarketavg", ""), name
            assert "dbminbuyout" not in field, name
            # a regional *sale* average is contaminated by snipes (section B).
            assert "dbregionsaleavg" not in field, name


def test_build_mog_export_conflict_inline_keeps_primary_groups():
    items = {"liquid": [1, 2], "conflict": [1]}
    data = deserialize(build_mog_export(items, conflict_inline=True))
    groups = parse_group_export(data["groupExport"])
    assert groups == {"Liquid": [1, 2]}
    assert "Conflict" not in data["groupOperations"]
    assert not any(k.startswith("Mogs Conflict")
                   for k in data["operations"].get("Auctioning", {}))


def test_build_mog_export_without_operations():
    data = deserialize(build_mog_export({"liquid": [1]}, with_operations=False))
    assert data["operations"] == {}
    assert data["groupOperations"] == {}
    assert parse_group_export(data["groupExport"]) == {"Liquid": [1]}


def test_price_reset_none_never_posts_at_the_max_above_a_cheap_wall():
    # "none" makes MakePostDecision return NOT_POSTING.BELOW_MIN when the lowest
    # auction is under the minimum; "maxPrice" would post at the maximum instead.
    from woweconomy.analytics.mog_operations import POLICIES

    for name, policy in POLICIES.items():
        assert policy.price_reset == "none", name


def _realm(klass="balanced", factor=1.0, realm="test"):
    from woweconomy.analytics.mog_realm import RealmProfile

    return RealmProfile(realm=realm, klass=klass, factor=factor)


def test_realm_scale_moves_every_band_and_keeps_it_ordered():
    from woweconomy.analytics.mog_operations import BAND_MULTIPLIERS, scaled_bands

    argent = _realm("deep", 0.21)
    dragonmaw = _realm("thin", 1.08)
    deep, thin = scaled_bands(argent), scaled_bands(dragonmaw)

    for slug, table in BAND_MULTIPLIERS.items():
        assert len(deep[slug]) == len(table), slug
        for (dn, dmin, dmax), (tn, tmin, tmax), (nn, nmin, nmax) in zip(deep[slug], thin[slug], table):
            # one scalar for every band and group: deep posture is 1.00
            assert abs(dn - nn * 0.21) < 1e-3, slug
            assert abs(dmin - nmin * 0.21) < 1e-3, slug
            assert abs(dmax - nmax * 0.21) < 1e-3, slug
            # thin: factor 1.08 x posture 1.20
            assert abs(tn - nn * 1.08 * 1.20) < 1e-3, slug
            assert abs(tmin - nmin * 1.08 * 1.20) < 1e-3, slug
            assert abs(tmax - nmax * 1.08 * 1.20) < 1e-3, slug
        for normal, minimum, maximum in deep[slug]:
            assert 0 < minimum <= normal <= maximum, slug


def test_class_posture_moves_price_duration_and_window_together():
    from woweconomy.analytics.mog_operations import policies
    from woweconomy.analytics.mog_realm import CLASS_POLICY

    thin = policies(_realm("thin", 1.08))
    balanced = policies(_realm("balanced", 1.0))
    deep = policies(_realm("deep", 0.21))

    # the holding window grows as competition falls
    assert (CLASS_POLICY["deep"]["hold_days"] < CLASS_POLICY["balanced"]["hold_days"]
            < CLASS_POLICY["thin"]["hold_days"])

    # liquidity groups repost at the realm's pace, long-tail groups never faster
    # than 48h (a slow item does not gain from being churned)
    assert thin["liquid"].duration == 48
    assert balanced["liquid"].duration == 24
    assert deep["liquid"].duration == 12
    for slug in ("collector", "slow", "very_slow"):
        assert thin[slug].duration == deep[slug].duration == 48, slug
    assert deep["unproven"].duration == 24      # never faster than 24h

    # maxExpires follows duration and window: 60d/48h, 30d/24h, 14d/12h
    assert thin["liquid"].max_expires == 29
    assert balanced["liquid"].max_expires == 29
    assert deep["liquid"].max_expires == 27
    assert deep["collector"].max_expires == 6
    assert thin["collector"].max_expires == 29

    for built in (thin, balanced, deep):
        for policy in built.values():
            assert policy.price_reset == "none"


def test_a_realm_never_anchors_on_the_realm_market():
    from woweconomy.analytics.mog_operations import policies

    for name in ("thin", "balanced", "deep"):
        for slug, policy in policies(_realm(name, 1.11)).items():
            for field in (policy.normal, policy.minimum, policy.maximum):
                assert "dbmarket" not in field.replace("dbregionmarketavg", ""), (name, slug)
                assert "dbminbuyout" not in field, (name, slug)
                assert "dbregionsaleavg" not in field, (name, slug)


def test_max_expires_follows_the_holding_window():
    from woweconomy.analytics.mog_operations import max_expires_for
    from woweconomy.analytics.mog_realm import CLASS_POLICY

    assert CLASS_POLICY["deep"]["hold_days"] < CLASS_POLICY["balanced"]["hold_days"]
    assert CLASS_POLICY["balanced"]["hold_days"] < CLASS_POLICY["thin"]["hold_days"]
    # 30 days at 24h -> 30 posts allowed, the 31st is refused (numExpires > 29)
    assert max_expires_for(24, 30) == 29
    assert max_expires_for(48, 30) == 14
    assert max_expires_for(24, 60) == 59
    assert max_expires_for(48, 14) == 6
    assert max_expires_for(12, 14) == 27


def test_build_mog_export_writes_the_realm_into_the_operation():
    deep = deserialize(build_mog_export({"liquid": [1], "collector": [2]},
                                        realm=_realm("deep", 0.21)))
    thin = deserialize(build_mog_export({"liquid": [1], "collector": [2]},
                                        realm=_realm("thin", 1.08)))
    neutral = deserialize(build_mog_export({"liquid": [1], "collector": [2]}))

    ops = deep["operations"]["Auctioning"]
    assert ops["Mogs Liquid"]["duration"] == 12
    assert ops["Mogs Liquid"]["maxExpires"] == 27
    assert ops["Mogs Collector"]["duration"] == 48
    assert ops["Mogs Collector"]["maxExpires"] == 6
    assert ops["Mogs Liquid"]["priceReset"] == "none"

    # groups, item lists and attached operations never change with the realm
    for other in (thin, neutral):
        assert parse_group_export(other["groupExport"]) == parse_group_export(deep["groupExport"])
        assert set(other["operations"]["Auctioning"]) == set(ops)
        assert other["groupOperations"] == deep["groupOperations"]

    # only the price fields, the duration and the window move
    base = neutral["operations"]["Auctioning"]
    changed = {field for op, policy in ops.items()
               for field, value in policy.items() if value != base[op][field]}
    assert changed == {"normalPrice", "minPrice", "maxPrice", "duration", "maxExpires"}, changed


def test_a_named_realm_resolves_to_its_measured_profile():
    import pytest

    from woweconomy.analytics import mog_realm
    from woweconomy.analytics.mog_operations import policies

    if not mog_realm.PROFILES_CSV.exists():
        pytest.skip("realm_profiles.csv not generated")

    # Dragonmaw shares its auction house with Spinebreaker, which is the name
    # the TSM app reports; both must resolve to the same thin profile.
    assert mog_realm.resolve("Dragonmaw") == mog_realm.resolve("Spinebreaker")
    assert mog_realm.resolve("dragonmaw").klass == "thin"
    assert mog_realm.resolve("ARGENT DAWN").klass == "deep"
    # accented names survive the encoding of the TSM realm table
    assert mog_realm.resolve("Pozzo dell'Eternita").klass == "thin"

    deep = policies("Argent Dawn")
    assert deep["liquid"].duration == 12
    assert "0.178 *" in deep["collector"].normal       # 0.85 x factor 0.21
    # an unknown realm falls back to the regional reference, never to a guess
    unknown = mog_realm.resolve("Nenhum-Reino")
    assert unknown.factor == 1.0 and unknown.confidence == "partilhada"
    assert unknown.realm == "referência regional"
