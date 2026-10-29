"""Multi-action turns: every requested action runs, or the reply asks about
the part that didn't (never a silent drop). Stubs the model.

Run: python -m server.tests.test_multi_action (runs on import).
"""

import contextlib
import io
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
from server.screens import SCREENS  # noqa: E402
from server.store import RecordStore  # noqa: E402

api.CHAIN_ENABLED = False


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


sent = []


class Script:
    queue = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        assert url.endswith("/responses"), url
        sent.append(kw["json"])
        return FakeResp(Script.queue.pop(0))


def perform(action, cid, **args):
    return {"type": "function_call", "name": "perform", "call_id": cid, "status": "completed",
            "arguments": json.dumps({"action": action, **args})}


def fresh():
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    api.store.r["step_index"] = 2
    api._SESSIONS.clear()
    sent.clear()


def turn(text, *outputs, session="ma"):
    Script.queue = [{"id": f"r{i}", "output": list(o)} for i, o in enumerate(outputs)]
    buf = io.StringIO()
    with patch("httpx.AsyncClient", Script), contextlib.redirect_stdout(buf):
        j = c.post("/orchestrate", json={"text": text, "session": session}).json()
    assert not Script.queue, ("model rounds left unused", Script.queue)
    return j, buf.getvalue()


c = TestClient(api.app)

# MA0. One vocabulary: every performable write has registry verbs, and the
# coverage check, grammar and buttons read the same list.
from server.grammar import match_grammar, request_parts  # noqa: E402
from server.screens import ACTIONS, verbs  # noqa: E402

assert set(ACTIONS) <= set(api.PERFORM_ACTIONS), set(ACTIONS) - set(api.PERFORM_ACTIONS)
assert all(verbs(a) for a in api.WRITE_ACTIONS if a != "edit_draft"), \
    [a for a in api.WRITE_ACTIONS if not verbs(a)]
for v in verbs("add_note"):  # every note verb is heard as a note, by grammar and the check
    said = f"{v} C8 cloudy"
    assert [p["actions"] for p in request_parts(said, api.WRITE_ACTIONS)] == [{"add_note"}], said
    g = match_grammar(said, api.store.prompt_state({}))
    assert g and g[0][0]["name"] == "add_note" and g[0][0]["args"]["target"] == "C8", (said, g)
parts = request_parts("add a note on C8 and exclude C6", api.WRITE_ACTIONS)
assert [p["actions"] for p in parts] == [{"add_note"}, {"exclude_well"}], parts
print("MA0 ok: registry verbs drive grammar and the coverage check")

# MA1. Grammar claims a whole turn or nothing: a second request goes to the
# model instead of riding along as the exclude reason.
fresh()
j, _ = turn("Exclude C6 and note bubbles in C8",
            [perform("exclude_well", "a1", well="C6"),
             perform("add_note", "a2", target="C8", text="bubbles")])
assert j["route"] == "spark", j
assert [t["name"] for t in j["tool_calls"]] == ["exclude_well", "add_note"], j
assert j["tool_calls"][0]["args"].get("reason") in (None, ""), j
j, _ = turn("Exclude C7 because it's cloudy")  # one request: grammar, no model
assert j["route"] == "grammar" and j["tool_calls"][0]["args"]["reason"] == "because it's cloudy", j
print("MA1 ok: grammar yields two-request turns to the model")

# MA2. A leading card answer goes to the card (no model), the rest is a turn.
fresh()
turn("Next step")
step0 = api.store.r["step_index"]
j, _ = turn("Yes. The medium is in. Exclude C6.", [perform("exclude_well", "b1", well="C6")])
assert api.store.r["step_index"] == step0 + 1, "yes must answer the step card"
assert not any(cd["kind"] == "next_step" for cd in api.store.r["cards"].values()), api.store.r["cards"]
assert [t["name"] for t in j["tool_calls"]] == ["exclude_well"], j
assert len(sent) == 1 and "Yes." not in json.dumps(sent[0]["input"]), "only the rest reaches the model"
fresh()
turn("Next step")
j, _ = turn("No. Start a 5 minute timer first.", [perform("set_timer", "b2", minutes=5)])
assert api.store.r["step_index"] == step0 and not api.store.r["cards"], api.store.r["cards"]
assert "set_timer" in [t["name"] for t in j["tool_calls"]], j
print("MA2 ok: card answer + new action both land")

# MA3. A missing part gets one followup round, which fills it.
fresh()
j, log = turn("Open C8 and note bubbles",
              [perform("open", "c1", target="C8")],
              [perform("add_note", "c2", target="C8", text="bubbles")])
assert [t["name"] for t in j["tool_calls"]] == ["open", "add_note"], j
assert "[orch] followup ms=" in log and "still=[]" in log, log
replay = sent[1]["input"]
assert replay[-1]["content"] == api.FOLLOWUP_TEXT, replay
assert any(i.get("type") == "function_call" and i["name"] == "perform" for i in replay), replay
print("MA3 ok: followup round fills the missing part")

# MA4. Still missing after the followup: say what ran, ask about the rest.
fresh()
j, log = turn("Open C8 and note bubbles", [perform("open", "d1", target="C8")], [])
assert [t["name"] for t in j["tool_calls"]] == ["open"], j
assert j["reply"].startswith("Opened C8.") and "note bubbles" in j["reply"] and j["reply"].endswith("?"), j
assert api._SESSIONS["ma"]["turns"][-1]["asked"]["question"] in j["reply"], api._SESSIONS["ma"]["turns"]
print("MA4 ok: unfilled part is asked, never dropped")

# MA5. Nothing missing: no followup round, one model call.
fresh()
j, log = turn("Mark C2 done and start a 5 minute timer",
              [perform("mark_done", "e1", wells="C2"), perform("set_timer", "e2", minutes=5)])
assert len(sent) == 1 and "[orch] followup" not in log, log
fresh()  # one call may cover two parts (C2 and C3 together)
j, log = turn("Mark C2 and C3 done, then exclude C4",
              [perform("mark_done", "e3", wells="C2,C3"), perform("exclude_well", "e4", well="C4")])
assert len(sent) == 1 and "[orch] followup" not in log, log
print("MA5 ok: no extra round when every part ran")

# MA6. The same question card (same action + args) is never stacked or
# re-issued; different args replace it.
fresh()
turn("C6 looks bad, drop it", [perform("exclude_well", "f1", well="C6")])
first = dict(api.store.r["cards"])
j, _ = turn("drop C6 again", [perform("exclude_well", "f2", well="C6")])
assert list(api.store.r["cards"]) == list(first), (api.store.r["cards"], first)
assert j["cards"] and j["cards"][0]["id"] in first, j
turn("drop C6, it's cloudy", [perform("exclude_well", "f3", well="C6", reason="cloudy")])
assert len(api.store.r["cards"]) == 1 and list(api.store.r["cards"]) != list(first), api.store.r["cards"]
print("MA6 ok: identical card stays, changed args replace")

# MA7. A different write that covers the count still misses a named well:
# "The medium is in" ran as mark_done, "Exclude C6" never touched C6.
fresh()
j, log = turn("The medium is in. Exclude C6.",
              [perform("mark_done", "g1", wells="D2-D8")],
              [perform("exclude_well", "g2", well="C6")])
assert "exclude_well" in [t["name"] for t in j["tool_calls"]] and "[orch] followup" in log, (j, log)
print("MA7 ok: an untouched named well triggers the followup")

# MA8. A registry verb phrase ("add a note") counts as a request: when the
# model drops it, the followup asks for it.
fresh()
j, log = turn("add a note on C8 and exclude C6",
              [perform("exclude_well", "h1", well="C6")],
              [perform("add_note", "h2", target="C8", text="note")])
assert [t["name"] for t in j["tool_calls"]] == ["exclude_well", "add_note"], j
assert "[orch] followup" in log, log
print("MA8 ok: 'add a note' is heard as a note request")

# MA9. Same action on several wells is said once over the range.
fresh()
j, _ = turn("Mark C2 done, mark C3 done and mark C4 done",
            [perform("mark_done", "i1", wells="C2"), perform("mark_done", "i2", wells="C3"),
             perform("mark_done", "i3", wells="C4")])
assert "C2–C4" in j["reply"] and j["reply"].count("C3") == 0, j["reply"]
assert len(j["messages"]) == 3, j  # per-call results stay for the screen
fresh()
j, _ = turn("start a 5 minute timer on C2 to C4", [perform("set_timer", "i4", minutes=5, wells="C2-C4")])
assert j["reply"].count("5 minutes") == 1 and "C2–C4" in j["reply"], j["reply"]
print("MA9 ok: same action over wells is one sentence")

# MA10. navigate to the well card with a well is the same as open{well}:
# the card shows that well, never a targetless "Showing Well card."
fresh()
r = api.store.apply_calls([{"name": "navigate", "args": {"screen": "well_card", "target": "C8"}}],
                          "voice", "show me C8", "spark", {})
assert r["messages"] == ["Opened C8."], r["messages"]
assert api.store.r["view"]["sheet"] == {"name": "well_card", "target": "C8"}, api.store.r["view"]
print("MA10 ok: navigate to a well card opens that well")
print("ALL PASS")
