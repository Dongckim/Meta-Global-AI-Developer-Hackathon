"""Start a run (⑤b-⑤f) as registry actions: review_draft / save_draft /
start_run / discard_draft / show_step / open_source_page run through the one
Action path (tap and voice alike), and each screen's prompt is filled from
the draft, never from example values.

Run: python -m server.tests.test_start_flow (runs on import).
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import protocol as proto  # noqa: E402
from server.store import RecordStore  # noqa: E402

DOC = "Media test\nSeed 3 control wells with cells.\nAdd 100 uL media to all wells.\nAdd PBS to control wells.\nIncubate 30 min."


def pip(liquid, volume, target, quote, title):
    return {"kind": "pipette", "text": quote, "short_title": title, "quote": quote, "minutes": None,
            "temp_c": None, "caution": None,
            "pipette": {"liquid": liquid, "volume": volume, "unit": "uL" if volume else None, "target": target,
                        "mix": None, "repeat": None}}


RAW = {"title": "Media test", "short_title": "Media test", "liquids": [
    {"id": "media", "name": "media", "concentration": None, "prep": None, "quote": "Add 100 uL media to all wells."},
    {"id": "pbs", "name": "PBS", "concentration": None, "prep": None, "quote": "Add PBS to control wells."}],
    "conditions": [{"id": "ctrl", "label": "Control", "abbr": "", "treatments": ["pbs"], "replicates": 3,
                    "wells": None, "quote": "Seed 3 control wells with cells."}],
    "steps": [pip("media", 100, {"type": "all_used", "value": None}, "Add 100 uL media to all wells.", "Add media"),
              pip("pbs", None, {"type": "condition", "value": "ctrl"}, "Add PBS to control wells.", "Add PBS"),
              {"kind": "incubate", "text": "Incubate 30 min.", "short_title": "Incubate plate",
               "quote": "Incubate 30 min.", "minutes": 30, "temp_c": None, "caution": None, "pipette": None}]}


def fresh(run: bool = False) -> RecordStore:
    st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    if run:
        st.start_demo_run()
    st.save_draft(proto.build_draft(RAW, proto.evidence_from_text(DOC), "media.pdf"))
    return st


c = TestClient(api.app)


def act(name, **args):
    return c.post("/input", json={"action": {"id": name, "args": args}, "session": "sf"}).json()


def said(text):
    return c.post("/input", json={"text": text, "session": "sf"}).json()


def here():
    return api.store.r["view"]["current"]


# SF1. The draft summary: robot rows, the rest "not for the robot", counts.
api.store = fresh()
d = api.store.snapshot()["draft"]["summary"]
assert [(r["n"], r["title"], r["detail"], r["flagged"]) for r in d["rows"]] == [
    (1, "Add media", "100 µL · All 5 wells", False), (2, "Add PBS", "All 5 wells", True)], d["rows"]
assert d["others"] == ["Incubate plate"] and (d["conditions"], d["wells"], d["steps"]) == (1, 5, 3), d
print("SF1 ok: draft rows")

# SF2. Each screen's line is its prompt with the draft's numbers.
v = api.store.snapshot()["view"]
assert here()["screen"] == "protocol_review" and v["prompt"] == "Draft ready. Review it?", v
j = said("review")
assert here()["screen"] == "draft_steps" and j["reply"] == "2 pipetting steps. Step 2 needs a look.", j
assert api.store.snapshot()["view"]["slots"]["step"] == "2"
j = said("step 2")  # the templated button "Step {step}", spoken
assert here() == {"screen": "draft_step", "target": "2"}, (here(), j)
assert j["reply"] == "Step 2 needs a look: pipette step needs a volume.", j
j = said("open pdf")
assert here() == {"screen": "draft_page", "target": "2"} and j["reply"] == "Page 1. Step 2 is highlighted.", j
said("back to step")
assert here() == {"screen": "draft_step", "target": "2"}, here()
j = said("looks right")  # back to the list, not a loop
assert here()["screen"] == "draft_steps" and [x["screen"] for x in api.store.r["view"]["stack"]][-1] == "protocol_review"
j = act("navigate", screen="plan_check")
assert j["reply"] == "Draft ready. 1 condition, 3 steps.", j
print("SF2 ok: prompts from the draft, buttons said == tapped")

# SF3. Start run: the draft goes to the library, the run starts on the bench.
j = act("start_run")
assert api.store.r.get("draft") is None and api.store.r["run"]["protocol_name"] == "Media test", j
assert here()["screen"] == "main" and j["reply"] == "Run 1 started. Step 1: add 100 µL media to B2–B6.", j
assert j["speak"] == "Run 1 started. Step 1: add 100 microliters media to B 2 to B 6.", j  # said as words
print("SF3 ok: start_run")

# SF4. Later: saved to the library, the draft screens are left for Home.
api.store = fresh()
j = said("later")
assert api.store.r.get("draft") is None and here()["screen"] == "protocol_detail", (here(), j)
assert next(x for x in api.store.protocol_summaries() if x["id"] == here()["target"])["name"] == "Media test"
assert "Media test" in j["reply"], j
print("SF4 ok: save_draft")

# SF5. Discard, and actions with no draft say so.
api.store = fresh()
j = act("discard_draft")
assert api.store.r.get("draft") is None and here()["screen"] != "protocol_review" and j["reply"] == "Draft discarded.", j
j = act("show_step", step=1)
assert j["reply"] == "No draft open — import a protocol from Home.", j
print("SF5 ok: discard_draft")

# SF6. With a run going, Start doesn't ask: that run pauses (kept as it is)
# and the new one opens on Step 1; the reply says both.
api.store = fresh(run=True)
old = api.store.r["run"]["run_id"]
api.store.r["step_index"] = 1
j = act("start_run")
assert not j["cards"] and api.store.r["run"]["protocol_name"] == "Media test" and here()["screen"] == "main", j
assert j["reply"] == "Run 1 started, Run 1 is paused. Step 1: add 100 µL media to B2–B6.", j
was = next(r for r in api.store.run_summaries(api.store.all_run_docs()) if r["run_id"] == old)
assert (was["status"], was["state"]) == ("paused", "Paused at step 2"), was
print("SF6 ok: start_run pauses the run going, no card")

# SF7. A finished run's Start run (shown greyed): tap or words, no model ->
# the protocol's next run starts at once.
from server.screens import buttons_for  # noqa: E402

new = api.store.r["run"]["run_id"]
api.store._finish_locked("finished")
j = act("open", target=f"run:{new}")
v = api.store.snapshot()["view"]
assert (v["current"]["screen"], v["slots"]["status"]) == ("run_view", "finished"), v
bs = buttons_for("run_view", api.store.view_slots("run_view", new))
assert [(b["say"], b.get("look")) for b in bs] == [("Start run", "disabled"), ("Back", None)], bs
j = said("start run")
assert j["route"] == "grammar" and not j["cards"] and api.store.r["run"]["run_id"] != new, j
assert j["reply"].startswith("Run 2 started. Step 1: "), j
print("SF7 ok: a finished run's Start run starts the next one")

# SF8. A paused run continues where it stopped; the run going pauses.
api.store.r["view"]["current"] = {"screen": "home", "target": ""}
j = act("open", target=f"run:{old}")
bs = buttons_for("run_view", api.store.view_slots("run_view", old))
assert [b["say"] for b in bs] == ["Continue", "Back"] and bs[0]["action"] == "resume_run", bs
going = api.store.r["run"]["run_id"]
j = said("continue")
assert api.store.r["run"]["run_id"] == old and api.store.r["step_index"] == 1 and here()["screen"] == "main", j
assert j["reply"] == "Run 1 picks up at step 2. Run 2 is paused.", j
assert next(r for r in api.store.run_summaries(api.store.all_run_docs()) if r["run_id"] == going)["status"] == "paused"
print("SF8 ok: paused runs continue")

# SF9. The active run opens its record too, with Continue / Back.
api.store.r["view"]["current"] = {"screen": "home", "target": ""}
act("open", target=f"run:{api.store.r['run']['run_id']}")
cur = api.store.r["view"]["current"]
assert cur == {"screen": "run_view", "target": api.store.r["run"]["run_id"]}, cur
assert [b["say"] for b in buttons_for("run_view", api.store.view_slots("run_view", cur["target"]))] == \
    ["Continue", "Back"]
said("continue")
assert api.store.r["view"]["current"]["screen"] == "main"
print("SF9 ok: active run -> Continue / Back")
# SF10. The import flow has one way in (registry `flows`): Mode, then the
# choice, then files / camera / text / the iOS picker. Asked for a later
# step, the earliest one still needed opens; Mode is remembered once passed.
api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
api._SESSIONS.clear()
here = lambda: api.store.r["view"]["current"]["screen"]  # noqa: E731
for ask in ({"id": "navigate", "args": {"screen": "import_file"}}, {"id": "open", "args": {"target": "importer"}},
            {"id": "navigate", "args": {"screen": "import_choice"}}):
    api.store.r["view"]["current"] = {"screen": "home", "target": ""}
    j = c.post("/input", json={"action": ask, "session": "sf"}).json()
    assert here() == "mode" and j["reply"] == "Full or Private?" and not j.get("ui"), (ask, j)
said("full")
assert here() == "import_choice", here()
said("file")
assert here() == "import_file", here()
said("back")
assert here() == "import_choice", "back inside the flow keeps the way in"
said("photo")
assert here() == "import_photo", here()
api.store.r["view"] = {**api.store.r["view"], "current": {"screen": "home", "target": ""}, "stack": []}
j = c.post("/input", json={"action": {"id": "navigate", "args": {"screen": "import_photo"}}, "session": "sf"}).json()
assert here() == "mode", "a new import starts at Mode again (iPad: an old Full skipped it)"
print("SF10 ok: the import flow's one way in")

# SF11. A ui action carries one id on the reply and on /events: the app runs it once.
events: list = []
api.store.subscribe(events.append)
api.store.r["view"]["current"] = {"screen": "main", "target": ""}
j = c.post("/input", json={"action": {"id": "open", "args": {"target": "C5"}}, "session": "sf"}).json()
sse = [e for e in events if e.get("kind") == "ui"]
assert j["ui"] and [a["id"] for a in j["ui"]] == [e["id"] for e in sse], (j["ui"], sse)
print("SF11 ok: one id per ui action")
# SF12. The app checks its event stream by the reply's record_version: every
# reply's version is a snapshot /events delivered (iPad: File tapped 12 times,
# the view changed each time, the app never rendered: a dead stream).
events.clear()
api.store.r["view"]["current"] = {"screen": "import_choice", "target": ""}
api.store.r["view"]["stack"] = [{"screen": "mode", "target": ""}]
for _ in range(3):
    j = c.post("/input", json={"text": "file", "session": "sf"}).json()
    snaps = [e for e in events if e.get("kind") == "snapshot"]
    assert snaps and snaps[-1]["version"] == j["record_version"], ([e["version"] for e in snaps], j["record_version"])
    assert snaps[-1]["view"]["current"]["screen"] == "import_file", snaps[-1]["view"]
    c.post("/input", json={"text": "back", "session": "sf"})
print("SF12 ok: every reply's version arrives as a snapshot")
# SF13. Start run after an import (Home -> Mode -> choice -> files -> draft
# screens) closes the whole flow and opens the new run's bench at once, the
# run that was going paused.
api.store = fresh(run=True)
api.store.r["view"] = {**api.store.r["view"], "current": {"screen": "plan_check", "target": ""},
                       "stack": [{"screen": "home", "target": ""}, {"screen": "mode", "target": ""},
                                 {"screen": "import_choice", "target": ""}, {"screen": "import_file", "target": ""},
                                 {"screen": "protocol_review", "target": ""}, {"screen": "draft_steps", "target": ""}]}
j = act("start_run")
assert not j["cards"] and api.store.r["run"]["protocol_name"] == "Media test", j
assert api.store.r["view"]["current"]["screen"] == "main" and api.store.r["view"]["stack"] == [], api.store.r["view"]
print("SF13 ok: start run closes the import flow")

# SF14. Again by voice, no model: a saved protocol picked on Home -> Start
# run is there and on; "run Media test again" / "start a new run of the media
# one" add the next run under it; a draft waiting shows Review on Home.
from server.screens import buttons_for  # noqa: E402

api.store = fresh()
said("later")  # saved: Media test is the protocol on screen
cur = lambda: api.store.r["view"]["current"]  # noqa: E731
pid = cur()["target"]
assert cur()["screen"] == "protocol_detail"
says = [b["say"] for b in buttons_for("protocol_detail", api.store.view_slots("protocol_detail", pid))]
assert says == ["Start run", "Import protocol", "Searches"], says
j = said("start run")
assert api.store.r["run"]["protocol_id"] == pid and cur()["screen"] == "main", j
api.store._finish_locked("finished")
api.store.r["view"] = {**api.store.r["view"], "current": {"screen": "home", "target": ""}, "stack": []}
for phrase in ("run Media test again", "Start a new run of the media one"):
    j = said(phrase)
    assert j["route"] == "grammar" and api.store.r["run"]["protocol_id"] == pid, (phrase, j)
    api.store._finish_locked("finished")
    api.store.r["view"] = {**api.store.r["view"], "current": {"screen": "home", "target": ""}, "stack": []}
nums = [r["label"] for r in api.store.run_summaries(api.store.all_run_docs()) if r["protocol_id"] == pid]
assert nums == ["Run 3", "Run 2", "Run 1"], nums
j = said("start a run")  # Home, nothing picked: which one
assert "Which protocol" in j["reply"] and not api.store.r.get("run"), j
api.store.save_draft(proto.build_draft(RAW, proto.evidence_from_text(DOC + " v2"), "media2.pdf"))
api.store.r["view"] = {**api.store.r["view"], "current": {"screen": "home", "target": ""}, "stack": []}
says = [b["say"] for b in buttons_for("home", api.store.view_slots("home", ""))]
assert "Review" in says, says
j = said("review")
assert cur()["screen"] == "draft_steps", j
print("SF14 ok: start again by voice, runs stack under one protocol, Review on Home")

# SF15. The same pasted text, spaced differently, is the same original.
from server import library as lib  # noqa: E402

assert lib.source_hash([], "Add 100 uL\n\n media.  ") == lib.source_hash([], "Add 100 uL media.")
assert lib.source_hash([b"a"]) != lib.source_hash([b"b"])
print("SF15 ok: same original, same hash")
print("ALL PASS")
