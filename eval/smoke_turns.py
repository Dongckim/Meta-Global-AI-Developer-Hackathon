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

#: Strict per-turn state checks, same order as TURNS (None = no check).
CHECKS = [
    lambda r: ({"C2", "C3"} <= set(r["done"]), "C2,C3 done"),
    lambda r: (len(r["notes"]) >= 1, "note saved"),
    lambda r: (len(r["timers"]) >= 1, "timer running"),
    None,
    lambda r: (len(r["cards"]) >= 1, "D5 card"),
    lambda r: ("D5" in (r["excluded"] or {}), "D5 excluded"),
    lambda r: (len(r["cards"]) >= 1, "redo cards"),
    None,
    lambda r: (len(r["timers"]) == 0, "timer cancelled"),
    lambda r: ({"C4", "C5", "C6", "C7", "C8"} <= set(r["done"]), "C4-C8 done"),
]


#: End-of-run script: fresh run, a few writes, end card, yes -> home.
END_TURNS = [
    ("say", "mark C2 and C3 done please"),
    ("say", "note C2, looks fine"),
    ("tap", ("open", {"target": "C5"})),
    ("say", "end the run"),
    ("confirm", None),  # answer the end_run card yes, like the app
]

END_CHECKS = [
    lambda r: ({"C2", "C3"} <= set(r["done"]), "C2,C3 done"),
    lambda r: (len(r["notes"]) >= 1, "note saved"),
    lambda r: (r["view"]["sheet"] == {"name": "well_card", "target": "C5"},
               "tap opens sheet"),
    lambda r: (len(r["cards"]) >= 1, "end card"),
    lambda r: (r.get("run") is None and r.get("cards") == {},
               "run ended, cards empty, home"),
]


def run_fake(be, strict, turns=TURNS, checks=CHECKS):
    """Same script through the fake app: every turn must render, and the
    render must match the record. Strict mode also checks absolute state."""
    from eval.fake_app import check_screen_matches_record, render
    fails = []
    pending_card = None
    prev = (be.record() or {}).get("version", 0)
    for i, (kind, text) in enumerate(turns):
        if kind == "say":
            j = be.say(text, session="t10")
            label = text
        elif kind == "tap":
            action, args = text
            j = be.tap(action, args, session="t10")
            label = f"tap:{action}"
        else:
            j = be.answer(pending_card, True, session="t10")
            label = "yes(confirm)"
        try:
            snap = be.wait_version(j.get("record_version", prev + 1) - 1, timeout=10)
        except TimeoutError as e:
            print(f"[FAIL] {label[:40]}: {e}")
            fails.append(label)
            continue
        prev = snap.get("version", prev)
        rec = be.record()
        shown = render(snap)
        print(f"[{label[:40]}]\n  {shown.splitlines()[0]}")
        errs = check_screen_matches_record(shown, rec)
        if strict and checks[i] is not None:
            ok, note = checks[i](rec)
            if not ok:
                errs.append(f"expected {note}")
        if j.get("cards"):
            pending_card = j["cards"][0]["id"]
        if errs:
            print(f"  FAIL: {'; '.join(errs)}")
            fails.append(label)
    print(f"fake_app {'FAIL: ' + str(len(fails)) if fails else 'ALL PASS'}"
          f" ({'strict' if strict else 'consistency-only'})")
    if fails:
        raise SystemExit(1)


def main():
    if "--fake-live" in sys.argv:
        from eval.fake_app import LiveBackend
        run_fake(LiveBackend(), strict=False)
        return
    if "--fake" in sys.argv:
        from eval.fake_app import TestBackend
        api.CHAIN_ENABLED = False
        api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
        api.store.start_demo_run()
        api.store.r["step_index"] = 2
        api._SESSIONS.clear()
        run_fake(TestBackend(TestClient(api.app), api.store), strict=True)
        return
    if "--fake-end" in sys.argv:
        from eval.fake_app import TestBackend
        api.CHAIN_ENABLED = False
        api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
        api.store.start_demo_run()
        api._SESSIONS.clear()
        run_fake(TestBackend(TestClient(api.app), api.store), strict=True,
                 turns=END_TURNS, checks=END_CHECKS)
        return
    api.CHAIN_ENABLED = False
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    api.store.r["step_index"] = 2  # mid-run bench, as before
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
