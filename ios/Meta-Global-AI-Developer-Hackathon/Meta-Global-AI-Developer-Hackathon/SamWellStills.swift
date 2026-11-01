// SamWellStills.swift
// Diagnostics "SAM well test", One a second (DEBUG, no registry or voice): a photo a second from the
// stream instead of video. The server (server/lab_sam_stills.py) judges each as it lands, so the
// wells fill in while recording.

#if DEBUG
import AVFoundation
import CoreImage
import MWDATCamera
import os
import SwiftUI

extension SamWellAPI {
    struct StillRun: Decodable {
        struct Well: Decodable, Identifiable {
            let well: String
            let first: Int
            let photos: Int
            let offMm: Double
            let confidence: String
            var id: String { "\(well)-\(first)" }
            var sure: Bool { confidence == "sure" }
        }
        let id: String
        let status: String
        let taken: Int
        let looked: Int
        let failed: Int
        let skipped: Int
        let wells: [Well]
        let tookS: Double?
        let lookS: Double?
        let plateCalls: Int
        let shaken: Int
        let error: String?
    }

    static func newStills(a1: String, pointer: String) async throws -> StillRun {
        var req = URLRequest(url: Config.baseURL.appending(path: "lab/sam-wells/stills"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["a1": a1, "pointer": pointer])
        return try await VisionAPI.send(req)
    }

    static func stills(_ id: String) async throws -> StillRun {
        try await VisionAPI.send(URLRequest(url: Config.baseURL.appending(path: "lab/sam-wells/stills/\(id)")))
    }

    static func stopStills(_ id: String) async throws -> StillRun {
        var req = URLRequest(url: Config.baseURL.appending(path: "lab/sam-wells/stills/\(id)/stop"))
        req.httpMethod = "POST"
        return try await VisionAPI.send(req)
    }
}

/// The fastest head turn since it was last read, rad/s. Samples arrive off the main thread.
final class GyroPeak: @unchecked Sendable {
    private let lock = NSLock()
    private var peak: Double?

    func note(_ rate: Double) { lock.withLock { peak = max(peak ?? 0, rate) } }

    func take() -> Double? {
        lock.withLock {
            defer { peak = nil }
            return peak
        }
    }
}

/// Takes the stream's next frame once a second, with the head's turn over the frame before it.
/// Frames arrive on the SDK's thread.
final class StillTaker: @unchecked Sendable {
    private let lock = NSLock()
    private let images = CIContext()
    private var next = 0.0
    private var count = 0
    private var onPhoto: (@Sendable (Int, Double?, Data) -> Void)?
    private var gyro: GyroPeak?

    func start(gyro: GyroPeak?, _ onPhoto: @escaping @Sendable (Int, Double?, Data) -> Void) {
        lock.withLock { next = 0; count = 0; self.gyro = gyro; self.onPhoto = onPhoto }
    }

    func stop() { lock.withLock { onPhoto = nil } }

    func frame(_ sb: CMSampleBuffer) {
        let now = CACurrentMediaTime()
        let turn = lock.withLock { gyro }?.take()
        let due: (Int, @Sendable (Int, Double?, Data) -> Void)? = lock.withLock {
            guard let onPhoto, now >= next else { return nil }
            next = now + 1
            count += 1
            return (count - 1, onPhoto)
        }
        guard let due, let raw = CMSampleBufferGetImageBuffer(sb),
              let jpeg = images.jpegRepresentation(of: CIImage(cvPixelBuffer: raw), colorSpace: CGColorSpaceCreateDeviceRGB())
        else { return }
        due.1(due.0, turn, jpeg)
    }
}

struct SamWellStillsView: View {
    var dat: GlassesDAT?
    let a1: String
    let pointer: String
    private enum Phase { case idle, recording, finishing, done }
    @State private var phase = Phase.idle
    @State private var taker = StillTaker()
    @State private var finder: PlateFinder?
    @State private var aimQuad: [CGPoint]?
    @State private var run: SamWellAPI.StillRun?
    @State private var uploads: [Task<Void, Never>] = []
    @State private var sent = 0
    @State private var unsent = 0
    @State private var error: String?

    private func name(_ w: SamWellAPI.StillRun.Well) -> String { w.sure ? w.well : "\(w.well)?" }

    private func seconds(_ s: Double?) -> String { s.map { String(format: "%.1f", $0) } ?? "–" }

    private func wells(_ run: SamWellAPI.StillRun) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            PlateCells(a1: a1, sure: Set(run.wells.filter(\.sure).map(\.well)),
                       notSure: Set(run.wells.filter { !$0.sure }.map(\.well)))
            Text(run.wells.isEmpty ? "No wells" : run.wells.map(name).joined(separator: " → "))
                .font(.title2.bold())
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if phase == .done, let run {
                    wells(run)
                    ForEach(run.wells) { w in
                        Text("\(name(w))  at \(w.first) s · \(w.photos) photos · \(String(format: "%.1f", w.offMm)) mm")
                            .font(.callout.monospaced()).opacity(w.sure ? 1 : 0.5)
                    }
                    Text("\(run.taken) photos · \(run.failed) failed · \(run.skipped) skipped · \(run.shaken) shaken · \(run.plateCalls) plate calls · \(seconds(run.lookS)) s a photo · \(seconds(run.tookS)) s after Stop")
                        .font(.callout).foregroundStyle(.secondary)
                    if let why = run.error { Text(why).foregroundStyle(.red) }
                    Button("Again") { reset() }.buttonStyle(.bordered)
                } else {
                    SamWellPreview(dat: dat, aimQuad: aimQuad).frame(height: 420).frame(maxWidth: .infinity)
                    switch phase {
                    case .idle:
                        Button("Record") { Task { await begin() } }
                            .buttonStyle(.borderedProminent).disabled(aimQuad == nil)
                        Text(aimQuad == nil ? "Waiting for the plate in view…" : "Plate in view").foregroundStyle(.secondary)
                    case .recording:
                        Button("Stop") { Task { await end() } }.buttonStyle(.borderedProminent).tint(.red)
                        Text("● \(sent) photos sent · \(run?.looked ?? 0) judged").font(.callout.monospaced())
                    default:
                        HStack { ProgressView(); Text("Finishing… \(run?.looked ?? 0) of \(run?.taken ?? sent)") }
                    }
                    if let run, phase != .idle { wells(run) }
                }
                if unsent > 0 { Text("\(unsent) photos didn't upload").foregroundStyle(.red) }
                if let error { Text(error).foregroundStyle(.red) }
            }
            .padding()
        }
        .navigationTitle("One a second")
        .task(id: phase) {  // the wells as they're judged
            guard phase == .recording, let id = run?.id else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(500))
                if let now = try? await SamWellAPI.stills(id), phase == .recording { run = now }
            }
        }
        .onAppear {
            let taker = taker
            let finder = PlateFinder { quad in Task { @MainActor in aimQuad = quad } }
            self.finder = finder
            dat?.testing = true
            dat?.wantsMotion = true
            dat?.frameTap.set { taker.frame($0); finder.frame($0) }
            dat?.stopLive()
            dat?.startLive(resolution: .high, frameRate: 7, hold: true)
        }
        .onDisappear {
            taker.stop()
            dat?.frameTap.set(nil)
            dat?.stopLive()
            dat?.wantsMotion = false
            dat?.testing = false
            finder = nil
        }
    }

    private func reset() {
        run = nil
        sent = 0
        unsent = 0
        error = nil
        uploads = []
        phase = .idle
    }

    private func begin() async {
        do {
            let new = try await SamWellAPI.newStills(a1: a1, pointer: pointer)
            run = new
            phase = .recording
            taker.start(gyro: dat?.gyro) { n, turn, jpeg in Task { @MainActor in upload(new.id, n, turn, jpeg) } }
        } catch { self.error = error.localizedDescription }
    }

    private func upload(_ id: String, _ n: Int, _ turn: Double?, _ jpeg: Data) {
        uploads.append(Task {
            do {
                try await VisionAPI.uploadStills(path: "stills/\(id)/photo", stills: [(n, jpeg)], gyro: turn)
                sent += 1
            } catch {
                Log.dat.error("[samwell] photo \(n): \(error.localizedDescription, privacy: .public)")
                unsent += 1
            }
        })
    }

    private func end() async {
        taker.stop()
        phase = .finishing
        for task in uploads { await task.value }
        guard let id = run?.id else { return }
        do {
            run = try await SamWellAPI.stopStills(id)
            while run?.status != "done" {
                try await Task.sleep(for: .milliseconds(500))
                run = try await SamWellAPI.stills(id)
            }
            phase = .done
        } catch { self.error = error.localizedDescription }
    }
}
#endif
