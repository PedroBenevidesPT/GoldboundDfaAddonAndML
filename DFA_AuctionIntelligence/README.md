# DFA Auction Intelligence (WoW Addon)

Companion addon for the DFA (Dragonmaw/Spinebreaker) economy analysis project. Captures observable auction house events with precise timestamps, transaction provenance, fee breakdowns, and connects listing outcomes to pricing decisions.

## Features

- **Empirical Event Capture**: Records completed sales, purchases, listing attempts, cancellations, and expirations.
- **Journalator Integration**: Automatically connects to Journalator v170+, backfills historical stores, and hooks real-time events.
- **Native Fallback**: If Journalator is absent or disabled, natively monitors Blizzard Auction House API hooks and mailbox events without error.
- **TSM Reference Tagging**: Captures local market value (`dbmarket`) and pricing operations at the exact moment of listing.
- **Deduplication & Integrity**: Deterministic signatures prevent duplicate ingestion across reloads and multi-source imports.
- **Analysis-Only Export**: Formats data into versioned JSONL and Lua tables in SavedVariables (`DFA_AUCTION_INTELLIGENCE_EXPORT`) ready for DFA ingestion.

## Installation

1. Copy the `DFA_AuctionIntelligence` directory into your World of Warcraft AddOns folder:
   ```
   World of Warcraft\_retail_\Interface\AddOns\DFA_AuctionIntelligence
   ```
2. Ensure `Journalator` (optional but recommended) and `TradeSkillMaster` are enabled in your AddOn list.
3. Start WoW or run `/reload`.

## Slash Commands

- `/dfaai status` — Show addon version, stored events count, and Journalator connection status.
- `/dfaai diag` — Display diagnostics, data quality metrics, and missing field analysis.
- `/dfaai export` — Force an immediate export refresh to SavedVariables.
- `/dfaai sync` — Re-scan Journalator archives for new records (reports rows read per
  section, how many were new, and any section that could not be read).
- `/dfaai inventory` — Snapshot bags, bank and posted auctions now, and refresh the export.
- `/dfaai sellers` — Capture the seller on the Auction House tooltip currently on screen.
  The raw tooltip line is stored next to the parsed name.
- `/dfaai mail` — Scan the open mailbox for seller and pending-sale invoices now.
  Buyer, quantity, value and the estimated sale time are stored as evidence.
  This is the only path that reads the mailbox by default; `/dfaai mail auto`
  also scans when the mailbox opens.
- `/dfaai undercut` — Run the addon's own owned-auction undercut scan, including
  commodities, when Auctionator's scan is not the one being used.
- `/dfaai kills` — Kills, looted corpses and copper recorded per mob since the
  last export.

## Kills, loot and copper

Since 1.5.0 the addon also records what the account killed and looted, per
NPC, and exports it as `kills_jsonl` next to the auction events:

- a kill needs two facts — this player (or their pet) damaged the unit, and
the unit died — and the GUID is remembered until the fight ends, so
`UNIT_DIED` and `PARTY_KILL` for one death count once;
- a loot window is attributed per corpse GUID and counted once: reopening a
corpse for the leftovers is the same drop, and the looted GUIDs survive a
reload;
- money slots are kept apart from item counts, so a valuation can price the
items and add the copper back.

The counters are flushed into one immutable batch per export; the export
carries every batch still retained, so a late import loses nothing and a
repeated one stores nothing twice. Nothing here prices anything: the backend
(`woweconomy.intel.mobs`) owns the price sources.

Since 1.6.0 the committed events are also announced in game, once per corpse
and once per kill, through `DFA_AI.Kills:RegisterLootListener(fn)` and
`RegisterKillListener(fn)`. A listener receives
`{ npcId, name, guid, copper, items = { [itemId] = count }, at }` (kills carry
no loot), *after* the counters are stored. It runs inside a `pcall`, so a
listener from another addon cannot cost the observation; its errors are shown
by `/dfaai status`. `ZoneTimerRedux` uses this to value the same loot in game
— it does not open a second loot window of its own. The announcement carries a
copy, so a listener cannot edit the counters.

MobInfo2 records the same primitives, but its SavedVariables are LibSerialize
blobs compressed with LibDeflate, so it is not read directly; both addons can
be enabled at once. In game, `MI2_GetMobData` (its documented API) is the way to
read its per-mob history, which `ZoneTimerRedux` uses for gold per kill; the
counters here stay the source the backend imports.

## Inventory and seller capture

Since 1.2.0 the addon also writes an inventory picture (`inventory_json` in the
export) and seller observations (`sellers_jsonl`). The inventory snapshot records
which locations it could actually read: a bank the character is nowhere near is
marked unreadable instead of being written as empty, so the Python reconciler
does not report a disappearance that never happened.

Seller identity is captured from the Auction House tooltip and from the
Auctionator undercut scan. The AH result sets carry `owners` for item listings,
and `C_AuctionHouse.GetCommoditySearchResultInfo` carries them for commodity
listings too; the addon reads both. A plain tooltip for a commodity still has
no seller line, so that path stores nothing rather than guessing.

## What happens when you leave the game

The client writes SavedVariables *after* `PLAYER_LOGOUT`, so the handler decides what
survives. Since 1.1.0 it:

1. re-syncs all three Journalator sections (`Invoices`, `Posting`, `Failures`), so an
   auction that sold late in the session is captured in that session, not the next one;
2. rebuilds `DFA_AUCTION_INTELLIGENCE_EXPORT` from the store, including `last_sync`
   (counts per section plus the errors);
3. does both inside `pcall`, so a failure in one cannot cost the other.

`/dfaai status` prints the last sync, which is how you can tell what the archive
actually held instead of assuming it was read.

Since 1.1.1 the logout sync survives Journalator 171's own shutdown: Archivist nulls
its saved-variable image (`archive.sv = nil`) on its `PLAYER_LOGOUT`, and when that ran
first every section read failed with *attempt to index a nil value* and nothing was
stored. The handler now reads the still-live `Journalator.State.Logs` in that case and
records the sections it fell back to (`fallback` in `last_sync`).

The market reference was silently missing too: `TSM_API.GetCustomPriceValue` only
accepts a TSM item string (`i:12345`) and raises on a hyperlink, and the `pcall`
turned that into `nil` — so every listing carried `marketRefCopper = 0`. Listings
and failures now pass `i:<itemId>`, and invoices, which have no link at all, ask the
client for the id from the item name before pricing.

Live rows also arrive one way only, through Auctionator's EventBus when it is present.
Hooking `Journalator.AddToLogs` with `hooksecurefunc` replaced the function object that
Journalator had registered as its EventBus source, so every archived auction raised
*All sources must be registered*; 1.1.1 listens instead of hooking.

Known limits of the log itself: Journalator's invoices carry no item link, so sale rows
export without `itemId` and the importer resolves the id from the item name
(`python scripts/dfa_ai.py --import`, table `auction_intel_events`). Listings and
failures only exist once the archive has them for the character being played.

## Verifying it without the game

```
python scripts/auction_intel_harness.py "C:\FarmAnalysis\DFA_AuctionIntelligence"
```

Runs the addon in a real Lua interpreter with a mocked client, fires
`ADDON_LOADED` → `PLAYER_LOGIN` → `PLAYER_LOGOUT` and asserts that every section was
imported, that a late auction is flushed on logout and that the export matches the
store.

## Files

- `DFA_AuctionIntelligence.toc`: TOC metadata and SavedVariables declaration.
- `Localization.lua`: Constants, strings, and addon namespace.
- `DataStore.lua`: Versioned local event storage and deduplication.
- `JournalatorAdapter.lua`: Verified integration with Journalator.
- `AuctionEvents.lua`: Normalization, provenance capture, and native Blizzard hooks.
- `Inventory.lua`: Full bags/bank/posted snapshot, with readable-location flags.
- `Sellers.lua`: Auction House tooltip/auction-info seller capture.
- `Mail.lua`: Mailbox invoice capture on demand, with delayed retries.  The
  addon does not hook Blizzard's mailbox frame: doing so made this client
  answer "action blocked by Blizzard" and left the mailbox unusable.
- `Undercut.lua`: Auctionator hook and native item/commodity undercut scans.
- `HUD.lua`: In-game action buttons and status line.
- `Diagnostics.lua`: System health and data quality reporting.
- `Export.lua`: Structured export serialization (JSONL).
- `Core.lua`: Lifecycle handlers and slash command routing.
