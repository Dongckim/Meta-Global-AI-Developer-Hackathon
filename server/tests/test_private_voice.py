"""Private: voice turns run on Glimmer and nothing reaches the Meta API
before its confirm card. Every outgoing model call is recorded here.

Run: python -m server.tests.test_private_voice (runs on import).
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"
os.environ["PRIVATE_LLM_URL"] = "http://glimmer.test"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import private_chat, tone  # noqa: E402
from server.runs import done_wells  # noqa: E402
from server.store import RecordStore  # noqa: E402


class Calls:
    """_post stand-in: Glimmer answers from `script`; Meta is only counted."""

    def __init__(self, *script):
        self.script, self.glimmer, self.meta = list(script), [], []

    async def __call__(self, client, name, url, **k):
        if url.startswith("http://glimmer.test"):
            self.glimmer.append(k["json"])
            r = self.script.pop(0)
            return httpx.Response(r, text="boom") if isinstance(r, int) else httpx.Response(200, json=r)
        self.meta.append(url)
        return httpx.Response(200, json={"output": [], "transcript": ""})


def chat(*calls, text="", finish="stop"):
    tools = [{"id": f"c{i}", "type": "function",
              "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": {"content": text, "tool_calls": tools}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3}}


def private(awake=True):
    tone.set_run_mode("private")
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.spawn_wake = lambda session, gen: None
    api.store.start_demo_run()
    api._SESSIONS.clear()
    if awake:
        api.store.wake.begin()
        api.store.wake.finish(True)
    return TestClient(api.app)


def say(c, text):
    return c.post("/input", json={"text": text, "session": "t"}).json()


# The adapter ---------------------------------------------------------------
body = {"instructions": "SYS", "input": [
    {"role": "user", "content": "what is on C6"},
    {"type": "function_call", "name": "read_record", "arguments": "{}", "call_id": "x"},
    {"type": "function_call_output", "call_id": "x", "output": "C6: pending"},
    {"role": "user", "content": "Continue."}],
    "tools": api.TOOLS, "max_output_tokens": 500, "store": False, "prompt_cache_key": "k"}
out = private_chat.to_chat(body)
assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant", "tool", "user"], out
assert out["messages"][3] == {"role": "tool", "tool_call_id": "x", "content": "C6: pending"}
assert set(out) == {"messages", "tools", "max_tokens"} and out["max_tokens"] == 500
assert [t["function"]["name"] for t in out["tools"]] == [t["name"] for t in api.TOOLS]
assert [t["function"]["parameters"] for t in out["tools"]] == [t["parameters"] for t in api.TOOLS], "same tools"
assert private_chat.to_chat({**body, "input": "hi"})["messages"][1] == {"role": "user", "content": "hi"}
back = private_chat.from_chat(chat(("perform", {"action": "mark_done"}), text="ok"))
assert [o["type"] for o in back["output"]] == ["function_call", "message"] and back["status"] == "completed"
assert private_chat.from_chat(chat(finish="length"))["status"] == "incomplete"
print("PV1 ok: Responses <-> chat keeps roles, tools and a cut answer")

# A voice turn --------------------------------------------------------------
c = private()
calls = Calls(chat(("perform", {"action": "mark_done", "wells": ["B7"]})))
with patch.object(api, "_post", calls):
    j = say(c, "the seventh one in row B, tick it off")
assert "B7" in done_wells(api.store.r) and len(calls.glimmer) == 1 and not calls.meta, (j, calls.meta)
print("PV2 ok: a voice turn runs on Glimmer through the same checks, Meta 0")

c = private()
calls = Calls(chat(("read_record", {"kind": "wells", "filter": "C6"})), chat(text="C6 is pending, 0 of 7 done."))
with patch.object(api, "_post", calls):
    j = say(c, "what is on C6")
second = calls.glimmer[1]["messages"]
assert len(calls.glimmer) == 2 and not calls.meta, calls.meta
assert [m["role"] for m in second[-3:]] == ["assistant", "tool", "user"], second[-3:]
assert j["reply"] == "C6 is pending, 0 of 7 done.", j
print("PV3 ok: a read's second round replays as tool messages")

c = private()
calls = Calls(chat(("perform", {"action": "mark_done", "wells": ["Z9"]})))
with patch.object(api, "_post", calls):
    say(c, "tick off that one")
assert "Z9" not in done_wells(api.store.r), "the checks after the model stay"
print("PV4 ok: a bad well from Glimmer is refused like Spark's")

c = private()
calls = Calls(500, chat(("perform", {"action": "mark_done", "wells": ["B7"]})))
with patch.object(api, "_post", calls):
    say(c, "tick off the seventh of row B")
assert len(calls.glimmer) == 2 and "B7" in done_wells(api.store.r), "a 500 retries once"
calls = Calls(500, 500)
with patch.object(api, "_post", calls):
    j = say(c, "tick off the eighth of row B")
assert j["reply"] == "Sorry, say that again?" and not calls.meta, j
print("PV5 ok: one retry, then sorry, never Spark")

c = private()
api.store.wake.ready_at -= 90
with patch.object(api, "_post", Calls(chat(text="Hi."))):
    say(c, "good morning there friend")
assert api.store.wake.clock() - api.store.wake.ready_at < 5, "a turn keeps the model awake"
print("PV6 ok: an answer restarts the idle clock")

c = private(awake=False)
calls = Calls()
with patch.object(api, "_post", calls):
    j = say(c, "how long can the cells sit out")
assert j["route"] == "wait" and not calls.glimmer and not calls.meta, j
print("PV7 ok: asleep: the turn waits for the wake-up")

tone.set_run_mode(None)
print("ALL PASS")
