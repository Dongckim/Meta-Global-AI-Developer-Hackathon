"""The Mode cards and the routing are one setting: change it and both move.

Run: python -m server.tests.test_mode_flow (runs on import).
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
import jsonschema  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import modes, tone  # noqa: E402
from server.store import RecordStore  # noqa: E402

DATA = tone._data()
SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "shared/schemas/snapshot.schema.json").read_text())


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


def setting(mode, **changes):
    """pip_voice.json with one mode's settings moved."""
    moved = json.loads(json.dumps(DATA))
    moved["modes"][mode].update(changes)
    return patch.object(tone, "_data", lambda: moved)


def named(step):
    return f"{step['name']} ({step['where']})" if step["where"] else step["name"]


def fresh(mode):
    tone.set_run_mode(mode)
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.spawn_wake = lambda session, gen: None
    api._SESSIONS.clear()
    api.store.start_demo_run()
    api.store.wake.begin()
    api.store.wake.finish(True)
    return TestClient(api.app)


def went_to(c):
    route = Route()
    with patch.object(api, "_post", route):
        c.post("/input", json={"text": "the seventh in row B, tick it off", "session": "m"})
    return route.to


full, private = modes.mode_view("full"), modes.mode_view("private")
assert [named(s) for s in full["steps"]] == ["Meta Voice Transcribe", "Muse Spark (Meta API)"], full
assert full["notes"] == ["Search · Photos · Robot code: Meta API"], full
assert [named(s) for s in private["steps"]] == ["iOS Speech (on iPad)", "Muse Glimmer (our server)"], private
assert private["notes"] == ["Search · Photos: asks first", "Robot code: Full only"], private
print("MF1 ok: the cards say what was asked for")

c = fresh("private")
snap = c.get("/record").json()
assert snap["modes"] == [full, private]
jsonschema.validate(snap["modes"], SCHEMA["properties"]["modes"])
print("MF2 ok: the cards ride the snapshot")

# Shown = routed, for every mode: a turn, then the same setting moved
for mode, model in (("full", "spark"), ("private", "glimmer")):
    c = fresh(mode)
    assert went_to(c) == [model]
    card = modes.mode_view(mode)["steps"][1]["name"].lower()
    assert model in card, (model, card)
    assert (c.get("/record").json()["transcribe"] == "device") == (modes.mode_view(mode)["steps"][0]["name"] == "iOS Speech")
with setting("private", commands="spark", asr="meta"):
    c = fresh("private")
    got = went_to(c)
    assert got == ["spark"], got
    assert [s["name"] for s in modes.mode_view("private")["steps"]] == ["Meta Voice Transcribe", "Muse Spark"]
    assert c.get("/record").json()["transcribe"] == "server"
with setting("full", commands="glimmer"):
    c = fresh("full")
    assert went_to(c) == ["glimmer"]
    assert modes.mode_view("full")["steps"][1]["name"] == "Muse Glimmer"
print("MF3 ok: move the setting, and the turn and the card move together")

with setting("private", confirm_photo=False, handoff=True):
    assert modes.mode_view("private")["notes"] == ["Photos · Robot code: Meta API", "Search: asks first"]
    c = fresh("private")
    j = c.post("/input", json={"text": "what does this label say", "session": "m"}).json()
    assert not j["cards"], "photo didn't ask: the card said so too"
print("MF4 ok: the fine print follows the confirm and hand off settings")

c = fresh("full")
api.store.r["view"]["current"] = {"screen": "mode", "target": ""}
route = Route()
with patch.object(api, "_post", route):
    j = c.post("/input", json={"text": "what's the difference?", "session": "m"}).json()
assert j["route"] == "grammar" and not route.to, j
reply = j["reply"]
for words in ("Meta Voice Transcribe", "Muse Spark (Meta API)", "iOS Speech (on iPad)", "Muse Glimmer (our server)",
              "Robot code is Full only."):
    assert words in reply, reply
print("MF5 ok: the difference is said from the same settings, no model")

tone.set_run_mode(None)
print("ALL PASS")
