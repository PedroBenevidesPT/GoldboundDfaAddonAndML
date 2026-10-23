# Dragonmaw auction intelligence — real-capture validation

Date: 2026-10-21 (Europe). Realm: Dragonmaw (EU), connected realm 3656.
All numbers below come from the live SavedVariables written by the game at
09:26 and from one live Blizzard API pull. Nothing in this report is a fixture.

## Verdict

The pipeline works end to end, with three honest caveats:

* seller capture works (96 tooltip lines seen, 4 unique, 92 duplicates);
* only **two** of those four observations are a competitor; the other two were
  our own auctions, printed by the client as `Seller: You`;
* the competitor sample is too small to support an undercut claim. The report
  therefore separates what was observed from what can only be inferred.

The most valuable fix found in this session is not a model: it is the mail
buyer gap. 611 of 762 Dragonmaw sales still have no counterparty (the
journal keeps growing: it was 757 when this analysis was written).

## 1. What was captured (confirmed facts)

### 1.1 Addon session

| Item | Value |
|---|---|
| SavedVariables file | `WTF/Account/1186541918#1/SavedVariables/DFA_AuctionIntelligence.lua` |
| Events read / already present / new | 8,144 / 8,139 / 5 |
| Item ids filled from names | 556 |
| Seller observations | 4 unique, 96 tooltip lines, 92 duplicates |
| Seller capture source | `tooltip` (all 4) |
| Inventory snapshots in the session | 3 (`login`, `manual`, `logout`) |
| Latest inventory rows | 66 (51 bag rows, 15 bank rows, 0 posted) |

Seller observations:

| Observed at | Seller | Item | Raw line |
|---|---|---|---|
| 1792570807 | `Sotakettu-Stormreaver` | 14120 Aboriginal Robe of the Fireflash | `Seller: Sotakettu-Stormreaver` |
| 1792570809 | `You` | 14120 Aboriginal Robe of the Fireflash | `Seller: You` |
| 1792570824 | `Sotakettu-Stormreaver` | 14169 Aboriginal Shoulder Pads | `Seller: Sotakettu-Stormreaver` |
| 1792570825 | `You` | 14169 Aboriginal Shoulder Pads | `Seller: You` |

`You` is the client's label for our own auction. It is not a competitor and is
now rejected at capture time; the Python side also excludes any row whose
seller name equals the character name or the literal `You`.

### 1.2 Blizzard auction snapshot

| Field | Value |
|---|---|
| Snapshot id | `0a3bbf85bcb1e735d0fb17d1` |
| Captured at | 1792571001 |
| API `Last-Modified` | Wed, 21 Oct 2026 07:31:39 GMT |
| Auctions stored | 18,993 |
| Distinct items | 6,544 |
| Commodity rows | 0 |
| Rejected rows | 0 |
| Unknown `time_left` values | 0 |
| Unit price source | `derived_from_buyout` (18,993) |
| Rows with bonus lists | 7,704 |
| Rows with modifiers | 17,391 |

Original `time_left` categories were preserved exactly:

| Raw value | Rows | Share |
|---|---:|---:|
| `VERY_LONG` | 11,071 | 58.29% |
| `LONG` | 6,505 | 34.25% |
| `MEDIUM` | 1,255 | 6.61% |
| `SHORT` | 162 | 0.85% |

All rows carry `quantity = 1` because this is the non-commodity book. The
connected-realm endpoint does not return the region-wide commodity book, so
`commodity_count = 0` is real, not a parser failure.

### 1.3 Inventory and reconciliation

| Snapshot | Reason | Rows | Bags | Bank | Posted | Available |
|---|---|---:|---:|---:|---:|---|
| 1792570738 | login | 0 | 0 | — | 0 | bags, posted |
| 1792571176 | manual | 376 | 51 | 15 | 310 | bags, bank, posted |
| 1792571187 | logout | 66 | 51 | 15 | 0 | bags, bank, posted |

The `manual → logout` pair was reconciled against the journal:

* bags and bank rows are identical (51/15);
* the journal contains **0 events** in the 11-second window;
* 310 posted rows disappear, producing **641 unexplained units**.

That is not stock movement. The Auction House was closed at logout, so
`C_AuctionHouse.GetNumOwnedAuctions()` answered 0. The addon now requires a
completed owned-auction query (`HasFullOwnedAuctionResults`) before it treats
zero as an empty shelf. The next reload will record `posted` as unreadable
instead of empty.

## 2. Seller intelligence

### 2.1 Confirmed

* 4 seller observations were stored with their raw tooltip lines.
* 2 are our own listings (`You`) and are excluded from competitor counts.
* 1 competitor name was observed: `Sotakettu-Stormreaver`, on 2 items.
* 210 buyer-identified Dragonmaw sales in the last 60 days were examined.
* **0** sales matched a seller observation.
* 0 confirmed sales exist for items 14120 or 14169.

### 2.2 Inferred (explicitly not facts)

| Claim | Status | Evidence | Why it is only an inference |
|---|---|---|---|
| `Sotakettu-Stormreaver` is an active transmog competitor | observation | 2 tooltip observations, 2 items | Two observations are a sample, not a market share |
| Sotakettu undercut us | **not claimed** | — | the capture stored no price and no auction id |
| Buyer relisted our item | **not claimed** | 0 matches | no buyer/seller name match in the window |

### 2.3 Item ladders at the snapshot

| Item | Listings | Floor | Second | Floor gap | Units at floor | Time-left |
|---|---:|---:|---:|---:|---:|---|
| 14120 Aboriginal Robe of the Fireflash | 2 | 1,608.85 g | 1,608.85 g | 0.00% | 1 | `VERY_LONG` ×2 |
| 14169 Aboriginal Shoulder Pads | 3 | 827.94 g | 827.94 g | 0.00% | 1 | `LONG` ×1, `VERY_LONG` ×2 |

Every listing sits at the same price. That is **price clustering on the
current shelf**, not evidence of an undercut event. A one-snapshot observation
cannot show who moved first or whether the earlier listing was already sold.

### 2.4 What is needed for a real competitor report

1. More hover samples (the current sample is 2 competitor observations).
2. The tooltip price and auction id on each observation, so an observation can
   be tied to a ladder position.
3. At least two snapshots, so a price change has a direction.
4. Mail buyer names, so our confirmed sales can be matched to sellers.

## 3. Realm-specific ML

Dataset: 4,204 rows, 922 items, 10 realms, 462 priced rows.

Only Dragonmaw had enough priced sales to model:

| Metric | Dragonmaw |
|---|---:|
| Realm rows / priced sales | 2,159 / 406 |
| Expanding-window folds | 2 |
| Untouched holdout | 84 sales, day 20746 |
| Holdout log-RMSE | **0.6179** |
| Training-median baseline | 2.3613 |
| Item-median baseline | 1.0776 |
| Last-price baseline | 1.9875 |
| Skill vs training median | **73.8%** lower RMSE |
| MAE (log) | 0.4086 |
| Median predicted/realised ratio | 1.0336 |
| Within 25% / 50% | 46.4% / 75.0% |
| Fold log-RMSE (earlier windows) | 0.8246 |
| Fold baseline | 2.1953 |

The holdout is the *last* day with enough priced sales. Folds are built only on
days **before** that day, so no fold trains or validates inside the holdout.
The fixed 25%-of-days window would have left the holdout empty (the last day had
fewer than 10 priced sales); the window grows backwards until it has at least 10
sales, and every training row is still strictly earlier.

Nine realms remained unmodelled, with 0–23 priced sales each:
Antonidas, Argent Dawn, Auchindoun, Blackrock, Defias Brotherhood, Draenor,
Kazzak, Silvermoon, Twisting Nether. No pooled fallback was used.

## 4. Farming validation

Inputs: Azshara, Ashenvale, Blackfathom Deeps; Dragonmaw; the live AH snapshot;
the 66-row inventory.

| Metric | Value |
|---|---:|
| NPCs ranked | 257 |
| Unique priced items | 1,648 |
| High-confidence prices | 29 |
| Low-confidence prices | 1,619 |
| Items with listing data | 671 |
| Items without listing data | 977 |
| Items with **both** a confirmed realm price and competition | **0** |
| NPCs with a measured kill rate | 0 (all 257 use the 20/h default) |

The top of the ranking is not actionable as printed:

| NPC | Realisable g/h | Confidence | Why |
|---|---:|---|---|
| Terrowulf Packlord | 666.87 | low | regional sale averages, 25% liquidity assumption, most items unlisted |
| Blindlight Bilefin | 435.03 | low | same |
| General Fangferror | 217.65 | low | same |
| Pleasure Palace VIP | 217.39 | low | same |
| Pleasure Palace Caddie | 207.24 | low | same |

The high-confidence side is materials, not transmog:

| Item | Realisable g/h | Price | Competition observed |
|---|---:|---:|---|
| Spider's Silk | 28.46 | 80.00 g | no |
| Small Egg | 20.97 | 8.39 g | no |
| Light Leather | 7.67 | 1.00 g | no |
| Medium Leather | 4.59 | 2.00 g | no |

This is the correct conclusion from the available data: the materials have a
defensible price from confirmed realm sales, but no competition measurement
because commodities are not in the connected-realm book. The transmogs have a
price only from a discounted regional average and no competition at all.

Fixes applied:

* kill rate is now per NPC; `--kill-rates JSON` supplies measured rates and
  every recommendation records whether its rate was supplied or defaulted;
* an unlisted item is reported as `competition_data_missing`, never as zero
  competition;
* a realm with no snapshot is distinguished from a snapshot that does not list
  the item;
* competition totals count an item once, not once per NPC;
* a regional-average-only price is **low** confidence, even when the regional
  sale rate looks active.

## 5. Defects found and fixed

| # | Defect | Evidence | Fix |
|---|---|---|---|
| 1 | `Seller: You` was stored as a competitor | 2 of 4 real observations | case-insensitive self check in Lua; Python `is_self` filter |
| 2 | Closed AH wrote `posted = 0` | 310 posted rows at 08:26:16, 0 at 08:26:27 | require a completed owned-auction query; otherwise unknown |
| 3 | Untouched test window was built but never scored | `split.test` unused | holdout scored, folds built strictly before it |
| 4 | Sparse last day left the holdout empty | Dragonmaw holdout was 0 rows | grow the window backwards until it has enough priced sales |
| 5 | Holdout fields not propagated to the trained result | CLI showed `holdout = 0` | `train_all` copies holdout metrics, span and baselines |
| 6 | One global kills/hour for every NPC | 257/257 defaulted | per-NPC rates + explicit basis + defaulted list |
| 7 | Missing competition looked like no cap | 977/1,648 items | per-item flag and top-level counts |
| 8 | Competition totals double-counted items | 34,579 “items” vs 1,648 unique | count unique item ids |
| 9 | Regional-only price was “medium” confidence | every regional-average-only item | always low without a realm sale |
| 10 | Mail invoices were only read when a mail was closed; 607 sales had no buyer | Journalator invoice path and journal counts | addon 1.3.0 inbox scan + conservative `mail.link_buyers` |

## 6. Tests

* `python -m pytest tests -q` → **636 passed**.
* `python scripts/intel_addon_harness.py` → **45/45 checks passed** in a real
  Lua interpreter.
* No regressions. The only test edit was replacing a hard-coded `15/15`
  assertion with a parse of the harness summary, so adding a check no longer
  breaks the test.
* End-to-end on a **copy** of the real journal: three Dragonmaw sales without
  a buyer, three invoices built from their own item, quantity and price →
  `linked 3` (`high 3`), `ambiguous 0`, and the sale table still has its 757
  rows. A second run links nothing and reports the three invoices as already
  used. The real database was not modified.

## 7. Remaining data gaps and the next highest-value action

### The mail buyer gap — implemented, awaiting a live mail session

The journal has 762 Dragonmaw sales, of which only 151 carry a buyer name. The
remaining 611 cannot enter seller correlation at all.

Journalator already has the right call in
`Journalator/Source/Monitor/AuctionMail.lua`:
`GetInboxInvoiceInfo(index)` returns `invoiceType, itemName, playerName, value,
…, count, …`. But it processes the invoice on `CLOSE_INBOX_ITEM` (or when an
empty mail auto-deletes), so an unopened mail contributes nothing.

The addon now has `Mail.lua` (1.3.0):

1. `MAIL_SHOW` and `MAIL_INBOX_UPDATE` trigger a throttled inbox scan;
2. every invoice with a buyer name is stored with its item, quantity, value,
   deposit, consignment, `daysLeft`, subject, sender and raw item link;
3. the export carries `mail_jsonl` next to `sellers_jsonl`;
4. `mail.link_buyers` updates the **existing** sale row (item, quantity and
   value match, inside the mail lifetime) and writes an audit row to
   `sale_buyer_link`;
5. it never inserts a sale, never overwrites a sale that already has a buyer,
   and leaves the sale alone when two buyers or two sales are possible;
6. one invoice is spent on one sale: a mail already in `sale_buyer_link` is not
   offered to a later sale that shares its item, quantity and price;
7. `seller_temp_invoice` (the "Sale Pending" mail) and the `seller` mail that
   replaces it share one id, so the second scan updates nothing;
8. the confidence is `high` when the invoice's `bid` is the money the journal
   recorded, `medium` when only the listing's `buyout` matches;
9. a scan that answers 0 mails before the inbox has loaded does not stamp the
   throttle, so the `MAIL_INBOX_UPDATE` scan is not skipped.

Validated in the Lua harness and in Python tests. **Not yet observed in a live
mail session**: the addon has to be reloaded and the mailbox opened once. The
next import will report `mail.linked`; until then the 611 are still unlinked.

Confirmed against the real SavedVariables on 2026-10-21: the file still has no
`mail_jsonl` key (it was written by 1.2.1), so `addon-import` reports
`mail.error` instead of pretending the inbox was empty. A new `mail-import`
against such a file exits 2 with the same explanation.

### Other gaps

* **Commodities**: the connected-realm endpoint returned 0 commodity rows. The
  region-wide `/data/wow/auctions/commodities` book is not fetched, which is why
  977 of 1,648 farm items have no competition. It is region-level by design and
  must be labelled that way.
* **Seller identity: closed, with or without Auctionator.** The tooltip capture
  stores the seller and item but no price, so it cannot say who undercut whom.
  Both Auction House APIs answer with the owner: each search result carries an
  `owners` array, and our own listings say `"player"`. `/dfaai undercut` now runs
  its own scan through the Blizzard API (owned list, then one item search at a
  time with a 6 s timeout), so it works with the default UI, which never shows
  the seller. The Auctionator hook remains as a passive capture that reads the
  results that scan already fetched, without an extra query. Per owned auction
  it records the item, my unit price, the cheapest listing below it, that
  seller's name and realm, the gap and how many units are ahead. Results of
  another item level are filtered with Auctionator's matcher when present, and
  commodity listings have no owner and are skipped. Not yet observed in a live
  scan: the reload has to load 1.3.0 and one of the two scans has to run.
* **Open All Mail**: Blizzard's button collects money and attachments, it does
  not open each mail, so the invoices are readable anyway. The bar is hooked to
  force one scan at the click, and `MAIL_SHOW` / `MAIL_INBOX_UPDATE` already
  capture before it. TSM's Open All is not hooked; it is unnecessary because
  the scan does not depend on opening mail.
* **One snapshot**: no price change, cancel response or time-to-sale dynamics
  can be estimated from a single capture (`change = null`).
* **Kill rates**: no measured values exist; the 20/h default is an assumption
  and every recommendation says so.
* **Login inventory**: the login snapshot had 0 bag rows, so it is too early to
  be a baseline; the manual/logout pair is the usable one.

### Recommended next step

Reload 1.3.0, open the Auction House mailbox once (`/dfaai mail` also forces a
scan), then run `python scripts/intel.py addon-import`. Check `mail.linked`
against the 611 unlinked sales before trusting any competitor inference. The
`seller_observation` rows captured at 09:51 still include `Seller: You`, which
means that session was still running the old addon: a reload is required
before the next capture.

Then run one Auctionator cancel scan (the normal one) and check
`/dfaai status`: `undercut observations` should be non-zero and
`auctionator hook=true`, `open-all hooked=true`. Import and read it with
`python scripts/intel.py undercut --realm Dragonmaw`. That gives the first
seller-level undercut evidence; commodity ingestion is the next gap after it.
