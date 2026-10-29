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
    /// RMS of the latest tap buffer (0 when the engine is down); read by the
    /// voice edge glow, which smooths it. Comes from the one existing tap.
    nonisolated var level: Float { box.level }

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
        guard !switching else {  // our own speech-route switch, handled in endSpeech()
            Log.audio.info("[audio] engine change during speech-route switch (\(why, privacy: .public)) ignored")
            return
        }
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
            MainActor.assumeIsolated {
                self?.diag.configChanges += 1
                self?.lost("config change")
            }
        })
        #if os(iOS)
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification,
                                        object: nil, queue: .main) { [weak self] n in
            let r = (n.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt) ?? 99
            Log.audio.info("[audio] route change reason=\(r) \(Log.route(), privacy: .public)")
            MainActor.assumeIsolated {
                self?.diag.routeChanges += 1
                self?.routeWaiter?()
            }
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

    // MARK: - Speech output route (a2dp_while_speaking)

    private var switching = false
    private var speechRouteOn = false
    private var routeWaiter: (() -> Void)?
    private var speechEndedAt: Date?

    /// Glasses connected over HFP and the A2DP-while-speaking mode is on.
    var speechSwitchApplies: Bool {
        #if os(iOS)
        return Recorder.outputMode == "a2dp_while_speaking"
            && AVAudioSession.sharedInstance().currentRoute.outputs.contains { $0.portType == .bluetoothHFP }
        #else
        return false
        #endif
    }

    /// Before the agent speaks: drop the mic engine (the live link streams
    /// silence on its own timer meanwhile), move output to A2DP, and return
    /// only once the route is there (plus a short settle) so the first
    /// syllable isn't clipped. No-op unless `speechSwitchApplies`.
    func beginSpeech() async {
        #if os(iOS)
        guard speechSwitchApplies, !speechRouteOn else { return }
        let t0 = Date()
        switching = true
        defer { switching = false }
        tearDownEngine()
        do {
            try Recorder.configureSpeechSession()
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            Log.audio.error("[audio] speech route a2dp failed: \(error.localizedDescription, privacy: .public)")
            return
        }
        speechRouteOn = true
        let ok = await waitForRoute(timeout: 1.5) { $0.outputs.contains { $0.portType == .bluetoothA2DP } }
        try? await Task.sleep(for: .milliseconds(150))
        Log.audio.info("[audio] speech route a2dp \(ok ? "ready" : "NOT ready (timeout)", privacy: .public) in \(Int(Date().timeIntervalSince(t0) * 1000))ms \(Log.route(), privacy: .public)")
        #endif
    }

    /// After the agent speaks: back to HFP, rebuild the mic engine for the
    /// live sink (its rate may change) and only then return, so the caller
    /// unmutes and shows listening when the mic really hears again.
    func endSpeech() async {
        #if os(iOS)
        guard speechRouteOn else { return }
        let t0 = speechEndedAt ?? Date()
        switching = true
        // "Finished" fires when the synthesizer hands off its last audio; on
        // A2DP that audio still sits in the Bluetooth output (150-300 ms), and
        // switching the route now cuts the reply's tail. Let it drain first.
        let session = AVAudioSession.sharedInstance()
        let drain = session.outputLatency + session.ioBufferDuration + 0.25
        try? await Task.sleep(for: .seconds(drain))
        Log.audio.info("[audio] speech route drained \(Int(drain * 1000))ms (outLat=\(Int(session.outputLatency * 1000))ms) before hfp")
        do {
            try Recorder.configureSession()
            Recorder.ensurePreferredInput()
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            Log.audio.error("[audio] speech route back to hfp failed: \(error.localizedDescription, privacy: .public)")
        }
        let ok = await waitForRoute(timeout: 2) { $0.inputs.contains { $0.portType == .bluetoothHFP } }
        try? await Task.sleep(for: .milliseconds(150))
        speechRouteOn = false
        switching = false
        if box.count > 0 {
            _ = try? await prepare()
            onLost?()  // the owner re-attaches its sinks to the rebuilt engine
        }
        speechEndedAt = nil
        Log.audio.info("[audio] speech route hfp \(ok ? "back" : "NOT back (timeout)", privacy: .public) mic=\(self.isRunning) in \(Int(Date().timeIntervalSince(t0) * 1000))ms since speech end \(Log.route(), privacy: .public)")
        #endif
    }

    #if os(iOS)
    /// Waits until the current route passes `ready` (checked now and on each
    /// route change) or the timeout.
    private func waitForRoute(timeout: Double, _ ready: @escaping (AVAudioSessionRouteDescription) -> Bool) async -> Bool {
        if ready(AVAudioSession.sharedInstance().currentRoute) { return true }
        return await withCheckedContinuation { (k: CheckedContinuation<Bool, Never>) in
            var done = false
            let finish: (Bool) -> Void = { [weak self] v in
                guard !done else { return }
                done = true
                self?.routeWaiter = nil
                k.resume(returning: v)
            }
            routeWaiter = { if ready(AVAudioSession.sharedInstance().currentRoute) { finish(true) } }
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(timeout))
                finish(ready(AVAudioSession.sharedInstance().currentRoute))
            }
        }
    }
    #endif

    private func tearDownEngine() {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        hwRate = 0
        box.clearLevel()
    }

    // MARK: - TTS diagnostics

    /// What runs while the agent speaks: logged at start and end so a rough
    /// or broken voice can be told apart (HFP call quality vs a starved
    /// main thread vs route/engine churn mid-reply).
    struct SpeechDiag {
        var start: Date?
        var routeChanges = 0
        var configChanges = 0
        var hitches = 0
        var worstHitchMs = 0
        var timer: Timer?
        var lastTick = Date()
    }

    private var diag = SpeechDiag()

    func speechStarted(liveMuted: Bool?) {
        #if os(iOS)
        let s = AVAudioSession.sharedInstance()
        diag.timer?.invalidate()
        diag = SpeechDiag(start: Date())
        diag.lastTick = Date()
        // main-thread watch: a 20 ms tick that arrives > 50 ms late is a hitch
        diag.timer = Timer.scheduledTimer(withTimeInterval: 0.02, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                let late = Date().timeIntervalSince(self.diag.lastTick) - 0.02
                if late > 0.05 {
                    self.diag.hitches += 1
                    self.diag.worstHitchMs = max(self.diag.worstHitchMs, Int(late * 1000))
                }
                self.diag.lastTick = Date()
            }
        }
        Log.audio.info("[audio] tts start opts=\(s.categoryOptions.rawValue) rate=\(s.sampleRate) io=\(Int(s.ioBufferDuration * 1000))ms outLat=\(Int(s.outputLatency * 1000))ms engine=\(self.isRunning) sinks=\(self.box.count) liveMuted=\(liveMuted.map { "\($0)" } ?? "no live", privacy: .public) mode=\(Recorder.outputMode, privacy: .public)/\(Recorder.sessionMode, privacy: .public) \(Log.route(), privacy: .public)")
        #endif
    }

    func speechFinished() {
        speechEndedAt = Date()
        diag.timer?.invalidate()
        let ms = diag.start.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1
        Log.audio.info("[audio] tts end \(ms)ms routeChanges=\(self.diag.routeChanges) engineChanges=\(self.diag.configChanges) mainHitches=\(self.diag.hitches) worst=\(self.diag.worstHitchMs)ms \(Log.route(), privacy: .public)")
        diag = SpeechDiag()
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
        box.clearLevel()
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

    nonisolated(unsafe) private var rms: Float = 0
    nonisolated var level: Float { lock.withLock { rms } }
    nonisolated func clearLevel() { lock.withLock { rms = 0 } }

    nonisolated func consume(_ buffer: AVAudioPCMBuffer) {
        var sum: Float = 0
        let n = Int(buffer.frameLength)
        if let ch = buffer.floatChannelData, n > 0 {
            for i in 0..<n { sum += ch[0][i] * ch[0][i] }
        }
        let r = n > 0 ? (sum / Float(n)).squareRoot() : 0
        lock.withLock {
            rms = r
            for s in sinks.values { s(buffer) }
        }
    }
}
