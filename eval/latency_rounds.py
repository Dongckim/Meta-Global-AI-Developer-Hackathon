"""Read-question (2-round) vs command (1-round) latency, one live session.

Goals: questions <= 4000ms, commands <= 3000ms (total HTTP round trip).
Run from repo root:
    .venv/bin/python eval/latency_rounds.py
Appends a table to eval/results/latency.md.
"""
import contextlib
import io
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

TURNS = [
    ("command", "mark C2 and C3 done please"),
    ("command", "note C4, slight edge effect"),
    ("command", "start the timer for C5, five minutes"),
    ("question", "what is on C6"),
    ("question", "read all my notes"),
    ("question", "how many wells have I finished"),
]

LINE = re.compile(r"spark ms=(\d+)")


def main():
    api.CHAIN_ENABLED = False
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api._SESSIONS.clear()
    c = TestClient(api.app)
    rows = []
    for kind, text in TURNS:
        buf = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            r = c.post("/orchestrate", json={"text": text, "session": "lat"})
        total = int((time.perf_counter() - t0) * 1000)
        j = r.json()
        spark = sum(int(m.group(1)) for m in LINE.finditer(buf.getvalue()))
        rounds = len(LINE.findall(buf.getvalue()))
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"kind": kind, "label": text, "total": total, "spark": spark,
                     "rounds": rounds, "route": j.get("route"), "calls": calls,
                     "reply": (j.get("reply") or "")[:60]})
        print(f"[{kind} {text[:36]}] total={total} spark={spark} rounds={rounds} {calls}")
    q_ok = all(r["total"] <= 4000 for r in rows if r["kind"] == "question")
    c_ok = all(r["total"] <= 3000 for r in rows if r["kind"] == "command")
    print(f"questions_under_4s: {q_ok}, commands_under_3s: {c_ok}")
    with open("eval/results/latency.md", "a") as f:
        f.write("\n## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)\n\n")
        f.write("One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.\n\n")
        f.write("| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['kind']} | {r['label']} | {r['total']} | {r['spark']} | "
                    f"{r['rounds']} | {r['route']} | {r['calls']} | {r['reply']} |\n")
        f.write(f"\nquestions_under_4s: {q_ok}, commands_under_3s: {c_ok}\n")
    print("appended latency rounds")


main()
