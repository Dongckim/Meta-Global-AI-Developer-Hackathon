# SAM well test: Voice-anchored mode (definition only, not built)

The tester says each well while working; the name marks where to look, and SAM
answers about that moment instead of the whole clip. Test page only: the product
flow doesn't use it. The ding exists as `SamTone.lowDing` (`PlateAim.swift`).

## Behavior

- **Listening needs the glasses mic.** A recording runs with HFP on, so the stream
  is fixed at LOW (360 x 640).
- **Mark.** While recording, a recognized well name is logged with its time. That
  well's segment is 3 s before to 2 s after that time.
- **Heard.** On recognizing the name: one low ding (`.lowDing`), at once. It's the
  only sound in this mode.
- **Ask.** Each segment goes to SAM as soon as it is complete, at the same time as
  any other segment. The recording doesn't have to end.
- **Answer.** On the screen only, no sound, as each result comes in:
  - SAM confirms that well: the well filled green, with an eye icon.
  - SAM saw another well: the said well outlined orange, the well it saw dashed, and
    "Saw C3, not C2".
  - SAM saw nothing: a gray question mark on the well.

## To settle when built

- LOW has about 2/3 of MEDIUM's pixels across, so a well is about 15 px pitch.
  Nothing has been measured at LOW yet; the test set is 2-7 fps at 504 and 720 wide.
- The segment window (5 s) must fit one stay, and 3 s back has to be in a buffer
  before the name is heard.
- Sound effects don't mute listening.
