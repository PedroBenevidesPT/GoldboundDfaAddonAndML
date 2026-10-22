# DfA pricing in TSM -- what is implemented, what is unknown

Generated 2026-10-22 02:09:57Z.  Dataset schema 2, engine 2.0.0.

## 1. What is implemented

| piece | state | where |
|---|---|---|
| the learning engine (price, probability, time to sale) | done and validated | `woweconomy/ml/` |
| the pricing engine (per realm, with confidence and evidence) | done | `woweconomy/pricing/engine.py` |
| the item / appearance mapping, with the ambiguity handled | done | `woweconomy/pricing/mapping.py` |
| the Data for Azeroth interest, as evidence and not as demand | done | `woweconomy/pricing/interests.py` |
| the deterministic dataset export | done | `woweconomy/pricing/dataset.py` |
| the TSM fork: the `DFAEstimate` price source | done and installed | `DFA_TSM/DFAEstimate.lua` |
| the patcher: install, verify, revert | done | `scripts/dfa_tsm_patch.py` |
| the companion addon: the tooltip and the panel | done | `DFA_Estimate/` |
| a separate data addon for the generated file | done -- the file ships with `DFA_Estimate` | `DFA_Estimate/Data/` |
| an Auctionator adapter | **not done** | -- the TSM fork is the chosen path; the adapter is a later alternative, not a second implementation now |

## 2. The vertical slice, end to end

| step | what happens |
|---|---|
| one validated mapping | the item is mapped to its appearance through `db2_item_appearance`, and the mapping says whether it is exact, shared or absent |
| one generated record | the export writes the item's estimate, its confidence, its sample size, its flags and the evidence |
| one pricing estimate | the engine combines the trained model, the live market references and the account's own history in that realm |
| one working price source | `{dfaestimate}` returns a positive copper value for a known item and nil for an unknown one |
| tests | the mapping, the missing data, the money, the schema, the low-confidence cases, the registration, the return values, the saved data and the determinism |

## 3. The numbers

- the dataset holds **8 realms**, **237 (realm, item) records** and **31 appearances**
- it maps **267 realm names** to **92 auction houses** and ships **31,184 regional prices**, which is what the model answers from for items the account never traded
- it rests on **557 observed transactions**
- the mean confidence is **26 %**
- the dataset is **860080dc6f7c64ff...**

### The model the game runs

| | |
|---|---|
| references | dbregionmarketavg, dbregionhistorical, dbregionsaleavg |
| coefficients | dbregionmarketavg +0.238, dbregionhistorical +0.239, dbregionsaleavg +0.544 |
| rows it was fitted on | 496 |
| rows with every reference | 496 |

| in sample | |
|---|---|
| rmse on the log scale | 0.690 |
| within 25 % of the price paid | 50 % |
| within 50 % of the price paid | 72 % |

| out of sample (walk-forward, pooled) | |
|---|---|
| rows scored | 331 |
| rmse on the log scale | 0.711 |
| within 25 % of the price paid | 42 % |
| within 50 % of the price paid | 69 % |

The confidence the game states for an item with no record is the **out-of-sample** one: **42 %** within 25 %, **69 %** within 50 %.  The in-sample number is reported because it is what the model can do with every row available, but it is optimistic and is not quoted in the game.

## 4. The tests

- the pricing suite: `64 passed in 1.44s`
- the whole suite: `1 failed, 859 passed in 10.02s`

The Lua is not re-implemented in Python: the file that goes into the fork is loaded into a real Lua runtime with a mocked TSM around it, and what is tested is the file itself.

## 5. What remains unknown

| what | what is known |
|---|---|
| the price of an item the account has never traded | the model answers from the regional references, and its error outside the rows it was fitted on is 0.71 on the log scale -- a factor of about two.  It is a *level* with a band, not a number |
| whether the estimate would have sold | the sale probability is a model, not an outcome.  The engine quotes the probability of a sale and the measured curve, and never claims the estimate *causes* a sale |
| how long a listing will take | the outcome data does not support a time to sale: the concordance index is near chance.  The field is therefore absent from the dataset and the engine says so rather than inventing one |
| whether a cosmetic set is wanted on the auction house | the Data for Azeroth interest measures how many collectors *lack* a set.  That is not a demand signal, and the engine reports it as evidence, never as a reason to price |
| the price of an item in a realm the account never traded in | the dataset has no per-realm record for it, so the source falls back on the model, which uses the regional references -- the same references in every realm.  The per-realm part of the estimate is the part that is missing, and it is missing rather than faked |
| the auction duration of a listing | Journalator does not record it; it is inferred from the expiry delay, which clusters on 12, 24 and 48 hours |
| the depth of the market for an item | `num_auctions` has no history, so the competition is represented only by the regional sold-per-day and sale-rate estimates |

## 6. The limitations

| limitation | what it means |
|---|---|
| the window is four days | the transactions run from 16 to 19 October.  Every score here is thin, and more history would sharpen all of them |
| the local references are almost always missing | a realm's own references exist only for the items currently in its auction house -- 4 % of the transactions.  The model was fitted on the regional references for that reason |
| the shipped model is a distillation | it is a linear model on three logarithms, not the ensemble.  It gives up some accuracy and buys the ability to run in the game, and the report says what it gives up |
| the dataset carries the items the account traded | the per-realm records exist only for those.  Everything else is answered by the model, and the two are told apart: a record has a sample size, a model answer does not |
| the fork is a patch, not a branch | it adds one file and one line to the TOC, and the patcher backs the TOC up and can undo both.  A TSM update that moves the API will make the patcher refuse to install it again -- which is the point |
| nothing is automatic | no posting, no cancelling, no undercutting, no repricing, and no writing to TSM's settings, groups, profiles or saved strings |

