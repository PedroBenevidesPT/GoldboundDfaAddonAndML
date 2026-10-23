"""The recommendation report: what the engine would say, and why.

Every number in the report is a number the engine can defend: the
estimate, the confidence, the sale probability, the evidence and the
warnings.  Where the engine has nothing to say, the report says that
instead of leaving a blank.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import PricingConfig
from .dataset import DatasetBuilder
from .engine import PricingEngine


def _mirror_facts(config: PricingConfig) -> Dict[str, object]:
    """What the TSM mirror actually holds, and what the engine read.

    This exists because "is it loading the snapshots?" is a question the
    export used to leave unanswered.  The counts are read from the same
    database the engine reads, so the report cannot claim a coverage the
    build did not have.
    """
    facts: Dict[str, object] = {}
    try:
        con = sqlite3.connect(f"file:{config.tsm_db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    try:
        facts["snapshots"] = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT scope) FROM tsm_snapshot").fetchone()
        facts["kinds"] = con.execute(
            "SELECT kind, COUNT(*), COUNT(DISTINCT scope) FROM tsm_snapshot "
            "GROUP BY kind ORDER BY kind").fetchall()
        facts["price_changes"] = con.execute(
            "SELECT COUNT(*) FROM tsm_price_change").fetchone()[0]
        facts["realm_prices"] = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT scope) FROM tsm_realm_price").fetchone()
        facts["region_prices"] = con.execute(
            "SELECT COUNT(*) FROM tsm_region_price").fetchone()[0]
        facts["region_latest"] = con.execute(
            "SELECT MAX(updated_at) FROM tsm_snapshot WHERE kind='region_items'"
        ).fetchone()[0]
        facts["event_realms"] = con.execute(
            "SELECT COUNT(DISTINCT realm) FROM auction_events WHERE realm <> ''"
        ).fetchone()[0]
        facts["event_scopes"] = con.execute(
            "SELECT COUNT(DISTINCT g.canonical_slug) FROM auction_events e "
            "JOIN tsm_realm_group g ON g.realm_name = e.realm "
            "WHERE e.realm <> ''").fetchone()[0]
    except sqlite3.Error as exc:
        facts["error"] = str(exc)
    finally:
        con.close()
    return facts


def _copper(value: Optional[int]) -> str:
    if not value:
        return "-"
    return f"{value / 10000:,.1f}g"


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%"


def write_report(config: PricingConfig, engine: PricingEngine,
                 builder: DatasetBuilder) -> Path:
    """Write the recommendations report as Markdown."""
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    path = config.reports_dir / "dfa_pricing_report.md"
    meta = builder.metadata()
    lines: List[str] = []
    add = lines.append

    add("# DfA pricing -- recommendations, evidence and confidence")
    add("")
    add(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}.  "
        f"Dataset schema {meta['version']}, engine {meta['engineVersion']}, "
        f"generated at {meta['generatedAt']}.")
    add("")
    add("**This is analysis, not action.**  Nothing here posts, cancels, "
        "undercuts or reprices an auction.  The engine answers questions; "
        "a human decides.")
    add("")

    add("## The dataset")
    add("")
    add(f"- {meta['realmCount']} realms, {meta['itemCount']} (realm, item) records, "
        f"{meta['appearanceCount']} appearances")
    add(f"- {meta['totalObservations']:,} observed transactions behind it, "
        f"between {meta['minSampleSize']} and {meta['maxSampleSize']} per record")
    add(f"- mean confidence {_pct(meta['meanConfidence'])}, "
        f"horizon {meta['horizonHours']}h")
    add(f"- the model prices **{len(builder.region_prices):,}** items from the "
        f"regional references, in every realm the dataset maps "
        f"(**{len(builder.realm_groups)}** realm names over "
        f"**{len(set(builder.realm_groups.values()))}** auction houses); the "
        f"per-realm records cover only the {meta['itemCount']} (realm, item) "
        f"pairs the account traded")
    add(f"- checksum `{builder.checksum()}`")
    if builder.notes:
        add("")
        add("Notes from the build:")
        for note in builder.notes:
            add(f"- {note}")
    add("")

    interval = getattr(engine, "price_interval", None) or {}
    shipped = getattr(engine, "shipped_model", None)
    if interval or (shipped is not None and getattr(shipped, "interval_half_width", 0) > 0):
        add("## Calibrated price intervals")
        add("")
        add("Every exported price carries a split-conformal interval.  The "
            "width is calibrated on one block and the coverage is measured "
            "on a later block, so the two numbers are separate claims.")
        add("")
        add("| model | nominal level | half-width (log) | measured coverage | held-out sales |")
        add("|---|---:|---:|---:|---:|")
        if interval:
            coverage = interval.get("coverage")
            add(f"| record price (CatBoost) | {_pct(interval.get('level'))} | "
                f"{float(interval.get('half_width_log') or 0):.3f} | "
                f"{_pct(coverage) if coverage is not None else 'unmeasured'} | "
                f"{int(interval.get('test_n') or 0)} |")
        if shipped is not None and getattr(shipped, "interval_half_width", 0) > 0:
            add(f"| shipped model | {_pct(shipped.interval_level)} | "
                f"{shipped.interval_half_width:.3f} | "
                f"{_pct(shipped.interval_coverage)} | {shipped.interval_n} |")
        sentinel = next((r.estimate for r in builder.records
                         if r.estimate.sale_price_interval_level), None)
        if sentinel is not None:
            width = 0.0
            if (sentinel.sale_price_low and sentinel.sale_price_high
                    and sentinel.sale_price_high > sentinel.sale_price_low):
                width = math.log(sentinel.sale_price_high
                                 / sentinel.sale_price_low) / 2.0
            add(f"| Sentinel realized price | {_pct(sentinel.sale_price_interval_level)} | "
                f"{width:.3f} | "
                f"{_pct(sentinel.sale_price_interval_coverage)} | "
                f"{sentinel.sale_price_interval_n} |")
        add("")
    add("## What was loaded from the TSM mirror")
    add("")
    facts = _mirror_facts(config)
    if "error" in facts:
        add(f"The mirror could not be read: {facts['error']}")
    else:
        snapshots, scopes = facts["snapshots"]
        add("| what | value |")
        add("|---|---:|")
        add(f"| TSM snapshots | **{snapshots:,}** over **{scopes}** scopes |")
        for kind, count, kind_scopes in facts["kinds"]:
            add(f"| `{kind}` | {count:,} over {kind_scopes} scope(s) |")
        add(f"| price changes (the point-in-time history) | {facts['price_changes']:,} |")
        realm_rows, realm_scopes = facts["realm_prices"]
        add(f"| realm prices | {realm_rows:,} over {realm_scopes} scopes |")
        add(f"| regional prices | {facts['region_prices']:,} |")
        if facts["region_latest"]:
            add(f"| latest regional snapshot | {facts['region_latest']} |")
        add(f"| realms with transactions (the training labels) | {facts['event_realms']} "
            f"-> **{facts['event_scopes']}** auction-house scopes |")
        add("")
        add("The learning engine loads only the scopes that appear in "
            "`auction_events`, because those are the only realms with outcomes to "
            "learn from; the region is always loaded.  The pricing engine loads "
            "the scopes of the realms with a record (the only ones a per-realm "
            "record can use) plus the region, and reads the realm -> auction-house "
            "map whole.  The shipped model uses the **regional** references, which "
            "are the same in every realm -- that is why it can answer for items "
            "the account never traded, and why the per-realm records are the "
            "exception and not the coverage.")
    add("")
    add("## The realms")
    add("")
    add("| realm | auction house | records | own sales | newest observation |")
    add("|---|---|---:|---:|---|")
    for realm in sorted(builder.realms):
        block = builder.realms[realm]
        items = block.get("items", {})
        newest = block.get("updatedAt") or 0
        stamp = (datetime.fromtimestamp(int(newest), tz=timezone.utc)
                 .strftime("%Y-%m-%d %H:%M") if newest else "-")
        add(f"| {realm} | `{block.get('auctionHouse') or '-'}` | {len(items)} | "
            f"{int(block.get('sampleSize') or 0)} | {stamp} |")
    add("")

    add("## A sample of the recommendations")
    add("")
    add("The items with the most evidence first.  Every row is one item in "
        "one realm: the estimate, how much it is worth, the chance of "
        "selling, and the warnings.")
    add("")

    records = sorted(builder.records,
                     key=lambda r: (-r.estimate.sample_size, -r.estimate.confidence))
    for record in records[:25]:
        e = record.estimate
        add(f"### {record.realm} / item {record.item_id}")
        add("")
        add(f"- **recommended ask**: {_copper(e.recommended_ask)} per unit")
        add(f"- **estimated realizable price**: {_copper(e.price)} net of the house cut")
        if e.price_low and e.price_high:
            add(f"- **{_pct(e.price_interval_level)} range**: {_copper(e.price_low)} "
                f"- {_copper(e.price_high)} "
                f"({_pct(e.price_interval_coverage)} of "
                f"{e.price_interval_n} held-out sales)")
        if e.expected_sale_price:
            add(f"- **expected realized**: {_copper(e.expected_sale_price)}")
            if e.sale_price_low and e.sale_price_high:
                add(f"- **{_pct(e.sale_price_interval_level)} realized range**: "
                    f"{_copper(e.sale_price_low)} - {_copper(e.sale_price_high)} "
                    f"({_pct(e.sale_price_interval_coverage)} of "
                    f"{e.sale_price_interval_n} held-out sales)")
        add(f"- **confidence**: {_pct(e.confidence)}")
        add(f"- **chance of selling in {config.horizon_h}h**: {_pct(e.sale_probability)}")
        if e.expected_time_hours is not None:
            add(f"- **expected time to sale**: {e.expected_time_hours:.1f} h")
        else:
            add("- **expected time to sale**: not supported by the outcome data")
        add(f"- **sample**: {e.sample_size} sale(s) of this item in this realm")
        evidence = e.evidence
        refs = [f"`{k}` {v}" for k, v in sorted(evidence.items())
                if k.startswith("db") and isinstance(v, int)]
        if refs:
            add(f"- **market references**: {', '.join(refs)}")
        if evidence.get("dfaMissing"):
            add(f"- **Data for Azeroth interest**: {evidence['dfaMissing']} of "
                f"{evidence.get('dfaSample')} collectors still lack this set "
                f"({evidence.get('dfaAppearanceName') or 'appearance'}) -- a "
                "measure of interest, not a measure of demand")
        if e.reasons:
            add("- **why**:")
            for reason in e.reasons:
                add(f"    - {reason}")
        if e.flags:
            add(f"- **warnings**: {', '.join(e.flags)}")
        add("")

    add("## What the numbers mean")
    add("")
    add("| field | what it is | what it is not |")
    add("|---|---|---|")
    add("| estimated realizable price | the median of what a unit of this item "
        "fetched in this realm, net of the house cut, over the transactions "
        "the model was trained on | it is not an asking price, and it is not "
        "a promise |")
    add("| calibrated range | the multiplicative interval the model's "
        "held-out errors support, with the coverage it actually delivered | "
        "it is not a guarantee, and a missing range means unmeasured, not zero "
        "uncertainty |")
    add("| confidence | how much the estimate is worth, from the sample "
        "behind it, the age of the evidence and how much the market "
        "references disagree | it is not a probability of selling |")
    add("| chance of selling | the model's probability that a listing at the "
        "recommended ask sells inside the horizon | it is not a guarantee, and "
        "it is not computed for items the model has never seen |")
    add("| expected time to sale | only present when the outcome data "
        "supports it | the data does **not** support it here, so it is absent |")
    add("| Data for Azeroth interest | how many collectors still lack the "
        "cosmetic set | it is **not** evidence of auction house demand |")
    add("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
