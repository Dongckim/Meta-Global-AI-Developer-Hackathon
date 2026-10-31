// SamWellLive.swift
// Diagnostics "SAM well test", Continuous (DEBUG, no registry or voice): nobody lists the wells.
// Recording never stops; every chunk uploads as it closes, and the wells the server finds
// (server/lab_sam_live.py) appear on the plate as they come.

#if DEBUG
import AVFoundation
import MWDATCamera
import os
import SwiftUI

/// Cuts the stream into overlapping chunks. Frames arrive on the SDK's thread.
final class WellChunks: @unchecked Sendable {
    struct Chunk { let n: Int; let start: Int; let url: URL }

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
    private var open: [Open] = []
    private var arrivals: [Double] = []
    private var stopped = false

    init(fps: Int, closed: @escaping @Sendable (Chunk) -> Void) {
        self.fps = fps
        self.closed = closed
    }

    private var length: Int { Self.seconds * fps }
    private var step: Int { (Self.seconds - Self.overlap) * fps }

    func frame(_ sb: CMSampleBuffer) {
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        queue.sync {
            guard !stopped else { return }
            let frame = arrivals.count
            arrivals.append(CACurrentMediaTime())
            if frame % step == 0 {
                let url = FileManager.default.temporaryDirectory.appending(path: "samwell-chunk-\(UUID().uuidString).mp4")
                if let (w, i, a) = WellCam.writer(url: url, width: CVPixelBufferGetWidth(pb), height: CVPixelBufferGetHeight(pb), fps: fps) {
                    open.append(Open(n: frame / step, start: frame, writer: w, input: i, adaptor: a))
                }
            }
            for k in open.indices where open[k].input.isReadyForMoreMediaData {
                // shortcut: a refused frame shifts the rest of its chunk one frame early, fine unless "refused" shows up in the log
                let pts = CMTime(value: CMTimeValue(open[k].written), timescale: CMTimeScale(fps))
                if open[k].adaptor.append(pb, withPresentationTime: pts) { open[k].written += 1 }
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
        /// Seconds from the tip stopping at the well to the well showing here.
        let delay: Double?
    }

    private(set) var phase = Phase.idle
    private(set) var seen: [Seen] = []
    private(set) var live: SamWellAPI.Live?
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

    func start(a1: String, pointer: String, fps: Int) async {
        guard phase == .idle else { return }
        do {
            let s = try await SamWellAPI.newLive(a1: a1, pointer: pointer)
            id = s.id
            live = s
        } catch {
            self.error = error.localizedDescription
            return
        }
        chunks = WellChunks(fps: fps) { chunk in
            Task { @MainActor [weak self] in self?.send(chunk) }
        }
        phase = .recording
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
                    try await SamWellAPI.uploadChunk(live: id, n: chunk.n, start: chunk.start, clip: chunk.url)
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
        guard let id, let s = try? await SamWellAPI.live(id) else { return phase != .done }
        take(s)
        return phase != .done
    }

    private func take(_ s: SamWellAPI.Live) {
        live = s
        let now = CACurrentMediaTime()
        for w in s.wells where !seen.contains(where: { $0.id == "\(w.well)-\(w.frame)" }) {
            let delay = chunks?.arrival(w.f0).map { now - $0 }
            seen.append(Seen(id: "\(w.well)-\(w.frame)", well: w.well, delay: delay))
            Log.dat.info("[samwell] live \(w.well, privacy: .public) frame \(w.frame) delay \(delay ?? -1)")
        }
        if s.done, phase == .finishing { phase = .done }
    }

    /// Leaves recording; whatever's open uploads, and the server finishes it.
    func stop() {
        guard phase == .recording else { return }
        phase = .finishing
        chunks?.stop()
        Task {
            try? await Task.sleep(for: .seconds(1))  // the last writers report through finishWriting
            for upload in uploads { await upload.value }
            guard let id else { return }
            do { take(try await SamWellAPI.stopLive(id)) } catch { self.error = error.localizedDescription }
        }
    }
}

struct SamWellLiveView: View {
    var dat: GlassesDAT?
    let a1: String
    let pointer: String
    /// As the attempts page: every frame goes to SAM.
    private let fps: UInt = 7
    @State private var run = SamWellRun()
    @State private var cam = WellCam()
    @State private var aimState = PlateAimState()
    @State private var aimQuad: [CGPoint]?
    @State private var finder: PlateFinder?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(run.seen.last?.well ?? "–")
                    .font(.system(size: 72, weight: .bold, design: .rounded))
                    .frame(maxWidth: .infinity)
                PlateCells(a1: a1, wells: Dictionary(run.seen.map { ($0.well, 1) }, uniquingKeysWith: +))
                if run.phase == .idle || run.phase == .recording {
                    ZStack(alignment: .topLeading) {
                        SamWellPreview(dat: dat, aimQuad: aimQuad)
                        TimelineView(.periodic(from: .now, by: 0.5)) { _ in
                            Text(cam.rate ?? "no frames").font(.callout.monospacedDigit().bold()).padding(8)
                                .background(.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 8)).foregroundStyle(.white)
                                .padding(8)
                        }
                    }
                    .frame(height: 420)
                    .frame(maxWidth: .infinity)
                }
                switch run.phase {
                case .idle:
                    Button("Start") { Task { await begin() } }
                        .buttonStyle(.borderedProminent).disabled(aimQuad == nil)
                    Text(aimQuad == nil ? "Waiting for the plate in view…" : "Plate in view.").foregroundStyle(.secondary)
                case .recording:
                    Button("Stop") { run.stop() }.buttonStyle(.borderedProminent).tint(.red)
                    Text("● Recording · \(run.seen.count) wells · \(run.waiting) chunks in analysis").font(.callout.monospacedDigit())
                case .finishing:
                    HStack { ProgressView(); Text("Finishing the last chunks (\(run.waiting) left)…") }
                case .done:
                    summary
                }
                if let error = run.error { Text(error).foregroundStyle(.red).font(.footnote) }
            }
            .padding()
        }
        .navigationTitle("Continuous")
        .onAppear {
            let cam = cam
            let finder = PlateFinder { quad in Task { @MainActor in aimed(quad) } }
            self.finder = finder
            dat?.frameTap.set { cam.frame($0); finder.frame($0) }
            dat?.stopLive()
            dat?.startLive(resolution: .high, frameRate: fps, hold: true)
        }
        .onDisappear {
            run.stop()
            dat?.frameTap.set(nil)
            dat?.stopLive()
            finder = nil
        }
    }

    private func begin() async {
        await run.start(a1: a1, pointer: pointer, fps: Int(fps))
        guard let tap = run.tap, let finder else { return }
        let cam = cam
        dat?.frameTap.set { cam.frame($0); finder.frame($0); tap($0) }
    }

    private func aimed(_ quad: [CGPoint]?) {
        guard finder != nil else { return }  // a result that landed after leaving the page
        aimQuad = quad
        if case .entered(true) = aimState.update(seen: quad != nil, at: CACurrentMediaTime()) {
            SpeechBus.shared.speak("Plate in view.")
        }
    }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Summary").font(.title2.bold())
            ForEach(Array(run.seen.enumerated()), id: \.element.id) { i, s in
                Text("\(i + 1). \(s.well)" + (s.delay.map { "   \(Int($0)) s" } ?? "")).font(.title2.monospaced())
            }
            if run.seen.isEmpty { Text("No wells").foregroundStyle(.secondary) }
            let delays = run.seen.compactMap(\.delay)
            if !delays.isEmpty {
                Text("In to shown: \(Int(delays.reduce(0, +) / Double(delays.count))) s on average, \(Int(delays.max() ?? 0)) s at worst")
            }
            let chunks = run.live?.chunks ?? []
            Text("\(chunks.count) chunks · \(chunks.filter { $0.status == "empty" }.count) empty · "
                 + "\(chunks.filter { $0.status == "failed" }.count) failed · "
                 + "reference set \(run.live?.references.count ?? 0)× · analysis "
                 + "\(Int(chunks.compactMap { $0.timing?["total_s"] }.reduce(0, +) / max(1, Double(chunks.count)))) s a chunk")
                .font(.callout).foregroundStyle(.secondary)
        }
        .padding()
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12))
    }
}

/// The plate as cells, seen as `a1` says (A1's corner top left when "tl"). A well with a count is filled and named.
struct PlateCells: View {
    let a1: String
    let wells: [String: Int]

    private func well(_ row: Int, _ col: Int) -> String {
        let r = a1 == "bl" || a1 == "br" ? 7 - row : row
        let c = a1 == "tr" || a1 == "br" ? 11 - col : col
        return "\("ABCDEFGH".map(String.init)[r])\(c + 1)"
    }

    var body: some View {
        Grid(horizontalSpacing: 4, verticalSpacing: 4) {
            ForEach(0..<8, id: \.self) { row in
                GridRow {
                    ForEach(0..<12, id: \.self) { col in
                        let name = well(row, col)
                        let hit = wells[name] != nil
                        Text(name)
                            .font(.caption2.monospacedDigit().weight(hit ? .bold : .regular))
                            .foregroundStyle(hit ? .white : .secondary)
                            .frame(maxWidth: .infinity, minHeight: 34)
                            .background(hit ? Color.green : Color.secondary.opacity(0.15), in: Circle())
                    }
                }
            }
        }
    }
}
#endif
