"""Human labels for protocols and runs: the one place for what people see
(display) and hear (spoken), and for resolving what they say back to an id.
Internal ids (run-YYYYMMDD-..., protocol hashes) never appear in either form.

Times are the server's local clock (the iPad and the bench server share a
location; shortcut: per-device time zones would need the app's offset).
"""

from __future__ import annotations

import datetime as _dt
import re

ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth"]
_PREP_TAIL = re.compile(r"\s+(?:in|for|with|using|on|at|from)\s+.*$", re.IGNORECASE)


def shorten_title(title: str) -> str:
    """Rule-based short title (<= 4 words) for library items without one:
    "Drug A / Drug B Dosing in a 96-well Plate" -> "Drug A/B Dosing"."""
    t = re.sub(r"\([^)]*\)", " ", title or "")
    t = re.sub(r"^\s*protocol\s*[:\-–]\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(\b\w+) (\w+)\s*/\s*\1 (\w+)", r"\1 \2/\3", t)  # Drug A / Drug B -> Drug A/B
    t = re.sub(r"\s*/\s*", "/", t)
    words = t.split()
    if len(words) > 2:
        t = _PREP_TAIL.sub("", " ".join(words)) or t
    t = re.sub(r"\s+protocol\s*$", "", t.strip(), flags=re.IGNORECASE)
    return " ".join(t.split()[:4]) or (title or "").strip()


#: Lab words with a usual short form; anything else follows the rule.
_ABBR_WORDS = {"control": "Ct", "vehicle": "Vh", "blank": "Bk", "untreated": "Un"}


def condition_abbrs(labels: dict) -> dict:
    """Plate letters per condition (key -> label): the extracted abbr if any,
    else a known lab word's short form ("Control" -> "Ct"), else a short
    last word ("Drug A" -> "A"), else the first letter; letters two
    conditions would share grow to two letters. At most 3 characters."""
    out = {}
    for key, c in labels.items():
        own = ((c or {}).get("abbr") or "").strip()[:3]
        label = ((c or {}).get("label") or key or "").strip()
        words = label.split()
        out[key] = own or _ABBR_WORDS.get(label.lower()) or (
            words[-1] if words and len(words) > 1 and len(words[-1]) <= 2 else label[:1].upper())
    for _ in range(2):  # shared letters grow: initials, then 2, then 3 letters
        dup = {a for a in out.values() if list(out.values()).count(a) > 1}
        for key, a in list(out.items()):
            if a not in dup or ((labels[key] or {}).get("abbr") or "").strip():
                continue
            label = ((labels[key] or {}).get("label") or key).strip()
            initials = "".join(w[:1].upper() for w in label.split())
            flat = label.replace(" ", "")
            out[key] = next(c for c in (initials if len(initials) > 1 else "", flat[:2].title(),
                                        flat[:3].title()) if c and len(c) > len(a) - 1 and c != a)
    return out


def card_lines(kind: str, slots: dict) -> tuple[str, str]:
    """A card's title and detail from pip_voice.json `cards` (the kind's
    template, else `default`). An empty slot drops its " · " part."""
    from collections import defaultdict
    from server.tone import section
    lines = section("cards")
    tpl = lines.get(kind) if isinstance(lines.get(kind), dict) else lines.get("default") or {}
    fill = lambda t: (t or "").format_map(defaultdict(str, {k: v for k, v in slots.items() if v is not None}))
    title = fill(tpl.get("title") or "{question}").strip()
    detail = " · ".join(p.strip() for p in fill(tpl.get("detail") or "").split(" · ") if p.strip())
    return title or slots.get("question") or "", detail


def step_short_title(step: dict) -> str:
    """Screen title of a plan step (<= 4 words): the extracted short_title,
    else "Add <liquid>" for a pipette step, else the step sentence through
    shorten_title ("Incubate the plate for 30 min" -> "Incubate the plate")."""
    s = step or {}
    own = " ".join((s.get("short_title") or "").split()[:4])
    if own:
        return own
    liquid = ((s.get("pipette") or {}).get("liquid") or "").strip()
    if s.get("kind") == "pipette" and liquid:
        return " ".join(f"Add {liquid}".split()[:4])
    return shorten_title(s.get("text") or "")


def protocol_short(doc: dict | None, fallback: str = "") -> str:
    doc = doc or {}
    return (doc.get("short_title") or "").strip() or shorten_title(doc.get("name") or fallback) or "untitled"


def _local(ts) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return None


def _clock(d: _dt.datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0")


def stamp(ts) -> str:
    """"Oct 29 · 12:51 AM" (local)."""
    d = _local(ts)
    return f"{d.strftime('%b')} {d.day} · {_clock(d)}" if d else ""


def protocol_label(doc: dict) -> dict:
    short = protocol_short(doc)
    display = " · ".join(x for x in (stamp(doc.get("imported_at")), short) if x)
    return {"display": display, "spoken": f"the {short} protocol"}


def _ordinal(i: int) -> str:
    return ORDINALS[i] if i < len(ORDINALS) else f"#{i + 1}"


def _proto_key(doc: dict, short_for) -> str:
    return doc.get("protocol_id") or short_for(doc)


def run_number(doc: dict, docs: list[dict], short_for) -> int:
    """1-based start order of this run among its protocol's runs ("Run 3")."""
    key = _proto_key(doc, short_for)
    same = sorted((x for x in docs if _proto_key(x, short_for) == key),
                  key=lambda x: x.get("started_at") or 0)
    return next((i + 1 for i, x in enumerate(same) if x.get("run_id") == doc.get("run_id")), len(same) or 1)


def day_label(ts, now: _dt.datetime | None = None) -> str:
    """"Today" or "Oct 27"."""
    d, now = _local(ts), now or _dt.datetime.now()
    if d is None:
        return ""
    return "Today" if d.date() == now.date() else f"{d.strftime('%b')} {d.day}"


def run_progress(doc: dict) -> dict:
    """Step reached: {"step" (1-based), "of", "state"}: "Step 4 of 5" while
    active, "Ended at step 3" when it stopped early, else "Finished"."""
    rec = (doc or {}).get("record") or {}
    of = len((rec.get("plan") or {}).get("steps") or [])
    step = min(int(rec.get("step_index") or 0) + 1, of) if of else 0
    if doc.get("status") == "active":
        state = f"Step {step} of {of}" if of else "In progress"
    elif doc.get("status") == "paused":
        state = f"Paused at step {step}" if of else "Paused"
    else:
        state = f"Ended at step {step}" if of and step < of else "Finished"
    return {"step": step, "of": of, "state": state}


def run_label(doc: dict, docs: list[dict], short_for, active_id: str | None = None,
              now: _dt.datetime | None = None) -> dict:
    """A run is named by its start order inside its protocol: display "Run 3",
    spoken "the active run" or "Drug A/B Dosing run 3"; plus its day
    ("Today", "Oct 27") and step state ("Step 4 of 5", "Ended at step 3",
    "Finished")."""
    n = run_number(doc, docs, short_for)
    out = {"display": f"Run {n}", "number": n, "day": day_label(doc.get("started_at"), now),
           **run_progress(doc)}
    if active_id and doc.get("run_id") == active_id:
        return {**out, "spoken": "the active run"}
    return {**out, "spoken": f"{short_for(doc)} run {n}"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _mentions(phrase: str, name: str) -> bool:
    """The phrase names this title by its lead: the first two words, spaces
    and slashes ignored ("the Drug A/B run" names "Drug A/B Dosing")."""
    lead = _compact(" ".join((name or "").split()[:2]))
    return len(lead) >= 3 and lead in _compact(phrase)


_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\W|$)", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"], 1)}


def select_run(phrase: str, docs: list[dict], short_for, active_id: str | None = None,
               now: _dt.datetime | None = None, context: str | None = None) -> tuple[dict | None, list[dict]]:
    """What someone said -> (run, []) or (None, two candidates when ambiguous)
    or (None, []) when nothing matches. Filters from the label facts: active,
    latest, day (today / yesterday / Oct 29 / 2026-10-29), time (2 PM, 14:20),
    protocol short title, run number ("run 2": the named protocol's, else the
    `context` protocol's), ordinal. An exact run id still works."""
    now = now or _dt.datetime.now()
    p = (phrase or "").strip()
    if not docs:
        return None, []
    exact = next((d for d in docs if d.get("run_id") == p), None)
    if exact:
        return exact, []
    low = p.lower()
    newest = sorted(docs, key=lambda d: d.get("started_at") or 0, reverse=True)
    if re.search(r"\b(active|current|ongoing)\b", low) and active_id:
        hit = next((d for d in docs if d.get("run_id") == active_id), None)
        if hit:
            return hit, []
    pool, used = list(newest), False
    day = None
    if re.search(r"\btoday\b", low):
        day = now.date()
    elif re.search(r"\byesterday\b", low):
        day = (now - _dt.timedelta(days=1)).date()
    elif m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low):
        day = _dt.date(int(m[1]), int(m[2]), int(m[3]))
    elif m := re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b", low):
        day = _dt.date(now.year, _MONTHS[m[1]], int(m[2]))
    if day:
        pool, used = [d for d in pool if (x := _local(d.get("started_at"))) and x.date() == day], True
    t = None if re.search(r"\d{4}-\d{2}-\d{2}", low) else _TIME.search(re.sub(
        r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+\d{1,2}\b", "", low))
    if t and (t[2] or t[3]):  # a bare number is not a time ("2 runs")
        h = int(t[1]) % 12 + (12 if (t[3] or "").startswith("p") else 0) if t[3] else int(t[1])
        mins = int(t[2]) if t[2] else None
        pool = [d for d in pool if (x := _local(d.get("started_at"))) and x.hour == h
                and (mins is None or x.minute == mins)]
        used = True
    named = [d for d in pool if _mentions(p, short_for(d))]
    if named:
        pool, used = named, True
    if m := re.search(r"\brun\s*(?:number\s*|#\s*)?(\d{1,3})\b", low):
        hits = [d for d in pool if run_number(d, docs, short_for) == int(m[1])]
        if not named and context and len({_proto_key(d, short_for) for d in hits}) > 1:
            hits = [d for d in hits if _proto_key(d, short_for) == context] or hits
        pool, used = hits, True
    if m := re.search(r"\b(" + "|".join(ORDINALS) + r")\b", low):
        same = sorted(pool, key=lambda d: d.get("started_at") or 0)
        i = ORDINALS.index(m[1])
        return (same[i], []) if i < len(same) else (None, [])
    if re.search(r"\b(latest|last|most recent|newest|recent)\b", low):
        return (pool[0], []) if pool else (None, [])
    if not used:
        return None, []
    if len(pool) == 1:
        return pool[0], []
    return None, pool[:2]


def select_protocol(phrase: str, docs: list[dict]) -> tuple[dict | None, list[dict]]:
    """A spoken protocol -> (doc, []) / (None, two candidates) / (None, [])."""
    p = (phrase or "").strip()
    exact = next((d for d in docs if d.get("id") == p), None)
    if exact:
        return exact, []
    said = _compact(re.sub(r"\b(the|protocol)\b", "", p.lower()))
    hits = [d for d in docs if _mentions(p, protocol_short(d))] or \
        [d for d in docs if len(said) >= 3 and (said in _compact(d.get("name"))
                                               or _compact(d.get("name")) in said)]
    if not hits:
        # words of it in the name ("the MTT one" -> MTT Viability Assay)
        stop = {"the", "one", "protocol", "run", "again", "new", "of", "for", "a", "an", "my", "that", "this"}
        words = {w for w in re.findall(r"[a-z0-9]+", p.lower()) if len(w) >= 3} - stop
        hits = [d for d in docs if words and words <= set(re.findall(r"[a-z0-9]+", f"{protocol_short(d)} {d.get('name')}".lower()))]
    if len(hits) == 1:
        return hits[0], []
    return (None, hits[:2]) if hits else (None, [])


def which_one(candidates: list[str]) -> str:
    """Ask between two spoken labels: "Today's 2:20 PM ... run or the first ...?"."""
    a, b = candidates[0], candidates[1]
    return f"{a[:1].upper() + a[1:]} or {b}?"


def search_label(doc: dict, now: _dt.datetime | None = None) -> dict:
    """A web search: day "Today" / "Oct 27", time "16:05", display
    "Oct 27 · 16:05", spoken "the 16:05 search about <question>"."""
    d = _local(doc.get("ts"))
    day, time_ = day_label(doc.get("ts"), now), d.strftime("%H:%M") if d else ""
    q = " ".join((doc.get("question") or doc.get("query") or "").split()[:6]).rstrip("?")
    # spoken: 12-hour clock, "today at 6:27 PM" / "on Oct 27 at 4:05 PM"
    said = "" if d is None else (f"today at {_clock(d)}" if day == "Today" else f"on {day} at {_clock(d)}")
    # clock: the answer card's time, as Milo says it ("12:08 AM")
    return {"day": day, "time": time_, "clock": _clock(d) if d else "",
            "display": " · ".join(x for x in (day, time_) if x), "searched": said,
            "spoken": " ".join(f"the {'' if day == 'Today' else day} {time_} search about {q}".split())}


_SEARCH_STOP = {"the", "a", "an", "one", "search", "searches", "open", "show", "me", "my", "about",
                "on", "for", "of", "latest", "last", "newest", "recent", "web", "that", "with"}


def select_search(phrase: str, docs: list[dict], now: _dt.datetime | None = None) -> tuple[dict | None, list[dict]]:
    """What someone said about a past search -> (doc, []) / (None, two
    candidates) / (None, []): "the latest search", "Oct 27, 16:05", "today",
    "the DMSO one" (words of the question or answer)."""
    now = now or _dt.datetime.now()
    low = (phrase or "").strip().lower()
    pool = sorted(docs, key=lambda d: d.get("ts") or 0, reverse=True)
    if not pool:
        return None, []
    exact = next((d for d in pool if d.get("id") == phrase.strip()), None)
    if exact:
        return exact, []
    used = False
    day = None
    if re.search(r"\btoday\b", low):
        day = now.date()
    elif re.search(r"\byesterday\b", low):
        day = (now - _dt.timedelta(days=1)).date()
    elif m := re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b", low):
        day = _dt.date(now.year, _MONTHS[m[1]], int(m[2]))
    if day:
        pool, used = [d for d in pool if (x := _local(d.get("ts"))) and x.date() == day], True
    if m := re.search(r"\b(\d{1,2}):(\d{2})\b", low):
        pool, used = [d for d in pool if (x := _local(d.get("ts"))) and (x.hour, x.minute) == (int(m[1]), int(m[2]))], True
    words = {w for w in re.findall(r"[a-z][a-z0-9]{2,}", low)} - _SEARCH_STOP - set(_MONTHS) - {"today", "yesterday"}
    if words:
        hits = [d for d in pool if words & set(re.findall(r"[a-z][a-z0-9]{2,}",
                                                          f"{d.get('question', '')} {d.get('answer_short', '')}".lower()))]
        pool, used = hits, True
    if re.search(r"\b(latest|last|newest|recent)\b", low) or not used:
        return (pool[0], []) if pool else (None, [])
    if len(pool) == 1:
        return pool[0], []
    return None, pool[:2]


def step_say(step: dict) -> str:
    """A step as Milo says it starting out: "add 100 µL culture medium to
    B2–D8" for a pipette step, else its sentence (speak_text turns the
    symbols into words)."""
    from server.grammar import wells_label
    s = step or {}
    p = s.get("pipette") or {}
    if s.get("kind") == "pipette" and p.get("liquid"):
        unit = {"ul": "µL", "ml": "mL"}.get(str(p.get("unit") or "").lower(), p.get("unit") or "")
        vol = f"{p['volume']:g} {unit} " if isinstance(p.get("volume"), (int, float)) else ""
        where = wells_label(p.get("wells") or s.get("wells") or [])
        return f"add {vol}{p['liquid']}" + (f" to {where}" if where else "")
    text = " ".join((s.get("text") or s.get("name") or "").split()).rstrip(".")
    return text[:1].lower() + text[1:]
