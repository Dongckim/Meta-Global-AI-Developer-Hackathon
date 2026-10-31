"""Which wells did a pipette tip go into, from a glasses clip? SAM 3.1 tracks
the plate and the tip; code puts the plate's 8 x 12 grid on each frame and
reads the tip end's well. Test only: eval/sam_video_wells.py and the
Diagnostics "SAM well test" (server/lab_sam.py) share this; the product
flow never calls it.

1. ffmpeg pulls the sampled frames (SAM sees every frame in the clip itself). SAM (image) finds "96-well plate"
   and "pipette tip": the first frames that have one, then the most
   confident of the next few sampled frames.
2. Those boxes prompt one SAM video request (objects 1 plate, 2 tip),
   propagated both ways. When the tip goes missing for a while, it's found
   again after the gap and prompted there too (same id), and the request
   runs again.
3. The plate is found once: its four edges cross at four corners on the frame that shows it
   most (follow_plate), and features matched to that frame carry those corners to the others; a
   frame that can't be matched finds its own edges. Each frame: corners -> homography to the plate's
   footprint in mm (SBS: 127.76 x 85.48, A1 at 14.38 / 11.24, pitch 9) ->
   the tip end's well. The tip end is the mask's long-axis end over the
   plate; both or neither: the one nearer the last frame's tip end (the
   first frame: nearer the plate's middle).
4. "Went in": the tip end stays within dwell_mm for >= dwell_s (up to
   MAX_MISSING frames without a tip don't break it); the visit's well is the
   cell it spent the most frames in. One camera has no depth, so "lowest" is
   the dwell frame where the least tip shows (the well wall hides it).
"""

import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import httpx

from server import look

BASE_URL = "https://api.meta.ai/v1"
PLATE, TIP = "96-well plate", "pipette tip"
#: What goes into the wells (SAM's prompt).
POINTERS = (TIP, "pen")
PLATE_MM = (127.76, 85.48)  # ANSI/SLAS footprint
A1_MM, PITCH_MM, WELL_D_MM = (14.38, 11.24), 9.0, 6.86
#: A visit is where the tip stays >= DWELL_S within DWELL_MM (half the well pitch).
DWELL_S, DWELL_MM, MAX_MISSING = 1.0, PITCH_MM / 2, 2
#: The settings the session summary compares (same clips, no new SAM calls).
DWELL_S_GRID, DWELL_MM_GRID = (0.5, 1.0, 2.0), (2.5, 3.5, 4.5)
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
#: Plate edges: within this share of the plate's diagonal is on the line; tries per edge; fewest
#: points to fit; most degrees between opposite edges.
EDGE_TOL, FIT_TRIES, MIN_EDGE_POINTS, MAX_SKEW = 0.01, 200, 8, 25.0
#: A grid past these isn't a view of a plate (degrees between opposite sides; their length ratio).
MAX_VIEW_SKEW, MAX_VIEW_RATIO = 20.0, 1.35
#: Following the plate from its best frame: ORB features, how far from the last frame's place a
#: match may be (in well pitches: the wells repeat, so a wider search lands one well over), and
#: the fewest inliers and share of them to trust; below that the frame finds its own edges.
ORB_FEATURES, MATCH_PITCHES, MIN_INLIERS, MIN_INLIER_SHARE = 2000, 1.0, 15, 0.3
MAX_BITS = 64  # of ORB's 256: a further pair isn't the same spot
#: The area around the plate features come from (x its size), and a frame fit to be the reference
#: shows at least this share of the clip's usual plate size.
PLATE_MARGIN, MIN_BASE_SIZE = 1.15, 0.85
#: The screen's edge is this share of its size from the plate's box; SAM's plate stands in for an
#: unmatched frame only when this much of it shows.
EDGE_GAP, MIN_SHOWN = 0.02, 0.9


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


# ---------- geometry ----------

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


def nearest_well(mm: tuple) -> tuple | None:
    """(row, col) of the nearest well's center, None off the 8 x 12 grid."""
    c = round((mm[0] - A1_MM[0]) / PITCH_MM)
    r = round((mm[1] - A1_MM[1]) / PITCH_MM)
    return (r, c) if 0 <= r < 8 and 0 <= c < 12 else None


def cell_at(mm: tuple) -> str | None:
    """The well whose pitch-sized cell holds this point (plate mm), else None."""
    rc = nearest_well(mm)
    return f"{ROWS[rc[0]]}{rc[1] + 1}" if rc else None


def well_at(mm: tuple) -> str | None:
    """The well whose circle holds this point (plate mm), else None."""
    rc = nearest_well(mm)
    return cell_at(mm) if rc and math.dist(mm, well_center_mm(*rc)) <= WELL_D_MM / 2 else None


def corners(points: list) -> list:
    """tl, tr, br, bl of a roughly rectangular blob (extremes of x+y, x-y)."""
    return [min(points, key=lambda p: p[0] + p[1]), max(points, key=lambda p: p[0] - p[1]),
            max(points, key=lambda p: p[0] + p[1]), min(points, key=lambda p: p[0] - p[1])]


def long_axis(points: list) -> tuple:
    """(middle, unit direction) of the points' best line."""
    n = len(points)
    mx, my = sum(p[0] for p in points) / n, sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return (mx, my), (math.cos(ang), math.sin(ang))


def outline(points: list) -> list:
    """The outermost point of each row and column: what's inside (a pen across the plate) drops out."""
    ends: dict = {}
    for p in points:
        for key, v in ((("row", round(p[1])), p[0]), (("col", round(p[0])), p[1])):
            lo, hi = ends.get(key, ((v, p), (v, p)))
            ends[key] = (min(lo, (v, p)), max(hi, (v, p)))
    return list({e[1] for pair in ends.values() for e in pair})


def hull(points: list) -> list:
    """The convex hull, in order (monotone chain)."""
    pts = sorted(set(points))

    def half(seq: list) -> list:
        out: list = []
        for p in seq:
            while len(out) > 1 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                    - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return half(pts) + half(pts[::-1])


def along(loop: list, step: float) -> list:
    """Points every `step` around a closed polygon, so a long edge counts for its length."""
    out = []
    for a, b in zip(loop, loop[1:] + loop[:1]):
        k = max(1, round(math.dist(a, b) / step))
        out += [(a[0] + (b[0] - a[0]) * i / k, a[1] + (b[1] - a[1]) * i / k) for i in range(k)]
    return out


def segment_dist(p: tuple, a: tuple, b: tuple) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy or 1)))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def skew(u: tuple, v: tuple) -> float:
    """Degrees between two undirected lines."""
    return math.degrees(math.acos(min(1.0, abs(u[0] * v[0] + u[1] * v[1]))))


def edge_points(points: list, tol: float, like: tuple | None = None) -> list:
    """The most points lying on one line within tol (RANSAC), so a stretch cut by a hand
    doesn't pull it. `like`: only lines within MAX_SKEW of this direction."""
    rng, best = random.Random(0), []
    for _ in range(FIT_TRIES):
        a, b = rng.sample(points, 2)
        d = math.dist(a, b)
        if d and (like is None or skew(((b[0] - a[0]) / d, (b[1] - a[1]) / d), like) <= MAX_SKEW):
            on = [p for p in points
                  if abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / d <= tol]
            if len(on) > len(best):
                best = on
    return best


def crossing(l1: tuple, l2: tuple) -> tuple:
    (p, u), (q, v) = l1, l2
    t = solve([[u[0], -v[0]], [u[1], -v[1]]], [q[0] - p[0], q[1] - p[1]])[0]
    return (p[0] + t * u[0], p[1] + t * u[1])


def plate_corners(points: list) -> list:
    """tl, tr, br, bl where the plate's four edges cross. The plate is convex, so the
    edges are on the mask's convex hull: a hand biting into an edge is bridged, and a
    corner under a hand is found from its two edges. Opposite edges are near parallel
    even in perspective: when two aren't, the one less square to the other pair is fitted
    again along its opposite (a long bridge over a hand isn't an edge). Too little outline: corners()."""
    seed = corners(points)
    tol = EDGE_TOL * math.dist(seed[0], seed[2])
    rim = along(hull(outline(points)), tol)
    sides: list = [[] for _ in range(4)]
    for p in rim:
        sides[min(range(4), key=lambda i: segment_dist(p, seed[i], seed[(i + 1) % 4]))].append(p)
    if min(map(len, sides)) < MIN_EDGE_POINTS:
        return seed
    edges = [edge_points(s, tol) for s in sides]
    for i in (0, 1):
        across = long_axis(max(edges[i + 1], edges[i - 1], key=len))[1]
        square = (-across[1], across[0])
        strong, weak = sorted((i, i + 2), key=lambda k: skew(long_axis(edges[k])[1], square))
        along_strong = long_axis(edges[strong])[1]
        if skew(long_axis(edges[weak])[1], along_strong) > MAX_SKEW:
            edges[weak] = edge_points(sides[weak], tol, along_strong) or edges[weak]
    lines = [long_axis(e) for e in edges]
    return [crossing(lines[i - 1], lines[i]) for i in range(4)]


def area_of(loop: list) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(loop, loop[1:] + loop[:1]))) / 2


def distorted(q: list) -> bool:
    """Too bent for any real view of a plate: opposite sides far from parallel or from equal."""
    sides = [(q[(i + 1) % 4][0] - q[i][0], q[(i + 1) % 4][1] - q[i][1]) for i in range(4)]
    units = [(x / (math.hypot(x, y) or 1), y / (math.hypot(x, y) or 1)) for x, y in sides]
    long = [math.hypot(*e) or 1 for e in sides]
    return (skew(units[0], units[2]) > MAX_VIEW_SKEW or skew(units[1], units[3]) > MAX_VIEW_SKEW
            or max(long[0] / long[2], long[2] / long[0], long[1] / long[3], long[3] / long[1]) > MAX_VIEW_RATIO)


def plate_quad(img_corners: list, a1: str) -> list:
    """Image corners ordered A1, A12, H12, H1 (A1 -> A12 is the long side)."""
    i = CORNERS.index(a1)
    q = img_corners
    cw, ccw, far = q[(i + 1) % 4], q[(i - 1) % 4], q[(i + 2) % 4]
    # both pairs of sides: one side alone is short under perspective
    if math.dist(q[i], cw) + math.dist(ccw, far) >= math.dist(q[i], ccw) + math.dist(cw, far):
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


def tip_ends(points: list) -> list:
    """The two ends of the tip's long axis."""
    (mx, my), ax = long_axis(points)
    proj = lambda p: (p[0] - mx) * ax[0] + (p[1] - my) * ax[1]  # noqa: E731
    return [min(points, key=proj), max(points, key=proj)]


def on_plate(mm: tuple) -> bool:
    return 0 <= mm[0] <= PLATE_MM[0] and 0 <= mm[1] <= PLATE_MM[1]


def dwells(track: list, fps: float, dwell_s: float = DWELL_S, dwell_mm: float = DWELL_MM) -> list:
    """track: [(frame, (x_mm, y_mm), tip_area)] in frame order -> [(f0, f1, rep_frame, well)],
    the well being the cell most of the visit's frames are in (None: all off the grid)."""
    out, i = [], 0
    while i < len(track):
        j = i
        while (j + 1 < len(track) and track[j + 1][0] - track[j][0] <= MAX_MISSING + 1
               and math.dist(track[j + 1][1], track[i][1]) <= dwell_mm):
            j += 1
        if (track[j][0] - track[i][0]) / fps >= dwell_s - 1e-9:
            visit = track[i:j + 1]
            rep = min(visit, key=lambda t: t[2])
            cells = Counter(c for c in (cell_at(t[1]) for t in visit) if c)
            out.append((track[i][0], track[j][0], rep[0], cells.most_common(1)[0][0] if cells else None))
            i = j + 1
        else:
            i += 1
    return out


def found_wells(track: list, fps: float, dwell_s: float = DWELL_S, dwell_mm: float = DWELL_MM) -> list:
    """[(well, rep_frame, f0, f1)]: each dwell over a well; the same well twice in a row is one."""
    found = []
    for f0, f1, rep, w in dwells(track, fps, dwell_s, dwell_mm):
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


def frames_dir(clip: Path, frames: list) -> Path:
    """A temp folder with these frames of the clip; frame_png pulls any other when asked."""
    d = Path(tempfile.mkdtemp(prefix="samwells_"))
    (d / "clip.txt").write_text(str(clip))
    pull(d, frames)
    return d


def pull(d: Path, frames: list) -> None:
    """These frames as PNGs named by frame number (one ffmpeg run, whatever the count)."""
    want = sorted(n for n in set(frames) if not (d / f"{n + 1:05d}.png").exists())
    if not want:
        return
    pick = "+".join(f"eq(n\\,{n})" for n in want)
    subprocess.run(["ffmpeg", "-v", "error", "-i", (d / "clip.txt").read_text(), "-vf", f"select='{pick}'",
                    "-vsync", "0", str(d / "got%05d.png")], check=True)
    for i, n in enumerate(want):
        (d / f"got{i + 1:05d}.png").rename(d / f"{n + 1:05d}.png")


def frame_png(d: Path, n: int) -> Path:
    pull(d, [n])
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

    def video(self, part: dict, prompt: str, on_frame=None) -> tuple[str, float]:
        """The streamed text, and seconds to the first frame line; on_frame(lines so far)."""
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
                    if on_frame:
                        on_frame(len(chunks))
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


def _quiet(stage: str, done: int | None = None, total: int | None = None) -> None:
    pass


def mask_points(match: dict, step: int = 1) -> list:
    """Mask pixels as source-pixel points (cell centers); the box corners when unreadable.
    A match from the cache (compact()) already has its points."""
    if "points" in match:
        return [tuple(p) for p in match["points"]]
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


def _points2(match: dict) -> list:
    return mask_points(match, 2)


def decode_masks(recs: dict, say=_quiet) -> None:
    """Every mask's points onto its match, on all CPUs: a mask is arithmetic-coded (each bit's
    odds depend on the last), so one can't be sped up, but they don't depend on each other."""
    todo = [m for r in recs.values() for m in r.values() if m.get("mask")]
    if not todo:
        return
    try:
        with ProcessPoolExecutor(os.cpu_count()) as pool:
            for i, (m, pts) in enumerate(zip(todo, pool.map(_points2, todo, chunksize=8))):
                m["points"] = pts
                say("Reading the masks", i, len(todo))
    except BrokenProcessPool as e:  # locate decodes what's left itself
        print(f"sam wells: mask workers failed ({e}); one at a time", flush=True)


def shown(match: dict) -> float:
    """The share of the mask's convex hull the mask fills: what a hand or pen leaves of the plate."""
    if "shown" in match:
        return match["shown"]
    pts = mask_points(match, 2)
    if not match.get("mask"):
        return 0.0
    rows, cols = map(int, match["mask"]["data"].split(",")[:2])
    x1, y1, x2, y2 = match["box"]
    cell = (x2 - x1 + 1) / cols * (y2 - y1 + 1) / rows
    return len(pts) * 4 * cell / (area_of(hull(outline(pts))) or 1)


def seen_points(match: dict) -> list:
    """Where the plate shows, thin enough to keep: the cache has every sixth pixel."""
    return match.get("seen") or mask_points(match, 2)


def compact(recs: dict) -> dict:
    """SAM's records as locate() reads them without decoding masks: JSON-ready. The plate
    keeps its outline (all plate_corners uses) and how much of it shows, the tip every
    other pixel (as locate)."""
    return {str(n): {str(oid): ({"box": m["box"], "points": outline(mask_points(m, 2)), "shown": shown(m),
                                 "seen": mask_points(m, 2)[::9]} if oid == 1
                                else {"box": m["box"], "points": mask_points(m, 2)})
                     for oid, m in r.items()} for n, r in recs.items()}


def expand(saved: dict) -> dict:
    """compact()'s JSON back in video_records' shape."""
    return {int(n): {int(oid): m for oid, m in r.items()} for n, r in saved.items()}


def scan_step(fps: float) -> int:
    return max(1, round(fps * SCAN_S))


def best_detect(sam, target: str, fdir: Path, nframes: int, fps: float, start: int = 0, say=_quiet):
    """(frame, match): the first sampled frame from `start` with the target,
    then the most confident of it and the next BEST_OF - 1 samples."""
    step = scan_step(fps)
    best = None
    for n in range(start, nframes, step):
        say(f"Looking for the {target}", n, nframes)
        got = sam.image(target, frame_png(fdir, n))
        if got:
            best = (n, got[0])
            break
    if best is None:
        return None
    say(f"Most confident {target} near frame {best[0]}", best[0], nframes)
    rest = list(range(best[0] + step, min(nframes, best[0] + step * BEST_OF), step))
    pull(fdir, rest)  # here, not from the threads below: they'd write the same temp names
    with ThreadPoolExecutor(BEST_OF) as pool:
        for n, got in zip(rest, pool.map(lambda n: sam.image(target, frame_png(fdir, n)), rest)):
            if got and got[0]["c"] > best[1]["c"]:
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


# ---------- the plate, found once ----------

def touches_edge(match: dict, size: tuple) -> bool:
    """The plate's box within EDGE_GAP of the screen's edge: part of it may be cut off."""
    x1, y1, x2, y2 = match["box"]
    gx, gy = EDGE_GAP * size[0], EDGE_GAP * size[1]
    return x1 < gx or y1 < gy or x2 > size[0] - gx or y2 > size[1] - gy


def best_frame(recs: dict, size: tuple) -> int | None:
    """The reference: the plate about its usual size, off the screen's edge, the most of it
    showing, and its opposite sides near parallel. None when no frame qualifies."""
    seen = {n: area_of(hull(outline(mask_points(r[1], 2)))) for n, r in recs.items() if 1 in r}
    if not seen:
        return None
    usual = sorted(seen.values())[len(seen) // 2]
    fit = [n for n in seen if seen[n] >= MIN_BASE_SIZE * usual and not touches_edge(recs[n][1], size)]
    for n in sorted(fit, key=lambda n: -shown(recs[n][1])):
        if not distorted(plate_corners(mask_points(recs[n][1], 2))):
            return n
    return None


def _region(gray, quad: list, tip: list | None, plate: list):
    """ORB's mask: the plate and a margin around it, without the tip, and without what covers
    the plate (a hand: inside the plate's corners, not in SAM's plate mask)."""
    import cv2
    import numpy as np
    corners_ = np.int32(quad)
    mid = np.mean(quad, axis=0)
    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillConvexPoly(mask, np.int32((np.array(quad) - mid) * PLATE_MARGIN + mid), 255)
    inside, seen = np.zeros(gray.shape, np.uint8), np.zeros(gray.shape, np.uint8)
    cv2.fillConvexPoly(inside, corners_, 255)
    h, w = gray.shape
    xy = np.int32(plate)
    xy = xy[(xy[:, 0] >= 0) & (xy[:, 0] < w) & (xy[:, 1] >= 0) & (xy[:, 1] < h)]
    seen[xy[:, 1], xy[:, 0]] = 255
    seen = cv2.dilate(seen, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    mask[(inside > 0) & (seen == 0)] = 0
    if tip:
        cv2.fillConvexPoly(mask, cv2.convexHull(np.int32(tip)), 0)
    return mask


def _follow(orb, base: tuple, gray, tip: list | None, plate: list, last, quad0: list, reach: float):
    """This frame's homography from the reference, or None. A match stays within `reach` px of
    where the last frame's homography puts it (the wells repeat)."""
    import cv2
    import numpy as np
    pts0, des0 = base
    guess = cv2.perspectiveTransform(np.float32(quad0).reshape(-1, 1, 2), last).reshape(-1, 2)
    kps, des = orb.detectAndCompute(gray, _region(gray, guess.tolist(), tip, plate))
    if des is None or len(kps) < MIN_INLIERS:
        return None
    pts = np.float32([k.pt for k in kps])
    moved = cv2.perspectiveTransform(pts0.reshape(-1, 1, 2), last).reshape(-1, 2)
    i, j = np.nonzero(np.linalg.norm(moved[:, None] - pts[None], axis=2) <= reach)
    bits = np.bitwise_count(des0[i] ^ des[j]).sum(axis=1)
    best = {}
    for k in np.argsort(bits).tolist():
        if bits[k] < MAX_BITS:
            best.setdefault(int(i[k]), int(j[k]))
    if len(best) < MIN_INLIERS:
        return None
    h, inliers = cv2.findHomography(pts0[list(best)], pts[list(best.values())], cv2.RANSAC, 3.0)
    if h is None or inliers.sum() < MIN_INLIERS or inliers.mean() < MIN_INLIER_SHARE:
        return None
    return h


def follow_plate(clip: Path, recs: dict, nframes: int) -> dict:
    """The plate's corners once, on its best frame, then carried to every frame by matching
    features to that frame. A frame that can't be matched takes SAM's plate if it's all in view,
    else stays where the last one was. -> {"quads": {frame: corners}, "ref", "redone", "kept"}."""
    import cv2
    import numpy as np
    video, frames = cv2.VideoCapture(str(clip)), {}
    for n in range(nframes):
        ok, img = video.read()
        if not ok:
            break
        frames[n] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    video.release()
    none = {"quads": {}, "ref": None, "redone": 0, "kept": 0}
    if not frames:
        return none
    h, w = frames[0].shape
    ref = best_frame(recs, (w, h))
    if ref is None or ref not in frames:
        return none
    quad0 = [tuple(map(float, p)) for p in plate_corners(mask_points(recs[ref][1], 2))]
    corners0 = np.float32(quad0).reshape(-1, 1, 2)
    orb = cv2.ORB_create(ORB_FEATURES)
    tip_at = lambda n: mask_points(recs[n][2], 2) if 2 in recs.get(n, {}) else None  # noqa: E731
    plate_at = lambda n: seen_points(recs[n][1]) if 1 in recs.get(n, {}) else [(0, 0)]  # noqa: E731
    kps, des = orb.detectAndCompute(frames[ref], _region(frames[ref], quad0, tip_at(ref), plate_at(ref)))
    if des is None or len(kps) < MIN_INLIERS:
        return none
    base = (np.float32([k.pt for k in kps]), des)
    reach = MATCH_PITCHES * pitch_px(plate_h(plate_quad(quad0, "tl")))
    out = {"quads": {ref: quad0}, "ref": ref, "redone": 0, "kept": 0}
    for way in (range(ref + 1, nframes), range(ref - 1, -1, -1)):
        last = np.eye(3)
        for n in way:
            if n not in frames:
                continue
            found = _follow(orb, base, frames[n], tip_at(n), plate_at(n), last, quad0, reach)
            if found is not None:
                quad = [tuple(map(float, p)) for p in cv2.perspectiveTransform(corners0, found).reshape(-1, 2)]
                if not distorted(quad):  # a good inlier count on the wrong wells bends the grid
                    last, out["quads"][n] = found, quad
                    continue
            plate = recs.get(n, {}).get(1)
            own = plate_corners(mask_points(plate, 2)) if plate and shown(plate) >= MIN_SHOWN \
                and not touches_edge(plate, (w, h)) else None
            if own and not distorted(own):
                last = cv2.getPerspectiveTransform(np.float32(quad0), np.float32(own))
                out["quads"][n] = own
                out["redone"] += 1
            else:
                out["quads"][n] = [tuple(map(float, p)) for p in cv2.perspectiveTransform(corners0, last).reshape(-1, 2)]
                out["kept"] += 1
    return out


# ---------- one clip ----------

def locate(recs: dict, nframes: int, a1: str, say=_quiet, quads: dict | None = None) -> tuple:
    """SAM's records -> (track, {frame: what to draw}, well pitches in px, failed frames by why).
    `quads` (follow_plate): each frame's plate corners; a frame without finds its own edges."""
    track, per, pitches, last = [], {}, [], None
    fails = {"no_plate": 0, "no_tip": 0, "bad_grid": 0}
    for n in range(nframes):
        say("Placing the grid on each frame", n, nframes)
        r = recs.get(n, {})
        plate, tip = r.get(1), r.get(2)
        if not plate:
            fails["no_plate"] += 1
            continue
        try:
            quad = plate_quad((quads or {}).get(n) or plate_corners(mask_points(plate, 2)), a1)
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
        pts = mask_points(tip, 2)
        ends = tip_ends(pts)
        over = [e for e in ends if on_plate(apply(h, e))]
        last = min(over if len(over) == 1 else ends, key=lambda e: math.dist(e, last or mid))
        mm = apply(h, last)
        per[n] = (plate, tip, quad, h, last, well_at(mm))
        track.append((n, mm, len(pts)))
    return track, per, pitches, fails


def analyze(clip: Path, a1: str, sam, out_dir: Path, pointer: str = TIP, say=_quiet) -> dict:
    """Everything one clip gives: clip facts, the tip track (plate mm, so
    other dwell settings rerun without SAM), wells found, failures, timing,
    check images in out_dir. say(stage, done, total) tells how far it is."""
    t, t_all = {}, time.perf_counter()
    say("Reading the clip's frames")
    info = ffprobe(clip)
    if info["duration_s"] > MAX_CLIP_S + 0.5:
        raise ClipTooLong(f"clip is {info['duration_s']:.0f} s (max {MAX_CLIP_S} s)")
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    nframes, fps = info["frames"], info["fps"]
    fdir = frames_dir(clip, list(range(0, nframes, scan_step(fps))))
    t["frames_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    with ThreadPoolExecutor(1) as pool:  # sam-3.1 allows 3,000 a minute, no limit on concurrency
        finding_plate = pool.submit(best_detect, sam, PLATE, fdir, nframes, fps)
        tip0 = best_detect(sam, pointer, fdir, nframes, fps, say=say)
        plate0 = finding_plate.result()
    t["first_find_s"] = time.perf_counter() - t0
    missing = [x for x, d in ((PLATE, plate0), (pointer, tip0)) if not d]
    if missing:
        raise RuntimeError(f"SAM found no {' and no '.join(missing)} on any sampled frame")
    size = (plate0[1]["w"], plate0[1]["h"])
    prompts = {1: [plate0], 2: [tip0]}

    t0 = time.perf_counter()
    say("Uploading the clip to SAM")
    part = sam.upload(clip)
    t["upload_s"] = time.perf_counter() - t0
    t["video_s"], t["video_first_frame_s"], t["refind_s"] = 0.0, 0.0, 0.0
    gap = max(2, round(fps * GAP_S))
    rounds = 0
    while True:
        rounds += 1
        t0 = time.perf_counter()
        say(f"SAM tracking plate and {pointer}" + (f" (round {rounds})" if rounds > 1 else ""), 0, nframes)
        text, first = sam.video(part, object_prompt(prompts, size),
                                lambda k, r=rounds: say(f"SAM tracking plate and {pointer}"
                                                        + (f" (round {r})" if r > 1 else ""), k, nframes))
        t["video_s"] += time.perf_counter() - t0
        t["video_first_frame_s"] = t["video_first_frame_s"] or first
        recs = video_records(text)
        if rounds >= MAX_ROUNDS:
            break
        g = tip_gap(recs, nframes, max(n for n, _ in prompts[2]), gap)
        if g is None:
            break
        t0 = time.perf_counter()
        say(f"The {pointer} went missing at frame {g}: finding it again")
        again = best_detect(sam, pointer, fdir, nframes, fps, start=g, say=say)
        t["refind_s"] += time.perf_counter() - t0
        if not again or any(n == again[0] for n, _ in prompts[2]):
            break
        prompts[2].append(again)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sam_video.txt").write_text(text)
    (out_dir / "detections.json").write_text(json.dumps(
        {oid: [{"frame": n, **{k: m[k] for k in ("box", "w", "h", "c")}} for n, m in items]
         for oid, items in prompts.items()}))

    t0 = time.perf_counter()
    decode_masks(recs, say)
    t["masks_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    say("Following the plate from its best frame")
    followed = follow_plate(clip, recs, nframes)
    t["follow_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    track, per, pitches, fails = locate(recs, nframes, a1, say, followed["quads"])
    t["grid_s"] = time.perf_counter() - t0
    found = found_wells(track, fps)

    t0 = time.perf_counter()
    images = []
    pick = {rep: w for w, rep, *_ in found}
    for n in [round(i * (nframes - 1) / 3) for i in range(4)]:
        pick.setdefault(n, "spread")
    for n in [n for n in range(nframes) if n not in per][:2]:
        pick.setdefault(n, "fail")
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    pull(fdir, list(pick))
    for i, (n, why) in enumerate(sorted(pick.items())):
        say("Drawing check images", i, len(pick))
        plate, tip, quad, h, end, well = per.get(n, (None,) * 6)
        if not plate:
            plate, tip = recs.get(n, {}).get(1), recs.get(n, {}).get(2)
        name = f"{n:05d}_{why}.jpg"
        draw(frame_png(fdir, n), img_dir / name, plate, tip, quad, h, end, well,
             f"frame {n} ({n / fps:.1f}s) {why}  tip well={well}")
        images.append(name)
    shutil.rmtree(fdir)
    t["images_s"] = time.perf_counter() - t0

    pitches.sort()
    pitch = pitches[len(pitches) // 2] if pitches else 0.0
    failed = sum(fails.values())
    t["total_s"] = time.perf_counter() - t_all
    return {**info, "frames": nframes, "sam_frames": len(recs), "rounds": rounds,
            "prompts": {str(k): [n for n, _ in v] for k, v in prompts.items()},
            "track": [[n, mm[0], mm[1], a] for n, mm, a in track],
            "found": [{"well": w, "rep": rep, "f0": f0, "f1": f1} for w, rep, f0, f1 in found],
            "plate_frame": followed["ref"], "plate_redone": followed["redone"], "plate_kept": followed["kept"],
            "pitch_px": pitch, "well_px": pitch * WELL_D_MM / PITCH_MM,
            "fails": fails, "failed_ratio": failed / nframes if nframes else 1.0,
            "timing": t, "images": images}


def track_of(result: dict) -> list:
    """A saved result's track back in analyze's shape."""
    return [(n, (x, y), a) for n, x, y, a in result["track"]]
