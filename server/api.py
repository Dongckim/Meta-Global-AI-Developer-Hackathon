"""Pip orchestration server: text/voice -> Spark tool calls.

Local dev only (no Modal/Private modes). Run from repo root:

    uvicorn server.api:app --reload --port 8000

Needs MODEL_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import json
import os
import time
import uuid
import wave
from io import BytesIO

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

load_dotenv()

BASE_URL = "https://api.meta.ai/v1"
SPARK_MODEL = "muse-spark-1.3"
ASR_MODEL = "muse-voice-transcribe-1.0"
KEYWORDS = ["B2", "C3", "D8", "aspirate", "Pip", "drug A", "drug B"]
LANGUAGE_BIAS = ["Korean", "English"]
TIMEOUT = httpx.Timeout(120.0)

SYSTEM = "너는 실험대 조수 Pip이다. 말을 도구 호출로 바꾼다. 도구가 필요 없으면 아무것도 호출하지 않는다."


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


WELLS = {"type": "array", "items": {"type": "string"}, "description": "Well IDs, e.g. [\"B2\", \"C3\"]"}

TOOLS = [
    _tool("mark_done", "Mark wells as finished.", {"wells": WELLS}, ["wells"]),
    _tool(
        "exclude_well",
        "Exclude a well from the run.",
        {
            "well": {"type": "string", "description": "Well ID, e.g. \"B2\""},
            "reason": {"type": "string", "description": "Why it is excluded, if stated"},
        },
        ["well"],
    ),
    _tool(
        "record_redo",
        "Record wells that must be redone.",
        {
            "wells": WELLS,
            "note": {"type": "string", "description": "What went wrong, if stated"},
        },
        ["wells"],
    ),
    _tool(
        "add_note",
        "Attach a free-text note to a target.",
        {
            "target": {"type": "string", "description": "Well, step, or sample the note is about"},
            "text": {"type": "string"},
        },
        ["target", "text"],
    ),
    _tool(
        "save_tip",
        "Save a reusable bench tip for a protocol step.",
        {"step_id": {"type": "string"}, "text": {"type": "string"}},
        ["step_id", "text"],
    ),
    _tool(
        "set_timer",
        "Start a countdown timer.",
        {
            "label": {"type": "string"},
            "minutes": {"type": "number", "description": "Minutes from now"},
            "on_done": {"type": "string", "description": "What to do when it rings, if stated"},
        },
        ["label", "minutes"],
    ),
    _tool(
        "manage_timers",
        "List, cancel, or extend running timers.",
        {
            "action": {"type": "string", "description": "One of: list, cancel, extend"},
            "label": {"type": "string", "description": "Timer label (not needed for list)"},
        },
        ["action"],
    ),
    _tool("where_am_i", "Report the current protocol step and progress.", {}, []),
    _tool("next_step", "Advance to the next protocol step.", {}, []),
    _tool(
        "show",
        "Show something on screen.",
        {
            "view": {"type": "string", "description": "What to show, e.g. plate, timer, protocol"},
            "target": {"type": "string", "description": "Specific well/step, if any"},
        },
        ["view"],
    ),
]

app = FastAPI(title="Pip server")


class OrchestrateIn(BaseModel):
    text: str = Field(min_length=1)
    state: dict = {}


def _key() -> str:
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise HTTPException(500, "MODEL_API_KEY is not set (check .env)")
    return key


def _check(name: str, resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise HTTPException(502, f"{name} upstream {resp.status_code}: {resp.text[:200]}")


async def _post(client: httpx.AsyncClient, name: str, *a, **k) -> httpx.Response:
    try:
        return await client.post(*a, **k)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"{name} unreachable: {e}")


async def _call_spark(client: httpx.AsyncClient, key: str, text: str, state: dict) -> tuple[list, str, int]:
    body = {
        "model": SPARK_MODEL,
        "instructions": SYSTEM + "\n\n현재 상태(JSON):\n" + json.dumps(state, ensure_ascii=False),
        "input": text,
        "reasoning": {"effort": "low"},
        "tools": TOOLS,
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
    _check("spark", resp)
    calls, texts = [], []
    for item in resp.json().get("output", []):
        if item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"name": item.get("name"), "args": args})
        elif item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
    return calls, "\n".join(texts), ms


@app.post("/orchestrate")
async def orchestrate(body: OrchestrateIn):
    key = _key()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        calls, reply, ms = await _call_spark(client, key, body.text, body.state)
    return {"tool_calls": calls, "reply": reply, "ms": {"spark": ms}}


@app.post("/voice")
async def voice(audio: UploadFile = File(...), state: str = Form("{}")):
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
    except (wave.Error, EOFError):
        wav_ok = False
    if not wav_ok:
        raise HTTPException(400, "audio must be mono 16-bit PCM WAV at 16kHz or 24kHz")
    key = _key()
    asr_req = {
        "mode": "PUSH_TO_TALK",  # single-turn voice command; the API default
        "model": ASR_MODEL,
        "audioEncoding": "WAV",
        "languageBias": LANGUAGE_BIAS,
        "keywords": KEYWORDS,
    }
    total0 = time.perf_counter()
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
        calls, reply, spark_ms = await _call_spark(client, key, transcript, state_obj)
    return {
        "transcript": transcript,
        "tool_calls": calls,
        "reply": reply,
        "ms": {"asr": asr_ms, "spark": spark_ms, "total": int((time.perf_counter() - total0) * 1000)},
    }
