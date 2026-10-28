"""Pip v2 structural tests. Stubs Meta APIs, asserts outcomes.

Run: python -m unittest server.tests.test_v2 (runs on import).
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
from server.tone import say, variants  # noqa: E402

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


def fc(name, args, cid):
    return {"type": "function_call", "name": name, "arguments": args,
            "call_id": cid, "status": "completed"}


class ScriptClient(FakeClient):
    queue = []

    async def post(self, url, **kw):
        calls.append((url, kw))
        if url.endswith("/responses"):
            return FakeResp(ScriptClient.queue.pop(0))
        raise AssertionError(url)


with patch("httpx.AsyncClient", FakeClient):
    c = TestClient(api.app)

    # V1. voice "yes" resolves the card: 0 model calls, card gone, C8 out.
    FakeClient.spark_payload = {"id": "v1a", "output": [
        fc("exclude_well", '{"well": "C8", "reason": "cloudy"}', "c1")]}
    r = c.post("/orchestrate", json={"text": "C8 looks cloudy, drop it", "session": "v1"})
    card_id = r.json()["cards"][0]["id"]
    assert r.json()["cards"][0]["question"] == "Drop C8?", r.json()
    n_before = len(calls)
    r = c.post("/orchestrate", json={"text": "yes", "session": "v1"})
    j = r.json()
    assert len(calls) == n_before, "card answer must not call the model"
    assert j["route"] == "card" and j["ms"] == {"spark": 0}, j
    assert j["reply"] == "C8 excluded (cloudy)", j
    assert card_id not in api.store.r["cards"] and "C8" in api.store.r["excluded"]
    print("V1 ok: voice yes resolves card, 0 model calls")

    # V2. same card twice replaces; still one card.
    FakeClient.spark_payload = {"id": "v2a", "output": [
        fc("exclude_well", '{"well": "C8", "reason": "cloudy"}', "c1")]}
    c.post("/orchestrate", json={"text": "drop C8", "session": "v2"})
    first = list(api.store.r["cards"])
    c.post("/orchestrate", json={"text": "drop C8 again", "session": "v2"})
    ids = [cid for cid, card in api.store.r["cards"].items()
           if (card.get("payload") or {}).get("well") == "C8"]
    assert len(ids) == 1 and ids != first, ids
    print("V2 ok: same card replaces, never stacks")

    # V3. memo keeps model-cleaned text plus the full raw origin; screen reacts.
    origin = ("um i wanna make some notes on C eight and I wanna write down "
              "the reagent is a little bit sticky")
    FakeClient.spark_payload = {"id": "v3a", "output": [
        fc("add_note", '{"target": "C8", "text": "Reagent is a little sticky."}', "c1")]}
    r = c.post("/orchestrate", json={"text": origin, "session": "v3"})
    j = r.json()
    memo = api.store.r["notes"][-1]
    assert memo["text"] == "Reagent is a little sticky.", memo
    assert memo["raw"] == origin, memo
    assert memo["wells"] == ["C8"], memo
    assert {"action": "flash", "wells": ["C8"]} in j["ui"], j
    assert {"action": "peek", "target": "C8", "seconds": 3} in j["ui"], j
    print("V3 ok: memo text+raw, flash+peek")

    # V4. article-number timers ground: "a 25 minute" and "a 5 minute".
    FakeClient.spark_payload = {"id": "v4a", "output": [
        fc("set_timer", '{"label": "x", "minutes": 25}', "c1")]}
    r = c.post("/orchestrate", json={"text": "start a 25 minute timer", "session": "v4"})
    assert r.json()["messages"] == ["25 minutes. I'll ping you."], r.json()
    FakeClient.spark_payload = {"id": "v4b", "output": [
        fc("set_timer", '{"label": "x", "minutes": 5, "wells": "C2"}', "c2")]}
    r = c.post("/orchestrate", json={"text": "a 5 minute timer for C2", "session": "v4"})
    assert r.json()["messages"] == ["Timer's on: 5 minutes for C2."], r.json()
    print("V4 ok: article-number minutes ground directly")

    # V5. "C nine nine" is uncertain: grammar defers, model asks.
    from server.grammar import match_grammar
    assert match_grammar("note C nine nine test", {}) is None
    ScriptClient.queue = [
        {"id": "v5a", "output": [fc("ask", '{"question": "Which well?"}', "c1")]},
    ]
    with patch("httpx.AsyncClient", ScriptClient):
        r = c.post("/orchestrate", json={"text": "note C nine nine test", "session": "v5"})
    assert r.json()["route"] == "spark" and r.json()["reply"] == "Which well?", r.json()
    print("V5 ok: uncertain wells defer to the model")

    # V6. redo via backup candidates, five fresh runs.
    for i in range(5):
        api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
        ScriptClient.queue = [
            {"id": f"v6a{i}", "output": [
                fc("find_free_wells", '{"near": "C3", "count": 3}', "c1")]},
            {"id": f"v6b{i}", "output": [
                fc("exclude_well", '{"well": "C3", "reason": "bubble"}', "c2"),
                fc("record_redo", '{"wells": ["C9"], "note": "bubble"}', "c3")]},
        ]
        with patch("httpx.AsyncClient", ScriptClient):
            r = c.post("/orchestrate", json={
                "text": "C3 has a bubble, redo it in a backup well", "session": f"v6-{i}"})
        j = r.json()
        assert [x["question"] for x in j["cards"]] == ["Drop C3?", "Redo C9?"], (i, j)
    print("V6 ok: redo backup flow x5")

    # V7. previous-turn minutes need a confirm card, then D5 runs 2 min.
    FakeClient.spark_payload = {"id": "v7a", "output": [
        fc("set_timer", '{"label": "x", "minutes": 2, "wells": "C5"}', "c1")]}
    c.post("/orchestrate", json={"text": "2 minute timer for C5", "session": "v7"})
    FakeClient.spark_payload = {"id": "v7b", "output": [
        fc("manage_timers", '{"action": "cancel", "label": "C5"}', "c2"),
        fc("set_timer", '{"label": "x", "minutes": 2, "wells": "D5"}', "c3")]}
    r = c.post("/orchestrate", json={"text": "oh no cancel C five I need D five", "session": "v7"})
    j = r.json()
    assert any("timer's off" in m for m in j["messages"]), j
    assert len(j["cards"]) == 1, j
    assert j["cards"][0]["question"] == "2 minutes on D5, like before?", j
    n_before = len(calls)
    r = c.post("/input", json={"card_id": j["cards"][0]["id"], "answer": "yes", "session": "v7"})
    j = r.json()
    assert len(calls) == n_before, "card answer must not call the model"
    assert j["route"] == "card" and j["reply"] == "Timer's on: 2 minutes for D5.", j
    assert any(t["minutes"] == 2 and t.get("wells") == "D5" for t in api.store.r["timers"])
    print("V7 ok: reuse card then D5 runs 2 min")

    # V8. tone rotates: same key never repeats twice in a row.
    sess: dict = {}
    want = [t.format(wells="C2", wells_spoken="C2") for t in variants("mark_done")]
    got = [say("mark_done", sess, wells="C2") for _ in range(3)]
    assert got == want, got
    assert say("mark_done", {}, wells="C2") == want[0]
    print("V8 ok: tone rotation")

    # V10. summary-mode round 2 replays round 1 inline (store:false can't
    # reference a previous response): question, call echo, bound output.
    c.post("/orchestrate", json={"text": "note C3, big bubbles", "session": "v10"})
    ScriptClient.queue = [
        {"id": "v10a", "output": [fc("read_record", '{"kind": "wells", "filter": "C3"}', "c1")]},
        {"id": "v10b", "output": [fc("answer", '{"text": "C3 has bubbles."}', "c2")]},
    ]
    seen = []
    orig_post = ScriptClient.post

    async def spy(self, url, **kw):
        if url.endswith("/responses"):
            seen.append(kw["json"])
        return await orig_post(self, url, **kw)

    with patch("httpx.AsyncClient", ScriptClient):
        with patch.object(ScriptClient, "post", spy):
            r = c.post("/orchestrate", json={"text": "what is on C3", "session": "v10"})
    assert r.json()["reply"] == "C3 has bubbles.", r.json()
    assert "previous_response_id" not in seen[1], seen[1]
    inp = seen[1]["input"]
    assert isinstance(inp, list), inp
    assert inp[0] == {"role": "user", "content": "what is on C3"}, inp
    assert inp[1]["type"] == "function_call" and inp[1]["call_id"] == "c1", inp
    assert inp[1]["name"] == "read_record", inp
    assert inp[2]["type"] == "function_call_output" and inp[2]["call_id"] == "c1", inp
    assert "big bubbles" in inp[2]["output"], inp
    assert inp[-1] == {"role": "user", "content": "Continue with the looked-up data."}, inp
    print("V10 ok: summary round 2 replays round 1 inline")

    # V9. /input text path, /health version, alias shapes.
    r = c.post("/input", json={"text": "C2 done", "session": "v9"})
    assert r.json()["route"] == "grammar", r.json()
    assert c.post("/input", json={"text": " ", "session": "v9"}).status_code == 400
    h = c.get("/health").json()
    assert h["ok"] and h["commit"] and h["tools"] == 19 and h["prompt_hash"], h
    assert h["memory"] == "summary", h
    FakeClient.spark_payload = {"id": "v9a", "output": [
        fc("exclude_well", '{"well": "C4"}', "c1")]}
    r = c.post("/orchestrate", json={"text": "drop C4", "session": "v9"})
    r = c.post("/confirm", json={"card_id": r.json()["cards"][0]["id"], "ok": True, "session": "v9"})
    assert set(r.json()) == {"message", "cards", "record_version"}, r.json()
    print("V9 ok: input, health, aliases")

    # V11. bare yes answers what Pip asked last: step-move waits, new
    # exclude asks, yes resolves only the exclude. 0 model calls.
    api.store.r["cards"].clear()
    FakeClient.spark_payload = {"id": "v11a", "output": [fc("next_step", "{}", "c1")]}
    r = c.post("/orchestrate", json={"text": "move to step 2", "session": "v11"})
    assert r.json()["cards"] and (r.json()["cards"][0]["payload"] or {}).get("op") == "next_step"
    FakeClient.spark_payload = {"id": "v11b", "output": [
        fc("exclude_well", '{"well": "C6", "reason": "cloudy"}', "c2")]}
    r = c.post("/orchestrate", json={"text": "drop C6", "session": "v11"})
    assert "C6" in r.json()["cards"][0]["question"], r.json()
    n_before = len(calls)
    r = c.post("/orchestrate", json={"text": "yes", "session": "v11"})
    j = r.json()
    assert len(calls) == n_before, "card answer must not call the model"
    assert j["route"] == "card" and "excluded" in j["reply"], j
    assert "C6" in api.store.r["excluded"], j
    ops = [(card.get("payload") or {}).get("op") for card in api.store.r["cards"].values()]
    assert ops == ["next_step"], ops
    print("V11 ok: yes hits the newest-asked card only")

    # V12. two cards asked in one breath: bare yes asks which one, keeps both.
    FakeClient.spark_payload = {"id": "v12a", "output": [
        fc("exclude_well", '{"well": "C7", "reason": "bubble"}', "c1"),
        fc("record_redo", '{"wells": "C10", "note": "bubble"}', "c2")]}
    r = c.post("/orchestrate", json={"text": "C7 has a bubble, redo in C10", "session": "v12"})
    assert len(r.json()["cards"]) == 2, r.json()
    n_before = len(calls)
    r = c.post("/orchestrate", json={"text": "yes", "session": "v12"})
    j = r.json()
    assert len(calls) == n_before, "disambiguation must not call the model"
    assert j["route"] == "card", j
    assert j["reply"].startswith("Which one — ") and j["reply"].endswith("?"), j
    assert "C7" in j["reply"] and "C10" in j["reply"], j
    assert len(api.store.r["cards"]) == 3, api.store.r["cards"]  # + V11 step card
    print("V12 ok: ambiguous yes asks which one")

print("ALL PASS")
