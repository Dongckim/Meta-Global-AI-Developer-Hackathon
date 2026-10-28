import AVFoundation
import Foundation

/// One engine, one tap, many sinks. Every mic consumer (wake word,
/// command capture, later streaming) subscribes here; nothing else may
/// create an audio engine or install a tap. The engine runs while at
/// least one sink is attached and tears down fully when the last
/// leaves, so the mic releases when nobody listens.
@MainActor
final class MicEngine {
    static let shared = MicEngine()

    private let box = SinkBox()
    private var engine: AVAudioEngine?
    private var hwRate: Double = 0

    var isRunning: Bool { engine?.isRunning ?? false }
    var sinkCount: Int { box.count }

    /// Starts the engine on first use (session config + preferred input
    /// live in Recorder; this only calls them). Idempotent.
    /// - Returns: the tap's sample rate, for sink-side conversion.
    func prepare() async throws -> Double {
        if let e = engine, e.isRunning { return hwRate }
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try Recorder.configureSession()
        let switched = Recorder.ensurePreferredInput()
        try session.setActive(true)
        if switched {
            try? await Task.sleep(for: .milliseconds(350))
        }
        #endif
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let hw = input.outputFormat(forBus: 0)
        guard hw.sampleRate > 0, hw.channelCount > 0 else { throw RecorderError.noInput }
        guard let tapFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: hw.sampleRate, channels: 1, interleaved: false
        ) else { throw RecorderError.cannotConvert }
        let box = self.box
        input.installTap(onBus: 0, bufferSize: 4096, format: tapFormat) { buffer, _ in
            box.consume(buffer)
        }
        engine.prepare()
        try engine.start()
        self.engine = engine
        self.hwRate = hw.sampleRate
        return hw.sampleRate
    }

    /// Attaches a sink. Engine must be running via prepare() first.
    func subscribe(_ sink: @escaping @Sendable (AVAudioPCMBuffer) -> Void) -> UUID {
        box.add(sink)
    }

    /// Detaches a sink; stops and tears down the engine when none remain.
    func unsubscribe(_ id: UUID) {
        box.remove(id)
        guard box.count == 0 else { return }
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false)
        #endif
        engine = nil
        hwRate = 0
    }
}

/// Lock-guarded fan-out. The tap thread only touches this; add/remove run
/// on MainActor. Consumption holds the lock so an unsubscribe strictly
/// excludes in-flight appends (a removed sink never sees another buffer).
final class SinkBox: @unchecked Sendable {
    private let lock = NSLock()
    nonisolated(unsafe) private var sinks: [UUID: (AVAudioPCMBuffer) -> Void] = [:]

    nonisolated var count: Int { lock.withLock { sinks.count } }

    nonisolated func add(_ sink: @escaping (AVAudioPCMBuffer) -> Void) -> UUID {
        let id = UUID()
        lock.withLock { sinks[id] = sink }
        return id
    }

    nonisolated func remove(_ id: UUID) {
        _ = lock.withLock { sinks.removeValue(forKey: id) }
    }

    nonisolated func consume(_ buffer: AVAudioPCMBuffer) {
        lock.withLock {
            for s in sinks.values { s(buffer) }
        }
    }
}
