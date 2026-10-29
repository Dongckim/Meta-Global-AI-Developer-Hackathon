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
assert lab["aaaa"]["display"] == "Run 2" and lab["aaaa"]["spoken"] == "the active run", lab["aaaa"]
assert (lab["bbbb"]["display"], lab["bbbb"]["spoken"], lab["bbbb"]["day"]) == \
    ("Run 1", "Drug A/B Dosing run 1", "Today"), lab["bbbb"]
assert lab["cccc"]["spoken"] == "Demo run 1", lab["cccc"]
assert (lab["dddd"]["spoken"], lab["dddd"]["day"]) == ("ELISA run 1", "Oct 27"), lab["dddd"]
assert lab["eeee"]["spoken"] == "ELISA run 2", lab["eeee"]
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
assert L.which_one(["the active run", "Drug A/B Dosing run 1"]) == \
    "The active run or Drug A/B Dosing run 1?"
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
assert "Run 1. Ended at step 1." in said, said  # the run record's line: "Run N. <state>."
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

# LB7. Cards carry their own title / detail (pip_voice.json `cards`), notes
# their source, conditions their plate letters; the app never splits text.
st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
st.r["step_index"] = 2
sess = {}
r = st.apply_calls([{"name": "mark_done", "args": {"wells": ["A1"]}}], "voice", "A1 done", "grammar", sess)
card = r["cards"][0]
assert (card["title"], card["detail"]) == ("A1 isn't in this plan", "Record anyway?"), card
assert card["question"] != card["title"], "the spoken question stays its own sentence"
r = st.apply_calls([{"name": "exclude_well", "args": {"well": "C3", "reason": "bubble"}}], "voice", "x", "grammar", sess)
assert (r["cards"][0]["title"], r["cards"][0]["detail"]) == ("Exclude C3?", "Drug A · bubble"), r["cards"][0]
st.apply_calls([{"name": "add_note", "args": {"target": "C5", "text": "cloudy"}}], "voice", "x", "grammar", sess)
st.apply_calls([{"name": "add_note", "args": {"target": "C6", "text": "ok"}}], "tap", "x", "tap", sess)
assert [n["source"] for n in st.r["notes"][-2:]] == ["voice", "tap"], st.r["notes"][-2:]
abbrs = {k: c["abbr"] for k, c in st.snapshot()["plan"]["conditions"].items()}
assert abbrs == {"ctrl": "Ct", "drug_A": "A", "drug_B": "B"}, abbrs
assert L.condition_abbrs({"a": {"label": "Low"}, "b": {"label": "Light"}}) == {"a": "Lo", "b": "Li"}
print("LB7 ok: card title/detail, note source, plate letters")

# LB8. Runs are numbered inside their protocol; the step reached is a label
# fact; "run 2" resolves inside the named (else the context) protocol.
for q, ctx, want in [("run 1", None, None), ("open run 2 of ELISA", None, "eeee"),
                     ("the latest run of Drug A/B", None, "aaaa"), ("run 1", "ELISA", "dddd")]:
    d, c = L.select_run(q, docs, short, "run-20261029-142000-aaaa", now,
                        context=ctx and L.shorten_title(ctx))
    assert (d and d["run_id"].endswith(want)) if want else (d is None and len(c) == 2), (q, d, c)
plan5 = {"steps": [{}] * 5}
prog = lambda status, i: L.run_progress({"status": status, "record": {"plan": plan5, "step_index": i}})  # noqa: E731
assert prog("active", 3)["state"] == "Step 4 of 5"
assert prog("finished", 2)["state"] == "Ended at step 3"
assert prog("finished", 4)["state"] == "Finished"
st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
st._finish_locked("finished")
st.start_demo_run()
st.r["step_index"] = 1
c = TestClient(api.app)
api.store = st
rs = c.get("/runs").json()["runs"]
assert [(r["label"], r["state"]) for r in rs] == [("Run 2", "Step 2 of 5"), ("Run 1", "Ended at step 1")], rs
p = c.get("/protocols").json()["protocols"][0]
assert (p["runs"], p["in_progress"], p["wells"]) == (2, 1, 21), p
# most recently used first: a newer import, until an older protocol runs again
newer = lib.save({"conditions": {}, "steps": [{"kind": "wait", "text": "x", "wells": []}], "backups": []},
                 "Never run", "n.txt", st.root)
assert [x["id"] for x in c.get("/protocols").json()["protocols"]][:2] == [newer["id"], p["id"]]
st._finish_locked("finished")
st.start_demo_run()
assert c.get("/protocols").json()["protocols"][0]["id"] == p["id"]
j = c.post("/input", json={"action": {"id": "open", "args": {"target": "run 1"}}, "session": "lb"}).json()
assert j["reply"] == "Run 1. Ended at step 1." and st.r["view"]["current"]["screen"] == "run_view", j
print("LB8 ok: run numbers, step reached, run N by voice")
print("ALL PASS")
