"""Unit tests for the DB2 import (scripts/import_db2).

Fixtures only -- no network: the CSVs are written to a tmp dir and the download
step is never reached.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import import_db2  # noqa: E402


ITEM_CSV = (
    "ID,Description_lang,Display_lang,ExpansionID,ItemLevel,ItemSquishEraID,"
    "Bonding,OverallQualityID,RequiredLevel\n"
    '25,,"Worn Shortsword",0,1,0,2,1,1\n'
    '271435,,"Slippers of the Hissing Cult",11,219,2,1,4,90\n'
    '19019,,"Thunderfury, Blessed Blade of the Windseeker",0,29,0,1,5,25\n'
)
APPEARANCE_CSV = (
    "ID,ItemID,ItemAppearanceModifierID,ItemAppearanceID,OrderIndex,"
    "TransmogSourceTypeEnum,Flags\n"
    "14,17,0,153,0,9,0\n"
    "15,271435,0,128975,0,5,0\n"
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def con():
    db = sqlite3.connect(":memory:")
    db.executescript(import_db2.SCHEMA)
    yield db
    db.close()


def test_number_parses_and_refuses_junk():
    assert import_db2.number("42") == 42
    assert import_db2.number("-1") == -1
    assert import_db2.number("") is None
    assert import_db2.number("n/a") is None


def test_binding_enum_marks_two_as_bind_on_equip():
    assert import_db2.BINDING_NAMES[2] == "ON_EQUIP"
    assert import_db2.BINDING_NAMES[1] == "ON_ACQUIRE"


def test_rows_of_maps_columns_by_name_not_position(tmp_path):
    # The live header is 104 columns wide and its order is build dependent, so
    # the import must go through names.  A reordered CSV must still work.
    path = write(tmp_path, "ItemSparse.csv",
                 "Bonding,Display_lang,ID,ItemSquishEraID,ExpansionID,ItemLevel,"
                 "RequiredLevel,OverallQualityID\n"
                 '2,"Worn Shortsword",25,0,0,1,1,1\n')
    assert list(import_db2.rows_of(path, "ItemSparse")) == [
        ("25", "Worn Shortsword", "2", "1", "0", "0", "1", "1")]


def test_rows_of_names_the_missing_column(tmp_path):
    path = write(tmp_path, "ItemSparse.csv", "ID,Display_lang\n1,Thing\n")
    with pytest.raises(SystemExit, match="Bonding"):
        list(import_db2.rows_of(path, "ItemSparse"))


def test_import_stores_binding_era_and_appearance_link(con, tmp_path):
    n = import_db2.import_table(con, "ItemSparse", write(tmp_path, "ItemSparse.csv", ITEM_CSV))
    assert n == 3
    rows = dict(con.execute(
        "SELECT itemId, binding FROM db2_items"))
    assert rows == {25: 2, 271435: 1, 19019: 1}
    # The classic-era marker is what the Slippers case turned on: it is the only
    # row whose id exists nowhere else, so era != 0 has to survive the import.
    era = dict(con.execute("SELECT itemId, era FROM db2_items"))
    assert era[271435] == 2 and era[25] == 0
    assert dict(con.execute("SELECT itemId, requiredLevel FROM db2_items"))[271435] == 90

    n = import_db2.import_table(con, "ItemModifiedAppearance",
                                write(tmp_path, "ItemModifiedAppearance.csv", APPEARANCE_CSV))
    assert n == 2
    linked = con.execute(
        "SELECT COUNT(*) FROM db2_item_appearance WHERE appearanceId = 128975").fetchone()[0]
    assert linked == 1
    assert con.execute(
        "SELECT itemId FROM db2_item_appearance WHERE appearanceId = 128975").fetchone()[0] \
        == 271435


def test_reimport_replaces_the_derived_table(con, tmp_path):
    # db2_items is derived: a second import must not leave the old rows behind
    # (an older, narrower layout would otherwise keep answering queries).
    import_db2.import_table(con, "ItemSparse", write(tmp_path, "ItemSparse.csv", ITEM_CSV))
    tiny = write(tmp_path, "tiny.csv", ITEM_CSV.split("\n")[0] + "\n" +
                 ITEM_CSV.splitlines()[1] + "\n")
    import_db2.import_table(con, "ItemSparse", tiny)
    assert con.execute("SELECT COUNT(*) FROM db2_items").fetchone()[0] == 1


def test_download_pins_the_build_into_the_url():
    # Without ?build= the site serves "latest", so two runs days apart would
    # merge two patches' item tables into one derived table.
    url = import_db2.WAGO.format(table="ItemSparse", build="12.1.7.70424")
    assert url == "https://wago.tools/db2/ItemSparse/csv?build=12.1.7.70424"
    assert import_db2.DEFAULT_BUILD.count(".") == 3


def test_wishlist_join_counts_only_bind_on_equip(con, tmp_path):
    import_db2.import_table(con, "ItemSparse", write(tmp_path, "ItemSparse.csv", ITEM_CSV))
    import_db2.import_table(con, "ItemModifiedAppearance",
                            write(tmp_path, "ItemModifiedAppearance.csv", APPEARANCE_CSV))
    demand = tmp_path / "dfa"
    demand.mkdir()
    (demand / "demand_appearances.csv").write_text(
        "kind,item_id,name,source,faction,missing,collectors,missing_pct,"
        "owned_pct,value,wowhead,note\n"
        "appearances,153,Worn Shortsword,Drop,,10,100,10.0,90.0,1.0,,,\n"
        "appearances,128975,Slippers of the Hissing Cult,Drop,,99,100,99.0,0.1,98.9,,,\n",
        encoding="utf-8")
    import_db2.wishlist_join(con, demand)
    # Only the Worn Shortsword appearance has a BoE item behind it.
    assert con.execute("SELECT COUNT(*) FROM tmp_wanted").fetchone()[0] == 2
