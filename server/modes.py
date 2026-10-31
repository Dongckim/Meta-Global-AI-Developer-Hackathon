"""What each run mode does, from pip_voice.json `modes`. The routing reads it
here and the Mode screen draws it from here, so what's shown is what happens.
"""

from server.tone import run_mode, section


def route(name: str, mode: str | None = None):
    """A mode's setting (asr, commands, imports, handoff, confirm_*): the chosen mode's by default."""
    return (section("modes").get(mode or run_mode()) or {}).get(name)


def _words(items: list[str]) -> str:
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else "".join(items)


def _meta_use(mode: str) -> tuple[list, list, bool]:
    """(ask first, go straight to the Meta Model API, robot code available)."""
    extras = (("Search", "confirm_search"), ("Photos", "confirm_photo"))
    asks = [name for name, key in extras if route(key, mode)]
    direct = [name for name, key in extras if not route(key, mode)]
    robot = bool(route("handoff", mode))
    return asks, direct + (["Robot code"] if robot else []), robot


def _step(kind: str, mode: str) -> dict:
    return section("mode_routes")[kind][route(kind, mode)]


def mode_view(mode: str) -> dict:
    """Snapshot `modes`: one Mode card, generated from the routing settings."""
    cfg = section("modes")[mode]
    asks, direct, robot = _meta_use(mode)
    notes = [{"feature": " · ".join(direct), "how": "Meta Model API"}] if direct else []
    notes += [{"feature": " & ".join(asks).capitalize(), "how": "Milo checks with you first."}] if asks else []
    notes += [] if robot else [{"feature": "Robot code", "how": "Full only"}]
    return {"id": mode, "label": cfg["label"], "line": cfg["line"],
            "steps": [_step("asr", mode), _step("commands", mode)], "notes": notes}


def all_modes() -> list[str]:
    return [m for m in section("modes") if m != "_note"]


def _named(step: dict) -> str:
    return f"{step['name']} {step.get('said') or step['where']}".strip()


def explain() -> str:
    """The same facts as the cards, a sentence at a time."""
    out = []
    for mode in all_modes():
        asks, direct, robot = _meta_use(mode)
        out.append(f"{section('modes')[mode]['label']}: {_named(_step('asr', mode))} hears you "
                   f"and {_named(_step('commands', mode))} runs your commands.")
        if direct:
            out.append(f"{_words(direct).capitalize()} use the Meta Model API.")
        if asks:
            out.append(f"{_words(asks).capitalize()} ask first.")
        if not robot:
            out.append("Robot code is Full only.")
    return " ".join(out)
