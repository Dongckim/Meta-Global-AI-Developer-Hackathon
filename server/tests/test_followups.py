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

# FU7. A live turn said whole for the screen isn't held for "the rest" (3 s): a card's
# "yes" (with the agent's name too), Import's "file" / "photo" / "text"; a real
# cut-off word ("add", "I want to") still waits.
api.store = st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
st.r["step_index"] = 0
st.apply_calls([{"name": "next_step", "args": {}}], "voice", "", "grammar", {})  # a card: wells not marked
assert st.r["cards"], "a card is up"
for said in ("yes", "Yes.", "yes Milo", "Milo, yes", "mark them", "not yet"):
    assert not api.live_unfinished(said, {}), said
st.r["cards"] = {}
st.r["view"]["current"] = {"screen": "import_choice", "target": ""}
for said in ("file", "File.", "photo", "text", "okay file"):
    assert not api.live_unfinished(said, {}), said
for said in ("add", "I want to"):
    assert api.live_unfinished(said, {}), said
st.r["view"]["current"] = {"screen": "draft_page", "target": "1"}
assert not api.live_unfinished("page 2", {}) and not api.live_unfinished("zoom in", {})
# The end of a turn, read once: Full's hold and the iPad's wait agree on any words
READ = [("mark D2 to", "unfinished-extended"), ("mark C2 and", "unfinished-extended"), ("mark the row D", "unfinished-extended"),
        ("add this to the", "unfinished-extended"), ("C4 through", "unfinished-extended"), ("note C3 looks cloudy", "silence"),
        ("mark D2 to D8 done", "silence"), ("what is on B2?", "silence")]
for said, reason in READ:
    got = api.device_endpoint(said, {})
    assert got["reason"] == reason, (said, got)
    assert api.live_unfinished(said, {}) == (reason == "unfinished-extended"), said
    assert got["extra_s"] == (2.5 if reason == "unfinished-extended" else 0), got
assert api.device_endpoint("page 2", {})["reason"] == "exact-match", "exactly a control: at once"
st.r["view"]["current"] = {"screen": "import_choice", "target": ""}
assert api.device_endpoint("file", {})["reason"] == "exact-match"
print("FU-end ok: Full and Private read the end of a turn with one function")
print("FU7 ok: whole for this screen goes now; a cut-off word still waits")

# FV. What the screen is about (registry `viewing`) is in the model's state, and an
# action naming no target takes it.
def turn(text, payload, session):
    script[:] = [{"id": "v", "output": payload}]
    with patch("httpx.AsyncClient", FakeClient):
        return c.post("/input", json={"text": text, "session": session}).json()


# FV1. On C5's well card, "add a note here": the note goes on C5.
api.store = st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
api._SESSIONS.clear()
st.r["view"]["sheet"] = {"name": "well_card", "target": "C5"}
sent.clear()
j = turn("add a note here, bubbles in the well", [fc("perform", {"action": "add_note", "text": "bubbles"}, "n1")], "fv1")
assert '"viewing": {"kind": "well", "label": "C5"}' in sent[0]["instructions"], sent[0]["instructions"][-600:]
assert [(m["target"], m["text"]) for m in st.r["notes"]] == [("C5", "bubbles")], (st.r["notes"], j)
print("FV1 ok: a note with no well, on a well card, goes on that well")

# FV2. On the bench at step 1, "skip this step": the model hears which step is on screen;
# moving on with wells left asks about that step's wells (Skip them / Mark them).
api._SESSIONS.clear()
st.r["view"]["sheet"] = {"name": "", "target": ""}
sent.clear()
j = turn("skip this step", [fc("perform", {"action": "next_step"}, "s1")], "fv2")
assert '"viewing": {"kind": "step", "label": "Step 1 · Add culture medium"}' in sent[0]["instructions"]
assert [x["kind"] for x in j["cards"]] == ["unmarked"] and j["cards"][0]["context"].startswith("Step 1 ·"), j
print("FV2 ok: this step is the step on screen")

# FV3. On a search answer, "open this" opens its source page.
api.store = st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api._SESSIONS.clear()
st.add_search({"id": "s1", "ts": 1, "question": "Max DMSO for HeLa?", "query": "max DMSO HeLa",
               "answer_short": "Up to 0.5%", "detail": "d", "status": "ok", "run_id": None, "ms": 1,
               "sources": [{"title": "A", "url": "https://a.org", "domain": "a.org"}]})
st.r["view"]["current"] = {"screen": "web_answer", "target": "s1"}
j = turn("open this", [fc("perform", {"action": "open"}, "o1")], "fv3")
assert st.r["view"]["current"] == {"screen": "web_source", "target": "s1:1"}, (st.r["view"], j)
print("FV3 ok: open this on a search answer opens its source")
print("ALL PASS")
