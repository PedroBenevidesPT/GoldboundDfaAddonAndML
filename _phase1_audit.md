# DFA Phase 1 — Decision Pipeline Audit

## Objective
Trace the real data flow from AH ingestion and addon exports to the final recommendation, and produce a capability matrix showing what is implemented, partially implemented, missing, or blocked by unavailable data.

## 1. Data Ingestion Flow

### 1.1 AH Snapshots (`watch.task_ah` → `ah_undercut.scan_realm`)
- **What it does**: Calls `intel.py ah-fetch` which runs Blizzard API to get current AH state
- **Inputs**: Realm, connected realm ID from DB
- **Outputs**: 
  - `api_rows` — raw AH auction data (item_id, unit_price_copper, quantity, seller, etc.)
  - `posted` — player's own posted listings from inventory
  - `stats` — undercut statistics (cheapest price, supply, etc.)
- **Stores**: `undercut_observation` table (item_id, item_name, own_buyout_copper, lowest_unit_price_copper, undercut_copper, undercut_pct, items_ahead, time_left, sellers_json, source, confidence, reference_status, imported_at)
- **Blocked**: Requires Blizzard API access; `--sync-realms` needed for realms not yet in `ah_realm` table

### 1.2 Inventory & Posted Listings
- **What it does**: `inventory.latest_posted(con, realm)` reads the player's posted auctions from the addon's SavedVariables
- **Inputs**: Realm, connection to `data/intel.sqlite`
- **Outputs**: List of posted items with quantities and prices
- **Stores**: `inventory_posted_listing` table
- **Blocked**: Requires the addon to have been run at least once; no posted data = error

### 1.3 Mail Invoices
- **What it does**: `mail.py` captures mail invoices on login/logout
- **Inputs**: Mail system events
- **Outputs**: `mail_invoice` table (sale, listing, expire, buyer, seller, item_id, itemName, valueCopper, buyoutCopper, depositCopper, consignmentCopper, subject, sender, itemLink, confidence, source)
- **Blocked**: None; always available on login

### 1.4 Journalator Events
- **What it does**: `watch.task_journal` / `refresh_journalator` re-reads Journalator saved variables into `auction_events`
- **Inputs**: Journalator `.txt` file
- **Outputs**: `auction_events` table (sale, listing, expire, seller, character, item_id, item_name, etc.)
- **Blocked**: Requires Journalator data; offline mode skips

## 2. Market Observations & Competitor Listings

### 2.1 `derive_observations` (`ah_undercut.py`)
- **What it does**: Compares player's auctions vs. API ah_rows to produce observations
- **Key outputs per observation**:
  - `own_unit_price_copper`, `lowest_unit_price_copper`
  - `gap_pct` = (own - lowest) / own
  - `above_cheapest_pct` = (own - lowest) / lowest
  - `relation` = `_relation(own_unit, lowest_unit)` → "overpriced", "underpriced", "at_cheapest"
  - `items_ahead`, `sellers_below`, `time_left`, `source`, `confidence`
  - `item_id`, `item_name`, `own_auction_id`
- **Blocked**: Player must own the item; if not owned, `own_unit_price_copper` = 0 → WATCH

### 2.1.1 `_relation` function
- Compares own_unit vs lowest_unit
- Returns "overpriced", "underpriced", or "at_cheapest"
- Uses `own_unit` as denominator for gap calculation

### 2.2 Undercut Statistics
- **What it provides**: `cheapest` price, `supply` (total quantity at that price), `listings` (number of listings at that price)
- **Stored in**: `stats` field of undercut observation
- **Blocked**: If no API rows or no posted listings, returns error

## 3. Item Identity & Variant Matching

### 3.1 Item Key Matching
- **What it does**: Uses `item_id` + `item_name` + `item_key` (`item_id:0:0:0`)
- **Where it appears**: 
  - `undercut_observation` table (item_key column)
  - `inventory_posted_listing` table
  - `ah_undercut.load_api_auctions` normalizes item keys
- **Variant handling**: The system does **not** automatically deconflict item variants (levels, suffixes, bonus stats, pet species). If two items share the same visual but different IDs, they are treated as separate items. The `reference_status` field can be `VARIANT_AMBIGUOUS` when the cheapest listing carries a different item variant.

### 3.2 Realm Matching
- **What it does**: Maps realm names to connected realm IDs via `ah_realm` table
- **Blocked**: Realms not in `ah_realm` are skipped by `watch.task_ah`

## 4. Reference Prices & Pricing Models

### 4.1 Reference Status (`relist.decide`)
- **`REFERENCE_VERIFIED`**: Trusted comparable ask → can produce `cancel_and_relist` / `let_expire`
- **`REFERENCE_PRICE_ONLY`**: Only price known, no sale evidence → review-only (WATCH)
- **`REFERENCE_STALE`**: Old data → review-only (WATCH)
- **`REFERENCE_INVALID`**: No valid price → WATCH + review_required
- **`REFERENCE_VARIANT_AMBIGUOUS`**: Different item variant → WATCH + review_required
- **`REFERENCE_UNVERIFIED`**: Default → WATCH + review_required

### 4.2 Sentinel Price Model
- **What it predicts**: `y_net_unit_log` = log of realized net price per unit (after 5% AH cut)
- **Model**: CatBoost regression, trained on confirmed sales with `y_net_unit > 0`
- **Prediction**: `public.Estimate(itemId)` → price in copper
- **Interval**: Split-conformal 80% interval `[plow, phigh]` with measured coverage
- **Data confidence**: `record.conf` based on item-level sample size
- **Blocked**: Only items with at least one recorded sale in the dataset; items never sold get no price

### 4.3 Record-Price CatBoost Model
- **What it predicts**: Same as Sentinel — the net price a unit fetch
- **Location**: `models/dfa_ml/price_model.cbm`
- **Differences**: Different iterations (600 vs 300), different seed, trained on different train/cal/test splits
- **Blocked**: Only available through the pricing export; not directly in the relist decision pipeline

### 4.4 Shipped Linear Model
- **What it predicts**: Linear model on logarithms of regional references: `dbregionmarketavg`, `dbregionhistorical`, `dbregionsaleavg`
- **Coefficients**: Intercept + 3 coefficients + medians for missing references
- **Blocked**: Only usable through the pricing engine; not integrated into relist decisions

### 4.5 Prediction Intervals
- **What it provides**: Split-conformal interval on the log scale
- **Level**: 80% nominal; measured coverage reported separately
- **Applied to**: Sentinel price records (`plow`, `phigh`, `plevel`, `pcov`, `pintn`)
- **Blocked**: Shipped model interval not integrated into relist; Sentinel interval only for items with recorded sales

## 5. Time-to-Sale Model

### 5.1 Censoring-Aware C-Index
- **What it measures**: Harrell's C-index for right-censored durations, **only on ranking quality**
- **Current status**: **0.931** on 729 test rows (68 events, 661 censored; 90.7% censoring rate)
- **Baseline**: 0.500 (constant prediction)
- **Blocked for export**: **Not exported as exact duration** — only 44% of observed sales fall within a factor of 2 of the predicted duration (below the 60% gate). Reported status: `validated_ranking_only`
- **Blocked reason**: Model ranks durations well but cannot reliably estimate absolute times; converting C-index to individual sale probabilities is invalid.

### 5.2 Decision Impact
- **No TTS export** in tooltip or relist advice
- **No sale probability** exported from TTS model
- **Status flag**: `time-to-sale-unknown` / `time-to-sale-unsupported` in dataset flags

## 6. Inventory Quantities & Existing Listings

### 6.1 `inventory_posted_listing` 
- **What it tracks**: Player's own posted auctions — item_id, item_name, quantity, unit_price_copper
- **Used by**: `relist.advise` (own_quantity), `watch.task_ah` (posted list for undercut scan)
- **Blocked**: Requires addon to have been run at least once

### 6.2 `inventory_snapshot`
- **What it tracks**: Periodic inventory state (bags, bank, warband)
- **Used by**: `watch.task_journal`, `watch.task_sync`
- **Blocked**: Periodic; not real-time

### 6.3 `inventory_latest` (addon state)
- **What it tracks**: Latest known inventory state per realm/character
- **Used by**: Addon tooltip `CountOwned`, `BabysitProbability`
- **Blocked**: Updated only on login/logout or explicit scan

## 7. Undercut Detection & Thresholds

### 7.1 `derive_undercut` (`watch.task_ah`)
- **What it does**: Scans realm, gets undercut stats, advises on action
- **Flow**:
  1. `ah_undercut.scan_realm(con, realm)` → API rows + posted list + stats
  2. `relist.advise(con, realm, days=14)` → advice per item
  3. `alerts.undercut_alerts(advice)` → Discord/webhook alerts
- **Output per realm**: `counts` (HOLD, RELAST, EXPIRE, WATCH), `advice` list, `review_required` count

### 7.2 `decide` function rules
- **HOLD**: Ask below cheapest competitor
- **RELAST**: Heavy gap (>= heavy_gap) + time left >= 2h; OR queue > own_quantity * queue_factor
- **EXPIRE**: Time left <= short_time_h (e.g., 2h)
- **WATCH**: Default; reference not verified, or no clear action
- **Thresholds** (configurable via `**thresholds` in `relist.advise`):
  - `min_gap` ≈ 0.01 (1%)
  - `heavy_gap` ≈ 0.20 (20%)
  - `queue_factor` ≈ 1.0
  - `short_time_h` ≈ 2h
  - `long_time_h` ≈ 30h

### 7.3 Reference Status Filtering
- Only `REFERENCE_VERIFIED` items can produce `cancel_and_relist` / `let_expire`
- All others → `WATCH` + `review_required`

## 8. Posting & Relisting Decisions

### 8.1 Posting Decision (not auto-executed)
- **Not automated** in current code — the addon only *reports* recommendations
- **Would need**: Economic model with costs, expected proceeds, inventory, demand evidence
- **Current limitation**: No integrated posting engine; the addon only reads data and gives advice

### 8.2 Relisting Decision (via `relist.advise`)
- **HOLD**: Item already priced below competitor; no action needed
- **RELAST**: Undercut recommended when:
  - Heavy price gap (>= 20%) + enough time left (> 2h)
  - OR long queue behind own listing (items_ahead > own_quantity * queue_factor)
- **EXPIRE**: Time remaining <= 2h → allow to expire (deposit lost either way)
- **WATCH**: Default when:
  - Reference not verified
  - Insufficient evidence
  - Price too close to competitor (small gap)
  - Unknown variant or stale data

### 8.3 Relisting Costs (not automatically computed)
- **Deposit**: Sunk cost whether cancel or expire
- **Repeated deposits**: Each relisting risks another deposit
- **Time cost**: Relisting takes time; undercut may not sell faster
- **No automatic cost integration** in current code

## 9. Deposit & Commission Calculations

### 9.1 AH Commission
- **Rate**: 5% of sale price (standard WoW AH cut)
- **Used in**: Contemplation of net proceeds, not yet integrated into relist logic

### 9.2 Deposit
- **Varies by item level/quality**: Listed in game UI, not exposed in API
- **Not currently integrated** into decision logic; noted as "sunk cost" in comments

### 9.3 Acquisition Cost
- **Not tracked** in current system
- **Would require**: Player's cost basis per item (vendor price, crafting cost, previous purchase)

## 10. Recommendation Delivery

### 9.1 Addon Output
- **What it produces**: JSON with `listings`, `counts`, `advice` array
- **Each advice item contains**: item_id, item_name, own_unit_price_copper, lowest_unit_price_copper, gap_pct, above_cheapest_pct, relation, reference_status, advice, reason, review_required
- **Displayed**: In-game via `/dfaai` or export; not in tooltip by default

### 9.2 Alerts
- **`alerts.undercut_alerts`**: Generates Discord/webhook message with digest of undercut findings
- **`alerts.reset_alerts`**: Generates alerts for price resets (significant price jumps + supply drops)
- **`alerts.inventory_alerts`**: Generates alerts for inventory changes

### 9.3 Sentinel Recommendations
- **Separate from relist**: SentinelAPP policy: only `reference_status="verified"` may produce `cancel_and_relist` or `let_expire`; other statuses remain review-only
- **SentinelAPP false-relist investigation**: Previously fixed variant-blind reference matching

## 10. Capability Matrix

| Feature | Implemented | Partial | Missing | Blocked / Reason |
|---------|-------------|---------|---------|-----------------|
| **AH snapshot ingestion** | ✅ (`watch.task_ah` + `ah_undercut.scan_realm`) | | | Requires Blizzard API + `--sync-realms` for new realms |
| **Posted listing detection** | ✅ (`inventory.latest_posted`) | | | Requires addon run |
| **Mail invoice capture** | ✅ (`mail.py`) | | | Always available on login |
| **Journalator events** | ✅ (`watch.task_journal`) | | | Requires Journalator data |
| **Reference status determination** | ✅ (`relist.decide` + `_reference_status`) | | | 7 statuses; only verified = actionable |
| **Undercut detection** | ✅ (`ah_undercut.scan_realm` + `derive_observations`) | | | Requires API + posted list |
| **Undercut advice (HOLD/RELAST/EXPIRE/WATCH)** | ✅ (`relist.decide`) | | | Thresholds configurable but hardcoded defaults |
| **Price model predictions** | ✅ (Sentinel CatBoost + shipped linear) | | | Sentinel only for items with recorded sales |
| **Prediction intervals** | ✅ (Sentinel split-conformal 80%) | | | Not integrated into relist; shipped not integrated |
| **Time-to-sale ranking** | ✅ (C-index 0.931, censoring-aware) | | | **Not exported as exact duration**; status = `validated_ranking_only` |
| **Inventory quantities** | ✅ (`inventory_posted_listing`, `inventory_snapshot`) | | | Requires addon run; not real-time |
| **Deposit/commission modeling** | ❌ | | | Not integrated; noted as "sunk cost" only |
| **Posting decision engine** | ❌ | | | No auto-listing; only advice/reporting |
| **Relisting decision** | ✅ (`relist.decide` + `derive_undercut`) | | | Thresholds hardcoded; no economic cost modeling |
| **Liquidation policy** | ❌ | | | Not implemented |
| **Economic decision pipeline** | ❌ | | | Not integrated; separate components exist but not unified |
| **Chronological evaluation** | ✅ (comparison pipeline) | | | Uses same test rows for all models |
| **Decision ledger** | ❌ | | | Not persisted; no traceable record |
| **Webhook/Discord alerts** | ✅ (`alerts.deliver`) | | | Configurable via `DFA_DISCORD_WEBHOOK` env var |
| **Item variant handling** | ⚠️ (VARIANT_AMBIGUOUS status) | | | Does not auto-resolve; flags for review |
| **Realm-specific pricing** | ✅ (`watch.realms()`, `ah_realm` table) | | | Only realms in `ah_realm` table get scanned |

## 11. Actual Entry Points & Ownership

| Decision Area | Existing Module | Should Own (extend vs replace) |
|---------------|----------------|-------------------------------|
| AH snapshot + undercut | `watch.task_ah` + `ah_undercut.scan_realm` | Extend `watch.task_ah`; keep `ah_undercut` as scanner |
| Undercut advice | `relist.advise` + `relist.decide` | Keep `relist.decide`; extend `derive_undercut` with economic costs |
| Price predictions | `SentinelTrainer` + `SentinelPredictor` + `compare_price_models` | Keep Sentinel predictor; integrate comparison results |
| TTS ranking | `train_time_to_sale` + `sentinel.py` time-to-sale target | Keep status as `validated_ranking_only`; no exact duration export |
| Posting decisions | None (currently) | Build new `strategy_engine` or extend `relist` with posting logic |
| Relisting decisions | `relist.decide` + `derive_undercut` | Extend existing; add economic cost modeling |
| Deposit/commission modeling | ❌ | Add to `relist.decide` or new `economics` module |
| Decision ledger | ❌ | Add to existing `watch.state.json` or new trace file |

## 12. Missing Inputs Identified

| Missing Input | Where Needed | Current State |
|---------------|--------------|---------------|
| Economic costs (deposit, commission, acquisition) | Relist decision, posting strategy | Not tracked; noted as "sunk cost" only |
| Player's strategy preferences | All decision points | Not configured; defaults used |
| Player's inventory across all realms | Posting/relist strategy | Only realm where addon was run |
| Historical sale data per item | Price model confidence, liquidity | Only items with recorded sales have data |
| Demand evidence (sale rate, turnover) | Posting vs. no-list decision | Not distinguished from listing volume |
| Player's strategy preferences (risk tolerance, flip vs. flip) | All decisions | Not configured |
| Real AH API access for all realms | Underhook scanning | Only realms in `ah_realm` table |
| Player's current listing position per item | Undercut advice | Available only for owned items |
| Time-left precision | TTS, relist EXPIRE/RELAST | Blizzard API sends SHORT/MEDIUM/LONG; exact hours None if source lacks clock |

## 13. Summary

The existing project has a **functional undercut and relist recommendation engine** (`relist.decide` + `derive_undercut`) that operates on verified reference prices and AH snapshot data. However:

- **No integrated economic decision engine** — decisions are rule-based without cost modeling
- **No posting automation** — only advice/reporting
- **No economic modeling** — no deposit/commission/profit calculations
- **TTS not exported** — ranking only; absolute durations not quoted
- **No decision ledger** — no traceable record of recommendations and outcomes
- **Item variants not auto-resolved** — `VARIANT_AMBIGUOUS` → review only
- **Only verified references** can trigger `cancel_and_relist` / `let_expire`

The project is well-structured for incremental extension rather than replacement. The key entry points are:
- `watch.task_ah` → undercut scanning
- `relist.advise` + `relist.decide` → recommendations
- `SentinelPredictor` → price predictions
- `alerts.deliver` → webhook/Discord alerts

Next phase should focus on: integrating economic costs, extending the relist decision with posting logic, and adding the decision ledger.