"""Pip orchestration server: text/voice -> validated calls -> record store.

Local dev only (no Modal/Private modes). Run from repo root:

    uvicorn server.api:app --reload --port 8000

Needs MODEL_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
import wave
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

from server.grammar import (
    call_wells,
    collapse_each_timers,
    expand_tool_wells,
    fill_on_done,
    final_pip_line,
    find_amounts,
    is_failure_message,
    keep_grounded_timers,
    mark_each_timers,
    match_grammar,
    needs_minutes_ask,
    normalize_text,
    relevant_plan_numbers,
    split_ask,
    timer_set_reply,
    wells_label,
)
from server.memory import Memory, SparkChainMemory
from server.store import RecordStore, is_well

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

SYSTEM = (
    "You are Pip, a bench assistant. Emit tool calls only, never reply text. "
    "Wells go as range strings (\"C2-C8\"). \"Those\" and \"it\" refer to recent wells. "
    "One set_timer per distinct duration; the server fans each out per well. "
    "Use only minutes stated in the message or plan. "
    "Answer questions with answer(text); use get_well only for memo text, never for counts. "
    "Ask the user with ask(question) only when you must. "
    "pending_question means this message answers it. "
    "Emit direct calls in run order; if a later step needs an earlier result, do only the first step now. "
    "For another or backup well, call find_free_wells first and propose the first candidate; redoing elsewhere always pairs exclude_well with record_redo; redo targets must be free. "
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


WELLS = {"type": "string", "description": 'Well range, e.g. "C2-C8" or "C2,C5,C7"'}

TOOLS = [
    _tool("mark_done", "Mark wells finished.", {"wells": WELLS}),
    _tool(
        "exclude_well",
        "Exclude one well.",
        {
            "well": {"type": "string", "description": "Well ID"},
            "reason": _opt({"type": "string", "description": "Why, if stated"}),
        },
    ),
    _tool(
        "record_redo",
        "Mark wells for redo. Redoing elsewhere: also exclude the damaged well.",
        {"wells": WELLS, "note": _opt({"type": "string", "description": "What went wrong"})},
    ),
    _tool(
        "add_note",
        "Note a target. Quote the speaker exactly.",
        {
            "target": {"type": "string", "description": "Well, step, or sample"},
            "text": {"type": "string"},
        },
    ),
    _tool(
        "save_tip",
        "Save a bench tip for a step.",
        {"step_id": {"type": "string"}, "text": {"type": "string"}},
    ),
    _tool(
        "set_timer",
        "Start a countdown.",
        {
            "label": {"type": "string"},
            "minutes": {"type": "number", "description": "Minutes from now"},
            "on_done": _opt({"type": "string", "description": "Next action, if stated"}),
            "wells": _opt(WELLS),
        },
    ),
    _tool(
        "manage_timers",
        "List, cancel, or extend timers. Extend adds 5 min unless minutes given.",
        {
            "action": {"type": "string", "description": "list, cancel, or extend"},
            "label": _opt({"type": "string", "description": "Timer label"}),
            "minutes": _opt({"type": "number", "description": "Minutes to add"}),
        },
    ),
    _tool("where_am_i", "Report step and progress.", {}),
    _tool("next_step", "Advance one step.", {}),
    _tool(
        "show",
        "Highlight on screen.",
        {
            "view": {"type": "string", "description": "plate, timer, protocol"},
            "target": _opt({"type": "string", "description": "Well/step, if any"}),
        },
    ),
    _tool(
        "ask",
        "Ask the user a question. Only when you must.",
        {"question": {"type": "string"}},
    ),
    _tool("make_robot", "Build the robot protocol.", {}),
    _tool(
        "get_well",
        "Look up one well's status and memos.",
        {"well": {"type": "string", "description": "Well ID"}},
    ),
    _tool(
        "find_free_wells",
        "List free wells near a well. For another or backup well, take these and propose the first; ask if none.",
        {
            "near": {"type": "string", "description": "Reference well"},
            "count": _opt({"type": "integer", "description": "How many (default 3)"}),
        },
    ),
    _tool(
        "answer",
        "Answer a question. Short text only.",
        {"text": {"type": "string"}},
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
        s = {"id": sid, "memory": SparkChainMemory(), "pending": None, "last_web": None}
        _SESSIONS[sid] = s
    return s


@asynccontextmanager
async def lifespan(app: FastAPI):
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


class EditNoteIn(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


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
    model: str = SPARK_MODEL, effort: str = REASONING_EFFORT,
) -> tuple[list, str, int, bool, dict, list]:
    """Returns (calls, text, ms, truncated, usage, web_searches).

    Chained turns carry function_call_outputs for the previous turn's calls.
    Any 4xx on a chained call drops the chain and retries once fresh with a
    recent-actions summary; a second failure answers sorry instead of a 502.
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
        chained = bool(mem.params())
        pend = session.get("pending_outputs") or []
        if chained and pend:
            req_input: object = [
                {"type": "function_call_output", "call_id": p["call_id"], "output": p["output"]}
                for p in pend
            ] + [{"role": "user", "content": text}]
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


CONTINUE_TEXT = "Continue with the looked-up data."


async def run_utterance(client: httpx.AsyncClient, key: str, text: str, session: dict, source: str):
    """Grammar first (no model call), else Spark with at most one lookup round.
    Returns (calls, messages, reply, ms, route, cards, info, sources)."""
    old_pending = session.get("pending")
    norm = normalize_text(text)
    g = match_grammar(norm, store.prompt_state(session))
    if g is not None:
        calls, _ = g
        calls = expand_tool_wells(calls)
        res = store.apply_calls(calls, source, text, "grammar", session)
        reply = _compose_reply(calls, res, store.prompt_state(session))
        if res["info"].get("edited"):
            reply = f"Note updated ({res['info']['edited']['target']})."
        _clear_pending(session, old_pending)
        return calls, res["messages"], reply, 0, "grammar", res["cards"], res["info"], []
    calls, _, ms, cut, _, searches = await _call_spark(
        client, key, norm, store.model_state(session), session)
    if cut:
        _clear_pending(session, old_pending)
        return [], [], "Sorry, say that again?", ms, "spark", [], {"dropped": 0, "drop_reasons": []}, []
    sentence, sources = web_answer(searches) if searches else ("", [])
    if sentence:
        session["last_web"] = sentence
    ordered_ids = [c.get("call_id") for c in calls]
    plan_allowed = relevant_plan_numbers(norm, [s.get("name", "") for s in store.r["plan"]["steps"]])
    calls, question, outcomes, gdrop = _validate(calls, norm, 1, plan_allowed)
    _set_ask_pending(session, question, searches)
    if not calls and not question:
        _store_outputs(session, ordered_ids, outcomes)
        if needs_minutes_ask(calls, norm) or gdrop:
            session["pending"] = {
                "question": "How many minutes for the timer?",
                "kind": "minutes",
                "target": recent_target(),
            }
            return [], [], "How many minutes for the timer?", ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
        if sentence:
            _clear_pending(session, old_pending)
            return [], [], sentence, ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
        _clear_pending(session, old_pending)
        return [], [], "I didn't catch a command.", ms, "spark", [], {"dropped": 0, "drop_reasons": []}, sources
    res = store.apply_calls(calls, source, text, "spark", session)
    _apply_outcomes(calls, res, outcomes)
    _store_outputs(session, ordered_ids, outcomes)
    if question is None and any(c.get("name") in ("get_well", "find_free_wells") and c.get("call_id") for c in calls):
        memo_allowed: set = set()
        for c in calls:
            if c.get("name") == "get_well":
                well = (c.get("args") or {}).get("well")
                if isinstance(well, str) and is_well(well):
                    d = store.get_well_data(well)
                    memo_allowed |= find_amounts(" ".join(d["memos"] + [d["reason"], d["redo_note"]]))
        ms2, more = await _lookup_round(client, key, norm, session, source, text, plan_allowed, memo_allowed)
        ms += ms2
        if more is not None:
            rc, res2, question2, ordered2, outcomes2, gdrop2 = more
            calls = calls + rc
            res["messages"] += res2["messages"]
            res["cards"] += res2["cards"]
            res["outcomes"] += res2["outcomes"]
            res["info"]["dropped"] += res2["info"]["dropped"]
            res["info"]["drop_reasons"] += res2["info"]["drop_reasons"]
            for k in ("saved_memo", "edited", "robot"):
                if res2["info"].get(k):
                    res["info"][k] = res2["info"][k]
            if question2 is not None:
                question = question2
            gdrop = gdrop or gdrop2
            _store_outputs(session, ordered2, outcomes2)
    calls = _strip_ids(calls)
    if question is not None:
        reply = question
    elif gdrop:
        session["pending"] = {
            "question": "How many minutes for the timer?",
            "kind": "minutes",
            "target": recent_target(),
        }
        base = _compose_reply(calls, res, store.prompt_state(session))
        reply = "How many minutes for the timer?" if base in ("I didn't catch a command.", "") else base + " How many minutes for the timer?"
    else:
        reply = _compose_reply(calls, res, store.prompt_state(session))
        if sentence and sources and reply in ("I didn't catch a command.", ""):
            reply = sentence
    for m in res["messages"]:
        if m.startswith("Note updated.") and res["info"].get("edited"):
            reply = f"Note updated ({res['info']['edited']['target']})."
    _clear_pending(session, old_pending)
    return calls, res["messages"], reply, ms, "spark", res["cards"], res["info"], sources


async def _lookup_round(client, key, norm, session, source, text, plan_numbers, memo_numbers):
    """Second spark round chained with lookup outputs. Returns (ms, None-or-tuple)."""
    rc, _, ms, cut, _, _ = await _call_spark(
        client, key, CONTINUE_TEXT, store.model_state(session), session)
    if cut:
        return ms, None
    ordered2 = [c.get("call_id") for c in rc]
    rc, question2, outcomes2, gdrop2 = _validate(rc, norm, 2, plan_numbers, memo_numbers)
    _set_ask_pending(session, question2, [])
    res2: dict = {"messages": [], "cards": [], "outcomes": [],
                  "info": {"dropped": 0, "drop_reasons": []}}
    if rc:
        res2 = store.apply_calls(rc, source, text, "spark", session)
        _apply_outcomes(rc, res2, outcomes2)
    return ms, (rc, res2, question2, ordered2, outcomes2, gdrop2)


def _validate(calls, norm, depth, plan_numbers=frozenset(), lookup_numbers=frozenset()):
    """expand -> ask/answer partition -> value-grounded timer filter.
    Returns (calls, question, outcomes, grounded_drop)."""
    outcomes: dict[str, str] = {}
    calls = fill_on_done(calls, norm)
    before, calls = calls, expand_tool_wells(calls)
    _note_lost(outcomes, before, calls, "dropped: no valid wells")
    question, kept = split_ask(calls)
    for c in calls:
        if c.get("name") == "ask" and c.get("call_id"):
            outcomes[c["call_id"]] = "asked user"
    calls = kept
    calls, dropped_answers = _partition_answer(calls, question is not None)
    for c in dropped_answers:
        if c.get("call_id"):
            outcomes[c["call_id"]] = _result(False, "dropped: action wins")
            _log_drop(c.get("name"), c.get("args"), "dropped: action wins")
    if depth >= 2:
        deep = [c for c in calls if c.get("name") in ("get_well", "find_free_wells")]
        calls = [c for c in calls if c.get("name") not in ("get_well", "find_free_wells")]
        for c in deep:
            if c.get("call_id"):
                outcomes[c["call_id"]] = _result(False, "lookup depth exceeded")
                _log_drop(c.get("name"), c.get("args"), "lookup depth exceeded")
    allowed = find_amounts(norm) | set(plan_numbers) | set(lookup_numbers)
    before, calls = calls, keep_grounded_timers(calls, allowed)
    grounded_drop = False
    after_ids = {c.get("call_id") for c in calls}
    for c in before:
        cid = c.get("call_id")
        if cid and c.get("name") == "set_timer" and cid not in after_ids and cid not in outcomes:
            grounded_drop = True
            outcomes[cid] = _result(False, "dropped: ungrounded minutes")
            _log_drop(c.get("name"), c.get("args"), "dropped: ungrounded minutes")
    before, calls = calls, collapse_each_timers(mark_each_timers(calls, norm), norm)
    _note_lost(outcomes, before, calls, "dropped: duplicate timer")
    return calls, question, outcomes, grounded_drop


def _partition_answer(calls, has_question):
    answers = [c for c in calls if c.get("name") == "answer"]
    if not answers:
        return calls, []
    rest = [c for c in calls if c.get("name") != "answer"]
    if has_question or any(c.get("name") != "get_well" for c in rest):
        return rest, answers
    return calls, []


def _set_ask_pending(session, question, searches):
    if question is None:
        return
    pending: dict = {"question": question}
    if searches and "attach" in question.lower() and session.get("last_web"):
        pending["kind"] = "attach"
        pending["candidate"] = session["last_web"]
    else:
        pending["kind"] = "ask"
        pending["target"] = recent_target()
    session["pending"] = pending


def _apply_outcomes(calls, res, outcomes):
    for i, c in enumerate(calls):
        cid = c.get("call_id")
        if not cid or cid in outcomes:
            continue
        if c.get("name") == "get_well":
            well = (c.get("args") or {}).get("well")
            if isinstance(well, str) and is_well(well):
                outcomes[cid] = json.dumps(store.get_well_data(well), ensure_ascii=False)
                continue
        if c.get("name") == "find_free_wells":
            a = c.get("args") or {}
            near = a.get("near")
            if isinstance(near, str) and is_well(near):
                outcomes[cid] = json.dumps(
                    {"near": near.upper(), "free": store.find_free_wells(near, a.get("count", 3))},
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


def _clear_pending(session: dict, old):
    if session.get("pending") is old:
        session["pending"] = None


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
    return [{"name": c.get("name"), "args": c.get("args") or {}} for c in calls]


def _refresh_output(session: dict, cid: str, ok: bool, message: str):
    for p in session.get("pending_outputs") or []:
        if p["call_id"] == cid:
            p["output"] = _result(ok, message if ok else "declined by user")


def _compose_reply(calls, res, state) -> str:
    """Reply purely from store results: messages, card questions, drop reasons."""
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
    return " ".join(parts) if parts else "I didn't catch a command."


#: Human verbs per tool. User-visible text never names a tool.
_COULDNT = {
    "add_note": "save the note",
    "save_tip": "save the tip",
    "set_timer": "start the timer",
    "answer": "answer that",
    "mark_done": "record that",
    "record_redo": "mark that for redo",
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
    for k in ("saved_memo", "edited", "robot"):
        if info.get(k):
            out[k] = info[k]
    if info.get("dropped"):
        out["dropped"] = info["dropped"]
    if extra:
        out.update(extra)
    return out


@app.post("/orchestrate")
async def orchestrate(body: OrchestrateIn):
    key = _key()
    session = get_session(body.session)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        out = await run_utterance(client, key, body.text, session, "spark")
    calls, messages, reply, ms, route, cards, info, sources = out
    print(f"orch route={route} calls={_call_names(calls)} reply={reply[:80]!r}", flush=True)
    return _utterance_response(calls, messages, reply, {"spark": ms}, route, cards, info, sources, store.r["version"])


@app.post("/voice")
async def voice(audio: UploadFile = File(...), state: str = Form("{}"), session: str = Form("bench")):
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
                "reply": "I didn't catch that, please say it again.",
                "ms": {"asr": asr_ms, "spark": 0, "total": int((time.perf_counter() - total0) * 1000)},
            }
        out = await run_utterance(client, key, transcript, sess, "voice")
    calls, messages, reply, spark_ms, route, cards, info, sources = out
    print(f"orch route={route} calls={_call_names(calls)} reply={reply[:80]!r}", flush=True)
    return _utterance_response(
        calls, messages, reply,
        {"asr": asr_ms, "spark": spark_ms, "total": int((time.perf_counter() - total0) * 1000)},
        route, cards, info, sources, store.r["version"], {"transcript": transcript},
    )


@app.post("/confirm")
async def confirm(body: ConfirmIn):
    session = get_session(body.session)
    message, new_cards, card = store.confirm(body.card_id, body.ok, "card", session)
    cid = (card.get("payload") or {}).get("_call_id") if card else None
    if cid:
        _refresh_output(session, cid, body.ok, message)
    return {"message": message, "cards": new_cards, "record_version": store.r["version"]}


@app.post("/notes/edit")
async def notes_edit(body: EditNoteIn):
    message = store.edit_note_manual(body.id, body.text)
    return {"message": message, "record_version": store.r["version"]}


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
