"""Screen registry: single source for UI navigation (server + app bundle).

Screens own their voice aliases and hint commands. Tool definitions derive
from here at import time and never change per call, so prompt caching holds.
"""

from __future__ import annotations

import json
from pathlib import Path

FALLBACK = {
    "screens": [
        {"id": "main", "title": "Bench", "parent": "main",
         "targets": ["well", "screen"], "aliases": ["main", "plate", "bench"],
         "commands": ["mark C2 done", "start a 5 minute timer", "show C5"]},
        {"id": "well_card", "title": "Well card", "parent": "main",
         "targets": ["well"], "aliases": ["well card"],
         "commands": ["note bubbles", "exclude this well", "back"]},
        {"id": "robot_preview", "title": "Robot preview", "parent": "main",
         "targets": [], "aliases": ["robot preview", "robot"],
         "commands": ["make the robot protocol", "back"]},
    ]
}

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "shared" / "schemas" / "screens.json"


def load() -> dict:
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError) as e:
        print(f"screens: {REGISTRY_PATH} unreadable ({e}); using fallback", flush=True)
        return FALLBACK


REGISTRY = load()
SCREENS: dict[str, dict] = {s["id"]: s for s in REGISTRY.get("screens", [])}
IDS = sorted(SCREENS)
ALIASES: dict[str, str] = {
    a.lower(): sid for sid, s in SCREENS.items() for a in s.get("aliases", [])
}


def canonical(name: str) -> str | None:
    """Screen id or voice alias -> canonical id. None when unknown."""
    n = (name or "").strip().lower()
    if n in SCREENS:
        return n
    return ALIASES.get(n)


def parent_of(screen_id: str) -> str:
    return SCREENS.get(screen_id, {}).get("parent", "main") or "main"


def title_of(screen_id: str) -> str:
    return SCREENS.get(screen_id, {}).get("title", screen_id)


def commands_for(screen_id: str) -> list[str]:
    return list(SCREENS.get(screen_id, {}).get("commands", []))
