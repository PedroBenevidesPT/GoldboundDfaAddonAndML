# Addon audit — what exists, what is deployed, what was broken

Verified against the live WoW install at
`C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns`.

## Inventory

There are three addon deliverables, and they are not the same kind of thing.

| addon | kind | files | deployed | `.toc` | status |
|---|---|---|---|---|---|
| `DFA_Estimate` | standalone addon | 8 | yes | `DFA_Estimate.toc` | byte-identical to project |
| `DFA_AuctionIntelligence` | standalone addon | 14 | yes | `DFA_AuctionIntelligence.toc` | byte-identical to project |
| `DFA_TSM` | **TSM plugin**, not an addon | 1 | into `TradeSkillMaster/` | none, by design | byte-identical, registered at line 226 of `TradeSkillMaster.toc` |

`DFA_TSM` having no `.toc` is correct. It is a single file dropped into the
TradeSkillMaster addon folder and listed in TSM's own `.toc`. Treating the missing
`.toc` as a packaging bug would be a mistake.

## `.toc` consistency

Both `.toc` files were checked in both directions — every file they list exists, and
every file that exists is listed. No dangling references, no orphans.

`DFA_Estimate` has no `Localization.lua` and its `.toc` does not ask for one.
`DFA_AuctionIntelligence` has one and does ask for it. Both are internally consistent.

## What was actually broken

### 1. The TSM plugin was stale (fixed)

The copy in `TradeSkillMaster/` had `SUPPORTED_DATASET_VERSION = 1` and rejected any
other version. The shipped dataset is `version = 2`, so `dfaestimate` returned nothing
inside TSM — silently, because TSM treats a failing custom price source as having no
value rather than as an error.

The project copy already accepted versions 1 and 2. Only the deployed copy was wrong.
It has been redeployed; both now hash to `b2c976a3…`.

### 2. Clicking an Auctionator row lost the price (fixed)

This is the bug reported when posting. Clicking a row in Auctionator's price list
calls `UpdateSalesPrice`, which fires `PriceSelected` and then `OnPriceSet`.
`OnPriceSet` called `ApplyEstimate` unconditionally, so the estimate was written back
over the price the player had just chosen. The handler that records "the player chose
this" had been commented out, with a comment explaining that the DFA estimate should
win. It should not — a row click is a choice, exactly like typing in the box.

Both halves are restored:

- `PriceSelected` sets `private.userChose = true` and unconfirms the last fill.
- `OnPriceSet` returns early when `userChose` is set, which the docstring already
  claimed it did.

`tests/test_auctionator_bridge.py` drives the real Lua through `lupa`. With the fix
stashed, four tests fail with `[250000, 250000]` where `[250000]` is expected — the
duplicate write is the bug, caught directly.

## Eight failing tests in the suite, and why none are product defects

The full suite is 8 failed / 1074 passed. All 8 also fail at the parent commit
`c277036`, so none are regressions. They are test-harness defects:

| tests | root cause |
|---|---|
| `test_dfa_estimate_addon.py` × 4 | `lupa.LuaSyntaxError: [string "<python>"]:11: function arguments expected near '='` — the inline Lua harness in the test is malformed |
| `test_dfa_estimate_addon.py::test_the_addon_initializes_without_a_localization_file_error` | asserts `DFA_Estimate/Localization.lua` must exist; that file never existed, is not in the `.toc`, and is not referenced by any Lua file |
| `test_dfa_estimate_addon.py::test_the_installed_copy_matches_the_project_after_a_deploy` | `FileNotFoundError` on the same non-existent `Localization.lua` |
| `test_dfa_pricing.py` × 2 | `attempt to call a nil value (global 'CreateFrame')` — the harness never stubs the WoW frame API |
| `test_auction_intel_addon.py::test_the_addon_syncs_and_saves_everything_on_logout` | `AttributeError: 'NoneType' object has no attribute 'kills'` — a subprocess harness parsing failure |

`tests/test_auctionator_bridge.py` is a working example of the harness these tests
need: `FakeFrame` with `RegisterEvent`/`SetScript`/`GetScript` defined **in Lua**, a
real `UpdateSalesPrice` mixin, and `hooksecurefunc`. The reason the existing tests fail
is worth recording: `lupa` exposes Python callables as `userdata`, so a harness that
checks `type(f) == "function"` rejects a Python stub. The harness has to be written in
Lua, not in Python.
