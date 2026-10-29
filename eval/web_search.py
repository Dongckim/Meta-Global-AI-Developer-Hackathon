"""Web search, live model: (A) does the model pick web_search only for
outside knowledge (record questions -> read_record, smalltalk -> nothing),
and (B) real searches: how often an answer comes with a cited source, and
how long it takes.

Run from repo root:
    .venv/bin/python eval/web_search.py
Appends a table to eval/results/web_search.md. Part B sends 5 real searches.
"""
import asyncio
import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import search  # noqa: E402
from server.store import RecordStore  # noqa: E402

SEARCH = ["Max DMSO for HeLa cells?", "Is doxorubicin stable at room temperature?",
          "What's a typical MTT incubation time?", "How much trypsin should I use for a T75 flask?",
          "What wavelength do I read MTT absorbance at?", "How long is PBS good for after opening?"]
RECORD = ["What's on C3?", "Which wells are done?", "How long is left on the timer?",
          "What step am I on?", "Did I note anything on C5?", "What's the next step?"]
CHAT = ["Thanks!", "Good morning", "That was quick", "Hey Milo, you there?", "Nice work", "Okay cool"]


def part_a() -> list:
    rows = []
    chosen: list = []

    async def fake_run(client, key, question, query, session):
        chosen.append(query)
        return "stub", []

    for group, texts, want in (("search", SEARCH, True), ("record", RECORD, False), ("chat", CHAT, False)):
        for t in texts:
            api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
            api.store.start_demo_run()
            api.store.apply_calls([{"name": "add_note", "args": {"target": "C5", "text": "bubbles"}}],
                                  "voice", "x", "grammar", {})
            api._SESSIONS.clear()
            chosen.clear()
            c = TestClient(api.app)
            t0 = time.perf_counter()
            with patch.object(api, "_run_search", fake_run), contextlib.redirect_stdout(io.StringIO()):
                j = c.post("/input", json={"text": t, "session": "ev"}).json()
            ms = int((time.perf_counter() - t0) * 1000)
            picked = bool(chosen)
            names = [x["name"] for x in j.get("tool_calls", [])]
            rows.append((group, t, picked == want, "web_search" if picked else ",".join(names) or "-",
                         chosen[0] if chosen else "", ms))
    return rows


async def part_b() -> list:
    out = []
    key = api._key()
    async with httpx.AsyncClient(timeout=30) as client:
        for q in SEARCH[:5]:
            t0 = time.perf_counter()
            try:
                h = {"Authorization": f"Bearer {key}"}
                r = await client.post(f"{api.BASE_URL}/responses", json=search.build_body(q, api.SPARK_MODEL), headers=h)
                got = search.found(r.json()) if r.status_code == 200 else None
                res = None
                if got:  # the same two calls the server makes
                    t = await client.post(f"{api.BASE_URL}/responses", headers=h,
                                          json=search.build_summary_body(q, got, api.SPARK_MODEL))
                    res = search.tidy(search.output_json(t.json()) if t.status_code == 200 else None, got)
                status = res["basis"] if res else f"no_source ({r.status_code})"
            except httpx.HTTPError as e:
                res, status = None, f"error {type(e).__name__}"
            ms = int((time.perf_counter() - t0) * 1000)
            out.append((q, status, len((res or {}).get("sources", [])), ms, (res or {}).get("answer_short", "")))
    return out


def main():
    a = part_a()
    b = asyncio.run(part_b())
    ok = sum(r[2] for r in a)
    lines = [f"\n## {time.strftime('%Y-%m-%d %H:%M')} — web_search choice {ok}/{len(a)}, "
             f"cited {sum(1 for r in b if r[1] == 'cited')}/{len(b)}, "
             f"pages only {sum(1 for r in b if r[1] == 'pages')}/{len(b)}\n",
             "| group | sentence | ok | chose | query sent | ms |", "|---|---|---|---|---|---|"]
    lines += [f"| {g} | {t} | {'✓' if k else '✗'} | {ch} | {q} | {ms} |" for g, t, k, ch, q, ms in a]
    lines += ["", "| search | status | sources | ms | answer |", "|---|---|---|---|---|"]
    lines += [f"| {q} | {st} | {n} | {ms} | {ans} |" for q, st, n, ms, ans in b]
    mss = sorted(r[3] for r in b)
    lines += ["", f"search latency: median {mss[len(mss) // 2]} ms, max {mss[-1]} ms"]
    text = "\n".join(lines) + "\n"
    out = Path(__file__).resolve().parent / "results" / "web_search.md"
    with out.open("a") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
