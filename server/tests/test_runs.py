"""Library + runs: save/dedup, start/end cards, past-run scope, source reads.

Run: python -m unittest server.tests.test_runs (runs on import).
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.runs import done_wells  # noqa: E402
from server.store import RecordStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
PDF = (ROOT / "samples" / "protocol_demo.pdf").read_bytes()

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


class FakeClient:
    spark_payload = {}

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


def extraction(raw):
    return {"id": "ext", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(raw)}]}]}


def spark(*calls):
    return {"id": "r", "output": list(calls)}


S1 = "Step 1: add 100 uL media to all wells; pipette slowly down the wall."
RAW = {
    "title": "Methods",
    "short_title": "Methods",
    "liquids": [{"id": "media", "name": "media", "concentration": None,
                 "prep": None, "quote": S1}],
    "conditions": [{"id": "control", "label": "control", "abbr": "", "treatments": ["media"],
                    "replicates": 5, "wells": None,
                    "quote": "Seed cells in control, drug A, and drug B wells, 5 replicates each."}],
    "steps": [{"kind": "pipette", "text": "add media", "short_title": "", "quote": S1,
               "minutes": None, "temp_c": None, "caution": None,
               "pipette": {"liquid": "media", "volume": 100, "unit": "uL",
                           "target": {"type": "condition", "value": "control"},
                           "mix": None, "repeat": None}}],
}


def backdate(run_id, days):
    from server import runs as runlog
    p = Path(api.store.path).parent / "runs" / f"{run_id}.json"
    doc = json.loads(p.read_text())
    doc["started_at"] -= days * 86400
    p.write_text(json.dumps(doc))
    return runlog


with patch("httpx.AsyncClient", FakeClient):
    c = TestClient(api.app)

    # LIB1. library seeds the Demo protocol; get + 404.
    j = c.get("/protocols").json()
    assert [p["name"] for p in j["protocols"]] == ["Demo protocol"], j
    demo_id = j["protocols"][0]["id"]
    assert j["protocols"][0]["steps"] == 5, j
    j = c.get(f"/protocols/{demo_id}").json()
    assert j["protocol"]["id"] == demo_id and "plan" in j["protocol"], j
    assert c.get("/protocols/nope").status_code == 404
    print("LIB1 ok: seeded library list/get")

    # LIB2. import -> save; quote_page 1; same PDF twice -> same id.
    FakeClient.spark_payload = extraction(RAW)
    j = c.post("/protocols/import",
               files={"file": ("protocol_demo.pdf", PDF, "application/pdf")}).json()
    assert j["errors"] == [], j
    assert api.store.r["draft"]["plan"]["steps"][0]["quote_page"] == 1, j
    j = c.post("/protocols/save", json={"session": "lib2"}).json()
    assert j["saved"] is True and j["protocol"]["name"] == "Methods", j
    pid1 = j["protocol"]["id"]
    assert "draft" not in api.store.r
    # the same original again: no extraction, its protocol opens (runs stack under it)
    FakeClient.spark_payload = {"id": "never", "output": []}
    j = c.post("/protocols/import",
               files={"file": ("protocol_demo.pdf", PDF, "application/pdf")}).json()
    assert j["protocol_id"] == pid1 and j["draft"] is None and "draft" not in api.store.r, j
    assert api.store.r["view"]["current"] == {"screen": "protocol_detail", "target": pid1}, api.store.r["view"]
    print("LIB2 ok: save + same original -> same protocol + quote_page")

    # LIB3. missing title falls back to the filename stem.
    raw = {"title": "", "short_title": "", "liquids": [], "conditions": [], "steps": []}
    FakeClient.spark_payload = extraction(raw)
    c.post("/protocols/import",
           files={"file": ("mylib.txt", b"Seed control wells.", "text/plain")})
    j = c.post("/protocols/save", json={"session": "lib3"}).json()
    assert j["saved"] is True and j["protocol"]["name"] == "mylib", j
    print("LIB3 ok: title stem fallback")

    # RUN1. no active run: end 400s, end_run can't, guidance says so.
    assert api.store.r.get("run") is None
    r = c.post("/runs/end", json={"session": "run1"})
    assert r.status_code == 400, r.text
    FakeClient.spark_payload = spark(fc("end_run", "{}", "c1"))
    j = c.post("/orchestrate", json={"text": "end the run", "session": "run1"}).json()
    assert j["reply"] == "No active run — open a protocol from Home to start.", j
    j = c.post("/orchestrate", json={"text": "where am I", "session": "run1"}).json()
    assert j["reply"] == "No active run — open a protocol from Home to start.", j
    print("RUN1 ok: honest empty state")

    # RUN2. start with no active run starts at once.
    j = c.post("/runs/start", json={"protocol_id": demo_id, "session": "run2"}).json()
    assert j["started"] is True, j
    assert j["reply"] == "Run started: 3 conditions, 5 steps.", j
    run1 = j["run_id"]
    assert api.store.r["run"]["run_id"] == run1
    r = c.post("/runs/start", json={"protocol_id": "nope", "session": "run2"})
    assert r.status_code == 404, r.text
    print("RUN2 ok: direct start + unknown 404")

    # RUN3. start with an active run starts at once; the old run is paused.
    j = c.post("/orchestrate", json={"text": "B2 done", "session": "run3"}).json()
    assert j["route"] == "grammar" and sorted(done_wells(api.store.r)) == ["B2"], j
    j = c.post("/runs/start", json={"protocol_id": demo_id, "session": "run3"}).json()
    assert j["started"] is True and not j.get("card"), j
    run2 = api.store.r["run"]["run_id"]
    assert run2 != run1 and sorted(done_wells(api.store.r)) == [], api.store.r["run"]
    runs = list((Path(api.store.path).parent / "runs").glob("run-*.json"))
    assert len(runs) == 1, runs
    old = json.loads(runs[0].read_text())
    assert old["run_id"] == run1 and old["status"] == "paused", old
    assert sorted(done_wells(old["record"])) == ["B2"], old["record"]["done"]
    j = c.get("/runs").json()
    assert [r["status"] for r in j["runs"]] == ["active", "paused"], j
    assert j["runs"][1]["summary"]["notes"] == 0, j
    j = c.get("/runs", params={"protocol_id": demo_id}).json()
    assert len(j["runs"]) == 2, j
    assert c.get("/runs/nope").status_code == 404
    print("RUN3 ok: start pauses the old run + archive + list")

    # RUN4. yesterday's run opens; writes refused, reads follow the view.
    backdate(run1, 1)
    FakeClient.spark_payload = spark(fc("open", '{"target": "yesterday\'s run"}', "c1"))
    j = c.post("/orchestrate", json={"text": "show yesterday's run",
                                     "session": "run4"}).json()
    assert api.store.r["view"]["current"] == {"screen": "run_view",
                                              "target": run1}, j
    j = c.post("/orchestrate", json={"text": "C3 done", "session": "run4"}).json()
    assert j["reply"] == "That run is finished — read only.", j
    assert sorted(done_wells(api.store.r)) == [] and run1 not in [
        d.get("run_id") for d in [api.store.active_run_doc()]], "active untouched"
    d = api.store.read_record("wells", "B2", 10)
    assert d["run"] == run1 and d["wells"][0]["status"] == "done", d
    FakeClient.spark_payload = spark(fc("open", '{"target": "run nope"}', "c9"))
    j = c.post("/orchestrate", json={"text": "open run nope",
                                     "session": "run4"}).json()
    assert j["reply"] == "I couldn't find that run.", j
    c.post("/input", json={"action": {"id": "back", "args": {}},
                           "session": "run4"})
    assert api.store.r["view"]["current"]["screen"] == "main"
    d = api.store.read_record("wells", "B2", 10)
    assert d["run"] == run2, d
    print("RUN4 ok: past-run scope gate + scoped reads")

    # RUN5. Import/Discard and Import/Save leave the active run alone.
    # (another original: the demo PDF is already a protocol, so it wouldn't draft)
    PDF5 = PDF + b"\n% run5\n"
    FakeClient.spark_payload = extraction(RAW)
    c.post("/protocols/import",
           files={"file": ("protocol_demo.pdf", PDF5, "application/pdf")})
    assert "draft" in api.store.r
    j = c.request("DELETE", "/protocols/draft", json={"session": "run5"}).json()
    assert j["ok"] is True and "draft" not in api.store.r, j
    assert api.store.r["run"]["run_id"] == run2
    FakeClient.spark_payload = extraction(RAW)
    c.post("/protocols/import",
           files={"file": ("protocol_demo.pdf", PDF5, "application/pdf")})
    j = c.post("/protocols/save", json={"session": "run5"}).json()
    assert j["card"]["kind"] == "save_as", j  # the demo's title: new version or separate?
    j = c.post("/confirm", json={"card_id": j["card"]["id"], "ok": True, "answer": "separate",
                                 "session": "run5"}).json()
    assert "library" in j["message"] and api.store.r["run"]["run_id"] == run2, j
    print("RUN5 ok: discard/save don't touch the run")

    # RUN7. source reads quote+page; 'the original' opens protocol_source.
    api.store.r["step_index"] = 2
    st = api.store.r["plan"]["steps"][2]
    st["quote"], st["quote_page"] = "add drug A now", 2
    d = api.store.read_record("source", "", 10)
    assert (d["step"], d["quote"], d["quote_page"]) == (3, "add drug A now", 2), d
    d = api.store.read_record("source", "1", 10)
    assert d["step"] == 1, d
    FakeClient.spark_payload = spark(fc("open", '{"target": "the original"}', "c1"))
    j = c.post("/orchestrate", json={"text": "show me the original",
                                     "session": "run7"}).json()
    assert api.store.r["view"]["current"] == {
        "screen": "protocol_source",
        "target": f"protocol:{demo_id}#step:3"}, j
    print("RUN7 ok: source kind + original navigation")

    # RUN6. end_run cards; confirm finishes; empty again after.
    FakeClient.spark_payload = spark(fc("end_run", "{}", "c1"))
    j = c.post("/orchestrate", json={"text": "end the run", "session": "run6"}).json()
    assert [x["kind"] for x in j["cards"]] == ["end_run"], j
    j = c.post("/confirm", json={"card_id": j["cards"][0]["id"], "ok": True,
                                 "session": "run6"}).json()
    assert j["message"].startswith("Run finished: Demo protocol. "), j  # then ⑨0's line
    assert api.store.r.get("run") is None
    assert api.store.r["view"]["current"] == {"screen": "final_protocol", "target": run2}, j
    j = c.get(f"/runs/{run2}").json()
    assert j["run"]["status"] == "finished", j
    assert time.time() - 120 < j["run"]["ended_at"] <= time.time() + 1, j["run"]
    r = c.post("/runs/end", json={"session": "run6"})
    assert r.status_code == 400, r.text
    print("RUN6 ok: end card finishes the run")

    # RUN10. version never regresses across start/end (the app drops
    # older snapshots; a regressed end snapshot left the dead card up).
    v_pre = api.store.r["version"]
    api.store.start_demo_run()
    assert api.store.r["version"] > v_pre, api.store.r["version"]
    v_run = api.store.r["version"]
    FakeClient.spark_payload = spark(fc("end_run", "{}", "c1"))
    j = c.post("/orchestrate", json={"text": "end the run",
                                     "session": "run10"}).json()
    cid = [x["id"] for x in j["cards"] if x["kind"] == "end_run"][0]
    v_card = api.store.r["version"]
    assert v_card > v_run, (v_run, v_card)
    j = c.post("/confirm", json={"card_id": cid, "ok": True,
                                 "session": "run10"}).json()
    assert api.store.r.get("run") is None, j
    assert api.store.r["version"] > v_card, api.store.r["version"]
    assert api.store.r["cards"] == {}, api.store.r["cards"]
    print("RUN10 ok: version monotonic across start/end")

    # RUN8. legacy run (no protocol) names the gap honestly.
    api.store.r["run"] = {"run_id": "legacy", "protocol_id": None,
                          "protocol_name": None, "started_at": time.time()}
    FakeClient.spark_payload = spark(fc("open", '{"target": "the original"}', "c1"))
    j = c.post("/orchestrate", json={"text": "show me the original",
                                     "session": "run8"}).json()
    assert j["reply"] == "This run has no saved protocol.", j
    assert j.get("ui") in (None, []), j
    print("RUN8 ok: legacy original is honest")

    # RUN9. view target scopes taps: past refuses, main applies.
    api.store.start_demo_run()
    backdate(run1, 2)
    c.post("/input", json={"action": {"id": "open", "args": {"target": run1}},
                           "session": "run9"})
    assert api.store.r["view"]["current"] == {"screen": "run_view",
                                              "target": run1}
    j = c.post("/orchestrate", json={"text": "C3 done", "session": "run9"}).json()
    assert j["reply"] == "That run is finished — read only.", j
    assert sorted(done_wells(api.store.r)) == [], api.store.r["done"]
    c.post("/input", json={"action": {"id": "back", "args": {}},
                           "session": "run9"})
    j = c.post("/orchestrate", json={"text": "C3 done", "session": "run9"}).json()
    assert sorted(done_wells(api.store.r)) == ["C3"], j
    print("RUN9 ok: view target scopes taps")

    # RUN11. opening the active run (tap or "latest") shows its record (Continue -> bench).
    active = api.store.r["run"]["run_id"]
    for target in (active, "latest"):
        c.post("/input", json={"action": {"id": "open", "args": {"target": f"run:{target}"}},
                               "session": "run11"})
        assert api.store.r["view"]["current"] == {"screen": "run_view", "target": active}, target
    c.post("/input", json={"action": {"id": "open", "args": {"target": f"run:{run1}"}},
                           "session": "run11"})
    assert api.store.r["view"]["current"] == {"screen": "run_view", "target": run1}
    print("RUN11 ok: active run opens its record")

    # RUN12. On an older run's record, "here" is that run: a navigate / open with no run
    # named stays on it (the model asked to see "the note here" and got the latest run),
    # and the model is told which run is on screen.
    assert run1 != active
    for call in ({"id": "navigate", "args": {"screen": "run_view"}}, {"id": "open", "args": {"target": "run"}}):
        c.post("/input", json={"action": call, "session": "run12"})
        assert api.store.r["view"]["current"] == {"screen": "run_view", "target": run1}, call
    st = api.store.model_state({})
    assert st["current_screen"] == "run_view" and st["viewing_run"] == api.store.run_label(
        api.runlog.resolve(run1, api.store.all_run_docs()))["spoken"], st
    print("RUN12 ok: here on a run record is that run, never the latest")

# LIB-merge. Duplicates of one original merge into the earliest import; their
# runs move under it and number by start order; files go to a backup.
from server import library as lib  # noqa: E402
from server import library_merge  # noqa: E402

mroot = Path(tempfile.mkdtemp())
plan_m = {"conditions": {}, "steps": [{"kind": "wait", "text": "wait 5 min", "wells": []}], "backups": []}
a = lib.save(plan_m, "Same Title", "a.pdf", mroot)
plan_b = {**plan_m, "backups": ["B2"]}  # a different extraction of the same document
b = lib.save(plan_b, "Same Title", "a.pdf", mroot)
assert a["id"] != b["id"]
(mroot / "runs").mkdir()
for i, (pid, t) in enumerate([(b["id"], 200), (a["id"], 100), (b["id"], 300)]):
    (mroot / "runs" / f"run-{i}.json").write_text(json.dumps(
        {"run_id": f"run-{i}", "protocol_id": pid, "protocol_name": "Same Title", "started_at": t,
         "ended_at": t + 1, "status": "finished", "summary": {}, "record": {}}))
assert [len(g) for g in library_merge.groups(mroot)] == [2]
library_merge.merge(mroot, apply=True)
docs = lib.all_docs(mroot)
assert [d["id"] for d in docs] == [a["id"]], docs
st_m = RecordStore(path=mroot / "record.json")
labels_m = sorted((r["started_at"], r["label"]) for r in st_m.run_summaries(st_m.all_run_docs()))
assert labels_m == [(100, "Run 1"), (200, "Run 2"), (300, "Run 3")], labels_m
assert list(mroot.glob("backup-merge-*/protocols/*.json")), "the dropped protocol is kept in a backup"
print("LIB-merge ok: one protocol per original, runs renumbered")
print("ALL PASS")
