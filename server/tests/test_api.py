"""API self-check for server/api.py. Stubs Meta APIs, asserts shapes.

Run: python -m unittest server.tests.test_api (runs on import).
"""

import io
import json
import os
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api._SESSIONS.clear()
api.CHAIN_ENABLED = True  # chain-mode assertions below; #33 covers summary mode

SPARK_OK = {
    "id": "resp_1",
    "output": [
        {"type": "function_call", "name": "mark_done", "arguments": '{"wells": ["C3"]}',
         "call_id": "c1", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": "C3 done"}]},
    ],
}
ASR_OK = {"sessionId": "s1", "transcript": "C3 분주 끝", "audioDurationMs": 800, "turns": []}

calls = []


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeClient:
    spark_payload = SPARK_OK
    asr_payload = ASR_OK

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
        if url.endswith("/asr/transcribe"):
            return FakeResp(FakeClient.asr_payload)
        raise AssertionError(url)


def make_wav(channels=1, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate // 4))  # 0.25s silence
    return buf.getvalue()


with patch("httpx.AsyncClient", FakeClient):
    c = TestClient(api.app)

    # 1. /orchestrate happy path: validated, applied, recorded
    r = c.post("/orchestrate", json={"text": "C3 all set", "state": {"step": 99}, "session": "s1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["tool_calls"] == [{"name": "mark_done", "args": {"wells": ["C3"]}}], j
    assert j["messages"] == ["✓ C3 done"], j
    assert j["reply"] == "✓ C3 done", j
    assert j["route"] == "spark", j
    assert j["cards"] == [] and j["record_version"] >= 1, j
    assert "C3" in api.store.r["done"], api.store.r["done"]
    url, kw = calls[-1]
    assert url == "https://api.meta.ai/v1/responses", url
    assert kw["headers"]["Authorization"] == f"Bearer {os.environ['MODEL_API_KEY']}"
    body = kw["json"]
    assert body["model"] == "muse-spark-1.3", body
    assert body["reasoning"] == {"effort": "minimal"}, body
    assert body["max_output_tokens"] == 1024, body
    assert len(body["tools"]) == 18, len(body["tools"])
    assert not [t for t in body["tools"] if t.get("type") == "web_search"], "flag off by default"
    names = [t.get("name") for t in body["tools"] if t.get("type") == "function"]
    assert "get_well" in names and "find_free_wells" in names, names
    assert "propose_plan" not in names, names
    assert {"navigate", "open", "back"} <= set(names), names
    assert "answer_card" not in names and "confirm" not in names, names
    nav = next(t for t in body["tools"] if t.get("name") == "navigate")
    assert nav["parameters"]["properties"]["screen"]["enum"] == ["main", "robot_preview", "well_card"], nav
    for t in body["tools"]:
        if t.get("type") != "function":
            continue
        p = t["parameters"]
        assert t.get("strict") is True, t["name"]
        assert p.get("additionalProperties") is False, t["name"]
        assert sorted(p.get("required", [])) == sorted(p["properties"]), t["name"]
    assert body["prompt_cache_key"] == "pip-bench-spark", body
    assert body["prompt_cache_retention"] == "24h", body
    assert "store" not in body, "chain mode stores responses"
    assert "previous_response_id" not in body, body
    assert body["instructions"].startswith("You are Pip"), body["instructions"][:40]
    assert "tool calls only" in body["instructions"] and "ask(question)" in body["instructions"]
    assert "pending_question" in body["instructions"] and '"recent"' in body["instructions"]
    assert "propose the first candidate" in body["instructions"] and "first step" in body["instructions"]
    assert '"step": 99' not in body["instructions"], "client state ignored; record is truth"
    assert '"step": "3/5' in body["instructions"]
    assert '"groups": "ctrl' in body["instructions"]
    assert '"plan_times": "5: 30m"' in body["instructions"]
    assert '"step_wells": "C2–C8"' in body["instructions"], body["instructions"]
    assert body["input"] == "C3 all set"
    print("1 ok: /orchestrate applies + record truth + 18 tools")

    # 2. /orchestrate validation
    assert c.post("/orchestrate", json={"text": ""}).status_code == 422
    assert c.post("/orchestrate", json={"state": {}}).status_code == 422
    print("2 ok: /orchestrate 422s")

    # 3. no-tool reply
    FakeClient.spark_payload = {"id": "resp_x", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "네"}]}]}
    r = c.post("/orchestrate", json={"text": "고마워", "session": "s3"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert j["tool_calls"] == [] and j["messages"] == [], j
    assert j["reply"] == "I didn't catch a command." and j["route"] == "spark", j
    print("3 ok: empty tool_calls")

    # 4. /voice happy path
    wav = make_wav()
    r = c.post("/voice", files={"audio": ("clip.wav", wav, "audio/wav")},
               data={"state": '{"step": 1}', "session": "s4"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["transcript"] == "C3 분주 끝", j
    assert j["tool_calls"] == [{"name": "mark_done", "args": {"wells": ["C3"]}}], j
    assert j["messages"] == ["✓ C3 done"], j
    assert j["reply"] == "✓ C3 done" and j["route"] == "spark", j
    assert set(j["ms"]) == {"asr", "spark", "total"}, j
    aurl, akw = calls[-2]
    assert aurl == "https://api.meta.ai/v1/asr/transcribe", aurl
    req_part = json.loads(akw["files"]["request"][1])
    assert req_part["mode"] == "PUSH_TO_TALK" and req_part["model"] == "muse-voice-transcribe-1.0", req_part
    assert req_part["audioEncoding"] == "WAV" and req_part["languageBias"] == ["English"], req_part
    assert len(req_part["keywords"]) == 114
    assert calls[-1][1]["json"]["input"] == "C3 분주 끝"
    print("4 ok: /voice shape + spark chaining")

    # 5. /voice rejects bad audio
    assert c.post("/voice", files={"audio": ("s.wav", make_wav(channels=2), "audio/wav")}).status_code == 400
    assert c.post("/voice", files={"audio": ("x.wav", b"not-a-wav", "audio/wav")}).status_code == 400
    assert c.post("/voice", files={"audio": ("c.wav", make_wav(), "audio/wav")},
                  data={"state": "nope"}).status_code == 400
    print("5 ok: /voice 400s")

    # 6. missing key -> 500
    del os.environ["MODEL_API_KEY"]
    assert c.post("/orchestrate", json={"text": "hi"}).status_code == 500
    assert c.post("/voice", files={"audio": ("c.wav", wav, "audio/wav")}).status_code == 500
    os.environ["MODEL_API_KEY"] = "dummy"
    print("6 ok: missing key 500s")

    # 7. upstream error -> 502
    class ErrClient(FakeClient):
        async def post(self, url, **kw):
            return FakeResp({"error": "boom"}, status=500)

    with patch("httpx.AsyncClient", ErrClient):
        assert c.post("/orchestrate", json={"text": "hi"}).status_code == 502
        r = c.post("/voice", files={"audio": ("c.wav", wav, "audio/wav")})
        assert r.status_code == 502, r.status_code
    print("7 ok: upstream 500 -> 502")

    # 8. transport error -> 502
    import httpx as _httpx

    class DeadClient(FakeClient):
        async def post(self, url, **kw):
            raise _httpx.ConnectError("no route")

    with patch("httpx.AsyncClient", DeadClient):
        r = c.post("/orchestrate", json={"text": "hi"})
        assert r.status_code == 502 and "unreachable" in r.text, r.text
    print("8 ok: transport error -> 502")

    # 9. empty transcript -> friendly 200, no spark call
    import contextlib
    from pathlib import Path as _Path

    FakeClient.asr_payload = {"sessionId": "s1", "transcript": "  ", "audioDurationMs": 800, "turns": []}
    _Path("/tmp/last.wav").unlink(missing_ok=True)
    n_before = len(calls)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = c.post("/voice", files={"audio": ("c.wav", wav, "audio/wav")})
    FakeClient.asr_payload = ASR_OK
    j = r.json()
    assert j["transcript"] == "" and j["tool_calls"] == [] and j["messages"] == [], j
    assert j["reply"] == "I didn't catch that, please say it again.", j
    assert all(not u.endswith("/responses") for u, _ in calls[n_before:])
    assert _Path("/tmp/last.wav").read_bytes() == wav
    assert 'dur=0.25s transcript="  " norm=""' in buf.getvalue(), buf.getvalue()
    print("9 ok: empty transcript short-circuit")

    # 10. grammar routes apply + card
    n_before = len(calls)
    r = c.post("/orchestrate", json={"text": "see four to six done", "session": "s10"})
    j = r.json()
    assert j["route"] == "grammar", j
    assert j["tool_calls"] == [{"name": "mark_done",
        "args": {"wells": ["C4", "C5", "C6"]}}], j
    assert j["messages"] == ["✓ C4, C5, C6 done"], j
    assert j["reply"] == "✓ C4, C5, C6 done", j
    assert j["ms"] == {"spark": 0}, j
    r = c.post("/orchestrate", json={"text": "how many left", "session": "s10"})
    assert r.json()["route"] == "grammar", r.json()
    assert r.json()["reply"].endswith("left: C2, C7, C8."), r.json()
    r = c.post("/orchestrate", json={"text": "exclude C7, contaminated", "session": "s10"})
    j = r.json()
    assert j["route"] == "grammar" and j["reply"] == "Exclude C7?", j
    assert j["cards"] and j["cards"][0]["detail"] == "contaminated", j
    assert "C7" not in api.store.r["excluded"]
    r = c.post("/orchestrate", json={"text": "note C3, I see bubbles", "session": "s10"})
    j = r.json()
    assert j["tool_calls"] == [{"name": "add_note", "args": {"target": "C3", "text": "I see bubbles"}}], j
    assert j["saved_memo"]["text"] == "I see bubbles", j
    r = c.post("/orchestrate", json={"text": "edit last note, no bubbles actually", "session": "s10"})
    assert r.json()["reply"] == "Note updated (C3).", r.json()
    r = c.post("/orchestrate", json={"text": "next", "session": "s10"})
    j = r.json()
    assert len(j["cards"]) == 1 and j["cards"][0]["kind"] == "next_step", j
    assert j["reply"].startswith("Move to step 4 ("), j
    assert all(not u.endswith("/responses") for u, _ in calls[n_before:]), calls[n_before:]
    print("10 ok: grammar applies, cards, no spark")

    # 11. chain memory: id chaining + re-anchor at cap
    r = c.post("/orchestrate", json={"text": "hi there", "session": "mem"})
    assert "previous_response_id" not in calls[-1][1]["json"]
    r = c.post("/orchestrate", json={"text": "and more", "session": "mem"})
    assert calls[-1][1]["json"]["previous_response_id"] == "resp_1", calls[-1][1]["json"]
    assert '"recent"' in calls[-1][1]["json"]["instructions"]
    assert '"mark_done"' in calls[-1][1]["json"]["instructions"], "grammar turn visible to model"
    api._SESSIONS["mem"]["memory"]._turns = 8
    r = c.post("/orchestrate", json={"text": "after cap", "session": "mem"})
    assert "previous_response_id" not in calls[-1][1]["json"]
    assert "Conversation so far" in calls[-1][1]["json"]["instructions"]
    print("11 ok: chain + re-anchor + recent context")

    # 12. chained 4xx (any reason) restarts fresh and retries once
    r = c.post("/orchestrate", json={"text": "chain me", "session": "mem2"})
    assert "previous_response_id" not in calls[-1][1]["json"]

    class SeqClient(FakeClient):
        n = 0

        async def post(self, url, **kw):
            calls.append((url, kw))
            if url.endswith("/responses"):
                SeqClient.n += 1
                if SeqClient.n == 1:
                    return FakeResp({"status": "error", "message": "stale chain"}, status=400)
                return FakeResp(SPARK_OK)
            return FakeResp(FakeClient.asr_payload)

    with patch("httpx.AsyncClient", SeqClient):
        r = c.post("/orchestrate", json={"text": "retry me", "session": "mem2"})
    assert r.status_code == 200 and r.json()["messages"] == ["✓ C3 done"], r.text
    assert SeqClient.n == 2
    first, second = calls[-2][1]["json"], calls[-1][1]["json"]
    assert first["previous_response_id"] == "resp_1"
    assert "previous_response_id" not in second
    assert "Conversation so far" in second["instructions"] and "Recent:" in second["instructions"]
    print("12 ok: any-4xx chain restart + retry")

    # 13. minutes ask + pending target from recent
    FakeClient.spark_payload = {"id": "resp_t", "output": [
        {"type": "function_call", "name": "set_timer",
         "arguments": '{"label": "x", "minutes": 5}', "call_id": "c1", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "set a timer", "session": "s13"})
    assert r.json()["reply"] == "How many minutes for the timer?", r.json()
    assert api._SESSIONS["s13"]["pending"]["kind"] == "minutes"
    assert api._SESSIONS["s13"]["pending"]["target"], api._SESSIONS["s13"]["pending"]
    r = c.post("/orchestrate", json={"text": "timer 5 minutes", "session": "s13"})
    assert len(r.json()["tool_calls"]) == 1, r.json()
    assert r.json()["messages"] == ["Timer started: drug A · 5 min"], r.json()
    assert api._SESSIONS["s13"]["pending"] is None
    FakeClient.spark_payload = SPARK_OK
    print("13 ok: minutes ask + pending lifecycle")

    # 14. web_search answer + attach flow
    FakeClient.spark_payload = {"id": "resp_w", "output": [
        {"type": "web_search_call",
         "results": [{"title": "Drug X guide", "url": "https://ex.test/x", "snippet": "Incubate 30 minutes at room temp"}]},
        {"type": "function_call", "name": "ask",
         "arguments": '{"question": "Attach to step 3?"}', "call_id": "c2", "status": "completed"},
    ]}
    r = c.post("/orchestrate", json={"text": "usual incubation for this drug?", "session": "s14"})
    j = r.json()
    assert j["reply"] == "Attach to step 3?", j
    assert j["sources"] == [{"title": "Drug X guide", "url": "https://ex.test/x"}], j
    assert api._SESSIONS["s14"]["pending"]["kind"] == "attach", api._SESSIONS["s14"]["pending"]
    FakeClient.spark_payload = {"id": "resp_w2", "output": [
        {"type": "function_call", "name": "save_tip",
         "arguments": '{"step_id": "s3", "text": "incubate 30 minutes"}', "call_id": "c1", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "yes", "session": "s14"})
    j = r.json()
    assert j["cards"] and j["cards"][0]["detail"] == "[s3] Incubate 30 minutes", j
    FakeClient.spark_payload = SPARK_OK
    print("14 ok: web answer + attach pending")

    # 15. web answer alone serves the sentence
    FakeClient.spark_payload = {"id": "resp_w3", "output": [
        {"type": "web_search_call",
         "results": [{"title": "T", "url": "https://ex.test/t", "snippet": "shake gently before use"}]},
    ]}
    r = c.post("/orchestrate", json={"text": "how to mix?", "session": "s15"})
    FakeClient.spark_payload = SPARK_OK
    assert r.json()["reply"] == "shake gently before use. (Source: T)", r.json()
    print("15 ok: web sentence reply")

    # 16. truncation short-circuits
    FakeClient.spark_payload = {"id": "resp_c", "status": "incomplete", "output": [
        {"type": "function_call", "name": "mark_done",
         "arguments": '{"wells": ["B2"]}', "call_id": "c1", "status": "in_progress"}]}
    r = c.post("/orchestrate", json={"text": "start the timer for B2", "session": "s16"})
    FakeClient.spark_payload = SPARK_OK
    assert r.json()["tool_calls"] == [] and r.json()["messages"] == [], r.json()
    assert r.json()["reply"] == "Sorry, say that again?", r.json()
    print("16 ok: truncation short-circuits")

    # 17. wells string expands; ask stripped
    FakeClient.spark_payload = {"id": "resp_e", "output": [
        {"type": "function_call", "name": "mark_done",
         "arguments": '{"wells": "C2-C4"}', "call_id": "c1", "status": "completed"},
        {"type": "function_call", "name": "ask",
         "arguments": '{"question": "Which timer"}', "call_id": "c2", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "do the thing", "session": "s17"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert j["tool_calls"] == [{"name": "mark_done", "args": {"wells": ["C2", "C3", "C4"]}}], j
    assert j["reply"] == "Which timer?", j
    print("17 ok: wells expand, ask stripped")

    # 18. confirm endpoint + record snapshot
    r = c.post("/orchestrate", json={"text": "exclude D8", "session": "s18"})
    card = r.json()["cards"][0]
    r = c.post("/confirm", json={"card_id": card["id"], "ok": True})
    assert r.json()["message"] == "D8 excluded", r.json()
    r = c.post("/confirm", json={"card_id": card["id"], "ok": True})
    assert r.json()["message"] == "That card is gone.", r.json()
    r = c.get("/record")
    assert "D8" in r.json()["excluded"] and r.json()["version"] >= 1, r.json()
    print("18 ok: confirm + record")

    # 19. multi-write turn queues cards; one mechanism, no plan key
    FakeClient.spark_payload = {"id": "resp_p", "output": [
        {"type": "function_call", "name": "exclude_well",
         "arguments": '{"well": "C4", "reason": "bubble"}', "call_id": "c1", "status": "completed"},
        {"type": "function_call", "name": "record_redo",
         "arguments": '{"wells": "C9", "note": "backup"}', "call_id": "c2", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "C4 has a bubble, redo it in a backup well", "session": "s19"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert "plan" not in j, j
    assert [c["question"] for c in j["cards"]] == ["Exclude C4?", "Redo C9?"], j
    assert j["reply"] == "Exclude C4? Redo C9?", j
    r = c.post("/confirm", json={"card_id": j["cards"][0]["id"], "ok": True})
    assert r.json()["message"] == "C4 excluded (bubble)", r.json()
    r = c.post("/confirm", json={"card_id": j["cards"][1]["id"], "ok": False})
    assert r.json()["message"] == "Canceled.", r.json()
    print("19 ok: card checklist replaces plans")

    # 20. manual note edit + robot pipeline (mocked modal)
    mid = api.store.r["notes"][-1]["id"]
    r = c.post("/notes/edit", json={"id": mid, "text": "fixed by hand"})
    assert r.json()["message"].startswith("Note updated ("), r.json()

    class Proc:
        returncode = 0
        stdout = "exit code: 0\ntransfer to C2\ntransfer to C3\n"
        stderr = ""

    with patch("server.robot.subprocess.run", return_value=Proc()):
        r = c.post("/robot")
    j = r.json()
    assert "C2" in j["csv"] and "p.transfer" in j["code"], j
    assert j["sim"]["ok"] is True and j["sim"]["exit"] == 0, j
    assert j["check"]["ok"] is False and "C4" in j["check"]["missing"], j
    assert "well,status" in j["preview"]["csv_head"], j
    print("20 ok: notes edit + robot pipeline")

    # 21. turn after an ask-ended turn carries function_call_outputs
    FakeClient.spark_payload = {"id": "resp_a", "output": [
        {"type": "function_call", "name": "ask",
         "arguments": '{"question": "Which timer?"}', "call_id": "ca", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "do the vague thing", "session": "s21"})
    assert r.json()["reply"] == "Which timer?", r.json()
    FakeClient.spark_payload = {"id": "resp_b", "output": [
        {"type": "function_call", "name": "mark_done",
         "arguments": '{"wells": ["C5"]}', "call_id": "cb", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "C5 then", "session": "s21"})
    FakeClient.spark_payload = SPARK_OK
    assert r.json()["messages"] == ["✓ C5 done"], r.json()
    assert r.json()["tool_calls"] == [{"name": "mark_done", "args": {"wells": ["C5"]}}], r.json()
    body = calls[-1][1]["json"]
    assert body["previous_response_id"] == "resp_a", body
    assert body["input"][0] == {"type": "function_call_output", "call_id": "ca",
                                "output": "asked user"}, body["input"]
    assert body["input"][1] == {"role": "user", "content": "C5 then"}, body["input"]
    print("21 ok: ask outputs ride the next turn")

    # 22. five-turn conversation chains with per-call outputs
    def fc(name, args, cid):
        return {"type": "function_call", "name": name, "arguments": args,
                "call_id": cid, "status": "completed"}

    class ScriptClient(FakeClient):
        queue = []

        async def post(self, url, **kw):
            calls.append((url, kw))
            if url.endswith("/responses"):
                return FakeResp(ScriptClient.queue.pop(0))
            return FakeResp(FakeClient.asr_payload)

    ScriptClient.queue = [
        {"id": "r1", "output": [fc("mark_done", '{"wells": ["C6"]}', "c1")]},
        {"id": "r2", "output": [fc("ask", '{"question": "Which one?"}', "c2")]},
        {"id": "r3", "output": [fc("exclude_well", '{"well": "D2"}', "c3")]},
        {"id": "r4", "output": [fc("mark_done", '{"wells": ["C7"]}', "c4")]},
        {"id": "r5", "output": [fc("mark_done", '{"wells": ["C8"]}', "c5")]},
    ]
    with patch("httpx.AsyncClient", ScriptClient):
        bodies = []
        for i in range(5):
            r = c.post("/orchestrate", json={"text": f"t{i} alpha", "session": "s22"})
            assert r.status_code == 200, r.text
            bodies.append(calls[-1][1]["json"])
    assert "previous_response_id" not in bodies[0] and bodies[0]["input"] == "t0 alpha"
    assert bodies[1]["previous_response_id"] == "r1"
    assert bodies[1]["input"][0] == {"type": "function_call_output", "call_id": "c1",
        "output": '{"ok": true, "message": "✓ C6 done"}'}, bodies[1]["input"]
    assert bodies[2]["input"][0]["output"] == "asked user", bodies[2]["input"]
    assert bodies[3]["input"][0] == {"type": "function_call_output", "call_id": "c3",
        "output": '{"ok": false, "message": "pending confirmation"}'}, bodies[3]["input"]
    assert json.loads(bodies[4]["input"][0]["output"]) == {"ok": True, "message": "✓ C7 done"}
    assert bodies[4]["input"][-1] == {"role": "user", "content": "t4 alpha"}
    assert {"C6", "C7", "C8"} <= set(api.store.r["done"])
    print("22 ok: five-turn outputs chain")

    # 23b. dropped write answers honestly, never "Noted"
    FakeClient.spark_payload = {"id": "resp_d", "output": [
        {"type": "function_call", "name": "add_note",
         "arguments": '{"target": "C3", "text": 5}', "call_id": "cd", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "note that down", "session": "s23b"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert j["messages"] == [], j
    assert j["reply"] == "I couldn't save the note: missing text.", j
    assert j["dropped"] == 1, j
    print("23b ok: dropped note is honest")

    # 24. lone get_well answers from the record; exactly one model call
    r = c.post("/orchestrate", json={"text": "note C6, sticky residue", "session": "s24"})
    assert r.json()["route"] == "grammar", r.json()
    n_before = len(calls)
    FakeClient.spark_payload = {"id": "g1", "output": [fc("get_well", '{"well": "C6"}', "g1c")]}
    r = c.post("/orchestrate", json={"text": "what is on C6", "session": "s24"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert j["reply"] == "C6 is done. Memos: sticky residue.", j
    assert j["messages"] == ["C6 is done. Memos: sticky residue."], j
    assert len([1 for u, _ in calls[n_before:] if u.endswith("/responses")]) == 1, "no round 2"
    print("24 ok: direct record answer, one call")

    # 24b. round-1 answer beside a lookup is dropped blind; round 2 answers
    class GWClient(FakeClient):
        queue = []

        async def post(self, url, **kw):
            calls.append((url, kw))
            if url.endswith("/responses"):
                return FakeResp(GWClient.queue.pop(0))
            return FakeResp(FakeClient.asr_payload)

    GWClient.queue = [
        {"id": "g1", "output": [fc("get_well", '{"well": "C6"}', "g1c"),
                                fc("answer", '{"text": "BLIND GUESS"}', "g1a")]},
        {"id": "g2", "output": [fc("answer", '{"text": "C6 has sticky residue."}', "g2c")]},
    ]
    with patch("httpx.AsyncClient", GWClient):
        r = c.post("/orchestrate", json={"text": "what is on C6", "session": "s24b"})
    j = r.json()
    assert j["reply"] == "C6 has sticky residue.", j
    assert "BLIND" not in " ".join(j["messages"]), j
    first, second = calls[-2][1]["json"], calls[-1][1]["json"]
    assert "sticky residue" not in first["instructions"], "lookup needed"
    assert second["previous_response_id"] == "g1"
    out = second["input"][0]
    assert out["type"] == "function_call_output" and out["call_id"] == "g1c", second["input"]
    data = json.loads(out["output"])
    assert data["well"] == "C6" and data["memos"] == ["sticky residue"], data
    assert second["input"][-1] == {"role": "user", "content": "Continue with the looked-up data."}
    print("24b ok: blind dropped, informed answers")

    # 25. direct answer passes through
    FakeClient.spark_payload = {"id": "resp_ans", "output": [
        {"type": "function_call", "name": "answer",
         "arguments": '{"text": "Yes, 6 of 5 left."}', "call_id": "ca1", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "do I still have enough", "session": "s25"})
    FakeClient.spark_payload = SPARK_OK
    assert r.json()["reply"] == "Yes, 6 of 5 left.", r.json()
    print("25 ok: direct answer")

    # 26. answer beside a write loses to the write
    FakeClient.spark_payload = {"id": "resp_mx", "output": [
        {"type": "function_call", "name": "mark_done",
         "arguments": '{"wells": ["C2"]}', "call_id": "cm", "status": "completed"},
        {"type": "function_call", "name": "answer",
         "arguments": '{"text": "Done!"}', "call_id": "cn", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "mark C2 done and confirm", "session": "s26"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert j["reply"] == "✓ C2 done", j
    assert j["tool_calls"] == [{"name": "mark_done", "args": {"wells": ["C2"]}}], j
    print("26 ok: action wins over answer")

    # 27. plan-time grounding: "incubate 30 min" step + "set the incubation timer"
    saved_plan, saved_idx = api.store.r["plan"], api.store.r["step_index"]
    api.store.r["plan"] = {"groups": {}, "steps": [{"name": "incubate 30 min",
                                                    "wells": ["C2", "C3", "C4", "C5", "C6", "C7", "C8"]},
                                                   {"name": "wash 5 min", "wells": ["C2"]}]}
    api.store.r["step_index"] = 0
    FakeClient.spark_payload = {"id": "resp_g", "output": [
        {"type": "function_call", "name": "set_timer",
         "arguments": '{"label": "x", "minutes": 30}', "call_id": "cg", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "set the incubation timer", "session": "s27"})
    FakeClient.spark_payload = SPARK_OK
    api.store.r["plan"], api.store.r["step_index"] = saved_plan, saved_idx
    j = r.json()
    assert j["messages"] == ["Timer started: incubate 30 min · 30 min"], j
    print("27 ok: plan-time grounded timer")

    # 28. round-2 memo grounding: mixed round 1, clean reply, no stale ask
    r = c.post("/orchestrate", json={"text": "note C7, wait 20 min", "session": "s28"})
    assert r.json()["route"] == "grammar", r.json()

    class GW2Client(FakeClient):
        queue = []

        async def post(self, url, **kw):
            calls.append((url, kw))
            if url.endswith("/responses"):
                return FakeResp(GW2Client.queue.pop(0))
            return FakeResp(FakeClient.asr_payload)

    GW2Client.queue = [
        {"id": "h1", "output": [fc("get_well", '{"well": "C7"}', "h1c"),
                                fc("set_timer", '{"label": "x", "minutes": 20}', "h1t")]},
        {"id": "h2", "output": [fc("set_timer", '{"label": "x", "minutes": 20}', "h2c")]},
    ]
    with patch("httpx.AsyncClient", GW2Client):
        r = c.post("/orchestrate", json={"text": "start the C7 timer", "session": "s28"})
    j = r.json()
    assert j["messages"] == ["Timer started: drug A · 20 min"], j
    assert j["reply"] == "Timer started: drug A · 20 min", j
    assert api._SESSIONS["s28"]["pending"] is None, api._SESSIONS["s28"]["pending"]
    print("28 ok: memo-grounded round-2 timer")

    # 29. round-2 ungrounded number is refused with a minutes ask
    GW2Client.queue = [
        {"id": "k1", "output": [fc("get_well", '{"well": "C6"}', "k1c"),
                                fc("set_timer", '{"label": "x", "minutes": 45}', "k1t")]},
        {"id": "k2", "output": [fc("set_timer", '{"label": "x", "minutes": 45}', "k2c")]},
    ]
    with patch("httpx.AsyncClient", GW2Client):
        r = c.post("/orchestrate", json={"text": "start the C6 timer", "session": "s29"})
    j = r.json()
    assert j["reply"] == "How many minutes for the timer?", j
    assert api._SESSIONS["s29"]["pending"]["kind"] == "minutes", api._SESSIONS["s29"]["pending"]
    print("29 ok: ungrounded round-2 refused")

    # 30. round-1 value mismatch is refused too
    FakeClient.spark_payload = {"id": "resp_mm", "output": [
        {"type": "function_call", "name": "set_timer",
         "arguments": '{"label": "x", "minutes": 45}', "call_id": "cm", "status": "completed"}]}
    r = c.post("/orchestrate", json={"text": "set a 10 minute timer", "session": "s30"})
    FakeClient.spark_payload = SPARK_OK
    assert r.json()["reply"] == "How many minutes for the timer?", r.json()
    print("30 ok: mismatched round-1 refused")

    # 23. failed retry answers sorry, not 502
    class DeadChainClient(FakeClient):
        async def post(self, url, **kw):
            calls.append((url, kw))
            if url.endswith("/responses"):
                return FakeResp({"status": "error", "message": "bad"}, status=400)
            return FakeResp(FakeClient.asr_payload)

    r = c.post("/orchestrate", json={"text": "chain first", "session": "s23"})
    with patch("httpx.AsyncClient", DeadChainClient):
        r = c.post("/orchestrate", json={"text": "then fail", "session": "s23"})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Sorry, say that again?", r.json()
    assert r.json()["tool_calls"] == [], r.json()
    print("23 ok: retry failure is a sorry")

    # 31. validation failures log raw args; user text names no tool
    FakeClient.spark_payload = {"id": "resp_u", "output": [
        {"type": "function_call", "name": "frobnicate",
         "arguments": '{"wells": "C9"}', "call_id": "cu", "status": "completed"}]}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = c.post("/orchestrate", json={"text": "do the unknown thing", "session": "s31"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert j["reply"] == "I couldn't do that: unknown tool.", j
    assert "frobnicate" not in j["reply"] and "propose_plan" not in j["reply"], j["reply"]
    log = buf.getvalue()
    assert 'drop frobnicate args={"wells": "C9"} reason=unknown tool' in log, log
    FakeClient.spark_payload = {"id": "resp_u2", "output": [
        {"type": "function_call", "name": "answer",
         "arguments": '{"text": "  "}', "call_id": "ca", "status": "completed"}]}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = c.post("/orchestrate", json={"text": "tell me nothing", "session": "s31b"})
    FakeClient.spark_payload = SPARK_OK
    assert r.json()["reply"] == "I couldn't answer that: missing text.", r.json()
    assert 'drop answer args={"text": "  "} reason=missing text' in buf.getvalue(), buf.getvalue()
    print("31 ok: drops logged with raw args, replies stay human")

    # 32. find_free_wells lookup round feeds round 2
    class FFClient(FakeClient):
        queue = []

        async def post(self, url, **kw):
            calls.append((url, kw))
            if url.endswith("/responses"):
                return FakeResp(FFClient.queue.pop(0))
            return FakeResp(FakeClient.asr_payload)

    FFClient.queue = [
        {"id": "f1", "output": [fc("find_free_wells", '{"near": "C3"}', "f1c")]},
        {"id": "f2", "output": [fc("record_redo", '{"wells": "C9", "note": "backup"}', "f2c")]},
    ]
    with patch("httpx.AsyncClient", FFClient):
        r = c.post("/orchestrate", json={"text": "C3 has a bubble, redo it in a backup well", "session": "s32"})
    j = r.json()
    assert [c["question"] for c in j["cards"]] == ["Redo C9?"], j
    first, second = calls[-2][1]["json"], calls[-1][1]["json"]
    out = second["input"][0]
    assert out["type"] == "function_call_output" and out["call_id"] == "f1c", second["input"]
    assert json.loads(out["output"])["free"][:2] == ["C9", "C10"], out["output"]
    assert second["input"][-1] == {"role": "user", "content": "Continue with the looked-up data."}
    print("32 ok: backup candidates ride round 2")

    # 33. summary mode: store:false, no chain, turns injected
    api.CHAIN_ENABLED = False
    r = c.post("/orchestrate", json={"text": "C3 all set", "session": "s33"})
    assert r.json()["messages"] == ["✓ C3 done"], r.json()
    body = calls[-1][1]["json"]
    assert body["store"] is False and "previous_response_id" not in body, body
    assert '"recent_turns"' not in body["instructions"], "first turn has no history"
    r = c.post("/orchestrate", json={"text": "C4 all set", "session": "s33"})
    body = calls[-1][1]["json"]
    assert body["store"] is False and "previous_response_id" not in body, body
    assert '"recent_turns": ["C3 all set → mark_done[C3]' in body["instructions"], body["instructions"]
    card = c.post("/orchestrate", json={"text": "exclude D8", "session": "s33"}).json()["cards"][0]
    c.post("/confirm", json={"card_id": card["id"], "ok": True, "session": "s33"})
    assert len(api._SESSIONS["s33"]["turns"]) == 4, api._SESSIONS["s33"]["turns"]
    assert api._SESSIONS["s33"]["turns"][-1]["text"] == "yes"
    api.CHAIN_ENABLED = True
    print("33 ok: summary mode stateless + turns")

    # 34. two ui calls echo in order; speak spells wells; screen validated
    FakeClient.spark_payload = {"id": "resp_ui", "output": [
        fc("show", '{"view": "plate", "target": "C5"}', "u1"),
        fc("open", '{"target": "C5"}', "u2")]}
    r = c.post("/orchestrate", json={"text": "show C5 and open its card",
                                     "session": "s34", "screen": "well_card",
                                     "selected_well": "C5"})
    FakeClient.spark_payload = SPARK_OK
    j = r.json()
    assert j["route"] == "spark", j
    assert j["ui"] == [{"action": "open", "target": "C5", "sheet": "well_card"}], j
    assert j["speak"] == "Viewing: plate C 5 Opened C 5.", j
    body = calls[-1][1]["json"]
    assert '"current_screen": "well_card"' in body["instructions"], body["instructions"]
    assert '"selected_well": "C5"' in body["instructions"], body["instructions"]
    assert f'"pending_cards": {len(api.store.r["cards"])}' in body["instructions"], body["instructions"]
    r = c.post("/orchestrate", json={"text": "C2 all set", "session": "s34b", "screen": "nope"})
    assert '"current_screen": "main"' in calls[-1][1]["json"]["instructions"]
    print("34 ok: ui echo + speak + screen state")

    # 35. text answers never resolve cards; only /confirm does
    r = c.post("/orchestrate", json={"text": "exclude D7", "session": "s35"})
    card_id = r.json()["cards"][0]["id"]
    r = c.post("/orchestrate", json={"text": "yes", "session": "s35"})
    assert r.json()["reply"] == "✓ C3 done", r.json()
    assert card_id in api.store.r["cards"], "model turn must not resolve cards"
    r = c.post("/confirm", json={"card_id": card_id, "ok": True, "session": "s35"})
    assert r.json()["message"] == "D7 excluded", r.json()
    assert card_id not in api.store.r["cards"]
    print("35 ok: cards need explicit confirm")

print("ALL PASS")
