"""Product identity for auction observations.

Two different questions are routinely confused in this codebase, and the
confusion is expensive:

* **Which auction is this?** -- ``auction_key``, an instance identifier.
* **Which good is this?** -- the *variant*: the item plus every modifier
  that changes what the buyer receives.

``auction_key`` is ``str(entry["id"])``, the Blizzard auction id.  It is
unique per auction and stable across snapshots (the same live auction
keeps its id), which makes it the right key for "did this listing
disappear".  It is *not* a product identifier:

* it changes on every relist, so one good has many keys;
* it says nothing about the variant, so two different goods share none;
* for the commodity book it is still an auction id, but a commodity
  auction is one stack at one price, so a single product has hundreds of
  live keys at once.

``item_id`` is not a product identifier either.  Item ``82800`` is the
caged-pet container: one item id, hundreds of species, and a price range
from a few gold to the gold cap.  Averaging it is meaningless.

The key below is the smallest set of fields that identifies what a buyer
actually receives:

* ``item_id`` -- the item;
* ``item_context`` -- the difficulty/context selector the API sends;
* ``bonus_lists`` -- bonus ids (item level, tertiary stats, sockets);
* ``modifiers`` -- modifier ids (pet breed/level, crafted stats);
* ``pet_species_id`` -- the species inside a caged pet.

It is a *key*, not a price model: two variants may legitimately share a
price, and the same variant may be listed at many prices.
"""
from __future__ import annotations

import hashlib
from typing import Optional


def variant_key(item_id: int,
                item_context: Optional[int] = None,
                bonus_lists: Optional[str] = None,
                modifiers: Optional[str] = None,
                pet_species_id: Optional[int] = None) -> str:
    """A stable identifier for the good, not the auction.

    Deterministic and independent of field order: the JSON fragments are
    stored already canonicalised by :mod:`woweconomy.intel.ah`
    (``sort_keys=True``), and the hash makes the key fixed-width so it can
    be indexed without a composite index on four nullable columns.
    """
    payload = "|".join((
        str(int(item_id)),
        str(int(item_context or 0)),
        bonus_lists or "",
        modifiers or "",
        str(int(pet_species_id or 0)),
    ))
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{int(item_id)}:{digest}"


#: The columns the key is derived from.  Consumers that group by variant
#: should use ``variant_key``; this tuple exists so an audit can show
#: exactly what went into it.
VARIANT_COLUMNS = ("item_id", "item_context", "bonus_lists", "modifiers",
                   "pet_species_id")


def variant_key_of(row) -> str:
    """``variant_key`` for a sqlite Row or a mapping of the auction row."""
    def get(name):
        try:
            return row[name]
        except (KeyError, IndexError):
            return None
    return variant_key(get("item_id"), get("item_context"),
                       get("bonus_lists"), get("modifiers"),
                       get("pet_species_id"))


def variant_concentration(con, snapshot_id: str) -> dict:
    """How badly does ``item_id`` pool distinct goods in one snapshot?

    Returns the counts an audit needs, computed from the snapshot itself
    so the answer is reproducible from the data rather than asserted.
    """
    row = con.execute(
        "SELECT COUNT(*) AS rows,"
        " COUNT(DISTINCT item_id) AS items,"
        " COUNT(DISTINCT variant_key) AS variants"
        " FROM ah_auction WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    pooled = con.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT item_id FROM ah_auction WHERE snapshot_id = ?"
        "  GROUP BY item_id HAVING COUNT(DISTINCT variant_key) > 1)",
        (snapshot_id,)).fetchone()[0]
    worst = con.execute(
        "SELECT item_id, COUNT(DISTINCT variant_key) AS variants, COUNT(*) AS rows"
        " FROM ah_auction WHERE snapshot_id = ?"
        " GROUP BY item_id ORDER BY variants DESC LIMIT 5",
        (snapshot_id,)).fetchall()
    return {
        "rows": int(row["rows"]),
        "items": int(row["items"]),
        "variants": int(row["variants"]),
        "items_with_more_than_one_variant": int(pooled),
        "worst": [dict(r) for r in worst],
    }
