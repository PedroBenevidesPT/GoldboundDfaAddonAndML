"""The posting plan for every transmog worth considering, kept current.

``mog_valuation`` answers "what is this worth", ``mog_strategy`` answers "post
it or not, and at what price".  Both were complete libraries with no caller, so
the answer to the second question existed only if somebody imported it.  This
module is the caller: it decides which items are in the mog market, gets a
valuation for each, turns the valuation into a :class:`StrategyInput`, and
persists the resulting :class:`PostingPlan` so there is always a plan to read.

Why a stored plan rather than computing on demand
-------------------------------------------------
The addon asks for a price while the player is standing at the auction house.
It cannot run a valuation across tens of thousands of items, and it must not
receive a number that was computed from a different snapshot than the one it
thinks it is looking at.  So the plan is built by the pipeline, stored with the
snapshot it was derived from, and read back.  A stale plan is visible as stale
because it carries its ``built_at`` and ``as_of_snapshot``.

The mog market is not the item market
-------------------------------------
Transmog sells on scarcity of *appearance*: a low sale rate is normal, the
deposit is the dominant cost, and the buyer is a collector completing a set.
That is a different market from materials, which turn over daily and where
undercutting is the whole game.  This module therefore restricts itself to
items the classifier calls transmog, and refuses to score anything whose
appearance data is missing -- "unknown" is a category, not a guess.

What is deliberately not invented
---------------------------------
``StrategyInput.p_sale`` is left at ``None`` unless a caller supplies a model
probability, so the probability comes from the measured regional turnover
(``sold_per_day``) and is labelled ``measured_rate``.  The strategy layer then
reports the price band it can defend and refuses the rest.  Nothing here
back-fits a price-response curve to make a higher ask look attractive.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from woweconomy.analytics.ah_costs import deposit_copper
from woweconomy.analytics.mog_classify import (
    CATEGORY_COLLECTIBLE,
    CATEGORY_NICHE,
    TransmogClassifier,
)
from woweconomy.analytics.mog_strategy import (
    ACTION_DO_NOT_POST,
    P_SOURCE_MODEL,
    StrategyInput,
    PostingPlan,
    evaluate,
)
from woweconomy.analytics.mog_valuation import estimate_price, load_signals
from woweconomy.dbcompat import connect_store, store_exists

logger = logging.getLogger(__name__)

#: The categories worth planning for.  ``plain`` items are common enough that
#: the deposit dominates and the strategy would refuse most of them anyway;
#: ``unknown`` has no appearance data and cannot be valued at all.
PLANNED_CATEGORIES = (CATEGORY_COLLECTIBLE, CATEGORY_NICHE)

#: How many items to plan per run when the caller does not say.  ``None``
#: means the whole region market, which is the honest default: the live store
#: holds 31,184 region prices and a cap of 20,000 silently planned two thirds
#: of the market while reporting itself as complete.  A full run costs about
#: 4 minutes, which a six-hourly task can afford.  A caller that wants a
#: bounded run passes a number and gets ``truncated`` set when it bites.
DEFAULT_LIMIT: Optional[int] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS mog_strategy_plan (
    item_id            INTEGER NOT NULL,
    built_at           TEXT    NOT NULL,
    as_of_snapshot     TEXT,
    name               TEXT    NOT NULL DEFAULT '',
    category           TEXT    NOT NULL DEFAULT 'unknown',
    action             TEXT    NOT NULL,
    price_unit_copper  REAL,
    estimate_copper    REAL,
    expected_net_copper REAL,
    p_sale             REAL,
    p_sale_source      TEXT    NOT NULL DEFAULT 'unavailable',
    horizon_days       REAL    NOT NULL DEFAULT 30.0,
    confidence         TEXT    NOT NULL DEFAULT 'none',
    observations       INTEGER NOT NULL DEFAULT 0,
    competition_copper REAL,
    competition_count  INTEGER NOT NULL DEFAULT 0,
    vendor_copper      REAL,
    reason             TEXT    NOT NULL DEFAULT '',
    flags              TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (item_id)
);
CREATE INDEX IF NOT EXISTS ix_mog_strategy_action
    ON mog_strategy_plan(action);
"""


def ensure_schema(con) -> None:
    """Create the plan table on SQLite; do nothing on Postgres.

    Alembic owns the Postgres schema (revision ``003_mog_strategy_plan``), and
    creating a table outside it would make the baseline fail the next time it
    runs against a fresh database -- ``pg_dump`` emits ``CREATE TABLE`` without
    ``IF NOT EXISTS``.  A SQLite workstation and the test suite have no
    migrations, so they get the table here.
    """
    from woweconomy.dbcompat import is_postgres

    if is_postgres():
        return
    con.executescript(SCHEMA)
    con.commit()


@dataclass(frozen=True)
class PlannedItem:
    """One plan, plus the inputs the stored row needs to stay auditable.

    The plan itself is the strategy layer's output and carries only what the
    decision needed.  The row also records the category, the estimate and the
    competition it was built against, so a reader can see *why* a price was
    chosen instead of having to re-run the valuation to find out.
    """

    plan: PostingPlan
    category: str
    estimate_copper: Optional[float]
    competition_copper: Optional[float]
    competition_count: int


@dataclass
class PlanReport:
    """What one planning run did."""

    plans: List[PlannedItem] = field(default_factory=list)
    categories: Dict[str, int] = field(default_factory=dict)
    considered: int = 0
    truncated: bool = False
    as_of_snapshot: Optional[str] = None
    elapsed_s: float = 0.0
    errors: List[str] = field(default_factory=list)

    def actions(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for item in self.plans:
            out[item.plan.action] = out.get(item.plan.action, 0) + 1
        return out

    def summary(self) -> str:
        lines = [
            f"strategy plans built in {self.elapsed_s:.1f}s",
            f"  considered {self.considered} mog item(s)"
            + ("  (LIMIT reached; raise --limit to cover the rest)"
               if self.truncated else ""),
            f"  as of snapshot {self.as_of_snapshot or 'unknown'}",
        ]
        if self.categories:
            lines.append("  categories: " + ", ".join(
                f"{k}={v}" for k, v in sorted(self.categories.items())))
        for action, count in sorted(self.actions().items(),
                                    key=lambda kv: -kv[1]):
            lines.append(f"  {action:16s} {count:>7,}")
        for err in self.errors:
            lines.append(f"  ERROR {err}")
        return "\n".join(lines)


def _latest_snapshot(con) -> Optional[str]:
    """The snapshot the plan is derived from, so a reader can tell it is stale."""
    try:
        row = con.execute(
            "SELECT MAX(snapshot_id) FROM ah_snapshot").fetchone()
    except Exception:  # pragma: no cover - no snapshot table
        return None
    return str(row[0]) if row and row[0] else None


def candidate_items(store_path: Path, *, limit: Optional[int] = None
                    ) -> List[int]:
    """The region-wide items worth planning, cheapest query first.

    ``tsm_region_price`` is the region market: one row per item, not per realm,
    which is the right grain for a plan because the *price* is regional even
    when the buyer is not.  Realm differences are applied later, by the export,
    from the realm factors -- not by inventing a per-realm plan here.

    ``limit`` of ``None`` reads the whole market; a number reads one more row
    than asked for, so the caller can tell "exactly this many" from "capped".
    """
    if not store_exists(store_path):
        return []
    con = connect_store(store_path, readonly=True)
    try:
        sql = ("SELECT key_id FROM tsm_region_price"
               " WHERE key_id IS NOT NULL AND key_id > 0"
               " ORDER BY key_id")
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit) + 1,)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [int(r[0]) for r in rows]


def build_plans(store_path: Path, *, intel_path: Optional[Path] = None,
                limit: Optional[int] = DEFAULT_LIMIT,
                categories: Sequence[str] = PLANNED_CATEGORIES,
                dfa_dir: Optional[Path] = None,
                now: Optional[int] = None,
                classifier: Optional[TransmogClassifier] = None
                ) -> PlanReport:
    """Plan every mog item the classifier recognises, and nothing else.

    ``classifier`` is an injection point, not a configuration: the real one
    needs the DB2 appearance import and the DfA demand cache, and a test that
    had to fabricate both would be testing its own fixtures rather than this
    function's decision about which verdicts become plans.
    """
    started = time.perf_counter()
    report = PlanReport()

    if not store_exists(store_path):
        report.errors.append(f"no market store at {store_path}")
        report.elapsed_s = time.perf_counter() - started
        return report

    ids = candidate_items(store_path, limit=limit)
    report.truncated = limit is not None and len(ids) > limit
    if limit is not None:
        ids = ids[:limit]
    if not ids:
        report.errors.append("the market store holds no region prices")
        report.elapsed_s = time.perf_counter() - started
        return report

    if classifier is None:
        classifier = TransmogClassifier(Path(store_path), dfa_dir=dfa_dir)
    verdicts = classifier.classify(ids)
    wanted = [v for v in verdicts if v.category in set(categories)]
    report.categories = classifier.summary(verdicts)
    report.considered = len(wanted)

    con = connect_store(store_path, readonly=True)
    try:
        report.as_of_snapshot = _latest_snapshot(con)
    finally:
        con.close()

    if not wanted:
        report.errors.append(
            "no item was classified as transmog; the DB2 appearance import "
            "or the DfA demand cache is missing, and an empty plan is the "
            "honest result rather than a plan of guesses")
        report.elapsed_s = time.perf_counter() - started
        return report

    signals = load_signals(Path(store_path), [v.item_id for v in wanted],
                           intel_db=Path(intel_path) if intel_path else None,
                           now=now)

    for verdict in wanted:
        signal = signals.get(verdict.item_id)
        if signal is None:
            continue
        estimate = estimate_price(signal)
        name = estimate.name or verdict.name or str(verdict.item_id)
        if estimate.unit_copper is None:
            # No price at all: the plan is still useful, and "do not post" with
            # the reason is the answer, not an omission.
            plan = evaluate(StrategyInput(
                item_id=verdict.item_id, name=name,
                estimate_unit_copper=None, confidence=estimate.confidence,
                observations=estimate.observations))
            report.plans.append(PlannedItem(
                plan=plan, category=verdict.category, estimate_copper=None,
                competition_copper=signal.competition_copper,
                competition_count=int(signal.competition_count or 0)))
            continue

        quantity = 1
        plan = evaluate(StrategyInput(
            item_id=verdict.item_id,
            name=name,
            estimate_unit_copper=estimate.unit_copper,
            confidence=estimate.confidence,
            observations=estimate.observations,
            quantity=quantity,
            sold_per_day=signal.sold_per_day,
            competition_unit_copper=signal.competition_copper,
            competition_count=int(signal.competition_count or 0),
            vendor_unit_copper=signal.vendor_copper,
            # The deposit is 15% of the *vendor* value, not of the price we
            # hope to get: the game charges it on what the item is worth to a
            # vendor, so passing the estimate here would overstate the cost of
            # a patient listing and bias the plan towards undercutting.
            deposit_copper=deposit_copper(signal.vendor_copper, quantity),
        ))
        report.plans.append(PlannedItem(
            plan=plan, category=verdict.category,
            estimate_copper=float(estimate.unit_copper),
            competition_copper=signal.competition_copper,
            competition_count=int(signal.competition_count or 0)))

    report.elapsed_s = time.perf_counter() - started
    return report


def _flag_text(plan: PostingPlan) -> str:
    return "|".join(plan.flags)


def store_plans(store_path: Path, report: PlanReport,
                *, built_at: Optional[str] = None) -> int:
    """Replace the stored plan set with this run's.

    The table holds *the current plan*, not a history: a row that is no longer
    planned must disappear, or the addon would keep showing a recommendation
    the pipeline has stopped making.  History lives in the evaluation tables,
    which is where a claim about the past belongs.
    """
    if not report.plans:
        return 0
    stamp = built_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    con = connect_store(store_path)
    try:
        ensure_schema(con)
        con.execute("DELETE FROM mog_strategy_plan")
        rows = [(
            int(item.plan.item_id), stamp, report.as_of_snapshot,
            item.plan.name, item.category, item.plan.action,
            item.plan.price_unit_copper, item.estimate_copper,
            item.plan.expected_net_copper, item.plan.p_sale,
            item.plan.p_sale_source, float(item.plan.horizon_days),
            item.plan.confidence, int(item.plan.observations),
            item.competition_copper, int(item.competition_count),
            item.plan.vendor_net_copper, item.plan.reason,
            _flag_text(item.plan),
        ) for item in report.plans]
        con.executemany(
            "INSERT OR REPLACE INTO mog_strategy_plan"
            " (item_id, built_at, as_of_snapshot, name, category, action,"
            "  price_unit_copper, estimate_copper, expected_net_copper,"
            "  p_sale, p_sale_source, horizon_days, confidence, observations,"
            "  competition_copper, competition_count, vendor_copper, reason,"
            "  flags)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()
    return len(rows)


def read_plans(store_path: Path, *, action: Optional[str] = None,
               limit: Optional[int] = None) -> List[dict]:
    """The stored plan, newest fields first, as plain dictionaries.

    A store that has never been planned has no table, which is an empty plan
    rather than an error: the caller is asking "what is the plan", and the
    honest answer before the first build is "none yet".
    """
    if not store_exists(store_path):
        return []
    con = connect_store(store_path, readonly=True)
    try:
        if not con.execute("PRAGMA table_info(mog_strategy_plan)").fetchall():
            return []
        sql = ("SELECT item_id, built_at, as_of_snapshot, name, category,"
               "       action, price_unit_copper, estimate_copper,"
               "       expected_net_copper, p_sale, p_sale_source,"
               "       confidence, observations, reason, flags"
               "  FROM mog_strategy_plan")
        params: List[object] = []
        if action:
            sql += " WHERE action = ?"
            params.append(action)
        sql += " ORDER BY expected_net_copper IS NULL, expected_net_copper DESC,"
        sql += " item_id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = con.execute(sql, tuple(params)).fetchall()
    finally:
        con.close()
    keys = ("item_id", "built_at", "as_of_snapshot", "name", "category",
            "action", "price_unit_copper", "estimate_copper",
            "expected_net_copper", "p_sale", "p_sale_source", "confidence",
            "observations", "reason", "flags")
    return [dict(zip(keys, row)) for row in rows]
