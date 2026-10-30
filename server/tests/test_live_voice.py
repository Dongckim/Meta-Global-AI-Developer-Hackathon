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
from server import tone  # noqa: E402
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
# the owner's turn (at t=1) restarted the window; the other speaker didn't
assert conv.should_close(60.9) is None and conv.should_close(61) == "window"
conv.owner_replied(5)
conv.tts(True, 5)
assert conv.should_close(500) is None  # paused while the agent speaks
conv.tts(False, 20)
assert conv.should_close(79.9) is None and conv.should_close(80) == "window"  # restarts at speech end
conv.feed({"type": "speechStart", "turnId": 3, "audioProcessedMs": 9000}, 81)
assert conv.should_close(200) is None  # never mid-turn
conv.feed({"type": "speaker", "label": "B", "audioProcessedMs": 9500}, 82)
conv.feed({"type": "speechComplete", "turnId": 3, "transcript": "lunch?", "audioProcessedMs": 9600}, 83)
assert conv.should_close(200) == "window"  # another voice doesn't extend
long = LiveConversation(opened_at=0, window_s=60, max_s=3300)
long.tts(False, 3290)
assert long.should_close(3300) == "max_duration"
print("LV2 ok: owner, ignored, window pause/restart, turn boundary, max")

from server.grammar import match_session_end  # noqa: E402
for t, want in [("Thanks.", "thanks"), ("thank you Milo", "thanks"), ("That's all.", "thanks"),
                ("Stop.", "stop"), ("Never mind.", "stop"), ("thanks for the help with C2", None),
                ("stop the timer", None), ("never mind the timer", None), ("Thanks Bob", None),
                # normalized: punctuation dropped, repeats folded
                ("Thanks. Thanks.", "thanks"), ("thanks thanks", "thanks"),
                ("thank you, thank you Milo", "thanks"), ("okay thanks", "thanks"),
                ("Okay. Thanks.", "thanks"), ("Thank you so much.", "thanks"),
                ("Stop. Stop.", "stop"), ("never mind, never mind", "stop"),
                ("thanks thanks for the help", None), ("stop stop the timer", None)]:
    assert match_session_end(t, "Milo") == want, (t, match_session_end(t, "Milo"))
print("LV2b ok: session-end grammar")

# Only the wake phrase: a "here" turn, and the owner spoke, so the clock restarts.
hw = LiveConversation(opened_at=0, window_s=60, wake_phrase="Hey Milo")
for ev in [{"type": "speechStart", "turnId": 1, "audioProcessedMs": 0},
           {"type": "speaker", "label": "A", "audioProcessedMs": 400}]:
    hw.feed(ev, 30)
out = hw.feed({"type": "speechComplete", "turnId": 1, "transcript": "Hey, Milo.", "audioProcessedMs": 500}, 40)
assert out == [{"type": "here", "turn": 1}], out
assert hw.should_close(99) is None and hw.should_close(100) == "window"
print("LV2c ok: wake-only turn")

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
assert got[-1]["why"] == "client", got[-1]
types = [m["type"] for m in got]
turn = next(m for m in got if m["type"] == "turn")
assert types[0] == "partial" and types[-1] == "closed", types
assert turn["transcript"] == "B2 done." and turn["end_session"] is False, turn
assert "B2" in done_wells(api.store.r) and "B3" not in done_wells(api.store.r), api.store.r["done"]
assert FakeMeta.last.sent == [b"\x00" * 2560], "app PCM relayed as is"
assert FakeMeta.last.kw["mode"] == "DIARIZATION" and FakeMeta.last.kw["language_bias"] == ["English"]
print("LV4 ok: endpoint relays, owner turn runs, other speaker ignored")

# "thanks" ends the session: a short reply, end_session, then closed(end_session)
FakeMeta.script = [
    {"type": "speechStart", "turnId": 1, "audioProcessedMs": 0},
    {"type": "speaker", "label": "A", "audioProcessedMs": 500},
    {"type": "speechComplete", "turnId": 1, "transcript": "Thanks, Milo.", "audioProcessedMs": 600},
]
with c.websocket_connect("/voice/live") as ws:
    ws.send_json({"type": "start", "session": "live-end"})
    assert check(ws.receive_json())["type"] == "ready"
    got = [check(ws.receive_json())]
    while got[-1]["type"] != "closed":
        got.append(check(ws.receive_json()))
turn = next(m for m in got if m["type"] == "turn")
assert turn["end_session"] is True and turn["reply"] in [v.format(wake=tone.persona()["wake_phrase"]) for v in tone._data()["session_thanks"]], turn
assert got[-1]["why"] == "end_session", got[-1]
print("LV5 ok: explicit end")

# A quiet session still pings (schema-valid), so the app can tell quiet from dead.
FakeMeta.script = []
api.HEARTBEAT_S, beat = 0.05, api.HEARTBEAT_S
with c.websocket_connect("/voice/live") as ws:
    ws.send_json({"type": "start", "session": "live-ping"})
    assert check(ws.receive_json())["type"] == "ready"
    assert check(ws.receive_json()) == {"type": "ping"}
    ws.send_json({"type": "end"})
    while check(ws.receive_json())["type"] != "closed":
        pass
api.HEARTBEAT_S = beat
print("LV5b ok: quiet live session pings")

# /events: the first message is a full snapshot (a reconnect restores what it
# missed), then a keepalive comment while nothing changes.
async def _first_two():
    api.HEARTBEAT_S = 0.05
    try:
        it = (await api.events()).body_iterator
        first, second = await it.__anext__(), await it.__anext__()
        await it.aclose()
        return first, second
    finally:
        api.HEARTBEAT_S = beat
first, second = asyncio.run(_first_two())
snap = json.loads(first[len("data: "):])
assert snap["kind"] == "snapshot" and snap["version"] == api.store.r["version"], snap.keys()
assert second == ": ping\n\n", second
print("LV5c ok: /events opens with a snapshot and keeps alive")

# A failing turn keeps the session: sorry, then the next turn still runs; a
# wake-only turn answers "here" with no model call.
real_handle = api._handle_text
calls = {"n": 0}


async def flaky(text, session, source):
    calls["n"] += 1
    if calls["n"] == 1:
        raise TypeError("sequence item 0: expected str instance, NoneType found")
    return await real_handle(text, session, source)

api._handle_text = flaky
FakeMeta.script = [
    {"type": "speechStart", "turnId": 1, "audioProcessedMs": 0},
    {"type": "speaker", "label": "A", "audioProcessedMs": 500},
    {"type": "speechComplete", "turnId": 1, "transcript": "let's move on to the recent run.", "audioProcessedMs": 600},
    {"type": "speechStart", "turnId": 2, "audioProcessedMs": 1000},
    {"type": "speaker", "label": "A", "audioProcessedMs": 1500},
    {"type": "speechComplete", "turnId": 2, "transcript": "Hey Milo.", "audioProcessedMs": 1600},
    {"type": "speechStart", "turnId": 3, "audioProcessedMs": 2000},
    {"type": "speaker", "label": "A", "audioProcessedMs": 2500},
    {"type": "speechComplete", "turnId": 3, "transcript": "C4 done.", "audioProcessedMs": 2600},
]
with c.websocket_connect("/voice/live") as ws:
    ws.send_json({"type": "start", "session": "live-flaky"})
    assert check(ws.receive_json())["type"] == "ready"
    turns = []
    while len(turns) < 3:
        m = check(ws.receive_json())
        if m["type"] == "turn":
            turns.append(m)
    ws.send_json({"type": "end"})
    while check(ws.receive_json())["type"] != "closed":
        pass
api._handle_text = real_handle
assert turns[0]["reply"] in ("Sorry, say that again?", "Didn't catch that — once more?"), turns[0]
assert turns[1]["reply"] in ("I'm here.", "Yes?", "Go ahead.") and turns[1]["route"] == "grammar", turns[1]
assert "C4" in done_wells(api.store.r), turns[2]
assert calls["n"] == 2, calls  # "here" never reached the text path
print("LV6 ok: failing turn keeps the session; wake-only says here")
# LV7. A cut-off owner turn waits for the rest: the live log's order
# ("I wanna I want to see", "I want", "I want to see all the notes") gets one
# answer, for the joined sentence. A cut-off turn alone is answered after the hold.
heard = []


async def record_text(text, session, source):
    heard.append(text)
    return await real_handle("next", session, source)  # any grammar reply

api._handle_text = record_text
FakeMeta.script = [
    {"type": "speechStart", "turnId": 1, "audioProcessedMs": 0},
    {"type": "speaker", "label": "A", "audioProcessedMs": 100},
    {"type": "speechComplete", "turnId": 1, "transcript": "I wanna I want to see", "audioProcessedMs": 200},
    {"type": "speechStart", "turnId": 2, "audioProcessedMs": 300},
    {"type": "speaker", "label": "A", "audioProcessedMs": 400},
    {"type": "speechComplete", "turnId": 2, "transcript": "I want", "audioProcessedMs": 500},
    {"type": "speechStart", "turnId": 3, "audioProcessedMs": 600},
    {"type": "speaker", "label": "A", "audioProcessedMs": 700},
    {"type": "speechComplete", "turnId": 3, "transcript": "I want to see all the notes", "audioProcessedMs": 800},
]
with c.websocket_connect("/voice/live") as ws:
    ws.send_json({"type": "start", "session": "live-hold"})
    assert check(ws.receive_json())["type"] == "ready"
    turns = []
    while not turns:
        m = check(ws.receive_json())
        if m["type"] == "turn":
            turns.append(m)
    ws.send_json({"type": "end"})
    while check(ws.receive_json())["type"] != "closed":
        pass
assert heard == ["I wanna I want to see I want I want to see all the notes"], heard
assert len(turns) == 1, turns
heard.clear()
api.LIVE_HOLD_S, hold = 0.2, api.LIVE_HOLD_S
FakeMeta.script = FakeMeta.script[:3]  # only the cut-off turn
with c.websocket_connect("/voice/live") as ws:
    ws.send_json({"type": "start", "session": "live-hold-2"})
    assert check(ws.receive_json())["type"] == "ready"
    while check(ws.receive_json())["type"] != "turn":
        pass
    ws.send_json({"type": "end"})
    while check(ws.receive_json())["type"] != "closed":
        pass
api.LIVE_HOLD_S = hold
api._handle_text = real_handle
assert heard == ["I wanna I want to see"], heard
print("LV7 ok: cut-off turns wait for the rest, then go alone")
# LV-send. One way out to the live socket: after it closes (or the app left),
# pings, stages and replies are dropped, never "websocket.send after close".
import asyncio  # noqa: E402

from server.live_session import LiveSender  # noqa: E402


class ClosingWS:
    def __init__(self):
        self.sent, self.closed = [], False

    async def send_json(self, m):
        if self.closed:
            raise RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'")
        self.sent.append(m)

    async def close(self):
        self.closed = True


async def after_close():
    ws = ClosingWS()
    tx = LiveSender(ws)
    assert await tx.send({"type": "ping"})
    await tx.close()
    assert not await tx.send({"type": "stage", "stage": "done"}) and not await tx.send({"type": "ping"})
    ws2 = ClosingWS()
    ws2.closed = True  # closed under us
    tx2 = LiveSender(ws2)
    assert not await tx2.send({"type": "turn"}) and tx2.closed
    return ws.sent

assert asyncio.run(after_close()) == [{"type": "ping"}]
print("LV-send ok: nothing goes out after close")
print("ALL PASS")
