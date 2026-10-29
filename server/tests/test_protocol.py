"""Protocol import v2: schema, quotes, draft edits, start. Stubs Meta APIs.

Run: python -m unittest server.tests.test_protocol (runs on import).
"""

import io
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
from server import protocol as proto  # noqa: E402
from server.store import RecordStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
PDF = (ROOT / "samples" / "protocol_demo.pdf").read_bytes()

api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
api.store.r["step_index"] = 2  # mid-run fixture, as before
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


def fc(name, args, cid):
    return {"type": "function_call", "name": name, "arguments": args,
            "call_id": cid, "status": "completed"}


def extraction(raw):
    return {"id": "ext", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(raw)}]}]}


SEED_Q = "Seed cells in control, drug A, and drug B wells, 5 replicates each."
S1 = "Step 1: add 100 uL media to all wells; pipette slowly down the wall."
S2 = "Step 2: add vehicle to control wells."
S3 = "Step 3: add drug A to drug A wells; do not mix up and down after adding."
S4 = "Step 4: add drug B to drug B wells."
S5 = "Step 5: incubate 30 min."

LIQUIDS = [
    {"id": "media", "name": "media", "concentration": None, "prep": None, "quote": S1},
    {"id": "vehicle", "name": "vehicle", "concentration": None, "prep": None, "quote": S2},
    {"id": "drug_a", "name": "drug A", "concentration": None, "prep": None, "quote": S3},
    {"id": "drug_b", "name": "drug B", "concentration": None, "prep": None, "quote": S4},
]
CONDITIONS = [
    {"id": "control", "label": "control", "treatments": ["vehicle"],
     "replicates": 5, "wells": None, "quote": SEED_Q},
    {"id": "drug_a", "label": "drug A", "treatments": ["drug_a"],
     "replicates": 5, "wells": None, "quote": SEED_Q},
    {"id": "drug_b", "label": "drug B", "treatments": ["drug_b"],
     "replicates": 5, "wells": None, "quote": SEED_Q},
]


def _pip(liquid, target, volume=None, unit=None, repeat=None):
    return {"liquid": liquid, "volume": volume, "unit": unit,
            "target": target, "mix": None, "repeat": repeat}


STEPS = [
    {"kind": "pipette", "text": "add 100 uL media to all wells", "short_title": "", "quote": S1,
     "minutes": None, "temp_c": None, "caution": None,
     "pipette": _pip("media", {"type": "all_used", "value": None}, 100, "uL")},
    {"kind": "pipette", "text": "add vehicle to control wells", "short_title": "", "quote": S2,
     "minutes": None, "temp_c": None, "caution": None,
     "pipette": _pip("vehicle", {"type": "condition", "value": "control"})},
    {"kind": "pipette", "text": "add drug A to drug A wells", "short_title": "", "quote": S3,
     "minutes": None, "temp_c": None,
     "caution": "do not mix up and down after adding",
     "pipette": _pip("drug_a", {"type": "condition", "value": "drug_a"})},
    {"kind": "pipette", "text": "add drug B to drug B wells", "short_title": "", "quote": S4,
     "minutes": None, "temp_c": None, "caution": None,
     "pipette": _pip("drug_b", {"type": "condition", "value": "drug_b"})},
    {"kind": "incubate", "text": "incubate 30 min", "short_title": "", "quote": S5,
     "minutes": 30, "temp_c": None, "caution": None, "pipette": None},
]


def demo_evidence():
    from pypdf import PdfReader
    text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(PDF)).pages)
    return proto.evidence_from_text(text)


with patch("httpx.AsyncClient", FakeClient):
    c = TestClient(api.app)

    # P1. schema file is strict-clean and checks extractions.
    assert proto.strict_subset_ok(proto.PROTOCOL_SCHEMA)
    assert not proto.strict_subset_ok({"type": "object", "properties": {"a": {"type": "string"}},
                                             "required": ["a"]}), "additionalProperties missing"
    good = {"title": "Methods", "short_title": "Methods", "liquids": LIQUIDS, "conditions": CONDITIONS, "steps": STEPS}
    assert proto.validate_against_schema(good, proto.PROTOCOL_SCHEMA) == []
    bad = {"liquids": [], "conditions": [{"id": "x"}], "steps": []}
    assert proto.validate_against_schema(bad, proto.PROTOCOL_SCHEMA), "label required"
    notip = {"liquids": [], "conditions": [], "steps": [
        {"kind": "pipette", "text": "t", "short_title": "", "quote": "q", "minutes": None,
         "temp_c": None, "caution": None, "tip": "x", "pipette": None}]}
    assert proto.validate_against_schema(notip, proto.PROTOCOL_SCHEMA), "no tip field"
    print("P1 ok: schema strict-clean, mini-validator works, no tip")

    # P10. norm_quote unifies units, degrees, dashes, quotes, case, space.
    nq = proto.norm_quote
    assert nq("10 µL") == nq("10 μL") == nq("10 uL") == "10 ul"
    assert nq("37 °C") == "37 c"
    assert nq("B2–B8—x") == "b2-b8-x"
    assert nq("‘q’ “d”") == "'q' 'd'"
    assert nq("A  B\nC") == "a b c"
    assert proto._repeat_in_quote(2, nq("wash twice with PBS"))
    assert proto._repeat_in_quote(2, nq("wash 2 times"))
    assert not proto._repeat_in_quote(3, nq("wash twice with PBS"))
    assert proto._number_in_quote(30, nq("incubate 30 min"), "min")
    assert not proto._number_in_quote(5, nq("15 replicates"))
    assert proto._number_in_quote(37, nq("at 37 °C before"), "temp")
    print("P10 ok: norm_quote mappings")

    # P6a. edit_draft with no draft open is an honest couldn't.
    FakeClient.spark_payload = {"id": "p6a", "output": [
        fc("edit_draft", '{"path": "conditions.control.replicates", "value": "6"}', "c1")]}
    j = c.post("/orchestrate", json={"text": "make control 6", "session": "p6a"}).json()
    assert j["reply"] == "I couldn't edit the draft: no draft open.", j
    print("P6a ok: no-draft edit is couldn't")

    # P2. PDF import: text.format json_schema + input_file, draft auto-laid.
    seen = []
    orig_post = FakeClient.post

    async def spy(self, url, **kw):
        if url.endswith("/responses"):
            seen.append(kw["json"])
        return await orig_post(self, url, **kw)

    FakeClient.spark_payload = extraction(good)
    with patch.object(FakeClient, "post", spy):
        r = c.post("/protocols/import", files={"file": ("protocol_demo.pdf", PDF, "application/pdf")})
    j = r.json()
    body = seen[-1]
    assert body["text"]["format"]["type"] == "json_schema", body["text"]
    assert body["text"]["format"]["strict"] is True
    content = body["input"][0]["content"]
    assert content[0]["type"] == "input_text", content
    assert content[1]["type"] == "input_file", content
    assert content[1]["file_data"].startswith("data:application/pdf;base64,"), content[1]
    assert j["errors"] == [], j
    assert {(i["path"], i["code"]) for i in j["review"]} == {
        ("steps.1.pipette.volume", "missing_value"),
        ("steps.2.pipette.volume", "missing_value"),
        ("steps.3.pipette.volume", "missing_value")}, j["review"]
    assert j["reply"] == "Draft ready: 3 conditions, 5 steps. 3 to review.", j
    conds = api.store.r["draft"]["plan"]["conditions"]
    assert conds["control"]["wells"] == ["B2", "B3", "B4", "B5", "B6", "B7", "B8"], conds
    assert conds["drug_a"]["wells"][0] == "C2" and conds["drug_b"]["wells"][0] == "D2"
    assert api.store.r["draft"]["meta"]["layout"] == {
        "control": "auto", "drug_a": "auto", "drug_b": "auto"}
    assert api.store.r["draft"]["plan"]["backups"] == ["B7", "B8", "C7", "C8", "D7", "D8"]
    assert api.store.r["draft"]["plan"]["steps"][2]["caution"] == \
        "do not mix up and down after adding"
    print("P2 ok: PDF import extracts + auto-lays + caution")

    # P3. invented numbers and unstated wells flag, never silent.
    raw = {"liquids": [], "conditions": [
        {"id": "control", "label": "control", "treatments": [], "replicates": 7,
         "wells": ["B2", "B3"], "quote": SEED_Q}], "steps": []}
    d = proto.build_draft(raw, demo_evidence(), "x.pdf")
    codes = {(i["path"], i["code"]) for i in d["review"]}
    assert ("conditions.control.replicates", "ungrounded_value") in codes, d["review"]
    assert ("conditions.control.wells", "dropped_positions") in codes, d["review"]
    assert d["plan"]["conditions"]["control"]["wells"][:2] == ["B2", "B3"], d["plan"]
    assert d["errors"] == []
    print("P3 ok: invented values become review")

    # P11. quote edges: quoteless node, stray caution, repeat words, endpoints.
    ev = proto.evidence_from_text(
        "Wash twice with buffer.\nSeed control wells, 5 replicates each.\n"
        "Plate: control wells are B2, B3, B4, B5, B6.\n")
    raw = {"liquids": [{"id": "x", "name": "x", "concentration": None,
                        "prep": None, "quote": "Seed cells nowhere."}],
           "conditions": [{"id": "control", "label": "control", "treatments": [],
                           "replicates": 5, "wells": ["B2-B6"],
                           "quote": "control wells are B2, B3, B4, B5, B6."}],
           "steps": [
               {"kind": "wash", "text": "wash twice", "short_title": "", "quote": "Wash twice with buffer.",
                "minutes": None, "temp_c": None, "caution": "do not centrifuge",
                "pipette": {"liquid": "x", "volume": None, "unit": None,
                            "target": {"type": "condition", "value": "control"},
                            "mix": None, "repeat": 2}},
               {"kind": "wash", "text": "wash thrice", "short_title": "", "quote": "Wash twice with buffer.",
                "minutes": None, "temp_c": None, "caution": None,
                "pipette": {"liquid": "x", "volume": 50, "unit": "drops",
                            "target": {"type": "condition", "value": "control"},
                            "mix": None, "repeat": 3}}]}
    d = proto.build_draft(raw, ev, "x.pdf")
    codes = {(i["path"], i["code"]) for i in d["review"]}
    assert ("liquids.x", "ungrounded_quote") in codes, d["review"]
    assert ("steps.0.caution", "ungrounded_value") in codes, d["review"]
    assert ("steps.0.pipette.repeat", "ungrounded_value") not in codes, d["review"]
    assert ("steps.1.pipette.repeat", "ungrounded_value") in codes, d["review"]
    assert ("steps.1.pipette.volume", "ungrounded_value") in codes, d["review"]
    assert d["plan"]["conditions"]["control"]["wells"] == ["B2", "B3", "B4", "B5", "B6"]
    assert d["plan"]["conditions"]["control"]["replicates"] == 5
    assert ("steps.1.pipette.unit", "bad_unit") in {
        (e["path"], e["code"]) for e in d["errors"]}, d["errors"]
    print("P11 ok: quote edges (repeat words, endpoints, stray caution)")

    # P9. overflow, row exhaustion, ungrounded minutes (direct).
    many = [{"id": f"g{i}", "label": f"g{i}", "treatments": [], "replicates": 2,
             "wells": None, "quote": SEED_Q} for i in range(8)]
    d = proto.build_draft({"liquids": [], "conditions": many, "steps": []},
                          demo_evidence(), "x.pdf")
    codes = {(i["path"], i["code"]) for i in d["review"]}
    assert any(code == "no_row" for _, code in codes), d["review"]
    big = {"liquids": [], "conditions": [
        {"id": "control", "label": "control", "treatments": [], "replicates": 11,
         "wells": None, "quote": SEED_Q}], "steps": []}
    d = proto.build_draft(big, demo_evidence(), "x.pdf")
    codes = {(i["path"], i["code"]) for i in d["review"]}
    assert ("conditions.control.wells", "overflow") in codes, d["review"]
    mins = {"liquids": [], "conditions": [], "steps": [
        {"kind": "wait", "text": "wait", "short_title": "", "quote": S5, "minutes": 45,
         "temp_c": None, "caution": None, "pipette": None}]}
    d = proto.build_draft(mins, demo_evidence(), "x.pdf")
    assert ("steps.0.minutes", "ungrounded_value") in {(i["path"], i["code"]) for i in d["review"]}
    print("P9 ok: overflow, no-row, minutes review")

    # P5. PATCH edits re-validate through the same validator.
    r = c.patch("/protocols/draft", json={"path": "conditions.nope.replicates", "value": 6})
    assert r.json() == {"ok": False, "reason": "no such condition: nope",
                        "record_version": r.json()["record_version"]}, r.json()
    r = c.patch("/protocols/draft", json={"path": "conditions.control.replicates", "value": "six"})
    assert r.json()["ok"] is False and "whole number" in r.json()["reason"], r.json()
    r = c.patch("/protocols/draft", json={"path": "conditions.control.replicates", "value": 6})
    j = r.json()
    assert j["ok"] is True, j
    assert api.store.r["draft"]["plan"]["conditions"]["control"]["wells"][-1] == "B9", j
    r = c.patch("/protocols/draft", json={"path": "steps.4.minutes", "value": 45})
    assert r.json()["ok"] is True, r.json()
    assert ("steps.4.minutes", "ungrounded_value") in {
        (i["path"], i["code"]) for i in r.json()["review"]}, r.json()
    r = c.patch("/protocols/draft", json={"path": "steps.4.minutes", "value": 30})
    assert ("steps.4.minutes", "ungrounded_value") not in {
        (i["path"], i["code"]) for i in r.json()["review"]}, r.json()
    r = c.patch("/protocols/draft", json={"path": "steps.4.temp_c", "value": 37})
    assert ("steps.4.temp_c", "ungrounded_value") in {
        (i["path"], i["code"]) for i in r.json()["review"]}, r.json()
    r = c.patch("/protocols/draft", json={"path": "steps.4.temp_c", "value": None})
    assert ("steps.4.temp_c", "ungrounded_value") not in {
        (i["path"], i["code"]) for i in r.json()["review"]}, r.json()
    r = c.patch("/protocols/draft",
                json={"path": "steps.0.pipette.target", "value": "condition:control"})
    assert r.json()["ok"] is True, r.json()
    assert api.store.r["draft"]["plan"]["steps"][0]["wells"][:2] == ["B2", "B3"], r.json()
    r = c.patch("/protocols/draft",
                json={"path": "steps.0.pipette.target", "value": "wells:ZZZ"})
    assert r.json()["ok"] is False and "not plate wells" in r.json()["reason"], r.json()
    r = c.patch("/protocols/draft", json={"path": "liquids.drug_a", "value": "Drug A+"})
    assert r.json()["ok"] is True, r.json()
    assert api.store.r["draft"]["plan"]["liquids"]["drug_a"]["name"] == "Drug A+"
    assert proto.parse_draft_path("steps.[0].pipette.volume") == ("step", 0, "pipette.volume")
    assert proto.parse_draft_path("steps[2].minutes") == ("step", 2, "minutes")
    d0 = api.store.r["draft"]
    assert proto.parse_draft_value(d0, ("step", 0, "pipette.volume"), "50 uL") == (True, 50.0)
    assert proto.parse_draft_value(d0, ("step", 4, "minutes"), "30 min") == (True, 30.0)
    assert proto.parse_draft_value(d0, ("step", 4, "minutes"), "C2")[0] is False
    assert proto.parse_draft_value(d0, ("condition", "control", "replicates"), "6")[0] is True
    print("P5 ok: PATCH edits + revalidation")

    # P6b. voice edit via the tool: row move applies, bad path is couldn't.
    FakeClient.spark_payload = {"id": "p6b", "output": [
        fc("edit_draft", '{"path": "conditions.drug_a.wells", "value": "row E"}', "c1")]}
    j = c.post("/orchestrate", json={"text": "change drug A to row E", "session": "p6b"}).json()
    assert j["reply"] == "Updated drug a wells.", j
    assert api.store.r["draft"]["plan"]["conditions"]["drug_a"]["wells"][0] == "E2", j
    FakeClient.spark_payload = {"id": "p6c", "output": [
        fc("edit_draft", '{"path": "conditions.drug_a.bogus", "value": "x"}', "c1")]}
    j = c.post("/orchestrate", json={"text": "change something", "session": "p6c"}).json()
    assert j["reply"] == "I couldn't edit the draft: unknown path conditions.drug_a.bogus.", j
    print("P6b ok: tool row move + bad path")

    # P4. overlap blocks Start; fix unblocks; archive + reset.
    r = c.patch("/protocols/draft", json={"path": "conditions.control.wells", "value": "E2-E8"})
    assert [(e["path"], e["code"]) for e in r.json()["errors"]] == [
        ("conditions.drug_a.wells", "overlap")], r.json()
    r = c.post("/protocols/start", json={"session": "p4"})
    assert r.json()["started"] is False and r.json()["reply"] == "Fix 1 red item first.", r.json()
    c.patch("/protocols/draft", json={"path": "conditions.control.wells", "value": "B2-B8"})
    c.patch("/protocols/draft", json={"path": "conditions.control.replicates", "value": 5})
    r = c.post("/protocols/start", json={"session": "p4"})
    j = r.json()
    assert j["started"] is False and j["card"], j
    assert j["card"]["kind"] == "finish_first", j["card"]
    assert j["protocol_name"] == "Methods", j
    r = c.post("/confirm", json={"card_id": j["card"]["id"], "ok": True, "session": "p4"})
    assert "Run started" in r.json()["message"], r.json()
    runs = list((Path(api.store.path).parent / "runs").glob("run-*.json"))
    assert len(runs) == 1, runs
    archived = json.loads(runs[0].read_text())
    assert "ctrl" in archived["record"]["plan"]["conditions"], "archive keeps the old run"
    assert "draft" not in api.store.r and api.store.r["step_index"] == 0
    assert sorted(api.store.r["plan"]["conditions"]) == ["control", "drug_a", "drug_b"]
    live = api.store.r["plan"]["steps"]
    assert live[4]["minutes"] == 30 and live[4]["kind"] == "incubate"
    assert live[0]["pipette"]["liquid"] == "media"
    assert live[0]["pipette"]["target_label"] == "control wells"
    assert live[1]["wells"] == ["B2", "B3", "B4", "B5", "B6", "B7", "B8"]
    assert all(s["tip"] is None for s in live), "imports never set tips"
    assert live[2]["caution"] == "do not mix up and down after adding"
    print("P4 ok: overlap blocks, start archives + resets")

    # P4b. Start expands repeats into sequential live steps.
    wash = {"liquids": {"pbs": {"name": "PBS", "concentration": None,
                               "prep": None, "quote": "q"}},
            "conditions": {"control": {"label": "control", "treatments": [],
                                      "replicates": 2, "wells": ["B2", "B3"],
                                      "quote": "q"}},
            "steps": [{"kind": "wash", "text": "wash twice", "short_title": "", "quote": "q",
                       "minutes": None, "temp_c": None, "caution": None,
                       "pipette": {"liquid": "pbs", "volume": 200, "unit": "ul",
                                   "target": {"type": "condition", "value": "control"},
                                   "mix": None, "repeat": 2},
                       "wells": ["B2", "B3"]}],
            "backups": []}
    started = proto.build_start_plan(wash)
    assert [s["text"] for s in started["steps"]] == ["wash twice (1 of 2)", "wash twice (2 of 2)"]
    assert started["steps"][0]["pipette"]["wells"] == ["B2", "B3"]
    assert started["conditions"]["control"]["treatments"] == []
    print("P4b ok: repeat expansion on Start")

    # P7. Full-mode gate refuses without calling Meta.
    n_before = len(calls)
    api.PROTOCOL_FULL_MODE = False
    try:
        r = c.post("/protocols/import", files={"file": ("p.pdf", PDF, "application/pdf")})
    finally:
        api.PROTOCOL_FULL_MODE = True
    assert r.status_code == 403, r.status_code
    assert len(calls) == n_before, "gated import must not call Meta"
    print("P7 ok: gate 403s with no Meta call")

    # P8. txt uploads ride input_text with raw evidence.
    FakeClient.spark_payload = extraction(good)
    with patch.object(FakeClient, "post", spy):
        r = c.post("/protocols/import", files={"file": ("m.txt", b"Seed control wells.", "text/plain")})
    body = seen[-1]
    assert len(body["input"][0]["content"]) == 1, body["input"]
    assert "Seed control wells." in body["input"][0]["content"][0]["text"]
    assert r.json()["reply"].startswith("Draft ready:"), r.json()
    print("P8 ok: txt path works")

    # P12. old records convert on load: kinds inferred, tips kept, no cautions.
    from server.store import migrate_plan
    old = {"groups": {"ctrl": {"wells": ["B2", "B3"], "n": 2}},
           "steps": [{"name": "vehicle B2-B8", "wells": ["B2", "B3"]},
                     {"name": "30 min incubation", "wells": [], "tip": "t", "minutes": 30}],
           "backups": []}
    new = migrate_plan(old)
    assert new["conditions"]["ctrl"] == {"label": "ctrl", "treatments": [],
                                        "replicates": 2, "wells": ["B2", "B3"]}
    assert new["steps"][0]["kind"] == "pipette" and new["steps"][0]["tip"] is None
    assert new["steps"][1]["kind"] == "incubate" and new["steps"][1]["tip"] == "t"
    assert all(s["caution"] is None for s in new["steps"])
    assert migrate_plan(new) is new, "idempotent"
    print("P12 ok: on-load migration")

    # P13. expiry suggestion merges into the same-key step card; entry proposes timers.
    import time as _t
    s13 = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    s13.start_demo_run()
    s13.r["step_index"] = 2
    sess13: dict = {}
    r = s13.apply_calls([{"name": "next_step", "args": {}}], "t", "move on", "spark", sess13)
    assert len(r["cards"]) == 1 and r["cards"][0]["payload"]["target"] == 4, r["cards"]
    s13.r["timers"].append({"id": "x", "label": "drug A · C2 · 5 min", "minutes": 0.001,
                           "on_done": None, "started_at": _t.time() - 10, "step": "drug A",
                           "wells": "C2", "warned": True, "notified": False})
    s13.check_timers(_t.time())
    cards = list(s13.r["cards"].values())
    assert len(cards) == 1, cards
    assert cards[0]["payload"]["target"] == 4 and cards[0]["payload"]["count"] == 2, cards
    s13b = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    s13b.start_demo_run()
    s13b.r["step_index"] = 3
    r = s13b.apply_calls([{"name": "next_step", "args": {}}], "t", "move on", "spark", {})
    _, newcards, _ = s13b.confirm(r["cards"][0]["id"], True, "card", {})
    sug = [c for c in newcards if c["kind"] == "timer_suggest"]
    assert len(sug) == 1 and sug[0]["payload"]["minutes"] == 30, newcards
    print("P13 ok: outcome card keys + entry timer suggestion")

    # P14. step cautions ride where_am_i / whats_next.
    from server.grammar import whats_next_reply, where_am_i_reply
    assert where_am_i_reply({"step": 2, "total": 5, "step_name": "drug A",
                             "done": [], "caution": "do not mix"}).endswith(
        "⚠️ Protocol: do not mix")
    assert whats_next_reply({"next_step": "x", "next_caution": "go slow"}).endswith(
        "⚠️ Protocol: go slow")
    assert "⚠️" not in whats_next_reply({"next_step": "", "next_caution": ""})
    print("P14 ok: caution in guidance replies")

    # P15a. wrong model quote: code re-grounds the condition row by itself.
    ev = proto.evidence_from_text(
        "Experimental design\nGroup\nTreatment\nReplicates\nControl\n"
        "Vehicle (PBS)\n5\nDrug A\nDrug A, 10 uM working solution\n5\n")
    raw = {"title": "Experimental design", "short_title": "Experimental Design", "liquids": [], "conditions": [
        {"id": "control", "label": "Control", "treatments": [], "replicates": 5,
         "wells": None, "quote": "Use one row of the plate per group."}], "steps": []}
    d = proto.build_draft(raw, ev, "x.pdf")
    c = d["plan"]["conditions"]["control"]
    assert c["quote"] == "Control Vehicle (PBS) 5", c["quote"]
    assert c["quote_source"] == "code", c
    assert d["review"] == [] and d["errors"] == [], (d["review"], d["errors"])
    print("P15a ok: row repair from a wrong quote")

    # P15b. adjacent-sentence caution: verbatim across a line break grounds.
    l1 = ("1. Add medium. Add 100 uL of fresh medium to every well that will "
          "be used. Tip: dispense")
    l2 = "slowly against the well wall so the cell layer is not disturbed."
    ev = proto.evidence_from_text(l1 + "\n" + l2 + "\n")
    raw = {"liquids": [{"id": "medium", "name": "medium", "concentration": None,
                        "prep": None, "quote": l1}],
           "conditions": [], "steps": [
        {"kind": "pipette", "text": "Add medium", "short_title": "",
         "quote": "Add 100 uL of fresh medium to every well that will be used.",
         "minutes": None, "temp_c": None,
         "caution": "Tip: dispense slowly against the well wall so the cell "
                    "layer is not disturbed.",
         "pipette": {"liquid": "medium", "volume": 100, "unit": "uL",
                     "target": {"type": "all_used", "value": None},
                     "mix": None, "repeat": None}}]}
    d = proto.build_draft(raw, ev, "x.pdf")
    s = d["plan"]["steps"][0]
    assert "tip: dispense" in proto.norm_quote(s["quote"]), s["quote"]
    assert s["quote_source"] == "code", s
    assert not [i for i in d["review"] if i["path"].startswith("steps.0")], d["review"]
    assert d["errors"] == [], d["errors"]
    print("P15b ok: adjacent caution repair")

    # P15c. numbers absent from the doc still flag; source stays model.
    raw = {"liquids": [], "conditions": [], "steps": [
        {"kind": "incubate", "text": "incubate", "short_title": "", "quote": S5, "minutes": 99,
         "temp_c": None, "caution": None, "pipette": None}]}
    d = proto.build_draft(raw, demo_evidence(), "x.pdf")
    assert ("steps.0.minutes", "ungrounded_value") in {
        (i["path"], i["code"]) for i in d["review"]}, d["review"]
    assert d["plan"]["steps"][0]["quote_source"] == "model", d["plan"]["steps"][0]
    raw = {"liquids": [], "conditions": [
        {"id": "control", "label": "control", "treatments": [], "replicates": 10,
         "wells": None, "quote": SEED_Q}], "steps": []}
    d = proto.build_draft(raw, demo_evidence(), "x.pdf")
    assert ("conditions.control.replicates", "ungrounded_value") in {
        (i["path"], i["code"]) for i in d["review"]}, d["review"]
    print("P15c ok: absent numbers still flag")

print("ALL PASS")
