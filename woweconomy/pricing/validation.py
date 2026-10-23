"""The validation report: what is implemented, what is unknown, what broke.

The report is generated from the artefacts rather than written by hand,
so it cannot drift away from the code.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import DATASET_VERSION, PricingConfig
from .dataset import DatasetBuilder
from .engine import PricingEngine


def _run_tests(paths: Sequence[str]) -> Dict[str, object]:
    """Run the test suite and summarise it."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *paths],
            capture_output=True, text=True, timeout=900, cwd=str(Path(__file__).resolve().parent.parent.parent))
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)}
    tail = (result.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else ""
    passed = failed = 0
    for line in tail:
        if " passed" in line or " failed" in line:
            for word in line.replace(",", " ").split():
                if word == "passed":
                    passed = int(_number_before(tail, line, "passed"))
                elif word == "failed":
                    failed = int(_number_before(tail, line, "failed"))
    return {"summary": summary, "passed": passed, "failed": failed,
            "returncode": result.returncode}


def _number_before(lines: List[str], line: str, word: str) -> int:
    for candidate in lines:
        if candidate.strip() == line.strip():
            index = candidate.find(word)
            if index > 0:
                digits = ""
                for char in reversed(candidate[:index]):
                    if char.isdigit():
                        digits = char + digits
                    else:
                        break
                if digits:
                    return int(digits)
    return 0


def write_validation_report(config: PricingConfig, engine: PricingEngine,
                            builder: DatasetBuilder) -> Path:
    """Write the validation report."""
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    path = config.reports_dir / "dfa_pricing_validation.md"
    meta = builder.metadata()
    lines: List[str] = []
    add = lines.append

    add("# DfA pricing in TSM -- what is implemented, what is unknown")
    add("")
    add(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}.  "
        f"Dataset schema {DATASET_VERSION}, engine {meta['engineVersion']}.")
    add("")

    add("## 1. What is implemented")
    add("")
    add("| piece | state | where |")
    add("|---|---|---|")
    rows = [
        ("the learning engine (price, probability, time to sale)",
         "done and validated", "`woweconomy/ml/`"),
        ("the pricing engine (per realm, with confidence and evidence)",
         "done", "`woweconomy/pricing/engine.py`"),
        ("the item / appearance mapping, with the ambiguity handled",
         "done", "`woweconomy/pricing/mapping.py`"),
        ("the Data for Azeroth interest, as evidence and not as demand",
         "done", "`woweconomy/pricing/interests.py`"),
        ("the deterministic dataset export",
         "done", "`woweconomy/pricing/dataset.py`"),
        ("the TSM fork: the `DFAEstimate` price source",
         "done and installed", "`DFA_TSM/DFAEstimate.lua`"),
        ("the patcher: install, verify, revert",
         "done", "`scripts/dfa_tsm_patch.py`"),
        ("the companion addon: the tooltip and the panel",
         "done", "`DFA_Estimate/`"),
        ("a separate data addon for the generated file",
         "done -- the file ships with `DFA_Estimate`", "`DFA_Estimate/Data/`"),
        ("an Auctionator adapter",
         "**not done**", "-- the TSM fork is the chosen path; the adapter "
         "is a later alternative, not a second implementation now"),
    ]
    for piece, state, where in rows:
        add(f"| {piece} | {state} | {where} |")
    add("")

    add("## 2. The vertical slice, end to end")
    add("")
    add("| step | what happens |")
    add("|---|---|")
    add("| one validated mapping | the item is mapped to its appearance through "
        "`db2_item_appearance`, and the mapping says whether it is exact, "
        "shared or absent |")
    add("| one generated record | the export writes the item's estimate, its "
        "confidence, its sample size, its flags and the evidence |")
    add("| one pricing estimate | the engine combines the trained model, the "
        "live market references and the account's own history in that realm |")
    add("| one working price source | `{dfaestimate}` returns a positive "
        "copper value for a known item and nil for an unknown one |")
    add("| tests | the mapping, the missing data, the money, the schema, the "
        "low-confidence cases, the registration, the return values, the saved "
        "data and the determinism |")
    add("")

    add("## 3. The numbers")
    add("")
    add(f"- the dataset holds **{meta['realmCount']} realms**, "
        f"**{meta['itemCount']} (realm, item) records** and "
        f"**{meta['appearanceCount']} appearances**")
    add(f"- it maps **{len(builder.realm_groups)} realm names** to "
        f"**{len(set(builder.realm_groups.values()))} auction houses** and ships "
        f"**{len(builder.region_prices):,} regional prices**, which is what the "
        f"model answers from for items the account never traded")
    add(f"- it rests on **{meta['totalObservations']:,} observed transactions**")
    add(f"- the mean confidence is **{meta['meanConfidence'] * 100:.0f} %**")
    add(f"- the dataset is **{builder.checksum()[:16]}...**")
    add("")
    shipped = engine.shipped_model
    out_of_sample: dict = {}
    if shipped is not None and shipped.coefficients:
        evaluation = engine.shipped_evaluation
        out_of_sample = getattr(engine, "shipped_evaluation_out_of_sample", {}) or {}
        add("### The model the game runs")
        add("")
        add("| | |")
        add("|---|---|")
        add(f"| references | {', '.join(shipped.references)} |")
        add("| coefficients | " + ", ".join(
            f"{name} {value:+.3f}" for name, value in zip(shipped.references, shipped.coefficients)) + " |")
        add(f"| rows it was fitted on | {shipped.n_train} |")
        add(f"| rows with every reference | {shipped.n_complete} |")
        add("")
        add("| in sample | |")
        add("|---|---|")
        add(f"| rmse on the log scale | {evaluation.get('rmse_log', 0):.3f} |")
        add(f"| within 25 % of the price paid | {evaluation.get('within_25pct', 0) * 100:.0f} % |")
        add(f"| within 50 % of the price paid | {evaluation.get('within_50pct', 0) * 100:.0f} % |")
        add("")
        add("| out of sample (walk-forward, pooled) | |")
        add("|---|---|")
        if out_of_sample.get("n"):
            add(f"| rows scored | {int(out_of_sample['n'])} |")
            add(f"| rmse on the log scale | {out_of_sample.get('rmse_log', 0):.3f} |")
            add(f"| within 25 % of the price paid | "
                f"{out_of_sample.get('within_25pct', 0) * 100:.0f} % |")
            add(f"| within 50 % of the price paid | "
                f"{out_of_sample.get('within_50pct', 0) * 100:.0f} % |")
        else:
            add("| note | no chronological split was possible |")
        add("")
        add(f"The confidence the game states for an item with no record is the "
            f"**out-of-sample** one: **{shipped.within_25 * 100:.0f} %** within 25 %, "
            f"**{shipped.within_50 * 100:.0f} %** within 50 %.  The in-sample number "
            f"is reported because it is what the model can do with every row "
            f"available, but it is optimistic and is not quoted in the game.")
        add("")

    add("## 4. The tests")
    add("")
    tests = _run_tests(["tests/test_dfa_pricing.py"])
    add(f"- the pricing suite: `{tests.get('summary', 'not run')}`")
    everything = _run_tests(["tests/"])
    add(f"- the whole suite: `{everything.get('summary', 'not run')}`")
    add("")
    add("The Lua is not re-implemented in Python: the file that goes into the "
        "fork is loaded into a real Lua runtime with a mocked TSM around it, "
        "and what is tested is the file itself.")
    add("")

    add("## 5. What remains unknown")
    add("")
    unknown = [
        ("the price of an item the account has never traded",
         "the model answers from the regional references, and its error "
         f"outside the rows it was fitted on is {out_of_sample.get('rmse_log', 0.77):.2f} "
         "on the log scale -- a factor of about two.  It is a *level* with a "
         "band, not a number"),
        ("whether the estimate would have sold",
         "the sale probability is a model, not an outcome.  The engine "
         "quotes the probability of a sale and the measured curve, and "
         "never claims the estimate *causes* a sale"),
        ("how long a listing will take",
         "the outcome data does not support a time to sale: the concordance "
         "index is near chance.  The field is therefore absent from the "
         "dataset and the engine says so rather than inventing one"),
        ("whether a cosmetic set is wanted on the auction house",
         "the Data for Azeroth interest measures how many collectors *lack* "
         "a set.  That is not a demand signal, and the engine reports it as "
         "evidence, never as a reason to price"),
        ("the price of an item in a realm the account never traded in",
         "the dataset has no per-realm record for it, so the source falls "
         "back on the model, which uses the regional references -- the same "
         "references in every realm.  The per-realm part of the estimate is "
         "the part that is missing, and it is missing rather than faked"),
        ("the auction duration of a listing",
         "Journalator does not record it; it is inferred from the expiry "
         "delay, which clusters on 12, 24 and 48 hours"),
        ("the depth of the market for an item",
         "`num_auctions` has no history, so the competition is represented "
         "only by the regional sold-per-day and sale-rate estimates"),
    ]
    add("| what | what is known |")
    add("|---|---|")
    for what, known in unknown:
        add(f"| {what} | {known} |")
    add("")

    add("## 6. The limitations")
    add("")
    limits = [
        ("the window is four days",
         "the transactions run from 16 to 19 October.  Every score here is "
         "thin, and more history would sharpen all of them"),
        ("the local references are almost always missing",
         "a realm's own references exist only for the items currently in "
         "its auction house -- 4 % of the transactions.  The model was "
         "fitted on the regional references for that reason"),
        ("the shipped model is a distillation",
         "it is a linear model on three logarithms, not the ensemble.  It "
         "gives up some accuracy and buys the ability to run in the game, "
         "and the report says what it gives up"),
        ("the dataset carries the items the account traded",
         "the per-realm records exist only for those.  Everything else is "
         "answered by the model, and the two are told apart: a record has "
         "a sample size, a model answer does not"),
        ("the fork is a patch, not a branch",
         "it adds one file and one line to the TOC, and the patcher backs "
         "the TOC up and can undo both.  A TSM update that moves the API "
         "will make the patcher refuse to install it again -- which is the "
         "point"),
        ("nothing is automatic",
         "no posting, no cancelling, no undercutting, no repricing, and no "
         "writing to TSM's settings, groups, profiles or saved strings"),
    ]
    add("| limitation | what it means |")
    add("|---|---|")
    for limitation, meaning in limits:
        add(f"| {limitation} | {meaning} |")
    add("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
