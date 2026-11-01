// VisionWells.swift
// Bench "Watching": the glasses stream cut into chunks for the server's well judging
// (server/lab_sam_live.py), and the app's side of what comes back. The engine here is
// shared with Diagnostics' SAM well test (SamWellLive.swift). Aiming is Apple Vision;
// which well is the server's. Private runs never start any of this.

import AVFoundation
import CoreImage
import os
import SwiftUI
import Vision

// MARK: - Server

enum VisionAPI {
    /// A session that reports to a Bench run.
    struct BenchLink { let runId: String; let step: Int }

    private static func url(_ path: String) -> URL { Config.baseURL.appending(path: "lab/sam-wells/" + path) }

    static func send<T: Decodable>(_ req: URLRequest) async throws -> T {
        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw NSError(domain: "SamWell", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: String(data: data, encoding: .utf8) ?? "HTTP \(http.statusCode)"])
        }
        let dec = JSONDecoder()
        dec.keyDecodingStrategy = .convertFromSnakeCase
        return try dec.decode(T.self, from: data)
    }

    struct Live: Decodable {
        struct Well: Decodable {
            let well: String
            let frame: Int
            let f0: Int
            let confidence: String
        }
        struct Summary: Decodable {
            let sure: Int
            let notSure: Int
            let hit: [String]
            let outside: [String]
            let missed: [String]
            let toCatch: [String]
        }
        struct Chunk: Decodable {
            let n: Int
            let status: String
            let why: String?
            let error: String?
            let timing: [String: Double]?
        }
        struct Reference: Decodable {
            let chunk: String
            let frame: Int
            let wells: Int
        }
        let id: String
        let wells: [Well]
        let targets: [String]
        let summary: Summary
        let done: Bool
        let chunks: [Chunk]
        let references: [Reference]
    }

    static func newLive(a1: String, pointer: String, targets: String, bench: BenchLink? = nil) async throws -> Live {
        var req = URLRequest(url: url("live"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["a1": a1, "pointer": pointer, "targets": targets]
        if let bench {
            body["run_id"] = bench.runId
            body["step"] = bench.step
            body["started_at"] = Date.now.timeIntervalSince1970
        }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(req)
    }

    static func live(_ id: String) async throws -> Live { try await send(URLRequest(url: url("live/\(id)"))) }

    static func stopLive(_ id: String) async throws -> Live {
        var req = URLRequest(url: url("live/\(id)/stop"))
        req.httpMethod = "POST"
        return try await send(req)
    }

    /// The frames at Record; the server picks the session's one reference from them.
    static func uploadStills(live id: String, stills: [(frame: Int, jpeg: Data)]) async throws {
        struct Out: Decodable { let frames: [Int] }
        let boundary = "----samwell\(UUID().uuidString)"
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"numbers\"\r\n\r\n\(stills.map { String($0.frame) }.joined(separator: ","))\r\n".utf8)
        for s in stills {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"frames\"; filename=\"\(s.frame).jpg\"\r\n".utf8))
            body.append(Data("Content-Type: image/jpeg\r\n\r\n".utf8))
            body.append(s.jpeg)
            body.append(Data("\r\n".utf8))
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        var req = URLRequest(url: url("live/\(id)/reference"))
        req.httpMethod = "POST"
        req.timeoutInterval = 30
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let _: Out = try await send(req)
    }

    static func uploadChunk(live id: String, n: Int, start: Int, clip: URL) async throws {
        struct Out: Decodable { let n: Int }
        let boundary = "----samwell\(UUID().uuidString)"
        var body = Data()
        for (k, v) in [("n", "\(n)"), ("start", "\(start)")] {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(k)\"\r\n\r\n\(v)\r\n".utf8))
        }
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"clip\"; filename=\"chunk.mp4\"\r\n".utf8))
        body.append(Data("Content-Type: application/octet-stream\r\n\r\n".utf8))
        body.append(try Data(contentsOf: clip))
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var req = URLRequest(url: url("live/\(id)/chunks"))
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let _: Out = try await send(req)
    }

}

func cornerName(_ c: String) -> String {
    ["tl": "Top left", "tr": "Top right", "br": "Bottom right", "bl": "Bottom left"][c] ?? c
}

// MARK: - Aiming

/// Plate seen / gone from the detector's yes/no over time. Entered after 0.5 s
/// in a row, lost after 2 s in a row; `first` is the page's first entry.
nonisolated struct PlateAimState {
    enum Event: Equatable { case entered(first: Bool), lost }

    private(set) var inView = false
    private var announced = false
    private var seenSince: Double?
    private var goneSince: Double?

    mutating func update(seen: Bool, at t: Double) -> Event? {
        if seen {
            goneSince = nil
            guard !inView else { return nil }
            let since = seenSince ?? t
            seenSince = since
            guard t - since >= 0.5 else { return nil }
            inView = true
            defer { announced = true }
            return .entered(first: !announced)
        }
        seenSince = nil
        guard inView else { return nil }
        let since = goneSince ?? t
        goneSince = since
        guard t - since >= 2 else { return nil }
        inView = false
        goneSince = nil
        return .lost
    }
}

/// Looks for a plate-shaped rectangle in the stream, about 4 times a second,
/// off the stream's thread. `found` gets the corners (0-1, top left origin), or nil.
final class PlateFinder: @unchecked Sendable {
    private let queue = DispatchQueue(label: "samwell.aim", qos: .userInitiated)
    private let lock = NSLock()
    private var busy = false
    private var last = 0.0
    private let found: @Sendable ([CGPoint]?) -> Void

    init(found: @escaping @Sendable ([CGPoint]?) -> Void) { self.found = found }

    func frame(_ sb: CMSampleBuffer) {
        let now = CACurrentMediaTime()
        let go = lock.withLock {
            guard !busy, now - last >= 0.25 else { return false }
            busy = true
            last = now
            return true
        }
        guard go, let pb = CMSampleBufferGetImageBuffer(sb) else {
            if go { lock.withLock { busy = false } }
            return
        }
        nonisolated(unsafe) let buffer = pb
        queue.async { [self] in
            let quad = Self.detect(buffer)
            lock.withLock { busy = false }
            found(quad)
        }
    }

    private static func detect(_ pb: CVPixelBuffer) -> [CGPoint]? {
        let request = VNDetectRectanglesRequest()
        // a plate is 2:3; tilted it reads narrower, a little wider from the side
        request.minimumAspectRatio = 0.45
        request.maximumAspectRatio = 0.9
        request.minimumSize = 0.15
        request.quadratureTolerance = 30
        request.minimumConfidence = 0.5
        request.maximumObservations = 0
        do {
            try VNImageRequestHandler(cvPixelBuffer: pb, orientation: .up).perform([request])
        } catch {
            Log.view.error("[samwell] rectangles failed: \(error.localizedDescription, privacy: .public)")
            return nil
        }
        // shortcut: the largest rectangle, a sheet of paper can pass for a plate; aiming only, SAM judges
        let best = request.results?.max { area($0) < area($1) }
        return best.map { o in [o.topLeft, o.topRight, o.bottomRight, o.bottomLeft].map { CGPoint(x: $0.x, y: 1 - $0.y) } }
    }

    private static func area(_ o: VNRectangleObservation) -> Double {
        let q = [o.topLeft, o.topRight, o.bottomRight, o.bottomLeft]
        return abs(zip(q, q[1...] + q[..<1]).reduce(0) { $0 + $1.0.x * $1.1.y - $1.1.x * $1.0.y }) / 2
    }
}

// MARK: - Chunks

/// Cuts the stream into overlapping chunks. Frames arrive on the SDK's thread.
final class WellChunks: @unchecked Sendable {
    struct Chunk { let n: Int; let start: Int; let url: URL }

    /// What the stream must stay at: fewer than this, over `watchSeconds`, is a downgrade.
    nonisolated static let wantedSize = "720x1280"
    nonisolated static let minFPS = 5
    nonisolated static let watchSeconds = 3

    /// 10 s with 1 s shared with the next.
    static let seconds = 10
    static let overlap = 1

    private struct Open {
        let n: Int
        let start: Int
        let writer: AVAssetWriter
        let input: AVAssetWriterInput
        let adaptor: AVAssetWriterInputPixelBufferAdaptor
        var written = 0
    }

    private let queue = DispatchQueue(label: "samwell.chunks")
    private let fps: Int
    private let closed: @Sendable (Chunk) -> Void
    private let stillsReady: @Sendable ([(frame: Int, jpeg: Data)]) -> Void
    private var stills: [(frame: Int, jpeg: Data)] = []
    private let images = CIContext()
    private var open: [Open] = []
    private var arrivals: [Double] = []
    private var stopped = false
    // what the stream really delivered, as the attempts page's "Last clip"
    private var sizes = Set<String>()
    private var refused = 0
    private var worstGap = 0.0

    init(fps: Int, stillsReady: @escaping @Sendable ([(frame: Int, jpeg: Data)]) -> Void,
         closed: @escaping @Sendable (Chunk) -> Void) {
        self.fps = fps
        self.stillsReady = stillsReady
        self.closed = closed
    }

    private var length: Int { Self.seconds * fps }
    private var step: Int { (Self.seconds - Self.overlap) * fps }
    /// Three frames over about the first second: the server picks the plate's reference from them.
    private var stillFrames: [Int] { [0, fps * 3 / 7, fps * 6 / 7] }

    func frame(_ sb: CMSampleBuffer) {
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        queue.sync {
            guard !stopped else { return }
            let frame = arrivals.count
            let now = CACurrentMediaTime()
            if let last = arrivals.last { worstGap = max(worstGap, now - last) }
            arrivals.append(now)
            sizes.insert("\(CVPixelBufferGetWidth(pb))x\(CVPixelBufferGetHeight(pb))")
            if stillFrames.contains(frame),
               let jpeg = images.jpegRepresentation(of: CIImage(cvPixelBuffer: pb), colorSpace: CGColorSpaceCreateDeviceRGB()) {
                stills.append((frame, jpeg))
                if stills.count == stillFrames.count { stillsReady(stills) }
            }
            if frame % step == 0 {
                let url = FileManager.default.temporaryDirectory.appending(path: "samwell-chunk-\(UUID().uuidString).mp4")
                if let (w, i, a) = Self.writer(url: url, width: CVPixelBufferGetWidth(pb), height: CVPixelBufferGetHeight(pb), fps: fps) {
                    open.append(Open(n: frame / step, start: frame, writer: w, input: i, adaptor: a))
                }
            }
            for k in open.indices {
                // shortcut: a refused frame shifts the rest of its chunk one frame early, fine unless "refused" shows up in the log
                let pts = CMTime(value: CMTimeValue(open[k].written), timescale: CMTimeScale(fps))
                if open[k].input.isReadyForMoreMediaData, open[k].adaptor.append(pb, withPresentationTime: pts) {
                    open[k].written += 1
                } else {
                    refused += 1
                }
            }
            let full = open.filter { frame + 1 - $0.start >= length }
            open.removeAll { frame + 1 - $0.start >= length }
            full.forEach(close)
        }
    }

    private func close(_ c: Open) {
        c.input.markAsFinished()
        let chunk = Chunk(n: c.n, start: c.start, url: c.writer.outputURL)
        let closed = closed
        c.writer.finishWriting { [writer = c.writer] in
            if writer.status == .completed { closed(chunk) }
        }
    }

    /// No more frames. What's open closes, but a tail the chunk before already holds doesn't.
    func stop() {
        queue.sync {
            stopped = true
            for c in open {
                if c.written < 2 || (c.n > 0 && c.written <= Self.overlap * fps) {
                    c.writer.cancelWriting()
                } else {
                    close(c)
                }
            }
            open = []
        }
    }

    /// Why this stream is below what the wells need, or nil. Watched once it has run `watchSeconds`.
    func downgrade(at now: Double) -> String? {
        queue.sync {
            if let other = sizes.first(where: { $0 != Self.wantedSize }) { return "size \(other)" }
            guard let first = arrivals.first, now - first >= Double(Self.watchSeconds) else { return nil }
            let got = arrivals.reversed().prefix { now - $0 <= Double(Self.watchSeconds) }.count
            return got < Self.minFPS * Self.watchSeconds ? "\(got) frames in \(Self.watchSeconds) s" : nil
        }
    }

    /// One line, as the attempts page's "Last clip". The link is whatever's on the route now.
    func report() -> String {
        let hfp = AVAudioSession.sharedInstance().currentRoute.inputs.contains { $0.portType == .bluetoothHFP }
        return queue.sync {
            let span = (arrivals.last ?? 0) - (arrivals.first ?? 0)
            let rate = Double(max(1, arrivals.count) - 1) / max(0.001, span)
            return "\(sizes.sorted().joined(separator: ", ")) · HFP \(hfp ? "on" : "off") · \(String(format: "%.2f", rate)) fps of \(fps) "
                + "· \(arrivals.count) frames over \(String(format: "%.1f", span)) s · \(refused) refused · worst gap \(Int(worstGap * 1000)) ms"
        }
    }

    /// A started H.264 .mp4 writer from time zero, or nil.
    static func writer(url: URL, width: Int, height: Int, fps: Int)
        -> (AVAssetWriter, AVAssetWriterInput, AVAssetWriterInputPixelBufferAdaptor)? {
        guard let w = try? AVAssetWriter(outputURL: url, fileType: .mp4) else { return nil }
        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
            // ~0.6 bits a pixel a frame (3.9 Mbps at 720×1280, 7 fps): the frames come
            // already compressed for Bluetooth, so more adds only size
            // a keyframe a second and no reordering: easy to seek, whichever way SAM reads it
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: max(500_000, width * height * 6 * fps / 10),
                                              AVVideoMaxKeyFrameIntervalKey: fps,
                                              AVVideoAllowFrameReorderingKey: false],
        ]
        let i = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        i.expectsMediaDataInRealTime = true
        let a = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: i, sourcePixelBufferAttributes: nil)
        guard w.canAdd(i) else { return nil }
        w.add(i)
        w.startWriting()
        w.startSession(atSourceTime: .zero)
        return (w, i, a)
    }

    /// When a session frame arrived here.
    func arrival(_ frame: Int) -> Double? {
        queue.sync { frame < arrivals.count ? arrivals[frame] : nil }
    }
}

/// One continuous run. Outlives the page, so leaving it still finishes what's in hand.
@MainActor @Observable
final class SamWellRun {
    enum Phase { case idle, recording, finishing, done }

    struct Seen: Identifiable {
        let id: String
        let well: String
        let sure: Bool
        /// Seconds from the tip stopping at the well to the well showing here.
        let delay: Double?
    }

    private(set) var phase = Phase.idle
    private(set) var seen: [Seen] = []
    private(set) var live: VisionAPI.Live?
    private(set) var sentChunks = 0
    var error: String?

    private var id: String?
    private var chunks: WellChunks?
    private var uploads: [Task<Void, Never>] = []
    private var polling: Task<Void, Never>?

    var waiting: Int { sentChunks - (live?.chunks.filter { $0.status != "analyzing" }.count ?? 0) }

    /// nil when no run is recording.
    var tap: (@Sendable (CMSampleBuffer) -> Void)? {
        guard phase == .recording, let chunks else { return nil }
        return { chunks.frame($0) }
    }

    func start(a1: String, pointer: String, targets: String, fps: Int, bench: VisionAPI.BenchLink? = nil) async {
        guard phase == .idle else { return }
        do {
            let s = try await VisionAPI.newLive(a1: a1, pointer: pointer, targets: targets, bench: bench)
            id = s.id
            live = s
        } catch {
            self.error = error.localizedDescription
            return
        }
        guard let live = id else { return }
        chunks = WellChunks(fps: fps, stillsReady: { stills in
            let copy = stills
            Task { try? await VisionAPI.uploadStills(live: live, stills: copy) }
        }, closed: { chunk in
            Task { @MainActor [weak self] in self?.send(chunk) }
        })
        phase = .recording
        guard bench == nil else { return }  // the Bench hears of wells through the run's snapshot
        polling = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self, await self.poll() else { return }
            }
        }
    }

    private func send(_ chunk: WellChunks.Chunk) {
        guard let id else { return }
        sentChunks += 1
        uploads.append(Task { [weak self] in
            defer { try? FileManager.default.removeItem(at: chunk.url) }
            for attempt in 1...3 {
                do {
                    try await VisionAPI.uploadChunk(live: id, n: chunk.n, start: chunk.start, clip: chunk.url)
                    return
                } catch where attempt < 3 {
                    try? await Task.sleep(for: .seconds(1))
                } catch {
                    Log.dat.error("[samwell] chunk \(chunk.n) upload failed: \(error.localizedDescription, privacy: .public)")
                    self?.sentChunks -= 1
                    self?.error = "Chunk \(chunk.n) didn't upload: \(error.localizedDescription)"
                }
            }
        })
    }

    /// True to keep polling.
    private func poll() async -> Bool {
        guard let id, let s = try? await VisionAPI.live(id) else { return phase != .done }
        take(s)
        return phase != .done
    }

    private func take(_ s: VisionAPI.Live) {
        live = s
        let now = CACurrentMediaTime()
        for w in s.wells where !seen.contains(where: { $0.id == "\(w.well)-\(w.frame)" }) {
            let delay = chunks?.arrival(w.f0).map { now - $0 }
            seen.append(Seen(id: "\(w.well)-\(w.frame)", well: w.well, sure: w.confidence == "sure", delay: delay))
            Log.dat.info("[samwell] live \(w.well, privacy: .public) frame \(w.frame) delay \(delay ?? -1)")
        }
        if s.done, phase == .finishing { phase = .done }
    }

    func downgrade() -> String? { chunks?.downgrade(at: CACurrentMediaTime()) }

    func report() -> String? { chunks?.report() }

    /// Leaves recording; whatever's open uploads, and the server finishes it.
    func stop() {
        guard phase == .recording else { return }
        phase = .finishing
        chunks?.stop()
        Task {
            try? await Task.sleep(for: .seconds(1))  // the last writers report through finishWriting
            for upload in uploads { await upload.value }
            guard let id else { return }
            do { take(try await VisionAPI.stopLive(id)) } catch { self.error = error.localizedDescription }
        }
    }
}

// MARK: - Bench

/// Runs one session per pipetting step: stream, Vision finds the plate, then chunks. Told what's
/// wanted on every change (`update`); anything else (step over, run paused or ended, Private) is nil.
@MainActor @Observable
final class VisionWatcher {
    enum Chip { case off, looking, watching, plateLost }

    struct Want: Equatable {
        let runId: String
        let step: Int
        let a1: String
        let pointer: String
    }

    private(set) var chip = Chip.off
    @ObservationIgnored private var want: Want?
    /// A step whose stream fell below what the wells need: Off until the step changes.
    @ObservationIgnored private var dropped: Want?
    @ObservationIgnored private var run: SamWellRun?
    @ObservationIgnored private var aim = PlateAimState()
    @ObservationIgnored private var finder: PlateFinder?
    @ObservationIgnored private var checking: Task<Void, Never>?
    @ObservationIgnored private weak var dat: GlassesDAT?
    @ObservationIgnored private var gen = 0

    func update(_ new: Want?, dat: GlassesDAT?) {
        guard new != want else { return }
        want = new
        gen += 1
        let g = gen
        finish()
        guard let new, let dat, new != dropped else {
            leave()
            return
        }
        self.dat = dat
        aim = PlateAimState()
        chip = .looking
        Log.dat.info("[watching] step \(new.step) looking for the plate")
        let finder = PlateFinder { [weak self] quad in
            Task { @MainActor in self?.aimed(quad, gen: g) }
        }
        self.finder = finder
        dat.visionTap.set { finder.frame($0) }
        dat.startWatching()
    }

    private func aimed(_ quad: [CGPoint]?, gen g: Int) {
        guard g == gen else { return }
        let event = aim.update(seen: quad != nil, at: CACurrentMediaTime())
        if run == nil {
            if chip == .looking, case .entered = event { Task { await begin(gen: g) } }
        } else if chip != .looking {  // recording goes on either way: a hand over the plate is normal
            if case .entered = event { chip = .watching }
            if event == .lost { chip = .plateLost }
        }
    }

    private func begin(gen g: Int) async {
        guard let want, let dat, let finder, run == nil else { return }
        let run = SamWellRun()
        self.run = run
        await run.start(a1: want.a1, pointer: want.pointer, targets: "", fps: 7,
                        bench: VisionAPI.BenchLink(runId: want.runId, step: want.step))
        guard g == gen else { run.stop(); return }
        guard let tap = run.tap else {
            Log.dat.error("[watching] no session: \(run.error ?? "?", privacy: .public)")
            self.run = nil
            drop()
            return
        }
        dat.visionTap.set { finder.frame($0); tap($0) }  // Vision keeps checking: the chip says if the plate left the view
        chip = aim.inView ? .watching : .plateLost
        Log.dat.info("[watching] step \(want.step) recording")
        checking = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self, !Task.isCancelled else { return }
                if let why = self.run?.downgrade() {
                    Log.dat.error("[watching] stream downgraded: \(why, privacy: .public)")
                    self.drop()
                    return
                }
            }
        }
    }

    /// Below what the wells need: this session ends, and the step stays Off.
    private func drop() {
        dropped = want
        gen += 1
        finish()
        leave()
    }

    private func leave() {
        dat?.visionTap.set(nil)
        dat?.stopWatching()
        finder = nil
        chip = .off
    }

    /// What's recorded uploads and the server finishes it; the report goes to the log and to Files.
    private func finish() {
        checking?.cancel()
        checking = nil
        guard let run else { return }
        self.run = nil
        dat?.visionTap.set(nil)
        if let line = run.report() { Self.note(line) }
        run.stop()
    }

    private static func note(_ line: String) {
        Log.dat.info("[watching] clip \(line, privacy: .public)")
        let url = URL.documentsDirectory.appending(path: "watching-log.txt")
        let text = Data("\(Date.now.formatted(date: .numeric, time: .standard))  \(line)\n".utf8)
        if let file = try? FileHandle(forWritingTo: url) {
            defer { try? file.close() }
            _ = try? file.seekToEnd()
            try? file.write(contentsOf: text)
        } else {
            try? text.write(to: url)
        }
    }
}

/// Says the server's camera lines, one at a time, only while Milo and the person are quiet.
@MainActor
final class VisionNotices {
    /// Said here, and the server told (or being told); never said twice.
    private var said = Set<String>()
    private var telling = Set<String>()
    private var speaking: String?

    /// Called on every change of the queue or of the voice state.
    func step(_ notices: [RecordSnapshot.VisionNotice], voice: VoiceSession) {
        if let id = speaking {
            guard voice.state != .speaking else { return }
            speaking = nil
            said.insert(id)
        }
        for n in notices where said.contains(n.id) && !telling.contains(n.id) {
            telling.insert(n.id)
            Task {
                do { try await PipClient.visionNoticePlayed(n.id) } catch {
                    Log.voice.error("[watching] played \(n.id, privacy: .public): \(error.localizedDescription, privacy: .public)")
                    telling.remove(n.id)  // the next change tries again
                }
            }
        }
        guard voice.state == .ready, voice.canSpeak, let next = notices.first(where: { !said.contains($0.id) }) else { return }
        speaking = next.id
        voice.speak(next.speak)
        if voice.state != .speaking { step(notices, voice: voice) }  // speech off: nothing to wait for
    }
}

// MARK: - Bench views

/// The temporary chip (no Figma yet): is the camera following this step?
struct VisionChip: View {
    let chip: VisionWatcher.Chip
    var u: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var look: (icon: String, label: String, color: Color) {
        switch chip {
        case .watching: ("eye.fill", "Watching", Theme.polishBlue)
        case .looking: ("viewfinder", "Looking for plate", Theme.text2)
        case .plateLost: ("eye.trianglebadge.exclamationmark", "Can't see plate", Theme.text1)
        case .off: ("eye.slash", "Off", Theme.text4)
        }
    }

    var body: some View {
        HStack(spacing: 8 * u) {
            Image(systemName: look.icon).font(Theme.ui(20 * u, weight: .medium))
                .symbolEffect(.pulse, isActive: chip == .watching && !reduce)  // only while it really sees the plate
            Text(look.label).font(Theme.ui(22 * u, weight: .medium)).lineLimit(1)
        }
        .foregroundStyle(look.color)
        .padding(.horizontal, 14 * u).frame(height: 30 * u)
        .background(Capsule().fill(Theme.chip)).onLight()
        .accessibilityElement(children: .combine)
    }
}

/// Asked once when a run starts: where A1 is as the glasses see the plate, and what goes into the wells.
struct VisionSetupSheet: View {
    @Binding var a1: String
    @Binding var pointer: String
    let onDone: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Where is A1?").font(Theme.ui(28, weight: .semibold)).foregroundStyle(Theme.text1)
            Picker("A1 corner, as you see the plate", selection: $a1) {
                ForEach(["tl", "tr", "br", "bl"], id: \.self) { Text(cornerName($0)).tag($0) }
            }
            .pickerStyle(.segmented)
            Picker("What goes into the wells", selection: $pointer) {
                Text("Pipette tip").tag("pipette tip")
                Text("Pen").tag("pen")
            }
            .pickerStyle(.segmented)
            Button("Done", action: onDone).buttonStyle(.borderedProminent)
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(32)
        .presentationDetents([.height(280)])
    }
}
