"""Original-source voice turns: open + read_record source, live model.

Imports the v1 demo PDF, starts the run, then asks three source questions.
Run from repo root:
    .venv/bin/python eval/source_check.py
Appends a table to eval/results/import.md.
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

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PDF_V1 = (SAMPLES / "protocol_demo.pdf").read_bytes()

TURNS = [
    # (utterance, verdict(j) -> (ok, note))
    ("show me the original for this step",
     lambda j: (any((u.get("screen") == "protocol_source" or "protocol:" in (u.get("target") or ""))
                    for u in j.get("ui", [])),
                "navigates to protocol_source")),
    ("where does it say 30 minutes",
     lambda j: ("read_record" in [x.get("name") for x in j.get("tool_calls", [])]
                and "30" in (j.get("reply") or ""),
                "reads the record, answers 30")),
    ("read me the original instruction",
     lambda j: ("read_record" in [x.get("name") for x in j.get("tool_calls", [])]
                and len((j.get("reply") or "")) > 10,
                "reads the record, speaks it")),
]


def quiet_post(c, *a, **k):
    buf = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        r = c.post(*a, **k)
    return r, int((time.perf_counter() - t0) * 1000)


def main():
    api.CHAIN_ENABLED = False
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api._SESSIONS.clear()
    c = TestClient(api.app)
    rows = []

    r, ms = quiet_post(c, "/protocols/import",
                       files={"file": ("protocol_demo.pdf", PDF_V1, "application/pdf")})
    j = r.json()
    ok = j.get("errors") == []
    rows.append({"label": "import v1 PDF", "total": ms, "ok": ok,
                 "reply": (j.get("reply") or "")[:70]})
    print(f"[{'PASS' if ok else 'FAIL'} {ms}ms] import v1 PDF")

    r, ms = quiet_post(c, "/protocols/start", json={"session": "eval"})
    j = r.json()
    steps = api.store.r["plan"]["steps"]
    ok = (j.get("started") is True
          and any(s.get("quote_page") == 1 for s in steps))
    rows.append({"label": "start (quotes ride along)", "total": ms, "ok": ok,
                 "reply": (j.get("reply") or "")[:70]})
    print(f"[{'PASS' if ok else 'FAIL'} {ms}ms] start")

    for text, verdict in TURNS:
        r, ms = quiet_post(c, "/orchestrate", json={"text": text, "session": "eval"})
        j = r.json()
        try:
            ok, note = verdict(j)
        except (KeyError, TypeError, IndexError):
            ok, note = False, "shape unexpected"
        calls = ",".join(x.get("name", "?") for x in j.get("tool_calls", [])) or "-"
        rows.append({"label": text, "total": ms, "ok": ok,
                     "reply": f"{calls} :: {(j.get('reply') or '')[:50]}"})
        print(f"[{'PASS' if ok else 'FAIL'} {ms}ms] {text[:40]} -> {calls} ({note})")

    acc = sum(1 for r in rows if r["ok"])
    print(f"accuracy: {acc}/{len(rows)}")
    with open("eval/results/import.md", "a") as f:
        f.write("\n## Original-source voice turns (live muse-spark-1.3 minimal)\n\n")
        f.write(f"Accuracy: {acc}/{len(rows)}. Import + start, then three asks.\n\n")
        f.write("| turn | total_ms | pass | reply |\n")
        f.write("|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['label']} | {r['total']} | {'y' if r['ok'] else 'n'} | {r['reply']} |\n")
    print("appended source_check")


main()
