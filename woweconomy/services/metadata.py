"""Item metadata service — vendor detection, category lookups."""
from __future__ import annotations

import csv
import logging
import os
from typing import Dict, Optional

from woweconomy.models.item_metadata import ItemMetadata

logger = logging.getLogger(__name__)

# Known NPC vendor items that players commonly relist on the AH.
# These have fixed vendor prices and infinite supply from NPCs.
_VENDOR_ITEMS: Dict[int, int] = {
    # --- Thread / Tailoring ---
    2320: 40,       # Coarse Thread
    2321: 80,       # Fine Thread
    2324: 3000,     # Heavy Silken Thread
    2325: 200,      # Black Dye
    38426: 5000,    # Eternium Thread
    62778: 100,     # Tidespray Linen (vendor price)
    2326: 100,      # Star Wood
    # --- Basic Food / Drink ---
    117: 5,         # Tough Jerky
    159: 10,        # Refreshing Spring Water
    4540: 15,       # Tough Hunk of Bread
    4541: 20,       # Freshly Baked Bread
    4542: 25,       # Moist Cornbread
    4544: 50,       # Mutton Chop
    4604: 10,       # Forest Mushroom Cap
    4605: 15,       # Red-speckled Mushroom
    4606: 25,       # Spongy Morel
    4607: 40,       # Delicious Cave Mold
    4608: 5,        # Raw Black Truffle
    8075: 5,        # Magic Essence (vendor)
    8076: 10,       # Magic Essence (vendor)
    8077: 25,       # Magic Essence (vendor)
    8078: 50,       # Magic Essence (vendor)
    8079: 5,        # Magic Essence (vendor)
    # --- Basic Alchemy / Reagents ---
    3371: 20,       # Crystal Vial
    3372: 50,       # Leaded Vial
    8925: 100,      # Crystal Vial
    6265: 100,      # Soul Shard
    6947: 50,       # Quick-drying Ink
    21931: 100,     # Woven Ivy Necklace
    # --- Keys ---
    5060: 100,      # Thieves' Tools
    5061: 100,      # Thieves' Tools (useful)
    5140: 100,      # Flash Powder
    5177: 100,      # Watertight Trunk Key
    5318: 100,      # Rattleskull Key
    # --- Rogue Poisons (vendor) ---
    2892: 50,       # Deadly Poison
    2893: 50,       # Instant Poison
    8984: 50,       # Deadly Poison III
    8985: 50,       # Deadly Poison IV
    20844: 50,      # Deadly Poison V
    # --- Scrolls (vendor) ---
    2290: 20,       # Scroll of Intellect
    2291: 20,       # Scroll of Stamina
    2292: 20,       # Scroll of Spirit
    2293: 20,       # Scroll of Strength
    2294: 20,       # Scroll of Agility
    2295: 20,       # Scroll of Protection
    2296: 20,       # Scroll of Stamina
    37091: 50,      # Scroll of Intellect
    37092: 50,      # Scroll of Stamina
    37093: 50,      # Scroll of Spirit
    37094: 50,      # Scroll of Strength
    37095: 50,      # Scroll of Agility
    37096: 50,      # Scroll of Protection
    37097: 0,       # Scroll of Recall (vendor)
    # --- Basic Gear (vendor whites) ---
    36: 50,         # Worn Mace
    25: 40,         # Worn Shortsword
    35: 30,         # Bent Staff
    37: 60,         # Worn Axe
    38: 70,         # Recruit's Shirt
    39: 80,         # Recruit's Pants
    40: 90,         # Recruit's Boots
    43: 50,         # Squire's Boots
    44: 60,         # Squire's Pants
    45: 70,         # Squire's Shirt
    47: 50,         # Footpad's Shoes
    48: 60,         # Footpad's Pants
    49: 70,         # Footpad's Shirt
    51: 50,         # Neophyte's Boots
    52: 60,         # Neophyte's Pants
    53: 70,         # Neophyte's Shirt
    55: 50,         # Apprentice's Boots
    56: 60,         # Apprentice's Robe
    57: 70,         # Acolyte's Robe
    59: 50,         # Acolyte's Shoes
    85: 80,         # Dirty Leather Vest
    86: 90,         # Scruffy Leather Vest
    87: 100,        # Battleworn Leather Vest
    88: 50,         # Ratty Leather Vest
    89: 60,         # Worn Leather Vest
    90: 70,         # Worn Leather Belt
    91: 80,         # Worn Leather Bracers
    92: 90,         # Worn Leather Gloves
    93: 100,        # Worn Leather Helm
    94: 110,        # Worn Leather Pants
    95: 120,        # Worn Leather Shoulders
    96: 130,        # Worn Leather Boots
    97: 140,        # Worn Leather Belt
    98: 150,        # Worn Leather Bracers
    99: 160,        # Worn Leather Gloves
    100: 170,       # Worn Leather Helm
    101: 180,       # Worn Leather Pants
    102: 190,       # Worn Leather Shoulders
    103: 200,       # Worn Leather Boots
    104: 210,       # Worn Leather Belt
    105: 220,       # Worn Leather Bracers
    106: 230,       # Worn Leather Gloves
    # --- Fishing ---
    6362: 100,      # Raw Rockscale Cod
    6363: 100,      # Raw Rockscale Cod
    6364: 100,      # Raw Rockscale Cod
    # --- Misc vendor trash that ends up on AH ---
    2672: 100,      # Stringy Wolf Meat
    2673: 100,      # Stringy Wolf Meat
    2674: 100,      # Stringy Wolf Meat
    2675: 100,      # Stringy Wolf Meat
    2680: 100,      # Stringy Wolf Meat
    2681: 100,      # Stringy Wolf Meat
    2682: 100,      # Stringy Wolf Meat
    2683: 100,      # Stringy Wolf Meat
    2684: 100,      # Stringy Wolf Meat
    2685: 100,      # Stringy Wolf Meat
    2686: 100,      # Stringy Wolf Meat
    2687: 100,      # Stringy Wolf Meat
}


class MetadataService:
    """Lookup item metadata — vendor status, categories, etc."""

    def __init__(self):
        self._cache: Dict[int, ItemMetadata] = {}
        self._loaded_file = False

    def get(self, item_id: int, name: str = "") -> ItemMetadata:
        """Get metadata for an item (cached)."""
        if item_id not in self._cache:
            meta = self._lookup(item_id, name)
            self._cache[item_id] = meta
        return self._cache[item_id]

    def is_vendor(self, item_id: int) -> bool:
        return self.get(item_id).is_vendor_item

    def vendor_price(self, item_id: int) -> int:
        return self.get(item_id).vendor_price

    def _lookup(self, item_id: int, name: str) -> ItemMetadata:
        # Check hardcoded vendor list
        if item_id in _VENDOR_ITEMS:
            return ItemMetadata(
                item_id=item_id,
                name=name,
                is_vendor_item=True,
                vendor_price=_VENDOR_ITEMS[item_id],
                is_farmable=False,
                data_source="hardcoded",
            )
        # Default: not vendor, farmable
        return ItemMetadata(
            item_id=item_id,
            name=name,
            is_vendor_item=False,
            is_farmable=True,
            data_source="default",
        )

    def load_csv(self, path: str) -> int:
        """Load metadata from a CSV file. Returns count loaded."""
        if not os.path.exists(path):
            logger.warning(f"Metadata CSV not found: {path}")
            return 0
        count = 0
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                iid = int(row["item_id"])
                meta = ItemMetadata(
                    item_id=iid,
                    name=row.get("name", ""),
                    is_vendor_item=row.get("is_vendor", "0") == "1",
                    vendor_price=int(row.get("vendor_price", 0) or 0),
                    is_farmable=row.get("is_farmable", "1") == "1",
                    is_craftable=row.get("is_craftable", "0") == "1",
                    data_source="csv",
                )
                self._cache[iid] = meta
                count += 1
        logger.info(f"Loaded {count} item metadata rows from {path}")
        return count
