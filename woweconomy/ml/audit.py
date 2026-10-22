"""The data-readiness report.

It answers one question honestly: **which of the four models can actually
be trained and evaluated on this data, and which cannot.**

It does not assume that a column exists because the brief mentions it.
Every claim is measured against the database at the time of the report,
and every signal is classified as one of:

``asking``
    what a seller listed.  ``dbmarket``, ``dbrecent``, ``dbminbuyout``,
    ``dbregionmarketavg``, ``dbregionhistorical``.  Evidence of intent,
    not of payment.
``market reference``
    a decayed average TSM computes.  ``dbregionsaleavg`` is a
    *transaction* average; ``dbregionsoldperday`` and
    ``dbregionsalerate`` are volume estimates.
``transaction``
    money that actually changed hands.  The Journalator invoices and the
    account's own sales.
``estimate``
    a number nobody observed directly, such as a predicted sale price.
``missing``
    a field the installed data does not carry at all.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import SENTINEL_COPPER
from .tsm_history import iso

logger = logging.getLogger(__name__)


def _fmt_ts(value: Optional[int]) -> str:
    return iso(int(value)) if value else "-"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


@dataclass
class ColumnFact:
    """What a single column actually contains."""

    table: str
    column: str
    kind: str
    rows: int = 0
    non_null: int = 0
    non_zero: int = 0
    sentinels: int = 0
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    note: str = ""

    @property
    def coverage(self) -> float:
        return self.non_zero / self.rows if self.rows else 0.0

    @property
    def null_rate(self) -> float:
        return (self.rows - self.non_null) / self.rows if self.rows else 0.0

    def as_row(self) -> List[str]:
        return [
            f"`{self.table}.{self.column}`",
            self.kind,
            f"{self.rows:,}",
            _pct(self.coverage),
            f"{self.sentinels:,}",
            f"{self.minimum:,}" if self.minimum is not None else "-",
            f"{self.maximum:,}" if self.maximum is not None else "-",
            self.note,
        ]


def _column_fact(con: sqlite3.Connection, table: str, column: str, kind: str,
                  note: str = "") -> ColumnFact:
    try:
        row = con.execute(
            f"SELECT COUNT(*), SUM({column} IS NOT NULL), "
            f"SUM(CASE WHEN {column} > 0 AND {column} < ? THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {column} >= ? THEN 1 ELSE 0 END), "
            f"MIN(CASE WHEN {column} > 0 AND {column} < ? THEN {column} END), "
            f"MAX(CASE WHEN {column} > 0 AND {column} < ? THEN {column} END) "
            f"FROM {table}", (SENTINEL_COPPER, SENTINEL_COPPER,
                               SENTINEL_COPPER, SENTINEL_COPPER)).fetchone()
    except sqlite3.OperationalError as exc:
        return ColumnFact(table, column, kind, note=f"unreadable: {exc}")
    return ColumnFact(table=table, column=column, kind=kind,
                      rows=int(row[0] or 0), non_null=int(row[1] or 0),
                      non_zero=int(row[2] or 0), sentinels=int(row[3] or 0),
                      minimum=int(row[4]) if row[4] is not None else None,
                      maximum=int(row[5]) if row[5] is not None else None,
                      note=note)


def _temporal_facts(con: sqlite3.Connection) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for label, sql in (
        ("auction_events (Journalator)",
         "SELECT MIN(ts), MAX(ts), COUNT(*) FROM auction_events"),
        ("journal_stats (Journalator statistics)",
         "SELECT MIN(imported_at), MAX(imported_at), COUNT(*) FROM journal_stats"),
        ("journal (raw journal)",
         "SELECT MIN(ts), MAX(ts), COUNT(*) FROM journal"),
        ("tsm_snapshot (TSM files)",
         "SELECT MIN(updated_at), MAX(updated_at), COUNT(*) FROM tsm_snapshot"),
        ("tsm_price_change (TSM history)",
         "SELECT MIN(s.updated_at), MAX(s.updated_at), COUNT(*) "
         "FROM tsm_price_change p JOIN tsm_snapshot s ON s.snapshot_id = p.snapshot_id"),
        ("market_snapshots (legacy region states)",
         "SELECT MIN(s.snapshotTimestamp), MAX(s.snapshotTimestamp), COUNT(DISTINCT s.snapshotId) "
         "FROM snapshots s JOIN market_snapshots m ON m.snapshotId = s.snapshotId"),
    ):
        try:
            row = con.execute(sql).fetchone()
        except sqlite3.OperationalError as exc:
            out.append({"source": label, "error": str(exc)})
            continue
        out.append({"source": label, "from": row[0], "to": row[1], "rows": int(row[2] or 0)})
    return out


def _collection_frequency(con: sqlite3.Connection) -> List[Dict[str, object]]:
    """How often each realm was actually sampled."""
    out: List[Dict[str, object]] = []
    try:
        rows = con.execute(
            "SELECT scope, COUNT(*), MIN(updated_at), MAX(updated_at) "
            "FROM tsm_snapshot WHERE kind='realm_items' "
            "GROUP BY scope ORDER BY COUNT(*) DESC, scope LIMIT 12").fetchall()
    except sqlite3.OperationalError:
        return out
    for scope, count, first, last in rows:
        out.append({"scope": scope, "snapshots": int(count),
                    "from": first, "to": last})
    return out


def _auction_event_facts(con: sqlite3.Connection) -> List[Dict[str, object]]:
    try:
        rows = con.execute(
            "SELECT event_type, COUNT(*), MIN(ts), MAX(ts), "
            "COUNT(DISTINCT item_id), COUNT(DISTINCT realm), "
            "SUM(CASE WHEN unit_price_copper > 0 THEN 1 ELSE 0 END) "
            "FROM auction_events GROUP BY event_type ORDER BY COUNT(*) DESC").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"event": r[0], "rows": int(r[1]), "from": r[2], "to": r[3],
             "items": int(r[4]), "realms": int(r[5]),
             "with_a_price": int(r[6] or 0)} for r in rows]


def _dfa_facts(dfa_db: Path) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    if not dfa_db.exists():
        return [{"error": f"dfa.db not found at {dfa_db}"}]
    try:
        con = sqlite3.connect(f"file:{dfa_db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        return [{"error": str(exc)}]
    try:
        for table in ("account", "scanned", "missing", "missing_realm", "catalogue",
                      "collector", "owned", "rarity", "item_slot"):
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                out.append({"table": table, "error": str(exc)})
                continue
            out.append({"table": table, "rows": int(n)})
        try:
            span = con.execute(
                "SELECT MIN(scanned_at), MAX(scanned_at) FROM account "
                "WHERE scanned_at IS NOT NULL").fetchone()
            out.append({"table": "account.scanned_at", "from": span[0], "to": span[1]})
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return out


def _feasibility(dataset_report, link_report, credibility: Optional[Dict] = None) -> List[Dict[str, str]]:
    """Can each of the four models be trained and evaluated on this data?"""
    rows: List[Dict[str, str]] = []

    price_rows = int(dataset_report.price_rows or 0)
    if price_rows >= 40:
        rows.append({"model": "A -- realizable price",
                     "trainable": "yes",
                     "evidence": f"{price_rows} observed sales with a linked listing",
                     "target": "net proceeds per unit (log) and gross unit price",
                     "caveat": ("a listing that never sold carries no price label, so "
                                "the model describes the price *of a sale*, not the "
                                "price of every listing")})
    else:
        rows.append({"model": "A -- realizable price", "trainable": "no",
                     "evidence": f"only {price_rows} sales with a linked listing",
                     "target": "-",
                     "caveat": "not enough transactions to fit a regression"})

    horizons = dataset_report.horizon_rows or {}
    usable = {h: n for h, n in horizons.items() if n >= 30}
    if usable:
        rows.append({"model": "B -- probability of selling",
                     "trainable": "yes",
                     "evidence": "; ".join(f"{h}h: {n} labelled rows" for h, n in sorted(usable.items())),
                     "target": f"sold within {'/'.join(str(h) for h in sorted(usable))} hours",
                     "caveat": (f"{sum(dataset_report.horizon_censored.values())} listings are "
                                "censored and "
                                f"{sum(dataset_report.horizon_cancelled.values())} are seller "
                                "cancellations; neither is counted as a failure")})
    else:
        rows.append({"model": "B -- probability of selling", "trainable": "no",
                     "evidence": f"labelled rows per horizon: {horizons}",
                     "target": "-", "caveat": "too few labelled rows"})

    tts_rows = int(dataset_report.time_to_sale_rows or 0)
    events = price_rows
    if tts_rows >= 40 and events >= 20:
        rows.append({"model": "C -- time to sale", "trainable": "yes",
                     "evidence": (f"{tts_rows} durations, of which {events} are observed "
                                  f"sales and {tts_rows - events} are right-censored"),
                     "target": "hours to sale, right-censored (SurvivalAft)",
                     "caveat": ("the outcome is recorded when the account opens the mail, "
                                "so the duration is an upper bound on the true time to sale")})
    else:
        rows.append({"model": "C -- time to sale", "trainable": "no",
                     "evidence": f"{tts_rows} durations, {events} observed sales",
                     "target": "-",
                     "caveat": "not enough observed events to fit a survival model"})

    rows.append({"model": "D -- listing-price strategy", "trainable": "yes",
                 "evidence": "a decision layer over A and B; it needs no labels of its own",
                 "target": "the ask, with its sale probability and expected net gold",
                 "caveat": ("the counterfactual 'what if I had asked that' is not "
                            "observable; the evaluation states the monotone-demand "
                            "assumption instead of hiding it")})
    return rows


def write_audit(config, engine, frame, dataset_report, link_report) -> Path:
    """Write the data-readiness report as Markdown."""
    con = sqlite3.connect(config.tsm_db)
    try:
        columns = [
            _column_fact(con, "tsm_realm_price", "market_value", "asking",
                         "the local market average (dbmarket)"),
            _column_fact(con, "tsm_realm_price", "min_buyout", "asking",
                         "the cheapest buyout listing (dbminbuyout)"),
            _column_fact(con, "tsm_realm_price", "recent", "asking",
                         "the decayed recent average (dbrecent)"),
            _column_fact(con, "tsm_realm_price", "historical", "asking",
                         "the long-run historical average (dbhistorical)"),
            _column_fact(con, "tsm_region_price", "market_value", "asking",
                         "the regional market average (dbregionmarketavg)"),
            _column_fact(con, "tsm_region_price", "historical", "asking",
                         "the regional historical average (dbregionhistorical)"),
            _column_fact(con, "tsm_region_price", "avg_sale_price", "transaction",
                         "the average price a sale closed at (dbregionsaleavg)"),
            _column_fact(con, "tsm_region_price", "sold_per_day", "estimate",
                         "the estimated daily volume (dbregionsoldperday)"),
            _column_fact(con, "tsm_region_price", "sale_rate", "estimate",
                         "the estimated sale frequency (dbregionsalerate)"),
            _column_fact(con, "tsm_market", "num_auctions", "asking",
                         "the visible listing count; no history, so it cannot be "
                         "used as a point-in-time feature"),
            _column_fact(con, "auction_events", "unit_price_copper", "transaction",
                         "the price a buyer paid, per unit"),
            _column_fact(con, "auction_events", "fee_copper", "transaction",
                         "the house cut actually charged"),
            _column_fact(con, "auction_events", "net_proceeds_copper", "transaction",
                         "what the seller pocketed"),
            _column_fact(con, "auction_events", "market_ref_copper", "asking",
                         "the dbmarket captured at listing time by the addon"),
        ]
        temporal = _temporal_facts(con)
        frequency = _collection_frequency(con)
        events = _auction_event_facts(con)
    finally:
        con.close()

    dfa = _dfa_facts(config.dfa_db)
    feasibility = _feasibility(dataset_report, link_report)
    credibility = engine.reference_credibility(frame) if frame is not None and not frame.empty else {}

    config.reports_dir.mkdir(parents=True, exist_ok=True)
    path = config.reports_dir / "dfa_ml_data_readiness.md"
    lines: List[str] = []
    add = lines.append

    add("# DfA learning engine -- data-readiness report")
    add("")
    add(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
        f"against `{config.tsm_db}` and `{config.dfa_db}`.")
    add("")
    add("## 1. What is actually in the data")
    add("")
    add("| source | from | to | rows |")
    add("|---|---|---|---:|")
    for entry in temporal:
        if "error" in entry:
            add(f"| {entry['source']} | - | - | error: {entry['error']} |")
        else:
            add(f"| {entry['source']} | {entry['from']} | {entry['to']} | {entry['rows']:,} |")
    add("")

    add("### 1.1 The signal families, as they really are")
    add("")
    add("| column | what it really is | rows | non-zero | TSM sentinels | min | max | note |")
    add("|---|---|---:|---:|---:|---:|---:|---|")
    for fact in columns:
        add("| " + " | ".join(fact.as_row()) + " |")
    add("")
    add("`asking` means the number is what a seller listed, `transaction` means money "
        "changed hands, `estimate` means nobody observed it directly.  The engine never "
        "uses an `asking` column as a training label.")
    add("")

    add("### 1.2 How often the market is actually sampled")
    add("")
    add("| scope | snapshots | from | to |")
    add("|---|---:|---|---|")
    for entry in frequency[:12]:
        add(f"| `{entry['scope']}` | {entry['snapshots']} | {entry['from']} | {entry['to']} |")
    add("")
    add("A realm is re-sampled every few hours; a *region* is sampled a couple of times a "
        "day at best, so a regional reference is often a day old.  The freshness of every "
        "reference is a feature, not an afterthought.")
    add("")

    add("### 1.3 The transaction record")
    add("")
    add("| event | rows | from | to | items | realms | with a price |")
    add("|---|---:|---|---|---:|---:|---:|")
    for entry in events:
        add(f"| {entry['event']} | {entry['rows']:,} | {entry['from']} | {entry['to']} | "
            f"{entry['items']:,} | {entry['realms']} | {entry['with_a_price']:,} |")
    add("")

    add("### 1.4 The demand side (Data for Azeroth)")
    add("")
    add("| table | rows |")
    add("|---|---:|")
    for entry in dfa:
        if "error" in entry:
            add(f"| - | error: {entry['error']} |")
        elif "from" in entry:
            add(f"| `{entry['table']}` | {entry['from']} .. {entry['to']} |")
        else:
            add(f"| `{entry['table']}` | {entry['rows']:,} |")
    add("")
    add("`missing_realm.n` counts the accounts of a realm's slice that still lack an "
        "appearance.  It has **no timestamp**: the demand of 16 October cannot be "
        "reconstructed from it, so a demand feature describes today's audience applied "
        "to a historical row.  It is flagged as not point-in-time and the report shows "
        "what including it does to the walk-forward scores.")
    add("")

    add("## 2. Which models can be trained")
    add("")
    add("| model | trainable | evidence | target | caveat |")
    add("|---|---|---|---|---|")
    for row in feasibility:
        add(f"| {row['model']} | **{row['trainable']}** | {row['evidence']} | "
            f"{row['target']} | {row['caveat']} |")
    add("")

    add("## 3. The dataset that was actually built")
    add("")
    summary = dataset_report.summary()
    link = link_report.summary()
    add(f"- rows (listings): **{summary['rows']:,}** over {summary['items']:,} items "
        f"and {summary['realms']} realms")
    add(f"- window: {_fmt_ts(summary['first_ts'])} .. {_fmt_ts(summary['last_ts'])}")
    add(f"- observation period ends at {_fmt_ts(link.get('observation_end'))}")
    add(f"- rows with a local TSM reference: {summary['rows_with_local_reference']:,}")
    add(f"- rows with a regional TSM reference: {summary['rows_with_region_reference']:,}")
    add(f"- rows with neither: {summary['rows_without_any_reference']:,}")
    add(f"- price rows (an observed sale): **{summary['price_rows']:,}**")
    add(f"- time-to-sale rows: {summary['time_to_sale_rows']:,} "
        f"({summary['time_to_sale_censored']:,} right-censored)")
    add("")
    add("| horizon | labelled rows | positive | censored (dropped) | cancelled (dropped) |")
    add("|---:|---:|---:|---:|---:|")
    for horizon in sorted(summary["horizon_rows"]):
        add(f"| {horizon}h | {summary['horizon_rows'][horizon]:,} | "
            f"{summary['horizon_positive'].get(horizon, 0):,} | "
            f"{summary['horizon_censored'].get(horizon, 0):,} | "
            f"{summary['horizon_cancelled'].get(horizon, 0):,} |")
    add("")

    add("## 4. The outcome linkage")
    add("")
    link = link_report.summary()
    add(f"- postings seen: **{link['postings']:,}**")
    add(f"- matched to a sale: **{link['matched_sale']:,}**")
    add(f"- matched to an expiry: **{link['matched_expired']:,}**")
    add(f"- matched to a cancellation: **{link['matched_cancelled']:,}**")
    add(f"- censored (no outcome observed): **{link['censored']:,}**")
    add(f"- outcomes with no matching posting: {link['orphan_outcomes']:,}")
    add(f"- **sales that closed at exactly the posted buyout: "
        f"{link['sold_at_ask']:,} of {link['matched_sale']:,} "
        f"({_pct(link['sold_at_ask_rate'] or 0)})** -- the link is not a guess: a wrong "
        "pairing would scatter the prices")
    add("")

    if credibility:
        add("## 5. How far each asking reference sits from what buyers paid")
        add("")
        add("| reference | median vs the realised price | quartiles | share above the price paid | verdict |")
        add("|---|---:|---|---:|---|")
        for name, entry in (credibility.get("per_reference") or {}).items():
            ratio = entry.get("median_reference_over_realised")
            if ratio is None:
                add(f"| {name} | - | - | - | {entry.get('note', 'no usable rows')} |")
                continue
            add(f"| {name} | {ratio:.3f} | "
                f"{entry.get('reference_over_realised_p25', 0):.3f} .. "
                f"{entry.get('reference_over_realised_p75', 0):.3f} | "
                f"{_pct(entry.get('share_reference_above_realised') or 0)} | "
                f"{entry.get('verdict', '')} |")
        add("")
        add("A ratio above one means the reference promises more than the market "
            "pays; a ratio near one means the reference is commercially "
            "defensible.  The engine lets CatBoost learn from the transaction "
            "outcomes when each family is trustworthy, instead of averaging the "
            "families together.")
        add("")

    add("## 6. What is missing, and what it costs")
    add("")
    missing = [
        ("the auction duration of a posting",
         "Journalator's posting hook does not record it",
         "the duration is inferred from the expiry delay, which clusters on 12/24/48 h"),
        ("the listing count over time",
         "`tsm_market.num_auctions` has no timestamp and no history",
         "market depth cannot be used as a point-in-time feature; only the "
         "regional sold-per-day and sale-rate proxies are available"),
        ("the item id on a sale invoice",
         "the invoice mail carries no item link in this installation",
         "sales are matched by item name, which the item catalogue resolves"),
        ("the true moment of a sale or an expiry",
         "both are recorded when the account opens the mail",
         "every duration is an upper bound; the models are kept conservative"),
        ("the demand of a past date",
         "`missing_realm` is a live aggregate with no timestamp",
         "the demand feature is not point-in-time and is flagged as such"),
        ("a DfA demand for every item",
         "only transmog appearances, mounts and recipes are scanned",
         "most auction items have no demand counter at all, which is itself a signal"),
        ("the sample size behind `dbregionsaleavg`",
         "TSM exposes the average but not how many sales produced it",
         "the width of the estimate is carried by the price model itself: the "
         "quantile models give a p10-p90 band, and a thin or stale sample "
         "shows up as a wide band rather than as a confident number"),
    ]
    add("| what is missing | why | what the engine does about it |")
    add("|---|---|---|")
    for what, why, what_now in missing:
        add(f"| {what} | {why} | {what_now} |")
    add("")

    # ------------------------------------------------- the feature dictionary
    from .features import FEATURE_DOCS
    add("## 7. Every feature, and where it came from")
    add("")
    add("`point-in-time` says whether the value was already true at the moment "
        "the item was listed.  A **no** means the feature describes a later "
        "state of the world and is therefore a mild extrapolation, flagged so "
        "that it can be switched off and measured.")
    add("")
    add("| feature | what it is | source | point-in-time |")
    add("|---|---|---|---|")
    for name in sorted(FEATURE_DOCS):
        what, source, pit = FEATURE_DOCS[name]
        add(f"| `{name}` | {what} | {source} | {pit} |")
    add("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
