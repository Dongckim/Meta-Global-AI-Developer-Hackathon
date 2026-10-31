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
assert set(out) == {"messages", "tools", "max_tokens", "cache_prompt", "parallel_tool_calls"} and out["max_tokens"] == 500
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

# An answer about the record comes from a read --------------------------------
c = private()
calls = Calls(chat(text="You've finished 2 wells: C2 and C3 are marked done."),
              chat(("read_record", {"kind": "wells", "filter": "C2-C8"})),
              chat(text="None of the 7 wells are done yet."))
with patch.object(api, "_post", calls):
    j = say(c, "how many wells have I finished so far")
assert calls.glimmer[1]["tool_choice"] == "required" and "tool_choice" not in calls.glimmer[0], calls.glimmer[0].keys()
assert j["reply"] == "None of the 7 wells are done yet.", j
print("PV-read ok: a claim with no read gets one forced read")

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


# Imports read on Glimmer, Meta 0 ------------------------------------------------
SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "protocol_wash.pdf"
EMPTY_PLAN = json.dumps({"title": "Wash", "short_title": "Wash", "liquids": [], "conditions": [], "steps": []})


def post_import(c, **k):
    return c.post("/protocols/import", data={"session": "t", "original_id": "o", **k.pop("data", {})}, **k)


c = private()
calls = Calls(chat(text=EMPTY_PLAN))
with patch.object(api, "_post", calls):
    r = post_import(c, files={"file": ("wash.pdf", SAMPLE.read_bytes(), "application/pdf")})
sent = calls.glimmer[0]
assert r.status_code == 200 and not calls.meta and len(calls.glimmer) == 1, (r.text, calls.meta)
assert "card" not in r.json() and r.json()["draft"], r.json()
assert sent["response_format"]["json_schema"]["schema"] == api.proto.PROTOCOL_SCHEMA, "the same schema"
assert sent["max_tokens"] == api.proto.IMPORT_MAX_OUTPUT_TOKENS and "tools" not in sent
assert isinstance(sent["messages"][1]["content"], list) and "Document:" in sent["messages"][1]["content"][0]["text"]
print("PV10 ok: a PDF goes as its text to Glimmer, same schema, no card, Meta 0")

c = private()
calls = Calls(chat(text="Add 10 uL to C2."), chat(text=EMPTY_PLAN))
with patch.object(api, "_post", calls):
    r = post_import(c, files={"file": ("page1.jpg", b"\xff\xd8x", "image/jpeg")})
assert r.status_code == 200 and len(calls.glimmer) == 2 and not calls.meta, (r.text, calls.meta)
image = calls.glimmer[0]["messages"][1]["content"][1]
assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg"), image
assert "Add 10 uL to C2." in calls.glimmer[1]["messages"][1]["content"][0]["text"], "the transcript is the original"
print("PV11 ok: a photo is transcribed on Glimmer first, then extracted, Meta 0")

c = private()
calls = Calls(chat(text=EMPTY_PLAN))
with patch.object(api, "_post", calls):
    r = post_import(c, data={"text": "Add 20 uL to C3."})
    again = post_import(c, data={"text": "Add 20 uL to C3."})
assert r.status_code == 200 and len(calls.glimmer) == 1 and not calls.meta and again.status_code == 200, again.text
print("PV12 ok: text on Glimmer; the same content doesn't extract twice")

c = private()
calls = Calls(chat(text="{not json"))
with patch.object(api, "_post", calls):
    r = post_import(c, data={"text": "Add 21 uL to C3."})
assert r.status_code == 502 and "did not return JSON" in r.text, r.text
print("PV13 ok: Glimmer's output meets the same checks")

long_text = "Add 10 uL to C2. " * 2000
assert len(long_text) > tone.section("private")["import_max_chars"]
c = private()
calls = Calls()
extracted = []


async def fake_once(uploads, text, filename, original_id, h):
    extracted.append(tone.run_mode())
    raise api.HTTPException(502, "stop here")


async def after_yes():
    """A yes spawns the extraction on the running loop."""
    api._spawn_import()
    await asyncio.sleep(0.01)

with patch.object(api, "_post", calls):
    j = post_import(c, data={"text": long_text}).json()
assert j["card"]["kind"] == "import_too_long" and not calls.glimmer and not calls.meta, j
assert j["reply"] == "This file is too long for Private mode. Switch to Full?", j
say(c, "no")
assert tone.run_mode() == "private" and not extracted
with patch.object(api, "_post", calls):
    j = post_import(c, data={"text": long_text}).json()
stages = []
card = j["card"]["id"]
with patch.object(api, "_import_once", fake_once), patch.object(api, "_stage", lambda s, st, **k: stages.append((st, k))):
    api.store.spawn_import = lambda: None  # the task needs a running loop: run it below
    c.post("/input", json={"text": "yes", "session": "t"})
    assert tone.run_mode() == "full" and not calls.meta
    tone.set_run_mode("full")
    asyncio.run(after_yes())
assert extracted == ["full"], extracted
assert ("failed", {"reason": "HTTPException", "say": "I couldn't read that file."}) in stages, stages
print("PV14 ok: too long: asks, no stays, yes reads on Full; a failure there is said")

c = private()
calls = Calls(chat(("perform", {"action": "mark_done", "wells": ["B7"]})))
with patch.object(api, "_post", calls):
    j = c.post("/voice", data={"transcript": "the seventh one in row B, tick it off", "session": "t"}).json()
    k = c.post("/voice", files={"audio": ("c.wav", b"RIFF", "audio/wav")}, data={"session": "t"}).json()
assert "B7" in done_wells(api.store.r) and not calls.meta, (j, calls.meta)
assert k["transcript"] == "" and k["reply"], k
print("PV12 ok: /voice takes the iPad's words; audio sent anyway stops here, ASR 0")

# What leaves: the question, the photo. Never the record -------------------------
SECRET = "SECRETNOTE cloudy near edge"
FORBIDDEN = [SECRET, "Demo protocol", "C6", "step_index", "현재 상태", "\"notes\""]


def clean(bodies):
    text = json.dumps(bodies, ensure_ascii=False)
    hit = [w for w in FORBIDDEN if w in text]
    assert not hit, (hit, text[:400])


class Meta(Calls):
    """Meta answers as each call expects; every body is kept."""

    def __init__(self, *script):
        super().__init__(*script)
        self.bodies = []

    async def __call__(self, client, name, url, **k):
        if url.startswith("http://glimmer.test"):
            return await super().__call__(client, name, url, **k)
        self.bodies.append(k["json"])
        if name == "search":
            return httpx.Response(200, json={"output": [
                {"type": "web_search_call", "results": [{"url": "https://a.org/x", "title": "A"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "Keep it under 0.5%.",
                                                 "annotations": [{"type": "url_citation", "url": "https://a.org/x", "title": "A"}]}]}]})
        if name == "sam":
            return httpx.Response(200, text="")
        out = {"answer_short": "Under 0.5%", "detail": "Fine.", "sources": ["https://a.org/x"], "read": "Drug", "needs_search": False, "query": ""}
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(out)}]}]})


c = private()
api.store.r["notes"].append({"text": SECRET, "target": "C6"})
ask = chat(("web_search", {"query": f"max DMSO for C6 {SECRET}"}))
calls = Meta(ask, ask, chat(text="That fits step 3."))
with patch.object(api, "_post", calls):
    j = say(c, f"what's the max DMSO for C6 {SECRET}")
    assert not calls.bodies and j["cards"][0]["kind"] == "confirm_search", j
    j = say(c, "not now")
    assert j["reply"] == "Okay, nothing was sent." and not calls.bodies, j
    j = say(c, f"what's the max DMSO for C6 {SECRET}")
    j = say(c, "search anyway")
assert len(calls.bodies) == 2 and tone.run_mode() == "private", len(calls.bodies)
clean(calls.bodies)
rec = api.store.searches()[-1]
assert rec["sent"] == "sent to Meta API (Private, confirmed)", rec
assert rec["detail"].endswith("That fits step 3."), rec
print("PV-out1 ok: a search: nothing before yes, no record in it, tied to the run on Glimmer, mode stays")

c = private()
api.store.r["notes"].append({"text": SECRET, "target": "C6"})
calls = Meta(chat(text="NONE"))
crop_bytes = (b"crop", (0, 0, 10, 10))
with patch.object(api, "_post", calls), patch.object(api.lookmod, "matches", lambda t: [{"box": (0, 0, 1, 1), "w": 1, "h": 1}]), \
        patch.object(api.lookmod, "crop", lambda raw, box, size: crop_bytes), patch.object(api.lookmod, "outline", lambda m: []):
    j = say(c, "what does this label say")
    assert j["cards"][0]["kind"] == "confirm_photo" and not calls.bodies, j
    j = say(c, "send anyway")
    rec = next(d for d in api.store.searches() if d.get("kind") == "photo")
    api.store.update_look(rec["id"], question=f"what does this label on C6 say {SECRET}")  # the worst a question holds
    r = c.post("/look", files={"file": ("p.jpg", b"\xff\xd8x", "image/jpeg")}, data={"search_id": rec["id"], "session": "t"})
assert r.status_code == 200 and len(calls.bodies) == 2, (r.text, len(calls.bodies))
clean(calls.bodies)
assert rec["sent"] == "sent to Meta API (Private, confirmed)" and tone.run_mode() == "private"
print("PV-out2 ok: a photo: nothing before yes, the question without the record, mode stays")

c = private()
with patch.object(api, "_post", Calls()):
    j = say(c, "what does this label say")
    j = say(c, "not now")
assert j["reply"] == "Okay, nothing was sent." and not api.store.searches(), j
print("PV-out3 ok: not now sends nothing")

tone.set_run_mode(None)
print("ALL PASS")
