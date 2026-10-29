import AVFoundation
import os
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
    @State private var log: [String] = ["\(Persona.current.agentName) ready — speak or type"]
    @State private var debug: DebugRun?
    @State private var showSettings = false
    @State private var lastReply = ""
    @State private var voice = VoiceSession()
    @State private var busy = false
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedWell: WellSelect?
    @State private var eventsTask: Task<Void, Never>?
    @State private var peekGen = 0
    @State private var peekTarget: String?
    @State private var lastViewScreen = ""
    /// App launch opens Home; the first snapshot moves the saved view there once.
    @State private var launchHomeSent = false
    @State private var robotState = RobotState.idle
    @State private var draftState = DraftState.idle
    @State private var reviewTab = 0
    @State private var showImporter = false
    @State private var ttsOn = SpeechBus.shared.enabled
    @State private var routeLine = ""
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
    private let registry = loadScreens()
    private var screens: [ScreenDef] { registry.screens }
    var dat: GlassesDAT? = nil
    /// Configures DAT from the sync screen's Bluetooth tap (the Bluetooth prompt).
    var enableDAT: () -> Void = {}
    /// Local network prompt fires on the first server request, so the app
    /// reaches the server only after the sync screen's Local network tap
    /// made that request.
    @AppStorage("pip.serverAllowed") private var serverAllowed = false
    private var canReachServer: Bool { serverAllowed }
    @State private var serverLink: ServerLink = .idle
    /// connect() owns the one SSE loop; later taps only re-fetch the record.
    @State private var connectStarted = false
    /// Top-level screens; every switch goes through `top.show`.
    enum TopScreen: Hashable { case splash, sync, main }
    @State private var top = ScreenStack(TopScreen.splash)
    /// Sync opened by the launch gate (Skip remembers) vs from settings/voice.
    @State private var syncFromGate = false
    @State private var syncRefreshGen = 0
    @AppStorage("pip.syncSkipped") private var syncSkipped = false

    /// Screen for hints: the server-owned view.
    private var currentScreen: String {
        let s = lab.view.current.screen
        return s.isEmpty ? ((showHome || lab.run == nil) ? "home" : "main") : s
    }

    /// Mirror the server-owned view into local presentation state.
    /// Runs after every snapshot sync; taps send Actions instead of
    /// setting this state directly.
    private func renderView() {
        let v = lab.view
        let cur = v.current.screen, tgt = v.current.target
        Log.view.info("[view] render cur=\(cur, privacy: .public):\(tgt, privacy: .public) run=\(self.lab.run?.runId ?? "nil", privacy: .public) launchHomeSent=\(self.launchHomeSent) top=\(String(describing: self.top.front), privacy: .public)")
        guard !cur.isEmpty else { return }
        if !launchHomeSent {
            launchHomeSent = true
            if cur != "home" {
                sendAction("navigate", ["screen": "home"])
                return
            }
        }
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
        // "setup" is the old id for the same screen (records saved before)
        if cur == "sync" || cur == "setup", top.front != .sync,
           lastViewScreen != cur {
            openSync()
        }
        if let i = ["liquids", "steps", "layout"].firstIndex(of: v.tab) { reviewTab = i }
        if cur != lastViewScreen, cur == "home" || cur == "protocol_detail" { loadLists() }
        lastViewScreen = cur
    }

    /// Launch flow: splash, then the sync screen while a permission is
    /// missing or the glasses aren't ready (unless skipped), then main.
    private func dismissSplash() {
        guard top.front == .splash else { return }
        let rows = SyncGate.rows(permission: { $0.state(serverAllowed: serverAllowed) },
                                 server: serverLink, dat: dat,
                                 hfpName: SyncGate.liveHFPName)
        let glasses = SyncItem.allCases.filter { $0.permission == nil }.map { rows[$0.rawValue] }
        if SyncGate.needed(
            permissions: [AppPermission.bluetooth, .localNetwork, .microphone]
                .map { $0.state(serverAllowed: serverAllowed) },
            glasses: glasses, skipped: syncSkipped) {
            syncFromGate = true
            top.show(.sync)
        } else {
            top.show(.main)
        }
    }

    private func skipSyncGate() {
        syncSkipped = true
        closeSync()
    }

    /// Sync from settings or voice (not the launch gate).
    private func openSync() {
        syncFromGate = false
        top.show(.sync)
    }

    private func leaveSync() {
        if syncFromGate { skipSyncGate() } else { closeSync() }
    }

    /// Back to main; the server view drops its sync entry if it has one.
    private func closeSync() {
        top.show(.main)
        let cur = lab.view.current.screen
        if cur == "sync" || cur == "setup" { sendAction("back") }
    }

    /// Voice "skip the glasses check": Skip on the sync screen, or just
    /// remember the skip when it isn't up.
    private func dismissSyncOrSetup() {
        if top.front == .sync {
            leaveSync()
        } else {
            syncSkipped = true
        }
    }

    /// Voice "retry the glasses check": re-run the checks on the sync screen.
    private func retrySync() {
        syncRefreshGen += 1
        if top.front != .sync { openSync() }
    }

    /// Local network row tap: the first tap allows reaching the server
    /// (its request shows the prompt); later taps re-fetch the record.
    private func connectServer() {
        if connectStarted {
            Task { @MainActor in await loadRecord() }
        } else {
            serverAllowed = true
        }
    }

    var body: some View {
        ScreenTransition(stack: $top, screens: [.splash, .sync, .main],
                         prewarm: [.main],
                         floor: { $0 == .main ? Theme.canvas : Theme.dark }) { id in
            switch id {
            case .splash:
                SplashView { dismissSplash() }
            case .sync:
                GlassesSyncScreen(dat: dat, voice: voice,
                                  serverAllowed: serverAllowed, server: serverLink,
                                  onAllowBluetooth: enableDAT,
                                  onConnectServer: connectServer,
                                  refreshGen: syncRefreshGen,
                                  onSkip: leaveSync,
                                  onComplete: closeSync)
            case .main:
                mainBody
            }
        }
        // One voice edge above every screen (touches pass through).
        .overlay { EdgeGlow(voice: voice).ignoresSafeArea() }
        .statusBarHidden(top.front != .main)
        .sheet(item: $selectedWell, onDismiss: {
            peekTarget = nil
            if lab.view.sheet.name == "well_card" { sendAction("back") }
        }) { sel in WellSheet(well: sel.id, lab: lab) { say($0) } }
        .sheet(isPresented: robotSheet) { robotPanel }
        .sheet(isPresented: draftSheet) { draftPanel }
        .sheet(isPresented: $showSettings) {
            VoiceSettingsView(onOpenSync: { showSettings = false; openSync() })
        }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.pdf, .plainText]) { res in
            importPicked(res)
        }
        .onAppear {
            if canReachServer { connect(); loadLists() }
            refreshRoute()
            voice.activate()
            dat?.checkCameraPermission()
        }
        .onChange(of: canReachServer) { _, on in
            if on { connect(); loadLists() }
        }
        .onChange(of: scenePhase) { _, phase in
            Log.voice.info("[voice] scenePhase \(String(describing: phase), privacy: .public)")
            // inactive (Control Center, alerts) keeps a live session; background ends it
            if phase == .active {
                voice.activate()
                // a stream that slept in the background may be dead: open a fresh one
                if connectStarted { openEvents() }
            } else if phase == .background || !voice.inSession {
                voice.deactivate()
            }
        }
        .onChange(of: showSettings) { _, shown in if !shown { voice.refresh() } }
        .onChange(of: voice.pendingAudio) { _, url in
            if let url { Task { await sendVoice(url) } }
        }
        .onChange(of: voice.liveTurnGen) { _, _ in
            if let r = voice.liveTurn { applyVoice(r) }
        }
        #if DEBUG
        .onChange(of: voice.typedGen) { _, _ in
            if let t = voice.typed { say("> \(t)"); Task { await sendText(t) } }
        }
        #endif
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
            TopHeader(voice: voice, speakText: lastReply, partialText: voice.partialText,
                      timers: showsBench ? [] : lab.timers,  // the bench lists its own timers
                      onBlockedTap: openSync)
            Group {
                if let src = viewingSource {
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
                        onSetup: { openSync() },
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

    /// Contract banners.
    private var contractBanners: some View {
        VStack(spacing: 0) {
            if let err = syncError {
                Text("Out of sync: \(err)")
                    .font(.callout.bold()).foregroundStyle(.white)
                    .frame(maxWidth: .infinity).padding(6).background(.red)
            }
            if let reason = registry.error {
                Text("Screen registry unreadable: \(reason) — rebuild the app.")
                    .font(.callout.bold()).foregroundStyle(.white)
                    .frame(maxWidth: .infinity).padding(6).background(.red)
            }
            if let mm = apiMismatch {
                Text(mm)
                    .font(.callout.bold()).foregroundStyle(.white)
                    .frame(maxWidth: .infinity).padding(6).background(.orange)
            }
        }
    }

    /// The run bench is on screen (no source, past run, protocol or home).
    private var showsBench: Bool {
        viewingSource == nil && viewingRun == nil && selectedProtocol == nil
            && !(showHome || lab.run == nil)
    }

    /// Bench, ver 2 (Figma 37:13521): plate card and group legend beside
    /// the step, timers, what's left and the registry's Say button. It
    /// starts right under the header with the design's 64 px side margins;
    /// design px scale with the space so landscape and portrait both fit
    /// without scrolling, and spare height stays at the bottom.
    private var experimentView: some View {
        GeometryReader { g in
            let w = g.size.width, h = g.size.height
            let wide = w >= h
            // design px: landscape 1376 wide x (32 + 700 + 32) tall; portrait 928 x 1410
            let u = wide ? min(w / 1376, h / 764) : min(w / 928, h / 1410)
            Group {
                if wide {
                    // the side column takes what the margins, plate and gap leave
                    let side = max(0, w - 2 * 64 * u - 800 * u - 36 * u)
                    HStack(alignment: .top, spacing: 36 * u) {
                        plateColumn(u)
                        benchSide(u).frame(width: side)
                    }
                    .frame(height: 700 * u)
                } else {
                    VStack(spacing: 40 * u) {
                        plateColumn(u)
                        benchSide(u).frame(width: 800 * u)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, 32 * u)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .background { BenchCanvas() }
    }

    private func plateColumn(_ u: CGFloat) -> some View {
        VStack(spacing: 0) {
            PlateView(lab: lab, selected: Binding(
                get: { selectedWell },
                set: { if let id = $0?.id { sendAction("open", ["target": id]) } }
            ))
            .padding(28 * u)
            .frame(width: 800 * u, height: 566 * u)
            .benchCard(radius: 44 * u)
            Spacer(minLength: 24 * u)
            ConditionBars(lab: lab, u: u).frame(height: 97 * u)
        }
        .frame(width: 800 * u)
    }

    private func benchSide(_ u: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            StepCard(lab: lab, u: u, onSource: openCurrentSource)
            Spacer(minLength: 24 * u)
            TimerList(lab: lab, u: u)
            Group {
                if lab.cards.isEmpty {
                    RemainingCard(lab: lab, u: u)
                } else {
                    // shortcut: the old card panel inside a ver 2 card, until the
                    // confirm-card screens (Figma 37:13714...) are built
                    CardPanel(cards: lab.cards, onAnswer: answer(id:ok:), onAnswerAll: answerAll)
                        .padding(20 * u)
                        .frame(maxHeight: 260 * u)
                        .benchCard(radius: 32 * u)
                }
            }
            .padding(.top, 10 * u)
            if let b = screens.first(where: { $0.id == currentScreen })?.buttons?.first {
                SayButton(button: b, size: 34 * u) { sendAction($0.action, $0.args ?? [:]) }
                    .padding(.top, 36 * u)
            }
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
            Spacer()
        }
    }

    // MARK: - Logic

    /// Initial `/record` fetch; its outcome is the Local network row.
    private func loadRecord() async {
        serverLink = .connecting
        do {
            lab.sync(try await PipClient.record())
            renderView()
            syncError = nil
            serverLink = .connected
        } catch {
            syncError = syncReason(error)
            serverLink = .failed
            say("Record unavailable: \(error.localizedDescription)")
        }
    }

    private func connect() {
        guard !connectStarted else { return }
        connectStarted = true
        Task { @MainActor in
            await loadRecord()
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
            openEvents()
        }
    }

    /// (Re)opens the server event stream. Each connection starts with a full
    /// snapshot, so a reconnect also restores any changes missed meanwhile.
    /// A silent stream times out (PipClient.idleTimeout) and retries.
    private func openEvents() {
        eventsTask?.cancel()
        eventsTask = Task { @MainActor in
            while !Task.isCancelled {
                do {
                    for try await ev in PipClient.events() {
                        serverLink = .connected
                        handleEvent(ev)
                    }
                } catch {
                    Log.view.info("[view] events stream: \(error.localizedDescription, privacy: .public)")
                }
                try? await Task.sleep(for: .seconds(2))
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
            say("\(Persona.current.agentName): \(message)")
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
        Log.view.info("[view] send action \(id, privacy: .public) \(String(describing: args), privacy: .public)")
        Task { @MainActor in
            do {
                let r = try await PipClient.inputAction(id: id, args: args)
                if !quiet {
                    for msg in r.messages { say(msg) }
                    if !r.reply.isEmpty {
                        lastReply = r.reply
                        say("\(Persona.current.agentName): \(r.reply)")
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

    #if DEBUG
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
    #endif

    @MainActor
    private func sendVoice(_ url: URL) async {
        do {
            let loud = Recorder.rms(of: url) ?? 0
            let data = try Data(contentsOf: url)
            try? FileManager.default.removeItem(at: url)
            Log.voice.info("[voice] send? bytes=\(data.count) dur=\(Recorder.duration(of: data) ?? -1) rms=\(loud) min=\(PipClient.minVoiceDuration)")
            guard loud >= 0.001 else {
                voice.didFail()
                say("No sound reached the mic — on the simulator, check the Mac mic permission and input")
                return
            }
            let r = try await PipClient.voice(wav: data)
            Log.voice.info("[voice] /voice ok ignored=\(r.ignored ?? false) transcript=\"\(r.transcript, privacy: .public)\" speak=\(r.speak?.count ?? -1)")
            applyVoice(r)
        } catch let e as PipClient.VoiceError {
            Log.voice.info("[voice] not sent: \(String(describing: e), privacy: .public)")
            voice.didFail()
            switch e {
            case .tooShort(let dur):
                say(voice.lastHeld >= 1 ? "Held it but nothing recorded — check the simulator audio input"
                    : String(format: "Too short (%.1fs) — press and hold to speak", dur))
            }
        } catch {
            Log.voice.error("[voice] /voice failed: \(error.localizedDescription, privacy: .public)")
            voice.didFail()
            say("Error: \(error.localizedDescription)")
        }
    }

    /// One path for a spoken turn's result: /voice replies and live turns.
    private func applyVoice(_ r: VoiceResponse) {
        guard r.ignored != true else { voice.didFail(); return }
        if r.setup_done == true { dismissSyncOrSetup() }
        if r.sync_retry == true { retrySync() }
        say("🎙 \(r.transcript)")
        voice.didRespond(hasSources: !r.sources.isEmpty)
        handle(calls: r.tool_calls, messages: r.messages, reply: r.reply, ms: r.ms,
               route: r.route, input: r.transcript, sources: r.sources,
               dropped: r.dropped ?? 0,
               ui: r.ui, speak: r.speak, recordVersion: r.record_version)
    }

    private func handle(calls: [ToolCall], messages: [String], reply: String, ms: [String: Int],
                        route: String?, input: String, sources: [Source],
                        dropped: Int,
                        ui: [UIAction]?, speak: String?, recordVersion: Int) {
        for msg in messages { say(msg) }
        if !reply.isEmpty {
            lastReply = reply
            say("\(Persona.current.agentName): \(reply)")
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
