"""Glimmer on Modal vs Spark: cold start, voice turns, a label, the guard.

Same SYSTEM, TOOLS and bench state as a real turn, straight to each model
(grammar skipped), one round, streamed so first token is real.
Run from repo root (needs PRIVATE_LLM_URL and MODEL_API_KEY in .env):
    .venv/bin/python eval/private_glimmer.py cold [n]
    .venv/bin/python eval/private_glimmer.py turns
    .venv/bin/python eval/private_glimmer.py effort none
    .venv/bin/python eval/private_glimmer.py label
    .venv/bin/python eval/private_glimmer.py guard
Appends to eval/results/private_glimmer.md.
"""
import base64
import contextlib
import io
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

GLIMMER = os.environ["PRIVATE_LLM_URL"].rstrip("/") + "/v1/chat/completions"
SPARK = f"{api.BASE_URL}/responses"
RESULTS = Path("eval/results/private_glimmer.md")
WEB_LIMIT_S = 150
SCALEDOWN_WAIT_S = 200
#: (said, tool, perform action or None)
TURNS = [
    ("mark C2 and C3 done please", "perform", "mark_done"),
    ("note C4, slight edge effect", "perform", "add_note"),
    ("start the timer for C5, five minutes", "perform", "set_timer"),
    ("what is on C6", "read_record", None),
    ("how many wells have I finished", "read_record", None),
]
LABEL = ("Drug A", "10 mM", "4471", "2027-03")
LABEL_ASK = "Read this tube label. Give the reagent, concentration, lot and expiry."


def log(text: str) -> None:
    print(text, flush=True)
    with RESULTS.open("a") as f:
        f.write(text + "\n")


def chat_tools() -> list:
    return [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}}
            for t in api.TOOLS]


def bench_system(extra: str = "") -> str:
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    api.store.r["step_index"] = 2  # mid-run bench, as latency_rounds
    state = json.dumps(api.store.model_state(api.get_session("private-eval")), ensure_ascii=False)
    return extra + api.SYSTEM + "\n\n현재 상태(JSON):\n" + state


def glimmer_body(system: str, content, effort: str | None) -> dict:
    return {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "tools": chat_tools(), "max_tokens": api.MAX_OUTPUT_TOKENS, "stream": True,
            "stream_options": {"include_usage": True}} | ({"reasoning_effort": effort} if effort else {})


def stream_glimmer(client, body: dict) -> dict:
    t0 = time.perf_counter()
    first = visible = None
    text, reasoning, calls, usage, timings = "", "", {}, {}, {}
    with client.stream("POST", GLIMMER, json=body) as r:
        if r.status_code != 200:
            return {"error": f"{r.status_code} {r.read()[:300]!r}", "ms": 0}
        for line in r.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ev = json.loads(line[6:])
            usage = ev.get("usage") or usage
            timings = ev.get("timings") or timings
            for choice in ev.get("choices") or []:
                d = choice.get("delta") or {}
                reasoning += d.get("reasoning_content") or ""
                text += d.get("content") or ""
                for c in d.get("tool_calls") or []:
                    slot = calls.setdefault(c.get("index", 0), {"name": "", "arguments": ""})
                    f = c.get("function") or {}
                    slot["name"] += f.get("name") or ""
                    slot["arguments"] += f.get("arguments") or ""
                now = time.perf_counter()
                if first is None and (d.get("reasoning_content") or d.get("content") or d.get("tool_calls")):
                    first = now
                if visible is None and (d.get("content") or d.get("tool_calls")):
                    visible = now
    end = time.perf_counter()
    return {"first": _ms(t0, first), "visible": _ms(t0, visible), "ms": _ms(t0, end), "text": text,
            "calls": list(calls.values()), "reasoning_chars": len(reasoning),
            "out": usage.get("completion_tokens"), "in": usage.get("prompt_tokens"),
            "tps": timings.get("predicted_per_second")}


def stream_spark(client, key: str, system: str, content, tools: list = api.TOOLS) -> dict:
    body = {"model": api.SPARK_MODEL, "instructions": system, "input": content,
            "reasoning": {"effort": api.REASONING_EFFORT}, "max_output_tokens": api.MAX_OUTPUT_TOKENS,
            "tools": tools, "prompt_cache_key": api.CACHE_KEY, "stream": True}
    t0 = time.perf_counter()
    first = None
    done = {}
    with client.stream("POST", SPARK, json=body, headers={"Authorization": f"Bearer {key}"}) as r:
        if r.status_code != 200:
            return {"error": f"{r.status_code} {r.read()[:300]!r}", "ms": 0}
        for line in r.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ev = json.loads(line[6:])
            kind = ev.get("type", "")
            if first is None and kind.endswith(".delta"):
                first = time.perf_counter()
            if kind == "response.completed":
                done = ev["response"]
    end = time.perf_counter()
    out = done.get("output") or []
    calls = [{"name": o["name"], "arguments": o["arguments"]} for o in out if o.get("type") == "function_call"]
    text = "".join(p.get("text", "") for o in out if o.get("type") == "message" for p in o.get("content", []))
    usage = done.get("usage") or {}
    return {"first": _ms(t0, first), "visible": _ms(t0, first), "ms": _ms(t0, end), "text": text, "calls": calls,
            "reasoning_chars": 0, "out": usage.get("output_tokens"), "in": usage.get("input_tokens"), "tps": None}


def _ms(t0, t) -> int | None:
    return None if t is None else int((t - t0) * 1000)


def check_call(res: dict, tool: str, action: str | None) -> str:
    """ok, or what's wrong: right tool, JSON args, schema, survives our gate."""
    if res.get("error"):
        return "ERROR"
    if not res["calls"]:
        return "no tool call"
    schemas = {t["name"]: t["parameters"] for t in api.TOOLS}
    parsed = []
    for c in res["calls"]:
        if c["name"] not in schemas:
            return f"unknown tool {c['name']}"
        try:
            args = json.loads(c["arguments"])
        except ValueError:
            return "args not JSON"
        errors = list(Draft202012Validator(schemas[c["name"]]).iter_errors(args))
        if errors:
            return f"schema: {errors[0].message[:50]}"
        parsed.append({"name": c["name"], "args": args})
    with contextlib.redirect_stdout(io.StringIO()):
        gated = api._expand_perform(parsed)
    names = [g["name"] for g in gated]
    want = action or tool
    return "ok" if want in names else f"valid, chose {','.join(names)}"


def run_turns(name: str, call) -> list:
    call(TURNS[0][0])  # prefix into cache: warm means warm
    rows = []
    for said, tool, action in TURNS * 2:
        res = call(said)
        verdict = check_call(res, tool, action)
        rows.append((said, res, verdict))
        log(f"| {name} | {said} | {res.get('first')} | {res.get('visible')} | {res.get('ms')} | "
            f"{res.get('in')} | {res.get('out')} | {res.get('reasoning_chars')} | {res.get('tps')} | {verdict} | "
            f"{(res.get('error') or res.get('text') or '')[:50]!r} |")
    return rows


def summary(name: str, rows: list) -> None:
    done = [r for _, r, _ in rows if not r.get("error")]
    if not done:
        log(f"| {name} | all failed | | | |")
        return
    med = lambda k: int(statistics.median(r[k] for r in done if r.get(k) is not None) or 0)
    log(f"| {name} | {med('first')} | {med('visible')} | {med('ms')} | "
        f"{sum(v == 'ok' for _, _, v in rows)}/{len(rows)} |")


def turns() -> None:
    log(f"\n## Warm voice turns ({time.strftime('%F %T')}), one round, ms\n")
    log("| model | turn | first token | first visible | total | in | out | reasoning chars | tok/s | call | text |")
    log("|---|---|---|---|---|---|---|---|---|---|---|")
    key = api._key()
    system, strength = bench_system(), bench_system("Reasoning strength: low\n\n")
    with httpx.Client(timeout=200) as c:
        param = run_turns("glimmer reasoning_effort=low",
                          lambda t: stream_glimmer(c, glimmer_body(system, t, "low")))
        prompt = run_turns("glimmer prompt low",
                           lambda t: stream_glimmer(c, glimmer_body(strength, t, None)))
        spark = run_turns("spark minimal", lambda t: stream_spark(c, key, system, t))
    log("\n| model | first token (median) | first visible | total | tool ok |\n|---|---|---|---|---|")
    summary("glimmer reasoning_effort=low", param)
    summary("glimmer prompt low", prompt)
    summary("spark minimal", spark)


def effort(value: str) -> None:
    log(f"\n## Glimmer reasoning_effort={value} ({time.strftime('%F %T')}), ms\n")
    log("| model | turn | first token | first visible | total | in | out | reasoning chars | tok/s | call | text |")
    log("|---|---|---|---|---|---|---|---|---|---|---|")
    system = bench_system()
    with httpx.Client(timeout=200) as c:
        sent = None if value == "server" else value  # server: flags on llama-server decide
        rows = run_turns(value, lambda t: stream_glimmer(c, glimmer_body(system, t, sent)))
    log("\n| model | first token (median) | first visible | total | tool ok |\n|---|---|---|---|---|")
    summary(value, rows)


def cold(n: int) -> None:
    """Time from a sleeping container to the first byte, redirects not followed."""
    log(f"\n## Cold start ({time.strftime('%F %T')}), limit {WEB_LIMIT_S}s\n")
    log("| try | status | first byte s | total s | location | body |\n|---|---|---|---|---|---|")
    body = {"messages": [{"role": "user", "content": "Say hi."}], "max_tokens": 16, "stream": True}
    for i in range(n):
        if i:
            time.sleep(SCALEDOWN_WAIT_S)
        t0 = time.perf_counter()
        with httpx.Client(timeout=900, follow_redirects=False) as c:
            with c.stream("POST", GLIMMER, json=body) as r:
                first = None
                raw = b""
                for chunk in r.iter_bytes():
                    first = first or time.perf_counter()
                    raw += chunk
        end = time.perf_counter()
        log(f"| {i + 1} | {r.status_code} | {(first or end) - t0:.1f} | {end - t0:.1f} | "
            f"{r.headers.get('location', '')[:60]} | {raw[:60]!r} |")


def label_image() -> str:
    """Our own label, so the answer is known; not a glasses photo."""
    img = Image.new("RGB", (900, 500), (236, 232, 222))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((90, 90, 810, 410), 16, fill="white", outline="black", width=3)
    font = ImageFont.load_default(38)
    for i, line in enumerate((f"{LABEL[0]}  {LABEL[1]}", f"Lot {LABEL[2]}", f"Exp {LABEL[3]}")):
        d.text((130, 130 + i * 85), line, fill="black", font=font)
    out = io.BytesIO()
    img.rotate(4, fillcolor=(236, 232, 222)).save(out, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def label() -> None:
    url = label_image()
    log(f"\n## Label read ({time.strftime('%F %T')})\n")
    log("| model | total ms | found | text |\n|---|---|---|---|")
    with httpx.Client(timeout=200) as c:
        g = stream_glimmer(c, {"messages": [{"role": "user", "content": [
            {"type": "text", "text": LABEL_ASK}, {"type": "image_url", "image_url": {"url": url}}]}],
            "max_tokens": 512, "stream": True, "reasoning_effort": "low"})
        s = stream_spark(c, api._key(), "You read labels.", [{"role": "user", "content": [
            {"type": "input_text", "text": LABEL_ASK}, {"type": "input_image", "image_url": url}]}], tools=[])
    for name, res in (("glimmer", g), ("spark", s)):
        text = res.get("text") or res.get("error") or ""
        found = sum(v.lower() in text.lower() for v in LABEL)
        log(f"| {name} | {res.get('ms')} | {found}/{len(LABEL)} | {text[:160]!r} |")


def guard() -> None:
    log(f"\n## Guard, live ({time.strftime('%F %T')})\n")
    base = GLIMMER.removesuffix("/v1/chat/completions")
    with httpx.Client(timeout=200, follow_redirects=False) as c:
        for method, path in (("GET", "/health"), ("GET", "/props"), ("POST", "/completion"), ("GET", "/")):
            log(f"- {method} {path}: {c.request(method, base + path).status_code}")
        codes = [c.post(GLIMMER, json={"messages": []}).status_code for _ in range(32)]
        log(f"- 32 fast refused bodies: {codes}")
        spoof = c.post(GLIMMER, json={"messages": []}, headers={"X-Forwarded-For": "8.8.8.8"})
        log(f"- limited, then X-Forwarded-For 8.8.8.8: {spoof.status_code}")


{"cold": lambda: cold(int(sys.argv[2]) if len(sys.argv) > 2 else 3),
 "turns": turns, "label": label, "guard": guard,
 "effort": lambda: effort(sys.argv[2])}[sys.argv[1]]()
