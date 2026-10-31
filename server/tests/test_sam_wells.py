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


# SW4. Settings: a 0.8 s stop at the 2nd target the 1 s dwell misses, the 0.5 s default catches;
# the best setting is the one with the most hits.
def stay(well, f0, frames):
    r, c = sw.ROWS.index(well[0]), int(well[1:]) - 1
    return [(f, sw.well_center_mm(r, c), 100 - f) for f in range(f0, f0 + frames)]


track = stay("C3", 0, 16) + stay("F7", 20, 5) + stay("B10", 40, 16)  # 5 fps: 3 s, 0.8 s, 3 s
assert [w for w, *_ in sw.found_wells(track, 5)] == ["C3", "F7", "B10"]
assert [w for w, *_ in sw.found_wells(track, 5, 1.0)] == ["C3", "B10"]
cmp = sw.compare_settings([(["C3", "F7", "B10"], track, 5)])
best = cmp["rows"][cmp["best"]]
assert len(cmp["rows"]) == 9 and best["dwell_s"] == sw.DWELL_S and best["hits"] == 3, best
print("SW4 ok: every dwell setting on the same clips; the best one marked")

PLATE_BOX_4C = (0, 0, 639, 479)

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

# SW4c. The tip end follows the last frame's end, whichever side of the plate's middle it is.
pen = [(100 + i, 100) for i in range(50)]
assert sw.tip_end(pen, (0, 100)) == (100, 100) and sw.tip_end(pen, (500, 100)) == (149, 100)
# frame 0: the end nearer the middle (x 320) is 300, of a pen lying 300-200; then it slides past
# the middle and the end stays the pen's left one, now the farther from the middle.
pen_boxes = ((200, 200, 300, 204), (340, 200, 440, 204))
recs = {n: {1: {"box": PLATE_BOX_4C, "mask": None}, 2: {"box": b, "mask": None}} for n, b in enumerate(pen_boxes)}
track4c, per4c, *_ = sw.locate(recs, 2, "tl")
assert [per4c[n][4][0] for n in (0, 1)] == [300, 340], per4c
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
print("ALL PASS")
