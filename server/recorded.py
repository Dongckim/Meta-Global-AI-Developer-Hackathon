"""MODEL_MODE=recorded: the app runs with no key, no glasses and no outside network, so a judge
can try it (README, Try it without hardware). Grammar, the record, the simulator and the code
verifier are the real ones. What needs a Meta model says so instead; no model reply is stored.
Robot code comes from `handoff.plain_code`, a stand-in for Spark that the same loop checks.
"""

import json
import os

import httpx
from fastapi import HTTPException

from server.handoff import plain_code
from server.tone import say


def on() -> bool:
    return os.environ.get("MODEL_MODE", "").strip().lower() == "recorded"


def block_network() -> None:
    """Every outside call fails here, whichever code path tries it."""
    os.environ["ROBOT_SIM"] = "local"

    def refuse(self, request, *a, **k):
        raise httpx.ConnectError(f"MODEL_MODE=recorded: no outside calls ({request.url.host})", request=request)

    async def refuse_async(self, request, *a, **k):
        refuse(self, request)

    httpx.HTTPTransport.handle_request = refuse
    httpx.AsyncHTTPTransport.handle_async_request = refuse_async


def post(name: str) -> httpx.Response:
    """Stands in for every model call. A chat turn gets Milo's line as its answer."""
    line = say("needs_key")
    if name != "spark":
        raise HTTPException(503, line)
    return httpx.Response(200, json={"id": "", "status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": line}]}]})


def handoff_call(approved: dict):
    """The handoff loop's `call`: the three rounds Spark would take. run_loop still simulates and checks."""
    def tool(name: str, args: dict | None = None) -> dict:
        return {"type": "function_call", "name": name, "arguments": json.dumps(args or {}), "call_id": name}

    rounds = iter([tool("generate_protocol", {"code": plain_code(approved)}), tool("simulate"),
                   tool("finish", {"summary": "Stand-in code."})])

    async def call(body: dict) -> dict:
        return {"output": [r] if (r := next(rounds, None)) else []}

    return call
