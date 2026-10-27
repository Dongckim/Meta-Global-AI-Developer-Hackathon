"""10-turn conversation, summary mode: every turn must stay under 3s.

Live muse-spark-1.3 minimal. Run from repo root:
    .venv/bin/python eval/smoke_turns.py
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
    ("say", "mark C2 and C3 done please"),
    ("say", "note C3, big bubbles near the edge"),
    ("say", "start a 25 minute timer for C4"),
    ("say", "how many wells have I finished"),
    ("say", "D5 looks cloudy, drop it from the run"),
    ("confirm", None),  # answer the D5 card yes, like the app
    ("say", "C6 has a bubble, redo it in a backup well"),
    ("say", "what is on C6"),
    ("say", "cancel the C4 timer"),
    ("say", "mark C4 through C8 done"),
]

LINE = re.compile(r"spark ms=(\d+)")


def main():
    api.CHAIN_ENABLED = False
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api._SESSIONS.clear()
    c = TestClient(api.app)
    rows = []
    pending_card = None
    for kind, text in TURNS:
        buf = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            if kind == "say":
                r = c.post("/orchestrate", json={"text": text, "session": "t10"})
            else:
                r = c.post("/confirm", json={"card_id": pending_card, "ok": True, "session": "t10"})
        total = int((time.perf_counter() - t0) * 1000)
        j = r.json()
        spark = sum(int(m.group(1)) for m in LINE.finditer(buf.getvalue()))
        if j.get("cards"):
            pending_card = j["cards"][0]["id"]
        label = text if kind == "say" else "yes(confirm)"
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"label": label, "total": total, "spark": spark,
                     "route": j.get("route", "confirm"), "calls": calls,
                     "reply": (j.get("reply") or j.get("message", ""))[:60]})
        print(f"[{label[:40]}] total={total} spark={spark} {rows[-1]['route']}/{calls}")
    worst = max(rows, key=lambda r: r["total"])
    print(f"worst: {worst['label'][:40]} total={worst['total']}")
    print(f"all_under_3s: {all(r['total'] <= 3000 for r in rows)}")
    with open("eval/results/latency.md", "a") as f:
        f.write("\n## 10-turn conversation (summary mode, goal: every turn <= 3000ms)\n\n")
        f.write("One session, live muse-spark-1.3 minimal. total = HTTP round trip, "
                "spark = model time inside it.\n\n")
        f.write("| turn | total_ms | spark_ms | route | calls | reply |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['label']} | {r['total']} | {r['spark']} | {r['route']} | "
                    f"{r['calls']} | {r['reply']} |\n")
        f.write(f"\nall_under_3s: {all(r['total'] <= 3000 for r in rows)}\n")
    print("appended 10-turn")


main()
