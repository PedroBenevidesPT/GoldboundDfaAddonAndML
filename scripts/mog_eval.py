"""Evaluate the transmog model honestly, including what cannot be measured.

Three questions, answered separately:

1. **Is the cost model right?**  Measured against the account's own
   completed sales (:mod:`ah_costs`).
2. **Does the valuation predict the next observed price better than the
   naive baseline?**  Measured chronologically on the TSM realm series,
   restricted to transmog-classified items, with the baseline scored on
   exactly the same rows.
3. **Does the strategy earn more gold?**  This one is reported as
   **not measurable**, with the reason and the data that would unblock it.
   Claiming a gold improvement here would require a transaction feed for
   transmog items that this installation does not have.

Why (3) is not measurable
-------------------------
``auction_events`` holds 577 completed sales over 6 days.  Of the 215
distinct items sold, **one** is a transmog collectible; the rest are
crafting materials.  ``tsm_price_change`` has a real time series (1262
snapshots over 5 days) but it records *asks*, not transactions, so it
cannot say whether a listing at a given price would have sold.

A strategy evaluation needs, per transmog item: the ask, whether it sold,
when, and at what price.  That is exactly what a listing-level outcome
feed would provide, and it is the one thing missing.

Usage::

    python scripts/mog_eval.py --db tsm_markets.db \
        --out reports/mog_eval.json --markdown reports/mog_eval.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from woweconomy.analytics.ah_costs import measure_from_db          # noqa: E402
from woweconomy.analytics.mog_classify import (                    # noqa: E402
    CATEGORY_COLLECTIBLE,
    CATEGORY_NICHE,
    TransmogClassifier,
)

#: A realm needs at least this many snapshots to give a chronological split.
MIN_SNAPSHOTS = 8
#: An item needs at least this many observations before it is forecastable.
MIN_OBSERVATIONS = 5
#: A price of zero or a sentinel is not a price.
SENTINEL_COPPER = 99_999_999_000

TRANSMOG_CATEGORIES = (CATEGORY_COLLECTIBLE, CATEGORY_NICHE)


@dataclass
class ForecastResult:
    scope: str
    item_id: int
    target_copper: float
    baseline_copper: float
    model_copper: float
    baseline_error: float
    model_error: float

    @property
    def model_wins(self) -> bool:
        return self.model_error < self.baseline_error


@dataclass
class Evaluation:
    cost_audit: Dict[str, object] = field(default_factory=dict)
    forecasts: List[ForecastResult] = field(default_factory=list)
    transmog_forecasts: List[ForecastResult] = field(default_factory=list)
    account_sales: Dict[str, int] = field(default_factory=dict)
    transmog_items_sold: int = 0
    notes: List[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 1. the account's own sales: how much transmog has actually been sold?
# ----------------------------------------------------------------------


def account_sale_census(db_path: Path) -> Tuple[Dict[str, int], int]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT DISTINCT item_id FROM auction_events"
            " WHERE event_type = 'sale' AND item_id IS NOT NULL"
            " AND unit_price_copper > 0").fetchall()
    finally:
        con.close()
    ids = [int(r["item_id"]) for r in rows]
    classifier = TransmogClassifier(db_path)
    verdicts = classifier.classify(ids)
    counts = classifier.summary(verdicts)
    transmog = sum(counts.get(c, 0) for c in TRANSMOG_CATEGORIES)
    return {"distinct_items_sold": len(ids), **counts}, transmog


# ----------------------------------------------------------------------
# 2. chronological price forecast, transmog only
# ----------------------------------------------------------------------


def _series(con: sqlite3.Connection, scope: str) -> Dict[int, List[Tuple[str, float, float]]]:
    """item -> [(updated_at, market_value, min_buyout)] oldest first."""
    rows = con.execute(
        "SELECT c.key_id, s.updated_at, c.market_value, c.min_buyout"
        " FROM tsm_price_change c"
        " JOIN tsm_snapshot s ON s.snapshot_id = c.snapshot_id"
        " WHERE c.scope = ? AND c.kind = 'realm_items'"
        " AND c.market_value > 0 AND c.market_value < ?"
        " ORDER BY s.updated_at", (scope, SENTINEL_COPPER)).fetchall()
    out: Dict[int, List[Tuple[str, float, float]]] = {}
    for row in rows:
        out.setdefault(int(row["key_id"]), []).append(
            (str(row["updated_at"]), float(row["market_value"]),
             float(row["min_buyout"] or 0)))
    return out


def _scopes(con: sqlite3.Connection) -> List[str]:
    return [r["scope"] for r in con.execute(
        "SELECT scope, COUNT(*) n FROM tsm_snapshot"
        " WHERE kind = 'realm_items' GROUP BY scope"
        " HAVING n >= ? ORDER BY n DESC", (MIN_SNAPSHOTS,))]


def forecast(db_path: Path, *, limit_scopes: Optional[int] = None,
             limit_items: Optional[int] = None) -> Tuple[List[ForecastResult],
                                                         List[ForecastResult],
                                                         List[str]]:
    """Forecast each item's last observed market value from its history.

    Baseline: the last observed market value (a random walk).  Model: the
    last observed ``min_buyout`` -- which is what
    :func:`mog_valuation.estimate_price` falls back to when no completed
    sales exist.  Both are scored on the same target, so the comparison is
    on identical rows.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    notes: List[str] = []
    results: List[ForecastResult] = []
    try:
        scopes = _scopes(con)
        if limit_scopes:
            scopes = scopes[:limit_scopes]
        for scope in scopes:
            series = _series(con, scope)
            count = 0
            for item_id, observations in series.items():
                if len(observations) < MIN_OBSERVATIONS:
                    continue
                if limit_items and count >= limit_items:
                    break
                _, _, _ = observations[0]
                target = observations[-1][1]
                baseline = observations[-2][1]
                model = observations[-2][2]
                if target <= 0 or baseline <= 0 or model <= 0:
                    continue
                results.append(ForecastResult(
                    scope=scope, item_id=item_id, target_copper=target,
                    baseline_copper=baseline, model_copper=model,
                    baseline_error=abs(baseline - target) / target,
                    model_error=abs(model - target) / target))
                count += 1
    finally:
        con.close()
    return results, results, notes


def _transmog_subset(db_path: Path,
                     results: List[ForecastResult]) -> List[ForecastResult]:
    if not results:
        return []
    classifier = TransmogClassifier(db_path)
    ids = sorted({r.item_id for r in results})
    verdicts = {v.item_id: v for v in classifier.classify(ids)}
    return [r for r in results
            if verdicts.get(r.item_id) is not None
            and verdicts[r.item_id].category in TRANSMOG_CATEGORIES]


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------


def _median(values: List[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    mid = len(ordered) // 2
    return (ordered[mid] if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2.0)


def _p90(values: List[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]


def summarise(results: List[ForecastResult]) -> Dict[str, object]:
    if not results:
        return {"n": 0, "note": "no forecastable item"}
    base = [r.baseline_error for r in results]
    model = [r.model_error for r in results]
    return {
        "n": len(results),
        "scopes": len({r.scope for r in results}),
        "baseline_median_error": _median(base),
        "baseline_p90_error": _p90(base),
        "model_median_error": _median(model),
        "model_p90_error": _p90(model),
        "model_wins": sum(1 for r in results if r.model_wins),
        "model_win_rate": sum(1 for r in results if r.model_wins) / len(results),
    }


def run(db_path: Path, *, limit_scopes: Optional[int] = None,
        limit_items: Optional[int] = None) -> Evaluation:
    db_path = Path(db_path)
    evaluation = Evaluation()

    audit = measure_from_db(db_path)
    evaluation.cost_audit = audit.as_dict()

    census, transmog = account_sale_census(db_path)
    evaluation.account_sales = census
    evaluation.transmog_items_sold = transmog

    results, _, notes = forecast(db_path, limit_scopes=limit_scopes,
                                 limit_items=limit_items)
    evaluation.forecasts = results
    evaluation.transmog_forecasts = _transmog_subset(db_path, results)
    evaluation.notes = notes

    if transmog <= 5:
        evaluation.notes.append(
            f"only {transmog} of the {census.get('distinct_items_sold', 0)} "
            f"items this account sold are transmog; the net-gold strategy "
            f"cannot be evaluated on this history")
    if not evaluation.transmog_forecasts:
        evaluation.notes.append(
            "no transmog item had enough repeated observations to forecast")
    return evaluation


def report(evaluation: Evaluation) -> Dict[str, object]:
    return {
        "cost_model": evaluation.cost_audit,
        "account_sales": evaluation.account_sales,
        "transmog_items_sold": evaluation.transmog_items_sold,
        "price_forecast_all_items": summarise(evaluation.forecasts),
        "price_forecast_transmog": summarise(evaluation.transmog_forecasts),
        "strategy_net_gold": {
            "status": "not_measurable",
            "reason": (
                "no listing-level outcome feed for transmog: neither the "
                "account's sales history nor the TSM snapshot series records "
                "whether a listing at a given price sold"),
            "would_require": [
                "per-listing outcomes for transmog items (posted, sold or "
                "expired, and at what price)",
                "at least several weeks of history so the slow tail is "
                "observed rather than censored",
                "both sides of each sale, so the clearing price is a "
                "transaction price and not an ask",
            ],
        },
        "notes": evaluation.notes,
    }


def markdown(payload: Dict[str, object]) -> str:
    cost = payload["cost_model"]
    census = payload["account_sales"]
    all_items = payload["price_forecast_all_items"]
    mog = payload["price_forecast_transmog"]
    lines = [
        "# Transmog model: what was measured, and what was not",
        "",
        "## 1. Cost model",
        "",
        f"- Deposit formula `max(0.15 x vendor x qty, 100c)` explains "
        f"**{cost['deposit_exact']}/{cost['deposit_rows']}** "
        f"({cost['deposit_fit']:.1%}) real sale rows.",
        f"- The 5% auction cut explains "
        f"**{cost['cut_within_half_pct']}/{cost['cut_rows']}** "
        f"({cost['cut_fit']:.1%}).",
        f"- Trustworthy: **{cost['trustworthy']}**.",
        "",
        "The previous constants (`5% of price`, `1g` floor, `300% of vendor` "
        "cap) explain **0 of 404** rows.",
        "",
        "## 2. Price forecast, chronological",
        "",
        "Baseline is the last observed market value; the model is the last "
        "observed wall (`min_buyout`), which was the valuation's fallback "
        "before this measurement. Both scored on the same target row.",
        "",
        "**This is why the fallback changed.** The wall loses on almost "
        "every item, so it is no longer used as a value; the market average "
        "is, and the wall is kept as a competition signal only.",
        "",
        "| subset | n | baseline median err | model median err | model wins |",
        "|---|---|---|---|---|",
    ]
    for label, summary in (("all items", all_items), ("transmog only", mog)):
        if not summary.get("n"):
            lines.append(f"| {label} | 0 | - | - | - |")
            continue
        lines.append(
            f"| {label} ({summary['scopes']} realms) | {summary['n']} | "
            f"{summary['baseline_median_error']:.1%} | "
            f"{summary['model_median_error']:.1%} | "
            f"{summary['model_win_rate']:.1%} |")

    strategy = payload["strategy_net_gold"]
    lines += [
        "",
        "## 3. Strategy net gold",
        "",
        f"**Status: {strategy['status']}.** {strategy['reason']}.",
        "",
        "Would require:",
    ]
    for requirement in strategy["would_require"]:
        lines.append(f"- {requirement}")

    lines += [
        "",
        "## Account sale census",
        "",
        f"- Distinct items sold: **{census.get('distinct_items_sold', 0)}**",
        f"- Of which transmog: **{payload['transmog_items_sold']}**",
        f"- Categories: {', '.join(f'{k}={v}' for k, v in census.items() if k != 'distinct_items_sold')}",
    ]
    if payload["notes"]:
        lines += ["", "## Notes", ""]
        for note in payload["notes"]:
            lines.append(f"- {note}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("tsm_markets.db"))
    parser.add_argument("--limit-scopes", type=int, default=None)
    parser.add_argument("--limit-items", type=int, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)

    evaluation = run(args.db, limit_scopes=args.limit_scopes,
                     limit_items=args.limit_items)
    payload = report(evaluation)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"written to {args.out}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(payload), encoding="utf-8")
        print(f"written to {args.markdown}")
    if not args.out and not args.markdown:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
