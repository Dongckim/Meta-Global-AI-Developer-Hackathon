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

from server import protocol as proto  # noqa: E402
from server.screens import fill_button  # noqa: E402

buttons = [(sid, b) for sid, s in SCREENS.items() for b in s.get("buttons", [])]
assert buttons, "no registry buttons"

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
TARGET = {"draft_step": "1", "draft_page": "1", "protocol_detail": ""}


def fresh(sid: str, b: dict | None = None) -> RecordStore:
    """On the button's own screen, in the state it shows in (`when`)."""
    st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    st.start_demo_run()
    finished = st.r["run"]["run_id"]
    st._finish_locked("finished")
    st.start_demo_run()
    st.r["draft"] = proto.build_draft(RAW, proto.evidence_from_text(DOC), "d.pdf")
    target = TARGET.get(sid, "")
    if sid == "run_view":
        target = st.r["run"]["run_id"] if ((b or {}).get("when") or {}).get("status") == "active" else finished
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
    call = button_call(f"  {fb['say'].upper()}. ", [sid], slots)
    assert call == {"name": b["action"], "args": fb["args"]}, (sid, call)
assert button_call("next step please", ["main"]) is None  # exact phrase only
assert fill_button({"say": "Step {step}", "action": "show_step"}, {}) is None  # no hole shown
print("BT1 ok: say == command == action")

# Spoken through /input on that screen -> the same call a tap sends.
c = TestClient(api.app)
for sid, b in buttons:
    api.store = fresh(sid, b)
    fb = fill_button(b, api.store.view_slots(sid, api.store.r["view"]["current"]["target"]))
    j = c.post("/input", json={"text": fb["say"], "session": "bt"}).json()
    names = [t["name"] for t in j["tool_calls"]]
    if (b.get("args") or {}).get("screen") in PLANNED:
        # a planned screen: saying and tapping both answer "not ready"
        tap = c.post("/input", json={"action": {"id": b["action"], "args": b["args"]}, "session": "bt"}).json()
        assert j["reply"] == tap["reply"] == "That isn't ready yet.", (sid, b, j, tap)
        continue
    assert names == [b["action"]] and j["route"] == "grammar", (sid, fb["say"], j)
print("BT2 ok: spoken button runs its action")

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
print("ALL PASS")
