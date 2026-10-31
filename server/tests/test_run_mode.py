"""A run owns its mode from its start; the app follows the run that's open,
and the header always says where a turn really goes.

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
from server import library as lib, tone  # noqa: E402
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


def fresh():
    tone.set_run_mode("full")
    st = api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    st.spawn_wake = lambda session, gen: None
    st.spawn_handoff = lambda *a, **k: None
    api._SESSIONS.clear()
    lib.ensure_demo_seed(st.root)
    return st, api.get_session("m"), lib.find_by_name("Demo protocol", st.root)["id"]


def act(st, s, name, **args):
    return st.apply_calls([{"name": name, "args": args}], "tap", "", "tap", s)


def screen(st):
    return st.r["view"]["current"]["screen"]


def awake(st, s):
    """What the wake-up task does when the model answers."""
    assert st.wake_done(st.wake.gen, True, s) or st.wake.fresh()


def start(st, s, pid, mode):
    """Start run, then the Mode screen, the only way in."""
    act(st, s, "start_run", target=pid)
    assert screen(st) == "mode", screen(st)
    act(st, s, "choose_mode", mode=mode)
    if st.wake.state == "warming":
        assert screen(st) == "private_wait"
        awake(st, s)
    assert screen(st) == "main", screen(st)
    assert st.r["run"]["mode"] == mode


def home(st, s):
    act(st, s, "navigate", screen="home")
    assert screen(st) == "home"


def agrees(st, mode):
    """Header, open run, transcription and model all say `mode`."""
    assert st.r["run"]["mode"] == mode and st.header_mode("main")["id"] == mode, (st.r["run"], tone.run_mode())
    c = TestClient(api.app)
    snap = c.get("/record").json()
    assert snap["header"]["mode"]["id"] == mode, snap["header"]
    assert snap["transcribe"] == ("device" if mode == "private" else "server"), snap["transcribe"]
    route = Route()
    with patch.object(api, "_post", route):
        c.post("/input", json={"text": "the seventh in row B, tick it off", "session": "m"})
    assert route.to == ["glimmer" if mode == "private" else "spark"], route.to


# The reported bug: a Full past doesn't make a new Private run Full
st, s, pid = fresh()
start(st, s, pid, "full")
home(st, s)
start(st, s, pid, "private")
assert [r["mode"] for r in TestClient(api.app).get("/runs").json()["runs"]].count("full") == 1, "the old run keeps its own"
agrees(st, "private")
print("RM1 ok: a protocol run in Full before starts Private and stays Private")

# Home is Full; Continue goes back to the run's own mode through the wait
home(st, s)
assert tone.run_mode() == "full" and st.r["run"]["mode"] == "private"
assert TestClient(api.app).get("/record").json()["transcribe"] == "server"
st.wake = type(st.wake)()  # gone to sleep meanwhile
act(st, s, "navigate", screen="main")
assert screen(st) == "private_wait" and tone.run_mode() == "private", screen(st)
assert [b["say"] for b in api.buttons_for("private_wait", st.view_slots("private_wait", "main"))] == [], "no way to Full"
awake(st, s)
assert screen(st) == "main"
agrees(st, "private")
print("RM2 ok: home is Full; Continue wakes Private and opens the Bench")

# A paused Private run: Continue goes Private; a failure offers Home, never Full
rid = st.r["run"]["run_id"]
with st._lock:
    st._finish_locked("paused")
home(st, s)
st.wake = type(st.wake)()
act(st, s, "navigate", screen="run_view", target=rid)
assert screen(st) == "run_view" and tone.run_mode() == "private" and st.wake.state == "warming"
act(st, s, "resume_run", target=rid)
assert screen(st) == "private_wait", screen(st)
assert st.wake_done(st.wake.gen, False, s)
says = [b["say"] for b in api.buttons_for("private_wait", st.view_slots("private_wait", "main"))]
assert says == ["Try again", "Back to Home"], says
act(st, s, "choose_mode", mode="private")
assert st.wake.state == "warming" and screen(st) == "private_wait"
assert st.wake_done(st.wake.gen, False, s)
act(st, s, "navigate", screen="home")
assert screen(st) == "home" and tone.run_mode() == "full" and st.r["run"]["mode"] == "private"
print("RM3 ok: a paused Private run wakes first; failing leaves Try again and Home only")

# A Full run opens in Full, whatever was open before
st, s, pid = fresh()
start(st, s, pid, "full")
rid = st.r["run"]["run_id"]
with st._lock:
    st._finish_locked("paused")
home(st, s)
act(st, s, "navigate", screen="run_view", target=rid)
assert tone.run_mode() == "full" and st.wake.state == "idle"
act(st, s, "resume_run", target=rid)
assert screen(st) == "main"
agrees(st, "full")
print("RM4 ok: a Full run stays Full")

# A finished Private run: the record opens now, the model wakes behind it
st, s, pid = fresh()
start(st, s, pid, "private")
rid = st.r["run"]["run_id"]
with st._lock:
    st._finish_locked("finished")
home(st, s)
st.wake = type(st.wake)()
act(st, s, "navigate", screen="run_view", target=rid)
assert screen(st) == "run_view" and tone.run_mode() == "private" and st.wake.state == "warming"
c = TestClient(api.app)
route = Route()
with patch.object(api, "_post", route):
    j = c.post("/input", json={"text": "how did the cells look overall", "session": "m"}).json()
    assert j["route"] == "wait" and not route.to and screen(st) == "run_view", j
    assert j["reply"] == "One moment, the private model is waking up.", j
    assert api.get_session("m").get("wake_pending"), "kept for after ready"
    j = c.post("/input", json={"text": "back", "session": "m"}).json()
assert j["route"] != "wait", "grammar doesn't wait"
print("RM5 ok: a finished Private run opens at once; model turns park, grammar doesn't")

# The mode is chosen on the Mode screen only
st, s, pid = fresh()
start(st, s, pid, "full")
res = act(st, s, "choose_mode", mode="private")
assert res["messages"] == ["A run keeps the mode it started in. Pick one when you start a new run."], res
agrees(st, "full")
print("RM6 ok: nothing but the Mode screen changes a mode")

# Hand off goes by the run's mode, and lists carry it
st, s, pid = fresh()
start(st, s, pid, "private")
rid = st.r["run"]["run_id"]
with st._lock:
    st._finish_locked("paused")
home(st, s)
assert [r["mode"] for r in TestClient(api.app).get("/runs").json()["runs"]] == ["private"]
doc = st.all_run_docs()[0]
assert st.r["cards"] == {} and not api.route("handoff", st._run_mode_of(doc))
print("RM7 ok: the run list gives each run's mode; hand off goes by it")

# A restart: the mode of what's open
st, s, pid = fresh()
start(st, s, pid, "private")
tone.set_run_mode(None)
again = RecordStore(path=st.path)
again.restore_mode()
assert tone.run_mode() == "private"
home(st, s)
RecordStore(path=st.path).restore_mode()
assert tone.run_mode() == "full"
print("RM8 ok: after a restart the app is in the mode of what's open")

tone.set_run_mode(None)
print("ALL PASS")
