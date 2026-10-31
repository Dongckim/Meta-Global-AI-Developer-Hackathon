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


# SW4. Settings: a 0.25 s stop at the 2nd target only the 0.2 s dwell catches;
# the best setting is the one with the most hits.
def stay(well, f0, frames):
    r, c = sw.ROWS.index(well[0]), int(well[1:]) - 1
    return [(f, sw.well_center_mm(r, c), 100 - f) for f in range(f0, f0 + frames)]


track = stay("C3", 0, 9) + stay("F7", 20, 5) + stay("B10", 40, 9)  # 15 fps: 0.53 s, 0.27 s, 0.53 s
assert [w for w, *_ in sw.found_wells(track, 15)] == ["C3", "B10"]
cmp = sw.compare_settings([(["C3", "F7", "B10"], track, 15)])
best = cmp["rows"][cmp["best"]]
assert len(cmp["rows"]) == 9 and best["dwell_s"] == 0.2 and best["hits"] == 3, best
print("SW4 ok: every dwell setting on the same clips; the best one marked")

# SW5. Endpoints on a real (tiny) clip, SAM faked: the tip stops on each target.
W, H, FPS, SECS = 640, 480, 15, 3
tmp = Path(tempfile.mkdtemp())
clip = tmp / "c.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate={FPS}",
                "-t", str(SECS), "-pix_fmt", "yuv420p", str(clip)], check=True)
PLATE_BOX = (40, 60, 599, 434)
ph = sw.plate_h(sw.plate_quad(sw.corners([(40, 60), (599, 60), (599, 434), (40, 434)]), "tl"))
pinv = sw.solve_inv(ph)
mid_y = sw.apply(pinv, (sw.PLATE_MM[0] / 2, sw.PLATE_MM[1] / 2))[1]


def tip_box(well):
    """A thin tip whose end is on the well, its body away from the plate's middle."""
    x, y = sw.apply(pinv, sw.well_center_mm(sw.ROWS.index(well[0]), int(well[1:]) - 1))
    x, y = round(x), round(y)
    return (x - 2, y, x + 2, y + 40) if y > mid_y else (x - 2, y - 40, x + 2, y)


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
        i, k = divmod(n - 2, 12)
        if 0 <= i < len(self.targets) and k < 8:
            return tip_box(self.targets[i])
        return (20 + 13 * n, 4, 24 + 13 * n, 44)  # above the plate, a new place each frame

    def upload(self, clip):
        return {"type": "input_video", "file_id": "f1"}

    def video(self, part, prompt):
        FakeSam.prompts.append(prompt)
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
FakeSam.hide = range(20, 30)  # the tip goes missing in the 2nd stop: found again, prompted there
r = c.post(f"/lab/sam-wells/sessions/{sid}/attempts", data={"targets": "C3,F7,B10", "source": "stream"},
           files={"clip": ("c.mp4", clip.read_bytes(), "video/mp4")})
assert r.status_code == 200 and r.json()["status"] == "analyzing", r.text
a = c.get(f"/lab/sam-wells/sessions/{sid}").json()["attempts"][0]
assert a["status"] == "done", a
assert a["judge"]["line"] == "3/3 matched", a["judge"]
res = a["result"]
assert res["rounds"] == 2 and len(res["prompts"]["2"]) == 2, res["prompts"]
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
print("ALL PASS")
