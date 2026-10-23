# DfA pricing -- recommendations, evidence and confidence

Generated 2026-10-22 02:09:57Z.  Dataset schema 2, engine 2.0.0, generated at 1792634986.

**This is analysis, not action.**  Nothing here posts, cancels, undercuts or reprices an auction.  The engine answers questions; a human decides.

## The dataset

- 8 realms, 237 (realm, item) records, 31 appearances
- 557 observed transactions behind it, between 1 and 14 per record
- mean confidence 26%, horizon 24h
- the model prices **31,184** items from the regional references, in every realm the dataset maps (**267** realm names over **92** auction houses); the per-realm records cover only the 237 (realm, item) pairs the account traded
- checksum `860080dc6f7c64ffa755b378332dda8d84ad403185957c9186d47ee9d7469107`

## Calibrated price intervals

Every exported price carries a split-conformal interval.  The width is calibrated on one block and the coverage is measured on a later block, so the two numbers are separate claims.

| model | nominal level | half-width (log) | measured coverage | held-out sales |
|---|---:|---:|---:|---:|
| record price (CatBoost) | 80% | 0.637 | 78% | 123 |
| shipped model | 80% | 0.720 | 85% | 132 |
| Sentinel realized price | 80% | 1.043 | 87% | 76 |

## What was loaded from the TSM mirror

| what | value |
|---|---:|
| TSM snapshots | **1,262** over **93** scopes |
| `commodities` | 3 over 1 scope(s) |
| `realm_items` | 1,072 over 92 scope(s) |
| `realm_pets` | 184 over 92 scope(s) |
| `region_items` | 2 over 1 scope(s) |
| `region_pets` | 1 over 1 scope(s) |
| price changes (the point-in-time history) | 11,088,171 |
| realm prices | 1,193,857 over 92 scopes |
| regional prices | 31,184 |
| latest regional snapshot | 2026-10-18T09:37:03Z |
| realms with transactions (the training labels) | 11 -> **11** auction-house scopes |

The learning engine loads only the scopes that appear in `auction_events`, because those are the only realms with outcomes to learn from; the region is always loaded.  The pricing engine loads the scopes of the realms with a record (the only ones a per-realm record can use) plus the region, and reads the realm -> auction-house map whole.  The shipped model uses the **regional** references, which are the same in every realm -- that is why it can answer for items the account never traded, and why the per-realm records are the exception and not the coverage.

## The realms

| realm | auction house | records | own sales | newest observation |
|---|---|---:|---:|---|
| Argent Dawn | `argent-dawn` | 7 | 7 | 2026-10-20 11:25 |
| Auchindoun | `auchindoun` | 17 | 18 | 2026-10-21 07:00 |
| Defias Brotherhood | `scarshield-legion` | 1 | 1 | 2026-10-19 17:20 |
| Draenor | `draenor` | 1 | 1 | 2026-10-20 16:49 |
| Dragonmaw | `spinebreaker` | 174 | 489 | 2026-10-21 07:12 |
| Kazzak | `kazzak` | 1 | 1 | 2026-10-19 15:05 |
| Silvermoon | `silvermoon` | 7 | 11 | 2026-10-21 06:57 |
| Twisting Nether | `twisting-nether` | 29 | 29 | 2026-10-18 20:51 |

## A sample of the recommendations

The items with the most evidence first.  Every row is one item in one realm: the estimate, how much it is worth, the chance of selling, and the warnings.

### Dragonmaw / item 2318

- **recommended ask**: 1.9g per unit
- **estimated realizable price**: 0.6g net of the house cut
- **80% range**: 0.3g - 1.2g (78% of 123 held-out sales)
- **expected realized**: 1.2g
- **80% realized range**: 0.8g - 1.7g (78% of 46 held-out sales)
- **confidence**: 64%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.2 h
- **sample**: 14 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 12620, `dbregionmarketavg` 11522, `dbregionsaleavg` 11115, `dbregionsoldperday` 39005
- **why**:
    - the price model expects a unit to fetch about 6236 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 87%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 14 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2319

- **recommended ask**: 3.7g per unit
- **estimated realizable price**: 1.2g net of the house cut
- **80% range**: 0.7g - 2.4g (78% of 123 held-out sales)
- **expected realized**: 3.0g
- **80% realized range**: 2.1g - 4.3g (78% of 46 held-out sales)
- **confidence**: 62%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.1 h
- **sample**: 13 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 24558, `dbregionmarketavg` 26299, `dbregionsaleavg` 21945, `dbregionsoldperday` 21228
- **why**:
    - the price model expects a unit to fetch about 12473 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 80%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 13 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 5465

- **recommended ask**: 0.5g per unit
- **estimated realizable price**: 0.0g net of the house cut
- **80% range**: 0.0g - 0.1g (78% of 123 held-out sales)
- **expected realized**: 0.1g
- **80% realized range**: 0.0g - 0.4g (87% of 76 held-out sales)
- **confidence**: 43%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.8 h
- **sample**: 13 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 1180, `dbregionmarketavg` 5492, `dbregionsaleavg` 879, `dbregionsoldperday` 687
- **why**:
    - the price model expects a unit to fetch about 474 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 73%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 13 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure, speculative-reference, contradictory-references

### Dragonmaw / item 2592

- **recommended ask**: 0.1g per unit
- **estimated realizable price**: 0.0g net of the house cut
- **80% range**: 0.0g - 0.1g (78% of 123 held-out sales)
- **expected realized**: 0.1g
- **80% realized range**: 0.0g - 0.2g (87% of 76 held-out sales)
- **confidence**: 58%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.3 h
- **sample**: 11 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 1084, `dbregionmarketavg` 952, `dbregionsaleavg` 665, `dbregionsoldperday` 52811
- **why**:
    - the price model expects a unit to fetch about 331 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 71%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 11 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2251

- **recommended ask**: 0.4g per unit
- **estimated realizable price**: 0.1g net of the house cut
- **80% range**: 0.1g - 0.2g (78% of 123 held-out sales)
- **expected realized**: 0.3g
- **80% realized range**: 0.1g - 0.9g (87% of 76 held-out sales)
- **confidence**: 56%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.4 h
- **sample**: 10 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 3698, `dbregionmarketavg` 4495, `dbregionsaleavg` 2936, `dbregionsoldperday` 3713
- **why**:
    - the price model expects a unit to fetch about 968 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 87%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 10 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2589

- **recommended ask**: 0.2g per unit
- **estimated realizable price**: 0.1g net of the house cut
- **80% range**: 0.0g - 0.1g (78% of 123 held-out sales)
- **expected realized**: 0.0g
- **80% realized range**: 0.0g - 0.1g (87% of 76 held-out sales)
- **confidence**: 56%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.2 h
- **sample**: 10 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 400, `dbregionmarketavg` 482, `dbregionsaleavg` 285, `dbregionsoldperday` 617958
- **why**:
    - the price model expects a unit to fetch about 507 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 79%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 10 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 22644

- **recommended ask**: 1.9g per unit
- **estimated realizable price**: 0.6g net of the house cut
- **80% range**: 0.3g - 1.1g (78% of 123 held-out sales)
- **expected realized**: 1.3g
- **80% realized range**: 0.9g - 1.8g (78% of 46 held-out sales)
- **confidence**: 56%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 3.9 h
- **sample**: 10 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 18891, `dbregionmarketavg` 16177, `dbregionsaleavg` 16625, `dbregionsoldperday` 2191
- **why**:
    - the price model expects a unit to fetch about 5857 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 82%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 10 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 783

- **recommended ask**: 4.4g per unit
- **estimated realizable price**: 1.5g net of the house cut
- **80% range**: 0.8g - 2.8g (78% of 123 held-out sales)
- **expected realized**: 3.1g
- **80% realized range**: 1.1g - 8.7g (87% of 76 held-out sales)
- **confidence**: 53%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.7 h
- **sample**: 9 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 28900, `dbregionmarketavg` 32510, `dbregionsaleavg` 23370, `dbregionsoldperday` 2119
- **why**:
    - the price model expects a unit to fetch about 14598 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 75%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 9 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2450

- **recommended ask**: 3.5g per unit
- **estimated realizable price**: 1.2g net of the house cut
- **80% range**: 0.6g - 2.2g (78% of 123 held-out sales)
- **expected realized**: 2.5g
- **80% realized range**: 1.7g - 3.6g (78% of 46 held-out sales)
- **confidence**: 53%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.9 h
- **sample**: 9 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 28500, `dbregionmarketavg` 24396, `dbregionsaleavg` 23216, `dbregionsoldperday` 2057
- **why**:
    - the price model expects a unit to fetch about 11815 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 85%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 9 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 4232

- **recommended ask**: 3.0g per unit
- **estimated realizable price**: 1.0g net of the house cut
- **80% range**: 0.5g - 1.9g (78% of 123 held-out sales)
- **expected realized**: 1.8g
- **80% realized range**: 1.2g - 2.5g (78% of 46 held-out sales)
- **confidence**: 53%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 4.0 h
- **sample**: 9 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 20000, `dbregionmarketavg` 19671, `dbregionsaleavg` 18600, `dbregionsoldperday` 27
- **why**:
    - the price model expects a unit to fetch about 10142 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 39%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 9 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 6889

- **recommended ask**: 12.3g per unit
- **estimated realizable price**: 4.1g net of the house cut
- **80% range**: 2.2g - 7.7g (78% of 123 held-out sales)
- **expected realized**: 6.8g
- **80% realized range**: 4.8g - 9.8g (78% of 46 held-out sales)
- **confidence**: 53%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.7 h
- **sample**: 9 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 80100, `dbregionmarketavg` 91466, `dbregionsaleavg` 74480, `dbregionsoldperday` 13894
- **why**:
    - the price model expects a unit to fetch about 40849 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 75%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 9 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2452

- **recommended ask**: 3.4g per unit
- **estimated realizable price**: 1.1g net of the house cut
- **80% range**: 0.6g - 2.2g (78% of 123 held-out sales)
- **expected realized**: 2.2g
- **80% realized range**: 1.5g - 3.1g (78% of 46 held-out sales)
- **confidence**: 50%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.6 h
- **sample**: 8 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 25379, `dbregionmarketavg` 22080, `dbregionsaleavg` 20140, `dbregionsoldperday` 18715
- **why**:
    - the price model expects a unit to fetch about 11481 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 86%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 8 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2453

- **recommended ask**: 3.2g per unit
- **estimated realizable price**: 1.1g net of the house cut
- **80% range**: 0.6g - 2.0g (78% of 123 held-out sales)
- **expected realized**: 2.1g
- **80% realized range**: 1.5g - 3.0g (78% of 46 held-out sales)
- **confidence**: 50%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.6 h
- **sample**: 8 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 21700, `dbregionmarketavg` 19933, `dbregionsaleavg` 18599, `dbregionsoldperday` 2897
- **why**:
    - the price model expects a unit to fetch about 10827 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 87%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 8 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 5504

- **recommended ask**: 0.9g per unit
- **estimated realizable price**: 0.3g net of the house cut
- **80% range**: 0.2g - 0.5g (78% of 123 held-out sales)
- **expected realized**: 0.6g
- **80% realized range**: 0.2g - 1.8g (87% of 76 held-out sales)
- **confidence**: 50%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.4 h
- **sample**: 8 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 6400, `dbregionmarketavg` 8661, `dbregionsaleavg` 5801, `dbregionsoldperday` 1302
- **why**:
    - the price model expects a unit to fetch about 2839 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 85%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 8 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2674

- **recommended ask**: 4.6g per unit
- **estimated realizable price**: 1.2g net of the house cut
- **80% range**: 0.6g - 2.2g (78% of 123 held-out sales)
- **expected realized**: 2.3g
- **80% realized range**: 0.8g - 6.5g (87% of 76 held-out sales)
- **confidence**: 35%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 3.0 h
- **sample**: 8 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 16500, `dbregionmarketavg` 45816, `dbregionsaleavg` 15175, `dbregionsoldperday` 236
- **why**:
    - the price model expects a unit to fetch about 11626 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 78%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 8 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 785

- **recommended ask**: 3.3g per unit
- **estimated realizable price**: 1.1g net of the house cut
- **80% range**: 0.6g - 2.1g (78% of 123 held-out sales)
- **expected realized**: 2.0g
- **80% realized range**: 1.4g - 2.9g (78% of 46 held-out sales)
- **confidence**: 47%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.8 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 21177, `dbregionmarketavg` 20316, `dbregionsaleavg` 18050, `dbregionsoldperday` 1097
- **why**:
    - the price model expects a unit to fetch about 11148 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 69%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 1206

- **recommended ask**: 16.6g per unit
- **estimated realizable price**: 5.5g net of the house cut
- **80% range**: 2.9g - 10.5g (78% of 123 held-out sales)
- **expected realized**: 9.5g
- **80% realized range**: 6.6g - 13.7g (78% of 46 held-out sales)
- **confidence**: 47%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 3.0 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 163864, `dbregionmarketavg` 150017, `dbregionsaleavg` 142500, `dbregionsoldperday` 379
- **why**:
    - the price model expects a unit to fetch about 55359 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 63%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 2934

- **recommended ask**: 1.4g per unit
- **estimated realizable price**: 0.5g net of the house cut
- **80% range**: 0.3g - 0.9g (78% of 123 held-out sales)
- **expected realized**: 0.9g
- **80% realized range**: 0.6g - 1.2g (78% of 46 held-out sales)
- **confidence**: 47%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.9 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 9500, `dbregionmarketavg` 9754, `dbregionsaleavg` 7600, `dbregionsoldperday` 874
- **why**:
    - the price model expects a unit to fetch about 4755 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 63%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 3355

- **recommended ask**: 7.4g per unit
- **estimated realizable price**: 2.5g net of the house cut
- **80% range**: 1.3g - 4.7g (78% of 123 held-out sales)
- **expected realized**: 4.4g
- **80% realized range**: 3.1g - 6.3g (78% of 46 held-out sales)
- **confidence**: 47%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 3.1 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 46515, `dbregionmarketavg` 47916, `dbregionsaleavg` 40945, `dbregionsoldperday` 429
- **why**:
    - the price model expects a unit to fetch about 24809 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 38%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 4306

- **recommended ask**: 0.1g per unit
- **estimated realizable price**: 0.0g net of the house cut
- **80% range**: 0.0g - 0.1g (78% of 123 held-out sales)
- **expected realized**: 0.1g
- **80% realized range**: 0.0g - 0.2g (87% of 76 held-out sales)
- **confidence**: 47%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 3.1 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 435, `dbregionmarketavg` 380, `dbregionsaleavg` 300, `dbregionsoldperday` 132093
- **why**:
    - the price model expects a unit to fetch about 296 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 84%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 1015

- **recommended ask**: 0.7g per unit
- **estimated realizable price**: 0.1g net of the house cut
- **80% range**: 0.0g - 0.2g (78% of 123 held-out sales)
- **expected realized**: 0.2g
- **80% realized range**: 0.1g - 0.6g (87% of 76 held-out sales)
- **confidence**: 33%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 3.1 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 2186, `dbregionmarketavg` 7154, `dbregionsaleavg` 2293, `dbregionsoldperday` 387
- **why**:
    - the price model expects a unit to fetch about 901 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 64%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure, speculative-reference, contradictory-references, low-confidence

### Dragonmaw / item 2675

- **recommended ask**: 0.8g per unit
- **estimated realizable price**: 0.2g net of the house cut
- **80% range**: 0.1g - 0.4g (78% of 123 held-out sales)
- **expected realized**: 0.2g
- **80% realized range**: 0.1g - 0.6g (87% of 76 held-out sales)
- **confidence**: 33%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.9 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 1917, `dbregionmarketavg` 8421, `dbregionsaleavg` 1330, `dbregionsoldperday` 1302
- **why**:
    - the price model expects a unit to fetch about 1884 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 79%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure, speculative-reference, contradictory-references, low-confidence

### Dragonmaw / item 3173

- **recommended ask**: 0.4g per unit
- **estimated realizable price**: 0.1g net of the house cut
- **80% range**: 0.1g - 0.2g (78% of 123 held-out sales)
- **expected realized**: 0.2g
- **80% realized range**: 0.1g - 0.6g (87% of 76 held-out sales)
- **confidence**: 33%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 3.2 h
- **sample**: 7 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 1000, `dbregionmarketavg` 4070, `dbregionsaleavg` 813, `dbregionsoldperday` 472
- **why**:
    - the price model expects a unit to fetch about 1202 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 53%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 7 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure, speculative-reference, contradictory-references, low-confidence

### Dragonmaw / item 3356

- **recommended ask**: 7.0g per unit
- **estimated realizable price**: 2.3g net of the house cut
- **80% range**: 1.2g - 4.4g (78% of 123 held-out sales)
- **expected realized**: 4.3g
- **80% realized range**: 3.0g - 6.2g (78% of 46 held-out sales)
- **confidence**: 43%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 2.9 h
- **sample**: 6 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 41800, `dbregionmarketavg` 44236, `dbregionsaleavg` 38000, `dbregionsoldperday` 1548
- **why**:
    - the price model expects a unit to fetch about 23351 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 78%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 6 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

### Dragonmaw / item 3730

- **recommended ask**: 0.1g per unit
- **estimated realizable price**: 0.0g net of the house cut
- **80% range**: 0.0g - 0.1g (78% of 123 held-out sales)
- **expected realized**: 0.1g
- **80% realized range**: 0.0g - 0.2g (87% of 76 held-out sales)
- **confidence**: 43%
- **chance of selling in 24h**: 63%
- **expected time to sale**: 5.5 h
- **sample**: 6 sale(s) of this item in this realm
- **market references**: `dbregionhistorical` 900, `dbregionmarketavg` 641, `dbregionsaleavg` 1010, `dbregionsoldperday` 38
- **why**:
    - the price model expects a unit to fetch about 288 copper net of the house cut
    - at that ask the chance of selling inside 24h is about 38%
    - the sale probability comes from the sale-probability model
    - the account has sold this item here 6 time(s)
- **warnings**: time-to-sale-unknown, undercut-is-market-pressure

## What the numbers mean

| field | what it is | what it is not |
|---|---|---|
| estimated realizable price | the median of what a unit of this item fetched in this realm, net of the house cut, over the transactions the model was trained on | it is not an asking price, and it is not a promise |
| calibrated range | the multiplicative interval the model's held-out errors support, with the coverage it actually delivered | it is not a guarantee, and a missing range means unmeasured, not zero uncertainty |
| confidence | how much the estimate is worth, from the sample behind it, the age of the evidence and how much the market references disagree | it is not a probability of selling |
| chance of selling | the model's probability that a listing at the recommended ask sells inside the horizon | it is not a guarantee, and it is not computed for items the model has never seen |
| expected time to sale | only present when the outcome data supports it | the data does **not** support it here, so it is absent |
| Data for Azeroth interest | how many collectors still lack the cosmetic set | it is **not** evidence of auction house demand |

