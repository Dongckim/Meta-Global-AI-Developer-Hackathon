"""iPad-log regressions for server/api.py. Stubs Meta APIs, asserts outcomes.

Run: python -m unittest server.tests.test_regressions (runs on import).
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
api.store.r["step_index"] = 2  # mid-run fixture, as before
api._SESSIONS.clear()
api.CHAIN_ENABLED = False  # summary mode: recent turns carry asked-lines


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeClient:
    spark_payload = {"id": "resp_0", "output": []}
    asr_payload = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        if url.endswith("/responses"):
            return FakeResp(FakeClient.spark_payload)
        raise AssertionError(url)


def fc(name, args, cid):
    return {"type": "function_call", "name": name, "arguments": args,
            "call_id": cid, "status": "completed"}


class ScriptClient(FakeClient):
    queue = []

    async def post(self, url, **kw):
        if url.endswith("/responses"):
            return FakeResp(ScriptClient.queue.pop(0))
        raise AssertionError(url)


with patch("httpx.AsyncClient", FakeClient):
    c = TestClient(api.app)

    # R1. cancel + retarget in one turn; minutes live in the utterance.
    FakeClient.spark_payload = {"id": "r1a", "output": [
        fc("set_timer", '{"label": "x", "minutes": 5, "wells": "C5"}', "c1")]}
    r = c.post("/orchestrate", json={"text": "start the timer for C5, five minutes", "session": "r1"})
    assert "I'll ping you" in r.json()["messages"][0], r.json()
    FakeClient.spark_payload = {"id": "r1b", "output": [
        fc("cancel_timer", '{"label": "C5"}', "c2"),
        fc("set_timer", '{"label": "x", "minutes": 2, "wells": "D5"}', "c3")]}
    r = c.post("/orchestrate", json={"text": "cancel C5, I need D5 for 2 minutes", "session": "r1"})
    j = r.json()
    assert "How many minutes" not in j["reply"], j
    assert any("timer's off" in m for m in j["messages"]), j
    assert any("D5" in m and "2 min" in m for m in j["messages"]), j
    labels = [t["label"] for t in api.store.r["timers"]]
    assert any("D5" in lab for lab in labels) and not any("C5" in lab for lab in labels), labels
    print("R1 ok: cancel C5 + D5 2min, no re-ask")

    # R2. a bare "two minutes" answers the asked question for its target.
    r = c.post("/orchestrate", json={"text": "C2 to C8 done", "session": "r2"})
    assert r.json()["route"] == "grammar", r.json()
    FakeClient.spark_payload = {"id": "r2a", "output": [
        fc("set_timer", '{"label": "x"}', "c1")]}
    r = c.post("/orchestrate", json={"text": "start a timer for each", "session": "r2"})
    assert r.json()["reply"] == "Sure — how long?", r.json()
    target = api._SESSIONS["r2"]["turns"][-1]["asked"]["target"]
    assert "C2" in target and "C8" in target, target
    FakeClient.spark_payload = {"id": "r2b", "output": [
        fc("set_timer", '{"label": "x", "minutes": 2}', "c2")]}
    r = c.post("/orchestrate", json={"text": "two minutes", "session": "r2"})
    j = r.json()
    assert j["reply"] == f"7 2-minute timers for {target}.", j
    assert any("2 min" in m for m in j["messages"]), j
    print("R2 ok: follow-up minutes hit the asked target")

    # R3. "read all my notes" speaks a summary and opens the list.
    c.post("/orchestrate", json={"text": "note C3, big bubbles near the edge", "session": "r3"})
    c.post("/orchestrate", json={"text": "note C5, sticky edge", "session": "r3"})
    summary = "You have 2 notes: C3 has big bubbles, C5 is sticky."
    ScriptClient.queue = [
        {"id": "r3a", "output": [fc("read_record", '{"kind": "notes"}', "c1")]},
        {"id": "r3b", "output": [fc("navigate", '{"screen": "main"}', "c3"),
                                 {"type": "message", "content": [{"type": "output_text", "text": summary}]}]},
    ]
    with patch("httpx.AsyncClient", ScriptClient):
        r = c.post("/orchestrate", json={"text": "read all my notes", "session": "r3"})
    j = r.json()
    # a read answer: the summary alone is the reply. The move's "Here's ..."
    # line is left out (the screen shows it) and never glued to the summary.
    assert j["reply"] == summary, j
    assert api.store.screen_line("main") in j["messages"], j  # the move still happened (the bench's line)
    assert [x["name"] for x in j["tool_calls"]] == ["read_record", "navigate"], j
    assert api.store.r["view"]["current"]["screen"] == "main", j
    # same without the model's line: the read itself is said, not the move
    ScriptClient.queue = [
        {"id": "r3e", "output": [fc("read_record", '{"kind": "notes"}', "c1")]},
        {"id": "r3f", "output": [fc("navigate", '{"screen": "main"}', "c3")]},
    ]
    with patch("httpx.AsyncClient", ScriptClient):
        j = c.post("/orchestrate", json={"text": "read all my notes", "session": "r3"}).json()
    assert "C3" in j["reply"] and api.store.screen_line("main") not in j["reply"], j
    # a model line that names nothing it read isn't a summary: the read is said
    ScriptClient.queue = [
        {"id": "r3g", "output": [fc("read_record", '{"kind": "notes"}', "c1")]},
        {"id": "r3h", "output": [fc("navigate", '{"screen": "main"}', "c3"),
                                 {"type": "message", "content": [{"type": "output_text",
                                  "text": "Got it — pulling up your notes now."}]}]},
    ]
    with patch("httpx.AsyncClient", ScriptClient):
        j = c.post("/orchestrate", json={"text": "read all my notes", "session": "r3"}).json()
    assert "C3" in j["reply"] and "pulling up" not in j["reply"], j
    print("R3 ok: notes summary is the reply, the screen move is silent")

    # R3b. model says success, the store failed: only the failure is said.
    ScriptClient.queue = [
        {"id": "r3c", "output": [fc("read_record", '{"kind": "protocols"}', "c1")]},
        {"id": "r3d", "output": [fc("navigate", '{"screen": "protocol_source", "target": "protocol:nope"}', "c3"),
                                 {"type": "message", "content": [{"type": "output_text",
                                  "text": "Nice — showing the importer so you can add a new protocol."}]}]},
    ]
    with patch("httpx.AsyncClient", ScriptClient):
        r = c.post("/orchestrate", json={"text": "I want to import a new protocol", "session": "r3b"})
    j = r.json()
    # an unknown protocol: a store failure
    assert j["reply"] == "I couldn't open that original: which protocol?", j
    assert ", but" not in j["reply"] and "Nice" not in j["reply"], j
    # importer is a registry sheet: navigate opens it as ui, no view change
    ScriptClient.queue = [{"id": "r3e", "output": [fc("navigate", '{"screen": "importer"}', "c1")]}]
    before = api.store.r["view"]["current"]
    with patch("httpx.AsyncClient", ScriptClient):
        r = c.post("/orchestrate", json={"text": "add a new protocol", "session": "r3c"})
    j = r.json()
    assert j["reply"] == "Opening the importer." and {"action": "import"} in (j.get("ui") or []), j
    assert api.store.r["view"]["current"] == before, api.store.r["view"]
    print("R3b ok: result-only replies, importer from the registry")

    # R3c. unusable model calls (perform without action, a tool without a
    # name) never crash the turn: they drop at the gate and the user is asked,
    # with the registry's best-matching screens as the choices.
    ScriptClient.queue = [{"id": "r3f", "output": [
        {"type": "function_call", "name": "perform", "arguments": "{}", "call_id": "c1"},
        {"type": "function_call", "name": None, "arguments": "{}", "call_id": "c2"}]}]
    with patch("httpx.AsyncClient", ScriptClient):
        r = c.post("/orchestrate", json={"text": "let's move on to the recent run.", "session": "r3f"})
    j = r.json()
    assert r.status_code == 200 and j["reply"] == "A past run or the active run?", j
    assert j["tool_calls"] == [], j
    assert api._compose_reply([], {"messages": [None, "", "Ok."], "cards": [],
                                   "info": {"drop_reasons": []}}, {}, {}) == "Ok."
    print("R3c ok: unusable calls ask; assembly is None-safe")

    # R4. "change C5's memo" edits that well's note, keeping history.
    c.post("/orchestrate", json={"text": "note C5, sticky edge", "session": "r4"})
    FakeClient.spark_payload = {"id": "r4a", "output": [
        fc("edit_note", '{"well": "C5", "text": "no longer sticky"}', "c1")]}
    r = c.post("/orchestrate", json={"text": "change C5's memo, no longer sticky", "session": "r4"})
    j = r.json()
    assert j["messages"] == ["Note updated."], j
    assert j["reply"] == "Note updated (C5).", j
    memo = api.store.r["notes"][-1]
    assert memo["text"] == "no longer sticky", memo
    assert [h["text"] for h in memo["history"]] == ["sticky edge"], memo
    print("R4 ok: well-targeted memo edit with history")

print("ALL PASS")
