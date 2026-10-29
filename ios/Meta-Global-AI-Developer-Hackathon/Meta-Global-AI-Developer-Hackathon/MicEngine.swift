import AVFoundation
import Foundation
import os

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
        if let e = engine, e.isRunning {
            Log.audio.info("[audio] prepare reuse running engine sinks=\(self.box.count)")
            return hwRate
        }
        preparing += 1
        defer { preparing -= 1 }
        Log.audio.info("[audio] prepare begin inflight=\(self.preparing) stale=\(self.engine != nil) sinks=\(self.box.count) \(Log.route(), privacy: .public)")
        do {
            #if os(iOS)
            let session = AVAudioSession.sharedInstance()
            try Recorder.configureSession()
            let switched = Recorder.ensurePreferredInput()
            try session.setActive(true)
            Log.audio.info("[audio] session active switched=\(switched) \(Log.route(), privacy: .public)")
            if switched {
                try? await Task.sleep(for: .milliseconds(350))
            }
            #endif
            let engine = AVAudioEngine()
            let input = engine.inputNode
            let hw = input.outputFormat(forBus: 0)
            Log.audio.info("[audio] input hw rate=\(hw.sampleRate) ch=\(hw.channelCount)")
            guard hw.sampleRate > 0, hw.channelCount > 0 else { throw RecorderError.noInput }
            guard let tapFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32, sampleRate: hw.sampleRate, channels: 1, interleaved: false
            ) else { throw RecorderError.cannotConvert }
            let box = self.box
            // 1024: at the 16kHz glasses rate 4096 was 256ms per buffer, so a
            // release dropped up to a quarter second of the command's tail.
            input.installTap(onBus: 0, bufferSize: 1024, format: tapFormat) { buffer, _ in
                box.consume(buffer)
            }
            Log.audio.info("[audio] tap installed")
            engine.prepare()
            try engine.start()
            if let old = self.engine {
                Log.audio.error("[audio] replacing engine running=\(old.isRunning) (overlapping prepare or stopped engine)")
            }
            self.engine = engine
            self.hwRate = hw.sampleRate
            Log.audio.info("[audio] engine started rate=\(hw.sampleRate)")
            return hw.sampleRate
        } catch {
            Log.audio.error("[audio] prepare failed: \(error.localizedDescription, privacy: .public)")
            throw error
        }
    }

    private var preparing = 0
    private var observers: [NSObjectProtocol] = []

    /// Fired when the running engine died under live sinks (interruption
    /// ended, config change stopped it). The owner restarts its consumers;
    /// that is the one restart path (unsubscribe tears down, prepare rebuilds).
    var onLost: (() -> Void)?

    private func lost(_ why: String) {
        guard box.count > 0, !isRunning else { return }
        Log.audio.error("[audio] engine lost (\(why, privacy: .public)) sinks=\(self.box.count) -> owner restart")
        onLost?()
    }

    /// Engine config changes and interruption ends trigger `onLost`;
    /// route changes are logged only.
    private init() {
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: .AVAudioEngineConfigurationChange,
                                        object: nil, queue: .main) { [weak self] _ in
            Log.audio.error("[audio] engine configuration change \(Log.route(), privacy: .public)")
            MainActor.assumeIsolated { self?.lost("config change") }
        })
        #if os(iOS)
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification,
                                        object: nil, queue: .main) { n in
            let r = (n.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt) ?? 99
            Log.audio.info("[audio] route change reason=\(r) \(Log.route(), privacy: .public)")
        })
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification,
                                        object: nil, queue: .main) { [weak self] n in
            let t = (n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) ?? 99
            Log.audio.error("[audio] interruption type=\(t) (1=began 0=ended)")
            if t == AVAudioSession.InterruptionType.ended.rawValue {
                MainActor.assumeIsolated { self?.lost("interruption ended") }
            }
        })
        #endif
    }

    /// Attaches a sink. Engine must be running via prepare() first.
    func subscribe(_ sink: @escaping @Sendable (AVAudioPCMBuffer) -> Void) -> UUID {
        let id = box.add(sink)
        Log.audio.info("[audio] subscribe sinks=\(self.box.count) running=\(self.isRunning)")
        return id
    }

    /// Detaches a sink; stops and tears down the engine when none remain.
    func unsubscribe(_ id: UUID) {
        box.remove(id)
        Log.audio.info("[audio] unsubscribe sinks=\(self.box.count)")
        guard box.count == 0 else { return }
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        #if os(iOS)
        // Session owner: deactivate only when nobody uses audio (no sink,
        // no prepare in flight, no TTS playing through the same session).
        if preparing == 0, !SpeechBus.shared.isSpeaking {
            do {
                try AVAudioSession.sharedInstance().setActive(false)
            } catch {
                Log.audio.error("[audio] setActive(false) failed: \(error.localizedDescription, privacy: .public)")
            }
        } else {
            Log.audio.info("[audio] session kept active (preparing=\(self.preparing) tts=\(SpeechBus.shared.isSpeaking))")
        }
        #endif
        Log.audio.info("[audio] engine torn down (tap removed, stopped)")
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
