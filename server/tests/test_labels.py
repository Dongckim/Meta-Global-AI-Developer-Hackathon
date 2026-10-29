"""Human labels (server/labels.py): short titles, display/spoken forms,
spoken selectors, and no internal ids in anything a person reads or hears.

Run: python -m server.tests.test_labels (runs on import).
"""

import datetime as dt
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import labels as L  # noqa: E402
from server import library as lib  # noqa: E402
from server.store import RecordStore  # noqa: E402

RUN_ID = re.compile(r"run-\d{8}-\d{6}-[0-9a-f]{4}")
HASH = re.compile(r"\b[0-9a-f]{16}\b")


def no_ids(text: str) -> bool:
    return not RUN_ID.search(text or "") and not HASH.search(text or "")


# Short titles: model's first, else a rule over the title.
assert L.shorten_title("Drug A / Drug B Dosing in a 96-well Plate") == "Drug A/B Dosing"
assert L.shorten_title("Demo protocol") == "Demo"
assert L.shorten_title("Protocol: Cell Viability Assay (MTT) for Adherent Cells") == "Cell Viability Assay"
assert L.protocol_short({"short_title": "MTT Viability", "name": "x"}) == "MTT Viability"
print("LB1 ok: short titles")

# Display and spoken forms, relative to "now".
now = dt.datetime(2026, 10, 29, 15, 0)
ts = lambda *a: dt.datetime(*a).timestamp()  # noqa: E731
docs = [
    {"run_id": "run-20261029-142000-aaaa", "started_at": ts(2026, 10, 29, 14, 20), "pn": "Drug A / Drug B Dosing"},
    {"run_id": "run-20261029-090500-bbbb", "started_at": ts(2026, 10, 29, 9, 5), "pn": "Drug A / Drug B Dosing"},
    {"run_id": "run-20261028-160000-cccc", "started_at": ts(2026, 10, 28, 16, 0), "pn": "Demo protocol"},
    {"run_id": "run-20261027-110000-dddd", "started_at": ts(2026, 10, 27, 11, 0), "pn": "ELISA"},
    {"run_id": "run-20261029-120000-eeee", "started_at": ts(2026, 10, 29, 12, 0), "pn": "ELISA"},
]
short = lambda d: L.shorten_title(d["pn"])  # noqa: E731
lab = {d["run_id"][-4:]: L.run_label(d, docs, short, "run-20261029-142000-aaaa", now) for d in docs}
assert lab["aaaa"] == {"display": "Oct 29 · 2:20 PM · Drug A/B Dosing", "spoken": "the active run"}, lab["aaaa"]
assert lab["bbbb"]["spoken"] == "the first Drug A/B Dosing run today", lab["bbbb"]
assert lab["cccc"]["spoken"] == "yesterday's Demo run", lab["cccc"]
assert lab["dddd"]["spoken"] == "the Oct 27 ELISA run", lab["dddd"]
assert lab["eeee"]["spoken"] == "today's 12:00 PM ELISA run", lab["eeee"]
assert all(no_ids(v["display"]) and no_ids(v["spoken"]) for v in lab.values())
print("LB2 ok: run labels")

# Spoken selectors resolve from label facts; ambiguous -> two candidates.
pick = lambda q: L.select_run(q, docs, short, "run-20261029-142000-aaaa", now)  # noqa: E731
for q, want in [("yesterday's run", "cccc"), ("the 2 PM run", "aaaa"), ("the latest run", "aaaa"),
                ("the active run", "aaaa"), ("the 9:05 am run", "bbbb"), ("oct 27", "dddd"),
                ("the first Drug A/B run today", "bbbb"), ("the ELISA run from today", "eeee")]:
    d, c = pick(q)
    assert d and d["run_id"].endswith(want), (q, d, c)
d, c = pick("the Drug A/B run")
assert d is None and [x["run_id"][-4:] for x in c] == ["aaaa", "bbbb"], (d, c)
assert L.which_one(["the active run", "the first Drug A/B Dosing run today"]) == \
    "The active run or the first Drug A/B Dosing run today?"
assert pick("2 runs") == (None, [])  # a bare number is not a time
print("LB3 ok: spoken selectors")

# Library: labels on summaries; re-import keeps the first import time.
root = Path(tempfile.mkdtemp())
plan = {"conditions": {}, "steps": [{"kind": "wait", "text": "w", "wells": []}], "backups": []}
first = lib.save(plan, "Drug A / Drug B Dosing in a 96-well Plate", "a.pdf", root, short_title="Drug A/B Dosing")
again = lib.save(plan, "Renamed", "b.pdf", root, short_title="Other")
assert again["imported_at"] == first["imported_at"] and again["short_title"] == "Drug A/B Dosing"
s = lib.summarize(first)
assert s["spoken"] == "the Drug A/B Dosing protocol" and s["label"].endswith("· Drug A/B Dosing"), s
assert no_ids(s["label"]) and no_ids(s["spoken"])
print("LB4 ok: protocol labels")

# End to end: nothing a person reads or hears carries an internal id.
api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
old = api.store.r["run"]["run_id"]
api.store._finish_locked("finished")
api.store.start_demo_run()
c = TestClient(api.app)
said = []
for target in (f"run:{old}", "run:latest"):
    j = c.post("/input", json={"action": {"id": "open", "args": {"target": target}}, "session": "lb"}).json()
    said += [j.get("reply", ""), *j.get("messages", [])]
runs = c.get("/runs").json()["runs"]
said += [r["label"] for r in runs] + [r["spoken"] for r in runs]
read = api.store.read_record("runs", "", 10)
said += api._render_record("runs", read)
said += [api.store.snapshot()["run"]["label"]]
said += [p["label"] for p in c.get("/protocols").json()["protocols"]]
assert all(no_ids(x) for x in said), [x for x in said if not no_ids(x)]
assert any("run" in x and "Here's" in x for x in said), said
print("LB5 ok: no internal ids in user text")

# LB6. Step screen titles: the extracted short_title, else one rule; every
# snapshot step carries one (old plans included).
assert L.step_short_title({"short_title": "Add Drug B now please", "text": "x"}) == "Add Drug B now"
assert L.step_short_title({"kind": "pipette", "text": "drug B D2–D8",
                           "pipette": {"liquid": "Drug B"}}) == "Add Drug B"
assert L.step_short_title({"kind": "incubate",
                           "text": "Incubate the plate for 30 min at 37 °C"}) == "Incubate the plate"
st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
titles = [s["short_title"] for s in st.snapshot()["plan"]["steps"]]
assert all(t and len(t.split()) <= 4 for t in titles), titles
assert "short_title" not in st.r["plan"]["steps"][0], "rule fills the snapshot, never the record"
print("LB6 ok: every step has a short screen title")
print("ALL PASS")
