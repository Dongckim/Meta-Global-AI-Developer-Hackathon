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
#: Built screens. `planned` ones (registry only, not in the app yet) are
#: never navigable, offered to the model, or resolved by alias.
SCREENS: dict[str, dict] = {s["id"]: s for s in REGISTRY.get("screens", []) if s.get("status") != "planned"}
PLANNED: list[str] = [s["id"] for s in REGISTRY.get("screens", []) if s.get("status") == "planned"]
_PLANNED_ALIASES: dict[str, str] = {
    a.lower(): s["id"] for s in REGISTRY.get("screens", []) if s.get("status") == "planned"
    for a in [s["id"].replace("_", " "), *s.get("aliases", [])]}


def planned_screen(text: str) -> str | None:
    """A planned screen asked for by name ("open web searches", "private
    mode"): its id, so the server can say it isn't ready without the model."""
    import re
    said = " ".join(re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split())
    said = re.sub(r"^(?:please |can you |could you )?(?:open|show(?: me)?|go to|start|let's see|see)\s+(?:the |my )?", "", said)
    return _PLANNED_ALIASES.get(said)
#: Card answer words (`answers`: all / yes / no / cancel -> phrases).
ANSWERS: dict[str, list[str]] = dict(REGISTRY.get("answers", {}))
#: Words that carry nothing beside an answer ("um", "please").
FILLERS: list[str] = list(REGISTRY.get("fillers", []))
IDS = sorted(SCREENS)
#: Every UI-displayable data kind, in registry order. read_record's kinds,
#: the tool enum, and the speechless renderer all derive from this.
READS: list[dict] = list(REGISTRY.get("reads", []))
READ_KINDS: tuple[str, ...] = tuple(r["kind"] for r in READS)
#: Per-action vocabulary (`verbs`), keyed by action name.
ACTIONS: dict[str, dict] = dict(REGISTRY.get("actions", {}))
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
    import re
    tmpl = SCREENS.get(screen_id, {}).get("result") or ""
    need = set(re.findall(r"{(\w+)}", tmpl))
    if not tmpl or any(not slots.get(k) for k in need):
        return f"Showing {title_of(screen_id)}."  # never a line with a hole in it
    return tmpl.format(**slots)


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


def _said(text: str) -> str:
    import re
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split())


def button_call(text: str, screen_ids: list[str]) -> dict | None:
    """A visible screen's "Say X" button spoken exactly -> its action call,
    the same one a tap sends (registry `buttons`)."""
    said = _said(text)
    for sid in screen_ids:
        for b in SCREENS.get(sid, {}).get("buttons", []):
            if _said(b["say"]) == said:
                return {"name": b["action"], "args": dict(b.get("args") or {})}
    return None


def verbs(action: str) -> list[str]:
    """Same-meaning phrases for an action (registry `actions.<name>.verbs`):
    the one word list grammar, buttons and the coverage check all use."""
    return list(ACTIONS.get(action, {}).get("verbs", []))
