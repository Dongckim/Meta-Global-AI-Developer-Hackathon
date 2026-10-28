"""Short-command grammar + well normalization. Stdlib only.

Pipeline: normalize_text() first (homophones -> canonical wells, range fill),
then match_grammar() (exact short commands, no model call), else Spark.
"""

from __future__ import annotations

import json
import re

from server.screens import canonical, title_of

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
# Uncertain after a well candidate: another number ("C nine nine") or a
# time/volume unit ("a 5 minute"). Never rewrite into a well there.
_REPEAT_WORD = "|".join(sorted(set(NUMBER_SOUND) - {"to"}, key=len, reverse=True))
_UNIT_WORD = r"minutes?|mins?|hours?|hrs?|seconds?|secs?|µl|ul|ml"
_NO_WELL_AFTER = rf"(?!\s*(?:{_REPEAT_WORD}|\d{{1,2}}|{_UNIT_WORD})\b)"


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
        rf"\b({_LETTER_WORD})\s+({_NUMBER_WORD}|\d{{1,2}})\b{_NO_WELL_AFTER}",
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
        rf"\b([A-Ha-h])\s+({_NUMBER_WORD})\b{_NO_WELL_AFTER}",
        lambda m: m.group(1).upper() + NUMBER_SOUND[m.group(2).lower()],
        t, flags=re.IGNORECASE,
    )
    # Join letter + digits ("C 2" -> "C2").
    t = re.sub(rf"\b([A-Ha-h])\s*(\d{{1,2}})\b{_NO_WELL_AFTER}",
               lambda m: m.group(1).upper() + m.group(2), t, flags=re.IGNORECASE)
    # Bare end takes the row ("C2 to 8" / "C2 to eight" -> "C2 to C8").
    t = re.sub(
        rf"\b([A-H])(\d{{1,2}})\s+(to|through)\s+(\d{{1,2}})\b{_NO_WELL_AFTER}",
        lambda m: f"{m.group(1)}{m.group(2)} {m.group(3)} {m.group(1)}{m.group(4)}",
        t, flags=re.IGNORECASE,
    )
    t = re.sub(
        rf"\b([A-H])(\d{{1,2}})\s+(to|through)\s+({_NUMBER_WORD})\b{_NO_WELL_AFTER}",
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


def parse_wells(s) -> list[str]:
    """'C2-C8' → full range; 'C2,C5,C7' → list; combos allowed. Invalid parts dropped."""
    if isinstance(s, list):
        return [w.upper() for w in s if isinstance(w, str) and _split(w.upper())]
    out = []
    for part in str(s or "").split(","):
        ends = [
            e.strip().upper()
            for e in re.split(r"\s*(?:–|—|-|to|through)\s*", part, flags=re.IGNORECASE)
        ]
        if len(ends) == 2:
            out.extend(expand_range(ends[0], ends[1]) or [])
        elif len(ends) == 1 and _split(ends[0]):
            out.append(ends[0])
    return list(dict.fromkeys(out))


def parse_wellref(s) -> list[str]:
    """The one WellRef parser. 'C5' | 'C2–C8' ('C2-C8', 'C2 to C8',
    'C2 through C8') | 'C2, C5, C7'. Every well arg (wells, well, near,
    target, original) parses here. [] when none or invalid; never raises."""
    try:
        return parse_wells(s)
    except Exception:
        return []


def find_well_mentions(text) -> set[str]:
    """Well IDs mentioned anywhere in free text ('note C3, ok' → {'C3'})."""
    out = set()
    for m in re.finditer(r"\b([A-Ha-h])(\d{1,2})\b", text or ""):
        w = f"{m.group(1).upper()}{m.group(2)}"
        if _split(w):
            out.add(w)
    return out


def expand_tool_wells(tool_calls):
    """Expand string wells args to arrays; drop calls left with no valid wells.

    Timer wells fall back to unscoped when unparseable (the app used to do this).
    """
    out = []
    for c in tool_calls:
        if c.get("name") in ("mark_done", "record_redo"):
            args = dict(c.get("args") or {})
            if "wells" in args:
                wells = parse_wellref(args["wells"])
                if not wells:
                    continue
                args["wells"] = wells
            c = {**c, "args": args}
        elif c.get("name") == "set_timer":
            args = dict(c.get("args") or {})
            if "wells" in args:
                wells = parse_wellref(args["wells"])
                if wells:
                    args["wells"] = wells
                else:
                    del args["wells"]
            c = {**c, "args": args}
        out.append(c)
    return out


def call_wells(call) -> list[str]:
    """Well IDs a call touches, for the recent-actions list."""
    args = call.get("args") or {}
    out: list[str] = []
    if isinstance(args.get("well"), str):
        out += parse_wellref(args["well"])
    if "wells" in args:
        out += parse_wellref(args["wells"])
    if isinstance(args.get("target"), str):
        out += parse_wellref(args["target"])
    return list(dict.fromkeys(out))


def where_am_i_reply(state: dict) -> str:
    if not state.get("has_run", True):
        return "No active run — open a protocol from Home to start."
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
    out = f"{s}. {n} wells done."
    if isinstance(state.get("remaining"), list):
        left = [w for w in state["remaining"] if isinstance(w, str)]
        out += f" {len(left)} left: {wells_label(left)}." if left else " None left."
    if state.get("caution"):
        out += f" ⚠️ Protocol: {state['caution']}"
    return out


def what_step_reply(state: dict) -> str:
    if not state.get("has_run", True):
        return "No active run — open a protocol from Home to start."
    step = state.get("step", 0)
    if not isinstance(step, int):
        step = 0
    total = state.get("total")
    name = state.get("step_name", "")
    s = f"Step {step + 1}"
    if isinstance(total, int):
        s += f" of {total}"
    return f"{s}, {name}." if name else f"{s}."


def whats_next_reply(state: dict) -> str:
    if not state.get("has_run", True):
        return "No active run — open a protocol from Home to start."
    nxt = state.get("next_step", "")
    out = f"Next: {nxt}." if nxt else "Already at the last step."
    if nxt and state.get("next_caution"):
        out += f" ⚠️ Protocol: {state['next_caution']}"
    return out


_CARD_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "confirm"}
_CARD_NO = {"no", "nope", "nah", "don't", "dont"}
_CARD_CANCEL = {"cancel", "never mind", "nevermind", "forget it"}
_CARD_ALL = {"yes to all", "yes all", "approve all"}


def match_card_answer(text: str) -> str | None:
    """Bare card answers only: yes/no/cancel/all. Anything else is a new command."""
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" \t.,!?;:")
    if t in _CARD_ALL:
        return "all"
    if t in _CARD_YES:
        return "yes"
    if t in _CARD_NO:
        return "no"
    if t in _CARD_CANCEL:
        return "cancel"
    return None


def match_grammar(text: str, state: dict) -> tuple[list, str] | None:
    """Exact short commands. Normalizes internally (certain rewrites only);
    structure matches on scrubbed text; reason/body sliced from normalized text.

    The scrub (, . : -> space) and lower() are length-preserving, so match spans
    index the normalized text for extraction.
    """
    text = normalize_text(text)
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
    if re.search(r"\b(how many|what(?:'s|s| is)?|which)\b[^?.!]{0,25}\b(left|remaining)\b", low):
        return [{"name": "where_am_i", "args": {}}], where_am_i_reply(state)
    if re.fullmatch(r"\s*what(?:'s|s| is)?\s+next(\s+step)?[?!]?\s*", low):
        return [{"name": "where_am_i", "args": {"about": "next"}}], whats_next_reply(state)
    if re.fullmatch(r"\s*(what|which)\s+step(\s+is\s+it|\s+are\s+we\s+on)?[?!]?\s*|\s*current\s+step[?!]?\s*", low):
        return [{"name": "where_am_i", "args": {"about": "step"}}], what_step_reply(state)
    if re.fullmatch(r"\s*(can you |could you |please )?(hand|pass|give|bring|fetch|hold|carry)\b.*\b(me|my)\b.*", low) \
            and not re.search(r"\b(timer|note|tip|step|well|protocol)\b", low):
        return [], "cant_do_physical"
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
    m = re.fullmatch(r"\s*edit\s+(?:the\s+|my\s+)?last\s+note\s*(.+)", low)
    if m:
        body = text[m.start(1):m.end(1)].strip(" \t.,!?;:")
        if not body:
            return None
        return [{"name": "edit_note", "args": {"text": body}}], "Note updated."
    m = re.fullmatch(r"\s*cancel\s+(?:the\s+)?(.*?)\s*timer\s*", low)
    if m:
        label = text[m.start(1):m.end(1)].strip(" \t.,!?;:")
        args = {"action": "cancel"}
        if label:
            args["label"] = label
        return [{"name": "manage_timers", "args": args}], f"Canceling {timer_who(label)}."
    m = re.fullmatch(r"\s*extend\s+(?:the\s+)?(.*?)\s*timer\s+by\s+(.+?)(?:\s*min(?:ute)?s?)?\s*", low)
    if m:
        minutes = words_to_minutes(text[m.start(2):m.end(2)])
        if minutes is not None:
            label = text[m.start(1):m.end(1)].strip(" \t.,!?;:")
            args = {"action": "extend", "minutes": minutes}
            if label:
                args["label"] = label
            n = int(minutes) if float(minutes).is_integer() else minutes
            who = timer_who(label)
            return [{"name": "manage_timers", "args": args}], f"Extending {who} by {n} minutes."
    if re.fullmatch(r"\s*back[?!]?\s*", low):
        return [{"name": "back", "args": {}}], "Back."
    m = re.fullmatch(r"\s*show\s+([a-h]\d{1,2})[?!]?\s*", low)
    if m:
        wells = expand_range(m.group(1).upper())
        if wells is None:
            return None
        return [{"name": "show", "args": {"view": "plate", "target": wells[0]}}], f"Showing {wells[0]}."
    m = re.fullmatch(r"\s*import(?:\s+an?)?\s+protocols?[?!]?\s*", low)
    if m:
        if state.get("draft_open"):
            return [], "A draft is already open — review or discard it first."
        return [{"name": "open", "args": {"target": "importer"}}], "Opening the importer."
    m = re.fullmatch(r"\s*open\s+(?:the\s+)?(.+?)[?!]?\s*", low)
    if m:
        wm = re.fullmatch(r"([a-h]\d{1,2})", m.group(1).strip())
        if wm:
            wells = expand_range(wm.group(1).upper())
            if wells is not None:
                return [{"name": "open", "args": {"target": wells[0]}}], f"Opening {wells[0]}."
        sid = canonical(text[m.start(1):m.end(1)].strip(" \t.,!?;:"))
        if sid is not None:
            return [{"name": "navigate", "args": {"screen": sid}}], f"Opening {title_of(sid)}."
    return None


_ONES_MAP = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS_MAP = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def words_to_minutes(s: str) -> int | float | None:
    """Digits, one..ninety words, or 'half an hour'. None when unparseable."""
    t = (s or "").strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        f = float(t)
        return int(f) if f.is_integer() else f
    if re.fullmatch(r"half (?:an )?hour", t):
        return 30
    total, ok, prev = 0, False, None
    for w in re.split(r"[\s-]+", t):
        if w == prev:
            continue  # stuttered repeat ("two two" heard once)
        prev = w
        if w in _ONES_MAP:
            total += _ONES_MAP[w]
            ok = True
        elif w in _TENS_MAP:
            total += _TENS_MAP[w]
            ok = True
        else:
            return None
    return total if ok and total > 0 else None


_ONES = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_NUMRUN = rf"(?:\d+(?:\.\d+)?|{_ONES}|{_TENS})(?:[\s-]+(?:\d+(?:\.\d+)?|{_ONES}|{_TENS}))*"
_WELL_TOKEN = re.compile(r"\b[A-Ha-h]\d{1,2}(?:\s*[–-]\s*[A-Ha-h]?\d{1,2})?\b")
_HOURS = re.compile(
    rf"\b({_NUMRUN})(?:\s+and\s+a\s+half)?\s+hours?\b"
    r"|\bhalf\s+(?:an\s+)?hour\b"
    r"|\ban\s+hour\s+and\s+a\s+half\b"
    r"|\ban?\s+hour\b",
    re.IGNORECASE,
)


def _as_num(f: float):
    return int(f) if float(f).is_integer() else f


def find_amounts(text) -> set:
    """Minute-amounts stated in free text: digits, one..ninety runs,
    half-hour (30), N-hour(s) (N*60). Well tokens excluded."""
    t = _WELL_TOKEN.sub(" ", text or "")
    found: set = set()

    def hm(m):
        full = m.group(0).lower()
        if m.group(1) is not None:
            v = words_to_minutes(m.group(1))
            if v is not None:
                found.add(_as_num(v * 60 + (30 if "half" in full else 0)))
        elif "and a half" in full:
            found.add(90)
        elif full.startswith("half"):
            found.add(30)
        else:
            found.add(60)
        return " "

    t = _HOURS.sub(hm, t)
    for m in re.finditer(r"\d+(?:\.\d+)?", t):
        found.add(_as_num(float(m.group(0))))
    t = re.sub(r"\d+(?:\.\d+)?", " ", t)
    for m in re.finditer(rf"\b(?:{_ONES}|{_TENS})(?:[\s-]+(?:{_ONES}|{_TENS}))*\b", t, re.IGNORECASE):
        v = words_to_minutes(m.group(0))
        if v is not None:
            found.add(v)
    return found


_TIMEUNIT = r"minutes?|mins?|hours?|hrs?|분|시간"


def find_durations(text) -> set:
    """Numbers with adjacent time units: '30 min', '2 hours', '30분'. Volumes ignored."""
    t = _WELL_TOKEN.sub(" ", text or "")
    found: set = set()
    for m in re.finditer(rf"\b({_NUMRUN})\s*(?:{_TIMEUNIT})(?![a-zA-Z])", t, re.IGNORECASE):
        v = words_to_minutes(m.group(1))
        if v is None:
            continue
        u = m.group(0).lower()
        mult = 60 if re.search(r"hours?|hrs?|시간", u) else 1
        found.add(_as_num(v * mult))
    if re.search(r"\bhalf\s+(?:an\s+)?hour\b", t, re.IGNORECASE):
        found.add(30)
    return found


_STOPWORDS = {
    "a", "an", "the", "set", "start", "new", "begin", "timer", "timers",
    "min", "mins", "minute", "minutes", "time", "times", "hour", "hours",
    "hr", "hrs", "for", "please", "to", "my", "on", "of", "and", "or",
    "it", "its", "this", "that", "with", "by", "at", "in", "is",
}


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ation", "ate", "ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _content_stems(text) -> set[str]:
    t = _WELL_TOKEN.sub(" ", text or "")
    t = re.sub(r"\d+(?:\.\d+)?", " ", t)
    return {
        _stem(w)
        for w in re.findall(r"[A-Za-z]+", t)
        if w.lower() not in _STOPWORDS and len(w) > 1
    }


def relevant_plan_numbers(norm, step_names) -> set:
    """Durations from steps sharing a content stem with the utterance.

    Entries are texts or (text, minutes) pairs; the minutes field counts
    when the text carries no digits.
    """
    stems = _content_stems(norm)
    if not stems:
        return set()
    out: set = set()
    for entry in step_names or []:
        text, minutes = entry if isinstance(entry, tuple) else (entry, None)
        if stems & _content_stems(text or ""):
            out |= find_durations(text or "")
            if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
                out.add(float(minutes))
    return out


def build_evidence(utterance="", summary="", plan_numbers=frozenset(), reads_text=""):
    """The one evidence pool every value check grounds against."""
    return {
        "utterance": utterance or "",
        "summary": summary or "",
        "plan_numbers": set(plan_numbers or ()),
        "reads_text": reads_text or "",
    }


def read_text_numbers(data) -> set:
    """Minute-amounts from READ-result text fields only (text/reason/redo_note/
    label/name/memos). Structural ids (well lists, step numbers, counts) never
    count — a timer needs a stated duration, not an id."""
    texts: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("text", "reason", "redo_note", "label", "name"):
                    if isinstance(v, str):
                        texts.append(v)
                elif k == "memos" and isinstance(v, list):
                    texts.extend(x for x in v if isinstance(x, str))
                elif k in ("near", "ago"):
                    pass
                elif k in ("wells", "free") and isinstance(v, list) and all(
                        isinstance(x, str) for x in v):
                    pass  # well-id lists; dict entries still walked
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    try:
        parsed = json.loads(data) if isinstance(data, str) else data
    except ValueError:
        return set()
    walk(parsed if isinstance(parsed, (dict, list)) else {})
    return find_amounts(" ".join(texts))


def evidence_numbers(evidence) -> set:
    """All minute-amounts the evidence pool states."""
    return (
        find_amounts(evidence.get("utterance", ""))
        | find_amounts(evidence.get("summary", ""))
        | set(evidence.get("plan_numbers") or ())
        | read_text_numbers(evidence.get("reads_text", ""))
    )


def evidence_summary(turns) -> str:
    """Recent conversation as grounding text: words, calls, results, asked.

    asked lives one turn: only the previous turn's open question grounds
    this one. Answered or ignored, it expires after a single turn.
    """
    lines = []
    turns = turns or []
    for i, t in enumerate(turns):
        line = f"{t.get('text', '')} → {', '.join(t.get('calls') or [])}: " \
            f"{' | '.join(t.get('messages') or []) or '-'}"
        asked = t.get("asked") or {}
        if asked.get("question") and i == len(turns) - 1:
            line += f" | asked: {asked['question']}"
            if asked.get("target"):
                line += f" for {asked['target']}"
        lines.append(line)
    return "\n".join(lines)


def keep_grounded_timers(tool_calls, evidence) -> list:
    """Keep set_timer calls whose minutes ground in the evidence pool."""
    out = []
    for c in tool_calls:
        if c.get("name") != "set_timer":
            out.append(c)
            continue
        m = (c.get("args") or {}).get("minutes")
        if isinstance(m, str):
            try:
                m = float(m)
            except ValueError:
                continue
        ok, _ = grounded(m, evidence)
        if ok:
            out.append(c)
    return out


_TIMER_WORD = re.compile(r"\btimer\b", re.IGNORECASE)
_ASK_VERB = re.compile(r"\b(start|set|new|begin)\b", re.IGNORECASE)
_NO_ASK = re.compile(r"\b(cancel|stop|extend)\b", re.IGNORECASE)


_AFTER_WORD = re.compile(r"\b([A-Za-z]+)\s+after\s*[.!?]*$", re.IGNORECASE)
_THEN_WORD = re.compile(r"\bthen\s+([A-Za-z]+)", re.IGNORECASE)
_ON_DONE_SKIP = {
    "a", "an", "the", "half", "minute", "minutes", "hour", "hours",
    "second", "seconds", "timer", "timers",
}


def fill_on_done(tool_calls, text):
    """'wash after' / 'then wash' sets set_timer on_done, over the model's value."""
    word = None
    for m in (_AFTER_WORD.search(text or ""), _THEN_WORD.search(text or "")):
        if m and m.group(1).lower() not in _ON_DONE_SKIP:
            word = m.group(1)
            break
    if word is None:
        return tool_calls
    out = []
    for c in tool_calls:
        if c.get("name") == "set_timer":
            args = dict(c.get("args") or {})
            args["on_done"] = word
            c = {**c, "args": args}
        out.append(c)
    return out


_EACH_PHRASE = re.compile(r"\bfor\s+(each|all|every)\b", re.IGNORECASE)


def mark_each_timers(tool_calls, text):
    """One set_timer + 'for each/all/every' → flag it; the app fans out per well."""
    idx = [i for i, c in enumerate(tool_calls) if c.get("name") == "set_timer"]
    if len(idx) != 1 or not _EACH_PHRASE.search(text or ""):
        return tool_calls
    out = list(tool_calls)
    c = out[idx[0]]
    args = dict(c.get("args") or {})
    args["each"] = True
    out[idx[0]] = {**c, "args": args}
    return out


def collapse_each_timers(tool_calls, text):
    """Model fanout for 'for each' phrases collapses to one call (the app expands it)."""
    timers = [c for c in tool_calls if c.get("name") == "set_timer"]
    if len(timers) > 1 and _EACH_PHRASE.search(text or ""):
        out, kept = [], False
        for c in tool_calls:
            if c.get("name") == "set_timer":
                if kept:
                    continue
                kept = True
            out.append(c)
        return out
    return tool_calls


def count_wells(target: str) -> int:
    """How many wells a target label covers: 'C2–C8' → 7, 'C2, D2' → 2, 'C4' → 1."""
    t = (target or "").strip()
    m = re.fullmatch(r"([A-H])(\d{1,2})\s*[–-]\s*([A-H])(\d{1,2})", t)
    if m and m.group(1) == m.group(3):
        return abs(int(m.group(4)) - int(m.group(2))) + 1
    if "," in t:
        return max(len([p for p in t.split(",") if p.strip()]), 1)
    return 1


def timer_set_reply(tool_calls, state):
    """Canonical reply when a single timer answers a ranged follow-up. None = keep model's."""
    if len(tool_calls) != 1 or tool_calls[0].get("name") != "set_timer":
        return None
    pq = state.get("last_asked")
    target = pq.get("target") if isinstance(pq, dict) else None
    args = tool_calls[0].get("args")
    minutes = args.get("minutes") if isinstance(args, dict) else None
    if not target or isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return None
    m = int(minutes) if float(minutes).is_integer() else minutes
    n = count_wells(target)
    if n > 1:
        return f"{n} {m}-minute timers for {target}."
    return f"{m}-minute timer for {target}."


def split_ask(tool_calls):
    """Pull out ask() follow-ups. Returns (question or None, remaining calls)."""
    rest, q = [], None
    for c in tool_calls:
        if c.get("name") == "ask":
            text = ((c.get("args") or {}).get("question") or "").strip()
            if text and q is None:
                q = text if text.endswith("?") else text + "?"
        else:
            rest.append(c)
    return q, rest


def filter_timer_calls(tool_calls, text, extra=frozenset()):
    """Collapse fanout, flag for-each, keep value-grounded set_timer calls."""
    evidence = build_evidence(text, "", frozenset(extra), "")
    return keep_grounded_timers(
        mark_each_timers(collapse_each_timers(tool_calls, text), text), evidence
    )


def needs_minutes_ask(tool_calls, text):
    """True when a new timer was asked for but no timer call survived."""
    return bool(
        _TIMER_WORD.search(text or "")
        and _ASK_VERB.search(text or "")
        and not _NO_ASK.search(text or "")
        and not any(c.get("name") in ("set_timer", "manage_timers") for c in tool_calls)
    )


def _num(x):
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def timer_who(label):
    """'wash' → 'the wash timer'; labels already naming a timer pass through."""
    if not label:
        return "the timer"
    if "timer" in label.lower():
        return label
    return f"the {label} timer"


def wells_label(wells):
    """Contiguous same-row wells collapse: [C2..C8] → 'C2–C8', else comma-joined."""
    ws = list(wells or [])
    if len(ws) == 1:
        return ws[0]
    parts = [_split(w) for w in ws]
    if ws and all(parts) and len({p[0] for p in parts}) == 1:
        lo = min(p[1] for p in parts)
        hi = max(p[1] for p in parts)
        if hi - lo + 1 == len(parts):
            return f"{parts[0][0]}{lo}–{parts[0][0]}{hi}"
    return ", ".join(ws)


_FILLER = re.compile(r"^(?:um|uh|like)[\s,]+|[\s,]+(?:um|uh|like)$", re.IGNORECASE)


def strip_fillers(s: str) -> str:
    """Trim punctuation, then leading/trailing um/uh/like only. Nothing else changes."""
    t = (s or "").strip(" \t.,!?;:")
    while True:
        n = _FILLER.sub("", t).strip(" \t.,!?;:")
        if n == t:
            return n
        t = n


_WELL_SAY_RANGE = re.compile(r"\b([A-H])(\d{1,2})\s*[–—-]\s*([A-H])(\d{1,2})\b")
_WELL_SAY_ONE = re.compile(r"\b([A-H])(\d{1,2})\b")
_SPEAK_STRIP = "✓⏰💡↻●○⚠︎️"


def spell_wells(text: str) -> str:
    """Wells for speech: C3 -> 'C 3', C2–C8 -> 'C 2 to C 8'."""
    s = _WELL_SAY_RANGE.sub(lambda m: f"{m.group(1)} {m.group(2)} to {m.group(3)} {m.group(4)}", text or "")
    return _WELL_SAY_ONE.sub(lambda m: f"{m.group(1)} {m.group(2)}", s)


def speak_text(text: str, limit: int = 140) -> str:
    """Short spoken form: spelled wells, no glyphs, word-cut at limit."""
    s = spell_wells(text or "")
    s = s.translate({ord(c): None for c in _SPEAK_STRIP}).replace("·", ",")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+,", ",", s).strip(" ,")
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut or s[:limit]


_SPAN_STOPWORDS = {"a", "an", "the"}


def grounded(value, evidence):
    """The one value check: every stated value grounds in the evidence pool.
    Returns (ok, source) with source in utterance/read/conversation/plan/none.
    Numbers (±0.01) match the strongest pool first; text must add no new facts
    (amounts, wells) beyond the pool and carry a content word. Callers decide:
    utterance/read apply now, conversation/plan ask to confirm, none re-asks —
    an ungrounded value is never approved."""
    if value is None or isinstance(value, bool):
        return False, "none"
    num = None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value)
        except ValueError:
            pass
    if num is not None:
        pools = (
            ("utterance", find_amounts(evidence.get("utterance", ""))),
            ("read", read_text_numbers(evidence.get("reads_text", ""))),
            ("conversation", find_amounts(evidence.get("summary", ""))),
            ("plan", set(evidence.get("plan_numbers") or ())),
        )
        for source, amounts in pools:
            if any(abs(num - a) < 0.01 for a in amounts):
                return True, source
        return False, "none"
    body = value if isinstance(value, str) else ""
    if not [w for w in re.findall(r"\w+", body.lower()) if w not in _SPAN_STOPWORDS]:
        return False, "none"
    pool = " ".join(
        p for p in (
            evidence.get("utterance", ""),
            evidence.get("summary", ""),
            evidence.get("reads_text", ""),
        ) if p
    )
    if not pool:
        return True, "utterance"  # empty pool (confirm replay): keep stored text
    new_numbers = find_amounts(body) - find_amounts(pool)
    new_wells = find_well_mentions(body) - find_well_mentions(pool)
    if new_numbers or new_wells:
        return False, "none"
    return True, "utterance"


def join_wells_ranges(wells) -> str:
    """Row-major run-join: [C2,C5,C6,C7,C8] → 'C2, C5–C8'. '' when empty."""
    pts = sorted({_split(w) for w in (wells or []) if _split(w)})
    runs = []
    for row, col in pts:
        if runs and runs[-1][0] == row and col == runs[-1][2] + 1:
            runs[-1][2] = col
        else:
            runs.append([row, col, col])
    return ", ".join(f"{r}{a}" if a == b else f"{r}{a}–{r}{b}" for r, a, b in runs)


_STATE_CLAIM = re.compile(
    r"\b(noted|done|excluded|started|recorded|saved|finished|completed|canceled|cancelled"
    r"|logged|moved|dropped|drop)\b",
    re.IGNORECASE,
)


def honest_reply(tool_calls, reply, fallback="I didn't record anything."):
    """Replies may only claim results the tools actually produced."""
    if not tool_calls and "?" not in (reply or "") and _STATE_CLAIM.search(reply or ""):
        return fallback
    return reply


def is_failure_message(s: str) -> bool:
    return (
        "couldn't" in s
        or s.startswith("Which timer")
        or s.startswith("That timer")
        or s.startswith("Unknown timer action")
        or s.startswith("Already at the last")
    )


def final_pip_line(successes, failures, server_reply: str) -> str:
    """Store result wins: failures replace the reply; mixed rounds join into one line."""
    if not failures:
        return server_reply
    if not successes:
        return " ".join(failures)
    f = " ".join(x[:1].lower() + x[1:] for x in failures)
    return ", ".join(successes) + ", but " + f
