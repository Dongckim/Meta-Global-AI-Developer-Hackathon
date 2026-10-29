import AVFoundation
import Foundation
import os

/// What the live conversation link reports (shared/schemas/live.schema.json).
enum LiveEvent {
    /// Running transcript of the current turn (any speaker until it ends).
    case partial(String)
    /// Another speaker's turn: show `line` briefly, no reply.
    case ignored(line: String)
    /// The owner's turn, processed by the server like a /voice reply.
    case turn(VoiceResponse, endSession: Bool)
    /// The session is over (server window, error, or the socket dropped).
    case closed(String)
}

/// Seam for tests; LiveLink is the real one.
@MainActor
protocol LiveControlling: AnyObject {
    var onEvent: ((LiveEvent) -> Void)? { get set }
    /// Sends silence instead of the mic (while the agent speaks).
    var muted: Bool { get set }
    func start(session: String) async throws
    func end()
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
/// HFP), other rates convert to 16 kHz; frames go to `send`.
@MainActor
final class LiveMic {
    let muted = FlagBox()
    private var sinkID: UUID?

    func start(send: @escaping @Sendable (Data) -> Void) async throws {
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
            guard let ch = src.floatChannelData, src.frameLength > 0 else { return }
            let samples = UnsafeBufferPointer(start: ch[0], count: Int(src.frameLength))
            for frame in framer.value.framer.append(samples, muted: muted.value) { send(frame) }
        }
    }

    func stop() {
        if let id = sinkID { MicEngine.shared.unsubscribe(id) }
        sinkID = nil
    }

    /// Tap-thread-only state holder.
    final class FramerBox: @unchecked Sendable {
        nonisolated(unsafe) var framer = PCMFramer()
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
    private let mic = LiveMic()
    private var finished = false

    var muted: Bool {
        get { mic.muted.value }
        set { mic.muted.set(newValue) }
    }

    func start(session: String) async throws {
        let t = URLSession.shared.webSocketTask(with: PipClient.liveURL)
        task = t
        t.resume()
        try await t.send(.string(#"{"type":"start","session":"\#(session)"}"#))
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
            try await mic.start { data in t.send(.data(data)) { _ in } }
        } catch {
            t.cancel(with: .goingAway, reason: nil)
            throw error
        }
        Task { await receiveLoop(t) }
    }

    func end() {
        task?.send(.string(#"{"type":"end"}"#)) { _ in }
    }

    private func receiveLoop(_ t: URLSessionWebSocketTask) async {
        while !finished {
            do {
                let m = try await t.receive()
                handle(m)
            } catch {
                finish("socket: \(error.localizedDescription)")
            }
        }
    }

    private func handle(_ m: URLSessionWebSocketTask.Message) {
        let obj = Self.object(m)
        switch obj["type"] as? String {
        case "partial":
            onEvent?(.partial(obj["text"] as? String ?? ""))
        case "ignored":
            Log.voice.info("[voice] ignored speaker=\(obj["speaker"] as? String ?? "?", privacy: .public) text=\"\(obj["text"] as? String ?? "", privacy: .public)\"")
            onEvent?(.ignored(line: obj["line"] as? String ?? ""))
        case "turn":
            guard case .string(let s) = m,
                  let r = try? JSONDecoder().decode(VoiceResponse.self, from: Data(s.utf8)) else {
                Log.voice.error("[voice] live turn undecodable")
                return
            }
            onEvent?(.turn(r, endSession: obj["end_session"] as? Bool ?? false))
        case "error":
            finish("error: \(obj["message"] as? String ?? "?")")
        case "closed":
            finish("closed \(obj["code"].map { "\($0)" } ?? "-") \(obj["reason"] as? String ?? "")")
        default:
            break
        }
    }

    private func finish(_ reason: String) {
        guard !finished else { return }
        finished = true
        mic.stop()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        Log.voice.info("[voice] live finished: \(reason, privacy: .public)")
        onEvent?(.closed(reason))
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
