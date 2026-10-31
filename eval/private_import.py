"""Protocol import on Glimmer vs Spark: the real /protocols/import, same PDFs.

Run from repo root (needs PRIVATE_LLM_URL and MODEL_API_KEY in .env):
    .venv/bin/python eval/private_import.py
Appends to eval/results/private_import.md.
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

FILES = ["protocol_demo_v2.pdf", "protocol_table.pdf", "protocol_wash.pdf"]
RESULTS = Path("eval/results/private_import.md")


def log(text: str) -> None:
    print(text, flush=True)
    with RESULTS.open("a") as f:
        f.write(text + "\n")


def run(name: str, mode: str) -> dict:
    api.tone.set_run_mode(mode)
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    api.store.wake.begin()
    api.store.wake.finish(True)
    meta, real = [], api._post

    async def counted(client, who, url, **k):
        if "private" not in who:
            meta.append(url)
        return await real(client, who, url, **k)

    api._post = counted
    buf = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        r = TestClient(api.app).post("/protocols/import", data={"session": "e", "original_id": "o"},
                                     files={"file": (name, (Path("samples") / name).read_bytes(), "application/pdf")})
    ms = int((time.perf_counter() - t0) * 1000)
    api._post = real
    usage = re.search(r"in_tok=(\S+) out_tok=(\S+)", buf.getvalue())
    return {"status": r.status_code, "ms": ms, "body": r.json(), "meta": len(meta),
            "in": usage and usage.group(1), "out": usage and usage.group(2)}


def steps(res: dict) -> list:
    return ((res["body"].get("draft") or {}).get("plan") or {}).get("steps") or []


def key(step: dict) -> dict:
    p = step.get("pipette") or {}
    return {"kind": step.get("kind"), "volume": p.get("volume"), "unit": p.get("unit"), "liquid": p.get("liquid"),
            "target": json.dumps(p.get("target"), sort_keys=True), "minutes": step.get("minutes")}


def photo() -> None:
    """One page as a picture: Glimmer transcribes it, then extracts."""
    from PIL import Image, ImageDraw
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader("samples/protocol_wash.pdf").pages)
    page = Image.new("RGB", (1400, 900), "white")
    ImageDraw.Draw(page).multiline_text((60, 60), text, fill="black", font_size=30)
    raw = io.BytesIO()
    page.save(raw, "JPEG", quality=90)
    api.tone.set_run_mode("private")
    api.store = RecordStore(path=tempfile.mkdtemp() + "/r.json")
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        r = TestClient(api.app).post("/protocols/import", data={"session": "e", "original_id": "o"},
                                     files={"file": ("page1.jpg", raw.getvalue(), "image/jpeg")})
    d = r.json().get("draft") or {}
    log(f"\nPhoto of protocol_wash on Glimmer: status {r.status_code}, {time.perf_counter() - t0:.1f} s, "
        f"{len((d.get('plan') or {}).get('steps') or [])} steps, errors {len(d.get('errors') or [])}, "
        f"review {len(d.get('review') or [])}")
    if r.status_code != 200:
        log(str(r.json())[:300])
    api.tone.set_run_mode(None)


def main() -> None:
    log(f"\n## Private import ({time.strftime('%F %T')})\n")
    log("| file | model | status | s | in tok | out tok | steps | conditions | errors | review | Meta calls |")
    log("|---|---|---|---|---|---|---|---|---|---|---|")
    for name in FILES:
        got = {}
        for label, mode in (("glimmer", "private"), ("spark", "full")):
            res = got[label] = run(name, mode)
            d = res["body"].get("draft") or {}
            log(f"| {name} | {label} | {res['status']} | {res['ms'] / 1000:.1f} | {res['in']} | {res['out']} | "
                f"{len(steps(res))} | {len((d.get('plan') or {}).get('conditions') or {})} | "
                f"{len(d.get('errors') or [])} | {len(d.get('review') or [])} | {res['meta']} |")
            if res["status"] != 200:
                log(f"  {label}: {str(res['body'])[:300]}")
        a, b = steps(got["glimmer"]), steps(got["spark"])
        diffs = [(i + 1, k, key(x)[k], key(y)[k]) for i, (x, y) in enumerate(zip(a, b))
                 for k in key(x) if key(x)[k] != key(y)[k]]
        log(f"\n{name}: {len(a)} vs {len(b)} steps, differences (step, field, glimmer, spark): {diffs or 'none'}")
        for label in got:
            d = got[label]["body"].get("draft") or {}
            log(f"{label} errors: {[x.get('reason') for x in d.get('errors') or []]} "
                f"review: {[x.get('reason') for x in d.get('review') or []]}\n")
    api.tone.set_run_mode(None)


main()
photo()
