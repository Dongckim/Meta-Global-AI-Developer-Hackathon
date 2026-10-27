"""Conversational turns, asked lifetime, truncation retry. Stubs Meta APIs.

Run: python -m unittest server.tests.test_conversational (runs on import).
"""

import copy
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
from server.grammar import evidence_summary  # noqa: E402
from server.store import RecordStore  # noqa: E402
from server.tone import variants  # noqa: E402

api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api._SESSIONS.clear()
api.CHAIN_ENABLED = False


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


calls = []


class FakeClient:
    spark_payload = {"id": "resp_0", "output": []}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        calls.append((url, kw))
        if url.endswith("/responses"):
            return FakeResp(FakeClient.spark_payload)
        raise AssertionError(url)


class ScriptClient(FakeClient):
    queue = []

    async def post(self, url, **kw):
        calls.append((url, kw))
        if url.endswith("/responses"):
            return FakeResp(ScriptClient.queue.pop(0))
        raise AssertionError(url)


def msg(text, rid="m1"):
    return {"id": rid, "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def fc(name, args, cid):
    return {"type": "function_call", "name": name, "arguments": args,
            "call_id": cid, "status": "completed"}


def snapshot():
    r = api.store.r
    return copy.deepcopy({k: r[k] for k in ("done", "excluded", "notes", "timers", "cards", "step_index")})


def spark_posts():
    return [u for u, _ in calls if u.endswith("/responses")]


with patch("httpx.AsyncClient", FakeClient):
    c = TestClient(api.app)

    # C1. thanks with no tools: model text spoken, record untouched.
    before = snapshot()
    FakeClient.spark_payload = msg("You got it!")
    r = c.post("/orchestrate", json={"text": "got it, thanks", "session": "c1"})
    j = r.json()
    assert j["reply"] == "You got it!" and j["route"] == "spark", j
    assert j["tool_calls"] == [] and len(j["reply"]) <= 280, j
    assert snapshot() == before, "conversational turn must not write"
    print("C1 ok: thanks spoken, no writes")

    # C2. more small talk, same shape.
    for text, line in (("cool", "Cool."), ("okay perfect", "Perfect.")):
        before = snapshot()
        FakeClient.spark_payload = msg(line)
        j = c.post("/orchestrate", json={"text": text, "session": "c2"}).json()
        assert j["reply"] == line and j["tool_calls"] == [], j
        assert snapshot() == before
    print("C2 ok: cool / okay perfect")

    # C3. asked expires after one turn without an answer; thanks writes nothing.
    sess = api.get_session("c3")
    sess["turns"].append({"text": "start the timer", "calls": [], "messages": ["How long?"],
                          "asked": {"question": "How long?", "target": "D5"}})
    assert "asked:" in evidence_summary(sess["turns"])
    before = snapshot()
    FakeClient.spark_payload = msg("Anytime!")
    j = c.post("/orchestrate", json={"text": "thanks", "session": "c3"}).json()
    assert j["reply"] == "Anytime!" and snapshot() == before, j
    assert "asked:" not in evidence_summary(sess["turns"]), evidence_summary(sess["turns"])
    assert "asked:" not in " ".join(api.store.model_state(sess)["recent_turns"])
    print("C3 ok: asked expires, thanks writes nothing")

    # C4. truncated once: same request retried, second answer wins.
    ScriptClient.queue = [
        {"id": "c4a", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        msg("On it.", "c4b"),
    ]
    n_before = len(spark_posts())
    with patch("httpx.AsyncClient", ScriptClient):
        j = c.post("/orchestrate", json={"text": "got it", "session": "c4"}).json()
    assert j["reply"] == "On it.", j
    assert len(spark_posts()) - n_before == 2, "cut must retry exactly once"
    print("C4 ok: truncation retried once")

    # C5. truncated twice: didnt_catch, still two posts total.
    ScriptClient.queue = [
        {"id": "c5a", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        {"id": "c5b", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    ]
    n_before = len(spark_posts())
    with patch("httpx.AsyncClient", ScriptClient):
        j = c.post("/orchestrate", json={"text": "got it", "session": "c5"}).json()
    assert j["reply"] in variants("didnt_catch"), j
    assert len(spark_posts()) - n_before == 2, "no third attempt"
    print("C5 ok: double cut is didnt_catch")

    # C6. write contracts record reported facts, never bench instructions.
    descs = {t["name"]: t["description"] for t in api.TOOLS if t.get("type") == "function"}
    assert "REPORTS" in descs["mark_done"] and "wash the remaining" in descs["mark_done"]
    assert "bench instructions" in descs["exclude_well"]
    assert "reports damage" in descs["record_redo"]
    assert "next physical action" in descs["next_step"]
    print("C6 ok: report-vs-instruction contracts locked")

    # C7. text claiming action without tools: didnt_act + asked target, 0 writes.
    before = snapshot()
    FakeClient.spark_payload = msg("Marked C2–C8 done")
    r = c.post("/orchestrate", json={"text": "Marked C2–C8 done", "session": "c7"})
    j = r.json()
    assert j["reply"] == "I didn't log anything — want me to?", j
    assert j["tool_calls"] == [] and snapshot() == before, j
    assert api._SESSIONS["c7"]["turns"][-1]["asked"] == {
        "question": j["reply"], "target": "C2, C8"}, j
    print("C7 ok: phantom claim becomes didnt_act")

    # C8. silent round 2 over notes: found phrase carries the read data.
    c.post("/orchestrate", json={"text": "note C4, edge effect", "session": "c8"})
    ScriptClient.queue = [
        {"id": "c8a", "output": [fc("read_record", '{"kind": "notes"}', "c1")]},
        {"id": "c8b", "output": []},
    ]
    with patch("httpx.AsyncClient", ScriptClient):
        j = c.post("/orchestrate", json={"text": "read my notes", "session": "c8"}).json()
    assert j["reply"] == "Here's what I found: C4: edge effect.", j
    assert [x["name"] for x in j["tool_calls"]] == ["read_record"], j
    print("C8 ok: speechless read renders data")

    # C9. validation drops speak couldnt-lines, never sorry.
    FakeClient.spark_payload = {"id": "c9a", "output": [
        fc("mark_done", '{"wells": "C99"}', "c1")]}
    before = snapshot()
    j = c.post("/orchestrate", json={"text": "mark C99 done", "session": "c9"}).json()
    FakeClient.spark_payload = {"id": "resp_0", "output": []}
    assert j["reply"] == "I couldn't record that: no valid wells.", j
    assert snapshot() == before
    print("C9 ok: dropped wells speak honestly")

    # C10. model returns nothing usable: didnt_act, not sorry.
    FakeClient.spark_payload = {"id": "c10a", "output": []}
    j = c.post("/orchestrate", json={"text": "hmm well", "session": "c10"}).json()
    assert j["reply"] == "I didn't log anything — want me to?", j
    assert api._SESSIONS["c10"]["turns"][-1]["asked"]["target"] == "", j
    print("C10 ok: empty model output is didnt_act")

    # C11. executed-reply map and read renderer, direct.
    assert api._executed_reply([{"name": "open", "args": {}}], [], {}) in variants("opened")
    assert api._executed_reply([{"name": "read_record", "args": {}}], [], {}) in variants(
        "found_empty")
    assert api._render_record("timers", {"timers": [{"label": "L", "minutes_left": 3.5}]}) == [
        "L (3.5m left)"]
    assert api._render_record("steps", {"steps": [{"i": 3, "name": "drug A", "current": True}]}) == [
        "Step 3: drug A (current)"]
    assert api._render_record("history", {"history": [{"action": "mark_done", "wells": ["C2"],
                                                            "ago": "5m"}]}) == ["mark_done C2 5m ago"]
    assert api._render_r1_reads(
        [{"name": "find_free_wells", "args": {}, "call_id": "x"}],
        {"x": '{"near": "C3", "free": ["C9", "C10"]}'}) == ["Free near C3: C9, C10"]
    print("C11 ok: executed map + renderer shapes")

print("ALL PASS")
