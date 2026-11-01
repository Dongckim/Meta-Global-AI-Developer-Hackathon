"""The latency page's splitting and its one-at-a-time rule (no SAM).

Run: python -m server.tests.test_sam_latency (runs on import).
"""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import lab_sam_latency as L  # noqa: E402


def reply(request: httpx.Request) -> httpx.Response:
    if "bad" in str(request.url):
        return httpx.Response(500, text="no")
    return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"x"}\n\ndata: [DONE]\n')


got = L.timed(httpx.Client(transport=httpx.MockTransport(reply)), "http://sam/responses", json={})
assert 'output_text.delta' in got["body"]
assert abs(got["send_s"] + got["wait_s"] + got["read_s"] - got["total_s"]) < 1e-6
try:
    L.timed(httpx.Client(transport=httpx.MockTransport(reply)), "http://sam/bad")
    raise SystemExit("an error status must fail the measurement")
except RuntimeError as e:
    assert "500" in str(e)

row = {"send_s": 1.0, "wait_s": 2.0, "read_s": 0.5, "total_s": 3.5}
slow = {**row, "total_s": 9.0}
assert L.line("x", [row, row, slow], "; y") == "x: send 1.0 · wait 2.0 · read 0.5 · total 3.5 s (slowest 9.0); y"
assert L.line("x", [row]) == "x: send 1.0 · wait 2.0 · read 0.5 · total 3.5 s"

c = TestClient(api.app)
release, entered = threading.Event(), threading.Event()


def fake(say):
    entered.set()
    release.wait(5)
    raise RuntimeError("boom")


L._measure = fake
assert c.post("/lab/sam-wells/latency").status_code == 202
assert entered.wait(5)
assert c.post("/lab/sam-wells/latency").status_code == 409
release.set()
for _ in range(200):
    if not c.get("/lab/sam-wells/latency").json()["running"]:
        break
    threading.Event().wait(.01)
st = c.get("/lab/sam-wells/latency").json()
assert not st["running"] and st["error"] == "boom"
print("ALL PASS")
