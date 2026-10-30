# Design reference

- **Layout, color, type**: Figma Glassistant, page "ver 2 - Final"
  <https://www.figma.com/design/UTzaU4F5RX3ROEckJQMMsR/Glassistant?node-id=20-31108>
  (fileKey `UTzaU4F5RX3ROEckJQMMsR`, node `20:31108`). Frames are 1376 x 1032 design px;
  the app scales them with `Theme.px` / the screen's `u`.
- **Motion**: the BenchTrace · Milo HTML prototype (v36, kept outside the repo). Its
  values live once in `Theme.Motion`; screens use them through the shared parts.
- **Names**: the app is Glassistant and the agent is Milo ("BenchTrace" is only the
  motion prototype's title). Private mode reuses every screen; only `Theme` mode tokens (color, badge,
  line) change (Figma section `43:2`).

## Screens

Status: **done** = matches ver 2 · **old** = exists in the pre-ver-2 style ·
**frame** = drawn from one data type, no feature behind it yet (registry `status:
"planned"`) · **todo** = not built.

| Group | Screen | Figma frame | Status | Bundle |
|---|---|---|---|---|
| Home | Splash | 37:12807 | done | 2 |
| Home | Glasses sync | 37:12820 | done (rows: the app's six checks) | 2 |
| Home | ⓪ Home · Protocols | 37:12877 | done | 2 |
| Home | ⓪b Home · Run record | 37:12931 | done (Code tab: frame) | 2 |
| Home | ⓪c Home · Web searches | 37:13104 | done | 2 |
| Start | ⑤a Mode | 37:13141 | done | 3 |
| Start | ⑤b Import protocol | 37:13167 | done (Text, File, Photo) | 3 |
| Start | ⑤b Draft ready | 37:13203 | done | 3 |
| Start | ⑤c Review steps | 37:13230 | done | 3 |
| Start | ⑤d Step source | 37:13288 | done (Fix it: planned) | 3 |
| Start | ⑤e PDF page | 37:13320 | done (PDF, photo or text) | 3 |
| Start | ⑤f Plan check | 37:13351 | done | 3 |
| In run | ① Main | 37:13521 | done | 1 |
| In run | ③ Confirm · single | 37:13714 | done | 1 |
| In run | ③b Confirm · checklist | 37:13742 | done (One by one: later) | 1 |
| In run | ③c Confirm · catch | 37:13777 | done | 1 |
| In run | ③d Confirm · next step | 37:13804 | done | 1 |
| In run | ④ Well card | 37:13833 | done | 1 |
| In run | ⑦ Label check | 37:14014 | frame | 4 |
| In run | ⑧ Web search answer | 37:14048 | done ("Source" or "Pages searched") | 4 |
| After run | ⑨ Simulate | 37:14080 | frame (robot panel stays) | 4 |
| After run | ⑨a Export | 37:14280 | frame | 4 |
| After run | ⑨b Files | 37:14316 | frame | 4 |
| After run | ⑨c Where to | 37:14393 | frame | 4 |
| After run | ⑨d Exported | 37:14427 | frame | 4 |
| — | ⑥ Learn | 37:14460 | later (out of scope) | — |
| Private | Private variants of the above | section 43:2 | mode tokens (Theme.RunMode) | — |

Reference PNGs exported from Figma live in `docs/design/ref/` (read only, not
committed). Milo's line at the top of each screen is the registry screen's
`prompt` (pip_voice.json `prompts`), filled by the server; button words are
the registry `buttons` (templated ones like "Step {step}" fill from the view's
slots).

## Not in the design (디자인에 없음, for Eunbi to check)

States the app draws that have no Figma frame; each is built from the
nearest frame's parts and should get a look.

- **Not marked card** (③ family, card kind `unmarked`): "Next step" with wells
  left. ③c's place and size (x 310, w 756, h 360), a small line over the title
  "Step {n} · {step short title}", title "{wells} not marked", detail "Mark all
  {n} done and go to step {m}?"; two buttons 360 x 93, 36 apart (primary "Mark
  them", muted "Skip them") and "Not yet" as a small text button under them.
  The header shows one line, "{wells} not marked."
- **Private model waking** (screen `private_wait`): after Private on ⑤a, or
  when a turn needs the model while it sleeps. Header as usual with a slower
  orb breath (6.4 s) and lock + "Private". Behind: the next screen as a
  skeleton (⑤b's three 345 tall cards, else one) in `cardGlass` with the
  search sheen slower and quieter (1.6 s, peak .16). Over it, a 1000 wide white
  card: one line (40 semibold, opacity crossfade .5 s) and the seconds ("12 s",
  28). Awake: crossfade to the real screen (`Motion.screen`). Failed: a 900
  wide card, "The private model didn't wake up." with Try again (primary) and
  Use Full mode (secondary), both 360 wide.
- **⑨0 Final protocol**: the ⑤c-⑤e review screens in final protocol mode
  (plan → run, Changed badge, the step's changes with their record event;
  Keep as run / Use plan on a timer step). See `handoff.md`.
- **⓪b Run record · Code tab**: protocol.py where the plate is, a colored bar
  and step badge per final protocol step; the steps list on the right.
- **⓪b Run record · Share**: in place of the greyed Start run once the run's
  robot code passed.
- **① Main · Finish**: the last step's button in place of Next step.

## Later

- ③b checklist **"One by one"** (answer the cards one at a time): needs a card
  mode in the server view and an answer word; the checklist shows "Yes to all"
  only until then.
- ⑥ Learn.
- Draft value edits (⑤d "Fix it"): the old tap editor sheet is gone; "Fix it" says it isn't ready yet.

Update the status column when a bundle lands.
