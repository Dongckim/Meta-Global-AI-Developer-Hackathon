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
for _s in REGISTRY.get("screens", []):  # "buttons_from": the same buttons as another screen
    if _s.get("buttons_from"):
        _s["buttons"] = next((x.get("buttons", []) for x in REGISTRY["screens"] if x["id"] == _s["buttons_from"]), [])
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
#: Multi-screen flows with one way in (`flows`: steps of screen ids, entry first).
FLOWS: list[dict] = list(REGISTRY.get("flows", []))
#: Card answer words (`answers`: all / yes / no / cancel -> phrases).
ANSWERS: dict[str, list[str]] = dict(REGISTRY.get("answers", {}))
#: Words that carry nothing beside an answer ("um", "please").
FILLERS: list[str] = list(REGISTRY.get("fillers", []))
#: Words said around a button's words that don't change it ("okay continue", "continue, Milo").
LEAD_INS: list[str] = list(REGISTRY.get("lead_ins", []))
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


def fill_button(b: dict, slots: dict) -> dict | None:
    """A templated button ("Step {step}", args {"step": "{step}"}) filled from
    the screen's slots; None while a slot it needs has no value or its `when`
    state doesn't hold (not shown). `look` ("disabled") is how it's drawn
    only: a tap or the words still run its action."""
    import re
    if any(slots.get(k) != v for k, v in (b.get("when") or {}).items()):
        return None  # shown only in that state ("when": {"status": "finished"})
    def fill(v):
        if not isinstance(v, str):
            return v
        need = re.findall(r"{(\w+)}", v)
        if any(k not in slots for k in need):
            raise KeyError
        return v.format(**{k: slots[k] for k in need})
    try:
        return {**b, "say": fill(b["say"]), "args": {k: fill(v) for k, v in (b.get("args") or {}).items()}}
    except KeyError:
        return None


def buttons_for(screen_id: str, slots: dict | None = None) -> list[dict]:
    """A screen's registry buttons with their templates filled (app and voice
    see the same words)."""
    out = [fill_button(b, slots or {}) for b in SCREENS.get(screen_id, {}).get("buttons", [])]
    return [b for b in out if b]


def button_words(text: str) -> str:
    """What is said, as a button's words: lowercase, punctuation out, lead-ins
    ("okay", "please", "oh") and the agent's name dropped wherever they are,
    repeated phrases folded ("full please full please" -> "full")."""
    from server.grammar import collapse_repeats
    from server.tone import agent_name
    drop = set(LEAD_INS) | {agent_name().lower()}
    said = f" {_said(text)} "
    for w in sorted(drop, key=len, reverse=True):  # two-word ones ("all right") first
        said = said.replace(f" {w} ", " ")
    return collapse_repeats(" ".join(said.split()))


#: Words that turn a button's words into something else: "not full", "what's the next step".
_NOT = {"not", "don't", "dont", "no", "never", "nope", "nah", "without", "cancel"}
_ASKING = {"what", "what's", "whats", "which", "how", "why", "when", "where", "who", "should", "can", "could"}


def button_match(text: str, screen_ids: list[str], slots: dict | None = None) -> tuple[dict | None, str | None]:
    """A visible button said -> (its call, None); two buttons inside what's
    said -> (None, "Full or Private?"); else (None, None). Exact words first;
    else a sentence holding exactly one button's words ("let's go full"),
    unless it negates or asks ("not full", "what's the next step?")."""
    import re
    said = button_words(text)
    if not said:
        return None, None
    shown = [b for sid in screen_ids for b in buttons_for(sid, slots) if not b.get("tap_only")]
    for b in shown:
        if _said(b["say"]) == said:
            return {"name": b["action"], "args": dict(b.get("args") or {})}, None
    words = said.split()
    if set(words) & _NOT or set(words) & _ASKING or (text or "").strip().endswith("?"):
        return None, None
    inside = [b for b in shown if re.search(rf"(^| ){re.escape(_said(b['say']))}( |$)", said)]
    if len(inside) == 1:
        b = inside[0]
        return {"name": b["action"], "args": dict(b.get("args") or {})}, None
    if len(inside) > 1:
        names = [b["say"] for b in inside]
        return None, (", ".join(names[:-1]) + " or " + names[-1] + "?")
    return None, None


def button_call(text: str, screen_ids: list[str], slots: dict | None = None) -> dict | None:
    """A visible screen's "Say X" button said -> its action call, the same
    one a tap sends (registry `buttons`); see button_match."""
    return button_match(text, screen_ids, slots)[0]


def verbs(action: str) -> list[str]:
    """Same-meaning phrases for an action (registry `actions.<name>.verbs`):
    the one word list grammar, buttons and the coverage check all use."""
    return list(ACTIONS.get(action, {}).get("verbs", []))


_ORD = {w: i + 1 for i, w in enumerate(["first", "second", "third", "fourth", "fifth", "sixth", "seventh",
                                         "eighth", "ninth", "tenth"])}
_NUMW = {w: i + 1 for i, w in enumerate(["one", "two", "three", "four", "five", "six", "seven", "eight",
                                          "nine", "ten"])}


def _n(word: str) -> int | None:
    return int(word) if word.isdigit() else _NUMW.get(word) or _ORD.get(word)


def control_call(text: str, screen_id: str, items: list[dict] | None = None) -> dict | None:
    """Words for a screen's own parts that aren't one fixed phrase: "page 2"
    (registry `controls`), and on a list screen (`pick`) "number 2", "the
    first one", or words of an item's name. None when it's not that."""
    sc = SCREENS.get(screen_id, {})
    said = button_words(text)
    import re
    if "page" in sc.get("controls", []):
        m = re.fullmatch(r"(?:go to |show |open )?(?:the )?page (\w+)", said)
        if m and _n(m[1]):
            return {"name": "control", "args": {"cmd": "page", "n": _n(m[1])}}
    if not sc.get("pick"):
        return None
    m = re.fullmatch(r"(?:open |pick |take |import |use )?(?:the )?(?:number (\w+)|(\w+)(?: one)?)", said)
    if m and _n(m[1] or m[2] or ""):
        return {"name": "pick", "args": {"n": _n(m[1] or m[2])}}
    words = {w for w in re.findall(r"[a-z0-9]{3,}", said)} - {"the", "one", "open", "file", "pick", "import"}
    if words and items:
        hits = [it for it in items if words <= set(re.findall(r"[a-z0-9]{3,}", it["name"].lower()))]
        if len(hits) == 1:
            return {"name": "pick", "args": {"name": hits[0]["name"]}}
    return None


def start_call(text: str) -> dict | None:
    """Starting a run by voice, no model: "start a run", "start a new run of
    the MTT one", "run Drug A/B again", "start the ELISA protocol". The words
    after it name the protocol ("" = the one on screen)."""
    import re
    said = button_words(text)
    m = (re.fullmatch(r"(?:start|begin)(?: a| another| a new| the next| one more)? run(?: (?:of|for|with|on) (?P<a>.+))?", said)
         or re.fullmatch(r"(?:run|rerun|redo) (?P<b>.+?)(?: again| one more time)", said)
         or re.fullmatch(r"(?:start|begin) (?:the )?(?P<c>.+?) (?:protocol|again)", said))
    if not m:
        return None
    t = next((g for g in m.groups() if g), "") or ""
    t = re.sub(r"^(?:the|a|my) ", "", t)
    t = re.sub(r" (?:one|protocol)$", "", t).strip()
    if t in ("it", "this", "that", "this one", "that one") or re.fullmatch(r"\d+", t):
        t = ""
    return {"name": "start_run", "args": {"target": t}}
