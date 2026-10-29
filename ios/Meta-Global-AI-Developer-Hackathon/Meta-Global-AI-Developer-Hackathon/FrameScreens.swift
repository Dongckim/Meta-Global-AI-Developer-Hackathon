import SwiftUI

// Screens with no feature behind them yet (registry status "planned"; the
// server answers "That isn't ready yet." when one is asked for). Each draws
// one input type; every word on screen comes from it. Example data lives
// only in the DEBUG previews below.
//
// ⑦ Label check (Figma 37:14014) — needs: camera capture of a tube label,
//    label text recognition, a compare against the step's liquid and
//    concentration (verdict, label, expected -> found).
// ⑧ Web search answer (37:14048) — needs: web search kept per run (query,
//    time, answer, source title and domain) and a "searches" read.
// ⑨ Hand off: Simulate (37:14080), Export (37:14280), Files (37:14316),
//    Where to (37:14393), Exported (37:14427) — needs: a run simulation,
//    plate map / protocol.py export files per run, export destinations
//    (Opentrons App, Files) and a send result.

/// A centered ver 2 frame: optional icon, a big title, small lines, and
/// the screen's registry buttons under it.
struct FrameLayout<Body: View>: View {
    var icon: String?
    var title: String
    var lines: [String]
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    @ViewBuilder var extra: Body

    var body: some View {
        VStack(spacing: 36 * u) {
            VStack(spacing: 18 * u) {
                if let icon {
                    Image(systemName: icon).font(.system(size: 44 * u, weight: .medium))
                        .foregroundStyle(Theme.polishBlue)
                        .enter(.pop, 0, u: u)
                }
                Text(title).font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u)
                    .foregroundStyle(Theme.text1).multilineTextAlignment(.center)
                    .lineLimit(2).minimumScaleFactor(0.6)
                    .enter(.text, 1, u: u)
                ForEach(Array(lines.enumerated()), id: \.offset) { i, l in
                    Text(l).font(.system(size: 28 * u)).tracking(-0.34 * u)
                        .foregroundStyle(i == 0 ? Theme.text1 : Theme.text2)
                        .multilineTextAlignment(.center).lineLimit(2)
                        .enter(.rise, 2 + i, u: u)
                }
                extra
            }
            .padding(.horizontal, 64 * u).padding(.vertical, 56 * u)
            .frame(width: 756 * u)
            .benchCard(radius: 44 * u)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 756 * u)
        }
    }
}

extension FrameLayout where Body == EmptyView {
    init(icon: String?, title: String, lines: [String], buttons: [ScreenButton], u: CGFloat,
         perform: @escaping (ScreenButton) -> Void) {
        self.init(icon: icon, title: title, lines: lines, buttons: buttons, u: u, perform: perform) { EmptyView() }
    }
}

// MARK: - ⑦ Label check

struct LabelCheck: Equatable {
    var verdict: String
    var label: String
    /// "10 µM → 1 µM": expected -> found.
    var change: String
}

struct LabelCheckScreen: View {
    var check: LabelCheck
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        FrameLayout(icon: "camera.viewfinder", title: check.verdict, lines: [check.label, check.change],
                    buttons: buttons, u: u, perform: perform)
            .modifier(ShakeOnce(u: u))
    }
}

// MARK: - ⑧ Web search answer

struct WebAnswer: Equatable {
    var when: String
    var query: String
    var answer: String
    var detail: String
    var source: String
}

struct WebAnswerScreen: View {
    var item: WebAnswer
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        FrameLayout(icon: "globe", title: item.answer, lines: [item.detail, item.source],
                    buttons: buttons, u: u, perform: perform) {
            Text("\(item.when) · \(item.query)").font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
                .lineLimit(1)
        }
    }
}

// MARK: - ⑨ Hand off

struct HandOffStep: Equatable {
    var title: String
    var lines: [String]
}

/// Simulate (⑨), Exported (⑨d): a title and its lines.
struct HandOffScreen: View {
    var step: HandOffStep
    var icon: String
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        FrameLayout(icon: icon, title: step.title, lines: step.lines, buttons: buttons, u: u, perform: perform)
    }
}

struct ExportFile: Identifiable, Equatable {
    var id: String { name }
    var name: String
    var line: String
}

/// Export (⑨a) and Where to (⑨c): a question over a few named choices.
struct ExportChoiceScreen: View {
    var title: String
    var files: [ExportFile]
    var icon: String
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        FrameLayout(icon: nil, title: title, lines: [], buttons: buttons, u: u, perform: perform) {
            VStack(spacing: 14 * u) {
                ForEach(Array(files.enumerated()), id: \.element.id) { i, f in
                    HStack(spacing: 18 * u) {
                        Image(systemName: icon).foregroundStyle(Theme.polishBlue)
                        Text(f.name).fontWeight(.semibold).foregroundStyle(Theme.text1)
                        Spacer(minLength: 0)
                        Text(f.line).foregroundStyle(Theme.text2)
                    }
                    .font(.system(size: 26 * u)).lineLimit(1).minimumScaleFactor(0.6)
                    .padding(.horizontal, 28 * u).frame(height: 76 * u)
                    .background(RoundedRectangle(cornerRadius: 24 * u, style: .continuous).fill(Theme.wellEmpty))
                    .enter(.rise, 2 + i, u: u)
                }
            }
            .padding(.top, 12 * u)
        }
    }
}

struct FileRow: Identifiable, Equatable {
    var id: String { cells.joined() }
    var cells: [String]
}

/// Files (⑨b): one exported file as a table.
struct ExportFilesScreen: View {
    var name: String
    var header: [String]
    var rows: [FileRow]
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        VStack(spacing: 36 * u) {
            VStack(alignment: .leading, spacing: 0) {
                Text(name).font(.system(size: 28 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                    .padding(.bottom, 18 * u)
                row(header, head: true)
                ForEach(Array(rows.prefix(8).enumerated()), id: \.element.id) { i, r in
                    row(r.cells, head: false).enter(.text, i, u: u)
                }
            }
            .padding(.horizontal, 56 * u).padding(.vertical, 40 * u)
            .frame(width: 956 * u)
            .benchCard(radius: 44 * u)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 756 * u)
        }
    }

    private func row(_ cells: [String], head: Bool) -> some View {
        HStack(spacing: 0) {
            ForEach(Array(cells.enumerated()), id: \.offset) { _, c in
                Text(c).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .font(.system(size: (head ? 20 : 24) * u, weight: head ? .medium : .regular))
        .foregroundStyle(head ? Theme.text2 : Theme.text1)
        .lineLimit(1).padding(.vertical, 10 * u)
    }
}

#if DEBUG
private let previewButtons = [ScreenButton(say: "Continue", action: "back", args: nil),
                              ScreenButton(say: "Back", action: "back", args: nil)]

#Preview("⑦ Label check") {
    LabelCheckScreen(check: LabelCheck(verdict: "Mismatch", label: "Drug B working solution",
                                       change: "10 µM → 1 µM"), buttons: previewButtons, u: 0.7) { _ in }
}

#Preview("⑧ Web answer") {
    WebAnswerScreen(item: WebAnswer(when: "Oct 27 · 15:12", query: "PBS stock concentration?",
                                    answer: "Usually a 10× stock, used at 1×.",
                                    detail: "Dilute it 1 to 10 in water to use at 1×.",
                                    source: "Source title · example.org"),
                    buttons: previewButtons, u: 0.7) { _ in }
}

#Preview("⑨ Simulate") {
    HandOffScreen(step: HandOffStep(title: "Simulating…", lines: ["Virtual robot", "Checking volumes, wells, order"]),
                  icon: "cpu", buttons: Array(previewButtons.prefix(1)), u: 0.7) { _ in }
}

#Preview("⑨a Export") {
    ExportChoiceScreen(title: "Check the files?", files: [
        ExportFile(name: "plate_map.csv", line: "21 wells · 1 excluded"),
        ExportFile(name: "protocol.py", line: "5 steps · simulated")], icon: "doc",
                       buttons: previewButtons, u: 0.7) { _ in }
}

#Preview("⑨b Files") {
    ExportFilesScreen(name: "plate_map.csv", header: ["Well", "Group", "Status", "Source"], rows: [
        FileRow(cells: ["B4", "Control", "Done · note", "Voice"]),
        FileRow(cells: ["C3", "Drug A", "Excluded", "Voice"])], buttons: previewButtons, u: 0.7) { _ in }
}

#Preview("⑨c Where to") {
    ExportChoiceScreen(title: "Where should they go?", files: [
        ExportFile(name: "Robot", line: "Opentrons App"), ExportFile(name: "Files", line: "iPad or iCloud")],
                       icon: "arrow.up.right", buttons: previewButtons, u: 0.7) { _ in }
}

#Preview("⑨d Exported") {
    HandOffScreen(step: HandOffStep(title: "Sending…", lines: ["In the Opentrons App, ready to run",
                                                               "Matches the approved record",
                                                               "A copy stays in Records"]),
                  icon: "checkmark.circle", buttons: Array(previewButtons.prefix(1)), u: 0.7) { _ in }
}
#endif
