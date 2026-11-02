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
   frame that can't be matched finds its own edges. SAM finds the wells on the flattened reference,
   and the grid moves onto them. Each frame: corners -> homography to the plate's
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
import threading
import time
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import httpx

from server import call_log, look

BASE_URL = "https://api.meta.ai/v1"
PLATE, TIP = "96-well plate", "pipette tip"
#: SAM finds a plate's wells as this ("well" alone finds none).
WELL = "hole"
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
#: After this many frames in a row left in place the plate has likely moved on: matches may come
#: from this many times further, and SAM's plate counts even partly covered, if its corners hold up.
LOST_FRAMES, LOST_REACH = 3, 3.0
#: Fitting the grid to the wells: px a mm on the flattened frame SAM sees; how far (mm) the outline can be
#: off (under half a pitch: further is the next well over); the fewest wells to trust; how many
#: more wells a shifted fit must explain than the one before; how far the fit may rescale.
FLAT_PX, MAX_WELL_SHIFT, MIN_WELLS, SHIFT_MARGIN, MAX_WELL_SCALE = 12, 4, 12, 2, 0.06
#: The screen's edge is this share of its size from the plate's box; SAM's plate stands in for an
#: unmatched frame only when this much of it shows.
EDGE_GAP, MIN_SHOWN = 0.02, 0.9
#: Continuous mode's "went in": stopped frames (at most STOP_MM_S) over one well add up to FILL_S
#: within the last FILL_WINDOW_S. SINK_RATIO: also ask for the tip's mask to shrink to this share of
#: its size while moving, in the SINK_BACK_S before; off, it lost more hits than false wells (cached:
#: 41/55 false 8 without, 27/55 false 6 at 1.0).
FILL_S, FILL_WINDOW_S, STOP_MM_S = 1.0, 3.0, 20.0
SINK_RATIO, SINK_BACK_S = None, 2.0
#: A continuous well is "sure" with at least SURE_INLIERS ORB inliers on its stay (the median) and
#: the tip within SURE_OFF_MM of the well's center. The reference's wells don't count: SAM can find
#: none on a clear view of a clear plate, and the grid then just keeps to the outline.
SURE_INLIERS, SURE_OFF_MM = 70, 3.0  # 70: the lowest 5th percentile of matched frames on the iPad sessions
#: A chunk's looks for the tip. SAM's image calls run about one at a time however many are sent
#: (1.6 s each), and a chunk closes every 9 s: 40 a chunk took 270 s, and dropped connections.
CHUNK_TIP_LOOKS = 3
#: The clip SAM gets; `light` is the Diagnostics trial, Watching stays on CLIP_CRF and PNG until replays match.
CLIP_CRF, LIGHT_CRF = 18, 23
#: Tries of an image call whose connection dropped.
IMAGE_TRIES = 3


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


def convex(q: list) -> bool:
    """Four corners in order that turn the same way at each."""
    turns = [(q[(i + 1) % 4][0] - q[i][0]) * (q[(i + 2) % 4][1] - q[i][1])
             - (q[(i + 1) % 4][1] - q[i][1]) * (q[(i + 2) % 4][0] - q[i][0]) for i in range(4)]
    return all(t > 0 for t in turns) or all(t < 0 for t in turns)


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


def _sunk(track: list, f0: int, visit: list, fps: float, ratio: float) -> bool:
    """The tip's mask smaller over the well than while it moved just before; nothing moved: True."""
    size = lambda t: sorted(t)[len(t) // 2]  # noqa: E731
    moving = [q[2] for p, q in zip(track, track[1:]) if f0 - SINK_BACK_S * fps <= q[0] < f0
              and math.dist(p[1], q[1]) * fps / (q[0] - p[0]) > STOP_MM_S]
    return not moving or size([t[2] for t in visit]) <= ratio * size(moving)


def filled(track: list, fps: float, sink: float | None = SINK_RATIO) -> list:
    """track as dwells' -> [{"well", "frame", "f0"}]: the frame a well's stopped time reached
    FILL_S within FILL_WINDOW_S, and that time's first frame. Another well must come between
    two of the same, and the tip must leave the spot first: one stay on the line between two
    wells would otherwise fill both, in turns."""
    recent, out, spot = deque(), [], None
    for p, q in zip(track, track[1:]):
        step = (q[0] - p[0]) / fps
        well = cell_at(q[1])
        if spot and math.dist(q[1], spot) > DWELL_MM:
            spot = None
        if spot or not well or q[0] - p[0] > MAX_MISSING + 1 or math.dist(p[1], q[1]) / step > STOP_MM_S:
            continue
        recent.append((q[0], well, step, q))
        while recent[0][0] < q[0] - FILL_WINDOW_S * fps:
            recent.popleft()
        here = [r for r in recent if r[1] == well]
        if sum(r[2] for r in here) < FILL_S - 1e-9 or (out and out[-1]["well"] == well):
            continue
        if sink is None or _sunk(track, here[0][0], [r[3] for r in here], fps, sink):
            mid = well_center_mm(ROWS.index(well[0]), int(well[1:]) - 1)
            off = sorted(math.dist(r[3][1], mid) for r in here)
            out.append({"well": well, "frame": q[0], "f0": here[0][0], "off_mm": off[len(off) // 2]})
            spot = q[1]
            recent.clear()
    return out


def not_sure_why(off_mm: float, inliers: list) -> list:
    """What's unsure about a well, by name; empty: sure. inliers: the stay's frames (0: not matched)."""
    middle = sorted(inliers)[len(inliers) // 2] if inliers else 0
    return [why for why, bad in (("matching", middle < SURE_INLIERS), ("off center", off_mm > SURE_OFF_MM)) if bad]


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


def for_sam(clip: Path, out: Path, fps: float, crf: int = CLIP_CRF) -> None:
    """The clip as SAM's video request gets it. Some files as the app writes them make SAM
    fail (server_error) when it turns back from the prompt: a keyframe a second, no B-frames.
    Every frame stays, so frame numbers still match the clip."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-an", "-c:v", "libx264", "-crf", str(crf),
                    "-preset", "veryfast", "-g", str(max(1, round(fps))), "-bf", "0", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-vsync", "0", str(out)], check=True)


def as_jpeg(png: Path) -> bytes:
    """A tenth of the PNG's bytes, for a `light` MetaSam."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), "-q:v", "3", "-f", "mjpeg", "-"],
                          check=True, capture_output=True, stdin=subprocess.DEVNULL).stdout


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


def video_body(part: dict, prompt: str) -> dict:
    return {"model": look.SAM_MODEL, "stream": True,
            "metadata": {"propagation_direction": "both", "include_confidence": "true"},
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": prompt}, part]}]}


class MetaSam:
    """SAM 3.1 on the Meta Model API (the key from .env, never sent elsewhere)."""

    def __init__(self, key: str, timeout: float = 600, light: bool = False):
        self.light = light
        self.crf = LIGHT_CRF if light else CLIP_CRF
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.client = httpx.Client(timeout=timeout)

    def image(self, target: str, png: Path) -> list:
        url = look.data_url(as_jpeg(png)) if self.light else look.data_url(png.read_bytes(), "image/png")
        body = look.build_sam_body(target, url)
        for attempt in range(IMAGE_TRIES):
            try:
                t0 = time.perf_counter()
                r = self.client.post(f"{BASE_URL}/responses", json=body,
                                     headers={**self.headers, "Accept": "text/event-stream"})
                break
            except httpx.TransportError:
                if attempt == IMAGE_TRIES - 1:
                    raise
        print(call_log.line(body["model"], f"find {target}", int((time.perf_counter() - t0) * 1000),
                            r.status_code), flush=True)
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
        body = video_body(part, prompt)
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
    """Where the plate shows, thinned: the same points live and from the cache, so both follow alike."""
    return match.get("seen") or mask_points(match, 2)[::9]


def compact(recs: dict) -> dict:
    """SAM's records as locate() reads them without decoding masks: JSON-ready. The plate
    keeps its outline (all plate_corners uses) and how much of it shows, the tip every
    other pixel (as locate)."""
    return {str(n): {str(oid): ({"box": m["box"], "points": outline(mask_points(m, 2)), "shown": shown(m),
                                 "seen": seen_points(m)} if oid == 1
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


def plates_on(sam, fdir: Path, frames: list) -> list:
    """[(frame, match)]: every plate SAM gives on these frames."""
    pull(fdir, frames)
    with ThreadPoolExecutor(len(frames)) as pool:
        got = list(pool.map(lambda n: sam.image(PLATE, frame_png(fdir, n)), frames))
    return [(n, m) for n, g in zip(frames, got) for m in g]


def hits_on(sam, target: str, fdir: Path, frames: list) -> list:
    """[(frame, match)] on these frames that have the target. For a chunk, which may not have the
    tip at all: best_detect keeps asking until it hits."""
    pull(fdir, frames)
    with ThreadPoolExecutor(len(frames)) as pool:
        got = list(pool.map(lambda n: sam.image(target, frame_png(fdir, n)), frames))
    return [(n, g[0]) for n, g in zip(frames, got) if g]


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


def _region(gray, quad: list, tip: list | None, plate: list | None):
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
    if plate is not None:
        h, w = gray.shape
        xy = np.int32(plate)
        xy = xy[(xy[:, 0] >= 0) & (xy[:, 0] < w) & (xy[:, 1] >= 0) & (xy[:, 1] < h)]
        seen[xy[:, 1], xy[:, 0]] = 255
        seen = cv2.dilate(seen, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
        mask[(inside > 0) & (seen == 0)] = 0
    if tip:
        cv2.fillConvexPoly(mask, cv2.convexHull(np.int32(tip)), 0)
    return mask


def _follow(orb, base: tuple, gray, tip: list | None, plate: list | None, last, quad0: list, reach: float):
    """(this frame's homography from the reference, its inliers), or None. A match stays within
    `reach` px of where the last frame's homography puts it (the wells repeat)."""
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
    for k in np.argsort(bits, kind="stable").tolist():
        if bits[k] < MAX_BITS:
            best.setdefault(int(i[k]), int(j[k]))
    if len(best) < MIN_INLIERS:
        return None
    cv2.setRNGSeed(0)  # RANSAC's draws: the same input gives the same plate, whatever ran before
    h, inliers = cv2.findHomography(pts0[list(best)], pts[list(best.values())], cv2.RANSAC, 3.0)
    if h is None or inliers.sum() < MIN_INLIERS or inliers.mean() < MIN_INLIER_SHARE:
        return None
    return h, int(inliers.sum())


def fit_wells(gray, quad: list, find) -> tuple:
    """The plate's corners moved so the grid sits on the wells that show. Seen from the side, the
    mask's outline takes in the plate's wall too, which pushes the grid off the wells: flatten the
    frame by that outline, have SAM find the wells (`find(png)` -> its matches), and fit the
    8 x 12 grid to them with RANSAC. -> (corners in the same order, wells matched); the corners as
    given when too few fit."""
    import cv2
    import numpy as np
    ordered = plate_quad(quad, "tl")  # the grid is the same turned 180 degrees, so any A1 will do
    w, h = PLATE_MM
    to_flat = cv2.getPerspectiveTransform(np.float32(ordered), np.float32([(0, 0), (w, 0), (w, h), (0, h)]) * FLAT_PX)
    flat = cv2.warpPerspective(gray, to_flat, (round(w * FLAT_PX), round(h * FLAT_PX)))
    d = Path(tempfile.mkdtemp(prefix="samwells_"))
    cv2.imwrite(str(d / "flat.png"), flat)
    matches = find(d / "flat.png")
    shutil.rmtree(d)
    sized = [m["box"] for m in matches if 0.4 * PITCH_MM <= (m["box"][2] - m["box"][0]) / FLAT_PX <= PITCH_MM
             and 0.4 * PITCH_MM <= (m["box"][3] - m["box"][1]) / FLAT_PX <= PITCH_MM]
    if len(sized) < MIN_WELLS:
        return quad, 0
    seen = np.float32([((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in sized]) / FLAT_PX
    grid = np.float32([well_center_mm(r, c) for r in range(8) for c in range(12)])
    best, most = None, 0
    cv2.setRNGSeed(0)
    shifts = sorted(((dx, dy) for dx in range(-MAX_WELL_SHIFT, MAX_WELL_SHIFT + 1)
                     for dy in range(-MAX_WELL_SHIFT, MAX_WELL_SHIFT + 1)), key=lambda d: abs(d[0]) + abs(d[1]))
    for dx, dy in shifts:
        near = np.linalg.norm(seen[:, None] - (grid + (dx, dy))[None], axis=2)
        pick = near.argmin(axis=1)
        keep = near[np.arange(len(seen)), pick] < PITCH_MM * 0.3
        if keep.sum() < MIN_WELLS:
            continue
        # move, turn and scale only: flattening took out the perspective, and more freedom lets a
        # few wells drag the grid a row over
        found, inliers = cv2.estimateAffinePartial2D(grid[pick[keep]], seen[keep], method=cv2.RANSAC,
                                                     ransacReprojThreshold=1.0)
        if found is None or abs(math.hypot(found[0, 0], found[1, 0]) - 1) > MAX_WELL_SCALE:
            continue
        if inliers.sum() > most + SHIFT_MARGIN * (dx != 0 or dy != 0):
            best, most = np.vstack([found, (0, 0, 1)]), int(inliers.sum())
    if best is None or most < MIN_WELLS:
        return quad, 0
    # image -> flat mm -> grid mm: where each corner's own plate position lands now
    to_mm = np.linalg.inv(best) @ np.diag([1 / FLAT_PX, 1 / FLAT_PX, 1.0]) @ to_flat
    back = np.linalg.inv(to_mm)
    was = cv2.perspectiveTransform(np.float32(quad).reshape(-1, 1, 2), np.diag([1 / FLAT_PX, 1 / FLAT_PX, 1.0]) @ to_flat)
    fitted = [tuple(map(float, p)) for p in cv2.perspectiveTransform(was, back).reshape(-1, 2)]
    return (fitted, most) if convex(fitted) and not distorted(fitted) else (quad, 0)


def follow_plate(clip: Path, recs: dict, nframes: int, find=None) -> dict:
    """The plate's corners once, on its best frame, then carried to every frame by matching
    features to that frame. A frame that can't be matched takes SAM's plate if it's all in view,
    else stays where the last one was; a few of those in a row and the search widens and a partly
    covered plate will do. -> {"quads": {frame: corners}, "ref", "redone", "kept"}."""
    import cv2
    import numpy as np
    frames = gray_frames(clip, nframes)
    none = {"quads": {}, "ref": None, "redone": 0, "kept": 0, "wells": 0}
    if not frames:
        return none
    h, w = frames[0].shape
    ref = best_frame(recs, (w, h))
    if ref is None or ref not in frames:
        return none
    quad0, wells = [tuple(map(float, p)) for p in plate_corners(mask_points(recs[ref][1], 2))], 0
    if find:
        quad0, wells = fit_wells(frames[ref], quad0, find)
    orb = cv2.ORB_create(ORB_FEATURES)
    base = orb_base(orb, frames[ref], quad0, _tip_at(recs, ref), _plate_at(recs, ref))
    if base is None:
        return none
    out = {"quads": {ref: quad0}, "ref": ref, "redone": 0, "kept": 0, "wells": wells}
    _carry(orb, base, frames, recs, quad0, (range(ref + 1, nframes), range(ref - 1, -1, -1)), np.eye(3), out)
    return out


def gray_frames(clip: Path, nframes: int) -> dict:
    """{frame: its gray image}."""
    import cv2
    video, frames = cv2.VideoCapture(str(clip)), {}
    for n in range(nframes):
        ok, img = video.read()
        if not ok:
            break
        frames[n] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    video.release()
    return frames


def _tip_at(recs: dict, n: int) -> list | None:
    return mask_points(recs[n][2], 2) if 2 in recs.get(n, {}) else None


def _plate_at(recs: dict, n: int, tracked: bool = True) -> list | None:
    """Where the plate shows on frame n; None when SAM didn't track the plate at all."""
    if not tracked:
        return None
    return seen_points(recs[n][1]) if 1 in recs.get(n, {}) else [(0, 0)]


def orb_base(orb, gray, quad: list, tip: list | None, plate: list) -> tuple | None:
    """The reference's features, or None with too few."""
    import numpy as np
    kps, des = orb.detectAndCompute(gray, _region(gray, quad, tip, plate))
    if des is None or len(kps) < MIN_INLIERS:
        return None
    return np.float32([k.pt for k in kps]), des


def _carry(orb, base: tuple, frames: dict, recs: dict, quad0: list, ways: tuple, start, out: dict,
           plate_tracked: bool = True) -> None:
    """The plate's corners onto the frames of each way, from homography `start`."""
    import cv2
    import numpy as np
    h, w = next(iter(frames.values())).shape
    corners0 = np.float32(quad0).reshape(-1, 1, 2)
    reach = MATCH_PITCHES * pitch_px(plate_h(plate_quad(quad0, "tl")))
    for way in ways:
        last, stuck = start, 0
        for n in way:
            if n not in frames:
                continue
            lost = stuck >= LOST_FRAMES
            found = _follow(orb, base, frames[n], _tip_at(recs, n), _plate_at(recs, n, plate_tracked), last, quad0,
                            reach * (LOST_REACH if lost else 1))
            if found is not None:
                quad = [tuple(map(float, p)) for p in cv2.perspectiveTransform(corners0, found[0]).reshape(-1, 2)]
                if not distorted(quad):  # a good inlier count on the wrong wells bends the grid
                    last, out["quads"][n], stuck = found[0], quad, 0
                    out.setdefault("inliers", {})[n] = found[1]
                    continue
            plate = recs.get(n, {}).get(1)
            own = plate_corners(mask_points(plate, 2)) if plate and (lost or shown(plate) >= MIN_SHOWN) \
                and not touches_edge(plate, (w, h)) else None
            if own and convex(own) and not distorted(own):
                last = cv2.getPerspectiveTransform(np.float32(quad0), np.float32(own))
                out["quads"][n], stuck = own, 0
                out["redone"] += 1
            else:
                out["quads"][n] = [tuple(map(float, p)) for p in cv2.perspectiveTransform(corners0, last).reshape(-1, 2)]
                out["kept"] += 1
                stuck += 1


# ---------- continuous: the plate once a session ----------

#: A plate this many times smaller than the biggest is a picture of one, not one.
MIN_AREA_SHARE = 4

def _overlap(a: list, b: list) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def plates_with_pen(plates: list, pens: list) -> list:
    ends = [e for pen in pens for e in tip_ends(mask_points(pen, 2))]
    return [m for m in plates if any(m["box"][0] <= x <= m["box"][2] and m["box"][1] <= y <= m["box"][3] for x, y in ends)]


def best_plate(plates: list, size: tuple, pens: list = ()) -> dict:
    """Of SAM's plates, the one to build on. There can be another plate on the desk, so only those
    with a pen's end in their box count; with the pen on none, those over the biggest (the nearest).
    Of them: unbent (a hand on the plate bends the outline SAM gives), off the screen's edge, the most
    showing. A plate far smaller than the biggest isn't a candidate: it's a picture of the plate, the
    glasses' own preview on the iPad, where the pen shows too."""
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])  # noqa: E731
    big = max(plates, key=lambda m: area(m["box"]))
    plates = [m for m in plates if area(m["box"]) * MIN_AREA_SHARE >= area(big["box"])]
    holding = plates_with_pen(plates, pens)
    if not holding:
        holding = [m for m in plates if _overlap(m["box"], big["box"])]
    return max(holding, key=lambda m: (not distorted(plate_corners(mask_points(m, 2))), not touches_edge(m, size), shown(m)))


def pick_reference(sam, stills: list, pointer: str) -> dict | None:
    """The session's grid correction from the frames at Record: stills are [(frame, gray, png)]. The
    middle one gives the plate, or the others when it shows none usable (SAM can miss a plate in
    plain view); a second apart the view barely moves, so that outline flattens all three for SAM's
    wells, and the one with the most wells sets the correction, asked again once when that's too
    few to fit (SAM can miss them all on a good view). Few image calls: chunks queue behind these.
    None when no still shows a usable plate."""
    mid = len(stills) // 2
    for _, gray, png in [stills[mid], *stills[:mid], *stills[mid + 1:]]:
        plates = sam.image(PLATE, png)
        if not plates:
            continue
        pens = sam.image(pointer, png)
        plate = best_plate(plates, gray.shape[::-1], pens)
        corners_ = [tuple(map(float, p)) for p in plate_corners(mask_points(plate, 2))]
        if not touches_edge(plate, gray.shape[::-1]) and not distorted(corners_):
            break
    else:
        return None
    covered = shown(plate) < MIN_SHOWN or any(_overlap(m["box"], plate["box"]) for m in pens)
    fit = lambda g: fit_wells(g, corners_, lambda p: sam.image(WELL, p))  # noqa: E731
    with ThreadPoolExecutor(len(stills)) as pool:
        fits = list(pool.map(fit, [g for _, g, _ in stills]))
    pick = max(range(len(stills)), key=lambda i: fits[i][1])
    quad, wells = fits[pick]
    if wells < MIN_WELLS:
        quad, wells = max((quad, wells), fit(stills[pick][1]), key=lambda f: f[1])
    return {"fit": outline_fit(corners_, quad), "wells": wells, "frame": stills[pick][0], "covered": covered}


UNIT = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def outline_fit(outline_: list, quad: list) -> list:
    """The session's grid correction: where the fitted corners sit with SAM's outline flattened to
    a unit square. It holds from any view, where a reference frame's pixels don't."""
    import cv2
    import numpy as np
    flat = cv2.getPerspectiveTransform(np.float32(outline_), np.float32(UNIT))
    return cv2.perspectiveTransform(np.float32(quad).reshape(-1, 1, 2), flat).reshape(-1, 2).tolist()


def corrected(outline_: list, fit: list | None) -> list:
    """SAM's outline on this frame, with the session's correction when there is one yet."""
    import cv2
    import numpy as np
    if fit is None:
        return [tuple(map(float, p)) for p in outline_]
    back = cv2.getPerspectiveTransform(np.float32(UNIT), np.float32(outline_))
    return [tuple(map(float, p)) for p in cv2.perspectiveTransform(np.float32(fit).reshape(-1, 1, 2), back).reshape(-1, 2)]


def follow_chunk(frames: dict, recs: dict, anchor: int, plate: dict | None, fit: list | None, quad: list | None = None) -> dict:
    """A chunk on its own: `plate`, SAM's on frame `anchor`, flattened and corrected by the session's
    `fit`, then ORB from that frame to the rest of the chunk (features fade as the view moves away,
    so nothing matches back to the session's first seconds). The pen's mask isn't in yet, so
    RANSAC drops its matches and the hand's. A bent `plate` means no frame gets inliers: _carry
    won't take a bent grid, so the chunk's wells come out not sure. `quad`, in place of `plate`:
    where the chunk before had the plate on `anchor`."""
    import cv2
    import numpy as np
    orb = cv2.ORB_create(ORB_FEATURES)
    quad = quad or corrected(plate_corners(mask_points(plate, 2)), fit)
    out = {"quads": {}, "redone": 0, "kept": 0}
    base = orb_base(orb, frames[anchor], quad, None, seen_points(plate) if plate else None)
    if base is None:
        return out
    out["quads"][anchor] = quad
    if not distorted(quad):
        out["inliers"] = {anchor: len(base[0])}
    _carry(orb, base, frames, recs, quad, (range(anchor + 1, len(frames)), range(anchor - 1, -1, -1)), np.eye(3), out, False)
    return out


def track_chunk(clip: Path, a1: str, sam, out_dir: Path, pointer: str, session) -> dict:
    """One chunk of a continuous session -> its tip track in plate mm (frames from the chunk's
    start) and times. Chunks run side by side and need nothing from each other; `session.ref()`
    gives the grid correction if it's there yet, and session.first() holds this chunk's image calls
    while that's being worked out (they queue at SAM, so it would get in line behind them). The
    plate is asked for on the middle look, and on the others only when that one won't do. No tip
    or no plate: {"track": []}, no SAM video."""
    t, t_all = {}, time.perf_counter()
    n_chunk = int(out_dir.name)
    info = ffprobe(clip)
    nframes, fps = info["frames"], info["fps"]
    out_dir.mkdir(parents=True, exist_ok=True)
    done = lambda r: {"frames": nframes, "fps": fps, "w": info["w"], "h": info["h"], "timing": t, **r}  # noqa: E731
    gray = gray_frames(clip, nframes)

    def follow(plate0: tuple | None, fit: list | None) -> dict:
        """With no unbent plate in this chunk, and only then, it waits for the chunk before and
        starts from the last place that one matched in the second they share."""
        lent = None
        if (not plate0 or distorted(plate_corners(mask_points(plate0[1], 2)))) and n_chunk:
            lent = session.end_of(n_chunk - 1)
        t0 = time.perf_counter()
        if lent:
            followed = follow_chunk(gray, {}, lent[0], None, fit, lent[1])
        elif plate0:
            followed = follow_chunk(gray, {}, plate0[0], plate0[1], fit)
        else:
            followed = {"quads": {}, "redone": 0, "kept": 0}
        followed["lent"] = lent is not None
        t["follow_s"] = time.perf_counter() - t0
        overlap = round(fps)
        last = max((n for n in followed.get("inliers", {}) if n >= nframes - overlap), default=None)
        session.hand(n_chunk, None if last is None else (last - nframes + overlap, followed["quads"][last]))
        return followed

    looks = [(2 * i + 1) * nframes // (2 * CHUNK_TIP_LOOKS) for i in range(CHUNK_TIP_LOOKS)]
    fdir = frames_dir(clip, looks)
    t0 = time.perf_counter()
    # each step that doesn't need another's result runs beside it: the chunk waits for the longest
    with ThreadPoolExecutor(3) as pool:
        sending = pool.submit(send_video, clip, out_dir, sam, fps, t)
        session.first()
        middle = looks[CHUNK_TIP_LOOKS // 2]
        finding_plates = pool.submit(plates_on, sam, fdir, [middle])
        tips = hits_on(sam, pointer, fdir, looks)
        plates = finding_plates.result()
        pens = [m for _, m in tips]
        if tips and not any(not distorted(plate_corners(mask_points(m, 2))) for m in plates_with_pen([m for _, m in plates], pens)):
            plates += plates_on(sam, fdir, [n for n in looks if n != middle])
        t["first_find_s"] = time.perf_counter() - t0
        plate0 = None
        if plates:
            best = best_plate([m for _, m in plates], gray[0].shape[::-1], pens)
            plate0 = next(p for p in plates if p[1] is best)
        if not tips:
            shutil.rmtree(fdir)
            sending.result()
            session.hand(n_chunk, None)
            t["total_s"] = time.perf_counter() - t_all
            return done({"track": [], "why": "no tip"})
        ref = session.ref()
        following = pool.submit(follow, plate0, ref["fit"] if ref else None)
        # every look that saw the tip prompts it, in one video call: a second round repeats the whole video
        _, recs, rounds = track_video(sending.result(), sam, {2: tips}, nframes, fps, fdir, pointer, t, max_rounds=1)
        shutil.rmtree(fdir)
        t0 = time.perf_counter()
        decode_masks(recs)
        t["masks_s"] = time.perf_counter() - t0
        followed = following.result()
    if not plate0 and not followed["lent"]:
        t["total_s"] = time.perf_counter() - t_all
        return done({"track": [], "why": "no plate"})
    track, *_ = locate(recs, nframes, a1, quads=followed["quads"])
    t["total_s"] = time.perf_counter() - t_all
    return done({"track": [[n, mm[0], mm[1], a] for n, mm, a in track], "rounds": rounds, "ref_wells": ref["wells"] if ref else 0,
                 "corrected": ref is not None, "anchor": plate0[0] if plate0 else None, "lent": followed["lent"],
                 "bent_anchor": "inliers" not in followed, "inliers": followed.get("inliers", {}),
                 "redone": followed["redone"], "kept": followed["kept"]})


# ---------- one clip ----------

def locate(recs: dict, nframes: int, a1: str, say=_quiet, quads: dict | None = None, near: tuple | None = None) -> tuple:
    """SAM's records -> (track, {frame: what to draw}, well pitches in px, failed frames by why).
    `quads` (follow_plate): each frame's plate corners; a frame without finds its own edges.
    `near`: where the tip last was, for a caller going a frame at a time."""
    track, per, pitches, last = [], {}, [], near
    fails = {"no_plate": 0, "no_tip": 0, "bad_grid": 0}
    for n in range(nframes):
        say("Placing the grid on each frame", n, nframes)
        r = recs.get(n, {})
        plate, tip = r.get(1), r.get(2)
        if not plate and n not in (quads or {}):
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


def send_video(clip: Path, out_dir: Path, sam, fps: float, t: dict, say=_quiet) -> dict:
    """The clip re-encoded and uploaded -> SAM's input part."""
    t0 = time.perf_counter()
    say("Uploading the clip to SAM")
    sent = out_dir / "clip_sam.mp4"
    for_sam(clip, sent, fps, crf=sam.crf)
    t["encode_s"] = time.perf_counter() - t0
    part = sam.upload(sent)
    sent.unlink()
    t["upload_s"] = time.perf_counter() - t0 - t["encode_s"]
    return part


def track_video(part: dict, sam, prompts: dict, nframes: int, fps: float, fdir: Path,
                pointer: str, t: dict, say=_quiet, max_rounds: int = MAX_ROUNDS) -> tuple:
    """The uploaded clip tracked from `prompts`, found again when the tip goes missing
    (prompts grows). -> (SAM's text, its records, rounds); t gets the times."""
    first_match = next(iter(prompts.values()))[0][1]
    size = (first_match["w"], first_match["h"])
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
        if rounds >= max_rounds:
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
    return text, recs, rounds


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
    prompts = {1: [plate0], 2: [tip0]}
    part = send_video(clip, out_dir, sam, fps, t, say)
    text, recs, rounds = track_video(part, sam, prompts, nframes, fps, fdir, pointer, t, say)
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
    followed = follow_plate(clip, recs, nframes, lambda png: sam.image(WELL, png))
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
            "plate_frame": followed["ref"], "plate_wells": followed["wells"], "plate_redone": followed["redone"], "plate_kept": followed["kept"],
            "pitch_px": pitch, "well_px": pitch * WELL_D_MM / PITCH_MM,
            "fails": fails, "failed_ratio": failed / nframes if nframes else 1.0,
            "timing": t, "images": images}


def track_of(result: dict) -> list:
    """A saved result's track back in analyze's shape."""
    return [(n, (x, y), a) for n, x, y, a in result["track"]]
