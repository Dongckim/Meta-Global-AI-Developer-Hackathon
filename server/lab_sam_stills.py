"""Diagnostics "One a second": a photo a second instead of video. Each photo is judged as it lands:
SAM finds the pen, and the plate on every PLATE_EVERY-th photo or when the view moved; a photo's own
plate (or the last usable one before it) gives the
grid, corrected by the session's grid correction from the first REF_PHOTOS photos, and the wells the pen stayed in for at least MIN_STILLS photos show on the page right away.
No ORB: at a second apart it lost the plate on two of three saved sessions, and it needs every photo
at once. Everything stays in server/data/lab/sam_stills/<session>/ (never committed).
"""

import json
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from server import lab_sam
from server import sam_wells as sw

router = APIRouter(prefix="/lab/sam-wells/stills")
ROOT = Path(__file__).resolve().parent / "data" / "lab" / "sam_stills"
MIN_STILLS = 2  # one photo in a well is the pen passing over it
PLATE_EVERY = 3  # a plate call costs what two pen calls do; at every photo the queue only grows
#: Mean change of a 45x80 gray picture (0-255) since the last plate call that means the head moved. On
#: 3 saved sessions, 12 flagged 63% of the moves of half a well or more, and 88% of what it flagged moved.
MOVED = 12
#: Head turn in rad/s above which a photo isn't judged. shortcut: from blur on paper (13 px in an exposure,
#: no saved session has IMU), set it from the gyros in stills.json once sessions have them.
GYRO_MAX = 1.0
REF_PHOTOS = 3  # as Watching's frames at Record
MAX_GAP = 2  # a photo with no pen between two of the same well doesn't end the stay
_SID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_POOL = ThreadPoolExecutor(6)
_RUNS: dict = {}


def wells_of(track: list) -> list:
    """track: [(photo, (x_mm, y_mm), tip_area)] -> the stays, in order: [{"well", "photos", ...}].
    The well is the one the most photos in a row fall in; a pass over another well doesn't split a stay."""
    runs: list = []
    for n, mm, _ in track:
        cell = sw.cell_at(mm)
        if not cell:
            continue
        if runs and runs[-1]["well"] == cell and n - runs[-1]["ns"][-1] <= MAX_GAP:
            runs[-1]["ns"].append(n)
            runs[-1]["mms"].append(mm)
        else:
            runs.append({"well": cell, "ns": [n], "mms": [mm]})
    stays: list = []
    for r in (r for r in runs if len(r["ns"]) >= MIN_STILLS):
        if stays and stays[-1]["well"] == r["well"]:
            stays[-1]["ns"] += r["ns"]
            stays[-1]["mms"] += r["mms"]
        else:
            stays.append(r)
    return stays


class Run:
    def __init__(self, sid: str, a1: str, pointer: str):
        self.id, self.a1, self.pointer = sid, a1, pointer
        self.dir = ROOT / sid
        self.lock = threading.Lock()
        self.quads: dict = {}  # photo: its plate's corners
        self.pens: dict = {}  # photo: its pen, decoded
        self.ends: dict = {}  # photo: where the tip was, in px
        self.track: dict = {}  # photo: (mm, tip area)
        self.look_s: list = []
        self.gyros: dict = {}  # photo: head turn when taken, rad/s
        self.asked, self.asked_view, self.stale = None, None, False  # the last plate call
        self.plate_calls = self.shaken = 0
        self.stills: list = []  # the first photos: [(photo, gray, png)]
        self.fit, self.fit_wells = None, 0  # the session's grid correction
        self.jobs: list = []
        self.taken = self.looked = self.failed = self.skipped = 0
        self.status, self.error, self.wells, self.took_s = "recording", None, [], None
        self.size = None

    def out(self) -> dict:
        with self.lock:
            return {"id": self.id, "status": self.status, "taken": self.taken, "looked": self.looked,
                    "failed": self.failed, "skipped": self.skipped, "wells": self.wells, "took_s": self.took_s,
                    "look_s": round(statistics.median(self.look_s), 1) if self.look_s else None,
                    "fit_wells": self.fit_wells, "plate_calls": self.plate_calls, "shaken": self.shaken,
                    "error": self.error}


def before(found: dict, n: int):
    """The nearest entry before photo n, or None."""
    return found[max(k for k in found if k < n)] if any(k < n for k in found) else None


def plate_due(run: Run, n: int, gray) -> bool:
    """Whether photo n asks SAM for the plate: the first, every PLATE_EVERY-th, after a view change."""
    import cv2
    import numpy as np
    view = cv2.resize(gray, (45, 80), interpolation=cv2.INTER_AREA).astype(np.int16)
    with run.lock:
        due = run.asked is None or n - run.asked >= PLATE_EVERY or run.stale \
            or float(np.abs(view - run.asked_view).mean()) >= MOVED
        if due:
            run.asked, run.asked_view, run.stale = n, view, False
            run.plate_calls += 1
    return due


def own_grid(plates: list, pens: list, size: tuple) -> list | None:
    """This photo's plate corners; None when it shows none usable."""
    if not plates:
        return None
    plates = [{**m, "points": sw.mask_points(m, 2)} for m in plates]  # a decode takes 0.25 s: once each
    quad = sw.plate_corners(sw.mask_points(sw.best_plate(plates, size, pens), 2))
    return None if sw.distorted(quad) else quad


def stays_out(stays: list) -> list:
    out = []
    for s in stays:
        mid = sw.well_center_mm(sw.ROWS.index(s["well"][0]), int(s["well"][1:]) - 1)
        off = statistics.median(sw.math.dist(mm, mid) for mm in s["mms"])
        why = sw.not_sure_why(off, [sw.SURE_INLIERS])  # nothing is matched here: only off center counts
        out.append({"well": s["well"], "first": s["ns"][0], "photos": len(s["ns"]), "off_mm": round(off, 1),
                    "confidence": "not_sure" if why else "sure", "why": why})
    return out


def place(run: Run, n: int, plates: list, pens: list, size: tuple) -> None:
    """One photo's finds -> the wells so far. Photos can land out of order: wells_of takes them sorted."""
    quad = own_grid(plates, pens, size)
    pen = {**pens[0], "points": sw.mask_points(pens[0], 2)} if pens else None
    with run.lock:
        if quad:
            run.quads[n] = quad
        run.pens[n] = pen
        # the pen is the quicker call: photos after this one may have read on the grid before it
        later = [k for k in run.quads if k > n]
        again = sorted(k for k in run.pens if k == n or quad and k > n and (not later or k < min(later)))
    for k in again:
        read(run, k)


def read(run: Run, n: int) -> None:
    """Where the pen is on photo n, on the plate's grid with the session's correction when it's in."""
    with run.lock:
        grid, pen, fit = run.quads.get(n) or before(run.quads, n), run.pens.get(n), run.fit
        near = before(run.ends, n)
    if not grid or not pen:
        return
    track, per, *_ = sw.locate({0: {2: pen}}, 1, run.a1, quads={0: sw.corrected(grid, fit)}, near=near)
    if not track:
        return
    with run.lock:
        if run.fit is not fit:  # the correction came in meanwhile: set_fit reads this photo again
            return
        run.track[n] = (track[0][1], track[0][2])
        run.ends[n] = per[0][4]
        run.wells = stays_out(wells_of([(k, *run.track[k]) for k in sorted(run.track)]))


def set_fit(run: Run, ref: dict) -> None:
    """The correction is in: the photos judged without it are read again."""
    with run.lock:
        run.fit, run.fit_wells = ref["fit"], ref["wells"]
        ns = sorted(run.pens)
    for n in ns:
        read(run, n)


def _reference(run: Run) -> None:
    try:
        ref = sw.pick_reference(lab_sam.make_sam(), run.stills, run.pointer)
        print(f"sam stills {run.id}: reference " + (f"{ref['wells']} wells" if ref else "none"), flush=True)
        if ref:
            set_fit(run, ref)
    except Exception as e:  # the wells still come, uncorrected
        print(f"sam stills {run.id}: reference failed: {type(e).__name__}: {e}", flush=True)


def _look(run: Run, n: int, data: bytes, landed: float, gyro: float | None = None) -> None:
    """One photo: its plate as a PNG (JPEG lost it on a replay), its pen as a JPEG (a tenth of the bytes)."""
    import cv2
    import numpy as np
    try:
        with run.lock:
            if gyro is not None:
                run.gyros[n] = round(gyro, 2)
            if gyro is not None and gyro > GYRO_MAX:  # blurred, and the grid before it no longer holds
                run.shaken += 1
                run.looked += 1
                run.stale = True
                return
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        size = img.shape[1::-1]
        with run.lock:
            run.size = run.size or size
            if size != run.size:  # the stream dropped its size: an earlier grid doesn't fit this one
                run.skipped += 1
                return
        png = run.dir / f"{n:05d}.png"
        cv2.imwrite(str(png), img)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ask = plate_due(run, n, gray)
        with run.lock:
            if len(run.stills) < REF_PHOTOS:
                run.stills.append((n, gray, png))
                if len(run.stills) == REF_PHOTOS:
                    run.jobs.append(_POOL.submit(_reference, run))
        with ThreadPoolExecutor(2) as both:
            plates = both.submit(lab_sam.make_sam().image, sw.PLATE, png) if ask else None
            pens = both.submit(lab_sam.make_sam(light=True).image, run.pointer, png)
        place(run, n, plates.result() if ask else [], pens.result(), size)
        with run.lock:
            run.looked += 1
            run.look_s.append(time.perf_counter() - landed)
    except Exception as e:  # the photos around it still count
        print(f"sam stills {run.id}: photo {n} failed: {type(e).__name__}: {e}", flush=True)
        with run.lock:
            run.failed += 1


def _finish(run: Run) -> None:
    t0 = time.perf_counter()
    seen = 0
    while seen < len(run.jobs):  # a last photo can start the reference
        seen = len(run.jobs)
        wait(run.jobs)
    run.took_s = round(time.perf_counter() - t0, 1)
    run.status = "done"
    (run.dir / "stills.json").write_text(json.dumps({**run.out(), "a1": run.a1, "pointer": run.pointer, "gyros": run.gyros}, indent=1))


class StillsIn(BaseModel):
    a1: str
    pointer: str = sw.TIP


@router.post("")
def new_run(body: StillsIn):
    if body.a1 not in sw.CORNERS:
        raise HTTPException(400, f"a1 is one of {', '.join(sw.CORNERS)}")
    if body.pointer not in sw.POINTERS:
        raise HTTPException(400, f"pointer is one of {', '.join(sw.POINTERS)}")
    sid = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    run = _RUNS[sid] = Run(sid, body.a1, body.pointer)
    run.dir.mkdir(parents=True)
    return run.out()


def _run(sid: str) -> Run:
    if not _SID.fullmatch(sid) or sid not in _RUNS:
        raise HTTPException(404, "no such run")
    return _RUNS[sid]


@router.get("/{sid}")
def state(sid: str):
    return _run(sid).out()


@router.post("/{sid}/photo")
async def photo(sid: str, frames: list[UploadFile] = File(...), numbers: str = Form(...), gyro: float | None = Form(None)):
    run = _run(sid)
    wanted = [int(x) for x in numbers.split(",") if x.isdigit()]
    if len(wanted) != len(frames) or run.status != "recording":
        raise HTTPException(400, "a bad photo, or the run is over")
    for n, f in zip(wanted, frames):
        data = await f.read()
        run.taken += 1
        run.jobs.append(_POOL.submit(_look, run, n, data, time.perf_counter(), gyro))
    return {"frames": wanted}


@router.post("/{sid}/stop")
def stop(sid: str):
    run = _run(sid)
    if run.status == "recording":
        run.status = "finishing"
        threading.Thread(target=_finish, args=(run,), daemon=True).start()
    return run.out()
