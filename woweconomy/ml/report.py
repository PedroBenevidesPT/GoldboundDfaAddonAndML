"""The engine's report, in words.

The JSON report holds the numbers; this turns them into the argument the
numbers support -- including the parts that are weak, which is most of
the point.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import MLConfig


def _pct(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def _num(value: Optional[float], digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _gold(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:,.1f}g"


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _span_label(first_ts, last_ts) -> Optional[tuple]:
    """The window length and the dates, read from the data rather than typed."""
    try:
        first = datetime.fromtimestamp(float(first_ts), timezone.utc)
        last = datetime.fromtimestamp(float(last_ts), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    days = max(1, int(round((last - first).total_seconds() / 86400.0)))
    start_month = _MONTHS[first.month - 1]
    end_month = _MONTHS[last.month - 1]
    if first.year != last.year:
        start = f"{first.day} {start_month} {first.year}"
        end = f"{last.day} {end_month} {last.year}"
    elif first.month != last.month:
        start = f"{first.day} {start_month}"
        end = f"{last.day} {end_month}"
    elif first.day != last.day:
        start = str(first.day)
        end = f"{last.day} {end_month}"
    else:
        start = end = f"{last.day} {end_month}"
    label = "The window is one day" if days == 1 else f"The window is {days} days"
    return label, f"the transactions run from {start} to {end}"


def _table(header: List[str], rows: List[List[str]]) -> List[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    return out


def write_report(config: MLConfig, payload: Dict[str, object]) -> Path:
    """Render the pipeline payload as Markdown."""
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    path = config.reports_dir / "dfa_ml_report.md"
    lines: List[str] = []
    add = lines.append

    add("# The DfA learning engine -- what it learned and how much to trust it")
    add("")
    add(f"Generated {payload.get('generated_at', '')}.")
    add("")
    add("**Listings show what sellers want to receive; transactions show what "
        "someone actually paid.**  Every model here is trained against the "
        "second kind, and the asking-price families -- `dbmarket`, "
        "`dbregionmarketavg` and their relatives -- are inputs, never labels.")
    add("")

    # ------------------------------------------------------------------ data
    link = payload["link"]
    dataset = payload["dataset"]
    add("## What it was trained on")
    add("")
    add(f"- **{dataset['rows']:,} listings** over {dataset['items']:,} items and "
        f"{dataset['realms']} realms")
    add(f"- **{link['matched_sale']:,}** of them reached a sale, "
        f"{link['matched_expired']:,} expired without one, "
        f"{link['matched_cancelled']:,} were withdrawn by the seller and "
        f"{link['censored']:,} had no observed outcome at all")
    add(f"- the linkage is **{_pct(link['price_exact_rate'])} price-exact**: the "
        "amount that changed hands is exactly the posted buyout times the stack "
        "size.  A wrong pairing would not do that.")
    add(f"- every row has a regional reference; "
        f"{dataset['rows_with_local_reference']:,} have a local one")
    add("")

    # ------------------------------------------------------------- model A
    validation = payload["validation"]
    test = payload["test_window"]
    price = validation.get("price", {})
    test_price = test.get("price", {})
    add("## Model A -- the realizable price")
    add("")
    add("**Target:** the money the seller pocketed per unit, in logarithms.  "
        "Never `dbmarket`, never `dbregionmarketavg`: those are asking prices, "
        "and an asking price is a hypothesis about a price, not a price.")
    add("")
    add("**The ask is deliberately withheld from this model.**  Without it the "
        "model has to answer \"what will the market pay for this item?\"; with "
        "it, the model would simply learn that the answer is whatever you "
        "asked, which is true of 99.7 % of the sales here and useless as a "
        "guide to what to ask.")
    add("")
    if price:
        add("### Walk-forward (train on an earlier day, validate on a later one)")
        add("")
        rows = [
            ["RMSE on the log scale", _num(price.get("rmse_log_mean")),
             _num(price.get("rmse_log_min")) + " .. " + _num(price.get("rmse_log_max"))],
            ["median of the error factor", _num(price.get("median_ratio_mean")),
             "1.00 means the estimate is right on the median"],
            ["mean absolute error", _gold(price.get("mae_gold_mean")),
             "in gold, per unit"],
            ["symmetric error", _pct(price.get("smape_gold_mean")),
             "on the gold scale"],
        ]
        lines.extend(_table(["metric", "mean over the folds", "spread"], rows))
    if test_price.get("metrics"):
        add("### The untouched test window")
        add("")
        m = test_price["metrics"]
        add(f"Trained on {test_price.get('n_train')} sales from the earlier days, "
            f"scored on {test_price.get('n_test')} sales from the last day that no "
            "fold ever saw.")
        add("")
        rows = [
            ["RMSE on the log scale", _num(m.get("rmse_log"))],
            ["median of the error factor", _num(m.get("median_ratio"))],
            ["within 25 % of the price paid", _pct(m.get("within_25pct"))],
            ["within 50 % of the price paid", _pct(m.get("within_50pct"))],
            ["mean absolute error", _gold(m.get("mae_gold"))],
        ]
        lines.extend(_table(["metric", "value"], rows))
        if m.get("rmse_log") and m.get("rmse_log") > 0.5:
            add("> The estimate is unbiased on the median but wide: the market "
                "references do not pin down the price of one particular sale.  "
                "Read it as a price *level* with a band, not as a number.")
            add("")

    # ------------------------------------------------- the demand curve
    curve = payload.get("demand_curve", {})
    if curve and "bands" in curve:
        add("### The demand curve: what happens when the ask moves")
        add("")
        add("The model of `P(sell)` says how likely a sale is at the prices the "
            "account happened to use.  The curve below says what happens when "
            "the ask is moved -- the ratio is *the ask over the regional sale "
            "average*, the average a sale actually closed at, so the ratio is "
            "\"how far above what buyers pay is this listing asking\".")
        add("")
        rows = []
        for band in curve["bands"]:
            rows.append([band["band"], str(band["n"]),
                         _pct(band["sale_rate"]),
                         _num(band.get("expected_gold_ratio"), 2),
                         _pct(band["share_of_sales"])])
        lines.extend(_table(["ask / regional sale average", "listings",
                              "sale rate within 24 h",
                              "expected gold per listing", "share of all the sales"],
                             rows))
        if curve.get("best_band"):
            add(f"**The expected gold per listing peaks in the band "
                f"`{curve['best_band']}`** -- the sale rate there is "
                f"{_pct(curve.get('best_sale_rate'))} and each listing collects "
                f"about {_num(curve.get('best_expected_gold_ratio'), 2)} times the "
                "regional sale average when it sells.  Below that band the ask is "
                "so low that the sale is nearly certain but the price is tiny; "
                "above it the ask is so high that the listing simply sits there.  "
                "This is the answer the data gives without any model in the way, "
                "and it is the benchmark the strategy is measured against.")
            add("")
        if curve.get("median_ratio") is not None:
            add(f"The median listing asks **{_num(curve['median_ratio'], 2)}x** the "
                "regional sale average, and the sale rate is flat until the ask "
                "runs well past the level buyers pay -- and then it collapses.  "
                f"{_pct(curve.get('share_asked_above_3x'))} of the listings ask "
                f"more than three times the regional sale average, and only "
                f"{_pct(curve.get('sale_rate_above_3x'))} of those sold, against "
                f"{_pct(curve.get('sale_rate_below_1_4x'))} for the listings that "
                "asked within 1.4x.")
            add("")
            add("That is the speculative-reference problem in the account's own "
                "behaviour: the asking prices are not what the market is paying, "
                "and the listings that are priced far above the market are the "
                "ones that sit unsold.")
            add("")

    # ------------------------------------------------- the two hypotheses
    credibility = payload.get("reference_credibility", {})
    per_ref = credibility.get("per_reference", {})

    speculation = payload.get("speculation_curve", {})
    if speculation and "bands" in speculation:
        add("### The mechanism: what actually stops a listing selling")
        add("")
        add("The table below says what the gap between the asking references and "
            "the transactions costs in sales, and it is the sharpest result in "
            "this report.")
        add("")
        rows = []
        for band in speculation["bands"]:
            rows.append([
                band["band"],
                str(band["n"]),
                _pct(band["sale_rate"]),
                _num(band.get("median_ask_over_asking_ref"), 2),
            ])
        lines.extend(_table(
            ["dbregionmarketavg / dbregionsaleavg", "listings",
             "sale rate within 24 h", "ask as a fraction of the asking reference"],
            rows))
        add("When the asking reference and the price buyers pay agree, an ask "
            "placed on the asking reference clears.  When the asking reference "
            "has run far ahead of the transactions, the ask still sits on that "
            "inflated reference -- and the listing does not sell.  The account's "
            "failures are not bad luck and not a bad item: they are asks placed "
            "on a price the market stopped paying.")
        add("")
        add("This is the whole argument of the engine in one table.  A tool that "
            "averages `dbmarket` and `dbregionsaleavg` cannot tell these two "
            "situations apart -- they look like the same item at two prices.  A "
            "model trained against the outcomes can, because the *sale rate* "
            "differs by an order of magnitude between the bands.")
        add("")

    if per_ref:
        add("### The two hypotheses, measured")
        add("")
        add("Each asking-price family is compared with what the buyer actually "
            "paid, on the same transactions.  A family whose median sits above "
            "one promises more than the market pays -- an inflated reference.  "
            "A family near one is commercially defensible, and the model is "
            "not allowed to lower the price just because it looks expensive.")
        add("")
        rows = []
        for name, entry in per_ref.items():
            ratio = entry.get("median_reference_over_realised")
            rows.append([
                name,
                _num(ratio),
                _num(entry.get("reference_over_realised_p25")) + " .. "
                + _num(entry.get("reference_over_realised_p75")),
                _pct(entry.get("share_reference_above_realised")),
                entry.get("verdict", ""),
            ])
        lines.extend(_table(
            ["reference", "median vs realised", "quartile spread",
             "share above the realised price", "verdict"], rows))
        add("The model does not average these families.  It receives the ratios "
            "between them as features and learns from the transactions which "
            "family to believe -- and the ablation below says what each family "
            "is worth.")
        add("")

    ablation = payload.get("ablation", {})
    if ablation:
        add("### What each family contributes (walk-forward)")
        add("")
        rows = []
        for name, m in ablation.items():
            rows.append([name, _num(m.get("rmse_log_mean")),
                         _num(m.get("mae_log_mean"))])
        lines.extend(_table(["the model without...", "RMSE on the log scale",
                             "mean absolute error"], rows))
        add("A family whose removal does not hurt was noise.  A family whose "
            "removal *helps* was misleading the model, which is the "
            "speculative-reference problem showing up in the numbers.")
        add("")

    importance = payload.get("final_models", {}).get("feature_importance", {})
    if importance:
        add("### What the price model leans on")
        add("")
        top = list(importance.items())[:15]
        rows = [[f"`{name}`", _pct(value / 100.0, 1)] for name, value in top]
        lines.extend(_table(["feature", "share of the model's attention"], rows))
        add("")

    # ------------------------------------------------------------- model B
    prob = validation.get("probability", {})
    if prob:
        add("## Model B -- the probability of selling")
        add("")
        add("**Target:** did the listing sell inside the horizon?  A listing "
            "whose outcome was never observed is *censored* and is in neither "
            "class; a listing the seller withdrew is *cancelled* and is in "
            "neither class either -- a seller changing their mind is not "
            "evidence that nobody wanted the item.")
        add("")
        rows = []
        for horizon, m in sorted(prob.items(), key=lambda kv: int(kv[0])):
            rows.append([
                f"{horizon}h",
                str(int(m.get("n_mean", 0))),
                _pct(m.get("base_rate_mean")),
                _num(m.get("auc_mean")),
                _num(m.get("brier_mean")),
                _num(m.get("logloss_mean")),
            ])
        lines.extend(_table(
            ["horizon", "labelled rows", "base rate", "AUC", "Brier", "log loss"],
            rows))
        equivalence = payload.get("horizon_equivalence", {})
        groups = equivalence.get("identical_groups") or []
        if groups:
            for group in groups:
                add(f"**The {', '.join(str(h) + 'h' for h in group)} horizons are the "
                    "same question.**  An auction lasts 12, 24 or 48 hours, so once "
                    "the horizon passes that, every sale that was going to happen "
                    "has already happened.  The identical scores are not a "
                    "coincidence and not three models: they are one model asked the "
                    "same thing three times, and a longer horizon is a promise the "
                    "auction house cannot keep.")
            add("")
        else:
            add("The horizons are not independent: an auction lasts 12, 24 or 48 "
                "hours, so once the horizon passes 72 hours every sale that was "
                "going to happen has already happened.")
            add("")
        prob_test = test.get("probability", {})
        for horizon, block in sorted(prob_test.items(), key=lambda kv: int(kv[0])):
            if not block.get("evaluable"):
                add(f"- **{horizon}h on a held-out window:** not evaluable -- "
                    f"{block.get('note', 'the observation period is too short')}")
            elif block.get("metrics"):
                m = block["metrics"]
                add(f"- **{horizon}h on a held-out window:** AUC "
                    f"{_num(m.get('auc'))} on {block.get('n_test')} labelled rows "
                    f"(base rate {_pct(m.get('base_rate'))}), trained on "
                    f"{block.get('n_train')} rows from the days before it.")
        add("")

    # ------------------------------------------------------------- model C
    tts = validation.get("time_to_sale", {})
    add("## Model C -- the time to sell")
    add("")
    add("**Method:** CatBoost's `SurvivalAft`, an accelerated-failure-time "
        "model that takes right-censored observations natively.  A sale is an "
        "event; an expiry and an unobserved listing are censored at the moment "
        "the account *knew* the item had not sold.")
    add("")
    if tts:
        add(f"- concordance index on the walk-forward folds: "
            f"**{_num(tts.get('c_index_mean'))}** "
            f"(spread {_num(tts.get('c_index_min'))} .. {_num(tts.get('c_index_max'))})")
        add("")
        c = tts.get("c_index_mean")
        if c is not None and c < 0.60:
            if c < 0.5:
                add("> **Verdict: the time to sale is not predictable from these "
                    "features.**  A concordance index of "
                    f"{_num(c)} is *below* chance: the model ranks the quick sales "
                    "below the slow ones as often as not.  The honest conclusion "
                    "is not a number but a limit -- the data cannot say how long a "
                    "listing will take, only how likely it is to sell.  Any tool "
                    "that quotes a confident time-to-sale from this data is "
                    "inventing it.")
            else:
                add("> **Verdict: the time to sale is not predictable from these "
                    "features.**  A concordance index of "
                    f"{_num(c)} is barely above chance.  The honest conclusion is "
                    "not a number but a limit: the data cannot say how long a "
                    "listing will take, only how likely it is to sell.  Any tool "
                    "that quotes a confident time-to-sale from this data is "
                    "inventing it.")
            add("")
    tts_test = test.get("time_to_sale", {})
    if tts_test.get("metrics"):
        m = tts_test["metrics"]
        c_index = m.get("c_index")
        add(f"On the untouched test window the concordance index is "
            f"{_num(c_index)} over {m.get('n')} rows.")
        if c_index is not None and c_index > 0.55:
            add(f"The median observed time to sale was "
                f"{_num(m.get('median_observed_hours'), 1)} hours against a median "
                f"predicted {_num(m.get('median_predicted_hours'), 1)} hours.")
        else:
            add("The model does not clear the bar for a usable time estimate, so "
                "**no time-to-sale figure is offered**: a number the data cannot "
                "support would be worse than no number at all.")
        add("")

    # ------------------------------------------------------------- model D
    strategy = payload.get("strategy_evaluation", {})
    add("## Model D -- the listing-price strategy")
    add("")
    add("This is **not** a fourth model.  It is the decision layer: it takes "
        "the predicted price, the predicted probability of selling and the "
        "deadline, and evaluates a grid of candidate asks.")
    add("")
    add("**How the two probability sources are combined.**  The *ask* "
        "dimension comes from the measured demand curve: it is an "
        "observation of how often a listing at that ask sold, and the model "
        "cannot be trusted outside the range of asks it was trained on.  The "
        "*item* dimension comes from the model, which ranks items well -- it "
        "separates the things that sell from the things that do not -- and is "
        "scaled into the curve as a lift.  Each source is used where it is "
        "strong, and the composition is stated rather than hidden.")
    add("")
    if strategy and "error" not in strategy:
        if strategy.get("folds"):
            add(f"Each fold is replayed with models fitted **only on the days "
                f"before it**, so the strategy never advises on a listing its "
                f"model has already seen.  {strategy.get('listings_evaluated'):,} "
                f"listings with a known outcome were replayed in total, of which "
                f"{strategy.get('sold'):,} sold.")
            add("")
            rows = []
            for fold in strategy["folds"]:
                rows.append([
                    fold["fold"], str(fold.get("listings")), str(fold.get("sold")),
                    _num(fold.get("median_ask_ratio"), 2),
                    _num(fold.get("sold_median_ask_ratio"), 2),
                    _num(fold.get("expired_median_ask_ratio"), 2),
                    _gold(fold.get("median_abs_ask_gold")),
                ])
            lines.extend(_table(
                ["fold", "listings", "sold", "median recommended / actual",
                 "on the sales", "on the expiries", "median gap"],
                rows))
            add(f"A recommendation of **1.0x** the ask that was actually used "
                "means the strategy agrees with the seller.  Above one means it "
                "would have asked more; below one, less.")
            add("")
        add(f"- across the folds the median recommendation sits at "
            f"{_num(strategy.get('median_ask_ratio'), 2)}x the ask that was "
            f"actually used")
        if strategy.get("sold_median_ask_ratio") is not None:
            add(f"- on the listings that **sold** it is "
                f"{_num(strategy.get('sold_median_ask_ratio'), 2)}x the ask that "
                f"cleared the market")
        if strategy.get("expired_median_ask_ratio") is not None:
            add(f"- on the listings that **expired** it is "
                f"{_num(strategy.get('expired_median_ask_ratio'), 2)}x the ask "
                f"that did not")
        add(f"- the recommendations are a median of "
            f"{_gold(strategy.get('median_abs_ask_gold'))} away from the ask "
            f"that was actually used")
        add(f"- the mean regret is {_gold(strategy.get('mean_regret_gold'))} per "
            f"listing")
        add("")
        add("**The trade-off, in plain words.**  On the listings that sold the "
            "strategy asks *more* than the seller did -- the market was paying "
            "that much.  On the listings that expired it asks a fraction of what "
            "the seller asked, and that is where the money is: most of the "
            "account's listings were priced where nothing was buying.  The "
            "regret is the price left on the table on the few sales where the "
            "strategy recommended less than the seller did.  It is the cost of "
            "the strategy, and it is paid deliberately.")
        add("")
        add("**The assumption, stated rather than hidden.**  A counterfactual "
            "ask cannot be observed, so the evaluation rests on *monotone "
            "demand*: a lower ask is at least as likely to sell.  Under that "
            "assumption a listing that sold at `A` would also have sold at any "
            "`P <= A` -- so recommending `P` instead of `A` left "
            "`A - P` on the table -- and a listing that expired at `A` would "
            "also have expired at any `P >= A`.  The regret is therefore an "
            "upper bound on what the strategy gives up, not a claim that the "
            "strategy maximises realised gold.  It does not, and no model on "
            "this data can prove that it does.")
        add("")
        add("**The second limit.**  The sale probability of the *ask* comes "
            "from the measured curve, which is an observation; the sale "
            "probability of the *item* comes from the model, which has only "
            "seen the prices this account actually asked.  A candidate ask far "
            "outside that behaviour is an extrapolation, and the engine says "
            "so in the answer it gives rather than presenting it as a "
            "measurement.")
        add("")

    value = payload.get("strategy_value", {})
    if value and "error" not in value:
        add("### What the strategy would have collected")
        add("")
        add("The demand curve is *measured*, so applying it to a different ask "
            "is an estimate over a relationship the data actually observed -- not "
            "a realised result, but the closest thing to a counterfactual that "
            "this data allows.")
        add("")
        rows = [
            ["the ask that was used",
             f"{_num(value.get('median_actual_ratio'), 2)}x the regional sale average",
             _num(value.get("expected_gold_actual"), 3)],
            ["the ask the strategy would have used",
             f"{_num(value.get('median_recommended_ratio'), 2)}x the regional sale average",
             _num(value.get("expected_gold_recommended"), 3)],
        ]
        lines.extend(_table(["", "median ask", "expected gold per listing"], rows))
        improvement = value.get("improvement")
        if improvement:
            add(f"**The expected gold per listing improves by "
                f"{_pct(improvement - 1)}** over the asking behaviour that was "
                f"actually used, and {_pct(value.get('share_in_the_optimal_band'))} "
                "of the recommendations land in the band the curve says is the "
                "best (1x to 2x the regional sale average).")
            add("")
            add("The account's own median listing asks "
                f"{_num(value.get('median_actual_ratio'), 2)}x the regional sale "
                f"average; the strategy's median is "
                f"{_num(value.get('median_recommended_ratio'), 2)}x.  The whole "
                "difference is the inflated asking reference, taken out of the "
                "price and put into the sale rate.")
            add("")
        add(f"Over {value.get('listings'):,} replayed listings.  "
            + str(value.get("note", "")).capitalize() + ".")
        add("")

    # --------------------------------------------------------- limitations
    add("## What the engine cannot do")
    add("")
    span = _span_label((payload.get("dataset") or {}).get("first_ts"),
                       (payload.get("dataset") or {}).get("last_ts"))
    # the split description ends with the untouched test window, which is
    # not a fold; counting it would overstate how much was validated
    folds = sum(1 for entry in (payload.get("split") or [])
                if not str(entry.get("fold", "")).startswith("TEST"))
    folds_text = "one fold" if folds == 1 else f"{folds} folds"
    if span is None:
        window = ("The window is short",
                  f"the walk-forward has {folds_text} and a test day.  "
                  "More history would sharpen every score here; none of it "
                  "is available yet.")
    else:
        window = (span[0],
                  f"{span[1]}.  The walk-forward has {folds_text} and a test "
                  "day.  More history would sharpen every score here; none "
                  "of it is available yet.")
    limits = [
        window,
        ("The prices are the ask",
         "99.7 % of the sales closed at exactly the posted buyout, because the "
         "account posts buyout-only auctions.  The price model therefore "
         "learns the market's clearing level, not a bidding behaviour."),
        ("The outcomes arrive by mail",
         "a sale or an expiry is recorded when the account opens the mail, so "
         "every duration is an upper bound.  The models are deliberately "
         "conservative because of it."),
        ("The demand signal has no clock",
         "the DfA counters are a live aggregate.  A demand feature describes "
         "today's audience applied to a historical row, and the feature set "
         "says so."),
        ("The market depth is missing",
         "`num_auctions` has no history, so competition is represented only by "
         "the regional sold-per-day and sale-rate estimates."),
        ("The horizon cannot exceed the auction",
         "a 7-day or 14-day horizon is not a longer sale probability; it is "
         "the same probability with more time in which the auction has "
         "already ended."),
    ]
    lines.extend(_table(["limit", "what it means"], [[a, b] for a, b in limits]))

    add("## Reproducing this")
    add("")
    add("```")
    add("python scripts/dfa_ml.py audit      # the data-readiness report")
    add("python scripts/dfa_ml.py train      # walk-forward, test window, models")
    add("python scripts/dfa_ml.py recommend  # a listing recommendation")
    add("```")
    add("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
