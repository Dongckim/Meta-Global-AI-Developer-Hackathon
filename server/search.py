"""Web search: one separate Spark call with the built-in web_search tool, the
same path in every mode, then a short no-search call that tidies its answer.
Settings decide only whether a card asks first (pip_voice.json
`modes.<mode>.confirm_search`).

What the Model API gives (dev.meta.ai/docs/search-grounding, checked live
2026-10-29):
- tools [{"type": "web_search", "search_context_size": ...}] on /v1/responses;
  the model may search several times (search / open_page / find_in_page).
- Citations are `url_citation` annotations on the answer text (url, title,
  start/end index into the model's answer), with no quote of the page.
  `include: ["web_search_call.results"]` lists the pages the search returned
  (title, url; their snippets came back empty).
- Citations are rare: asked to cite every claim, 2 of 6 answers had any (0 of
  6 with search_context_size medium; json_schema or a fixed reply shape on the
  search call dropped them). Pages searched came back 6 of 6.

So the answer says what backs it, never more: `basis` "cited" (citations ->
"Source"), else "pages" (the pages searched -> "Pages searched"), else no
answer ("I couldn't find a solid source."). The tidy call (json_schema, no
tools) gets only the answer text and the source list (no page bodies);
the server keeps only urls from that list and every number the tidy lines
state must be in the answer text.
- $2.50 per 1,000 search queries plus tokens.
"""

from __future__ import annotations

import re
import time
import uuid
from urllib.parse import urlparse

from server.grammar import parse_wellref

SEARCH_TIMEOUT_S = 15.0
CITE = ("Search the web, then answer briefly. Every claim MUST carry a url_citation annotation "
        "to the page it came from; never answer from memory.")


def build_body(query: str, model: str) -> dict:
    """The search call: web_search only (context low), no output schema."""
    return {
        "model": model,
        "instructions": CITE,
        "input": query,
        "tools": [{"type": "web_search", "search_context_size": "low"}],
        "include": ["web_search_call.results"],
        "reasoning": {"effort": "minimal"},
        "max_output_tokens": 600,
        "store": False,
    }


SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answer_short", "detail", "sources"],
    "properties": {"answer_short": {"type": "string"}, "detail": {"type": "string"},
                   "sources": {"type": "array", "items": {"type": "string"}}}}


def build_summary_body(question: str, found: dict, model: str) -> dict:
    """The tidy call: no tools; the answer text and the source list only."""
    listed = "\n".join(f"- {x['url']} ({x['title']})" for x in found["sources"])
    return {
        "model": model,
        "instructions": "Rewrite a web answer for a lab screen. Use only facts in it; add nothing.",
        "input": f"Question: {question}\nAnswer:\n{found['text']}\n\nSources:\n{listed}\n\n"
                 "answer_short: the answer in at most 8 words. detail: at most 2 short sentences. "
                 "sources: the urls from the list that back it, copied exactly.",
        "reasoning": {"effort": "minimal"},
        "max_output_tokens": 1200,  # reasoning tokens count too; 300 left no answer
        "text": {"format": {"type": "json_schema", "name": "WebAnswer", "schema": SUMMARY_SCHEMA,
                            "strict": True}},
        "store": False,
    }


def _words(s: str, n: int) -> str:
    return " ".join((s or "").split()[:n]).rstrip(",;:")


def _sentences(s: str, n: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", (s or "").strip())
    return " ".join(p for p in parts[:n] if p)


def _clean(s: str) -> str:
    """Markdown and citation leftovers out of a line."""
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s or "")
    s = " ".join(re.sub(r"[*_`#>|]+", " ", s).split())
    return re.sub(r"\s+([,.;:!?])", r"\1", s)  # a removed citation leaves "hours ,"


def _source(url: str, title: str) -> dict:
    domain = (urlparse(url).hostname or "").removeprefix("www.")
    return {"title": (title or domain).strip(), "url": url, "domain": domain}


def found(payload: dict) -> dict | None:
    """The search call -> {text, sources, basis}: its citations ("cited"),
    else the pages it searched ("pages"); None when it has neither, or no answer."""
    text, cites, pages = "", [], []
    for item in (payload or {}).get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    cites += [a for a in part.get("annotations") or [] if a.get("type") == "url_citation"]
                    text = part.get("text", "")  # the last message is the answer
        elif item.get("type") == "web_search_call":
            pages += [r for r in item.get("results") or [] if r.get("url")]
    basis, picked = ("cited", cites) if cites else ("pages", pages)
    sources, seen = [], set()
    for x in picked:
        if x["url"] not in seen:
            seen.add(x["url"])
            sources.append(_source(x["url"], x.get("title") or ""))
    if not sources or not _clean(text):
        return None
    return {"text": _clean(text), "sources": sources[:5], "basis": basis}


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def tidy(summary: dict | None, got: dict) -> dict:
    """{answer_short, detail, sources, basis}. The tidy lines stand when every
    number they state is in the answer text, else lines cut from that text;
    sources are only urls the search gave (the tidy's picks, else the top 3)."""
    text = got["text"]
    s = summary or {}
    short, detail = _clean(s.get("answer_short", "")), _clean(s.get("detail", ""))
    if not short or not set(_NUM.findall(f"{short} {detail}")) <= set(_NUM.findall(text)):
        first = _sentences(text, 1)
        short, detail = first, _sentences(text[len(first):], 2) or first
    allowed = {x["url"]: x for x in got["sources"]}
    picks = [allowed[u] for u in dict.fromkeys(s.get("sources") or []) if u in allowed]
    return {"answer_short": _words(short, 8), "detail": _sentences(detail, 2),
            "sources": (picks or got["sources"])[:3], "basis": got["basis"]}


def output_json(payload: dict) -> dict | None:
    import json
    for item in (payload or {}).get("output", []):
        for part in item.get("content", []) if item.get("type") == "message" else []:
            if part.get("type") == "output_text":
                try:
                    return json.loads(part.get("text", ""))
                except ValueError:
                    return None
    return None


_RUN = re.compile(r"\b(?:run\s*#?\s*\d+|run-\d{8}-\d{6}-[0-9a-f]{4})\b", re.IGNORECASE)
_WELLISH = re.compile(r"\b[A-Ha-h]\d{1,2}(?:\s*(?:-|–|to|through)\s*[A-Ha-h]?\d{1,2})?\b")


def clean_query(query: str, private_texts: list[str]) -> str:
    """The words that leave the lab: well refs, run names and note text out.
    Nothing left -> ""."""
    q = " ".join((query or "").split())
    for t in sorted({" ".join((x or "").split()) for x in private_texts}, key=len, reverse=True):
        if len(t) >= 8:
            q = re.sub(re.escape(t), " ", q, flags=re.IGNORECASE)
    q = _RUN.sub(" ", q)
    q = _WELLISH.sub(lambda m: " " if parse_wellref(m[0].replace(" ", "").upper()) else m[0], q)
    q = re.sub(r"\s+([?.,!])", r"\1", " ".join(q.split()))
    while True:  # words left hanging where a removed part was ("HeLa in of?")
        q2 = re.sub(r"\s*\b(?:in|on|for|of|at|to|from|and|wells?)\s*([?.!,]|$)", r"\1", q).strip(" ,")
        if q2 == q:
            break
        q = q2
    return q if len(re.findall(r"[A-Za-z]{2,}", q)) >= 2 else ""


def record(question: str, query: str, result: dict | None, status: str, run_id: str | None,
           ms: int) -> dict:
    """One sent search, kept whether or not it answered (the count of what
    left the iPad)."""
    r = result or {}
    return {"id": uuid.uuid4().hex[:8], "ts": time.time(), "question": question, "query": query,
            "answer_short": r.get("answer_short", ""), "detail": r.get("detail", ""),
            "sources": r.get("sources", []), "basis": r.get("basis", ""), "run_id": run_id,
            "status": status, "ms": ms}
