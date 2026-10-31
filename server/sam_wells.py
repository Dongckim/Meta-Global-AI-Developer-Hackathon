"""Which wells did a pipette tip go into, from a glasses clip? SAM 3.1 tracks
the plate and the tip; code puts the plate's 8 x 12 grid on each frame and
reads the tip end's well. Test only: eval/sam_video_wells.py and the
Diagnostics "SAM well test" (server/lab_sam.py) share this; the product
flow never calls it.

1. ffmpeg pulls every frame at full size. SAM (image) finds "96-well plate"
   and "pipette tip": the first frames that have one, then the most
   confident of the next few sampled frames.
2. Those boxes prompt one SAM video request (objects 1 plate, 2 tip),
   propagated both ways. When the tip goes missing for a while, it's found
   again after the gap and prompted there too (same id), and the request
   runs again.
3. Each frame: plate mask -> four corners -> homography to the plate's
   footprint in mm (SBS: 127.76 x 85.48, A1 at 14.38 / 11.24, pitch 9) ->
   the tip end's well.
4. "Went in": the tip end stays within dwell_mm for >= dwell_s. One camera
   has no depth, so "lowest" is the dwell frame where the least tip shows
   (the well wall hides it).
"""

import json
import math
import random
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from server import look

BASE_URL = "https://api.meta.ai/v1"
PLATE, TIP = "96-well plate", "pipette tip"
PLATE_MM = (127.76, 85.48)  # ANSI/SLAS footprint
A1_MM, PITCH_MM, WELL_D_MM = (14.38, 11.24), 9.0, 6.86
DWELL_S, DWELL_MM = 0.3, 2.5
#: The settings the session summary compares (same clips, no new SAM calls).
DWELL_S_GRID, DWELL_MM_GRID = (0.2, 0.3, 0.5), (2.0, 2.5, 3.5)
#: First find: sample every SCAN_S from the start; of the first hit and the
#: BEST_OF - 1 samples after it, the most confident.
SCAN_S, BEST_OF = 0.5, 5
#: The tip missing this long (after it was seen) is found again and re-prompted.
GAP_S, MAX_ROUNDS = 0.5, 4
MAX_CLIP_S = 60
ROWS = "ABCDEFGH"
#: Targets skip the edge (rows A and H, columns 1 and 12), as the demo protocol does.
INNER = [f"{r}{c}" for r in ROWS[1:-1] for c in range(2, 12)]
CORNERS = ("tl", "tr", "br", "bl")


class ClipTooLong(ValueError):
    pass


# ---------- targets and judging ----------

def random_targets(rng: random.Random) -> list:
    """3-4 distinct inner wells, in the order to fill them."""
    return rng.sample(INNER, rng.choice((3, 4)))


def parse_targets(text: str) -> list:
    """"C3, F7 b10" -> ["C3", "F7", "B10"]; ValueError for a bad or repeated well."""
    out = [w.upper() for w in re.split(r"[\s,→>-]+", text or "") if w]
    for w in out:
        m = re.fullmatch(r"([A-H])(\d{1,2})", w)
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise ValueError(f"not a well: {w}")
    if not out or len(set(out)) != len(out):
        raise ValueError("give 1 or more wells, each once")
    return out


def judge(targets: list, found: list) -> dict:
    """Each target hit or not, whether the hits came in order, wells found
    that weren't targets, and one line ("3/4 matched · order differs")."""
    hits = [w for w in targets if w in found]
    seen = []
    for w in found:
        if w in targets and w not in seen:
            seen.append(w)
    false = [w for w in found if w not in targets]
    order_ok = seen == hits  # the targets found came in the targets' order
    line = f"{len(hits)}/{len(targets)} matched"
    if not order_ok:
        line += " · order differs"
    if false:
        line += f" · {len(false)} false"
    return {"wells": [{"well": w, "ok": w in found} for w in targets], "hits": len(hits),
            "total": len(targets), "order_ok": order_ok, "false": false, "line": line,
            "all_in_order": len(hits) == len(targets) and order_ok}


# ---------- geometry (pure python: .venv has no numpy) ----------

def solve(a: list, b: list) -> list:
    """Gaussian elimination with partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(m[r][i]))
        m[i], m[p] = m[p], m[i]
        if abs(m[i][i]) < 1e-12:
            raise ValueError("singular")
        for r in range(n):
            if r != i:
                f = m[r][i] / m[i][i]
                m[r] = [x - f * y for x, y in zip(m[r], m[i])]
    return [m[i][n] / m[i][i] for i in range(n)]


def homography(src: list, dst: list) -> list:
    """3x3 (h33 = 1) taking 4 src points to 4 dst points."""
    a, b = [], []
    for (x, y), (u, v) in zip(src, dst):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    h = solve(a, b)
    return [h[0:3], h[3:6], h[6:8] + [1.0]]


def apply(h: list, p: tuple) -> tuple:
    x, y = p
    w = h[2][0] * x + h[2][1] * y + h[2][2]
    return ((h[0][0] * x + h[0][1] * y + h[0][2]) / w, (h[1][0] * x + h[1][1] * y + h[1][2]) / w)


def well_center_mm(r: int, c: int) -> tuple:
    return (A1_MM[0] + PITCH_MM * c, A1_MM[1] + PITCH_MM * r)


def well_at(mm: tuple) -> str | None:
    """The well whose circle holds this point (plate mm), else None."""
    c = round((mm[0] - A1_MM[0]) / PITCH_MM)
    r = round((mm[1] - A1_MM[1]) / PITCH_MM)
    if not (0 <= r < 8 and 0 <= c < 12):
        return None
    return f"{ROWS[r]}{c + 1}" if math.dist(mm, well_center_mm(r, c)) <= WELL_D_MM / 2 else None


def corners(points: list) -> list:
    """tl, tr, br, bl of a roughly rectangular blob (extremes of x+y, x-y)."""
    return [min(points, key=lambda p: p[0] + p[1]), max(points, key=lambda p: p[0] - p[1]),
            max(points, key=lambda p: p[0] + p[1]), min(points, key=lambda p: p[0] - p[1])]


def plate_quad(img_corners: list, a1: str) -> list:
    """Image corners ordered A1, A12, H12, H1 (A1 -> A12 is the long side)."""
    i = CORNERS.index(a1)
    q = img_corners
    cw, ccw = q[(i + 1) % 4], q[(i - 1) % 4]
    if math.dist(q[i], cw) >= math.dist(q[i], ccw):
        return [q[i], cw, q[(i + 2) % 4], ccw]
    return [q[i], ccw, q[(i + 2) % 4], cw]


def plate_h(quad: list) -> list:
    w, h = PLATE_MM
    return homography(quad, [(0, 0), (w, 0), (w, h), (0, h)])


def solve_inv(h: list) -> list:
    """mm -> image: the plate corners mapped back, then their homography."""
    w, hh = PLATE_MM
    mm = [(0, 0), (w, 0), (w, hh), (0, hh)]
    img = []
    for q in mm:
        a = [[h[0][0] - q[0] * h[2][0], h[0][1] - q[0] * h[2][1]],
             [h[1][0] - q[1] * h[2][0], h[1][1] - q[1] * h[2][1]]]
        b = [q[0] * h[2][2] - h[0][2], q[1] * h[2][2] - h[1][2]]
        img.append(tuple(solve(a, b)))
    return homography(mm, img)


def pitch_px(h: list) -> float:
    """Well pitch in pixels at the plate's middle."""
    inv = solve_inv(h)
    c = well_center_mm(3, 5)
    p0, px, py = apply(inv, c), apply(inv, (c[0] + PITCH_MM, c[1])), apply(inv, (c[0], c[1] + PITCH_MM))
    return (math.dist(p0, px) + math.dist(p0, py)) / 2


def tip_end(points: list, toward: tuple) -> tuple:
    """The end of the tip's long axis nearer `toward` (the plate's middle)."""
    n = len(points)
    mx, my = sum(p[0] for p in points) / n, sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ax = (math.cos(ang), math.sin(ang))
    proj = lambda p: (p[0] - mx) * ax[0] + (p[1] - my) * ax[1]  # noqa: E731
    ends = [min(points, key=proj), max(points, key=proj)]
    return min(ends, key=lambda p: math.dist(p, toward))


def dwells(track: list, fps: float, dwell_s: float = DWELL_S, dwell_mm: float = DWELL_MM) -> list:
    """track: [(frame, (x_mm, y_mm), tip_area)] in frame order -> [(f0, f1, rep_frame, mm)]."""
    out, i = [], 0
    while i < len(track):
        j = i
        while (j + 1 < len(track) and track[j + 1][0] - track[j][0] <= 3
               and math.dist(track[j + 1][1], track[i][1]) <= dwell_mm):
            j += 1
        if (track[j][0] - track[i][0]) / fps >= dwell_s - 1e-9:
            rep = min(track[i:j + 1], key=lambda t: t[2])
            out.append((track[i][0], track[j][0], rep[0], rep[1]))
            i = j + 1
        else:
            i += 1
    return out


def found_wells(track: list, fps: float, dwell_s: float = DWELL_S, dwell_mm: float = DWELL_MM) -> list:
    """[(well, rep_frame, f0, f1)]: each dwell over a well; the same well twice in a row is one."""
    found = []
    for f0, f1, rep, mm in dwells(track, fps, dwell_s, dwell_mm):
        w = well_at(mm)
        if w and (not found or found[-1][0] != w):
            found.append((w, rep, f0, f1))
    return found


def compare_settings(attempts: list) -> dict:
    """attempts: [(targets, track, fps)] -> every dwell_s x dwell_mm on all of
    them, and the best (most wells hit, then fewest false, then most in order)."""
    rows = []
    for s in DWELL_S_GRID:
        for mm in DWELL_MM_GRID:
            hits = total = false = in_order = 0
            for targets, track, fps in attempts:
                j = judge(targets, [w for w, *_ in found_wells(track, fps, s, mm)])
                hits, total = hits + j["hits"], total + j["total"]
                false, in_order = false + len(j["false"]), in_order + j["all_in_order"]
            rows.append({"dwell_s": s, "dwell_mm": mm, "hits": hits, "total": total,
                         "false": false, "in_order": in_order})
    best = max(range(len(rows)), key=lambda i: (rows[i]["hits"], -rows[i]["false"], rows[i]["in_order"],
                                                 rows[i]["dwell_s"] == DWELL_S and rows[i]["dwell_mm"] == DWELL_MM))
    return {"rows": rows, "best": best}


# ---------- frames and SAM ----------

def ffprobe(clip: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                          "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames,codec_name"
                          ":stream_side_data=rotation:format=duration", "-of", "json", str(clip)],
                         capture_output=True, text=True, check=True)
    j = json.loads(out.stdout)
    s = j["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    rot = next((abs(int(d.get("rotation", 0))) for d in s.get("side_data_list", [])), 0)
    w, h = (s["height"], s["width"]) if rot in (90, 270) else (s["width"], s["height"])
    fps = int(num) / int(den)
    frames = int(s["nb_read_frames"])
    return {"w": w, "h": h, "fps": fps, "frames": frames, "codec": s["codec_name"],
            "duration_s": float(j.get("format", {}).get("duration") or frames / fps)}


def frames_dir(clip: Path) -> Path:
    d = Path(tempfile.mkdtemp(prefix="samwells_"))
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vsync", "0", str(d / "%05d.png")], check=True)
    return d


def frame_png(d: Path, n: int) -> Path:
    return d / f"{n + 1:05d}.png"


class MetaSam:
    """SAM 3.1 on the Meta Model API (the key from .env, never sent elsewhere)."""

    def __init__(self, key: str, timeout: float = 600):
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.client = httpx.Client(timeout=timeout)

    def image(self, target: str, png: Path) -> list:
        body = look.build_sam_body(target, look.data_url(png.read_bytes(), "image/png"))
        r = self.client.post(f"{BASE_URL}/responses", json=body,
                             headers={**self.headers, "Accept": "text/event-stream"})
        r.raise_for_status()
        return look.matches(look.sse_text(r.text))

    def upload(self, clip: Path) -> dict:
        """The clip as an input_video part: Files API, else a data URL."""
        mime = "video/quicktime" if clip.suffix.lower() == ".mov" else "video/mp4"
        try:
            r = self.client.post(f"{BASE_URL}/files", headers={"Authorization": self.headers["Authorization"]},
                                 data={"purpose": "user_data"}, files={"file": (clip.name, clip.read_bytes(), mime)})
            r.raise_for_status()
            return {"type": "input_video", "file_id": r.json()["id"]}
        except Exception as e:
            print(f"sam wells: files upload failed ({type(e).__name__}: {str(e)[:200]}); a data URL", flush=True)
            return {"type": "input_video", "video_url": look.data_url(clip.read_bytes(), mime)}

    def video(self, part: dict, prompt: str) -> tuple[str, float]:
        """The streamed text, and seconds to the first frame line."""
        body = {"model": look.SAM_MODEL, "stream": True,
                "metadata": {"propagation_direction": "both", "include_confidence": "true"},
                "input": [{"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": prompt}, part]}]}
        t0, first, chunks = time.perf_counter(), None, []
        with self.client.stream("POST", f"{BASE_URL}/responses", json=body,
                                headers={**self.headers, "Accept": "text/event-stream"}) as r:
            if r.status_code >= 400:
                r.read()
                raise RuntimeError(f"sam video {r.status_code}: {r.text[:500]}")
            for line in r.iter_lines():
                if not line.startswith("data:") or line[5:].strip() in ("", "[DONE]"):
                    continue
                try:
                    ev = json.loads(line[5:])
                except ValueError:
                    continue
                if ev.get("type") == "response.output_text.delta":
                    first = first or time.perf_counter() - t0
                    chunks.append((ev.get("delta") or "") + "\n")
                elif ev.get("type") in ("response.failed", "error"):
                    raise RuntimeError(f"sam video failed: {json.dumps(ev)[:500]}")
        return "".join(chunks), first or 0.0


def video_records(text: str) -> dict:
    """{frame: {obj_id: match}} from the per-frame lines."""
    out: dict = {}
    for m in re.finditer(r"<(\d+)f>(.*?)(?=<\d+f>|\Z)", text, re.S):
        frame = int(m.group(1))
        for rec in re.split(r",(?=\d+<\|box)", m.group(2).strip()):
            idm = re.match(r"(\d+)<\|box", rec)
            got = look.matches(rec)
            if idm and got:
                out.setdefault(frame, {})[int(idm.group(1))] = got[0]
    return out


def mask_points(match: dict, step: int = 1) -> list:
    """Mask pixels as source-pixel points (cell centers); the box corners when unreadable."""
    x1, y1, x2, y2 = match["box"]
    try:
        from meta_sam_parser import decode_mask_to_raster
        hh, ww, payload = (match["mask"] or {}).get("data", "").split(",", 2)
        rows, cols = int(hh), int(ww)
        raster = decode_mask_to_raster(look._parser_mask(payload, cols, rows))
    except Exception:
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    sx, sy = (x2 - x1 + 1) / cols, (y2 - y1 + 1) / rows
    pts = [(x1 + (c + 0.5) * sx, y1 + (r + 0.5) * sy)
           for r in range(0, rows, step) for c in range(0, cols, step) if raster[r * cols + c]]
    return pts or [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def best_detect(sam, target: str, fdir: Path, nframes: int, fps: float, start: int = 0):
    """(frame, match): the first sampled frame from `start` with the target,
    then the most confident of it and the next BEST_OF - 1 samples."""
    step = max(1, round(fps * SCAN_S))
    first = None
    for n in range(start, nframes, step):
        if sam.image(target, frame_png(fdir, n)):
            first = n
            break
    if first is None:
        return None
    best = None
    for n in range(first, min(nframes, first + step * BEST_OF), step):
        got = sam.image(target, frame_png(fdir, n))
        if got and (best is None or got[0]["c"] > best[1]["c"]):
            best = (n, got[0])
    return best


def object_prompt(prompts: dict, size: tuple) -> str:
    """{obj_id: [(frame, match)]} -> SAM's box-prompt text."""
    from meta_sam_parser import FramePrompt, PromptBox, PromptObject, build_object_prompt
    objs = []
    for oid, items in prompts.items():
        fps_ = []
        for n, m in items:
            x1, y1, x2, y2 = m["box"]
            box = PromptBox(left=max(0, x1), top=max(0, y1),
                            right=min(x2 + 1, size[0]), bottom=min(y2 + 1, size[1]))
            fps_.append(FramePrompt(frame=n, box=box))
        objs.append(PromptObject(id=oid, prompts=fps_))
    return build_object_prompt(size=size, objects=objs)


def tip_gap(recs: dict, nframes: int, after: int, gap: int) -> int | None:
    """The first frame after `after` that starts `gap` frames with no tip,
    once the tip was seen after `after`."""
    seen, run = False, 0
    for n in range(after, nframes):
        if 2 in recs.get(n, {}):
            seen, run = True, 0
        elif seen:
            run += 1
            if run >= gap:
                return n - gap + 1
    return None


# ---------- drawing ----------

def draw(png: Path, out: Path, plate, tip, quad, h, end, well, title: str) -> None:
    from PIL import Image, ImageDraw
    img = Image.open(png).convert("RGB")
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for m, col in ((plate, (40, 120, 255, 70)), (tip, (255, 60, 60, 110))):
        if m:
            for x, y in mask_points(m, 2):
                d.rectangle([x - 1, y - 1, x + 1, y + 1], fill=col)
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    d = ImageDraw.Draw(img)
    if quad:
        d.line([*quad, quad[0]], fill=(255, 220, 0), width=2)
        for name, p in zip(("A1", "A12", "H12", "H1"), quad):
            d.text((p[0] + 4, p[1] + 4), name, fill=(255, 220, 0))
    if h:
        inv = solve_inv(h)
        rad = max(2, pitch_px(h) * WELL_D_MM / PITCH_MM / 2)
        for r in range(8):
            for c in range(12):
                p = apply(inv, well_center_mm(r, c))
                hit = f"{ROWS[r]}{c + 1}" == well
                d.ellipse([p[0] - rad, p[1] - rad, p[0] + rad, p[1] + rad],
                          outline=(0, 230, 0) if hit else (255, 255, 255), width=3 if hit else 1)
    if end:
        d.ellipse([end[0] - 5, end[1] - 5, end[0] + 5, end[1] + 5], fill=(255, 0, 255))
    d.rectangle([0, 0, img.width, 22], fill=(0, 0, 0))
    d.text((6, 5), title, fill=(255, 255, 255))
    img.save(out, "JPEG", quality=85)


# ---------- one clip ----------

def analyze(clip: Path, a1: str, sam, out_dir: Path) -> dict:
    """Everything one clip gives: clip facts, the tip track (plate mm, so
    other dwell settings rerun without SAM), wells found, failures, timing,
    check images in out_dir."""
    t, t_all = {}, time.perf_counter()
    info = ffprobe(clip)
    if info["duration_s"] > MAX_CLIP_S + 0.5:
        raise ClipTooLong(f"clip is {info['duration_s']:.0f} s (max {MAX_CLIP_S} s)")
    t0 = time.perf_counter()
    fdir = frames_dir(clip)
    nframes = len(list(fdir.glob("*.png")))
    fps = info["fps"]
    t["frames_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    plate0 = best_detect(sam, PLATE, fdir, nframes, fps)
    tip0 = best_detect(sam, TIP, fdir, nframes, fps)
    t["first_find_s"] = time.perf_counter() - t0
    missing = [x for x, d in ((PLATE, plate0), (TIP, tip0)) if not d]
    if missing:
        raise RuntimeError(f"SAM found no {' and no '.join(missing)} on any sampled frame")
    size = (plate0[1]["w"], plate0[1]["h"])
    prompts = {1: [plate0], 2: [tip0]}

    t0 = time.perf_counter()
    part = sam.upload(clip)
    t["upload_s"] = time.perf_counter() - t0
    t["video_s"], t["video_first_frame_s"], t["refind_s"] = 0.0, 0.0, 0.0
    gap = max(2, round(fps * GAP_S))
    rounds = 0
    while True:
        rounds += 1
        t0 = time.perf_counter()
        text, first = sam.video(part, object_prompt(prompts, size))
        t["video_s"] += time.perf_counter() - t0
        t["video_first_frame_s"] = t["video_first_frame_s"] or first
        recs = video_records(text)
        if rounds >= MAX_ROUNDS:
            break
        g = tip_gap(recs, nframes, max(n for n, _ in prompts[2]), gap)
        if g is None:
            break
        t0 = time.perf_counter()
        again = best_detect(sam, TIP, fdir, nframes, fps, start=g)
        t["refind_s"] += time.perf_counter() - t0
        if not again or any(n == again[0] for n, _ in prompts[2]):
            break
        prompts[2].append(again)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sam_video.txt").write_text(text)

    track, per, pitches = [], {}, []
    fails = {"no_plate": 0, "no_tip": 0, "bad_grid": 0}
    for n in range(nframes):
        r = recs.get(n, {})
        plate, tip = r.get(1), r.get(2)
        if not plate:
            fails["no_plate"] += 1
            continue
        try:
            quad = plate_quad(corners(mask_points(plate, 2)), a1)
            h = plate_h(quad)
            pitches.append(pitch_px(h))
            mid = apply(solve_inv(h), (PLATE_MM[0] / 2, PLATE_MM[1] / 2))
        except ValueError:
            fails["bad_grid"] += 1
            continue
        per[n] = (plate, tip, quad, h, None, None)
        if not tip:
            fails["no_tip"] += 1
            continue
        pts = mask_points(tip)
        end = tip_end(pts, mid)
        mm = apply(h, end)
        per[n] = (plate, tip, quad, h, end, well_at(mm))
        track.append((n, mm, len(pts)))
    found = found_wells(track, fps)

    images = []
    pick = {rep: w for w, rep, *_ in found}
    for n in [round(i * (nframes - 1) / 3) for i in range(4)]:
        pick.setdefault(n, "spread")
    for n in [n for n in range(nframes) if n not in per][:2]:
        pick.setdefault(n, "fail")
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    for n, why in sorted(pick.items()):
        plate, tip, quad, h, end, well = per.get(n, (None,) * 6)
        if not plate:
            plate, tip = recs.get(n, {}).get(1), recs.get(n, {}).get(2)
        name = f"{n:05d}_{why}.jpg"
        draw(frame_png(fdir, n), img_dir / name, plate, tip, quad, h, end, well,
             f"frame {n} ({n / fps:.1f}s) {why}  tip well={well}")
        images.append(name)
    for f in fdir.glob("*.png"):
        f.unlink()
    fdir.rmdir()

    pitches.sort()
    pitch = pitches[len(pitches) // 2] if pitches else 0.0
    failed = sum(fails.values())
    t["total_s"] = time.perf_counter() - t_all
    return {**info, "frames": nframes, "sam_frames": len(recs), "rounds": rounds,
            "prompts": {str(k): [n for n, _ in v] for k, v in prompts.items()},
            "track": [[n, mm[0], mm[1], a] for n, mm, a in track],
            "found": [{"well": w, "rep": rep, "f0": f0, "f1": f1} for w, rep, f0, f1 in found],
            "pitch_px": pitch, "well_px": pitch * WELL_D_MM / PITCH_MM,
            "fails": fails, "failed_ratio": failed / nframes if nframes else 1.0,
            "timing": t, "images": images}


def track_of(result: dict) -> list:
    """A saved result's track back in analyze's shape."""
    return [(n, (x, y), a) for n, x, y, a in result["track"]]
