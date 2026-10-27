"""Short-command grammar + well normalization. Stdlib only.

Pipeline: normalize_text() first (homophones -> canonical wells, range fill),
then match_grammar() (exact short commands, no model call), else Spark.
"""

from __future__ import annotations

import re

# Well letters/digits by sound. Only applied AT well positions, never inside free text.
LETTER_SOUND = {"see": "C", "sea": "C", "she": "C", "bee": "B", "dee": "D"}
NUMBER_SOUND = {
    "one": "1", "won": "1", "two": "2", "too": "2", "three": "3", "four": "4", "for": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "ate": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}

_LETTER_WORD = "|".join(sorted(LETTER_SOUND, key=len, reverse=True))
_NUMBER_WORD = "|".join(sorted(NUMBER_SOUND, key=len, reverse=True))
# What may follow a range word (matched case-insensitively).
_RANGE_TAIL = rf"(?:[A-Ha-h]\d{{1,2}}|\d{{1,2}}|{_NUMBER_WORD})\b"


def normalize_text(text: str) -> str:
    """Canonicalize well mentions only; note/tip/reason text stays verbatim (case included)."""
    t = text.strip()
    # Stuttered range start ("C to to eight" -> "C2 to C8").
    t = re.sub(
        rf"\b([A-Ha-h])\s+to\s+to\s+(\d{{1,2}}|{_NUMBER_WORD})\b",
        lambda m: f"{m.group(1).upper()}2 to {m.group(1).upper()}{NUMBER_SOUND.get(m.group(2).lower(), m.group(2))}",
        t, flags=re.IGNORECASE,
    )
    # Letter-word + number -> well ("see two" -> "C2").
    t = re.sub(
        rf"\b({_LETTER_WORD})\s+({_NUMBER_WORD}|\d{{1,2}})\b",
        lambda m: LETTER_SOUND[m.group(1).lower()] + NUMBER_SOUND.get(m.group(2).lower(), m.group(2)),
        t, flags=re.IGNORECASE,
    )
    # Letter-word + "to" is 2 unless a range follows ("see to done" -> "C2 done").
    t = re.sub(
        rf"\b({_LETTER_WORD})\s+to\b(?!\s*{_RANGE_TAIL})",
        lambda m: LETTER_SOUND[m.group(1).lower()] + "2",
        t, flags=re.IGNORECASE,
    )
    # Bare letter + number-word -> well ("C too" -> "C2").
    t = re.sub(
        rf"\b([A-Ha-h])\s+({_NUMBER_WORD})\b",
        lambda m: m.group(1).upper() + NUMBER_SOUND[m.group(2).lower()],
        t, flags=re.IGNORECASE,
    )
    # Join letter + digits ("C 2" -> "C2").
    t = re.sub(r"\b([A-Ha-h])\s*(\d{1,2})\b", lambda m: m.group(1).upper() + m.group(2), t)
    # Bare end takes the row ("C2 to 8" / "C2 to eight" -> "C2 to C8").
    t = re.sub(
        rf"\b([A-H])(\d{{1,2}})\s+(to|through)\s+(\d{{1,2}})\b",
        lambda m: f"{m.group(1)}{m.group(2)} {m.group(3)} {m.group(1)}{m.group(4)}",
        t, flags=re.IGNORECASE,
    )
    t = re.sub(
        rf"\b([A-H])(\d{{1,2}})\s+(to|through)\s+({_NUMBER_WORD})\b",
        lambda m: f"{m.group(1)}{m.group(2)} {m.group(3)} {m.group(1)}{NUMBER_SOUND[m.group(4).lower()]}",
        t, flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", t).strip()


def _split(well: str):
    m = re.fullmatch(r"([A-H])(\d{1,2})", well)
    if not m:
        return None
    col = int(m.group(2))
    return (m.group(1), col) if 1 <= col <= 12 else None


def expand_range(start: str, end: str | None = None) -> list[str] | None:
    """Same-row expansion (sorted). None when invalid or cross-row (let Spark decide)."""
    s = _split(start)
    if not s:
        return None
    if end is None:
        return [start]
    e = _split(end)
    if not e or e[0] != s[0]:
        return None
    lo, hi = sorted([s[1], e[1]])
    return [f"{s[0]}{c}" for c in range(lo, hi + 1)]


def where_am_i_reply(state: dict) -> str:
    step = state.get("step", 0)
    if not isinstance(step, int):
        step = 0
    total = state.get("total")
    name = state.get("step_name", "")
    done = state.get("done", [])
    n = len(done) if isinstance(done, list) else 0
    s = f"Step {step + 1}"
    if isinstance(total, int):
        s += f" of {total}"
    if name:
        s += f", {name}"
    return f"{s}. {n} wells done."


def match_grammar(text: str, state: dict) -> tuple[list, str] | None:
    """Exact short commands. Structure matches on scrubbed text; reason/body sliced verbatim.

    The scrub (, . : -> space) and lower() are length-preserving, so match spans
    index the original text for verbatim extraction.
    """
    low = re.sub(r"[,.:]", " ", text.lower())
    m = re.fullmatch(r"\s*([a-h]\d{1,2})(?:\s+(?:to|through)\s+([a-h]\d{1,2}))?\s+done[?!]?\s*", low)
    if m:
        wells = expand_range(m.group(1).upper(), m.group(2).upper() if m.group(2) else None)
        if wells is None:
            return None
        spec = wells[0] if m.group(2) is None else f"{wells[0]} to {wells[-1]}"
        return [{"name": "mark_done", "args": {"wells": wells}}], f"{spec} done."
    if re.fullmatch(r"\s*next[?!]?\s*", low):
        return [{"name": "next_step", "args": {}}], "Next step."
    if re.fullmatch(r"\s*where am i[?!]?\s*", low):
        return [{"name": "where_am_i", "args": {}}], where_am_i_reply(state)
    m = re.fullmatch(r"\s*exclude\s+([a-h]\d{1,2})\s*(.*)", low)
    if m:
        wells = expand_range(m.group(1).upper())
        if wells is None:
            return None
        args = {"well": wells[0]}
        reason = text[m.start(2):m.end(2)].strip(" \t.,!?;:")
        if reason:
            args["reason"] = reason
        return [{"name": "exclude_well", "args": args}], f"Exclude {wells[0]}?"
    m = re.fullmatch(r"\s*note\s+([a-h]\d{1,2})\s*(.+)", low)
    if m:
        wells = expand_range(m.group(1).upper())
        if wells is None:
            return None
        body = text[m.start(2):m.end(2)].strip(" \t.,!?;:")
        if not body:
            return None
        return [{"name": "add_note", "args": {"target": wells[0], "text": body}}], f"Noted for {wells[0]}."
    return None


_ONES = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_HAS_NUMBER = re.compile(rf"\d+(?:\.\d+)?|\bhalf (?:an )?hour\b|\b(?:{_ONES}|{_TENS})\b", re.IGNORECASE)


def drop_ungrounded_timers(tool_calls, text):
    """Keep set_timer calls only when the text actually mentions an amount (well digits excluded)."""
    scrubbed = re.sub(r"[A-Ha-h]\d{1,2}", " ", text or "")
    if _HAS_NUMBER.search(scrubbed):
        return tool_calls
    return [c for c in tool_calls if c.get("name") != "set_timer"]


_TIMER_WORD = re.compile(r"\btimer\b", re.IGNORECASE)


def collapse_each_timers(tool_calls, text):
    """'for each'/'for all' still means one shared timer (app labels it with the range)."""
    timers = [c for c in tool_calls if c.get("name") == "set_timer"]
    if len(timers) > 1 and re.search(r"\bfor\s+each\b|\bfor\s+all\b", text or "", re.IGNORECASE):
        out, kept = [], False
        for c in tool_calls:
            if c.get("name") == "set_timer":
                if kept:
                    continue
                kept = True
            out.append(c)
        return out
    return tool_calls


def timer_set_reply(tool_calls, state):
    """Canonical reply when a single timer answers a ranged follow-up. None = keep model's."""
    if len(tool_calls) != 1 or tool_calls[0].get("name") != "set_timer":
        return None
    pq = state.get("pending_question")
    target = pq.get("target") if isinstance(pq, dict) else None
    args = tool_calls[0].get("args")
    minutes = args.get("minutes") if isinstance(args, dict) else None
    if not target or isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return None
    m = int(minutes) if float(minutes).is_integer() else minutes
    return f"{m}-minute timer for {target}."


def timer_followup(tool_calls, reply, text):
    """Drop ungrounded set_timer calls; if 'timer' was mentioned but no timer
    call survived, turn the reply into a minutes follow-up (feeds pending_question)."""
    calls = drop_ungrounded_timers(collapse_each_timers(tool_calls, text), text)
    if _TIMER_WORD.search(text or "") and not any(
        c.get("name") in ("set_timer", "manage_timers") for c in calls
    ):
        kept = " ".join(
            s for s in re.split(r"(?<=[.?!])\s+", reply or "") if not _TIMER_WORD.search(s)
        ).strip()
        if kept.endswith("?"):
            return calls, kept
        return calls, f"{kept} How many minutes for the timer?".strip()
    return calls, reply


_STATE_CLAIM = re.compile(
    r"\b(noted|done|excluded|started|recorded|saved|finished|completed|canceled|cancelled)\b",
    re.IGNORECASE,
)


def honest_reply(tool_calls, reply):
    """Replies may only claim results the tools actually produced."""
    if not tool_calls and "?" not in (reply or "") and _STATE_CLAIM.search(reply or ""):
        return "I didn't record anything."
    return reply
