"""A question that needs a photo (server/look.py, POST /look): the grammar or
the model asks for a photo, the camera comes up ("Show me."), SAM 3.1 finds
the thing asked about, only its crop is kept and goes to Spark, which reads
it and answers on ⑧ ("Read from photo"), or the web search path runs on what
was read. SAM and Spark are fakes here.

Run: python -m server.tests.test_look (runs on import).
"""

import base64
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
from PIL import Image  # noqa: E402

import server.api as api  # noqa: E402
from server import look  # noqa: E402
from server.screens import look_call  # noqa: E402
from server.store import RecordStore  # noqa: E402


class FakeResp:
    def __init__(self, payload=None, text=None):
        self._payload = payload
        self.status_code = 200
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


sent: list = []


class ScriptClient:
    queue: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        sent.append(kw.get("json") or {})
        nxt = ScriptClient.queue.pop(0)
        return nxt() if callable(nxt) else nxt  # a callable: something happens meanwhile, then its reply


def sam(*records):
    """A streamed SAM reply: one text delta with these records (box only; the mask can't decode)."""
    text = "<0f>" + ",".join(f"{i}<|box;x1={b[0]};y1={b[1]};x2={b[2]};y2={b[3]};w=400;h=300;c={c}|>"
                             f"<|mask;x=0;y=0;data=4,4,~zzzz|>" for i, (b, c) in enumerate(records)) if records else ""
    ev = [{"type": "response.output_text.delta", "delta": text}, {"type": "response.completed"}]
    return FakeResp(text="".join(f"data: {json.dumps(e)}\n\n" for e in ev))


def read(answer, detail="", read_text="DMSO 10 mM", needs_search=False, query=""):
    return FakeResp({"id": "r", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(
        {"read": read_text, "answer_short": answer, "detail": detail, "needs_search": needs_search,
         "query": query})}]}]})


def photo() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 200, 200)).save(out, "JPEG")
    return out.getvalue()


def fresh():
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()


c = TestClient(api.app)
said = lambda t: c.post("/input", json={"text": t, "session": "lk"}).json()  # noqa: E731
send = lambda sid, raw=None: c.post("/look", data={"search_id": sid, "session": "lk"},  # noqa: E731
                                    files={"file": ("p.jpg", raw or photo(), "image/jpeg")})
item = lambda sid: api.store.search_item(sid)  # noqa: E731

# LK1. The grammar asks for a photo when the question is about what's in
# front of the camera; "this" in a command doesn't; the target is what's named.
for q in ("what does this say?", "look at this tube", "is this tube the right one?", "What's on this label?",
          "which reagent is this?"):
    assert look_call(q) == {"name": "look", "args": {"question": q}}, q
for q in ("mark this done", "start this protocol", "this one", "next step", "C2 done"):
    assert look_call(q) is None, q
assert [look.target_of(q) for q in ("what does this say?", "is this tube right?", "read these bottles")] == \
    ["label", "tube", "bottle"]
print("LK1 ok: photo questions by grammar, target from the question")

# LK2. Full mode: the camera comes up on ⑧ ("Show me."), the record waits in looking.
with patch("httpx.AsyncClient", ScriptClient):
    fresh()
    j = said("what does this label say?")
    assert j["route"] == "grammar" and j["reply"] == "Show me.", j
    cam = [u for u in j.get("ui") or [] if u["action"] == "camera"]
    sid = cam[0]["search_id"]
    assert item(sid)["kind"] == "photo" and item(sid)["status"] == "looking" and item(sid)["target"] == "label"
    assert api.store.r["view"]["current"] == {"screen": "web_answer", "target": sid}
    slots = api.store.view_slots("web_answer", sid)
    assert slots["camera"] == "on" and slots["status"] == "pending", slots
    print("LK2 ok: the camera comes up, the question waits for its photo")

    # LK3. SAM finds nothing: the camera stays, Milo says try closer; no Spark call.
    sent.clear()
    ScriptClient.queue = [sam()]
    j = send(sid).json()
    assert j["ok"] is False and j["reply"] == "I can't find a label. Try again closer.", j
    assert item(sid)["status"] == "looking" and len(sent) == 1
    print("LK3 ok: not found -> try again closer, camera stays")

    # LK4. Found: only the best match's crop is kept and goes to Spark; the
    # answer is read from the photo (no search); outline over the crop.
    sent.clear()
    ScriptClient.queue = [sam(((10, 10, 60, 40), 0.4), ((100, 50, 300, 250), 0.9)), read("DMSO, 10 mM.", "Stock.")]
    j = send(sid).json()
    assert j["ok"] is True and j["reply"] == "DMSO, 10 mM. Read from photo.", j
    assert j["speak"] == "DMSO, 10 millimolar. Stock.", j["speak"]  # the answer, then its summary (units spoken)
    rec = item(sid)
    assert (rec["status"], rec["basis"], rec["answer_short"], rec["read"]) == ("ok", "photo", "DMSO, 10 mM.",
                                                                                 "DMSO 10 mM"), rec
    assert sent[0]["model"] == "sam-3.1" and sent[0]["input"][0]["content"][0]["text"] == "label"
    spark_img = sent[1]["input"][0]["content"][1]["image_url"]
    w, h = Image.open(io.BytesIO(base64.b64decode(spark_img.split(",", 1)[1]))).size
    assert (w, h) == (225, 225), "Spark gets the crop of the best match (box + 6%), never the photo"
    assert api.store.look_crop_path(sid).exists() and c.get(f"/looks/{sid}/crop").status_code == 200
    assert len(rec["outline"]) >= 4 and all(0 <= x <= 1 and 0 <= y <= 1 for x, y in rec["outline"]), rec["outline"]
    assert send(sid).status_code == 409, "an answered question takes no more photos"
    from server.screens import buttons_for  # noqa: E402
    says = [b["say"] for b in buttons_for("web_answer", api.store.view_slots("web_answer", sid))]
    assert says == ["Retake", "Back"], says  # read from the photo: no Open source
    assert item(sid)["clock"].endswith(("AM", "PM")), item(sid)
    assert rec["run_id"] == api.store.r["run"]["run_id"], "kept with the run"
    print("LK4 ok: SAM crop only to Spark, read from photo, outline, kept with the run")

    # LK5. Reading needs outside facts: the web search path runs on what was read.
    fresh()
    j = said("is this tube still good?")
    sid = j["ui"][0]["search_id"]
    GOOD = {"id": "s", "output": [  # a search that cites its source (server/search.py)
        {"type": "web_search_call", "id": "ws1", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": "Keep DMSO under 0.5%. Stock lasts a year.",
                                         "annotations": [{"type": "url_citation", "url": "https://lab.example.com/faq",
                                                          "title": "FAQ", "start_index": 0, "end_index": 5}]}]}]}
    TIDY = {"id": "t", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(
        {"answer_short": "Keep it under 0.5%.", "detail": "Stock lasts a year.",
         "sources": ["https://lab.example.com/faq"]})}]}]}
    ScriptClient.queue = [sam(((100, 50, 300, 250), 0.9)),
                          read("Check the date.", read_text="DMSO exp 2024", needs_search=True,
                               query="DMSO shelf life"), FakeResp(GOOD), FakeResp(TIDY)]
    j = send(sid).json()
    rec = item(sid)
    assert rec["target"] == "tube" and rec["basis"] == "cited" and rec["query"] == "DMSO shelf life", rec
    assert rec["answer_short"] == "Keep it under 0.5%." and rec["sources"], rec
    assert j["speak"] == "Keep it under 0.5%. Stock lasts a year.", j["speak"]
    print("LK5 ok: what was read goes to the web search when it must")

    # LK6. Private mode asks before a photo leaves; yes opens the camera.
    fresh()
    with patch.dict(os.environ, {"RUN_MODE": "private"}):
        j = said("what does this say?")
        assert [x["kind"] for x in j["cards"]] == ["confirm_photo"] and not j.get("ui"), j
        assert j["reply"] == "Send a photo to answer that?", j
        j = said("yes")
        recs = [d for d in api.store.searches() if d.get("kind") == "photo"]
        assert len(recs) == 1 and recs[0]["status"] == "looking", recs
    print("LK6 ok: Private asks first (confirm_photo)")

# LK7. A model can ask for a photo too (look{question}); a bad id is refused.
fresh()
r = api.store.apply_calls([{"name": "look", "args": {"question": "what's printed on it?"}, "from_model": True}],
                          "voice", "what's printed on it?", "spark", {})
assert r["messages"] == ["Show me."] and r["info"]["look"]["search_id"], r
assert send("nope").status_code == 404
calls = api._expand_perform([{"name": "perform", "args": {"action": "look", "query": "Is this the right device?"}}])
assert calls[0]["name"] == "look", "no question arg: never an ask (iPad: \"Which one?\")"
fresh()
r = api.store.apply_calls(calls, "voice", "Um is this right device for my protocol?", "spark", {})
assert r["messages"] == ["Show me."] and \
    api.store.search_item(r["info"]["look"]["search_id"])["question"] == "Um is this right device for my protocol?"
print("LK7 ok: the model's look, unknown ids refused")

# LK8. Retake (said or tapped) while the photo is read or after its answer:
# back to "Show me." (attempt moves on, the crop goes); a reply for the old
# photo, even one that arrives late, changes nothing.
with patch("httpx.AsyncClient", ScriptClient):
    fresh()
    sid = said("what does this say?")["ui"][0]["search_id"]
    ScriptClient.queue = [sam(((100, 50, 300, 250), 0.9)), read("DMSO, 10 mM.")]
    assert send(sid).json()["ok"] is True
    slots = api.store.view_slots("web_answer", sid)
    assert slots["retake"] == "on" and "camera" not in slots, slots
    j = said("take it again")
    assert j["reply"] == "Show me." and [u["action"] for u in j["ui"]] == ["camera"], j
    rec = item(sid)
    assert (rec["status"], rec["attempt"], rec["answer_short"]) == ("looking", 2, ""), rec
    assert not api.store.look_crop_path(sid).exists()
    j = c.post("/look", data={"search_id": sid, "session": "lk", "attempt": "1"},
               files={"file": ("p.jpg", photo(), "image/jpeg")}).json()
    assert j["stale"] is True and item(sid)["status"] == "looking", "an old photo's reply is ignored"

    # the late reply: Retake lands while SAM is still answering for attempt 2
    def retake_meanwhile():
        said("retake")
        return sam(((100, 50, 300, 250), 0.9))
    ScriptClient.queue = [retake_meanwhile]
    j = c.post("/look", data={"search_id": sid, "session": "lk", "attempt": "2"},
               files={"file": ("p.jpg", photo(), "image/jpeg")}).json()
    rec = item(sid)
    assert j["stale"] is True and (rec["status"], rec["attempt"], rec["outline"]) == ("looking", 3, []), (j, rec)
    assert not ScriptClient.queue and not api.store.look_crop_path(sid).exists(), "no read, no crop for it"
print("LK8 ok: retake drops the photo, late replies ignored")
# LK9. A photo that can't be read (the API fails, or the crop): back to
# looking, so Take it works again; "Show me." is said once (the reply, not
# the stage too).
with patch("httpx.AsyncClient", ScriptClient):
    fresh()
    stages = []
    with patch.object(api, "_stage", lambda s, st, **kw: stages.append((st, kw))):
        sid = said("what does this say?")["ui"][0]["search_id"]
        assert [kw.get("say") for st, kw in stages if st == "looking"] == [None], stages
        bad = FakeResp(text="boom")
        bad.status_code = 500
        ScriptClient.queue = [bad]
        assert send(sid).status_code >= 500
    assert item(sid)["status"] == "looking" and api.store.view_slots("web_answer", sid)["camera"] == "on"
    ScriptClient.queue = [sam(((100, 50, 300, 250), 0.9)), read("DMSO, 10 mM.")]
    assert send(sid).json()["ok"] is True
print("LK9 ok: a failed read brings the camera back; Show me said once")
print("ALL PASS")
