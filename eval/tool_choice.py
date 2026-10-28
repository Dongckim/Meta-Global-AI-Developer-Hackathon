"""Write-vs-view tool-choice accuracy: 10 confusing sentences, live model.

Run from repo root:
    .venv/bin/python eval/tool_choice.py
Appends a table to eval/results/tool_choice.md.
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

WRITES = {"mark_done", "exclude_well", "record_redo", "add_note", "edit_note",
          "save_tip", "set_timer", "cancel_timer", "extend_timer", "next_step",
          "make_robot"}

TURNS = [
    ("Note C8, sticky edges", {"must": ["add_note"], "must_not": ["open", "navigate", "show"]}),
    ("I want to make notes on C8", {"must_not": sorted(WRITES), "question": True}),
    ("Show me C8", {"must_any": ["open", "show"], "must_not": sorted(WRITES)}),
    ("C8 looks weird", {"must": ["add_note"], "must_not": ["open", "navigate", "show"]}),
    ("Open C8 and note bubbles", {"must": ["open", "add_note"]}),
    ("Let me see the timers", {"must_not": sorted(WRITES)}),
    ("C8 is contaminated, exclude it", {"must": ["exclude_well"]}),
    ("What is on C8", {"must": ["read_record"], "must_not": sorted(WRITES)}),
    ("Mark C8 done", {"must": ["mark_done"]}),
    ("Go to the robot preview", {"must": ["navigate"]}),
]


def verdict(j, exp) -> bool:
    names = [x.get("name", "?") for x in j.get("tool_calls", [])]
    for m in exp.get("must", []):
        if m not in names:
            return False
    if exp.get("must_any") and not (set(names) & set(exp["must_any"])):
        return False
    if set(names) & set(exp.get("must_not", [])):
        return False
    if exp.get("question") and not (j.get("reply") or "").strip().endswith("?"):
        return False
    return True


def main():
    api.CHAIN_ENABLED = False
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    api.store.r["step_index"] = 2  # mid-run bench, as before
    api._SESSIONS.clear()
    c = TestClient(api.app)
    rows = []
    for text, exp in TURNS:
        buf = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            r = c.post("/orchestrate", json={"text": text, "session": "eval"})
        total = int((time.perf_counter() - t0) * 1000)
        j = r.json()
        ok = verdict(j, exp)
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"label": text, "total": total, "calls": calls,
                     "reply": (j.get("reply") or "")[:70], "ok": ok})
        print(f"[{'PASS' if ok else 'FAIL'} {total}ms] {text[:44]} -> {calls}")
    acc = sum(1 for r in rows if r["ok"])
    print(f"accuracy: {acc}/{len(rows)}")
    with open("eval/results/tool_choice.md", "a") as f:
        f.write("\n## Write-vs-view tool choice (live muse-spark-1.3 minimal)\n\n")
        f.write(f"Accuracy: {acc}/{len(rows)}. One session, fresh record.\n\n")
        f.write("| turn | total_ms | calls | pass | reply |\n")
        f.write("|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['label']} | {r['total']} | {r['calls']} | "
                    f"{'y' if r['ok'] else 'n'} | {r['reply']} |\n")
    print("appended tool_choice")


main()
