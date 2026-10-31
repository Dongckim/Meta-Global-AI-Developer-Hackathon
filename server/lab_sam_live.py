"""Diagnostics "SAM well test", Continuous: nobody says or lists the wells. The app records
without stopping and uploads overlapping chunks as they close; each is analysed at once
(sam_wells.track_chunk), and the wells come from all finished chunks joined on one frame
timeline, so a stay across a chunk boundary is one stay. Kept in
server/data/lab/sam_live/<session>/ (never committed).
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from server import lab_sam
from server import sam_wells as sw

router = APIRouter(prefix="/lab/sam-wells/live")
ROOT = Path(__file__).resolve().parent / "data" / "lab" / "sam_live"
MAX_CHUNK_BYTES = 100_000_000
_SID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
#: Open sessions, in memory (the reference holds an image).
_LIVE: dict = {}


class Live:
    def __init__(self, sid: str, a1: str, pointer: str):
        self.id, self.a1, self.pointer, self.dir = sid, a1, pointer, ROOT / sid
        self.lock = threading.Lock()
        self.chunks: dict = {}  # n -> {"n", "start", "status", ...}
        self.best = None  # the reference
        self.refs: list = []  # every change of it
        self.stopped = False
        self.fps = None
        self.wells: list = []
        self.created = time.time()

    def ref(self) -> dict | None:
        with self.lock:
            return self.best

    def offer(self, cand: dict) -> None:
        """Keeps the first reference, then one clearly better."""
        with self.lock:
            if self.best and cand["score"] < self.best["score"] + sw.REF_MARGIN:
                return
            was = self.best
            self.best = cand
            self.refs.append({k: cand[k] for k in ("chunk", "frame", "wells", "score")} | {"t": time.time() - self.created})
        print(f"sam live {self.id}: reference {'set' if was is None else 'replaced'}: chunk {cand['chunk']} "
              f"frame {cand['frame']}, {cand['wells']} wells, score {cand['score']:.2f}"
              + (f" (was {was['score']:.2f})" if was else ""), flush=True)

    def _judge(self) -> None:
        """The wells on the frames every chunk before has finished (results can land out of order,
        and a late chunk must not change what's shown). An earlier chunk's frame stands. Holds the lock."""
        joined: dict = {}
        for n in sorted(self.chunks):
            c = self.chunks[n]
            if n and n - 1 not in self.chunks and not self.stopped or c["status"] == "analyzing":
                break
            for f, x, y, a in c.get("track", ()):
                joined.setdefault(c["start"] + f, (c["start"] + f, (x, y), a))
        if self.fps:
            self.wells = sw.filled([joined[f] for f in sorted(joined)], self.fps)

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

    def out(self) -> dict:
        return {"id": self.id, "a1": self.a1, "pointer": self.pointer, "fps": self.fps, "wells": self.wells,
                "stopped": self.stopped, "done": self.stopped and all(c["status"] != "analyzing" for c in self.chunks.values()),
                "chunks": [{k: v for k, v in c.items() if k != "track"} for _, c in sorted(self.chunks.items())],
                "references": self.refs}

    def _save(self) -> None:
        (self.dir / "live.json").write_text(json.dumps({**self.out(), "tracks": {
            n: {"start": c["start"], "track": c.get("track", [])} for n, c in self.chunks.items()}}))


class LiveIn(BaseModel):
    a1: str = "tl"
    pointer: str = "pen"


@router.post("")
def new_live(body: LiveIn):
    if body.a1 not in sw.CORNERS:
        raise HTTPException(400, f"a1 is one of {', '.join(sw.CORNERS)}")
    if body.pointer not in sw.POINTERS:
        raise HTTPException(400, f"pointer is one of {', '.join(sw.POINTERS)}")
    sid = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    live = _LIVE[sid] = Live(sid, body.a1, body.pointer)
    live.dir.mkdir(parents=True)
    return live.out()


def _live(sid: str) -> Live:
    if not _SID.fullmatch(sid) or sid not in _LIVE:
        raise HTTPException(404, "no such session")
    return _LIVE[sid]


@router.get("/{sid}")
def live_state(sid: str):
    return _live(sid).out()


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
