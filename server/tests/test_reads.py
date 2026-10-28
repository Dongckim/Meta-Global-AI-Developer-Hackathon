"""Reads registry: UI-displayable kinds = read_record kinds, one source.

screens.json `reads` owns the kind list; store.READ_KINDS, the tool enum,
and the speechless renderer all derive from it. Any drift fails here.

Run: python -m unittest server.tests.test_reads (runs on import).
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

import server.api as api  # noqa: E402
from server import runs as runlog  # noqa: E402
from server import library as lib  # noqa: E402
from server import screens  # noqa: E402
from server.store import RecordStore  # noqa: E402

store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
store.start_demo_run()
plan = store.r["plan"]
lib.save({"conditions": plan.get("conditions", {}),
            "steps": [{"kind": "pipette", "text": "evening seed step"}]},
         name="Evening assay", source_filename="evening.pdf", root=store.root)
past = {"run_id": "run-20240102-030405-ab12", "protocol_id": "p1",
        "protocol_name": "Demo protocol",
        "started_at": time.time() - 86400, "ended_at": time.time() - 86000,
        "status": "finished", "summary": runlog.summarize_record(store.r),
        "record": {"plan": plan, "done": ["C2"], "excluded": {}, "redo": {},
                   "notes": [], "timers": [], "recent": [], "step_index": 1}}
runlog.write_run(past, store.root)

# R1. one source: registry == store kinds == tool enum; screens exist.
want = [r["kind"] for r in screens.READS]
assert list(store.READ_KINDS) == want, (store.READ_KINDS, want)
rr = next(t for t in api.TOOLS if t.get("name") == "read_record")
assert rr["parameters"]["properties"]["kind"]["enum"] == want, rr
for r in screens.READS:
    for s in r["screens"]:
        assert s in screens.SCREENS, (r["kind"], s)
print("R1 ok: reads registry is the single source")

# R2. every kind reads without error on a seeded store.
for kind in want:
    f = "" if kind != "free_wells" else "C3"
    d = store.read_record(kind, f)
    assert "error" not in d, (kind, d)
    if kind == "source":
        assert "step" in d, d
    elif kind == "free_wells":
        assert isinstance(d.get("free"), list), d
    else:
        assert isinstance(d.get(kind), list), (kind, d)
print("R2 ok: every kind reads clean")

# R3. protocols filter by name; miss reads empty, never errors.
hit = store.read_record("protocols", "evening")
assert [p["name"] for p in hit["protocols"]] == ["Evening assay"], hit
miss = store.read_record("protocols", "no such protocol")
assert miss == {"protocols": []}, miss
print("R3 ok: protocol name filter")

# R4. runs newest-first with the active run on top; selectors resolve.
all_runs = store.read_record("runs")["runs"]
assert all_runs[0]["status"] == "active", all_runs
assert any(r["run_id"] == past["run_id"] for r in all_runs), all_runs
latest = store.read_record("runs", "latest")["runs"]
assert [r["status"] for r in latest] == ["active"], latest
yday = store.read_record("runs", "yesterday")["runs"]
assert [r["run_id"] for r in yday] == [past["run_id"]], yday
none = store.read_record("runs", "2020-01-01")["runs"]
assert none == [], none
print("R4 ok: run list and selectors")

# R5. the speechless renderer covers every kind.
samples = {
    "wells": {"wells": [{"well": "C2", "status": "done", "group": "g",
                            "memos": [], "reason": "", "redo_note": ""}]},
    "notes": {"notes": [{"target": "C2", "text": "x"}]},
    "timers": {"timers": [{"label": "L", "minutes_left": 1}]},
    "steps": {"steps": [{"i": 1, "name": "s", "current": True}]},
    "history": {"history": [{"action": "a", "wells": [], "ago": "1m"}]},
    "source": {"step": 1, "quote": "q", "quote_page": 2},
    "free_wells": {"near": "C3", "free": ["C4"]},
    "protocols": {"protocols": [{"name": "P", "steps": 5}]},
    "runs": {"runs": [{"run_id": "r", "protocol_name": "P", "status": "active"}]},
}
for kind in want:
    lines = api._render_record(kind, samples[kind])
    assert lines and all(isinstance(x, str) and x for x in lines), (kind, lines)
assert api._render_record("nope", {}) == []
print("R5 ok: renderer covers every kind")

print("ALL PASS")
