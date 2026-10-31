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

- Applies only when the reply would be raw model text with no
  surviving tool calls (no writes or reads this turn), so any claimed
  outcome is ungrounded by construction.
- The text is discarded only when it has BOTH a completed-outcome
  verb (`_OUTCOME_VERB` in grammar.py: marked, logged, saved, started,
  done, dropped, moved, set, …) AND a named record target: well refs
  (`[A-H]\d+`, ranges), `timer`, `step N`/`next step`, `note`/`memo`
  (`ungrounded_targets`). Bare mentions without an outcome verb pass
  ("C7 looks cloudy"); state words (running, left) are not outcome
  verbs.
- Replacement: if the user's utterance mentioned that target too →
  didnt_act naming the target ("I didn't log C7 — want me to?") and
  set asked so the follow-up grounds the intent; otherwise neutral
  ack "Got it!" (new pip_voice key `ack`). To talk about record
  state, the model must call read_record first.
- The v2 `?` exemption stays: questions claim nothing
  ("Should I drop C7?" passes; "C7 is dropped." is discarded).
- Expected fallout: only smalltalk that claims an outcome on a target
  ("Nice, C2–C8 are set") becomes "Got it!"; plain smalltalk and bare
  well mentions pass unchanged.

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

## Voice sessions (slice A done, pending iPad check)

- One machine, one owner: VoiceSession (@MainActor, app-local audio
  state, not server view) — ready/listening/transcribing/thinking/
  speaking/searching. Views observe; transitions only via methods.
- One engine, one tap (MicEngine); wake/command/stream share sinks.
  Recorder rebased onto it unchanged API-wise; session config still
  lives in Recorder; HFP path untouched.
- Wake word on-device only (requiresOnDeviceRecognition, "Hey Pip"
  contextual string); ambient audio never reaches the server.
  Unsupported -> Setup shows the reason, no silent fallback.
- Wake chimes, then a separate command (no one-shot utterances);
  auto-stops on 1s silence, gives up quietly without speech.
- Triggers abstracted (wakeWord/manual, later glassesTap); all Pip
  speech (replies, timers, import) flows through speak() so the orb
  never lies. Orb + reply line only, no overlay (orb styling is a
  placeholder until Eunbi's design lands).
- No Setup page: Splash -> Glasses sync (while a permission is missing,
  or the glasses aren't ready and weren't skipped) -> main. Each app
  launch opens Home: the first snapshot navigates the saved view there
  once (an active run stays one tap / "back" away). Opening the active
  run (card tap, "open the latest run") goes to the live bench (`main`);
  only finished runs open the read-only `run_view` snapshot. Voice
  "open setup" / "glasses sync" open the same screen (`setup` is now an
  alias of `sync` in screens.json; API 4).
- Glasses sync rows (superseded below): Bluetooth, Local network, Meta AI
  app, Glasses, Glasses audio, Camera. A row tap is its one action; permissions are
  asked only there (denied opens Settings). Glasses shows the device name
  and battery when connected. Glasses audio asks the mic (iOS needs it for
  the glasses' HFP mic too; speech recognition for "Hey Pip" is asked in
  the same tap) and then follows the glasses route. Camera: DAT reports a
  never-requested permission as denied, so denied means "Tap to allow"
  (the Meta AI grant flow). DAT is configured only when Bluetooth is
  allowed (configure starts Bluetooth); before that its rows wait on
  Bluetooth. The server is reached only after the Local network tap made
  its first request (no status API: `pip.serverAllowed`), and that row
  shows the connection. Gate permissions: Bluetooth, local network, mic.
  Skip covers the glasses rows only. Developer diagnostics are a DEBUG
  sheet on long-press of the glasses icon.
- The top voice line says what blocks talking ("Allow mic to talk to
  Pip") and opens Glasses sync on tap.
- iPad incident fixes: concurrent permission requests join one prompt; restart backoff (5/10s) + failure
  latch instead of churning; press/release pairing (pressGen) so a
  release before spin-up aborts instead of stranding listening; mic
  permission owned by beginManual.
- Voice outage fixes (device log): the wake converter streams with
  .noDataNow (.endOfStream fed it one buffer, ever); capture attaches to
  the shared engine before wake detaches (tearing it down made every
  press re-switch to glasses HFP and record 0 frames); 1024-frame tap;
  WAV duration walks RIFF chunks (AVAudioFile pads the header); every
  non-ready state times out to ready (listening at maxCapture, speaking
  at text length/12 chars/s + 5s, else 45s). MicEngine owns the session:
  it deactivates only with no sink, no prepare in flight and no TTS; an
  interruption end or config change under live sinks fires onLost, and
  VoiceSession restarts its consumers (the one restart path).
- Voice admission is one table (`VoiceSession.admission`, state x trigger
  -> accept / ignore / bargeIn): ready accepts both unless a capture is
  already starting; speaking lets only the mic barge in. A wake is consumed
  only when accepted (`onWake` returns it); a rejected one re-arms on a
  fresh task. Every capture exit releases its trigger (the wake path leaked
  it, so the 2nd "Hey Pip" was ignored as "starting").

- Per-step done (API 5): `record.done` is a log of {step (1-based), well,
  source, ts}, one per (step, well), sorted by step then well; excluded
  stays per well across steps. mark_done{wells, step?} lands on the current
  step when step is blank and checks off-plan against that step. Plate
  (design `.well`): current-step targets are done (filled) or now (blue
  ring); wells done only in other steps are dimmed; others are planned
  (group ring). CSV hand-off rows carry step/source/ts. API 4 bare-well
  records migrate on load as step 1 (the step was never recorded).
- Model action gate (`_expand_perform`, one place): blank placeholders
  (null, "", "null", "none") are dropped, and an action missing a
  REQUIRED_ARGS entry becomes ask() ("Which screen?") instead of a failure
  line. navigate.screen is an enum of the screen registry ids.

- Persona (one place): `shared/pip_voice.json` `persona` = agent_name
  "Milo", wake_phrase "Hey Milo", voice "Jamie" premium (+ language, rate,
  pitch). Server prompt and app UI/TTS read only this (code identifiers
  keep "Pip"). The app bundles the same file. No voice/rate picker.
  TTS voice: name+quality -> same name enhanced -> same-language premium
  -> system default, logged as `[voice] tts voice` and shown in DEBUG
  diagnostics. Wake: `WakeMatcher` compares by sound (caller word + 1-2
  words whose sound key is within one edit of the name); the on-device
  recognizer gets a custom language model for the phrase when supported.
  DEBUG "Measure ×10" logs the hit rate and every heard sentence.

- Voice edge (design `.edge`): one `EdgeGlow` overlay above the screen
  container, touch-through, full-bleed; the soft conic band only (blur
  20, band 30: thicker than the design's 16/18, and the crisp 4 px line
  dropped, both by iPad review),
  2.6 s spin, .5 s fade, all 1376-scaled.
  `Theme.edgeStyle` maps VoiceSession state only: listening (band and
  brightness follow the smoothed tap RMS from MicEngine's one tap),
  transcribing/thinking/searching (half speed, dimmer), speaking (voice
  palette, pulse per TTS word), ready (not drawn). Reduce Motion: no spin.
- Light-screen header: one `TopHeader` (mode "Full" small top-left; orb 52
  + line 22 px centered at top 44 from the screen top, not the safe area)
  for every light screen; none on Splash or Sync. Text is 22 (design .t24)
  instead of the design's 26 by iPad review. The orb glows on its own; the
  wandering halo is gone. The orb keeps a slot for its largest state, the
  line wraps to two lines then truncates; DEBUG `-uiPreview header` stacks
  every state and line length.
- Screen registry owns when to use each screen (`use`/`not`, needs_target)
  and its spoken result line; the perform tool's screen guide and every
  navigation reply derive from it. importer is a registry sheet (ui
  import, no view). Replies are store results only: the model's own line is
  spoken only when the store said nothing, mixed results are separate
  sentences, and text is cut only at sentence ends.
- The app reads only `screens` from the bundled screens.json (hint
  commands). No built-in screen list: an unreadable registry shows a red
  "Screen registry unreadable" banner (it silently fell back before,
  because the whole file was decoded as screen lists and `reads` broke it).

## Live conversation (Meta realtime ASR, DIARIZATION) — fixed design

Docs: dev.meta.ai/docs/speech-to-text, api-reference/voice/{realtime,schemas},
pricing-rate-limits. App ↔ our server (WS) ↔ wss://api.meta.ai/v1/asr/realtime;
the key stays on the server (handshake `authorization.accessToken`).

- Session: "Hey Milo" (on-device) opens one stream (PCM_16KHZ, mode
  DIARIZATION, languageBias English, keywords = wake phrase, agent name,
  current protocol reagents/conditions, well examples). On-device wake
  detection pauses while a session is open.
- Owner = the session's first `speaker` label; `speaker` has no turnId,
  so it labels the turn of the latest `speechStart`. Owner `speechComplete`
  text goes into the one orchestrator path. Other-speaker turns: no reply,
  sound or window extension; the top line shows "Another voice — ignored"
  (pip_voice.json) in gray for ~1.5 s; log `[voice] ignored speaker=B`.
- not_addressed: a perform action (no args) chosen in the same call;
  left out of the candidates for the first turn after the wake phrase;
  grammar-path commands run as before.
- Window: 60 s (pip_voice.json) after the owner's last turn; other
  speakers and not_addressed don't extend it; Milo speaking doesn't count
  (the clock restarts when TTS ends). Card yes/no works inside it without
  the wake phrase. Expiry: endStream, read to 1000, edge fades ~1 s with a
  small sound, silent; only the day's first close says "Call me anytime."
- Explicit end (server grammar, `end_session` in the response): "thanks /
  that's all" -> short reply then close; "stop / never mind" -> close now.
- While Milo speaks the app sends PCM silence instead of the mic (no voice
  barge-in; the mic button still interrupts). Silence is processed audio.
- Max 60 min (docs): at 55 min close at the next turn boundary (no hand-off).
- Partials go to the app as WS messages (not snapshots) -> top line.
- Private: the iPad transcribes (SFSpeechRecognizer with
  requiresOnDeviceRecognition; contextualStrings = wells A1–H12, this
  screen's buttons, protocol short titles) and sends `heard` text. No audio
  leaves, Meta realtime never opens, and there are no speaker labels: every
  voice counts as the owner's, so nothing is ignored. A turn ends after
  0.9 s without new words. The mic button transcribes its recording the
  same way. Changing mode closes the session (`mode`) and the app reopens
  it the new way; audio to Meta stops the moment the mode changes.
- Edge "session idle": dim, slow while open and quiet; listening when the
  owner talks; gone when the session ends. Top line a quiet "Listening…".
- Fallback: stream failure or drop -> per-utterance /voice, shown on screen.
- Private mode (no Meta streaming) is out of scope. API version 7 (new WS
  messages, end_session / not_addressed).
- Plan first, run when confirmed (direction; decided after real recordings):
  the server detects end of speech from ~0.8 s silence (setting) and starts
  the orchestrator on the partial, planning tool calls (reads allowed)
  without `_apply_call`. On speechComplete + speaker: owner -> run the plan;
  other speaker -> drop it (record untouched); a final text that differs
  after normalization -> plan again. Today plan and run are interleaved
  (round 1 writes apply before the lookup round; session memory/tone state
  change during the model call), so this needs a pure planning step.
- Checkpoint 1 (built): app `LiveLink` opens ws /voice/live on an accepted
  wake (fallback: the per-utterance capture), streams 80 ms PCM16 16 kHz
  frames from one more MicEngine sink (16 kHz as is, else converted), and
  sends silence while the agent speaks. Server `LiveConversation` keeps the
  owner (first label), strips a leading wake phrase, runs owner turns through
  `_handle_text`, reports others as `ignored`; partials as they come.
  Messages: shared/schemas/live.schema.json. API 7.
- Checkpoint 2 (built): window clock restarts on owner turns and when the
  agent stops talking (app `tts` messages pause it); closes only at a turn
  boundary; 55 min (`live.max_s`) closes at the next boundary. "thanks /
  that's all" -> `session_thanks` reply + end_session; "stop / never mind"
  -> end_session with no reply (grammar `match_session_end`; a pending
  card's answer wins). `closed.why` tells window / max_duration /
  end_session / client / server / error. App: normal close -> 1 s edge fade
  + small sound, the day's first window close says `live.first_close_line`;
  error -> `live.fallback_line` and per-utterance capture for 2 min. Edge
  "session idle" (dim, slow) and a quiet "Listening…" while open and quiet.
- Live fixes: the perform gate drops calls without a name or action (the
  strict schema doesn't constrain model output); if every call dropped,
  the user is asked between the registry's best-matching screens (names
  weigh twice the `use` text, `choice` phrases the options). Reply assembly
  skips None/blank. A live turn that fails says `didnt_catch` and the
  session stays. Session-end grammar normalizes first (punctuation out,
  repeated phrases folded). A turn that is only the wake phrase answers
  `here` without a model call and restarts the window.
- Sessions close only on the window, an explicit end, the max duration, a
  broken stream, or the app going to background. Timeouts recover to
  listening; an engine rebuild re-attaches the live mic; inactive (Control
  Center) keeps the session. Speaking ends on a stalled voice (no word
  boundary for `speakMargin` s), so long replies at any rate are never cut.
  While muted, an audio-queue timer sends 80 ms of silence on its own clock.
- Speech is short (`speak_text`, the one place): at most two sentences; a
  sentence listing 3+ items says the first and "N more on screen"
  (`live.more_on_screen`). Model guide: lists -> count + top one + navigate.
  Registry results never keep an empty slot; screens that need a target
  ask "which one?" when opened without one.
- Labels (server/labels.py, the one place): protocols "Oct 29 · 12:51 AM ·
  <short_title>" / "the <short_title> protocol"; runs "Oct 29 · 2:20 PM ·
  <short_title>" / "the active run", "today's 2:20 PM <short> run",
  "yesterday's <short> run", "the second <short> run today". short_title
  comes from import extraction (plan.schema.json, <= 4 words) or a rule over
  the title. Spoken run/protocol phrases resolve by label facts (active,
  latest, day, time, short title, ordinal); ambiguous -> ask between two.
  Lists, results, reads, run detail, the snapshot run and server logs use
  them; user text never carries ids (test). API 8.
- Header look (final design "BenchTrace · Milo", v26 block): the orb is a
  hollow blue ring with a soft glow (white core + stronger glow while
  hearing; 1.18x while answering). The pill lights (`.pip.live`: white .94,
  blue-tinted shadow, padding 0 30 0 8) while the agent listens, thinks
  and answers; quiet otherwise (and while a live session idles). Status
  words are blue `.lst`: Listening / Thinking / Searching the web, with
  blue dots while listening; a person's words show as gray "You" + quoted
  text; the agent's words are quoted.
- Glasses sync rows: Bluetooth, Local network, Connect Glasses (Meta AI
  link, then the glasses: model · battery), Audio (mic permission, then a
  live level meter once the glasses mic is on the route), Camera.
- UI by iPad review: no typed command field and no hold-to-talk mic button
  at the bottom (voice is wake + live session); all six Glasses sync rows
  are the same white (no .38 dim for pending/failed).
- Piece 1 (server only): server/live_asr.py relay + eval/live_asr_measure.py
  (real-time WAV playback, per-turn latency vs file VAD, label accuracy,
  `--simulate` plan-first estimate with real model time).

## Standing rules (carried over)

- Bug-judged behavior changes need a question first (AGENTS.md).
- No commit/push without an explicit ask.
- Private: voice commands and protocol imports run on Glimmer, our own
  deployment of Meta's open weights (server/private/README.md), and speech is
  transcribed on the iPad. Hand off, photo questions and search summaries stay
  on Spark; search and photo questions ask first (`modes.private.confirm_*`).
  A document too long for Glimmer asks to switch to Full. Which model runs
  what is one setting, `modes` in pip_voice.json, read by the routing and the
  Mode card alike (server/modes.py): never a mode check in code.
  Say it as "not sent to the Meta API, processed on the Meta model we run";
  never "on device".
- "Hey Meta" voice invocation is out of scope; add no related
  plist/code/settings.
