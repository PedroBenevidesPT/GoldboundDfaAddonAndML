# The DfA pricing engine in TradeSkillMaster

How a number that is computed on this machine ends up on an item tooltip
in the game, and what it is allowed to say.

## The shape of it

```
  tsm_markets.db ─┐
  dfa.db         ─┼─►  the pricing engine  ─►  the dataset  ─►  the addon  ─►  the game
  auction_events ─┘     (offline, Python)     (Lua, generated)   (reader)      (TSM)
```

Nothing in the game reaches out.  The engine runs offline, writes a
versioned Lua file, the addon ships that file, and TSM's fork reads it.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| the learning engine | `woweconomy/ml/` | learns from the observed transactions: the price, the probability of selling, the time to sell |
| the pricing engine | `woweconomy/pricing/` | turns what was learned plus the live market into one explainable answer per (realm, item) |
| the export | `scripts/dfa_pricing_export.py` | writes `DFA_Estimate/Data/DFA_Estimate_Data.lua` |
| the data addon | `DFA_Estimate/` | reads the generated file, publishes it as `DFA_ESTIMATE`, shows the tooltip and the panel |
| the TSM fork | `DFA_TSM/` | registers the `DFAEstimate` price source |
| the patcher | `scripts/dfa_tsm_patch.py` | installs, verifies and removes the fork |
| the LootAppraiser integration | `DFA_Estimate/Integration/LootAppraiser.lua` | points LootAppraiser's Recipe, Weapon and Armor classes at `dfaestimate` |

## What the source answers, and for whom

The source is called with an **item string** and nothing else.  It
answers with a number of copper, or with nil.

**In a custom string it is written bare: `dfaestimate`.**  TSM's custom
string language has no braces -- `{dfaestimate}` is an *invalid* token,
and an identifier must consist of letters only.  A source name is a
word, like `dbmarket`, not a placeholder.

The order of preference is deliberate:

1. **The account's own record for that item in that realm.**  It carries
   the sample size, the confidence and the warnings, and it is the only
   figure that is about *this* market.
2. **The model, applied to the live market references.**  This is what
   makes the source work for *every* item and not only the ones the
   account has traded.  The model is a linear model on the logarithms of
   the regional references, fitted offline and shipped in the dataset.

If neither exists, the source returns nil.  It never returns the price
of "a typical item" for an item with no market data at all, because that
is a guess dressed up as an estimate.

## LootAppraiser

LootAppraiser Reloaded already values item classes through separate price
sources, and it hands any source string to TSM as a custom price source.
The integration therefore does not patch LootAppraiser's files: on login
it enables the class overrides for **Recipe**, **Weapon** and **Armor**
and sets those three fields to `dfaestimate`.  The other classes and the
main source are untouched.

The previous values are saved in `DFA_ESTIMATE_DB`; the change is visible
in LootAppraiser's own options, survives a reload, and is undone with
`/dfaestimate lootappraiser off` (or `/dfala off`).  `/dfala status`
shows what LootAppraiser is using right now.

The integration also wraps `PriceSources.ResolveForItem` and decides by
the Blizzard item class id (2 weapon, 4 armor, 9 recipe), because
LootAppraiser's own table compares the item-type string with the English
`"Armor"` / `"Weapon"` / `"Recipe"`; that string is localized, so on a
Portuguese client its class overrides never matched.  The wrapper passes
every other class straight through to LootAppraiser.

## Per realm, because the market is per realm

Two realms of the same region are two auction houses with two different
prices.  And realms of one **connected group** share a single auction
house: Dragonmaw and Spinebreaker are one market, and TSM files their
data under the group's canonical realm.

So the dataset carries:

* `realmGroups` -- the auction house each realm trades in;
* `realms[realm].items` -- the account's own record for that realm.

The in-game side resolves the realm the same way TSM itself does
(`SessionInfo.GetRealmName()`, with the same apostrophe sanitising), so
Dragonmaw's record is found on Dragonmaw and Spinebreaker's on
Spinebreaker, and neither leaks into the other.

## The model that ships

The game cannot run the CatBoost ensemble, and it does not have the
features the ensemble uses.  What it has is the family of market
references, so the engine distils the ensemble into a form the game can
compute and **measures what the distillation costs**:

| variant | walk-forward rmse | walk-forward ±25 % | test rmse | test ±25 % |
|---|---|---|---|---|
| **the three regional references** (ships) | **0.727** | **43 %** | **0.734** | **44 %** |
| the regional sale average alone | 0.752 | 44 % | 0.782 | 36 % |
| all six references | 0.863 | 52 % | 0.738 | 44 % |
| regional + local market | 0.523 | 51 % | 0.734 | 44 % |
| regional + local historical | 0.504 | 52 % | 0.734 | 44 % |
| regional + local market + historical | 0.802 | 51 % | 0.738 | 44 % |
| the three local references only | 34.961 | 0 % | -- | -- |

The numbers are **out of sample**: each variant is fitted on a training
window and scored on the window that follows it.  The test window holds
only 25 realised sales, so it is a small sample and the last column should
be read as a direction, not a decimal.

The local variants *look* better on the walk-forward, and they are not:
every variant that includes the local references lands on the **same**
test rmse, 0.734, as the regional-only model.  The explanation is a fact
about the data rather than about the model -- **a realm's own references
exist only for the items currently in its auction house.**  At the moment
the model can use them for **15 of 398 realised sales (3.8 %)**.  The walk-forward
gain is therefore fitted on a handful of rows; when a row without a local
reference arrives, the model falls back to the training median and the
advantage disappears.

The regional references exist for every item, which is why they are the
ones that ship.

A missing reference is filled with the median of its logarithm -- the
median the model was fitted with, shipped alongside it so both sides
fill it the same way.

## The measured demand curve

The curve is not modelled: it is the sale rate in each band of *ask
divided by the regional sale average*, measured on the transactions.

```
 ask / sale avg      sale rate     expected gold per listing
 0.25 – 0.5           63.5 %            0.16
 0.85 – 1.0           62.5 %            0.58
 1.15 – 1.4           60.8 %            0.77   <- the peak
 1.4  – 2.0           39.3 %            0.67
 2.0  – 3.0            8.5 %            0.21
 3.0  +                2.6 %            0.12
```

The game needs it because the sale probability of an *ask* is the one
thing the linear model cannot say.

## The confidence, and the warnings

The confidence is not a mood.  It moves with:

* the number of the account's own observations of the item in that realm;
* the age of those observations;
* how much the market references disagree with each other.

The warnings are the same things, spelled out: `stale`,
`thin-sample`, `speculative-reference`, `contradictory-references`,
`no-local-reference`, `shared-appearance`, `time-to-sale-unsupported`,
`low-confidence`.

For an item the account has never traded there is no sample at all, so the
confidence is the model's own measured hit rate.  That number is taken
**out of sample** -- the model is fitted on a training window and scored on
the window after it -- because a model scored on the rows it was fitted on
always flatters itself.  Today it is **42 % within 25 %** and **70 % within
50 %** of the price actually paid; the in-sample figure (51 % / 73 %) is
recorded in the validation report but is not what the game states.

## Could the whole thing be one giant custom string?

It is worth answering, because it is the alternative that needs no fork
at all: a custom source that is a chain of conditionals, one per item.

**It is expressible.**  TSM's custom string language has `ifeq` and
`baseitem`, so the chain would be written

```
ifeq(baseitem, "i:114931", 5000g, ifeq(baseitem, "i:5465", 300g, dbmarket))
```

**But it is bounded.**  The generated function is Lua, and TSM refuses to
compile a custom string that needs **more than 190 local variables**
(`CodeGen.lua`: `if #self._locals > 190 then ... TOO_MANY_VARS`).  Each
comparison costs one, so a single string holds roughly **190 items** --
and TSM says so in the error it shows: *"This custom string is too complex
for WoW to handle; use custom sources to simplify it."*

**And it would cost three things the fork does not:**

1. **It would have to be written into TSM's saved variables.**  TSM's
   settings database is version- and hash-checked, so writing into it from
   outside is fragile, and it is overwritten on every TSM update.
2. **The prices would be frozen.**  A string is a constant; it cannot
   follow the market.  The fork reads the *live* references.
3. **It is not per realm.**  A custom string is a global setting, not a
   per-realm one.  Per realm would mean a TSM profile per realm, each
   with its own giant string.

So the giant string is a **fallback**, not a design: it works for a
couple of dozen items per realm, it is static, and it needs the settings
database.  The fork has none of those limits.

## What the source does not do

It does not post, cancel, undercut or reprice anything.  It does not
write to TSM's settings, its groups, its profiles or its saved strings.
It is a number; using it is the user's decision.

The test suite checks this by reading the fork's own source: the strings
`AuctioningOperation`, `SniperOperation`, `ShoppingOperation`,
`GroupOperation`, `RegisterCustomSource`, `AddCustomSource`,
`SetActiveProfile` and `Group.SetItem` must not appear in it.

## The version check

The dataset carries a schema version.  The source understands version
`1` and **refuses any other**: a reader that does not know the format
must not guess at it.  The same check is in the patcher, which refuses
to install the fork into a TSM whose version it was not written against
or whose API has moved.
