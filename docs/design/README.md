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
| Start | ⑤a Mode | 37:13141 | done (Private: frame) | 3 |
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

## Later

- ③b checklist **"One by one"** (answer the cards one at a time): needs a card
  mode in the server view and an answer word; the checklist shows "Yes to all"
  only until then.
- ⑥ Learn.
- Draft value edits (⑤d "Fix it"): the old tap editor sheet is gone; "Fix it" says it isn't ready yet.

Update the status column when a bundle lands.
