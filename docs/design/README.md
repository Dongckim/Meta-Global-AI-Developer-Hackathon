# Design reference

- **Layout, color, type**: Figma Glassistant, page "ver 2 - Final"
  <https://www.figma.com/design/UTzaU4F5RX3ROEckJQMMsR/Glassistant?node-id=20-31108>
  (fileKey `UTzaU4F5RX3ROEckJQMMsR`, node `20:31108`). Frames are 1376 x 1032 design px;
  the app scales them with `Theme.px` / the screen's `u`.
- **Motion**: the BenchTrace · Milo HTML prototype (v36, kept outside the repo). Its
  values live once in `Theme.Motion`; screens use them through the shared parts.
- **Names**: the app is BenchTrace and the agent is Milo; the mock's "Glassistant" is
  not used. Private mode reuses every screen; only `Theme` mode tokens (color, badge,
  line) change (Figma section `43:2`).

## Screens

Status: **done** = matches ver 2 · **old** = exists in the pre-ver-2 style ·
**frame** = drawn from one data type, no feature behind it yet (registry `status:
"planned"`) · **todo** = not built.

| Group | Screen | Figma frame | Status | Bundle |
|---|---|---|---|---|
| Home | Splash | 37:12807 | old (compare) | 2 |
| Home | Glasses sync | 37:12820 | old (compare) | 2 |
| Home | ⓪ Home · Protocols | 37:12877 | old | 2 |
| Home | ⓪b Home · Run record | 37:12931 | old | 2 |
| Home | ⓪c Home · Web searches | 37:13104 | todo | 2 |
| Start | ⑤a Mode | 37:13141 | todo | 3 |
| Start | ⑤b Import protocol | 37:13167 | old (importer sheet) | 3 |
| Start | ⑤b Draft ready | 37:13203 | old (draft panel) | 3 |
| Start | ⑤c Review steps | 37:13230 | old (draft panel) | 3 |
| Start | ⑤d Step source | 37:13288 | old (source view) | 3 |
| Start | ⑤e PDF page | 37:13320 | old (source view) | 3 |
| Start | ⑤f Plan check | 37:13351 | todo | 3 |
| In run | ① Main | 37:13521 | done | 1 (compare only) |
| In run | ③ Confirm · single | 37:13714 | done | 1 |
| In run | ③b Confirm · checklist | 37:13742 | done | 1 |
| In run | ③c Confirm · catch | 37:13777 | done | 1 |
| In run | ③d Confirm · next step | 37:13804 | done | 1 |
| In run | ④ Well card | 37:13833 | done | 1 |
| In run | ⑦ Label check | 37:14014 | todo (frame) | 4 |
| In run | ⑧ Web search answer | 37:14048 | todo (frame) | 4 |
| After run | ⑨ Simulate | 37:14080 | old (robot panel) | 4 |
| After run | ⑨a Export | 37:14280 | todo (frame) | 4 |
| After run | ⑨b Files | 37:14316 | todo (frame) | 4 |
| After run | ⑨c Where to | 37:14393 | todo (frame) | 4 |
| After run | ⑨d Exported | 37:14427 | todo (frame) | 4 |
| — | ⑥ Learn | 37:14460 | later (out of scope) | — |
| Private | Private variants of the above | section 43:2 | mode tokens only | per bundle |

Update the status column when a bundle lands.
