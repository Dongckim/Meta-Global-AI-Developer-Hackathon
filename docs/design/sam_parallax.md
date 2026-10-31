# SAM well check: parallax correction (design only, not applied)

## The problem

Across the desk clips the column reads true, but stops in row B (1.0) read 0.38-0.94:
always toward the top of the screen, while columns stay within about 0.3. Below 0.5 a
stop lands in row A; that is the B1 -> A1 miss on 011204 and 004939.

The likely cause, not yet shown: the grid sits on the plane of the plate's outline,
while the tip is at the top of the wells, about 14 mm higher (SBS plate height). Seen
at an angle, a point above the plane lands further from the camera on that plane.
The same shift would show up if the tip mask stopped short of the real tip, or if
the pen hovered over the well, so step 1 comes first.

## Step 1: is it height?

No code change; from the cached tracks, for the known wells of the tuning clips:

- Offset of each stay from its well's center, in mm, in the plate's own axes.
- Parallax grows with tilt and points away from the camera. So plot the offset
  against the camera's tilt (from the homography), per clip.
- Height explains it if the offset scales with tilt and lies along the view
  direction. It doesn't if the offset is fixed, or follows the pen's direction
  (a short mask).

## Step 2: the correction

Move the grid to the plane z = 14 mm rather than shifting the tip:

1. Take the camera's focal length from the stream (glasses' calibration, or fitted).
2. Split the reference homography into rotation and translation with it.
3. Project each well center at (x, y, 14 mm) instead of (x, y, 0), and judge against
   the tip in the image. Plate following carries it as now, since the plane moves
   with the plate.

The one new value, `PLATE_TOP_MM`, goes through `--why` like any other judging value.
With no usable focal length, the fallback is a shift of the fitted size along the view
direction; that is weaker, since it fits these clips.

## Test plan

- Compare on the tuning set: B-row stops should center near 1.0, columns should stay,
  and no clip may get worse. This is a regression check only.
- New clips off row B: tuning stops are almost all in rows A-B, near the top of the
  plate. A correction fitted there may not hold at row G, so film rows B, D and G.
- Judged on the confirm set, filmed in the design's conditions.
- Stop after 2 misses on the same problem, as usual.
