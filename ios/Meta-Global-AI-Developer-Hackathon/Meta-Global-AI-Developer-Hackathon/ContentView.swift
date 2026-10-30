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
    /// An import is being read (the server says "Reading the protocol…");
    /// a second start is ignored and the import screens are disabled.
    @State private var importing = false
    @State private var showImporter = false
    @State private var showTextImport = false
    /// Control commands for the screen on top (next page, take it, scroll down…).
    @State private var commands = ScreenCommands()
    /// The camera for photo import (made when that screen first opens).
    @State private var camera: DeviceCamera?
    @State private var files: [LocalFile] = []
    /// The last view change came from a tap (its screen line is spoken only
    /// in a live session; a voice reply already says it).
    @State private var navByTap = false
    @State private var promptKey = ""
    @State private var ttsOn = SpeechBus.shared.enabled
    @State private var routeLine = ""
    @State private var showHome = false
    @State private var selectedProtocol: String?
    @State private var viewingRun: String?
    /// The run record opened last: Home's run list comes back on its page.
    @State private var lastRun: String?
    @State private var viewingSource: ViewingSource?
    @State private var protocols: [ProtocolSummary] = []
    @State private var runs: [RunSummary] = []
    @State private var searches: [WebSearch] = []
    /// Searches that answered (⓪c, counts); ⑧ also shows pending and failed ones.
    private var answered: [WebSearch] { searches.filter { $0.status == "ok" } }
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
    /// Mic, wake word, audio route and the glasses camera check start once
    /// the splash has gone (their main-thread work would stall its bloom).
    @State private var devicesStarted = false
    /// The splash can leave: the first record is in (or failed), or there's no server yet.
    private var bootReady: Bool { !canReachServer || serverLink == .connected || serverLink == .failed }
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
        if let r = viewingRun { lastRun = r }
        selectedProtocol = cur == "protocol_detail" && !tgt.isEmpty ? tgt : nil
        if cur == "protocol_source", !tgt.isEmpty {
            if viewingSource?.target != tgt { openSourceTarget(tgt) }
        } else {
            viewingSource = nil
        }
        if cur == "robot_preview" {
            if case .idle = robotState { runRobot() }
        } else if case .idle = robotState {} else { robotState = .idle }
        // Milo's line for the screen (registry prompt): shown on entry, and
        // said when a tap opened it during a live session
        let key = "\(cur)#\(tgt)"
        if key != promptKey {
            promptKey = key
            if !v.prompt.isEmpty {
                lastReply = v.prompt
                if navByTap && voice.inSession { sayAloud(v.promptSpeak) }
            }
        }
        navByTap = false
        showHome = cur == "home"
        // "setup" is the old id for the same screen (records saved before)
        if cur == "sync" || cur == "setup", top.front != .sync,
           lastViewScreen != cur {
            openSync()
        }
        if cur != lastViewScreen, cur == "import_file" { refreshFiles() }
        if cur == "import_photo", camera == nil { camera = DeviceCamera() }
        if cur != lastViewScreen, ["home", "protocol_detail", "web_searches", "web_answer", "run_view"].contains(cur) {
            loadLists()
        } else if cur == "web_answer", !searches.contains(where: { $0.id == tgt }) {
            loadLists()  // a search that just ran
        }
        lastViewScreen = cur
    }

    /// Launch flow: splash, then the sync screen while a permission is
    /// missing or the glasses aren't ready (unless skipped), then main.
    private func dismissSplash() {
        guard top.front == .splash else { return }
        let needed = Boot.time("permission check (sync gate)") {
            let rows = SyncGate.rows(permission: { $0.state(serverAllowed: serverAllowed) },
                                     server: serverLink, dat: dat,
                                     hfpName: SyncGate.liveHFPName)
            let glasses = SyncItem.allCases.filter { $0.permission == nil }.map { rows[$0.rawValue] }
            return SyncGate.needed(
                permissions: [AppPermission.bluetooth, .localNetwork, .microphone]
                    .map { $0.state(serverAllowed: serverAllowed) },
                glasses: glasses, skipped: syncSkipped)
        }
        if needed {
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
        ScreenTransition(stack: $top, floor: { $0 == .main ? Theme.canvas : Theme.dark }) { id in
            switch id {
            case .splash:
                SplashView(ready: { bootReady }) { dismissSplash() }
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
                    // the app floor under every part of Main, edge to edge: no black shows through
                    .background(Theme.canvas.ignoresSafeArea())
                    .background { LayoutProbe(name: "main") }
            }
        }
        // One voice edge above every screen (touches pass through).
        .overlay { EdgeGlow(voice: voice).ignoresSafeArea() }
        // hidden from the first frame on and never toggled: a status bar
        // appearing with Main changed the safe area under it (the bands)
        .statusBarHidden(true)
        .sheet(isPresented: robotSheet) { robotPanel }
        .sheet(isPresented: $showSettings) {
            VoiceSettingsView(onOpenSync: { showSettings = false; openSync() })
        }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.pdf, .plainText]) { res in
            importPicked(res)
        }
        .sheet(isPresented: $showTextImport) {
            TextImportSheet { text, name in upload(text: text, filename: name) }
        }

        .onAppear {
            Boot.mark("first frame (root appear) serverAllowed=\(canReachServer)")
            if canReachServer { connect(); loadLists() }
        }
        .onChange(of: top.front) { old, new in
            Boot.mark("screen \(String(describing: old)) -> \(String(describing: new))")
            if old == .splash { startDevices() }
        }
        .onChange(of: canReachServer) { _, on in
            if on { connect(); loadLists() }
        }
        .onChange(of: scenePhase) { _, phase in
            Log.voice.info("[voice] scenePhase \(String(describing: phase), privacy: .public)")
            // inactive (Control Center, alerts) keeps a live session; background ends it
            if phase == .active, devicesStarted {
                voice.systemSheet(false)  // back from any iOS picker
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
        .onChange(of: voice.stage == nil) { _, ended in
            if ended { loadLists() }  // a turn ended: a search's answer (or why not) fills ⑧
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

    /// After the splash's crossfade: the audio session, mic engine and wake
    /// word come up on the main actor (MicEngine, WakeListener), so they wait
    /// until no launch animation is running.
    private func startDevices() {
        guard !devicesStarted else { return }
        devicesStarted = true
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(Theme.Motion.screen))
            Boot.time("audio route") { refreshRoute() }
            Boot.time("voice activate (wake listener start is async)") { voice.activate() }
            Boot.time("glasses camera permission check") { dat?.checkCameraPermission() }
        }
    }

    private var mainBody: some View {
        VStack(spacing: 0) {
            contractBanners
            TopHeader(voice: voice, speakText: lastReply, partialText: voice.partialText,
                      // the plain bench lists its own timers; cards and the well card show the chip
                      timers: showsBench && lab.cards.isEmpty && selectedWell == nil ? [] : lab.timers,
                      onBlockedTap: openSync, mode: lab.headerMode)
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
                               title: "Original", commands: commands)
                    ControlChips(buttons: buttons("protocol_source").filter { $0.action == "control" },
                                 u: 0.8, perform: performButton)
                    hintsBar.padding(.horizontal)
                    inputBar.padding(.horizontal).padding(.bottom)
                }
            } else if lab.view.current.screen == "import_file" {
                // our file list (voice can't drive the iOS picker)
                stage { u in
                    FileListScreen(files: files, buttons: buttons("import_file"), u: u,
                                   onPick: { sendAction("pick", ["n": $0]) }, perform: performButton)
                        .disabled(importing).opacity(importing ? 0.5 : 1)
                }
            } else if lab.view.current.screen == "import_photo", let cam = camera {
                // our camera: "take it", "one more", "done"
                stage { u in
                    CameraScreen(source: cam, commands: commands, buttons: buttons("import_photo"), u: u,
                                 perform: performButton,
                                 onPages: { n in report("import_photo", (0..<n).map { ("p\($0 + 1)", "page \($0 + 1)") }) },
                                 onDone: importPhotos, onSystemSheet: { voice.systemSheet($0) })
                        .disabled(importing).opacity(importing ? 0.5 : 1)
                }
            } else if lab.view.current.screen == "web_source",
                      let url = URL(string: lab.view.slots["url"] ?? "") {
                // a source page inside the app: our header, registry buttons
                stage { u in
                    WebSourceScreen(url: url, title: lab.view.slots["title"] ?? "", domain: lab.view.slots["domain"] ?? "",
                                    commands: commands, buttons: buttons("web_source"), u: u, perform: performButton)
                }
            } else if lab.view.current.screen == "web_answer" {
                // ⑧ web search answer (Figma 37:14048)
                stage { u in
                    if let item = searches.first(where: { $0.id == lab.view.current.target }) {
                        WebAnswerScreen(item: item, buttons: buttons("web_answer"), u: u, perform: performButton)
                    }
                }
            } else if lab.view.current.screen == "web_searches" {
                // ⓪c web searches (Figma 37:13104)
                stage { u in
                    WebSearchesScreen(items: answered, buttons: buttons("web_searches"), u: u,
                                      onOpen: { sendAction("open", ["target": "search:\($0)"]) },
                                      perform: performButton)
                }
            } else if let d = lab.draft, StartScreen.ids.contains(lab.view.current.screen) {
                // ⑤b-⑤f start review (Figma 37:13203-37:13351)
                stage { u in
                    if lab.cards.isEmpty {
                        startScreen(d, u)
                    } else {
                        // "Methods already exists. New version, or separate?" (saving a revision)
                        ConfirmScreen(lab: lab, buttons: registry.cards, u: u, onAnswer: answer(id:ok:),
                                      onAnswerAll: answerAll, onChoice: choose(id:answer:))
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
            } else if ["mode", "import_choice"].contains(lab.view.current.screen) {
                // ⑤a / ⑤b start (Figma 37:13141, 37:13167)
                stage { u in
                    if lab.view.current.screen == "mode" {
                        ModeScreen(buttons: buttons("mode"), u: u, perform: performButton)
                    } else {
                        ImportChoiceScreen(buttons: buttons("import_choice"), u: u, perform: performButton)
                    }
                }
            } else if let rid = viewingRun {
                // ⓪b run record (Figma 37:12931)
                stage { u in
                    if lab.cards.isEmpty {
                        RunRecordScreen(runId: rid, runs: runs, searches: answered.filter { $0.runId == rid },
                                        buttons: buttons("run_view"), u: u, perform: performButton,
                                        onSearch: { sendAction("open", ["target": "search:\($0)"]) })
                    } else {
                        // "Run 1 is finished. Start a new run?" (and finish-first) take the screen
                        ConfirmScreen(lab: lab, buttons: registry.cards, u: u,
                                      onAnswer: answer(id:ok:), onAnswerAll: answerAll, onChoice: choose(id:answer:))
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
            } else if let pid = selectedProtocol {
                // ⓪ a protocol picked on Home: its detail and runs on the right
                stage { u in
                    if lab.cards.isEmpty {
                        ProtocolsHome(protocols: protocols, runs: runs, selectedId: pid,
                                      activeRunId: lab.run?.runId, searchCount: answered.count,
                                      buttons: buttons("protocol_detail"), u: u,
                                      onProtocol: { sendAction("open", ["target": "protocol:\($0)"]) },
                                      onRun: { sendAction("open", ["target": "run:\($0)"]) },
                                      perform: performButton, draftTitle: lab.draft?.plan.title ?? "",
                                      lastRunId: lastRun)
                    } else {
                        // "Run 3 of Drug A/B is going. Finish it and start MTT?"
                        ConfirmScreen(lab: lab, buttons: registry.cards, u: u,
                                      onAnswer: answer(id:ok:), onAnswerAll: answerAll, onChoice: choose(id:answer:))
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
            } else if showHome || lab.run == nil {
                // ⓪ protocols (Figma 37:12877); a waiting card takes it like the bench
                VStack(spacing: 0) {
                    stage { u in
                        if lab.cards.isEmpty {
                            ProtocolsHome(protocols: protocols, runs: runs, selectedId: nil,
                                          activeRunId: lab.run?.runId, searchCount: answered.count, buttons: buttons("home"), u: u,
                                          onProtocol: { sendAction("open", ["target": "protocol:\($0)"]) },
                                          onRun: { sendAction("open", ["target": "run:\($0)"]) },
                                          perform: performButton, draftTitle: lab.draft?.plan.title ?? "",
                                          lastRunId: lastRun)
                        } else {
                            ConfirmScreen(lab: lab, buttons: registry.cards, u: u,
                                          onAnswer: answer(id:ok:), onAnswerAll: answerAll, onChoice: choose(id:answer:))
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                        }
                    }
                    // settings + voice on/off: not in the design, kept so setup stays reachable
                    inputBar.padding(.horizontal).padding(.bottom, 8)
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

    /// A registry screen's Say buttons, templates filled from the view's
    /// slots ("Say Step {step}" -> "Say Step 2"); unfilled ones are hidden.
    private func buttons(_ screen: String) -> [ScreenButton] {
        (screens.first { $0.id == screen }?.buttons ?? []).compactMap { $0.filled(lab.view.slots) }
    }

    /// Taps send the registry button's action, as saying it would.
    private func performButton(_ b: ScreenButton) {
        sendAction(b.action, b.args ?? [:])
    }

    private enum StartScreen {
        static let ids = ["protocol_review", "draft_steps", "draft_step", "draft_page", "plan_check"]
    }

    @ViewBuilder private func startScreen(_ d: ProtocolDraft, _ u: CGFloat) -> some View {
        let cur = lab.view.current
        let step = Int(cur.target) ?? 1
        switch cur.screen {
        case "draft_steps":
            DraftStepsScreen(draft: d, buttons: buttons(cur.screen), u: u,
                             onRow: { sendAction("show_step", ["step": $0]) }, perform: performButton)
        case "draft_step":
            DraftStepScreen(draft: d, step: step, buttons: buttons(cur.screen), u: u, perform: performButton)
        case "draft_page":
            DraftPageScreen(draft: d, step: step, buttons: buttons(cur.screen), u: u, perform: performButton,
                            commands: commands)
        case "plan_check":
            PlanCheckScreen(draft: d, buttons: buttons(cur.screen), u: u, perform: performButton)
        default:
            DraftReadyScreen(draft: d, title: lab.view.slots["title"] ?? d.plan.title ?? "",
                             buttons: buttons(cur.screen), u: u, perform: performButton)
        }
    }

    /// A ver 2 screen's stage: design px scaled to the space under the
    /// header, the design's side margins, the canvas glow behind.
    private func stage<V: View>(@ViewBuilder _ content: @escaping (CGFloat) -> V) -> some View {
        GeometryReader { g in
            // the first measure can be zero: draw once the size is real (no scale from 0)
            if g.size.width > 0, g.size.height > 0 {
                let u = Theme.Layout.unit(g.size)
                content(u)
                    .padding(.horizontal, Theme.Layout.side * u)
                    .padding(.top, Theme.Layout.contentTop * u).padding(.bottom, Theme.Layout.contentBottom * u)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            }
        }
        // the design's bottom margin counts from the screen edge, not above the home indicator
        .ignoresSafeArea(.container, edges: .bottom)
        .background { BenchCanvas() }
        .background { LayoutProbe(name: "stage") }
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
            let u = Theme.Layout.unit(g.size, bench: true)
            Group {
                if !lab.cards.isEmpty {
                    // ③ a waiting card takes the bench (Figma 37:13714...)
                    ConfirmScreen(lab: lab, buttons: registry.cards, u: u,
                                  onAnswer: answer(id:ok:), onAnswerAll: answerAll, onChoice: choose(id:answer:))
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if wide {
                    // the side column takes what the margins, plate and gap leave
                    let side = max(0, w - 2 * Theme.Layout.side * u - 800 * u - Theme.Layout.column * u)
                    HStack(alignment: .top, spacing: Theme.Layout.column * u) {
                        plateColumn(u)
                        benchSide(u).frame(width: side)
                    }
                    .frame(height: Theme.Layout.benchHeight * u)
                } else {
                    VStack(spacing: 40 * u) {
                        plateColumn(u)
                        benchSide(u).frame(width: 800 * u)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
            // landscape: the bench block and the cards center in the content box
            // (bench top 222, cards on 572); portrait keeps its top-aligned bench
            .padding(.top, (wide || !lab.cards.isEmpty ? Theme.Layout.contentTop : 32) * u)
            .padding(.bottom, (wide || !lab.cards.isEmpty ? Theme.Layout.contentBottom : 0) * u)
            .frame(maxWidth: .infinity, maxHeight: .infinity,
                   alignment: wide || !lab.cards.isEmpty ? .center : .top)
        }
        .ignoresSafeArea(.container, edges: .bottom)
        .background { BenchCanvas() }
    }

    private func plateColumn(_ u: CGFloat) -> some View {
        VStack(spacing: 0) {
            PlateView(lab: lab, selected: Binding(
                get: { selectedWell },
                set: { if let id = $0?.id { sendAction("open", ["target": id]) } }
            ), focus: selectedWell?.id)
            .padding(28 * u)
            .frame(width: 800 * u, height: 566 * u)
            .benchCard(radius: 44 * u)
            Spacer(minLength: 24 * u)
            ConditionBars(lab: lab, u: u).frame(height: 97 * u)
        }
        .frame(width: 800 * u)
    }

    @ViewBuilder private func benchSide(_ u: CGFloat) -> some View {
        if let w = selectedWell?.id {
            // ④ the well card takes the side column; the plate rings the well
            WellCardSide(well: w, lab: lab,
                         buttons: screens.first { $0.id == "well_card" }?.buttons ?? [], u: u) { b in
                if lab.view.sheet.name == "well_card" {
                    sendAction(b.action, b.args ?? [:])
                } else {  // a peek the server never opened: close it here
                    selectedWell = nil
                    peekTarget = nil
                }
            }
        } else {
            mainSide(u)
        }
    }

    private func mainSide(_ u: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            StepCard(lab: lab, u: u, onSource: openCurrentSource)
            Spacer(minLength: 24 * u)
            TimerList(lab: lab, u: u)
            RemainingCard(lab: lab, u: u)
                .padding(.top, 10 * u)
                .enter(.pop, 2, u: u)
            if let b = screens.first(where: { $0.id == "main" })?.buttons?.first {
                SayButton(button: b, size: 34 * u) { sendAction($0.action, $0.args ?? [:]) }
                    .padding(.top, 36 * u)
                    .modifier(CommandIn(u: u, delay: Theme.Motion.enterStep * 3))
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
        Boot.mark("record fetch")
        do {
            lab.sync(try await PipClient.record())
            renderView()
            syncError = nil
            serverLink = .connected
            Boot.mark("first snapshot (record) v\(lab.version)")
        } catch {
            syncError = syncReason(error)
            serverLink = .failed
            Boot.mark("record failed: \(syncReason(error))")
            say("Record unavailable: \(error.localizedDescription)")
        }
    }

    private func connect() {
        guard !connectStarted else { return }
        connectStarted = true
        Boot.mark("connect")
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
                Log.view.info("[view] events open (have v\(lab.version))")
                Boot.mark("sse open")
                do {
                    for try await ev in PipClient.events() {
                        serverLink = .connected
                        handleEvent(ev)
                    }
                    Log.view.info("[view] events stream ended by the server")
                } catch {
                    Log.view.info("[view] events stream: \(error.localizedDescription, privacy: .public)")
                }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    /// Every reply says which record version it made; the event stream must
    /// bring that snapshot. If it hasn't shortly after, the stream is dead
    /// though its socket looks open (iPad: taps sent, no render): fetch the
    /// record now and reopen the stream.
    private func expectSnapshot(_ version: Int) {
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(1.5))
            guard lab.version < version else { return }
            Log.view.error("[view] events stale: reply v\(version), have v\(lab.version) -> reload record, reopen events")
            await loadRecord()
            openEvents()
        }
    }

    private func handleEvent(_ ev: SSEvent) {
        switch ev {
        case .snapshot(let snap):
            Log.view.info("[view] snapshot v\(snap.version) cur=\(snap.view.current.screen, privacy: .public)")
            if lab.version == 0 || top.front == .splash { Boot.mark("sse snapshot v\(snap.version)") }
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
        case .stage(let st):
            applyStage(st)
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

    /// A server turn stage (SSE, outside a live session; a live session's come
    /// on its socket to VoiceSession directly).
    private func applyStage(_ st: TurnStage) {
        voice.applyStage(st)
    }

    /// Applies one server ui action. False when the app could not do it: says
    /// so on the spot, in the log, the Pip slot, and aloud.
    /// ui ids already run: the reply and /events deliver each action once each.
    @State private var ranUI: [String] = []

    @discardableResult
    private func applyUI(_ a: UIAction) -> Bool {
        if let id = a.id {
            guard !ranUI.contains(id) else { return true }
            ranUI = Array((ranUI + [id]).suffix(50))
        }
        switch a.action {
        case "navigate", "open", "back":
            // The server drives these through view now; a ui copy is stale.
            return uiFail("I couldn't do that.")
        case "import":
            openImporter()
            return true
        case "import_text":
            showTextImport = true
            return true
        case "control":
            // the screen's own buttons, said or tapped (registry control actions)
            guard let cmd = a.target else { return uiFail("I couldn't do that.") }
            if cmd == "browse" { openImporter() } else { commands.send(cmd, n: a.n) }
            return true
        case "pick":
            guard let f = files.first(where: { $0.id == a.target }) else { return uiFail("I couldn't open that file.") }
            importLocal(f)
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

    /// The iOS file picker. A live session is held from here until the picker
    /// answers or the app is active again: its binding flips back to false
    /// while the picker is still up (iPad log), so it can't be the signal.
    private func openImporter() {
        voice.systemSheet(true)
        showImporter = true
    }

    private func importPicked(_ res: Result<URL, Error>) {
        voice.systemSheet(false)
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
            let oid = UUID().uuidString
            if url.pathExtension.lowercased() == "pdf" {
                OriginalStore.save(data, protocolId: oid)
            }
            upload(files: [(url.lastPathComponent, data)], oid: oid)
        }
    }

    /// The app's files, newest first, told to the server ("number 2", a name).
    private func refreshFiles() {
        files = LocalFile.all()
        report("import_file", files.map { ($0.id, $0.name) })
    }

    private func report(_ screen: String, _ items: [(String, String)]) {
        Task { try? await PipClient.reportItems(screen: screen, items: items.map { (id: $0.0, name: $0.1) }) }
    }

    /// A file from the app's folder: the same import as a picked one.
    private func importLocal(_ f: LocalFile) {
        guard let data = try? Data(contentsOf: f.url) else {
            say("Can't read that file")
            return
        }
        let oid = UUID().uuidString
        if f.kind == "PDF" { OriginalStore.save(data, protocolId: oid) }
        upload(files: [(f.url.lastPathComponent, data)], oid: oid)
    }

    /// Page photos stay on this iPad under the import's original id.
    private func importPhotos(_ pages: [Data]) {
        let oid = UUID().uuidString
        for (i, d) in pages.enumerated() {
            try? d.write(to: OriginalStore.photoURL(oid, page: i + 1), options: .atomic)
        }
        upload(files: pages.enumerated().map { ("page\($0.offset + 1).jpg", $0.element) }, oid: oid)
    }

    /// The one import route (PDF, .txt, pasted text, photos). The server opens
    /// Draft ready when it's done.
    private func upload(files: [(String, Data)] = [], text: String = "", filename: String = "",
                        oid: String = UUID().uuidString) {
        guard !importing else { return }  // one import at a time; the server joins repeats too
        importing = true
        navByTap = true  // Draft ready's line is said in a live session, like a tap
        Task { @MainActor in
            do {
                let r = try await PipClient.importProtocol(
                    files: files.map { (name: $0.0, data: $0.1) }, text: text,
                    filename: filename, originalId: oid)
                lab.draft = r.draft
                say(r.reply)
                importing = false
            } catch {
                say("Import failed: \(error.localizedDescription)")
                importing = false
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
                async let w = PipClient.searches()
                protocols = try await p
                runs = try await r
                searches = try await w.searches
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
        // the original lives under its import's id; older imports under the protocol id
        let oid = protocols.first { $0.id == parsed.protocolId }?.originalId ?? ""
        viewingSource = ViewingSource(target: oid.isEmpty ? t : "protocol:\(oid)#\(parsed.fragment)",
                                      page: page, phrase: phrase)
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
        navByTap = true
        Task { @MainActor in
            do {
                let r = try await PipClient.inputAction(id: id, args: args)
                expectSnapshot(r.record_version)
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

    private func choose(id: String, answer: String) {
        sendAction("answer_card", ["card_id": id, "ok": true, "answer": answer], quiet: false)
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
        expectSnapshot(r.record_version)
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
