r"""Storage layer of the scan database, shared by the Blizzard collectors.

Extracted verbatim from ``C:\economics\woweconomy\services\dataforazeroth.py``
(the Data for Azeroth scraper): only the parts the Blizzard collectors need
to write what they fetch -- the schema, the bitmap codec that stores owned
collections, the weighted "missing" counters and the collector weight.

The scraper itself is not here: it reads a site that now blocks this IP, and
nothing in this project calls it.  What stays is the contract the tables in
``dfa.db`` already have, so ``collector``/``owned``/``missing`` keep the exact
layout the reports (``item_demand``, ``sale_probability``, ``realm_gaps``)
already read.

Byte-identical semantics: every function below is the original code, with
the only edit being the default data folder (see ``DEFAULT_OUT_DIR``).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from woweconomy.pricing.config import PricingConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KindSource:
    """Where one collection kind's data lives, and under which id.

    None of these can be derived from the kind's name, and getting one wrong
    fails silently rather than loudly, so they are spelled out:

    * ``catalog``   the /dynamic table holding names and sources
    * ``file``      the /dynamic table holding the rarity percentages
    * ``payload``   the key *inside* the rarity table (mounts!="mountsrarity",
                    and appearances uses "appearances" while its catalogue is
                    "appearancesources")
    * ``id_field``  which id the collection API and the rarity table key on.
                    Appearances are keyed by ``visualid`` (16128 of Vulppu's
                    16129 owned ids match it; only 10854 match ``id``), while
                    mounts and recipes just use ``id``.
    """

    catalog: str
    file: str
    route: str
    payload: str
    id_field: str = "id"


KIND_SOURCES: dict[str, KindSource] = {
    "mounts": KindSource("mounts", "mountsrarity", "mounts", "mounts"),
    "recipes": KindSource("recipes", "recipesrarity", "recipes", "recipes"),
    "appearances": KindSource("appearancesources", "appearancesrarity",
                              "appearances", "appearances", "visualid"),
}


KINDS = tuple(KIND_SOURCES)


UNOBTAINABLE_CODES = (1, 2)


#: where the catalogue and demand files live.  It follows the scan
#: database the rest of the project already configures, so a collector
#: run here reads and writes the same folder as the reports do.
DEFAULT_OUT_DIR = Path(os.environ.get("DFA_DIR")
                       or PricingConfig().dfa_db).parent


def _cache_path(out_dir: Path, key: str) -> Path:
    return out_dir / "cache" / f"{key}.json"


def is_obtainable(entry: dict) -> bool:
    """Still gettable in game, per the catalogue's own flags."""
    if entry.get("excluded"):
        return False
    return entry.get("unobtainable") not in UNOBTAINABLE_CODES


SCHEMA = """
PRAGMA journal_mode=WAL;
-- region/realm here are the character's OWN realm (Dragonmaw and Vashj are
-- different rows of the same connected leaderboard), so the leaderboard a row
-- came from is tracked separately in `board`.
CREATE TABLE IF NOT EXISTS collector(
    region TEXT NOT NULL, realm TEXT NOT NULL, name TEXT NOT NULL,
    rank INTEGER, score INTEGER, class INTEGER, faction INTEGER,
    guild TEXT, account TEXT, board TEXT, probed_at TEXT,
    PRIMARY KEY(region, realm, name));
-- Collections are ACCOUNT wide: 4.03M characters share 1.26M accounts, so ~3.2
-- characters describe the same wishlist.  Counting them all would triple-weight
-- the 3-alts-in-one-house crowd, so the sample is counted per account and every
-- further character of a known account is just noted.
-- `board` is the leaderboard whose walk fetched the account, and `realm` the
-- realm of the character that named it; the character-level detail (every alt
-- and the realm it lives on) stays in `collector`.
CREATE TABLE IF NOT EXISTS account(
    account TEXT PRIMARY KEY, region TEXT NOT NULL, realm TEXT NOT NULL,
    name TEXT NOT NULL, rank INTEGER, scanned_at TEXT,
    characters INTEGER NOT NULL DEFAULT 1, board TEXT);
-- `n` is one vote per account; `w` is the same votes weighted by score (see
-- `collector_weight`), so a whale's wishlist counts for more than a fresh 80's.
CREATE TABLE IF NOT EXISTS missing(
    kind TEXT NOT NULL, item_id INTEGER NOT NULL, n INTEGER NOT NULL DEFAULT 0,
    w REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(kind, item_id));
-- `weight` is the score weight this wishlist was counted with, NULL for
-- accounts scanned before weights existed (see `reweight`).
CREATE TABLE IF NOT EXISTS scanned(
    kind TEXT NOT NULL, account TEXT NOT NULL, scanned_at TEXT, weight REAL,
    PRIMARY KEY(kind, account));
-- Which leaderboards a character was listed on.  The same character sits on its
-- realm's board and on its connected group's board, while `collector` keeps one
-- row per (region, realm, name): membership belongs here, so each board is
-- resumed on its own and nobody is counted twice across them.
CREATE TABLE IF NOT EXISTS board_member(
    board TEXT NOT NULL, region TEXT NOT NULL, realm TEXT NOT NULL,
    name TEXT NOT NULL, rank INTEGER,
    PRIMARY KEY(board, region, realm, name));
CREATE INDEX IF NOT EXISTS board_member_rank ON board_member(board, rank);
-- The catalogues the site serves as JSON, flattened into rows: what an item is
-- called, where it comes from, whether it can still be got, and how many
-- profiles own it.  `to-sql` fills them from the download cache and `report`
-- reads them when they are there, so a report needs no JSON on disk.
CREATE TABLE IF NOT EXISTS catalogue(
    kind TEXT NOT NULL, item_id INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', faction TEXT,
    obtainable INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(kind, item_id));
CREATE TABLE IF NOT EXISTS rarity(
    kind TEXT NOT NULL, item_id INTEGER NOT NULL, owned_pct REAL,
    PRIMARY KEY(kind, item_id));
-- The same votes sliced by the account's OWN realm: `missing` answers "what does
-- the whole sample want" (every board summed), this answers "what does this
-- realm want", which is the number a seller on that auction house needs.  It is
-- written by `scan` from now on; `reweight --realm X` fills in the wishlists that
-- were counted before it existed (`scanned.by_realm` says which are in here).
CREATE TABLE IF NOT EXISTS missing_realm(
    kind TEXT NOT NULL, item_id INTEGER NOT NULL,
    region TEXT NOT NULL, realm TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0, w REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(kind, item_id, region, realm));
CREATE INDEX IF NOT EXISTS missing_realm_lookup
    ON missing_realm(realm, kind, n);
-- One stable slot per obtainable item, so an account's wishlist can be a bitmap
-- instead of ~19k rows.  Slots are handed out once, in ascending id order, and
-- never reused or reordered: a blob written today has to still read correctly
-- after a patch adds items, and that is exactly what appending slots gives
-- (`owned.slots` remembers how many bits were meaningful when it was written).
CREATE TABLE IF NOT EXISTS item_slot(
    kind TEXT NOT NULL, item_id INTEGER NOT NULL, slot INTEGER NOT NULL,
    PRIMARY KEY(kind, item_id));
CREATE INDEX IF NOT EXISTS item_slot_slot ON item_slot(kind, slot);
-- What one account OWNS, one zlib bitmap per kind (~2KB) over `item_slot`.
-- Row-per-id was never an option: 44,462 obtainable appearances x 6,400 accounts
-- is 124M rows, which is why the scan used to reduce each payload to counters
-- and drop it.  The bitmap keeps the payload, and that is what makes "who wants
-- this item" answerable -- a count says how many, never who.
CREATE TABLE IF NOT EXISTS owned(
    kind TEXT NOT NULL, account TEXT NOT NULL, bits BLOB NOT NULL,
    owned INTEGER NOT NULL DEFAULT 0, slots INTEGER NOT NULL DEFAULT 0,
    written_at TEXT, src TEXT,
    PRIMARY KEY(kind, account));
-- The per-realm demand with the name/source already joined, so a query on this
-- one table is enough (`WHERE realm='Silvermoon' ORDER BY n DESC`).
CREATE VIEW IF NOT EXISTS demand_realm AS
SELECT m.kind, m.realm, m.item_id, c.name, c.source, c.faction,
       c.obtainable, m.n, m.w
FROM missing_realm m
LEFT JOIN catalogue c ON c.kind = m.kind AND c.item_id = m.item_id;
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema without losing progress."""
    # Score-weighted demand: an older database has the counts but neither the
    # weighted sums nor the per-account weight, and both are added here so a
    # database written by the previous version keeps every row it already has.
    mcols = {row[1] for row in con.execute("PRAGMA table_info(missing)")}
    if "w" not in mcols:
        con.execute("ALTER TABLE missing ADD COLUMN w REAL NOT NULL DEFAULT 0")
    scols = {row[1] for row in con.execute("PRAGMA table_info(scanned)")}
    if "weight" not in scols:
        con.execute("ALTER TABLE scanned ADD COLUMN weight REAL")
    # 1 = this wishlist's votes are in `missing_realm`; 0 = they only went into
    # the global `missing` (counted before the table existed), which is what
    # `reweight --realm` looks for to backfill.
    if "by_realm" not in scols:
        con.execute("ALTER TABLE scanned ADD COLUMN by_realm INTEGER NOT NULL DEFAULT 0")
    con.commit()
    cols = {row[1] for row in con.execute("PRAGMA table_info(collector)")}
    if "probed_at" not in cols:
        con.execute("ALTER TABLE collector ADD COLUMN probed_at TEXT")
        # Anything already tied to an account has, by definition, been probed.
        con.execute("UPDATE collector SET probed_at=? WHERE account IS NOT NULL", (_now(),))
        con.commit()
    # Which board an account came from is new, and it is recoverable exactly: the
    # account row remembers the character that named it, and that character's
    # `board` is the one the walk was on.  The backfill runs whenever rows are
    # missing it -- not only when the column is added -- because an older process
    # still writing to the same database inserts accounts without one.
    acols = {row[1] for row in con.execute("PRAGMA table_info(account)")}
    if "board" not in acols:
        con.execute("ALTER TABLE account ADD COLUMN board TEXT")
    todo = con.execute("SELECT COUNT(*) FROM account WHERE board IS NULL").fetchone()[0]
    if todo:
        cur = con.execute(
            "UPDATE account SET board = (SELECT c.board FROM collector c "
            "WHERE c.region=account.region AND c.realm=account.realm "
            "AND c.name=account.name) "
            "WHERE board IS NULL AND EXISTS ("
            "  SELECT 1 FROM collector c2 WHERE c2.region=account.region "
            "  AND c2.realm=account.realm AND c2.name=account.name "
            "  AND c2.board IS NOT NULL)")
        if cur.rowcount:
            logger.info("account.board: %d rows backfilled from collector", cur.rowcount)
        con.commit()
    # Older databases only know the board each character was *stored* from, and
    # that is enough to seed the membership table.  The test is "fewer rows than
    # `collector` knows about" rather than "empty": a run of the previous version
    # still writing while this one starts is exactly the case that has to keep
    # working, and re-seeding costs one pass over a few thousand rows.
    seeded = con.execute("SELECT COUNT(*) FROM board_member").fetchone()[0]
    listed = con.execute(
        "SELECT COUNT(*) FROM collector WHERE board IS NOT NULL").fetchone()[0]
    if seeded < listed:
        cur = con.execute(
            "INSERT OR IGNORE INTO board_member(board, region, realm, name, rank) "
            "SELECT board, region, realm, name, rank FROM collector "
            "WHERE board IS NOT NULL")
        if cur.rowcount > 0:
            logger.info("board_member: %d rows seeded from collector", cur.rowcount)
        con.commit()


WEIGHT_REF_SCORE = 400_000


WEIGHT_MIN = 0.25


WEIGHT_MAX = 4.0


def collector_weight(score: Optional[int]) -> float:
    """How many votes one collector casts, relative to a mid-ladder (400k) one."""
    if not score or score <= 0:
        return 1.0
    return max(WEIGHT_MIN, min(WEIGHT_MAX, score / WEIGHT_REF_SCORE))


def bump_realm(con: sqlite3.Connection, kind: str, ids: Iterable[int],
               region: str, realm: str, count: int = 1,
               weight: float = 0.0) -> int:
    """Slice votes by realm: +`count` accounts and +`weight` per item.

    `count=0` is the reweight case -- the vote is already in the table, only its
    weight was missing.
    """
    rows = [(kind, int(i), region, realm, count, weight) for i in ids]
    con.executemany(
        "INSERT INTO missing_realm(kind, item_id, region, realm, n, w) "
        "VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, item_id, region, realm) DO UPDATE SET "
        "n = n + excluded.n, w = w + excluded.w",
        rows,
    )
    return len(rows)


def bump_missing(con: sqlite3.Connection, kind: str, ids: Iterable[int],
                 weight: float = 1.0, region: Optional[str] = None,
                 realm: Optional[str] = None) -> int:
    """Count `ids` as missing: +1 account, +`weight` weighted votes.

    With `region`/`realm` the same votes are also counted in `missing_realm`, so
    the global totals and the per-realm slice are written in one go (one
    transaction, so a Ctrl-C cannot leave them disagreeing).
    """
    ids = [int(i) for i in ids]
    rows = [(kind, i, 1, weight) for i in ids]
    con.executemany(
        "INSERT INTO missing(kind, item_id, n, w) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(kind, item_id) DO UPDATE SET n = n + 1, w = w + excluded.w",
        rows,
    )
    if region and realm:
        bump_realm(con, kind, ids, region, realm, 1, weight)
    return len(rows)


def slot_index(con: sqlite3.Connection, kind: str,
               ids: Optional[Iterable[int]] = None
               ) -> tuple[dict[int, int], list[int]]:
    """`(item_id -> slot, slot -> item_id)` for one kind, assigning new slots.

    Called with the obtainable ids the caller is about to store, this hands a new
    slot to every one of them that has none yet.  The assignment is
    append-only and in ascending id order, so it does not depend on which account
    happens to be scanned first, and a blob written before a patch still means
    the same thing after it.
    """
    pairs = con.execute("SELECT item_id, slot FROM item_slot WHERE kind=?",
                        (kind,)).fetchall()
    index = {int(i): int(s) for i, s in pairs}
    if ids is not None:
        fresh = sorted({int(i) for i in ids if int(i) not in index})
        if fresh:
            nxt = (con.execute("SELECT MAX(slot) FROM item_slot WHERE kind=?",
                               (kind,)).fetchone()[0]
                   if pairs else None)
            start = 0 if nxt is None else int(nxt) + 1
            rows = [(kind, item_id, start + k) for k, item_id in enumerate(fresh)]
            con.executemany(
                "INSERT OR IGNORE INTO item_slot(kind, item_id, slot) VALUES(?,?,?)",
                rows)
            con.commit()
            index.update({item_id: start + k for k, item_id in enumerate(fresh)})
    by_slot = [0] * (max(index.values()) + 1 if index else 0)
    for item_id, slot in index.items():
        if slot < len(by_slot):
            by_slot[slot] = item_id
    return index, by_slot


def pack_owned(ids: Iterable[int], index: Mapping[int, int], slots: int) -> bytes:
    """`ids` as a zlib bitmap of `slots` bits (bit set = the account owns it).

    Ids without a slot are not in the catalogue and are dropped rather than
    silently landing in bit 0.
    """
    size = slots // 8 + (1 if slots % 8 else 0)
    raw = bytearray(size)
    for item_id in ids:
        slot = index.get(int(item_id))
        if slot is None:
            continue
        raw[slot >> 3] |= 1 << (slot & 7)
    return zlib.compress(bytes(raw), 6)


def unpack_owned(bits: bytes, by_slot: Sequence[int]) -> set[int]:
    """The item ids a stored bitmap says the account owns."""
    own: set[int] = set()
    for pos, byte in enumerate(zlib.decompress(bits)):
        if not byte:
            continue
        base = pos * 8
        for bit in range(8):
            if byte >> bit & 1:
                slot = base + bit
                if slot < len(by_slot):
                    own.add(by_slot[slot])
    return own


def store_owned(con: sqlite3.Connection, kind: str, account: str,
                ids: Iterable[int], index: Mapping[int, int], slots: int,
                src: str = "scan") -> int:
    """Write one account's owned set as a bitmap; idempotent per (kind, account)."""
    kept = {int(i) for i in ids if int(i) in index}
    con.execute(
        "INSERT INTO owned(kind, account, bits, owned, slots, written_at, src) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(kind, account) DO UPDATE SET bits=excluded.bits, "
        "owned=excluded.owned, slots=excluded.slots, "
        "written_at=excluded.written_at, src=excluded.src",
        (kind, account, pack_owned(kept, index, slots), len(kept), slots,
         _now(), src),
    )
    return len(kept)


def _obtainable_for(con: sqlite3.Connection, out_dir: Path, kind: str) -> set[int]:
    """The ids that can still be got: from SQL when `to-sql` filled `catalogue`.

    Same fallback as the report, so `reweight` keeps working after the JSON cache
    is deleted -- it filters with the catalogue, and only the catalogue matters.
    """
    rows = con.execute("SELECT item_id, obtainable FROM catalogue WHERE kind=?",
                       (kind,)).fetchall()
    if rows:
        return {i for i, ok in rows if ok}
    try:
        catalog = load_catalog_local(out_dir, kind)
    except KeyError:
        # A kind the DfA feed never served (pets, toys): there is no JSON to fall
        # back to, and the caller says what to run instead of dying on a KeyError.
        return set()
    return {i for i, e in catalog.items() if is_obtainable(e)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_catalog_local(out_dir: Path, kind: str) -> dict[int, dict]:
    path = _cache_path(out_dir, KIND_SOURCES[kind].catalog)
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8")).get(KIND_SOURCES[kind].catalog) or []
    out: dict[int, dict] = {}
    for row in rows:
        try:
            key = int(row[KIND_SOURCES[kind].id_field])
        except (KeyError, TypeError, ValueError):
            continue
        prev = out.get(key)
        if prev is None or (not is_obtainable(prev) and is_obtainable(row)):
            out[key] = row
    return out
