"""Diagnostics "SAM well test", Continuous: nobody says or lists the wells. The app records
without stopping and uploads overlapping chunks as they close; each is analysed at once
(sam_wells.track_chunk), and the wells come from all finished chunks joined on one frame
timeline, so a stay across a chunk boundary is one stay. Kept in
server/data/lab/sam_live/<session>/ (never committed).
"""

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from server import lab_sam
from server import sam_wells as sw

router = APIRouter(prefix="/lab/sam-wells/live")
ROOT = Path(__file__).resolve().parent / "data" / "lab" / "sam_live"
REFS = ROOT.parent / "sam_refs"
MAX_CHUNK_BYTES = 100_000_000
#: Longest a chunk's image calls stay behind the session's first ones.
REF_FIRST_S = 30
_SID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
#: Open sessions, in memory (the reference holds an image).
_LIVE: dict = {}


class Live:
    def __init__(self, sid: str, a1: str, pointer: str, targets: list | None = None):
        self.id, self.a1, self.pointer, self.dir = sid, a1, pointer, ROOT / sid
        self.targets = targets or []  # debug only: what the tester says they'll fill
        self.lock = threading.Lock()
        self.chunks: dict = {}  # n -> {"n", "start", "status", ...}
        self.best = None  # the session's one reference, from the frames at Record
        self.ready = threading.Event()
        self.picking = False
        self.refs: list = []
        self.stopped = False
        self.fps = None
        self.wells: list = []
        self.created = time.time()

    def ref(self) -> dict | None:
        """The session's grid correction, if it's in yet; a chunk doesn't wait for it."""
        return self.best

    def first(self) -> None:
        """While the stills are being worked on, a chunk's image calls wait: those run first."""
        if self.picking:
            self.ready.wait(REF_FIRST_S)

    def set_reference(self, ref: dict | None) -> None:
        self.best = ref
        if ref:
            self.refs.append({"chunk": "record", "frame": ref["frame"], "wells": ref["wells"], "covered": ref["covered"]})
        print(f"sam live {self.id}: reference " + (f"frame {ref['frame']}, {ref['wells']} wells, covered {ref['covered']}" if ref else "none"), flush=True)
        self.ready.set()

    def _judge(self) -> None:
        """The wells on the frames every chunk before has finished (results can land out of order,
        and a late chunk must not change what's shown). An earlier chunk's frame stands. Holds the lock."""
        joined, inliers, holes = {}, {}, {}
        for n in sorted(self.chunks):
            c = self.chunks[n]
            if n and n - 1 not in self.chunks and not self.stopped or c["status"] == "analyzing":
                break
            for f, x, y, a in c.get("track", ()):
                joined.setdefault(c["start"] + f, (c["start"] + f, (x, y), a))
                holes.setdefault(c["start"] + f, c.get("ref_wells", 0))
            for f, count in c.get("inliers", {}).items():
                inliers.setdefault(c["start"] + int(f), count)
        if self.fps:
            self.wells = []
            for w in sw.filled([joined[f] for f in sorted(joined)], self.fps):
                frames = [f for f in joined if w["f0"] <= f <= w["frame"]]
                why = sw.not_sure_why(w["off_mm"], [inliers.get(f, 0) for f in frames])
                self.wells.append({**w, "confidence": "not_sure" if why else "sure", "why": why,
                                   "ref_wells": holes[w["frame"]],
                                   "inliers": sorted(inliers.get(f, 0) for f in frames)[len(frames) // 2]})

    def finish(self, n: int, result: dict) -> None:
        with self.lock:
            self.chunks[n].update(result)
            self.fps = self.fps or result.get("fps")
            self._judge()
            self._save()

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            self._judge()
            self._save()

    def summary(self) -> dict:
        """Sure and not-sure wells; with a target list, how those compare. to_catch: targets seen only as not sure."""
        sure = [w["well"] for w in self.wells if w["confidence"] == "sure"]
        unsure = {w["well"] for w in self.wells if w["confidence"] == "not_sure"}
        return {"sure": len(sure), "not_sure": sum(w["confidence"] == "not_sure" for w in self.wells),
                "hit": [w for w in self.targets if w in sure], "outside": [w for w in sure if w not in self.targets] if self.targets else [],
                "missed": [w for w in self.targets if w not in sure and w not in unsure],
                "to_catch": [w for w in self.targets if w not in sure and w in unsure]}

    def out(self) -> dict:
        return {"id": self.id, "a1": self.a1, "pointer": self.pointer, "fps": self.fps, "wells": self.wells,
                "targets": self.targets, "summary": self.summary(),
                "stopped": self.stopped, "done": self.stopped and all(c["status"] != "analyzing" for c in self.chunks.values()),
                "chunks": [{k: v for k, v in c.items() if k not in ("track", "inliers")} for _, c in sorted(self.chunks.items())],
                "references": self.refs}

    def _save(self) -> None:
        (self.dir / "live.json").write_text(json.dumps({**self.out(), "tracks": {
            n: {"start": c["start"], "track": c.get("track", []), "inliers": c.get("inliers", {})}
            for n, c in self.chunks.items()}}))


class LiveIn(BaseModel):
    a1: str = "tl"
    pointer: str = "pen"
    targets: str = ""


@router.post("")
def new_live(body: LiveIn):
    if body.a1 not in sw.CORNERS:
        raise HTTPException(400, f"a1 is one of {', '.join(sw.CORNERS)}")
    if body.pointer not in sw.POINTERS:
        raise HTTPException(400, f"pointer is one of {', '.join(sw.POINTERS)}")
    try:
        targets = sw.parse_targets(body.targets) if body.targets.strip() else []
    except ValueError as e:
        raise HTTPException(400, str(e))
    sid = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    live = _LIVE[sid] = Live(sid, body.a1, body.pointer, targets)
    live.dir.mkdir(parents=True)
    return live.out()


def _live(sid: str) -> Live:
    if not _SID.fullmatch(sid) or sid not in _LIVE:
        raise HTTPException(404, "no such session")
    return _LIVE[sid]


@router.get("/{sid}")
def live_state(sid: str):
    return _live(sid).out()


def _pick(live: Live, stills: list) -> None:
    """Background: the session's reference, saved as an image. stills: [(frame, jpeg or png bytes)]."""
    import cv2
    import numpy as np
    live.picking = True
    try:
        d = live.dir / "stills"
        d.mkdir()
        decoded = []
        for frame, data in stills:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            cv2.imwrite(str(d / f"{frame}.png"), img)
            decoded.append((frame, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), d / f"{frame}.png"))
        ref = sw.pick_reference(lab_sam.make_sam(), decoded, live.pointer)
        if ref:
            REFS.mkdir(exist_ok=True)
            shutil.copy(d / f"{ref['frame']}.png", REFS / f"{live.id}.png")
        live.set_reference(ref)
    except Exception as e:
        print(f"sam live {live.id}: reference failed: {type(e).__name__}: {e}", flush=True)
        live.set_reference(None)


@router.post("/{sid}/reference")
async def new_reference(sid: str, background: BackgroundTasks, frames: list[UploadFile] = File(...), numbers: str = Form(...)):
    """The frames at Record, named by `numbers` ("0,3,6"); the session's reference comes from these, once."""
    live = _live(sid)
    wanted = [int(x) for x in numbers.split(",") if x.isdigit()]
    if len(wanted) != len(frames) or live.ready.is_set():
        raise HTTPException(400, "a bad or repeated reference")
    background.add_task(_pick, live, [(n, await f.read()) for n, f in zip(wanted, frames)])
    return {"frames": wanted}


def _analyze(live: Live, n: int, clip: Path) -> None:
    try:
        r = sw.track_chunk(clip, live.a1, lab_sam.make_sam(), clip.parent, live.pointer, live)
        live.finish(n, {**r, "status": "done" if r["track"] else "empty"})
    except Exception as e:  # the chunks around it still count
        print(f"sam live {live.id}: chunk {n} failed: {type(e).__name__}: {e}", flush=True)
        live.finish(n, {"status": "failed", "error": f"{type(e).__name__}: {e}"})


@router.post("/{sid}/chunks")
async def new_chunk(sid: str, background: BackgroundTasks, clip: UploadFile = File(...),
                    n: int = Form(...), start: int = Form(...)):
    """Chunk n, its first frame being the session's frame `start`; analysed in the background, alongside the others."""
    live = _live(sid)
    if n < 0 or start < 0 or n in live.chunks:
        raise HTTPException(400, "a bad or repeated chunk")
    data = await clip.read(MAX_CHUNK_BYTES + 1)
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(413, "chunk too big")
    d = live.dir / str(n)
    d.mkdir()
    (d / "clip.mp4").write_bytes(data)
    with live.lock:
        live.chunks[n] = {"n": n, "start": start, "status": "analyzing", "created": time.time()}
    background.add_task(_analyze, live, n, d / "clip.mp4")
    return {"n": n}


@router.post("/{sid}/stop")
def stop(sid: str):
    """No more chunks are coming; the ones in hand finish, and `done` turns true."""
    _live(sid).stop()
    return _live(sid).out()
