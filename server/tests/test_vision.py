"""Wells the glasses saw, on a Bench run (fake events, no SAM): late wells land on their step,
people's marks win, off-plan is asked, not sure stays a hint, Catch clears, Undo and "No, not C2",
Private and a dropped stream, and the lines Milo gets.

Run: python -m server.tests.test_vision (runs on import).
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import lab_sam_live as live_mod  # noqa: E402
from server import runs  # noqa: E402
from server import sam_wells as sw  # noqa: E402
from server.live_session import live_keywords  # noqa: E402
from server.store import RecordStore  # noqa: E402
from server.tone import set_run_mode  # noqa: E402

c = TestClient(api.app)
PLAN = {"conditions": {"a": {"label": "A", "wells": ["C2", "C3", "C4", "C5", "D2"], "replicates": 5}},
        "steps": [{"text": "Add A", "kind": "pipette", "wells": ["C2", "C3", "C4", "C5"]},
                  {"text": "Add B", "kind": "pipette", "wells": ["C5", "D2"]},
                  {"text": "Add C", "kind": "pipette", "wells": ["D2"]}],
        "backups": []}


def fresh() -> RecordStore:
    set_run_mode("full")
    s = api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    s._begin_locked(json.loads(json.dumps(PLAN)), "p1", "Test")
    s.r["step_log"][0]["ts"] = time.time() - 1000
    return s


def run_id(s):
    return s.r["run"]["run_id"]


def sees(s, *wells, ts=None, confidence="sure", usable=True):
    s.vision_events(run_id(s), [{"well": w, "ts": ts or time.time(), "confidence": confidence, "usable": usable} for w in wells])


def said(text):
    return c.post("/input", json={"text": text, "session": "v"}).json()


def next_step(s):
    s.apply_calls([{"name": "next_step", "args": {}}], "voice", "", "grammar", {})
    card = next(c for c in s.r["cards"].values() if c["kind"] in ("next_step", "unmarked"))
    return card


def go_next(s):
    with s._lock:
        s._apply_call({"name": "next_step", "args": {}}, "voice", "", "t", {}, {"messages": [], "cards": [], "ui": []}, force=True)


def marks(s):
    return {(m["step"], m["well"]): m["source"] for m in runs.done_marks(s.r)}


def lines(s):
    return [n["message"] for n in s.snapshot()["vision"]["notices"]]


def outcomes(s):
    return [e["outcome"] for e in s.r["vision"]["log"]]


# Step times, and which step a moment belongs to.
s = fresh()
t0 = s.r["step_log"][0]["ts"]
go_next(s)
assert [e["step"] for e in s.r["step_log"]] == [1, 2]
assert runs.step_at(s.r, t0 + 1) == 1 and runs.step_at(s.r, time.time() + 1) == 2 and runs.step_at(s.r, t0 - 5) == 1
assert runs.step_at({"step_index": 2}, 5) == 3  # an older run has no log
print("V1 ok: step times")

# A sure target: through mark_done as vision, a line, the eye.
s = fresh()
sees(s, "C2", "C3", "C4")
assert marks(s) == {(1, w): "vision" for w in ("C2", "C3", "C4")}
spoken = [n["speak"] for n in s.snapshot()["vision"]["notices"]]
assert spoken == ["C 2 to C 4 done."], spoken  # spelled for TTS
b = s.snapshot()["bench"]
assert b["vision"]["seen"] == ["C2", "C3", "C4"] and b["vision"]["logged_by"] == {w: "vision" for w in ("C2", "C3", "C4")}
assert b["wells"]["C2"] == "done"
sees(s, "C5")  # the step's last well offers the next step
assert any(c["kind"] == "next_step" for c in s.r["cards"].values())
sees(s, "C5")  # the same stay again: nothing new
assert len(lines(s)) == 2 and outcomes(s)[-1] == "repeat"
print("V2 ok: sure targets are logged, one line a chunk")

# Late: the stay was on a step already passed.
s = fresh()
go_next(s)
sees(s, "C2", ts=s.r["step_log"][1]["ts"] - 5)
assert marks(s) == {(1, "C2"): "vision"} and lines(s) == ["Step 1: C2 done."], lines(s)
print("V3 ok: a late well lands on its step")

# A person's mark of the same well stands, in silence, with the eye.
s = fresh()
s.apply_calls([{"name": "mark_done", "args": {"wells": ["C2"]}}], "voice", "", "grammar", {})
sees(s, "C2")
assert marks(s) == {(1, "C2"): "voice"} and lines(s) == [] and s.snapshot()["bench"]["vision"]["seen"] == ["C2"]
assert s.snapshot()["bench"]["vision"]["logged_by"] == {} and outcomes(s) == ["agrees"]
print("V4 ok: voice wins the same well")

# A person says C2 and the camera reads C3 for that moment: the person's stands, nothing added.
s = fresh()
s.apply_calls([{"name": "mark_done", "args": {"wells": ["C2"]}}], "voice", "", "grammar", {})
sees(s, "C3")
assert marks(s) == {(1, "C2"): "voice"} and lines(s) == [] and outcomes(s) == ["person_differs"]
sees(s, "C4", ts=time.time() + 60)  # a minute on it's another fill
assert (1, "C4") in marks(s)
print("V5 ok: voice wins another well at the same moment")

# Off plan and excluded: asked on the current step, at the end on a passed one.
s = fresh()
sees(s, "H9")
assert marks(s) == {} and [c["kind"] for c in s.r["cards"].values()] == ["catch"]
assert s.snapshot()["vision"]["notices"][0]["kind"] == "ask"
print("V6 ok: off plan is asked")
s = fresh()
s.apply_calls([{"name": "exclude_well", "args": {"well": "C3", "reason": "bubble"}}], "voice", "", "grammar", {})
for cid in list(s.r["cards"]):
    s.confirm(cid, True, "voice", {})
sees(s, "C3")
assert (1, "C3") not in marks(s) and any(c["kind"] == "catch" for c in s.r["cards"].values())
print("V7 ok: an excluded well is asked")
s = fresh()
go_next(s)
sees(s, "H9", ts=s.r["step_log"][1]["ts"] - 5)
assert not s.r["cards"] and outcomes(s) == ["off_plan_review"]
s.apply_calls([{"name": "end_run", "args": {}}], "voice", "", "grammar", {})
assert [c["kind"] for c in s.r["cards"].values()] == ["catch"]
cid = next(iter(s.r["cards"]))
assert s.r["cards"][cid]["payload"]["step"] == 1
s.confirm(cid, True, "voice", {})
assert (1, "H9") in marks(s)
s.apply_calls([{"name": "end_run", "args": {}}], "voice", "", "grammar", {})
assert any(c["kind"] == "end_run" for c in s.r["cards"].values())
print("V8 ok: off plan on a passed step waits for the run's end")

# Not sure: asked on a well the step wants, a hint on any other. Sure later settles it.
s = fresh()
sees(s, "C4", confidence="not_sure")
sees(s, "H9", confidence="not_sure")
assert marks(s) == {} and [c["kind"] for c in s.r["cards"].values()] == ["vision_check"]
assert lines(s) == ["Did C4 go in?"], lines(s)
assert s.snapshot()["bench"]["vision"]["hints"] == ["C4", "H9"]
sees(s, "C4")
assert marks(s) == {(1, "C4"): "vision"} and not s.r["cards"]
assert s.snapshot()["bench"]["vision"]["hints"] == ["H9"]  # logged since
print("V9 ok: not sure asks")

# Yes logs it and shows the eye; no drops it, and the camera won't bring it up again.
s = fresh()
sees(s, "C4", "C5", confidence="not_sure")
assert [c["question"] for c in s.r["cards"].values()] == ["Did C4 go in?"], s.r["cards"]  # one at a time, oldest first
sees(s, "C3", confidence="not_sure")
assert lines(s) == ["Did C4 go in?"]
said("yes")
assert marks(s).get((1, "C4")) and [c["question"] for c in s.r["cards"].values()] == ["Did C5 go in?"], s.r["cards"]
said("yes")
assert [c["question"] for c in s.r["cards"].values()] == ["Did C3 go in?"], s.r["cards"]
said("yes")
assert marks(s).get((1, "C5")) and marks(s).get((1, "C3")) and not s.r["cards"]
assert s.snapshot()["bench"]["vision"]["seen"] == ["C3", "C4", "C5"]
assert lines(s) == ["Did C4 go in?", "Did C5 go in?", "Did C3 go in?"], lines(s)
s = fresh()
sees(s, "C4", "C5", confidence="not_sure")
sees(s, "C5")  # logged while it waits its turn: never asked
said("no")
assert not s.r["cards"] and lines(s) == ["Did C4 go in?", "C5 done."], lines(s)
s = fresh()
sees(s, "C4", confidence="not_sure")
said("no")
assert marks(s) == {} and not s.r["cards"] and s.snapshot()["bench"]["vision"]["hints"] == []
sees(s, "C4", confidence="not_sure")
sees(s, "C4")
assert marks(s) == {} and not s.r["cards"]
print("V23 ok: the person settles a not-sure well")

# Catch clears when the camera logs the well after all, asked or already skipped.
s = fresh()
s.apply_calls([{"name": "next_step", "args": {}}], "voice", "", "grammar", {})
card = next(c for c in s.r["cards"].values() if c["kind"] == "unmarked")
assert card["payload"]["unmarked"] == ["C2", "C3", "C4", "C5"]
sees(s, "C2", "C3", ts=time.time() - 1)
assert card["payload"]["unmarked"] == ["C4", "C5"]
sees(s, "C4", "C5", ts=time.time() - 1)
assert not any(c["kind"] == "unmarked" for c in s.r["cards"].values())
print("V10 ok: the step-end Catch clears")
s = fresh()
s.apply_calls([{"name": "next_step", "args": {}}], "voice", "", "grammar", {})
cid = next(k for k, c in s.r["cards"].items() if c["kind"] == "unmarked")
s.confirm(cid, True, "voice", {}, "skip")
assert s.snapshot()["bench"]["wells"]["C2"] == "skipped" or s.step_wells() == ["C5", "D2"]
sees(s, "C2", ts=s.r["step_log"][-1]["ts"] - 2)
assert not any(m["well"] == "C2" for m in s.r.get("skipped") or []) and (1, "C2") in marks(s)
print("V11 ok: a skipped well clears too")

# Undo: the last batch. "No, not C2": that well, and it stays off. A person's mark stays.
s = api.store = fresh()
sees(s, "C2", "C3")
sees(s, "C4")
assert "C4" in said("Undo")["reply"] and (1, "C4") not in marks(s) and (1, "C2") in marks(s)
assert "C2" in said("No, not C2")["reply"] and (1, "C2") not in marks(s) and (1, "C3") in marks(s)
sees(s, "C2")
assert (1, "C2") not in marks(s) and outcomes(s)[-1] == "rejected_before"
s.apply_calls([{"name": "mark_done", "args": {"wells": ["C5"]}}], "voice", "", "grammar", {})
said("No, not C5")
assert marks(s)[(1, "C5")] == "voice"
assert said("Undo") and said("Undo")["reply"] == "Nothing from the camera to undo."
s = api.store = fresh()
sees(s, "C2", "C3")
s.apply_calls([{"name": "next_step", "args": {}}], "voice", "", "grammar", {})  # a card is up
assert any(c["kind"] == "unmarked" for c in s.r["cards"].values())
said("No, not C2")  # a well named: the camera's mark, and the card stays
assert (1, "C2") not in marks(s) and any(c["kind"] == "unmarked" for c in s.r["cards"].values())
said("no")  # a bare no answers the card
assert not any(c["kind"] == "unmarked" for c in s.r["cards"].values()) and (1, "C3") in marks(s)
s = api.store = fresh()
sees(s, "H9")  # asks about H9
sees(s, "C2")
said("No, H9")  # the card asking about H9: its no
assert not s.r["cards"] and (1, "H9") not in marks(s) and (1, "C2") in marks(s)
sees(s, "H8")
said("No, C2")  # another well: the camera's mark, and the card stays
assert (1, "C2") not in marks(s) and [c["kind"] for c in s.r["cards"].values()] == ["catch"]
print("V12 ok: Undo and No, not C2, with a card up too")

# Private: nothing happens. A dropped stream: nothing recorded, but it's in the log.
s = fresh()
s.r["run"]["mode"] = "private"
sees(s, "C2")
assert marks(s) == {} and "log" not in (s.r.get("vision") or {})
s = fresh()
sees(s, "C2", usable=False)
assert marks(s) == {} and outcomes(s) == ["ignored_stream"]
s.vision_events("run-other", [{"well": "C2", "ts": time.time(), "confidence": "sure", "usable": True}])
assert marks(s) == {}
print("V13 ok: Private, a dropped stream, another run")

# A SAM session bound to a run: new wells once, by chunk; a stream other than 720x1280 7 fps isn't judged.
s = fresh()
got = []
live_mod.on_wells = lambda rid, ev: got.append((rid, ev))
live = live_mod.Live("x", "tl", "pen", run_id="r1", started_at=100.0)
live.fps = 7.0
live.chunks = {0: {"n": 0, "start": 0, "status": "done", "w": 720, "h": 1280, "fps": 7.0},
               1: {"n": 1, "start": 63, "status": "done", "w": 504, "h": 896, "fps": 7.0}}
live.wells = [{"well": "C2", "frame": 30, "f0": 21, "confidence": "sure"}, {"well": "C3", "frame": 70, "f0": 63, "confidence": "sure"}]
live._deliver(live._new_events())
assert [(r, [(e["well"], e["usable"], e["ts"]) for e in ev]) for r, ev in got] == [("r1", [("C2", True, 103.0)]), ("r1", [("C3", False, 109.0)])]
got.clear()
live._deliver(live._new_events())
assert got == []
live_mod.on_wells = None
print("V14 ok: a session reports to its run")

# The well's frame is shared with the next chunk, still in analysis: the chunk that gave it answers for the stream.
live_mod.on_wells = lambda rid, ev: got.append((rid, ev))
live = live_mod.Live("x", "tl", "pen", run_id="r1", started_at=100.0)
live.fps = 7.0
live.chunks = {3: {"n": 3, "start": 189, "status": "done", "w": 720, "h": 1280, "fps": 7.0},
               4: {"n": 4, "start": 252, "status": "analyzing", "created": 0.0}}
live.wells = [{"well": "D8", "frame": 253, "f0": 246, "confidence": "sure"}]
live._deliver(live._new_events())
assert [(n["well"], n["usable"]) for _, ev in got for n in ev] == [("D8", True)], got
live_mod.on_wells = None
print("V16 ok: a chunk in analysis doesn't judge the stream")

# Chunks in analysis, over all the run's sessions, reach the snapshot; only the run's own, never saved.
s = fresh()
counts = []
rid = run_id(s)
live_mod.on_pending = lambda r, n: (counts.append(n), s.vision_pending(r, n))
one = live_mod.Live("p1", "tl", "pen", run_id=rid)
two = live_mod.Live("p2", "tl", "pen", run_id=rid)
other = live_mod.Live("p3", "tl", "pen")
one.chunks = {0: {"status": "analyzing"}, 1: {"status": "done"}}
two.chunks = {0: {"status": "analyzing"}}
other.chunks = {0: {"status": "analyzing"}}
live_mod._LIVE.update(p1=one, p2=two, p3=other)
one.report_pending()
assert s.snapshot()["bench"]["vision"]["pending"] == 2 and counts == [2]
version = s.r["version"]
two.report_pending()
assert s.r["version"] == version  # a number for the chip: never a new record
one.chunks[0]["status"] = two.chunks[0]["status"] = "done"
one.report_pending()
assert s.snapshot()["bench"]["vision"]["pending"] == 0 and "pending" not in (s.r.get("vision") or {})
assert "pending" not in s.bench_view(s.r)["vision"]
other.report_pending()
assert counts == [2, 2, 0]  # a test page's session reports to nobody
for k in ("p1", "p2", "p3"):
    del live_mod._LIVE[k]
live_mod.on_pending = None
print("V17 ok: chunks in analysis")

# Why a session ended goes with it; the test page may not say.
sid = "20261101-000000-abcd"
live = live_mod.Live(sid, "tl", "pen")
live.dir = Path(tempfile.mkdtemp())
live_mod._LIVE[sid] = live
assert c.post(f"/lab/sam-wells/live/{sid}/stop", json={"why": "stream downgraded: 9 frames in 3 s"}).json()["stop_why"].startswith("stream")
assert json.loads((live.dir / "live.json").read_text())["stop_why"].startswith("stream")
assert c.post(f"/lab/sam-wells/live/{sid}/stop").status_code == 200
del live_mod._LIVE[sid]
print("V18 ok: the reason a session stopped")

# "Watching" and "End watch" turn the camera on and off; a run starts off, Private refuses.
s = fresh()
assert s.snapshot()["vision"]["on"] is False
assert "Call me" in said("watching")["reply"] and s.snapshot()["vision"]["on"] is True
assert s.r["cards"] == {} and "Stopped" in said("End watch.")["reply"] and s.snapshot()["vision"]["on"] is False
said("Start watching.")
assert s.snapshot()["vision"]["on"] is True
said("stop watching")
assert s.snapshot()["vision"]["on"] is False
said("watching")
s._begin_locked(json.loads(json.dumps(PLAN)), "p2", "Next")
assert s.snapshot()["vision"]["on"] is False  # a new run asks again
set_run_mode("private")
assert "Private" in " ".join(s.apply_calls([{"name": "watch_start", "args": {}}], "voice", "", "grammar", {}).get("messages", []))
assert s.snapshot()["vision"]["on"] is False
set_run_mode("full")
s = fresh()
said("watching")
said("watching")
assert s.snapshot()["vision"]["asked"] == 2  # said again: the app tries again
c.post("/input", json={"action": {"id": "navigate", "args": {"screen": "home"}}, "session": "v"})
assert s.r["view"]["current"]["screen"] == "home" and s.snapshot()["vision"]["on"] is False  # asked for, not resumed
for text, on in (("Watch this.", True), ("finish the watch.", False), ("watch", True), ("End the watch", False)):
    s.r["view"]["current"] = {"screen": "main", "target": ""}
    said(text)
    assert s.snapshot()["vision"]["on"] is on, text
assert "watching" in live_keywords(s.r)
print("V19 ok: the person turns the camera on and off")

# Frames the glasses sent smaller keep the timeline, and nothing's judged on them.
mid = sw.well_center_mm(1, 1)
stay = [[f, mid[0], mid[1], 100] for f in range(21)]
live = live_mod.Live("x", "tl", "pen")
live.dir = Path(tempfile.mkdtemp())
live.chunks = {0: {"n": 0, "start": 0, "status": "analyzing", "low": []}}
live.finish(0, {"status": "done", "fps": 7.0, "track": stay, "inliers": {str(f): 200 for f in range(21)}})
assert [w["well"] for w in live.wells] == ["B2"], live.wells
live = live_mod.Live("x", "tl", "pen")
live.dir = Path(tempfile.mkdtemp())
live.chunks = {0: {"n": 0, "start": 0, "status": "analyzing", "low": list(range(21))}}
live.finish(0, {"status": "done", "fps": 7.0, "track": stay, "inliers": {str(f): 200 for f in range(21)}})
assert [w["well"] for w in live.wells] == ["B2"], live.wells
print("V20 ok: frames that came in small are judged like the rest")

# SAM's service fails once: the chunk gets a second go; twice: it's failed, the others go on.
calls = []
real, make, wait = live_mod.sw.track_chunk, live_mod.lab_sam.make_sam, live_mod.RETRY_S
live_mod.lab_sam.make_sam, live_mod.RETRY_S = (lambda: None), 0


def flaky(*a):
    calls.append(1)
    if len(calls) == 1:
        raise RuntimeError("500")
    return {"track": [], "fps": 7.0, "why": "no tip"}


live_mod.sw.track_chunk = flaky
live = live_mod.Live("x", "tl", "pen")
live.dir = Path(tempfile.mkdtemp())
live.chunks = {0: {"n": 0, "start": 0, "status": "analyzing"}}
live_mod._analyze(live, 0, live.dir / "0" / "clip.mp4")
assert len(calls) == 2 and live.chunks[0]["status"] == "empty", live.chunks


def down(*a):
    raise RuntimeError("500")


live_mod.sw.track_chunk = down
live.chunks[1] = {"n": 1, "start": 63, "status": "analyzing"}
live_mod._analyze(live, 1, live.dir / "1" / "clip.mp4")
assert live.chunks[1]["status"] == "failed"
live_mod.sw.track_chunk, live_mod.lab_sam.make_sam, live_mod.RETRY_S = real, make, wait
print("V21 ok: a chunk gets a second go")

# Wells of a run that was left: kept, and logged when it's back, out loud.
s = fresh()
first = run_id(s)
s.apply_calls([{"name": "end_run", "args": {}}], "voice", "", "grammar", {})
s.r["cards"] = {}
s._finish_locked("paused")
s._begin_locked(json.loads(json.dumps(PLAN)), "p2", "Other")
sees_late = [{"well": "C2", "ts": time.time(), "confidence": "sure", "usable": True}]
s.vision_events(first, sees_late)
assert marks(s) == {} and json.loads(s._late_path.read_text())[first] == sees_late
res = {"messages": [], "cards": [], "ui": [], "info": {"dropped": 0, "drop_reasons": []}}
with s._lock:
    s._resume_locked(first, {"id": "t"}, res)
assert marks(s).get((1, "C2")) == "vision", marks(s)
assert lines(s) and first not in json.loads(s._late_path.read_text())
print("V22 ok: a run that was left gets its wells back")

# Every event is logged, and the numbers add up. The queue plays through.
s = fresh()
s.apply_calls([{"name": "mark_done", "args": {"wells": ["C2", "C3"]}}], "voice", "", "grammar", {})
sees(s, "C2")
sees(s, "C4", ts=time.time() + 60)
assert runs.vision_agreement(s.r) == {"both": 1, "camera_only": 1, "person_only": 1, "undone": 0}
s = fresh()
sees(s, "C2")
snap = s.snapshot()
assert "log" not in snap["vision"]
assert c.post(f"/vision/notices/{snap['vision']['notices'][0]['id']}/played").json()["ok"] and lines(s) == []
print("V15 ok: the log, and the queue")
print("ALL PASS")
