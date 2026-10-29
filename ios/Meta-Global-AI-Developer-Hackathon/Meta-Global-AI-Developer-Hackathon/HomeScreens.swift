import SwiftUI

// Home, ver 2: ⓪ Protocols (Figma 37:12877), ⓪b Run record (37:12931),
// ⓪c Web searches (37:13104, skeleton).
// Data: /protocols and /runs lists (name, source file, steps, conditions;
// run status and times), a run's record for the run record.
// Server to do: a run's step reached ("Step 4 of 5", "Ended at step 3") and
// a protocol's well count in the lists; robot code per run (Code tab); web
// search history (⓪c, registry status "planned").

/// Runs of one protocol, oldest first, so a run's number is its place.
func runsOf(_ protocolId: String?, in runs: [RunSummary]) -> [RunSummary] {
    runs.filter { $0.protocolId == protocolId }.sorted { ($0.startedAt ?? 0) < ($1.startedAt ?? 0) }
}

/// "Today" or "Oct 27".
func dayLabel(_ ts: Double?) -> String {
    guard let ts else { return "" }
    let d = Date(timeIntervalSince1970: ts)
    return Calendar.current.isDateInToday(d) ? "Today" : d.formatted(.dateTime.month(.abbreviated).day())
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
            ?? protocols.max { ($0.importedAt ?? 0) < ($1.importedAt ?? 0) }
    }

    private func button(_ say: String) -> ScreenButton? { buttons.first { $0.say == say } }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 60 * u) {
                left.frame(width: 476 * u)
                right.frame(maxWidth: .infinity, alignment: .leading)
            }
            Spacer(minLength: 24 * u)
            SayRow(buttons: [activeRunId != nil ? button("Continue") : nil, button("Import protocol")]
                    .compactMap { $0 }, u: u, perform: perform)
        }
    }

    private var left: some View {
        VStack(spacing: 14 * u) {
            ForEach(Array(protocols.prefix(4).enumerated()), id: \.element.id) { i, p in
                let rs = runsOf(p.id, in: runs)
                let live = rs.filter { $0.status == "active" }.count
                Button { onProtocol(p.id) } label: {
                    VStack(alignment: .leading, spacing: 4 * u) {
                        if p.id == selected?.id {
                            Text(p.name).font(.system(size: 34 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                        } else {
                            (Text("Say ").foregroundStyle(Theme.polishBlue)
                             + Text(p.name).foregroundStyle(Theme.text2))
                                .font(.system(size: 34 * u, weight: .semibold))
                        }
                        (Text("\(rs.count) runs").foregroundStyle(Theme.text2)
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
            Rectangle().fill(Theme.cardOff).frame(height: 2 * u).padding(.horizontal, 8 * u)
            if let b = button("Searches") {
                // ⓪c is planned: the server answers "That isn't ready yet."
                Button { perform(b) } label: {
                    HStack(spacing: 18 * u) {
                        Image(systemName: "globe").foregroundStyle(Theme.polishBlue)
                        (Text("Say ").foregroundStyle(Theme.polishBlue) + Text(b.say).foregroundStyle(Theme.text1))
                        Spacer(minLength: 0)
                    }
                    .font(.system(size: 28 * u))
                    .padding(.horizontal, 32 * u).frame(height: 84 * u)
                    .background(Capsule().fill(Theme.chip))
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder private var right: some View {
        if let p = selected {
            VStack(alignment: .leading, spacing: 0) {
                Text(p.name).font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u)
                    .foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.6)
                    .enter(.text, 0, u: u)
                HStack(spacing: 12 * u) {
                    Image(systemName: "doc.fill").foregroundStyle(Theme.polishBlue)
                    Text([p.sourceFilename, "\(p.steps) steps", "\(p.conditions) groups"].compactMap { $0 }
                            .joined(separator: " · "))
                        .foregroundStyle(Theme.text2).lineLimit(1).minimumScaleFactor(0.6)
                }
                .font(.system(size: 28 * u)).padding(.top, 14 * u)
                .enter(.rise, 1, u: u)
                Text("Runs").font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
                    .padding(.top, 40 * u).padding(.bottom, 12 * u)
                let rs = runsOf(p.id, in: runs)
                ForEach(Array(rs.enumerated().reversed().prefix(4)), id: \.element.runId) { n, r in
                    runRow(r, number: n + 1, first: r.runId == rs.last?.runId)
                        .enter(.rise, 2 + rs.count - n, u: u)
                }
            }
        }
    }

    private func runRow(_ r: RunSummary, number: Int, first: Bool) -> some View {
        Button { onRun(r.runId) } label: {
            HStack(spacing: 18 * u) {
                Circle().fill(first ? Theme.polishBlue : Theme.text4).frame(width: 12 * u, height: 12 * u)
                    .overlay(Circle().strokeBorder(first ? Theme.polishBlue.opacity(0.3) : .clear,
                                                   lineWidth: 4 * u).frame(width: 24 * u, height: 24 * u))
                    .frame(width: 24 * u)
                HStack(spacing: 18 * u) {
                    if first {
                        Text("Run \(number)").font(.system(size: 28 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                    } else {
                        (Text("Say ").foregroundStyle(Theme.polishBlue)
                         + Text("Run \(number)").foregroundStyle(Theme.polishBlue))
                            .font(.system(size: 28 * u, weight: .semibold))
                    }
                    Text(dayLabel(r.startedAt)).font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
                    Spacer(minLength: 0)
                    Text(r.status == "active" ? "In progress" : "Finished")
                        .font(.system(size: 22 * u, weight: r.status == "active" ? .semibold : .regular))
                        .foregroundStyle(r.status == "active" ? Theme.polishBlue : Theme.text2)
                }
                .lineLimit(1).minimumScaleFactor(0.6)
                .padding(.horizontal, 28 * u).frame(height: 76 * u)
                .background { if first { Color.clear.benchCard(radius: 24 * u) } }
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
    private var number: Int? {
        runsOf(summary?.protocolId, in: runs).firstIndex { $0.runId == runId }.map { $0 + 1 }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 36 * u) {
                VStack(spacing: 0) {
                    PlateView(lab: rlab, selected: $selected)
                        .padding(28 * u)
                        .frame(width: 800 * u, height: 566 * u)
                        .benchCard(radius: 44 * u)
                    ConditionBars(lab: rlab, u: u).frame(height: 97 * u).padding(.top, 24 * u)
                }
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
            Text(number.map { "Run \($0)" } ?? "Run")
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1)
                .padding(.top, 8 * u)
            Text([dayLabel(doc?.startedAt), doc?.status == "active" ? "In progress" : "Finished"]
                    .filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.system(size: 28 * u)).foregroundStyle(Theme.text1).padding(.top, 14 * u)
            tabs.padding(.top, 36 * u)
            Group {
                switch tab {
                case 0: notes
                case 1: timers
                default: Text("That isn't ready yet.").foregroundStyle(Theme.text2)  // Code: planned
                }
            }
            .font(.system(size: 26 * u))
            .padding(.top, 24 * u)
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
                        Capsule().fill(tab == i ? Theme.polishBlue : .clear).frame(height: 4 * u)
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
                HStack(alignment: .firstTextBaseline, spacing: 24 * u) {
                    Text(m.target).fontWeight(.semibold).foregroundStyle(Theme.text1)
                    VStack(alignment: .leading, spacing: 4 * u) {
                        Text(m.text).foregroundStyle(Theme.text1).lineLimit(2)
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
            VStack(alignment: .leading, spacing: 18 * u) {
                Image(systemName: icon).font(.system(size: 40 * u)).foregroundStyle(Theme.polishBlue)
                (Text("Say ").font(.system(size: 52 * u)).foregroundStyle(Theme.polishBlue)
                 + Text(button.say).font(.system(size: 64 * u, weight: .semibold)).foregroundStyle(Theme.text1))
                    .tracking(-1.2 * u).lineLimit(1).minimumScaleFactor(0.6)
                Text(line).font(.system(size: 28 * u)).foregroundStyle(Theme.text1).lineLimit(2)
            }
            .padding(48 * u)
            .frame(maxWidth: .infinity, minHeight: 360 * u, alignment: .topLeading)
            .benchCard(radius: 44 * u)
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
        VStack(spacing: 48 * u) {
            Text(buttons.map { mode($0)?.label ?? $0.say }.joined(separator: " or ") + "?")
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1)
                .enter(.text, u: u)
            HStack(spacing: 36 * u) {
                ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                    ChoiceCard(icon: b.say.lowercased() == "private" ? "lock" : "cloud", button: b, line: mode(b)?.line ?? "",
                               u: u, perform: perform)
                        .enter(i == 0 ? .left : .right, i + 1, u: u)
                }
            }
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
        VStack(spacing: 48 * u) {
            // "Text, file, or photo?" from the choices themselves
            let words = buttons.enumerated().map { $0.offset == 0 ? $0.element.say : $0.element.say.lowercased() }
            Text(words.dropLast().joined(separator: ", ") + (words.count > 1 ? ", or " : "") + (words.last ?? "") + "?")
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1)
                .enter(.text, u: u)
            HStack(spacing: 36 * u) {
                ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                    ChoiceCard(icon: ["text.alignleft", "doc", "camera"][min(i, 2)], button: b, line: b.line ?? "",
                               u: u, perform: perform)
                        .enter(.rise, i + 1, u: u)
                }
            }
        }
        .frame(maxHeight: .infinity)
    }
}
