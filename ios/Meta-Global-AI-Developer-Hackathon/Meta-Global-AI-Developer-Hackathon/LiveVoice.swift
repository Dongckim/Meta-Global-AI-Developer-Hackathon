import AVFoundation
import Foundation
import os
import Speech

/// What the live conversation link reports (shared/schemas/live.schema.json).
enum LiveEvent {
    /// Running transcript of the current turn (any speaker until it ends).
    case partial(String)
    /// Another speaker's turn: show `line` briefly, no reply.
    case ignored(line: String)
    /// The owner's turn, processed by the server like a /voice reply.
    case turn(VoiceResponse, endSession: Bool)
    /// Where that turn is on the server (thinking, searching, ...).
    case stage(TurnStage)
    /// The session is over. `why`: window / max_duration / end_session /
    /// client / server (normal) or error (the stream broke).
    case closed(why: String)
}

/// What counts as a quiet mic here: a second of the room, measured at the
/// session's start and when the audio route changes. Milo's voice isn't the
/// room: nothing is measured while the mic is muted.
struct QuietGauge {
    static let least: Float = 0.006
    static let aboveFloor: Float = 3
    /// One second of the 100 ms checks.
    static let samples = 10
    /// Latest for Diagnostics.
    @MainActor static var shown: (floor: Float, threshold: Float)?

    /// Until the first measure.
    private(set) var threshold: Float = 0.012
    private var window: [Float] = []

    /// Starts a measure; the threshold stays until it's done.
    mutating func restart() { window = [] }

    private var measuring: Bool { window.count < Self.samples }

    /// One check of the mic level; the new floor when this completes a measure.
    /// The median: someone speaking for part of the second doesn't set it.
    mutating func feed(_ level: Float, muted: Bool) -> Float? {
        guard !muted, measuring else { return nil }
        window.append(level)
        guard !measuring else { return nil }
        let floor = window.sorted()[Self.samples / 2]
        threshold = max(Self.least, floor * Self.aboveFloor)
        return floor
    }
}

/// What Diagnostics changed about when a turn ends; nothing set: the server's
/// defaults (pip_voice.json live.turn_end).
enum TurnEndTimes {
    static let defaults = [("quiet_s", 1.2), ("extra_s", 2.5), ("cap_s", 3.0)]
    private static func key(_ name: String) -> String { "pip.turnEnd.\(name)" }
    static var chosen: [String: Double] {
        defaults.reduce(into: [:]) { out, d in
            if let v = UserDefaults.standard.object(forKey: key(d.0)) as? Double { out[d.0] = v }
        }
    }
    static func value(_ name: String) -> Double {
        chosen[name] ?? defaults.first { $0.0 == name }?.1 ?? 0
    }
    static func set(_ name: String, _ v: Double) { UserDefaults.standard.set(v, forKey: key(name)) }
}

/// Seam for tests; LiveLink is the real one.
@MainActor
protocol LiveControlling: AnyObject {
    var onEvent: ((LiveEvent) -> Void)? { get set }
    /// The agent is speaking: send silence instead of the mic and tell the
    /// server (its window clock pauses).
    var muted: Bool { get set }
    func start(session: String) async throws
    func end()
    /// The engine was rebuilt (route change, interruption): re-attach the mic.
    func restartAudio() async
}

/// Float samples at 16 kHz -> 80 ms PCM16 LE mono frames (2560 bytes).
/// Muted input becomes the same amount of silence, so the stream keeps time.
struct PCMFramer {
    static let frameBytes = 2560
    private var pending = Data()

    mutating func append(_ samples: UnsafeBufferPointer<Float>, muted: Bool) -> [Data] {
        pending.reserveCapacity(pending.count + samples.count * 2)
        for x in samples {
            let v: Int16 = muted ? 0 : Int16(max(-1, min(1, x)) * 32767)
            withUnsafeBytes(of: v.littleEndian) { pending.append(contentsOf: $0) }
        }
        var out: [Data] = []
        while pending.count >= Self.frameBytes {
            out.append(pending.prefix(Self.frameBytes))
            pending.removeFirst(Self.frameBytes)
        }
        return out
    }
}

/// Lock-guarded flag the tap thread reads.
final class FlagBox: @unchecked Sendable {
    private let lock = NSLock()
    nonisolated(unsafe) private var v = false
    nonisolated var value: Bool { lock.withLock { v } }
    nonisolated func set(_ x: Bool) { lock.withLock { v = x } }
}

/// One more sink on the one MicEngine tap: 16 kHz stays as is (glasses
/// HFP), other rates convert to 16 kHz; frames go to `send`. While muted
/// (the agent speaks) the tap's audio is dropped and an audio-queue timer
/// sends 80 ms of silence on its own clock, so the stream keeps real time
/// whatever TTS, the UI or the engine do.
@MainActor
final class LiveMic {
    let muted = FlagBox()
    private var sinkID: UUID?
    private var send: (@Sendable (Data) -> Void)?
    private var silence: DispatchSourceTimer?
    private let audioQueue = DispatchQueue(label: "live.silence", qos: .userInteractive)

    func setMuted(_ on: Bool) {
        muted.set(on)
        silence?.cancel()
        silence = nil
        guard on, let send else { return }
        let t = DispatchSource.makeTimerSource(queue: audioQueue)
        let frame = Data(count: PCMFramer.frameBytes)
        t.schedule(deadline: .now(), repeating: .milliseconds(80), leeway: .milliseconds(5))
        t.setEventHandler { send(frame) }
        t.resume()
        silence = t
    }

    func start(send: @escaping @Sendable (Data) -> Void) async throws {
        self.send = send
        let rate = try await MicEngine.shared.prepare()
        guard let input = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                                        channels: 1, interleaved: false),
              let output = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000,
                                         channels: 1, interleaved: false)
        else { throw RecorderError.cannotConvert }
        let conv = rate == 16000 ? nil : AVAudioConverter(from: input, to: output).map { Unchecked(value: $0) }
        if rate != 16000, conv == nil { throw RecorderError.cannotConvert }
        let framer = Unchecked(value: FramerBox())
        let muted = self.muted
        let fmt = Unchecked(value: output)
        Log.audio.info("[audio] live sink rate=\(rate) convert=\(conv != nil)")
        sinkID = MicEngine.shared.subscribe { buffer in
            let buf = Unchecked(value: buffer)
            var src = buf.value
            if let conv {
                guard let out = AVAudioPCMBuffer(pcmFormat: fmt.value, frameCapacity:
                        AVAudioFrameCount(Double(buf.value.frameLength) * 16000 / rate) + 64) else { return }
                var fed = false
                var err: NSError?
                let status = conv.value.convert(to: out, error: &err) { _, st in
                    if fed { st.pointee = .noDataNow; return nil } // streaming: keep it open
                    fed = true
                    st.pointee = .haveData
                    return buf.value
                }
                guard err == nil, status != .error else { return }
                src = out
            }
            guard !muted.value else { return } // the silence timer speaks for us
            guard let ch = src.floatChannelData, src.frameLength > 0 else { return }
            let samples = UnsafeBufferPointer(start: ch[0], count: Int(src.frameLength))
            for frame in framer.value.framer.append(samples, muted: false) { send(frame) }
        }
    }

    func stop() {
        silence?.cancel()
        silence = nil
        if let id = sinkID { MicEngine.shared.unsubscribe(id) }
        sinkID = nil
    }

    /// Re-attach to a rebuilt engine, keeping the same send and mute state.
    /// The new sink attaches before the old one leaves, so the engine never
    /// drops to zero sinks (which would tear it down and release the session).
    func restart() async {
        guard let send else { return }
        let wasMuted = muted.value
        let old = sinkID
        sinkID = nil
        do {
            try await start(send: send)
            Log.audio.info("[audio] live sink re-attached")
        } catch {
            Log.audio.error("[audio] live sink re-attach failed: \(error.localizedDescription, privacy: .public)")
        }
        if let old { MicEngine.shared.unsubscribe(old) }
        setMuted(wasMuted)
    }

    /// Tap-thread-only state holder.
    final class FramerBox: @unchecked Sendable {
        nonisolated(unsafe) var framer = PCMFramer()
    }
}

/// Private: hears the owner on the iPad (requiresOnDeviceRecognition) and
/// hands over words; no audio leaves. One more sink on the MicEngine tap. The
/// server decides when a turn is over (live_session.turn_end) and says so
/// (`end_turn`); a fresh task starts then.
@MainActor
final class DeviceEars {
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let live = RequestBox()
    private var task: SFSpeechRecognitionTask?
    private var sinkID: UUID?
    private var generation = 0
    private var text = ""
    private var restarts = RestartGate(limit: 5, window: 10)
    private var heard: ((String, Int) -> Void)?
    private var failed: (() -> Void)?
    private var muted: FlagBox?
    /// What the recognizer should expect; the next task picks it up.
    var hints: [String] = []

    func start(muted: FlagBox, heard: @escaping (String, Int) -> Void,
               failed: @escaping () -> Void) async throws {
        guard let recognizer, recognizer.supportsOnDeviceRecognition else { throw WakeError.noRecognizer }
        self.muted = muted
        self.heard = heard
        self.failed = failed
        try await attach()
        beginTask()
    }

    func stop() {
        generation += 1
        task?.cancel()
        task = nil
        live.current?.endAudio()
        live.current = nil
        if let id = sinkID { MicEngine.shared.unsubscribe(id) }
        sinkID = nil
        heard = nil
    }

    /// Re-attach to a rebuilt engine; new sink first, as LiveMic.restart.
    func restart() async {
        guard heard != nil else { return }
        let old = sinkID
        do {
            try await attach()
        } catch {
            Log.audio.error("[audio] ears re-attach failed: \(error.localizedDescription, privacy: .public)")
        }
        if let old { MicEngine.shared.unsubscribe(old) }
    }

    private func attach() async throws {
        _ = try await MicEngine.shared.prepare()
        let live = self.live
        let muted = self.muted
        sinkID = MicEngine.shared.subscribe { buffer in
            guard muted?.value != true else { return }  // the agent is speaking
            live.current?.append(buffer)
        }
    }

    private func beginTask() {
        guard let recognizer else { return }
        generation += 1
        let gen = generation
        text = ""
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true
        req.shouldReportPartialResults = true
        req.contextualStrings = hints
        live.current = req
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            let words = result?.bestTranscription.formattedString
            let done = result?.isFinal ?? false
            let failure = error?.localizedDescription
            Task { @MainActor in self?.handle(words, done: done, failure: failure, gen: gen) }
        }
    }

    private func handle(_ words: String?, done: Bool, failure: String?, gen: Int) {
        guard gen == generation else { return }
        if let words, words != text {
            text = words
            heard?(text, gen)
        }
        if done {
            finishTurn()
        } else if let failure {
            // long silence ends a task with an error too: only a burst is a failure
            Log.voice.info("[voice] ears task ended: \(failure, privacy: .public)")
            if restarts.note(date: Date()) {
                failed?()
            } else {
                finishTurn()
            }
        }
    }

    /// The turn is over (the server's call), or the recognizer ended its task.
    func finishTurn() {
        task?.cancel()
        live.current?.endAudio()
        beginTask()
    }

    /// One recorded turn (the held mic), heard on the iPad.
    static func transcribe(wav: Data) async throws -> String {
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.supportsOnDeviceRecognition else { throw WakeError.noRecognizer }
        let url = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).wav")
        try wav.write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        let req = SFSpeechURLRecognitionRequest(url: url)
        req.requiresOnDeviceRecognition = true
        req.shouldReportPartialResults = false
        return try await withCheckedThrowingContinuation { k in
            var done = false
            let holder = Unchecked(value: recognizer)
            _ = holder.value.recognitionTask(with: req) { result, error in
                guard !done else { return }
                if let result, result.isFinal {
                    done = true
                    k.resume(returning: result.bestTranscription.formattedString)
                } else if let error {
                    done = true
                    k.resume(throwing: error)
                }
            }
        }
    }
}

enum LiveError: Error, LocalizedError {
    case refused(String)
    case timeout
    var errorDescription: String? {
        switch self {
        case .refused(let m): "Live session refused: \(m)"
        case .timeout: "Live session didn't start in time"
        }
    }
}

/// App side of /voice/live: start frame, mic frames, server messages.
@MainActor
final class LiveLink: LiveControlling {
    var onEvent: ((LiveEvent) -> Void)?
    private var task: URLSessionWebSocketTask?
    private var gauge = QuietGauge()
    private var routeWatch: Task<Void, Never>?
    private let mic = LiveMic()
    private let ears = DeviceEars()
    private var session = ""
    private var finished = false
    /// Last message from the server (pings included).
    private var lastHeard = Date()

    var muted: Bool {
        get { mic.muted.value }
        set {
            guard newValue != mic.muted.value else { return }
            Log.voice.info("[voice] live mic \(newValue ? "muted (silence sent)" : "open", privacy: .public)")
            mic.setMuted(newValue)
            task?.send(.string(#"{"type":"tts","speaking":\#(newValue)}"#)) { _ in }
        }
    }

    func start(session: String) async throws {
        self.session = session
        try await connect()
        Task { await idleWatch() }
        #if os(iOS)
        routeWatch = Task { [weak self] in
            for await _ in NotificationCenter.default.notifications(named: AVAudioSession.routeChangeNotification) {
                self?.gauge.restart()
            }
        }
        #endif
    }

    /// The server closed for a mode change: same session on the VoiceSession
    /// side, a new link hearing the new way.
    private func reconnect() {
        mic.stop()
        ears.stop()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        Task {
            do {
                try await connect()
            } catch {
                Log.voice.error("[voice] live reconnect failed: \(error.localizedDescription, privacy: .public)")
                finish("error")
            }
        }
    }

    private func connect() async throws {
        let t = URLSession.shared.webSocketTask(with: PipClient.liveURL)
        task = t
        t.resume()
        var opening: [String: Any] = ["type": "start", "session": session]
        for (key, value) in TurnEndTimes.chosen { opening[key] = value }
        try await t.send(.string(String(decoding: try JSONSerialization.data(withJSONObject: opening), as: UTF8.self)))
        let first = try await withThrowingTaskGroup(of: URLSessionWebSocketTask.Message.self) { g in
            g.addTask { try await t.receive() }
            g.addTask { try await Task.sleep(for: .seconds(6)); throw LiveError.timeout }
            let m = try await g.next()!
            g.cancelAll()
            return m
        }
        let obj = Self.object(first)
        guard obj["type"] as? String == "ready" else {
            t.cancel(with: .goingAway, reason: nil)
            throw LiveError.refused(obj["message"] as? String ?? "no ready")
        }
        Log.voice.info("[voice] live ready session=\(obj["session_id"] as? String ?? "-", privacy: .public)")
        do {
            if obj["transcribe"] as? String == "device" {
                ears.hints = obj["hints"] as? [String] ?? []
                try await ears.start(muted: mic.muted, heard: { text, task in
                    Self.send(t, ["type": "heard", "text": text, "task": task])
                }, failed: { [weak self] in self?.finish("error") })
            } else {
                try await mic.start { [weak self] data in
                    t.send(.data(data)) { err in
                        guard let err else { return }
                        // a frame that can't go out means the socket is gone
                        Task { @MainActor in
                            guard let self, !self.finished, self.task === t else { return }
                            Log.voice.error("[voice] live send failed: \(err.localizedDescription, privacy: .public)")
                            self.finish("error")
                        }
                    }
                }
            }
        } catch {
            t.cancel(with: .goingAway, reason: nil)
            throw error
        }
        lastHeard = Date()
        Task { await receiveLoop(t) }
        Task { await micWatch(t) }
    }

    /// Mic quiet or not, on change: half of how the server hears a turn's end.
    /// While the agent speaks there's nothing to report or measure.
    private func micWatch(_ t: URLSessionWebSocketTask) async {
        gauge.restart()
        var quiet: Bool?
        while !finished, task === t {
            try? await Task.sleep(for: .milliseconds(100))
            let level = MicEngine.shared.level
            if let floor = gauge.feed(level, muted: mic.muted.value) {
                QuietGauge.shown = (floor, gauge.threshold)
                Log.audio.info("[audio] mic floor \(floor) threshold \(self.gauge.threshold)")
                quiet = nil  // the server hears the new threshold
            }
            let now = level < gauge.threshold
            if now != quiet, !mic.muted.value {
                quiet = now
                Self.send(t, ["type": "mic", "quiet": now, "threshold": Double(gauge.threshold)])
            }
        }
    }

    private static func send(_ t: URLSessionWebSocketTask, _ msg: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: msg) else { return }
        t.send(.string(String(decoding: d, as: UTF8.self))) { _ in }
    }

    /// A session silent past the idle timeout (no ping either) is a dead
    /// socket, closed as a broken stream.
    private func idleWatch() async {
        while !finished {
            try? await Task.sleep(for: .seconds(5))
            if !finished, Date().timeIntervalSince(lastHeard) > PipClient.idleTimeout {
                Log.voice.error("[voice] live silent \(Int(PipClient.idleTimeout), privacy: .public)s: closing")
                finish("error")
            }
        }
    }

    func end() {
        Log.voice.info("[voice] live link end sent")
        task?.send(.string(#"{"type":"end"}"#)) { _ in }
    }

    func restartAudio() async {
        guard !finished else { return }
        await mic.restart()
        await ears.restart()
    }

    private func receiveLoop(_ t: URLSessionWebSocketTask) async {
        while !finished, task === t {
            do {
                let m = try await t.receive()
                lastHeard = Date()
                handle(m)
            } catch {
                guard task === t else { return }  // a reconnect closed it
                Log.voice.error("[voice] live socket: \(error.localizedDescription, privacy: .public)")
                finish("error")
            }
        }
    }

    private func handle(_ m: URLSessionWebSocketTask.Message) {
        let obj = Self.object(m)
        switch obj["type"] as? String {
        case "ping":
            break  // keepalive: only refreshes lastHeard
        case "partial":
            onEvent?(.partial(obj["text"] as? String ?? ""))
        case "hints":
            ears.hints = obj["hints"] as? [String] ?? []
        case "end_turn":
            Log.voice.info("[voice] turn ended by server: \(obj["reason"] as? String ?? "?", privacy: .public)")
            ears.finishTurn()
        case "ignored":
            Log.voice.info("[voice] ignored speaker=\(obj["speaker"] as? String ?? "?", privacy: .public) text=\"\(obj["text"] as? String ?? "", privacy: .public)\"")
            onEvent?(.ignored(line: obj["line"] as? String ?? ""))
        case "stage":
            if case .string(let s) = m, let st = try? JSONDecoder().decode(TurnStage.self, from: Data(s.utf8)) {
                onEvent?(.stage(st))
            }
        case "turn":
            guard case .string(let s) = m,
                  let r = try? JSONDecoder().decode(VoiceResponse.self, from: Data(s.utf8)) else {
                Log.voice.error("[voice] live turn undecodable")
                return
            }
            onEvent?(.turn(r, endSession: obj["end_session"] as? Bool ?? false))
        case "error":
            Log.voice.error("[voice] live error: \(obj["message"] as? String ?? "?", privacy: .public)")
            finish("error")
        case "closed":
            Log.voice.info("[voice] live closed code=\(obj["code"].map { "\($0)" } ?? "-", privacy: .public) why=\(obj["why"] as? String ?? "?", privacy: .public)")
            if obj["why"] as? String == "mode" {
                reconnect()
            } else {
                finish(obj["why"] as? String ?? "server")
            }
        default:
            break
        }
    }

    private func finish(_ why: String) {
        guard !finished else { return }
        finished = true
        routeWatch?.cancel()
        mic.stop()
        ears.stop()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        Log.voice.info("[voice] live finished: \(why, privacy: .public)")
        onEvent?(.closed(why: why))
    }

    private static func object(_ m: URLSessionWebSocketTask.Message) -> [String: Any] {
        let data: Data
        switch m {
        case .string(let s): data = Data(s.utf8)
        case .data(let d): data = d
        @unknown default: return [:]
        }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }
}
