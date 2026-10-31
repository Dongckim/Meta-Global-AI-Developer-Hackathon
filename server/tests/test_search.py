"""Web search (server/search.py): one separate Spark call with the web_search
tool, chosen by the model as perform web_search{query}; no cited source, no
answer; lab details never leave in the query; a mode setting decides whether a
card asks first; every sent search is kept (⓪c list, ⑧ answer).

Run: python -m server.tests.test_search (runs on import).
"""

import asyncio
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
from server import labels as L  # noqa: E402
from server import search  # noqa: E402
from server.store import RecordStore  # noqa: E402


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


sent: list = []


class ScriptClient:
    queue: list = []
    delay = 0.0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        sent.append(kw.get("json") or {})
        if any(t.get("type") == "web_search" for t in (kw.get("json") or {}).get("tools", [])):
            await asyncio.sleep(ScriptClient.delay)
        return FakeResp(ScriptClient.queue.pop(0))


def perform(action, cid, **args):
    return {"id": cid, "output": [{"type": "function_call", "name": "perform", "call_id": cid,
                                   "status": "completed", "arguments": json.dumps({"action": action, **args})}]}


def answer(text, cites):
    return {"id": "s", "output": [
        {"type": "web_search_call", "id": "ws1", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": [
            {"type": "url_citation", "url": u, "title": t, "start_index": 0, "end_index": 5} for t, u in cites]}]}]}


GOOD = answer("**Keep DMSO at or below 0.5% for HeLa cells.** HeLa cells tolerate up to about 0.5% DMSO. "
              "Above 1% growth drops. Use 0.1% to be safe.",
              [("DMSO tolerance", "https://www.example.org/dmso"), ("DMSO tolerance", "https://www.example.org/dmso"),
               ("Cell culture FAQ", "https://lab.example.com/faq")])


def tidy_out(short, detail, urls=()):
    return {"id": "t", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({"answer_short": short, "detail": detail,
                                                    "sources": list(urls)})}]}]}


def pages_only(text, pages):
    """A search that cited nothing but returned pages."""
    return {"id": "p", "output": [
        {"type": "web_search_call", "id": "ws", "status": "completed",
         "results": [{"type": "text_result", "title": t, "url": u, "snippet": ""} for t, u in pages]},
        {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": []}]}]}


TIDY = tidy_out("Keep it under 0.5%.", "HeLa cells tolerate about 0.5% DMSO. Above 1% growth drops.",
                ["https://lab.example.com/faq", "https://invented.example/x"])

# S1. Citations are the sources (deduped, domains); the tidy lines stand only
# when every number they state is in the cited text; no citation, no answer.
got = search.found(GOOD)
assert got["basis"] == "cited" and [(x["domain"], x["title"]) for x in got["sources"]] == [
    ("example.org", "DMSO tolerance"), ("lab.example.com", "Cell culture FAQ")], got
r = search.tidy(search.output_json(TIDY), got)
assert (r["answer_short"], r["detail"]) == ("Keep it under 0.5%.",
                                             "HeLa cells tolerate about 0.5% DMSO. Above 1% growth drops."), r
assert [x["url"] for x in r["sources"]] == ["https://lab.example.com/faq"], "only urls the search gave"
r = search.tidy({"answer_short": "Keep it under 2%.", "detail": "x", "sources": []}, got)  # 2 isn't in the text
assert r["answer_short"] == "Keep DMSO at or below 0.5% for HeLa" and "2%" not in r["detail"], r
pg = search.found(pages_only("Usually 0.5%.", [("Page A", "https://a.org/1"), ("Page A", "https://a.org/1")]))
assert pg["basis"] == "pages" and len(pg["sources"]) == 1, pg
assert search.found(answer("Use 0.5%.", [])) is None, "no citation and no pages, no answer"
body = search.build_summary_body("q", got, "m")
assert "tools" not in body and body["text"]["format"]["type"] == "json_schema", "the tidy call never searches"
assert got["text"] in body["input"] and "results" not in json.dumps(body), "answer text + source list only"
print("S1 ok: cited first, else pages searched; urls and numbers checked")

# S2. Lab details never leave: wells, run names and note text come out of the query.
assert search.clean_query("max DMSO for HeLa in C3 to C5 of run 2?", []) == "max DMSO for HeLa?"
assert search.clean_query("is drug B stable, bubbles near the edge popped", ["bubbles near the edge popped"]) \
    == "is drug B stable"
assert search.clean_query("C3?", []) == ""
assert "A549" in search.clean_query("A549 doubling time", [])
print("S2 ok: queries are cleaned before they leave")


def fresh():
    st = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    st.start_demo_run()
    api.store = st
    api._SESSIONS.clear()
    sent.clear()
    return st


c = TestClient(api.app)
events: list = []

# S3. Chosen by the model -> one separate search call (web_search tool only
# there), the answer opens ⑧, the record keeps it.
st = fresh()
st.subscribe(events.append)
ScriptClient.queue = [perform("web_search", "c1", query="max DMSO for HeLa cells in C3"), GOOD, TIDY]
with patch("httpx.AsyncClient", ScriptClient):
    j = c.post("/input", json={"text": "Max DMSO for HeLa cells?", "session": "s"}).json()
assert not any(t.get("type") == "web_search" for t in sent[0].get("tools", [])), "chat call has no search tool"
assert sent[1]["tools"] == [{"type": "web_search", "search_context_size": "low"}], sent[1]
assert "C3" not in json.dumps(sent[1]["input"]), "the well never leaves"
time_ = L.search_label(st.answered_searches()[0])["time"]
when = L.search_label(st.answered_searches()[0])["searched"]
assert when.startswith("today at ") and when[-2:] in ("AM", "PM"), when  # 12-hour, for people
# answer, then where from, then when
assert j["reply"] == f"Keep it under 0.5%. Source: lab.example.com, searched {when}.", j
assert "at most" not in j["reply"] and api.speak_text(j["reply"]).startswith("Keep it under 0.5%.")
assert [x["url"] for x in j["sources"]] == ["https://lab.example.com/faq"], j  # the tidy's pick
assert st.r["view"]["current"]["screen"] == "web_answer", j
stages = [(e["stage"], e.get("say")) for e in events if e.get("kind") == "stage"]
assert stages == [("heard", None), ("thinking", None), ("searching", "Looking it up."),
                  ("summarizing", None), ("done", None)], stages
snaps = [e["view"]["slots"].get("status") for e in events if e.get("kind") == "snapshot"
         and e["view"]["current"]["screen"] == "web_answer"]
assert snaps[:1] == ["pending"] and snaps[-1] == "ok", snaps  # ⑧ opens first, then fills
rec = st.searches()[0]
assert (rec["question"], rec["query"], rec["run_id"], rec["status"]) == (
    "Max DMSO for HeLa cells?", "max DMSO for HeLa cells", st.r["run"]["run_id"], "ok"), rec
print("S3 ok: one search call, answer opens, record kept")

# S4. No citation -> "I couldn't find a solid source."; still counted as sent.
ScriptClient.queue = [perform("web_search", "c2", query="is drug B stable at room temperature"),
                      answer("It is stable.", [])]  # no citation and no pages
with patch("httpx.AsyncClient", ScriptClient):
    j = c.post("/input", json={"text": "Is Drug B stable at room temp?", "session": "s"}).json()
assert j["reply"] == "I couldn't find a solid source.", j
lst = c.get("/searches").json()
assert ([x["status"] for x in lst["searches"]], lst["sent"]) == (["no_source", "ok"], 2), lst
print("S4 ok: no source, no answer")

# S5. Over 15 s -> "That took too long. Try again?".
ScriptClient.queue = [perform("web_search", "c3", query="MTT incubation time"), GOOD, TIDY]
ScriptClient.delay = 0.3
with patch("httpx.AsyncClient", ScriptClient), patch.object(search, "SEARCH_TIMEOUT_S", 0.05):
    j = c.post("/input", json={"text": "How long is MTT incubation?", "session": "s"}).json()
ScriptClient.delay = 0.0
assert j["reply"] == "That took too long. Try again?" and st.searches()[0]["status"] == "timeout", j
print("S5 ok: slow search times out")

# S6. A mode that asks first (setting, not a branch): card, then yes searches.
st = fresh()
with patch.dict(os.environ, {"RUN_MODE": "private", "PRIVATE_LLM_URL": "http://glimmer.test"}):
    api.store.wake.begin()
    api.store.wake.finish(True)  # the private model is up
    ScriptClient.queue = [perform("web_search", "c4", query="max DMSO for HeLa cells")]
    with patch("httpx.AsyncClient", ScriptClient), patch.object(api, "from_chat", lambda p: p):
        j = c.post("/input", json={"text": "Max DMSO for HeLa cells?", "session": "s"}).json()
    assert j["reply"].endswith("Search anyway?") and len(sent) == 1, j
    card = j["cards"][0]
    assert card["title"] == "Search the web?" and "Only this question" in card["detail"], card
    ScriptClient.queue = [GOOD, TIDY, {"choices": [{"message": {"content": "NONE"}}]}]  # Glimmer: nothing to tie
    with patch("httpx.AsyncClient", ScriptClient):
        j = c.post("/input", json={"text": "yes", "session": "s"}).json()
    assert ". Source: " in j["reply"] and len(st.answered_searches()) == 1, j
print("S6 ok: confirm card from the mode setting, yes searches")

# S7. Saying which one: "the DMSO one", "Oct 27, 16:05", "the latest search";
# ambiguous -> ask between two. Open source -> the app's browser sheet.
docs = [{"id": "a", "ts": 1793136300.0, "question": "Max DMSO for HeLa cells?", "answer_short": "≤0.5%"},
        {"id": "b", "ts": 1793140000.0, "question": "Is Drug B stable at room temp?", "answer_short": "Yes"},
        {"id": "c", "ts": 1793150000.0, "question": "DMSO in PBS freezing?", "answer_short": "No"}]
import datetime as dt  # noqa: E402

t_a = dt.datetime.fromtimestamp(docs[0]["ts"])
assert L.select_search("the latest search", docs)[0]["id"] == "c"
assert L.select_search("the drug B one", docs)[0]["id"] == "b"
assert L.select_search(f"{t_a.strftime('%b')} {t_a.day}, {t_a.strftime('%H:%M')}", docs)[0]["id"] == "a"
d, cands = L.select_search("the DMSO one", docs)
assert d is None and [x["id"] for x in cands] == ["c", "a"], cands
st = fresh()
st.add_search(search.record("Max DMSO for HeLa cells?", "max DMSO HeLa", search.tidy(search.output_json(tidy_out("Under 0.5%.", "d.", ["https://www.example.org/dmso", "https://lab.example.com/faq"])), search.found(GOOD)),
                            "ok", None, 9))
j = c.post("/input", json={"action": {"id": "open", "args": {"target": "search the DMSO one"}}, "session": "s"}).json()
assert st.r["view"]["current"]["screen"] == "web_answer", j
sid_ = st.r["view"]["current"]["target"]
j = c.post("/input", json={"action": {"id": "open_source", "args": {"n": "2"}}, "session": "s"}).json()
assert st.r["view"]["current"] == {"screen": "web_source", "target": f"{sid_}:2"}, j  # inside the app
st.r["view"]["current"] = {"screen": "web_searches", "target": ""}
assert st.snapshot()["view"]["prompt"] == "1 web search. Which one?"
print("S7 ok: open a search by what was said; sources open")

# S8. Pages searched (no citation) is said and kept as such, never as a source.
st = fresh()
ScriptClient.queue = [perform("web_search", "c5", query="PBS shelf life after opening"),
                      pages_only("About a month at room temperature.", [("PBS FAQ", "https://pbs.example/faq")]),
                      tidy_out("About a month once opened.", "Keep it sterile.", ["https://pbs.example/faq"])]
with patch("httpx.AsyncClient", ScriptClient):
    j = c.post("/input", json={"text": "How long is PBS good for after opening?", "session": "s"}).json()
assert j["reply"].startswith("About a month once opened. Based on pages from pbs.example, searched today at "), j
assert "Source" not in j["reply"], "no citation: never called a source"
assert st.searches()[0]["basis"] == "pages", st.searches()[0]
print("S8 ok: pages searched, said as pages searched")

# S9. Slow: "Still looking…" once after 6 s; a failed search says why on ⑧
# with Try again (a tap runs the same search path).
st = fresh()
events.clear()
st.subscribe(events.append)
ScriptClient.queue = [perform("web_search", "c6", query="MTT incubation time"), GOOD, TIDY]
ScriptClient.delay = 0.3
with patch("httpx.AsyncClient", ScriptClient), patch.object(api, "STILL_LOOKING_S", 0.1), \
        patch.object(search, "SEARCH_TIMEOUT_S", 0.2):
    j = c.post("/input", json={"text": "How long is MTT incubation?", "session": "s"}).json()
ScriptClient.delay = 0.0
says = [e.get("say") for e in events if e.get("kind") == "stage" and e.get("say")]
assert says == ["Looking it up.", "Still looking…"], says
failed = [e for e in events if e.get("kind") == "stage" and e["stage"] == "failed"]
assert failed and failed[0]["reason"] == "timeout", failed
sid = st.r["view"]["current"]["target"]
assert st.view_slots("web_answer", sid)["status"] == "failed"
from server.screens import buttons_for  # noqa: E402
assert [b["say"] for b in buttons_for("web_answer", st.view_slots("web_answer", sid))] == ["Try again", "Back"]
ScriptClient.queue = [GOOD, TIDY]
with patch("httpx.AsyncClient", ScriptClient):
    j = c.post("/input", json={"action": {"id": "web_search", "args": {"query": "MTT incubation time"}},
                               "session": "s"}).json()
assert ". Source: " in j["reply"] and st.searches()[0]["status"] == "ok", j
print("S9 ok: still looking once, failed says why, Try again searches")

# S10. "stop" while a search runs cancels the search, not the session.
st = fresh()


async def stop_mid_search():
    sess = api.get_session("stop")
    async with ScriptClient() as client:
        run = asyncio.ensure_future(api._run_search(client, "k", "q?", "MTT incubation time", sess))
        await asyncio.sleep(0.05)
        assert api._stop_search(sess, "stop") and not api._stop_search(sess, "stop")
        return await run

ScriptClient.queue = [GOOD, TIDY]
ScriptClient.delay = 1.0
reply, srcs = asyncio.run(stop_mid_search())
ScriptClient.delay = 0.0
assert reply == "Okay, I stopped looking." and st.searches()[0]["status"] == "canceled", (reply, st.searches()[0])
assert not api._stop_search(api.get_session("stop"), "stop"), "nothing to stop now"
print("S10 ok: stop cancels the search")

# S11. One place for stages: a live session's turns go on its socket only.
st = fresh()
events.clear()
st.subscribe(events.append)
sock: list = []
sess = api.get_session("live")
sess["stage_sink"] = sock.append
api._stage(sess, "thinking")
assert sock == [{"type": "stage", "stage": "thinking"}] and not events, (sock, events)
sess.pop("stage_sink")
api._stage(sess, "thinking")
assert events == [{"kind": "stage", "type": "stage", "stage": "thinking"}], events
print("S11 ok: stages ride the live socket, else /events")
print("ALL PASS")
