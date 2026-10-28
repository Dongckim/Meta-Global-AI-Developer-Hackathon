import AVFoundation
import SwiftUI
import UniformTypeIdentifiers

struct DebugRun {
    var input: String
    var calls: [(name: String, args: String)]
    var ms: [String: Int]
    var route: String?
    var sources: [String]
    var recordVersion: Int
}

/// Reliable press-and-hold: tracks the button's own pressed state, no gesture fights.
struct HoldStyle: ButtonStyle {
    let onHold: (Bool) -> Void
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .onChange(of: configuration.isPressed) { _, pressed in onHold(pressed) }
    }
}

enum RobotState {
    case idle
    case building
    case done(RobotResponse)
}

enum DraftState {
    case idle
    case importing
    case reviewing
}

/// Server-driven text field: edits commit one draft path on submit.
private struct DraftField: View {
    let initial: String
    let onCommit: (String) -> Void
    @State private var text = ""
    var body: some View {
        TextField("", text: $text)
            .textFieldStyle(.roundedBorder)
            .onAppear { text = initial }
            .onChange(of: initial) { _, v in text = v }
            .onSubmit { onCommit(text) }
    }
}

struct ViewingSource: Equatable {
    var target: String
    var page: Int?
    var phrase: String?
}

/// Right column, top to bottom: step → Pip response (fixed slot) → timers →
/// memos → log (fills rest) → input. Nothing overlays anything.
struct ContentView: View {
    @State private var lab = LabState()
    @State private var input = ""
    @State private var log: [String] = ["Pip ready — speak or type"]
    @State private var debug: DebugRun?
    @State private var showDebug = false
    @State private var showSettings = false
    @State private var lastReply = ""
    @State private var voice = VoiceSession()
    @State private var busy = false
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedWell: WellSelect?
    @State private var peekGen = 0
    @State private var peekTarget: String?
    @State private var lastViewScreen = ""
    @State private var robotState = RobotState.idle
    @State private var draftState = DraftState.idle
    @State private var reviewTab = 0
    @State private var showImporter = false
    @State private var ttsOn = SpeechBus.shared.enabled
    @State private var routeLine = ""
    @AppStorage("pip.setupDone") private var setupDone = false
    @State private var showHome = false
    @State private var selectedProtocol: String?
    @State private var viewingRun: String?
    @State private var viewingSource: ViewingSource?
    @State private var protocols: [ProtocolSummary] = []
    @State private var runs: [RunSummary] = []
    @State private var reviewError = ""
    @State private var reviewBusy = false
    @State private var detailNotice = ""
    @State private var syncError: String?
    @State private var apiMismatch: String?
    private let screens = loadScreens()
    var dat: GlassesDAT? = nil
    @State private var showSplash = true
    @State private var showSync = false
    @State private var showSyncCover = false
    @State private var syncRefreshGen = 0
    @AppStorage("pip.syncSkipped") private var syncSkipped = false

    /// Screen for hints: the server-owned view (setup boots app-local).
    private var currentScreen: String {
        if !setupDone { return "setup" }
        let s = lab.view.current.screen
        return s.isEmpty ? ((showHome || lab.run == nil) ? "home" : "main") : s
    }

    /// Mirror the server-owned view into local presentation state.
    /// Runs after every snapshot sync; taps send Actions instead of
    /// setting this state directly.
    private func renderView() {
        let v = lab.view
        let cur = v.current.screen, tgt = v.current.target
        guard !cur.isEmpty else { return }
        let sheetWell = v.sheet.name == "well_card" && !v.sheet.target.isEmpty ? v.sheet.target
            : (cur == "well_card" && !tgt.isEmpty ? tgt : nil)
        if let w = sheetWell {
            if selectedWell?.id != w { selectedWell = WellSelect(id: w) }
        } else if selectedWell != nil, selectedWell?.id != peekTarget {
            selectedWell = nil
        }
        viewingRun = cur == "run_view" && !tgt.isEmpty ? tgt : nil
        selectedProtocol = cur == "protocol_detail" && !tgt.isEmpty ? tgt : nil
        if cur == "protocol_source", !tgt.isEmpty {
            if viewingSource?.target != tgt { openSourceTarget(tgt) }
        } else {
            viewingSource = nil
        }
        if cur == "robot_preview" {
            if case .idle = robotState { runRobot() }
        } else if case .idle = robotState {} else { robotState = .idle }
        if cur == "protocol_review" {
            if lab.draft != nil, case .idle = draftState { draftState = .reviewing }
        }
        showHome = cur == "home"
        if cur == "setup", setupDone, lastViewScreen != "setup" {
            setupDone = false
            showSyncCover = false
        }
        if cur == "sync", !showSync, lastViewScreen != "sync" {
            showSyncCover = true
        }
        if let i = ["liquids", "steps", "layout"].firstIndex(of: v.tab) { reviewTab = i }
        if cur != lastViewScreen, cur == "home" || cur == "protocol_detail" { loadLists() }
        lastViewScreen = cur
    }

    /// Launch flow: splash, then the sync gate unless glasses are
    /// already connected or the user skipped (Setup comes after).
    private func dismissSplash() {
        showSplash = false
        showSync = SyncGate.needed(
            rows: SyncGate.rows(dat: dat, hfpName: SyncGate.liveHFPName),
            skipped: syncSkipped)
    }

    private func skipSyncGate() {
        syncSkipped = true
        showSync = false
    }

    /// Voice "skip the glasses check": leave the sync UI when it is up,
    /// else finish Setup (tap Skip behaves the same per surface).
    private func dismissSyncOrSetup() {
        if showSync {
            skipSyncGate()
        } else if showSyncCover {
            showSyncCover = false
        } else {
            setupDone = true
        }
    }

    /// Voice "retry the glasses check": re-run the sync re-check, opening
    /// the screen when in setup-phase without it.
    private func retrySync() {
        syncRefreshGen += 1
        if !showSync, !showSyncCover, !setupDone {
            showSyncCover = true
        }
    }

    var body: some View {
        Group {
            if showSplash {
                SplashView { dismissSplash() }
            } else if showSync {
                GlassesSyncScreen(dat: dat, refreshGen: syncRefreshGen,
                                  onSkip: skipSyncGate,
                                  onComplete: { showSync = false })
            } else {
                mainBody
            }
        }
        .fullScreenCover(isPresented: $showSyncCover) {
            GlassesSyncScreen(dat: dat, refreshGen: syncRefreshGen,
                              onSkip: { showSyncCover = false },
                              onComplete: { showSyncCover = false })
        }
        .sheet(item: $selectedWell, onDismiss: {
            peekTarget = nil
            if lab.view.sheet.name == "well_card" { sendAction("back") }
        }) { sel in WellSheet(well: sel.id, lab: lab) { say($0) } }
        .sheet(isPresented: robotSheet) { robotPanel }
        .sheet(isPresented: draftSheet) { draftPanel }
        .sheet(isPresented: $showSettings) { VoiceSettingsView() }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.pdf, .plainText]) { res in
            importPicked(res)
        }
        .onAppear { connect(); refreshRoute(); loadLists(); voice.activate(); dat?.checkCameraPermission() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { voice.activate() } else { voice.deactivate() }
        }
        .onChange(of: showSettings) { _, shown in if !shown { voice.refresh() } }
        .onChange(of: voice.pendingAudio) { _, url in
            if let url { Task { await sendVoice(url) } }
        }
        .onChange(of: voice.noticeGen) { _, _ in
            if !voice.notice.isEmpty { say(voice.notice) }
        }
        .onChange(of: lab.run?.runId) { _, new in
            if new != nil {
                showHome = false
                selectedProtocol = nil
            } else {
                showHome = true
                selectedWell = nil
                debug = nil
            }
            loadLists()
        }
        .onReceive(NotificationCenter.default.publisher(for: AVAudioSession.routeChangeNotification)) { _ in
            refreshRoute()
        }
        .onOpenURL { url in dat?.handleCallback(url) }
    }

    private var mainBody: some View {
        VStack(spacing: 0) {
            contractBanners
            VoiceOrb(voice: voice, speakText: lastReply)
            Group {
                if !setupDone {
                SetupView(dat: dat, voice: voice,
                          onOpenSync: { showSyncCover = true }) {
                    setupDone = true
                    if lab.view.current.screen == "setup" { sendAction("back") }
                }
            } else if let src = viewingSource {
                VStack(spacing: 8) {
                    HStack {
                        Button { sendAction("back") } label: {
                            Image(systemName: "chevron.left").font(.headline)
                        }
                        .buttonStyle(.borderless)
                        Spacer()
                    }
                    .padding(.horizontal)
                    SourceView(target: src.target, page: src.page, phrase: src.phrase,
                               title: "Original")
                    hintsBar.padding(.horizontal)
                    inputBar.padding(.horizontal).padding(.bottom)
                }
            } else if let rid = viewingRun {
                VStack(spacing: 0) {
                    PastRunView(runId: rid, onBack: { sendAction("back") },
                                onSource: openSource)
                    hintsBar.padding(.horizontal)
                    inputBar.padding(.horizontal).padding(.bottom)
                }
            } else if let pid = selectedProtocol {
                VStack(spacing: 0) {
                    ProtocolDetailView(
                        protocolId: pid, cards: lab.cards, notice: detailNotice,
                        onBack: { sendAction("back") },
                        onStart: startProtocolRun,
                        onRun: { sendAction("open", ["target": "run:\($0)"]) },
                        onAnswer: answer(id:ok:), onAnswerAll: answerAll)
                    hintsBar.padding(.horizontal)
                    inputBar.padding(.horizontal).padding(.bottom)
                }
            } else if showHome || lab.run == nil {
                VStack(spacing: 0) {
                    HomeView(
                        protocols: protocols, runs: runs, cards: lab.cards,
                        onImport: { showImporter = true },
                        onReview: { sendAction("navigate", ["screen": "protocol_review"], quiet: false) },
                        hasDraft: lab.draft != nil,
                        onProtocol: { sendAction("open", ["target": "protocol:\($0)"]) },
                        onRun: { sendAction("open", ["target": "run:\($0)"]) },
                        onAnswer: answer(id:ok:), onAnswerAll: answerAll,
                        onSetup: { sendAction("navigate", ["screen": "setup"]) },
                        onRefresh: loadLists)
                    hintsBar.padding(.horizontal)
                    inputBar.padding(.horizontal).padding(.bottom)
                }
            } else {
                experimentView
            }
        }
        }
    }

    /// Contract banners (hidden on setup: it has its own server check).
    private var contractBanners: some View {
        VStack(spacing: 0) {
            if setupDone, let err = syncError {
                Text("Out of sync: \(err)")
                    .font(.callout.bold()).foregroundStyle(.white)
                    .frame(maxWidth: .infinity).padding(6).background(.red)
            }
            if setupDone, let mm = apiMismatch {
                Text(mm)
                    .font(.callout.bold()).foregroundStyle(.white)
                    .frame(maxWidth: .infinity).padding(6).background(.orange)
            }
        }
    }

    private var experimentView: some View {
        GeometryReader { g in
            let inner = g.size.width - 32
            let left = inner * 0.58
            HStack(spacing: 12) {
                VStack(spacing: 8) {
                    HStack(spacing: 6) {
                        Circle().fill(.green).frame(width: 8, height: 8)
                        Text("LIVE · run \(lab.run?.runId ?? "–") · v\(lab.version)")
                            .font(.caption2).foregroundStyle(.secondary)
                        Spacer()
                    }
                    PlateView(lab: lab, selected: Binding(
                        get: { selectedWell },
                        set: { if let id = $0?.id { sendAction("open", ["target": id]) } }
                    ))
                    ConditionBars(lab: lab)
                }
                .frame(width: left)
                VStack(spacing: 8) {
                    StepCard(lab: lab, onEndRun: endRun, onSource: openCurrentSource)
                    responseCard
                    TimerList(lab: lab)
                    MemoList(lab: lab) { sendAction("open", ["target": $0]) }
                    logSection.frame(maxHeight: .infinity)
                    hintsBar
                    inputBar
                }
                .frame(width: inner - left - 12)
            }
            .padding()
        }
    }

    private var robotSheet: Binding<Bool> {
        Binding(
            get: { if case .idle = robotState { false } else { true } },
            set: {
                if !$0 {
                    robotState = .idle
                    if lab.view.current.screen == "robot_preview" { sendAction("back") }
                }
            }
        )
    }

    private var draftSheet: Binding<Bool> {
        Binding(
            get: { if case .idle = draftState { false } else { true } },
            set: {
                if !$0 {
                    draftState = .idle
                    if lab.view.current.screen == "protocol_review" { sendAction("back") }
                }
            }
        )
    }

    // MARK: - Subviews

    /// The current Pip response, or the card(s) awaiting an answer. Fixed slot.
    private var responseCard: some View {
        Group {
            if !lab.cards.isEmpty {
                CardPanel(cards: lab.cards, onAnswer: answer(id:ok:), onAnswerAll: answerAll)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Pip").font(.caption.bold()).foregroundStyle(.secondary)
                    Text(lastReply.isEmpty ? "Speak or type a command" : lastReply)
                        .font(.body)
                        .foregroundStyle(lastReply.isEmpty ? .secondary : .primary)
                }
                .frame(maxWidth: .infinity, minHeight: 92, alignment: .leading)
                .padding(10)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private var logSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Log").font(.headline)
                Spacer()
                Button(showDebug ? "Hide debug" : "Debug") { showDebug.toggle() }
                    .font(.caption)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
            Text(routeLine).font(.caption2).foregroundStyle(.secondary)
            if showDebug {
                Text("voice: \(SpeechBus.shared.voiceInfo)").font(.caption2).foregroundStyle(.secondary)
            }
            if showDebug, let d = debug {
                debugPanel(d)
            } else if showDebug {
                Text("No runs yet").font(.caption).foregroundStyle(.secondary)
            }
            logView
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private var logView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(log.indices, id: \.self) { i in
                        Text(log[i]).font(.callout).textSelection(.enabled)
                    }
                    Color.clear.frame(height: 1).id("logBottom")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: log.count) { _, _ in
                proxy.scrollTo("logBottom", anchor: .bottom)
            }
        }
    }

    private var robotPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Robot protocol").font(.title2.bold())
                Spacer()
                Button("Close") { robotState = .idle }
            }
            switch robotState {
            case .idle: EmptyView()
            case .building:
                HStack {
                    ProgressView()
                    Text("Simulating in a Modal sandbox…").foregroundStyle(.secondary)
                }
            case .done(let r):
                LabeledContent("Simulation", value: r.sim.ok ? "passed" : "failed")
                    .foregroundStyle(r.sim.ok ? .green : .red)
                LabeledContent("Wells match", value: r.check.ok ? "yes" : "missing \(r.check.missing.joined(separator: ", "))")
                    .foregroundStyle(r.check.ok ? .green : .orange)
                Text("Code").font(.callout).foregroundStyle(.secondary)
                ScrollView {
                    Text(r.preview.code_head).font(.caption.monospaced()).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Spacer()
        }
        .padding()
    }

    private var draftPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Review protocol").font(.title2.bold())
                Spacer()
                Button("Discard") { discard() }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(reviewBusy)
                Button("Close") {
                    draftState = .idle
                    if lab.view.current.screen == "protocol_review" { sendAction("back") }
                }
                    .buttonStyle(.bordered).controlSize(.small)
            }
            switch draftState {
            case .idle: EmptyView()
            case .importing:
                HStack {
                    ProgressView()
                    Text("Reading protocol…").foregroundStyle(.secondary)
                }
            case .reviewing:
                if !lab.cards.isEmpty {
                    CardPanel(cards: lab.cards, onAnswer: answer(id:ok:), onAnswerAll: answerAll)
                }
                if !reviewError.isEmpty {
                    Text(reviewError).font(.callout).foregroundStyle(.red)
                }
                if let d = lab.draft {
                    if let title = d.plan.title, !title.isEmpty {
                        Text(title).font(.headline)
                    }
                    Text(reviewSummary(d)).font(.callout).foregroundStyle(.secondary)
                    TabView(selection: Binding(
                        get: { reviewTab },
                        set: { reviewTab = $0
                            sendAction("set_tab", ["tab": ["liquids", "steps", "layout"][$0]])
                        }
                    )) {
                        reviewLiquids(d).tabItem { Text("Liquids") }.tag(0)
                        reviewSteps(d).tabItem { Text("Steps") }.tag(1)
                        reviewLayout(d).tabItem { Text("Layout") }.tag(2)
                    }
                    let rest = unmatchedIssues(d)
                    if !rest.isEmpty {
                        ForEach(rest) { r in issueRow(r, .yellow) }
                    }
                    HStack {
                        Button("Save to library") { save() }
                            .buttonStyle(.bordered)
                            .disabled(!d.errors.isEmpty || reviewBusy)
                        Button("Start run") { start() }
                            .buttonStyle(.borderedProminent)
                            .disabled(!d.errors.isEmpty || reviewBusy)
                    }
                } else {
                    Text("No draft.").foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
        .padding()
    }

    private func reviewLiquids(_ d: ProtocolDraft) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(d.plan.liquids.keys.sorted(), id: \.self) { k in
                    let l = d.plan.liquids[k]!
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            DraftField(initial: l.name) {
                                patch(path: "liquids.\(k)", text: $0)
                            }
                            if let c = l.concentration, !c.isEmpty {
                                Text(c).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        if let prep = l.prep, !prep.isEmpty {
                            Text(prep).font(.caption).foregroundStyle(.secondary)
                        }
                        quoteLine(l.quote, source: l.quoteSource) {
                            openDraftQuote(page: l.quotePage, phrase: l.quote)
                        }
                        itemIssues(d, prefix: "liquids.\(k)")
                    }
                }
            }
        }
    }

    private func reviewSteps(_ d: ProtocolDraft) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(d.plan.steps.enumerated()), id: \.offset) { i, s in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("[\(i)] \(s.text)").bold()
                            .foregroundStyle(s.pipette == nil ? .secondary : .primary)
                        HStack {
                            Text(s.kind).font(.caption)
                            DraftField(initial: s.minutes.map { fmtNum($0) } ?? "") {
                                patch(path: "steps.\(i).minutes", num: $0)
                            }.frame(width: 56)
                            Text("min").font(.caption)
                            DraftField(initial: s.tempC.map { fmtNum($0) } ?? "") {
                                patch(path: "steps.\(i).temp_c", num: $0)
                            }.frame(width: 56)
                            Text("°C").font(.caption)
                        }
                        if let p = s.pipette {
                            HStack {
                                DraftField(initial: p.volume.map { fmtNum($0) } ?? "") {
                                    patch(path: "steps.\(i).pipette.volume", num: $0)
                                }.frame(width: 56)
                                Text("\(p.unit ?? "") \(p.liquid)").font(.caption)
                                DraftField(initial: targetText(p.target)) {
                                    patch(path: "steps.\(i).pipette.target", text: $0)
                                }.frame(width: 130)
                            }
                            if let r = p.repeatCount, r > 1 {
                                Text("×\(r)").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        HStack {
                            Text("⚠️").font(.caption)
                            DraftField(initial: s.caution ?? "") {
                                patch(path: "steps.\(i).caution", text: $0)
                            }
                        }
                        quoteLine(s.quote, source: s.quoteSource) {
                            openDraftQuote(page: s.quotePage, phrase: s.quote)
                        }
                        itemIssues(d, prefix: "steps.\(i)")
                    }
                }
            }
        }
    }

    private func targetText(_ t: DraftTarget) -> String {
        switch t.type {
        case "wells": "wells:\(t.value ?? "")"
        case "condition": "condition:\(t.value ?? "")"
        case "all_used": "all"
        default: "other"
        }
    }

    private func reviewSummary(_ d: ProtocolDraft) -> String {
        var parts: [String] = []
        if !d.errors.isEmpty { parts.append("\(d.errors.count) to fix") }
        if !d.review.isEmpty { parts.append("\(d.review.count) to review") }
        return parts.isEmpty ? "Ready to start." : parts.joined(separator: " · ")
    }

    private func issueRow(_ issue: DraftIssue, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(issue.reason).font(.callout).foregroundStyle(color)
            if let q = issue.quote, !q.isEmpty {
                Text("“\(q)”").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func itemIssues(_ d: ProtocolDraft, prefix: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(d.errors.filter { $0.path == prefix || $0.path.hasPrefix(prefix + ".") }) {
                issueRow($0, .red)
            }
            ForEach(d.review.filter { $0.path == prefix || $0.path.hasPrefix(prefix + ".") }) {
                issueRow($0, .yellow)
            }
        }
    }

    private func unmatchedIssues(_ d: ProtocolDraft) -> [DraftIssue] {
        (d.errors + d.review).filter { i in
            !(i.path.hasPrefix("liquids.") || i.path.hasPrefix("conditions.")
                || i.path.hasPrefix("steps."))
        }
    }

    private func quoteLine(_ quote: String, source: String?, open: (() -> Void)? = nil) -> some View {
        Group {
            if let open {
                Button(action: open) {
                    HStack(spacing: 4) {
                        Text("“\(quote)”").font(.caption).foregroundStyle(.secondary)
                        if source == "code" {
                            Text("found by code").font(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                }
                .buttonStyle(.plain)
            } else {
                HStack(spacing: 4) {
                    Text("“\(quote)”").font(.caption).foregroundStyle(.secondary)
                    if source == "code" {
                        Text("found by code").font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
        }
    }

    /// Draft quotes open the picked PDF, kept as _draft until Save/Start names it.
    private func openDraftQuote(page: Int?, phrase: String) {
        draftState = .idle
        openSource("protocol:_draft", page, phrase)
    }

    private func reviewLayout(_ d: ProtocolDraft) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                reviewMiniPlate(d)
                ForEach(d.plan.conditions.keys.sorted(), id: \.self) { k in
                    let c = d.plan.conditions[k]!
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(c.label).bold()
                            if d.meta.layout[k] == "auto" {
                                Text("auto").font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text("n").font(.caption)
                            DraftField(initial: "\(c.replicates)") {
                                patch(path: "conditions.\(k).replicates", int: $0)
                            }.frame(width: 50)
                        }
                        if !c.treatments.isEmpty {
                            Text(c.treatments.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                        }
                        DraftField(initial: c.wells.joined(separator: ",")) {
                            patch(path: "conditions.\(k).wells", text: $0)
                        }
                        quoteLine(c.quote, source: c.quoteSource) {
                            openDraftQuote(page: c.quotePage, phrase: c.quote)
                        }
                        itemIssues(d, prefix: "conditions.\(k)")
                    }
                }
            }
        }
    }

    private func reviewMiniPlate(_ d: ProtocolDraft) -> some View {
        let owners: [String: String] = Dictionary(
            uniqueKeysWithValues: d.plan.conditions.flatMap { k, c in c.wells.map { ($0, k) } })
        return VStack(spacing: 1) {
            ForEach(plateRows, id: \.self) { r in
                HStack(spacing: 1) {
                    ForEach(1...12, id: \.self) { c in
                        let w = "\(r)\(c)"
                        Circle()
                            .fill(owners[w].map { conditionColor($0) } ?? .secondary.opacity(0.15))
                            .frame(width: 9, height: 9)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 4)
    }

    private var hintsBar: some View {
        let cmds = screens.first { $0.id == currentScreen }?.commands.prefix(3) ?? []
        return Text("Try saying: " + cmds.joined(separator: " · "))
            .font(.caption).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            Button { showSettings = true } label: {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.bordered).controlSize(.small)
            Button {
                ttsOn.toggle()
                SpeechBus.shared.enabled = ttsOn
                if !ttsOn { SpeechBus.shared.stop() }
            } label: {
                Image(systemName: ttsOn ? "speaker.wave.2.fill" : "speaker.slash.fill")
            }
            .buttonStyle(.bordered).controlSize(.small)
            TextField("Type a command", text: $input)
                .textFieldStyle(.roundedBorder)
                .onSubmit { submit() }
            if voice.state == .listening, let t0 = voice.listeningSince {
                TimelineView(.periodic(from: t0, by: 0.2)) { ctx in
                    Text(String(format: "%.1fs", ctx.date.timeIntervalSince(t0)))
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.red)
                }
                .frame(width: 52, alignment: .trailing)
            }
            Button(action: {}) {
                Image(systemName: voice.state == .listening ? "mic.fill" : "mic")
                    .font(.title2)
                    .foregroundStyle(.white)
                    .padding(10)
                    .background(voice.state == .listening ? .red : .blue, in: Circle())
            }
            .buttonStyle(HoldStyle { pressed in pressed ? startHold() : endHold() })
            .disabled(!voice.micLive)
            .opacity(voice.micLive ? 1 : 0.5)
        }
    }

    private func debugPanel(_ d: DebugRun) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("“\(d.input)”").font(.caption).lineLimit(2)
            Text("route: \(d.route ?? "-")").font(.caption2).foregroundStyle(.secondary)
            ForEach(d.calls.indices, id: \.self) { i in
                Text("\(d.calls[i].name)(\(d.calls[i].args))").font(.caption2.monospaced())
            }
            ForEach(d.sources, id: \.self) { s in
                Text("src: \(s)").font(.caption2).foregroundStyle(.secondary)
            }
            Text("reply v\(d.recordVersion) · sync v\(lab.version)")
                .font(.caption2)
                .foregroundStyle(d.recordVersion == lab.version ? Color.secondary : Color.red)
            Text(msLine(d.ms))
                .font(.caption2).foregroundStyle(.secondary)
            Divider()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 4)
    }

    // MARK: - Logic

    private func connect() {
        Task { @MainActor in
            do {
                lab.sync(try await PipClient.record())
                renderView()
                syncError = nil
            } catch {
                syncError = syncReason(error)
                say("Record unavailable: \(error.localizedDescription)")
            }
            do {
                let h = try await PipClient.health()
                if let v = h.apiVersion, v != PipClient.EXPECTED_API_VERSION {
                    apiMismatch = "API mismatch: app \(PipClient.EXPECTED_API_VERSION), server \(v) — update one."
                } else {
                    apiMismatch = nil
                }
            } catch {
                apiMismatch = nil // unreachable server already shows as out of sync
            }
            while !Task.isCancelled {
                do {
                    for try await ev in PipClient.events() {
                        handleEvent(ev)
                    }
                } catch {
                    try? await Task.sleep(for: .seconds(2))
                }
            }
        }
    }

    private func handleEvent(_ ev: SSEvent) {
        switch ev {
        case .snapshot(let snap):
            lab.sync(snap)
            renderView()
            syncError = nil
        case .broken(let reason):
            syncError = reason
        case .timerWarning(let message, let speak):
            say(message)
            sayAloud(speak)
        case .timerDone(let message, let speak):
            lastReply = message
            say("Pip: \(message)")
            sayAloud(speak)
        case .ui(let act):
            _ = applyUI(act)
        }
    }

    private func refreshRoute() {
        Recorder.ensurePreferredInput()
        let session = AVAudioSession.sharedInstance()
        let ins = session.currentRoute.inputs.map(\.portName).joined(separator: ", ")
        let outs = session.currentRoute.outputs.map(\.portName).joined(separator: ", ")
        routeLine = "in: \(ins.isEmpty ? "-" : ins) · out: \(outs.isEmpty ? "-" : outs)"
    }

    /// All Pip speech flows through the voice machine (speaking state,
    /// wake pause). Refused mid-listening; card questions pre-selected.
    private func sayAloud(_ text: String?) {
        voice.speak(text)
    }

    /// Applies one server ui action. False when the app could not do it: says
    /// so on the spot, in the log, the Pip slot, and aloud.
    @discardableResult
    private func applyUI(_ a: UIAction) -> Bool {
        switch a.action {
        case "navigate", "open", "back":
            // The server drives these through view now; a ui copy is stale.
            return uiFail("I couldn't do that.")
        case "import":
            showImporter = true
            return true
        case "flash":
            lab.flash(Set(a.wells ?? []))
            return true
        case "peek":
            guard let t = a.target, !t.isEmpty else { return uiFail("I couldn't open that.") }
            robotState = .idle
            selectedWell = WellSelect(id: t)
            peekTarget = t
            peekGen += 1
            let g = peekGen
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(a.seconds ?? 3))
                if peekGen == g, selectedWell?.id == t {
                    selectedWell = nil
                    peekTarget = nil
                }
            }
            return true
        default:
            return uiFail("I couldn't do that.")
        }
    }

    private func uiFail(_ text: String) -> Bool {
        lastReply = text
        say(text)
        sayAloud(text)
        return false
    }

    private func importPicked(_ res: Result<URL, Error>) {
        switch res {
        case .failure(let e):
            say("Pick failed: \(e.localizedDescription)")
        case .success(let url):
            guard url.startAccessingSecurityScopedResource() else {
                say("Can't read that file")
                return
            }
            defer { url.stopAccessingSecurityScopedResource() }
            guard let data = try? Data(contentsOf: url) else {
                say("Can't read that file")
                return
            }
            if url.pathExtension.lowercased() == "pdf" {
                OriginalStore.save(data, protocolId: "_draft")
            }
            draftState = .importing
            Task { @MainActor in
                do {
                    let r = try await PipClient.importProtocol(data: data,
                                                               filename: url.lastPathComponent)
                    lab.draft = r.draft
                    reviewError = ""
                    say(r.reply)
                    sayAloud(r.speak)
                    draftState = .reviewing
                } catch {
                    say("Import failed: \(error.localizedDescription)")
                    draftState = .idle
                }
            }
        }
    }

    private func patch(path: String, value: Any) {
        Task { @MainActor in
            do {
                let r = try await PipClient.patchDraft(path: path, value: value)
                if !r.ok { say(r.reason ?? "Edit failed") }
            } catch {
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func patch(path: String, int raw: String) {
        guard let v = Int(raw.trimmingCharacters(in: .whitespaces)) else {
            say("Not a number: \(raw)")
            return
        }
        patch(path: path, value: v)
    }

    private func patch(path: String, num raw: String) {
        guard let v = Double(raw.trimmingCharacters(in: .whitespaces)) else {
            say("Not a number: \(raw)")
            return
        }
        patch(path: path, value: v)
    }

    private func patch(path: String, text raw: String) {
        patch(path: path, value: raw)
    }

    private func start() {
        guard !reviewBusy else { return }
        reviewBusy = true
        reviewError = ""
        Task { @MainActor in
            defer { reviewBusy = false }
            do {
                let r = try await PipClient.startRun()
                say(r.reply)
                sayAloud(r.speak)
                if r.started {
                    if let id = r.protocolId { keepOriginal(protocolId: id) }
                    lab.draft = nil
                    draftState = .idle
                    loadLists()
                } else if r.card != nil {
                    // The draft is saved and popped; the card lives on.
                    lab.draft = nil
                    draftState = .idle
                    loadLists()
                } else {
                    reviewError = r.reply
                }
            } catch {
                reviewError = error.localizedDescription
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func save() {
        guard !reviewBusy else { return }
        reviewBusy = true
        reviewError = ""
        Task { @MainActor in
            defer { reviewBusy = false }
            do {
                let r = try await PipClient.saveDraft()
                say(r.reply)
                sayAloud(r.speak)
                if r.saved {
                    if let id = r.proto?.id {
                        keepOriginal(protocolId: id)
                        selectedProtocol = id
                    }
                    lab.draft = nil
                    draftState = .idle
                    loadLists()
                } else {
                    reviewError = r.reply
                }
            } catch {
                reviewError = error.localizedDescription
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    /// The picked PDF becomes this protocol's on-iPad original.
    private func keepOriginal(protocolId: String) {
        let fm = FileManager.default
        let draft = OriginalStore.url(protocolId: "_draft")
        guard fm.fileExists(atPath: draft.path) else { return }
        try? fm.removeItem(at: OriginalStore.url(protocolId: protocolId))
        try? fm.moveItem(at: draft, to: OriginalStore.url(protocolId: protocolId))
    }

    private func discard() {
        guard !reviewBusy else { return }
        reviewBusy = true
        reviewError = ""
        Task { @MainActor in
            defer { reviewBusy = false }
            do {
                try await PipClient.discardDraft()
                try? FileManager.default.removeItem(at: OriginalStore.url(protocolId: "_draft"))
                lab.draft = nil
                draftState = .idle
            } catch {
                reviewError = error.localizedDescription
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func startProtocolRun(_ id: String) {
        detailNotice = ""
        Task { @MainActor in
            do {
                let r = try await PipClient.runsStart(protocolId: id)
                say(r.reply)
                sayAloud(r.speak)
                if r.started {
                    detailNotice = ""
                    selectedProtocol = nil
                    loadLists()
                }
            } catch {
                detailNotice = error.localizedDescription
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func endRun() {
        Task { @MainActor in
            do {
                let r = try await PipClient.runsEnd()
                say(r.reply)
                sayAloud(r.speak)
            } catch {
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func loadLists() {
        Task { @MainActor in
            do {
                async let p = PipClient.protocols()
                async let r = PipClient.runs()
                protocols = try await p
                runs = try await r
            } catch {
                // Home shows empty; the refresh button retries.
            }
        }
    }

    private func openSource(_ target: String, _ page: Int?, _ phrase: String?) {
        viewingSource = ViewingSource(target: target, page: page, phrase: phrase)
    }

    private func openSourceTarget(_ t: String) {
        guard let parsed = parseSourceTarget(t) else { return }
        var page: Int?
        var phrase: String?
        if let n = stepNumber(fragment: parsed.fragment),
           n >= 1, n <= lab.planSteps.count {
            page = lab.planSteps[n - 1].quotePage
            phrase = lab.planSteps[n - 1].quote
        } else {
            phrase = textPhrase(fragment: parsed.fragment)
        }
        viewingSource = ViewingSource(target: t, page: page, phrase: phrase)
    }

    private func openCurrentSource() {
        guard let pid = lab.run?.protocolId, !pid.isEmpty,
              lab.stepIndex < lab.planSteps.count else { return }
        let s = lab.planSteps[lab.stepIndex]
        openSource("protocol:\(pid)#step:\(lab.stepIndex + 1)", s.quotePage, s.quote)
    }

    private func say(_ s: String) {
        log.append(s)
        if log.count > 100 { log.removeFirst(log.count - 100) }
    }

    /// Taps travel as Actions; the server updates view and broadcasts.
    private func sendAction(_ id: String, _ args: [String: Any] = [:], quiet: Bool = true) {
        Task { @MainActor in
            do {
                let r = try await PipClient.inputAction(id: id, args: args)
                if !quiet {
                    for msg in r.messages { say(msg) }
                    if !r.reply.isEmpty {
                        lastReply = r.reply
                        say("Pip: \(r.reply)")
                    }
                    _ = (r.ui ?? []).allSatisfy { applyUI($0) }
                    sayAloud(r.speak)
                } else {
                    _ = (r.ui ?? []).allSatisfy { applyUI($0) }
                }
            } catch {
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func answer(id: String, ok: Bool) {
        sendAction("answer_card", ["card_id": id, "ok": ok], quiet: false)
    }

    private func answerAll() {
        for id in lab.cards.map(\.id) {
            sendAction("answer_card", ["card_id": id, "ok": true], quiet: false)
        }
    }

    private func submit() {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        input = ""
        say("> \(text)")
        Task { await sendText(text) }
    }

    @MainActor
    private func sendText(_ text: String) async {
        busy = true
        defer { busy = false }
        do {
            let r = try await PipClient.orchestrate(text: text)
            voice.didRespond(hasSources: !r.sources.isEmpty)
            handle(calls: r.tool_calls, messages: r.messages, reply: r.reply, ms: r.ms,
                   route: r.route, input: text, sources: r.sources,
                   dropped: r.dropped ?? 0,
                   ui: r.ui, speak: r.speak, recordVersion: r.record_version)
        } catch {
            voice.didFail()
            say("Error: \(error.localizedDescription)")
        }
    }

    private func startHold() {
        voice.beginManual()
    }

    private func endHold() {
        voice.endManual()
    }

    @MainActor
    private func sendVoice(_ url: URL) async {
        do {
            let loud = Recorder.rms(of: url) ?? 0
            let data = try Data(contentsOf: url)
            try? FileManager.default.removeItem(at: url)
            guard loud >= 0.001 else {
                voice.didFail()
                say("No sound reached the mic — on the simulator, check the Mac mic permission and input")
                return
            }
            let r = try await PipClient.voice(wav: data)
            guard r.ignored != true else { voice.didFail(); return }
            if r.setup_done == true { dismissSyncOrSetup() }
            if r.sync_retry == true { retrySync() }
            say("🎙 \(r.transcript)")
            voice.didRespond(hasSources: !r.sources.isEmpty)
            handle(calls: r.tool_calls, messages: r.messages, reply: r.reply, ms: r.ms,
                   route: r.route, input: r.transcript, sources: r.sources,
                   dropped: r.dropped ?? 0,
                   ui: r.ui, speak: r.speak, recordVersion: r.record_version)
        } catch let e as PipClient.VoiceError {
            voice.didFail()
            switch e {
            case .tooShort(let dur):
                say(voice.lastHeld >= 1 ? "Held it but nothing recorded — check the simulator audio input"
                    : String(format: "Too short (%.1fs) — press and hold to speak", dur))
            }
        } catch {
            voice.didFail()
            say("Error: \(error.localizedDescription)")
        }
    }

    private func handle(calls: [ToolCall], messages: [String], reply: String, ms: [String: Int],
                        route: String?, input: String, sources: [Source],
                        dropped: Int,
                        ui: [UIAction]?, speak: String?, recordVersion: Int) {
        for msg in messages { say(msg) }
        if !reply.isEmpty {
            lastReply = reply
            say("Pip: \(reply)")
        }
        let uiOk = (ui ?? []).allSatisfy { applyUI($0) }
        if uiOk { sayAloud(speak) }
        if dropped > 0 { say("Ignored \(dropped) bad call(s)") }
        debug = DebugRun(
            input: input,
            calls: calls.map { ($0.name, "{" + $0.args.map { "\"\($0.key)\": \($0.value.compact)" }.sorted().joined(separator: ", ") + "}") },
            ms: ms,
            route: route,
            sources: sources.map(\.title),
            recordVersion: recordVersion
        )
        say("⏱ " + msLine(ms))
    }

    private func runRobot() {
        robotState = .building
        Task { @MainActor in
            do {
                let doc = try await PipClient.robot()
                if lab.view.current.screen == "robot_preview" { robotState = .done(doc) }
                say("Robot protocol ready")
            } catch {
                robotState = .idle
                say("Robot failed: \(error.localizedDescription)")
            }
        }
    }
}

#Preview {
    ContentView()
}
