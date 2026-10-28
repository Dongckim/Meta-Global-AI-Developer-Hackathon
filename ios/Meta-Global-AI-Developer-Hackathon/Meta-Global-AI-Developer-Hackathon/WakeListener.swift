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
        case .undetermined: "Waiting for permission — allow mic + speech recognition"
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

    /// Fired once per detection; the owner stops the listener when opening.
    var onWake: (() -> Void)?
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
    private var tail: [String] = []
    private var lastText = ""
    private var generation = 0
    private var fired = false
    private var restarts = RestartGate(limit: 5, window: 10)
    private(set) var running = false

    func start() async throws {
        guard !running else { return }
        guard recognizer != nil else { throw WakeError.noRecognizer }
        let rate = try await MicEngine.shared.prepare()
        guard let input = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                                        channels: 1, interleaved: false),
              let output = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000,
                                         channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: input, to: output)
        else { throw WakeError.cannotConvert }
        counter.reset()
        lastError = nil
        fired = false
        tail = []
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
                if fed { st.pointee = .endOfStream; return nil }
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
        req.contextualStrings = ["Hey Pip"]
        live.current = req
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
            if !fired, WakeMatcher.matches((tail + [lastText]).joined(separator: " ")) {
                fired = true
                onWake?()
                return
            }
            if result.isFinal { restart() }
        } else if error != nil {
            lastError = (error as? LocalizedError)?.errorDescription ?? "recognition ended"
            restart()
        }
    }

    /// Fresh request + task on the same sink; the transcript tail covers a
    /// phrase split across the boundary. Trips the backoff instead of
    /// churning when the recognizer keeps failing (auth revoked, etc.).
    private func restart() {
        guard running else { return }
        if restarts.note(date: Date()) {
            lastError = "recognition keeps failing — tap Check in Setup to retry"
            let cb = onFailed
            stop()
            cb?()
            return
        }
        task?.cancel()
        task = nil
        live.current?.endAudio()
        tail = lastText.lowercased().split { !$0.isLetter }.map(String.init).suffix(2)
        fired = false
        generation += 1
        beginTask(generation: generation)
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
