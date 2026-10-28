import AVFoundation

/// Hold-to-talk recorder. Always writes 16kHz mono 16-bit WAV (what /voice wants).
/// The tap only collects raw Float32 samples; the resample happens once in stop(),
/// because per-tap resampling drops samples inside the converter.
final class Recorder {
    private var sinkID: UUID?
    private var box: SampleBox?
    private var hwRate: Double?

    #if os(iOS)
    static func requestPermission() async -> Bool {
        await withCheckedContinuation { cont in
            AVAudioSession.sharedInstance().requestRecordPermission { cont.resume(returning: $0) }
        }
    }

    /// A2DP output when on. Warning: with A2DP the mic may stay on the iPad.
    static var a2dpEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "pip.a2dp") as? Bool ?? false }
        set { UserDefaults.standard.set(newValue, forKey: "pip.a2dp") }
    }

    /// "auto" (prefer glasses HFP) or an input port UID.
    static var preferredInputUID: String {
        get { UserDefaults.standard.string(forKey: "pip.preferredInput") ?? "auto" }
        set { UserDefaults.standard.set(newValue, forKey: "pip.preferredInput") }
    }

    static func configureSession() throws {
        var opts: AVAudioSession.CategoryOptions = [.allowBluetoothHFP]
        if a2dpEnabled { opts.insert(.allowBluetoothA2DP) }
        try AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .default, options: opts)
    }

    static func hfpInput() -> AVAudioSessionPortDescription? {
        AVAudioSession.sharedInstance().availableInputs?.first { $0.portType == .bluetoothHFP }
    }

    /// Applies the stored input choice. Safe on every route change: asking
    /// for the already-active input is a no-op. Returns true when it asked
    /// for a switch (the caller should let the route settle before tapping).
    @discardableResult
    static func ensurePreferredInput() -> Bool {
        let session = AVAudioSession.sharedInstance()
        guard let inputs = session.availableInputs else { return false }
        let uid = preferredInputUID
        let want: AVAudioSessionPortDescription? =
            uid == "auto" ? inputs.first { $0.portType == .bluetoothHFP }
                           : inputs.first { $0.uid == uid }
        let current = session.preferredInput ?? session.currentRoute.inputs.first
        guard want?.uid != current?.uid else { return false }
        try? session.setPreferredInput(want)
        return true
    }
    #endif

    var currentPeak: Float { box?.peak ?? 0 }

    func start() async throws {
        let rate = try await MicEngine.shared.prepare()
        let box = SampleBox()
        let sink = Unchecked(value: box) // tap thread appends; stop() unsubscribes first, then reads
        sinkID = MicEngine.shared.subscribe { buffer in
            guard let ch = buffer.floatChannelData else { return }
            let box = sink.value
            if box.samples.count < 16_000_000 { // ~5min at 48kHz; commands are seconds
                box.append(UnsafeBufferPointer(start: ch[0], count: Int(buffer.frameLength)))
            }
        }
        self.box = box
        self.hwRate = rate
    }

    /// Stops recording and writes the collected audio as 16kHz mono 16-bit WAV. Returns its URL.
    func stop() throws -> URL {
        if let id = sinkID { MicEngine.shared.unsubscribe(id) }
        sinkID = nil
        guard let box, let rate = hwRate else { throw RecorderError.notRecording }
        self.box = nil
        self.hwRate = nil
        guard let disk = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true),
              let srcFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false)
        else { throw RecorderError.cannotConvert }
        let url = FileManager.default.temporaryDirectory.appending(path: "pip-\(UUID().uuidString).wav")
        let file = try AVAudioFile(forWriting: url, settings: disk.settings)
        // Writes must use the file's processingFormat (Float32 here) or write() traps.
        let proc = file.processingFormat
        guard proc.sampleRate == 16000, proc.channelCount == 1,
              let converter = AVAudioConverter(from: srcFormat, to: proc)
        else { throw RecorderError.cannotConvert }
        let n = box.samples.count
        if n > 0 {
            guard let src = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: AVAudioFrameCount(n)) else {
                throw RecorderError.cannotConvert
            }
            src.frameLength = AVAudioFrameCount(n)
            box.samples.withUnsafeBufferPointer { src.floatChannelData![0].update(from: $0.baseAddress!, count: n) }
            try Self.convertAll(src, with: converter, to: proc, into: file)
        }
        return url
    }

    /// Converts one buffer end to end (single supply + endOfStream flush, so nothing is lost).
    nonisolated static func convertAll(
        _ src: AVAudioPCMBuffer, with converter: AVAudioConverter, to proc: AVAudioFormat, into file: AVAudioFile
    ) throws {
        var fed = false
        var status: AVAudioConverterOutputStatus = .haveData
        while status == .haveData {
            let need = AVAudioFrameCount(Double(src.frameLength) * proc.sampleRate / src.format.sampleRate) + 4096
            guard let out = AVAudioPCMBuffer(pcmFormat: proc, frameCapacity: need) else {
                throw RecorderError.cannotConvert
            }
            var err: NSError?
            status = converter.convert(to: out, error: &err) { _, st in
                if fed { st.pointee = .endOfStream; return nil }
                fed = true
                st.pointee = .haveData
                return src
            }
            if let err { throw err }
            if out.frameLength > 0 { try file.write(from: out) }
        }
    }

    /// Clip length in seconds, or nil when the file is unreadable.
    static func duration(of url: URL) -> Double? {
        guard let f = try? AVAudioFile(forReading: url), f.fileFormat.sampleRate > 0 else { return nil }
        return Double(f.length) / f.fileFormat.sampleRate
    }

    /// Clip length from WAV bytes (standard 44-byte header), or nil when malformed.
    static func duration(of wav: Data) -> Double? {
        guard wav.count >= 44 else { return nil }
        let byteRate = UInt32(wav[28]) | UInt32(wav[29]) << 8
            | UInt32(wav[30]) << 16 | UInt32(wav[31]) << 24
        guard byteRate > 0 else { return nil }
        return Double(wav.count - 44) / Double(byteRate)
    }

    /// Loudness 0...1, or nil when unreadable. Exact 0 means digital silence.
    static func rms(of url: URL) -> Float? {
        guard let f = try? AVAudioFile(forReading: url), f.length > 0,
              let buf = AVAudioPCMBuffer(pcmFormat: f.processingFormat, frameCapacity: AVAudioFrameCount(f.length))
        else { return nil }
        do { try f.read(into: buf) } catch { return nil }
        return rms(of: buf)
    }

    static func rms(of buf: AVAudioPCMBuffer) -> Float? {
        guard buf.frameLength > 0 else { return nil }
        if let ch = buf.floatChannelData {
            var sum: Double = 0
            for i in 0..<Int(buf.frameLength) { let s = Double(ch[0][i]); sum += s * s }
            return Float(sqrt(sum / Double(buf.frameLength)))
        }
        if let ch = buf.int16ChannelData {
            var sum: Double = 0
            for i in 0..<Int(buf.frameLength) { let s = Double(ch[0][i]) / 32768; sum += s * s }
            return Float(sqrt(sum / Double(buf.frameLength)))
        }
        return nil
    }
}

enum RecorderError: Error, LocalizedError {
    case noInput
    case notRecording
    case cannotConvert
    var errorDescription: String? {
        switch self {
        case .noInput: "No mic input (on the simulator, check the Mac mic permission and input)"
        case .notRecording: "Not recording"
        case .cannotConvert: "Couldn't prepare the audio conversion"
        }
    }
}

/// Sample accumulator. `samples` only on the tap thread while recording;
/// `peak` is lock-guarded because the level meter reads it from the UI.
final class SampleBox {
    nonisolated(unsafe) var samples: [Float] = []
    private let peakLock = NSLock()
    nonisolated(unsafe) private var _peak: Float = 0
    nonisolated var peak: Float { peakLock.withLock { _peak } }

    nonisolated func append(_ ptr: UnsafeBufferPointer<Float>) {
        samples.append(contentsOf: ptr)
        var m: Float = 0
        for s in ptr { let a = abs(s); if a > m { m = a } }
        peakLock.withLock { _peak = m }
    }
}

/// Sendable box for values the audio tap hands to a nonisolated function.
struct Unchecked<T>: @unchecked Sendable {
    nonisolated(unsafe) let value: T
}
