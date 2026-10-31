import SwiftUI

// Home, ver 2: ⓪ Protocols (Figma 37:12877), ⓪b Run record (37:12931),
// ⓪c Web searches (37:13104, skeleton).
// Data: /protocols (short title, source file, steps, wells, run counts) and
// /runs (server labels: "Run 3", day, "Step 4 of 5" / "Ended at step 3" /
// "Finished"), a run's record for the run record.
// Code tab: the run's protocol.py beside its final protocol steps (RunCodeView).

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
    /// Answered web searches (the Searches pill's count).
    var searchCount = 0
    /// Registry home buttons (Continue, Import protocol, Searches).
    var buttons: [ScreenButton]
    var u: CGFloat
    var onProtocol: (String) -> Void
    var onRun: (String) -> Void
    var perform: (ScreenButton) -> Void
    /// The open draft's title (shown with Review while it waits).
    var draftTitle = ""
    /// The run record just left: its page of runs shows again (this view is
    /// rebuilt after a run record, so its own page state starts over).
    var lastRunId: String? = nil

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

    /// The Searches pill's button (it opens web_searches).
    private var searches: ScreenButton? { buttons.first { $0.args?["screen"] == "web_searches" } }
    /// A draft waiting (registry Review, shown while one is open): its card on top of the list.
    private var review: ScreenButton? { buttons.first { $0.action == "review_draft" } }
    /// The bottom row: the rest, as the server's state allows (Continue only
    /// while a run goes, Start run on a picked protocol).
    private var row: [ScreenButton] { buttons.filter { $0 != searches && $0 != review } }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: Theme.Layout.listGap * u) {
                left.frame(width: Theme.Layout.listWidth * u)
                    .onChange(of: selected?.id) { _, _ in runPage = 0 }
                    .onChange(of: selectedId, initial: true) { _, id in
                        // the protocol on the right is always one of the cards on the left
                        if let i = protocols.firstIndex(where: { $0.id == id }) { page = i / perPage }
                    }
                right.frame(maxWidth: .infinity, alignment: .leading)
                    .onAppear {
                        let rs = Array(runsOf(selected?.id, in: runs).reversed())
                        if let i = rs.firstIndex(where: { $0.runId == lastRunId }) { runPage = i / runsPerPage }
                    }
            }
            Spacer(minLength: 24 * u)
            SayRow(buttons: row, u: u, perform: perform)
        }
    }

    private var left: some View {
        VStack(spacing: Theme.Layout.listRowGap * u) {
            if let review {
                // not saved yet: why it isn't a protocol here, and the way on
                Button { perform(review) } label: {
                    VStack(alignment: .leading, spacing: 4 * u) {
                        Text(draftTitle).font(Theme.ui(34 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                        (Text(review.line ?? "").foregroundStyle(Theme.text2) + Text("  ·  ")
                         + Say.prefix(Theme.text2) + Text(review.say).fontWeight(.semibold).foregroundStyle(Theme.polishBlue))
                            .font(Theme.ui(22 * u))
                    }
                    .lineLimit(1).minimumScaleFactor(0.6)
                    .padding(.horizontal, 32 * u).padding(.vertical, 26 * u)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 32 * u, style: .continuous)
                        .strokeBorder(Theme.polishBlue.opacity(0.5), style: StrokeStyle(lineWidth: 2 * u, dash: [8 * u, 6 * u])))
                }
                .buttonStyle(.plain)
            }
            ForEach(Array(shown.enumerated()), id: \.element.id) { i, p in
                let n = p.runs ?? runsOf(p.id, in: runs).count
                let live = p.inProgress ?? 0
                // the server says whether the row is on (a done protocol isn't); nothing is worked out here
                let on = p.enabled ?? true
                // a list row shows its name only (Say belongs to buttons); an off
                // row (done) takes no tap, as the server takes no words for it
                HStack(spacing: 16 * u) {
                    Button { onProtocol(p.id) } label: {
                        VStack(alignment: .leading, spacing: 4 * u) {
                            Text(p.title).font(Theme.ui(34 * u, weight: .semibold))
                                .foregroundStyle(p.id == selected?.id ? Theme.text1 : on ? Theme.text2 : Theme.text4)
                                .truncationMode(.tail)
                            (Text(n == 1 ? "1 run" : "\(n) runs").foregroundStyle(Theme.text2)
                             + Text(live > 0 ? " · \(live) in progress" : "").foregroundStyle(Theme.polishBlue))
                                .font(Theme.ui(22 * u)).minimumScaleFactor(0.6)
                        }
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!on)
                }
                .padding(.horizontal, 32 * u)
                .frame(minHeight: Theme.Layout.listRow * u)
                .background {
                    if p.id == selected?.id {
                        Color.clear.benchCard(radius: 32 * u)
                    } else {
                        RoundedRectangle(cornerRadius: 32 * u, style: .continuous).fill(Theme.cardOff)
                    }
                }
                .enter(.left, i, u: u)
            }
            if pages > 1 {
                // every protocol stays reachable (finished ones too) without scrolling
                Button { withAnimation(Theme.Motion.ease(Theme.Motion.seg)) { page = (page + 1) % pages } } label: {
                    Text(page + 1 < pages ? "+\(protocols.count - (page + 1) * perPage) more" : "Back to the latest")
                        .font(Theme.ui(22 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 32 * u)
                        .frame(height: 40 * u).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            Rectangle().fill(Theme.cardOff).frame(height: 2 * u).padding(.horizontal, 8 * u)
                .padding(.vertical, 4 * u)  // 20 from the last card and to Searches
            if let b = searches {
                Button { perform(b) } label: {
                    HStack(spacing: 18 * u) {
                        Image(systemName: "globe").foregroundStyle(Theme.polishBlue)
                        Text(b.say).foregroundStyle(Theme.text1)  // a list row: its name only
                        Spacer(minLength: 0)
                        if searchCount > 0 { Text("\(searchCount)").foregroundStyle(Theme.text2) }
                    }
                    .font(Theme.ui(28 * u))
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
                Text(p.title).font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                    .foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.6)
                    .enter(.text, 0, u: u)
                HStack(spacing: 12 * u) {
                    Image(systemName: "doc.fill").foregroundStyle(Theme.polishBlue)
                    Text([p.sourceFilename, "\(p.steps) steps", p.wells.map { "\($0) wells" }].compactMap { $0 }
                            .joined(separator: " · "))
                        .foregroundStyle(Theme.text2).lineLimit(1).minimumScaleFactor(0.6)
                }
                .font(Theme.ui(28 * u)).padding(.top, 14 * u)
                .enter(.rise, 1, u: u)
                Text("Runs").font(Theme.ui(22 * u)).foregroundStyle(Theme.text2)
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
                            .font(Theme.ui(22 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
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
                    // a list row: its name only
                    Text(r.label ?? "").font(Theme.ui(28 * u, weight: .semibold))
                        .foregroundStyle(first ? Theme.text1 : Theme.text5)
                    Text(r.day ?? "").font(Theme.ui(24 * u)).foregroundStyle(Theme.text2)
                    Spacer(minLength: 0)
                    Text(r.state ?? "")
                        .font(Theme.ui(24 * u, weight: r.status == "finished" ? .regular : .semibold))
                        .foregroundStyle(Theme.runState(r.status))
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
    /// Web searches asked during this run (Searches tab).
    var searches: [WebSearch] = []
    /// Registry run_view buttons (Continue or a greyed Start run, Back).
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    var onSearch: (String) -> Void = { _ in }
    @State private var rlab = LabState()
    @State private var doc: RunDoc?
    @State private var error = ""
    @State private var tab = 0
    @State private var selected: WellSelect?
    /// Code tab: the run's robot code, and the step lit in it (0: setup).
    @State private var code: RunCode?
    @State private var codeStep: Int?
    /// Server `has_code`: a run saved without robot code has no Code tab.
    @State private var hasCode = false

    private var summary: RunSummary? { runs.first { $0.runId == runId } }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: Theme.Layout.column * u) {
                Group {
                    if tab == 3, let code {
                        // Code tab: protocol.py where the plate was, each step's lines marked
                        RunCodeView(code: code, selected: $codeStep, u: u)
                    } else {
                        PlateView(lab: rlab, selected: $selected).padding(28 * u)
                    }
                }
                    .frame(width: 800 * u, height: 566 * u)
                    .benchCard(radius: 44 * u)
                side.frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.top, Theme.Layout.benchTop * u)  // the bench kind: plate at 222
            Spacer(minLength: 24 * u)
            SayRow(buttons: buttons, u: u, perform: perform)
        }
        .task(id: runId) { await load() }
    }

    private var side: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(doc?.protocolName ?? summary?.protocolName ?? "")
                .font(Theme.ui(28 * u)).foregroundStyle(Theme.text2).lineLimit(1)
            Text(summary?.label ?? "")
                .font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1).displayLine(u)
                .padding(.top, 8 * u)
            Text([summary?.day, summary?.state].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                .font(Theme.ui(28 * u)).foregroundStyle(Theme.text1).padding(.top, 14 * u)
            tabs.padding(.top, 36 * u)
            Group {
                switch tab {
                case 0: notes
                case 1: timers
                case 2: searchList
                default:
                    if let code {
                        RunCodeSteps(code: code, selected: $codeStep, u: u)
                    } else {
                        Text("No robot code yet. Say \u{201C}make the robot protocol\u{201D}.").foregroundStyle(Theme.text2)
                    }
                }
            }
            .font(Theme.ui(28 * u))
            .padding(.top, 28 * u)
            .id(tab)
            if !error.isEmpty { Text(error).font(Theme.ui(22 * u)).foregroundStyle(Theme.text2) }
        }
        .enter(.rise, u: u)
    }

    private var tabs: some View {
        HStack(spacing: 36 * u) {
            ForEach(Array((hasCode ? ["Notes", "Timers", "Searches", "Code"] : ["Notes", "Timers", "Searches"])
                .enumerated()), id: \.offset) { i, t in
                Button { withAnimation(Theme.Motion.ease(Theme.Motion.seg)) { tab = i } } label: {
                    VStack(spacing: 10 * u) {
                        Text(t).font(Theme.ui(26 * u, weight: tab == i ? .semibold : .regular))
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
                            Text(meta.joined(separator: " · ")).font(Theme.ui(20 * u)).foregroundStyle(Theme.text2)
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

    private var searchList: some View {
        VStack(alignment: .leading, spacing: 18 * u) {
            ForEach(Array(searches.prefix(4).enumerated()), id: \.element.id) { i, s in
                Button { onSearch(s.id) } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 0) {
                        Text(s.time).foregroundStyle(Theme.polishBlue).frame(width: 110 * u, alignment: .leading)
                        VStack(alignment: .leading, spacing: 4 * u) {
                            Text(s.question).foregroundStyle(Theme.text1)
                            Text(s.answerShort).font(Theme.ui(20 * u)).foregroundStyle(Theme.text2)
                        }
                        .lineLimit(1)
                    }
                }
                .buttonStyle(.plain)
                .enter(.text, i, u: u)
            }
            if searches.isEmpty { Text("No web searches in this run.").foregroundStyle(Theme.text2) }
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
            code = r.handoff
            hasCode = r.has_code ?? false
            if !hasCode, tab == 3 { tab = 0 }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - ⑤a Mode, ⑤b Import (Start)
// ⑤a (Figma 37:13141): Full or Private, lines from pip_voice.json `modes`;
// Private waits for its model (PrivateWaitScreen), then goes on to ⑤b.
// ⑤b (37:13167): Text / File / Photo from the registry import_choice
// buttons; File opens the importer, Text and Photo are planned.

/// A big "Say Full" choice card with its line.
struct ChoiceCard: View {
    var icon: String
    var button: ScreenButton
    var line: String
    var detail = ""
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        Button { perform(button) } label: {
            VStack(spacing: 0) {
                Image(systemName: icon).font(Theme.ui(40 * u)).foregroundStyle(Theme.text2)
                    .frame(height: 50 * u)
                (Say.prefix(Theme.polishBlue).font(Theme.ui(52 * u))
                 + Text(button.say).font(Theme.ui(64 * u, weight: .semibold)).foregroundStyle(Theme.text1))
                    .tracking(-1.2 * u).lineLimit(1).minimumScaleFactor(0.6)
                    .padding(.top, 70 * u)
                Text(line).font(Theme.ui(28 * u)).foregroundStyle(Theme.text2).lineLimit(2)
                    .multilineTextAlignment(.center).minimumScaleFactor(0.6).padding(.top, 22 * u)
                if !detail.isEmpty {
                    Text(detail).font(Theme.ui(22 * u)).foregroundStyle(Theme.text3)
                        .multilineTextAlignment(.center).padding(.top, 10 * u)
                }
            }
            .padding(.horizontal, 24 * u)
            .padding(.vertical, 24 * u)
            .frame(maxWidth: .infinity, minHeight: 345 * u, maxHeight: .infinity)
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
        VStack(spacing: Theme.Layout.titleGap * u) {
            Text(buttons.map { mode($0)?.label ?? $0.say }.joined(separator: " or ") + "?")
                .font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1).displayLine(u)
                .enter(.text, u: u)
            HStack(spacing: 36 * u) {
                ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                    ChoiceCard(icon: b.say.lowercased() == "private" ? "lock.fill" : "cloud.fill", button: b,
                               line: mode(b)?.line ?? "", detail: mode(b)?.detail ?? "", u: u, perform: perform)
                        .enter(i == 0 ? .left : .right, i + 1, u: u)
                }
            }
            .fixedSize(horizontal: false, vertical: true)  // a long detail grows both cards, never cuts
            .frame(width: 1122 * u)
        }
        .padding(.bottom, Theme.Layout.choiceLift * u)
        .frame(maxHeight: .infinity)
    }
}

/// Private's model is waking (not in the design): the next screen as a
/// skeleton, the server's line and seconds over it; failed: a card with the
/// registry's buttons. The server decides all of it (snapshot `private`).
struct PrivateWaitScreen: View {
    var wake: RecordSnapshot.PrivateWake
    /// The screen that opens once it's awake (view target).
    var next: String
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        ZStack {
            skeleton
            if wake.state == "failed" { failed } else { waiting }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var skeleton: some View {
        let cards = next == "import_choice" ? 3 : 1
        return HStack(spacing: 36 * u) {
            ForEach(0..<cards, id: \.self) { _ in
                RoundedRectangle(cornerRadius: 44 * u, style: .continuous).fill(Theme.cardGlass)
                    .overlay {
                        Sheen(period: Theme.Motion.shimmer, peak: Theme.Motion.shimmerPeak)
                            .clipShape(RoundedRectangle(cornerRadius: 44 * u, style: .continuous))
                    }
                    .frame(maxWidth: .infinity).frame(height: 345 * u)
            }
        }
        .frame(width: 1248 * u)
        .padding(.bottom, Theme.Layout.choiceLift * u)
        .accessibilityHidden(true)
    }

    private var waiting: some View {
        VStack(spacing: 18 * u) {
            ZStack {
                Text(wake.line).id(wake.line).transition(.opacity)
                    .font(Theme.ui(40 * u, weight: .semibold)).tracking(-0.8 * u).foregroundStyle(Theme.text1)
                    .multilineTextAlignment(.center)
            }
            .animation(Theme.Motion.ease(Theme.Motion.lineFade), value: wake.line)
            Text("\(wake.elapsedS) s").font(Theme.ui(28 * u)).monospacedDigit().foregroundStyle(Theme.text2)
        }
        .padding(.horizontal, 48 * u).padding(.vertical, 36 * u)
        .frame(width: 1000 * u)
        .benchCard(radius: 44 * u, fill: Theme.card)
    }

    private var failed: some View {
        VStack(spacing: 40 * u) {
            Text(wake.line).font(Theme.ui(40 * u, weight: .semibold)).tracking(-0.8 * u)
                .foregroundStyle(Theme.text1).multilineTextAlignment(.center)
            HStack(spacing: 36 * u) {
                ForEach(Array(buttons.filter { $0.voiceOnly != true }.enumerated()), id: \.offset) { i, b in
                    SayButton(button: b, kind: i == 0 ? .primary : .secondary, size: 34 * u, perform: perform)
                        .frame(width: 360 * u)
                }
            }
        }
        .padding(48 * u)
        .frame(width: 900 * u)
        .benchCard(radius: 44 * u, fill: Theme.card)
    }
}

struct ImportChoiceScreen: View {
    /// Registry import_choice buttons (Text, File, Photo) with their lines.
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        VStack(spacing: Theme.Layout.titleGap * u) {
            // "Text, file, or photo?" from the choices themselves
            let words = buttons.enumerated().map { $0.offset == 0 ? $0.element.say : $0.element.say.lowercased() }
            Text(words.dropLast().joined(separator: ", ") + (words.count > 1 ? ", or " : "") + (words.last ?? "") + "?")
                .font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).foregroundStyle(Theme.text1).displayLine(u)
                .enter(.text, u: u)
            HStack(spacing: 36 * u) {
                ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                    ChoiceCard(icon: ["text.alignleft", "doc.fill", "camera.fill"][min(i, 2)], button: b, line: b.line ?? "",
                               u: u, perform: perform)
                        .enter(.rise, i + 1, u: u)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
            .frame(width: 1248 * u)
        }
        .padding(.bottom, Theme.Layout.choiceLift * u)
        .frame(maxHeight: .infinity)
    }
}
