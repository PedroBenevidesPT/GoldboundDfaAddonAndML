# Seller identity — how it is captured, and what it can prove

Status: **implemented, not yet exercised in-game on this machine.** The Lua
harness proves the parser and the export; the first real observation requires
hovering a non-commodity auction in the client.

## 1. The public API does not carry the seller

The Game Data API auction object consumed by this project is built in
`woweconomy/intel/ah.py`. Its fields are `id`, `item` (`id`, `context`,
`bonus_lists`, `modifiers`, `pet_*`), `quantity`, `bid`, `buyout`,
`unit_price` and `time_left`. The project's other Blizzard client
(`C:\collectorproject\wow_blizzard_client.py`, `count_item_auctions`) reads
only `item.id`. No owner, seller or player field is read anywhere in either
codebase, and `ah_auction` has no seller column.

That is the reason seller identity is captured from the **client UI**, not
from the API.

## 2. What the addon captures

`DFA_AuctionIntelligence/Sellers.lua`:

1. scans the regions of `GameTooltip` and parses a line shaped
   `Label: Name-Realm`;
2. accepts the line only when `Label` is a known seller label (`Seller`, plus
   the client's localized labels and `_G.SELLER` / `_G.AUCTION_HOUSE_SELLER`
   when present);
3. stores the **raw line** next to the parsed name, realm and item id;
4. refuses the player's own auctions;
5. exposes `CaptureFromInfo(info, itemId, itemName)` for an auction-info
   table produced by `C_AuctionHouse.GetAuctionInfoByID`. It tests
   `ownerFullName`, `ownerName`, `owner`, `sellerName`, `seller` and uses
   only a field that is actually present, recording which one it was. It
   is **not** wired to a hook of its own: the tooltip path is the verified
   one, and the info path is available without pretending to know the
   client's field name.

Hooks: `GameTooltip` `OnTooltipCleared`, `OnShow`, and `SetAuctionItem`
(falling back to `SetItem`) with a zero-delay timer, because the tooltip for
the next row can be repopulated without a fresh `OnShow`. Capture is ignored
unless the Auction House frame is shown, except for the explicit
`/dfaai sellers` command, which is the in-game verification path.

**No bulk scan is attempted.** The addon does not enumerate every auction to
collect names: that is exactly the kind of unverified API assumption this
project avoids, and the client's tooltip is the only confirmed source here.

## 3. Facts and inferences are separate

`woweconomy/intel/sellers.py` returns two lists:

* `confirmed` — a sale row from the journal (buyer name, price, time) and a
  seller observation (raw line, source, item). These are observations.
* `inferred` — `buyer_relisted`: the buyer of our sale was later seen listing
  the same item on the same realm. It carries the lag, the confidence tier
  (high ≤48 h, medium ≤7 d, low beyond), the evidence ids, and an explicit
  limit: *a name match is not proof of the same player, and the buyer's
  purchase from another competitor is not observable.*

Competitor-to-competitor transactions therefore are **not** claimed. What is
measured is co-listing (two sellers listing the same item within a window)
and observed undercuts (a later observation has a lower unit price). Both are
returned under `kind: "inference"` with their limits.

## 4. Commodities

Commodity auctions are anonymous in the client and the API. For them the
engine reports `sellers.commodity_market_summary`: listing count, units
listed, minimum and median unit price, and `seller_identity_available:
false`. Per-seller competition is `None`, never zero.

## 5. How to verify it in game

```text
/dfaai status                  # seller observations: 0 at first
/dfaai sellers                 # hover a transmog auction, then run this
/dfaai status                  # observations: 1, and the export is refreshed
```

Then, on the machine:

```powershell
python scripts/intel.py addon-import
python scripts/intel.py sellers --realm Dragonmaw --days 30
```

`addon-import` prints `sellers.parsed` / `sellers.inserted`; `sellers` prints
`observations`, `confirmed`, `inferred` and per-seller profiles. If the
tooltip in this client does not show a seller line, `observations` stays 0 —
that is a real finding, not a parser failure, and the raw-line design makes
it diagnosable.

## 6. Known limits

* Coverage is only what was hovered; it is not a census of the realm.
* A player can rename or transfer; the name key is a string, not an account id.
* Cross-realm sellers show `Name-Realm`; the realm is split at the first
  hyphen, which is safe because player names cannot contain one.
* The tooltip label list is a best-effort localization set. An unknown
  locale simply yields no observation; the label is never guessed.
