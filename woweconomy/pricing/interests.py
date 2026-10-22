"""Data for Azeroth appearance interest.

The scan knows, for every transmog appearance, how many of the accounts it
scanned still *lack* it.  That is a real audience measurement -- but it is
**not** proof of auction house demand, and this module never pretends
otherwise:

* it is a *prevalence* figure (how many collectors want the set), not a
  sale rate;
* it has **no timestamp** beyond the scan's own collection window, so it
  is reported with the collection date and treated as a slow-moving
  signal;
* the interest of an item whose appearance is worn by several items
  belongs to the *set*, not to the item.

The figures are therefore used two ways only: as a feature the price
model already knows how to weigh, and as *evidence* a human can read
next to the recommendation.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: the wishlist kinds the scan produces
KINDS = ("appearances", "mounts", "recipes")


@dataclass
class AppearanceInterest:
    """What the scan says about one appearance."""

    appearance_id: int
    kind: str = "appearances"
    #: how many scanned accounts lack it
    missing: int = 0
    #: how many scanned accounts there are in total
    sample: int = 0
    #: the share of the sample that owns it, as a **percentage** (the
    #: scan stores it that way: 16.8 means 16.8 %)
    owned_pct: Optional[float] = None
    #: how many realms the figure covers
    realm_coverage: int = 0
    #: how many realms the appearance is worth something in
    realms_with_demand: int = 0
    #: the name in the scan's catalogue
    name: str = ""

    @property
    def missing_rate(self) -> float:
        """The share of the scanned accounts that still want the set."""
        if not self.sample:
            return 0.0
        return self.missing / self.sample

    @property
    def has_measurement(self) -> bool:
        return self.sample > 0 and self.missing > 0


@dataclass
class InterestBook:
    """The interest figures, by appearance id."""

    by_appearance: Dict[int, AppearanceInterest] = field(default_factory=dict)
    #: when the scan was collected
    collected_at: Optional[str] = None
    #: how many accounts the scan covered
    accounts: int = 0
    source: str = ""
    available: bool = False
    #: whether the per-realm coverage was actually read.  ``False`` means
    #: "not measured", which is not the same as "wanted in no realm", and
    #: a consumer that stores the number has to tell the two apart.
    realm_coverage_read: bool = False
    #: the overall demand came from the cached rarity file rather than the
    #: ``rarity`` table, so it is the same measurement read a different way
    rarity_source: str = ""
    notes: List[str] = field(default_factory=list)

    def get(self, appearance_id: Optional[int]) -> Optional[AppearanceInterest]:
        if appearance_id is None:
            return None
        return self.by_appearance.get(int(appearance_id))

    def union(self, appearance_ids: Sequence[int]) -> Optional[AppearanceInterest]:
        """The interest of several appearances, summed.

        An item that is worn as several appearances carries the interest
        of all of them; the union is an upper bound on the interest of
        the item, not an exact figure, and the caller says so.
        """
        if not appearance_ids:
            return None
        parts = [self.by_appearance[a] for a in appearance_ids if a in self.by_appearance]
        if not parts:
            return None
        missing = sum(p.missing for p in parts)
        sample = max(p.sample for p in parts)
        owned = [p.owned_pct for p in parts if p.owned_pct is not None]
        return AppearanceInterest(
            appearance_id=parts[0].appearance_id,
            kind=parts[0].kind,
            missing=missing,
            sample=sample,
            owned_pct=(sum(owned) / len(owned)) if owned else None,
            realm_coverage=max(p.realm_coverage for p in parts),
            realms_with_demand=sum(p.realms_with_demand for p in parts),
        )

    def summary(self) -> Dict[str, object]:
        return {
            "available": self.available,
            "appearances": len(self.by_appearance),
            "accounts": self.accounts,
            "collected_at": self.collected_at,
            "source": self.source,
            "notes": list(self.notes),
        }


def _rarity_json_for(dfa_db: Path) -> Optional[Path]:
    """The cached DfA rarity file that sits next to the scan, if any.

    It is the same measurement the ``rarity`` table holds -- checked
    against the live scan: 51,279 appearances in both, no value differing
    by even half a point -- but it is 0.8 MB of JSON instead of a table
    inside a 3 GB database, so it is read in 0.01 s and it still works
    while a scan holds the write lock.
    """
    candidates = [dfa_db.parent / "cache" / "appearancesrarity.json",
                  dfa_db.parent / "appearancesrarity.json"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_rarity_json(path: Path | str) -> Dict[int, float]:
    """``{appearance_id: owned_pct}`` from the cached rarity file.

    A corrupt or unexpected file is not fatal: the caller falls back to
    the database, and the book records why.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    block = payload.get("appearances") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return {}
    out: Dict[int, float] = {}
    for key, value in block.items():
        try:
            out[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def load_interests(dfa_db: Path | str,
                   appearance_ids: Optional[Sequence[int]] = None,
                   with_realm_coverage: bool = True,
                   rarity_json: Optional[Path | str] = None) -> InterestBook:
    """Read the interest figures for the given appearances.

    ``appearance_ids`` of ``None`` reads every appearance, which is what
    the full dataset wants.

    ``with_realm_coverage`` costs 3 s on the live scan and answers only
    "in how many realms is this wanted".  The overall demand -- the share
    of scanned accounts that own the appearance -- comes from ``rarity``
    and is free, so a caller that only needs demand (the pricing engine,
    the addon evidence) passes ``False`` and skips the walk of 13M rows.

    ``rarity_json`` overrides the cached rarity file; ``None`` discovers
    ``cache/appearancesrarity.json`` next to the scan and falls back to
    the ``rarity`` table when it is missing or corrupt.
    """
    book = InterestBook()
    dfa_db = Path(dfa_db)
    if not dfa_db.exists():
        book.notes.append(f"dfa.db not found at {dfa_db}")
        return book
    try:
        con = sqlite3.connect(f"file:{dfa_db.as_posix()}?mode=ro", uri=True, timeout=15.0)
    except sqlite3.Error as exc:
        book.notes.append(f"dfa.db unreadable: {exc}")
        return book
    # The coverage aggregate walks millions of rows and the pages it needs do
    # not fit SQLite's default 8 MB cache, so the same rows were read from
    # disk over and over: 58 s for one query, 8 s with these settings.  They
    # are connection-local, read-only, and take no lock, so a scan running
    # elsewhere keeps writing.
    for pragma in ("PRAGMA cache_size=-131072", "PRAGMA mmap_size=1073741824",
                   "PRAGMA query_only=1"):
        try:
            con.execute(pragma)
        except sqlite3.Error:
            pass
    try:
        # the collection window and the sample size
        row = con.execute(
            "SELECT MIN(scanned_at), MAX(scanned_at), COUNT(*) FROM account "
            "WHERE scanned_at IS NOT NULL").fetchone()
        if row and row[0]:
            book.collected_at = str(row[1])
            book.accounts = int(row[2] or 0)

        sample = con.execute(
            "SELECT COUNT(*) FROM scanned WHERE kind='appearances' AND by_realm=1"
        ).fetchone()[0] or 0

        # the global counters
        wanted = None
        if appearance_ids is not None:
            wanted = sorted({int(a) for a in appearance_ids})
        rows = []
        if wanted is None:
            rows = con.execute(
                "SELECT item_id, n FROM missing WHERE kind='appearances' AND n > 0"
            ).fetchall()
        else:
            for start in range(0, len(wanted), 900):
                chunk = wanted[start:start + 900]
                marks = ",".join("?" * len(chunk))
                rows.extend(con.execute(
                    f"SELECT item_id, n FROM missing WHERE kind='appearances' AND n > 0 "
                    f"AND item_id IN ({marks})", chunk).fetchall())
        for appearance_id, n in rows:
            book.by_appearance[int(appearance_id)] = AppearanceInterest(
                appearance_id=int(appearance_id), kind="appearances",
                missing=int(n), sample=int(sample))

        # The rarity: the share of scanned accounts that owns the appearance.
        # This is the **overall demand** -- no realm dimension -- and it is
        # what the pricing engine needs.  The cached JSON is preferred: it
        # is the same measurement as the table (checked against the live
        # scan: same 51,279 appearances, no value differing), costs 0.01 s
        # instead of touching the 3 GB database, and is readable while a
        # scan holds the write lock.  The table stays as the fallback.
        source = Path(rarity_json) if rarity_json is not None else _rarity_json_for(dfa_db)
        cached = load_rarity_json(source) if source is not None else {}
        if cached:
            book.notes.append(f"rarity from {source}")
            book.rarity_source = str(source)
            wanted_set = set(wanted) if wanted is not None else None
            rarity = [(appearance_id, value)
                      for appearance_id, value in cached.items()
                      if wanted_set is None or appearance_id in wanted_set]
        elif wanted is None:
            rarity = con.execute(
                "SELECT item_id, owned_pct FROM rarity WHERE kind='appearances' "
                "AND owned_pct IS NOT NULL").fetchall()
        else:
            rarity = []
            for start in range(0, len(wanted), 900):
                chunk = wanted[start:start + 900]
                marks = ",".join("?" * len(chunk))
                rarity.extend(con.execute(
                    f"SELECT item_id, owned_pct FROM rarity WHERE kind='appearances' "
                    f"AND owned_pct IS NOT NULL AND item_id IN ({marks})", chunk).fetchall())
        for appearance_id, owned_pct in rarity:
            entry = book.by_appearance.get(int(appearance_id))
            if entry is not None:
                entry.owned_pct = float(owned_pct)

        # the catalogue name, for the evidence
        names = {}
        for appearance_id, name in con.execute(
                "SELECT item_id, name FROM catalogue WHERE kind='appearances'"):
            names[int(appearance_id)] = str(name or "")
        for appearance_id, entry in book.by_appearance.items():
            entry.name = names.get(appearance_id, "")

        # The realm coverage: in how many realms the appearance is wanted.
        #
        # `COUNT(DISTINCT realm) ... WHERE n > 0` is the obvious query and the
        # wrong one: missing_realm holds 13.0M appearances rows, `n` is not in
        # the primary key, and every row became a random page read -- 58 s on
        # the real scan.  Almost all of those rows are wanted (`n > 0`); only
        # 12k of 13.0M are not.  So count the rows of the covering index
        # (1.0 s) and subtract the unwanted ones realm by realm, which the
        # `missing_realm_lookup(realm, kind, n)` index answers directly
        # (2.2 s).  Both halves were checked against the one-shot query on the
        # live scan: same 44,477 appearances, same counts.
        if with_realm_coverage:
            book.realm_coverage_read = True
            counts: Dict[int, int] = {}
            for item_id, total in con.execute(
                    "SELECT item_id, COUNT(*) FROM missing_realm "
                    "WHERE kind='appearances' GROUP BY item_id"):
                counts[int(item_id)] = int(total)
            for (realm,) in con.execute(
                    "SELECT DISTINCT realm FROM missing_realm WHERE kind='appearances'"):
                for item_id, unwanted in con.execute(
                        "SELECT item_id, COUNT(*) FROM missing_realm "
                        "WHERE realm=? AND kind='appearances' AND n <= 0 "
                        "GROUP BY item_id", (realm,)):
                    key = int(item_id)
                    if key in counts:
                        counts[key] -= int(unwanted)
            for appearance_id, realms in counts.items():
                entry = book.by_appearance.get(appearance_id)
                if entry is not None and realms > 0:
                    entry.realm_coverage = int(realms)
                    entry.realms_with_demand = int(realms)
        else:
            book.notes.append("realm coverage not read (overall demand only)")

        book.available = True
        book.source = "dfa.db::missing + rarity + catalogue"
    except sqlite3.Error as exc:
        book.notes.append(f"dfa.db query failed: {exc}")
    finally:
        con.close()
    return book
