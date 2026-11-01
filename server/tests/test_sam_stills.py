"""One a second: which wells the photos give, as each lands (no SAM).

Run: python -m server.tests.test_sam_stills (runs on import).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server import lab_sam_stills as T  # noqa: E402
from server import sam_wells as sw  # noqa: E402


def at(well: str) -> tuple:
    return sw.well_center_mm(sw.ROWS.index(well[0]), int(well[1:]) - 1)


def track(wells: list) -> list:
    """One photo a second; None: no pen on that photo."""
    return [(n, at(w), 100) for n, w in enumerate(wells) if w]


def names(wells: list) -> list:
    return [(s["well"], len(s["ns"])) for s in T.wells_of(track(wells))]


assert names(["D8", "D8", "D8", "D7", "D7"]) == [("D8", 3), ("D7", 2)]
assert names(["D8", "D7", "D6", "D6"]) == [("D6", 2)]  # one photo is the pen passing
assert names(["D8", None, "D8"]) == [("D8", 2)]  # a photo without the pen doesn't end a stay
assert names(["D8", "D8", "D7", "D8", "D8"]) == [("D8", 4)]  # a pass over a neighbour doesn't split it
assert names(["D8", "D8", "D7", "D7", "D8", "D8"]) == [("D8", 2), ("D7", 2), ("D8", 2)]  # really went back
assert names(["D8", None, None, None, "D8"]) == []  # too far apart to be one stay
assert T.wells_of([(0, (-50, -50), 1), (1, (-50, -50), 1)]) == []  # off the plate

assert T.before({1: "a", 3: "b"}, 3) == "a" and T.before({1: "a"}, 1) is None and T.before({}, 5) is None

# Judged as each photo lands, with stand-ins for SAM's masks and the plate's outline.
QUAD = [(0.0, 0.0), (sw.PLATE_MM[0], 0.0), (sw.PLATE_MM[0], sw.PLATE_MM[1]), (0.0, sw.PLATE_MM[1])]
T.own_grid = lambda plates, pens, size: QUAD if plates else None


def pen(well: str) -> list:
    x, y = at(well)
    return [{"points": [(x, y), (x + 0.5, y), (x + 1, y)]}]


def shown(run: T.Run) -> list:
    return [w["well"] for w in run.wells]


SIZE = (720, 1280)
run = T.Run("x", "tl", sw.TIP)
T.place(run, 1, [], pen("D8"), SIZE)
assert not run.track  # no plate on any photo yet: nothing to read it on
T.place(run, 2, ["plate"], pen("D8"), SIZE)
assert not run.wells  # one photo is the pen passing
T.place(run, 3, [], pen("D8"), SIZE)  # no plate here: the one before holds
assert shown(run) == ["D8"] and run.wells[0]["photos"] == 2
T.place(run, 5, ["plate"], pen("D7"), SIZE)
T.place(run, 4, ["plate"], pen("D7"), SIZE)  # landed late
assert shown(run) == ["D8", "D7"], shown(run)
assert run.wells[0]["confidence"] == "sure", run.wells

# The correction moves the grid a row down: the photos already judged are read again.
ROW = sw.PITCH_MM / sw.PLATE_MM[1]
T.set_fit(run, {"fit": [(x, y + ROW) for x, y in sw.UNIT], "wells": 40})
assert shown(run) == ["C8", "C7"], shown(run)
assert run.fit_wells == 40

# The plate is asked for on a schedule, sooner when the view moved.
import numpy as np  # noqa: E402

still, other = np.zeros((1280, 720), np.uint8), np.full((1280, 720), 40, np.uint8)
run = T.Run("x", "tl", sw.TIP)
assert [T.plate_due(run, n, still) for n in range(7)] == [True, False, False, True, False, False, True]
assert run.plate_calls == 3
assert T.plate_due(run, 7, other) and not T.plate_due(run, 8, other)  # moved, and only once for it

# A plate that lands after the photos behind it, which were quicker, corrects them.
run = T.Run("x", "tl", sw.TIP)
run.quads[0] = [(x + 1000, y) for x, y in QUAD]  # the view before the head moved: the pen's off this grid
T.place(run, 2, [], pen("D8"), SIZE)
T.place(run, 3, [], pen("D8"), SIZE)
assert not run.wells
T.place(run, 1, ["plate"], pen("D8"), SIZE)
assert shown(run) == ["D8"] and run.wells[0]["photos"] == 3, run.wells

print("ALL PASS")
