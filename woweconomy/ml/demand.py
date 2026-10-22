"""Data for Azeroth demand counters, as a feature source.

``dfa.db`` records, for every transmog appearance, recipe or mount, how
many scanned accounts of a realm still *lack* it.  That is a genuine
demand signal -- it is the size of the audience that wants the item and
has not found it yet -- and it is the only demand measure in the
installation that is not derived from the auction house itself.

Two properties have to be kept straight:

**It has no clock.**  ``missing_realm`` is a live aggregate; the only
timestamp in the database is ``account.scanned_at`` and it is not per item.
The demand state of the training window (16-19 October) cannot be
reconstructed, so a demand feature computed today describes *today's*
audience applied to a historical row.  It is therefore flagged
``point_in_time = False`` everywhere, and the walk-forward report shows
what including it does.  A slow-moving audience is a reasonable feature; a
leaked one is not, and the reader must be able to tell which is which.

**The identifier is not the item id.**  The counters are keyed by
*appearance* id.  The bridge is ``db2_item_appearance`` in the TSM mirror.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The wishlist kinds the scan produces.
DEMAND_KINDS = ("appearances", "mounts", "recipes")


@dataclass
class DemandSample:
    """Demand counters for one item, in one realm."""

    #: n accounts of the realm's slice that lack the item
    realm_missing: int = 0
    #: how many accounts the realm's slice has
    realm_sample: int = 0
    #: n accounts of the whole scan that lack the item
    global_missing: int = 0
    #: how many accounts the whole scan has
    global_sample: int = 0
    #: the scan's own weight for the row
    weight: float = 0.0
    #: the item is in the catalogue at all
    in_catalogue: bool = False
    source: str = ""

    @property
    def realm_rate(self) -> float:
        if not self.realm_sample:
            return 0.0
        return self.realm_missing / self.realm_sample

    @property
    def global_rate(self) -> float:
        if not self.global_sample:
            return 0.0
        return self.global_missing / self.global_sample


@dataclass
class DemandBook:
    """Demand counters, indexed by (kind, item id, realm)."""

    per_realm: Dict[Tuple[str, int, str], DemandSample] = field(default_factory=dict)
    appearances: Dict[int, List[int]] = field(default_factory=dict)
    catalogue_names: Dict[int, str] = field(default_factory=dict)
    realm_samples: Dict[str, int] = field(default_factory=dict)
    global_samples: Dict[str, int] = field(default_factory=dict)
    source: str = ""
    available: bool = False
    notes: List[str] = field(default_factory=list)

    def get(self, kind: str, item_id: int, realm: str) -> DemandSample:
        key = (kind, int(item_id), realm)
        hit = self.per_realm.get(key)
        if hit is not None:
            return hit
        return DemandSample(source=self.source or "sem dfa.db")

    def item_demand(self, item_id: int, realm: str) -> Dict[str, DemandSample]:
        """Demand for one TSM item, across the wishlist kinds it maps to."""
        out: Dict[str, DemandSample] = {}
        for kind in DEMAND_KINDS:
            for appearance in self.appearances.get(int(item_id), ()):
                sample = self.get(kind, appearance, realm)
                if sample.in_catalogue or sample.realm_missing or sample.global_missing:
                    out[f"{kind}"] = sample
        return out


def load_realm_groups(dfa_db: Path | str) -> Dict[str, List[str]]:
    """realm -> the realms that share its auction house.

    The scan attributes every account to a *board*; a board named
    ``eu/connected-<x>`` is a connected-realm group, and every member of it
    trades in the same auction house.  A listing in Dragonmaw is therefore
    bought by Dragonmaw *and* Stormreaver, and the demand counters -- which
    are sliced per realm -- have to be added over the whole group.
    """
    groups: Dict[str, List[str]] = {}
    dfa_db = Path(dfa_db)
    if not dfa_db.exists():
        return groups
    try:
        con = sqlite3.connect(f"file:{dfa_db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error:
        return groups
    try:
        board_of: Dict[str, str] = {}
        members: Dict[str, List[str]] = {}
        for realm, board in con.execute("SELECT realm, board FROM account"):
            realm, board = str(realm), str(board)
            members.setdefault(board, [])
            if realm not in members[board]:
                members[board].append(realm)
            # a realm's most specific board wins
            current = board_of.get(realm)
            if current is None or (board.startswith("eu/connected")
                                   and not current.startswith("eu/connected")):
                board_of[realm] = board
        for realm, board in board_of.items():
            group = list(members.get(board, [realm]))
            if realm not in group:
                group.append(realm)
            groups[realm] = group
    except sqlite3.Error as exc:
        logger.warning("could not read the DfA realm groups: %s", exc)
    finally:
        con.close()
    return groups


def item_appearances(tsm_db: Path | str, item_ids: Sequence[int]) -> Dict[int, List[int]]:
    """itemId -> [appearanceId], from the DB2 bridge."""
    out: Dict[int, List[int]] = {}
    if not item_ids:
        return out
    con = sqlite3.connect(tsm_db)
    try:
        ids = list(dict.fromkeys(int(i) for i in item_ids))
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for item_id, appearance_id in con.execute(
                    f"SELECT itemId, appearanceId FROM db2_item_appearance "
                    f"WHERE itemId IN ({marks})", chunk):
                out.setdefault(int(item_id), []).append(int(appearance_id))
    except sqlite3.OperationalError as exc:
        logger.warning("db2_item_appearance unavailable: %s", exc)
    finally:
        con.close()
    return out


def load_demand(dfa_db: Path | str,
                tsm_db: Path | str,
                item_ids: Sequence[int]) -> DemandBook:
    """Read the demand counters for ``item_ids`` out of ``dfa.db``.

    The database is opened read-only: the scan writes to it while it runs.
    """
    book = DemandBook()
    dfa_db = Path(dfa_db)
    if not dfa_db.exists():
        book.notes.append(f"dfa.db not found at {dfa_db}")
        return book

    book.appearances = item_appearances(tsm_db, item_ids)
    wanted = sorted({a for v in book.appearances.values() for a in v})
    if not wanted:
        book.notes.append("no item maps to a DfA appearance id")
        return book

    try:
        con = sqlite3.connect(f"file:{dfa_db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        book.notes.append(f"dfa.db unreadable: {exc}")
        return book
    try:
        # denominators: how many accounts each realm's slice has
        for realm, n in con.execute(
                "SELECT a.realm, COUNT(DISTINCT s.account) FROM scanned s "
                "JOIN account a ON a.account = s.account "
                "WHERE s.kind='appearances' AND s.by_realm=1 GROUP BY a.realm"):
            book.realm_samples[str(realm)] = int(n)
        for kind, n in con.execute(
                "SELECT kind, COUNT(*) FROM scanned WHERE by_realm=1 GROUP BY kind"):
            book.global_samples[str(kind)] = int(n)

        # global counters
        global_missing: Dict[Tuple[str, int], int] = {}
        for kind, item_id, n in con.execute(
                "SELECT kind, item_id, n FROM missing WHERE n > 0"):
            global_missing[(str(kind), int(item_id))] = int(n)

        # catalogue names, for provenance
        for kind, item_id, name in con.execute(
                "SELECT kind, item_id, name FROM catalogue"):
            if int(item_id) in wanted:
                book.catalogue_names[int(item_id)] = str(name or "")

        # per-realm counters, restricted to the appearances we care about
        rows = 0
        for start in range(0, len(wanted), 900):
            chunk = wanted[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for kind, item_id, realm, n, w in con.execute(
                    f"SELECT kind, item_id, realm, n, w FROM missing_realm "
                    f"WHERE item_id IN ({marks}) AND n > 0", chunk):
                sample = book.per_realm.get((str(kind), int(item_id), str(realm)))
                if sample is None:
                    sample = DemandSample(
                        realm_sample=book.realm_samples.get(str(realm), 0),
                        global_sample=book.global_samples.get(str(kind), 0),
                        global_missing=global_missing.get((str(kind), int(item_id)), 0),
                        in_catalogue=True,
                        source="dfa.db::missing_realm")
                    book.per_realm[(str(kind), int(item_id), str(realm))] = sample
                sample.realm_missing = int(n)
                sample.weight = float(w or 0.0)
                rows += 1
        book.available = True
        book.source = f"dfa.db::missing_realm ({rows} rows, {len(wanted)} appearances)"
    except sqlite3.Error as exc:
        book.notes.append(f"dfa.db query failed: {exc}")
    finally:
        con.close()
    return book
