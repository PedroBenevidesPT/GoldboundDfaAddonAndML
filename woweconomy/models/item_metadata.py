"""Item metadata — vendor status, category flags, and static data."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ItemMetadata:
    """Static metadata about an item."""
    item_id: int
    name: str = ""

    # Vendor
    is_vendor_item: bool = False
    vendor_price: int = 0

    # Categories (optional filters)
    is_farmable: bool = True
    is_craftable: bool = False
    is_auction_only: bool = True
    is_quest_item: bool = False
    is_currency: bool = False
    is_pet: bool = False
    is_mount: bool = False
    is_recipe: bool = False
    is_toy: bool = False
    is_consumable: bool = False
    is_material: bool = False

    # Profession / expansion
    profession: str = ""
    expansion: str = ""
    gathering_profession: str = ""

    # Confidence
    data_source: str = "unknown"  # hardcoded, csv, blizzard_api, etc.
