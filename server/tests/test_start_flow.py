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
assert here()["screen"] == "main" and j["reply"] == "Media test started. Step 1 of 3.", j
print("SF3 ok: start_run")

# SF4. Later: saved to the library, the draft screens are left for Home.
api.store = fresh()
j = said("later")
assert api.store.r.get("draft") is None and here()["screen"] == "home", (here(), j)
assert "Media test" in j["reply"], j
print("SF4 ok: save_draft")

# SF5. Discard, and actions with no draft say so.
api.store = fresh()
j = act("discard_draft")
assert api.store.r.get("draft") is None and here()["screen"] != "protocol_review" and j["reply"] == "Draft discarded.", j
j = act("show_step", step=1)
assert j["reply"] == "No draft open — import a protocol from Home.", j
print("SF5 ok: discard_draft")

# SF6. With a run going, Start asks first (the finish-first card).
api.store = fresh(run=True)
j = act("start_run")
assert j["cards"] and api.store.r["run"]["protocol_name"] == "Demo protocol", j
assert j["reply"].count(j["cards"][0]["question"]) == 1, j  # asked once
print("SF6 ok: start_run with an active run asks first")

# SF7. A finished run's Start run (shown greyed): tap or words, no model ->
# "Run 1 is finished. Start a new run of <protocol>?"; yes starts one.
api.store = fresh()
act("start_run")
old = api.store.r["run"]["run_id"]
api.store._finish_locked("finished")
j = act("open", target=f"run:{old}")
v = api.store.snapshot()["view"]
assert (v["current"]["screen"], v["slots"]["status"]) == ("run_view", "finished"), v
from server.screens import buttons_for  # noqa: E402
bs = buttons_for("run_view", api.store.view_slots("run_view", old))
assert [(b["say"], b.get("look")) for b in bs] == [("Start run", "disabled"), ("Back", None)], bs
j = said("start run")
assert j["route"] == "grammar" and j["reply"] == "Run 1 is finished. Start a new run of Media test?", j
card = j["cards"][0]
assert (card["title"], card["detail"]) == ("Run 1 is finished", "Start a new run of Media test?"), card
j = act("answer_card", card_id=card["id"], ok=True)
assert api.store.r["run"]["run_id"] != old and api.store.r["run"]["protocol_name"] == "Media test", j
print("SF7 ok: a finished run offers a new run")

# SF8. With a run going, yes to that card still asks to finish it first.
api.store.r["view"]["current"] = {"screen": "run_view", "target": old}
j = said("start run")
j = act("answer_card", card_id=j["cards"][0]["id"], ok=True)
assert j["cards"] and j["cards"][0]["kind"] == "finish_first", j
print("SF8 ok: the active run still asks first")

# SF9. The active run opens its record too, with Continue / Back.
api.store.r["view"]["current"] = {"screen": "home", "target": ""}
act("open", target="run:latest")
cur = api.store.r["view"]["current"]
assert cur == {"screen": "run_view", "target": api.store.r["run"]["run_id"]}, cur
assert [b["say"] for b in buttons_for("run_view", api.store.view_slots("run_view", cur["target"]))] == \
    ["Continue", "Back"]
said("continue")
assert api.store.r["view"]["current"]["screen"] == "main"
print("SF9 ok: active run -> Continue / Back")
print("ALL PASS")
