"""Diagnostics "SAM well test": tap-only, no registry, record or voice.
A session is ATTEMPTS clips of a tip going into target wells; each clip is
analysed by server/sam_wells.py and judged against its targets. Kept in
server/data/lab/sam_wells/<session>/<attempt>/ (never committed).
"""

import json
import os
import random
import re
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server import sam_wells as sw

router = APIRouter(prefix="/lab/sam-wells")
ROOT = Path(__file__).resolve().parent / "data" / "lab" / "sam_wells"
ATTEMPTS = 5
MAX_UPLOAD_BYTES = 500_000_000
_SID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_IMG = re.compile(r"\d{5}_[A-Za-z0-9]+\.jpg")
#: One analysis at a time (each is minutes of SAM calls).
_LOCK = threading.Lock()


def make_sam():
    """The real SAM; tests put a fake here."""
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise RuntimeError("MODEL_API_KEY is not set (check .env)")
    return sw.MetaSam(key)


def _dir(sid: str) -> Path:
    if not _SID.fullmatch(sid) or not (ROOT / sid / "session.json").exists():
        raise HTTPException(404, "no such session")
    return ROOT / sid


def _read(p: Path) -> dict:
    return json.loads(p.read_text())


def _write(p: Path, data: dict) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def _attempts(d: Path) -> list:
    return [_read(p) for p in sorted(d.glob("*/attempt.json"), key=lambda p: int(p.parent.name))]


def summary(attempts: list) -> dict | None:
    """Over the analysed attempts: wells hit, attempts all in order, false
    wells, mean analysis time, clip sizes, well size, failed frames, and the
    dwell settings compared on the same clips."""
    done = [a for a in attempts if a["status"] == "done"]
    if not done:
        return None
    js = [a["judge"] for a in done]
    rs = [a["result"] for a in done]
    hits, total = sum(j["hits"] for j in js), sum(j["total"] for j in js)
    frames = sum(r["frames"] for r in rs)
    well_px = sorted(r["well_px"] for r in rs)
    return {
        "attempts": len(done), "complete": len(done) >= ATTEMPTS,
        "hits": hits, "total": total, "accuracy": hits / total if total else 0.0,
        "in_order": sum(j["all_in_order"] for j in js), "false": sum(len(j["false"]) for j in js),
        "mean_analysis_s": sum(r["timing"]["total_s"] for r in rs) / len(rs),
        "sizes": sorted({f"{r['w']}x{r['h']} {r['fps']:.0f} fps" for r in rs}),
        "well_px": well_px[len(well_px) // 2],
        "failed_ratio": sum(r["failed_ratio"] * r["frames"] for r in rs) / frames if frames else 1.0,
        "settings": sw.compare_settings([(a["targets"], sw.track_of(a["result"]), a["result"]["fps"])
                                         for a in done]),
    }


def _public(a: dict) -> dict:
    """An attempt without its track (big, and only the summary needs it)."""
    if a.get("result"):
        a = {**a, "result": {k: v for k, v in a["result"].items() if k != "track"}}
    return a


def _session_out(d: Path) -> dict:
    s = _read(d / "session.json")
    atts = _attempts(d)
    return {**s, "attempts": [_public(a) for a in atts], "summary": summary(atts)}


@router.get("/targets")
def targets():
    return {"targets": sw.random_targets(random.Random())}


class SessionIn(BaseModel):
    a1: str = "tl"
    pointer: str = sw.TIP


@router.post("/sessions")
def new_session(body: SessionIn):
    if body.a1 not in sw.CORNERS:
        raise HTTPException(400, f"a1 is one of {', '.join(sw.CORNERS)}")
    if body.pointer not in sw.POINTERS:
        raise HTTPException(400, f"pointer is one of {', '.join(sw.POINTERS)}")
    sid = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    d = ROOT / sid
    d.mkdir(parents=True)
    _write(d / "session.json", {"id": sid, "created": time.time(), "a1": body.a1, "pointer": body.pointer})
    return _session_out(d)


@router.get("/sessions")
def sessions():
    """Newest first: date, sources, clip sizes, accuracy so far."""
    out = []
    for p in sorted(ROOT.glob("*/session.json"), reverse=True):
        s, atts = _read(p), _attempts(p.parent)
        sm = summary(atts)
        out.append({**s, "attempts": len(atts), "sources": sorted({a["source"] for a in atts}),
                    "sizes": sm["sizes"] if sm else [], "accuracy": sm["accuracy"] if sm else None,
                    "hits": sm["hits"] if sm else 0, "total": sm["total"] if sm else 0})
    return out


@router.get("/sessions/{sid}")
def session(sid: str):
    return _session_out(_dir(sid))


def _analyze(d: Path, session: dict) -> None:
    """Background: SAM, then the judge; the attempt says done or failed."""
    p = d / "attempt.json"
    a = _read(p)
    last = [0.0, ""]

    def say(stage: str, done: int | None = None, total: int | None = None) -> None:
        """How far it is, on the attempt (written at most twice a second, and on each new stage)."""
        now = time.time()
        if stage == last[1] and now - last[0] < 0.5:
            return
        last[:] = [now, stage]
        a["progress"] = {"stage": stage, "done": done, "total": total, "elapsed_s": now - started}
        _write(p, a)

    started = time.time()
    say("Waiting for the attempt before")
    with _LOCK:
        started = time.time()
        try:
            clip = next(d.glob("clip.*"))
            r = sw.analyze(clip, session["a1"], make_sam(), d, session.get("pointer", sw.TIP), say)
            a.update(status="done", result=r, judge=sw.judge(a["targets"], [f["well"] for f in r["found"]]))
        except Exception as e:
            print(f"sam wells: attempt {d} failed: {type(e).__name__}: {e}", flush=True)
            a.update(status="failed", error=f"{type(e).__name__}: {e}")
        a.pop("progress", None)
    _write(p, a)


@router.post("/sessions/{sid}/attempts")
async def new_attempt(sid: str, background: BackgroundTasks, clip: UploadFile = File(...),
                      targets: str = Form(...), source: str = Form("stream")):
    """One clip and its target wells (in order); analysed in the background."""
    d = _dir(sid)
    try:
        wells = sw.parse_targets(targets)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if source not in ("stream", "file"):
        raise HTTPException(400, "source is stream or file")
    ext = Path(clip.filename or "").suffix.lower()
    if ext not in (".mp4", ".mov"):
        raise HTTPException(400, "need an .mp4 or .mov clip")
    n = len(list(d.glob("*/attempt.json"))) + 1
    if n > ATTEMPTS:
        raise HTTPException(409, f"a session is {ATTEMPTS} attempts")
    ad = d / str(n)
    ad.mkdir()
    size = 0
    with open(ad / f"clip{ext}", "wb") as f:
        while chunk := await clip.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                shutil.rmtree(ad)
                raise HTTPException(413, "clip too big")
            f.write(chunk)
    a = {"n": n, "targets": wells, "source": source, "filename": clip.filename, "status": "analyzing",
         "created": time.time()}
    _write(ad / "attempt.json", a)
    background.add_task(_analyze, ad, _read(d / "session.json"))
    return a


@router.get("/sessions/{sid}/attempts/{n}/images/{name}")
def image(sid: str, n: int, name: str):
    p = _dir(sid) / str(n) / "images" / name
    if not _IMG.fullmatch(name) or not p.exists():
        raise HTTPException(404, "no such image")
    return FileResponse(p, media_type="image/jpeg")
