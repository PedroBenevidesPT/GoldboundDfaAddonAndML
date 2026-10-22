"""TSM 4 item strings.

The group import field in TradeSkillMaster 4 expects every item as
``i:<itemID>`` and several items separated by commas::

    i:12345,i:67890,i:11223,i:44556

Raw, comma-separated IDs (``12345,67890``) are accepted by TSM 4.14+ when
pasted from Wowhead, but the prefixed form is the canonical one and works on
every TSM 4 version.  TSM 3 used the raw form, so both are parsed when
converting existing files.
"""
from __future__ import annotations

import re
from typing import Iterable, List

TOKEN_SPLIT = re.compile(r"[\s,;]+")
ITEM_TOKEN = re.compile(r"^(?:i:|item:)?(\d+)$", re.IGNORECASE)


def tsm_item_string(item_ids: Iterable[int]) -> str:
    """Return one TSM import line: ``i:1,i:2,...``.

    Duplicates are dropped, preserving the first occurrence, because a TSM
    group is a set of items anyway.
    """
    seen = set()
    tokens: List[str] = []
    for raw in item_ids:
        item_id = int(raw)
        if item_id in seen:
            continue
        seen.add(item_id)
        tokens.append(f"i:{item_id}")
    return ",".join(tokens)


def parse_item_ids(text: str) -> List[int]:
    """Parse a pasted item list (raw or ``i:`` prefixed) into unique IDs.

    Accepts commas, semicolons, whitespace and newlines as separators, which
    covers the TXT files produced before the ``i:`` prefix and lists copied
    from Wowhead/TSM.  Raises ``ValueError`` on anything that is not a plain
    item ID, so a name or a bonus-ID string is never silently discarded.
    """
    ids: List[int] = []
    seen = set()
    for token in TOKEN_SPLIT.split(text.strip()):
        if not token:
            continue
        match = ITEM_TOKEN.match(token)
        if not match:
            raise ValueError(f"token inválido: {token!r}")
        item_id = int(match.group(1))
        if item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)
    return ids
