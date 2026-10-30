"""Hand off (docs/design/handoff.md): a finished run's changes, the approved
as-run protocol, the Spark loop (model and simulator faked), the run-log
match, and the hand off screens by voice and tap.

Run: python -m server.tests.test_handoff (runs on import).
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import handoff as hand  # noqa: E402
from server import runs as runlog  # noqa: E402
from server.store import RecordStore  # noqa: E402


def pip(liquid, volume, unit, wells, title):
    return {"kind": "pipette", "text": title, "short_title": title, "quote": f"{title} quote", "quote_page": 1,
            "minutes": None, "temp_c": None, "caution": None, "wells": wells,
            "pipette": {"liquid": liquid, "volume": volume, "unit": unit, "wells": wells}}


PLAN = {"conditions": {"ctrl": {"label": "Control", "wells": ["B2", "B3", "B4"], "replicates": 3},
                       "drug": {"label": "Drug A", "wells": ["C2", "C3"], "replicates": 2}},
        "steps": [pip("Medium", 100, "ul", ["B2", "B3", "B4", "C2", "C3"], "Add medium"),
                  pip("PBS", 10, "ul", ["B2", "B3", "B4"], "Add PBS"),
                  pip("Drug A", 0.01, "ml", ["C2", "C3"], "Add Drug A"),
                  {"kind": "incubate", "text": "Incubate", "short_title": "Incubate", "quote": "Incubate 30 min",
                   "quote_page": 2, "minutes": 30, "temp_c": None, "caution": None, "pipette": None, "wells": []},
                  pip("Stop", 5, "ul", ["B2"], "Add stop")],
        "backups": []}


def mark(step, *wells, source="voice"):
    return [{"step": step, "well": w, "source": source, "ts": 1.0} for w in wells]


def ran(mode: str = "full") -> RecordStore:
    """A run that went off plan in every way the record can show, ended on
    the incubate step (step 5 never reached), and finished."""
    st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    st._begin_locked(json.loads(json.dumps(PLAN)), None, "Hand test")
    st.r["run"]["mode"] = mode
    st.r["done"] = mark(1, "B2", "B3", "B4", "C2", "C3") + mark(1, "C4", source="tap") \
        + mark(2, "B2", "B4") + mark(3, "C2", "C9")
    st.r["excluded"] = {"C3": "bubbles"}
    st.r["redo"] = {"C9": "retry for C3"}
    st.r["skipped"] = [{"step": 2, "well": "B3", "ts": 5.0}]
    st.r["step_index"] = 3
    st.r["timers"] = [{"id": "t1", "label": "Incubate", "minutes": 35, "planned": 30, "on_done": None,
                       "started_at": 0.0, "step": "Incubate", "wells": "", "warned": True, "notified": True,
                       "step_index": 3}]
    return st


def events_for(steps, wrong=None):
    """The simulator's events for exactly these steps (optionally broken)."""
    ev = []
    for s in steps:
        ev.append({"op": "comment", "text": f"step {s['n']}"})
        if s["kind"] == "delay":
            ev.append({"op": "delay", "minutes": s["minutes"]})
            continue
        for w in s["wells"]:
            ev.append({"op": "aspirate", "volume": s["volume_ul"], "well": s["source"], "labware": hand.RESERVOIR})
            ev.append({"op": "dispense", "volume": s["volume_ul"], "well": w, "labware": hand.PLATE})
    return wrong(ev) if wrong else ev


def code_for(steps) -> str:
    """Protocol code that does exactly these steps (what Spark should write)."""
    d = hand.DECK
    lines = ["from opentrons import protocol_api", "",
             f'requirements = {{"robotType": "Flex", "apiLevel": "{d["api_level"]}"}}', "",
             "def run(protocol):",
             f'    tips = protocol.load_labware("{d["tips"]["load_name"]}", "{d["tips"]["slot"]}")',
             f'    res = protocol.load_labware("{d["reservoir"]["load_name"]}", "{d["reservoir"]["slot"]}")',
             f'    plate = protocol.load_labware("{d["plate"]["load_name"]}", "{d["plate"]["slot"]}")',
             f'    protocol.load_trash_bin("{d["trash"]}")',
             f'    p = protocol.load_instrument("{d["pipette"]["name"]}", "{d["pipette"]["mount"]}", tip_racks=[tips])']
    for s in steps:
        lines.append(f'    protocol.comment("step {s["n"]}")')
        if s["kind"] == "delay":
            lines.append(f'    protocol.delay(minutes={s["minutes"]})')
        else:
            lines.append(f'    for w in {s["wells"]}:')
            lines.append(f'        p.transfer({s["volume_ul"]}, res["{s["source"]}"], plate[w])')
    return "\n".join(lines) + "\n"


# HO1. Changes, each with the record event it came from; step 5 never reached.
st = ran()
doc = st._finish_locked("finished")
rec = doc["record"]
assert rec["timers"] == [] and rec["timer_log"][0]["minutes"] == 35, rec["timer_log"]  # actual time kept
d = hand.changes(rec)
assert (d["of"], len(d["rows"]), d["changed"]) == (5, 4, 4), d
r1, r2, r3, r4 = d["rows"]
assert [(c["kind"], c["wells"], c["event"]) for c in r1["changes"]] == [
    ("excluded", ["C3"], "excluded"), ("off_plan", ["C4"], "done")], r1
assert r1["changes"][0]["why"] == "bubbles" and r1["changes"][1]["source"] == "tap", r1
assert r1["plan"] != r1["run"] and r1["detail"] == r1["run"], r1
assert [(c["kind"], c["wells"]) for c in r2["changes"]] == [("skipped", ["B3"])], r2
assert [(c["kind"], c["wells"]) for c in r3["changes"]] == [("excluded", ["C3"]), ("redo", ["C9"])], r3
assert r3["changes"][1]["why"] == "retry for C3", r3
assert [(c["kind"], c["from"], c["to"]) for c in r4["changes"]] == [("timer", "30", "35")], r4
assert (r4["plan"], r4["run"]) == ("30 min", "35 min"), r4
steps = hand.as_run(rec)
assert [(s["kind"], s.get("source"), s.get("volume_ul"), s.get("wells"), s.get("minutes")) for s in steps] == [
    ("transfer", "A1", 100, ["B2", "B3", "B4", "C2", "C4"], None),
    ("transfer", "A2", 10, ["B2", "B4"], None),
    ("transfer", "A3", 10.0, ["C2", "C9"], None),  # 0.01 mL in µL
    ("delay", None, None, None, 35.0)], steps
assert hand.blockers(steps) == []
print("HO1 ok: changes with their events; as-run steps; step 5 not reached")

# HO1b. Timers keep their actual time when they leave: done (extended) and cancelled.
st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st._begin_locked(json.loads(json.dumps(PLAN)), None, "Timer test")
st.r["timers"] = [{"id": "a", "label": "A", "minutes": 12, "planned": 10, "started_at": 0.0, "step_index": 3,
                   "step": "x", "wells": "", "warned": True, "notified": True, "on_done": None}]
st._purge(12 * 60 + 61)
st.r["timers"] = [{"id": "b", "label": "B", "minutes": 10, "planned": 10, "started_at": 0.0, "step_index": 3,
                   "step": "x", "wells": "", "warned": False, "notified": False, "on_done": None}]
st._cancel({"messages": []}, "B", "voice", 4 * 60)
assert [(t["planned"], t["minutes"], t["status"]) for t in st.r["timer_log"]] == [
    (10, 12, "done"), (10, 4.0, "cancelled")], st.r["timer_log"]
print("HO1b ok: timer_log keeps planned and actual minutes")

# HO6. The match: right code passes; an excluded well, a wrong volume, a wrong order don't.
assert hand.check(events_for(steps), steps)["ok"]
bad_well = events_for(steps, lambda ev: ev[:1] + [
    {"op": "aspirate", "volume": 100, "well": "A1", "labware": hand.RESERVOIR},
    {"op": "dispense", "volume": 100, "well": "C3", "labware": hand.PLATE}] + ev[1:])  # C3 was excluded
chk = hand.check(bad_well, steps)
assert not chk["ok"] and "extra C3" in chk["steps"][0]["why"], chk
bad_vol = events_for(steps, lambda ev: [{**e, "volume": 50} if e.get("well") == "B4" and e["op"] == "dispense"
                                        and e["volume"] == 10 else e for e in ev])
chk = hand.check(bad_vol, steps)
assert not chk["ok"] and chk["steps"][1]["why"] == "B4 got 50 µL, not 10", chk
swapped = events_for([steps[1], steps[0], steps[2], steps[3]])
chk = hand.check(swapped, steps)
assert not chk["ok"] and chk["problems"], chk
assert not hand.check(events_for(steps)[:-1], steps)["ok"]  # no delay
print("HO6 ok: run-log match catches excluded wells, volumes, order, waits")


# HO3-5, HO7. The loop: Spark is scripted, the simulator faked.
def fc(name, args=None, cid="c"):
    return {"type": "function_call", "name": name, "arguments": json.dumps(args or {}), "call_id": cid}


def script(*turns):
    """Model turns in order; each turn is a list of function calls."""
    it = iter(turns)
    sent = []

    async def call(body):
        sent.append(json.loads(json.dumps(body)))
        return {"output": next(it, [])}
    return call, sent


def loop(call, sims):
    stages = []
    it = iter(sims)

    async def sim(code):
        return next(it)
    out = asyncio.run(hand.run_loop(call, steps, sim, lambda s, **k: stages.append(s)))
    return out, stages


OK = {"ok": True, "events": events_for(steps)}
BAD = {"ok": True, "events": events_for(steps)[:-1]}
ERR = {"ok": False, "error": "NameError: name 'plat' is not defined"}

call, sent = script([fc("read_run")], [fc("generate_protocol", {"code": "c1"})], [fc("simulate")], [fc("finish", {"summary": "ok"})])
out, stages = loop(call, [OK])
assert out["stage"] == "passed" and stages == ["generating", "simulating", "passed"], stages
assert sent[0]["tools"][0]["name"] == "read_run" and "comment(\"step N\")" in sent[0]["instructions"]
first = [i for i in sent[1]["input"] if i.get("type") == "function_call_output"][0]
assert json.loads(first["output"])["steps"] == steps  # read_run hands over the approved steps
print("HO3 ok: passes first try")

call, _ = script([fc("generate_protocol", {"code": "c1"})], [fc("simulate")],
                 [fc("generate_protocol", {"code": "c2"})], [fc("simulate")], [fc("finish", {"summary": "ok"})])
out, stages = loop(call, [ERR, OK])
assert out["stage"] == "passed" and stages == ["generating", "simulating", "fixing", "simulating", "passed"], stages
assert out["code"] == "c2" and out["attempt"] == 2, out
print("HO4 ok: one failure, fixing, then passed")

call, sent = script(*[t for _ in range(3) for t in ([fc("generate_protocol", {"code": "c"})], [fc("simulate")])])
out, stages = loop(call, [ERR, BAD, ERR])
assert out["stage"] == "needs_you" and stages[-1] == "needs_you" and "NameError" in out["error"], out
print("HO5 ok: three failures go to the person")

call, sent = script([fc("finish", {"summary": "done"})], [fc("generate_protocol", {"code": "c"})],
                    [fc("simulate")], [fc("finish", {"summary": "done"})])
out, stages = loop(call, [OK])
rejected = json.loads([i for i in sent[1]["input"] if i.get("type") == "function_call_output"][0]["output"])
assert rejected["ok"] is False and "simulate must pass" in rejected["error"], rejected
assert out["stage"] == "passed", out
print("HO7 ok: finish before a passing simulate is rejected")

# HO2, HO8, HO9. On the screens, by voice and tap: ⑨0 after the end card.
c = TestClient(api.app)
api.store = st = ran()
spawned = []
st.spawn_handoff = spawned.append
card = st.request_end_run({})[1]["card"]
j = c.post("/confirm", json={"card_id": card["id"], "ok": True, "session": "ho"}).json()
run_id = st.r["view"]["current"]["target"]
assert st.r["view"]["current"]["screen"] == "final_protocol" and run_id, st.r["view"]
assert j["message"].endswith("4 of 5 robot steps, 4 changes from the plan. Look right?"), j
snap = c.get("/record").json()
h = snap["handoff"]
assert h["run_id"] == run_id and h["approved"] is False and h["stage"] is None, h
assert h["summary"]["rows"][3]["changes"][0]["kind"] == "timer", h

j = c.post("/input", json={"text": "step 1", "session": "ho"}).json()  # the button shown: the first change
assert st.r["view"]["current"] == {"screen": "final_step", "target": f"{run_id}:1"}, st.r["view"]
assert j["reply"] == "Step 1: C3 excluded: bubbles; C4 marked off plan.", j
j = c.post("/input", json={"action": {"id": "show_step", "args": {"step": "2"}}, "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "final_step", "target": f"{run_id}:2"}, st.r["view"]
assert j["reply"] == "Step 2: B3 skipped.", j
j = c.post("/input", json={"text": "open PDF", "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "final_page", "target": f"{run_id}:2"}, st.r["view"]
c.post("/input", json={"text": "back to step", "session": "ho"})
j = c.post("/input", json={"text": "looks right", "session": "ho"}).json()  # the step's: back to the list
assert st.r["view"]["current"] == {"screen": "final_protocol", "target": run_id}, st.r["view"]
assert spawned == []
before = hand.record_hash(runlog.get(run_id, st.root)["record"])
j = c.post("/input", json={"text": "looks right", "session": "ho"}).json()  # the list's: approve
assert st.r["view"]["current"] == {"screen": "simulate", "target": run_id}, st.r["view"]
assert spawned == [run_id] and j["reply"] == "Writing the robot code…", j
assert st.handoff_steps(run_id) == steps

# the loop's progress lands on the run and the screen; the run record stays as it was (HO8)
st.handoff_stage(run_id, "simulating", attempt=1)
assert c.get("/record").json()["view"]["prompt"] == "Running it on a virtual robot…"
st.handoff_stage(run_id, "passed", code=code_for(steps), check=hand.check(events_for(steps), steps), attempt=1)
doc = runlog.get(run_id, st.root)
assert hand.record_hash(doc["record"]) == before
assert doc["handoff"]["csv"].startswith("well,group,status,steps,source,note\nB2,"), doc["handoff"]["csv"]
assert 'C3,"Drug A",excluded,1,voice,"bubbles"' in doc["handoff"]["csv"], doc["handoff"]["csv"]
snap = c.get("/record").json()
assert snap["view"]["prompt"] == "All 4 steps match your record. Export?", snap["view"]
assert snap["handoff"]["stage"] == "passed" and "protocol.comment(\"step 1\")" in snap["handoff"]["code"]
j = c.post("/input", json={"text": "export", "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "export_files", "target": run_id}, st.r["view"]
j = c.post("/input", json={"text": "export", "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "exported", "target": run_id}, st.r["view"]
out = st.root / "exports" / run_id
assert (out / "protocol.py").read_text() == code_for(steps) and (out / "plate_map.csv").exists()
j = c.post("/input", json={"text": "done", "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "run_view", "target": run_id}, st.r["view"]
print("HO9 ok: step, open PDF, looks right, export, export, done; HO8 ok: run record untouched")

# HO2. A changed run record clears the approval: back to ⑨0, said why.
doc = runlog.get(run_id, st.root)
doc["record"]["excluded"]["B2"] = "late note"
runlog.write_run(doc, st.root)
assert st.handoff_steps(run_id) is None
j = c.post("/input", json={"action": {"id": "navigate", "args": {"screen": "simulate", "target": run_id}},
                           "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "final_protocol", "target": run_id}, st.r["view"]
assert j["reply"].startswith("The run changed since you approved it."), j
st.handoff_stage(run_id, "passed")  # a loop still going can't write past a stale approval
assert runlog.get(run_id, st.root)["handoff"]["stage"] == "passed" and c.get("/record").json()["handoff"]["stage"] is None
print("HO2 ok: a changed run record clears the approval")

# A step without a volume can't go to the robot: said, never guessed.
st2 = ran()
st2.r["plan"]["steps"][1]["pipette"]["volume"] = None
doc2 = st2._finish_locked("finished")
assert hand.blockers(hand.as_run(doc2["record"])) == ["Step 2 (Add PBS) has no volume in µL."]
print("HO-vol ok: no volume, no approval")

# HO10. Private runs have no hand off.
api.store = st = ran("private")
card = st.request_end_run({})[1]["card"]
c.post("/confirm", json={"card_id": card["id"], "ok": True, "session": "ho"})
assert st.r["view"]["current"]["screen"] != "final_protocol", st.r["view"]
j = c.post("/input", json={"action": {"id": "make_robot", "args": {}}, "session": "ho"}).json()
assert j["reply"] == "Hand off is off in Private mode." and st.r["view"]["current"]["screen"] != "final_protocol", j
print("HO10 ok: private runs skip hand off")

# Live: the local simulator on the code for these steps (skipped when .venv-sim isn't installed).
if hand.SIM_PYTHON.exists():
    res = hand.simulate(code_for(steps))
    assert res["ok"], res
    chk = hand.check(res["events"], steps)
    assert chk["ok"], chk
    print("HO-live ok: opentrons_simulate run log matches the approved steps")
else:
    print("HO-live skipped: .venv-sim not installed")
print("ALL PASS")
