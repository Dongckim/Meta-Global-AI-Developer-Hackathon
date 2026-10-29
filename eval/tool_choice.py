"""Write-vs-view tool-choice accuracy: 10 confusing sentences, live model.

Run from repo root:
    .venv/bin/python eval/tool_choice.py
Appends a table to eval/results/tool_choice.md.
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
    ("the most recent runs", {"must": ["read_record"],
                              "must_kind": {"read_record": "runs"},
                              "reply_not": ["no recent runs", "no runs"]}),
    ("show the most recent run", {"must": ["read_record", "navigate"],
                                  "must_kind": {"read_record": "runs"}}),
    ("What time is right now?", {"must_not": sorted(WRITES),
                                 "reply_match": r"\d{1,2}:\d{2}"}),
    # importer (new protocol) vs protocol_source (an existing one's original);
    # each in its own session so earlier turns don't steer the choice.
    *[(t, {"target": "importer", "fresh": True}) for t in (
        "import a new protocol", "I want to import a new protocol",
        "let's bring in a new protocol", "upload a protocol", "add a new protocol",
        "I have a new protocol PDF to load")],
    *[(t, {"target": "source", "fresh": True}) for t in (
        "show me the original PDF", "open the source for this protocol",
        "where does this step come from in the original", "let me see the original protocol document")],
    # run history: reply comes from the result, never model line + result glued
    *[(t, {"must_any": ["read_record", "navigate", "open"], "fresh": True,
           "reply_not": [", but", "showing home"]}) for t in (
        "show the runs", "my past runs", "open run history")],
    # speech stays short: at most two sentences, lists go to the screen
    *[(t, {"must_any": ["read_record", "navigate", "open"], "fresh": True, "speak_max": 2})
      for t in ("read all my notes", "list my runs")],
]


def _picked(j) -> set[str]:
    """importer / source by the calls made (any route, any spelling)."""
    out = set()
    for x in j.get("tool_calls", []):
        a = x.get("args") or {}
        what = f"{a.get('screen') or ''} {a.get('target') or ''}".lower()
        if x.get("name") in ("open", "navigate"):
            if "importer" in what:
                out.add("importer")
            if "protocol_source" in what or "original" in what or "source" in what:
                out.add("source")
    if any(u.get("action") == "import" for u in j.get("ui") or []):
        out.add("importer")
    return out


def verdict(j, exp) -> bool:
    names = [x.get("name", "?") for x in j.get("tool_calls", [])]
    if exp.get("target") and _picked(j) != {exp["target"]}:
        return False
    for m in exp.get("must", []):
        if m not in names:
            return False
    if exp.get("must_any") and not (set(names) & set(exp["must_any"])):
        return False
    if set(names) & set(exp.get("must_not", [])):
        return False
    if exp.get("question") and not (j.get("reply") or "").strip().endswith("?"):
        return False
    for name, kind in (exp.get("must_kind") or {}).items():
        if kind not in [(x.get("args") or {}).get("kind")
                        for x in j.get("tool_calls", []) if x.get("name") == name]:
            return False
    if exp.get("reply_match") and not re.search(exp["reply_match"], j.get("reply") or ""):
        return False
    if exp.get("speak_max") is not None:
        said = [x for x in re.split(r"(?<=[.!?])\s+", (j.get("speak") or "").strip()) if x]
        if len(said) > exp["speak_max"]:
            return False
    low = (j.get("reply") or "").lower()
    if any(p in low for p in exp.get("reply_not", [])):
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
    for i, (text, exp) in enumerate(TURNS):
        buf = io.StringIO()
        t0 = time.perf_counter()
        session = f"eval-{i}" if exp.get("fresh") else "eval"
        with contextlib.redirect_stdout(buf):
            r = c.post("/orchestrate", json={"text": text, "session": session})
        total = int((time.perf_counter() - t0) * 1000)
        j = r.json()
        ok = verdict(j, exp)
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"label": text, "total": total, "calls": calls,
                     "reply": ((j.get("reply") or "") + (f" [speak: {j.get('speak')}]" if exp.get("speak_max") else "")).replace("|", "/"),
                     "ok": ok})
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
