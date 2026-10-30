"""Glimmer's public guard: only chat/completions, capped, rate limited.

Run: python -m server.tests.test_private_guard (runs on import).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.private import glimmer_modal as g  # noqa: E402

URL = "/v1/chat/completions"
HELLO = [{"role": "user", "content": "hi"}]
seen = []


def upstream(request):
    seen.append(json.loads(request.content))
    return httpx.Response(200, headers={"content-type": "application/json"},
                          stream=httpx.ByteStream(b'{"ok": true}'))


def fresh():
    seen.clear()
    return TestClient(g.make_guard(httpx.MockTransport(upstream)))


def image(url):
    return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]


# allowed keys pass, the rest drop, tokens capped
c = fresh()
r = c.post(URL, json={"messages": HELLO, "max_tokens": 999999, "tools": [],
                      "n_predict": 999999, "n": 8, "reasoning_effort": "low"})
assert r.status_code == 200 and r.json() == {"ok": True}
assert seen == [{"messages": HELLO, "max_tokens": g.MAX_TOKENS, "tools": [],
                 "reasoning_effort": "low"}], seen

# max_tokens is always set, whatever was sent
for given, sent in [(None, g.MAX_TOKENS), (0, g.MAX_TOKENS), (-1, g.MAX_TOKENS),
                    ("9", g.MAX_TOKENS), (True, g.MAX_TOKENS), (50, 50)]:
    c = fresh()
    c.post(URL, json={"messages": HELLO, "max_tokens": given})
    assert seen[-1]["max_tokens"] == sent, given

# every other endpoint is closed
c = fresh()
for method, path in [("get", "/health"), ("get", "/props"), ("post", "/completion"),
                     ("post", "/v1/embeddings"), ("get", "/docs"), ("get", "/")]:
    assert getattr(c, method)(path).status_code == 404, path
assert seen == []

# refused before the model
for body, code in [
    ({"messages": []}, 400),
    ({"messages": "hi"}, 400),
    ({"messages": HELLO * (g.MAX_MESSAGES + 1)}, 413),
    ({"messages": [{"role": "user", "content": "x" * g.MAX_TEXT_CHARS}]}, 413),
    ({"messages": HELLO, "tools": [{"description": "x" * g.MAX_TEXT_CHARS}]}, 413),
    ({"messages": image("http://169.254.169.254/latest")}, 400),
    ({"messages": image("file:///etc/passwd")}, 400),
    ({"messages": image("data:image/png;base64,AA") * (g.MAX_IMAGES + 1)}, 413),
    ({"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]}, 400),
]:
    c = fresh()
    assert c.post(URL, json=body).status_code == code, str(body)[:80]
    assert seen == []

c = fresh()
assert c.post(URL, content=b"nope").status_code == 400
assert c.post(URL, content=b"x" * (g.MAX_BODY_BYTES + 1)).status_code == 413
assert seen == []

# an image doesn't count as text
url = "data:image/jpeg;base64," + "A" * (g.MAX_TEXT_CHARS * 2)
assert fresh().post(URL, json={"messages": image(url)}).status_code == 200

# the route limits
c = fresh()
for _ in range(g.RATE_PER_MIN):
    assert c.post(URL, json={"messages": HELLO}).status_code == 200
r = c.post(URL, json={"messages": HELLO})
assert r.status_code == 429 and r.headers["retry-after"] == "60"
assert len(seen) == g.RATE_PER_MIN

# per IP, sliding minute
now = [0.0]
limit = g.RateLimit(per_min=2, clock=lambda: now[0])
assert limit.allow("a") and limit.allow("a") and not limit.allow("a")
assert limit.allow("b")
now[0] = 59.9
assert not limit.allow("a")
now[0] = 60.0
assert limit.allow("a")

print("test_private_guard OK")
