import AudioToolbox
import Foundation
import os
import Observation
import SwiftUI

/// How a voice session opened. Later: .glassesTap (DAT Inputs Select
/// fires the same event; the machine doesn't care which input woke it).
enum VoiceTrigger {
    case wakeWord
    /// Tapping the header orb: opens like the wake word, interrupts speech.
    case orbTap
    case manual

    /// Opens a live session (or one auto-ended utterance), not a held capture.
    var handsFree: Bool { self != .manual }
}

/// Seams for tests; real types conform below.
protocol WakeControlling: AnyObject {
    /// Returns true when the session accepted the wake (listener consumes it).
    var onWake: (() -> Bool)? { get set }
    var onFailed: (() -> Void)? { get set }
    var running: Bool { get }
    var debugDetail: String { get }
    func start() async throws
    func stop()
}

protocol CaptureControlling: AnyObject {
    var peak: Float { get }
    func start() async throws
    func stop() throws -> URL
}

protocol SpeakerControlling: AnyObject {
    func speak(_ text: String?, onDone: @escaping () -> Void)
    func stop()
    /// When TTS last started a word: speech still progressing.
    var lastWordAt: Date { get }
}

extension WakeListener: WakeControlling {
    var debugDetail: String { "\(convertedBuffers) bufs" }
}

extension Recorder: CaptureControlling {
    var peak: Float { currentPeak }
}

extension SpeechBus: SpeakerControlling {}

/// Single owner of voice-input state (Eunbi's Pip states, 1:1).
/// Views observe; all transitions go through these methods.
///
/// ready -> listening -> transcribing -> thinking/searching -> speaking -> ready.
/// Wake-word opens with a chime and auto-stops on silence; the mic button
/// opens manually and stops on release. Session speech and stray speech
/// (timers, import) share speak(), so the orb never lies.
@Observable
@MainActor
final class VoiceSession {
    enum State: String {
        case ready
        case listening
        case transcribing
        case thinking
        case speaking
        case searching
    }

    /// "Hey Pip" detection. Off by support, toggle, or while busy.
    static var wakeEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "pip.wake") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "pip.wake") }
    }

    private(set) var state: State = .ready {
        didSet {
            if oldValue != state {
                Log.voice.info("[voice] state \(oldValue.rawValue, privacy: .public) -> \(self.state.rawValue, privacy: .public) gen=\(self.gen) press=\(self.pressGen)")
                armWatchdog()
            }
        }
    }
    private(set) var listeningSince: Date?
    private(set) var lastHeld: Double = 0
    private(set) var wakeStatus = "Not checked"
    /// Last read wake support (permissions, recognizer); drives the top line.
    private(set) var wakeSupport: WakeListener.Support = .ok
    /// Capture output for the owner to send; cleared when it responds.
    private(set) var pendingAudio: URL?
    /// Live conversation (wake opens it; the server closes it). nil = none.
    private var live: LiveControlling?
    private let liveFactory: (@MainActor () -> LiveControlling)?
    /// Running transcript of the current live turn (top line).
    private(set) var partialText: String?
    /// Brief gray top line (another voice ignored), nil when none.
    private(set) var aside: String?
    private var asideGen = 0
    /// Owner turn result from the live link; the owner view applies it like /voice.
    private(set) var liveTurn: VoiceResponse?
    private(set) var liveTurnGen = 0
    #if DEBUG
    /// Typed command from Diagnostics (voice-down check); the owner sends it.
    private(set) var typed: String?
    private(set) var typedGen = 0
    func type(_ text: String) {
        typed = text
        typedGen += 1
    }
    #endif
    var inSession: Bool { live != nil }
    /// Edge fade for the next disappearance: 1 s when a session closes.
    private(set) var edgeFade: Double = Theme.edgeFade
    /// After a broken stream, wakes use the per-utterance capture until then.
    private var liveBackoffUntil: Date = .distantPast
    private let endChime: () -> Void
    private let closeDay: (Date) -> Bool
    private(set) var notice = ""
    private(set) var noticeGen = 0

    var stateLabel: String {
        switch state {
        case .ready: "Ready"
        case .listening: "Listening…"
        case .transcribing: "Transcribing…"
        case .thinking: "Thinking…"
        case .speaking: "Speaking…"
        case .searching: "Searching…"
        }
    }

    enum Admission: Equatable { case accept, ignore, bargeIn, interrupt }

    /// The one table for state x trigger. `starting`: a capture was
    /// accepted and is spinning up (state still ready, wake still attached).
    ///
    /// | state                         | wake word | orb tap   | manual  |
    /// |-------------------------------|-----------|-----------|---------|
    /// | ready                         | accept    | accept    | accept  |
    /// | ready + starting              | ignore    | ignore    | ignore  |
    /// | listening (session idle too)  | ignore    | ignore    | ignore  |
    /// | transcribing/thinking/searching | ignore  | ignore    | ignore  |
    /// | speaking                      | ignore    | interrupt | bargeIn |
    ///
    /// Wake never barges in: TTS through the speaker would wake itself.
    /// Interrupt only stops the voice (a session keeps listening).
    static func admission(state: State, trigger: VoiceTrigger, starting: Bool) -> Admission {
        switch (state, trigger) {
        case (.ready, _): starting ? .ignore : .accept
        case (.speaking, .manual): .bargeIn
        case (.speaking, .orbTap): .interrupt
        default: .ignore
        }
    }

    /// Mic button enabled where the table admits a press.
    var micLive: Bool {
        Self.admission(state: state, trigger: .manual, starting: false) != .ignore
    }

    private let wake: WakeControlling
    private let capture: CaptureControlling
    private let speaker: SpeakerControlling
    private let chime: () -> Void
    private let supportStub: (() -> WakeListener.Support)?
    private let micRequest: (() async -> Bool)?
    private let silenceTimeout: TimeInterval
    private let minCapture: TimeInterval
    private let maxCapture: TimeInterval
    private let awaitSpeech: TimeInterval
    private let stuckTimeout: TimeInterval
    private let speakMargin: TimeInterval
    private var speakStartedAt = Date.distantPast
    private var gen = 0
    private var pressGen = 0
    private(set) var activeTrigger: VoiceTrigger?
    /// gen that set activeTrigger; only that capture's exit may clear it.
    private var captureGen = 0
    private var activated = false
    private var startingWake = false
    private var wakeBroken = false

    convenience init() {
        self.init(wake: WakeListener(), capture: Recorder(), speaker: SpeechBus.shared,
                  liveFactory: { LiveLink() })
        MicEngine.shared.onLost = { [weak self] in self?.audioLost() }
    }

    /// Shared engine died under us: end a capture as a release (or drop a
    /// wake capture) and restart wake from scratch. A live session is not
    /// closed: its mic sink re-attaches to the rebuilt engine.
    func audioLost() {
        Log.voice.error("[voice] audio lost state=\(self.state.rawValue, privacy: .public) trigger=\(String(describing: self.activeTrigger), privacy: .public) live=\(self.live != nil)")
        if let live {
            Task { await live.restartAudio() }
            return
        }
        if state == .listening {
            if activeTrigger == .manual { endManual() } else { close() }
        }
        wake.stop()
        resumeWake()
    }

    init(wake: WakeControlling,
         capture: CaptureControlling,
         speaker: SpeakerControlling,
         chime: @escaping () -> Void = { AudioServicesPlaySystemSound(1113) },
         supportStub: (() -> WakeListener.Support)? = nil,
         micRequest: (() async -> Bool)? = nil,
         silenceTimeout: TimeInterval = 1.0,
         minCapture: TimeInterval = 0.5,
         maxCapture: TimeInterval = 20,
         awaitSpeech: TimeInterval = 4,
         stuckTimeout: TimeInterval = 45,
         speakMargin: TimeInterval = 5,
         liveFactory: (@MainActor () -> LiveControlling)? = nil,
         endChime: @escaping () -> Void = { AudioServicesPlaySystemSound(1114) },
         closeDay: @escaping (Date) -> Bool = VoiceSession.firstCloseToday) {
        self.wake = wake
        self.capture = capture
        self.speaker = speaker
        self.chime = chime
        self.supportStub = supportStub
        self.micRequest = micRequest
        self.silenceTimeout = silenceTimeout
        self.minCapture = minCapture
        self.maxCapture = maxCapture
        self.awaitSpeech = awaitSpeech
        self.stuckTimeout = stuckTimeout
        self.speakMargin = speakMargin
        self.liveFactory = liveFactory
        self.endChime = endChime
        self.closeDay = closeDay
        self.wake.onWake = { @MainActor [weak self] in self?.open(trigger: .wakeWord) ?? false }
        self.wake.onFailed = { @MainActor [weak self] in
            self?.wakeBroken = true
            self?.emit("Wake-word keeps failing — tap Check in Setup to retry")
            self?.refreshWakeStatus()
        }
    }

    // MARK: - Lifecycle

    /// Starts wake detection when supported + enabled. Call on appear and
    /// foreground; deactivate() on background (mic releases with the engine).
    /// Asks for mic + speech access on first launch (each prompts once).
    func activate() {
        activated = true
        refreshWakeStatus()
        Log.voice.info("[voice] activate support=\(String(describing: self.support()), privacy: .public)")
        // Launch never prompts: undetermined waits for Setup's Hey Pip /
        // Microphone check (resumeWake needs .ok).
        resumeWake()
    }

    /// An iOS system sheet (file picker, photo picker) is up: the app goes
    /// inactive, maybe background, but the user is still here. A live
    /// session stays open until it closes.
    private(set) var systemSheetUp = false
    func systemSheet(_ up: Bool) {
        systemSheetUp = up
        Log.voice.info("[voice] system sheet \(up ? "up" : "down", privacy: .public) inSession=\(self.inSession)")
    }

    func deactivate() {
        if systemSheetUp, live != nil {
            Log.voice.info("[voice] deactivate skipped: a system sheet is up, the session stays")
            return
        }
        Log.voice.info("[voice] deactivate state=\(self.state.rawValue, privacy: .public)")
        activated = false
        if live != nil { endLive("app background") }
        wake.stop()
        gen += 1
        if state == .listening {
            try? capture.stop()
            activeTrigger = nil
            state = .ready
        }
    }

    /// Re-reads the toggle + support (Settings dismiss, Check button).
    /// Clears the failure latch so Check retries a broken listener.
    func refresh() {
        wakeBroken = false
        refreshWakeStatus()
        if state == .ready { resumeWake() } else { wake.stop() }
    }

    #if DEBUG
    /// Scores the next `count` wake utterances ([wake] log) instead of
    /// opening sessions; starts the listener if it is idle.
    func measureWake(count: Int) {
        (wake as? WakeListener)?.measure(count: count)
        refresh()
    }
    #endif

    func refreshWakeStatus() {
        let s = support()
        wakeSupport = s
        var text = WakeListener.describe(s)
        if s == .ok, !Self.wakeEnabled {
            text = "Off — enable “\(Persona.current.wakePhrase)” in Settings"
        } else if s == .ok, wake.running {
            text += " · listening (\(wake.debugDetail))"
        }
        wakeStatus = text
    }

    // MARK: - Triggers

    /// Opens a capture when `admission` accepts (or barges in). Returns
    /// whether it was accepted, so the wake listener consumes only those.
    @discardableResult
    func open(trigger: VoiceTrigger) -> Bool {
        open(trigger: trigger, press: pressGen)
    }

    @discardableResult
    private func open(trigger: VoiceTrigger, press: Int) -> Bool {
        let starting = activeTrigger != nil
        let a = Self.admission(state: state, trigger: trigger, starting: starting)
        Log.voice.info("[voice] open \(String(describing: trigger), privacy: .public) state=\(self.state.rawValue, privacy: .public) starting=\(starting) -> \(String(describing: a), privacy: .public) press=\(press)/\(self.pressGen)")
        switch a {
        case .ignore: return false
        case .bargeIn: speaker.stop()
        case .accept: break
        case .interrupt:
            gen += 1
            speaker.stop()
            live?.muted = false
            state = restState
            resumeWake()
            return true
        }
        gen += 1
        let g = gen
        captureGen = g
        activeTrigger = trigger
        // Wake keeps running until capture is attached (beginCapture): the
        // shared engine stays up, so no route re-switch eats the press.
        if trigger.handsFree { chime() }
        if trigger.handsFree, let make = liveFactory, Date() >= liveBackoffUntil {
            Task { await beginLive(make, trigger: trigger, gen: g, press: press) }
        } else {
            Task { await beginCapture(trigger: trigger, gen: g, press: press) }
        }
        return true
    }

    /// Capture `g` ended (any path): drop its trigger unless a newer open owns it.
    private func release(_ g: Int) {
        if captureGen == g { activeTrigger = nil }
    }

    /// Mic press. Captures the press number synchronously so a release
    /// before the engine spins up (or before permission answers) aborts
    /// instead of stranding a capture.
    func beginManual() {
        pressGen += 1
        let p = pressGen
        Log.voice.info("[voice] button press p=\(p) state=\(self.state.rawValue, privacy: .public) wakeRunning=\(self.wake.running)")
        Task {
            let ok: Bool
            if let micRequest {
                ok = await micRequest()
            } else {
                ok = await Recorder.requestPermission()
            }
            Log.voice.info("[voice] press p=\(p) mic permission=\(ok)")
            guard ok else { emit("Microphone permission needed"); return }
            open(trigger: .manual, press: p)
        }
    }

    /// Mic release. Always bumps the press number first (pairs with a
    /// press that hasn't reached open() yet), then finishes a live
    /// manual capture. Wake captures end themselves on silence.
    func endManual() {
        pressGen += 1
        Log.voice.info("[voice] button release press=\(self.pressGen) state=\(self.state.rawValue, privacy: .public) trigger=\(String(describing: self.activeTrigger), privacy: .public)")
        if activeTrigger == .manual {
            gen += 1
            activeTrigger = nil
            if state == .listening {
                finishCapture(gen: gen, silent: false)
                return
            }
        }
        resumeWake()
    }

    /// Any state -> ready (errors, teardown). Owner already logged if needed.
    func close() {
        if live != nil { endLive("closed") }
        gen += 1
        activeTrigger = nil
        if state == .listening { try? capture.stop() }
        speaker.stop()
        state = .ready
        listeningSince = nil
        resumeWake()
    }

    // MARK: - Pipeline (owner calls these around send/speak)

    /// Owner received the reply: thinking, or searching when Pip searched.
    func didRespond(hasSources: Bool) {
        pendingAudio = nil
        guard state == .transcribing || state == .thinking || state == .ready
            || (live != nil && state == .listening) else { return }
        state = hasSources ? .searching : .thinking
    }

    /// Owner failed the turn (network etc.): back to ready, wake resumes.
    func didFail() {
        gen += 1
        pendingAudio = nil
        activeTrigger = nil
        state = restState
        listeningSince = nil
        resumeWake()
    }

    /// The one way Pip talks (replies, timers, import): speaking while
    /// TTS runs, then ready. Refused mid-listening (no self-feedback).
    func speak(_ text: String?) {
        Log.voice.info("[voice] speak len=\(text?.count ?? -1) state=\(self.state.rawValue, privacy: .public) activated=\(self.activated)")
        guard state != .listening || live != nil else { return }
        guard activated else { state = .ready; return }
        gen += 1
        let g = gen
        wake.stop()
        // The watchdog follows the voice's real progress (word boundaries),
        // so a long reply at any rate never trips it; only a stall does.
        speakStartedAt = Date()
        state = .speaking
        live?.muted = true // the mic would hear the agent: stream silence
        let route = MicEngine.shared
        if route.speechSwitchApplies {
            // a2dp_while_speaking: output moves to A2DP first (the live link
            // keeps streaming silence on its own timer meanwhile)
            Task { @MainActor [weak self] in
                await route.beginSpeech()
                guard let self, g == self.gen else { await route.endSpeech(); return }
                self.say(text, gen: g, switched: true)
            }
        } else {
            say(text, gen: g, switched: false)
        }
    }

    /// Speaks for turn `g`; after it, a switched route returns to HFP before
    /// the mic opens again, so listening shows only once the mic really hears.
    private func say(_ text: String?, gen g: Int, switched: Bool) {
        MicEngine.shared.speechStarted(liveMuted: live.map { $0.muted })
        speaker.speak(text) { @MainActor [weak self] in
            MicEngine.shared.speechFinished()
            guard let self else { return }
            Log.voice.info("[voice] tts done gen=\(g)/\(self.gen) state=\(self.state.rawValue, privacy: .public)")
            let reopen: @MainActor () -> Void = { [weak self] in
                // only the latest utterance reopens the mic: an earlier one cut
                // off by a newer speak() must not unmute it mid-sentence
                guard let self, g == self.gen, self.state == .speaking else { return }
                self.live?.muted = false
                self.state = self.restState
                self.resumeWake()
            }
            let done: @MainActor () -> Void = { [weak self] in
                reopen()
                // a session that ended on this reply closes only now (fade, chime, wake)
                if let close = self?.afterSpeech { self?.afterSpeech = nil; close() }
            }
            if switched {
                Task { @MainActor in
                    await MicEngine.shared.endSpeech()
                    done()
                }
            } else {
                done()
            }
        }
    }

    /// Where a turn returns to: listening while a live session is open.
    /// Between turns: listening in a session, else ready; while the server
    /// is still on a turn (thinking / searching), that.
    private var restState: State { serverBusy ?? (live != nil ? .listening : .ready) }

    /// The server's stage for the current turn (nil when none is running).
    private(set) var stage: TurnStage?
    /// The session ended on a reply ("Thanks Milo"): close after it's spoken.
    private var closeAfterReply = false
    private var afterSpeech: (() -> Void)?
    private var serverBusy: State?

    /// The server says where the turn is; the voice state follows it (the
    /// app never guesses). A stage's `say` line is spoken once.
    func applyStage(_ s: TurnStage) {
        switch s.stage {
        case "heard", "thinking", "reading":
            stage = s
            serverBusy = .thinking
            partialText = nil
        case "searching", "summarizing":
            stage = s.stage == "summarizing" ? TurnStage(stage: s.stage, query: stage?.query) : s
            serverBusy = .searching
        default:  // done, failed: the reply (or its absence) takes it from here
            stage = nil
            serverBusy = nil
            if state == .thinking || state == .searching { state = restState }
            return
        }
        if state != .speaking, let busy = serverBusy { state = busy }
        if let line = s.say, !line.isEmpty { speak(line) }
    }

    // MARK: - Live conversation

    /// Wake accepted: open the live link (the wake listener stays on the
    /// shared engine until the link's mic sink is attached). Falls back to
    /// the per-utterance capture when the link can't start.
    private func beginLive(_ make: @MainActor () -> LiveControlling, trigger: VoiceTrigger,
                           gen g: Int, press p: Int) async {
        let link = make()
        link.onEvent = { [weak self, weak link] ev in
            guard let self, let link, self.live === link else { return }
            self.liveEvent(ev)
        }
        live = link
        edgeFade = Theme.edgeFade
        do {
            try await link.start(session: "bench")
        } catch {
            Log.voice.error("[voice] live start failed: \(error.localizedDescription, privacy: .public) -> per-utterance capture")
            live = nil
            guard g == gen else { return }
            await beginCapture(trigger: trigger, gen: g, press: p)
            return
        }
        guard g == gen, live === link else { link.end(); return }
        release(g)
        wake.stop()
        state = .listening
        listeningSince = Date()
        Log.voice.info("[voice] live session open")
    }

    private func liveEvent(_ ev: LiveEvent) {
        switch ev {
        case .partial(let text):
            partialText = text
            if state == .listening { state = .transcribing }
        case .ignored(let line):
            partialText = nil
            if state == .transcribing { state = .listening }
            showAside(line)
        case .turn(let r, let endSession):
            partialText = nil
            closeAfterReply = endSession && !(r.speak ?? r.reply).isEmpty
            liveTurn = r
            liveTurnGen += 1
        case .stage(let s):
            applyStage(s)
        case .closed(let why):
            endLive(why)
        }
    }

    private func showAside(_ line: String) {
        asideGen += 1
        let g = asideGen
        aside = line
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(1.5))
            guard let self, self.asideGen == g else { return }
            self.aside = nil
        }
    }

    /// Session over: back to ready and wake detection resumes (a reply still
    /// speaking finishes first). A normal close fades the edge over 1 s with a
    /// small sound; the day's first window close also says the close line. A
    /// broken stream shows the fallback line and wakes use the per-utterance
    /// capture for a while.
    private func endLive(_ why: String) {
        guard let link = live else { return }
        Log.voice.info("[voice] live close reason=\(why, privacy: .public)")
        live = nil
        link.onEvent = nil
        link.end()
        partialText = nil
        edgeFade = Theme.edgeSessionFade
        // "Thanks Milo": the reply is said first; the edge stays until it ends
        let waitForReply = why == "end_session" && (closeAfterReply || state == .speaking)
        closeAfterReply = false
        if !waitForReply, state == .listening || state == .transcribing {
            state = .ready
            listeningSince = nil
        }
        // the close line is the day's first close; a thank-you reply counts as it
        let firstToday = why != "error" && why != "client" && closeDay(Date())
        let close: () -> Void = { [weak self] in
            guard let self else { return }
            switch why {
            case "error":
                self.liveBackoffUntil = Date().addingTimeInterval(120)
                self.showAside(Persona.live.fallbackLine)
            case "client":
                break // the app left (background): nothing to announce
            default:
                self.endChime()
                if why != "end_session", firstToday, !Persona.live.firstCloseLine.isEmpty {
                    self.speak(Persona.live.firstCloseLine)
                }
            }
            self.resumeWake()
        }
        if waitForReply {
            afterSpeech = close
            // the reply never got spoken (TTS off, failed): close anyway
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(20))
                if let f = self?.afterSpeech { self?.afterSpeech = nil; f() }
            }
        } else {
            close()
        }
    }

    /// True the first time it's asked on a calendar day (then remembered).
    static func firstCloseToday(_ now: Date) -> Bool {
        let day = now.formatted(.iso8601.year().month().day())
        let key = "pip.liveFirstCloseDay"
        guard UserDefaults.standard.string(forKey: key) != day else { return false }
        UserDefaults.standard.set(day, forKey: key)
        return true
    }

    // MARK: - Private

    private func beginCapture(trigger: VoiceTrigger, gen g: Int, press p: Int) async {
        guard p == pressGen else {
            // Released before we even started (permission was slow).
            Log.voice.info("[voice] capture skipped: released before start")
            release(g)
            resumeWake()
            return
        }
        do {
            try await capture.start()
            Log.voice.info("[voice] capture started gen=\(g)/\(self.gen) press=\(p)/\(self.pressGen)")
        } catch {
            Log.voice.error("[voice] capture start failed: \(error.localizedDescription, privacy: .public)")
            release(g)
            guard g == gen else { return }
            emit("Recording failed: \(error.localizedDescription)")
            state = .ready
            wake.stop() // fresh restart (a fired listener won't fire again)
            resumeWake()
            return
        }
        guard g == gen else {
            try? capture.stop()
            release(g)
            // An accepted wake was consumed: restart it fresh (resume checks state).
            if trigger == .wakeWord { wake.stop(); resumeWake() }
            return
        }
        guard p == pressGen else {
            // Released before the engine spun up: unwind quietly.
            try? capture.stop()
            release(g)
            resumeWake()
            return
        }
        wake.stop()
        state = .listening
        listeningSince = Date()
        if trigger.handsFree {
            await runAutoCapture(gen: g)
        }
    }

    /// Wake path: stops on silence, caps length, gives up quietly when the
    /// owner never speaks (a stray detection, not a turn).
    private func runAutoCapture(gen g: Int) async {
        var gate = SilenceGate(threshold: 0.03, silenceTimeout: silenceTimeout,
                               minDuration: minCapture, start: Date())
        let deadline = Date().addingTimeInterval(maxCapture)
        let awaitDeadline = Date().addingTimeInterval(awaitSpeech)
        while g == gen, state == .listening {
            try? await Task.sleep(for: .milliseconds(100))
            guard g == gen, state == .listening else { return }
            let now = Date()
            if gate.feed(rms: capture.peak, now: now) { break }
            if !gate.heardSpeech, now >= awaitDeadline {
                gen += 1
                try? capture.stop()
                release(g)
                state = .ready
                listeningSince = nil
                resumeWake()
                return
            }
            if now >= deadline { break }
        }
        guard g == gen, state == .listening else { return }
        finishCapture(gen: g, silent: !gate.heardSpeech)
    }

    private func finishCapture(gen g: Int, silent: Bool) {
        // Wake capture ends here (its trigger leaked before: the next
        // "Hey Pip" was ignored as starting). Manual: endManual cleared it.
        release(g)
        var url: URL?
        do {
            url = try capture.stop()
        } catch {
            Log.voice.error("[voice] capture stop failed: \(error.localizedDescription, privacy: .public)")
            if !silent { emit("Couldn't save the recording") }
        }
        Log.voice.info("[voice] capture finished silent=\(silent) held=\(self.listeningSince.map { Date().timeIntervalSince($0) } ?? -1) file=\(url != nil) gen=\(g)/\(self.gen)")
        if let t0 = listeningSince { lastHeld = Date().timeIntervalSince(t0) }
        listeningSince = nil
        guard g == gen else { return }
        guard !silent, let url else {
            state = .ready
            resumeWake()
            return
        }
        state = .transcribing
        pendingAudio = url
    }

    /// No state is terminal: if a state outlives its limit with nothing
    /// moving (same gen), go back to ready. Listening is capped at
    /// maxCapture (a held button finishes as a release); the rest at stuckTimeout.
    /// No state is terminal. Speaking ends when the voice stalls (no word
    /// boundary for `speakMargin` s); listening caps at maxCapture; others
    /// at stuckTimeout. In a live session a timeout recovers to listening and
    /// never closes the session (only the window, an explicit end, the max
    /// duration or a broken stream do).
    private func armWatchdog() {
        guard state != .ready else { return }
        let s = state, g = gen
        Task { [weak self] in
            if s == .speaking {
                while true {
                    try? await Task.sleep(for: .seconds(0.5))
                    guard let self, self.state == s, self.gen == g else { return }
                    let last = max(self.speakStartedAt, self.speaker.lastWordAt)
                    if Date().timeIntervalSince(last) >= self.speakMargin { break }
                }
            } else {
                guard let limit = self?.watchLimit(s) else { return }
                try? await Task.sleep(for: .seconds(limit))
            }
            guard let self, self.state == s, self.gen == g else { return }
            Log.voice.error("[voice] watchdog: \(s.rawValue, privacy: .public) stuck -> \(self.live != nil ? "listening (session kept)" : "ready", privacy: .public)")
            if live != nil {
                if s == .speaking { speaker.stop() }
                live?.muted = false
                partialText = nil
                gen += 1
                state = .listening
            } else if s == .listening, self.activeTrigger == .manual {
                self.endManual()
            } else {
                self.close()
            }
        }
    }

    private func watchLimit(_ s: State) -> TimeInterval {
        switch s {
        case .listening: live != nil ? 600 : maxCapture
        default: stuckTimeout
        }
    }

    private func support() -> WakeListener.Support {
        supportStub?() ?? WakeListener.support()
    }

    private func resumeWake() {
        guard activated, Self.wakeEnabled, state == .ready, live == nil, !wakeBroken,
              support() == .ok, !wake.running, !startingWake else {
            Log.wake.info("[wake] resume skipped activated=\(self.activated) enabled=\(Self.wakeEnabled) state=\(self.state.rawValue, privacy: .public) broken=\(self.wakeBroken) support=\(String(describing: self.support()), privacy: .public) running=\(self.wake.running) starting=\(self.startingWake)")
            return
        }
        startingWake = true
        let g = gen
        Log.wake.info("[wake] resume start gen=\(g)")
        Task {
            defer { startingWake = false }
            do {
                try await wake.start()
                Log.wake.info("[wake] started gen=\(g)/\(self.gen)")
                Boot.mark("wake listener ready")
            } catch {
                Log.wake.error("[wake] start failed: \(error.localizedDescription, privacy: .public)")
                emit("Wake-word failed: \(error.localizedDescription)")
            }
            guard g == gen else {
                Log.wake.info("[wake] stale start (gen moved) -> stop")
                wake.stop(); return
            }
            refreshWakeStatus()
        }
    }

    private func emit(_ text: String) {
        Log.voice.info("[voice] notice: \(text, privacy: .public)")
        notice = text
        noticeGen += 1
    }
}

/// Pure silence detector: speech must start, then go quiet past the
/// timeout, with a minimum capture so a cough isn't a command.
struct SilenceGate {
    let threshold: Float
    let silenceTimeout: TimeInterval
    let minDuration: TimeInterval
    let start: Date
    private(set) var heardSpeech = false
    private var lastSpeech: Date?

    init(threshold: Float, silenceTimeout: TimeInterval, minDuration: TimeInterval, start: Date) {
        self.threshold = threshold
        self.silenceTimeout = silenceTimeout
        self.minDuration = minDuration
        self.start = start
    }

    /// - Returns: true when capture should stop.
    mutating func feed(rms: Float, now: Date) -> Bool {
        if rms >= threshold {
            heardSpeech = true
            lastSpeech = now
            return false
        }
        guard heardSpeech, let last = lastSpeech else { return false }
        return now.timeIntervalSince(last) >= silenceTimeout
            && now.timeIntervalSince(start) >= minDuration
    }
}

// MARK: - Top header (design light-screen `.top` + `.pip`)

/// The one header every light screen shows: mode ("Full") small at top
/// left, orb + Pip line centered at the top as one group (design `.pip`:
/// top 44, row 64, gap 16, orb 52; 1376-canvas scaled). Reads VoiceSession
/// state only. Dark screens (Splash, Sync) have no header.
struct TopHeader: View {
    var voice: VoiceSession
    var speakText: String = ""
    var partialText: String? = nil
    /// Running timers for the header chip (top right).
    var timers: [TimerEntry] = []
    /// Tap while talking is blocked by a permission: opens the sync screen.
    var onBlockedTap: () -> Void = {}

    var body: some View {
        let line = Theme.pipText(for: voice.state, speakText: speakText,
                                 partialText: partialText, wake: voice.wakeSupport,
                                 wakeOn: VoiceSession.wakeEnabled, stage: voice.stage)
        let idle = voice.inSession && voice.state == .listening
        TopHeaderView(style: Theme.orbStyle(for: voice.state),
                      text: voice.aside ?? (idle ? Theme.sessionIdleLine : line.text),
                      icon: voice.aside == nil && !idle ? line.icon : nil,
                      dots: voice.aside == nil && !idle && line.dots,
                      you: voice.aside == nil && line.you,
                      status: voice.aside == nil && !idle && line.status,
                      live: voice.aside == nil && !idle && Theme.pipLive(voice.state),
                      hearing: voice.state == .transcribing || (voice.state == .listening && !idle),
                      muted: voice.aside != nil || idle,
                      blocked: voice.state == .ready
                          && Theme.talkBlocker(wake: voice.wakeSupport, wakeOn: true) != nil,
                      onBlockedTap: onBlockedTap,
                      onOrbTap: { voice.open(trigger: .orbTap) },
                      timers: timers)
    }
}

/// Header layout only (plain values, so DEBUG previews can show any line).
/// The orb keeps a slot as wide as its largest state so growing never
/// pushes or covers the text; the line wraps to two lines, then truncates,
/// and the group stays centered.
struct TopHeaderView: View {
    var style: Theme.OrbStyle
    var text: String
    /// SF Symbol before the line (searching: the magnifier).
    var icon: String? = nil
    var dots: Bool
    /// Someone is talking: "You" in gray before the quoted words.
    var you = false
    /// The line is a status word (blue, design `.lst`); its dots go blue too.
    var status = false
    /// Listening / thinking / answering: the group sits on the lit pill.
    var live = false
    /// The mic is taking speech: the orb brightens (quiet otherwise).
    var hearing = false
    /// Gray line (another voice ignored).
    var muted = false
    var blocked = false
    var onBlockedTap: () -> Void = {}
    /// Orb tap: talk (VoiceSession.admission decides what it does).
    var onOrbTap: () -> Void = {}
    var timers: [TimerEntry] = []
    @State private var width: CGFloat = 0

    var body: some View {
        let px = { (v: CGFloat) in Theme.px(v, in: width) }
        ZStack(alignment: .top) {
            HStack(spacing: px(12)) {
                Image(systemName: Theme.modeIcon).font(.system(size: px(22)))
                Text(Theme.modeLabel).font(Theme.ui(px(22))).tracking(-0.012 * px(22))
            }
            .foregroundStyle(Theme.mutSay)
            .frame(height: px(Theme.Layout.headerRow))
            .padding(.leading, px(Theme.Layout.side))
            .frame(maxWidth: .infinity, alignment: .leading)
            TimerChip(timers: timers, size: px(22))
                .frame(height: px(Theme.Layout.headerRow))
                .padding(.trailing, px(Theme.Layout.side))
                .frame(maxWidth: .infinity, alignment: .trailing)
            HStack(spacing: px(16)) {
                OrbDisc(style: style, diameter: px(52), bright: hearing)
                    .frame(width: px(52) * Theme.orbMaxScale)
                    .contentShape(Circle())
                    .onTapGesture { blocked ? onBlockedTap() : onOrbTap() }
                    .accessibilityLabel("Talk to \(Persona.current.agentName)")
                    .accessibilityAddTraits(.isButton)
                if let icon {
                    Image(systemName: icon).font(.system(size: px(20), weight: .semibold))
                        .foregroundStyle(status ? Theme.blue : .primary)
                }
                // 22 px (design .t24) by review; the design .pip is 26
                WordReveal(text: text, dots: dots, size: px(22), lines: 2,
                           color: muted ? Theme.mutT : (status ? Theme.blue : .primary),
                           prefix: you ? "You" : nil, weight: status ? .medium : .regular,
                           dotColor: status ? Theme.blue : Theme.mutT)
            }
            .frame(minHeight: px(Theme.Layout.headerRow))
            // design `.pip.live`: rgba(255,255,255,.94), padding 0 30 0 8,
            // shadow 0 1 2 rgba(0,0,0,.05) + 0 12 34 -14 rgba(0,113,227,.45)
            .padding(.leading, live ? px(8) : 0)
            .padding(.trailing, live ? px(30) : 0)
            .background {
                if live {
                    Capsule().fill(.white.opacity(0.94))
                        .shadow(color: .black.opacity(0.05), radius: px(1), y: px(1))
                        .shadow(color: Theme.blue.opacity(0.45), radius: px(17), y: px(12))
                        .transition(.opacity)
                }
            }
            .animation(Theme.ease(duration: 0.35), value: live)
            // keep clear of the mode label on both sides so the group stays centered
            .frame(maxWidth: max(0, width - 2 * px(64 + 180)))
            .contentShape(Rectangle())
            .onTapGesture { if blocked { onBlockedTap() } }
            .accessibilityAddTraits(blocked ? .isButton : [])
            .frame(maxWidth: .infinity)
        }
        .padding(.top, px(Theme.Layout.headerTop)) // from the screen top, like the design canvas
        .padding(.bottom, px(Theme.Layout.headerGap))
        .ignoresSafeArea(edges: .top)
        .onGeometryChange(for: CGFloat.self, of: { $0.size.width }) { width = $0 }
    }
}

/// Design orb (`.orb`, `diameter` = core size): radial gradient core with
/// a tight glow, breathe, expanding ring. No wandering halo (iPad review).
struct OrbDisc: View {
    var style: Theme.OrbStyle
    var diameter: CGFloat = 40
    /// Bright white core and stronger glow while hearing someone.
    var bright = false
    @State private var on = false

    var body: some View {
        ZStack {
            Circle()
                // design polish: the header orb is a hollow blue ring
                .fill(RadialGradient(
                    gradient: Gradient(stops: Theme.orbSpeakingStops),
                    center: .init(x: 0.5, y: 0.5),
                    startRadius: 0, endRadius: diameter / 2))
                .overlay {
                    // hearing someone: the ring fills with a bright white core
                    Circle()
                        .fill(RadialGradient(colors: [.white, .white.opacity(0.6), .clear],
                                             center: .center, startRadius: 0, endRadius: diameter * 0.42))
                        .opacity(bright ? 1 : 0)
                        .animation(Theme.ease(duration: 0.35), value: bright)
                }
                .frame(width: diameter, height: diameter)
                .blur(radius: diameter * 0.075)
                // quiet: a soft glow; hearing: a stronger one
                .shadow(color: glow.opacity(bright ? 0.9 : 0.45), radius: diameter * (bright ? 0.3 : 0.16))
                .scaleEffect(breathe * style.scale * sweepScale)
                .offset(x: sweepShift)
                .opacity(breatheOpacity)
                .animation(breatheAnim, value: on)
                .animation(sweeping ? Theme.Motion.ease(Theme.sweep / 2).repeatForever(autoreverses: true) : nil,
                           value: on)
                .animation(Theme.spring(), value: style.scale)
            if style.ring != nil {
                Circle()
                    .stroke(Theme.orbRing, lineWidth: 2)
                    .frame(width: diameter * 0.75, height: diameter * 0.75)
                    .scaleEffect(on ? 1.9 : 0.8)
                    .opacity(on ? 0 : 0.7)
                    .animation(
                        .easeOut(duration: style.ring ?? Theme.orbRingPeriod)
                            .repeatForever(autoreverses: false),
                        value: on)
            }
        }
        .frame(width: diameter, height: diameter)
        .onAppear { on = true }
    }

    /// Glow tint, matched to the edge palette (violet while speaking).
    private var glow: Color {
        if case .voice = style.aurora { Theme.auroraVoice[3] } else { Theme.blue }
    }

    @Environment(\.accessibilityReduceMotion) private var reduce
    /// Searching (HTML sweep): side to side, never a spin; Reduce Motion: still.
    private var sweeping: Bool {
        if case .sweep = style.aurora { return !reduce }
        return false
    }
    private var sweepShift: CGFloat { sweeping ? (on ? 1 : -1) * diameter * Theme.Motion.sweepShift : 0 }
    private var sweepScale: CGFloat {
        sweeping ? (on ? Theme.Motion.sweepScale.upperBound : Theme.Motion.sweepScale.lowerBound) : 1
    }

    private var breathe: CGFloat {
        guard style.breathe != nil else { return 1 }
        return on ? 1.08 : 0.92
    }

    private var breatheOpacity: Double {
        guard style.breathe != nil else { return 1 }
        return on ? 1 : 0.9
    }

    private var breatheAnim: Animation? {
        guard let period = style.breathe else { return nil }
        return .easeInOut(duration: period)
            .repeatForever(autoreverses: true)
    }
}

/// Pip line copy with word-by-word reveal (design `data-words`).
struct WordReveal: View {
    var text: String
    var dots: Bool
    /// Font size in points (header: design 22 px scaled).
    var size: CGFloat = 17
    /// Lines before truncating (header: 2).
    var lines: Int = 1
    var color: Color = .primary
    /// Gray lead word ("You" while a person talks).
    var prefix: String? = nil
    var weight: Font.Weight = .regular
    var dotColor: Color = Theme.mutT
    @State private var shown = false

    var body: some View {
        HStack(spacing: 6) {
            textFlow
            if dots { ThinkingDots(color: dotColor) }
        }
        .id(text)
        .onAppear { shown = true }
    }

    private var textFlow: some View {
        // Single Text keeps selection/ VoiceOver simple; per-word reveal
        // via staggered overlay is overkill at bar size, so the line fades
        // in as one while the orb carries the motion.
        let lead = prefix.map { Text("\($0) ").foregroundColor(Theme.mutT) } ?? Text("")
        return (lead + Text(text).foregroundColor(color).fontWeight(weight))
            .font(Theme.ui(size))
            .tracking(-0.012 * size)
            .lineLimit(lines)
            .multilineTextAlignment(.leading)
            .truncationMode(.tail)
            .fixedSize(horizontal: false, vertical: true)
            .opacity(shown ? 1 : 0)
            .animation(Theme.ease(duration: 0.45), value: shown)
    }
}

/// Trailing `.pip .dots`: three 7px dots, sequential blink.
struct ThinkingDots: View {
    var color: Color = Theme.mutT
    @State private var on = false

    var body: some View {
        HStack(spacing: 6) {
            ForEach(0 ..< 3) { i in
                Circle()
                    .fill(color)
                    .frame(width: 7, height: 7)
                    .opacity(on ? 1 : 0.25)
                    .animation(
                        .easeInOut(duration: 0.5).repeatForever()
                            .delay(Double(i) * 0.25),
                        value: on)
            }
        }
        .onAppear { on = true }
    }
}
