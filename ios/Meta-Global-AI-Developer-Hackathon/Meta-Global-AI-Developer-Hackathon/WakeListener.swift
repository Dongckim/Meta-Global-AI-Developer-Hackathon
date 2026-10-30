import AVFoundation
import Foundation
import os
import Speech

/// Always-on "Hey Pip" spotter. Feeds a shared-engine sink through a 16kHz
/// converter into an on-device recognizer; ambient audio never leaves the
/// iPad (requiresOnDeviceRecognition). Restarts seamlessly when a task
/// ends or errors, carrying the transcript tail across the boundary.
@MainActor
final class WakeListener {
    enum Support {
        case ok
        case undetermined
        case micDenied
        case speechDenied
        case speechRestricted
        case noRecognizer
        case noOnDevice
    }

    /// Gate check. Sync, prompts nothing; Setup shows the reason when not ok.
    static func support() -> Support {
        #if os(iOS)
        switch AVAudioApplication.shared.recordPermission {
        case .denied: return .micDenied
        case .undetermined: return .undetermined
        default: break
        }
        #endif
        switch SFSpeechRecognizer.authorizationStatus() {
        case .denied: return .speechDenied
        case .restricted: return .speechRestricted
        case .notDetermined: return .undetermined
        default: break
        }
        guard let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US")) else {
            return .noRecognizer
        }
        if !rec.supportsOnDeviceRecognition { return .noOnDevice }
        return .ok
    }

    static func describe(_ s: Support) -> String {
        switch s {
        case .ok: "Ready"
        case .undetermined: "Not allowed yet — tap Glasses audio on Glasses sync"
        case .micDenied: "Off — mic denied (enable in Settings)"
        case .speechDenied: "Off — speech recognition denied (enable in Settings)"
        case .speechRestricted: "Off — speech recognition restricted on this iPad"
        case .noRecognizer: "Off — no en-US recognizer on this iPad"
        case .noOnDevice: "Off — this iPad can't do on-device recognition"
        }
    }

    /// In-flight prompt: system permission UI is one-shot, so concurrent
    /// callers (launch auto-ask + Setup Check) join one request.
    private static var inflight: Task<Bool, Never>?

    /// Asks for speech + mic access. True when both granted.
    static func requestAccess() async -> Bool {
        if let t = inflight {
            Log.wake.info("[wake] requestAccess join inflight")
            return await t.value
        }
        Log.wake.info("[wake] requestAccess begin")
        let t = Task<Bool, Never> {
            let speech = await withCheckedContinuation { cont in
                SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0) }
            }
            Log.wake.info("[wake] speech=\(speechString(speech), privacy: .public)")
            guard speech == .authorized else { return false }
            #if os(iOS)
            let mic = await Recorder.requestPermission()
            Log.wake.info("[wake] mic=\(mic ? "granted" : "denied", privacy: .public)")
            return mic
            #else
            return true
            #endif
        }
        inflight = t
        let granted = await t.value
        inflight = nil
        Log.wake.info("[wake] requestAccess end granted=\(granted)")
        return granted
    }

    private static func speechString(_ s: SFSpeechRecognizerAuthorizationStatus) -> String {
        switch s {
        case .authorized: "authorized"
        case .denied: "denied"
        case .restricted: "restricted"
        case .notDetermined: "notDetermined"
        @unknown default: "unknown"
        }
    }

    /// Offered per detection; true = accepted (the owner stops the listener
    /// when the capture attaches). A rejection re-arms on a fresh task.
    var onWake: (() -> Bool)?
    /// Fired when restarts keep failing (backoff tripped); owner latches.
    var onFailed: (() -> Void)?

    /// Buffers converted since start (0 with no mic data); for Setup/debug.
    var convertedBuffers: Int { counter.value }
    private(set) var lastError: String?
    private let counter = CounterBox()

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let live = RequestBox()
    private var task: SFSpeechRecognitionTask?
    private var sinkID: UUID?
    private var latch = WakeLatch()
    private var lastText = ""
    /// Recognition-task counter (own numbering, not VoiceSession.gen).
    private var generation = 0
    private var restarts = RestartGate(limit: 5, window: 10)
    private(set) var running = false

    /// Wake phrase taught to the on-device recognizer (custom language
    /// model). nil until prepared, or when unsupported: the matcher alone.
    private static var phraseModel: SFSpeechLanguageModel.Configuration?
    private static var preparingModel = false

    /// Builds the custom language model once per launch in the background;
    /// tasks begun after it is ready use it.
    private static func prepareModel() {
        guard phraseModel == nil, !preparingModel else { return }
        preparingModel = true
        let phrase = Persona.current.wakePhrase
        Boot.mark("wake phrase model prepare")
        Task {
            let dir = FileManager.default.temporaryDirectory
            let asset = dir.appending(path: "wake-lm.bin")
            let config = SFSpeechLanguageModel.Configuration(languageModel: dir.appending(path: "wake-lm"))
            do {
                let data = SFCustomLanguageModelData(locale: Locale(identifier: "en_US"),
                                                     identifier: "wake.phrase", version: "1") {
                    SFCustomLanguageModelData.PhraseCount(phrase: phrase, count: 200)
                }
                try await data.export(to: asset)
                try await SFSpeechLanguageModel.prepareCustomLanguageModel(
                    for: asset, clientIdentifier: "wake.phrase", configuration: config)
                phraseModel = config
                Log.wake.info("[wake] custom language model ready for \"\(phrase, privacy: .public)\"")
                Boot.mark("wake phrase model ready")
            } catch {
                Log.wake.error("[wake] custom language model unavailable (matcher only): \(error.localizedDescription, privacy: .public)")
            }
            preparingModel = false
        }
    }

    func start() async throws {
        guard !running else { return }
        guard recognizer != nil else { throw WakeError.noRecognizer }
        Self.prepareModel()
        Log.wake.info("[wake] start: preparing engine")
        let rate = try await MicEngine.shared.prepare()
        guard let input = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                                        channels: 1, interleaved: false),
              let output = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000,
                                         channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: input, to: output)
        else { throw WakeError.cannotConvert }
        counter.reset()
        lastError = nil
        latch = WakeLatch()
        lastText = ""
        restarts = RestartGate(limit: 5, window: 10)
        running = true
        let live = self.live
        let counter = self.counter
        let conv = Unchecked(value: converter) // tap thread only; converter outlives the sink
        let fmt = Unchecked(value: output)
        sinkID = MicEngine.shared.subscribe { [weak live, weak counter] buffer in
            guard let live, let req = live.current else { return }
            let buf = Unchecked(value: buffer) // converted synchronously below
            guard let out = AVAudioPCMBuffer(pcmFormat: fmt.value,
                                             frameCapacity: AVAudioFrameCount(
                                                 Double(buf.value.frameLength) * 16000 / rate) + 64)
            else { return }
            var fed = false
            var err: NSError?
            let status = conv.value.convert(to: out, error: &err) { _, st in
                // Streaming: .noDataNow keeps the converter open. .endOfStream
                // ended it after the first buffer (device log: bufs=1 forever).
                if fed { st.pointee = .noDataNow; return nil }
                fed = true
                st.pointee = .haveData
                return buf.value
            }
            guard err == nil, status != .error, out.frameLength > 0 else { return }
            counter?.increment()
            req.append(out)
        }
        beginTask(generation: generation)
    }

    func stop() {
        Log.wake.info("[wake] stop running=\(self.running) sink=\(self.sinkID != nil) bufs=\(self.counter.value)")
        generation += 1 // invalidates late callbacks
        task?.cancel()
        task = nil
        live.current?.endAudio()
        live.current = nil
        if let id = sinkID { MicEngine.shared.unsubscribe(id) }
        sinkID = nil
        running = false
    }

    private func beginTask(generation gen: Int) {
        guard running, gen == generation, let recognizer else { return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true
        req.shouldReportPartialResults = true
        req.contextualStrings = [Persona.current.wakePhrase]
        if let lm = Self.phraseModel { req.customizedLanguageModel = lm }
        live.current = req
        Log.wake.info("[wake] recognition task begin task=\(gen) bufs=\(self.counter.value)")
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            Task { @MainActor in
                self.handle(result: result, error: error, generation: gen)
            }
        }
    }

    private func handle(result: SFSpeechRecognitionResult?, error: Error?,
                        generation gen: Int) {
        guard running, gen == generation else { return }
        if let result {
            lastText = result.bestTranscription.formattedString
            Log.wake.info("[wake] heard final=\(result.isFinal) \"\(self.lastText, privacy: .public)\"")
            #if DEBUG
            if probe != nil {
                measure(text: lastText, isFinal: result.isFinal)
                return
            }
            #endif
            if latch.hear(lastText) {
                let accepted = onWake?() ?? false
                Log.wake.info("[wake] fired accepted=\(accepted)")
                if accepted {
                    latch.accept()
                } else {
                    rearm() // the heard phrase must not re-offer; next one fires
                }
                return
            }
            if result.isFinal { restart() }
        } else if let error {
            lastError = (error as? LocalizedError)?.errorDescription ?? "recognition ended"
            Log.wake.error("[wake] task error: \(error.localizedDescription, privacy: .public) bufs=\(self.counter.value)")
            restart()
        }
    }

    /// Fresh request + task on the same sink; the transcript tail covers a
    /// phrase split across the boundary. Trips the backoff instead of
    /// churning when the recognizer keeps failing (auth revoked, etc.).
    private func restart() {
        guard running else { return }
        Log.wake.info("[wake] restart task")
        if restarts.note(date: Date()) {
            Log.wake.error("[wake] restart backoff tripped -> failed latch")
            lastError = "recognition keeps failing — tap Check in Setup to retry"
            let cb = onFailed
            stop()
            cb?()
            return
        }
        newTask(carry: lastText)
    }

    /// Rejected offer: fresh task with nothing carried, outside the
    /// failure backoff (a rejection is not a recognizer failure).
    private func rearm() {
        newTask(carry: nil)
    }

    #if DEBUG
    /// Measurement mode: the next `count` utterances are scored (hit = the
    /// matcher fires) instead of opening sessions; the summary is logged.
    private var probe: WakeProbe?

    func measure(count: Int) {
        probe = WakeProbe(target: count)
        Log.wake.info("[wake] measure: say \"\(Persona.current.wakePhrase, privacy: .public)\" \(count) times")
    }

    private func measure(text: String, isFinal: Bool) {
        guard var p = probe else { return }
        let hit = latch.hear(text)
        guard hit || (isFinal && !text.isEmpty) else { return }
        let summary = p.note(text, hit: hit)
        probe = summary == nil ? p : nil
        Log.wake.info("[wake] measure \(p.heard.count)/\(p.target) \(hit ? "hit" : "miss", privacy: .public) \"\(text, privacy: .public)\"")
        if let summary { Log.wake.info("[wake] \(summary, privacy: .public)") }
        if hit { rearm() } else { restart() }
    }
    #endif

    private func newTask(carry: String?) {
        task?.cancel()
        task = nil
        live.current?.endAudio()
        latch.newTask(carry: carry)
        lastText = ""
        generation += 1
        beginTask(generation: generation)
    }
}

/// Pure wake decision across recognition tasks. A detection is offered
/// until the owner accepts it; only acceptance latches (until a new task).
struct WakeLatch {
    private(set) var fired = false
    private var tail: [String] = []

    /// One recognition result: true when it should be offered.
    func hear(_ text: String) -> Bool {
        !fired && WakeMatcher.matches((tail + [text]).joined(separator: " "))
    }

    mutating func accept() { fired = true }

    /// New task. `carry` keeps the last two words so a phrase split across
    /// the boundary still matches; nil (rejection) starts clean.
    mutating func newTask(carry: String?) {
        tail = carry.map { Array($0.lowercased().split { !$0.isLetter }.map(String.init).suffix(2)) } ?? []
        fired = false
    }
}

/// Pure restart backoff: true when more than `limit` notes land in `window`.
struct RestartGate {
    let limit: Int
    let window: TimeInterval
    private var marks: [Date] = []

    init(limit: Int, window: TimeInterval) {
        self.limit = limit
        self.window = window
    }

    /// Records a restart; true means stop retrying.
    mutating func note(date: Date) -> Bool {
        marks = marks.filter { date.timeIntervalSince($0) < window } + [date]
        return marks.count > limit
    }
}

/// Lock-guarded holder so the realtime sink always appends to the live
/// request across seamless restarts.
final class RequestBox: @unchecked Sendable {
    private let lock = NSLock()
    nonisolated(unsafe) private var req: SFSpeechAudioBufferRecognitionRequest?
    nonisolated var current: SFSpeechAudioBufferRecognitionRequest? {
        get { lock.withLock { req } }
        set { lock.withLock { req = newValue } }
    }
}

/// Lock-guarded counter for tap-thread statistics.
final class CounterBox: @unchecked Sendable {
    private let lock = NSLock()
    nonisolated(unsafe) private var n = 0
    nonisolated var value: Int { lock.withLock { n } }
    nonisolated func increment() { lock.withLock { n += 1 } }
    nonisolated func reset() { lock.withLock { n = 0 } }
}

enum WakeError: Error, LocalizedError {
    case noRecognizer
    case cannotConvert
    var errorDescription: String? {
        switch self {
        case .noRecognizer: "No en-US speech recognizer"
        case .cannotConvert: "Couldn't prepare wake-word audio conversion"
        }
    }
}

/// DEBUG wake measurement: scores utterances until `target`, then reports.
struct WakeProbe {
    let target: Int
    private(set) var heard: [(text: String, hit: Bool)] = []

    init(target: Int) { self.target = target }

    /// Records one utterance; returns the summary line once `target` is reached.
    mutating func note(_ text: String, hit: Bool) -> String? {
        heard.append((text, hit))
        guard heard.count >= target else { return nil }
        let hits = heard.filter(\.hit).count
        let list = heard.map { "\($0.hit ? "✓" : "✗") \"\($0.text)\"" }.joined(separator: " | ")
        return "measure done: \(hits)/\(heard.count) (\(hits * 100 / max(heard.count, 1))%) — \(list)"
    }
}
