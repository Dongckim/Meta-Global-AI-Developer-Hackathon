# SAM well check: Voice-anchored design

The tester says each well just before filling it. The name marks the part of the
video to look at, and SAM answers about that part alone, instead of a whole clip.

Tested on the Diagnostics SAM well test page. The product flow (Bench, the
registry, voice commands) stays out of this until P3. Wells are judged by SAM 3.1
(Meta), aiming by Apple Vision (an OS feature), and head movement is corrected by
OpenCV ORB + RANSAC only, with no learned models (no `cv2.dnn`).

## 1. How to film

- The plate on the desk, portrait: its long side runs along the screen's long side.
- The whole plate in frame, not touching the screen's edge.
- Start after "Plate in view".
- Come in from the side, so the wrist doesn't cover the plate.
- LOW, 360 x 640, 7 fps, with HFP kept on.
- Say the well's name before going into it.

## 2. iPad

- Keeps the last 2 s of frames in a ring buffer.
- A recognized well name plays a low ding and opens a segment, starting 1 s before
  the moment of recognition.
- The segment closes on the next recognized well name or after 10 s, whichever
  comes first. 10 s counts from the segment's start, so a segment is at most 70
  frames.
- Only that segment is uploaded, as a short MP4 with its target well. Each segment
  is its own request.

## 3. Server

Per segment, concurrently.

1. **Plate reference, once a session.** Pick a frame with the whole plate in frame,
   off the screen's edge, and the least of it covered. SAM image ("96-well plate")
   -> the convex quad's four corners, opposite sides checked for parallel -> the SBS
   8 x 12 grid. Reused for the rest of the session.
2. **Following the plate.** Each frame gets a homography to the reference by ORB +
   RANSAC. Matches are only searched near the last frame's position, and the tip and
   hand areas are left out. On failure: SAM finds the plate again if it's all in
   view, otherwise the last position stands.
3. **Tip.** The last segment's tip box carries over (with none, text detection of
   "pipette tip") -> SAM 3.1 video tracking from that box. The tip end is the end of
   the long axis that's over the plate; with both over it, the one nearer the last
   frame's. Masks decode on all CPUs.
4. **Judging.** A stay of 1.0 s or more within 4.5 mm, allowing 1-2 missing frames.
   A visit's well is the cell it stayed in longest.
5. **Result.** `confirmed`, `other_well` (with which), or `not_seen`, with a
   confidence.

## 4. Screen

No sound for results; the low ding when a name is heard is the only sound.

| Result | On the plate drawing | Product rule (P3) |
|---|---|---|
| `confirmed` | Well filled green, eye icon | Recorded |
| `other_well` | Said well outlined orange, seen well dashed, "Saw C3, not C2" | Milo asks to confirm |
| `not_seen` | Gray question mark, in the summary at the step's end | Summary at the step's end |

## 5. Speed

- A segment is at most 70 frames.
- SAM tracks the tip only.
- A result within 15-25 s of the segment closing.

## 6. Passing

On the confirm set: well accuracy of 90% or better, and at most 1 false well per 10
wells. Passing leads to P3.

## 7. Alternatives

- If following the plate keeps wavering: SAM "well" detection, and RANSAC fits an
  8 x 12 grid to the visible well centers.
- For a transparent tip: the "pipette" prompt, extended along the body's direction.
