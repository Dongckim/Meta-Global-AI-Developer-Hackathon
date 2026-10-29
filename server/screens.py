"""Screen registry: single source for UI navigation (server + app bundle).

Screens own their voice aliases and hint commands; the top-level reads list
owns every UI-displayable data kind (read_record kinds derive from it, so a
kind the UI can show is always a kind the model can read). Tool definitions
derive from here at import time and never change per call, so prompt
caching holds.
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
    ],
    "reads": [
        {"kind": "wells", "screens": ["main"], "desc": "well status"},
        {"kind": "notes", "screens": ["main"], "desc": "saved memos"},
        {"kind": "timers", "screens": ["main"], "desc": "labels, remaining"},
        {"kind": "steps", "screens": ["main"], "desc": "plan order"},
        {"kind": "history", "screens": ["main"], "desc": "recent actions"},
        {"kind": "source", "screens": [], "desc": "step quote and page"},
        {"kind": "free_wells", "screens": [], "desc": "backup candidates"},
        {"kind": "protocols", "screens": [], "desc": "saved protocols"},
        {"kind": "runs", "screens": [], "desc": "past and active runs"},
    ],
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
#: Every UI-displayable data kind, in registry order. read_record's kinds,
#: the tool enum, and the speechless renderer all derive from this.
READS: list[dict] = list(REGISTRY.get("reads", []))
READ_KINDS: tuple[str, ...] = tuple(r["kind"] for r in READS)
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


def result_of(screen_id: str, **slots) -> str:
    """User-facing line for having opened a screen (registry `result`),
    never the raw id. Slots: name, target."""
    from collections import defaultdict
    tmpl = SCREENS.get(screen_id, {}).get("result") or f"Showing {title_of(screen_id)}."
    return tmpl.format_map(defaultdict(str, slots))


def ui_of(screen_id: str) -> str | None:
    """App-side UI action for registry entries that are sheets, not views."""
    return SCREENS.get(screen_id, {}).get("ui")


def _stems(text: str) -> set[str]:
    import re
    stop = {"the", "a", "an", "to", "on", "of", "my", "me", "let", "lets", "s", "and", "or",
            "for", "this", "that", "is", "it", "go", "move", "show", "open", "please", "i", "want"}
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", (text or "").lower())} - stop


def ask_choice(text: str) -> str | None:
    """When the model's calls were unusable: ask between the two screens whose
    registry words (title, aliases, use, choice) best match what was said,
    e.g. "The active run or a past run?". None when nothing matches."""
    said = _stems(text)
    scored = []
    for i, sid in enumerate(SCREENS):
        s = SCREENS[sid]
        # names (title, aliases, choice) weigh twice the `use` description
        named = _stems(" ".join([s.get("title", ""), s.get("choice", ""), *s.get("aliases", [])]))
        hit = 2 * len(said & named) + len(said & (_stems(s.get("use", "")) - named))
        if hit and s.get("choice"):
            scored.append((-hit, i, s["choice"]))
    if not scored:
        return None
    picks = [c for _, _, c in sorted(scored)[:2]]
    q = picks[0][:1].upper() + picks[0][1:]
    return f"{q} or {picks[1]}?" if len(picks) > 1 else f"{q}?"


def guide() -> str:
    """When to use each screen, for the model (registry `use`/`not`)."""
    parts = []
    for sid in IDS:
        s = SCREENS[sid]
        if not s.get("use"):
            continue
        line = f"{sid}{'(+target)' if s.get('needs_target') else ''}: {s['use']}"
        if s.get("not"):
            line += f"; not for {s['not']}"
        parts.append(line)
    return ". ".join(parts) + "."


def commands_for(screen_id: str) -> list[str]:
    return list(SCREENS.get(screen_id, {}).get("commands", []))
