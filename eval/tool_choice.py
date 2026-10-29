"""Write-vs-view tool choice, graded by outcome: confusing sentences, live model.

Run from repo root:
    .venv/bin/python eval/tool_choice.py
Appends a table to eval/results/tool_choice.md.
"""
import contextlib
import io
import json
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

# Graded by outcome: the screen reached, the well shown, and the record
# change. Tool names differ for the same outcome (open C8 vs navigate to
# the well card), so the old name checks are kept only as a reference column.
#   screen: current screen after the turn; well: the well shown (well card
#   or flash); writes: record changes as {kind: well} ("none" = no change);
#   ui: an app action (import); question/reply_*/speak_max: the reply.
NONE = {"writes": "none"}
TURNS = [
    ("Note C8, sticky edges", {"writes": {"note": "C8"}},
     {"must": ["add_note"], "must_not": ["open", "navigate", "show"]}),
    ("I want to make notes on C8", {**NONE, "question": True},
     {"must_not": sorted(WRITES), "question": True}),
    ("Show me C8", {**NONE, "well": "C8"}, {"must_any": ["open", "show"], "must_not": sorted(WRITES)}),
    ("C8 looks weird", {"writes": {"note": "C8"}},
     {"must": ["add_note"], "must_not": ["open", "navigate", "show"]}),
    ("Open C8 and note bubbles", {"writes": {"note": "C8"}, "well": "C8"}, {"must": ["open", "add_note"]}),
    ("Let me see the timers", NONE, {"must_not": sorted(WRITES)}),
    ("C8 is contaminated, exclude it", {"writes": {"exclude": "C8"}}, {"must": ["exclude_well"]}),
    ("What is on C8", {**NONE, "reply_match": r"C8"},
     {"must": ["read_record"], "must_not": sorted(WRITES)}),
    ("Mark C8 done", {"writes": {"done": "C8"}}, {"must": ["mark_done"]}),
    ("Go to the robot preview", {**NONE, "screen": "robot_preview"}, {"must": ["navigate"]}),
    ("the most recent runs", {**NONE, "reply_not": ["no recent runs", "no runs"]},
     {"must": ["read_record"], "must_kind": {"read_record": "runs"},
      "reply_not": ["no recent runs", "no runs"]}),
    ("show the most recent run", {**NONE, "screen": "run_view"},
     {"must": ["read_record", "navigate"], "must_kind": {"read_record": "runs"}}),
    ("What time is right now?", {**NONE, "reply_match": r"\d{1,2}:\d{2}"},
     {"must_not": sorted(WRITES), "reply_match": r"\d{1,2}:\d{2}"}),
    # importer (new protocol) vs protocol_source (an existing one's original);
    # each in its own session so earlier turns don't steer the choice.
    *[(t, {**NONE, "ui": "import", "fresh": True}, {"target": "importer"}) for t in (
        "import a new protocol", "I want to import a new protocol",
        "let's bring in a new protocol", "upload a protocol", "add a new protocol",
        "I have a new protocol PDF to load")],
    *[(t, {**NONE, "screen": "protocol_source", "fresh": True}, {"target": "source"}) for t in (
        "show me the original PDF", "open the source for this protocol",
        "where does this step come from in the original", "let me see the original protocol document")],
    # run history: reply comes from the result, never model line + result glued
    *[(t, {**NONE, "fresh": True, "reply_not": [", but", "showing home"]},
       {"must_any": ["read_record", "navigate", "open"], "reply_not": [", but", "showing home"]})
      for t in ("show the runs", "my past runs", "open run history")],
    # speech stays short: at most two sentences, lists go to the screen
    *[(t, {**NONE, "fresh": True, "speak_max": 2},
       {"must_any": ["read_record", "navigate", "open"], "speak_max": 2})
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


def names_ok(j, exp) -> bool:
    """Reference only: the tool names the old grading expected."""
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
    for name, kind in (exp.get("must_kind") or {}).items():
        if kind not in [(x.get("args") or {}).get("kind")
                        for x in j.get("tool_calls", []) if x.get("name") == name]:
            return False
    return True


def snapshot() -> dict:
    """What a turn can change: record writes (incl. pending cards) and view."""
    r = api.store.r
    v = r.get("view") or {}
    return {
        "note": sorted(w for n in r.get("notes", []) for w in n.get("wells") or []),
        "done": sorted(m["well"] for m in r.get("done", [])),
        "exclude": sorted(r.get("excluded", {})),
        "timers": len(r.get("timers", [])),
        "cards": sorted(json.dumps({k: c["payload"].get(k) for k in ("op", "well", "wells")}, sort_keys=True)
                        for c in r.get("cards", {}).values()),
        "screen": (v.get("current") or {}).get("screen"),
        "sheet": v.get("sheet"),
    }


def changes(before, after) -> dict:
    """{kind: wells} written by the turn; a pending card counts as its write."""
    out = {}
    for k in ("note", "done", "exclude"):
        new = [w for w in after[k] if after[k].count(w) > before[k].count(w)]
        if new:
            out[k] = set(new)
    for c in set(after["cards"]) - set(before["cards"]):
        p = json.loads(c)
        kind = {"exclude_well": "exclude", "mark_done": "done"}.get(p["op"], p["op"])
        ws = p.get("wells") or ([p["well"]] if p.get("well") else [])
        out.setdefault(kind, set()).update(ws if isinstance(ws, list) else [ws])
    if after["timers"] != before["timers"]:
        out["timer"] = set()
    return out


def verdict(j, exp, before, after) -> bool:
    did = changes(before, after)
    if exp.get("writes") == "none":
        if did:
            return False
    elif exp.get("writes"):
        for kind, well in exp["writes"].items():
            if well not in did.get(kind, set()):
                return False
        if set(did) - set(exp["writes"]):
            return False
    if exp.get("screen") and after["screen"] != exp["screen"]:
        return False
    if exp.get("well"):
        shown = {(after["sheet"] or {}).get("target")}
        shown |= {w for u in j.get("ui") or [] if u.get("action") == "flash" for w in u.get("wells") or []}
        if exp["well"] not in shown:
            return False
    if exp.get("ui") and not any(u.get("action") == exp["ui"] for u in j.get("ui") or []):
        return False
    if exp.get("question") and not (j.get("reply") or "").strip().endswith("?"):
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


def fresh_store():
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.start_demo_run()
    api.store.r["step_index"] = 2  # mid-run bench, as before


def main():
    api.CHAIN_ENABLED = False
    fresh_store()
    api._SESSIONS.clear()
    c = TestClient(api.app)
    rows = []
    for i, (text, exp, names) in enumerate(TURNS):
        buf = io.StringIO()
        session = f"eval-{i}" if exp.get("fresh") else "eval"
        if exp.get("fresh"):
            fresh_store()  # its own bench too: an earlier turn's screen never counts
        before = snapshot()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            r = c.post("/orchestrate", json={"text": text, "session": session})
        total = int((time.perf_counter() - t0) * 1000)
        j = r.json()
        ok = verdict(j, exp, before, snapshot())
        nok = names_ok(j, names)
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"label": text, "total": total, "calls": calls,
                     "reply": ((j.get("reply") or "") + (f" [speak: {j.get('speak')}]" if exp.get("speak_max") else "")).replace("|", "/"),
                     "ok": ok, "names": nok})
        print(f"[{'PASS' if ok else 'FAIL'}{'' if nok else ' names≠'} {total}ms] {text[:44]} -> {calls}")
    acc = sum(1 for r in rows if r["ok"])
    nacc = sum(1 for r in rows if r["names"])
    print(f"accuracy: {acc}/{len(rows)}  (tool names match: {nacc}/{len(rows)})")
    with open("eval/results/tool_choice.md", "a") as f:
        f.write("\n## Tool choice by outcome (live muse-spark-1.3 minimal)\n\n")
        f.write(f"Outcome accuracy: {acc}/{len(rows)}. Tool names match (reference): "
                f"{nacc}/{len(rows)}. One session, fresh record.\n\n")
        f.write("| turn | total_ms | calls | pass | names | reply |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['label']} | {r['total']} | {r['calls']} | "
                    f"{'y' if r['ok'] else 'n'} | {'y' if r['names'] else 'n'} | {r['reply']} |\n")
    print("appended tool_choice")


main()
