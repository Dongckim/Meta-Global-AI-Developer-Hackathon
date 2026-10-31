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
| Private | Private variants of the above | section 43:2 | done: dark scheme on screens whose header says Private; `Theme` tokens, white surfaces `onLight()`. Variant ⑤a not used (its "Nothing leaves this iPad" isn't true) | — |

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
  when a turn needs the model while it sleeps. Dark tokens, no white card, no
  skeleton. The header keeps lock + "Private"; its orb and line hide. In the
  middle a 160 orb with a progress ring (5 wide), then "Waking up the private
  model" (44 semibold, white), a witty line (28, secondary, a new one every
  6 s, opacity crossfade .5 s) and "Glimmer · on our server · 12 s" (20).
  Ring: p = .9 × (1 − e^(−t/τ)), τ half the median of recent cold starts, so
  only ready fills it. Motion: the orb flies in from the header (.5 s) and
  breathes (.96–1.04, 2.4 s). Ready: ring to 100% in .3 s, orb bright, "I'm
  awake.", .6 s later the orb flies home and ⑤b's cards rise 12 with .08 s
  between. Failed: the ring stops orange, "The private model didn't wake up.",
  Try again (primary) and Use Full mode (secondary), both 360 wide. Reduce
  Motion: ring only. All times are `Theme.Motion`.
- **⑤a Mode cards, what each mode does**: inside the 543 x 345 card, centered:
  icon + "Say Full" (50), the line (22), then the path a command takes, two
  chips (78 tall, radius 20, SF Symbol + name 19 semibold + where 16) with a
  chevron between, then one or two fine print lines (18: the feature semibold,
  then how it runs in secondary, 10 apart). Full's card is white,
  Private's the dark canvas color on any screen. After the card is in, chip,
  chevron, chip arrive left to right (.35 s, then .1 s apart, 8 from the left);
  Reduce Motion: no motion. Saying "what's the difference?" on this screen
  speaks the same facts. All of it comes from the routing settings
  (`server/modes.py`, snapshot `modes`).
- **Private badge on run lists**: on Home's and the protocol's run rows and on
  Run record, a 30 tall capsule in the dark canvas color with a lock (14) and
  "Private" (18 medium, white), after the row's day. Only Private runs have it.
- **⑤a Mode, Private variant (differs from the design)**: its line "Nothing
  leaves this iPad" isn't true, so it isn't used. What happens: speech is
  transcribed on the iPad; voice commands and imports run on Glimmer on our
  server; search and photo questions use the Meta Model API after a card's Yes.
- **Home, Mode, protocol detail, Run record stay light in Private**: the
  design has no Private variant of them.
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
