"""MODEL_MODE=recorded (server/recorded.py): no key, no outside call, no error a judge can reach.
The hand off runs for real here: the stand-in code goes through the local simulator and the verifier.

Run: python -m server.tests.test_recorded (runs on import; needs .venv-sim).
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MODEL_MODE"] = "recorded"
os.environ.pop("MODEL_API_KEY", None)
os.environ.pop("PRIVATE_LLM_URL", None)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402
from server.tone import say  # noqa: E402

KEY, WATCH, PRIVATE = say("needs_key"), say("recorded_watch"), say("recorded_private")


def said(c, text):
    r = c.post("/input", json={"text": text, "session": "rec"})
    assert r.status_code == 200, (text, r.status_code, r.text)
    return r.json()


def tap(c, action, **args):
    r = c.post("/input", json={"action": {"id": action, "args": args}, "session": "rec"})
    assert r.status_code == 200, (action, r.text)
    return r.json()


api.os.environ.pop("MODEL_API_KEY", None)
api.store = st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
spawned = []
st.spawn_handoff = spawned.append

with TestClient(api.app) as c:
    # RC1. Outside calls fail, at the transport.
    for call in (lambda: httpx.get("https://api.meta.ai/v1/models"),):
        try:
            call()
        except httpx.ConnectError as e:
            assert "recorded" in str(e), e
        else:
            raise AssertionError("an outside call went through")
    print("RC1 ok: no outside calls")

    # RC2. The demo protocol is there without an import, and it starts.
    st.start_demo_run()
    print("RC2 ok: the seeded protocol runs")

    # RC3. What needs a model says so; the run goes on.
    assert said(c, "B2 to B4 done")["route"] == "grammar"
    assert said(c, "what is DMSO used for?")["reply"] == KEY
    assert said(c, "search the web for DMSO")["reply"] == KEY
    assert said(c, "what does this label say")["reply"] == KEY
    assert said(c, "watching")["reply"] == WATCH and not st.r.get("vision", {}).get("on"), st.r.get("vision")
    print("RC3 ok: chat, search and a photo question say they need a key")

    # RC4. An import doesn't fail.
    r = c.post("/protocols/import", data={"text": "Add 10 uL to C2.", "session": "rec"})
    assert r.status_code == 200 and r.json()["reply"] == KEY and r.json()["draft"] is None, r.text
    print("RC4 ok: an import says it needs a key")

    # RC5. The live socket says so too and closes.
    with c.websocket_connect("/voice/live") as ws:
        ws.send_json({"session": "rec"})
        assert ws.receive_json() == {"type": "error", "message": KEY}
    print("RC5 ok: the mic's socket says it needs a key")

    # RC6. Private stays closed; the Mode screen stays.
    st._push_view("mode", "")
    assert tap(c, "choose_mode", mode="private")["reply"] == PRIVATE
    assert st.r["view"]["current"]["screen"] == "mode", st.r["view"]
    tap(c, "back")
    print("RC6 ok: Private says where to see it")

    # RC7. The whole run, then the hand off through the real simulator and verifier.
    for _ in range(4):
        j = said(c, "next step")
        if j["cards"]:
            said(c, "mark them")
    assert st.r["step_index"] == 4, st.r["step_index"]
    if st.r["cards"]:
        said(c, "yes")
    j = said(c, "finish")
    if j["cards"]:
        said(c, "yes")
    assert st.r.get("run") is None and st.r["view"]["current"]["screen"] == "final_protocol", st.r["view"]
    run_id = st.r["view"]["current"]["target"]
    said(c, "review changes")
    for _ in range(8):
        if st.r["view"]["current"]["screen"] == "final_protocol":
            break
        said(c, "next")
    tap(c, "approve_handoff")
    assert spawned == [run_id], spawned
    asyncio.run(api._handoff_task(run_id))
    h = c.get("/record").json()["handoff"]
    assert h["stage"] == "passed", h
    assert h["check"]["ok"], h["check"]
    assert "protocol.comment" in h["code"] and h["csv"], h
    print(f"RC7 ok: the stand-in code passed the simulator and the verifier ({run_id})")

print("ALL PASS")
