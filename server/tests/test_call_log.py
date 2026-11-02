"""The [model] line every real model call prints (server/call_log.py).

Run: python -m server.tests.test_call_log (runs on import).
"""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server import call_log  # noqa: E402

json_resp = httpx.Response(200, json={"usage": {"input_tokens": 3409, "output_tokens": 81}})
stream_resp = httpx.Response(200, text="data: {}\n", headers={"content-type": "text/event-stream"})

assert call_log.line("muse-spark-1.3", "import", 4210, 200, call_log.usage_of(json_resp)) == \
    "[model] muse-spark-1.3 · import · 4210 ms · 200 · in 3409 / out 81 tok"
assert call_log.usage_of(stream_resp) == {}
assert call_log.line("sam-3.1", "find pipette tip", 812, 200) == "[model] sam-3.1 · find pipette tip · 812 ms · 200"
assert call_log.line("", "spark", 5, 500, {"prompt_tokens": 9}) == "[model] ? · spark · 5 ms · 500 · in 9 / out ? tok"
print("ALL PASS")
