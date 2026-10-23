# Transmog market model — what was measured, built and refused

**Date**: 2026-10-22
**Branch**: `fix/sentinel-probability` (not merged)
**Scope**: modelling transmog as a separate market category, with measured costs

---

## 1. What the existing system already had

I inspected before designing. The project was further along than a greenfield
implementation would assume:

| Asset | What it gives | Reused? |
|---|---|---|
| `db2_item_appearance` (153,290 rows) | `itemId → appearanceId`, the authoritative transmog signal | yes |
| `db2_items` (176,702 rows) | `binding`, `quality`, `era` | yes |
| `collector_demand.py` | `item_appearances()`, `AppearanceDemand` over 75,756 sampled accounts | yes |
| `mog_groups.py` | existing behaviour classifier (`liquid`/`collector`/`slow`/…) | left intact |
| `mog_operations.py`, `mog_realm.py` | TSM export, per-realm posture | left intact |
| `reference_price.py` | evidence-weighted ask reference | left intact |
| `auction_events` (577 sales) | real deposit, fee, net proceeds | **measured against** |

So the work was to add the missing layer — measured costs, id-keyed
classification with explicit uncertainty, sale-anchored valuation, and an
expected-net-gold strategy — without duplicating what existed.

---

## 2. The largest finding: the cost model was wrong

The code assumed (`pricing_engine.py`, `token_rush.py`):

```
deposit = min(max(5% x price, 1g), 300% x vendor)
```

Measured against the account's own 577 completed sales:

| Formula | Rows explained |
|---|---|
| `max(0.15 x vendor x qty, 100c)` | **343 / 404 (84.9%)** |
| `min(max(5% x price, 1g), 300% x vendor)` | **0 / 404 (0.0%)** |

The real mechanic is **15% of the vendor price per unit, floored at 100 copper
(10 silver, not 1 gold)**, and there is no 300%-of-vendor cap: one row carries
a deposit of 3,000,000c against a 1,000,000c vendor price.

The auction-house cut is **5%**: `net_proceeds / total_price` is 0.95 for 205 of
577 rows, median 0.9525, 339/577 within 0.5%.

A units trap was also confirmed: `unit_price_copper` is **per unit** while
`net_proceeds_copper` and `total_price_copper` are **stack totals**. Comparing
them directly gives `net / unit_price` with a median of 3.8 and a maximum of
266, which reads as corruption and is not. Every function now takes the stack
total explicitly.

---

## 3. The second finding: my own fallback was measurably bad

The valuation's first design used the cheapest live listing (`min_buyout`) as
the price when no completed sales existed. Measured chronologically on the TSM
realm series:

| Subset | n | Baseline (market avg) median error | Wall (`min_buyout`) median error | Wall wins |
|---|---|---|---|---|
| All items, 6 realms | 59,151 | **0.4%** | 11.9% | 2.8% |
| Transmog only, 6 realms | 41,777 | **0.4%** | 11.3% | 2.9% |

The wall loses on 97% of items. A wall is a `MIN` over listings, so one junk
listing defines it. The fallback was therefore **changed**: the market average
is the estimate when no realised sales exist, and the wall is demoted to a
competition signal for the strategy layer. That is a design decision forced by
measurement, not a preference.

---

## 4. What was built

| File | Purpose |
|---|---|
| `woweconomy/analytics/ah_costs.py` | Measured cut/deposit, plus `measure_from_db()` so the fit is re-checked after every import |
| `woweconomy/analytics/mog_classify.py` | id-keyed classification: `collectible` / `niche` / `plain` / `restricted` / `unknown` |
| `woweconomy/analytics/mog_valuation.py` | Sale-anchored price with evidence tiers and counts |
| `woweconomy/analytics/mog_strategy.py` | Expected net gold, action selection, labelled probability source |
| `scripts/mog_eval.py` | Chronological evaluation; reports what cannot be measured |
| `tests/test_ah_costs.py` … `test_mog_strategy.py` | 65 tests |

### Classification rules

Driven entirely by id joins, never by name:

- No `db2_item_appearance` row → `unknown`. **Absence of a row is absence of
  evidence, not evidence that the item is not transmog.** A recipe and a
  never-imported equipment item are indistinguishable, and the code says so.
- Look present, binding not `0`/`2` → `restricted` (a BoP look cannot be sold).
- `era != 0` → `restricted` (an era-Classic row must not enter a retail value).
- No DB2 row at all → `tradeable = False`, so it routes to `restricted` rather
  than silently posting something that may be soulbound.
- Demand ≥ 50% of sample → `collectible`; 15–50% → `niche`; below → `plain`.

### Valuation tiers

1. **Completed sales** (≥3, recent) → `high`/`medium`, `is_valuation = True`
2. **Market average** → `low`, explicitly *not* a realised price
3. **Wall alone** → `low`, flagged as a floor of asks
4. **Vendor price** → `confidence = "none"`, a downside floor, never a valuation

`PriceEstimate.is_valuation` is the flag a UI must check before calling a number
"the price".

### Strategy

Scores candidate prices by

```
E[net] = p x net(total) - (1-p) x deposit - (1-p) x relist_deposits - (1-p) x holding_cost
```

with the deposit from the measured formula. Actions: `post`, `post_patient`,
`undercut`, `maintain`, `relist`, `hold`, `vendor`, `do_not_post`.

**No price-response curve is invented.** The sale probability was measured at
the level the item has actually been selling at, so it describes that price and
nothing else. Candidates priced above it are reported with
`measured=False, expected_net_copper=None` rather than scored by a curve nobody
fitted. This was a correction: the first implementation used
`p x exp(-1.5 x (ratio-1))`, which is an extrapolation from sparse data and was
removed.

---

## 5. What could not be validated

**The net-gold claim is not measurable on this installation.**

- `auction_events` holds 577 sales over 6 days across **215 distinct items**.
  Of those, **31 are transmog** (22 collectible, 9 niche, 184 `unknown` — the
  account's sales are overwhelmingly crafting materials).
- `tsm_price_change` has a real time series (1,262 snapshots, 5 days, 14–16
  snapshots per realm) but records **asks, not transactions**. It cannot say
  whether a listing at a given price sold.

A strategy evaluation needs, per transmog item: the ask, whether it sold, when,
and at what price. That is a listing-level outcome feed, and it does not exist
here. The alternative — inventing a "would have sold" rule and reporting gold —
was rejected.

**Would unblock it:** per-listing outcomes (posted / sold / expired and the
price), several weeks of history so the slow tail is observed rather than
censored, and both sides of each sale so the clearing price is a transaction
price.

---

## 6. Test results

```
tests/test_ah_costs.py tests/test_mog_classify.py
tests/test_mog_valuation.py tests/test_mog_strategy.py
→ 65 passed
```

```
python -m pytest tests/ -q
→ 8 failed, 1065 passed
```

The 8 failures are the **pre-existing** Lua/addon-bridge failures
(`test_auction_intel_addon.py`, `test_dfa_estimate_addon.py` ×5,
`test_dfa_pricing.py` ×2), unchanged from the baseline established earlier in
this session. `test_retention.py::test_policy_matches_the_live_schema`
intermittently fails with `unable to open database file` while the live watcher
holds the WAL; it passes standalone (23 passed).

Tests specifically covering the spec's edge cases: sparse data (1 observation →
`low`, wide uncertainty), stale prices (45-day anchor expiry), outliers
(sentinel and impossible prices excluded from `MIN`), zero-sale items
(`plain` with `has_demand = False`, not "dead"), expired auctions (no outcome
inferred), and missing metadata (`unknown`, never `plain`).

---

## 7. Exact commands

```bash
# costs measured against real sales
python -m pytest tests/test_ah_costs.py -q

# the evaluation, including what it refuses to claim
python scripts/mog_eval.py --db tsm_markets.db --limit-scopes 6 \
    --out reports/mog_eval.json --markdown reports/mog_eval.md

# full regression
python -m pytest tests/ -q
```

Output: `reports/mog_eval.md`, `reports/mog_eval.json`.

---

## 8. Remaining risks

1. **The deposit fit is 84.9%, not 100%.** The 15% residual is probably a
   duration multiplier (12/24/48h postings) that the data does not let me
   separate, because the posting duration is not recorded per row. Treating the
   deposit as duration-independent understates the cost of a 48h posting.
2. **The cut fit is 58.8%.** The tail sits at 0.955–0.968 rather than 0.950,
   consistent with the addon reconstructing net proceeds from invoices rather
   than reading them. 5% is the modal value; the residual is data noise.
3. **31 transmog sales is not a market.** Every statement about transmog
   economics rests on the DfA demand sample (75,756 accounts), not on this
   account's trading.
4. **`missing_pct` is a share of a scanned sample**, not of the server
   population. It is an audience proxy.
5. **`plain` vs `unknown` is a real distinction but a coarse one.** 184 of 215
   sold items are `unknown` because they are materials with no appearance row —
   correct, but it means the classifier cannot yet separate "equipment whose
   DB2 row is missing" from "not equipment".
6. **The `era`/`binding` gate depends on the DB2 import.** If `db2_items` is
   stale relative to a game patch, new items route to `restricted` (safe) rather
   than being posted.

---

## 9. Conditions for promotion

The transmog layer may be promoted to a user-facing recommendation when:

1. `measure_from_db()` reports `trustworthy = True` on the current data, and the
   deposit residual is explained by duration or explicitly bounded.
2. A listing-level outcome feed exists for transmog items, with ≥4 weeks of
   history, and the strategy beats **both** `vendor_all` and `undercut_all` on
   net gold over an identical chronological window.
3. The price forecast beats the naive "last market value" baseline on
   `is_valuation = True` rows (completed sales present) — the current
   measurement only covers the no-sales fallback, where it deliberately ties.

Until (2), the UI must show `action` and `confidence` but label any gold figure
as a modelled estimate, and must never present a `low`/`none` confidence price
as a validated item-specific prediction.
