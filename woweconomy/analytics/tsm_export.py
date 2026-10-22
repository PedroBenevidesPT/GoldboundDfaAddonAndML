"""Read and write TradeSkillMaster 4 group export strings.

A TSM group export is a single line of ``AceSerializer`` rev 1 data
(``^1^T...^t^^``) holding one table::

    {
        operations      = { <Module> = { <Operation> = { <setting> = value } } },
        groupOperations = { <group path> = { <Module> = { 1 = <Operation>,
                                                         override = true } } },
        groupExport     = "group:<path>,i:1,i:2,group:<path2>,i:3",
    }

A module table must carry ``override = true`` for the addon to attach that
operation to the group (``GroupOperation.Import`` does ``if operations.override
then``); without it the group inherits the profile root and the operation is
dropped.  The old ``ignoreItemVariations`` / ``ignoreRandomEnchants`` fields are
legacy and the importer wipes them, so they are not emitted.

The ``groupExport`` string is the item list: groups separated by a literal
``,group:`` and the path hierarchy with a backtick (``1. Transmog`Armor``),
items written as ``i:<itemID>``.  This is the format the addon writes in its
Export dialog and reads back in Import/Export, so a string built here imports
as *named* groups with their operations attached — unlike a bare item list,
which always lands in a group called "Imported Group".

The encoder below is a faithful port of AceSerializer-3.0 (string escaping,
``^N`` numbers, ``^B``/``^b`` booleans, ``^F``/``^f`` floats, ``^T``/``^t``
tables) so the output is byte-compatible with the addon.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Sequence

_REVISION = "1"
_TOKEN = re.compile(r"\^(.)([^^]*)")
_STRIP = re.compile(r"[\x00-\x1f ]")

# AceSerializer escapes everything <= 0x20, plus "^", "~" and DEL, by
# prefixing "~" and bumping the byte by 64 (see SerializeStringHelper).
def _escape(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == 30:            # ends up as "~^" otherwise (ticket 115)
            out.append("~z")
        elif code <= 32:
            out.append("~" + chr(code + 64))
        elif code == 94:          # "^"
            out.append("~}")
        elif code == 126:         # "~"
            out.append("~|")
        elif code == 127:         # DEL
            out.append("~{")
        else:
            out.append(ch)
    return "".join(out)


def _unescape(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        if text[i] == "~":
            esc = text[i + 1]
            if esc == "z":
                out.append(chr(30))
            elif esc == "{":
                out.append(chr(127))
            elif esc == "|":
                out.append("~")
            elif esc == "}":
                out.append("^")
            else:
                out.append(chr(ord(esc) - 64))
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _serialize_value(value: Any, out: list) -> None:
    if isinstance(value, bool):
        out.append("^B" if value else "^b")
    elif isinstance(value, str):
        out.append("^S")
        out.append(_escape(value))
    elif isinstance(value, int):
        out.append("^N")
        out.append(str(value))
    elif isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            out.append("^N")
            out.append(str(int(value)))
        else:
            out.append("^N")
            out.append(format(value, ".14g"))
    elif value is None:
        out.append("^Z")
    elif isinstance(value, dict):
        out.append("^T")
        for key, item in value.items():
            _serialize_value(key, out)
            _serialize_value(item, out)
        out.append("^t")
    else:
        raise TypeError(f"não serializável: {type(value).__name__}")


def serialize(value: Any) -> str:
    """Serialize a value to an AceSerializer rev 1 string (```^1`` prefix)."""
    out = ["^1"]
    _serialize_value(value, out)
    out.append("^^")
    return "".join(out)


def _deserialize_value(iterator, control: str, data: str) -> Any:
    if control == "S":
        return _unescape(data)
    if control == "N":
        if data in ("1.#INF", "inf"):
            return float("inf")
        if data in ("-1.#INF", "-inf"):
            return float("-inf")
        return float(data) if ("." in data or "e" in data.lower()) else int(data)
    if control == "B":
        return True
    if control == "b":
        return False
    if control == "Z":
        return None
    if control == "F":
        control2, exponent = next(iterator)
        if control2 != "f":
            raise ValueError("float malformado no TSM string")
        return float(data) * (2 ** int(exponent))
    if control == "T":
        table: Dict[Any, Any] = {}
        while True:
            control, data = next(iterator)
            if control == "t":
                return table
            key = _deserialize_value(iterator, control, data)
            control, data = next(iterator)
            table[key] = _deserialize_value(iterator, control, data)
    raise ValueError(f"controlo AceSerializer inválido: ^%s" % control)


def deserialize(text: str) -> Any:
    """Parse an AceSerializer rev 1 string back into Python values."""
    tokens = iter(
        (m.group(1), m.group(2)) for m in _TOKEN.finditer(_STRIP.sub("", text))
    )
    try:
        control, data = next(tokens)
    except StopIteration:
        raise ValueError("string TSM vazia")
    if control != _REVISION:
        raise ValueError("não é um TSM string (revisão AceSerializer != 1)")
    control, data = next(tokens)
    return _deserialize_value(tokens, control, data)


def build_group_export(groups: Mapping[str, Sequence[int]]) -> str:
    """Build the ``groupExport`` payload: ``group:<path>,i:1,i:2,...``."""
    tokens = []
    for path, item_ids in groups.items():
        tokens.append(f"group:{path}")
        tokens.extend(f"i:{int(item_id)}" for item_id in item_ids)
    return ",".join(tokens)


def parse_group_export(payload: str) -> Dict[str, list]:
    """Split a ``groupExport`` payload back into ``{path: [item_id, ...]}``."""
    groups: Dict[str, list] = {}
    current = None
    for token in payload.split(","):
        if token.startswith("group:"):
            current = token[len("group:"):]
            groups[current] = []
        elif token.startswith("i:"):
            if current is None:
                raise ValueError("item antes do primeiro 'group:'")
            groups[current].append(int(token[2:]))
        elif token:
            raise ValueError(f"token inválido no groupExport: {token!r}")
    return groups


def build_export(
    operations: Mapping[str, Mapping[str, Any]] | None = None,
    group_operations: Mapping[str, Mapping[str, Any]] | None = None,
    group_export: str = "",
) -> str:
    """Serialize the three top-level tables TSM's importer reads."""
    return serialize({
        "operations": dict(operations or {}),
        "groupOperations": dict(group_operations or {}),
        "groupExport": group_export,
    })
