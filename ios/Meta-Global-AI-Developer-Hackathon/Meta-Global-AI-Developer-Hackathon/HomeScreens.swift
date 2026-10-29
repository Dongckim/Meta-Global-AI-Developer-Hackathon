import SwiftUI

// Home, ver 2: ⓪ Protocols (Figma 37:12877), ⓪b Run record (37:12931),
// ⓪c Web searches (37:13104, skeleton).
// Data: /protocols (short title, source file, steps, wells, run counts) and
// /runs (server labels: "Run 3", day, "Step 4 of 5" / "Ended at step 3" /
// "Finished"), a run's record for the run record.
// Server to do: robot code per run (Code tab); web search history (⓪c and
// the Searches count, registry status "planned").

/// Runs of one protocol, oldest first.
func runsOf(_ protocolId: String?, in runs: [RunSummary]) -> [RunSummary] {
    runs.filter { $0.protocolId == protocolId }.sorted { ($0.startedAt ?? 0) < ($1.startedAt ?? 0) }
}

/// Bottom row of registry buttons: the first is primary.
struct SayRow: View {
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        HStack(spacing: 36 * u) {
            ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                SayButton(button: b, kind: i == 0 ? .primary : .secondary, size: 34 * u, perform: perform)
                    .modifier(RowEntrance(primary: i == 0, order: i, u: u))
            }
        }
    }
}

private struct RowEntrance: ViewModifier {
    var primary: Bool
    var order: Int
    var u: CGFloat
    func body(content: Content) -> some View {
        if primary { content.modifier(CommandIn(u: u)) } else { content.enter(.rise, order + 1, u: u) }
    }
}

// MARK: - ⓪ Protocols

struct ProtocolsHome: View {
    var protocols: [ProtocolSummary]
    var runs: [RunSummary]
    /// The protocol shown on the right (protocol_detail target, else the
    /// active run's, else the newest).
    var selectedId: String?
    var activeRunId: String?
    /// Registry home buttons (Continue, Import protocol, Searches).
    var buttons: [ScreenButton]
    var u: CGFloat
    var onProtocol: (String) -> Void
    var onRun: (String) -> Void
    var perform: (ScreenButton) -> Void

    private var selected: ProtocolSummary? {
        protocols.first { $0.id == selectedId }
            ?? protocols.first { p in runs.contains { $0.runId == activeRunId && $0.protocolId == p.id } }
            ?? protocols.first  // the server lists the most recently used first
    }

    /// Cards that fit the column (design: three); the rest page through "+N more".
    private let perPage = 3
    @State private var page = 0
    /// Runs of the picked protocol, four at a time (newest first).
    private let runsPerPage = 4
    @State private var runPage = 0
    private var pages: Int { max(1, (protocols.count + perPage - 1) / perPage) }
    private var shown: ArraySlice<ProtocolSummary> {
        let p = min(page, pages - 1)
        return protocols[(p * perPage)..<min(protocols.count, (p + 1) * perPage)]
    }

    /// The Searches pill's button (it opens web_searches); the row takes the rest.
    private var searches: ScreenButton? { buttons.first { $0.args?["screen"] == "web_searches" } }
    /// Continue (back to the bench) only while a run is going.
    private var row: [ScreenButton] {
        buttons.filter { $0 != searches && ($0.args?["screen"] != "main" || activeRunId != nil) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 60 * u) {
                left.frame(width: 476 * u)
                    .onChange(of: selected?.id) { _, _ in runPage = 0 }
                    .onChange(of: selectedId, initial: true) { _, id in
                        // the protocol on the right is always one of the cards on the left
                        if let i = protocols.firstIndex(where: { $0.id == id }) { page = i / perPage }
                    }
                right.frame(maxWidth: .infinity, alignment: .leading)
            }
            Spacer(minLength: 24 * u)
            SayRow(buttons: row, u: u, perform: perform)
        }
    }

    private var left: some View {
        VStack(spacing: 18 * u) {
            ForEach(Array(shown.enumerated()), id: \.element.id) { i, p in
                let n = p.runs ?? runsOf(p.id, in: runs).count
                let live = p.inProgress ?? 0
                Button { onProtocol(p.id) } label: {
                    VStack(alignment: .leading, spacing: 4 * u) {
                        if p.id == selected?.id {
                            Text(p.title).font(.system(size: 34 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                        } else {
                            (Text("Say ").foregroundStyle(Theme.polishBlue)
                             + Text(p.title).foregroundStyle(Theme.text2))
                                .font(.system(size: 34 * u, weight: .semibold))
                        }
                        (Text(n == 1 ? "1 run" : "\(n) runs").foregroundStyle(Theme.text2)
                         + Text(live > 0 ? " · \(live) in progress" : "").foregroundStyle(Theme.polishBlue))
                            .font(.system(size: 22 * u))
                    }
                    .lineLimit(1).minimumScaleFactor(0.6)
                    .padding(.horizontal, 32 * u).padding(.vertical, 26 * u)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background {
                        if p.id == selected?.id {
                            Color.clear.benchCard(radius: 32 * u)
                        } else {
                            RoundedRectangle(cornerRadius: 32 * u, style: .continuous).fill(Theme.cardOff)
                        }
                    }
                }
                .buttonStyle(.plain)
                .enter(.left, i, u: u)
            }
            if pages > 1 {
                // every protocol stays reachable (finished ones too) without scrolling
                Button { withAnimation(Theme.Motion.ease(Theme.Motion.seg)) { page = (page + 1) % pages } } label: {
                    Text(page + 1 < pages ? "+\(protocols.count - (page + 1) * perPage) more" : "Back to the latest")
                        .font(.system(size: 22 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 32 * u)
                        .frame(height: 40 * u).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            Rectangle().fill(Theme.cardOff).frame(height: 2 * u).padding(.horizontal, 8 * u)
            if let b = searches {
                // ⓪c is planned: the server answers "That isn't ready yet."
                Button { perform(b) } label: {
                    HStack(spacing: 18 * u) {
                        Image(systemName: "globe").foregroundStyle(Theme.polishBlue)
                        (Text("Say ").foregroundStyle(Theme.polishBlue) + Text(b.say).foregroundStyle(Theme.text1))
                        Spacer(minLength: 0)
                    }
                    .font(.system(size: 28 * u))
                    .padding(.horizontal, 32 * u).frame(height: 80 * u)
                    .background(Capsule().fill(Theme.chip))
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder private var right: some View {
        if let p = selected {
            VStack(alignment: .leading, spacing: 0) {
                Text(p.title).font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u)
                    .foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.6)
                    .enter(.text, 0, u: u)
                HStack(spacing: 12 * u) {
                    Image(systemName: "doc.fill").foregroundStyle(Theme.polishBlue)
                    Text([p.sourceFilename, "\(p.steps) steps", p.wells.map { "\($0) wells" }].compactMap { $0 }
                            .joined(separator: " · "))
                        .foregroundStyle(Theme.text2).lineLimit(1).minimumScaleFactor(0.6)
                }
                .font(.system(size: 28 * u)).padding(.top, 14 * u)
                .enter(.rise, 1, u: u)
                Text("Runs").font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
                    .padding(.top, 40 * u).padding(.bottom, 12 * u)
                let rs = Array(runsOf(p.id, in: runs).reversed())
                let pages = max(1, (rs.count + runsPerPage - 1) / runsPerPage)
                let rp = min(runPage, pages - 1)
                VStack(spacing: 10 * u) {
                    ForEach(Array(rs[(rp * runsPerPage)..<min(rs.count, (rp + 1) * runsPerPage)].enumerated()),
                            id: \.element.runId) { i, r in
                        runRow(r, first: rp == 0 && i == 0).enter(.rise, 2 + i, u: u)
                    }
                }
                // the timeline through the run dots
                .background(alignment: .leading) {
                    Rectangle().fill(Theme.cardOff).frame(width: 2 * u).padding(.vertical, 40 * u)
                        .padding(.leading, 11 * u)
                }
                if pages > 1 {
                    Button { withAnimation(Theme.Motion.ease(Theme.Motion.seg)) { runPage = (rp + 1) % pages } } label: {
                        Text(rp + 1 < pages ? "+\(rs.count - (rp + 1) * runsPerPage) older runs" : "Back to the newest")
                            .font(.system(size: 22 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
                            .padding(.leading, 54 * u).frame(height: 44 * u)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func runRow(_ r: RunSummary, first: Bool) -> some View {
        Button { onRun(r.runId) } label: {
            HStack(spacing: 18 * u) {
                Circle().fill(first ? Theme.polishBlue : Theme.text4).frame(width: first ? 14 * u : 12 * u)
                    .frame(width: 30 * u, height: 30 * u)
                    .background { if first { Circle().fill(Theme.card) } }
                    .overlay(Circle().strokeBorder(first ? Theme.polishBlue.opacity(0.3) : .clear, lineWidth: 5 * u))
                    .frame(width: 24 * u)
                HStack(spacing: 18 * u) {
                    if first {
                        Text(r.label ?? "").font(.system(size: 28 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                    } else {
                        (Text("Say ").foregroundStyle(Theme.polishBlue)
                         + Text(r.label ?? "").foregroundStyle(Theme.polishBlue))
                            .font(.system(size: 28 * u, weight: .semibold))
                    }
                    Text(r.day ?? "").font(.system(size: 24 * u)).foregroundStyle(Theme.text2)
                    Spacer(minLength: 0)
                    Text(r.state ?? "")
                        .font(.system(size: 24 * u, weight: r.status == "active" ? .semibold : .regular))
                        .foregroundStyle(r.status == "active" ? Theme.polishBlue : Theme.text2)
                }
                .lineLimit(1).minimumScaleFactor(0.6)
                .padding(.horizontal, 30 * u).frame(height: 80 * u)
                .background { if first { Color.clear.benchCard(radius: 40 * u) } }
            }
        }
        .buttonStyle(.plain)
    }
}

// MARK: - ⓪b Run record

struct RunRecordScreen: View {
    var runId: String
    var runs: [RunSummary]
    /// Registry run_view buttons (Hand off, Back).
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    @State private var rlab = LabState()
    @State private var doc: RunDoc?
    @State private var error = ""
    @State private var tab = 0
    @State private var selected: WellSelect?

    private var summary: RunSummary? { runs.first { $0.runId == runId } }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 36 * u) {
                PlateView(lab: rlab, selected: $selected)
                    .padding(28 * u)
                    .frame(width: 800 * u, height: 566 * u)
                    .benchCard(radius: 44 * u)
                side.frame(maxWidth: .infinity, alignment: .leading)
            }
            Spacer(minLength: 24 * u)
            SayRow(buttons: buttons, u: u, perform: perform)
        }
        .task(id: runId) { await load() }
    }

    private var side: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(doc?.protocolName ?? summary?.protocolName ?? "")
                .font(.system(size: 28 * u)).foregroundStyle(Theme.text2).lineLimit(1)
            Text(summary?.label ?? "")
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1)
                .padding(.top, 8 * u)
            Text([summary?.day, summary?.state].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.system(size: 28 * u)).foregroundStyle(Theme.text1).padding(.top, 14 * u)
            tabs.padding(.top, 36 * u)
            Group {
                switch tab {
                case 0: notes
                case 1: timers
                default: Text("That isn't ready yet.").foregroundStyle(Theme.text2)  // Code: planned
                }
            }
            .font(.system(size: 28 * u))
            .padding(.top, 28 * u)
            .id(tab)
            if !error.isEmpty { Text(error).font(.system(size: 22 * u)).foregroundStyle(Theme.text2) }
        }
        .enter(.rise, u: u)
    }

    private var tabs: some View {
        HStack(spacing: 36 * u) {
            ForEach(Array(["Notes", "Timers", "Code"].enumerated()), id: \.offset) { i, t in
                Button { withAnimation(Theme.Motion.ease(Theme.Motion.seg)) { tab = i } } label: {
                    VStack(spacing: 10 * u) {
                        Text(t).font(.system(size: 26 * u, weight: tab == i ? .semibold : .regular))
                            .foregroundStyle(tab == i ? Theme.text1 : Theme.text2)
                        Capsule().fill(tab == i ? Theme.text1 : .clear).frame(height: 3 * u)
                    }
                    .fixedSize()
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var notes: some View {
        VStack(alignment: .leading, spacing: 20 * u) {
            ForEach(Array(rlab.notes.suffix(4).enumerated()), id: \.element.id) { i, m in
                HStack(alignment: .firstTextBaseline, spacing: 0) {
                    Text(m.target).foregroundStyle(Theme.text2).frame(width: 128 * u, alignment: .leading)
                    VStack(alignment: .leading, spacing: 4 * u) {
                        Text("“\(m.text)”").foregroundStyle(Theme.text1).lineLimit(2)
                        let meta = [m.source?.capitalized, m.step.map { "step \($0)" }].compactMap { $0 }
                        if !meta.isEmpty {
                            Text(meta.joined(separator: " · ")).font(.system(size: 20 * u)).foregroundStyle(Theme.text2)
                        }
                    }
                }
                .enter(.text, i, u: u)
            }
            if rlab.notes.count > 4 {
                Text("+\(rlab.notes.count - 4)").foregroundStyle(Theme.polishBlue)
            }
        }
    }

    private var timers: some View {
        VStack(alignment: .leading, spacing: 14 * u) {
            ForEach(Array(rlab.timers.suffix(4).enumerated()), id: \.element.id) { i, t in
                HStack {
                    Text(t.wells.isEmpty ? t.label : t.wells).fontWeight(.semibold)
                    Text(t.step).foregroundStyle(Theme.text2)
                    Spacer(minLength: 0)
                    Text(fmtNum(t.minutes) + " min")
                }
                .lineLimit(1).minimumScaleFactor(0.6)
                .enter(.text, i, u: u)
            }
        }
    }

    @MainActor
    private func load() async {
        error = ""
        do {
            let r = try await PipClient.runDetail(id: runId)
            doc = r.run
            rlab.sync(r.run.record)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - ⓪c Web searches (skeleton)

/// One past web search: what was asked, the answer's source, when.
struct WebSearchItem: Identifiable, Equatable {
    var id: String
    var query: String
    var source: String
    var day: String
}

/// Draws a list of searches; no data source yet (registry "planned").
struct WebSearchesScreen: View {
    var items: [WebSearchItem]
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        VStack(spacing: 36 * u) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { i, s in
                    HStack(spacing: 18 * u) {
                        Image(systemName: "globe").foregroundStyle(Theme.polishBlue)
                        VStack(alignment: .leading, spacing: 4 * u) {
                            Text(s.query).font(.system(size: 28 * u)).foregroundStyle(Theme.text1)
                            Text(s.source).font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
                        }
                        Spacer(minLength: 0)
                        Text(s.day).font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
                    }
                    .lineLimit(1)
                    .padding(.vertical, 18 * u)
                    .enter(.text, i, u: u)
                }
            }
            .padding(.horizontal, 48 * u).padding(.vertical, 24 * u)
            .frame(width: 756 * u)
            .benchCard(radius: 44 * u)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 756 * u)
        }
    }
}

#if DEBUG
#Preview("⓪c Web searches") {
    WebSearchesScreen(items: [
        WebSearchItem(id: "1", query: "MTT incubation time", source: "abcam.com", day: "Today"),
        WebSearchItem(id: "2", query: "DMSO max concentration", source: "sigmaaldrich.com", day: "Oct 27"),
    ], buttons: [ScreenButton(say: "Back", action: "back", args: nil)], u: 0.7) { _ in }
    .background(Theme.canvas)
}
#endif

// MARK: - ⑤a Mode, ⑤b Import (Start)
// ⑤a (Figma 37:13141): Full or Private, lines from pip_voice.json `modes`;
// Private is planned (the server answers "That isn't ready yet.").
// ⑤b (37:13167): Text / File / Photo from the registry import_choice
// buttons; File opens the importer, Text and Photo are planned.
// Server to do: a Private mode (on-device model, asks before anything
// leaves), text and photo import.

/// A big "Say Full" choice card with its line.
struct ChoiceCard: View {
    var icon: String
    var button: ScreenButton
    var line: String
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        Button { perform(button) } label: {
            VStack(spacing: 0) {
                Image(systemName: icon).font(.system(size: 40 * u)).foregroundStyle(Theme.text2)
                    .frame(height: 50 * u)
                (Text("Say ").font(.system(size: 52 * u)).foregroundStyle(Theme.polishBlue)
                 + Text(button.say).font(.system(size: 64 * u, weight: .semibold)).foregroundStyle(Theme.text1))
                    .tracking(-1.2 * u).lineLimit(1).minimumScaleFactor(0.6)
                    .padding(.top, 70 * u)
                Text(line).font(.system(size: 28 * u)).foregroundStyle(Theme.text2).lineLimit(1)
                    .minimumScaleFactor(0.6).padding(.top, 22 * u)
            }
            .padding(.horizontal, 24 * u)
            .frame(maxWidth: .infinity, minHeight: 345 * u)
            .benchCard(radius: 44 * u, fill: Theme.cardGlass)
            .overlay(RoundedRectangle(cornerRadius: 44 * u, style: .continuous)
                .strokeBorder(Theme.card, lineWidth: 2 * u))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(button.say)
    }
}

struct ModeScreen: View {
    /// Registry mode buttons (Full, Private).
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    private func mode(_ b: ScreenButton) -> Persona.Mode? { Persona.modes[b.say.lowercased()] }

    var body: some View {
        VStack(spacing: 44 * u) {
            Text(buttons.map { mode($0)?.label ?? $0.say }.joined(separator: " or ") + "?")
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1)
                .enter(.text, u: u)
            HStack(spacing: 36 * u) {
                ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                    ChoiceCard(icon: b.say.lowercased() == "private" ? "lock.fill" : "cloud.fill", button: b,
                               line: mode(b)?.line ?? "", u: u, perform: perform)
                        .enter(i == 0 ? .left : .right, i + 1, u: u)
                }
            }
            .frame(width: 1122 * u)
        }
        .frame(maxHeight: .infinity)
    }
}

struct ImportChoiceScreen: View {
    /// Registry import_choice buttons (Text, File, Photo) with their lines.
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        VStack(spacing: 44 * u) {
            // "Text, file, or photo?" from the choices themselves
            let words = buttons.enumerated().map { $0.offset == 0 ? $0.element.say : $0.element.say.lowercased() }
            Text(words.dropLast().joined(separator: ", ") + (words.count > 1 ? ", or " : "") + (words.last ?? "") + "?")
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1)
                .enter(.text, u: u)
            HStack(spacing: 36 * u) {
                ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                    ChoiceCard(icon: ["text.alignleft", "doc.fill", "camera.fill"][min(i, 2)], button: b, line: b.line ?? "",
                               u: u, perform: perform)
                        .enter(.rise, i + 1, u: u)
                }
            }
            .frame(width: 1248 * u)
        }
        .frame(maxHeight: .infinity)
    }
}
