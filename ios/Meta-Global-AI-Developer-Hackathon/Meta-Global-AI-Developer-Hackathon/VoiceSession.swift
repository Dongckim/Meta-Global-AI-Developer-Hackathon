import AudioToolbox
import Foundation
import os
import Observation
import SwiftUI

/// How a voice session opened. Later: .glassesTap (DAT Inputs Select
/// fires the same event; the machine doesn't care which input woke it).
enum VoiceTrigger {
    case wakeWord
    case manual
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

    enum Admission: Equatable { case accept, ignore, bargeIn }

    /// The one table for state x trigger. `starting`: a capture was
    /// accepted and is spinning up (state still ready, wake still attached).
    ///
    /// | state                         | wake word | manual  |
    /// |-------------------------------|-----------|---------|
    /// | ready                         | accept    | accept  |
    /// | ready + starting              | ignore    | ignore  |
    /// | listening                     | ignore    | ignore  |
    /// | transcribing/thinking/searching | ignore  | ignore  |
    /// | speaking                      | ignore    | bargeIn |
    ///
    /// Wake never barges in: TTS through the speaker would wake itself.
    static func admission(state: State, trigger: VoiceTrigger, starting: Bool) -> Admission {
        switch (state, trigger) {
        case (.ready, _): starting ? .ignore : .accept
        case (.speaking, .manual): .bargeIn
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
    private var speakLimit: TimeInterval = 0
    private var gen = 0
    private var pressGen = 0
    private(set) var activeTrigger: VoiceTrigger?
    /// gen that set activeTrigger; only that capture's exit may clear it.
    private var captureGen = 0
    private var activated = false
    private var startingWake = false
    private var wakeBroken = false

    convenience init() {
        self.init(wake: WakeListener(), capture: Recorder(), speaker: SpeechBus.shared)
        MicEngine.shared.onLost = { [weak self] in self?.audioLost() }
    }

    /// Shared engine died under us: end a live capture as a release (or
    /// drop a wake capture), and restart wake from scratch.
    func audioLost() {
        Log.voice.error("[voice] audio lost state=\(self.state.rawValue, privacy: .public) trigger=\(String(describing: self.activeTrigger), privacy: .public)")
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
         speakMargin: TimeInterval = 5) {
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

    func deactivate() {
        Log.voice.info("[voice] deactivate state=\(self.state.rawValue, privacy: .public)")
        activated = false
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
        }
        gen += 1
        let g = gen
        captureGen = g
        activeTrigger = trigger
        // Wake keeps running until capture is attached (beginCapture): the
        // shared engine stays up, so no route re-switch eats the press.
        if trigger == .wakeWord { chime() }
        Task { await beginCapture(trigger: trigger, gen: g, press: press) }
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
        guard state == .transcribing || state == .thinking || state == .ready else { return }
        state = hasSources ? .searching : .thinking
    }

    /// Owner failed the turn (network etc.): back to ready, wake resumes.
    func didFail() {
        gen += 1
        pendingAudio = nil
        activeTrigger = nil
        state = .ready
        listeningSince = nil
        resumeWake()
    }

    /// The one way Pip talks (replies, timers, import): speaking while
    /// TTS runs, then ready. Refused mid-listening (no self-feedback).
    func speak(_ text: String?) {
        Log.voice.info("[voice] speak len=\(text?.count ?? -1) state=\(self.state.rawValue, privacy: .public) activated=\(self.activated)")
        guard state != .listening else { return }
        guard activated else { state = .ready; return }
        gen += 1
        let g = gen
        wake.stop()
        // Expected length (~12 chars/s at the default rate) + margin; the
        // watchdog ends speaking even if TTS never reports done.
        speakLimit = Double(text?.count ?? 0) / 12 + speakMargin
        state = .speaking
        speaker.speak(text) { @MainActor [weak self] in
            guard let self else { return }
            Log.voice.info("[voice] tts done gen=\(g)/\(self.gen) state=\(self.state.rawValue, privacy: .public)")
            guard g == self.gen, self.state == .speaking else { return }
            self.state = .ready
            self.resumeWake()
        }
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
        if trigger == .wakeWord {
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
    private func armWatchdog() {
        guard state != .ready else { return }
        let s = state, g = gen
        let limit = switch s {
        case .listening: maxCapture
        case .speaking: speakLimit
        default: stuckTimeout
        }
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(limit))
            guard let self, self.state == s, self.gen == g else { return }
            Log.voice.error("[voice] watchdog: \(s.rawValue, privacy: .public) for \(limit)s -> ready")
            if s == .listening, self.activeTrigger == .manual {
                self.endManual()
            } else {
                self.close()
            }
        }
    }

    private func support() -> WakeListener.Support {
        supportStub?() ?? WakeListener.support()
    }

    private func resumeWake() {
        guard activated, Self.wakeEnabled, state == .ready, !wakeBroken,
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
    /// Tap while talking is blocked by a permission: opens the sync screen.
    var onBlockedTap: () -> Void = {}

    var body: some View {
        let line = Theme.pipText(for: voice.state, speakText: speakText,
                                 partialText: partialText, wake: voice.wakeSupport,
                                 wakeOn: VoiceSession.wakeEnabled)
        TopHeaderView(style: Theme.orbStyle(for: voice.state), text: line.text, dots: line.dots,
                      blocked: voice.state == .ready
                          && Theme.talkBlocker(wake: voice.wakeSupport, wakeOn: true) != nil,
                      onBlockedTap: onBlockedTap)
    }
}

/// Header layout only (plain values, so DEBUG previews can show any line).
/// The orb keeps a slot as wide as its largest state so growing never
/// pushes or covers the text; the line wraps to two lines, then truncates,
/// and the group stays centered.
struct TopHeaderView: View {
    var style: Theme.OrbStyle
    var text: String
    var dots: Bool
    var blocked = false
    var onBlockedTap: () -> Void = {}
    @State private var width: CGFloat = 0

    var body: some View {
        let px = { (v: CGFloat) in Theme.px(v, in: width) }
        ZStack(alignment: .top) {
            HStack(spacing: px(12)) {
                Image(systemName: "cloud").font(.system(size: px(22)))
                Text(Theme.modeLabel).font(Theme.ui(px(22))).tracking(-0.012 * px(22))
            }
            .foregroundStyle(Theme.mutSay)
            .frame(height: px(64))
            .padding(.leading, px(64))
            .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: px(16)) {
                OrbDisc(style: style, diameter: px(52))
                    .frame(width: px(52) * Theme.orbMaxScale)
                // 22 px (design .t24) by review; the design .pip is 26
                WordReveal(text: text, dots: dots, size: px(22), lines: 2)
            }
            .frame(minHeight: px(64))
            // keep clear of the mode label on both sides so the group stays centered
            .frame(maxWidth: max(0, width - 2 * px(64 + 180)))
            .contentShape(Rectangle())
            .onTapGesture { if blocked { onBlockedTap() } }
            .accessibilityAddTraits(blocked ? .isButton : [])
            .frame(maxWidth: .infinity)
        }
        .padding(.top, px(44)) // from the screen top, like the design canvas
        .padding(.bottom, px(24))
        .ignoresSafeArea(edges: .top)
        .onGeometryChange(for: CGFloat.self, of: { $0.size.width }) { width = $0 }
    }
}

/// Design orb (`.orb`, `diameter` = core size): radial gradient core with
/// a tight glow, breathe, expanding ring. No wandering halo (iPad review).
struct OrbDisc: View {
    var style: Theme.OrbStyle
    var diameter: CGFloat = 40
    @State private var on = false

    var body: some View {
        ZStack {
            Circle()
                .fill(RadialGradient(
                    gradient: Gradient(stops: style.hollow
                        ? Theme.orbSpeakingStops : Theme.orbStops),
                    center: .init(x: 0.5, y: 0.52),
                    startRadius: 0, endRadius: diameter / 2))
                .frame(width: diameter, height: diameter)
                .blur(radius: diameter * 0.075)
                // the orb itself glows; no larger aurora around it
                .shadow(color: glow.opacity(0.75), radius: diameter * 0.18)
                .scaleEffect(breathe * style.scale)
                .opacity(breatheOpacity)
                .animation(breatheAnim, value: on)
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
    @State private var shown = false

    var body: some View {
        HStack(spacing: 6) {
            textFlow
            if dots { ThinkingDots() }
        }
        .id(text)
        .onAppear { shown = true }
    }

    private var textFlow: some View {
        // Single Text keeps selection/ VoiceOver simple; per-word reveal
        // via staggered overlay is overkill at bar size, so the line fades
        // in as one while the orb carries the motion.
        Text(text)
            .font(Theme.ui(size))
            .tracking(-0.012 * size)
            .foregroundStyle(.primary)
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
    @State private var on = false

    var body: some View {
        HStack(spacing: 6) {
            ForEach(0 ..< 3) { i in
                Circle()
                    .fill(Theme.mutT)
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
