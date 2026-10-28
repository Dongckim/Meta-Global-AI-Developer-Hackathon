# Pip agent v3 — one state, one action path, one registry, one contract

Decisions locked: ask stays a tool (Q1); view carries a back stack (Q2);
Setup/offline/banners stay app-local (Q4); jsonschema validates the
snapshot contract (Q5). Piece 1 is implemented; pieces 2–3 below are the
approved design. No commits until each piece passes tests + fake_app
smoke + iPad check.

## Piece 1 (done): contract, version, fake app

- `shared/schemas/snapshot.schema.json`: record top level strict
  (version/plan/step_index/done/excluded/redo/notes/tips/timers/stamps/
  cards/recent + nullable run/draft); nodes tolerate additive keys.
  Timer `wells` is a string label (matches server + app).
- `server/tests/test_snapshot_schema.py`: 8 representative states
  validate; schema negative tests; `API_VERSION` (server) equals
  `EXPECTED_API_VERSION` (app) equals `/health api_version`.
- App decoder: missing/null collections default, wrong types throw to
  a red `Out of sync: <reason>` banner (root, every screen except
  setup); last good snapshot kept; clears on next good snapshot.
  Draft decode errors never take down the bench. Replicates null
  falls back to wells count (server also normalizes in
  `build_start_plan`).
- `/health api_version` (now 1); app compares at startup, orange
  mismatch banner (non-blocking). Bump rule: any shared/schemas,
  view, or Action-shape change.
- `eval/fake_app.py`: `TestBackend` (in-process) + `LiveBackend`
  (SSE+POST vs a running server); text bench render; `smoke_turns.py
  --fake` (strict) / `--fake-live` (consistency-only).

## Piece 2 (done, pending iPad check): server-owned view + one action path

### State

- `record.view = {current: {screen, target}, stack: [{screen, target}],
  tab, sheet}`. The app renders `view` and sends taps as Actions.
  Back pops `stack` (parents stay in the registry for fresh entry).
- Session `screen`/`run_id` are deleted; `view.target` is the run
  scope: on `run_view` with a non-active target, writes are refused
  ("That run is finished — read only"). Viewing a source keeps active
  scope (matches v2 behavior).
- App-local, explicitly NOT in view: Setup (first run, no-server
  boot), offline/unreachable state, contract banners. The app boots to
  setup, then renders server view.
- RUN9-style coverage moves to view-target scoping tests.

### Actions

- Transport: `/input {action: {id, args}}` for taps, buttons, and
  sheet picks. Card taps send `answer_card {card_id, ok}`.
- Grammar emits Actions (same handler). Voice yes/no/cancel still go
  through the card discriminator, which emits `answer_card`.
- `answer_card` is EXCLUDED from the model's `perform` enum: only taps
  and the voice discriminator emit it. A test pins that no model tool
  list contains it and that model calls can never change card state.
- Model tools: `perform(action, args)` + `read_record` + `ask`
  (questions are not actions). `args` is one flat strict object with
  nullable fields (wells, well, target, text, minutes, label, note,
  reason, kind, filter, limit, run, screen); the handler validates
  per action. `ask(question)` keeps its v2 meaning and lifetime.
- The handler is today's `_apply_call`, reshaped to Action input:
  same validation, same cards, same evidence-strength tiers
  (utterance→direct, memory/plan→card with reason, none→ask).
  No safety rewrite in this piece.

### Replies: ungrounded-target rule (replaces the verb list)

- After a turn whose reply would be raw model text (zero tool
  calls), extract mentioned record targets: well refs (`[A-H]\d+`,
  ranges), timer words (`timer` + optional label), step refs
  (`step N`, `next step`), memo/note refs (`note on X`, `memo`).
- Ground each mentioned target against THIS turn's writes and reads
  (well args, read_record wells/notes/timers/steps filters + returned
  rows). Any mentioned-but-untouched target discards the whole text.
- Replacement: if the user's utterance mentioned that target too →
  didnt_act naming the target ("I didn't log C7 — want me to?") and
  set asked so the follow-up grounds the intent; otherwise neutral
  ack "Got it!" (new pip_voice key `ack`). To talk about record
  state, the model must call read_record first.
- The v2 `?` exemption stays: questions claim nothing
  ("Should I drop C7?" passes; "C7 is dropped." is discarded).
- Expected fallout: smalltalk replies naming wells ("cool" →
  "Nice, we're set on C2–C8") become "Got it!". The instruction
  eval's smalltalk expectations change accordingly.

### View transitions

- Navigation actions (`open`/`navigate`/`back`) update `view` in the
  handler and broadcast; the app renders. No optimistic local nav:
  LAN round trip only. UI flash/peek/sheet hints keep flowing over
  the existing `ui` channel.

### Pre-piece-2 incident (2026-10-28): stale plate, dead-feeling taps

- Symptom: server + fake_app agreed (ALL PASS, v14), voice replies
  normal, but the iPad plate never refreshed and well taps felt dead.
- Cause (corrected): `lab.sync` ran on background threads. The
  SSE loop and the initial `/record` fetch live in an unstructured
  `Task` in `connect()`, so snapshot mutations fired Observation
  notifications off-main and SwiftUI dropped the re-renders
  (same-shape code had rendered fine before, ruling out a
  structural cause). The earlier `let`-vs-`@Bindable` theory was
  wrong: Observation tracks reads through a plain `let` too.
- Minimal fix (kept): `@MainActor` on `LabState` (compile-enforced),
  `await MainActor.run` hops at every background touch: initial
  `/record` sync, the whole SSE `handleEvent`, the `handle()` call
  plus the `currentScreen` capture in `sendText`/`sendVoice`
  (covers `lastShown`/`flash` and the `applyUI` lab reads), and the
  five `lab.draft` writes in import/start/save/discard. `@Bindable`
  in the 7 child views kept (harmless, correct direction). Plus an
  always-visible source line (`LIVE · run <id> · v<n>` on the
  experiment plate, `VIEWING past run <id> (read-only)` in
  run_view).
- Ruled out: no `allowsHitTesting(false)` or tap-blocking overlay in
  the plate path; `.disabled` only on review/send buttons; exactly
  two `LabState` instances (`ContentView.lab` = the SSE-updated one,
  `PastRunView.rlab` = one-shot read-only copy). Start-new-run path
  verified: POST → `selectedProtocol = nil` → brief home flash while
  `lab.run == nil` → SSE snapshot sets `lab.run` → experiment view
  on the same `lab` instance.
- Piece 2 removes the class: server-owned `view` + single snapshot
  render path, no per-screen instance to bind wrong.
- Follow-up (same night): end_run "worked" server-side (run null,
  archived) but the app kept the dead card and "That card is gone"
  repeated. Cause: `_finish_locked`/`_begin_locked` rebuilt the
  record with version 0, so the end snapshot (v2) was OLDER than the
  app's v3 and the stale guard dropped it. Fix: version carries over
  (`prev_version`, bumped by `_commit`), enforced by RUN10. Same
  regression had been hiding start-run updates too.

### Piece 2 gates

- Unit + probe suites migrated (grammar/action shapes, card paths,
  view transitions, no-model-card-approval). DONE, all green.
- fake_app smoke in Action mode (fake_app speaks Actions, renders
  server view). DONE (--fake and --fake-end strict ALL PASS).
- Live re-runs: tool_choice 10/10, smoke 9/10 under 3s (worst turn
  varies run to run — model jitter; over-budget turns are 2-round
  read questions, inside their 4s budget), latency (questions <=4s,
  commands <=3s PASS), instruction 10/10 + smalltalk 3/3. DONE.
- iPad check, then commit. PENDING.

### Piece 2 deviations (as built)

- Transport is `{action: {id, args}}`, but the internal call shape
  stays `{name, args}` (id maps to name at the /input boundary);
  one handler either way, no test churn for a key rename.
- `manage_timers` split into `cancel_timer` / `extend_timer`
  (the perform wrapper already owns the `action` field); listing is
  a `read_record` timers read.
- `answer` tool deleted: round-2 answers are raw model text.
  Round-2 text beside nav-only calls is prepended (summary first);
  beside writes it stays ignored (write wins).
- `find_free_wells` folded into `read_record` kind `free_wells`
  (filter=near, limit=count).
- `/confirm` and `/orchestrate` aliases kept (evals + old builds);
  deletion moves to piece 3.
- Token win: 20 model tools -> 3. Observed in_tok ~1950-2000
  on bench turns (was ~2600+).

## Piece 3: unified registry + safe iteration

### Registry

- `shared/registry.json`: per screen,
  `actions: [{id, args schema, risk: read|nav|write_low|write_high,
  examples}]`. Single source; generates:
  - `perform` action enum + per-action descriptions (screen map
    included),
  - "Try saying…" hints,
  - exact-phrase grammar (example utterances matched verbatim),
  - the app's button→Action catalog (bundled, read at launch).
- Parametric grammar (well refs, numbers, timers) stays hand-written:
  no grammar compiler. `screens.json`, the hardcoded screen map, and
  the app's hints fallback become generated artifacts (or are
  deleted where piece 2 already removed them).
- Risk is declarative: the handler keeps piece-2 logic, and a
  contract test asserts registry risk matches handler behavior for
  every action (read/nav free, write_low evidence tiers, write_high
  always cards).

### Bounded multi-round reads

- Rounds continue (max 4) only while a round emits read/nav actions
  alone. The first write action stops iteration and takes the
  existing evidence-strength path. Whole-turn wall-clock cap 6s from
  turn start; on cap, answer from gathered reads and log it. Round
  budget and clock injectable for tests.
- Scripted tests: 3-read chain then answer; write-on-round-2 stops;
  cap hit answers partial. No infinite loops by construction
  (round counter + deadline both enforced).

### Piece 3 gates

- Registry contract tests (risk parity, generation outputs pinned).
- Round/cap tests. Live latency re-run (questions ≤4s, commands ≤3s
  must still hold; multi-round is opt-in per turn by the model, the
  cap is worst-case only). fake_app smoke. iPad check, then commit.

## Standing rules (carried over)

- Bug-judged behavior changes need a question first (AGENTS.md).
- No commit/push without an explicit ask.
- Private mode and "Hey Meta" voice invocation are out of scope;
  add no related plist/code/settings.
