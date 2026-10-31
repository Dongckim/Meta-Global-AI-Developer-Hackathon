"""Private: voice turns run on Glimmer and nothing reaches the Meta API
before its confirm card. Every outgoing model call is recorded here.

Run: python -m server.tests.test_private_voice (runs on import).
"""

import asyncio
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
    api.store.spawn_import = api._spawn_import
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
assert set(out) == {"messages", "tools", "max_tokens", "cache_prompt"} and out["max_tokens"] == 500
assert [t["function"]["name"] for t in out["tools"]] == [t["name"] for t in api.TOOLS]
assert [t["function"]["parameters"] for t in out["tools"]] == [t["parameters"] for t in api.TOOLS], "same tools"
assert private_chat.to_chat({**body, "input": "hi"})["messages"][1] == {"role": "user", "content": "hi"}
turn = private_chat.to_chat({**body, "instructions": "SYS" + api.STATE_MARK + '{"step": 3}'})["messages"]
assert turn[0]["content"] == "SYS" and '{"step": 3}' in turn[1]["content"], "state stays out of the fixed prefix"
assert turn[1]["content"].endswith("what is on C6")
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

# A named well is never dropped quietly --------------------------------------
c = private()
timer = ("perform", {"action": "set_timer", "minutes": 5, "target": "C5"})
calls = Calls(chat(timer), chat(("perform", {"action": "mark_done", "wells": ["C6"]})))
with patch.object(api, "_post", calls):
    j = say(c, "put a five minute clock on C5 and also tick off C6")
assert "C6" in done_wells(api.store.r) and len(calls.glimmer) == 2, j
print("PV-lost1 ok: a verb grammar doesn't know still gets the followup round")

c = private()
calls = Calls(chat(timer), chat(text="Done."))
with patch.object(api, "_post", calls):
    j = say(c, "put a five minute clock on C5 and also tick off C6")
assert "C6" not in done_wells(api.store.r) and "C6" in j["reply"] and j["reply"].endswith("?"), j
print("PV-lost2 ok: still not done: it says so")

c = private()
calls = Calls(chat(("read_record", {"kind": "wells", "filter": "C2"})), chat(text="C2 is pending."))
with patch.object(api, "_post", calls):
    j = say(c, "how is C2 compared to C3")
assert len(calls.glimmer) == 2 and "log" not in j["reply"], j
print("PV-lost3 ok: a read turn isn't asked about")

# Nothing reaches Meta before its card --------------------------------------
c = private()
calls = Calls(chat(("web_search", {"query": "max DMSO for HeLa cells"})))
searched = []


async def fake_search(client, key, question, query, session):
    searched.append(query)
    return "Found it.", []

with patch.object(api, "_post", calls), patch.object(api, "_run_search", fake_search):
    j = say(c, "what's the max DMSO for HeLa cells")
    assert [x["kind"] for x in j["cards"]] == ["confirm_search"] and not searched and not calls.meta, j
    j = say(c, "yes")
assert searched == ["max DMSO for HeLa cells"], j
print("PV8 ok: web_search chosen on Glimmer: card first, search only after yes")

c = private()
calls = Calls()
with patch.object(api, "_post", calls):
    j = say(c, "what does this label say")
    assert [x["kind"] for x in j["cards"]] == ["confirm_photo"] and not j.get("ui") and not calls.meta, j
    j = say(c, "yes")
assert [d["status"] for d in api.store.searches()] == ["looking"], j
print("PV9 ok: a photo question: card first, the camera only after yes")


extracted = []


async def fake_once(uploads, text, filename, original_id, h):
    extracted.append(text)
    raise api.HTTPException(502, "stop here")


def import_text(c, text):
    return c.post("/protocols/import", data={"text": text, "session": "t"}).json()


async def after_yes():
    """A yes spawns the extraction on the running loop."""
    api._spawn_import()
    await asyncio.sleep(0.01)

c = private()
with patch.object(api, "_import_once", fake_once):
    j = import_text(c, "Add 10 uL to C2.")
    assert [j["card"]["kind"], j["draft"], extracted] == ["confirm_import", None, []], j
    say(c, "no")
    assert extracted == [], extracted
    print("PV10 ok: an import waits for its card; no sends nothing")
    j = import_text(c, "Add 20 uL to C3.")
    assert j["card"]["kind"] == "confirm_import" and extracted == [], j
    asyncio.run(after_yes())
assert extracted == ["Add 20 uL to C3."], extracted
print("PV11 ok: yes reads the held import, once")

c = private()
calls = Calls(chat(("perform", {"action": "mark_done", "wells": ["B7"]})))
with patch.object(api, "_post", calls):
    j = c.post("/voice", data={"transcript": "the seventh one in row B, tick it off", "session": "t"}).json()
    k = c.post("/voice", files={"audio": ("c.wav", b"RIFF", "audio/wav")}, data={"session": "t"}).json()
assert "B7" in done_wells(api.store.r) and not calls.meta, (j, calls.meta)
assert k["transcript"] == "" and k["reply"], k
print("PV12 ok: /voice takes the iPad's words; audio sent anyway stops here, ASR 0")

tone.set_run_mode(None)
print("ALL PASS")
