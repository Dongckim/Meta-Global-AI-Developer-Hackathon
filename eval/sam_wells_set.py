"""The fixed SAM well test set, judged from cached SAM output in
server/data/lab/sam_cache/<clip>/ (never committed). Changing the grid, tip
end or dwell needs no SAM call: only a clip with no cache calls SAM, once.

eval/sam_wells_set.json lists each clip: its wells, "where" (desk or hand),
"view" (the plate's long side: portrait or landscape) and "use". "tune" clips
only guard against getting worse, they say nothing of the 90% bar. "confirm"
clips are filmed later and never tuned on; one off the design's conditions
(docs/design/sam_voice_anchored.md) is left out, with why. Clips run at the
app's rate: a faster one is cut to APP_FPS first.

The plate following (ORB) is kept in the cache too, until its code or values change.

Run from repo root:
    .venv/bin/python eval/sam_wells_set.py [--save] [--why "reason"]
Prints the tables (also eval/results/sam_wells_set.md). A clip that hits fewer
wells or has more false ones than eval/sam_wells_baseline.json exits 1. --save
takes this run as the baseline; if a judging value changed it needs --why,
kept in eval/sam_wells_changes.md.
"""
import argparse
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from server import sam_wells as sw  # noqa: E402

LAB = Path("server/data/lab/sam_wells")
CACHE = Path("server/data/lab/sam_cache")
SET = Path("eval/sam_wells_set.json")
BASELINE = Path("eval/sam_wells_baseline.json")
CHANGES = Path("eval/sam_wells_changes.md")
OUT = Path("eval/results/sam_wells_set.md")
APP_FPS = 7
#: Every judging value; a change to one must say why.
VALUES = ("DWELL_S", "DWELL_MM", "MAX_MISSING", "EDGE_TOL", "FIT_TRIES", "MIN_EDGE_POINTS", "MAX_SKEW",
          "MAX_VIEW_SKEW", "MAX_VIEW_RATIO", "ORB_FEATURES", "MATCH_PITCHES", "MIN_INLIERS", "MIN_INLIER_SHARE",
          "MAX_BITS", "PLATE_MARGIN", "MIN_BASE_SIZE", "EDGE_GAP", "MIN_SHOWN")
#: What a confirm clip must be (the design's filming conditions).
DESIGN = {"where": "desk", "view": "portrait", "w": 360, "h": 640}


def cache_dir(clip: str) -> Path:
    return CACHE / clip.replace("/", "_")


def fill(clip: str) -> None:
    """A clip's cache: its saved SAM output when that is at the app's rate, else SAM, once."""
    d, c = LAB / clip, cache_dir(clip)
    if (c / "meta.json").exists():
        return
    c.mkdir(parents=True, exist_ok=True)
    session = json.loads((d.parent / "session.json").read_text())
    pointer = session.get("pointer") or "pen"
    src = next(d.glob("clip.*"))
    info = sw.ffprobe(src)
    if info["fps"] > APP_FPS + 0.5:
        src = c / f"clip_{APP_FPS}fps.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(next(d.glob("clip.*"))), "-vf", f"fps={APP_FPS}",
                        "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(src)], check=True)
        info = sw.ffprobe(src)
        print(f"{clip}: SAM at {APP_FPS} fps", flush=True)
        sw.analyze(src, session["a1"], sw.MetaSam(os.environ["MODEL_API_KEY"]), c, pointer)
    else:
        shutil.copy(d / "sam_video.txt", c)
        for name in ("detections.json",):
            if (d / name).exists():
                shutil.copy(d / name, c)
    recs = sw.video_records((c / "sam_video.txt").read_text())
    (c / "frames.json").write_text(json.dumps(sw.compact(recs)))
    (c / "meta.json").write_text(json.dumps({"fps": info["fps"], "frames": info["frames"], "a1": session["a1"],
                                             "pointer": pointer, "w": info["w"], "h": info["h"]}))


def follow_key() -> str:
    """Changes with the following's code or values."""
    code = [sw.follow_plate, sw._follow, sw._region, sw.best_frame, sw.touches_edge, sw.distorted, sw.plate_corners]
    text = "".join(inspect.getsource(f) for f in code) + json.dumps(values())
    return hashlib.sha1(text.encode()).hexdigest()


def followed(item: dict, recs: dict, frames: int) -> dict:
    """follow_plate's result for this clip, from the cache when it's still good."""
    c, key = cache_dir(item["clip"]), follow_key()
    saved = c / "follow.json"
    if saved.exists():
        f = json.loads(saved.read_text())
        if f["key"] == key:
            return {**f, "quads": {int(n): q for n, q in f["quads"].items()}}
    video = next(c.glob("clip_*fps.mp4"), None) or next((LAB / item["clip"]).glob("clip.*"))
    t0 = time.perf_counter()
    f = {**sw.follow_plate(video, recs, frames), "seconds": 0.0, "key": key}
    f["seconds"] = time.perf_counter() - t0
    saved.write_text(json.dumps(f))
    return f


def run(item: dict) -> dict:
    c = cache_dir(item["clip"])
    meta = json.loads((c / "meta.json").read_text())
    recs = sw.expand(json.loads((c / "frames.json").read_text()))
    f = followed(item, recs, meta["frames"])
    track, per, *_ = sw.locate(recs, meta["frames"], meta["a1"], quads=f["quads"])
    found = [w for w, *_ in sw.found_wells(track, meta["fps"])]
    j = sw.judge(item["targets"], found)
    return {**item, "fps": meta["fps"], "size": f"{meta['w']}x{meta['h']}", "w": meta["w"], "h": meta["h"],
            "found": found, "hits": j["hits"], "total": j["total"], "false": j["false"],
            "missed": [w["well"] for w in j["wells"] if not w["ok"]],
            "bent": sum(sw.distorted(p[2]) for p in per.values() if p[2]), "frames": meta["frames"],
            "ref": f["ref"], "redone": f["redone"], "kept": f["kept"], "seconds": f["seconds"]}


def off_design(r: dict) -> list:
    """Why a confirm clip doesn't count: the conditions it misses."""
    return [f"{k} is {r[k]}, needs {v}" for k, v in DESIGN.items() if r[k] != v]


def values() -> dict:
    return {k: getattr(sw, k) for k in VALUES}


def table(rows: list, base: dict) -> tuple:
    lines, worse = ["| clip | where | view | size | fps | hit | missed | false | bent | redone | kept | ref | follow s "
                    "| found | baseline |", "|" + "---|" * 15], []
    for r in rows:
        b = base.get(r["clip"])
        down = b and (r["hits"] < b["hits"] or len(r["false"]) > b["false"])
        if down:
            worse.append(r["clip"])
        was = f"{b['hits']}/{r['total']}, {b['false']} false" if b else "-"
        lines.append(f"| {r['clip']} | {r['where']} | {r['view']} | {r['size']} | {r['fps']:.0f} "
                     f"| {r['hits']}/{r['total']} | {' '.join(r['missed']) or '-'} | {' '.join(r['false']) or '-'} "
                     f"| {r['bent']}/{r['frames']} | {r['redone']} | {r['kept']} | {r['ref']} | {r['seconds']:.1f} "
                     f"| {' '.join(r['found']) or '-'} | {'WORSE than ' if down else ''}{was} |")
    for where in ("desk", "hand"):
        rs = [r for r in rows if r["where"] == where]
        lines.append(f"\n{where}: {sum(r['hits'] for r in rs)}/{sum(r['total'] for r in rs)} wells, "
                     f"{sum(len(r['false']) for r in rs)} false, {len(rs)} clips")
    return lines, worse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--why", default="")
    a = ap.parse_args()
    load_dotenv(".env")
    items = json.loads(SET.read_text())
    for i in items:
        fill(i["clip"])
    t0 = time.perf_counter()
    with Pool(len(items)) as pool:
        rows = pool.map(run, items)
    took = time.perf_counter() - t0
    saved = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    base = saved.get("clips", {})
    lines, worse = [], []
    tune = [r for r in rows if r["use"] == "tune"]
    fair = [r for r in rows if r["use"] == "confirm" and not off_design(r)]
    left = [r for r in rows if r["use"] == "confirm" and off_design(r)]
    for title, rs in (("Tuning clips: regression check only, not for the 90% bar", tune),
                      ("Confirm clips (never tuned on; this is the 90% bar)", fair)):
        part, down = table(rs, base) if rs else (["none yet"], [])
        lines += [f"## {title}", *part, ""]
        worse += down
    if left:
        lines += ["## Left out of the confirm clips", *(f"- {r['clip']}: {'; '.join(off_design(r))}" for r in left), ""]
    lines.append(f"recomputed {len(rows)} clips from the cache in {took:.1f} s")
    print("\n".join(lines))
    OUT.write_text("\n".join(lines) + "\n")
    if a.save:
        now, was = values(), saved.get("values")
        moved = {k: (was.get(k), v) for k, v in now.items() if was and was.get(k) != v}
        if moved and not a.why:
            sys.exit("a judging value changed: say why with --why")
        if moved or not was:
            with open(CHANGES, "a") as f:
                what = ", ".join(f"{k} {o} -> {n}" for k, (o, n) in moved.items()) or f"start: {now}"
                f.write(f"\n## {time.strftime('%Y-%m-%d')}\n- {what}\n- why: {a.why or 'first baseline'}\n")
        BASELINE.write_text(json.dumps({"values": now, "clips": {r["clip"]: {"hits": r["hits"], "false": len(r["false"]), "bent": r["bent"]}
                                                                 for r in rows}}, indent=2) + "\n")
    elif worse:
        sys.exit(f"worse than the baseline: {', '.join(worse)}")


if __name__ == "__main__":
    main()
