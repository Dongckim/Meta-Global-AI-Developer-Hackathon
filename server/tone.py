"""Tone loader. All user-facing strings live in shared/pip_voice.json
(Eunbi-editable, no code); this module only loads, rotates, and fills slots."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

VOICE_PATH = Path(__file__).resolve().parent.parent / "shared" / "pip_voice.json"


def _data() -> dict:
    try:
        return json.loads(VOICE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def keys() -> list[str]:
    """Template keys (variant lists); persona and _notes are not templates."""
    return [k for k, v in _data().items() if not k.startswith("_") and isinstance(v, list)]


#: The mode chosen on the Mode screen; until then the RUN_MODE setting.
_chosen_mode: str | None = None


def set_run_mode(mode: str | None) -> None:
    global _chosen_mode
    _chosen_mode = mode


def run_mode() -> str:
    """The run mode now, a key of pip_voice.json `modes`."""
    import os
    return _chosen_mode or os.environ.get("RUN_MODE", "full")


def section(name: str) -> dict:
    """A settings block of pip_voice.json (persona, live, ...); {} if absent."""
    v = _data().get(name)
    return v if isinstance(v, dict) else {}


def persona() -> dict:
    """Shown/spoken identity: agent_name, wake_phrase, voice, quality, ..."""
    return section("persona")


def agent_name() -> str:
    return persona().get("agent_name") or "the assistant"


def variants(key: str) -> list[str]:
    vs = _data().get(key)
    if isinstance(vs, list) and vs and all(isinstance(v, str) for v in vs):
        return vs
    return [f"[{key}]"]


def pick(key: str, session: dict | None = None) -> str:
    """Rotate variants so the same key never repeats twice in a row."""
    vs = variants(key)
    last = {}
    if session is not None:
        last = session.get("tone_last") or {}
    idx = (last.get(key, -1) + 1) % len(vs) if len(vs) > 1 else 0
    if session is not None:
        session["tone_last"] = {**last, key: idx}
    return vs[idx]


def say(key: str, session: dict | None = None, **slots) -> str:
    """Fill a template. wells_spoken defaults to the display label; speech
    spelling happens downstream in speak_text, so one rendering serves both."""
    slots.setdefault("agent", agent_name())
    slots.setdefault("wells", "")
    slots.setdefault("wells_spoken", slots["wells"])
    out = pick(key, session).format_map(defaultdict(str, slots))
    out = re.sub(r"\s+on\s+([,.?])", r"\1", out)  # dangling "on" when wells are empty
    return re.sub(r"\s+", " ", out).strip()
