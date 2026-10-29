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
    whole_sentences,
    find_well_mentions,
    grounded,
    mentioned_targets,
    ungrounded_targets,
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
from server.screens import ask_choice, button_call
from server.screens import guide as _screen_guide
from server.screens import READS as SCREEN_READS
from server import library as lib
from server import protocol as proto
from server import runs as runlog
from server.store import RecordStore, describe_well_row
from server import live_asr
from server import tone
from server.grammar import match_session_end
from server.live_session import LiveConversation, live_keywords
from server.live_session import settings as live_settings
from server.tone import keys as tone_keys
from server.tone import say

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

load_dotenv()

#: Snapshot/action contract version. Bump on any shared/schemas or
#: view/action-shape change; the app compares at startup.
API_VERSION = 8

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
    f"You're {tone.agent_name()}, a friendly lab buddy at the bench. "
    "Short, warm, casual (Got it / Nice / Heads up / Hmm). "
    "Never claim anything the record doesn't show. "
    "Record only reported facts (done, finished, added, saw, looks). "
    "Bench instructions (wash, add, do, start the next) are not records: "
    "do your part (timer, guidance) or ask. "
    "If no tool fits (thanks, greeting), answer in one short sentence. "
    "Text beside tool calls is ignored. "
    "A run-plan draft may be open (draft_open): change it only with perform edit_draft "
    "using exact paths from the draft, never with record actions. "
    "Wells are WellRefs (\"C2-C8\"). \"Those\" and \"it\" refer to recent wells. "
    "One set_timer per distinct duration; the server fans each out per well. "
    "Ground every value in this message, the plan, or read results. "
    "For questions about the record, call read_record once, then answer briefly for speech. "
    "Speak at most two short sentences. For a list, say how many and the top one, "
    "and perform navigate to the screen that shows it all; never read a whole list. "
    "Name runs and protocols by their `spoken` labels, never by ids. "
    "After a write, the screen updates itself; never open to confirm. "
    "Ask the user with ask(question) only when you must. "
    "asked: is your last question; this message may answer it. "
    "Emit direct calls in run order; if a later step needs an earlier result, do only the first step now. "
    "For another or backup well, read free_wells first, then act on the first candidate at once (redo: exclude_well + record_redo); the server confirms via cards."
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


SCREEN_GUIDE = _screen_guide()

WELLREF = {"type": "string", "description": 'WellRef: "C5" | "C2-C8" | "C2,C5,C7"'}

#: Actions the model may perform. answer_card is deliberately absent: only
#: taps and the voice card discriminator emit it (pinned by test).
PERFORM_ACTIONS = ["mark_done", "exclude_well", "record_redo", "add_note",
                   "edit_note", "save_tip", "set_timer", "cancel_timer",
                   "extend_timer", "next_step", "end_run", "show", "navigate",
                   "open", "back", "make_robot", "edit_draft", "retry_sync",
                   "skip_sync"]

TOOLS = [
    _tool(
        "perform",
        "Do one bench action per call; emit in run order. "
        "mark_done{wells,step?}: REPORTED finished only (done/finished/added/I put); "
        "step = the plan step number it belongs to (blank = current step); "
        "bench instructions (wash/add/do) record nothing. "
        "exclude_well{well,reason?}; record_redo{wells,note?} (+exclude when elsewhere); "
        "add_note{target,text} (stated observation; nothing to write -> ask); "
        "edit_note{well?,text}; save_tip{step_id,text}; "
        "set_timer{minutes,wells?,on_done?} (minutes stated, else ask); "
        "cancel_timer{label?}; extend_timer{label?,minutes?} (+5 default); "
        "next_step; end_run; show{view?,target?}; navigate{screen,target?}; "
        "open{target} (well/screen/run/original/importer); back; make_robot; "
        "edit_draft{path,value} (draft_open only); "
        "retry_sync; skip_sync (setup screen only). Never answer_card. "
        f"Screens (navigate.screen): {SCREEN_GUIDE} "
        "Backup well: read free_wells first, then act on the first candidate at once.",
        {
            "action": {"type": "string", "enum": PERFORM_ACTIONS},
            "wells": _opt(WELLREF),
            "well": _opt(WELLREF),
            "target": _opt({"type": "string"}),
            "text": _opt({"type": "string"}),
            "minutes": _opt({"type": "number"}),
            "label": _opt({"type": "string"}),
            "note": _opt({"type": "string"}),
            "reason": _opt({"type": "string"}),
            "screen": {"type": ["string", "null"], "enum": [*SCREEN_IDS, None]},
            "step": _opt({"type": "integer"}),
            "path": _opt({"type": "string"}),
            "value": _opt({"type": "string"}),
            "step_id": _opt({"type": "string"}),
            "on_done": _opt({"type": "string"}),
            "view": _opt({"type": "string"}),
        },
    ),
    _tool(
        "read_record",
        "Use when you need record facts to answer or decide. "
        f"kinds: {', '.join(f'{r['kind']} ({r['desc']})' for r in SCREEN_READS)}. "
        "Don't use when the state you have already answers it.",
        {
            "kind": {"type": "string", "enum": [r["kind"] for r in SCREEN_READS]},
            "filter": _opt({"type": "string",
                            "description": "WellRef, condition, step, label, protocol name, "
                            "or run selector (latest/today/yesterday/date/id); blank for natural scope"}),
            "limit": _opt({"type": "integer", "description": "Max entries (default 10)"}),
        },
    ),
    _tool(
        "ask",
        "Use when you must ask the user something to proceed. "
        "Don't use when you can act or answer already.",
        {"question": {"type": "string"}},
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
        bits.append(f"{c.get('name') or '?'}[{w}]" if w else (c.get("name") or "?"))
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


class ConfirmIn(BaseModel):
    card_id: str = Field(min_length=1)
    ok: bool
    session: str = "bench"


class ActionIn(BaseModel):
    id: str = Field(min_length=1)
    args: dict = {}


class InputIn(BaseModel):
    text: str = ""
    card_id: str = ""
    answer: str = ""
    action: ActionIn | None = None
    session: str = "bench"


class EditNoteIn(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class DraftPatchIn(BaseModel):
    path: str = ""
    value: Any = None
    session: str = "bench"


class DraftStartIn(BaseModel):
    session: str = "bench"


class RunStartIn(BaseModel):
    protocol_id: str = Field(min_length=1)
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
    """Model's conversational line: collapsed, at most two whole sentences
    within 280 chars (never cut mid-sentence; a long first one stays whole)."""
    return whole_sentences(" ".join((t or "").split()), 280, max_count=2)


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
    calls = [{"name": "answer_card", "args": {"card_id": cid, "ok": ok}}
             for cid, ok in targets]
    res = store.apply_calls(calls, "card", "", "card", session)
    reply = " ".join(res["messages"]) or "Done."
    return [], res["messages"], reply, 0, "card", res["cards"], res["info"], []


#: Args an action can't run without. A missing one becomes ask(), never a
#: failure line ("I couldn't open the null screen").
REQUIRED_ARGS = {
    "mark_done": ["wells"], "exclude_well": ["well"], "record_redo": ["wells"],
    "add_note": ["target", "text"], "edit_note": ["text"],
    "save_tip": ["step_id", "text"], "set_timer": ["minutes"],
    "navigate": ["screen"], "open": ["target"], "edit_draft": ["path", "value"],
}
ASK_FOR = {
    "wells": "Which wells?", "well": "Which well?", "target": "Which one?",
    "text": "What should it say?", "step_id": "Which step?",
    "minutes": "How many minutes?", "screen": "Which screen?",
    "path": "Which field?", "value": "What value?",
}


def _blank(v) -> bool:
    """Model placeholders for "no value": null, "", "null", "none", "undefined"."""
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "undefined", "n/a"))


def _expand_perform(calls: list) -> list:
    """perform(action, args) -> the action call the handler takes.

    The one gate for every model action: blank placeholders are dropped,
    and an action missing a required arg becomes ask() for it. Unknown
    actions flow through (the handler drops them, as with unknown tools
    before); answer_card from the model flows through too and is rejected
    by the handler, so no model path can touch card state.
    """
    out = []
    for c in calls:
        name = c.get("name")
        if not isinstance(name, str) or not name.strip():
            _log_drop("?", c.get("args"), "dropped: tool without a name")
            continue
        if name != "perform":
            out.append(c)
            continue
        a = c.get("args") or {}
        action = a.get("action")
        if not isinstance(action, str) or not action.strip():
            _log_drop("perform", a, "dropped: no action")
            continue
        rest = {k: v for k, v in a.items() if k != "action" and not _blank(v)}
        missing = [k for k in REQUIRED_ARGS.get(action, []) if k not in rest]
        if missing:
            _log_drop(action, a, f"ask: missing {missing[0]}")
            ask = {"name": "ask", "args": {"question": ASK_FOR.get(missing[0], "Which one?")}}
            if c.get("call_id"):
                ask["call_id"] = c["call_id"]
            out.append(ask)
            continue
        if action not in PERFORM_ACTIONS:
            _log_drop("perform", a,
                      "rejected: answer_card" if action == "answer_card"
                      else f"unknown action: {action}")
        new = {"name": action, "args": rest, "from_model": True}
        if c.get("call_id"):
            new["call_id"] = c["call_id"]
        out.append(new)
    return out


async def run_utterance(client: httpx.AsyncClient, key: str, text: str, session: dict, source: str):
    """Cards first (no model call), then grammar, else Spark with one lookup round.
    The model hears the raw transcript; normalization lives inside grammar only.
    Returns (calls, messages, reply, ms, route, cards, info, sources)."""
    if store.r["cards"]:
        ans = match_card_answer(text)
        if ans is not None:
            return _answer_cards(ans, session)
    v = store.r["view"]
    b = button_call(text, [x for x in ((v.get("sheet") or {}).get("name"), v["current"]["screen"]) if x])
    g = ([b], "") if b else match_grammar(text, store.prompt_state(session))
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
    raw_count = len(calls)
    calls = _expand_perform(calls)
    if raw_count and not calls and not texts.strip() and not searches:
        # every call was unusable (no action / no name): ask, offering the
        # registry's best-matching screens instead of guessing
        q = ask_choice(text) or say("didnt_catch", session)
        print(f"spark calls unusable for {text[:60]!r}: asking", flush=True)
        asked = {"question": q, "target": ""}
        return [], [], q, ms, "spark", [], {"dropped": raw_count, "drop_reasons": [], "asked": asked}, []
    sentence, sources = web_answer(searches) if searches else ("", [])
    if sentence:
        session["last_web"] = sentence
    ordered_ids = [c.get("call_id") for c in calls]
    r1raw = [dict(c) for c in calls]
    plan_allowed = relevant_plan_numbers(
        text, [(s.get("text") or s.get("name", ""), s.get("minutes"))
               for s in store.r["plan"]["steps"]])
    has_reads = any(c.get("name") == "read_record" and c.get("call_id") for c in calls)
    pre_all_reads = bool(calls) and all(c.get("name") == "read_record" for c in calls)
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
            bad = ungrounded_targets(t)
            if not bad["wells"] and not bad["timer"] and not bad["step"] and not bad["memo"]:
                return [], [], t, ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
            ut = mentioned_targets(text)
            shared_wells = sorted(bad["wells"] & ut["wells"])
            if shared_wells:
                label = wells_label(shared_wells)
            elif bad["timer"] and ut["timer"]:
                label = "the timer"
            elif bad["step"] and ut["step"]:
                label = "that step"
            elif bad["memo"] and ut["memo"]:
                label = "that note"
            else:
                return [], [], say("ack", session), ms, "spark", [], \
                    {"dropped": 0, "drop_reasons": []}, sources
            reply = say("didnt_act_target", session, target=label)
            asked = {"question": reply,
                     "target": wells_label(shared_wells) if shared_wells else ""}
            return [], [], reply, ms, "spark", [], \
                {"dropped": 0, "drop_reasons": [], "asked": asked}, sources
        dropped = _dropped_replies(r1raw, outcomes)
        if dropped:
            return [], [], dropped, ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
        print(f"spark empty: no calls, no text for {text[:60]!r}", flush=True)
        return _didnt_act(text, ms, session, sources)
    res = store.apply_calls(calls, source, text, "spark", session)
    _apply_outcomes(calls, res, outcomes, session)
    read_lines = _render_r1_reads(calls, outcomes)
    _store_outputs(session, ordered_ids, outcomes)
    fire_lookup = (
        has_reads and question is None
        and any(c.get("name") == "read_record" and c.get("call_id") for c in calls)
    )
    model_line = ""  # the model's own sentence; spoken only when the store said nothing
    if fire_lookup:
        reads_text = " ".join(
            outcomes[cid] for c in calls
            if c.get("name") == "read_record"
            and (cid := c.get("call_id")) and cid in outcomes
        )
        ms2, more = await _lookup_round(client, key, text, session, source, plan_allowed, reads_text,
                                        summary, r1raw)
        ms += ms2
        if more is not None:
            rc, res2, question2, ordered2, outcomes2, gdrop2, t2 = more
            if question is None and question2 is None and pre_all_reads and not res2["cards"]:
                nav_only = all(c.get("name") in ("navigate", "open", "back", "show") for c in rc)
                if t2.strip() and (not rc or nav_only):
                    model_line = _short_text(t2)
                elif not t2.strip() and not rc:
                    for c in calls:
                        a = c.get("args") or {}
                        if c.get("name") != "read_record" or a.get("kind") != "wells":
                            continue
                        if store._viewing_run() is not None:
                            continue  # scoped reads answer from their own data, never the live record
                        filt = a.get("filter") or ""
                        for w in store.filter_wells(filt if isinstance(filt, str) else "")[:4]:
                            res["messages"].append(store.describe_well(w))
            calls = calls + rc
            res["messages"] += res2["messages"]
            res["cards"] += res2["cards"]
            res["outcomes"] += res2["outcomes"]
            res["ui"] += res2["ui"]
            res["info"]["ui"] = res["ui"]
            res["info"]["dropped"] += res2["info"]["dropped"]
            res["info"]["drop_reasons"] += res2["info"]["drop_reasons"]
            for k in ("saved_memo", "edited", "robot", "setup_done",
                        "sync_retry"):
                if res2["info"].get(k):
                    res["info"][k] = res2["info"][k]
            if question2 is not None:
                question = question2
            if res2["info"].get("timer_started"):
                gdrop = gdrop2
            else:
                gdrop = gdrop or gdrop2
            _store_outputs(session, ordered2, outcomes2)
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
        # Store results are the reply; the model's line never joins them.
        reply = _compose_reply(calls, res, store.prompt_state(session), session) or model_line
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
    rc = _expand_perform(rc)
    ordered2 = [c.get("call_id") for c in rc]
    rc, question2, outcomes2, gdrop2 = _validate(rc, text, 2, plan_numbers, reads_text, summary)
    res2: dict = {"messages": [], "cards": [], "outcomes": [], "ui": [],
                  "info": {"dropped": 0, "drop_reasons": []}}
    if rc:
        res2 = store.apply_calls(rc, source, text, "spark", session, reads_text)
        _apply_outcomes(rc, res2, outcomes2, session)
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
    """expand -> ask split -> evidence-graded timer filter.
    Returns (calls, question, outcomes, grounded_drop)."""
    outcomes: dict[str, str] = {}
    calls = fill_on_done(calls, text)
    before, calls = calls, expand_tool_wells(calls)
    _note_lost(outcomes, before, calls, "dropped: no valid wells")
    question, kept = split_ask(calls)
    for c in calls:
        if c.get("name") == "ask" and c.get("call_id"):
            outcomes[c["call_id"]] = "asked user"
    calls = kept
    if depth >= 2:
        deep = [c for c in calls if c.get("name") == "read_record"]
        calls = [c for c in calls if c.get("name") != "read_record"]
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


def _apply_outcomes(calls, res, outcomes, session=None):
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
        return [describe_well_row(w).rstrip(".")
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
    if kind == "source":
        q = (data.get("quote") or "")[:140]
        if not q:
            return []
        return [f"Step {data.get('step')}: {q} (page {data.get('quote_page')})"]
    if kind == "free_wells":
        free = data.get("free") or []
        if not free:
            return []
        return [f"Free near {data.get('near', '')}: {', '.join(free[:4])}"]
    if kind == "protocols":
        return [f"{p.get('spoken') or p.get('name', '?')} ({p.get('steps', '?')} steps)"
                for p in (data.get("protocols") or [])[:3]]
    if kind == "runs":
        return [f"{r.get('spoken') or 'a run'} ({r.get('status', '?')})"
                for r in (data.get("runs") or [])[:3]]
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
    if any(c.get("name") == "read_record" for c in calls):
        return say("found_empty", session)
    return ""


def _compose_reply(calls, res, state, session) -> str:
    """Reply purely from store results: messages, card questions, drop reasons.
    Empty when nothing resulted; run_utterance resolves via _executed_reply."""
    msgs = [m for m in res["messages"] if isinstance(m, str) and m]  # None/blank never joins
    fails = [m for m in msgs if is_failure_message(m)]
    if fails:
        ok = [m for m in msgs if not is_failure_message(m)]
        return final_pip_line(ok, fails, " ".join(msgs))
    if not res["cards"]:
        r = timer_set_reply(calls, state)
        if r is not None:
            return r
    parts = [m for m in msgs if isinstance(m, str) and m] + \
        [c["question"] for c in res["cards"] if isinstance(c.get("question"), str)]
    for d in res["info"].get("drop_reasons", []):
        _log_drop(d["call"], d.get("args"), d["reason"])
        parts.append(_couldnt(d["call"], d["reason"]))
    return " ".join(parts)


#: Human verbs per tool. User-visible text never names a tool.
_COULDNT = {
    "add_note": "save the note",
    "save_tip": "save the tip",
    "set_timer": "start the timer",
    "mark_done": "record that",
    "record_redo": "mark that for redo",
    "edit_draft": "edit the draft",
}


def _couldnt(name, reason):
    return f"I couldn't {_COULDNT.get(name, 'do that')}: {reason}."


def _call_names(calls) -> str:
    return ",".join(str(c.get("name") or "?") for c in calls) or "-"


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
    for k in ("saved_memo", "edited", "robot", "ui", "setup_done",
                "sync_retry"):
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
    message, new_cards, _ = store.confirm(card_id, ok, "card", session)
    return message, new_cards


@app.post("/input")
async def input_entry(body: InputIn):
    session = get_session(body.session)
    if body.action is not None:
        calls = [{"name": body.action.id, "args": body.action.args or {}}]
        calls = expand_tool_wells(calls)
        res = store.apply_calls(calls, "tap", "", "tap", session)
        reply = _compose_reply(calls, res, store.prompt_state(session), session)
        if not reply:
            reply = _executed_reply(calls, [], session)
        log_turn(session, f"tap:{body.action.id}", calls, [reply] if reply else [])
        return _utterance_response(calls, res["messages"], reply, {"spark": 0}, "tap",
                                   res["cards"], res["info"], [], store.r["version"])
    if body.card_id:
        message, new_cards = _resolve_card(body.card_id, body.answer or "yes", session)
        log_turn(session, f"card:{body.card_id} {body.answer or 'yes'}", [], [message])
        return _utterance_response([], [message], message, {"spark": 0}, "card", new_cards,
                                   {"dropped": 0, "drop_reasons": []}, [], store.r["version"])
    if not body.text.strip():
        raise HTTPException(400, "text, card_id, or action required")
    return await _handle_text(body.text, session, "input")


@app.post("/orchestrate")
async def orchestrate(body: OrchestrateIn):
    """Alias: forwards into /input's text path (same shapes)."""
    session = get_session(body.session)
    return await _handle_text(body.text, session, "spark")


@app.post("/voice")
async def voice(audio: UploadFile = File(...), state: str = Form("{}"), session: str = Form("bench")):
    total0 = time.perf_counter()
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
    if dur <= 0:
        # No content in, nothing out: skip ASR/Spark, no turn logged.
        # (Audio present but wordless still answers didnt_catch below.)
        print(f"voice dur={dur:.2f}s ignored: empty audio", flush=True)
        return _utterance_response(
            [], [], "",
            {"asr": 0, "spark": 0, "total": int((time.perf_counter() - total0) * 1000)},
            "ignored", [], {"dropped": 0, "drop_reasons": []}, [], store.r["version"],
            {"transcript": "", "ignored": True},
        )
    Path("/tmp/last.wav").write_bytes(raw)
    key = _key()
    asr_req = {
        "mode": "PUSH_TO_TALK",  # single-turn voice command; the API default
        "model": ASR_MODEL,
        "audioEncoding": "WAV",
        "languageBias": LANGUAGE_BIAS,
        "keywords": KEYWORDS,
    }
    sess = get_session(session)
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


@app.websocket("/voice/live")
async def voice_live(ws: WebSocket):
    """Live conversation relay (shared/schemas/live.schema.json): app PCM16
    16 kHz frames -> Meta realtime (DIARIZATION); partials and ignored turns
    go back as they come; owner turns run the one text path (_handle_text)
    and the reply returns as a `turn` message (record changes ride SSE)."""
    await ws.accept()
    start = await ws.receive_json()
    session = get_session((start or {}).get("session") or "bench")
    meta = live_asr.MetaStream(_key(), mode="DIARIZATION", keywords=live_keywords(store.r),
                               language_bias=["English"])
    try:
        ok = await meta.open()
    except Exception as e:  # network, handshake timeout
        ok, meta.error = False, {"message": f"{type(e).__name__}: {e}"}
    if not ok:
        print(f"live open failed: {meta.error}", flush=True)
        await ws.send_json({"type": "error", "message": (meta.error or {}).get("message", "open failed")})
        await meta.aclose()
        await ws.close()
        return
    print(f"live open session={meta.session_id}", flush=True)
    await ws.send_json({"type": "ready", "session_id": meta.session_id})
    conv = LiveConversation(opened_at=time.monotonic(),
                            window_s=float(live_settings().get("window_s") or 60),
                            max_s=float(live_settings().get("max_s") or 55 * 60),
                            wake_phrase=tone.persona().get("wake_phrase") or "")
    owner_q: asyncio.Queue = asyncio.Queue()
    ended = False
    why = ""

    async def end_stream(reason: str):
        """endStream once; Meta flushes and closes 1000 (downlink then ends)."""
        nonlocal ended, why
        if not ended:
            ended, why = True, reason
            print(f"live end: {reason}", flush=True)
            await meta.end()

    async def uplink():
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                if not ended:
                    await meta.send_audio(msg["bytes"])
                continue
            body = json.loads(msg.get("text") or "{}")
            if body.get("type") == "tts":
                conv.tts(bool(body.get("speaking")), time.monotonic())
            elif body.get("type") == "end":
                break
        await end_stream("client")

    async def downlink():
        async for ev in meta.events():
            for out in conv.feed(ev, time.monotonic()):
                if out["type"] in ("owner", "here"):
                    owner_q.put_nowait(out)
                    continue
                if out["type"] == "ignored":
                    print(f'[voice] ignored speaker={out["speaker"]} text="{out["text"]}"', flush=True)
                await ws.send_json(out)

    def short(reply: str, route: str) -> dict:
        """A turn answered without the model (grammar / here / sorry)."""
        return _utterance_response([], [], reply, {}, route, [],
                                   {"dropped": 0, "drop_reasons": []}, [], store.r["version"])

    async def owner_turn(item: dict) -> tuple[str, dict, str | None]:
        if item["type"] == "here":
            return "", short(say("here", session), "grammar"), None
        text = item["text"]
        print(f'live owner turn: "{text}"', flush=True)
        # Session end is a grammar decision; a pending card's answer wins.
        end = None if (store.r["cards"] and match_card_answer(text)) else \
            match_session_end(text, tone.agent_name())
        if end is not None:
            return text, short(say("session_thanks", session) if end == "thanks" else "", "grammar"), end
        return text, await _handle_text(text, session, "voice"), None

    async def worker():
        while True:
            item = await owner_q.get()
            try:
                text, resp, end = await owner_turn(item)
            except Exception as e:  # any failure: the session stays, the turn says sorry
                print(f"live turn failed: {type(e).__name__}: {e}", flush=True)
                text, resp, end = item.get("text", ""), short(say("didnt_catch", session), "error"), None
            await ws.send_json({"type": "turn", "transcript": text, "end_session": end is not None, **resp})
            conv.owner_replied(time.monotonic())
            if end is not None:
                await end_stream("end_session")

    async def watchdog():
        while not ended:
            await asyncio.sleep(1)
            reason = conv.should_close(time.monotonic()) if owner_q.empty() else None
            if reason:
                await end_stream(reason)

    tasks = [asyncio.create_task(c()) for c in (uplink, worker, watchdog)]
    try:
        await downlink()  # ends when Meta closes (1000 after endStream, or an error)
    finally:
        for t in tasks:
            t.cancel()
        code, reason = meta.close
        # why: window / max_duration / end_session / client, else the stream broke
        why = why or ("error" if code != 1000 or meta.error else "server")
        print(f"live closed {code} {reason!r} why={why}", flush=True)
        try:
            await ws.send_json({"type": "closed", "code": code, "reason": reason, "why": why})
            await ws.close()
        except Exception:
            pass  # the app already left
        await meta.aclose()


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
    pages = None
    if kind == "pdf":
        try:
            from pypdf import PdfReader
            pages = [(pg.extract_text() or "") for pg in PdfReader(BytesIO(raw)).pages]
            text = "\n".join(pages)
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
    draft = proto.build_draft(data, proto.evidence_from_text(text, pages),
                               file.filename or "protocol")
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
    """Draft -> library, then start. Cards when a run is active. Errors block."""
    session = get_session(body.session)
    ok, info = store.start_run(session)
    if not ok:
        if "errors" in info:
            n = len(info["errors"])
            reply = f"Fix {n} red item{'s' if n != 1 else ''} first."
            log_turn(session, "start run", [], [reply])
            return {"started": False, "errors": info["errors"], "reply": reply,
                    "record_version": store.r["version"]}
        if "reason" in info:
            raise HTTPException(400, info["reason"])
        card = info["card"]
        log_turn(session, "start run", [], [card["question"]])
        return {"started": False, "card": card, "reply": card["question"],
                "protocol_id": info.get("protocol_id"),
                "protocol_name": info.get("protocol_name"),
                "speak": speak_text(card["question"]),
                "record_version": store.r["version"]}
    reply = (f"Run started: {info['protocol_name']} — {info['conditions']} conditions, "
             f"{info['steps']} steps.")
    if info["review"]:
        reply += f" {info['review']} unreviewed."
    log_turn(session, "start run", [], [reply])
    return {"started": True, "run_id": (info["run"] or {}).get("run_id"),
            "protocol_id": info.get("protocol_id"),
            "protocol_name": info.get("protocol_name"), "reply": reply,
            "speak": speak_text(reply), "record_version": store.r["version"]}


@app.post("/protocols/save")
async def protocol_save(body: DraftStartIn):
    """Draft -> library only. The active run is untouched."""
    session = get_session(body.session)
    ok, info = store.save_draft_to_library()
    if not ok:
        if isinstance(info, dict) and "errors" in info:
            n = len(info["errors"])
            reply = f"Fix {n} red item{'s' if n != 1 else ''} first."
            log_turn(session, "save protocol", [], [reply])
            return {"saved": False, "errors": info["errors"], "reply": reply,
                    "record_version": store.r["version"]}
        raise HTTPException(400, info if isinstance(info, str) else "cannot save")
    reply = f"Saved {info.get('name')} to the library."
    log_turn(session, "save protocol", [], [reply])
    return {"saved": True, "protocol": lib.summarize(info), "reply": reply,
            "speak": speak_text(reply), "record_version": store.r["version"]}


@app.get("/protocols")
async def protocols_list():
    return {"protocols": lib.list_all(store.root)}


@app.get("/protocols/{protocol_id}")
async def protocol_get(protocol_id: str):
    doc = lib.get(protocol_id, store.root)
    if doc is None:
        raise HTTPException(404, "unknown protocol")
    return {"protocol": doc}


@app.post("/runs/start")
async def runs_start(body: RunStartIn):
    """Start now, or card when an active run must finish first."""
    session = get_session(body.session)
    ok, info = store.request_start_run(body.protocol_id, session)
    if not ok and "reason" in info:
        raise HTTPException(404, info["reason"])
    if not ok:
        card = info["card"]
        log_turn(session, f"start {body.protocol_id}", [], [card["question"]])
        return {"started": False, "card": card, "reply": card["question"],
                "speak": speak_text(card["question"]),
                "record_version": store.r["version"]}
    reply = f"Run started: {info['conditions']} conditions, {info['steps']} steps."
    log_turn(session, f"start {body.protocol_id}", [], [reply])
    return {"started": True, "run_id": (info["run"] or {}).get("run_id"),
            "protocol_id": info["protocol_id"], "reply": reply,
            "speak": speak_text(reply), "record_version": store.r["version"]}


@app.post("/runs/end")
async def runs_end(body: DraftStartIn):
    """End run always asks first; the card does the finishing."""
    session = get_session(body.session)
    ok, info = store.request_end_run(session)
    if not ok and "reason" in info:
        raise HTTPException(400, info["reason"])
    card = info["card"]
    log_turn(session, "end run", [], [card["question"]])
    return {"ended": False, "card": card, "reply": card["question"],
            "speak": speak_text(card["question"]),
            "record_version": store.r["version"]}


@app.get("/runs")
async def runs_list(protocol_id: str = ""):
    docs = store.all_run_docs()
    if protocol_id:
        docs = [d for d in docs if d.get("protocol_id") == protocol_id]
    return {"runs": store.run_summaries(docs)}


@app.get("/runs/{run_id}")
async def run_get(run_id: str):
    doc = runlog.resolve(run_id, store.all_run_docs())
    if doc is None:
        raise HTTPException(404, "unknown run")
    lab = store.run_label(doc)
    return {"run": doc, "label": lab["display"], "spoken": lab["spoken"]}


@app.delete("/protocols/draft")
async def draft_delete(body: DraftStartIn):
    store.clear_draft()
    return {"ok": True, "record_version": store.r["version"]}


@app.get("/health")
async def health():
    return {"ok": True, "commit": git_commit(), "tools": len(TOOLS),
            "prompt_hash": prefix_hash(), "api_version": API_VERSION,
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
