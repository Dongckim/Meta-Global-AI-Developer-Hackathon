"""Multi-action turns: every action runs, or the reply asks about the rest.

Run from repo root (live model, ~3 calls per case per repeat):
    .venv/bin/python eval/multi_action.py [repeats=3] [label]
    RUN_MODE=private .venv/bin/python eval/multi_action.py 3 glimmer
Appends a table to eval/results/multi_action.md.

A case passes when every expected action ran and every pre-turn card was
answered. "silent" counts runs that missed an action without asking about
it (the failure this eval exists for).
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

# (pre-turns, utterance, expected actions, card outcome: None | "yes" | "no")
CASES = [
    ([], "Open C8 and note bubbles", ["open", "add_note"], None),
    ([], "Exclude C6 and note bubbles in C8", ["exclude_well", "add_note"], None),
    ([], "Mark C2 done and start a 5 minute timer", ["mark_done", "set_timer"], None),
    ([], "Show C5 and note C5 looks clear", ["add_note"], None),
    ([], "add a note on C8 and exclude C6", ["add_note", "exclude_well"], None),
    ([], "Note C3 cloudy, exclude C4, and start a 10 minute timer",
     ["add_note", "exclude_well", "set_timer"], None),
    ([], "Mark C2 done, mark C3 done, and exclude C4", ["mark_done", "exclude_well"], None),
    (["Next step"], "Yes. The medium is in. Exclude C6.", ["exclude_well"], "yes"),
    (["C8 is contaminated, exclude it"], "Yes, and note C7 cloudy", ["add_note"], "yes"),
    (["Next step"], "No. Start a 5 minute timer first.", ["set_timer"], "no"),
    (["Next step"], "Yes, mark C2 done and note C2 clear", ["mark_done", "add_note"], "yes"),
]


def run_case(pre, text):
    api.CHAIN_ENABLED = False
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    api.store.r["step_index"] = 2  # mid-run bench, as tool_choice
    api.store.wake.begin()
    api.store.wake.finish(True)  # RUN_MODE=private: the model is up
    api._SESSIONS.clear()
    c = TestClient(api.app)
    for p in pre:
        with contextlib.redirect_stdout(io.StringIO()):
            c.post("/orchestrate", json={"text": p, "session": "m"})
    before = dict(api.store.r["cards"])
    step0 = api.store.r["step_index"]
    buf = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        j = c.post("/orchestrate", json={"text": text, "session": "m"}).json()
    ms = int((time.perf_counter() - t0) * 1000)
    followup = "[orch] followup" in buf.getvalue()
    return j, ms, followup, before, step0


def judge(j, exp, card, before, step0):
    names = {x.get("name") for x in j.get("tool_calls", [])}
    missing = [a for a in exp if a not in names]
    if card:
        still = [cid for cid in before if cid in api.store.r["cards"]]
        moved = api.store.r["step_index"] != step0
        if still or (card == "yes" and before and
                     any(c["kind"] == "next_step" for c in before.values()) and not moved):
            missing.append("card:" + card)
    import re
    cardq = {(c.get("question") or "").strip() for c in j.get("cards") or []}
    said = [x for x in re.split(r"(?<=[.!?])\s+", (j.get("reply") or "").strip()) if x]
    # told the user: a question that isn't just a confirm card's (asks for the
    # rest, for a missing value, or to say it again)
    asked = any(x.endswith("?") and x not in cardq for x in said)
    return missing, asked


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    label = sys.argv[2] if len(sys.argv) > 2 else "run"
    rows, total_ms, n, ok_n, silent_n, fu_n = [], 0, 0, 0, 0, 0
    for pre, text, exp, card in CASES:
        for _ in range(reps):
            j, ms, fu, before, step0 = run_case(pre, text)
            missing, asked = judge(j, exp, card, before, step0)
            ok = not missing
            silent = bool(missing) and not asked
            n += 1
            total_ms += ms
            ok_n += ok
            silent_n += silent
            fu_n += fu
            calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
            rows.append((text, ms, calls, ok, silent, fu, (j.get("reply") or "").replace("|", "/")))
            print(f"[{'PASS' if ok else 'SILENT' if silent else 'ASKED'} {ms}ms{' fu' if fu else ''}] "
                  f"{text[:44]} -> {calls} | {j.get('reply')}")
    avg = total_ms // max(n, 1)
    print(f"all actions: {ok_n}/{n}  silent drops: {silent_n}/{n}  followups: {fu_n}  avg_ms: {avg}")
    with open("eval/results/multi_action.md", "a") as f:
        f.write(f"\n## Multi-action ({label}, live {api.SPARK_MODEL} {api.REASONING_EFFORT}, x{reps})\n\n")
        f.write(f"All actions: {ok_n}/{n}. Silent drops: {silent_n}/{n}. "
                f"Followup rounds: {fu_n}. Avg latency: {avg} ms.\n\n")
        f.write("| turn | ms | calls | pass | silent | followup | reply |\n|---|---|---|---|---|---|---|\n")
        for t, ms, calls, ok, silent, fu, reply in rows:
            f.write(f"| {t} | {ms} | {calls} | {'y' if ok else 'n'} | {'y' if silent else ''} | "
                    f"{'y' if fu else ''} | {reply} |\n")
    print("appended multi_action")


main()
