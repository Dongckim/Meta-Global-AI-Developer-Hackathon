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
    var onWake: (() -> Void)? { get set }
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

    private(set) var state: State = .ready
    private(set) var listeningSince: Date?
    private(set) var lastHeld: Double = 0
    private(set) var wakeStatus = "Not checked"
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

    /// Mic button enabled: idle, or speaking (barge-in interrupts).
    var micLive: Bool { state == .ready || state == .speaking }

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
    private var gen = 0
    private var pressGen = 0
    private var activeTrigger: VoiceTrigger?
    private var activated = false
    private var startingWake = false
    private var wakeBroken = false

    convenience init() {
        self.init(wake: WakeListener(), capture: Recorder(), speaker: SpeechBus.shared)
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
         awaitSpeech: TimeInterval = 4) {
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
        self.wake.onWake = { @MainActor [weak self] in self?.open(trigger: .wakeWord) }
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

    func refreshWakeStatus() {
        let s = support()
        var text = WakeListener.describe(s)
        if s == .ok, !Self.wakeEnabled {
            text = "Off — enable “Hey Pip” in Settings"
        } else if s == .ok, wake.running {
            text += " · listening (\(wake.debugDetail))"
        }
        wakeStatus = text
    }

    // MARK: - Triggers

    /// Wake word opens from ready only (never mid-session, never mid-speech).
    /// Manual opens from ready, or speaking (barge-in stops TTS first).
    func open(trigger: VoiceTrigger) {
        open(trigger: trigger, press: pressGen)
    }

    private func open(trigger: VoiceTrigger, press: Int) {
        switch trigger {
        case .wakeWord:
            guard state == .ready else { return }
        case .manual:
            if state == .speaking {
                speaker.stop()
            } else {
                guard state == .ready else { return }
            }
        }
        gen += 1
        let g = gen
        activeTrigger = trigger
        wake.stop()
        if trigger == .wakeWord { chime() }
        Task { await beginCapture(trigger: trigger, gen: g, press: press) }
    }

    /// Mic press. Captures the press number synchronously so a release
    /// before the engine spins up (or before permission answers) aborts
    /// instead of stranding a capture.
    func beginManual() {
        pressGen += 1
        let p = pressGen
        Task {
            let ok: Bool
            if let micRequest {
                ok = await micRequest()
            } else {
                ok = await Recorder.requestPermission()
            }
            guard ok else { emit("Microphone permission needed"); return }
            open(trigger: .manual, press: p)
        }
    }

    /// Mic release. Always bumps the press number first (pairs with a
    /// press that hasn't reached open() yet), then finishes a live
    /// manual capture. Wake captures end themselves on silence.
    func endManual() {
        pressGen += 1
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
        guard state != .listening else { return }
        guard activated else { state = .ready; return }
        gen += 1
        let g = gen
        wake.stop()
        state = .speaking
        speaker.speak(text) { @MainActor [weak self] in
            guard let self, g == self.gen, self.state == .speaking else { return }
            self.state = .ready
            self.resumeWake()
        }
    }

    // MARK: - Private

    private func beginCapture(trigger: VoiceTrigger, gen g: Int, press p: Int) async {
        guard p == pressGen else {
            // Released before we even started (permission was slow).
            activeTrigger = nil
            resumeWake()
            return
        }
        do {
            try await capture.start()
        } catch {
            guard g == gen else { return }
            activeTrigger = nil
            emit("Recording failed: \(error.localizedDescription)")
            state = .ready
            resumeWake()
            return
        }
        guard g == gen else { try? capture.stop(); return }
        guard p == pressGen else {
            // Released before the engine spun up: unwind quietly.
            try? capture.stop()
            activeTrigger = nil
            resumeWake()
            return
        }
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
                activeTrigger = nil
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
        var url: URL?
        do {
            url = try capture.stop()
        } catch {
            if !silent { emit("Couldn't save the recording") }
        }
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

    private func support() -> WakeListener.Support {
        supportStub?() ?? WakeListener.support()
    }

    private func resumeWake() {
        guard activated, Self.wakeEnabled, state == .ready, !wakeBroken,
              support() == .ok, !wake.running, !startingWake else { return }
        startingWake = true
        let g = gen
        Task {
            defer { startingWake = false }
            do {
                try await wake.start()
            } catch {
                emit("Wake-word failed: \(error.localizedDescription)")
            }
            guard g == gen else { wake.stop(); return }
            refreshWakeStatus()
        }
    }

    private func emit(_ text: String) {
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

// MARK: - Orb (placeholder styling until Eunbi's design lands)

/// Top orb + Pip line (design `.orb` + `.pip`, shrunk from the fullscreen
/// Pip screens to bar size). Reads VoiceSession state only; never blocks
/// the run screen. `partialText` is nil until slice B streams transcripts.
struct VoiceOrb: View {
    var voice: VoiceSession
    var speakText: String = ""
    var partialText: String? = nil

    var body: some View {
        let style = Theme.orbStyle(for: voice.state)
        let line = Theme.pipText(for: voice.state, speakText: speakText,
                                 partialText: partialText)
        HStack(spacing: 10) {
            OrbDisc(style: style)
            WordReveal(text: line.text, dots: line.dots)
            Spacer()
        }
        .padding(.horizontal)
        .padding(.vertical, 4)
    }
}

/// Design orb at top size (40pt): radial gradient core, breathe, expanding
/// ring, and a small state-palette halo standing in for the fullscreen
/// aurora.
struct OrbDisc: View {
    var style: Theme.OrbStyle
    @State private var on = false

    var body: some View {
        ZStack {
            Circle()
                .fill(halo)
                .frame(width: 56, height: 56)
                .blur(radius: 14)
                .opacity(haloOpacity)
                .scaleEffect(haloScale)
                .offset(x: haloX)
                .animation(haloAnim, value: on)
            Circle()
                .fill(RadialGradient(
                    gradient: Gradient(stops: style.hollow
                        ? Theme.orbSpeakingStops : Theme.orbStops),
                    center: .init(x: 0.5, y: 0.52),
                    startRadius: 0, endRadius: 20))
                .frame(width: 40, height: 40)
                .blur(radius: 3)
                .scaleEffect(breathe * style.scale)
                .opacity(breatheOpacity)
                .animation(breatheAnim, value: on)
                .animation(Theme.spring(), value: style.scale)
            if style.ring != nil {
                Circle()
                    .stroke(Theme.orbRing, lineWidth: 2)
                    .frame(width: 30, height: 30)
                    .scaleEffect(on ? 1.9 : 0.8)
                    .opacity(on ? 0 : 0.7)
                    .animation(
                        .easeOut(duration: style.ring ?? Theme.orbRingPeriod)
                            .repeatForever(autoreverses: false),
                        value: on)
            }
        }
        .frame(width: 56, height: 56)
        .onAppear { on = true }
    }

    private var halo: Color {
        if case .voice(let _) = style.aurora {
            Theme.auroraVoice[3]
        } else {
            Theme.auroraDefault[3]
        }
    }

    private var haloOpacity: Double {
        style.aurora == .think ? 0.7 : 0.85
    }

    private var haloScale: CGFloat {
        switch style.aurora {
        case .breath: on ? 1.05 : 1
        case .pulse: on ? 1.2 : 1.1
        case let .still(s): s
        case .think: on ? 0.82 : 0.75
        case let .voice(amp): 1.24 + CGFloat(amp) * 0.16
        case .sweep: on ? 0.95 : 0.9
        }
    }

    private var haloX: CGFloat {
        style.aurora == .sweep ? (on ? 6 : -6) : 0
    }

    private var haloAnim: Animation? {
        switch style.aurora {
        case .breath:
            .easeInOut(duration: Theme.readyBreath)
                .repeatForever(autoreverses: true)
        case .pulse:
            .easeInOut(duration: Theme.pulse).repeatForever(autoreverses: true)
        case .still: nil
        case .think:
            .easeInOut(duration: Theme.think).repeatForever(autoreverses: true)
        case .voice: nil
        case .sweep:
            .easeInOut(duration: Theme.sweep).repeatForever(autoreverses: true)
        }
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
            .font(.system(size: 17, weight: .light))
            .foregroundStyle(.primary)
            .lineLimit(1)
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
