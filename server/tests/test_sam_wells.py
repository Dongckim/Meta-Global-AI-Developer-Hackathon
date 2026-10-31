"""SAM well test (server/sam_wells.py, server/lab_sam.py), SAM faked:
targets are inner wells, distinct, 3-4; the judge (hit / order / false);
the dwell settings compared; geometry; a clip through the endpoints, with
the tip found again after it goes missing.

Run: python -m server.tests.test_sam_wells (runs on import; needs ffmpeg).
"""

import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
os.environ["MODEL_API_KEY"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

import server.api as api  # noqa: E402
from server import lab_sam  # noqa: E402
from server import sam_wells as sw  # noqa: E402

# SW1. Random targets: 3-4 wells, none twice, none on the edge (rows A/H, columns 1/12).
for seed in range(500):
    t = sw.random_targets(random.Random(seed))
    assert len(t) in (3, 4) and len(set(t)) == len(t), t
    assert all(w[0] not in "AH" and w[1:] not in ("1", "12") for w in t), t
assert sw.parse_targets("c3, F7 → b10") == ["C3", "F7", "B10"]
for bad in ("C3, C3", "I4", "C13", ""):
    try:
        sw.parse_targets(bad)
        raise AssertionError(bad)
    except ValueError:
        pass
print("SW1 ok: targets are 3-4 distinct inner wells")

# SW2. The judge: each target hit or not, order, false wells, one line.
T = ["C3", "F7", "B10", "G2"]
j = sw.judge(T, T)
assert j["line"] == "4/4 matched" and j["all_in_order"] and all(w["ok"] for w in j["wells"])
j = sw.judge(T, ["C3", "B10", "F7", "G2"])
assert j["hits"] == 4 and not j["order_ok"] and j["line"] == "4/4 matched · order differs"
j = sw.judge(T, ["C3", "D5", "F7", "G2"])
assert j["false"] == ["D5"] and j["order_ok"] and [w["ok"] for w in j["wells"]] == [True, True, False, True]
assert j["line"] == "3/4 matched · 1 false" and not j["all_in_order"]
assert sw.judge(T, [])["line"] == "0/4 matched"
print("SW2 ok: hits, order and false wells")

# SW3. Geometry: square-on, turned 180°, portrait; between wells is no well.
quad = [(50, 50), (50 + 1277.6, 50), (50 + 1277.6, 50 + 854.8), (50, 50 + 854.8)]
h = sw.plate_h(sw.plate_quad(sw.corners(quad), "tl"))
assert sw.well_at(sw.apply(h, (50 + 143.8, 50 + 112.4))) == "A1"
assert sw.well_at(sw.apply(h, (50 + 143.8 + 90 * 11, 50 + 112.4 + 90 * 7))) == "H12"
assert sw.well_at(sw.apply(h, (50 + 143.8 + 45, 50 + 112.4))) is None
h2 = sw.plate_h(sw.plate_quad(sw.corners(quad), "br"))
assert sw.well_at(sw.apply(h2, (50 + 143.8, 50 + 112.4))) == "H12"
pq = [(0, 0), (854.8, 0), (854.8, 1277.6), (0, 1277.6)]
h3 = sw.plate_h(sw.plate_quad(sw.corners(pq), "tl"))
assert sw.well_at(sw.apply(h3, (112.4, 143.8 + 90))) == "A2"
persp = [(100, 80), (900, 120), (880, 640), (90, 600)]
hp = sw.plate_h(sw.plate_quad(sw.corners(persp), "tl"))
assert sw.well_at(sw.apply(hp, sw.apply(sw.solve_inv(hp), sw.well_center_mm(2, 3)))) == "C4"
print("SW3 ok: grid from four corners, any A1 corner, perspective")

# SW3b. A hand over a corner, a pen across the middle: the edges still cross at the true corner.
blob = [(x, y) for x in range(0, 300, 2) for y in range(0, 200, 2)
        if x + y < 440 and not (140 <= x <= 160 and 40 <= y <= 160)]  # br corner gone, a hole inside
assert sw.corners(blob)[2] != (298, 198)
RECT = [(0, 0), (298, 0), (298, 198), (0, 198)]
for got, want in zip(sw.plate_corners(blob), RECT):
    assert sw.math.dist(got, want) < 3, (got, want)
# A hand biting into the top edge: the hull bridges it.
bitten = [(x, y) for x in range(0, 300, 2) for y in range(0, 200, 2) if not (60 < x < 240 and y < 120)]
for got, want in zip(sw.plate_corners(bitten), RECT):
    assert sw.math.dist(got, want) < 3, (got, want)
# The bridge over the hand is longer than what shows of the right edge: not parallel to the left, not an edge.
cut = [(x, y) for x in range(0, 300, 2) for y in range(0, 200, 2) if x + y < 360]
right = sw.plate_corners(cut)[1:3]
assert abs(right[0][0] - 298) < 12 and abs(right[1][0] - 298) < 12, right
box = [(0, 0), (10, 0), (10, 10), (0, 10)]
for got, want in zip(sw.plate_corners(box), box):  # four points: the plain box
    assert sw.math.dist(got, want) < 1, (got, want)
print("SW3b ok: corners come from the edges, a hidden one included")


# SW4. Settings: a 0.8 s stop at the 2nd target the 1 s default misses, the 0.5 s setting catches;
# the best setting is the one with the most hits.
def stay(well, f0, frames):
    r, c = sw.ROWS.index(well[0]), int(well[1:]) - 1
    return [(f, sw.well_center_mm(r, c), 100 - f) for f in range(f0, f0 + frames)]


track = stay("C3", 0, 16) + stay("F7", 20, 5) + stay("B10", 40, 16)  # 5 fps: 3 s, 0.8 s, 3 s
assert [w for w, *_ in sw.found_wells(track, 5)] == ["C3", "B10"]
assert [w for w, *_ in sw.found_wells(track, 5, 0.5)] == ["C3", "F7", "B10"]
cmp = sw.compare_settings([(["C3", "F7", "B10"], track, 5)])
best = cmp["rows"][cmp["best"]]
assert len(cmp["rows"]) == 9 and best["dwell_s"] == 0.5 and best["hits"] == 3, best
print("SW4 ok: every dwell setting on the same clips; the best one marked")

# SW4b. A visit: 2 missing frames don't break it, 3 do; its well is the cell it stayed in longest.
c3, c4 = sw.well_center_mm(2, 2), sw.well_center_mm(2, 3)
edge = (c3[0] + 4, c3[1])  # C3's cell, outside its circle
near = (c3[0] + 5, c3[1])  # C4's cell, within DWELL_MM of `edge`
pts = lambda frames, mm: [(f, mm, 100) for f in frames]  # noqa: E731
assert sw.well_at(edge) is None and sw.cell_at(edge) == "C3" and sw.cell_at(near) == "C4"
assert sw.cell_at((-20, -20)) is None
assert [w for w, *_ in sw.found_wells(pts(range(2), near) + pts(range(2, 8), edge), 5)] == ["C3"]
assert [(w, f0, f1) for w, _, f0, f1 in sw.found_wells(pts([0, 1, 4, 5], c3), 2)] == [("C3", 0, 5)]
assert sw.found_wells(pts([0, 4], c3), 2) == []
print("SW4b ok: a visit bridges 2 lost frames and takes its longest cell")

# SW4c. The tip end: the end over the plate; both over it: the end the last frame had.
assert sw.tip_ends([(100 + i, 100) for i in range(50)]) == [(100, 100), (149, 100)]
PLATE_BOX_4C = (0, 0, 639, 479)
# frame 0: both ends over the plate, the one nearer the middle (x 320) is 300; then it slides
# past the middle and stays the left end, now the farther one.
pen_boxes = ((200, 200, 300, 204), (340, 200, 440, 204))
recs = {n: {1: {"box": PLATE_BOX_4C, "mask": None}, 2: {"box": b, "mask": None}} for n, b in enumerate(pen_boxes)}
track4c, per4c, *_ = sw.locate(recs, 2, "tl")
assert [per4c[n][4][0] for n in (0, 1)] == [300, 340], per4c
# the first frame's end is the back of the pen; once the other end is alone on the plate, that's the tip
plate_small = {"box": (100, 100, 300, 250), "mask": None}
pen_boxes = ((120, 150, 400, 154), (120, 150, 900, 154))
recs = {n: {1: plate_small, 2: {"box": b, "mask": None}} for n, b in enumerate(pen_boxes)}
recs[0][2] = {"box": (310, 150, 900, 154), "mask": None}  # neither end on the plate
track4c, per4c, *_ = sw.locate(recs, 2, "tl")
assert per4c[0][4][0] == 310 and per4c[1][4][0] == 120, per4c
# the cache gives locate the same answer as SAM's records
saved = sw.expand(sw.json.loads(sw.json.dumps(sw.compact(recs))))
assert sw.locate(saved, 2, "tl")[0] == track4c
# SW4d. The reference frame: never a plate at the screen's edge, and the one showing the most.
near_edge = {"box": (2, 100, 300, 300), "mask": None, "shown": 1.0, "points": [(2, 100), (300, 100), (300, 300), (2, 300)]}
clear = {"box": (50, 100, 348, 300), "mask": None, "shown": 0.7, "points": [(50, 100), (348, 100), (348, 300), (50, 300)]}
clearer = {**clear, "shown": 0.9}
assert sw.touches_edge(near_edge, (640, 480)) and not sw.touches_edge(clear, (640, 480))
assert sw.best_frame({0: {1: near_edge}, 1: {1: clear}, 2: {1: clearer}}, (640, 480)) == 2
assert sw.best_frame({0: {1: near_edge}}, (640, 480)) is None
bent = [(0, 0), (100, 0), (300, 200), (0, 200)]
assert sw.distorted(bent) and not sw.distorted([(0, 0), (300, 8), (320, 200), (-10, 200)])
print("SW4c ok: the tip end keeps to the same end from frame to frame")

# SW5. Endpoints on a real (tiny) clip, SAM faked: the tip stops on each target.
W, H, FPS, SECS = 640, 480, 5, 17
tmp = Path(tempfile.mkdtemp())
clip = tmp / "c.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate={FPS}",
                "-t", str(SECS), "-pix_fmt", "yuv420p", str(clip)], check=True)
PLATE_BOX = (40, 60, 599, 434)
ph = sw.plate_h(sw.plate_quad(sw.corners([(40, 60), (599, 60), (599, 434), (40, 434)]), "tl"))
pinv = sw.solve_inv(ph)


PEN = 800


def tip_box(well):
    """A long thin pen, its end on the well, held from below (moves stay under half its length)."""
    x, y = sw.apply(pinv, sw.well_center_mm(sw.ROWS.index(well[0]), int(well[1:]) - 1))
    x, y = round(x), round(y)
    return (x - 2, y, x + 2, y + PEN)


def rec(i, box, c=0.9):
    return f"{i}<|box;x1={box[0]};y1={box[1]};x2={box[2]};y2={box[3]};w={W};h={H};c={c}|>"


class FakeSam:
    """Plate every frame; the tip stops 8 frames on each target and moves fast between.
    `hide` frames have no tip until the tip is prompted after them."""
    targets: list = []
    hide: range = range(0)
    prompts: list = []

    def image(self, target, png):
        n = int(png.stem) - 1
        if target == sw.PLATE:
            return [{"box": PLATE_BOX, "w": W, "h": H, "c": 0.5 + n / 100, "mask": None}]
        box = self.tip_at(n)
        return [{"box": box, "w": W, "h": H, "c": 0.8, "mask": None}] if box else []

    def tip_at(self, n):
        i, k = divmod(n - 2, 20)
        if 0 <= i < len(self.targets) and k < 17:  # 3.2 s on each target, 0.6 s moving
            return tip_box(self.targets[i])
        x = 20 + 13 * n % 600
        return (x, 440, x + 4, 440 + PEN)  # below the plate, a new place each frame

    def upload(self, clip):
        return {"type": "input_video", "file_id": "f1"}

    def video(self, part, prompt, on_frame=None):
        FakeSam.prompts.append(prompt)
        if on_frame:
            on_frame(1)
        refound = len(prompt.split("2<|box")) > 2  # tip prompted on 2 frames
        lines = []
        for n in range(FPS * SECS):
            parts = [rec(1, PLATE_BOX)]
            if n not in self.hide or refound:
                parts.append(rec(2, self.tip_at(n)))
            lines.append(f"<{n}f>" + ",".join(parts))
        return "\n".join(lines), 0.1


lab_sam.ROOT = tmp / "lab"
lab_sam.make_sam = FakeSam
c = TestClient(api.app)
assert len(c.get("/lab/sam-wells/targets").json()["targets"]) in (3, 4)
s = c.post("/lab/sam-wells/sessions", json={"a1": "tl"}).json()
sid = s["id"]
assert s["attempts"] == [] and s["summary"] is None
FakeSam.targets = ["C3", "F7", "B10"]
FakeSam.hide = range(26, 31)  # the tip goes missing in the 2nd stop: found again, prompted there
r = c.post(f"/lab/sam-wells/sessions/{sid}/attempts", data={"targets": "C3,F7,B10", "source": "stream"},
           files={"clip": ("c.mp4", clip.read_bytes(), "video/mp4")})
assert r.status_code == 200 and r.json()["status"] == "analyzing", r.text
a = c.get(f"/lab/sam-wells/sessions/{sid}").json()["attempts"][0]
assert a["status"] == "done", a
assert a["judge"]["line"] == "3/3 matched", a["judge"]
res = a["result"]
assert res["rounds"] == 2 and len(res["prompts"]["2"]) == 2, res["prompts"]
assert "progress" not in a  # gone once analysed
assert "track" not in res and (res["w"], res["h"], res["frames"]) == (W, H, FPS * SECS)
assert res["well_px"] > 10 and res["images"], res
assert c.get(f"/lab/sam-wells/sessions/{sid}/attempts/1/images/{res['images'][0]}").headers[
    "content-type"] == "image/jpeg"
assert c.get(f"/lab/sam-wells/sessions/{sid}/attempts/1/images/..%2Fattempt.json").status_code == 404
print("SW5 ok: an attempt is analysed and judged; a lost tip is found again and re-prompted")

# SW6. Four more (one from Files, one in the wrong order); the summary; a 6th is refused.
FakeSam.hide = range(0)
for targets, said, source in ((["D4", "E5", "F6", "G7"], "D4,E5,F6,G7", "file"),
                              (["B2", "C3", "D4"], "B2,C3,D4", "stream"),
                              (["E8", "F9", "G10"], "E8,G10,F9", "stream"),   # filled in another order
                              (["C5", "D6", "E7"], "C5,D6,E7", "stream")):
    FakeSam.targets = targets
    r = c.post(f"/lab/sam-wells/sessions/{sid}/attempts", data={"targets": said, "source": source},
               files={"clip": ("c.mp4", clip.read_bytes(), "video/mp4")})
    assert r.status_code == 200, r.text
out = c.get(f"/lab/sam-wells/sessions/{sid}").json()
sm = out["summary"]
assert sm["complete"] and sm["hits"] == sm["total"] == 16 and sm["in_order"] == 4 and sm["false"] == 0, sm
assert len(sm["settings"]["rows"]) == 9 and sm["sizes"] == [f"{W}x{H} {FPS} fps"], sm
r = c.post(f"/lab/sam-wells/sessions/{sid}/attempts", data={"targets": "C3"},
           files={"clip": ("c.mp4", clip.read_bytes(), "video/mp4")})
assert r.status_code == 409
lst = c.get("/lab/sam-wells/sessions").json()
assert lst[0]["id"] == sid and lst[0]["sources"] == ["file", "stream"] and lst[0]["accuracy"] == 1.0, lst
print("SW6 ok: five attempts give the summary; a session stays five")

# SW7. Refused: bad wells, not a clip, over 60 s, unknown session, bad corner.
s2 = c.post("/lab/sam-wells/sessions", json={"a1": "br"}).json()["id"]
assert c.post(f"/lab/sam-wells/sessions/{s2}/attempts", data={"targets": "C3,C3"},
              files={"clip": ("c.mp4", b"x", "video/mp4")}).status_code == 400
assert c.post(f"/lab/sam-wells/sessions/{s2}/attempts", data={"targets": "C3"},
              files={"clip": ("c.avi", b"x", "video/x-msvideo")}).status_code == 400
assert c.get("/lab/sam-wells/sessions/nope").status_code == 404
assert c.post("/lab/sam-wells/sessions", json={"a1": "xx"}).status_code == 400
long = tmp / "long.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=2", "-t", "61",
                "-pix_fmt", "yuv420p", str(long)], check=True)
c.post(f"/lab/sam-wells/sessions/{s2}/attempts", data={"targets": "C3"},
       files={"clip": ("long.mp4", long.read_bytes(), "video/mp4")})
a = c.get(f"/lab/sam-wells/sessions/{s2}").json()["attempts"][0]
assert a["status"] == "failed" and "max 60 s" in a["error"], a
print("SW7 ok: bad input refused; a clip over 60 s fails with why")

# SW8. A pen instead of a pipette: the session's pointer is what SAM looks for.
asked = []
_image = FakeSam.image
FakeSam.image = lambda self, target, png: asked.append(target) or _image(self, target, png)
FakeSam.targets = ["C3", "F7", "B10"]
s3 = c.post("/lab/sam-wells/sessions", json={"a1": "tl", "pointer": "pen"}).json()
assert s3["pointer"] == "pen"
c.post(f"/lab/sam-wells/sessions/{s3['id']}/attempts", data={"targets": "C3,F7,B10"},
       files={"clip": ("c.mp4", clip.read_bytes(), "video/mp4")})
a = c.get(f"/lab/sam-wells/sessions/{s3['id']}").json()["attempts"][0]
assert a["judge"]["line"] == "3/3 matched" and "pen" in asked and sw.TIP not in asked, (a, set(asked))
assert c.post("/lab/sam-wells/sessions", json={"pointer": "finger"}).status_code == 400
FakeSam.image = _image
print("SW8 ok: a pen session finds the pen")

# SW9. While it runs, the attempt says how far it is (stage, frames done of all).
seen = []
_write = lab_sam._write
lab_sam._write = lambda p, data: (seen.append(dict(data.get("progress") or {})), _write(p, data))[1]
s4 = c.post("/lab/sam-wells/sessions", json={"a1": "tl", "pointer": "pen"}).json()["id"]
c.post(f"/lab/sam-wells/sessions/{s4}/attempts", data={"targets": "C3,F7,B10"},
       files={"clip": ("c.mp4", clip.read_bytes(), "video/mp4")})
lab_sam._write = _write
stages = [x.get("stage") for x in seen if x]
assert stages[0] == "Waiting for the attempt before" and "Reading the clip's frames" in stages, stages
assert any(s.startswith("Looking for the pen") for s in stages) and any(s.startswith("SAM tracking") for s in stages)
assert any(x.get("total") == FPS * SECS for x in seen if x) and all(x["elapsed_s"] >= 0 for x in seen if x)
print("SW9 ok: progress while analysing")

# SW10. Every frame of a 15 fps clip goes to SAM: nothing is cut.
fast = tmp / "fast.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate=15", "-t", str(SECS),
                "-pix_fmt", "yuv420p", str(fast)], check=True)
s5 = c.post("/lab/sam-wells/sessions", json={"a1": "tl", "pointer": "pen"}).json()["id"]
c.post(f"/lab/sam-wells/sessions/{s5}/attempts", data={"targets": "C3,F7,B10"},
       files={"clip": ("fast.mp4", fast.read_bytes(), "video/mp4")})
res = c.get(f"/lab/sam-wells/sessions/{s5}").json()["attempts"][0]["result"]
assert (res["fps"], res["frames"]) == (15, 15 * SECS), res
print("SW10 ok: every frame is analysed")
# SW11. The first find never asks SAM about the same frame twice, and keeps the most confident.
calls = []


class CountSam:
    def image(self, target, png):
        calls.append(png.name)
        n = int(png.stem) - 1
        return [{"box": (0, 0, 9, 9), "w": W, "h": H, "c": n / 100, "mask": None}] if n >= 5 else []


fdir = sw.frames_dir(clip, [0, 7])
assert sorted(f.name for f in fdir.glob("*.png")) == ["00001.png", "00008.png"]
# a frame asked for later is that same frame
whole = Path(tempfile.mkdtemp())
subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vsync", "0", str(whole / "%05d.png")], check=True)
assert all(sw.frame_png(fdir, n).read_bytes() == (whole / f"{n + 1:05d}.png").read_bytes() for n in (0, 3, 7, 40))
found = sw.best_detect(CountSam(), "pen", fdir, FPS * SECS, FPS)
assert len(calls) == len(set(calls)) and found[0] == 6 + 2 * (sw.BEST_OF - 1), (calls, found[0])
print("SW11 ok: the first find asks once a frame")
# SW12. What SAM's video request gets: every frame, a keyframe a second, no B-frames.
sent = tmp / "sent.mp4"
sw.for_sam(clip, sent, FPS)
kinds = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pict_type",
                        "-of", "csv=p=0", str(sent)], capture_output=True, text=True).stdout.replace(",", "").split()
assert len(kinds) == FPS * SECS and "B" not in kinds, kinds
assert [i for i, k in enumerate(kinds) if k == "I"][:3] == [0, FPS, 2 * FPS], kinds
print("SW12 ok: the clip for SAM")
# SW13. The same input follows the same way, run after run, and cached or not.
n13 = FPS * SECS
box = (40, 60, 599, 434)
plate13 = lambda: {"box": box, "mask": None, "points": [(x, y) for x in range(40, 600, 8) for y in range(60, 435, 8)],  # noqa: E731
                   "shown": 1.0}
recs13 = {n: {1: plate13()} for n in range(n13)}
one, two = sw.follow_plate(clip, recs13, n13), sw.follow_plate(clip, recs13, n13)
assert one == two and one["ref"] is not None, (one["ref"], two["ref"])
cached = sw.expand(sw.json.loads(sw.json.dumps(sw.compact(recs13))))
assert sw.seen_points(cached[0][1]) == [list(p) for p in sw.seen_points(recs13[0][1])]
assert sw.convex([(0, 0), (10, 0), (10, 10), (0, 10)]) and sw.convex([(0, 10), (10, 10), (10, 0), (0, 0)])
assert not sw.convex([(0, 0), (10, 10), (10, 0), (0, 10)])  # crossed
assert not sw.convex([(0, 0), (10, 0), (2, 2), (0, 10)])  # dented
print("SW13 ok: following repeats exactly")
print("ALL PASS")
