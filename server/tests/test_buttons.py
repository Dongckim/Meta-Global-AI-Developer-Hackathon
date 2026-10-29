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
from server.screens import SCREENS, button_call  # noqa: E402
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
    api.store.r["view"]["current"] = {"screen": sid, "target": ""}
    j = c.post("/input", json={"text": b["say"], "session": "bt"}).json()
    names = [t["name"] for t in j["tool_calls"]]
    assert names == [b["action"]] and j["route"] == "grammar", (sid, b["say"], j)
print("BT2 ok: spoken button runs its action")
print("ALL PASS")
