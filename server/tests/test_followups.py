"""Follow-up questions and what Milo may say (model faked, scripted per call):
a photo answer's follow-up carries what was read and the protocol; a read
dropped in the lookup round and a "Checking..." promise never reach the
person; the server's own reasons never do either.

Run: python -m server.tests.test_followups (runs on import).
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

sent: list = []
script: list = []


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeClient:
    """Answers each /responses call with the next scripted payload."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        sent.append(kw.get("json") or {})
        return FakeResp(script.pop(0) if script else {"id": "r", "output": []})


def fc(name, args, cid):
    return {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": cid,
            "status": "completed"}


def msg(text):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def photo_store():
    """A run of the demo protocol, on the answer to a photo of a plate's label."""
    st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    st.start_demo_run()
    st.add_search({"id": "p1", "ts": 1, "question": "what plate is this?", "query": "", "kind": "photo",
                   "target": "plate", "answer_short": "Corning 96-well plate, Non-Tissue Culture Treated",
                   "detail": "Flat bottom, polystyrene, sterile.", "status": "ok", "basis": "photo",
                   "attempt": 1, "run_id": st.r["run"]["run_id"], "ms": 0, "sources": []})
    st.r["view"]["current"] = {"screen": "web_answer", "target": "p1"}
    return st


c = TestClient(api.app)
api.CHAIN_ENABLED = False

# FU1. A follow-up on a photo answer: the model gets what was read and the protocol's
# materials and conditions (the label vs. what the protocol needs).
api.store = photo_store()
api._SESSIONS.clear()
sent.clear()
script[:] = [{"id": "r1", "output": [msg("No: it's non-treated, and this protocol grows cells on the well wall.")]}]
with patch("httpx.AsyncClient", FakeClient):
    j = c.post("/input", json={"text": "So is this right tool for our protocol?", "session": "fu"}).json()
state = sent[0]["instructions"]
assert "Non-Tissue Culture Treated" in state and '"photo_answer"' in state, state[-1500:]
assert "culture medium" in state and "Drug A" in state and '"protocol"' in state, state[-1500:]
assert "On a photo answer (photo_answer in state)" in state
assert j["reply"].startswith("No: it's non-treated"), j
print("FU1 ok: a photo answer's follow-up carries the label read and the protocol")

# FU2. The lookup round asks to read again (dropped: one lookup round per turn) and the
# model says "Checking the protocol...": that promise isn't said; the person hears
# what couldn't be done.
api.store = st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
api._SESSIONS.clear()
script[:] = [
    {"id": "r1", "output": [fc("read_record", {"kind": "steps", "filter": "", "limit": 10}, "a1")]},
    {"id": "r2", "output": [fc("read_record", {"kind": "source", "filter": "", "limit": 10}, "a2"),
                            msg("Checking the protocol for the plate type now…")]},
]
with patch("httpx.AsyncClient", FakeClient):
    j = c.post("/input", json={"text": "does the protocol say which plate to use?", "session": "fu2"}).json()
assert "Checking" not in j["reply"] and "lookup depth" not in j["reply"], j
assert j["reply"].endswith("I couldn't open the protocol just now. Ask again?"), j  # after what was read
print("FU2 ok: a dropped read and a promise give way to a line the person can act on")

# FU3. Each turn starts with no drops: the next turn's reply isn't the last turn's failure.
script[:] = [{"id": "r3", "output": [msg("Step 3 adds Drug A.")]}]
with patch("httpx.AsyncClient", FakeClient):
    j = c.post("/input", json={"text": "what's step 3 about?", "session": "fu2"}).json()
assert j["reply"] == "Step 3 adds Drug A.", j
print("FU3 ok: drops are per turn")

# FU4. web_search chosen in the lookup round (after a read) goes to the search path,
# never the store ("unknown tool"); in a mode that asks first, it's the search card.
searched = []


async def fake_search(client, key, question, query, session):
    searched.append(query)
    return "Up to 0.5% DMSO is usually fine.", []

api.store = st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
api._SESSIONS.clear()
script[:] = [
    {"id": "r1", "output": [fc("read_record", {"kind": "steps", "filter": "", "limit": 10}, "b1")]},
    {"id": "r2", "output": [fc("perform", {"action": "web_search", "query": "max DMSO for HeLa"}, "b2")]},
]
with patch("httpx.AsyncClient", FakeClient), patch.object(api, "_run_search", fake_search):
    j = c.post("/input", json={"text": "how much DMSO can HeLa take?", "session": "fu4"}).json()
assert searched == ["max DMSO for HeLa"] and j["reply"] == "Up to 0.5% DMSO is usually fine.", (searched, j)
script[:] = [
    {"id": "r1", "output": [fc("read_record", {"kind": "steps", "filter": "", "limit": 10}, "c1")]},
    {"id": "r2", "output": [fc("perform", {"action": "web_search", "query": "max DMSO for HeLa"}, "c2")]},
]
with patch("httpx.AsyncClient", FakeClient), patch.object(api, "_run_search", fake_search), \
        patch.object(api, "_confirm_search", lambda: True):
    j = c.post("/input", json={"text": "how much DMSO can HeLa take?", "session": "fu4b"}).json()
assert [x["kind"] for x in j["cards"]] == ["confirm_search"] and len(searched) == 1, j
print("FU4 ok: web_search from the lookup round takes the search path (a card when asked first)")

# FU5. One list: every model action is the turn's (TURN_ACTIONS) or the store's; none is
# "unknown" to the store.
st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
for name in api.PERFORM_ACTIONS:
    if name in api.TURN_ACTIONS:
        continue
    res = st.apply_calls([{"name": name, "args": {}}], "tap", "", "tap", {})
    assert not any(d["reason"] == "unknown tool" for d in res["info"]["drop_reasons"]), name
print("FU5 ok: every perform action has a handler")

# FU6. "Search it" on a photo answer searches what the photo was read as.
api.store = photo_store()
api._SESSIONS.clear()
searched.clear()
with patch("httpx.AsyncClient", FakeClient), patch.object(api, "_run_search", fake_search):
    j = c.post("/input", json={"text": "search it", "session": "fu6"}).json()
assert searched == ["Corning 96-well plate, Non-Tissue Culture Treated Flat bottom, polystyrene, sterile."], searched
print("FU6 ok: search it on a photo answer searches the label")
print("ALL PASS")
