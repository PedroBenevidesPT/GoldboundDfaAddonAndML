# DfA learning engine -- data-readiness report

Generated 2026-10-21 21:37:38Z against `C:\FarmAnalysis\tsm_markets.db` and `C:\economics\woweconomy\dfa\dfa.db`.

## 1. What is actually in the data

| source | from | to | rows |
|---|---|---|---:|
| auction_events (Journalator) | 2026-10-16T00:11:23Z | 2026-10-21T09:46:20Z | 7,861 |
| journal_stats (Journalator statistics) | 2026-10-21T21:30:27Z | 2026-10-21T21:30:27Z | 1,286 |
| journal (raw journal) | None | None | 0 |
| tsm_snapshot (TSM files) | 2026-10-16T03:36:05Z | 2026-10-21T07:31:39Z | 1,262 |
| tsm_price_change (TSM history) | 2026-10-16T03:36:05Z | 2026-10-21T07:31:39Z | 11,088,171 |
| market_snapshots (legacy region states) | 2026-07-27T00:59:53Z | 2026-10-13T00:21:32Z | 4 |

### 1.1 The signal families, as they really are

| column | what it really is | rows | non-zero | TSM sentinels | min | max | note |
|---|---|---:|---:|---:|---:|---:|---|
| `tsm_realm_price.market_value` | asking | 1,193,857 | 99.3% | 801 | 100 | 99,999,998,400 | the local market average (dbmarket) |
| `tsm_realm_price.min_buyout` | asking | 1,193,857 | 65.9% | 491 | 100 | 99,999,996,600 | the cheapest buyout listing (dbminbuyout) |
| `tsm_realm_price.recent` | asking | 1,193,857 | 65.9% | 488 | 100 | 99,999,996,600 | the decayed recent average (dbrecent) |
| `tsm_realm_price.historical` | asking | 1,193,857 | 99.9% | 1,042 | 100 | 99,999,998,400 | the long-run historical average (dbhistorical) |
| `tsm_region_price.market_value` | asking | 31,184 | 99.9% | 29 | 100 | 99,999,998,400 | the regional market average (dbregionmarketavg) |
| `tsm_region_price.historical` | asking | 31,184 | 99.9% | 24 | 100 | 99,999,996,000 | the regional historical average (dbregionhistorical) |
| `tsm_region_price.avg_sale_price` | transaction | 31,184 | 80.8% | 0 | 95 | 60,000,000,000 | the average price a sale closed at (dbregionsaleavg) |
| `tsm_region_price.sold_per_day` | estimate | 31,184 | 67.5% | 0 | 0 | 617,958 | the estimated daily volume (dbregionsoldperday) |
| `tsm_region_price.sale_rate` | estimate | 31,184 | 77.7% | 0 | 0 | 1 | the estimated sale frequency (dbregionsalerate) |
| `tsm_market.num_auctions` | asking | 295,009 | 75.5% | 0 | 1 | 2,470 | the visible listing count; no history, so it cannot be used as a point-in-time feature |
| `auction_events.unit_price_copper` | transaction | 7,861 | 59.9% | 0 | 100 | 18,346,718,600 | the price a buyer paid, per unit |
| `auction_events.fee_copper` | transaction | 7,861 | 7.3% | 0 | 10 | 1,356,680 | the house cut actually charged |
| `auction_events.net_proceeds_copper` | transaction | 7,861 | 7.3% | 0 | 240 | 25,787,492 | what the seller pocketed |
| `auction_events.market_ref_copper` | asking | 7,861 | 0.0% | 0 | - | - | the dbmarket captured at listing time by the addon |

`asking` means the number is what a seller listed, `transaction` means money changed hands, `estimate` means nobody observed it directly.  The engine never uses an `asking` column as a training label.

### 1.2 How often the market is actually sampled

| scope | snapshots | from | to |
|---|---:|---|---|
| `blackmoore` | 14 | 2026-10-18T09:36:04Z | 2026-10-21T07:31:39Z |
| `blackrock` | 14 | 2026-10-18T09:36:04Z | 2026-10-21T07:31:39Z |
| `twisting-nether` | 14 | 2026-10-16T03:36:05Z | 2026-10-21T07:31:39Z |
| `arakarahm` | 13 | 2026-10-18T08:36:07Z | 2026-10-21T04:31:38Z |
| `archimonde` | 13 | 2026-10-18T11:36:05Z | 2026-10-21T04:31:38Z |
| `chamber-of-aspects` | 13 | 2026-10-18T08:36:07Z | 2026-10-21T04:31:38Z |
| `confrérie-du-thorium` | 13 | 2026-10-18T09:36:04Z | 2026-10-21T04:31:38Z |
| `dentarg` | 13 | 2026-10-18T08:36:07Z | 2026-10-21T07:31:39Z |
| `dethecus` | 13 | 2026-10-18T09:36:04Z | 2026-10-21T04:31:38Z |
| `die-arguswacht` | 13 | 2026-10-18T09:36:04Z | 2026-10-21T04:31:38Z |
| `drakthul` | 13 | 2026-10-18T08:36:07Z | 2026-10-21T04:31:38Z |
| `emeriss` | 13 | 2026-10-18T08:36:07Z | 2026-10-21T07:31:39Z |

A realm is re-sampled every few hours; a *region* is sampled a couple of times a day at best, so a regional reference is often a day old.  The freshness of every reference is a feature, not an afterthought.

### 1.3 The transaction record

| event | rows | from | to | items | realms | with a price |
|---|---:|---|---|---:|---:|---:|
| listing | 4,120 | 2026-10-16T07:05:07Z | 2026-10-21T07:09:13Z | 924 | 10 | 4,120 |
| expire | 2,513 | 2026-10-16T12:24:59Z | 2026-10-21T09:46:20Z | 787 | 11 | 0 |
| cancel | 639 | 2026-10-16T00:18:18Z | 2026-10-20T11:54:43Z | 430 | 7 | 0 |
| sale | 577 | 2026-10-16T00:11:23Z | 2026-10-21T08:53:55Z | 215 | 8 | 577 |
| purchase | 12 | 2026-10-17T06:02:14Z | 2026-10-20T07:34:34Z | 10 | 6 | 12 |

### 1.4 The demand side (Data for Azeroth)

| table | rows |
|---|---:|
| `account` | 73,602 |
| `scanned` | 281,683 |
| `missing` | 59,006 |
| `missing_realm` | 16,711,440 |
| `catalogue` | 67,900 |
| `collector` | 912,406 |
| `owned` | 244,118 |
| `rarity` | 64,007 |
| `item_slot` | 59,006 |
| `account.scanned_at` | 2026-10-16T21:19:54+00:00 .. 2026-10-21T21:37:37+00:00 |

`missing_realm.n` counts the accounts of a realm's slice that still lack an appearance.  It has **no timestamp**: the demand of 16 October cannot be reconstructed from it, so a demand feature describes today's audience applied to a historical row.  It is flagged as not point-in-time and the report shows what including it does to the walk-forward scores.

## 2. Which models can be trained

| model | trainable | evidence | target | caveat |
|---|---|---|---|---|
| A -- realizable price | **yes** | 489 observed sales with a linked listing | net proceeds per unit (log) and gross unit price | a listing that never sold carries no price label, so the model describes the price *of a sale*, not the price of every listing |
| B -- probability of selling | **yes** | 24h: 3242 labelled rows; 48h: 3242 labelled rows; 72h: 3242 labelled rows; 168h: 3242 labelled rows; 336h: 3242 labelled rows | sold within 24/48/72/168/336 hours | 6225 listings are censored and 1380 are seller cancellations; neither is counted as a failure |
| C -- time to sale | **yes** | 4487 durations, of which 489 are observed sales and 3998 are right-censored | hours to sale, right-censored (SurvivalAft) | the outcome is recorded when the account opens the mail, so the duration is an upper bound on the true time to sale |
| D -- listing-price strategy | **yes** | a decision layer over A and B; it needs no labels of its own | the ask, with its sale probability and expected net gold | the counterfactual 'what if I had asked that' is not observable; the evaluation states the monotone-demand assumption instead of hiding it |

## 3. The dataset that was actually built

- rows (listings): **4,763** over 931 items and 10 realms
- window: 2026-10-16T07:05:07Z .. 2026-10-21T21:23:43Z
- observation period ends at 2026-10-21T21:32:37Z
- rows with a local TSM reference: 3,212
- rows with a regional TSM reference: 4,763
- rows with neither: 0
- price rows (an observed sale): **489**
- time-to-sale rows: 4,487 (3,998 right-censored)

| horizon | labelled rows | positive | censored (dropped) | cancelled (dropped) |
|---:|---:|---:|---:|---:|
| 24h | 3,242 | 485 | 1,245 | 276 |
| 48h | 3,242 | 488 | 1,245 | 276 |
| 72h | 3,242 | 489 | 1,245 | 276 |
| 168h | 3,242 | 489 | 1,245 | 276 |
| 336h | 3,242 | 489 | 1,245 | 276 |

## 4. The outcome linkage

- postings seen: **4,766**
- matched to a sale: **492**
- matched to an expiry: **2,794**
- matched to a cancellation: **279**
- censored (no outcome observed): **1,245**
- outcomes with no matching posting: 903
- **sales that closed at exactly the posted buyout: 491 of 492 (99.8%)** -- the link is not a guess: a wrong pairing would scatter the prices

## 5. How far each asking reference sits from what buyers paid

| reference | median vs the realised price | quartiles | share above the price paid | verdict |
|---|---:|---|---:|---|
| dbmarket (local asking) | 2.479 | 1.222 .. 10.143 | 90.6% | inflated asking reference |
| dbrecent (local recent) | 1.588 | 1.052 .. 7.413 | 92.3% | inflated asking reference |
| dbhistorical (local historical) | 3.498 | 1.270 .. 6.656 | 90.6% | inflated asking reference |
| dbminbuyout (local floor) | 2.018 | 1.052 .. 7.413 | 92.3% | inflated asking reference |
| dbregionmarketavg (regional asking) | 1.374 | 1.110 .. 2.468 | 84.0% | inflated asking reference |
| dbregionhistorical (regional historical) | 1.338 | 1.035 .. 1.964 | 77.9% | inflated asking reference |
| dbregionsaleavg (regional sales) | 1.060 | 0.781 .. 1.415 | 54.6% | commercially defensible |

A ratio above one means the reference promises more than the market pays; a ratio near one means the reference is commercially defensible.  The engine lets CatBoost learn from the transaction outcomes when each family is trustworthy, instead of averaging the families together.

## 6. What is missing, and what it costs

| what is missing | why | what the engine does about it |
|---|---|---|
| the auction duration of a posting | Journalator's posting hook does not record it | the duration is inferred from the expiry delay, which clusters on 12/24/48 h |
| the listing count over time | `tsm_market.num_auctions` has no timestamp and no history | market depth cannot be used as a point-in-time feature; only the regional sold-per-day and sale-rate proxies are available |
| the item id on a sale invoice | the invoice mail carries no item link in this installation | sales are matched by item name, which the item catalogue resolves |
| the true moment of a sale or an expiry | both are recorded when the account opens the mail | every duration is an upper bound; the models are kept conservative |
| the demand of a past date | `missing_realm` is a live aggregate with no timestamp | the demand feature is not point-in-time and is flagged as such |
| a DfA demand for every item | only transmog appearances, mounts and recipes are scanned | most auction items have no demand counter at all, which is itself a signal |
| the sample size behind `dbregionsaleavg` | TSM exposes the average but not how many sales produced it | the width of the estimate is carried by the price model itself: the quantile models give a p10-p90 band, and a thin or stale sample shows up as a wide band rather than as a confident number |

## 7. Every feature, and where it came from

`point-in-time` says whether the value was already true at the moment the item was listed.  A **no** means the feature describes a later state of the world and is therefore a mild extrapolation, flagged so that it can be switched off and measured.

| feature | what it is | source | point-in-time |
|---|---|---|---|
| `ask_over_local_market` | the ask over the local market average | derived | yes |
| `ask_over_region_market` | the ask over the regional market average | derived | yes |
| `ask_over_region_sale_avg` | the ask over the regional sale average | derived | yes |
| `ask_over_tsm_normal` | the ask over the Normal level | derived | yes |
| `ask_unit_log` | log of the per-unit buyout asked for | Journalator posting record | yes |
| `bid_to_buyout` | the bid as a fraction of the buyout | Journalator posting record (empty in this archive) | yes |
| `day_of_week` | the day of the posting | derived | yes |
| `deposit_log` | log of the deposit; the deposit is 15 % of the item's vendor price, so it carries the item's value without carrying the ask | Journalator posting record | yes |
| `deposit_ratio` | the deposit as a fraction of the asked total | Journalator posting record | yes |
| `dfa_appearances_global_rate` | the share of all collectors that lack the transmog | dfa.db::missing | **no** -- a live aggregate |
| `dfa_appearances_known` | whether the transmog demand is known at all | derived | **no** |
| `dfa_appearances_realm_missing` | how many collectors of the realm lack it | dfa.db::missing_realm | **no** -- a live aggregate |
| `dfa_appearances_realm_rate` | the share of the realm's collectors that still lack the transmog | dfa.db::missing_realm | **no** -- a live aggregate |
| `dfa_known` | whether any demand counter applies to the item | derived | **no** |
| `dfa_mounts_global_rate` | the same for mounts | dfa.db::missing | **no** -- a live aggregate |
| `dfa_mounts_known` | whether the mount demand is known | derived | **no** |
| `dfa_mounts_realm_missing` | the same for mounts | dfa.db::missing_realm | **no** -- a live aggregate |
| `dfa_mounts_realm_rate` | the same for mounts | dfa.db::missing_realm | **no** -- a live aggregate |
| `dfa_recipes_global_rate` | the same for recipes | dfa.db::missing | **no** -- a live aggregate |
| `dfa_recipes_known` | whether the recipe demand is known | derived | **no** |
| `dfa_recipes_realm_missing` | the same for recipes | dfa.db::missing_realm | **no** -- a live aggregate |
| `dfa_recipes_realm_rate` | the same for recipes | dfa.db::missing_realm | **no** -- a live aggregate |
| `hour_of_day` | the hour of the posting, UTC | derived | yes |
| `is_stack` | whether the listing is a stack | derived | yes |
| `local_age_h` | hours since that local snapshot | derived | yes |
| `local_has_snapshot` | whether any local snapshot covered that moment | derived | yes |
| `local_historical_log` | log of dbhistorical, the long-run average | tsm_price_change:realm_items | yes |
| `local_market_chg` | the change in the local asking price since the previous snapshot | derived | yes |
| `local_market_log` | log of dbmarket, the local market average | tsm_price_change:realm_items | yes |
| `local_market_over_historical` | the local asking price over its own history | derived | yes |
| `local_min_buyout_log` | log of dbminbuyout, the cheapest buyout listing | tsm_price_change:realm_items | yes |
| `local_present` | whether a local reference existed at that moment | derived | yes |
| `local_recent_log` | log of dbrecent, the decayed recent average | tsm_price_change:realm_items | yes |
| `local_recent_over_market` | the local recent average over the local market average | derived | yes |
| `local_recent_over_region_sale_avg` | the local recent average over the regional sale average | derived | yes |
| `local_stale` | whether the local reference is older than the freshness cap | derived | yes |
| `qty` | the stack size | Journalator posting record | yes |
| `qty_log` | log of the stack size | Journalator posting record | yes |
| `ref_count` | how many references existed at all | derived | yes |
| `ref_dispersion` | the relative spread of every reference that existed | derived | yes |
| `region_age_h` | hours since that regional snapshot | derived | yes |
| `region_has_snapshot` | whether any regional snapshot covered that moment | derived | yes |
| `region_historical_log` | log of dbregionhistorical | tsm_price_change:region_items | yes |
| `region_market_chg` | the change in the regional asking price since the previous snapshot | derived | yes |
| `region_market_log` | log of dbregionmarketavg | tsm_price_change:region_items | yes |
| `region_market_over_historical` | the regional asking price over its own history | derived | yes |
| `region_present` | whether a regional reference existed at that moment | derived | yes |
| `region_sale_avg_chg` | the change in the regional sale average since the previous snapshot | derived | yes |
| `region_sale_avg_log` | log of dbregionsaleavg, the average a sale closed at | tsm_price_change:region_items | yes |
| `region_sale_avg_over_historical` | the regional sale average over its own history | derived | yes |
| `region_sale_avg_over_market` | the transaction family over the asking family: how far the asking price runs ahead of what buyers pay | derived | yes |
| `region_sale_rate` | dbregionsalerate, the estimated sale frequency | tsm_price_change:region_items | yes |
| `region_sold_per_day` | dbregionsoldperday, the estimated daily volume | tsm_price_change:region_items | yes |
| `region_stale` | whether the regional reference is older than the cap | derived | yes |
| `spread_local_vs_region` | the gap between the local and regional asking prices | derived | yes |
| `spread_market_vs_sale` | the gap between the regional asking and sale averages | derived | yes |
| `tsm_maximum_over_normal` | the width of the posting band | derived | yes |
| `tsm_normal_log` | log of the Normal level, after the sale-rate discount | woweconomy.pricing.tsm_strings | yes |
| `tsm_normal_over_region_market` | the Normal level against the regional ask: how far the policy sits from the market | derived | yes |
| `tsm_normalmarket_log` | log of the wall: the local ask capped by the regional one and floored at 85 % of it | woweconomy.pricing.tsm_strings | yes |
| `unit_bid_log` | log of the per-unit opening bid | Journalator posting record (empty in this archive) | yes |

