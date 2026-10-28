import AudioToolbox
import Foundation
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
        if support() == .undetermined {
            Task {
                _ = await WakeListener.requestAccess()
                refreshWakeStatus()
                resumeWake()
            }
        } else {
            resumeWake()
        }
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

/// Top status dot + state label. Display only; never blocks the screen.
struct VoiceOrb: View {
    var voice: VoiceSession

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 9, height: 9)
                .opacity(voice.state == .ready ? 0.6 : 1)
                .animation(pulsing
                    ? .easeInOut(duration: 0.4).repeatForever(autoreverses: true)
                    : .default, value: pulsing)
            Text(voice.stateLabel)
                .font(.caption2).foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.horizontal)
        .padding(.vertical, 3)
    }

    private var pulsing: Bool { voice.state != .ready }

    private var color: Color {
        switch voice.state {
        case .ready: .gray
        case .listening: .red
        case .transcribing: .orange
        case .thinking: .blue
        case .speaking: .green
        case .searching: .purple
        }
    }
}
