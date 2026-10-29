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


def run_label(doc: dict, docs: list[dict], short_for, active_id: str | None = None,
              now: _dt.datetime | None = None) -> dict:
    """display "Oct 29 · 2:20 PM · Drug A/B Dosing"; spoken relative to today:
    "the active run", "today's 2:20 PM Drug A/B Dosing run", "yesterday's
    Drug A/B Dosing run", "the Oct 27 ... run"; several the same day with the
    same protocol: "the second Drug A/B Dosing run today"."""
    now = now or _dt.datetime.now()
    short = short_for(doc)
    d = _local(doc.get("started_at"))
    display = " · ".join(x for x in (stamp(doc.get("started_at")), short) if x)
    if active_id and doc.get("run_id") == active_id:
        return {"display": display, "spoken": "the active run"}
    if d is None:
        return {"display": display, "spoken": f"the {short} run"}
    days = (now.date() - d.date()).days
    same = sorted((x for x in docs if (dx := _local(x.get("started_at"))) and dx.date() == d.date()
                   and short_for(x) == short), key=lambda x: x.get("started_at") or 0)
    when = {0: "today", 1: "yesterday"}.get(days, f"on {d.strftime('%b')} {d.day}")
    if len(same) > 1:
        i = next((k for k, x in enumerate(same) if x.get("run_id") == doc.get("run_id")), 0)
        return {"display": display, "spoken": f"the {_ordinal(i)} {short} run {when}"}
    if days == 0:
        spoken = f"today's {_clock(d)} {short} run"
    elif days == 1:
        spoken = f"yesterday's {short} run"
    else:
        spoken = f"the {d.strftime('%b')} {d.day} {short} run"
    return {"display": display, "spoken": spoken}


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
               now: _dt.datetime | None = None) -> tuple[dict | None, list[dict]]:
    """What someone said -> (run, []) or (None, two candidates when ambiguous)
    or (None, []) when nothing matches. Filters from the label facts: active,
    latest, day (today / yesterday / Oct 29 / 2026-10-29), time (2 PM, 14:20),
    protocol short title, ordinal. An exact run id still works."""
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
    if len(hits) == 1:
        return hits[0], []
    return (None, hits[:2]) if hits else (None, [])


def which_one(candidates: list[str]) -> str:
    """Ask between two spoken labels: "Today's 2:20 PM ... run or the first ...?"."""
    a, b = candidates[0], candidates[1]
    return f"{a[:1].upper() + a[1:]} or {b}?"
