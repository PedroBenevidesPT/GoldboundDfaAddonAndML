"""Unit tests for the generated collector + market report.

Fixtures only: a throwaway ``dfa.db`` and hand-built bitmaps, so every number
here is checkable on paper.  The module's own docstring claims that the depth
definition is duplicated from the scanner and pinned here so the two cannot
drift silently -- this is that file.
"""
import sqlite3
import zlib

import pytest

from woweconomy.analytics.dfa_report import (
    GENERATED_MARKER,
    MarketRow,
    DfaFacts,
    TsmFacts,
    _depth_rows,
    _output_path,
    _owned_by_realm,
    _recommendation,
    market_wants,
    read_dfa,
    render,
)

SCHEMA = """
CREATE TABLE account (
    account TEXT PRIMARY KEY, region TEXT NOT NULL, realm TEXT NOT NULL,
    name TEXT NOT NULL, rank INTEGER, scanned_at TEXT,
    characters INTEGER NOT NULL DEFAULT 1, board TEXT);
CREATE TABLE collector (
    region TEXT NOT NULL, realm TEXT NOT NULL, name TEXT NOT NULL,
    rank INTEGER, score INTEGER, class INTEGER, faction INTEGER,
    guild TEXT, account TEXT, board TEXT, probed_at TEXT,
    PRIMARY KEY(region, realm, name));
CREATE TABLE board_member (
    board TEXT NOT NULL, region TEXT NOT NULL, realm TEXT NOT NULL,
    name TEXT NOT NULL, rank INTEGER,
    PRIMARY KEY(board, region, realm, name));
CREATE TABLE scanned (
    kind TEXT NOT NULL, account TEXT NOT NULL, scanned_at TEXT, weight REAL,
    by_realm INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(kind, account));
CREATE TABLE missing_realm (
    kind TEXT NOT NULL, item_id INTEGER NOT NULL,
    region TEXT NOT NULL, realm TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0, w REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(kind, item_id, region, realm));
CREATE TABLE catalogue (
    kind TEXT NOT NULL, item_id INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', faction TEXT,
    obtainable INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(kind, item_id));
"""


def _blob(owned_slots, slots):
    """A stored collection, packed the way the scanner packs it (LSB first)."""
    raw = bytearray(slots // 8 + (1 if slots % 8 else 0))
    for slot in owned_slots:
        raw[slot >> 3] |= 1 << (slot & 7)
    return zlib.compress(bytes(raw), 6)


class FakeItem:
    """Only what `market_wants` reads off a basket item."""

    def __init__(self, appearance_id, name, prices):
        self.appearance_id = appearance_id
        self.name = name
        self.realm_prices = prices


@pytest.fixture()
def db(tmp_path):
    """A ladder where depth and the regional count are both known by hand.

    Realm A: ranks 1..5 (scores 500..100), all on its own ladder -> the 3rd is
    300.  Realm B: three characters on the region ladder only (scores 900, 800,
    700) -> a count, never a depth.  Realm C: two characters, fewer than the
    depth -> no N-th at all.
    """
    path = tmp_path / "dfa.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rows = [("eu", "A", f"a{i}", i, 600 - 100 * i, "eu/connected-a") for i in range(1, 6)]
    rows += [("eu", "B", f"b{i}", i, 1000 - 100 * i, "eu") for i in range(1, 4)]
    rows += [("eu", "C", f"c{i}", i, 200, "eu/connected-c") for i in range(1, 3)]
    con.executemany(
        "INSERT INTO collector(region, realm, name, rank, score, board) "
        "VALUES(?,?,?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO board_member(board, region, realm, name, rank) "
        "VALUES(?,?,?,?,?)",
        [(board, region, realm, name, rank) for region, realm, name, rank, _s, board in rows])
    con.executemany(
        "INSERT INTO account(account, region, realm, name, rank, scanned_at, board) "
        "VALUES(?,?,?,?,?,?,?)",
        [(f"acc{i}", "eu", "Dragonmaw" if i < 3 else "Vashj", f"m{i}", i,
          "2026-10-17T00:00:00+00:00", "eu/connected-a") for i in range(1, 5)])
    con.executemany(
        "INSERT INTO scanned(kind, account, scanned_at, weight, by_realm) "
        "VALUES('appearances', ?, '2026-10-17T00:00:00+00:00', 1.0, 1)",
        [(f"acc{i}",) for i in range(1, 5)])
    con.execute("INSERT INTO missing_realm(kind, item_id, region, realm, n, w) "
                "VALUES('appearances', 801, 'eu', 'Dragonmaw', 2, 2.0)")
    con.commit()
    con.close()
    return path


def _facts(db):
    # depth 3 so "fewer than three" (realm C) is a real case
    return read_dfa(db, depth=3, threshold=400)


def test_depth_is_the_nth_best_not_the_average_or_the_best(db):
    facts = _facts(db)
    by = {(d.realm, d.board): d for d in facts.depths}
    a = by[("A", "eu/connected-a")]
    assert a.local
    assert a.at_depth == 300          # 3rd best of 500, 400, 300, 200, 100
    assert a.listed == 5
    assert a.best == 500
    assert a.average == 300           # the mean of everything read
    assert a.above == 2               # 500 and 400 are >= 400


def test_a_realm_with_fewer_than_n_has_no_depth(db):
    facts = _facts(db)
    c = [d for d in facts.depths if d.realm == "C"][0]
    assert c.listed == 2
    assert c.at_depth is None         # not the 2nd best pretending to be the 3rd


def test_the_region_ladder_is_a_count_not_a_depth(db):
    facts = _facts(db)
    b = [d for d in facts.depths if d.realm == "B"][0]
    assert not b.local
    assert b.above == 3               # everyone above the cut, all regional
    assert facts.max_rank["B"] == 3   # "lido até", for the sample table


def test_sample_and_database_sizes_come_from_the_same_read(db):
    facts = _facts(db)
    assert facts.accounts == 4
    assert facts.by_realm_accounts == 4
    assert facts.missing_realm_rows == 1
    assert sum(o.accounts for o in facts.owned) == 0   # nothing stored yet


def test_the_default_output_never_eats_a_hand_written_report(tmp_path):
    mine = tmp_path / "realm_report_2026-10-18.md"
    assert _output_path(mine) == mine          # nothing there: write where asked
    mine.write_text("# Relatório\n\n**Data:** 2026-10-18\n", encoding="utf-8")
    assert _output_path(mine) == tmp_path / "realm_report_2026-10-18.auto.md"
    # ...and a file this module wrote is ours to refresh
    mine.write_text(f"# Relatório\n\n**Gerado:** x {GENERATED_MARKER}\n",
                    encoding="utf-8")
    assert _output_path(mine) == mine


def test_the_report_reads_a_database_without_bitmaps(tmp_path):
    con = sqlite3.connect(tmp_path / "old.db")
    con.executescript(SCHEMA)      # no item_slot, no owned
    assert _owned_by_realm(con, "appearances") == []
    con.close()


def _with_bitmaps(db, rows):
    """Add item_slot/owned to the fixture: `rows` = (account, slots, blob)."""
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS item_slot (
            kind TEXT NOT NULL, item_id INTEGER NOT NULL, slot INTEGER NOT NULL,
            PRIMARY KEY(kind, item_id));
        CREATE INDEX IF NOT EXISTS item_slot_slot ON item_slot(kind, slot);
        CREATE TABLE IF NOT EXISTS owned (
            kind TEXT NOT NULL, account TEXT NOT NULL, bits BLOB NOT NULL,
            owned INTEGER NOT NULL DEFAULT 0, slots INTEGER NOT NULL DEFAULT 0,
            written_at TEXT, src TEXT, PRIMARY KEY(kind, account));
    """)
    con.execute("INSERT OR REPLACE INTO item_slot VALUES('appearances', 801, 0)")
    con.execute("INSERT OR REPLACE INTO item_slot VALUES('appearances', 802, 1)")
    con.execute("INSERT OR REPLACE INTO item_slot VALUES('appearances', 803, 2)")
    con.executemany("INSERT OR REPLACE INTO owned VALUES(?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def test_a_bit_at_zero_means_the_account_still_wants_it(db):
    # acc1 owns 801 only; acc2 owns 801 and 802; acc3 owns all three.
    _with_bitmaps(db, [("appearances", "acc1", _blob([0], 3), 1, 3, "x", "t"),
                       ("appearances", "acc2", _blob([0, 1], 3), 2, 3, "x", "t"),
                       ("appearances", "acc3", _blob([0, 1, 2], 3), 3, 3, "x", "t")])
    facts = _facts(db)
    assert facts.slots == {801: 0, 802: 1, 803: 2}
    assert sum(o.accounts for o in facts.owned) == 3
    # acc1 and acc2 are Dragonmaw, acc3 is Vashj: two markets, one row each.
    rows = [MarketRow(label="Dragonmaw +1", realms=("Dragonmaw", "Vashj"),
                      items=3, cover=100.0, value=1.0, premium=1.0, supply=1.0,
                      capped=0, liquid=3, liquid_share=100.0, liquid_premium=1.0)]
    common = [FakeItem(801, "Um", {"Dragonmaw": 10000}),
              FakeItem(802, "Dois", {"Dragonmaw": 20000}),
              FakeItem(803, "Três", {"Dragonmaw": 30000})]
    wanted = market_wants(facts, rows, common)
    accounts, ranked = wanted["Dragonmaw +1"]
    assert accounts == 3
    by_name = {name: (count, pct, price) for name, count, pct, price in ranked}
    assert "Um" not in by_name                  # everyone owns it: not a row
    assert by_name["Dois"][0] == 1              # only acc1 lacks it
    assert by_name["Três"][0] == 2              # acc1 and acc2 lack it
    assert by_name["Três"][2] == pytest.approx(3.0)   # price from the market


def test_market_wants_is_silent_without_a_bitmap(db):
    facts = _facts(db)
    row = MarketRow(label="A", realms=("A",), items=1, cover=100.0, value=1.0,
                    premium=1.0, supply=1.0, capped=0)
    assert market_wants(facts, [row], [FakeItem(801, "Um", {"A": 1})]) == {}


def test_recommendation_reads_the_three_axes(db):
    facts = _facts(db)
    at = {d.realm: d for d in facts.depths if d.at_depth is not None}
    rows = [
        MarketRow(label="Fundo", realms=("A",), items=10, cover=100.0, value=1.0,
                  premium=0.5, supply=5.0, capped=0, liquid=9,
                  liquid_share=90.0, liquid_premium=0.5),
        MarketRow(label="Meio", realms=("C",), items=10, cover=100.0, value=1.0,
                  premium=1.7, supply=2.0, capped=0, liquid=4,
                  liquid_share=40.0, liquid_premium=1.6),
        MarketRow(label="Dragonmaw", realms=("Dragonmaw",), items=10, cover=100.0,
                  value=1.0, premium=1.9, supply=1.0, capped=0, liquid=1,
                  liquid_share=10.0, liquid_premium=1.8),
    ]
    text = "\n".join(_recommendation(rows, at, "Dragonmaw"))
    assert "listar em **Fundo**" in text        # deep and cheap wins rotation
    assert "no teu reino (Dragonmaw" in text    # margin goes to the journal realm
    assert "Teste controlado:** Meio" in text   # the mid market is the test
    assert "«pede-se mais»" in text             # the warning is never dropped


def test_render_prints_the_cross_table_and_says_what_is_missing(db):
    facts = _facts(db)
    rows = [MarketRow(label="A +1", realms=("A", "C"), items=2, cover=100.0,
                      value=1.0, premium=1.8, supply=1.0, capped=0, liquid=0,
                      liquid_share=0.0, liquid_premium=0.0)]
    tsm = TsmFacts(rows=10, items=10, realms=["A"], scanned_at="2026-10-16 21:36",
                   journal=(2, 3, 4, 5), home_realm="A")
    text = render(facts, tsm, rows, {"wanted": 1, "fora_bop": 0, "fora_era": 0,
                                     "sem_preco": 0, "sem_item": 0},
                  basket_size=2, common_size=1, depth=3, threshold=400,
                  min_score=70.0, cap=2.0, source_dfa=db, source_tsm=db)
    assert "## Onde pôr o quê (coleccionadores × mercado)" in text
    assert "**A +1** | 300 @3, 2 ≥ 400" in text           # depth cell, not a guess
    assert "## Quem quer (dos bitmaps de colecção)" in text
    assert "## Leitura mecânica" in text
    # realm C has two characters, so it is reported without a 3rd best
    assert "Sem 3.º lugar nestes reinos" in text
    assert "lido até" in text
