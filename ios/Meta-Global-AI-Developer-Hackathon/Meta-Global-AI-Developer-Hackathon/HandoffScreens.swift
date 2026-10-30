import SwiftUI

// Hand off, after a run (benchtrace-milo-v36.html `handoff`, `files`, `done`;
// docs/design/handoff.md): ⑨ Simulate, ⑨b Files, ⑨d Exported. ⑨0 Final
// protocol is the ⑤c-⑤e review in final protocol mode (StartScreens).
// Data: snapshot handoff (stage, check, code, csv) and the run's record for
// the plate (GET /runs/{id}, as the run record draws it). Buttons are the
// registry's (Export / Fix it / Back, Export, Done).

// MARK: - ⑨ Simulate

/// While Spark writes and the simulator runs: the stage, rows waiting. Once
/// passed, the virtual robot replays the approved steps one by one (v36
/// runSim): the row spins, the header reads "Step i of n · title · amount",
/// each well it touches lights then fills, a wait glows the plate; then
/// "All steps match" and Export. The replay is the simulator's checked
/// result shown step by step, not a second run.
struct SimulateScreen: View {
    var handoff: Handoff
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    @State private var rlab = LabState()
    @State private var selected: WellSelect?
    /// Replay: the step lit now, the steps done, finished, the wait glow.
    @State private var playing: Int?
    @State private var played: Set<Int> = []
    @State private var replayed = false
    @State private var glow = false

    private var steps: [Handoff.Step] { handoff.steps ?? [] }
    private var stage: String { handoff.stage ?? "generating" }
    private var passed: Bool { stage == "passed" }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: Theme.Layout.column * u) {
                PlateView(lab: rlab, selected: $selected, showPlanned: true)
                    .padding(28 * u)
                    .frame(width: 800 * u, height: 566 * u)
                    .benchCard(radius: 44 * u)
                    .shadow(color: Theme.polishBlue.opacity(glow ? 0.45 : 0), radius: 30 * u)
                    .animation(.easeInOut(duration: 0.5), value: glow)
                    .allowsHitTesting(false)
                    .enter(.pop, 0, u: u)
                side.frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.top, Theme.Layout.benchTop * u)
            Spacer(minLength: 24 * u)
            // Export shows once the replay has played (Fix it / Back when it didn't pass)
            if !passed || replayed {
                SayRow(buttons: buttons, u: u, perform: perform)
            }
        }
        .task(id: handoff.runId) {
            if let r = try? await PipClient.runDetail(id: handoff.runId) {
                rlab.sync(r.run.record)
                rlab.bench = start(r.run.record.bench)
            }
            if passed { await replay() }
        }
        .onChange(of: passed) { _, now in if now { Task { await replay() } } }
    }

    /// The plate before the robot: the plan's wells as rings, excluded ones crossed.
    private func start(_ b: RecordSnapshot.Bench?) -> RecordSnapshot.Bench {
        RecordSnapshot.Bench(wells: (b?.wells ?? [:]).filter { $0.value == "excluded" }, legend: b?.legend ?? [], rows: [])
    }

    @MainActor
    private func replay() async {
        guard !replayed, playing == nil else { return }
        rlab.bench = start(rlab.bench)
        played = []
        try? await Task.sleep(for: .milliseconds(1400))
        for s in steps {
            playing = s.n
            let wells = s.kind == "transfer" ? (s.wells ?? []) : []
            if wells.isEmpty {
                glow = true
                try? await Task.sleep(for: .milliseconds(1000))
                glow = false
                try? await Task.sleep(for: .milliseconds(300))
            } else {
                try? await Task.sleep(for: .milliseconds(250))
                for w in wells {
                    rlab.flashing.insert(w)
                    Task { @MainActor in
                        try? await Task.sleep(for: .milliseconds(200))
                        rlab.flashing.remove(w)
                        rlab.bench?.wells[w] = "done"
                    }
                    try? await Task.sleep(for: .milliseconds(45))
                }
                try? await Task.sleep(for: .milliseconds(500))
            }
            played.insert(s.n)
            playing = nil
            try? await Task.sleep(for: .milliseconds(180))
        }
        try? await Task.sleep(for: .milliseconds(300))
        withAnimation(.easeOut(duration: 0.3)) { replayed = true }
    }

    private var side: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12 * u) {
                Image(systemName: "gearshape.2")
                Text("Virtual robot")
            }
            .font(Theme.ui(30 * u)).foregroundStyle(Theme.text2).enter(.rise, 0, u: u)
            Text(title).font(Theme.ui(70 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                .foregroundStyle(Theme.text1).lineLimit(1).minimumScaleFactor(0.6)
                .padding(.top, 8 * u).enter(.text, 1, u: u)
                .contentTransition(.opacity).animation(.easeOut(duration: 0.3), value: title)
            Text(sub).font(Theme.ui(30 * u)).foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.6)
                .enter(.rise, 2, u: u)
            Spacer(minLength: 24 * u)
            VStack(spacing: 0) {
                ForEach(Array(rowItems.enumerated()), id: \.offset) { i, r in
                    simRow(r, last: i == rowItems.count - 1).enter(.rise, 3 + i, u: u)
                }
            }
        }
        .frame(height: Theme.Layout.benchHeight * 0.81 * u, alignment: .top)
    }

    /// One sim row: the approved steps once there, else the final protocol's rows.
    private var rowItems: [(n: Int, title: String, amount: String)] {
        steps.isEmpty ? handoff.summary.rows.map { ($0.n, $0.title, $0.detail) }
            : steps.map { ($0.n, $0.title, $0.amount) }
    }

    private var title: String {
        if passed, !replayed {
            return playing.map { "Step \($0) of \(steps.count)" } ?? "Simulating…"
        }
        switch stage {
        case "simulating": return "Simulating…"
        case "fixing": return "Fixing…"
        case "passed":
            let of = handoff.summary.of ?? steps.count
            return handoff.summary.rows.count == of ? "All steps match" : "\(handoff.summary.rows.count) of \(of) match"
        case "needs_you": return "Didn't pass"
        default: return "Writing the code…"
        }
    }

    private var sub: String {
        if passed, !replayed {
            return playing.flatMap { n in steps.first { $0.n == n } }.map { "\($0.title) · \($0.amount)" }
                ?? "Checking volumes, wells, order"
        }
        switch stage {
        case "simulating": return "Checking volumes, wells, order"
        case "fixing": return "Try \(handoff.attempt + 1) of 3 · reading the error"
        case "passed": return "Volumes, wells and order match your record."
        case "needs_you": return handoff.error
        default: return "From the final protocol you approved"
        }
    }

    /// v36 simrow: pending (dim ring), running (spinner), ok (blue tick), off (red).
    private func simRow(_ r: (n: Int, title: String, amount: String), last: Bool) -> some View {
        let result = handoff.check?.steps.first { $0.n == r.n }
        let state: String = {
            if passed { return played.contains(r.n) || replayed ? "ok" : playing == r.n ? "on" : "pending" }
            if let result { return result.ok ? "ok" : "off" }
            return stage == "simulating" ? "on" : "pending"
        }()
        return HStack(spacing: 16 * u) {
            ZStack {
                switch state {
                case "ok":
                    Circle().fill(Theme.polishBlue)
                    Image(systemName: "checkmark").font(Theme.ui(14 * u, weight: .bold)).foregroundStyle(.white)
                case "off":
                    Circle().fill(Color.red)
                    Image(systemName: "xmark").font(Theme.ui(14 * u, weight: .bold)).foregroundStyle(.white)
                case "on":
                    ProgressView().controlSize(.small)
                default:
                    Circle().stroke(Theme.text4, lineWidth: 2 * u)
                }
            }
            .frame(width: 28 * u, height: 28 * u)
            Text(r.title).foregroundStyle(Theme.text1).lineLimit(1)
            Spacer(minLength: 8 * u)
            Text(state == "off" ? (result?.why ?? "") : r.amount)
                .foregroundStyle(state == "off" ? Color.red : Theme.text2).lineLimit(1).minimumScaleFactor(0.6)
        }
        .font(Theme.ui(24 * u))
        .padding(.vertical, 13 * u)
        .opacity(state == "pending" ? 0.42 : 1)
        .overlay(alignment: .bottom) {
            if !last { Rectangle().fill(Color.black.opacity(0.06)).frame(height: 1) }
        }
        .animation(.easeOut(duration: 0.4), value: state)
    }
}

// MARK: - ⑨b Files

struct HandoffFilesScreen: View {
    var handoff: Handoff
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    /// Where the Export button is (screen points): the iPad share sheet's arrow.
    var onAnchor: (CGRect) -> Void = { _ in }

    /// The columns ⑨b shows of plate_map.csv, by header name.
    private static let shown = [("well", "Well"), ("group", "Group"), ("contents", "Contents"), ("status", "Status")]

    /// plate_map.csv rows (its "# ..." head left out), the shown columns only.
    private var table: [[String]] {
        let lines = handoff.csv.split(separator: "\n").map(String.init).filter { !$0.hasPrefix("#") }
        guard let head = lines.first.map(Self.fields) else { return [] }
        let cols = Self.shown.map { head.firstIndex(of: $0.0) }
        return lines.dropFirst().map { line in
            let f = Self.fields(line)
            return cols.map { $0.flatMap { f[safe: $0] } ?? "" }
        }
    }

    var body: some View {
        VStack(spacing: 36 * u) {
            HStack(alignment: .top, spacing: 36 * u) {
                VStack(alignment: .leading, spacing: 28 * u) {
                    Text("plate_map.csv").font(Theme.ui(43 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                    ScrollView {
                        Grid(alignment: .leading, horizontalSpacing: 24 * u, verticalSpacing: 0) {
                            GridRow {
                                ForEach(Self.shown, id: \.0) { Text($0.1) }
                            }
                            .font(Theme.ui(20 * u, weight: .medium)).foregroundStyle(Theme.text2)
                            .padding(.vertical, 16 * u)
                            ForEach(Array(table.enumerated()), id: \.offset) { i, row in
                                Divider()
                                GridRow {
                                    ForEach(Array(row.enumerated()), id: \.offset) { k, v in
                                        Text(k == 3 ? v.capitalized : v).foregroundStyle(k == 2 ? Theme.text2 : Theme.text1)
                                            .lineLimit(1).minimumScaleFactor(0.7)
                                    }
                                }
                                .font(Theme.ui(26 * u)).padding(.vertical, 16 * u)
                                .enter(.rise, min(i, 8), u: u)
                            }
                        }
                    }
                }
                .padding(48 * u).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .benchCard(radius: 44 * u).enter(.left, 0, u: u)
                .overlay(alignment: .bottomLeading) { reservoir }
                VStack(alignment: .leading, spacing: 28 * u) {
                    Text("protocol.py").font(Theme.ui(43 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                    ScrollView {
                        Text(handoff.code).font(Theme.mono(19 * u)).foregroundStyle(Color(hex: 0x4D4D4D))
                            .lineSpacing(13 * u).frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                }
                .padding(48 * u).frame(width: 520 * u).frame(maxHeight: .infinity, alignment: .topLeading)
                .benchCard(radius: 44 * u).enter(.right, 1, u: u)
            }
            .frame(maxHeight: .infinity)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 620 * u)
                .background {
                    GeometryReader { g in
                        Color.clear
                            .onAppear { onAnchor(g.frame(in: .global)) }
                            .onChange(of: g.frame(in: .global)) { _, f in onAnchor(f) }
                    }
                }
        }
    }

    /// Under the table: where each liquid goes in the reservoir, in its Opentrons App color.
    @ViewBuilder private var reservoir: some View {
        if let liqs = handoff.liquids, !liqs.isEmpty {
            HStack(spacing: 18 * u) {
                Text("Reservoir").foregroundStyle(Theme.text2)
                ForEach(liqs, id: \.well) { lq in
                    HStack(spacing: 8 * u) {
                        Circle().fill(Color(hex: UInt32(lq.color.dropFirst(), radix: 16) ?? 0)).frame(width: 14 * u, height: 14 * u)
                        Text(lq.line).foregroundStyle(Theme.text1)
                    }
                }
            }
            .font(Theme.ui(20 * u)).lineLimit(1).minimumScaleFactor(0.6)
            .padding(.horizontal, 48 * u).padding(.bottom, 24 * u)
        }
    }

    /// One CSV line's fields (quoted fields may hold commas and "").
    static func fields(_ line: String) -> [String] {
        var out: [String] = [], cur = "", quoted = false
        let it = Array(line)
        var i = 0
        while i < it.count {
            let ch = it[i]
            if quoted {
                if ch == "\"", i + 1 < it.count, it[i + 1] == "\"" { cur.append("\""); i += 1 }
                else if ch == "\"" { quoted = false }
                else { cur.append(ch) }
            } else if ch == "\"" {
                quoted = true
            } else if ch == "," {
                out.append(cur); cur = ""
            } else {
                cur.append(ch)
            }
            i += 1
        }
        out.append(cur)
        return out
    }
}

private extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}

// MARK: - ⑨d Exported

struct ExportedScreen: View {
    var handoff: Handoff
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        VStack(spacing: 34 * u) {
            ZStack {
                Circle().fill(Theme.polishBlue)
                Image(systemName: "checkmark").font(Theme.ui(56 * u, weight: .bold)).foregroundStyle(.white)
            }
            .frame(width: 140 * u, height: 140 * u).enter(.pop, 0, u: u)
            Text("Shared").font(Theme.ui(70 * u, weight: .semibold)).tracking(-1.79 * u)
                .foregroundStyle(Theme.text1).enter(.text, 1, u: u)
            VStack(alignment: .leading, spacing: 14 * u) {
                // what really happened: the two files went through the share sheet, a copy stays
                ForEach(Array(["Shared protocol.py and plate_map.csv", "Matches the approved record",
                               "A copy stays with the run"].enumerated()), id: \.offset) { i, line in
                    HStack(spacing: 14 * u) {
                        Image(systemName: "checkmark").foregroundStyle(Theme.polishBlue)
                        Text(line).foregroundStyle(Theme.text1)
                    }
                    .font(Theme.ui(30 * u)).enter(.rise, 2 + i, u: u)
                }
            }
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 620 * u).padding(.top, 10 * u)
        }
        .frame(maxHeight: .infinity)
    }
}

// MARK: - Share sheet

/// The iPad share sheet (AirDrop, Save to Files, Mail) for the hand off
/// files, its popover arrow on ⑨b's Export button. `done` is false when it
/// was cancelled.
enum SharePresenter {
    @MainActor
    static func present(_ urls: [URL], from anchor: CGRect, done: @escaping (Bool) -> Void) {
        let vc = UIActivityViewController(activityItems: urls, applicationActivities: nil)
        vc.completionWithItemsHandler = { _, completed, _, _ in done(completed) }
        guard let window = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene })
                .flatMap(\.windows).first(where: \.isKeyWindow),
              var top = window.rootViewController else {
            done(false)
            return
        }
        while let next = top.presentedViewController { top = next }
        if let pop = vc.popoverPresentationController {
            pop.sourceView = window
            pop.sourceRect = anchor == .zero ? CGRect(x: window.bounds.midX, y: window.bounds.maxY - 80, width: 1, height: 1)
                : anchor
            pop.permittedArrowDirections = [.down, .up]
        }
        top.present(vc, animated: true)
    }
}
