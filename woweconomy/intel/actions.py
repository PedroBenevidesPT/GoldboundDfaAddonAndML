"""HUD actions: the in-game half of the automation loop.

The player presses a button; the client cannot run Python, so the press is
written to SavedVariables and the daemon finds it on the next tick.  This
module reads that list, normalises it, and remembers which presses were
already handled -- a SavedVariables file is rewritten on every logout, so
the same action is visible many times.

What the daemon does with each action is in ``scripts/watch.py``; this
module only answers "which presses are new".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from woweconomy.analytics.auction_intel import extract_lua_string


def parse_actions_jsonl(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().lower()
        if not name:
            continue
        out.append({
            "name": name,
            "at": int(row.get("at") or 0),
            "ok": bool(row.get("ok")),
            "detail": str(row.get("detail") or ""),
        })
    return out


def parse_export_file(path: Path | str) -> List[Dict[str, Any]]:
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    raw = extract_lua_string(content, "actions_jsonl")
    if raw is None:
        return []
    return parse_actions_jsonl(raw)


def action_key(action: Dict[str, Any]) -> str:
    return f"{action.get('name')}|{action.get('at')}|{action.get('ok')}"


def new_actions(actions: List[Dict[str, Any]],
                seen: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Actions not in ``seen``; the caller persists them by key."""
    return [action for action in actions
            if action_key(action) not in seen]


def mark_seen(actions: List[Dict[str, Any]], seen: Dict[str, Any],
              now: int, keep: int = 400) -> None:
    for action in actions:
        seen[action_key(action)] = int(action.get("at") or now)
    if len(seen) > keep:
        for key, _ in sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - keep]:
            seen.pop(key, None)
