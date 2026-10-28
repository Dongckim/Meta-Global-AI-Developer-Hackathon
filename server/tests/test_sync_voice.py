"""Sync voice verbs: registry commands that must actually work.

"retry the glasses check" / "skip the glasses check" are registered in
shared/schemas/screens.json, so the server must handle them (grammar,
no model call) and the app must be able to act (response signals).

Run: python -m unittest server.tests.test_sync_voice (runs on import).
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
from server.grammar import match_grammar  # noqa: E402
from server.store import RecordStore  # noqa: E402
from server.tone import variants  # noqa: E402

api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
api.store.start_demo_run()
api._SESSIONS.clear()

calls = []


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        calls.append((url, kw))
        raise AssertionError(f"unexpected model call: {url}")


with patch("httpx.AsyncClient", FakeClient):
    c = TestClient(api.app)

    # V1. retry routes via grammar, signals the app, changes nothing.
    before = json.loads(json.dumps(api.store.r["done"]))
    r = c.post("/input", json={"text": "retry the glasses check",
                               "session": "sync1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["route"] == "grammar", j
    assert j["tool_calls"] == [{"name": "retry_sync", "args": {}}], j
    assert j.get("sync_retry") is True, j
    assert "setup_done" not in j, j
    assert j["reply"] in variants("sync_retry"), j
    assert j["cards"] == [], j
    assert calls == [], "grammar must not call the model"
    assert api.store.r["done"] == before, api.store.r["done"]
    assert api.store.r["excluded"] == {}, api.store.r["excluded"]
    print("V1 ok: retry the glasses check")

    # V2. skip records the session, signals setup_done, is idempotent.
    r = c.post("/input", json={"text": "skip the glasses check",
                               "session": "sync1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["route"] == "grammar", j
    assert j["tool_calls"] == [{"name": "skip_sync", "args": {}}], j
    assert j.get("setup_done") is True, j
    assert j["reply"] in variants("sync_skip"), j
    assert api.get_session("sync1").get("setup_done") is True
    r2 = c.post("/input", json={"text": "skip the glasses check",
                                "session": "sync1"})
    j2 = r2.json()
    assert j2.get("setup_done") is True and j2["cards"] == [], j2
    assert calls == [], "grammar must not call the model"
    print("V2 ok: skip the glasses check + idempotent")

    # V3. grammar is strict: bare/ambiguous forms fall through to Spark.
    assert match_grammar("retry", {}) is None
    assert match_grammar("skip", {}) is None
    assert match_grammar("skip the timer", {}) is None
    assert match_grammar("retry the step", {}) is None
    g = match_grammar("retry the sync", {})
    assert g is not None and g[0][0]["name"] == "retry_sync", g
    g = match_grammar("skip glasses", {})
    assert g is not None and g[0][0]["name"] == "skip_sync", g
    g = match_grammar("retry the GLASSES CHECK!", {})
    assert g is not None and g[0][0]["name"] == "retry_sync", g
    print("V3 ok: strict grammar")

    # V4. model path can reach the actions; answer_card still pinned out.
    enum = None
    for t in api.TOOLS:
        if t.get("name") == "perform":
            enum = t["parameters"]["properties"]["action"]["enum"]
    assert enum is not None
    assert "retry_sync" in enum and "skip_sync" in enum, enum
    assert "answer_card" not in enum, enum
    print("V4 ok: perform enum")

    # V5. user-visible text never names a tool.
    for key in ("sync_retry", "sync_skip"):
        for t in variants(key):
            assert "_" not in t, t
            assert "retry_sync" not in t and "skip_sync" not in t, t
    print("V5 ok: human verbs")

print("ALL PASS")
