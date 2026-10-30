# Hand off — run → approved as-run protocol → robot code

UI reference: `benchtrace-milo-v36.html` (sections `handoff`, `files`,
`done`), not Figma. ⑨0 has no design: it reuses the import review screens.
Follows pip_agent_v3: one record, one action path, one registry, one
contract. The model picks from code-computed candidates; code validates.

## Why

`server/robot.py` filled a fixed template with the done wells (50 µL from
reservoir A1) and simulated it on Modal. Nothing gathered what changed
during the run, nobody approved it, and plan volumes, liquids and timer
changes never reached the code. Now the robot code comes from an approved
as-run protocol, and code (not the model) proves the robot code matches it.

## Flow (demo path: four screens)

```
"All steps done. Finish the run?" yes   (run archived as today)
 └ ⑨0 Final protocol  final_protocol / final_step / final_page
     the import review screens (⑤c, ⑤d, ⑤e) in "final protocol" mode
     Say "Looks right" → approval written, the loop starts
 └ ⑨  Simulate        simulate: generating → simulating → (fixing) → passed
 └ ⑨b Files           export_files: plate_map.csv + protocol.py
 └ ⑨d Exported        exported: files saved with the run, Done → run_view
```

⑨a Export and ⑨c Where to stay `planned`. ⑨c's Robot card will read
"Save for Opentrons App" (a file save, never a push). Private runs skip hand
off: the run ends as before and `make_robot` says it is off in Private.

Hand off works on the finished run doc (finishing archives the record), so
its state lives on that doc (`handoff`), and the snapshot serves it while a
hand off screen shows that run.

## ⑨0 Final protocol

Robot steps are the plan's pipette steps and its timed steps (incubate /
wait with minutes: a robot `delay`). For each robot step the as-run wells
are the step's done marks minus every excluded well. Steps the run never
reached are left out ("n of m").

Changes, computed by code, each with the record event it came from:

| change | record event | as-run protocol |
|---|---|---|
| excluded | `excluded{well: reason}` | removed from every step |
| skipped (passed over unmarked) | `skipped[]` (step, ts) | removed from that step |
| not marked (no skip, no mark) | none | removed from that step |
| redo well | `redo{well: note}` + its done marks | in the steps it was marked in |
| marked off plan | a done mark outside the step's wells (source, ts) | added to that step |
| timer ran longer / shorter | `timer_log` (planned → final minutes) | step minutes = final |

`timer_log` is new: timers leave `timers` a minute after they end, so the
record kept no actual time. Each timer that ends or is cancelled appends
`{step_index, label, planned, minutes, started_at, ended_at, status}`.

Later: Spark reading notes for volume / liquid changes ("used 12 µL in
C2–C4") as candidates the person approves.

Screens (the app's review components with a mode, no copied views):
- `final_protocol` (⑤c layout): rows as the import review, `plan → run` and
  a change badge on changed steps; buttons `Looks right`, `Step {step}`.
- `final_step` (⑤d layout): the step, its changes with their record events,
  the quote; buttons `Looks right`, `Open {doc}`.
- `final_page` (⑤e layout): the protocol's original page.
Voice and buttons are the same as review (`step 2`, `open PDF`, `next
page`, `back`). Final protocol UI may change: the server sends structured
changes (kind, wells, from, to, event) and the app draws them in one small
view.

`Looks right` (`approve_handoff`) writes `handoff.approved = {hash, ts,
steps}` (hash of plan, done, excluded, redo, skipped, timer_log,
step_index). Before simulate and export the hash is checked; a changed run
record clears the approval and returns to ⑨0.

## ⑨ Simulate: Spark orchestration loop

A background task started by the approval. Spark (Responses API) gets four
tools; none writes the record.

- `read_run()` → the approved as-run steps (liquid, volume, unit, wells,
  minutes) and the fixed deck: Flex, `flex_1channel_1000` left, 96 tiprack
  1000 µL C1, `nest_12_reservoir_15ml` D1 (liquids in plan order → A1, A2,
  …), `corning_96_wellplate_360ul_flat` D2, trash A3. The code must call
  `protocol.comment("step N")` before each step.
- `generate_protocol(code)` → stores the text.
- `simulate()` → runs it; returns ok, error, and the match diff.
- `finish(summary)` → accepted only after a passing simulate.

Stage (in `handoff.stage`, drawn on ⑨'s header): `generating`,
`simulating`, `fixing` (a generate after a failed simulate), `passed`,
`needs_you` (3 failed simulates or the model stopped). `needs_you` shows the
error with `Try again` / `Back`; no files without `passed`.

Match check (code): the simulator's run log as events (aspirate source well,
dispense well + volume into the plate, delay minutes; mix strokes left out)
split by the `step N` comments. Pass = every as-run step present in order,
the same dispense wells with the same volume, the liquid's reservoir well
as source, delay minutes equal, and nothing dispensed into a well that is
not in that step (so never into an excluded one).

## Simulator

`ROBOT_SIM=local|modal`, default `local`, so judges need no outside account.
- local: `.venv-sim` (Python 3.12, `uv venv --python 3.12 .venv-sim && uv pip
  install --python .venv-sim/bin/python opentrons`; 3.11 fails on pydantic),
  `server/robot_runner.py` in a subprocess, a temp folder as cwd, env PATH
  plus `OT_API_CONFIG_DIR` (temp; opentrons writes config there instead of
  ~/.opentrons), 30 s limit, 64 KB output limit.
  Measured: install 21 s, one simulate ~1 s (2 s cold).
- modal: `modal run server/robot_sim.py` runs the same runner in a Sandbox.

## Contract

- record: `timer_log` (list). Run doc: `handoff` `{stage, attempt, error,
  check, code, csv, approved, exported}`.
- snapshot: `handoff` (null unless a hand off screen shows): run_id, stage,
  rows / others / counts (the review summary shape plus `plan`, `run`,
  `changes`), meta (the protocol's origin, original id, text), files, check.
- api_version 19 → 20. `robot_preview` and `POST /robot` retire;
  `make_robot` opens `final_protocol` for the latest finished run.

## Tests (server/tests/test_handoff.py; model and simulator mocked)

1. changes: excluded / skipped / not marked / redo / off plan / timer → rows
   with their events; not-reached steps left out.
2. approve → as-run steps; a changed run record clears the approval.
3. loop passes first try → generating, simulating, passed; files written.
4. one failed simulate → fixing → passed.
5. three failures → needs_you, no files, finish rejected.
6. sim ok but a dispense into an excluded well / wrong volume / wrong order
   → not passed.
7. finish before simulate → rejected.
8. run record unchanged by the loop (only `handoff` moves).
9. voice and buttons: "looks right", "step 2", "open PDF", "export", "done".
10. private run: no hand off.
Later: note candidates (dropped when not grounded; rows only after approval).
Plus one live check: the local runner on a real protocol.
