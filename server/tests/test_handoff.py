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


def approved_for(rec, timing=None, title="Hand test", n=1, date="2026-10-30"):
    """What approve_handoff stores: steps, liquids, the setup head, the verifier's seconds."""
    steps, liqs = hand.place(rec, hand.as_run(rec, timing))
    return {"steps": steps, "liquids": liqs, "setup": hand.setup_code(steps, liqs, title, n, date),
            "seconds": hand.expected_seconds(rec, steps, timing)}


def events_for(steps, wrong=None):
    """The simulator's events for exactly these steps (optionally broken)."""
    ev = []
    for s in steps:
        ev.append({"op": "comment", "text": s["comment"]})
        if s["kind"] == "delay":
            ev.append({"op": "delay", "seconds": s["seconds"]})
            continue
        size = hand.DECK["pipettes"][s["pipette"]]["max_ul"]
        rack = hand.DECK["pipettes"][s["pipette"]]["tips"]["load_name"]
        loads = {c["var"]: c["load_name"] for c in hand.CONTAINERS.values()}
        for src in s["sources"]:
            for w in src["wells"]:
                ev.append({"op": "pick_up_tip", "labware": rack, "pipette": size})
                ev.append({"op": "aspirate", "volume": s["volume_ul"], "well": src["well"],
                           "labware": loads[src["labware"]], "pipette": size})
                ev.append({"op": "dispense", "volume": s["volume_ul"], "well": w, "labware": hand.PLATE,
                           "pipette": size})
    return wrong(ev) if wrong else ev


def code_for(approved) -> str:
    """Protocol code that does exactly these steps (what Spark should write): the head, then the steps."""
    lines = []
    for s in approved["steps"]:
        lines.append(f"    protocol.comment({json.dumps(s['comment'], ensure_ascii=False)})")
        if s["kind"] == "delay":
            lines.append(f"    protocol.delay(seconds={s['seconds']}, msg={json.dumps(s['msg'], ensure_ascii=False)})")
        else:
            for src in s["sources"]:
                lines.append(f"    {s['pipette']}.transfer({s['volume_ul']}, {src['labware']}[\"{src['well']}\"], "
                             f"[plate[w] for w in {src['wells']}], new_tip=\"always\")")
    return approved["setup"] + "\n".join(lines) + "\n"


# HO1. Changes, each with the record event it came from; step 5 never reached.
st = ran()
doc = st._finish_locked("finished")
rec = doc["record"]
assert rec["timers"] == [] and rec["timer_log"][0]["duration_s"] == 2100, rec["timer_log"]  # actual time kept
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
assert [(c["kind"], c["from"], c["to"], c["text"]) for c in r4["changes"]] == [
    ("timer", "30 min", "35 min", "ran 35 min (plan 30 min)")], r4
assert (r4["plan"], r4["run"]) == ("30 min", "35 min"), r4
approved = approved_for(rec)
steps = approved["steps"]
assert [(s["kind"], [(x["labware"], x["well"]) for x in s.get("sources") or []], s.get("volume_ul"), s.get("wells"),
         s.get("pipette"), s.get("seconds")) for s in steps] == [
    ("transfer", [("tubes", "A1")], 100, ["B2", "B3", "B4", "C2", "C4"], "p1000", None),
    ("transfer", [("tubes", "B1")], 10, ["B2", "B4"], "p50", None),
    ("transfer", [("tubes", "C1")], 10.0, ["C2", "C9"], "p50", None),  # 0.01 mL in µL; ≤ 50 µL: the 50
    ("delay", [], None, None, None, 2100.0)], steps
assert steps[0]["comment"] == "Step 1: Add medium · 100 µL" and steps[3]["comment"] == "Step 4: Incubate · as run 35 min (plan 30 min)"
assert steps[3]["msg"] == "Incubate (as run: 35 min; plan: 30 min)", steps[3]
assert hand.blockers(steps, approved["liquids"]) == []
print("HO1 ok: changes with their events; as-run steps with pipettes and seconds; step 5 not reached")

# HO1c. Liquids: reservoir well in plan order, what's drawn + 10 %, condition tone (medium neutral);
# the head is code-made (metadata, both pipettes, define_liquid / load_liquid, empty plate wells).
# small amounts sit in 1.5 mL tubes: drawn x 1.1 + the tube's dead volume
assert [(lq["name"], lq["labware"], lq["well"], lq["volume_ul"], lq["color"]) for lq in approved["liquids"]] == [
    ("Medium", "tubes", "A1", 600.0, hand.NEUTRAL_COLOR),  # 5 wells x 100 µL x 1.1 + 50
    ("PBS", "tubes", "B1", 72.0, hand.LIQUID_COLORS[0]),     # ctrl (sorted first) wells
    ("Drug A", "tubes", "C1", 72.0, hand.NEUTRAL_COLOR)], approved["liquids"]  # C9 is outside Drug A's wells
head = approved["setup"]
assert '"protocolName": "Hand test — Run 1 (as-run)"' in head and '"author": "Glassistant (Milo)"' in head
assert "2026-10-30. Generated from the recorded run; simulated with opentrons_simulate." in head
assert '"apiLevel": "2.22"' in head and 'load_instrument("flex_1channel_50", "left"' in head
assert 'load_instrument("flex_1channel_1000", "right"' in head
assert 'tubes = protocol.load_labware("opentrons_24_tuberack_nest_1.5ml_snapcap", "B1")' in head
assert "res = protocol" not in head  # no reservoir when nothing needs one
assert 'tubes.load_liquid(["A1"], 600, liquid_1)' in head and f'display_color="{hand.NEUTRAL_COLOR}"' in head
assert "500 µL drawn by the run + 10 % + 50 µL dead volume per tube" in head
assert 'plate.load_empty(["B2", "B3", "B4", "C2", "C4", "C9"])' in head
# more than a tube: reservoir wells (dead volume 1800 µL each), split when one would overflow
mid_steps, mid = hand.place(rec, [{**steps[0], "volume_ul": 300.0}])  # 5 x 300 = 1500 µL drawn
assert [(lq["labware"], lq["well"], lq["volume_ul"]) for lq in mid] == [("reservoir", "A1", 3450.0)], mid
big_steps, big = hand.place(rec, [{**steps[0], "volume_ul": 3000.0}])  # 15000 µL drawn
assert [(lq["labware"], lq["well"], lq["drawn_ul"], lq["volume_ul"]) for lq in big] == [
    ("reservoir", "A1", 12000.0, 15000.0), ("reservoir", "A2", 3000.0, 5100.0)], big
assert [(x["well"], x["wells"]) for x in big_steps[0]["sources"]] == [("A1", ["B2", "B3", "B4", "C2"]), ("A2", ["C4"])], big_steps[0]["sources"]
assert all(lq["volume_ul"] <= hand.CONTAINERS["reservoir"]["max_ul"] for lq in big) and hand.blockers(big_steps, big) == []
bh = hand.setup_code(big_steps, big, "Hand test", 1, "2026-10-30")
assert 'res.load_liquid(["A1"], 15000, liquid_1)' in bh and 'res.load_liquid(["A2"], 5100, liquid_1)' in bh
assert "(2 reservoir wells)" in bh
print("HO1c ok: liquids, colors and the code-made head")

# HO1b. Timers keep their actual time in seconds when they leave: done (extended) and cancelled.
st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st._begin_locked(json.loads(json.dumps(PLAN)), None, "Timer test")
st.r["timers"] = [{"id": "a", "label": "A", "minutes": 12, "planned": 10, "started_at": 0.0, "step_index": 3,
                   "step": "x", "wells": "", "warned": True, "notified": True, "on_done": None}]
st._purge(12 * 60 + 61)
st.r["timers"] = [{"id": "b", "label": "B", "minutes": 10, "planned": 10, "started_at": 0.0, "step_index": 3,
                   "step": "x", "wells": "", "warned": False, "notified": False, "on_done": None}]
st._cancel({"messages": []}, "B", "voice", 18.04)
assert [(t["planned_s"], t["duration_s"], t["status"]) for t in st.r["timer_log"]] == [
    (600, 720, "done"), (600, 18.0, "cancelled")], st.r["timer_log"]
print("HO1b ok: timer_log keeps planned and actual seconds (18 s stays 18 s, not 0.3 min)")


# HO4b. Waits of 0.3 s, 18 s, 90 s and 30 min: seconds in the code, said as people say them,
# checked against the record; Use plan swaps in the plan's time.
def waited(secs, status="cancelled"):
    r = json.loads(json.dumps(rec))
    r["timer_log"] = [{"step_index": 3, "label": "Incubate", "planned_s": 1800, "duration_s": secs,
                       "started_at": 0, "ended_at": secs, "status": status}]
    return r


for secs, said, run_txt in [(0.3, "stopped after <1 s", "<1 s"), (18, "stopped after 18 s", "18 s"),
                            (90, "stopped after 90 s", "90 s")]:
    r = waited(secs)
    row = hand.changes(r)["rows"][3]
    assert row["changes"][0]["text"] == f"{said} (plan 30 min)" and row["run"] == run_txt, row
    a = approved_for(r)
    w = a["steps"][3]
    assert w["seconds"] == secs and a["seconds"]["4"] == secs, (w, a["seconds"])
    assert f"protocol.delay(seconds={secs}," in code_for(a), code_for(a)
    assert w["msg"] == f"Incubate (as run: {run_txt}; plan: 30 min)", w
    assert hand.check(events_for(a["steps"]), a["steps"], a["seconds"])["ok"]
    wrong = hand.check(events_for(a["steps"], lambda ev: [{**e, "seconds": secs * 60} if e["op"] == "delay" else e
                                                           for e in ev]), a["steps"], a["seconds"])
    assert not wrong["ok"] and "waits" in wrong["steps"][3]["why"], wrong  # minutes for seconds: caught
r = waited(1800, "done")
assert not hand.changes(r)["rows"][3]["changes"] and approved_for(r)["steps"][3]["seconds"] == 1800
plan_a = approved_for(waited(18), {"3": "plan"})
assert plan_a["steps"][3]["seconds"] == 1800 and plan_a["seconds"]["4"] == 1800, plan_a["steps"][3]
assert hand.changes(waited(18), {"3": "plan"})["rows"][3]["run"] == "30 min"
print("HO4b ok: 0.3 s, 18 s, 90 s, 30 min waits; Keep as run / Use plan")

# HO6. The match: right code passes; an excluded well, a wrong volume, order, pipette, tips don't.
S = approved["seconds"]
assert hand.check(events_for(steps), steps, S)["ok"]
bad_well = events_for(steps, lambda ev: ev[:1] + [
    {"op": "pick_up_tip", "labware": "x", "pipette": 1000},
    {"op": "aspirate", "volume": 100, "well": "A1", "labware": hand.TUBES, "pipette": 1000},
    {"op": "dispense", "volume": 100, "well": "C3", "labware": hand.PLATE, "pipette": 1000}] + ev[1:])  # C3 excluded
chk = hand.check(bad_well, steps, S)
assert not chk["ok"] and "extra C3" in chk["steps"][0]["why"], chk
bad_vol = events_for(steps, lambda ev: [{**e, "volume": 50} if e.get("well") == "B4" and e["op"] == "dispense"
                                        and e["volume"] == 10 else e for e in ev])
chk = hand.check(bad_vol, steps, S)
assert not chk["ok"] and chk["steps"][1]["why"] == "B4 got 50 µL, not 10", chk
swapped = events_for([steps[1], steps[0], steps[2], steps[3]])
chk = hand.check(swapped, steps, S)
assert not chk["ok"] and chk["problems"], chk
assert not hand.check(events_for(steps)[:-1], steps, S)["ok"]  # no delay
big_pip = events_for(steps, lambda ev: [{**e, "pipette": 1000} if e.get("volume") == 10 else e for e in ev])
chk = hand.check(big_pip, steps, S)
assert not chk["ok"] and chk["steps"][1]["why"] == "uses the 1000 µL pipette, not the 50 µL", chk
one_tip = events_for(steps, lambda ev: [e for k, e in enumerate(ev)
                                        if not (e["op"] == "pick_up_tip" and k > 1 and ev[k - 1]["op"] == "dispense")])
chk = hand.check(one_tip, steps, S)
assert not chk["ok"] and "tips for" in chk["steps"][0]["why"], chk
L = approved["liquids"]
assert hand.check(events_for(steps), steps, S, L)["ok"]
wrong_src = events_for(steps, lambda ev: [{**e, "well": "D1"} if e["op"] == "aspirate" and e["volume"] == 10 else e
                                          for e in ev])
chk = hand.check(wrong_src, steps, S, L)
assert not chk["ok"] and chk["steps"][1]["why"].startswith("B2 gets liquid from D1"), chk
greedy = events_for(steps, lambda ev: [{**e, "volume": 40} if e["op"] == "aspirate" and e["well"] == "B1" else e
                                       for e in ev])  # 80 µL from a tube that holds 22 above its dead volume
chk = hand.check(greedy, steps, S, L)
assert not chk["ok"] and any("PBS: 80 µL drawn from Tube B1" in p for p in chk["problems"]), chk
print("HO6 ok: run-log match catches excluded wells, volumes, order, waits, pipettes, tips, containers and amounts")


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
    out = asyncio.run(hand.run_loop(call, approved, sim, lambda s, **k: stages.append(s)))
    return out, stages


GOOD = code_for(approved)
OK = {"ok": True, "events": events_for(steps)}
BAD = {"ok": True, "events": events_for(steps)[:-1]}
ERR = {"ok": False, "error": "NameError: name 'plat' is not defined"}

call, sent = script([fc("read_run")], [fc("generate_protocol", {"code": GOOD})], [fc("simulate")],
                   [fc("finish", {"summary": "ok"})])
out, stages = loop(call, [OK])
assert out["stage"] == "passed" and stages == ["generating", "simulating", "passed"], stages
assert out["tips"] == {"opentrons_flex_96_tiprack_1000ul": 5, "opentrons_flex_96_tiprack_50ul": 4}, out["tips"]
assert sent[0]["tools"][0]["name"] == "read_run" and "setup_code exactly" in sent[0]["instructions"]
first = json.loads([i for i in sent[1]["input"] if i.get("type") == "function_call_output"][0]["output"])
assert first["steps"] == steps and first["setup_code"] == approved["setup"]  # read_run hands over the approval
print("HO3 ok: passes first try")

# The head is the record's: code that changes it never reaches the simulator.
call, sent = script([fc("generate_protocol", {"code": GOOD.replace("], 600,", "], 500,")})], [fc("simulate")])
out, stages = loop(call, [OK])
assert out["stage"] == "needs_you" and "setup_code was changed" in out["error"], out
print("HO3b ok: an edited setup head fails before simulating")
print("HO3 ok: passes first try")

call, _ = script([fc("generate_protocol", {"code": GOOD + "# 1"})], [fc("simulate")],
                 [fc("generate_protocol", {"code": GOOD})], [fc("simulate")], [fc("finish", {"summary": "ok"})])
out, stages = loop(call, [ERR, OK])
assert out["stage"] == "passed" and stages == ["generating", "simulating", "fixing", "simulating", "passed"], stages
assert out["code"] == GOOD and out["attempt"] == 2, out
print("HO4 ok: one failure, fixing, then passed")

call, sent = script(*[t for _ in range(3) for t in ([fc("generate_protocol", {"code": GOOD})], [fc("simulate")])])
out, stages = loop(call, [ERR, BAD, ERR])
assert out["stage"] == "needs_you" and stages[-1] == "needs_you" and "NameError" in out["error"], out
print("HO5 ok: three failures go to the person")

call, sent = script([fc("finish", {"summary": "done"})], [fc("generate_protocol", {"code": GOOD})],
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
plan = st.handoff_plan(run_id)
assert plan["steps"] == steps and plan["liquids"] == approved["liquids"] and plan["seconds"] == approved["seconds"]
assert '"protocolName": "Hand test — Run 1 (as-run)"' in plan["setup"], plan["setup"][:300]

# the loop's progress lands on the run and the screen; the run record stays as it was (HO8)
st.handoff_stage(run_id, "simulating", attempt=1)
assert c.get("/record").json()["view"]["prompt"] == "Running it on a virtual robot…"
st.handoff_stage(run_id, "passed", code=code_for(plan), check=hand.check(events_for(steps), steps, plan["seconds"]),
                 attempt=1)
doc = runlog.get(run_id, st.root)
assert hand.record_hash(doc["record"]) == before
csv = doc["handoff"]["csv"].splitlines()
assert csv[:2] == ["# protocol: Hand test", "# run: Run 1"] and csv[2].startswith("# date: 20"), csv[:3]
assert csv[3:6] == ["# reagent: Medium · Tube A1 · 600 µL (500 drawn + 10 % + 50 dead)",
                    "# reagent: PBS · Tube B1 · 72 µL (20 drawn + 10 % + 50 dead)",
                    "# reagent: Drug A · Tube C1 · 72 µL (20 drawn + 10 % + 50 dead)"], csv[3:6]
csv = csv[3:]
assert csv[3] == "well,group,contents,total_uL,status,excluded_reason,logged_by,note", csv[3]
assert csv[4] == 'B2,"Control","Medium 100 µL; PBS 10 µL",110,done,"",voice,""', csv[4]
assert 'C3,"Drug A","Medium 100 µL",100,excluded,"bubbles",voice,""' in csv, csv
assert 'C9,"","Drug A 10 µL",10,redo,"",voice,"retry for C3"' in csv, csv
snap = c.get("/record").json()
assert snap["view"]["prompt"] == "All 4 steps match your record. Export?", snap["view"]
assert snap["handoff"]["stage"] == "passed" and 'protocol.comment("Step 1: Add medium · 100 µL")' in snap["handoff"]["code"]
assert snap["handoff"]["steps"] == steps and snap["handoff"]["liquids"] == approved["liquids"]  # ⑨ replays, ⑨b's line
j = c.post("/input", json={"text": "export", "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "export_files", "target": run_id}, st.r["view"]
# ⑨b Export: the iPad share sheet gets both files (named by protocol and run); the screen holds
j = c.post("/input", json={"text": "export", "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "export_files", "target": run_id}, st.r["view"]
assert j["reply"] == "Pick where to send them.", j
share = [a for a in j["ui"] if a["action"] == "share"][0]
assert [f["name"] for f in share["files"]] == ["HandTest_Run1_protocol.py", "HandTest_Run1_plate_map.csv"], share
r = c.get(share["files"][0]["url"])
assert r.status_code == 200 and r.text == code_for(plan), r.text[:80]
assert c.get(share["files"][1]["url"]).text.startswith("# protocol: Hand test")
for bad in (f"/handoff/{run_id}/files/record.json", "/handoff/..%2F..%2Fserver/files/protocol.py",
            "/handoff/run-nope/files/protocol.py"):
    assert c.get(bad).status_code == 404, bad  # known names under a finished run only
j = c.post("/input", json={"text": "back", "session": "ho"}).json()  # sheet up: no screen changes
assert j["reply"] == "Finish sharing first." and st.r["view"]["current"]["screen"] == "export_files", j
j = c.post("/input", json={"action": {"id": "share_result", "args": {"done": "no"}}, "session": "ho"}).json()
assert j["reply"] == "Not shared. Say export to try again." and st.r["view"]["current"]["screen"] == "export_files", j
c.post("/input", json={"text": "done", "session": "ho"})  # not shared, and done: Home
assert st.r["view"]["current"]["screen"] == "home" and st.r["view"]["stack"] == [], st.r["view"]
c.post("/input", json={"action": {"id": "navigate", "args": {"screen": "export_files", "target": run_id}},
                       "session": "ho"})  # the approval still holds: straight back to the files
assert st.r["view"]["current"] == {"screen": "export_files", "target": run_id}, st.r["view"]
c.post("/input", json={"text": "export", "session": "ho"})
j = c.post("/input", json={"action": {"id": "share_result", "args": {"done": "yes"}}, "session": "ho"}).json()
assert st.r["view"]["current"] == {"screen": "exported", "target": run_id}, st.r["view"]
assert j["reply"] == "Shared protocol.py and plate_map.csv. A copy stays with the run.", j
assert runlog.get(run_id, st.root)["handoff"]["exported"] and "sharing" not in runlog.get(run_id, st.root)["handoff"]
j = c.post("/input", json={"text": "done", "session": "ho"}).json()  # shared, and done: Home
assert st.r["view"]["current"]["screen"] == "home" and st.r["view"]["stack"] == [], st.r["view"]

# Home: the finished run's robot code, each final protocol step with the lines it became
code = c.get(f"/runs/{run_id}").json()["handoff"]
lines = code["code"].splitlines()
assert code["code"] == code_for(plan) and code["exported"] is True
assert "# --- setup" in lines[code["setup"][0] - 1] and lines[code["setup"][1] - 1].strip() == "# --- steps ---", code["setup"]
assert [(x["n"], x["title"], x["detail"]) for x in code["steps"]] == [
    (1, "Add medium", "100 µL · B2, B3, B4, C2, C4"), (2, "Add PBS", "10 µL · B2, B4"),
    (3, "Add Drug A", "0.01 mL · C2, C9"), (4, "Incubate", "35 min")], code["steps"]
for x in code["steps"]:
    a, b = x["lines"]
    assert lines[a - 1].strip().startswith(f'protocol.comment("Step {x["n"]}:'), (x, lines[a - 1])
    assert a <= b and not any("protocol.comment(\"Step" in ln for ln in lines[a:b]), x  # up to the next step
assert [x["color"] for x in code["steps"]] == [hand.NEUTRAL_COLOR, hand.LIQUID_COLORS[0], hand.NEUTRAL_COLOR, hand.WAIT_COLOR]
assert code["steps"][-1]["lines"][1] == len(lines) and code["steps"][3]["changes"] == ["ran 35 min (plan 30 min)"]
# Home: the run record shares the files too (Share takes the greyed Start run's place)
c.post("/input", json={"action": {"id": "navigate", "args": {"screen": "run_view", "target": run_id}}, "session": "ho"})
slots = c.get("/record").json()["view"]["slots"]
assert slots.get("status") == "finished" and slots.get("code") == "yes", slots
j = c.post("/input", json={"text": "share", "session": "ho"}).json()
assert [a["action"] for a in j["ui"]] == ["share"] and st.r["view"]["current"]["screen"] == "run_view", j
j = c.post("/input", json={"text": "back", "session": "ho"}).json()
assert j["reply"] == "Finish sharing first." and st.r["view"]["current"]["screen"] == "run_view", j
j = c.post("/input", json={"action": {"id": "share_result", "args": {"done": "yes"}}, "session": "ho"}).json()
assert j["reply"] == "Shared protocol.py and plate_map.csv." and st.r["view"]["current"]["screen"] == "run_view", j
c.post("/input", json={"text": "back", "session": "ho"})
assert st.r["view"]["current"]["screen"] == "home", st.r["view"]
# the same map when Spark writes the comments in single quotes (a real run did)
doc = runlog.get(run_id, st.root)
doc["handoff"]["code"] = doc["handoff"]["code"].replace('comment("', "comment('").replace('")\n', "')\n")
assert "comment('Step 1:" in doc["handoff"]["code"]
assert [x["lines"] for x in st.handoff_code(doc)["steps"]] == [x["lines"] for x in code["steps"]]
print("HO9 ok: step, open PDF, looks right, export (share sheet, cancel, done → Home, shared, done → Home); "
      "HO8 ok: run record untouched; HO11 ok: Home shows the code by step")

# HO2. A changed run record clears the approval: back to ⑨0, said why.
doc = runlog.get(run_id, st.root)
doc["record"]["excluded"]["B2"] = "late note"
runlog.write_run(doc, st.root)
assert st.handoff_plan(run_id) is None
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

# The demo seed (samples/plan_demo.json, the volumes of protocol_demo_v2.pdf) hands off whole.
seed = RecordStore(path=tempfile.mkdtemp() + "/r.json")
seed.start_demo_run()
seed.r["done"] = [m for i, stp in enumerate(seed.r["plan"]["steps"]) for m in mark(i + 1, *stp.get("wells") or [])]
seed.r["step_index"] = len(seed.r["plan"]["steps"]) - 1
seed_a = approved_for(seed._finish_locked("finished")["record"], title="Demo protocol")
seed_steps = seed_a["steps"]
assert hand.blockers(seed_steps, seed_a["liquids"]) == [] and \
    [s.get("volume_ul") for s in seed_steps] == [100, 10, 10, 10, None], seed_steps
assert [s.get("pipette") for s in seed_steps] == ["p1000", "p50", "p50", "p50", None]
assert (seed_steps[4]["comment"], seed_steps[4]["msg"]) == (
    "Step 5: Incubate · 30 min", "Incubate at 37 °C (as run: 30 min; plan: 30 min)"), seed_steps[4]
assert [lq["color"] for lq in seed_a["liquids"]] == [hand.NEUTRAL_COLOR, *hand.LIQUID_COLORS[:3]]
assert [(lq["labware"], lq["well"], lq["volume_ul"]) for lq in seed_a["liquids"]] == [
    ("reservoir", "A1", 4110.0), ("tubes", "A1", 127.0), ("tubes", "B1", 127.0), ("tubes", "C1", 127.0)]
print("HO-seed ok: the demo protocol has every volume")

# HO10. Private runs have no hand off.
api.store = st = ran("private")
card = st.request_end_run({})[1]["card"]
c.post("/confirm", json={"card_id": card["id"], "ok": True, "session": "ho"})
assert st.r["view"]["current"]["screen"] != "final_protocol", st.r["view"]
j = c.post("/input", json={"action": {"id": "make_robot", "args": {}}, "session": "ho"}).json()
assert j["reply"] == "Hand off is off in Private mode." and st.r["view"]["current"]["screen"] != "final_protocol", j
print("HO10 ok: private runs skip hand off")

# HO4c. On ⑨0's step: a timer that ran off plan offers Keep as run / Use plan (voice and tap alike).
api.store = st = ran()
st.r["timers"][0].update({"minutes": 30, "started_at": 0.0})
st._cancel({"messages": []}, "Incubate", "voice", 18.0)  # stopped after 18 s
card = st.request_end_run({})[1]["card"]
c.post("/confirm", json={"card_id": card["id"], "ok": True, "session": "ho"})
run_id = st.r["view"]["current"]["target"]
c.post("/input", json={"action": {"id": "show_step", "args": {"step": "4"}}, "session": "ho"})
snap = c.get("/record").json()
assert snap["view"]["slots"].get("timer") == "changed", snap["view"]["slots"]
assert snap["handoff"]["summary"]["rows"][3]["run"] == "18 s"
j = c.post("/input", json={"text": "use plan", "session": "ho"}).json()
assert j["reply"] == "Step 4 uses the plan's time.", j
assert c.get("/record").json()["handoff"]["summary"]["rows"][3]["run"] == "30 min"
j = c.post("/input", json={"text": "keep as run", "session": "ho"}).json()
assert j["reply"] == "Step 4 keeps the time it ran." and \
    c.get("/record").json()["handoff"]["summary"]["rows"][3]["run"] == "18 s", j
print("HO4c ok: Keep as run / Use plan on the step")

# Live: the local simulator on the code for these steps (skipped when .venv-sim isn't installed).
if hand.SIM_PYTHON.exists():
    res = hand.simulate(code_for(approved))
    assert res["ok"], res
    chk = hand.check(res["events"], steps, approved["seconds"], approved["liquids"])
    assert chk["ok"], chk
    res = hand.simulate(code_for(seed_a))
    assert res["ok"] and hand.check(res["events"], seed_steps, seed_a["seconds"], seed_a["liquids"])["ok"], res
    assert hand.tips_used(res["events"]) == {"opentrons_flex_96_tiprack_1000ul": 21,
                                              "opentrons_flex_96_tiprack_50ul": 21}, hand.tips_used(res["events"])
    short = waited(0.3)
    a = approved_for(short)
    res = hand.simulate(code_for(a))
    assert res["ok"] and hand.check(res["events"], a["steps"], a["seconds"])["ok"], res
    print("HO-live ok: opentrons_simulate run log matches the approved steps (test plan, demo seed)")
else:
    print("HO-live skipped: .venv-sim not installed")
print("ALL PASS")
