// SamWellLive.swift
// Diagnostics "SAM well test", Continuous (DEBUG, no registry or voice): nobody lists the wells.
// Recording never stops; every chunk uploads as it closes, and the wells the server finds
// (server/lab_sam_live.py) appear on the plate as they come. The engine is VisionWells.swift.

#if DEBUG
import AVFoundation
import CoreImage
import MWDATCamera
import os
import SwiftUI

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
    /// Debug only: the wells the tester means to fill, saved with the session to check against.
    @State private var targets = ""

    private func name(_ s: SamWellRun.Seen) -> String { s.sure ? s.well : "\(s.well)?" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(run.seen.last.map(name) ?? "–")
                    .font(.system(size: 72, weight: .bold, design: .rounded))
                    .opacity(run.seen.last?.sure == false ? 0.35 : 1)
                    .frame(maxWidth: .infinity)
                PlateCells(a1: a1, sure: Set(run.seen.filter(\.sure).map(\.well)),
                           notSure: Set(run.seen.filter { !$0.sure }.map(\.well)))
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
                    TextField("Wells you'll fill (optional): C3, F7", text: $targets)
                        .textFieldStyle(.roundedBorder).textInputAutocapitalization(.characters)
                    Button("Record") { Task { await begin() } }
                        .buttonStyle(.borderedProminent).disabled(aimQuad == nil)
                    Text(aimQuad == nil ? "Waiting for the plate in view…" : "Plate in view.").foregroundStyle(.secondary)
                case .recording:
                    Button("Stop") { run.stop() }.buttonStyle(.borderedProminent).tint(.red)
                    Text("● REC · \(run.seen.count) wells · \(run.sentChunks) chunks sent · \(run.waiting) in analysis").font(.callout.monospacedDigit())
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
            dat?.testing = true
            dat?.frameTap.set { cam.frame($0); finder.frame($0) }
            dat?.stopLive()
            dat?.startLive(resolution: .high, frameRate: fps, hold: true)
        }
        .onDisappear {
            run.stop()
            dat?.frameTap.set(nil)
            dat?.stopLive()
            dat?.testing = false
            finder = nil
        }
    }

    private func begin() async {
        await run.start(a1: a1, pointer: pointer, targets: targets, fps: Int(fps))
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

    private func list(_ wells: [String]) -> String { wells.isEmpty ? "none" : wells.joined(separator: ", ") }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Summary").font(.title2.bold())
            ForEach(Array(run.seen.enumerated()), id: \.element.id) { i, s in
                Text("\(i + 1). \(name(s))" + (s.delay.map { "   \(Int($0)) s" } ?? "")).font(.title2.monospaced())
                    .opacity(s.sure ? 1 : 0.35)
            }
            if let sm = run.live?.summary {
                Text("Sure \(sm.sure) · not sure \(sm.notSure)").font(.headline)
                if run.live?.targets.isEmpty == false {
                    Text("Hit: \(list(sm.hit))")
                    Text("Missed: \(list(sm.missed))")
                    Text("Outside the list: \(list(sm.outside))")
                    Text("To catch (not sure only): \(list(sm.toCatch))")
                }
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

/// The plate as cells, seen as `a1` says (A1's corner top left when "tl"). A sure well is filled; a not-sure one is faint, "C4?".
struct PlateCells: View {
    let a1: String
    let sure: Set<String>
    let notSure: Set<String>

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
                        let hit = sure.contains(name)
                        let maybe = !hit && notSure.contains(name)
                        Text(maybe ? "\(name)?" : name)
                            .font(.caption2.monospacedDigit().weight(hit ? .bold : .regular))
                            .foregroundStyle(hit ? .white : .secondary)
                            .frame(maxWidth: .infinity, minHeight: 34)
                            .background(hit ? Color.green : maybe ? Color.green.opacity(0.2) : Color.secondary.opacity(0.15), in: Circle())
                    }
                }
            }
        }
    }
}
#endif
