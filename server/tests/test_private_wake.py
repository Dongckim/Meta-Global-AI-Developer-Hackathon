"""Waking the private model: states, the wait screen, turns held for it.

Run: python -m server.tests.test_private_wake (runs on import).
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import server.api as api  # noqa: E402
from server import private_wake as pw, tone  # noqa: E402
from server.private import glimmer_modal  # noqa: E402
from server.store import RecordStore  # noqa: E402
from server.tone import section  # noqa: E402

LINES = section("private")["lines"]
SIZES = {glimmer_modal.MODEL_FILE: 19_654_000_000, glimmer_modal.MMPROJ_FILE: 1_400_000_000,
         glimmer_modal.DRAFT_FILE: 1_630_000_000, "other.gguf": 9_000_000_000}


def fresh():
    tone.set_run_mode(None)
    st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    st.spawn_wake = api._spawn_wake
    st.r["view"]["current"] = {"screen": "mode", "target": ""}
    st.r["view"]["stack"] = [{"screen": "home", "target": ""}]
    api.store = st
    api._SESSIONS.clear()
    return st, api.get_session("wake")


def tap(st, session, name, **args):
    return st.apply_calls([{"name": name, "args": args}], "tap", "", "tap", session)


def screen(st):
    return st.r["view"]["current"]["screen"]


def posts(*results):
    """_wake_post stand-in: each call takes the next result (an Exception
    raises, an Event is waited for)."""
    calls = []

    async def post():
        calls.append(1)
        r = results[min(len(calls), len(results)) - 1]
        if isinstance(r, asyncio.Event):
            await r.wait()
        elif isinstance(r, Exception):
            raise r
    return post, calls


async def size():
    return SIZES


async def settle():
    while api._wake_tasks:
        await asyncio.gather(*list(api._wake_tasks), return_exceptions=True)


api._weights_sizes = size
api.WAKE_TICK_S = 0.01
stages = []
real_stage = api._stage
api._stage = lambda session, stage, **d: stages.append((stage, d.get("say")))

# the schedule and the names are the deployed ones
assert (glimmer_modal.MODEL_FILE, glimmer_modal.MMPROJ_FILE, glimmer_modal.DRAFT_FILE) == tuple(section("private")["files"])
assert glimmer_modal.REPO == section("private")["repo"]
assert api.WAKE_TIMEOUT_S == 90
assert [x["at"] for x in LINES] == [0, 5, 10, 16, 22, 28, 40]

# file size: weights + mmproj only, from the bytes
assert pw.weights_gb(SIZES, section("private")["files"]) == 23

# line times
w = [(t, pw.line_at(t, 17)) for t in (0, 4.9, 5, 9.9, 10, 16, 22, 28, 39, 40, 59)]
assert [x[1] for x in w] == [LINES[i]["text"].format(gb=17) for i in (0, 0, 1, 1, 2, 3, 4, 5, 5, 6, 6)], w
assert pw.line_at(5, 17) == "Pipetting 17 GB of weights onto the GPU…"
assert pw.line_at(5, None) == LINES[0]["text"], "no size: the line before stays"


async def warming_to_ready():
    st, s = fresh()
    gate = asyncio.Event()
    api._wake_post, calls = posts(gate)
    tap(st, s, "choose_mode", mode="private")
    assert tone.run_mode() == "private"
    snap = st.snapshot()
    assert screen(st) == "private_wait" and st.r["view"]["current"]["target"] == "import_choice"
    assert snap["private"]["state"] == "warming" and snap["private"]["elapsed_s"] == 0
    assert snap["private"]["line"] == LINES[0]["text"]
    assert snap["header"]["mode"] == {"id": "private", "label": "Private"}
    v0 = st.r["version"]
    await asyncio.sleep(0.05)
    assert st.r["version"] > v0, "the seconds reach the app"
    gate.set()
    await settle()
    assert st.snapshot()["private"]["state"] == "ready" and screen(st) == "import_choice"
    assert [x["screen"] for x in st.r["view"]["stack"]] == ["home", "mode"]
    assert len(calls) == 1
    # already awake: no wait, no second request
    st.r["view"]["current"] = {"screen": "mode", "target": ""}
    st.r["view"]["stack"] = [{"screen": "home", "target": ""}]
    tap(st, s, "choose_mode", mode="private")
    assert screen(st) == "import_choice" and len(calls) == 1 and st.wake.state == "ready"


async def retry_then_ready():
    st, s = fresh()
    api._wake_post, calls = posts(RuntimeError("500"), None)
    tap(st, s, "choose_mode", mode="private")
    await settle()
    assert len(calls) == 2 and st.wake.state == "ready" and screen(st) == "import_choice"


async def retry_fails_then_try_again():
    st, s = fresh()
    api._wake_post, calls = posts(RuntimeError("500"), RuntimeError("500"), None)
    tap(st, s, "choose_mode", mode="private")
    await settle()
    assert len(calls) == 2 and st.wake.state == "failed" and screen(st) == "private_wait"
    snap = st.snapshot()
    assert snap["private"]["line"] == "The private model didn't wake up."
    assert snap["view"]["slots"]["wake"] == "failed"
    await api.run_utterance(None, "k", "try again", s, "input")
    assert st.wake.state == "warming"
    await settle()
    assert st.wake.state == "ready" and screen(st) == "import_choice" and len(calls) == 3


async def failed_at_timeout():
    st, s = fresh()
    gate = asyncio.Event()
    api._wake_post, calls = posts(gate)
    api.WAKE_TIMEOUT_S = 0.05
    tap(st, s, "choose_mode", mode="private")
    await settle()
    api.WAKE_TIMEOUT_S = 90
    assert st.wake.state == "failed" and len(calls) == 1, "the timeout covers the retry too"


async def use_full_mode():
    st, s = fresh()
    api._wake_post, _ = posts(RuntimeError("500"))
    tap(st, s, "choose_mode", mode="private")
    await settle()
    s["wake_pending"] = ("what is on C6", "input")
    out = await api.run_utterance(None, "k", "use full mode", s, "input")
    assert out[4] == "grammar" and tone.run_mode() == "full" and st.wake.state == "idle"
    assert screen(st) == "import_choice" and "wake_pending" not in s, "private words don't go to Full"
    assert st.snapshot()["private"] is None


async def grammar_while_waiting():
    st, s = fresh()
    gate = asyncio.Event()
    api._wake_post, _ = posts(gate)
    for said, gone in (("back", "mode"), ("full mode", "import_choice")):
        st, s = fresh()
        tap(st, s, "choose_mode", mode="private")
        out = await api.run_utterance(None, "k", said, s, "input")
        assert out[4] == "grammar" and screen(st) == gone, (said, out, screen(st))
    # a wake-up left behind by "full mode" lands nowhere
    gate.set()
    await settle()
    assert st.wake.state == "idle" and screen(st) == "import_choice"


async def free_speech_runs_after_ready():
    st, s = fresh()
    st.start_demo_run()
    st.r["view"]["current"] = {"screen": "main", "target": ""}
    st.r["view"]["stack"] = []
    tone.set_run_mode("private")
    heard = []

    async def model(client, key, text, state, session, **k):
        heard.append(text)
        return [], "C6 is planned.", 0, False, {}
    real, api._call_spark = api._call_spark, model
    real_key, api._key = api._key, lambda: "k"
    gate = asyncio.Event()
    api._wake_post, _ = posts(gate)
    stages.clear()
    out = await api.run_utterance(None, "k", "tell me a story about C6", s, "input")
    assert out[4] == "wait" and out[2] == "" and heard == [] and screen(st) == "private_wait"
    assert st.r["view"]["current"]["target"] == "main"
    await asyncio.sleep(0.05)
    gate.set()
    await settle()
    api._call_spark, api._key = real, real_key
    assert heard == ["tell me a story about C6"] and screen(st) == "main"
    assert ("awake", None) not in stages
    said = [x for x in stages if x[0] == "answer"]
    assert said and said[0][1], stages


async def milo_speaks_once():
    st, s = fresh()
    gate = asyncio.Event()
    api._wake_post, _ = posts(gate)
    real_speak, api.WAKE_SPEAK_AFTER_S = api.WAKE_SPEAK_AFTER_S, 0.02
    stages.clear()
    tap(st, s, "choose_mode", mode="private")
    await asyncio.sleep(0.1)
    gate.set()
    await settle()
    api.WAKE_SPEAK_AFTER_S = real_speak
    assert [x for x in stages if x[0] in ("waking", "awake")] == [
        ("waking", section("private")["waking_line"]), ("awake", section("private")["awake_line"])], stages


async def asleep_again():
    st, s = fresh()
    now = [0.0]
    st.wake.clock = lambda: now[0]
    api._wake_post, calls = posts(None)
    tap(st, s, "choose_mode", mode="private")
    await settle()
    assert st.wake.fresh()
    now[0] = section("private")["ready_ttl_s"]
    assert not st.wake.fresh(), "the container sleeps after 120 s idle"


for case in (warming_to_ready, retry_then_ready, retry_fails_then_try_again, failed_at_timeout,
             use_full_mode, grammar_while_waiting, free_speech_runs_after_ready, milo_speaks_once,
             asleep_again):
    asyncio.run(case())
    print("ok", case.__name__)
tone.set_run_mode(None)
api._stage = real_stage

# the app's buttons: both words, and "Try again" only once failed
st, s = fresh()
assert [b["say"] for b in api.buttons_for("private_wait", {"wake": "warming"})] == ["Use Full mode", "Full mode"]
assert [b["say"] for b in api.buttons_for("private_wait", {"wake": "failed"})][:2] == ["Try again", "Use Full mode"]

# the chosen mode outlives a restart and a new record
st, s = fresh()
st.spawn_wake = lambda session, gen: None
tap(st, s, "choose_mode", mode="private")
tone.set_run_mode(None)
RecordStore(path=st.path).restore_mode()
assert tone.run_mode() == "private"
tone.set_run_mode(None)
print("test_private_wake OK")
