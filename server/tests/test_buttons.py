"""Registry "Say X" buttons (screens.json `buttons`): the shown word, the
screen's commands, and what the server hears are one thing.

Run: python -m server.tests.test_buttons (runs on import).
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.screens import PLANNED, SCREENS, button_call  # noqa: E402
from server.store import RecordStore  # noqa: E402

def noid(ui):
    """ui actions without their delivery id (the app runs each id once)."""
    return [{k: v for k, v in a.items() if k != "id"} for a in ui or []]


from server import protocol as proto  # noqa: E402
from server.screens import fill_button  # noqa: E402
from server import handoff  # noqa: E402
from server import runs as runlog  # noqa: E402

everything = [(sid, b) for sid, s in SCREENS.items() for b in s.get("buttons", [])]
# tap_only: iOS system pickers we can't drive by voice ("Browse files…"); the
# rest is said == tapped, every screen, every button
TAP_ONLY = [(sid, b["say"]) for sid, b in everything if b.get("tap_only")]
buttons = [(sid, b) for sid, b in everything if not b.get("tap_only")]
assert buttons, "no registry buttons"
assert all(button_call(say, [sid]) is None for sid, say in TAP_ONLY), "tap-only buttons never answer to voice"
print(f"tap_only (not voice): {TAP_ONLY}")

# A state where every screen's buttons have their slots: an active run and an
# imported draft with one pipetting step that needs a look.
DOC = "Add 100 uL media to all wells.\nIncubate 30 min."
RAW = {"title": "", "short_title": "Media test", "liquids": [], "conditions": [
    {"id": "ctrl", "label": "Control", "abbr": "", "treatments": [], "replicates": 3, "wells": None,
     "quote": "three control wells"}], "steps": [
    {"kind": "pipette", "text": "add media", "short_title": "Add media", "quote": "Add 100 uL media to all wells.",
     "minutes": None, "temp_c": None, "caution": None,
     "pipette": {"liquid": "", "volume": None, "unit": None, "target": {"type": "all_used", "value": None},
                 "mix": None, "repeat": None}},
    {"kind": "incubate", "text": "incubate", "short_title": "", "quote": "Incubate 30 min.", "minutes": 30,
     "temp_c": None, "caution": None, "pipette": None}]}
TARGET = {"draft_step": "1", "draft_page": "1", "protocol_detail": "", "web_answer": "s1", "web_source": "s1:1"}


def fresh(sid: str, b: dict | None = None) -> RecordStore:
    """On the button's own screen, in the state it shows in (`when`)."""
    st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    st.start_demo_run()
    finished = st.r["run"]["run_id"]
    st._finish_locked("finished")
    st.start_demo_run()
    paused = st.r["run"]["run_id"]
    st._finish_locked("paused")
    st.start_demo_run()
    st.r["draft"] = proto.build_draft(RAW, proto.evidence_from_text(DOC), "d.pdf")
    st.add_search({"id": "s1", "ts": 1, "question": "Max DMSO for HeLa cells?", "query": "max DMSO HeLa",
                   "answer_short": "Up to 0.5%", "detail": "d", "status": "ok", "run_id": None, "ms": 1,
                   "sources": [{"title": "A", "url": "https://a.org", "domain": "a.org"},
                               {"title": "B", "url": "https://b.org", "domain": "b.org"}]})
    st.add_search({"id": "s2", "ts": 2, "question": "PBS shelf life?", "query": "PBS shelf life",
                   "answer_short": "", "detail": "", "status": "timeout", "run_id": None, "ms": 1, "sources": []})
    target = TARGET.get(sid, "")
    if sid == "web_answer" and ((b or {}).get("when") or {}).get("status") == "failed":
        target = "s2"
    if sid == "web_answer" and ((b or {}).get("when") or {}).get("camera") == "on":
        # a photo question waiting for its photo (the camera is up)
        st.add_search({"id": "s3", "ts": 3, "question": "what does this say?", "query": "", "kind": "photo",
                       "target": "label", "answer_short": "", "detail": "", "status": "looking", "run_id": None,
                       "ms": 0, "sources": []})
        target = "s3"
    if sid == "web_answer" and ((b or {}).get("when") or {}).get("retake") == "on":
        # a photo question with its photo in (read, answered)
        st.add_search({"id": "s4", "ts": 4, "question": "what does this say?", "query": "", "kind": "photo",
                       "target": "label", "answer_short": "DMSO", "detail": "", "status": "ok", "basis": "photo",
                       "attempt": 1, "run_id": None, "ms": 0, "sources": []})
        target = "s4"
    if sid in RecordStore.HANDOFF_SCREENS:
        # a finished run's hand off, approved, at the stage the button shows in
        target = finished + (":1" if sid in ("final_step", "final_page") else "")
        doc = runlog.get(finished, st.root)
        if ((b or {}).get("when") or {}).get("timer") == "changed":
            # the run reached its incubate step and stopped its timer early
            doc["record"]["step_index"] = 4
            doc["record"]["timer_log"] = [{"step_index": 4, "label": "Incubate", "planned_s": 1800,
                                           "duration_s": 18, "started_at": 0, "ended_at": 18,
                                           "status": "cancelled"}]
            target = f"{finished}:5"
        doc["handoff"] = {"approved": {"hash": handoff.record_hash(doc["record"]), "ts": 1, "steps": []},
                          "stage": ((b or {}).get("when") or {}).get("stage") or "passed", "attempt": 1}
        runlog.write_run(doc, st.root)
    if sid == "protocol_detail":
        target = st.r["run"]["protocol_id"]  # the protocol of the run going (Continue shows there)
    if sid == "run_view":
        status = ((b or {}).get("when") or {}).get("status")
        target = st.r["run"]["run_id"] if status == "active" else paused if status == "paused" else finished
    st.r["view"]["current"] = {"screen": sid, "target": target}  # said on its own screen
    return st


# Shown word == a screen command (templates as written); action is one voice
# can perform too; the filled word spoken calls the filled action.
for sid, b in buttons:
    cmds = [c.lower() for c in SCREENS[sid].get("commands", [])]
    assert b["say"].lower() in cmds, (sid, b["say"], "missing from commands")
    assert b["action"] in api.PERFORM_ACTIONS, (sid, b["action"], "not a performable action")
    st = fresh(sid, b)
    slots = st.view_slots(sid, st.r["view"]["current"]["target"])
    fb = fill_button(b, slots)
    assert fb is not None, (sid, b, slots, "a slot the button needs has no value")
    for said in (fb["say"], f"  {fb['say'].upper()}. ", f"{fb['say'].lower()}.", f"okay {fb['say']}",
                 f"{fb['say']}, Milo", f"um, so {fb['say']} please"):
        call = button_call(said, [sid], slots)
        assert call == {"name": b["action"], "args": fb["args"]}, (sid, said, call)
assert button_call("don't go to the next step", ["main"]) is None  # negated: not a press
assert fill_button({"say": "Step {step}", "action": "show_step"}, {}) is None  # no hole shown
print("BT1 ok: say == command == action")

# Spoken through /input on that screen -> the same call a tap sends.
c = TestClient(api.app)


async def no_network_search(client, key, question, query, session):
    return "searched", []

api._run_search = no_network_search  # the search itself is test_search's
for sid, b in buttons:
    api.store = fresh(sid, b)
    fb = fill_button(b, api.store.view_slots(sid, api.store.r["view"]["current"]["target"]))
    j = c.post("/input", json={"text": f"Okay, {fb['say'].lower()}.", "session": "bt"}).json()
    names = [t["name"] for t in j["tool_calls"]]
    if (b.get("args") or {}).get("screen") in PLANNED:
        # a planned screen: saying and tapping both answer "not ready"
        tap = c.post("/input", json={"action": {"id": b["action"], "args": b["args"]}, "session": "bt"}).json()
        assert j["reply"] == tap["reply"] == "That isn't ready yet.", (sid, b, j, tap)
        continue
    assert names == [b["action"]] and j["route"] == "grammar", (sid, fb["say"], j)
print("BT2 ok: spoken button runs its action (lead-ins and punctuation aside)")

# BT2b. The iPad cases: a run in progress -> "Continue" goes to the bench;
# a finished run -> "Start run" asks for a new run; Home -> "Continue" resumes.
for said in ("Continue", "continue.", "okay continue"):
    api.store = fresh("run_view", {"when": {"status": "active"}})
    j = c.post("/input", json={"text": said, "session": "bt"}).json()
    assert api.store.r["view"]["current"]["screen"] == "main" and j["route"] == "grammar", (said, j)
api.store = fresh("run_view", {"when": {"status": "finished"}})
before = api.store.r["run"]["run_id"]
j = c.post("/input", json={"text": "Start run", "session": "bt"}).json()
assert not j["cards"] and api.store.r["run"]["run_id"] != before and api.store.r["view"]["current"]["screen"] == "main", j
api.store = fresh("home")
j = c.post("/input", json={"text": "Continue", "session": "bt"}).json()
assert api.store.r["view"]["current"]["screen"] == "main", j
print("BT2b ok: Continue / Start run on the run screens, Continue on Home")

# BT3. A button's word is one of its action's registry verbs (one vocabulary).
from server.screens import verbs  # noqa: E402

for sid, b in buttons:
    if b.get("args"):  # a targeted button is its screen's command (BT1), not a bare verb
        continue
    assert b["say"].lower() in [v.lower() for v in verbs(b["action"])], (sid, b, verbs(b["action"]))
print("BT3 ok: button words come from action verbs")

# BT4. Every screen has a person-facing label and result line; no screen
# ever falls back to its raw id or leaves a hole.
from server.screens import result_of  # noqa: E402

for sid, s in SCREENS.items():
    assert (s.get("title") or "").strip() and (s.get("choice") or "").strip(), (sid, "no label")
    assert (s.get("result") or "").strip(), (sid, "no result line")
    line = result_of(sid, target="C5", name="the demo protocol")
    assert "_" not in line and "?" not in line and "{" not in line, (sid, line)
    assert not line.startswith("Showing "), (sid, line, "fallback: no result line")
api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
r = api.store.apply_calls([{"name": "show", "args": {"view": "well_card", "target": "C5"}}],
                          "voice", "show C5", "spark", {})
assert r["messages"] == ["Here's C5."], r["messages"]
print("BT4 ok: every screen has a label; show says it")

# BT5. Card answer buttons (registry `cards`) say a registry answer word for
# their answer, so a tap and saying the label do the same thing.
from server.grammar import match_card_answer  # noqa: E402
from server.screens import REGISTRY  # noqa: E402

for layout, bs in REGISTRY.get("cards", {}).items():
    for b in bs:
        assert b["action"] == "answer_card", (layout, b)
        assert match_card_answer(b["say"]) == b["args"]["answer"], (layout, b)
print("BT5 ok: card buttons say their answer")
# BT6. The screen's own parts, said or tapped: original pages, the file list,
# the camera, a source page. "page 2", "number 2", "the first one", a name.
from server.screens import control_call  # noqa: E402

api.store = fresh("draft_page")
for said, want in (("next page", {"cmd": "next_page"}), ("Page 2", {"cmd": "page", "n": 2}),
                   ("go to page three", {"cmd": "page", "n": 3}), ("show the quote", {"cmd": "quote"}),
                   ("zoom in", {"cmd": "zoom_in"})):
    j = c.post("/input", json={"text": said, "session": "bt"}).json()
    assert j["route"] == "grammar" and noid(j["ui"]) == [{"action": "control", "target": want["cmd"],
                                                     **({"n": want["n"]} if "n" in want else {})}], (said, j)
api.store = fresh("main")
j = c.post("/input", json={"action": {"id": "control", "args": {"cmd": "next_page"}}, "session": "bt"}).json()
assert not j.get("ui") and j["reply"] == "I can't do that here.", j  # not this screen's
api.store = fresh("import_file")
assert api.store.snapshot()["view"]["prompt"] == "Here are your files.", "no claim before the app's list"
c.post("/screen/items", json={"screen": "import_file", "items": [
    {"id": "drug_ab_dosing.pdf", "name": "drug ab dosing"}, {"id": "mtt_assay.txt", "name": "mtt assay"}]})
assert api.store.snapshot()["view"]["prompt"] == "2 files. Which one?"
for said, want in (("number 2", "mtt_assay.txt"), ("the first one", "drug_ab_dosing.pdf"),
                   ("okay the second one", "mtt_assay.txt"), ("the MTT assay", "mtt_assay.txt"),
                   ("drug dosing", "drug_ab_dosing.pdf")):
    j = c.post("/input", json={"text": said, "session": "bt"}).json()
    assert j["route"] == "grammar" and noid(j["ui"]) == [{"action": "pick", "target": want}], (said, j)
j = c.post("/input", json={"action": {"id": "pick", "args": {"n": 9}}, "session": "bt"}).json()
assert j["reply"] == "There are only 2.", j
assert control_call("browse files", "import_file", []) is None
api.store = fresh("import_photo")
j = c.post("/input", json={"text": "take it", "session": "bt"}).json()
assert noid(j["ui"]) == [{"action": "control", "target": "take"}], j
c.post("/screen/items", json={"screen": "import_photo", "items": [{"id": "p1", "name": "page 1"}]})
assert api.store.snapshot()["view"]["prompt"] == "1 page so far. Say one more, or done."
api.store = fresh("web_answer")
j = c.post("/input", json={"text": "open source", "session": "bt"}).json()
assert api.store.r["view"]["current"] == {"screen": "web_source", "target": "s1:1"}, j
v = api.store.snapshot()["view"]
assert v["prompt"] == "A, a.org." and v["slots"]["url"] == "https://a.org", v
j = c.post("/input", json={"text": "scroll down", "session": "bt"}).json()
assert noid(j["ui"]) == [{"action": "control", "target": "scroll_down"}], j
c.post("/input", json={"text": "source 2", "session": "bt"})
assert api.store.r["view"]["current"] == {"screen": "web_source", "target": "s1:2"}
c.post("/input", json={"text": "back", "session": "bt"})
assert api.store.r["view"]["current"]["screen"] == "web_answer", "one page deep"
print("BT6 ok: pages, files, camera, source page by voice and tap")
# BT7. Lead-ins anywhere, repeats folded; a sentence holding exactly one of
# the screen's buttons is that button, unless it negates or asks; two -> ask.
from server.screens import button_match  # noqa: E402

for said in ("full please full please", "um full", "let's go full", "Oh. Full."):
    call, ask = button_match(said, ["mode"])
    assert call == {"name": "navigate", "args": {"screen": "import_choice"}} and ask is None, (said, call, ask)
assert button_match("not full", ["mode"]) == (None, None)
assert button_match("full or private", ["mode"]) == (None, "Full or Private?")
assert button_match("what's the next step?", ["main"]) == (None, None), "a question isn't a press"
api.store = fresh("mode")
j = c.post("/input", json={"text": "full please full please", "session": "bt"}).json()
assert j["route"] == "grammar" and api.store.r["view"]["current"]["screen"] == "import_choice", j
api.store = fresh("mode")
j = c.post("/input", json={"text": "full or private", "session": "bt"}).json()
assert j["reply"] == "Full or Private?" and j["route"] == "grammar" and api.store.r["view"]["current"]["screen"] == "mode", j
print("BT7 ok: buttons heard inside a sentence, not when negated or asked")
# BT8. A snapshot can be made on every screen, whatever the target (odd ones
# too), with a run, a draft, searches and listed files: it never fails and it
# is strict JSON (the app's GET /record and /events).
import json  # noqa: E402

for sid in SCREENS:
    st = fresh(sid)
    st.screen_items = {"import_file": [{"id": "a.pdf", "name": "a"}], "import_photo": []}
    for tgt in ("", "junk", "s1", "s2", "s1:1", "s1:9", "1", "99"):
        st.r["view"]["current"] = {"screen": sid, "target": tgt}
        json.dumps(st.snapshot(), allow_nan=False)
print("BT8 ok: every screen's snapshot is strict JSON")
print("ALL PASS")
