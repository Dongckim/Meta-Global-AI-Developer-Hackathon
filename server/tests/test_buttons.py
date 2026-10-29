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

buttons = [(sid, b) for sid, s in SCREENS.items() for b in s.get("buttons", [])]
assert buttons, "no registry buttons"

# Shown word == a screen command; action is one voice can perform too.
for sid, b in buttons:
    cmds = [c.lower() for c in SCREENS[sid].get("commands", [])]
    assert b["say"].lower() in cmds, (sid, b["say"], "missing from commands")
    assert b["action"] in api.PERFORM_ACTIONS, (sid, b["action"], "not a performable action")
    call = button_call(f"  {b['say'].upper()}. ", [sid])
    assert call == {"name": b["action"], "args": b.get("args") or {}}, (sid, call)
assert button_call("next step please", ["main"]) is None  # exact phrase only
print("BT1 ok: say == command == action")

# Spoken through /input on that screen -> the same call a tap sends.
api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
c = TestClient(api.app)
for sid, b in buttons:
    api.store.r["view"]["current"] = {"screen": sid, "target": ""}  # said on its own screen
    j = c.post("/input", json={"text": b["say"], "session": "bt"}).json()
    names = [t["name"] for t in j["tool_calls"]]
    if (b.get("args") or {}).get("screen") in PLANNED:
        # a planned screen: saying and tapping both answer "not ready"
        tap = c.post("/input", json={"action": {"id": b["action"], "args": b["args"]}, "session": "bt"}).json()
        assert j["reply"] == tap["reply"] == "That isn't ready yet.", (sid, b, j, tap)
        continue
    assert names == [b["action"]] and j["route"] == "grammar", (sid, b["say"], j)
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
