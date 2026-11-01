"""Diagnostics "SAM latency": where a chunk's seconds go. One saved Watching chunk, sent to SAM from
this server as Watching sends it: PNG against JPEG, files of three sizes, one at a time and four at
once, each split into sending, waiting for SAM's answer and reading it. Lines go to the page and to
server/data/lab/sam_latency/ (never committed). Run with Watching off, or chunks skew it.
"""

import json
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from server import lab_sam, look
from server import sam_wells as sw

router = APIRouter(prefix="/lab/sam-wells/latency")
LIVE = Path(__file__).resolve().parent / "data" / "lab" / "sam_live"
OUT = Path(__file__).resolve().parent / "data" / "lab" / "sam_latency"
ROUNDS = 3
AT_ONCE = 4  # what Watching has in flight: 4 to 6 chunks in the saved sessions
CRFS = (18, 23, 28)
_GUARD = threading.Lock()
_STATE: dict = {"running": False, "step": "", "lines": [], "error": None}


def timed(client: httpx.Client, url: str, **kw) -> dict:
    """One POST, split at the last byte sent and the first answer (a text delta when SAM streams,
    the whole reply otherwise). Fails on an error status."""
    marks: dict = {}

    def trace(name: str, _info: dict) -> None:
        if name == "http11.send_request_body.complete":
            marks["sent"] = time.perf_counter()

    t0 = time.perf_counter()
    lines = []
    with client.stream("POST", url, extensions={"trace": trace}, **kw) as r:
        for line in r.iter_lines():
            if "first" not in marks and "response.output_text.delta" in line:
                marks["first"] = time.perf_counter()
            lines.append(line)
    end = time.perf_counter()
    if r.status_code >= 400:
        raise RuntimeError(f"{url} {r.status_code}: {' '.join(lines)[:300]}")
    sent = marks.get("sent", t0)
    answer = marks.get("first", end)
    return {"send_s": sent - t0, "wait_s": answer - sent, "read_s": end - answer, "total_s": end - t0, "body": "\n".join(lines)}


def line(name: str, rows: list, extra: str = "") -> str:
    """Medians of a group, with the slowest when it's more than one call."""
    med = {k: statistics.median(r[k] for r in rows) for k in ("send_s", "wait_s", "read_s", "total_s")}
    worst = f" (slowest {max(r['total_s'] for r in rows):.1f})" if len(rows) > 1 else ""
    return (f"{name}: send {med['send_s']:.1f} · wait {med['wait_s']:.1f} · read {med['read_s']:.1f} · "
            f"total {med['total_s']:.1f} s{worst}{extra}")


def _chunk() -> tuple:
    """The newest saved chunk SAM tracked at full size: (clip, pointer)."""
    for p in sorted(LIVE.glob("*/live.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        j = json.loads(p.read_text())
        for c in j.get("chunks", []):
            clip = p.parent / str(c["n"]) / "clip.mp4"
            if c.get("status") == "done" and (c.get("w"), c.get("h")) == (720, 1280) and clip.exists():
                return clip, j["pointer"]
    raise RuntimeError("no saved Watching chunk to measure")


def _measure(say) -> list:
    clip, pointer = _chunk()
    key = lab_sam.make_sam()
    client = httpx.Client(timeout=300)
    auth = {"Authorization": key.headers["Authorization"]}
    responses = f"{sw.BASE_URL}/responses"
    events = {**key.headers, "Accept": "text/event-stream"}
    info = sw.ffprobe(clip)
    nframes, fps = info["frames"], info["fps"]
    lines = [f"clip {clip.parent.parent.name}/{clip.parent.name}, {pointer}, {nframes} frames"]
    work = Path(tempfile.mkdtemp(prefix="samlatency_"))
    say("Finding the tip", lines)
    looks = [(2 * i + 1) * nframes // (2 * sw.CHUNK_TIP_LOOKS) for i in range(sw.CHUNK_TIP_LOOKS)]
    fdir = sw.frames_dir(clip, looks)
    try:
        tips = sw.hits_on(key, pointer, fdir, looks)
        if not tips:
            raise RuntimeError("no tip on this chunk")
        png = sw.frame_png(fdir, tips[0][0])
        jpg = work / "f.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(png), "-q:v", "3", str(jpg)], check=True, stdin=subprocess.DEVNULL)
        bodies = {name: look.build_sam_body(pointer, look.data_url(p.read_bytes(), mime))
                  for name, p, mime in (("png", png, "image/png"), ("jpeg", jpg, "image/jpeg"))}
        sizes = {"png": png.stat().st_size, "jpeg": jpg.stat().st_size}

        def image(name: str) -> dict:
            return timed(client, responses, json=bodies[name], headers=events)

        rows: dict = {"png": [], "jpeg": []}
        for i in range(ROUNDS):  # by turns: a slow minute lands on both
            for name in rows:
                say(f"Image {name} {i + 1} of {ROUNDS}", lines)
                rows[name].append(image(name))
        for name, got in rows.items():
            top = [look.matches(look.sse_text(r["body"])) for r in got]
            conf = ", ".join(f"{t[0]['c']:.2f}" if t else "none" for t in top)
            lines.append(line(f"image {name} {sizes[name] // 1024} KB", got, f"; tip {conf}"))
        with ThreadPoolExecutor(AT_ONCE) as pool:
            for name in rows:
                say(f"{AT_ONCE} images at once, {name}", lines)
                lines.append(line(f"{AT_ONCE} at once, image {name}", list(pool.map(image, [name] * AT_ONCE))))

        files: dict = {}
        for crf in CRFS:
            files[crf] = work / f"crf{crf}.mp4"
            sw.for_sam(clip, files[crf], fps, crf=crf)

        def upload(crf: int) -> dict:
            return timed(client, f"{sw.BASE_URL}/files", headers=auth, data={"purpose": "user_data"},
                         files={"file": (files[crf].name, files[crf].read_bytes(), "video/mp4")})

        ups: dict = {crf: [] for crf in CRFS}
        for i in range(ROUNDS):
            for crf in CRFS:
                say(f"Upload crf {crf}, {i + 1} of {ROUNDS}", lines)
                ups[crf].append(upload(crf))
        for crf, got in ups.items():
            size = files[crf].stat().st_size
            mbps = size * 8 / 1e6 / max(statistics.median(r["send_s"] for r in got), 1e-3)
            lines.append(line(f"upload crf {crf} {size / 1e6:.1f} MB", got, f"; {mbps:.1f} Mbps"))
        say(f"{AT_ONCE} uploads at once", lines)
        with ThreadPoolExecutor(AT_ONCE) as pool:
            lines.append(line(f"{AT_ONCE} at once, upload crf {CRFS[0]}", list(pool.map(upload, [CRFS[0]] * AT_ONCE))))

        size = (tips[0][1]["w"], tips[0][1]["h"])
        prompt = sw.object_prompt({2: tips}, size)
        for crf in CRFS[:2]:
            say(f"Video crf {crf}", lines)
            part = {"type": "input_video", "file_id": json.loads(ups[crf][-1]["body"])["id"]}
            got = timed(client, responses, json=sw.video_body(part, prompt), headers=events)
            recs = sw.video_records(look.sse_text(got["body"]))
            lines.append(line(f"video crf {crf}", [got], f"; tip on {sum(2 in v for v in recs.values())} of {nframes} frames"))
        return lines
    finally:
        client.close()
        shutil.rmtree(fdir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def _run() -> None:
    def say(step: str, lines: list) -> None:
        _STATE.update(step=step, lines=list(lines))

    try:
        lines = _measure(say)
        _STATE["lines"] = lines
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(lines, indent=1))
    except Exception as e:  # shown on the page: this is a test
        print(f"sam latency failed: {e}", flush=True)
        _STATE["error"] = str(e)[:300]
    _STATE.update(running=False, step="")


@router.post("", status_code=202)
def start():
    with _GUARD:
        if _STATE["running"]:
            raise HTTPException(409, "already measuring")
        _STATE.update(running=True, step="Starting", lines=[], error=None)
    threading.Thread(target=_run, daemon=True).start()
    return _STATE


@router.get("")
def state():
    return _STATE
