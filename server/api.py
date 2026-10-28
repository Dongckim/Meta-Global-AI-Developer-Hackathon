"""Pip orchestration server: text/voice -> validated calls -> record store.

Local dev only (no Modal/Private modes). Run from repo root:

    uvicorn server.api:app --reload --port 8000

Needs MODEL_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
from typing import Any
import time
import uuid
import wave
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

from server.grammar import (
    build_evidence,
    call_wells,
    collapse_each_timers,
    evidence_summary,
    expand_tool_wells,
    fill_on_done,
    final_pip_line,
    find_well_mentions,
    grounded,
    honest_reply,
    is_failure_message,
    mark_each_timers,
    match_card_answer,
    match_grammar,
    needs_minutes_ask,
    normalize_text,
    parse_wellref,
    relevant_plan_numbers,
    speak_text,
    split_ask,
    timer_set_reply,
    wells_label,
)
from server.memory import Memory, SparkChainMemory, SummaryMemory
from server.screens import IDS as SCREEN_IDS
from server.screens import canonical as canonical_screen
from server import protocol as proto
from server.store import RecordStore
from server.tone import keys as tone_keys
from server.tone import say

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

load_dotenv()

BASE_URL = "https://api.meta.ai/v1"
SPARK_MODEL = "muse-spark-1.3"
REASONING_EFFORT = "minimal"  # shortest pass; Spark rejects "none" (HTTP 400)
MAX_OUTPUT_TOKENS = 1024
ASR_MODEL = "muse-voice-transcribe-1.0"
WELL_KEYWORDS = [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)]
KEYWORDS = WELL_KEYWORDS + ["to", "through", "done", "exclude", "redo", "note", "timer", "next", "drug A", "drug B", "pipette", "thirty", "fifteen", "minutes", "wash", "each", "every", "all"]
LANGUAGE_BIAS = ["English"]
TIMEOUT = httpx.Timeout(120.0)
WEB_SEARCH_ENABLED = os.environ.get("WEB_SEARCH_ENABLED", "false").lower() == "true"
#: Stable per-prefix cache keys (docs: never per-user/per-run) + 24h retention hint.
CACHE_KEY = "pip-bench-spark"
CACHE_KEY_SEARCH = "pip-bench-spark-search"
CACHE_RETENTION = "24h"
#: False = stateless summary mode (default); True keeps previous_response_id chaining.
CHAIN_ENABLED = os.environ.get("CHAIN_ENABLED", "false").lower() == "true"
PROTOCOL_FULL_MODE = os.environ.get("PROTOCOL_FULL_MODE", "true").lower() == "true"
PROTOCOL_MAX_BYTES = 10_000_000
TURN_HISTORY = 6

SYSTEM = (
    "You're Pip, a friendly lab buddy at the bench. "
    "Short, warm, casual (Got it / Nice / Heads up / Hmm). "
    "Never claim anything the record doesn't show. "
    "Record only reported facts (done, finished, added, saw, looks). "
    "Bench instructions (wash, add, do, start the next) are not records: "
    "do the Pip-side part (timer, guidance) or ask. "
    "If no tool fits (thanks, greeting), answer in one short sentence. "
    "Text beside tool calls is ignored. "
    "A run-plan draft may be open (draft_open): change it only with edit_draft "
    "using exact paths from the draft, never with record tools. "
    "Wells are WellRefs (\"C2-C8\"). \"Those\" and \"it\" refer to recent wells. "
    "One set_timer per distinct duration; the server fans each out per well. "
    "Ground every value in this message, the plan, or read results. "
    "For questions about the record, call read_record once, then answer(text) briefly for speech. "
    "If the list is long, navigate to show it all and summarize aloud. "
    "After a write, the screen updates itself; never call open to confirm. "
    "Ask the user with ask(question) only when you must. "
    "asked: is your last question; this message may answer it. "
    "Emit direct calls in run order; if a later step needs an earlier result, do only the first step now. "
    "For another or backup well, call find_free_wells first, then act on the first candidate at once (redo: exclude_well + record_redo); the server confirms via cards. "
    "Robot protocol: make_robot."
)
if WEB_SEARCH_ENABLED:
    SYSTEM += " For factual questions use web_search, then ask(question) offering to attach."


def _opt(spec: dict) -> dict:
    """Optional strict field: required but nullable (Meta accepts type arrays)."""
    return {**spec, "type": [spec.get("type", "string"), "null"]}


def _tool(name: str, desc: str, props: dict) -> dict:
    """Strict function tool: every prop required, no extras.

    Per dev.meta.ai strict only validates OUR schema; model output is NOT
    constrained, so server-side validation stays as the real gate.
    """
    return {
        "type": "function",
        "name": name,
        "description": desc,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": sorted(props),
            "additionalProperties": False,
        },
    }


WELLREF = {"type": "string", "description": 'WellRef: "C5" | "C2-C8" | "C2,C5,C7"'}

TOOLS = [
    _tool("mark_done",
          "Use when the user REPORTS wells finished (done, finished, added, I put, washed). "
          "Don't use for instructions about bench work to do ('wash the remaining', 'add drug A') "
          "— record nothing; guide or ask instead. "
          "Example: 'C2 to C8 done'.",
          {"wells": WELLREF}),
    _tool(
        "exclude_well",
        "Use when the user drops a well from the run. "
        "Don't use for redo (use record_redo), done (use mark_done), "
        "or bench instructions ('wash C6'). "
        "Example: 'C3 looks cloudy, drop it'.",
        {
            "well": WELLREF,
            "reason": _opt({"type": "string", "description": "Why, if stated"}),
        },
    ),
    _tool(
        "record_redo",
        "Use when the user reports damage needing a rerun. "
        "Don't use without excluding the damaged well when redoing elsewhere. "
        "Don't use for run-ahead instructions ('run the next ones'). "
        "Example: 'C3 has a bubble, redo it in a backup well'.",
        {"wells": WELLREF, "note": _opt({"type": "string", "description": "What went wrong"})},
    ),
    _tool(
        "add_note",
        "Use when the user states an observation to record on a well or step. "
        "Don't use when there is nothing to write ('I want to make notes on C8') — "
        "ask what to write instead. "
        "Example: 'Note C8, sticky edges'.",
        {
            "target": {"type": "string", "description": 'WellRef, or a step/sample name. "C8"'},
            "text": {"type": "string", "description": "Clean one-line memo of what was said"},
        },
    ),
    _tool(
        "edit_note",
        "Use when the user corrects a note. "
        "Don't use for new notes (use add_note). "
        "Example: 'change C5's memo, no longer sticky'.",
        {
            "well": _opt({"type": "string", "description": 'WellRef, if stated. "C5"'}),
            "text": {"type": "string", "description": "Replacement memo text"},
        },
    ),
    _tool(
        "save_tip",
        "Use when the user states a bench tip for a step, or confirms attaching a searched answer. "
        "Don't use for well observations (use add_note). "
        "Example: 'attach it to step 3'.",
        {"step_id": {"type": "string"}, "text": {"type": "string"}},
    ),
    _tool(
        "set_timer",
        "Use when the user starts a countdown with stated minutes. "
        "Don't use without minutes — ask instead. "
        "Don't use to cancel or extend (use manage_timers). "
        "Example: 'two minutes for C7'.",
        {
            "label": {"type": "string"},
            "minutes": {"type": "number", "description": "Minutes from now"},
            "on_done": _opt({"type": "string", "description": "Next action, if stated"}),
            "wells": _opt(WELLREF),
        },
    ),
    _tool(
        "manage_timers",
        "Use to list, cancel, or extend timers. Extend adds 5 min unless minutes given. "
        "Don't use to start one (use set_timer). "
        "Example: 'cancel the wash timer'.",
        {
            "action": {"type": "string", "description": "list, cancel, or extend"},
            "label": _opt({"type": "string", "description": "Timer label"}),
            "minutes": _opt({"type": "number", "description": "Minutes to add"}),
        },
    ),
    _tool("next_step",
          "Use when the user moves to the next step. "
          "Don't use to ask what the next step is (read steps instead). "
          "Don't use for the next physical action ('do the next wash'). "
          "Example: 'move to step 4'.", {}),
    _tool(
        "show",
        "Use when the user wants something highlighted on screen. "
        "Don't use after a write — the screen updates itself. "
        "Example: 'show column 8'.",
        {
            "view": {"type": "string", "description": "plate, timer, protocol"},
            "target": _opt({"type": "string", "description": 'WellRef/step, if any. "C8"'}),
        },
    ),
    _tool(
        "ask",
        "Use when you must ask the user something to proceed. "
        "Don't use when you can act or answer already. "
        "Example: 'Which well?'.",
        {"question": {"type": "string"}},
    ),
    _tool("make_robot",
          "Use when the user wants the robot protocol built. "
          "Don't use for anything else. "
          "Example: 'make the robot protocol'.", {}),
    _tool(
        "read_record",
        "Use when you need record facts to answer or decide. "
        "Don't use when the state you have already answers it. "
        "Example: 'what is on C6'.",
        {
            "kind": {"type": "string", "enum": ["wells", "notes", "timers", "steps", "history"]},
            "filter": _opt({"type": "string",
                            "description": "WellRef, condition, step, or label; blank for natural scope"}),
            "limit": _opt({"type": "integer", "description": "Max entries (default 10)"}),
        },
    ),
    _tool(
        "find_free_wells",
        "Use when the user wants another or backup well. "
        "After the lookup, act on the first candidate at once (redo: exclude_well + record_redo); "
        "the server confirms via cards, so don't ask which candidate. Ask only if none. "
        "A range near searches past its end. Don't guess free wells yourself. "
        "Example: 'redo it in a backup well'.",
        {
            "near": {"type": "string", "description": 'Reference WellRef. "C3"'},
            "count": _opt({"type": "integer", "description": "How many (default 3)"}),
        },
    ),
    _tool(
        "answer",
        "Use to answer the user's question briefly after reading. "
        "Don't use beside a write — the write wins. "
        "Don't use to narrate tool calls. "
        "Example: 'Yes, 6 of 5 left.'.",
        {"text": {"type": "string"}},
    ),
    _tool(
        "navigate",
        "Use when the user asks to go somewhere (go to). "
        "Don't use to confirm a write — the screen updates itself. "
        "Example: 'go to the robot preview'.",
        {"screen": {"type": "string", "enum": SCREEN_IDS,
                    "description": "Screen id from the registry"}},
    ),
    _tool(
        "open",
        "Use when the user asks to view something (open, show, let me see). "
        "Don't use after a write — the screen shows it. "
        "Example: 'open C8'.",
        {"target": {"type": "string", "description": "WellRef or screen name"}},
    ),
    _tool("back",
          "Use when the user says back. "
          "Don't use after other actions in the same turn. "
          "Example: 'back'.", {}),
    _tool(
        "edit_draft",
        "Use to change the open run-plan draft, only when draft_open is true. "
        "Path is conditions.<id>.replicates (.wells), liquids.<id>, or steps.N.minutes "
        "(.temp_c/.caution/.text/.pipette.volume/.pipette.target) with exact ids and the "
        "N from the draft's [N] step labels; value is text (digits for counts/minutes, "
        "a WellRef or 'row X' for wells, wells:<WellRef>|condition:<id>|all|other for targets). "
        "Don't use when no draft is open. Don't use for the live record. "
        "Example: 'change drug A to row E'.",
        {
            "path": {"type": "string", "description": "Draft path to set"},
            "value": {"type": "string", "description": "New value as text"},
        },
    ),
]
if WEB_SEARCH_ENABLED:
    TOOLS.append({"type": "web_search", "search_context_size": "low"})

def prefix_hash() -> str:
    """Hash of the cache-stable request prefix (instructions head + tool defs)."""
    return hashlib.sha1(
        (SYSTEM + json.dumps(TOOLS, sort_keys=True)).encode()
    ).hexdigest()[:8]


class _InvalidChain(Exception):
    pass


store = RecordStore()
_SESSIONS: dict[str, dict] = {}


def get_session(sid: str | None) -> dict:
    sid = sid or "bench"
    s = _SESSIONS.get(sid)
    if s is None:
        s = {"id": sid, "chained": CHAIN_ENABLED,
             "memory": SparkChainMemory() if CHAIN_ENABLED else SummaryMemory(),
             "last_web": None, "turns": []}
        _SESSIONS[sid] = s
    return s


def log_turn(session: dict, text: str, calls, messages, asked=None) -> None:
    """One line per turn: user words / tools and results / Pip's question. Capped."""
    bits = []
    for c in calls or []:
        w = ",".join(call_wells(c))
        bits.append(f"{c.get('name', '?')}[{w}]" if w else c.get("name", "?"))
    turns = session.setdefault("turns", [])
    turns.append({"text": (text or "")[:120], "calls": bits,
                  "messages": [m[:100] for m in (messages or [])[:4]],
                  "asked": asked or {}})
    del turns[:-TURN_HISTORY]


_COMMIT: str | None = None


def git_commit() -> str:
    """Running code's commit hash (cached). 'unknown' when git is unavailable."""
    global _COMMIT
    if _COMMIT is None:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True, text=True, timeout=5)
            _COMMIT = r.stdout.strip() or "unknown"
        except Exception:
            _COMMIT = "unknown"
    return _COMMIT


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"pip-server commit={git_commit()} tools={len(TOOLS)} "
          f"ph={prefix_hash()} memory={'chain' if CHAIN_ENABLED else 'summary'}", flush=True)

    async def watch():
        while True:
            try:
                store.check_timers(time.time())
            except Exception as e:
                print(f"watcher: {e}", flush=True)
            await asyncio.sleep(10)

    task = asyncio.create_task(watch())
    yield
    task.cancel()


app = FastAPI(title="Pip server", lifespan=lifespan)


class OrchestrateIn(BaseModel):
    text: str = Field(min_length=1)
    state: dict = {}
    session: str = "bench"
    screen: str = "main"
    selected_well: str = ""
    open_sheet: str = ""


class ConfirmIn(BaseModel):
    card_id: str = Field(min_length=1)
    ok: bool
    session: str = "bench"


class InputIn(BaseModel):
    text: str = ""
    card_id: str = ""
    answer: str = ""
    session: str = "bench"
    screen: str = "main"
    selected_well: str = ""
    open_sheet: str = ""


class EditNoteIn(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class DraftPatchIn(BaseModel):
    path: str = ""
    value: Any = None
    session: str = "bench"


class DraftStartIn(BaseModel):
    session: str = "bench"


def _key() -> str:
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise HTTPException(500, "MODEL_API_KEY is not set (check .env)")
    return key


def _check(name: str, resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        print(f"{name} upstream {resp.status_code}: {resp.text[:2000]}", flush=True)
        raise HTTPException(502, f"{name} upstream {resp.status_code}: {resp.text[:200]}")


async def _post(client: httpx.AsyncClient, name: str, *a, **k) -> httpx.Response:
    try:
        return await client.post(*a, **k)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"{name} unreachable: {e}")


async def _call_spark(
    client: httpx.AsyncClient, key: str, text: str, state: dict, session: dict,
    model: str = SPARK_MODEL, effort: str = REASONING_EFFORT, lookup: bool = False,
    echo_calls: list | None = None,
) -> tuple[list, str, int, bool, dict, list]:
    """Returns (calls, text, ms, truncated, usage, web_searches).

    Chained turns carry function_call_outputs for the previous turn's calls.
    Lookup rounds replay round 1 inline (question, calls, outputs) so the
    second round answers from bound tool results instead of guessing.
    Any 4xx on a chained call drops the chain and retries once fresh with
    a recent-actions summary; a second failure answers sorry instead of a 502.
    """
    mem: Memory = session["memory"]
    if isinstance(mem, SparkChainMemory) and mem.needs_reanchor:
        mem.reset(store.session_summary())
        session["pending_outputs"] = []
    for attempt in (0, 1):
        state_json = json.dumps(state, ensure_ascii=False)
        seed = getattr(mem, "seed", "")
        if seed:
            mem.seed = ""
        chained = "previous_response_id" in mem.params()
        pend = session.get("pending_outputs") or []
        if pend and chained:
            req_input: object = [
                {"type": "function_call_output", "call_id": p["call_id"], "output": p["output"]}
                for p in pend
            ] + [{"role": "user", "content": text}]
        elif pend and lookup:
            # Summary mode stores nothing, so round 1 is replayed inline:
            # question, calls, bound outputs, continue nudge. A bare
            # repeated question restarts the lookup; text-only results
            # don't bind. Outputs without a call are dropped (unbound
            # outputs 400).
            outs = {p["call_id"]: p["output"] for p in pend if p.get("call_id")}
            req_input = [{"role": "user", "content": text}]
            for c in echo_calls or []:
                cid = c.get("call_id")
                if not cid or cid not in outs:
                    continue
                req_input.append({
                    "type": "function_call", "name": c.get("name"),
                    "arguments": json.dumps(c.get("args") or {}), "call_id": cid})
                req_input.append({"type": "function_call_output", "call_id": cid,
                                  "output": outs.pop(cid)})
            for cid, output in outs.items():
                print(f"spark lookup: dropping unbound output {cid}", flush=True)
            req_input.append({"role": "user", "content": CONTINUE_TEXT})
        else:
            req_input = text
        body = {
            "model": model,
            "instructions": SYSTEM + (f"\n\nConversation so far: {seed}" if seed else "")
            + "\n\n현재 상태(JSON):\n" + state_json,
            "input": req_input,
            "reasoning": {"effort": effort},
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "tools": TOOLS,
            "prompt_cache_key": CACHE_KEY_SEARCH if WEB_SEARCH_ENABLED else CACHE_KEY,
            "prompt_cache_retention": CACHE_RETENTION,
            **mem.params(),
        }
        t0 = time.perf_counter()
        resp = await _post(
            client,
            "spark",
            f"{BASE_URL}/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
        )
        ms = int((time.perf_counter() - t0) * 1000)
        if chained and 400 <= resp.status_code < 500 and attempt == 0:
            print(f"spark chain {resp.status_code}: {resp.text[:500]}; restarting chain", flush=True)
            mem.reset(store.session_summary())
            session["pending_outputs"] = []
            continue
        if attempt == 1 and resp.status_code >= 400:
            print(f"spark retry {resp.status_code}: {resp.text[:500]}", flush=True)
            return [], "Sorry, say that again?", ms, True, {}, []
        _check("spark", resp)
        payload = resp.json()
        mem.note_response(payload.get("id"))
        usage = payload.get("usage") or {}
        in_tok = usage.get("input_tokens", usage.get("prompt_tokens", "?"))
        out_tok = usage.get("output_tokens", usage.get("completion_tokens", "?"))
        det_in = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        cached = det_in.get("cached_tokens", usage.get("cached_tokens", "?"))
        det = usage.get("output_tokens_details") or {}
        reason_tok = det.get("reasoning_tokens", usage.get("reasoning_tokens", "?"))
        print(f"spark ms={ms} retry={attempt} chain={mem.turns} "
              f"in_tok={in_tok} cached={cached} out_tok={out_tok} reason_tok={reason_tok} "
              f"ph={prefix_hash()}", flush=True)
        if payload.get("status") == "incomplete":
            reason = (payload.get("incomplete_details") or {}).get("reason", "?")
            print(f"spark incomplete: reason={reason} attempt={attempt}", flush=True)
            if attempt == 0:
                continue  # one retry, same effort and tools
            return [], "Sorry, say that again?", ms, True, usage, []
        calls, texts, searches = [], [], []
        for item in payload.get("output", []):
            if item.get("type") == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"name": item.get("name"), "args": args, "call_id": item.get("call_id")})
            elif item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part.get("text", ""))
            elif item.get("type") == "web_search_call":
                searches.append(item)
        return calls, "\n".join(texts), ms, False, usage, searches
    raise HTTPException(502, "spark chain would not restart")


def _scan_sources(node, out: list):
    if isinstance(node, dict):
        if isinstance(node.get("url"), str) and isinstance(node.get("title") or node.get("snippet") or node.get("text"), str):
            out.append({
                "title": node.get("title") or "",
                "url": node["url"],
                "snippet": node.get("snippet") or node.get("text") or "",
            })
        for v in node.values():
            _scan_sources(v, out)
    elif isinstance(node, list):
        for v in node:
            _scan_sources(v, out)


def web_answer(searches: list) -> tuple[str, list]:
    """One sentence from the top result plus its source. No model text involved."""
    sources: list = []
    _scan_sources(searches, sources)
    if not sources:
        return "I found nothing on the web.", []
    first = (sources[0]["snippet"] or sources[0]["title"]).strip().split(". ")[0].strip()
    if first and not first.endswith("."):
        first += "."
    src = sources[0]["title"] or sources[0]["url"]
    return f"{first} (Source: {src})", [{"title": s["title"], "url": s["url"]} for s in sources[:3]]


def recent_target() -> str | None:
    wells: list[str] = []
    for a in reversed(store.r["recent"]):
        for w in a.get("wells") or []:
            if w not in wells:
                wells.append(w)
        if wells:
            break
    return wells_label(wells) if wells else None


#: Lookup-round nudge, mirroring the chained shape inline.
CONTINUE_TEXT = "Continue with the looked-up data."


def _short_text(t: str) -> str:
    """Model's conversational line: collapsed, first two sentences, capped."""
    t = " ".join((t or "").split())
    t = " ".join(re.split(r"(?<=[.!?])\s+", t, maxsplit=2)[:2])
    return t if len(t) <= 280 else t[:277].rstrip() + "…"


def _which_one(pool: list) -> str:
    """Disambiguation over pending cards, newest first, no resolution."""
    qs = []
    for c in pool[:3]:
        q = (c.get("question") or "that").strip().rstrip("?")
        qs.append(q[:1].lower() + q[1:] if q else "that")
    if len(qs) == 2:
        return f"Which one — {qs[0]}, or {qs[1]}?"
    return "Which one — " + ", ".join(qs[:-1]) + f", or {qs[-1]}?"


def _answer_cards(ans: str, session: dict):
    """Resolve open cards without a model call. Returns the run_utterance tuple.

    A bare yes/no/cancel answers what Pip asked last (still pending); when
    that is unclear the turn asks which one instead of guessing.
    """
    cards = sorted(store.r["cards"].values(), key=lambda c: c["ts"], reverse=True)
    if ans == "all":
        targets = [(c["id"], True) for c in cards]
    else:  # yes / no / cancel
        last = session.get("last_asked_cards") or []
        pool = [c for c in cards if c["id"] in last] or cards
        if len(pool) > 1:
            reply = _which_one(pool)
            return [], [reply], reply, 0, "card", [], {"dropped": 0, "drop_reasons": []}, []
        targets = [(pool[0]["id"], ans == "yes")]
    messages, new_cards = [], []
    for cid, ok in targets:
        message, fresh, card = store.confirm(cid, ok, "card", session)
        messages.append(message)
        new_cards += fresh
        call_id = (card.get("payload") or {}).get("_call_id") if card else None
        if call_id:
            _refresh_output(session, call_id, ok, message)
    reply = " ".join(messages) or "Done."
    return [], messages, reply, 0, "card", new_cards, {"dropped": 0, "drop_reasons": []}, []


async def run_utterance(client: httpx.AsyncClient, key: str, text: str, session: dict, source: str):
    """Cards first (no model call), then grammar, else Spark with one lookup round.
    The model hears the raw transcript; normalization lives inside grammar only.
    Returns (calls, messages, reply, ms, route, cards, info, sources)."""
    if store.r["cards"]:
        ans = match_card_answer(text)
        if ans is not None:
            return _answer_cards(ans, session)
    g = match_grammar(text, store.prompt_state(session))
    if g is not None:
        calls, gtext = g
        if not calls:
            reply = say(gtext, session) if gtext in tone_keys() else gtext
            return [], [], reply, 0, "grammar", [], {"dropped": 0, "drop_reasons": []}, []
        calls = expand_tool_wells(calls)
        res = store.apply_calls(calls, source, text, "grammar", session)
        reply = _compose_reply(calls, res, store.prompt_state(session), session)
        if res["info"].get("edited"):
            reply = f"Note updated ({res['info']['edited']['target']})."
        return calls, res["messages"], reply, 0, "grammar", res["cards"], res["info"], []
    summary = evidence_summary(session.get("turns"))
    calls, texts, ms, cut, _, searches = await _call_spark(
        client, key, text, store.model_state(session), session)
    if cut:
        return [], [], say("didnt_catch", session), ms, "spark", [], {"dropped": 0, "drop_reasons": []}, []
    sentence, sources = web_answer(searches) if searches else ("", [])
    if sentence:
        session["last_web"] = sentence
    ordered_ids = [c.get("call_id") for c in calls]
    r1raw = [dict(c) for c in calls]
    plan_allowed = relevant_plan_numbers(
        text, [(s.get("text") or s.get("name", ""), s.get("minutes"))
               for s in store.r["plan"]["steps"]])
    has_reads = any(c.get("name") in ("read_record", "find_free_wells") and c.get("call_id") for c in calls)
    pre_all_reads = bool(calls) and all(c.get("name") in ("read_record", "find_free_wells") for c in calls)
    calls, question, outcomes, gdrop = _validate(calls, text, 1, plan_allowed, summary=summary,
                                                 blind=has_reads)
    if not calls and not question:
        _store_outputs(session, ordered_ids, outcomes)
        if needs_minutes_ask(calls, text) or gdrop:
            q = say("ask_minutes", session)
            asked = {"question": q, "target": recent_target() or ""}
            info = {"dropped": 0, "drop_reasons": [], "asked": asked}
            return [], [], q, ms, "spark", [], info, sources
        if sentence:
            return [], [], sentence, ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
        if texts.strip():
            t = _short_text(texts)
            if honest_reply([], t) != t:
                return _didnt_act(text, ms, session, sources)
            return [], [], t, ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
        dropped = _dropped_replies(r1raw, outcomes)
        if dropped:
            return [], [], dropped, ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
        print(f"spark empty: no calls, no text for {text[:60]!r}", flush=True)
        return _didnt_act(text, ms, session, sources)
    res = store.apply_calls(calls, source, text, "spark", session)
    _apply_outcomes(calls, res, outcomes)
    read_lines = _render_r1_reads(calls, outcomes)
    _store_outputs(session, ordered_ids, outcomes)
    fire_lookup = (
        has_reads and question is None
        and any(c.get("name") in ("read_record", "find_free_wells") and c.get("call_id") for c in calls)
    )
    if fire_lookup:
        reads_text = " ".join(
            outcomes[cid] for c in calls
            if c.get("name") in ("read_record", "find_free_wells")
            and (cid := c.get("call_id")) and cid in outcomes
        )
        ms2, more = await _lookup_round(client, key, text, session, source, plan_allowed, reads_text,
                                        summary, r1raw)
        ms += ms2
        if more is not None:
            rc, res2, question2, ordered2, outcomes2, gdrop2, t2 = more
            calls = calls + rc
            res["messages"] += res2["messages"]
            res["cards"] += res2["cards"]
            res["outcomes"] += res2["outcomes"]
            res["ui"] += res2["ui"]
            res["info"]["ui"] = res["ui"]
            res["info"]["dropped"] += res2["info"]["dropped"]
            res["info"]["drop_reasons"] += res2["info"]["drop_reasons"]
            for k in ("saved_memo", "edited", "robot"):
                if res2["info"].get(k):
                    res["info"][k] = res2["info"][k]
            if question2 is not None:
                question = question2
            if res2["info"].get("timer_started"):
                gdrop = gdrop2
            else:
                gdrop = gdrop or gdrop2
            _store_outputs(session, ordered2, outcomes2)
            if question is None and pre_all_reads and not rc and not res2["messages"] and not res2["cards"]:
                if t2.strip():
                    res["messages"].append(_short_text(t2))
                else:
                    for c in calls:
                        if c.get("name") == "read_record" and (c.get("args") or {}).get("kind") == "wells":
                            filt = (c.get("args") or {}).get("filter") or ""
                            for w in store.filter_wells(filt if isinstance(filt, str) else "")[:4]:
                                res["messages"].append(store.describe_well(w))
    calls = _strip_ids(calls)
    asked: dict = {}
    if question is not None:
        reply = question
        is_attach = bool(searches) and "attach" in question.lower() and session.get("last_web")
        asked = {"question": question, "target": "" if is_attach else (recent_target() or "")}
    elif gdrop:
        base = _compose_reply(calls, res, store.prompt_state(session), session)
        q = say("ask_minutes", session)
        reply = q if not base else base + " " + q
        asked = {"question": q, "target": recent_target() or ""}
    else:
        reply = _compose_reply(calls, res, store.prompt_state(session), session)
        if sentence and sources and not reply:
            reply = sentence
        if not reply:
            reply = _executed_reply(calls, read_lines, session)
        if not reply:
            print(f"ORPHAN REPLY: {sorted({c.get('name') for c in calls})} produced nothing",
                  flush=True)
            reply = say("found_empty", session)
    for m in res["messages"]:
        if m.startswith("Note updated.") and res["info"].get("edited"):
            reply = f"Note updated ({res['info']['edited']['target']})."
    if asked:
        res["info"]["asked"] = asked
    return calls, res["messages"], reply, ms, "spark", res["cards"], res["info"], sources


async def _lookup_round(client, key, text, session, source, plan_numbers, reads_text, summary,
                      r1raw):
    """Second spark round replaying round 1 inline (question, calls, outputs).

    A bare repeated question restarts the lookup; outputs without their
    calls don't bind. The replay mirrors the chained shape without storing.
    Returns (ms, None-or-tuple).
    """
    rc, t2, ms, cut, _, _ = await _call_spark(
        client, key, text, store.model_state(session), session, lookup=True, echo_calls=r1raw)
    if cut:
        return ms, None
    ordered2 = [c.get("call_id") for c in rc]
    rc, question2, outcomes2, gdrop2 = _validate(rc, text, 2, plan_numbers, reads_text, summary)
    res2: dict = {"messages": [], "cards": [], "outcomes": [], "ui": [],
                  "info": {"dropped": 0, "drop_reasons": []}}
    if rc:
        res2 = store.apply_calls(rc, source, text, "spark", session, reads_text)
        _apply_outcomes(rc, res2, outcomes2)
    return ms, (rc, res2, question2, ordered2, outcomes2, gdrop2, t2)


def _tag_timer_sources(tool_calls, evidence):
    """Direct (utterance/read) passes; conversation/plan rides tagged for a
    confirm card; ungrounded drops. An ungrounded value is never approved."""
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
        ok, src = grounded(m, evidence)
        if not ok:
            continue
        if src in ("conversation", "plan"):
            args = dict(c.get("args") or {})
            args["_source"] = src
            c = {**c, "args": args}
        out.append(c)
    return out


def _validate(calls, text, depth, plan_numbers=frozenset(), reads_text="",
              summary="", blind: bool = False):
    """expand -> ask/answer partition -> evidence-graded timer filter.
    Returns (calls, question, outcomes, grounded_drop).
    blind: a lookup round follows, so round-1 answers (written without the
    looked-up data) are dropped; the lookup round answers instead."""
    outcomes: dict[str, str] = {}
    calls = fill_on_done(calls, text)
    before, calls = calls, expand_tool_wells(calls)
    _note_lost(outcomes, before, calls, "dropped: no valid wells")
    question, kept = split_ask(calls)
    for c in calls:
        if c.get("name") == "ask" and c.get("call_id"):
            outcomes[c["call_id"]] = "asked user"
    calls = kept
    calls, dropped_answers = _partition_answer(calls, question is not None, blind)
    for c in dropped_answers:
        if c.get("call_id"):
            pending = blind and question is None
            reason = "dropped: lookup pending" if pending else "dropped: action wins"
            outcomes[c["call_id"]] = _result(False, reason)
            _log_drop(c.get("name"), c.get("args"), reason)
    if depth >= 2:
        deep = [c for c in calls if c.get("name") in ("read_record", "find_free_wells")]
        calls = [c for c in calls if c.get("name") not in ("read_record", "find_free_wells")]
        for c in deep:
            if c.get("call_id"):
                outcomes[c["call_id"]] = _result(False, "lookup depth exceeded")
                _log_drop(c.get("name"), c.get("args"), "lookup depth exceeded")
    evidence = build_evidence(text, summary, plan_numbers, reads_text)
    before, calls = calls, _tag_timer_sources(calls, evidence)
    grounded_drop = False
    after_ids = {c.get("call_id") for c in calls}
    for c in before:
        cid = c.get("call_id")
        if cid and c.get("name") == "set_timer" and cid not in after_ids and cid not in outcomes:
            grounded_drop = True
            outcomes[cid] = _result(False, "dropped: ungrounded minutes")
            _log_drop(c.get("name"), c.get("args"), "dropped: ungrounded minutes")
    if blind and question is None:
        # Reads are in flight: a weakly-evidenced round-1 timer is re-decided
        # in round 2 with read evidence instead of carding twice.
        kept = []
        for c in calls:
            if c.get("name") == "set_timer" and (c.get("args") or {}).get("_source") in ("conversation", "plan"):
                if c.get("call_id"):
                    outcomes[c["call_id"]] = _result(False, "dropped: lookup pending")
                    _log_drop(c.get("name"), c.get("args"), "dropped: lookup pending")
            else:
                kept.append(c)
        calls = kept
    before, calls = calls, collapse_each_timers(mark_each_timers(calls, text), text)
    _note_lost(outcomes, before, calls, "dropped: duplicate timer")
    return calls, question, outcomes, grounded_drop


def _partition_answer(calls, has_question, blind=False):
    answers = [c for c in calls if c.get("name") == "answer"]
    if not answers:
        return calls, []
    rest = [c for c in calls if c.get("name") != "answer"]
    non_competing = ("read_record", "find_free_wells", "navigate", "open", "back", "show")
    if blind or has_question or any(c.get("name") not in non_competing for c in rest):
        return rest, answers
    return calls, []


def _apply_outcomes(calls, res, outcomes):
    for i, c in enumerate(calls):
        cid = c.get("call_id")
        if not cid or cid in outcomes:
            continue
        if c.get("name") == "read_record":
            a = c.get("args") or {}
            if a.get("kind") in store.READ_KINDS:
                outcomes[cid] = json.dumps(
                    store.read_record(a["kind"], a.get("filter", ""), a.get("limit", 10)),
                    ensure_ascii=False)
                continue
        if c.get("name") == "find_free_wells":
            a = c.get("args") or {}
            near = a.get("near")
            if isinstance(near, str) and parse_wellref(near):
                outcomes[cid] = json.dumps(
                    {"near": near.strip().upper(),
                     "free": store.find_free_wells(near, a.get("count", 3))},
                    ensure_ascii=False)
                continue
        oc = res["outcomes"][i]
        if oc["cards"]:
            outcomes[cid] = _result(False, "pending confirmation")
        elif oc["messages"]:
            ok = not any(is_failure_message(m) for m in oc["messages"])
            outcomes[cid] = _result(ok, "; ".join(oc["messages"]))
        else:
            outcomes[cid] = _result(False, "dropped")


def _result(ok: bool, message: str) -> str:
    return json.dumps({"ok": ok, "message": message}, ensure_ascii=False)


def _log_drop(name, args, reason: str):
    blob = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)[:300]
    print(f"drop {name} args={blob} reason={reason}", flush=True)


def _note_lost(outcomes: dict, before: list, after: list, reason: str):
    output = _result(False, reason)
    after_ids = {c.get("call_id") for c in after}
    for c in before:
        cid = c.get("call_id")
        if cid and cid not in after_ids and cid not in outcomes:
            outcomes[cid] = output
            _log_drop(c.get("name"), c.get("args"), reason)


def _store_outputs(session: dict, ordered_ids: list, outcomes: dict):
    session["pending_outputs"] = [
        {"call_id": cid, "output": outcomes[cid]} for cid in ordered_ids if cid in outcomes
    ]


def _strip_ids(calls: list) -> list:
    return [{"name": c.get("name"),
             "args": {k: v for k, v in (c.get("args") or {}).items() if not k.startswith("_")}}
            for c in calls]


def _refresh_output(session: dict, cid: str, ok: bool, message: str):
    for p in session.get("pending_outputs") or []:
        if p["call_id"] == cid:
            p["output"] = _result(ok, message if ok else "declined by user")


def _didnt_act(text: str, ms: int, session: dict, sources: list):
    """Claimed action without tools (or nothing at all): honest + answerable."""
    reply = say("didnt_act", session)
    wells = sorted(find_well_mentions(text or ""))
    asked = {"question": reply, "target": wells_label(wells) if wells else ""}
    info = {"dropped": 0, "drop_reasons": [], "asked": asked}
    return [], [], reply, ms, "spark", [], info, sources


def _dropped_replies(r1raw: list, outcomes: dict) -> str:
    """Honest couldn't-lines for validation drops (branch 1 never applied)."""
    names = {c.get("call_id"): c.get("name") for c in r1raw or []}
    parts = []
    for cid, out in (outcomes or {}).items():
        try:
            msg = (json.loads(out) or {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(msg, str) and msg.startswith("dropped:"):
            parts.append(_couldnt(names.get(cid, "?"), msg[len("dropped: "):]))
    return " ".join(parts)


def _render_record(kind: str, data: dict) -> list[str]:
    """One short line per read hit, period-free (the found template punctuates)."""
    if not isinstance(data, dict):
        return []
    if kind == "wells":
        return [store.describe_well(w.get("well", "")).rstrip(".")
                for w in (data.get("wells") or [])[:3] if w.get("well")]
    if kind == "notes":
        return [f"{m.get('target', '?')}: {(m.get('text') or '')[:120]}"
                for m in (data.get("notes") or [])[:3]]
    if kind == "timers":
        return [f"{t.get('label', '?')} ({t.get('minutes_left', '?')}m left)"
                for t in (data.get("timers") or [])[:3]]
    if kind == "steps":
        return [f"Step {s.get('i')}: {s.get('name')}"
                + (" (current)" if s.get("current") else "")
                for s in (data.get("steps") or [])[:3]]
    if kind == "history":
        return [f"{e.get('action')} {','.join(e.get('wells') or [])} {e.get('ago')} ago"
                for e in (data.get("history") or [])[:3]]
    return []


def _render_r1_reads(calls: list, outcomes: dict) -> list[str]:
    """Render round-1 read outcomes for the speechless fallback."""
    lines = []
    for c in calls:
        cid = c.get("call_id")
        if not cid or cid not in outcomes:
            continue
        try:
            data = json.loads(outcomes[cid])
        except json.JSONDecodeError:
            continue
        if c.get("name") == "read_record":
            lines += _render_record((c.get("args") or {}).get("kind"), data)
        elif c.get("name") == "find_free_wells" and isinstance(data, dict):
            free = data.get("free") or []
            if free:
                lines.append(f"Free near {data.get('near', '')}: {', '.join(free[:4])}")
    return lines


#: Executed-but-speechless tools speak their result key (UI shows the rest).
_EXECUTED_VOICE = {"show": "opened", "open": "opened", "navigate": "opened", "back": "opened"}


def _executed_reply(calls: list, read_lines: list, session: dict) -> str:
    """Executed tools speak from what ran: UI actions opened, reads found data."""
    for c in calls:
        key = _EXECUTED_VOICE.get(c.get("name") or "")
        if key:
            return say(key, session)
    if read_lines:
        return say("found", session, detail=": " + "; ".join(read_lines[:3]) + ".")
    if any(c.get("name") in ("read_record", "find_free_wells") for c in calls):
        return say("found_empty", session)
    return ""


def _compose_reply(calls, res, state, session) -> str:
    """Reply purely from store results: messages, card questions, drop reasons.
    Empty when nothing resulted; run_utterance resolves via _executed_reply."""
    msgs = res["messages"]
    fails = [m for m in msgs if is_failure_message(m)]
    if fails:
        ok = [m for m in msgs if not is_failure_message(m)]
        return final_pip_line(ok, fails, " ".join(msgs))
    if not res["cards"]:
        r = timer_set_reply(calls, state)
        if r is not None:
            return r
    parts = list(msgs) + [c["question"] for c in res["cards"]]
    for d in res["info"].get("drop_reasons", []):
        _log_drop(d["call"], d.get("args"), d["reason"])
        parts.append(_couldnt(d["call"], d["reason"]))
    return " ".join(parts)


#: Human verbs per tool. User-visible text never names a tool.
_COULDNT = {
    "add_note": "save the note",
    "save_tip": "save the tip",
    "set_timer": "start the timer",
    "answer": "answer that",
    "mark_done": "record that",
    "record_redo": "mark that for redo",
    "edit_draft": "edit the draft",
}


def _couldnt(name, reason):
    return f"I couldn't {_COULDNT.get(name, 'do that')}: {reason}."


def _call_names(calls) -> str:
    return ",".join(c.get("name", "?") for c in calls) or "-"


def _utterance_response(calls, messages, reply, ms, route, cards, info, sources, version, extra=None):
    out = {
        "tool_calls": calls,
        "messages": messages,
        "reply": reply,
        "ms": ms,
        "route": route,
        "cards": cards,
        "record_version": version,
        "sources": sources,
    }
    for k in ("saved_memo", "edited", "robot", "ui"):
        if info.get(k):
            out[k] = info[k]
    if info.get("dropped"):
        out["dropped"] = info["dropped"]
    speak = speak_text(cards[0]["question"] if cards else reply)
    if speak:
        out["speak"] = speak
    if extra:
        out.update(extra)
    return out


def set_screen(session: dict, screen: str, selected_well: str = "", open_sheet: str = ""):
    """App-reported screen, validated against the registry. Unknown -> main."""
    session["screen"] = canonical_screen(screen) or "main"
    session["selected_well"] = selected_well or ""
    session["open_sheet"] = open_sheet or ""


async def _handle_text(text: str, session: dict, source: str):
    """Shared text path for /input and the /orchestrate alias."""
    key = _key()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        out = await run_utterance(client, key, text, session, source)
    calls, messages, reply, ms, route, cards, info, sources = out
    print(f"orch route={route} calls={_call_names(calls)} reply={reply[:80]!r}", flush=True)
    session["last_asked_cards"] = [c["id"] for c in cards]
    log_turn(session, text, calls, messages or ([reply] if reply else []),
             asked=info.get("asked"))
    return _utterance_response(calls, messages, reply, {"spark": ms}, route, cards, info, sources,
                               store.r["version"])


def _resolve_card(card_id: str, answer: str, session: dict):
    """Shared card path for /input and the /confirm alias."""
    ok = answer.strip().lower() not in ("no", "cancel", "false", "0", "dismiss")
    message, new_cards, card = store.confirm(card_id, ok, "card", session)
    cid = (card.get("payload") or {}).get("_call_id") if card else None
    if cid:
        _refresh_output(session, cid, ok, message)
    return message, new_cards


@app.post("/input")
async def input_entry(body: InputIn):
    session = get_session(body.session)
    set_screen(session, body.screen, body.selected_well, body.open_sheet)
    if body.card_id:
        message, new_cards = _resolve_card(body.card_id, body.answer or "yes", session)
        log_turn(session, f"card:{body.card_id} {body.answer or 'yes'}", [], [message])
        return _utterance_response([], [message], message, {"spark": 0}, "card", new_cards,
                                   {"dropped": 0, "drop_reasons": []}, [], store.r["version"])
    if not body.text.strip():
        raise HTTPException(400, "text or card_id required")
    return await _handle_text(body.text, session, "input")


@app.post("/orchestrate")
async def orchestrate(body: OrchestrateIn):
    """Alias: forwards into /input's text path (same shapes)."""
    session = get_session(body.session)
    set_screen(session, body.screen, body.selected_well, body.open_sheet)
    return await _handle_text(body.text, session, "spark")


@app.post("/voice")
async def voice(audio: UploadFile = File(...), state: str = Form("{}"), session: str = Form("bench"),
                screen: str = Form("main"), selected_well: str = Form(""),
                open_sheet: str = Form("")):
    try:
        state_obj = json.loads(state)
    except json.JSONDecodeError:
        raise HTTPException(400, "state must be a JSON object")
    if not isinstance(state_obj, dict):
        raise HTTPException(400, "state must be a JSON object")
    raw = await audio.read()
    try:
        with wave.open(BytesIO(raw), "rb") as w:
            wav_ok = w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() in (16000, 24000)
            dur = w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        wav_ok = False
    if not wav_ok:
        raise HTTPException(400, "audio must be mono 16-bit PCM WAV at 16kHz or 24kHz")
    Path("/tmp/last.wav").write_bytes(raw)
    key = _key()
    asr_req = {
        "mode": "PUSH_TO_TALK",  # single-turn voice command; the API default
        "model": ASR_MODEL,
        "audioEncoding": "WAV",
        "languageBias": LANGUAGE_BIAS,
        "keywords": KEYWORDS,
    }
    total0 = time.perf_counter()
    sess = get_session(session)
    set_screen(sess, screen, selected_well, open_sheet)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        t0 = time.perf_counter()
        resp = await _post(
            client,
            "asr",
            f"{BASE_URL}/asr/transcribe",
            params={"sessionId": uuid.uuid4().hex},
            headers={"Authorization": f"Bearer {key}"},
            files={
                "request": (None, json.dumps(asr_req), "application/json"),
                "audio": (audio.filename or "clip.wav", raw, "audio/wav"),
            },
        )
        asr_ms = int((time.perf_counter() - t0) * 1000)
        _check("asr", resp)
        transcript = resp.json().get("transcript", "")
        norm = normalize_text(transcript)
        print(f'voice dur={dur:.2f}s transcript="{transcript}" norm="{norm}"', flush=True)
        if not norm.strip(" .?!"):
            return {
                "transcript": "",
                "tool_calls": [],
                "messages": [],
                "reply": say("didnt_catch", sess),
                "ms": {"asr": asr_ms, "spark": 0, "total": int((time.perf_counter() - total0) * 1000)},
            }
        out = await run_utterance(client, key, transcript, sess, "voice")
    calls, messages, reply, spark_ms, route, cards, info, sources = out
    print(f"orch route={route} calls={_call_names(calls)} reply={reply[:80]!r}", flush=True)
    sess["last_asked_cards"] = [c["id"] for c in cards]
    log_turn(sess, transcript, calls, messages or ([reply] if reply else []),
             asked=info.get("asked"))
    return _utterance_response(
        calls, messages, reply,
        {"asr": asr_ms, "spark": spark_ms, "total": int((time.perf_counter() - total0) * 1000)},
        route, cards, info, sources, store.r["version"], {"transcript": transcript},
    )


@app.post("/confirm")
async def confirm(body: ConfirmIn):
    """Alias: forwards into /input's card path (same shapes)."""
    session = get_session(body.session)
    message, new_cards = _resolve_card(body.card_id, "yes" if body.ok else "no", session)
    log_turn(session, "yes" if body.ok else "no", [], [message])
    return {"message": message, "cards": new_cards, "record_version": store.r["version"]}


@app.post("/notes/edit")
async def notes_edit(body: EditNoteIn):
    message = store.edit_note_manual(body.id, body.text)
    return {"message": message, "record_version": store.r["version"]}


def _coerce_patch_value(draft, parsed, value):
    """PATCH JSON values -> typed values. Returns (ok, value|reason)."""
    kind, _, field = parsed
    if field == "replicates":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return False, "count must be a whole number of 1 or more"
        return True, value
    if field in ("minutes", "temp_c", "pipette.volume"):
        noun = {"minutes": "minutes", "temp_c": "temp", "pipette.volume": "volume"}[field]
        if value is None:
            return True, None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return False, f"{noun} must be 0 or more"
        return True, float(value)
    if field == "wells":
        if isinstance(value, str):
            items = proto.parse_wellref(value)
            if not items:
                return False, f"not plate wells: {value}"
            return True, items
        items = list(value) if isinstance(value, list) else None
        if items is None or not all(isinstance(w, str) and proto.parse_wellref(w) == [w.upper()]
                                    for w in items):
            return False, f"not plate wells: {value}"
        if not items:
            return False, "needs at least one well"
        return True, [w.upper() for w in items]
    if field == "pipette.target":
        if isinstance(value, dict):
            t, v = value.get("type"), value.get("value")
            if t == "wells" and (not isinstance(v, str) or not proto.parse_wellref(v)):
                return False, f"not plate wells: {v}"
            if t == "condition" and v not in (draft.get("plan") or {}).get("conditions", {}):
                return False, f"no such condition: {v}"
            if t not in ("wells", "condition", "all_used", "other"):
                return False, "need wells:<WellRef>, condition:<id>, all, or other"
            return True, {"type": t, "value": v}
        if isinstance(value, str):
            return proto.parse_target_value(draft, value)
        return False, "need wells:<WellRef>, condition:<id>, all, or other"
    if not isinstance(value, str) and value is not None:
        return False, f"{field} must be text"
    return True, (value.strip() if value else None)


@app.post("/protocols/import")
async def protocol_import(file: UploadFile = File(...), session: str = Form("bench")):
    """PDF/txt -> extracted draft. Full mode only; bytes never touch disk."""
    if not PROTOCOL_FULL_MODE:
        raise HTTPException(403, "protocol import needs Full mode")
    sess = get_session(session)
    raw = await file.read()
    if len(raw) > PROTOCOL_MAX_BYTES:
        raise HTTPException(413, "protocol file too large (10MB max)")
    name = (file.filename or "").lower()
    kind = "pdf" if name.endswith(".pdf") else "txt" if name.endswith(".txt") else None
    if kind is None:
        raise HTTPException(400, "need a .pdf or .txt file")
    if kind == "pdf":
        try:
            from pypdf import PdfReader
            text = "\n".join((pg.extract_text() or "")
                             for pg in PdfReader(BytesIO(raw)).pages)
        except Exception:
            text = ""
        payload = "data:application/pdf;base64," + base64.b64encode(raw).decode("ascii")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "txt must be UTF-8")
        payload = text[: proto.PROMPT_CHARS]
    body = proto.build_extract_body(kind, file.filename or "protocol", payload, SPARK_MODEL)
    key = _key()
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _post(client, "protocol", f"{BASE_URL}/responses",
                           headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"},
                           json=body)
    ms = int((time.perf_counter() - t0) * 1000)
    _check("protocol", resp)
    usage = resp.json().get("usage") or {}
    print(f"protocol ms={ms} in_tok={usage.get('input_tokens', '?')} "
          f"out_tok={usage.get('output_tokens', '?')}", flush=True)
    ok, data = proto.parse_extract_payload(resp.json())
    if not ok:
        raise HTTPException(502, f"protocol extraction failed: {data}")
    draft = proto.build_draft(data, proto.evidence_from_text(text), file.filename or "protocol")
    store.save_draft(draft)
    conds = len(draft["plan"]["conditions"])
    steps = len(draft["plan"]["steps"])
    reply = f"Draft ready: {conds} conditions, {steps} steps."
    if draft["errors"]:
        reply += f" {len(draft['errors'])} red items to fix."
    elif draft["review"]:
        reply += f" {len(draft['review'])} to review."
    log_turn(sess, f"import {file.filename or 'protocol'}", [], [reply])
    return {"draft": draft, "review": draft["review"], "errors": draft["errors"], "reply": reply,
            "speak": speak_text(reply), "ms": ms, "record_version": store.r["version"]}


@app.patch("/protocols/draft")
async def draft_patch(body: DraftPatchIn):
    """Hand edit one draft path; same validator as the voice tool."""
    get_session(body.session)
    draft = store.r.get("draft")
    if not draft:
        return {"ok": False, "reason": "no draft open", "record_version": store.r["version"]}
    parsed = proto.parse_draft_path(body.path)
    if parsed is None:
        return {"ok": False, "reason": f"unknown path {body.path}",
                "record_version": store.r["version"]}
    ok, value = _coerce_patch_value(draft, parsed, body.value)
    if not ok:
        return {"ok": False, "reason": value, "record_version": store.r["version"]}
    ok, reason = proto.apply_draft_edit(draft, parsed, value)
    if not ok:
        return {"ok": False, "reason": reason, "record_version": store.r["version"]}
    proto.recompute_draft(draft)
    store.save_draft(draft)
    return {"ok": True, "draft": draft, "review": draft["review"], "errors": draft["errors"],
            "record_version": store.r["version"]}


@app.post("/protocols/start")
async def protocol_start(body: DraftStartIn):
    """Archive the current run, reset on the draft plan. Errors block."""
    session = get_session(body.session)
    ok, info = store.start_run()
    if not ok:
        if "errors" in info:
            n = len(info["errors"])
            reply = f"Fix {n} red item{'s' if n != 1 else ''} first."
            log_turn(session, "start run", [], [reply])
            return {"started": False, "errors": info["errors"], "reply": reply,
                    "record_version": store.r["version"]}
        raise HTTPException(400, info["reason"])
    reply = f"Run started: {info['conditions']} conditions, {info['steps']} steps."
    if info["review"]:
        reply += f" {info['review']} unreviewed."
    log_turn(session, "start run", [], [reply])
    return {"started": True, "reply": reply, "speak": speak_text(reply),
            "record_version": store.r["version"]}


@app.delete("/protocols/draft")
async def draft_delete(body: DraftStartIn):
    store.clear_draft()
    return {"ok": True, "record_version": store.r["version"]}


@app.get("/health")
async def health():
    return {"ok": True, "commit": git_commit(), "tools": len(TOOLS),
            "prompt_hash": prefix_hash(),
            "memory": "chain" if CHAIN_ENABLED else "summary"}


@app.get("/record")
async def record():
    return store.snapshot()


@app.get("/events")
async def events():
    queue: asyncio.Queue = asyncio.Queue()

    def cb(msg: dict):
        queue.put_nowait(msg)

    store.subscribe(cb)

    async def gen():
        try:
            yield f"data: {json.dumps({'kind': 'snapshot', **store.snapshot()}, ensure_ascii=False)}\n\n"
            while True:
                msg = await queue.get()
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
        finally:
            try:
                store._listeners.remove(cb)
            except ValueError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/robot")
async def robot():
    from server.robot import run_pipeline

    return run_pipeline(store)
