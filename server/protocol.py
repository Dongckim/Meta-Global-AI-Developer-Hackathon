"""Protocol import: extract a run plan from PDF/txt, validate, draft, start.

Pipeline: bytes -> evidence text (pypdf, in memory) + Meta extraction with
text.format json_schema -> normalize -> quote checks + code validation ->
auto layout -> record["draft"] -> review (hand + voice) -> Start run.

Grounding rule: every value sits in its node's quote, every quote sits in
the document (norm_quote both sides). Failures become needs_review, never
silent drops. Blocking errors (overlap, invalid wells, unknown liquid or
condition id, bad WellRef, count > wells) prevent Start. Import never
writes tips; document cautions ride steps[].caution verbatim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from server.grammar import (
    _SPAN_STOPWORDS,
    find_amounts,
    parse_wellref,
    wells_label,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "shared" / "schemas" / "plan.schema.json"
PROTOCOL_SCHEMA: dict = json.loads(SCHEMA_PATH.read_text())

IMPORT_CACHE_KEY = "pip-bench-protocol"
IMPORT_MAX_OUTPUT_TOKENS = 4096
EVIDENCE_CHARS = 20000
PROMPT_CHARS = 60000
DEFAULT_N = 5
RESERVE_WELLS = 2
LAYOUT_ROWS = "BCDEFGH"
LAYOUT_FIRST_COL = 2
TIMED_KINDS = ("incubate", "centrifuge", "wait")
VOLUME_UNITS = {"ul", "ml"}

EXTRACT_PROMPT = (
    "Extract the bench run plan from this protocol document. "
    "Output ONLY what the document states. "
    "title: the document title (first heading), verbatim, or empty. "
    "short_title: at most 4 words taken from the title, for people to say "
    "(e.g. Drug A/B Dosing), or empty. "
    "liquids: reagents as {id, name, concentration, prep, quote} with short "
    "lowercase ids; quote is the verbatim sentence each liquid comes from. "
    "conditions: experimental conditions as {id, label, treatments, "
    "replicates, wells, quote}; treatments holds liquid ids; replicates is "
    "the count or null when unstated; wells holds WellRef strings (B2, "
    "B2-B8) ONLY when the document states positions, else null. "
    "steps: bench actions in document order as {kind, text, quote, minutes, "
    "temp_c, caution, pipette}; text is the document sentence lightly "
    "trimmed; seeding/design sentences belong to conditions, never steps; "
    "minutes/temp_c are stated numbers or null; pipette holds "
    "{liquid id, volume, unit, target, mix, repeat} for pipette, wash, and "
    "aspirate steps, else null; target type is wells (stated WellRef "
    "value), condition (its id), all_used (every used well), or other. "
    "caution is a verbatim caution sentence (do not..., Tip:/Note:/Caution: "
    "markers included) or null. There is no tip field: never write tips. "
    "Every quote must be verbatim document text. "
    "Never invent plate positions or numbers."
)


def strict_subset_ok(schema: dict) -> bool:
    """True when the schema satisfies the Responses strict subset."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    return _strict_node_ok(schema)


def _strict_node_ok(node: dict) -> bool:
    if not isinstance(node, dict):
        return True
    for banned in ("allOf", "oneOf"):
        if banned in node:
            return False
    props = node.get("properties")
    if isinstance(props, dict):
        if node.get("additionalProperties") is not False:
            return False
        if sorted(node.get("required", [])) != sorted(props):
            return False
        if not all(_strict_node_ok(v) for v in props.values()):
            return False
    items = node.get("items")
    if isinstance(items, dict) and not _strict_node_ok(items):
        return False
    addl = node.get("additionalProperties")
    if isinstance(addl, dict) and not _strict_node_ok(addl):
        return False
    return True


assert strict_subset_ok(PROTOCOL_SCHEMA), f"{SCHEMA_PATH} violates the strict subset"


def validate_against_schema(data, schema: dict) -> list[str]:
    """Mini JSON-Schema check (type/required/properties/items/enum subset)."""
    return _check_node(data, schema, "$")


def _check_node(data, schema: dict, path: str) -> list[str]:
    if not isinstance(schema, dict):
        return []
    errs = []
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else list(types or [])
    if types and not _type_ok(data, types):
        return [f"{path}: expected {types}"]
    if data is None:
        return []
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: not an allowed value")
    if isinstance(data, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in data:
                errs.append(f"{path}.{k}: missing")
        addl = schema.get("additionalProperties")
        for k, v in data.items():
            if k in props:
                errs += _check_node(v, props[k], f"{path}.{k}")
            elif addl is False:
                errs.append(f"{path}.{k}: not allowed")
            elif isinstance(addl, dict):
                errs += _check_node(v, addl, f"{path}.{k}")
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(data):
            errs += _check_node(v, schema["items"], f"{path}[{i}]")
    return errs


def _type_ok(data, types: list) -> bool:
    for t in types:
        if t == "null" and data is None:
            return True
        if t == "string" and isinstance(data, str):
            return True
        if t == "boolean" and isinstance(data, bool):
            return True
        if t == "integer" and isinstance(data, int) and not isinstance(data, bool):
            return True
        if t == "number" and isinstance(data, (int, float)) and not isinstance(data, bool):
            return True
        if t == "array" and isinstance(data, list):
            return True
        if t == "object" and isinstance(data, dict):
            return True
    return False


def normalize_group(name) -> str:
    """Group keys: lowercase, runs of [^a-z0-9] become one underscore."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def evidence_from_text(full: str, pages: list | None = None) -> dict:
    """Capped text for the record plus complete amounts for grounding.

    pages (per-page texts, PDF order) ride along capped to the same budget
    so quote_page assignment never stores bytes the record doesn't hold.
    """
    ev = {"text": (full or "")[:EVIDENCE_CHARS], "amounts": sorted(find_amounts(full or ""))}
    if pages:
        kept, budget = [], EVIDENCE_CHARS
        for pg in pages:
            if budget <= 0:
                break
            chunk = (pg or "")[:budget]
            kept.append(chunk)
            budget -= len(chunk)
        ev["pages"] = kept
    return ev


_QUOTE_DASHES = "‐‑‒–—―−"
_QUOTE_QUOTES = "‘’‚‛“”„‟"
_QUOTE_MU = "µμ"
_REPEAT_WORDS = {1: "once", 2: "twice", 3: "thrice"}
_MIN_WORD = re.compile(r"\bmin(ute)?s?\b")
_TEMP_WORD = re.compile(r"(c|f|degrees?|celsius|fahrenheit)\b")


def norm_quote(s: str) -> str:
    """One normalization for document, quotes, and values: lowercase, unify
    micro/mu to u, drop the degree sign, fold unicode dashes/quotes/spaces
    to ASCII, collapse whitespace."""
    s = (s or "").lower()
    for ch in _QUOTE_MU:
        s = s.replace(ch, "u")
    s = s.replace("°", "").replace("℃", "c").replace("℉", "f")
    for ch in _QUOTE_DASHES:
        s = s.replace(ch, "-")
    for ch in _QUOTE_QUOTES:
        s = s.replace(ch, "'")
    return re.sub(r"\s+", " ", s).strip()


def _num_token(v) -> str:
    """30 and 30.0 both read 30; other numbers keep their decimals."""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _content_words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", norm_quote(s)) if w not in _SPAN_STOPWORDS]


def _quote_in_doc(quote: str, doc: str) -> bool:
    q = norm_quote(quote)
    return bool(q) and q in doc


def _num_found(tok: str, nq: str):
    """The token as a standalone number (5 never matches 15, 30.5, or B5)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![\d.])", nq)


def _number_in_quote(value, nq: str, unit: str | None = None) -> bool:
    """value's digits plus its unit word sit in the normalized quote."""
    if not _num_found(_num_token(value), nq):
        return False
    if unit == "min":
        return _MIN_WORD.search(nq) is not None
    if unit == "temp":
        m = re.search(rf"(?<![a-z0-9]){re.escape(_num_token(value))}\s*([a-z]*)", nq)
        return bool(m) and (not m.group(1) or _TEMP_WORD.search(m.group(1)) is not None)
    return True


_UNIT_ALIASES = {
    "ul": ("ul", "microliter", "microlitre"),
    "ml": ("ml", "milliliter", "millilitre"),
}


def _unit_in_quote(unit: str, nq: str) -> bool:
    return any(a in nq for a in _UNIT_ALIASES.get(norm_quote(unit), (norm_quote(unit),)))


def _name_in_quote(name: str, nq: str) -> bool:
    """Every content word of the name appears in the normalized quote."""
    words = _content_words(name)
    return bool(words) and all(w in nq for w in words)


def _repeat_in_quote(count: int, nq: str) -> bool:
    tok = str(count)
    if _num_found(tok, nq) or f"x{tok}" in nq:
        return True
    word = _REPEAT_WORDS.get(count)
    return bool(word) and re.search(rf"\b{word}\b", nq) is not None


def _wells_in_quote(entry: str, nq: str) -> bool:
    """A WellRef entry matches by whole string or by both endpoints."""
    if norm_quote(entry) in nq:
        return True
    wells = parse_wellref(entry)
    return bool(wells) and wells[0].lower() in nq and wells[-1].lower() in nq


# --- Quote repair: model quotes are hints, code re-grounds in the doc ---

_MAX_ROW_GAP = 2  # table row block: label line plus following cells
_MAX_SENT_GAP = 1  # same or adjacent line (covers mid-sentence breaks)
_MAX_SPAN_CHARS = 400  # degenerate giant lines never ground by proximity
_LONG_LINE = 400

_ANCHOR_DROP = {"an", "the", "ul", "ml", "l", "um", "mm", "nm", "min", "mins", "c"}

_KIND_WORDS = {
    "pipette": ("pipette", "pipet", "add", "dispense", "transfer"),
    "incubate": ("incubate", "incubation", "incubated"),
    "wait": ("wait",),
    "wash": ("wash", "washed", "rinse"),
    "aspirate": ("aspirate", "remove"),
    "mix": ("mix", "mixed"),
    "centrifuge": ("centrifuge", "spin"),
    "read": ("read", "measure"),
    "prep": ("prep", "prepare"),
    "other": (),
}

_COUNT_UNIT_AFTER = re.compile(r"\s*(ul|ml|microliter|millilitre|min|ute|um\b|g\b|mg|c\b)")


def _anchor_words(name: str) -> list[str]:
    """Anchor tokens: keep single letters (Drug A vs B), drop units/numbers."""
    return [w for w in re.findall(r"[a-z0-9]+", norm_quote(name))
            if w not in _ANCHOR_DROP and not w.isdigit()]


def _word_in(word: str, span: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", span) is not None


def _phrase_in(phrase: str, span: str) -> bool:
    p = norm_quote(phrase)
    return bool(p) and re.search(rf"\b{re.escape(p)}\b", span) is not None


def _well_in_span(well: str, span: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(well.lower())}(?![0-9])", span) is not None


def _count_in_span(n: int, span: str) -> bool:
    """Standalone count not followed by a unit (10 uL is a volume, not n)."""
    m = _num_found(str(n), span)
    if not m:
        return False
    return _COUNT_UNIT_AFTER.match(span[m.end():m.end() + 8]) is None


def _doc_lines(text: str) -> list[tuple[str, str]]:
    """(raw, normalized) non-blank lines; giant lines split into sentences."""
    out = []
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if len(line) > _LONG_LINE:
            parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", line) if p.strip()]
            out += [(p, norm_quote(p)) for p in parts] or [(line, norm_quote(line))]
        else:
            out.append((line, norm_quote(line)))
    return out


def _doc_sentences(text: str) -> list[tuple[str, str]]:
    """(raw, normalized) sentences; a newline before lowercase rejoins wraps."""
    out = []
    for chunk in re.split(r"(?<=[.!?])\s+", text or ""):
        buf = ""
        for raw in chunk.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if buf and line[:1].islower():
                buf += " " + line
            else:
                if buf:
                    out.append(buf)
                buf = line
        if buf:
            out.append(buf)
    return [(s, norm_quote(s)) for s in out if s.strip()]


def _find_span(nlines: list[str], anchor_words, value_match, max_gap, start_match=None):
    """Best span where anchor + value co-occur: coverage, shortest, earliest.

    start_match constrains the span start (table rows run label-first, so a
    previous row's count plus the next row's label never forms a span).
    """
    best = None
    n = len(nlines)
    for i in range(n):
        if start_match is not None and not start_match(nlines[i]):
            continue
        for j in range(i, min(n, i + max_gap + 1)):
            span = " ".join(nlines[i:j + 1])
            if len(span) > _MAX_SPAN_CHARS or not value_match(span):
                continue
            cov = sum(1 for w in anchor_words if _word_in(w, span))
            if anchor_words and cov == 0:
                continue
            key = (cov, -(j - i), -i)
            if best is None or key > best[0]:
                best = (key, i, j)
    return best[1:] if best else None


def _span_offset(ndoc: str, nlines: list[str], i: int, j: int) -> int:
    return ndoc.find(" ".join(nlines[i:j + 1]))


def _repair_collect(model_quote: str, ndoc: str, found: list[tuple[int, str]],
                  current: str = "model"):
    """Model piece (when doc-verbatim) + code-found spans -> quote + source.

    No new evidence means no mutation at all: quote text and source stay
    untouched, which keeps recomputes idempotent.
    """
    if not found:
        return model_quote or "", current or "model"
    pieces = []  # (offset, raw text, is_model)
    nq = norm_quote(model_quote)
    if nq and nq in ndoc:
        pieces.append((ndoc.find(nq), (model_quote or "").strip(), True))
    seen = {p[1] for p in pieces}
    for off, txt in found:
        if txt and txt not in seen:
            seen.add(txt)
            pieces.append((off, txt, False))
    norms = {id(p): norm_quote(p[1]) for p in pieces}
    keep = [p for p in pieces
            if not any(p is not o and norms[id(p)] != norms[id(o)]
                       and norms[id(p)] in norms[id(o)] for o in pieces)]
    if not keep:
        return model_quote or "", current or "model"
    keep.sort(key=lambda p: p[0])
    src = "model" if all(p[2] for p in keep) else "code"
    return " ".join(t for _, t, _ in keep), src


def _repair_liquid(liq: dict, lines, ndoc: str) -> None:
    nlines = [n for _, n in lines]
    found = []
    nq = norm_quote(liq.get("quote") or "")
    name = liq.get("name") or ""
    if not _phrase_in(name, nq):
        hit = _find_span(nlines, [], lambda sp: _phrase_in(name, sp), _MAX_SENT_GAP)
        if hit:
            i, j = hit
            found.append((_span_offset(ndoc, nlines, i, j),
                          " ".join(raw for raw, _ in lines[i:j + 1])))
    conc = liq.get("concentration")
    if isinstance(conc, str) and conc.strip() and norm_quote(conc) not in nq:
        nc = norm_quote(conc)
        hit = _find_span(nlines, _anchor_words(name), lambda sp: nc in sp, _MAX_SENT_GAP)
        if hit:
            i, j = hit
            found.append((_span_offset(ndoc, nlines, i, j),
                          " ".join(raw for raw, _ in lines[i:j + 1])))
    liq["quote"], liq["quote_source"] = _repair_collect(liq.get("quote") or "", ndoc, found, liq.get("quote_source") or "model")


def _repair_condition(key: str, c: dict, lines, ndoc: str) -> None:
    # No standalone label search: a bare label mention (title, purpose) is
    # not the row. The label verifies through row spans, which start at a
    # label line by construction.
    nlines = [n for _, n in lines]
    label = c.get("label") or key
    anchors = _anchor_words(label)
    found = []
    nq = norm_quote(c.get("quote") or "")
    start = lambda ln: _phrase_in(label, ln)  # noqa: E731

    def seek(value_match, gap=_MAX_ROW_GAP):
        hit = _find_span(nlines, anchors, value_match, gap, start_match=start)
        if hit:
            i, j = hit
            found.append((_span_offset(ndoc, nlines, i, j),
                          " ".join(raw for raw, _ in lines[i:j + 1])))

    n = c.get("replicates")
    need_row = not _phrase_in(label, nq)
    if isinstance(n, int) and not isinstance(n, bool) and (need_row or not _number_in_quote(n, nq)):
        seek(lambda sp: _count_in_span(n, sp))
    for e in [w for w in (c.get("wells") or []) if isinstance(w, str)]:
        if _wells_in_quote(e, nq):
            continue
        wells = parse_wellref(e)
        if not wells:
            continue
        lo, hi = wells[0].lower(), wells[-1].lower()
        seek(lambda sp, lo=lo, hi=hi: _well_in_span(lo, sp) and _well_in_span(hi, sp))
    c["quote"], c["quote_source"] = _repair_collect(c.get("quote") or "", ndoc, found, c.get("quote_source") or "model")


def _step_anchors(s: dict, liquids: dict) -> list[str]:
    words: list[str] = []
    p = s.get("pipette") if isinstance(s.get("pipette"), dict) else None
    if p and p.get("liquid") in (liquids or {}):
        words += _anchor_words((liquids[p["liquid"]] or {}).get("name") or "")
    words += list(_KIND_WORDS.get(s.get("kind") or "", ()))
    words += _anchor_words(s.get("text") or "")
    return list(dict.fromkeys(words))


def _repair_step(s: dict, liquids: dict, lines, ndoc: str) -> None:
    nlines = [n for _, n in lines]
    anchors = _step_anchors(s, liquids)
    found = []
    nq = norm_quote(s.get("quote") or "")

    def seek(value_match):
        hit = _find_span(nlines, anchors, value_match, _MAX_SENT_GAP)
        if hit:
            i, j = hit
            found.append((_span_offset(ndoc, nlines, i, j),
                          " ".join(raw for raw, _ in lines[i:j + 1])))

    m = s.get("minutes")
    if _is_num(m) and not _number_in_quote(m, nq, "min"):
        seek(lambda sp: _number_in_quote(m, sp, "min"))
    t = s.get("temp_c")
    if _is_num(t) and not _number_in_quote(t, nq, "temp"):
        seek(lambda sp: _number_in_quote(t, sp, "temp"))
    caut = s.get("caution")
    if isinstance(caut, str) and caut.strip() and norm_quote(caut) not in nq:
        nc = norm_quote(caut)
        hit = _find_span(nlines, [], lambda sp: nc in sp, _MAX_SENT_GAP)
        if hit:
            i, j = hit
            found.append((_span_offset(ndoc, nlines, i, j),
                          " ".join(raw for raw, _ in lines[i:j + 1])))
    p = s.get("pipette") if isinstance(s.get("pipette"), dict) else None
    if p is not None:
        v = p.get("volume")
        if _is_num(v) and not _number_in_quote(v, nq):
            seek(lambda sp: _number_in_quote(v, sp))
        u = p.get("unit")
        if isinstance(u, str) and u.strip() and not _unit_in_quote(u, nq):
            seek(lambda sp: _unit_in_quote(u, sp))
        tgt = p.get("target") if isinstance(p.get("target"), dict) else {}
        if tgt.get("type") == "wells" and isinstance(tgt.get("value"), str):
            tval = tgt["value"]
            if not _wells_in_quote(tval, nq):
                wells = parse_wellref(tval)
                if wells:
                    lo, hi = wells[0].lower(), wells[-1].lower()
                    seek(lambda sp, lo=lo, hi=hi: _well_in_span(lo, sp) and _well_in_span(hi, sp))
        r = p.get("repeat")
        if isinstance(r, int) and not isinstance(r, bool) and not _repeat_in_quote(r, nq):
            seek(lambda sp: _repeat_in_quote(r, sp))
    s["quote"], s["quote_source"] = _repair_collect(s.get("quote") or "", ndoc, found, s.get("quote_source") or "model")


def repair_quotes(plan: dict, evidence: dict) -> None:
    """Model quotes are hints; code re-grounds shaky values in the document.

    Runs at build and on every recompute. Idempotent: values already in the
    quote are never re-searched. Repaired nodes carry quote_source "code".
    Conditions search line windows (table rows); liquids and steps search
    sentence windows.
    """
    text = (evidence or {}).get("text", "")
    ndoc = norm_quote(text)
    lines = _doc_lines(text)
    sents = _doc_sentences(text)
    for liq in (plan.get("liquids") or {}).values():
        _repair_liquid(liq or {}, sents, ndoc)
    for key, c in (plan.get("conditions") or {}).items():
        _repair_condition(key, c or {}, lines, ndoc)
    for s in plan.get("steps") or []:
        _repair_step(s or {}, plan.get("liquids") or {}, sents, ndoc)


def assign_quote_pages(plan: dict, evidence: dict) -> None:
    """First page holding each quote (1-based, else null). Runs post-repair."""
    pages = [norm_quote(pg) for pg in (evidence or {}).get("pages") or []]
    if not pages:
        pages = [norm_quote((evidence or {}).get("text", ""))]

    def page_of(quote: str):
        nq = norm_quote(quote)
        if not nq:
            return None
        for cand in (nq, nq[:120], nq[:60]):
            if not cand:
                continue
            for i, pg in enumerate(pages):
                if cand in pg:
                    return i + 1
        return None

    for node in list((plan.get("liquids") or {}).values()):
        (node or {})["quote_page"] = page_of((node or {}).get("quote") or "")
    for node in list((plan.get("conditions") or {}).values()):
        (node or {})["quote_page"] = page_of((node or {}).get("quote") or "")
    for node in plan.get("steps") or []:
        (node or {})["quote_page"] = page_of((node or {}).get("quote") or "")


def resolve_short_title(draft: dict) -> str:
    """Spoken/display short title: the model's (<= 4 words), else a rule
    over the library name."""
    from server.labels import shorten_title
    short = " ".join((((draft or {}).get("plan") or {}).get("short_title") or "").split()[:4])
    return short or shorten_title(resolve_name(draft))


def resolve_name(draft: dict) -> str:
    """Library name: grounded title, else the source filename stem."""
    plan = (draft or {}).get("plan") or {}
    title = (plan.get("title") or "").strip()
    flagged = any(i.get("path") == "title" for i in (draft or {}).get("review", []))
    if title and not flagged:
        return title
    stem = ((draft or {}).get("meta") or {}).get("source") or "protocol"
    stem = stem.rsplit("/", 1)[-1]
    return stem.rsplit(".", 1)[0] if "." in stem else stem


def _norm_id(raw) -> str:
    return normalize_group(raw)


def build_draft(raw: dict, evidence: dict, source: str) -> dict:
    """Normalize extraction -> auto layout -> validate. Returns the draft."""
    liquids: dict[str, dict] = {}
    for liq in raw.get("liquids") or []:
        liq = liq if isinstance(liq, dict) else {}
        key = _norm_id(liq.get("id"))
        if not key or key in liquids:
            continue
        liquids[key] = {"name": liq.get("name") or "", "concentration": liq.get("concentration"),
                        "prep": liq.get("prep"), "quote": liq.get("quote") or ""}
    conditions: dict[str, dict] = {}
    for c in raw.get("conditions") or []:
        c = c if isinstance(c, dict) else {}
        key = _norm_id(c.get("id"))
        if not key or key in conditions:
            continue
        conditions[key] = {"label": c.get("label") or key,
                           "treatments": [t for t in (c.get("treatments") or [])
                                          if isinstance(t, str)],
                           "replicates": c.get("replicates"),
                           "wells": c.get("wells"), "quote": c.get("quote") or ""}
    steps = []
    for s in raw.get("steps") or []:
        s = s if isinstance(s, dict) else {}
        p = s.get("pipette") if isinstance(s.get("pipette"), dict) else None
        steps.append({
            "kind": s.get("kind") or "other",
            "text": s.get("text") or "",
            "quote": s.get("quote") or "",
            "minutes": s.get("minutes"),
            "temp_c": s.get("temp_c"),
            "caution": s.get("caution"),
            "pipette": ({"liquid": _norm_id(p.get("liquid")),
                         "volume": p.get("volume"), "unit": p.get("unit"),
                         "target": {"type": (p.get("target") or {}).get("type") or "other",
                                    "value": (p.get("target") or {}).get("value")},
                         "mix": p.get("mix"), "repeat": p.get("repeat")} if p else None),
            "wells": [],
        })
    draft = {"plan": {"title": (raw.get("title") or "").strip(),
                      "short_title": (raw.get("short_title") or "").strip(), "liquids": liquids,
                      "conditions": conditions, "steps": steps, "backups": []},
             "review": [], "errors": [],
             "meta": {"source": source, "evidence_chars": len(evidence.get("text", "")),
                      "evidence": evidence, "layout": {}}}
    repair_quotes(draft["plan"], draft["meta"]["evidence"])
    dropped = _settle_stated_wells(draft)
    _auto_layout(draft)
    recompute_draft(draft)
    draft["review"] = dropped + draft["review"]
    return draft


def _settle_stated_wells(draft: dict) -> list:
    """Build-time only: WellRef entries that sit in the condition quote
    expand to well lists; the rest drop to auto layout with a review item."""
    items = []
    for key in sorted(draft["plan"].get("conditions") or {}):
        c = draft["plan"]["conditions"][key]
        entries = [w for w in (c.get("wells") or []) if isinstance(w, str)]
        if not entries:
            c["wells"] = []
            continue
        nq = norm_quote(c.get("quote") or "")
        missing = [e for e in entries if not _wells_in_quote(e, nq)]
        if missing:
            c["wells"] = []
            items.append({"path": f"conditions.{key}.wells", "code": "dropped_positions",
                          "reason": f"not in the quote: {', '.join(missing)}",
                          "quote": c.get("quote") or ""})
            continue
        c["wells"] = sorted({w for e in entries for w in parse_wellref(e)},
                            key=lambda w: (w[0], int(w[1:])))
    return items


def _auto_layout(draft: dict) -> None:
    """Fill wells the document did not state: one row per condition from col 2.

    Unstated replicates default to DEFAULT_N; validate_draft flags them.
    Overflow and no-row states are also recomputed there, so this pass
    emits no items itself.
    """
    plan = draft["plan"]
    layout = draft["meta"]["layout"]
    taken_rows = {w[0] for c in plan["conditions"].values() for w in (c.get("wells") or [])
                  if isinstance(w, str) and len(w) >= 2}
    for key in sorted(plan["conditions"]):
        c = plan["conditions"][key]
        if c.get("replicates") is None:
            c["replicates"] = DEFAULT_N
        if [w for w in (c.get("wells") or []) if isinstance(w, str)]:
            layout.setdefault(key, "stated")
            continue
        n = c["replicates"] if isinstance(c.get("replicates"), int) else DEFAULT_N
        row = next((r for r in LAYOUT_ROWS if r not in taken_rows), None)
        if row is None:
            c["wells"] = []
            layout[key] = "auto"
            continue
        taken_rows.add(row)
        full = [f"{row}{c}" for c in range(LAYOUT_FIRST_COL, LAYOUT_FIRST_COL + n + RESERVE_WELLS)]
        c["wells"] = [w for w in full if int(w[1:]) <= 12]
        layout[key] = "auto"
        plan["backups"] += [w for w in c["wells"][-RESERVE_WELLS:] if w not in plan["backups"]]


def recompute_draft(draft: dict) -> None:
    """Repair quotes, page them, derive step wells + revalidate. Every edit."""
    plan, evidence = draft["plan"], draft["meta"]["evidence"]
    repair_quotes(plan, evidence)
    assign_quote_pages(plan, evidence)
    review = derive_step_wells(plan)
    more, errors = validate_draft(plan, evidence, draft["meta"]["layout"])
    draft["review"], draft["errors"] = review + more, errors


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_draft(plan: dict, evidence: dict, layout: dict | None = None) -> tuple[list, list]:
    """Review flags (warn) + errors (block Start). Stateless recompute."""
    review, errors = [], []
    layout = layout or {}
    doc = norm_quote((evidence or {}).get("text", ""))
    liquids = plan.get("liquids") or {}
    conditions = plan.get("conditions") or {}

    def flag(path, code, reason, quote=""):
        item = {"path": path, "code": code, "reason": reason}
        if quote:
            item["quote"] = quote
        review.append(item)

    title = (plan.get("title") or "").strip()
    if not title:
        flag("title", "missing_value", "no title found; filename is used instead")
    elif not _phrase_in(title, doc):
        flag("title", "ungrounded_value", f"{title} not in the document")

    for key in sorted(liquids):
        liq = liquids[key] or {}
        base = f"liquids.{key}"
        quote = liq.get("quote") or ""
        if not (liq.get("name") or "").strip():
            errors.append({"path": f"{base}.name", "code": "empty_name",
                           "reason": "liquid needs a name"})
        if liq.get("quote_source") != "code" and not _quote_in_doc(quote, doc):
            flag(base, "ungrounded_quote", "quote not in the document", quote)
            continue
        nq = norm_quote(quote)
        if not _name_in_quote(liq.get("name") or "", nq):
            flag(f"{base}.name", "ungrounded_value",
                 f"{liq.get('name')} not in its quote", quote)
        conc = liq.get("concentration")
        if isinstance(conc, str) and conc.strip() and norm_quote(conc) not in nq:
            flag(f"{base}.concentration", "ungrounded_value",
                 f"{conc} not in its quote", quote)
    for key in sorted(conditions):
        c = conditions[key] or {}
        base = f"conditions.{key}"
        quote = c.get("quote") or ""
        wells = c.get("wells") or []
        bad = [w for w in wells if parse_wellref(w) != [str(w).upper()]]
        if bad:
            errors.append({"path": f"{base}.wells", "code": "invalid_wells",
                           "reason": f"not plate wells: {', '.join(bad)}"})
        good = [w for w in wells if w not in bad]
        n = c.get("replicates")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            errors.append({"path": f"{base}.replicates", "code": "bad_n",
                           "reason": "count must be a whole number of 1 or more"})
        elif n > len(good):
            errors.append({"path": f"{base}.replicates", "code": "n_overflow",
                           "reason": f"needs {n} wells but has {len(good)}"})
        if layout.get(key) == "auto":
            if not good:
                flag(f"{base}.wells", "no_row", "no free row left")
            elif good[-1][1:] == "12" and len(good) < (n if isinstance(n, int) else 0) + RESERVE_WELLS:
                flag(f"{base}.wells", "overflow",
                     f"row full, clamped to {len(good)} wells")
        for t in c.get("treatments") or []:
            if t not in liquids:
                errors.append({"path": f"{base}.treatments", "code": "unknown_liquid",
                               "reason": f"no such liquid: {t}"})
        if c.get("quote_source") != "code" and not _quote_in_doc(quote, doc):
            flag(base, "ungrounded_quote", "quote not in the document", quote)
            continue
        nq = norm_quote(quote)
        if not _name_in_quote(c.get("label") or key, nq):
            flag(base, "ungrounded_value",
                 f"{c.get('label') or key} not in its quote", quote)
        if isinstance(n, int) and not isinstance(n, bool) and not _number_in_quote(n, nq):
            flag(f"{base}.replicates", "ungrounded_value",
                 f"{n} not in its quote", quote)
    seen: dict[str, str] = {}
    for key in sorted(conditions):
        wells = [w for w in ((conditions[key] or {}).get("wells") or [])
                 if parse_wellref(w) == [str(w).upper()]]
        clashes: dict[str, list] = {}
        for w in wells:
            if w in seen:
                clashes.setdefault(seen[w], []).append(w)
            else:
                seen[w] = key
        for other, ws in sorted(clashes.items()):
            errors.append({"path": f"conditions.{key}.wells", "code": "overlap",
                           "reason": f"{wells_label(sorted(ws))} overlaps {other}"})
    for i, s in enumerate(plan.get("steps") or []):
        s = s or {}
        base = f"steps.{i}"
        quote = s.get("quote") or ""
        if not (s.get("text") or "").strip():
            errors.append({"path": f"{base}.text", "code": "empty_name",
                           "reason": "step needs text"})
        if s.get("quote_source") != "code" and not _quote_in_doc(quote, doc):
            flag(base, "ungrounded_quote", "quote not in the document", quote)
            continue
        nq = norm_quote(quote)
        m = s.get("minutes")
        if m is None:
            if (s.get("kind") or "") in TIMED_KINDS:
                flag(f"{base}.minutes", "missing_value",
                     "timed step needs minutes", quote)
        elif not _is_num(m) or m < 0:
            errors.append({"path": f"{base}.minutes", "code": "bad_minutes",
                           "reason": "minutes must be 0 or more"})
        elif not _number_in_quote(m, nq, "min"):
            flag(f"{base}.minutes", "ungrounded_value",
                 f"{_num_token(m)} min not in its quote", quote)
        t = s.get("temp_c")
        if t is not None and (not _is_num(t) or not _number_in_quote(t, nq, "temp")):
            if not _is_num(t):
                errors.append({"path": f"{base}.temp_c", "code": "bad_temp",
                               "reason": "temp must be a number"})
            else:
                flag(f"{base}.temp_c", "ungrounded_value",
                     f"{_num_token(t)} C not in its quote", quote)
        caut = s.get("caution")
        if isinstance(caut, str) and caut.strip() and norm_quote(caut) not in nq:
            flag(f"{base}.caution", "ungrounded_value",
                 "caution not in its quote", quote)
        p = s.get("pipette") if isinstance(s.get("pipette"), dict) else None
        if p is None:
            continue
        if p.get("liquid") not in liquids:
            errors.append({"path": f"{base}.pipette.liquid", "code": "unknown_liquid",
                           "reason": f"no such liquid: {p.get('liquid')}"})
        v = p.get("volume")
        if v is None:
            flag(f"{base}.pipette.volume", "missing_value",
                 "pipette step needs a volume", quote)
        elif not _is_num(v) or v < 0:
            errors.append({"path": f"{base}.pipette.volume", "code": "bad_volume",
                           "reason": "volume must be 0 or more"})
        elif not _number_in_quote(v, nq):
            flag(f"{base}.pipette.volume", "ungrounded_value",
                 f"{_num_token(v)} not in its quote", quote)
        u = p.get("unit")
        if u is None:
            if v is not None:
                flag(f"{base}.pipette.unit", "missing_value",
                     "volume needs a unit", quote)
        elif norm_quote(u) not in VOLUME_UNITS:
            errors.append({"path": f"{base}.pipette.unit", "code": "bad_unit",
                           "reason": f"unit must be ul or ml, not {u}"})
        elif not _unit_in_quote(u, nq):
            flag(f"{base}.pipette.unit", "ungrounded_value",
                 f"{u} not in its quote", quote)
        tgt = p.get("target") if isinstance(p.get("target"), dict) else {}
        ttype, tval = tgt.get("type"), tgt.get("value")
        if ttype == "wells":
            if not tval or not parse_wellref(tval):
                errors.append({"path": f"{base}.pipette.target", "code": "bad_target",
                               "reason": "wells target needs a WellRef"})
            elif not _wells_in_quote(tval, nq):
                flag(f"{base}.pipette.target", "ungrounded_value",
                     f"{tval} not in its quote", quote)
        elif ttype == "condition":
            if tval not in conditions:
                errors.append({"path": f"{base}.pipette.target", "code": "unknown_condition",
                               "reason": f"no such condition: {tval}"})
        elif ttype not in ("all_used", "other"):
            errors.append({"path": f"{base}.pipette.target", "code": "bad_target",
                           "reason": f"unknown target type: {ttype}"})
        r = p.get("repeat")
        if r is not None:
            if not isinstance(r, int) or isinstance(r, bool) or not 1 <= r <= 12:
                errors.append({"path": f"{base}.pipette.repeat", "code": "bad_repeat",
                               "reason": "repeat must be 1-12"})
            elif not _repeat_in_quote(r, nq):
                flag(f"{base}.pipette.repeat", "ungrounded_value",
                     f"{r}x not in its quote", quote)
    return review, errors


_ID = r"[A-Za-z0-9_]+"


def parse_draft_path(path: str):
    """'conditions.drug_a.wells' -> ('condition', 'drug_a', 'wells').
    'liquids.drug_a' renames; 'steps[2]' == 'steps.2'. None when malformed."""
    p = (path or "").strip().replace("[", ".").replace("]", "")
    parts = [x for x in p.split(".") if x != ""]
    if len(parts) == 2 and parts[0] == "liquids" and re.fullmatch(_ID, parts[1]):
        return ("liquid", parts[1], "name")
    if len(parts) >= 2 and parts[0] == "conditions" and re.fullmatch(_ID, parts[1]):
        if len(parts) == 2:
            return ("condition", parts[1], "label")
        if len(parts) == 3 and parts[2] in ("replicates", "wells"):
            return ("condition", parts[1], parts[2])
        return None
    if len(parts) >= 3 and parts[0] == "steps" and parts[1].isdigit():
        rest = parts[2:]
        if rest == ["text"] or rest == ["minutes"] or rest == ["temp_c"] or rest == ["caution"]:
            return ("step", int(parts[1]), rest[0])
        if rest == ["pipette", "volume"] or rest == ["pipette", "target"]:
            return ("step", int(parts[1]), ".".join(rest))
    return None


_ROW_VALUE = re.compile(r"^row\s+([a-h])$", re.IGNORECASE)


def _num_text(raw) -> float | None:
    """Strict float, else a leading number plus one alpha suffix (50 uL).

    Never fishes digits out of identifiers: C2 and step 5 stay invalid.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    t = (raw or "").strip() if isinstance(raw, str) else ""
    try:
        return float(t)
    except (ValueError, TypeError):
        pass
    m = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([a-zA-Z°µμ]+)", t)
    return float(m.group(1)) if m else None


def parse_target_value(draft: dict, raw: str):
    """'wells:<WellRef>' | 'condition:<id>' | 'all' | 'other' -> target dict."""
    t = (raw or "").strip()
    plan = draft["plan"]
    if t.lower() == "all":
        return True, {"type": "all_used", "value": None}
    if t.lower() == "other":
        return True, {"type": "other", "value": None}
    if t.lower().startswith("wells:"):
        wells = parse_wellref(t[6:])
        if not wells:
            return False, f"not plate wells: {t[6:]}"
        return True, {"type": "wells", "value": t[6:].strip()}
    if t.lower().startswith("condition:"):
        cid = normalize_group(t[10:])
        if cid not in (plan.get("conditions") or {}):
            return False, f"no such condition: {t[10:].strip()}"
        return True, {"type": "condition", "value": cid}
    cid = normalize_group(t)
    if cid in (plan.get("conditions") or {}):
        return True, {"type": "condition", "value": cid}
    wells = parse_wellref(t)
    if wells:
        return True, {"type": "wells", "value": t}
    return False, f"need wells:<WellRef>, condition:<id>, all, or other: {raw}"


def parse_draft_value(draft: dict, parsed, raw):
    """Tool-channel strings -> typed values. Returns (ok, value|reason)."""
    kind, key, field = parsed
    raw = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    plan = draft["plan"]
    if kind == "liquid":
        if key not in (plan.get("liquids") or {}):
            return False, f"no such liquid: {key}"
        return (True, raw.strip()) if raw.strip() else (False, "liquid needs a name")
    if kind == "condition":
        c = (plan.get("conditions") or {}).get(key)
        if c is None:
            return False, f"no such condition: {key}"
        if field == "label":
            return (True, raw.strip()) if raw.strip() else (False, "condition needs a label")
        if field == "replicates":
            val = _num_text(raw)
            if val is None or not val.is_integer():
                return False, "count must be a whole number"
            return (True, int(val)) if val >= 1 else (False, "count must be 1 or more")
        m = _ROW_VALUE.match((raw or "").strip())
        if m:
            cols = sorted({int(w[1:]) for w in (c.get("wells") or [])
                           if isinstance(w, str) and len(w) >= 2 and w[1:].isdigit()})
            if not cols:
                n = c.get("replicates") if isinstance(c.get("replicates"), int) else DEFAULT_N
                cols = list(range(LAYOUT_FIRST_COL, LAYOUT_FIRST_COL + n + RESERVE_WELLS))
            return True, [f"{m.group(1).upper()}{c}" for c in cols if c <= 12]
        wells = parse_wellref(raw or "")
        return (True, wells) if wells else (False, f"not plate wells: {raw}")
    steps = plan.get("steps") or []
    if key >= len(steps):
        return False, f"no step {key + 1}"
    if field in ("minutes", "temp_c", "pipette.volume"):
        noun = {"minutes": "minutes", "temp_c": "temp", "pipette.volume": "volume"}[field]
        val = _num_text(raw)
        if val is None:
            return False, f"{noun} must be a number"
        return (True, val) if val >= 0 else (False, f"{noun} must be 0 or more")
    if field == "pipette.target":
        return parse_target_value(draft, raw)
    return True, (raw or "").strip() or None


def apply_draft_edit(draft: dict, parsed, value) -> tuple[bool, str]:
    """Set one draft path (validated values only). Returns (ok, reason)."""
    kind, key, field = parsed
    plan = draft["plan"]
    if kind == "liquid":
        liq = (plan.get("liquids") or {}).get(key)
        if liq is None:
            return False, f"no such liquid: {key}"
        if not isinstance(value, str) or not value.strip():
            return False, "liquid needs a name"
        liq["name"] = value.strip()
        return True, ""
    if kind == "condition":
        c = (plan.get("conditions") or {}).get(key)
        if c is None:
            return False, f"no such condition: {key}"
        if field == "label":
            if not isinstance(value, str) or not value.strip():
                return False, "condition needs a label"
            c["label"] = value.strip()
            return True, ""
        if field == "replicates":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                return False, "count must be a whole number of 1 or more"
            c["replicates"] = value
            if draft["meta"]["layout"].get(key) == "auto":
                _relayout_row(draft, key)
            return True, ""
        if not value:
            return False, "needs at least one well"
        c["wells"] = value
        draft["meta"]["layout"][key] = "hand"
        return True, ""
    steps = plan.get("steps") or []
    if key >= len(steps):
        return False, f"no step {key + 1}"
    if field in ("minutes", "temp_c") and value is not None and (
            not isinstance(value, (int, float)) or value < 0):
        return False, f"{'minutes' if field == 'minutes' else 'temp'} must be 0 or more"
    if field == "pipette.volume":
        p = steps[key].get("pipette")
        if not isinstance(p, dict):
            return False, f"step {key + 1} has no pipette"
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            return False, "volume must be 0 or more"
        p["volume"] = value
        return True, ""
    if field == "pipette.target":
        p = steps[key].get("pipette")
        if not isinstance(p, dict):
            return False, f"step {key + 1} has no pipette"
        if not isinstance(value, dict) or value.get("type") not in (
                "wells", "condition", "all_used", "other"):
            return False, "need wells:<WellRef>, condition:<id>, all, or other"
        p["target"] = value
        return True, ""
    steps[key][field] = value
    return True, ""


def _relayout_row(draft: dict, key: str) -> None:
    """Auto conditions keep replicates + reserve wells after a count change."""
    c = draft["plan"]["conditions"][key]
    wells = c.get("wells") or []
    row = wells[0][0] if wells and isinstance(wells[0], str) else None
    if row not in LAYOUT_ROWS:
        used = {w[0] for k2, c2 in draft["plan"]["conditions"].items() if k2 != key
                for w in (c2.get("wells") or []) if isinstance(w, str) and len(w) >= 2}
        row = next((r for r in LAYOUT_ROWS if r not in used), None)
        if row is None:
            c["wells"] = []
            return
    n = c.get("replicates") if isinstance(c.get("replicates"), int) else DEFAULT_N
    c["wells"] = [f"{row}{c}" for c in range(LAYOUT_FIRST_COL, LAYOUT_FIRST_COL + n + RESERVE_WELLS)
                  if c <= 12]
    plan = draft["plan"]
    plan["backups"] = [w for w in plan.get("backups", []) if w not in c["wells"]]
    plan["backups"] += [w for w in c["wells"][-RESERVE_WELLS:] if w not in plan["backups"]]


def edit_target(parsed) -> str:
    """Human slot for the draft_edited voice line."""
    kind, key, field = parsed
    if kind == "liquid":
        return str(key).replace("_", " ")
    if kind == "condition":
        noun = {"replicates": "count", "wells": "wells"}.get(field, field)
        return f"{key} {noun}".replace("_", " ")
    noun = {"pipette.volume": "volume", "pipette.target": "target"}.get(field, field)
    return f"step {key + 1} {noun}"


def resolve_target(plan: dict, target: dict) -> list[str]:
    """Target dict -> well list. Unknown ids and bad WellRefs yield []."""
    conditions = plan.get("conditions") or {}
    ttype = (target or {}).get("type")
    tval = (target or {}).get("value")
    if ttype == "wells":
        return parse_wellref(tval) if isinstance(tval, str) else []
    if ttype == "condition":
        return list((conditions.get(tval) or {}).get("wells") or [])
    if ttype == "all_used":
        out: list[str] = []
        for c in conditions.values():
            out += [w for w in (c or {}).get("wells") or [] if w not in out]
        return out
    return []


def target_label(plan: dict, target: dict) -> str:
    """Human target for live steps: label, range, or scope."""
    ttype = (target or {}).get("type")
    tval = (target or {}).get("value")
    if ttype == "condition":
        return f"{(plan.get('conditions') or {}).get(tval, {}).get('label', tval)} wells"
    if ttype == "wells":
        return wells_label(resolve_target(plan, target)) or "wells"
    if ttype == "all_used":
        return "all used wells"
    return "—"


def derive_step_wells(plan: dict) -> list:
    """Pipette steps take their resolved target wells; returns mismatch items."""
    out = []
    for i, s in enumerate(plan.get("steps") or []):
        p = (s or {}).get("pipette") if isinstance((s or {}).get("pipette"), dict) else None
        if p is None:
            s["wells"] = []
            continue
        wells = resolve_target(plan, p.get("target") or {})
        s["wells"] = wells
        if (p.get("target") or {}).get("type") == "wells" and wells:
            pool = {w for c in (plan.get("conditions") or {}).values()
                    for w in ((c or {}).get("wells") or [])}
            if pool.isdisjoint(wells):
                out.append({"path": f"steps.{i}.pipette.target", "code": "mismatch",
                            "reason": "target wells sit outside every condition",
                            "quote": s.get("quote") or ""})
    return out


def build_extract_body(kind: str, filename: str, payload: str, model: str) -> dict:
    """Responses body: PDF rides input_file/file_data, txt rides input_text."""
    if kind == "pdf":
        content = [
            {"type": "input_text", "text": EXTRACT_PROMPT},
            {"type": "input_file", "filename": filename, "file_data": payload},
        ]
    else:
        content = [{"type": "input_text", "text": f"{EXTRACT_PROMPT}\n\nDocument:\n{payload}"}]
    return {
        "model": model,
        "instructions": "You extract bench run plans from protocol documents.",
        "input": [{"type": "message", "role": "user", "content": content}],
        "reasoning": {"effort": "minimal"},
        "max_output_tokens": IMPORT_MAX_OUTPUT_TOKENS,
        "text": {"format": {"type": "json_schema", "name": "BenchPlan",
                             "schema": PROTOCOL_SCHEMA, "strict": True}},
        "prompt_cache_key": IMPORT_CACHE_KEY,
        "prompt_cache_retention": "24h",
        "store": False,
    }


def parse_extract_payload(payload: dict):
    """Constrained output_text -> plan dict. Returns (ok, data|reason)."""
    if (payload or {}).get("status") == "incomplete":
        return False, "protocol too long, try fewer pages"
    texts = []
    for item in (payload or {}).get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
    try:
        data = json.loads("\n".join(texts))
    except (json.JSONDecodeError, TypeError):
        return False, "extraction did not return JSON"
    errs = validate_against_schema(data, PROTOCOL_SCHEMA)
    if errs:
        return False, f"extraction failed checks: {errs[0]}"
    return True, data


def build_start_plan(plan: dict) -> dict:
    """Draft -> live run plan: expand repeats, resolve targets + liquid names."""
    liquids = plan.get("liquids") or {}
    conditions = {}
    for key in sorted(plan.get("conditions") or {}):
        c = plan["conditions"][key] or {}
        conditions[key] = {
            "label": c.get("label") or key,
            "treatments": [(liquids.get(t) or {}).get("name", t)
                           for t in (c.get("treatments") or [])],
            "replicates": c.get("replicates") if isinstance(c.get("replicates"), int)
            else len(list(c.get("wells") or [])),
            "wells": list(c.get("wells") or []),
            "quote": c.get("quote") or "",
            "quote_page": c.get("quote_page"),
        }
    steps = []
    for s in plan.get("steps") or []:
        s = s or {}
        p = s.get("pipette") if isinstance(s.get("pipette"), dict) else None
        n = p.get("repeat") if p and isinstance(p.get("repeat"), int) else 1
        n = max(1, min(12, n))
        for k in range(n):
            text = s.get("text") or ""
            row = {"kind": s.get("kind") or "other",
                   "text": text if n == 1 else f"{text} ({k + 1} of {n})",
                   "minutes": s.get("minutes"), "temp_c": s.get("temp_c"),
                   "tip": None, "caution": s.get("caution"),
                   "quote": s.get("quote") or "", "quote_page": s.get("quote_page"),
                   "pipette": None, "wells": []}
            if p is not None:
                wells = resolve_target(plan, p.get("target") or {})
                row["pipette"] = {
                    "liquid": (liquids.get(p.get("liquid")) or {}).get("name", p.get("liquid")),
                    "volume": p.get("volume"), "unit": p.get("unit"), "wells": wells,
                    "target_label": target_label(plan, p.get("target") or {})}
                row["wells"] = wells
            steps.append(row)
    return {"conditions": conditions, "steps": steps,
            "backups": list(plan.get("backups") or [])}


def draft_summary(plan: dict) -> dict:
    """Compact model context: liquid ids, condition ranges, [N] steps."""
    liquids = ", ".join(sorted((plan.get("liquids") or {})))
    conditions = "; ".join(
        f"{k} {wells_label(v.get('wells') or [])} n={v.get('replicates')}"
        for k, v in sorted((plan.get("conditions") or {}).items()))
    steps = "; ".join(f"[{i}] {(s.get('text') or '')[:40]}"
                      for i, s in enumerate(plan.get("steps") or []))
    return {"liquids": liquids, "conditions": conditions, "steps": steps}
