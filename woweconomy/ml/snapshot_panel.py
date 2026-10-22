"""The market panel, built from our own auction-house snapshots.

The risk targets ("will the cheapest listing fall?", "will the market value
drop?") are *market* questions, and until now they were answered from TSM's
`tsm_snapshot` table, restricted to the realms and items the account had
personally traded.  That restriction is the wrong shape for the question: the
account has traded 11 realms and a few hundred items, while the Blizzard API
gives us 92 connected realms and 30,595 items per sweep.

So this module builds the same panel from `ah_snapshot` + `ah_auction`, which
is data the project fetches itself and can fetch for every book.  The output
columns are the ones :func:`woweconomy.ml.sentinel.build_risk_examples`
already reads, so the target definition does not change -- only the evidence
behind it.

What a snapshot can and cannot supply
-------------------------------------
A snapshot is a list of live listings.  From it, honestly:

* ``min_buyout`` -- the cheapest unit price on offer;
* ``market``     -- the quantity-weighted mean unit price.  A weighted mean
  is not a median, and it is stated as a weighted mean rather than dressed
  up as a "market value"; a lone 500k listing for a 20-copper item is one
  listing against however many others exist, and the weight says so;
* ``recent``     -- the cheapest unit price among listings with a ``SHORT``
  time left, i.e. the ones about to expire;
* ``listings`` / ``supply`` -- how many offers and how many units.

What it cannot supply is anything that requires *sales*: ``sale_rate``,
``sold_per_day``, ``avg_sale_price``, ``historical``.  Those stay absent and
the feature builder turns them into NaN, which the imputer handles.  Filling
them with a plausible constant would be inventing the exact signal the model
is supposed to learn, so they are not filled.

Grouping
--------
Rows are grouped by **variant**, not by item, whenever the snapshot carries a
`variant_key`.  Pooling variants is what made "the price of item 82800" a
number spanning 400 copper to 9,999,999 gold: that item id is a caged-pet
container whose contents differ per listing.  ``variant_key`` is item plus
every modifier that changes what the buyer receives, so two different pets
are two different markets.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

#: Only these rows may contribute a price.  ``prices.OK`` is the status that
#: says the stored value is usable; ``missing``/``sentinel``/``outlier`` are
#: observations, not prices.
OK_STATUS = "ok"

PANEL_COLUMNS: Sequence[str] = (
    "ts", "scope", "item_id", "variant_key",
    "market", "min_buyout", "recent", "listings", "supply",
)


def _canonical_slugs(con: sqlite3.Connection) -> Dict[int, str]:
    """connected_realm_id -> one slug, taken from the realm index.

    The scope of a snapshot must be the **book**, not the name it was stored
    under.  Historical rows carry an empty ``realm_slug`` (the single-realm
    fetch path never set one), so 10 books currently appear twice -- once as
    ``''`` and once as ``'dragonmaw'`` -- and a change series split across
    two names loses every observation that spans the split.  The index knows
    which realms form a book, so it can answer this without trusting the
    stored name.
    """
    try:
        rows = con.execute(
            "SELECT connected_realm_id, MIN(realm_slug) FROM ah_realm"
            " WHERE connected_realm_id IS NOT NULL AND realm_slug <> ''"
            " GROUP BY connected_realm_id").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(cr): str(slug) for cr, slug in rows}


@dataclass
class PanelReport:
    """Coverage, so a thin panel is visible instead of silently small."""

    rows: int = 0
    books: int = 0
    items: int = 0
    snapshots: int = 0
    span_hours: float = 0.0
    books_dropped_single_snapshot: int = 0
    variants_pooled: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "rows": self.rows,
            "books": self.books,
            "items": self.items,
            "snapshots": self.snapshots,
            "span_hours": round(self.span_hours, 1),
            "books_dropped_single_snapshot":
                self.books_dropped_single_snapshot,
            "variants_pooled": self.variants_pooled,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        return (f"market panel: {self.rows:,} rows, {self.books} books, "
                f"{self.items:,} items, {self.snapshots} snapshots, "
                f"{self.span_hours:.1f} h span"
                + (f", {self.books_dropped_single_snapshot} books with a"
                   " single snapshot dropped"
                   if self.books_dropped_single_snapshot else ""))


def _has_variant_column(con: sqlite3.Connection) -> bool:
    return any(row[1] == "variant_key"
               for row in con.execute("PRAGMA table_info(ah_auction)"))


def load_market_panel(intel_db: Path | str, *,
                      scopes: Optional[Sequence[str]] = None,
                      item_ids: Optional[Sequence[int]] = None,
                      days: Optional[int] = None,
                      min_snapshots: int = 2,
                      group_by_variant: bool = True,
                      max_rows: Optional[int] = None
                      ) -> tuple[pd.DataFrame, PanelReport]:
    """Read the panel of per-snapshot market states.

    ``min_snapshots`` drops books that have been observed fewer than that
    many times.  A book with one snapshot cannot contribute a change row --
    the target needs a "before" and an "after" -- so keeping it would only
    inflate the row count without adding a label.  The number dropped is
    reported rather than swallowed: it is the difference between "the model
    has no signal" and "the fetcher has not run twice yet".
    """
    report = PanelReport()
    path = Path(intel_db)
    if not path.exists():
        report.notes.append(f"database not found: {path}")
        return pd.DataFrame(columns=list(PANEL_COLUMNS)), report

    con = sqlite3.connect(path)
    try:
        variant = group_by_variant and _has_variant_column(con)
        report.variants_pooled = not variant

        where = ["a.price_status = ?", "a.unit_price_copper > 0",
                 "s.realm <> ''"]
        args: List[object] = [OK_STATUS]
        if days:
            where.append("s.captured_at >= ?")
            args.append(int(pd.Timestamp.utcnow().timestamp()) - int(days) * 86400)
        if scopes:
            where.append("s.realm_slug IN (%s)" % ",".join("?" * len(scopes)))
            args.extend([str(s) for s in scopes])
        if item_ids:
            where.append("a.item_id IN (%s)" % ",".join("?" * len(item_ids)))
            args.extend([int(v) for v in item_ids])

        # Books observed once cannot produce a change; count them first so
        # the report can say how many were skipped and why.
        counted = con.execute(
            "SELECT s.connected_realm_id, COUNT(*) FROM ah_snapshot s"
            " WHERE s.realm <> '' GROUP BY s.connected_realm_id").fetchall()
        if min_snapshots > 1:
            keep = {int(cr) for cr, n in counted if n >= int(min_snapshots)}
            report.books_dropped_single_snapshot = len(counted) - len(keep)
            if not keep:
                # No book qualifies.  Returning the unfiltered rows here
                # would hand the caller a panel whose every group is a
                # single observation, which builds zero examples -- the
                # same emptiness, but arrived at silently.
                report.notes.append(
                    "no book has been observed twice; the panel is empty and"
                    " the risk targets cannot be built yet")
                return (pd.DataFrame(columns=list(PANEL_COLUMNS)), report)
            where.append("s.connected_realm_id IN (%s)"
                         % ",".join("?" * len(keep)))
            args.extend(sorted(keep))

        key = ("COALESCE(a.variant_key, 'item:' || a.item_id)"
               if variant else "'item:' || a.item_id")
        query = (
            "SELECT s.captured_at AS ts,"
            "       s.connected_realm_id AS book,"
            "       s.realm_slug AS stored_slug,"
            "       a.item_id AS item_id,"
            f"       {key} AS variant_key,"
            "       CAST(SUM(a.unit_price_copper * a.quantity) AS REAL)"
            "         / SUM(a.quantity) AS market,"
            "       MIN(a.unit_price_copper) AS min_buyout,"
            "       MIN(CASE WHEN a.time_left_band = 'SHORT'"
            "                THEN a.unit_price_copper END) AS recent,"
            "       COUNT(*) AS listings,"
            "       SUM(a.quantity) AS supply"
            "  FROM ah_snapshot s"
            "  JOIN ah_auction a ON a.snapshot_id = s.snapshot_id"
            " WHERE " + " AND ".join(where) +
            " GROUP BY s.snapshot_id, a.item_id"
            + (", a.variant_key" if variant else ""))
        if max_rows:
            query += " LIMIT %d" % int(max_rows)
        rows = con.execute(query, args).fetchall()
        slugs = _canonical_slugs(con)
    except sqlite3.OperationalError as exc:
        report.notes.append(f"panel unavailable: {exc}")
        return pd.DataFrame(columns=list(PANEL_COLUMNS)), report
    finally:
        con.close()

    if not rows:
        return pd.DataFrame(columns=list(PANEL_COLUMNS)), report

    frame = pd.DataFrame(rows, columns=[
        "ts", "book", "stored_slug", "item_id", "variant_key",
        "market", "min_buyout", "recent", "listings", "supply"])
    # The book decides the scope.  A stored slug is only a label, and it is
    # missing on older rows; falling back to it keeps an unmapped book usable
    # instead of dropping its rows.
    frame["scope"] = [
        slugs.get(int(book)) or (str(stored) if str(stored or "") else f"cr{int(book)}")
        for book, stored in zip(frame["book"], frame["stored_slug"])
    ]
    frame = frame[list(PANEL_COLUMNS)]
    frame["ts"] = frame["ts"].astype("int64")
    for column in ("market", "min_buyout", "recent", "listings", "supply"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.sort_values(["scope", "item_id", "ts"],
                              kind="mergesort").reset_index(drop=True)

    report.rows = int(len(frame))
    report.books = int(frame["scope"].nunique())
    report.items = int(frame["item_id"].nunique())
    report.snapshots = int(frame["ts"].nunique())
    report.span_hours = float((frame["ts"].max() - frame["ts"].min()) / 3600.0)
    return frame, report


def coverage_by_scope(panel: pd.DataFrame) -> pd.DataFrame:
    """Per-book observation counts.

    The single most useful diagnostic for this panel: a book with one row
    per item and a book with forty look identical in a total row count, and
    only one of them can train a temporal target.
    """
    if panel.empty:
        return pd.DataFrame(columns=["scope", "snapshots", "items", "rows",
                                     "span_hours"])
    grouped = panel.groupby("scope", sort=True)
    out = pd.DataFrame({
        "snapshots": grouped["ts"].nunique(),
        "items": grouped["item_id"].nunique(),
        "rows": grouped.size(),
        "span_hours": (grouped["ts"].max() - grouped["ts"].min()) / 3600.0,
    })
    return out.sort_values(["snapshots", "items"], ascending=False)
