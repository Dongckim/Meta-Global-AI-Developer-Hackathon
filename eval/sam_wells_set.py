"""The fixed SAM well test set, judged from cached SAM output in
server/data/lab/sam_cache/<clip>/ (never committed). Changing the grid, tip
end or dwell needs no SAM call: only a clip with no cache calls SAM, once.

eval/sam_wells_set.json lists each clip: its wells, "where" (desk is the main
measure, hand is reference) and "use": "tune" clips may steer the judging,
"confirm" clips (filmed later) are only read, never tuned on. Clips run at the
app's rate: a faster one is cut to APP_FPS first.

Run from repo root:
    .venv/bin/python eval/sam_wells_set.py [--save] [--why "reason"]
Prints the tables (also eval/results/sam_wells_set.md). A clip that hits fewer
wells or has more false ones than eval/sam_wells_baseline.json exits 1. --save
takes this run as the baseline; if a judging value changed it needs --why,
kept in eval/sam_wells_changes.md.
"""
import argparse
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
VALUES = ("DWELL_S", "DWELL_MM", "MAX_MISSING", "EDGE_TOL", "FIT_TRIES", "MIN_EDGE_POINTS", "MAX_SKEW")


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


def run(item: dict) -> dict:
    c = cache_dir(item["clip"])
    meta = json.loads((c / "meta.json").read_text())
    recs = sw.expand(json.loads((c / "frames.json").read_text()))
    track, *_ = sw.locate(recs, meta["frames"], meta["a1"])
    found = [w for w, *_ in sw.found_wells(track, meta["fps"])]
    j = sw.judge(item["targets"], found)
    return {**item, "fps": meta["fps"], "found": found, "hits": j["hits"], "total": j["total"], "false": j["false"],
            "missed": [w["well"] for w in j["wells"] if not w["ok"]]}


def values() -> dict:
    return {k: getattr(sw, k) for k in VALUES}


def table(rows: list, base: dict) -> tuple:
    lines, worse = ["| clip | where | fps | hit | missed | false | found | baseline |", "|---|---|---|---|---|---|---|---|"], []
    for r in rows:
        b = base.get(r["clip"])
        down = b and (r["hits"] < b["hits"] or len(r["false"]) > b["false"])
        if down:
            worse.append(r["clip"])
        was = f"{b['hits']}/{r['total']}, {b['false']} false" if b else "-"
        lines.append(f"| {r['clip']} | {r['where']} | {r['fps']:.0f} | {r['hits']}/{r['total']} "
                     f"| {' '.join(r['missed']) or '-'} | {' '.join(r['false']) or '-'} "
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
    for use, title in (("tune", "Tuning clips"), ("confirm", "Confirm clips (never tuned on)")):
        rs = [r for r in rows if r["use"] == use]
        part, down = table(rs, base) if rs else (["none yet"], [])
        lines += [f"## {title}", *part, ""]
        worse += down
    lines.append(f"recomputed {len(rows)} clips from the cache in {took:.1f} s")
    print("\n".join(lines))
    OUT.write_text("\n".join(lines) + "\n")
    if a.save:
        now, was = values(), saved.get("values")
        moved = {k: (was[k], v) for k, v in now.items() if was and was[k] != v}
        if moved and not a.why:
            sys.exit("a judging value changed: say why with --why")
        if moved or not was:
            with open(CHANGES, "a") as f:
                what = ", ".join(f"{k} {o} -> {n}" for k, (o, n) in moved.items()) or f"start: {now}"
                f.write(f"\n## {time.strftime('%Y-%m-%d')}\n- {what}\n- why: {a.why or 'first baseline'}\n")
        BASELINE.write_text(json.dumps({"values": now, "clips": {r["clip"]: {"hits": r["hits"], "false": len(r["false"])}
                                                                 for r in rows}}, indent=2) + "\n")
    elif worse:
        sys.exit(f"worse than the baseline: {', '.join(worse)}")


if __name__ == "__main__":
    main()
