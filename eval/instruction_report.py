"""Instruction vs report (10 live sentences) + conversational timing (3 turns).

Instructions about bench work must NOT record; reports must. Conversational
turns must answer short with zero writes. Run from repo root:
    .venv/bin/python eval/instruction_report.py
Appends tables to eval/results/instruction_report.md.
"""
import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

RECORD_WRITES = {"mark_done", "exclude_well", "record_redo"}

# (sentence, kind): instruction -> no record writes; report -> mark_done.
TURNS = [
    ("wash the remaining wells right now", "instruction"),
    ("add drug A to C2 through C8", "instruction"),
    ("go do the next wash step", "instruction"),
    ("start the next incubation", "instruction"),
    ("pipette the samples now", "instruction"),
    ("C2 to C8 done", "report"),
    ("finished adding drug A to C2 through C5", "report"),
    ("I put the samples in D2 to D8", "report"),
    ("washed C6 to C8", "report"),
    ("C6, C7 and C8 are finished", "report"),
]

SMALLTALK = ["got it, thanks", "cool", "okay perfect"]


def verdict(j, kind):
    names = {x.get("name", "?") for x in j.get("tool_calls", [])}
    if kind == "instruction":
        return not (names & RECORD_WRITES)
    return "mark_done" in names


def main():
    api.CHAIN_ENABLED = False
    rows = []
    for text, kind in TURNS:
        api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
        api.store.start_demo_run()
        api.store.r["step_index"] = 2  # mid-run bench, as before
        api._SESSIONS.clear()
        c = TestClient(api.app)
        buf = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            r = c.post("/orchestrate", json={"text": text, "session": "eval"})
        total = int((time.perf_counter() - t0) * 1000)
        j = r.json()
        ok = verdict(j, kind)
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"label": text, "kind": kind, "total": total, "calls": calls,
                     "reply": (j.get("reply") or "")[:70], "ok": ok})
        print(f"[{'PASS' if ok else 'FAIL'} {total}ms] {text[:44]} -> {calls}")
    acc = sum(1 for r in rows if r["ok"])
    print(f"accuracy: {acc}/{len(rows)}")

    talk = []
    for text in SMALLTALK:
        api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
        api.store.start_demo_run()
        api.store.r["step_index"] = 2  # mid-run bench, as before
        api._SESSIONS.clear()
        c = TestClient(api.app)
        before = (list(api.store.r["done"]), list(api.store.r["excluded"]),
                  list(api.store.r["notes"]), list(api.store.r["timers"]))
        buf = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            r = c.post("/orchestrate", json={"text": text, "session": "eval"})
        total = int((time.perf_counter() - t0) * 1000)
        j = r.json()
        after = (list(api.store.r["done"]), list(api.store.r["excluded"]),
                 list(api.store.r["notes"]), list(api.store.r["timers"]))
        ok = (j.get("tool_calls") == [] and before == after and total <= 2000
              and (j.get("reply") or "") != "Sorry, say that again?")
        talk.append({"label": text, "total": total, "reply": (j.get("reply") or "")[:70], "ok": ok})
        print(f"[{'PASS' if ok else 'FAIL'} {total}ms] {text} -> {(j.get('reply') or '')[:50]}")

    with open("eval/results/instruction_report.md", "a") as f:
        f.write("\n## Instruction vs report (live muse-spark-1.3 minimal)\n\n")
        f.write(f"Accuracy: {acc}/{len(rows)}. Fresh record per turn.\n\n")
        f.write("| turn | kind | total_ms | calls | pass | reply |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['label']} | {r['kind']} | {r['total']} | {r['calls']} | "
                    f"{'y' if r['ok'] else 'n'} | {r['reply']} |\n")
        f.write("\n## Conversational timing (<=2000ms, zero writes)\n\n")
        f.write("| turn | total_ms | pass | reply |\n")
        f.write("|---|---|---|---|\n")
        for r in talk:
            f.write(f"| {r['label']} | {r['total']} | {'y' if r['ok'] else 'n'} | {r['reply']} |\n")
    print("appended instruction_report")


main()
