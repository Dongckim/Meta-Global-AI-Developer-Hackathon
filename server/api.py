"""Pip orchestration server: text/voice -> validated calls -> record store.

Local dev only (no Modal/Private modes). Run from repo root:

    uvicorn server.api:app --reload --port 8000

Needs MODEL_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import contextvars
import json
import os
import re
import subprocess
from typing import Any, Literal
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
    join_wells_ranges,
    mark_each_timers,
    match_card_answer,
    match_grammar,
    needs_minutes_ask,
    normalize_text,
    parse_wellref,
    request_parts,
    split_card_answer,
    bare_completion,
    answer_or_filler,
    unfinished,
    unserved_parts,
    relevant_plan_numbers,
    speak_text,
    split_ask,
    timer_set_reply,
    wells_label,
)
from server.memory import Memory, SparkChainMemory, SummaryMemory
from server.screens import IDS as SCREEN_IDS
from server.screens import SCREENS
from server.screens import (PLAIN_ANSWERS, ask_choice, button_match, button_words, buttons_for, card_choices,
                            control_call, look_call, planned_screen, start_call, verbs)
from server import handoff
from server import look as lookmod
from server.screens import guide as _screen_guide
from server.screens import READS as SCREEN_READS
from server import library as lib
from server import protocol as proto
from server import runs as runlog
from server import search
from server.store import RecordStore, describe_well_row
from server import live_asr
from server import tone
from server.grammar import match_session_end
from server.labels import protocol_short
from server.live_session import LiveSender, LiveConversation, device_hints, live_keywords
from server.live_session import settings as live_settings
from server.tone import keys as tone_keys
from server.private_wake import wake_up, weights_gb
from server.tone import run_mode, say, section

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

load_dotenv()

#: Snapshot/action contract version. Bump on any shared/schemas or
#: view/action-shape change; the app compares at startup.
API_VERSION = 23
#: Seconds between keepalives on /events and /voice/live. The app drops a
#: link that stays silent for three of these and reconnects.
HEARTBEAT_S = 15.0
#: Seconds a cut-off live owner turn ("I want to") waits for the rest.
LIVE_HOLD_S = 3.0

BASE_URL = "https://api.meta.ai/v1"
SPARK_MODEL = "muse-spark-1.3"
REASONING_EFFORT = "minimal"  # shortest pass; Spark rejects "none" (HTTP 400)
MAX_OUTPUT_TOKENS = 1024
ASR_MODEL = "muse-voice-transcribe-1.0"
WELL_KEYWORDS = [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)]
KEYWORDS = WELL_KEYWORDS + ["to", "through", "done", "exclude", "redo", "note", "timer", "next", "drug A", "drug B", "pipette", "thirty", "fifteen", "minutes", "wash", "each", "every", "all"]
LANGUAGE_BIAS = ["English"]
TIMEOUT = httpx.Timeout(120.0)
#: Stable per-prefix cache keys (docs: never per-user/per-run) + 24h retention hint.
CACHE_KEY = "pip-bench-spark"
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
    "On a photo answer (photo_answer in state), answer a follow-up from what was read and the "
    "protocol (protocol: liquids, conditions, steps, notes): say plainly whether it fits and why. "
    "Speak at most two short sentences. For a list, say how many and the top one, "
    "and perform navigate to the screen that shows it all; never read a whole list. "
    "Name runs and protocols by their `spoken` labels, never by ids. "
    "After a write, the screen updates itself; never open to confirm. "
    "Ask the user with ask(question) only when you must. "
    "asked: is your last question; this message may answer it. "
    "Emit direct calls in run order; if a later step needs an earlier result, do only the first step now. "
    "For another or backup well, read free_wells first, then act on the first candidate at once (redo: exclude_well + record_redo); the server confirms via cards."
)


def _opt(spec: dict) -> dict:
    """Optional strict field: required but nullable (Meta accepts type arrays)."""
    return {**spec, "type": [spec.get("type", "string"), "null"]}


def _tool(name: str, desc: str, props: dict, required: list | None = None) -> dict:
    """Function tool, no extras. Strict (every prop required, blanks as null)
    unless `required` names the only must-have props: then the rest may be
    left out, so a call carries only what it uses.

    Per dev.meta.ai strict only validates OUR schema; model output is NOT
    constrained, so server-side validation stays as the real gate.
    """
    return {
        "type": "function",
        "name": name,
        "description": desc,
        "strict": required is None,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": sorted(props) if required is None else required,
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
                   "skip_sync", "review_draft", "save_draft", "start_run", "discard_draft", "look", "retake_look",
                   "show_step", "open_source_page", "web_search", "open_source", "control", "pick",
                   "resume_run", "approve_handoff", "save_handoff", "next_change", "retry_handoff",
                   "export_handoff", "set_timing", "choose_mode"]
#: Model actions the turn runs itself, not the store: they leave the turn for
#: their own path (web_search -> _web_search_turn, a card first in Private).
#: Every other PERFORM_ACTIONS entry is the store's (pinned by test).
TURN_ACTIONS = frozenset({"web_search"})
#: Actions that aren't record writes; the coverage check counts only writes.
VIEW_ACTIONS = frozenset(a for a in PERFORM_ACTIONS if a not in RecordStore.RUN_WRITES) \
    - {"edit_draft"} | {"read_record", "ask"}
#: Writes a turn can ask for by their registry verbs (screens.json `actions`).
WRITE_ACTIONS = [a for a in PERFORM_ACTIONS if a in RecordStore.RUN_WRITES]

TOOLS = [
    _tool(
        "perform",
        "Do one bench action per call; emit in run order. "
        "Two requests in one sentence = two calls. "
        "mark_done{wells,step?}: REPORTED finished only (done/finished/added/I put); "
        "step = the plan step number it belongs to (blank = current step); "
        "bench instructions (wash/add/do) record nothing. "
        "exclude_well{well,reason?}; record_redo{wells,note?} (+exclude when elsewhere); "
        "add_note{target,text} (stated observation; nothing to write -> ask); "
        "edit_note{well?,text}; save_tip{step_id,text}; "
        "set_timer{minutes,wells?,on_done?} (minutes stated, else ask); "
        "cancel_timer{label?}; extend_timer{label?,minutes?} (+5 default); "
        "next_step; end_run; show{view?,target?}; navigate{screen,target?}; "
        "open{target} (well/screen/original/importer, or \"run <as said>\" e.g. \"run 2\", "
        "\"run latest of Drug A/B\"; a well is always open{target}); "
        "back; make_robot (a finished run's final protocol, for the robot); "
        "approve_handoff / save_handoff / next_change / retry_handoff / export_handoff (the hand off screens' "
        "Make robot code, Save without code, Next, Fix it, Export); "
        "set_timing{choice: run|plan} (a final protocol step's timer: Keep as run / Use plan); "
        "edit_draft{path,value} (draft_open only); review_draft; save_draft (later); "
        "start_run{target?} (the open draft, else the protocol on screen or named); discard_draft; "
        "show_step{step} / open_source_page{step?} (a draft's pipetting step, 1-based); "
        "look{question?} (question = what was asked; default: the words said) when answering needs seeing something in front of the person (a tube, "
        "label, bottle, screen: \"what does it say\", \"is this the right one\"); the app takes a photo; "
        "web_search{query} ONLY for outside knowledge the record and protocol can't hold (reagent "
        "limits, stability, general method facts); query = the general question, never wells, run "
        "names or notes; record facts -> read_record instead; open{target: \"search <as said>\"} "
        "opens a past search; open_source{n?} on a search answer (a page in the app); "
        "control{cmd,n?} = the screen's own buttons (next_page, page n, zoom_in, scroll_down, take, done...); "
        "pick{n|name} = an item on a list screen (a file); "
        "retry_sync; skip_sync (setup screen only). choose_mode{mode: full|private}. Never answer_card. "
        f"Screens (navigate.screen): {SCREEN_GUIDE} "
        "Backup well: read free_wells first, then act on the first candidate at once.",
        {
            "action": {"type": "string", "enum": PERFORM_ACTIONS},
            "wells": WELLREF,
            "well": WELLREF,
            "target": {"type": "string"},
            "text": {"type": "string"},
            "minutes": {"type": "number"},
            "label": {"type": "string"},
            "note": {"type": "string"},
            "reason": {"type": "string"},
            "screen": {"type": "string", "enum": SCREEN_IDS},
            "step": {"type": "integer"},
            "path": {"type": "string"},
            "value": {"type": "string"},
            "step_id": {"type": "string"},
            "on_done": {"type": "string"},
            "view": {"type": "string"},
            "query": {"type": "string"},
            "cmd": {"type": "string"},
            "name": {"type": "string"},
            "n": {"type": "integer"},
            "mode": {"type": "string", "enum": ["full", "private"]},
        },
        # only what the action uses: a strict call spelled out all 16 props
        # (~250 output tokens each) and three of them overran the cap
        required=["action"],
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
                            "or run selector (run 2/latest/today/yesterday/date); blank for natural scope"}),
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

#: Hand off: Spark writes the robot code with room to think and a whole file.
HANDOFF_EFFORT = "low"
HANDOFF_MAX_TOKENS = 8192
#: One hand off loop per run; a retry replaces the one still going.
_HANDOFFS: dict[str, asyncio.Task] = {}


async def _handoff_task(run_id: str) -> None:
    """The approved run's robot code: Spark writes, the simulator runs, code
    checks (server/handoff.py); progress lands on the run (store)."""
    plan = store.handoff_plan(run_id)
    if not plan:
        return
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async def call(body: dict) -> dict:
            return await _spark_json(client, "handoff", {
                "model": SPARK_MODEL, "reasoning": {"effort": HANDOFF_EFFORT},
                "max_output_tokens": HANDOFF_MAX_TOKENS, **body})

        out = await handoff.run_loop(call, plan, lambda code: asyncio.to_thread(handoff.simulate, code),
                                     lambda stage, **st: store.handoff_stage(run_id, stage, **st))
    print(f"handoff {run_id}: {out['stage']} after {out['attempt']} simulation(s), tips {out['tips']}", flush=True)


def _spawn_handoff(run_id: str) -> None:
    old = _HANDOFFS.get(run_id)
    if old and not old.done():
        old.cancel()
    _HANDOFFS[run_id] = asyncio.get_running_loop().create_task(_handoff_task(run_id))


store.spawn_handoff = _spawn_handoff

#: The whole wake-up, one retry included.
WAKE_TIMEOUT_S = section("private")["wake_timeout_s"]
WAKE_TICK_S = 1.0
WAKE_SPEAK_AFTER_S = section("private")["speak_after_s"]
HF_TREE = "https://huggingface.co/api/models/{repo}/tree/main"
_wake_tasks: set[asyncio.Task] = set()


async def _wake_post() -> None:
    """One token from the private model: its container is up when this returns."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
        resp = await client.post(os.environ["PRIVATE_LLM_URL"].rstrip("/") + "/v1/chat/completions",
                                 json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
        resp.raise_for_status()


async def _weights_sizes() -> dict:
    """{file: bytes} as the model's repo lists them."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(HF_TREE.format(repo=section("private")["repo"]))
        resp.raise_for_status()
    return {f["path"]: f["size"] for f in resp.json()}


async def _learn_weights_size() -> None:
    """Only a line waits on this: no size, no line."""
    try:
        store.wake.gb = store.wake.gb or weights_gb(await _weights_sizes(), section("private")["files"])
    except Exception as e:
        print(f"private wake: size unknown: {type(e).__name__}: {e}", flush=True)


async def _wake_ticks(session: dict, gen: int) -> None:
    """The seconds on screen; Milo speaks only if this isn't over at once."""
    spoke, t0 = False, time.monotonic()
    while True:
        await asyncio.sleep(WAKE_TICK_S)
        store.wake_tick(gen)
        if not spoke and time.monotonic() - t0 >= WAKE_SPEAK_AFTER_S:
            spoke = session["wake_spoke"] = True
            _stage(session, "waking", say=section("private")["waking_line"])


async def _wake_task(session: dict, gen: int) -> None:
    """Wake the private model; a turn parked for it runs after."""
    session["wake_spoke"] = False
    size = asyncio.create_task(_learn_weights_size())
    ticks = asyncio.create_task(_wake_ticks(session, gen))
    try:
        ok = await wake_up(_wake_post, WAKE_TIMEOUT_S)
    finally:
        ticks.cancel()
    await asyncio.gather(size, return_exceptions=True)
    if not store.wake_done(gen, ok, session):
        return
    print(f"private wake: {store.wake.state} in {store.wake.elapsed:.1f}s", flush=True)
    if ok and session.get("wake_spoke"):
        _stage(session, "awake", say=section("private")["awake_line"])
    pending = session.pop("wake_pending", None) if ok else None
    if pending:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            out = await _turn(client, _key(), pending[0], session, pending[1])
        _stage(session, "answer", say=out[2])


def _spawn_wake(session: dict, gen: int) -> None:
    task = asyncio.get_running_loop().create_task(_wake_task(session, gen))
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)


store.spawn_wake = _spawn_wake


class OrchestrateIn(BaseModel):
    text: str = Field(min_length=1)
    state: dict = {}
    session: str = "bench"


class ConfirmIn(BaseModel):
    card_id: str = Field(min_length=1)
    ok: bool
    answer: str = ""  # a choice card's answer (registry `cards`): open / review / version / separate
    session: str = "bench"


class ActionIn(BaseModel):
    id: str = Field(min_length=1)
    args: dict = {}
    #: Where the action came from: a tap answers on screen only (no speech);
    #: voice and the model are answered aloud.
    source: Literal["tap", "voice", "model"] = "tap"


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
    echo_calls: list | None = None, nudge: str = "",
) -> tuple[list, str, int, bool, dict, list]:
    """Returns (calls, text, ms, truncated, usage). Chat calls never search
    (web search is its own call: server/search.py).

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
            req_input.append({"role": "user", "content": nudge or CONTINUE_TEXT})
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
            "prompt_cache_key": CACHE_KEY,
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
            return [], "Sorry, say that again?", ms, True, {}
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
            return [], "Sorry, say that again?", ms, True, usage
        calls, texts = [], []
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
        return calls, "\n".join(texts), ms, False, usage
    raise HTTPException(502, "spark chain would not restart")


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
#: Followup-round nudge: the model picks what is left; code re-checks.
FOLLOWUP_TEXT = ("Do any part of my request that isn't done yet, one call each. "
                 "If everything is done, call nothing.")


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


def _answer_cards(ans: str, session: dict, latest: bool = False):
    """Resolve open cards without a model call. Returns the run_utterance tuple.

    A bare yes/no/cancel answers what Pip asked last (still pending); when
    that is unclear the turn asks which one instead of guessing. `latest`
    (an answer leading a longer turn) takes the most recent of them.
    """
    cards = sorted(store.r["cards"].values(), key=lambda c: c["ts"], reverse=True)
    if ans not in PLAIN_ANSWERS:  # "review again": the card that offers it
        c = next(c for c in cards if ans in card_choices(c["kind"]))
        return _resolve_cards([(c["id"], True, ans)], session)
    if ans == "all":
        targets = [(c["id"], True) for c in cards]
    else:  # yes / no / cancel
        last = session.get("last_asked_cards") or []
        pool = [c for c in cards if c["id"] in last] or cards
        if len(pool) > 1 and not latest:
            reply = _which_one(pool)
            return [], [reply], reply, 0, "card", [], {"dropped": 0, "drop_reasons": []}, []
        targets = [(pool[0]["id"], ans == "yes")]
    return _resolve_cards(targets, session)


def _offered(ans: str | None) -> str | None:
    """A plain answer, or a choice ("open it") an open card offers; else None."""
    if ans in PLAIN_ANSWERS or any(ans in card_choices(c["kind"]) for c in store.r["cards"].values()):
        return ans
    return None


def _resolve_cards(targets: list, session: dict):
    """Answer cards by id ((id, ok) pairs) through the store; no model call."""
    calls = [{"name": "answer_card", "args": {"card_id": t[0], "ok": t[1], "answer": t[2] if len(t) > 2 else ""}}
             for t in targets]
    res = store.apply_calls(calls, "card", "", "card", session)
    reply = _sentences(res["messages"]) or "Done."
    return [], res["messages"], reply, 0, "card", res["cards"], res["info"], []


def search_mode() -> str:
    """The run mode searches follow (tone.run_mode)."""
    return run_mode()


def _confirm_search() -> bool:
    """Ask before a search leaves? One setting per mode (pip_voice.json
    `modes.<mode>.confirm_search`), never a mode branch in code."""
    return bool((section("modes").get(search_mode()) or {}).get("confirm_search"))


async def _web_search_turn(client, key, question: str, query: str, session: dict, ms: int = 0):
    """web_search chosen: clean the query, ask first when the mode says so,
    else search now. Returns the run_utterance tuple."""
    q = search.clean_query(query, store.private_texts())
    info = {"dropped": 0, "drop_reasons": []}
    if not q:
        return [], [], say("search_private", session), ms, "spark", [], info, []
    if _confirm_search():
        card = store.ask_search(question, q, say("confirm_search", session, query=q))
        return [], [], card["question"], ms, "spark", [card], info, []
    reply, sources = await _run_search(client, key, question, q, session)
    return [{"name": "web_search", "args": {"query": q}}], [], reply, ms, "spark", [], info, sources


STILL_LOOKING_S = 6.0


def _stage(session: dict, stage: str, **data) -> None:
    """The one place a turn says where it is (shared/schemas/live.schema.json
    `stage`): heard -> thinking -> searching -> summarizing -> done | failed.
    A live session's turns go on its socket, every other turn on /events;
    the app sets its state from these and never guesses."""
    msg = {"type": "stage", "stage": stage, **{k: v for k, v in data.items() if v not in (None, "")}}
    sink = session.get("stage_sink")
    if sink is not None:
        sink(msg)
    else:
        store._broadcast({"kind": "stage", **msg})


async def _search_calls(client, key, question: str, query: str, session: dict):
    """The search call, then the tidy call. (result | None, status)."""
    t0 = time.perf_counter()
    resp = await asyncio.wait_for(
        _post(client, "search", f"{BASE_URL}/responses",
              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
              json=search.build_body(query, SPARK_MODEL)),
        search.SEARCH_TIMEOUT_S)
    _check("search", resp)
    got = search.found(resp.json())
    if not got:
        return None, "no_source"
    _stage(session, "summarizing")
    # a no-search call tidies the answer from its text and source list only
    left = search.SEARCH_TIMEOUT_S - (time.perf_counter() - t0)
    tidy = await asyncio.wait_for(
        _post(client, "search tidy", f"{BASE_URL}/responses",
              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
              json=search.build_summary_body(question, got, SPARK_MODEL)), max(left, 1.0))
    return search.tidy(search.output_json(tidy.json()) if tidy.status_code == 200 else None, got), "ok"


async def _run_search(client, key, question: str, query: str, session: dict) -> tuple[str, list]:
    """Send one search: ⑧ opens on it at once (pending), "Looking it up."
    now and "Still looking…" once after 6 s; "stop" cancels it. Every sent
    search is kept. (reply, sources)."""
    rec = search.record(question, query, None, "pending", (store.r.get("run") or {}).get("run_id"), 0)
    store.add_search(rec)
    store.show_search(rec["id"])
    _stage(session, "searching", query=query, search_id=rec["id"], say=say("looking_up", session))

    async def still():
        await asyncio.sleep(STILL_LOOKING_S)
        _stage(session, "searching", query=query, search_id=rec["id"], say=say("still_looking", session))

    nudge = asyncio.create_task(still())
    task = asyncio.ensure_future(_search_calls(client, key, question, query, session))
    session["search_task"] = task
    t0 = time.perf_counter()
    result = None
    try:
        result, status = await task
    except asyncio.TimeoutError:
        status = "timeout"
    except HTTPException:
        status = "error"
    except asyncio.CancelledError:
        if not task.cancelled():
            raise  # this turn itself was cancelled
        status = "canceled"
    finally:
        nudge.cancel()
        session.pop("search_task", None)
    ms = int((time.perf_counter() - t0) * 1000)
    line = "" if status == "ok" else \
        say({"timeout": "search_slow", "canceled": "search_stopped"}.get(status, "search_no_source"), session)
    rec = store.finish_search(rec["id"], result, status, ms, line)
    print(f"search status={status} ms={ms} sources={len(rec['sources'])} q={query[:60]!r}", flush=True)
    if status != "ok":
        _stage(session, "failed", reason=status, search_id=rec["id"], reply=line)
        return line, []
    session["last_web"] = f"{rec['answer_short']}. {rec['detail']}"  # "attach it as a tip"
    return store.screen_line("web_answer", rec["id"]), rec["sources"]


def _stop_search(session: dict, text: str) -> bool:
    """"stop" while a search runs cancels the search (not the session)."""
    task = session.get("search_task")
    if task is None or task.done() or task.cancelling() \
            or match_session_end(text, tone.agent_name()) != "stop":
        return False
    task.cancel()
    return True


async def _turn_search(client, query: str, session: dict) -> tuple:
    """Try again (a tap): the search path with its stages."""
    out = await _web_search_turn(client, _key(), query, query, session)
    _stage(session, "done", reply=out[2])
    return out


async def _turn(client, key, text: str, session: dict, source: str) -> tuple:
    """One owner turn with its stages around it (heard ... done/failed)."""
    _stage(session, "heard", text=text)
    token = _TURN_DROPS.set([])  # this turn's drops only (a new list every turn)
    try:
        out = await run_utterance(client, key, text, session, source)
        out = await _finish_search(client, key, out, session)
        out = (*out[:2], _user_facing(out[2], session), *out[3:])
    except Exception as e:
        _stage(session, "failed", reason=type(e).__name__)
        raise
    finally:
        _TURN_DROPS.reset(token)
    if out[6].get("look"):  # a photo question: the turn waits for the photo (POST /look)
        _stage(session, "looking", search_id=out[6]["look"]["search_id"])  # its line is the reply's, said once
        return out
    _stage(session, "done", reply=out[2])
    return out


#: "Search it" and its kin, said on a photo answer.
_SEARCH_IT = re.compile(r"(?:search|look up|google) (?:it|that|this|for it|for that)(?: up| online| on the web)?|"
                        r"look (?:it|that|this) up|search the web|search online")


def _photo_read_here() -> str:
    """On a photo answer: what the photo was read as ("" elsewhere)."""
    cur = store.r["view"]["current"]
    if cur.get("screen") != "web_answer":
        return ""
    doc = next((d for d in store.searches() if d["id"] == cur.get("target")), None)
    if not doc or doc.get("kind") != "photo" or doc.get("status") != "ok":
        return ""
    return " ".join(x for x in (doc.get("answer_short"), doc.get("detail")) if x).strip()


def whole_here(text: str) -> bool:
    """Said whole for this screen: the words (lead-ins and the agent's name
    dropped) are exactly a button on screen or one of its bare action's
    verbs, a card answer, or one of the screen's controls ("page 2")."""
    said = button_words(text)
    if not said:
        return False
    v = store.r["view"]
    cur = v["current"]
    shown = [x for x in ((v.get("sheet") or {}).get("name"), cur["screen"]) if x]
    slots = store.view_slots(cur["screen"], cur.get("target") or "")
    words = set()
    for b in (b for sid in shown for b in buttons_for(sid, slots) if not b.get("tap_only")):
        words.add(button_words(b["say"]))
        if not b.get("args"):
            words |= {button_words(x) for x in verbs(b["action"])}
    return said in words or match_card_answer(said) is not None \
        or control_call(text, cur["screen"], store.screen_items.get(cur["screen"])) is not None


def live_unfinished(text: str, session: dict) -> bool:
    """Hold a live turn for the rest (LIVE_HOLD_S)? Never when it's said whole
    for this screen ("yes", "file"); else the grammar's cut-off rule ("add",
    "I want to")."""
    if whole_here(text):
        return False
    held = unfinished(text, store.prompt_state(session), tone.agent_name())
    if held:
        print(f"live unfinished: {text!r} (words={button_words(text)!r} screen="
              f"{store.r['view']['current']['screen']})", flush=True)
    return held


async def _finish_search(client, key, out: tuple, session: dict) -> tuple:
    """A yes to the search card: run the search it asked about."""
    calls, messages, reply, ms, route, cards, info, sources = out
    s = info.get("search")
    if not s:
        return out
    reply, sources = await _run_search(client, key, s["question"], s["query"], session)
    return calls, messages, reply, ms, route, cards, info, sources


def _sentences(lines) -> str:
    """Reply lines joined as whole sentences: a line without an end mark gets
    a period, so two results never read as one ("C3 excluded. Drop C4?")."""
    out = [x.strip() for x in lines if isinstance(x, str) and x.strip()]
    return " ".join(x if x[-1] in ".!?…" else x + "." for x in out)


#: Args an action can't run without. A missing one becomes ask(), never a
#: failure line ("I couldn't open the null screen").
REQUIRED_ARGS = {
    "mark_done": ["wells"], "exclude_well": ["well"], "record_redo": ["wells"],
    "add_note": ["target", "text"], "edit_note": ["text"],
    "save_tip": ["step_id", "text"], "set_timer": ["minutes"],
    "navigate": ["screen"], "choose_mode": ["mode"], "open": ["target"], "edit_draft": ["path", "value"],
    "web_search": ["query"], "control": ["cmd"],
}
ASK_FOR = {
    "wells": "Which wells?", "well": "Which well?", "target": "Which one?",
    "text": "What should it say?", "step_id": "Which step?",
    "minutes": "How many minutes?", "screen": "Which screen?",
    "path": "Which field?", "value": "What value?",
}


#: An action's argument a viewed thing fills when the model names no target:
#: kind -> {action: (arg, as a list?)}.
VIEWING_ARGS = {
    "well": {"add_note": ("target", False), "exclude_well": ("well", False), "record_redo": ("wells", True),
             "mark_done": ("wells", True)},
    "step": {"show_step": ("step", False), "open_source_page": ("step", False), "save_tip": ("step_id", False)},
}
#: How `open` names a viewed thing that has no `viewing.open` of its own.
_OPEN_AS = {"run": "run:{id}", "protocol": "protocol:{id}", "search": "search:{id}", "look": "search:{id}",
            "well": "{id}"}


def _from_viewing(action: str, args: dict) -> tuple[str, dict]:
    """A model action that names no target takes what the screen is about
    (registry `viewing`): a well card's note goes on that well, "open this"
    on a search answer opens its source. Anything named stays as named."""
    vw = store.viewing()
    if vw is None:
        return action, args
    fill = VIEWING_ARGS.get(vw["kind"], {}).get(action)
    if fill and fill[0] not in args:
        return action, {**args, fill[0]: [vw["id"]] if fill[1] else vw["id"]}
    if action == "open" and "target" not in args:
        screen = store.r["view"]["current"].get("screen") or ""
        own = (SCREENS.get(screen, {}).get("viewing") or {}).get("open")
        if own and vw["kind"] != "well":
            return own["action"], {**(own.get("args") or {}), **({"step": vw["id"]} if vw["kind"] == "step" else {})}
        if vw["kind"] in _OPEN_AS:
            return action, {"target": _OPEN_AS[vw["kind"]].format(id=vw["id"])}
    return action, args


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
        action, rest = _from_viewing(action, rest)  # "this" / "here": what the screen is about
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
    if _stop_search(session, text):
        return [], [], "", 0, "grammar", [], {"dropped": 0, "drop_reasons": []}, []
    if store.r["cards"]:
        ans = _offered(match_card_answer(text))
        if ans is not None:
            return _answer_cards(ans, session)
        ans, rest = split_card_answer(text)
        if _offered(ans) is not None:
            # "Yes. Exclude C6.": the answer goes to the latest card, the rest is a new turn
            head = _answer_cards(ans, session, latest=True)
            if answer_or_filler(rest) or (not store.r.get("run") and bare_completion(rest)):
                return head  # "Yes, um." / "Yep, finished." with the run closed: nothing left
            tail = await run_utterance(client, key, rest, session, source)
            return (tail[0], head[1] + tail[1], _sentences([head[2], tail[2]]), tail[3],
                    tail[4], head[5] + tail[5], tail[6], tail[7])
    elif match_card_answer(text) in PLAIN_ANSWERS and not ((session.get("turns") or [{}])[-1].get("asked")):
        # "yes" with no card and no question of ours pending: nothing to answer
        reply = say("nothing_to_confirm", session)
        return [], [], reply, 0, "grammar", [], {"dropped": 0, "drop_reasons": []}, []
    steps = store.r["plan"]["steps"]
    if store.r.get("run") and steps and store.r["step_index"] >= len(steps) - 1 \
            and bare_completion(text):
        # "Finished." on the last step ends the run, never marks wells: answer
        # an open end-run card, else ask with one
        end = next((c for c in sorted(store.r["cards"].values(), key=lambda c: c["ts"], reverse=True)
                    if (c.get("payload") or {}).get("op") == "end_run"), None)
        if end is not None:
            return _resolve_cards([(end["id"], True)], session)
        calls = [{"name": "end_run", "args": {}}]
        res = store.apply_calls(calls, source, text, "grammar", session)
        reply = _compose_reply(calls, res, store.prompt_state(session), session)
        return calls, res["messages"], reply, 0, "grammar", res["cards"], res["info"], []
    v = store.r["view"]
    shown = [x for x in ((v.get("sheet") or {}).get("name"), v["current"]["screen"]) if x]
    vslots = store.view_slots(v["current"]["screen"], v["current"].get("target") or "")
    b, which = button_match(text, shown, vslots)
    if b is None and not which and planned_screen(text):
        # a registry screen that isn't built yet (a button on screen comes first): say so, no model call
        return [], [], say("not_ready", session), 0, "grammar", [], {"dropped": 0, "drop_reasons": []}, []
    parts = request_parts(text, WRITE_ACTIONS)
    b = b or (None if which else control_call(text, v["current"]["screen"],
                                              store.screen_items.get(v["current"]["screen"])) or start_call(text)
                             or look_call(text))
    if b is None and not which and v["current"]["screen"] in ("home", "protocol_detail") \
            and (pid := store.protocol_by_title(button_words(text))):
        b = {"name": "open", "args": {"target": f"protocol:{pid}"}}  # a row's title said: it opens (done: refused)
    # what the turn was matched against (iPad: "Continue" did nothing)
    print(f"[turn] screen={v['current']['screen']}:{v['current'].get('target') or ''} "
          f"buttons={[x['say'] for sid in shown for x in buttons_for(sid, vslots)]} "
          f"said={text!r} as_button={button_words(text)!r} -> {b['name'] if b else 'ask' if which else 'none'}",
          flush=True)
    if which:  # two of this screen's buttons in one breath: ask which, no model
        asked = {"question": which, "target": ""}
        return [], [], which, 0, "grammar", [], {"dropped": 0, "drop_reasons": [], "asked": asked}, []
    # grammar claims a whole turn or nothing: two requests go to the model
    g = ([b], "") if b else None if len(parts) > 1 else match_grammar(text, store.prompt_state(session))
    if b is None and not which and (read := _photo_read_here()) and _SEARCH_IT.fullmatch(button_words(text)):
        # "search it" on a photo answer: search what the photo said
        return await _web_search_turn(client, key, read, read, session)
    if b is not None and b["name"] == "web_search":  # Try again, said
        out = await _web_search_turn(client, key, b["args"].get("query") or text,
                                     b["args"].get("query") or text, session)
        return (*out[:4], "grammar", *out[5:])
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
    if run_mode() == "private" and not store.wake.fresh():  # asleep: this turn runs once it's up
        return [], [], store.park_for_private(text, session, source), 0, "wait", [], \
            {"dropped": 0, "drop_reasons": []}, []
    summary = evidence_summary(session.get("turns"))
    _stage(session, "thinking")
    calls, texts, ms, cut, _ = await _call_spark(
        client, key, text, store.model_state(session), session)
    if cut:
        return [], [], say("didnt_catch", session), ms, "spark", [], {"dropped": 0, "drop_reasons": []}, []
    raw_count = len(calls)
    model_raw = [dict(c) for c in calls]
    calls = _expand_perform(calls)
    ws = next((c for c in calls if c.get("name") in TURN_ACTIONS), None)
    if ws is not None:
        # outside knowledge: one separate search call (server/search.py), nothing else this turn
        return await _web_search_turn(client, key, text, (ws.get("args") or {}).get("query") or text,
                                      session, ms)
    if raw_count and not calls and not texts.strip():
        # every call was unusable (no action / no name): ask, offering the
        # registry's best-matching screens instead of guessing
        q = ask_choice(text) or say("didnt_catch", session)
        print(f"spark calls unusable for {text[:60]!r}: asking", flush=True)
        asked = {"question": q, "target": ""}
        return [], [], q, ms, "spark", [], {"dropped": raw_count, "drop_reasons": [], "asked": asked}, []
    sources: list = []
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
        if texts.strip() and _no_promises(_short_text(texts)):
            t = _no_promises(_short_text(texts))
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
        if more is not None and more[0] == "turn":
            return await _web_search_turn(client, key, text, (more[1].get("args") or {}).get("query") or text,
                                          session, ms)
        if more is not None:
            rc, res2, question2, ordered2, outcomes2, gdrop2, t2 = more
            if question is None and question2 is None and pre_all_reads and not res2["cards"]:
                nav_only = all(c.get("name") in ("navigate", "open", "back", "show") for c in rc)
                line = _no_promises(_short_text(t2)) if t2.strip() and (not rc or nav_only) else ""
                # a summary names something it read (a well or a count);
                # "pulling up your notes" names nothing, so the read is said
                if line and (find_well_mentions(line) & find_well_mentions(reads_text)
                             or re.search(r"\d", line) or not read_lines):
                    model_line = line
                elif rc and nav_only and read_lines:
                    model_line = _executed_reply(calls, read_lines, session)
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
            _merge_round(res, res2)
            if question2 is not None:
                question = question2
            if res2["info"].get("timer_started"):
                gdrop = gdrop2
            else:
                gdrop = gdrop or gdrop2
            _store_outputs(session, ordered2, outcomes2)
    also = ""
    missing = unserved_parts(parts, calls, VIEW_ACTIONS) if question is None and not gdrop else []
    if missing:
        # one followup round, only when a requested part didn't run
        session["pending_outputs"] = [{"call_id": c["call_id"], "output": outcomes[c["call_id"]]}
                                      for c in model_raw if c.get("call_id") in outcomes]
        ms3, more = await _lookup_round(client, key, text, session, source, plan_allowed, "",
                                        summary, model_raw, nudge=FOLLOWUP_TEXT)
        ms += ms3
        if more is not None and more[0] == "turn":
            return await _web_search_turn(client, key, text, (more[1].get("args") or {}).get("query") or text,
                                          session, ms)
        if more is not None:
            rc, res3, question3, ordered3, outcomes3, gdrop3, _ = more
            calls = calls + rc
            _merge_round(res, res3)
            question = question3
            gdrop = gdrop or gdrop3
            _store_outputs(session, ordered3, outcomes3)
        still = unserved_parts(parts, calls, VIEW_ACTIONS) if question is None and not gdrop else []
        print(f"[orch] followup ms={ms3} missing={[p['text'] for p in missing]} "
              f"filled={[c.get('name') for c in (more[0] if more else [])]} "
              f"still={[p['text'] for p in still]}", flush=True)
        if still:
            what = " and ".join(p["text"][:1].lower() + p["text"][1:] for p in still)
            also = say("also_ask", session, what=what)
    calls = _strip_ids(calls)
    asked: dict = {}
    if question is not None:
        reply = question
        is_attach = "attach" in question.lower() and bool(session.get("last_web"))
        asked = {"question": question, "target": "" if is_attach else (recent_target() or "")}
    elif gdrop:
        base = _compose_reply(calls, res, store.prompt_state(session), session)
        q = say("ask_minutes", session)
        reply = q if not base else base + " " + q
        asked = {"question": q, "target": recent_target() or ""}
    else:
        # Store results are the reply and the model's line never joins them;
        # but a read answer (reads, then only a screen move) is the summary,
        # and the move's "Here's ..." line is left out (the screen shows it).
        base = _compose_reply(calls, res, store.prompt_state(session), session)
        failed = any(is_failure_message(m) for m in res["messages"])
        reply = model_line if model_line and not failed else base or model_line
        if not reply:
            reply = _executed_reply(calls, read_lines, session)
        if any(d[0] == "read_record" for d in _TURN_DROPS.get() or []):
            # a read dropped this turn: say what couldn't be read, never that it's being done
            reply = f"{reply} {_couldnt_now(session)}".strip()
        if not reply:
            print(f"ORPHAN REPLY: {sorted({c.get('name') for c in calls})} produced nothing",
                  flush=True)
            reply = say("found_empty", session)
        if also:
            # what ran is said first, then the part that didn't, as a question
            reply = f"{reply} {also}"
            wells = sorted({w for p in still for w in p["wells"]})
            asked = {"question": also, "target": wells_label(wells) if wells else ""}
    for m in res["messages"]:
        if m.startswith("Note updated.") and res["info"].get("edited"):
            reply = f"Note updated ({res['info']['edited']['target']})."
    if asked:
        res["info"]["asked"] = asked
    return calls, res["messages"], reply, ms, "spark", res["cards"], res["info"], sources


def _merge_round(res: dict, res2: dict):
    """Fold a later round's store results into the turn's."""
    res["messages"] += res2["messages"]
    res["cards"] += res2["cards"]
    res["outcomes"] += res2["outcomes"]
    res["ui"] += res2["ui"]
    res["info"]["ui"] = res["ui"]
    res["info"]["dropped"] += res2["info"]["dropped"]
    res["info"]["drop_reasons"] += res2["info"]["drop_reasons"]
    res.setdefault("told", []).extend(res2.get("told") or [])
    for k in ("saved_memo", "edited", "setup_done", "sync_retry"):
        if res2["info"].get(k):
            res["info"][k] = res2["info"][k]


async def _lookup_round(client, key, text, session, source, plan_numbers, reads_text, summary,
                      r1raw, nudge=""):
    """Second spark round replaying round 1 inline (question, calls, outputs).

    A bare repeated question restarts the lookup; outputs without their
    calls don't bind. The replay mirrors the chained shape without storing.
    Returns (ms, None-or-tuple).
    """
    rc, t2, ms, cut, _ = await _call_spark(
        client, key, text, store.model_state(session), session, lookup=True, echo_calls=r1raw,
        nudge=nudge)
    if cut:
        return ms, None
    rc = _expand_perform(rc)
    turn_calls = [c for c in rc if c.get("name") in TURN_ACTIONS]
    if turn_calls:  # e.g. web_search chosen after a read: the search path, never the store
        return ms, ("turn", turn_calls[0])
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


#: What this turn dropped (name, args, reason): the reply says what it couldn't do,
#: never why in the server's words.
_TURN_DROPS: contextvars.ContextVar = contextvars.ContextVar("turn_drops", default=None)


def _log_drop(name, args, reason: str):
    blob = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)[:300]
    print(f"drop {name} args={blob} reason={reason}", flush=True)
    drops = _TURN_DROPS.get()
    if drops is not None:
        drops.append((name, args if isinstance(args, dict) else {}, reason))


#: The server's own words for why something was dropped: logged, never said.
INTERNAL_REASONS = ("unknown tool", "lookup depth", "tool without a name", "no action", "unbound",
                    "call_id", "pending confirmation")
#: A model sentence promising work after the answer ("Checking the protocol…"):
#: a turn is over when it answers, so the promise is never kept.
_PROMISE = re.compile(r"\b(let me|i'll|i will|i'm going to|i am going to|checking|looking (?:it )?up|"
                      r"looking into|pulling (?:it |that )?up|one (?:moment|sec)|give me a (?:moment|sec)|"
                      r"hang on|hold on)\b", re.I)


def _sentence_split(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if x]


def _no_promises(line: str) -> str:
    """The model's own line without sentences promising work the turn didn't do."""
    keep = []
    for s in _sentence_split(line):
        if _PROMISE.search(s):
            print(f"[reply] cut promise {s!r}", flush=True)
            continue
        keep.append(s)
    return " ".join(keep)


def _couldnt_now(session) -> str:
    """What to say when this turn dropped a tool: a read of the record or
    protocol -> couldn't open it; anything else -> couldn't do it."""
    drops = _TURN_DROPS.get() or []
    kinds = {str((a or {}).get("kind") or "") for n, a, _ in drops if n == "read_record"}
    if kinds & {"source", "steps", "protocols"}:
        return say("couldnt_open", session, what="the protocol")
    if kinds:
        return say("couldnt_open", session, what="the record")
    return say("couldnt_now", session)


def _user_facing(reply: str, session) -> str:
    """The last check on what Milo says: a sentence holding the server's own
    reasons ("unknown tool") gives way to a line a person can act on."""
    keep, cut = [], False
    for s in _sentence_split(reply):
        if any(r in s.lower() for r in INTERNAL_REASONS):
            print(f"[reply] cut internal {s!r}", flush=True)
            cut = True
            continue
        keep.append(s)
    if not cut:
        return reply
    line = _couldnt_now(session)
    return " ".join(keep + [line]) if keep else line


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
    msgs = _fold_told([m for m in res["messages"] if isinstance(m, str) and m],  # None/blank never joins
                      res.get("told") or [], session)
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
    return _sentences(parts)


def _fold_told(msgs: list, told: list, session) -> list:
    """One sentence per action: same-key, same-slot well lines (timer on
    C2, C3, C4) become one line over the joined range ("on C2–C4")."""
    groups: dict = {}
    for t in told:
        groups.setdefault((t["key"], json.dumps(t["slots"], sort_keys=True)), []).append(t)
    out = list(msgs)
    for (key, _), ts in groups.items():
        if len(ts) < 2 or any(t["line"] not in out for t in ts):
            continue
        at = out.index(ts[0]["line"])
        for t in ts:
            out.remove(t["line"])
        wells = [w for t in ts for w in t["wells"]]
        out.insert(min(at, len(out)), say(key, session, wells=join_wells_ranges(wells), **ts[0]["slots"]))
    return out


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
    for k in ("saved_memo", "edited", "ui", "setup_done",
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


def _quiet_tap(source: str, out: dict) -> dict:
    """A tap is answered on screen (reply, header) but never aloud, even in a
    live voice session; voice and the model keep `speak`."""
    if source == "tap":
        out.pop("speak", None)
    return out


async def _handle_text(text: str, session: dict, source: str):
    """Shared text path for /input and the /orchestrate alias."""
    key = _key()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        out = await _turn(client, key, text, session, source)
    calls, messages, reply, ms, route, cards, info, sources = out
    print(f"orch route={route} calls={_call_names(calls)} reply={reply[:80]!r}", flush=True)
    session["last_asked_cards"] = [c["id"] for c in cards]
    log_turn(session, text, calls, messages or ([reply] if reply else []),
             asked=info.get("asked"))
    return _utterance_response(calls, messages, reply, {"spark": ms}, route, cards, info, sources,
                               store.r["version"])


def _resolve_card(card_id: str, answer: str, session: dict, choice: str = ""):
    """Shared card path for /input and the /confirm alias."""
    ok = answer.strip().lower() not in ("no", "cancel", "false", "0", "dismiss")
    message, new_cards, _ = store.confirm(card_id, ok, "card", session, choice)
    return message, new_cards


@app.post("/input")
async def input_entry(body: InputIn):
    session = get_session(body.session)
    if body.action is not None and body.action.id == "web_search":
        q = str((body.action.args or {}).get("query") or "")
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            calls, messages, reply, ms, route, cards, info, sources = await _turn_search(client, q, session)
        return _quiet_tap(body.action.source, _utterance_response(
            calls, messages, reply, {"spark": ms}, "tap", cards, info, sources, store.r["version"]))
    if body.action is not None:
        src = body.action.source
        calls = [{"name": body.action.id, "args": body.action.args or {}}]
        calls = expand_tool_wells(calls)
        res = store.apply_calls(calls, src, "", src, session)
        reply = _compose_reply(calls, res, store.prompt_state(session), session)
        if not reply:
            reply = _executed_reply(calls, [], session)
        sources = []
        if res["info"].get("search"):  # tapped yes on the search card
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                reply, sources = await _run_search(client, _key(), res["info"]["search"]["question"],
                                                   res["info"]["search"]["query"], session)
        log_turn(session, f"{src}:{body.action.id}", calls, [reply] if reply else [])
        return _quiet_tap(src, _utterance_response(calls, res["messages"], reply, {"spark": 0}, "tap",
                                                   res["cards"], res["info"], sources, store.r["version"]))
    if body.card_id:
        a = body.answer or "yes"  # a choice card's answer ("review") is a yes with that choice
        message, new_cards = _resolve_card(body.card_id, a, session, "" if a in PLAIN_ANSWERS else a)
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
async def voice(audio: UploadFile | None = File(None), transcript: str = Form(""),
                state: str = Form("{}"), session: str = Form("bench")):
    total0 = time.perf_counter()
    if run_mode() == "private":  # heard on the iPad; audio sent anyway stops here
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await _voice_turn(client, _key(), transcript, get_session(session), 0, total0)
    if audio is None:
        raise HTTPException(400, "audio required")
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
        print(f'voice dur={dur:.2f}s transcript="{transcript}" norm="{normalize_text(transcript)}"', flush=True)
        return await _voice_turn(client, key, transcript, sess, asr_ms, total0)


async def _voice_turn(client: httpx.AsyncClient, key: str, transcript: str, sess: dict,
                      asr_ms: int, total0: float) -> dict:
    """One /voice turn from its transcript, wherever it was heard."""
    if not normalize_text(transcript).strip(" .?!"):
        return {
            "transcript": "",
            "tool_calls": [],
            "messages": [],
            "reply": say("didnt_catch", sess),
            "ms": {"asr": asr_ms, "spark": 0, "total": int((time.perf_counter() - total0) * 1000)},
        }
    out = await _turn(client, key, transcript, sess, "voice")
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


def _device_hints() -> list[str]:
    """What the iPad's recognizer should expect to hear on this screen."""
    v = store.r["view"]
    shown = [x for x in ((v.get("sheet") or {}).get("name"), v["current"]["screen"]) if x]
    slots = store.view_slots(v["current"]["screen"], v["current"].get("target") or "")
    return device_hints([b["say"] for sid in shown for b in buttons_for(sid, slots)],
                        [protocol_short(d) for d in lib.all_docs()])


@app.websocket("/voice/live")
async def voice_live(ws: WebSocket):
    """Live conversation relay (shared/schemas/live.schema.json): app PCM16
    16 kHz frames -> Meta realtime (DIARIZATION), or in Private the words the
    iPad heard (`heard`), no audio; partials and ignored turns go back as
    they come; owner turns run the one text path (_handle_text) and the reply
    returns as a `turn` message (record changes ride SSE). The mode changing
    closes the session with why `mode`; the app reopens it the new way."""
    await ws.accept()
    sender = LiveSender(ws)
    start = await ws.receive_json()
    session = get_session((start or {}).get("session") or "bench")
    mode = run_mode()
    device = mode == "private"
    meta = live_asr.DeviceStream() if device else live_asr.MetaStream(
        _key(), mode="DIARIZATION", keywords=live_keywords(store.r), language_bias=["English"])
    hints = _device_hints() if device else []
    try:
        ok = await meta.open()
    except Exception as e:  # network, handshake timeout
        ok, meta.error = False, {"message": f"{type(e).__name__}: {e}"}
    if not ok:
        print(f"live open failed: {meta.error}", flush=True)
        await sender.send({"type": "error", "message": (meta.error or {}).get("message", "open failed")})
        await meta.aclose()
        await sender.close()
        return
    print(f"live open session={meta.session_id}", flush=True)
    await sender.send({"type": "ready", "session_id": meta.session_id,
                       "transcribe": "device" if device else "server", "hints": hints})
    conv = LiveConversation(opened_at=time.monotonic(),
                            window_s=float(live_settings().get("window_s") or 60),
                            max_s=float(live_settings().get("max_s") or 55 * 60),
                            wake_phrase=tone.persona().get("wake_phrase") or "")
    owner_q: asyncio.Queue = asyncio.Queue()
    ended = False
    why = ""
    busy = False  # an owner turn is running
    # this session's turn stages ride its socket (_stage)
    session["stage_sink"] = lambda m: asyncio.ensure_future(sender.send(m))

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
                if not ended and not device and run_mode() == mode:
                    await meta.send_audio(msg["bytes"])
                continue
            body = json.loads(msg.get("text") or "{}")
            if body.get("type") == "heard" and device:
                meta.heard(str(body.get("text") or ""), bool(body.get("final")))
            elif body.get("type") == "tts":
                conv.tts(bool(body.get("speaking")), time.monotonic())
            elif body.get("type") == "end":
                break
        await end_stream("client")

    async def downlink():
        async for ev in meta.events():
            for out in conv.feed(ev, time.monotonic()):
                if out["type"] == "owner" and _stop_search(session, out["text"]):
                    print("live: search stopped", flush=True)
                    continue
                if out["type"] in ("owner", "here"):
                    owner_q.put_nowait(out)
                    continue
                if out["type"] == "ignored":
                    print(f'[voice] ignored speaker={out["speaker"]} text="{out["text"]}"', flush=True)
                await sender.send(out)

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
            line = say("session_thanks", session, wake=tone.persona().get("wake_phrase") or "")
            return text, short(line if end == "thanks" else "", "grammar"), end
        return text, await _handle_text(text, session, "voice"), None

    async def next_owner_item(held: str) -> dict:
        """The next owner turn, with a held cut-off turn joined in front; a
        held turn alone after LIVE_HOLD_S of silence."""
        if not held:
            return await owner_q.get()
        try:
            item = await asyncio.wait_for(owner_q.get(), LIVE_HOLD_S)
        except asyncio.TimeoutError:
            return {"type": "owner", "text": held, "flush": True}
        if item["type"] != "owner":
            return item
        return {**item, "text": f"{held} {item['text']}"}

    async def worker():
        nonlocal busy
        held = ""
        while True:
            item = await next_owner_item(held)
            held = ""
            if item["type"] == "owner" and not item.get("flush") and live_unfinished(item["text"], session):
                held = item["text"]  # "I want to see": wait for the rest
                print(f'live hold: "{held}"', flush=True)
                continue
            busy = True
            try:
                text, resp, end = await owner_turn(item)
            except Exception as e:  # any failure: the session stays, the turn says sorry
                print(f"live turn failed: {type(e).__name__}: {e}", flush=True)
                text, resp, end = item.get("text", ""), short(say("didnt_catch", session), "error"), None
            await sender.send({"type": "turn", "transcript": text, "end_session": end is not None, **resp})
            busy = False
            conv.owner_replied(time.monotonic())
            if end is not None:
                await end_stream("end_session")

    async def heartbeat():
        """A ping while nothing else flows, so the app can tell a quiet
        session from a dead socket."""
        while not ended:
            await asyncio.sleep(HEARTBEAT_S)
            await sender.send({"type": "ping"})

    async def watchdog():
        nonlocal hints
        while not ended:
            await asyncio.sleep(1)
            if owner_q.empty() and not busy and run_mode() != mode:
                await end_stream("mode")
                break
            reason = conv.should_close(time.monotonic()) if owner_q.empty() else None
            if reason:
                await end_stream(reason)
            elif device and (now := _device_hints()) != hints:
                hints = now
                await sender.send({"type": "hints", "hints": hints})

    tasks = [asyncio.create_task(c()) for c in (uplink, worker, watchdog, heartbeat)]
    try:
        await downlink()  # ends when Meta closes (1000 after endStream, or an error)
    finally:
        session.pop("stage_sink", None)
        for t in tasks:
            t.cancel()
        code, reason = meta.close
        # why: window / max_duration / end_session / client, else the stream broke
        why = why or ("error" if code != 1000 or meta.error else "server")
        print(f"live closed {code} {reason!r} why={why}", flush=True)
        await sender.send({"type": "closed", "code": code, "reason": reason, "why": why})
        await sender.close()
        await meta.aclose()


@app.post("/confirm")
async def confirm(body: ConfirmIn):
    """Alias: forwards into /input's card path (same shapes)."""
    session = get_session(body.session)
    message, new_cards = _resolve_card(body.card_id, "yes" if body.ok else "no", session, body.answer)
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


_IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "heic": "image/heic"}


async def _spark_json(client, name: str, body: dict) -> dict:
    resp = await _post(client, name, f"{BASE_URL}/responses",
                       headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
                       json=body)
    _check(name, resp)
    return resp.json()


#: Extractions in flight by content hash: a second request for the same
#: content joins the first instead of extracting again.
_IMPORTS: dict[str, asyncio.Future] = {}


#: A photo for a photo question, at most (the iPad camera's full JPEG fits).
LOOK_MAX_BYTES = 15 * 1024 * 1024


@app.post("/look")
async def look_photo(file: UploadFile = File(...), search_id: str = Form(...), session: str = Form("bench"),
                     attempt: int = Form(0)):
    """The photo for a photo question (server/look.py): SAM 3.1 finds the
    thing asked about, only its crop is kept and goes to Spark, which reads
    it and answers; if the answer needs outside facts, the web search path
    runs on what was read. Not found: the camera stays ("Try again closer").
    `attempt` is the photo's turn (Retake moves it on): a reply for an
    attempt that's no longer current changes nothing (stale: true)."""
    sess = get_session(session)
    rec = store.search_item(search_id)
    if rec is None or rec.get("kind") != "photo":
        raise HTTPException(404, "unknown photo question")
    a = attempt or rec.get("attempt", 1)
    if a != rec.get("attempt", 1):
        return _look_stale()
    if rec.get("status") != "looking":
        raise HTTPException(409, f"photo question is {rec.get('status')}")
    raw = await file.read()
    if not raw or len(raw) > LOOK_MAX_BYTES:
        raise HTTPException(413 if raw else 400, "need one photo (15MB max)")
    target, question = rec.get("target") or lookmod.DEFAULT_TARGET, rec.get("question") or ""
    store.update_look(search_id, a, status="reading")
    _stage(sess, "reading", search_id=search_id)
    t0 = time.perf_counter()
    key = _key()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await _post(client, "sam", f"{BASE_URL}/responses", headers={**headers, "Accept": "text/event-stream"},
                               json=lookmod.build_sam_body(target, lookmod.data_url(raw, file.content_type or "image/jpeg")))
            _check("sam", resp)
            sam_text = lookmod.sse_text(resp.text)
            found = lookmod.matches(sam_text)
            if not found:
                # why SAM found nothing: what it was asked, the photo it got, what it said;
                # the photo is kept beside the looks (server/data, never committed)
                miss = store.root / "looks" / f"{re.sub(r'[^0-9a-f]', '', search_id)}-{int(a)}-miss.jpg"
                miss.parent.mkdir(parents=True, exist_ok=True)
                miss.write_bytes(raw)
                try:
                    from PIL import Image
                    import io as _io
                    size = "x".join(map(str, Image.open(_io.BytesIO(raw)).size))
                except Exception:
                    size = "?"
                print(f"look none target={target!r} q={question[:60]!r} photo={len(raw)}B {size} "
                      f"kept={miss.name} sam={sam_text[:400]!r}", flush=True)
                line = say("look_none", sess, target=target)
                if store.update_look(search_id, a, status="looking") is None:
                    return _look_stale()
                _stage(sess, "looking", search_id=search_id)
                return {"ok": False, "reply": line, "speak": speak_text(line), "record_version": store.r["version"]}
            m = found[0]
            cut, area = lookmod.crop(raw, m["box"], (m["w"], m["h"]))
            ax1, ay1, ax2, ay2 = area
            shape = [[round((x - ax1) / max(1, ax2 - ax1), 4), round((y - ay1) / max(1, ay2 - ay1), 4)]
                     for x, y in lookmod.outline(m)]  # 0-1 on the crop, for drawing over its thumbnail
            if store.update_look(search_id, a, outline=shape) is None:
                return _look_stale()
            path = store.look_crop_path(search_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(cut)
            read = await _post(client, "look read", f"{BASE_URL}/responses", headers=headers,
                               json=lookmod.build_read_body(question, target, lookmod.data_url(cut), SPARK_MODEL))
            _check("look read", read)
            got = search.output_json(read.json()) or {}
            if not (got.get("answer_short") or "").strip():
                line = say("look_unread", sess)
                if store.update_look(search_id, a, status="looking", outline=[]) is None:
                    return _look_stale()
                _stage(sess, "looking", search_id=search_id)
                return {"ok": False, "reply": line, "speak": speak_text(line), "record_version": store.r["version"]}
            result, basis = {"answer_short": got["answer_short"], "detail": got.get("detail") or "", "sources": []}, "photo"
            q = search.clean_query(got.get("query") or "", store.private_texts()) if got.get("needs_search") else ""
            if q and not _confirm_search():  # outside facts: the web search path, on what was read
                _stage(sess, "searching", query=q, search_id=search_id, say=say("looking_up", sess))
                try:
                    web, status = await _search_calls(client, key, f"{question} (it reads: {got.get('read', '')})", q, sess)
                except (asyncio.TimeoutError, HTTPException):
                    web, status = None, "timeout"
                if status == "ok" and web:
                    result, basis = web, web.get("basis") or "pages"
    except Exception:  # the photo couldn't be read (API, crop): the camera comes back, Take it again
        store.update_look(search_id, a, status="looking", outline=[])
        _stage(sess, "looking", search_id=search_id)
        raise
    ms = int((time.perf_counter() - t0) * 1000)
    if store.update_look(search_id, a, status="ok", query=q, read=got.get("read") or "", basis=basis, ms=ms,
                         answer_short=result.get("answer_short", ""), detail=result.get("detail", ""),
                         sources=result.get("sources", [])) is None:
        return _look_stale()
    print(f"look target={target} basis={basis} ms={ms} q={question[:60]!r}", flush=True)
    line = store.screen_line("web_answer", search_id)
    # the screen keeps its line; Milo answers the question: the answer, then its summary
    whole = " ".join(x for x in (result.get("answer_short", ""), result.get("detail") or "") if x.strip())
    _stage(sess, "done", reply=line)
    log_turn(sess, f"look {target}", [], [whole])
    return {"ok": True, "reply": line, "speak": speak_text(whole, limit=LOOK_SPEAK_LIMIT, sentences=None), "record_version": store.r["version"]}


#: A photo question's spoken answer: whole sentences up to about 200 words.
LOOK_SPEAK_LIMIT = 1200


def _look_stale() -> dict:
    """A reply for a photo Retake replaced: nothing shown, nothing said."""
    return {"ok": False, "stale": True, "reply": "", "record_version": store.r["version"]}


@app.get("/looks/{search_id}/crop")
async def look_crop(search_id: str):
    """The kept crop of a photo question (the ⑧ thumbnail)."""
    from fastapi.responses import FileResponse
    path = store.look_crop_path(search_id)
    if not path.exists():
        raise HTTPException(404, "no crop")
    return FileResponse(path, media_type="image/jpeg")


@app.post("/protocols/import")
async def protocol_import(file: list[UploadFile] = File(default=[]), text: str = Form(""),
                          filename: str = Form(""), original_id: str = Form(""),
                          session: str = Form("bench")):
    """Where an import goes, decided here only (the original's hash, then the
    extracted title). The active run is never touched.

    ③ its draft is still in review: no extraction, the review picks up.
    ② it is already a protocol: no extraction; "open it or review again?"
    ① new: extract, review, save.
    ④ new original, a saved protocol's title (a revision, the same page
       photographed again): extract and review; saving asks "new version
       or separate?" (draft meta `same_title`).
    The same content in flight joins that extraction. The turn shows
    "Reading the protocol…" (stage reading) and Draft ready opens when done."""
    if not PROTOCOL_FULL_MODE:
        raise HTTPException(403, "protocol import needs Full mode")
    sess = get_session(session)
    uploads = [(f.filename or "", await f.read()) for f in file]
    h = lib.source_hash([raw for _, raw in uploads], text)
    draft = store.r.get("draft")
    if draft and (draft.get("meta") or {}).get("content_hash") == h:
        print(f"import {h}: case 3, its draft is in review", flush=True)
        reply = store.resume_review(sess)
        return {**_import_reply(draft, sess, 0), "reply": reply, "speak": speak_text(reply)}
    known = lib.find_by_source(h, store.root)
    if known:
        print(f"import {h}: case 2, already the protocol {known['id']}", flush=True)
        card = store.ask_known_import(known["id"], sess)
        return {"draft": None, "review": [], "errors": [], "reply": card["question"],
                "speak": speak_text(card["question"]), "card": card, "ms": 0, "protocol_id": known["id"],
                "record_version": store.r["version"]}
    if h in _IMPORTS:
        print(f"import {h}: joining the extraction in flight", flush=True)
        return await asyncio.shield(_IMPORTS[h])
    fut = asyncio.get_running_loop().create_future()
    _IMPORTS[h] = fut
    _stage(sess, "reading")
    try:
        draft, ms = await _import_once(uploads, text, filename, original_id, h)
        # the title the page says (flagged or not): a re-photographed page reads the same
        same = lib.find_by_title((draft["plan"].get("title") or "").strip() or proto.resolve_name(draft), store.root)
        draft["meta"]["same_title"] = same["id"] if same else ""
        print(f"import {h}: case {4 if same else 1}", flush=True)
        store.save_draft(draft)  # opens Draft ready (⑤b)
        out = _import_reply(draft, sess, ms)
        log_turn(sess, f"import {draft['meta'].get('source') or ''}", [], [out["reply"]])
        fut.set_result(out)
        _stage(sess, "done", reply=out["reply"])
        return out
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()  # read here: nobody else may be waiting
        _stage(sess, "failed", reason=getattr(e, "detail", None) or type(e).__name__)
        raise
    finally:
        _IMPORTS.pop(h, None)


def _import_reply(draft: dict, sess: dict, ms: int) -> dict:
    conds = len(draft["plan"]["conditions"])
    steps = len(draft["plan"]["steps"])
    reply = f"Draft ready: {conds} conditions, {steps} steps."
    if draft["errors"]:
        reply += f" {len(draft['errors'])} red items to fix."
    elif draft["review"]:
        reply += f" {len(draft['review'])} to review."
    return {"draft": draft, "review": draft["review"], "errors": draft["errors"], "reply": reply,
            "speak": speak_text(reply), "ms": ms, "record_version": store.r["version"]}


async def _import_once(uploads: list, text: str, filename: str, original_id: str, h: str) -> tuple[dict, int]:
    """One extraction path for three origins. pdf: the file rides input_file;
    text: pasted text or a .txt; photo: page photos (several = pages) are
    transcribed first through the same Spark call path and the transcript is
    the original. Quotes are checked against that text. Bytes never touch disk:
    the app keeps PDFs and photos under `original_id` on the device."""
    if sum(len(raw) for _, raw in uploads) + len(text.encode()) > PROTOCOL_MAX_BYTES:
        raise HTTPException(413, "protocol file too large (10MB max)")
    ext = lambda n: n.lower().rsplit(".", 1)[-1] if "." in n else ""  # noqa: E731
    t0 = time.perf_counter()
    pages = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        if text.strip():
            origin, name, kind, doc_text = "text", filename or "Pasted text", "txt", text
        elif uploads and all(ext(n) in _IMAGE_TYPES for n, _ in uploads):
            origin, name, kind = "photo", filename or "Photo", "txt"
            outs = await asyncio.gather(*(_spark_json(client, "transcribe", proto.build_transcribe_body(
                f"data:{_IMAGE_TYPES[ext(n)]};base64," + base64.b64encode(raw).decode("ascii"), SPARK_MODEL))
                for n, raw in uploads))
            pages = [proto.output_text(o).strip() for o in outs]
            doc_text = "\n".join(pages)
            if not doc_text.strip():
                raise HTTPException(502, "no text found in the photo")
        elif len(uploads) == 1 and ext(uploads[0][0]) in ("pdf", "txt"):
            name, raw = uploads[0]
            kind = ext(name)
            origin = "pdf" if kind == "pdf" else "text"
            if kind == "txt":
                try:
                    doc_text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise HTTPException(400, "txt must be UTF-8")
        else:
            raise HTTPException(400, "need a .pdf or .txt file, page photos, or text")
        if kind == "pdf":
            try:
                from pypdf import PdfReader
                pages = [(pg.extract_text() or "") for pg in PdfReader(BytesIO(raw)).pages]
                doc_text = "\n".join(pages)
            except Exception:
                doc_text = ""
            payload = "data:application/pdf;base64," + base64.b64encode(raw).decode("ascii")
        else:
            payload = doc_text[: proto.PROMPT_CHARS]
        out = await _spark_json(client, "protocol", proto.build_extract_body(kind, name, payload, SPARK_MODEL))
    ms = int((time.perf_counter() - t0) * 1000)
    usage = out.get("usage") or {}
    print(f"protocol origin={origin} ms={ms} in_tok={usage.get('input_tokens', '?')} "
          f"out_tok={usage.get('output_tokens', '?')}", flush=True)
    ok, data = proto.parse_extract_payload(out)
    if not ok:
        raise HTTPException(502, f"protocol extraction failed: {data}")
    draft = proto.build_draft(data, proto.evidence_from_text(doc_text, pages), name)
    draft["meta"]["origin"] = origin
    draft["meta"]["original_id"] = re.sub(r"[^A-Za-z0-9_-]", "", original_id)[:64]
    draft["meta"]["content_hash"] = h
    return draft, ms


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
        if isinstance(info, dict) and info.get("card"):  # ④ new version or separate?
            reply = info["card"]["question"]
            log_turn(session, "save protocol", [], [reply])
            return {"saved": False, "card": info["card"], "reply": reply, "speak": speak_text(reply),
                    "record_version": store.r["version"]}
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
    return {"protocols": store.protocol_summaries()}


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
    # the record's bench as the app draws it (same rule as the live snapshot)
    doc = {**doc, "record": {**(doc.get("record") or {}), "bench": store.bench_view(doc.get("record") or {})}}
    # the robot code it became, step by step (Home: Run record, Code tab)
    code = store.handoff_code(doc)
    return {"run": doc, "label": lab["display"], "spoken": lab["spoken"], "handoff": code, "has_code": code is not None}


@app.delete("/protocols/draft")
async def draft_delete(body: DraftStartIn):
    store.clear_draft()
    return {"ok": True, "record_version": store.r["version"]}


class ItemsIn(BaseModel):
    screen: str
    items: list[dict] = []


@app.post("/screen/items")
async def screen_items(body: ItemsIn):
    """The app says what a list screen shows (its files, the photos taken), so
    "number 2" and a file's name pick from exactly those."""
    store.set_items(body.screen, body.items)
    return {"ok": True, "record_version": store.r["version"]}


@app.get("/searches")
async def searches_list():
    """Every web search sent (status: pending / ok / why not), newest first."""
    return {"searches": store.search_summaries(), "sent": len(store.searches())}


@app.get("/handoff/{run_id}/files/{name}")
async def handoff_file(run_id: str, name: str):
    """A hand off file the share sheet sends (protocol.py, plate_map.csv), by name only."""
    from fastapi.responses import FileResponse

    path = store.handoff_file(run_id, name)
    if path is None:
        raise HTTPException(404, "no such file")
    return FileResponse(path, filename=name)


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
    print(f"events open: listeners={len(store._listeners)}", flush=True)

    async def gen():
        try:
            yield f"data: {json.dumps({'kind': 'snapshot', **store.snapshot()}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # SSE comment: proves the stream is alive, carries nothing
                    continue
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
        finally:
            try:
                store._listeners.remove(cb)
            except ValueError:
                pass
            print(f"events closed: listeners={len(store._listeners)}", flush=True)

    return StreamingResponse(gen(), media_type="text/event-stream")


