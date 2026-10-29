"""Live conversation: rules (owner, ignore, wake strip, close window,
keywords) and /voice/live end to end over a fake Meta stream. Every server
message is checked against shared/schemas/live.schema.json.

Run: python -m server.tests.test_live_voice (runs on import).
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

import jsonschema  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.live_session import LiveConversation, live_keywords, strip_wake  # noqa: E402
from server.runs import done_wells  # noqa: E402
from server.store import RecordStore  # noqa: E402

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "shared/schemas/live.schema.json").read_text())


def check(msg):
    jsonschema.validate(msg, SCHEMA)
    return msg


# Rules ---------------------------------------------------------------------
assert strip_wake("Hey Milo, mark C2 done.", "Hey Milo") == "mark C2 done."
assert strip_wake("hey milo mark C2 done", "Hey Milo") == "mark C2 done"
assert strip_wake("Mark C2 done.", "Hey Milo") == "Mark C2 done."
assert strip_wake("Hey Milo.", "Hey Milo") == ""
print("LV1 ok: wake strip")

conv = LiveConversation(opened_at=0, window_s=60, wake_phrase="Hey Milo")
outs = []
for ev in [
    {"type": "speechStart", "turnId": 1, "audioProcessedMs": 0},
    {"type": "transcript", "transcript": "Mark C2", "final": False, "audioProcessedMs": 300},
    {"type": "speaker", "label": "A", "audioProcessedMs": 800},
    {"type": "speechEnd", "turnId": 1, "audioProcessedMs": 900},
    {"type": "speechComplete", "turnId": 1, "transcript": "Mark C2 done.", "audioProcessedMs": 900},
    {"type": "speechStart", "turnId": 2, "audioProcessedMs": 1500},
    {"type": "speaker", "label": "B", "audioProcessedMs": 2500},
    {"type": "speechComplete", "turnId": 2, "transcript": "What time is lunch?", "audioProcessedMs": 2600},
    {"type": "speechComplete", "turnId": 2, "transcript": "dup", "audioProcessedMs": 2600},
]:
    outs += conv.feed(ev, 1.0)
assert [o["type"] for o in outs] == ["partial", "owner", "ignored"], outs
assert outs[1]["text"] == "Mark C2 done." and outs[2]["speaker"] == "B", outs
assert outs[2]["line"] == "Another voice — ignored"
assert not conv.should_close(59) and conv.should_close(60)  # nobody spoke since opening
conv.owner_replied(100)
assert not conv.should_close(159) and conv.should_close(160)  # quiet after the last owner reply
print("LV2 ok: owner, ignored, window")

st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
st.start_demo_run()
kw = live_keywords(st.r)
assert kw[:2] == ["Hey Milo", "Milo"], kw
assert "done" in kw and any(k in kw for k in ("C2", "B2")) and len(kw) <= 40, kw
assert len({k.lower() for k in kw}) == len(kw), kw
print("LV3 ok: keywords")


# Endpoint over a fake Meta stream -------------------------------------------
class FakeMeta:
    script: list = []
    last = None

    def __init__(self, key, **kw):
        self.session_id, self.error, self.sent = "fake-1", None, []
        self.kw = kw
        self._ended = asyncio.Event()
        FakeMeta.last = self

    async def open(self):
        return True

    async def send_audio(self, pcm):
        self.sent.append(pcm)

    async def end(self):
        self._ended.set()

    async def events(self):
        for ev in FakeMeta.script:
            await asyncio.sleep(0.01)
            yield ev
        await self._ended.wait()

    @property
    def close(self):
        return (1000, "No more transcript segments")

    async def aclose(self):
        pass


api.live_asr.MetaStream = FakeMeta
api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
FakeMeta.script = [
    {"type": "speechStart", "turnId": 1, "audioProcessedMs": 0},
    {"type": "transcript", "transcript": "mark", "final": False, "audioProcessedMs": 200},
    {"type": "speaker", "label": "A", "audioProcessedMs": 800},
    {"type": "speechComplete", "turnId": 1, "transcript": "Hey Milo, B2 done.", "audioProcessedMs": 900},
    {"type": "speechStart", "turnId": 2, "audioProcessedMs": 1500},
    {"type": "speaker", "label": "B", "audioProcessedMs": 2500},
    {"type": "speechComplete", "turnId": 2, "transcript": "B3 done.", "audioProcessedMs": 2600},
]
c = TestClient(api.app)
with c.websocket_connect("/voice/live") as ws:
    ws.send_json({"type": "start", "session": "live-test"})
    assert check(ws.receive_json())["type"] == "ready"
    ws.send_bytes(b"\x00" * 2560)
    got = []
    while not any(m["type"] == "turn" for m in got) or not any(m["type"] == "ignored" for m in got):
        got.append(check(ws.receive_json()))
    ws.send_json({"type": "end"})
    while got[-1]["type"] != "closed":
        got.append(check(ws.receive_json()))
types = [m["type"] for m in got]
turn = next(m for m in got if m["type"] == "turn")
assert types[0] == "partial" and types[-1] == "closed", types
assert turn["transcript"] == "B2 done." and turn["end_session"] is False, turn
assert "B2" in done_wells(api.store.r) and "B3" not in done_wells(api.store.r), api.store.r["done"]
assert FakeMeta.last.sent == [b"\x00" * 2560], "app PCM relayed as is"
assert FakeMeta.last.kw["mode"] == "DIARIZATION" and FakeMeta.last.kw["language_bias"] == ["English"]
print("LV4 ok: endpoint relays, owner turn runs, other speaker ignored")
print("ALL PASS")
