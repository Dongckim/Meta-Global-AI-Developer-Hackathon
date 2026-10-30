import SwiftUI

// Hand off, after a run (benchtrace-milo-v36.html `handoff`, `files`, `done`;
// docs/design/handoff.md): ⑨ Simulate, ⑨b Files, ⑨d Exported. ⑨0 Final
// protocol is the ⑤c-⑤e review in final protocol mode (StartScreens).
// Data: snapshot handoff (stage, check, code, csv) and the run's record for
// the plate (GET /runs/{id}, as the run record draws it). Buttons are the
// registry's (Export / Write it again / Back, Export, Done).

// MARK: - ⑨ Simulate

struct SimulateScreen: View {
    var handoff: Handoff
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    @State private var rlab = LabState()
    @State private var selected: WellSelect?

    private var rows: [DraftRow] { handoff.summary.rows }
    private var stage: String { handoff.stage ?? "generating" }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: Theme.Layout.column * u) {
                PlateView(lab: rlab, selected: $selected)
                    .padding(28 * u)
                    .frame(width: 800 * u, height: 566 * u)
                    .benchCard(radius: 44 * u)
                    .allowsHitTesting(false)
                    .enter(.pop, 0, u: u)
                side.frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.top, Theme.Layout.benchTop * u)
            Spacer(minLength: 24 * u)
            SayRow(buttons: buttons, u: u, perform: perform)
        }
        .task(id: handoff.runId) {
            if let r = try? await PipClient.runDetail(id: handoff.runId) { rlab.sync(r.run.record) }
        }
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
                ForEach(Array(rows.enumerated()), id: \.element.id) { i, r in
                    simRow(r, last: i == rows.count - 1).enter(.rise, 3 + i, u: u)
                }
            }
        }
        .frame(height: Theme.Layout.benchHeight * 0.81 * u, alignment: .top)
    }

    private var title: String {
        switch stage {
        case "simulating": return "Simulating…"
        case "fixing": return "Fixing…"
        case "passed":
            let of = handoff.summary.of ?? rows.count
            return rows.count == of ? "All steps match" : "\(rows.count) of \(of) match"
        case "needs_you": return "Didn't pass"
        default: return "Writing the code…"
        }
    }

    private var sub: String {
        switch stage {
        case "simulating": return "Checking volumes, wells, order"
        case "fixing": return "Try \(handoff.attempt + 1) of 3 · reading the error"
        case "passed": return "Volumes, wells and order match your record."
        case "needs_you": return handoff.error
        default: return "From the final protocol you approved"
        }
    }

    /// v36 simrow: pending (dim ring), running (spinner), ok (blue tick), off (red).
    private func simRow(_ r: DraftRow, last: Bool) -> some View {
        let result = handoff.check?.steps.first { $0.n == r.n }
        let state: String = result.map { $0.ok ? "ok" : "off" } ?? (stage == "simulating" ? "on" : "pending")
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
            Text(result.flatMap { $0.ok ? nil : $0.why } ?? r.detail)
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

    /// plate_map.csv rows as v36 shows them: Well, Group, Status, Source.
    private var table: [[String]] {
        handoff.csv.split(separator: "\n").dropFirst().map { line in
            let f = Self.fields(String(line))
            return [f[safe: 0], f[safe: 1], f[safe: 2], f[safe: 4]].map { $0 ?? "" }
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
                                ForEach(["Well", "Group", "Status", "Source"], id: \.self) { Text($0) }
                            }
                            .font(Theme.ui(20 * u, weight: .medium)).foregroundStyle(Theme.text2)
                            .padding(.vertical, 16 * u)
                            ForEach(Array(table.enumerated()), id: \.offset) { i, row in
                                Divider()
                                GridRow {
                                    ForEach(Array(row.enumerated()), id: \.offset) { k, v in
                                        Text(v.capitalized).foregroundStyle(k == 3 ? Theme.text2 : Theme.text1)
                                            .lineLimit(1)
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
            Text("Exported safely").font(Theme.ui(70 * u, weight: .semibold)).tracking(-1.79 * u)
                .foregroundStyle(Theme.text1).enter(.text, 1, u: u)
            VStack(alignment: .leading, spacing: 14 * u) {
                // what really happened: files saved with the run, nothing pushed to a robot
                ForEach(Array(["protocol.py · simulation passed", "Matches the approved record",
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
