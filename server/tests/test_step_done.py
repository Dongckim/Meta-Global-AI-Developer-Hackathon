"""Milo asks first when a step is done: the last target well marked raises
"Step n done. Move to step n+1?" once; a step with no target wells asks when
its timer ends; the last step asks to finish the run.

Run: python -m server.tests.test_step_done (runs on import).
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

c = TestClient(api.app)


def said(text):
    return c.post("/input", json={"text": text, "session": "sd"}).json()


def fresh() -> None:
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()


def step_cards():
    return [x for x in api.store.r["cards"].values() if x["payload"].get("op") in ("next_step", "end_run")]


# SD1. The last target well marked: the card comes once, yes moves on.
fresh()
said("B2 to B8 done")
said("C2 to C8 done")
assert not step_cards()
j = said("D2 to D8 done")
assert j["reply"].endswith("Step 1 done. Move to step 2?"), j
assert [x["question"] for x in step_cards()] == ["Step 1 done. Move to step 2?"]
said("D8 done")  # nothing left before or after: no new question
said("next step")  # said first: the same card, not a second one
assert len(step_cards()) == 1, step_cards()
said("yes")
assert api.store.r["step_index"] == 1 and not step_cards()
print("SD1 ok: all wells -> one card, yes moves on")

# SD2. "Not yet" keeps the step.
fresh()
for rows in ("B2 to B8", "C2 to C8", "D2 to D8"):
    said(f"{rows} done")
said("not yet")
assert api.store.r["step_index"] == 0 and not step_cards()
print("SD2 ok: no stays")

# SD3. The last step's wells all done: finish the run?
fresh()
api.store.r["plan"]["steps"].pop()  # the drug B step is now the last
api.store.r["step_index"] = 3
j = said("D2 to D8 done")
assert j["reply"].endswith("All steps done. Finish the run?"), j
assert [x["payload"]["op"] for x in step_cards()] == ["end_run"]
print("SD3 ok: last step -> end card")

# SD4. A step with no target wells (incubate) asks when its timer ends.
fresh()
api.store.r["step_index"] = 4
api.store.r["timers"].append({"id": "inc", "label": "incubate · 30 min", "minutes": 30, "on_done": None,
                              "started_at": time.time() - 30 * 60 - 10, "step": "incubate", "wells": "",
                              "warned": True, "notified": False})
evs = api.store.check_timers(time.time())
assert evs[-1]["message"].endswith("All steps done. Finish the run?"), evs
assert [x["payload"]["op"] for x in step_cards()] == ["end_run"]
print("SD4 ok: wells-less step -> card on timer end")

# SD5. The header chip's name comes from the server: the timer's step short
# title (no total time, no sentence); older timers fall back to a short label.
fresh()
api.store.r["step_index"] = 1
api.store.apply_calls([{"name": "set_timer", "args": {"minutes": 30}}], "voice", "x", "grammar", {})
snap = api.store.snapshot()
assert [t["chip"] for t in snap["timers"]] == [snap["plan"]["steps"][1]["short_title"]], snap["timers"]
api.store.r["timers"][0].pop("step_index")
assert len(api.store.snapshot()["timers"][0]["chip"].split()) <= 4
print("SD5 ok: timer chip names from the server")

# SD6. The header's mode label shows only after the mode was chosen
# (registry `header_mode`): the run's screens and the start flow from Import
# on; Home, a run record, web searches and Mode itself have none.
for screen, shown in (("main", True), ("well_card", True), ("import_choice", True), ("plan_check", True),
                      ("home", False), ("protocol_detail", False), ("run_view", False),
                      ("web_searches", False), ("mode", False)):
    api.store.r["view"]["current"] = {"screen": screen, "target": ""}
    m = api.store.snapshot()["header"]["mode"]
    assert (m == {"id": "full", "label": "Full"}) if shown else m is None, (screen, m)
print("SD6 ok: header mode by screen from the registry")
print("ALL PASS")
