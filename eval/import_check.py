"""Live protocol import v2: 4 PDFs + voice edits + Start. Run from repo root:
    .venv/bin/python eval/import_check.py
Appends a table to eval/results/import.md.
"""
import contextlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server.store import RecordStore  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PDF_V1 = (SAMPLES / "protocol_demo.pdf").read_bytes()
PDF_V2 = (SAMPLES / "protocol_demo_v2.pdf").read_bytes()
PDF_WASH = (SAMPLES / "protocol_wash.pdf").read_bytes()
PDF_TABLE = (SAMPLES / "protocol_table.pdf").read_bytes()

EDITS = [
    # (utterance, check(draft) -> (ok, note)) on the v1 demo draft.
    ("move the controls one row down",
     lambda d: (d["plan"]["conditions"]["control"]["wells"] == ["C2", "C3", "C4", "C5", "C6", "C7", "C8"]
                and any(e["code"] == "overlap" for e in d["errors"]),
                "controls C2-C8 + overlap error")),
    ("make drug B 6 replicates",
     lambda d: (d["plan"]["conditions"]["drug_b"]["replicates"] == 6
                and d["plan"]["conditions"]["drug_b"]["wells"] == ["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"],
                "drug_b n=6 D2-D9")),
    ("change drug A to row E",
     lambda d: (d["plan"]["conditions"]["drug_a"]["wells"] == ["E2", "E3", "E4", "E5", "E6", "E7", "E8"]
                and d["errors"] == [],
                "drug_a E2-E8, no errors")),
    ("change the first step to 50 microliters",
     lambda d: (d["plan"]["steps"][0]["pipette"]["volume"] == 50,
                "steps[0] volume 50")),
]


def fresh():
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api._SESSIONS.clear()
    return TestClient(api.app)


def quiet_post(c, *a, **k):
    buf = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        r = c.post(*a, **k)
    return r, int((time.perf_counter() - t0) * 1000)


def check_v1(j):
    plan = (j.get("draft") or {}).get("plan", {})
    conds = sorted(plan.get("conditions", {}))
    steps = plan.get("steps", [])
    return (set(conds) == {"control", "drug_a", "drug_b"} and len(steps) == 5
            and any(s.get("minutes") == 30 for s in steps) and j.get("errors") == [])


def check_v2(j):
    plan = (j.get("draft") or {}).get("plan", {})
    liqs = plan.get("liquids", {})
    conds = plan.get("conditions", {})
    steps = plan.get("steps", [])
    vols = {p.get("volume") for s in steps for p in [s.get("pipette") or {}] if p.get("volume")}
    cautions = [s.get("caution") for s in steps if s.get("caution")]
    joined = " ".join(cautions).lower()
    return (len(liqs) == 4 and len(conds) == 3 and len(steps) == 5
            and all(c.get("replicates") == 5 for c in conds.values())
            and {100, 10} <= vols
            and any(s.get("minutes") == 30 for s in steps)
            and any(s.get("temp_c") == 37 for s in steps)
            and len(cautions) == 2 and "wall" in joined and "up and down" in joined
            and all("tip" not in s for s in steps)
            and set((j.get("draft") or {}).get("meta", {}).get("layout", {}).values()) == {"auto"}
            and j.get("errors") == [])


def check_wash(j):
    plan = (j.get("draft") or {}).get("plan", {})
    conds = plan.get("conditions", {})
    steps = plan.get("steps", [])
    vols = {p.get("volume") for s in steps for p in [s.get("pipette") or {}] if p.get("volume")}
    return (len(conds) == 2 and len(steps) == 4
            and any(s.get("kind") == "wash" and (s.get("pipette") or {}).get("repeat") == 2
                    for s in steps)
            and any(s.get("kind") == "centrifuge" and s.get("minutes") == 5 for s in steps)
            and {50, 200, 100} <= vols and j.get("errors") == [])


def check_table(j):
    d = j.get("draft") or {}
    plan = d.get("plan", {})
    conds = plan.get("conditions", {})
    b6 = ["B2", "B3", "B4", "B5", "B6"]
    c6 = ["C2", "C3", "C4", "C5", "C6"]
    return (len(conds) == 2 and j.get("errors") == []
            and set(d.get("meta", {}).get("layout", {}).values()) == {"stated"}
            and conds.get("control", {}).get("wells") == b6
            and conds.get("drug_a", {}).get("wells") == c6)


def main():
    api.CHAIN_ENABLED = False
    rows = []

    for label, pdf, name, check in [
            ("import v1 PDF", PDF_V1, "protocol_demo.pdf", check_v1),
            ("import v2 user PDF", PDF_V2, "protocol_demo_v2.pdf", check_v2),
            ("import wash PDF", PDF_WASH, "protocol_wash.pdf", check_wash),
            ("import table PDF", PDF_TABLE, "protocol_table.pdf", check_table)]:
        c = fresh()
        r, ms = quiet_post(c, "/protocols/import", files={"file": (name, pdf, "application/pdf")})
        j = r.json()
        try:
            ok = check(j)
            note = ""
        except (KeyError, TypeError, IndexError):
            ok, note = False, "shape unexpected"
        plan = (j.get("draft") or {}).get("plan", {})
        info = (f"C={len(plan.get('conditions', {}))} S={len(plan.get('steps', []))} "
                f"err={len(j.get('errors', []))} rev={len(j.get('review', []))} {note}").strip()
        rows.append({"label": label, "total": ms, "ok": ok,
                     "reply": (j.get("reply") or "")[:70]})
        print(f"[{'PASS' if ok else 'FAIL'} {ms}ms] {label}: {info}")

    for text, check in EDITS:
        c = fresh()
        quiet_post(c, "/protocols/import",
                   files={"file": ("protocol_demo.pdf", PDF_V1, "application/pdf")})
        r, ms = quiet_post(c, "/orchestrate", json={"text": text, "session": "eval"})
        j = r.json()
        try:
            ok, note = check(api.store.r["draft"])
        except (KeyError, TypeError, IndexError):
            ok, note = False, "draft shape unexpected"
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"label": text, "total": ms, "ok": ok,
                     "reply": f"{calls} :: {(j.get('reply') or '')[:50]}"})
        print(f"[{'PASS' if ok else 'FAIL'} {ms}ms] {text[:40]} -> {calls} ({note})")

    c = fresh()
    quiet_post(c, "/protocols/import",
               files={"file": ("protocol_wash.pdf", PDF_WASH, "application/pdf")})
    r, ms = quiet_post(c, "/protocols/start", json={"session": "eval"})
    j = r.json()
    try:
        live = api.store.r["plan"]["steps"]
        ok = (j.get("started") is True and len(live) == 5
              and live[1]["text"].endswith("(1 of 2)") and live[2]["text"].endswith("(2 of 2)"))
        note = f"{len(live)} live steps"
    except (KeyError, TypeError, IndexError):
        ok, note = False, "shape unexpected"
    rows.append({"label": "start wash (repeat x2)", "total": ms, "ok": ok, "reply": note})
    print(f"[{'PASS' if ok else 'FAIL'} {ms}ms] start wash -> {note} ({(j.get('reply') or '')[:50]})")

    acc = sum(1 for r in rows if r["ok"])
    print(f"accuracy: {acc}/{len(rows)}")
    with open("eval/results/import.md", "a") as f:
        f.write("\n## Protocol import v2 + voice edits + Start (live muse-spark-1.3 minimal)\n\n")
        f.write(f"Accuracy: {acc}/{len(rows)}. Fresh record per case.\n\n")
        f.write("| turn | total_ms | pass | reply |\n")
        f.write("|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['label']} | {r['total']} | {'y' if r['ok'] else 'n'} | {r['reply']} |\n")
    print("appended import")


main()
