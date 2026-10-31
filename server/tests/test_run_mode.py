"""A run is always in the chosen mode: a protocol's or a run's past has no say,
and the header says where a turn really goes.

Run: python -m server.tests.test_run_mode (runs on import).
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"
os.environ["PRIVATE_LLM_URL"] = "http://glimmer.test"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import tone  # noqa: E402
from server.store import RecordStore  # noqa: E402


class Route:
    """_post stand-in: says which model a turn went to."""

    def __init__(self):
        self.to = []

    async def __call__(self, client, name, url, **k):
        call = {"name": "perform", "arguments": json.dumps({"action": "mark_done", "wells": ["B7"]})}
        if url.startswith("http://glimmer.test"):
            self.to.append("glimmer")
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c", "type": "function", "function": call}]}, "finish_reason": "stop"}]})
        self.to.append("spark")
        return httpx.Response(200, json={"id": "r", "status": "completed", "output": [
            {"type": "function_call", "call_id": "c", **call}]})


def fresh(mode):
    tone.set_run_mode(mode)
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.spawn_wake = lambda session, gen: None
    api.store.spawn_handoff = lambda *a, **k: None
    api._SESSIONS.clear()
    return api.store, api.get_session("m")


def choose(st, session, mode):
    """As the Mode screen does; its wake-up isn't what's under test."""
    with st._lock:
        st._save_mode(mode)
        st._commit()


def pause(st):
    with st._lock:
        st._finish_locked("paused")


def resume(st, session, run_id):
    return st.apply_calls([{"name": "resume_run", "args": {"target": run_id}}], "tap", "", "tap", session)


def agrees(st, mode):
    """The header, the run, the transcription and the model all say `mode`."""
    assert st.header_mode("main")["id"] == mode and st.r["run"]["mode"] == mode, (st.header_mode("main"), st.r["run"])
    st.wake.begin()
    st.wake.finish(True)
    snap = TestClient(api.app).get("/record").json()
    assert snap["header"]["mode"]["id"] == mode or snap["header"]["mode"] is None, snap["header"]
    assert snap["transcribe"] == ("device" if mode == "private" else "server"), snap["transcribe"]
    route = Route()
    with patch.object(api, "_post", route):
        TestClient(api.app).post("/input", json={"text": "the seventh in row B, tick it off", "session": "m"})
    assert route.to == ["glimmer" if mode == "private" else "spark"], route.to


# A protocol run in Full, started again in Private, and the other way
for before, now in (("full", "private"), ("private", "full")):
    st, s = fresh(before)
    st.start_demo_run()
    with st._lock:
        st._finish_locked("finished")
    choose(st, s, now)
    st.start_demo_run()
    agrees(st, now)
    assert not st.r["run"].get("mode_changes"), "a new run has no past"
print("RM1 ok: a new run takes the chosen mode, not its protocol's last run")

# A paused run picks up in the chosen mode, and keeps a line about it
for before, now, line in (("full", "private", "Switched to Private at step 3"),
                          ("private", "full", "Switched to Full at step 3")):
    st, s = fresh(before)
    st.start_demo_run()
    st.r["step_index"] = 2
    rid = st.r["run"]["run_id"]
    pause(st)
    choose(st, s, now)
    res = resume(st, s, rid)
    assert not res["cards"], "picking up never asks"
    assert [c["line"] for c in st.r["run"]["mode_changes"]] == [line], st.r["run"]
    agrees(st, now)
    pause(st)
    j = TestClient(api.app).get(f"/runs/{rid}").json()
    assert j["mode_changes"] == [line], j["mode_changes"]
print("RM2 ok: resuming follows the chosen mode, the switch stays on the record")

# Same mode: nothing to note. A switch mid-run is noted and followed at once
st, s = fresh("private")
st.start_demo_run()
rid = st.r["run"]["run_id"]
pause(st)
resume(st, s, rid)
assert not st.r["run"].get("mode_changes")
choose(st, s, "full")
assert st.r["run"]["mode"] == "full" and len(st.r["run"]["mode_changes"]) == 1
agrees(st, "full")
print("RM3 ok: only a real change is noted; mid-run follows too")

# Hand off goes by the mode the run ends in
for started, ended, offered in (("full", "private", False), ("private", "full", True)):
    st, s = fresh(started)
    st.start_demo_run()
    choose(st, s, ended)
    res = st.apply_calls([{"name": "end_run", "args": {}}], "tap", "", "tap", s)
    card = next(iter(st.r["cards"].values()), None)
    if card:
        st.confirm(card["id"], True, "card", s)
    doc = st.all_run_docs()[0]
    assert doc["record"]["run"]["mode"] == ended, doc["record"]["run"]
    on_screen = st.r["view"]["current"]["screen"]
    assert (on_screen == "final_protocol") == offered, (on_screen, started, ended)
print("RM4 ok: hand off goes by the mode the run ends in")

tone.set_run_mode(None)
print("ALL PASS")
