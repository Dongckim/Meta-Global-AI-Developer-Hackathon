// SamWellTest.swift
// Diagnostics "SAM well test" (DEBUG, tap only: no registry, record or voice).
// A session is 5 attempts: random target wells, a glasses clip of the tip
// going into them (or a clip from Files), the server's SAM analysis
// (server/lab_sam.py) judged against the targets, then a summary.

#if DEBUG
import AVFoundation
import MWDATCamera
import os
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Server

enum SamWellAPI {
    struct SessionRow: Decodable, Identifiable {
        let id: String
        let created: Double
        let a1: String
        let attempts: Int
        let sources: [String]
        let sizes: [String]
        let accuracy: Double?
        let hits: Int
        let total: Int
    }

    struct Session: Decodable {
        let id: String
        let created: Double
        let a1: String
        /// What goes into the wells: "pipette tip" or "pen" (nil: sessions before the choice).
        let pointer: String?
        let attempts: [Attempt]
        let summary: Summary?
    }

    struct Attempt: Decodable, Identifiable {
        let n: Int
        let targets: [String]
        let source: String
        let filename: String?
        let status: String
        let error: String?
        let result: Result?
        let judge: Judge?
        /// While analysing: the stage, how far (frames), seconds so far.
        let progress: Progress?
        var id: Int { n }
    }

    struct Result: Decodable {
        let w: Int
        let h: Int
        let fps: Double
        let frames: Int
        let durationS: Double
        let found: [Found]
        let wellPx: Double
        let failedRatio: Double
        let fails: [String: Int]
        let timing: [String: Double]
        let images: [String]
        let rounds: Int
    }

    struct Progress: Decodable {
        let stage: String
        let done: Int?
        let total: Int?
        let elapsedS: Double
    }

    struct Found: Decodable {
        let well: String
        let rep: Int
    }

    struct Judge: Decodable {
        struct Well: Decodable { let well: String; let ok: Bool }
        let wells: [Well]
        let hits: Int
        let total: Int
        let orderOk: Bool
        let falseWells: [String]
        let line: String
        enum CodingKeys: String, CodingKey { case wells, hits, total, orderOk, falseWells = "false", line }
    }

    struct Summary: Decodable {
        let attempts: Int
        let complete: Bool
        let hits: Int
        let total: Int
        let accuracy: Double
        let inOrder: Int
        let falseCount: Int
        let meanAnalysisS: Double
        let sizes: [String]
        let wellPx: Double
        let failedRatio: Double
        let settings: Settings
        enum CodingKeys: String, CodingKey {
            case attempts, complete, hits, total, accuracy, inOrder, falseCount = "false", meanAnalysisS, sizes,
                 wellPx, failedRatio, settings
        }
    }

    struct Settings: Decodable {
        struct Row: Decodable {
            let dwellS: Double
            let dwellMm: Double
            let hits: Int
            let total: Int
            let falseCount: Int
            let inOrder: Int
            enum CodingKeys: String, CodingKey { case dwellS, dwellMm, hits, total, falseCount = "false", inOrder }
        }
        let rows: [Row]
        let best: Int
    }

    private static func url(_ path: String) -> URL { Config.baseURL.appending(path: "lab/sam-wells/" + path) }

    private static func send<T: Decodable>(_ req: URLRequest) async throws -> T {
        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw NSError(domain: "SamWell", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: String(data: data, encoding: .utf8) ?? "HTTP \(http.statusCode)"])
        }
        let dec = JSONDecoder()
        dec.keyDecodingStrategy = .convertFromSnakeCase
        return try dec.decode(T.self, from: data)
    }

    static func targets() async throws -> [String] {
        struct Out: Decodable { let targets: [String] }
        let out: Out = try await send(URLRequest(url: url("targets")))
        return out.targets
    }

    static func sessions() async throws -> [SessionRow] { try await send(URLRequest(url: url("sessions"))) }

    static func session(_ id: String) async throws -> Session { try await send(URLRequest(url: url("sessions/\(id)"))) }

    static func newSession(a1: String, pointer: String) async throws -> Session {
        var req = URLRequest(url: url("sessions"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["a1": a1, "pointer": pointer])
        return try await send(req)
    }

    static func upload(session: String, clip: URL, targets: [String], source: String) async throws {
        let boundary = "----samwell\(UUID().uuidString)"
        var body = Data()
        for (k, v) in [("targets", targets.joined(separator: ",")), ("source", source)] {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(k)\"\r\n\r\n\(v)\r\n".utf8))
        }
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"clip\"; filename=\"\(clip.lastPathComponent)\"\r\n".utf8))
        body.append(Data("Content-Type: application/octet-stream\r\n\r\n".utf8))
        body.append(try Data(contentsOf: clip))
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var req = URLRequest(url: url("sessions/\(session)/attempts"))
        req.httpMethod = "POST"
        req.timeoutInterval = 300
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let _: Attempt = try await send(req)
    }

    static func imageURL(session: String, attempt: Int, name: String) -> URL {
        url("sessions/\(session)/attempts/\(attempt)/images/\(name)")
    }
}

// MARK: - Glasses frames: rate meter and clip writer

/// Counts stream frames (size, fps) and, while recording, writes them to an
/// H.264 .mp4 at full size. Frames arrive on the SDK's thread.
final class WellCam: @unchecked Sendable {
    struct Clip { let url: URL; let width: Int; let height: Int; let fps: Double; let report: String }

    private let queue = DispatchQueue(label: "samwell.cam")
    private var size = (0, 0)
    private var stamps: [Double] = []
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var first: CMTime?
    private var last: CMTime?
    private var written = 0
    private var recording = false
    // what the stream really delivered while recording
    private var arrived = 0
    private var refused = 0
    private var late = 0
    private var worstGap = 0.0
    private var firstArrival: Double?
    private var lastArrival: Double?
    private var sizes = Set<String>()

    /// "1280×720 · 14.8 fps" over the last second, or nil before frames.
    var rate: String? {
        queue.sync {
            guard size.0 > 0 else { return nil }
            let now = CACurrentMediaTime()
            let fps = stamps.filter { now - $0 <= 1 }.count
            return "\(size.0)×\(size.1) · \(fps) fps"
        }
    }

    func frame(_ sb: CMSampleBuffer) {
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        queue.sync {
            size = (CVPixelBufferGetWidth(pb), CVPixelBufferGetHeight(pb))
            let now = CACurrentMediaTime()
            stamps.append(now)
            stamps.removeAll { now - $0 > 1 }
            guard recording else { return }
            arrived += 1
            sizes.insert("\(size.0)x\(size.1)")
            if let a = lastArrival {
                worstGap = max(worstGap, now - a)
                if now - a > 1.5 / Double(fps) { late += 1 }
            }
            lastArrival = now
            firstArrival = firstArrival ?? now
            if writer == nil { open(width: size.0, height: size.1) }
            guard let input, let adaptor, input.isReadyForMoreMediaData else { refused += 1; return }
            // frame number x 1/fps, not arrival: a constant frame rate file, however the stream wavers
            let pts = CMTime(value: CMTimeValue(written), timescale: CMTimeScale(fps))
            if adaptor.append(pb, withPresentationTime: pts) {
                written += 1
                last = pts
            } else {
                refused += 1
            }
        }
    }

    private var pendingURL: URL?
    private var fps = 2

    private func open(width: Int, height: Int) {
        guard let url = pendingURL, let w = try? AVAssetWriter(outputURL: url, fileType: .mp4) else { return }
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
        guard w.canAdd(i) else { return }
        w.add(i)
        w.startWriting()
        w.startSession(atSourceTime: .zero)
        (writer, input, adaptor, first) = (w, i, a, .zero)
    }

    /// Starts writing from the next frame to a temp file.
    func start(fps: Int) {
        queue.sync {
            self.fps = fps
            pendingURL = FileManager.default.temporaryDirectory.appending(path: "samwell-\(UUID().uuidString).mp4")
            writer = nil; input = nil; adaptor = nil; first = nil; last = nil; written = 0
            arrived = 0; refused = 0; late = 0; worstGap = 0; firstArrival = nil; lastArrival = nil; sizes = []
            recording = true
        }
    }

    /// Stops; the clip moved into Documents (Files app) named with its size
    /// and frame rate, or nil when no frame came.
    func stop() async -> Clip? {
        let (w, i, n, began, ended, sz, got, no, slow, gap, seen, asked, span) = queue.sync {
            recording = false
            return (writer, input, written, first, last, size, arrived, refused, late, worstGap, sizes, self.fps,
                    (lastArrival ?? 0) - (firstArrival ?? 0))
        }
        guard let w, let i, n > 1, began != nil, ended != nil else { return nil }
        i.markAsFinished()
        await w.finishWriting()
        guard w.status == .completed else { return nil }
        let fps = Double(got - 1) / max(0.001, span)  // as it arrived: the file's own rate is fixed
        let df = DateFormatter()
        df.dateFormat = "yyyyMMdd-HHmmss"
        let stamp = df.string(from: .now)
        // the glasses' link is whatever's on the route now: HFP shares its Bluetooth with the stream
        let hfp = AVAudioSession.sharedInstance().currentRoute.inputs.contains { $0.portType == .bluetoothHFP }
        let name = "samwell_\(stamp)_\(sz.0)x\(sz.1)_\(String(format: "%.1f", fps))fps_\(hfp ? "hfp" : "nohfp").mp4"
        let dest = URL.documentsDirectory.appending(path: name)
        try? FileManager.default.moveItem(at: w.outputURL, to: dest)
        let bytes = (try? dest.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        let report = "\(seen.sorted().joined(separator: ", ")) · HFP \(hfp ? "on" : "off") · \(String(format: "%.2f", fps)) fps of \(asked) "
            + "· \(got) frames over \(String(format: "%.1f", span)) s · \(n) written, \(no) refused, \(slow) late "
            + "· worst gap \(Int(gap * 1000)) ms · file \(String(format: "%.1f", Double(bytes) * 8 / max(0.001, span) / 1_000_000)) Mbps"
        Log.dat.info("[samwell] clip \(report, privacy: .public)")
        return Clip(url: dest, width: sz.0, height: sz.1, fps: fps, report: report)
    }
}

// MARK: - Sessions

struct SamWellTestView: View {
    var dat: GlassesDAT?
    @State private var rows: [SamWellAPI.SessionRow] = []
    @State private var a1 = "tl"
    @State private var pointer = "pen"
    @State private var opened: String?
    @State private var error: String?

    var body: some View {
        List {
            Section("New session") {
                Picker("A1 corner (as the glasses see the plate)", selection: $a1) {
                    ForEach(["tl", "tr", "br", "bl"], id: \.self) { Text(cornerName($0)).tag($0) }
                }
                .pickerStyle(.segmented)
                Picker("What goes into the wells", selection: $pointer) {
                    Text("Pen").tag("pen")
                    Text("Pipette tip").tag("pipette tip")
                }
                .pickerStyle(.segmented)
                Button("Start session (5 attempts)") {
                    Task {
                        do { opened = try await SamWellAPI.newSession(a1: a1, pointer: pointer).id } catch { self.error = error.localizedDescription }
                    }
                }
            }
            Section("Sessions") {
                if rows.isEmpty { Text("None yet").foregroundStyle(.secondary) }
                ForEach(rows) { r in
                    Button { opened = r.id } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(Date(timeIntervalSince1970: r.created).formatted(date: .abbreviated, time: .shortened))
                                .font(.headline)
                            Text([r.sources.joined(separator: "+"), r.sizes.joined(separator: ", "),
                                  r.accuracy.map { "\(r.hits)/\(r.total) wells (\(Int($0 * 100))%)" } ?? "no result",
                                  "\(r.attempts)/5"].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.callout).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            if let error { Text(error).foregroundStyle(.red).font(.footnote) }
        }
        .navigationTitle("SAM well test")
        .navigationDestination(item: $opened) { SamWellSessionView(sessionID: $0, dat: dat) }
        .task(id: opened) {
            if opened == nil { rows = (try? await SamWellAPI.sessions()) ?? rows }
        }
    }
}

func cornerName(_ c: String) -> String {
    ["tl": "Top left", "tr": "Top right", "br": "Bottom right", "bl": "Bottom left"][c] ?? c
}

// MARK: - One session

struct SamWellSessionView: View {
    let sessionID: String
    var dat: GlassesDAT?
    @State private var session: SamWellAPI.Session?
    /// The same wells every attempt (not random): the tester knows them without looking at the screen.
    private let targets = ["B1", "B2", "B3", "B4"]
    @State private var seconds = 30
    /// High (720×1280) at 7 fps: a 0.5 s stay is 3-4 frames (at 2 fps, one lost frame ends it)
    /// with less compression a frame than 15. SAM gets every frame.
    private let fps: UInt = 7
    @State private var cam = WellCam()
    @AppStorage("samwell.sound") private var soundOn = true
    @AppStorage("samwell.sayPlate") private var sayPlate = true
    @State private var aimState = PlateAimState()
    @State private var aimQuad: [CGPoint]?
    @State private var finder: PlateFinder?
    @State private var recordingUntil: Date?
    @State private var uploading = false
    @State private var uploadMB = 0.0
    @State private var clipReport: String?
    @State private var showing: Int?  // an attempt's result open; nil: capture the next
    @State private var picking = false
    @State private var pickedClip: URL?
    @State private var typedTargets = ""
    @State private var error: String?

    private var attempts: [SamWellAPI.Attempt] { session?.attempts ?? [] }
    private var analyzing: Bool { attempts.contains { $0.status == "analyzing" } }
    private var full: Bool { attempts.count >= 5 }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let s = session {
                    Text("A1 corner: \(cornerName(s.a1)) · \(s.pointer ?? "pipette tip") · attempt \(min(attempts.count + (showing == nil && !full ? 1 : 0), 5)) of 5")
                        .font(.headline)
                }
                if let n = showing, let a = attempts.first(where: { $0.n == n }) {
                    AttemptResult(sessionID: sessionID, attempt: a)
                    if !full {
                        Button("Next attempt") { showing = nil }.buttonStyle(.borderedProminent)
                    }
                } else if !full {
                    capture
                }
                if let sm = session?.summary, sm.complete || full {
                    SummaryView(summary: sm)
                }
                if !attempts.isEmpty {
                    Text("Attempts").font(.title3.bold())
                    ForEach(attempts) { a in
                        Button { showing = a.n } label: {
                            HStack {
                                Text("\(a.n). \(a.targets.joined(separator: " → "))")
                                Spacer()
                                Text(a.status == "done" ? (a.judge?.line ?? "") : a.status)
                                    .foregroundStyle(a.status == "failed" ? .red : .secondary)
                            }
                        }
                    }
                }
                if let error { Text(error).foregroundStyle(.red).font(.footnote) }
            }
            .padding()
        }
        .navigationTitle("Session")
        .task { await load() }
        .task(id: analyzing) {
            while analyzing, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                await load()
            }
        }
        .onAppear {
            let cam = cam
            let finder = PlateFinder { quad in Task { @MainActor in aimed(quad) } }
            self.finder = finder
            dat?.frameTap.set { cam.frame($0); finder.frame($0) }
            live()
        }
        .onDisappear {
            dat?.frameTap.set(nil)
            dat?.stopLive()
            finder = nil
            aimQuad = nil
            aimState = PlateAimState()
        }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.mpeg4Movie, .quickTimeMovie]) { r in
            if case .success(let url) = r { pickedClip = url; typedTargets = targets.joined(separator: ", ") }
        }
        .alert("Wells filled in this clip, in order", isPresented: Binding(
            get: { pickedClip != nil }, set: { if !$0 { pickedClip = nil } })) {
            TextField("C3, F7, B10", text: $typedTargets).textInputAutocapitalization(.characters)
            Button("Analyze") { if let u = pickedClip { sendFile(u) } }
            Button("Cancel", role: .cancel) { pickedClip = nil }
        }
    }

    @ViewBuilder private var capture: some View {
        if analyzing || uploading {
            if uploading {
                HStack { ProgressView(); Text("Uploading \(String(format: "%.0f", uploadMB)) MB to the server…") }.font(.title3)
            } else {
                AnalysisProgress(progress: attempts.last { $0.status == "analyzing" }?.progress)
            }
        } else {
            Text(targets.joined(separator: " → "))
                .font(.system(size: 54, weight: .bold, design: .rounded))
                .frame(maxWidth: .infinity)
            ZStack(alignment: .topLeading) {
                Group {
                    if let img = dat?.liveFrame {
                        Color.black.overlay(Image(uiImage: img).resizable().scaledToFill())
                    } else {
                        Color.black.overlay(Text("Waiting for the glasses' camera…").foregroundStyle(.white))
                    }
                }
                // the plate's shape (the middle of the 9:16 frame); the clip keeps the whole frame
                .aspectRatio(127.76 / 85.48, contentMode: .fit)
                .overlay {
                    if let aimQuad, let size = dat?.liveFrame?.size {
                        AimOutline(quad: aimQuad, image: size).stroke(.green, lineWidth: 3)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 12))
                TimelineView(.periodic(from: .now, by: 0.5)) { ctx in
                    HStack {
                        Text((cam.rate ?? "no frames") + " from the glasses (asked 720×1280 · \(fps) fps)")
                        if let until = recordingUntil {
                            Text("● REC \(max(0, Int(until.timeIntervalSince(ctx.date).rounded(.up)))) s").foregroundStyle(.red)
                        }
                    }
                    .font(.callout.monospacedDigit().bold()).padding(8)
                    .background(.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 8)).foregroundStyle(.white)
                    .padding(8)
                }
            }
            .frame(maxWidth: .infinity)
            if let clipReport { Text("Last clip: \(clipReport)").font(.caption.monospacedDigit()) }
            Toggle("Sounds", isOn: $soundOn)
            Toggle("Say \"Plate in view\"", isOn: $sayPlate)
            Picker("Length", selection: $seconds) {
                ForEach([10, 20, 30], id: \.self) { Text("\($0) s").tag($0) }
            }
            .pickerStyle(.segmented)
            .disabled(recordingUntil != nil)
            HStack {
                Button(recordingUntil == nil ? "Record" : "Stop now") {
                    if recordingUntil == nil { record() } else { recordingUntil = .now }
                }
                .buttonStyle(.borderedProminent).tint(.red).disabled(dat?.liveFrame == nil)
                Spacer()
                Button("Pick a clip") { picking = true }.disabled(recordingUntil != nil)
            }
        }
    }

    /// A detector result: the outline, and the aiming sounds. Aiming only, SAM judges the wells.
    private func aimed(_ quad: [CGPoint]?) {
        guard finder != nil else { return }  // a result that landed after leaving the page
        aimQuad = quad
        switch aimState.update(seen: quad != nil, at: CACurrentMediaTime()) {
        case .entered(let first):
            if soundOn { SamTone.rising.play() }
            if first, sayPlate { SpeechBus.shared.speak("Plate in view.") }
        case .lost:
            if soundOn { SamTone.falling.play() }
        case nil:
            break
        }
    }

    /// The glasses' stream at high, held there (no drop to medium), at the chosen fps.
    private func live() {
        dat?.stopLive()
        dat?.startLive(resolution: .high, frameRate: fps, hold: true)
    }

    private func load() async {
        do { session = try await SamWellAPI.session(sessionID) } catch { self.error = error.localizedDescription }
    }


    private func record() {
        error = nil
        cam.start(fps: Int(fps))
        let until = Date.now.addingTimeInterval(Double(seconds))
        recordingUntil = until
        Task {
            while let u = recordingUntil, u > .now { try? await Task.sleep(for: .milliseconds(100)) }
            recordingUntil = nil
            guard let clip = await cam.stop() else { error = "No frames were recorded."; return }
            clipReport = clip.report
            await send(clip.url, targets: targets, source: "stream")
        }
    }

    private func sendFile(_ url: URL) {
        let wells = typedTargets.uppercased().split(whereSeparator: { ", →>".contains($0) }).map(String.init)
        pickedClip = nil
        Task {
            let ok = url.startAccessingSecurityScopedResource()
            defer { if ok { url.stopAccessingSecurityScopedResource() } }
            await send(url, targets: wells, source: "file")
        }
    }

    private func send(_ clip: URL, targets: [String], source: String) async {
        uploadMB = Double((try? clip.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) / 1_000_000
        uploading = true
        defer { uploading = false }
        do {
            try await SamWellAPI.upload(session: sessionID, clip: clip, targets: targets, source: source)
            await load()
            showing = attempts.last?.n
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - One attempt's result

struct AttemptResult: View {
    let sessionID: String
    let attempt: SamWellAPI.Attempt

    private func details(_ j: SamWellAPI.Judge, _ r: SamWellAPI.Result) -> String {
        var parts = ["Order \(j.orderOk ? "matches" : "differs")", "false \(j.falseWells.count)"]
        parts += ["\(r.w)×\(r.h) · \(Int(r.fps.rounded())) fps", "well \(Int(r.wellPx)) px", "failed frames \(Int(r.failedRatio * 100))%",
                  "analysis \(Int(r.timing["total_s"] ?? 0)) s"]
        if r.rounds > 1 { parts.append("tip re-found \(r.rounds - 1)×") }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch attempt.status {
            case "analyzing":
                AnalysisProgress(progress: attempt.progress)
            case "failed":
                Text("Failed: \(attempt.error ?? "")").foregroundStyle(.red)
            default:
                if let j = attempt.judge, let r = attempt.result {
                    Text(j.line).font(.system(size: 40, weight: .bold))
                        .foregroundStyle(j.hits == j.total && j.orderOk && j.falseWells.isEmpty ? .green : .orange)
                    HStack(alignment: .top, spacing: 32) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Target").font(.headline)
                            ForEach(Array(j.wells.enumerated()), id: \.offset) { i, w in
                                Text("\(i + 1). \(w.well)  \(w.ok ? "✓" : "✗")").font(.title2.monospaced())
                                    .foregroundStyle(w.ok ? .green : .red)
                            }
                        }
                        VStack(alignment: .leading, spacing: 6) {
                            Text("SAM found").font(.headline)
                            ForEach(Array(r.found.enumerated()), id: \.offset) { i, f in
                                Text("\(i + 1). \(f.well)\(j.falseWells.contains(f.well) ? "  false" : "")")
                                    .font(.title2.monospaced())
                                    .foregroundStyle(j.falseWells.contains(f.well) ? .red : .primary)
                            }
                            if r.found.isEmpty { Text("nothing").foregroundStyle(.secondary) }
                        }
                    }
                    Text(details(j, r)).font(.callout).foregroundStyle(.secondary)
                    TabView {
                        ForEach(r.images, id: \.self) { name in
                            AsyncImage(url: SamWellAPI.imageURL(session: sessionID, attempt: attempt.n, name: name)) {
                                $0.resizable().scaledToFit()
                            } placeholder: { ProgressView() }
                        }
                    }
                    .tabViewStyle(.page(indexDisplayMode: .always))
                    .frame(height: 520)
                    .background(.black, in: RoundedRectangle(cornerRadius: 12))
                }
            }
        }
    }
}

/// Where the server's analysis is: its stage, a bar when it counts frames, the time so far.
struct AnalysisProgress: View {
    let progress: SamWellAPI.Progress?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(progress?.stage ?? "Starting…").font(.title3.bold())
            if let p = progress, let done = p.done, let total = p.total, total > 0 {
                ProgressView(value: Double(min(done, total)), total: Double(total))
                Text("frame \(done) of \(total) · \(Int(p.elapsedS)) s").font(.callout.monospacedDigit())
            } else {
                ProgressView()
                if let p = progress { Text("\(Int(p.elapsedS)) s").font(.callout.monospacedDigit()) }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12))
    }
}

// MARK: - Summary

struct SummaryView: View {
    let summary: SamWellAPI.Summary

    var body: some View {
        let s = summary
        VStack(alignment: .leading, spacing: 8) {
            Text("Summary (\(s.attempts) attempts)").font(.title2.bold())
            Text("Wells \(s.hits)/\(s.total) (\(Int(s.accuracy * 100))%)").font(.system(size: 36, weight: .bold))
            Text("All in order: \(s.inOrder)/\(s.attempts) attempts · false wells \(s.falseCount)")
            Text("Analysis \(Int(s.meanAnalysisS)) s on average · \(s.sizes.joined(separator: ", "))")
            Text("Well \(Int(s.wellPx)) px across · failed frames \(Int(s.failedRatio * 100))%")
            Text("Dwell settings on the same clips").font(.headline).padding(.top, 8)
            Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 4) {
                GridRow { Text("stay"); Text("radius"); Text("wells"); Text("in order"); Text("false") }
                    .font(.caption.bold())
                ForEach(Array(s.settings.rows.enumerated()), id: \.offset) { i, r in
                    GridRow {
                        Text("\(String(format: "%.1f", r.dwellS)) s")
                        Text("\(String(format: "%.1f", r.dwellMm)) mm")
                        Text("\(r.hits)/\(r.total)")
                        Text("\(r.inOrder)")
                        Text("\(r.falseCount)")
                    }
                    .font(.callout.monospacedDigit())
                    .fontWeight(i == s.settings.best ? .bold : .regular)
                    .foregroundStyle(i == s.settings.best ? .green : .primary)
                }
            }
            Text("Green: most accurate (most wells, then fewest false). Default is 1 s / 4.5 mm. Every frame is analysed.")
                .font(.footnote).foregroundStyle(.secondary)
        }
        .padding()
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12))
    }
}
#endif
