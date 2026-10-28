import AVFoundation
import SwiftUI
import UniformTypeIdentifiers

struct DebugRun {
    var input: String
    var calls: [(name: String, args: String)]
    var ms: [String: Int]
    var route: String?
    var sources: [String]
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
    @State private var recorder = Recorder()
    @State private var recording = false
    @State private var pressing = false
    @State private var recordStart: Date?
    @State private var busy = false
    @State private var selectedWell: WellSelect?
    @State private var peekGen = 0
    @State private var robotState = RobotState.idle
    @State private var draftState = DraftState.idle
    @State private var reviewTab = 0
    @State private var showImporter = false
    @State private var ttsOn = SpeechBus.shared.enabled
    @State private var routeLine = ""
    private let screens = loadScreens()

    /// Screen the server reasons about. One sheet at a time; robot wins.
    private var currentScreen: String {
        if case .idle = robotState {} else { return "robot_preview" }
        if case .idle = draftState {} else { return "protocol_review" }
        return selectedWell == nil ? "main" : "well_card"
    }

    var body: some View {
        GeometryReader { g in
            let inner = g.size.width - 32
            let left = inner * 0.58
            HStack(spacing: 12) {
                VStack(spacing: 8) {
                    PlateView(lab: lab, selected: $selectedWell)
                    ConditionBars(lab: lab)
                }
                .frame(width: left)
                VStack(spacing: 8) {
                    StepCard(lab: lab)
                    responseCard
                    TimerList(lab: lab)
                    MemoList(lab: lab) { selectedWell = WellSelect(id: $0) }
                    logSection.frame(maxHeight: .infinity)
                    hintsBar
                    inputBar
                }
                .frame(width: inner - left - 12)
            }
            .padding()
        }
        .sheet(item: $selectedWell) { sel in WellSheet(well: sel.id, lab: lab) { say($0) } }
        .sheet(isPresented: robotSheet) { robotPanel }
        .sheet(isPresented: draftSheet) { draftPanel }
        .sheet(isPresented: $showSettings) { VoiceSettingsView() }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.pdf, .plainText]) { res in
            importPicked(res)
        }
        .onAppear { connect(); refreshRoute() }
        .onReceive(NotificationCenter.default.publisher(for: AVAudioSession.routeChangeNotification)) { _ in
            refreshRoute()
        }
    }

    private var robotSheet: Binding<Bool> {
        Binding(
            get: { if case .idle = robotState { false } else { true } },
            set: { if !$0 { robotState = .idle } }
        )
    }

    private var draftSheet: Binding<Bool> {
        Binding(
            get: { if case .idle = draftState { false } else { true } },
            set: { if !$0 { draftState = .idle } }
        )
    }

    // MARK: - Subviews

    /// The current Pip response, or the card(s) awaiting an answer. Fixed slot.
    private var responseCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Pip").font(.caption.bold()).foregroundStyle(.secondary)
            if lab.cards.count >= 2 {
                cardChecklist
            } else if let first = lab.cards.first {
                Text(first.question).font(.headline)
                if let detail = first.detail, !detail.isEmpty {
                    Text(detail).font(.callout).foregroundStyle(.secondary)
                }
                HStack(spacing: 16) {
                    Button("Yes") { answer(true) }
                        .buttonStyle(.borderedProminent)
                    Button("No") { answer(false) }
                        .buttonStyle(.bordered)
                }
            } else {
                Text(lastReply.isEmpty ? "Speak or type a command" : lastReply)
                    .font(.body)
                    .foregroundStyle(lastReply.isEmpty ? .secondary : .primary)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 92, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    /// One row per pending card, each with its own Yes/No, plus Yes to all.
    private var cardChecklist: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Checklist (\(lab.cards.count))").font(.headline)
                Spacer()
                Button("Yes to all") { answerAll() }
                    .buttonStyle(.borderedProminent).controlSize(.small)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(lab.cards.enumerated()), id: \.element.id) { idx, card in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(card.question).font(.callout).bold(idx == 0)
                                if let detail = card.detail, !detail.isEmpty {
                                    Text(detail).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            Button("Yes") { answer(id: card.id, ok: true) }
                                .buttonStyle(.borderedProminent).controlSize(.small)
                            Button("No") { answer(id: card.id, ok: false) }
                                .buttonStyle(.bordered).controlSize(.small)
                        }
                        .padding(4)
                        .background(idx == 0 ? Color.accentColor.opacity(0.12) : .clear,
                                    in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }
            .frame(maxHeight: 180)
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
            if showDebug {
                Text(routeLine).font(.caption2).foregroundStyle(.secondary)
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
                Button("Close") { draftState = .idle }
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
                if let d = lab.draft {
                    if !d.errors.isEmpty {
                        ForEach(d.errors) { e in
                            Text(e.reason).foregroundStyle(.red).font(.callout)
                        }
                    }
                    if !d.review.isEmpty {
                        ForEach(d.review) { r in
                            VStack(alignment: .leading, spacing: 1) {
                                Text(r.reason).foregroundStyle(.yellow).font(.callout)
                                if let q = r.quote, !q.isEmpty {
                                    Text("“\(q)”").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    TabView(selection: $reviewTab) {
                        reviewLiquids(d).tabItem { Text("Liquids") }.tag(0)
                        reviewSteps(d).tabItem { Text("Steps") }.tag(1)
                        reviewLayout(d).tabItem { Text("Layout") }.tag(2)
                    }
                    Button("Start run") { start() }
                        .buttonStyle(.borderedProminent)
                        .disabled(!d.errors.isEmpty)
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
                        Text("“\(l.quote)”").font(.caption).foregroundStyle(.secondary)
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
                        Text("“\(s.quote)”").font(.caption).foregroundStyle(.secondary)
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
            Button(lab.draft == nil ? "Import" : "Review") {
                if lab.draft == nil { showImporter = true } else { draftState = .reviewing }
            }
            .buttonStyle(.bordered).controlSize(.small)
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
            if recording, let t0 = recordStart {
                TimelineView(.periodic(from: t0, by: 0.2)) { ctx in
                    Text(String(format: "%.1fs", ctx.date.timeIntervalSince(t0)))
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.red)
                }
                .frame(width: 52, alignment: .trailing)
            }
            Button(action: {}) {
                Image(systemName: recording ? "mic.fill" : "mic")
                    .font(.title2)
                    .foregroundStyle(.white)
                    .padding(10)
                    .background(recording ? .red : .blue, in: Circle())
            }
            .buttonStyle(HoldStyle { pressed in pressed ? startHold() : endHold() })
            .disabled(busy)
            .opacity(busy ? 0.5 : 1)
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
            Text(msLine(d.ms))
                .font(.caption2).foregroundStyle(.secondary)
            Divider()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 4)
    }

    // MARK: - Logic

    private func connect() {
        Task {
            do {
                lab.sync(try await PipClient.record())
            } catch {
                say("Record unavailable: \(error.localizedDescription)")
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
        let session = AVAudioSession.sharedInstance()
        let ins = session.currentRoute.inputs.map(\.portName).joined(separator: ", ")
        let outs = session.currentRoute.outputs.map(\.portName).joined(separator: ", ")
        routeLine = "in: \(ins.isEmpty ? "-" : ins) · out: \(outs.isEmpty ? "-" : outs)"
    }

    /// Speaks unless recording. Card questions arrive pre-selected by the server.
    private func sayAloud(_ text: String?) {
        guard !recording else { return }
        SpeechBus.shared.speak(text)
    }

    /// Applies one server ui action. False when the app could not do it: says
    /// so on the spot, in the log, the Pip slot, and aloud.
    @discardableResult
    private func applyUI(_ a: UIAction) -> Bool {
        switch a.action {
        case "navigate":
            switch a.screen {
            case "robot_preview":
                selectedWell = nil
                if case .idle = robotState { runRobot() }
                return true
            case "main":
                selectedWell = nil
                robotState = .idle
                return true
            case "well_card":
                return true // nothing to move without a target; open carries one
            default:
                return uiFail("I couldn't open the \(a.screen ?? "?") screen.")
            }
        case "open":
            guard let t = a.target, !t.isEmpty else { return uiFail("I couldn't open that.") }
            robotState = .idle
            selectedWell = WellSelect(id: t)
            return true
        case "flash":
            lab.flash(Set(a.wells ?? []))
            return true
        case "peek":
            guard let t = a.target, !t.isEmpty else { return uiFail("I couldn't open that.") }
            robotState = .idle
            selectedWell = WellSelect(id: t)
            peekGen += 1
            let g = peekGen
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(a.seconds ?? 3))
                if peekGen == g, selectedWell?.id == t { selectedWell = nil }
            }
            return true
        case "back":
            selectedWell = nil
            robotState = .idle
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
            draftState = .importing
            Task {
                do {
                    let r = try await PipClient.importProtocol(data: data,
                                                               filename: url.lastPathComponent)
                    lab.draft = r.draft
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
        Task {
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
        Task {
            do {
                let r = try await PipClient.startRun()
                say(r.reply)
                sayAloud(r.speak)
                if r.started { draftState = .idle }
            } catch {
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func discard() {
        Task {
            do {
                try await PipClient.discardDraft()
                draftState = .idle
            } catch {
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func say(_ s: String) {
        log.append(s)
        if log.count > 100 { log.removeFirst(log.count - 100) }
    }

    private func answer(_ ok: Bool) {
        guard let first = lab.cards.first else { return }
        answer(id: first.id, ok: ok)
    }

    private func answer(id: String, ok: Bool) {
        Task {
            do {
                let r = try await PipClient.confirm(cardId: id, ok: ok)
                say(r.message)
            } catch {
                say("Error: \(error.localizedDescription)")
            }
        }
    }

    private func answerAll() {
        let ids = lab.cards.map(\.id)
        Task {
            for id in ids {
                do {
                    say(try await PipClient.confirm(cardId: id, ok: true).message)
                } catch {
                    say("Error: \(error.localizedDescription)")
                }
            }
        }
    }

    private func submit() {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        input = ""
        say("> \(text)")
        Task { await sendText(text) }
    }

    private func sendText(_ text: String) async {
        busy = true
        defer { busy = false }
        do {
            let r = try await PipClient.orchestrate(text: text, screen: currentScreen,
                                                    selectedWell: selectedWell?.id ?? "",
                                                    openSheet: selectedWell == nil ? "" : "well_card")
            handle(calls: r.tool_calls, messages: r.messages, reply: r.reply, ms: r.ms,
                   route: r.route, input: text, sources: r.sources,
                   robot: r.robot == true, dropped: r.dropped ?? 0,
                   ui: r.ui, speak: r.speak)
        } catch {
            say("Error: \(error.localizedDescription)")
        }
    }

    private func startHold() {
        SpeechBus.shared.stop()
        pressing = true
        guard !recording, !busy else { return }
        Task {
            guard await Recorder.requestPermission() else { say("Microphone permission needed"); return }
            guard pressing else { return } // released before the mic was ready
            do {
                try recorder.start()
                recordStart = Date()
                recording = true
            } catch {
                say("Recording failed: \(error.localizedDescription)")
            }
        }
    }

    private func endHold() {
        pressing = false
        guard recording else { return }
        recording = false
        let held = recordStart.map { Date().timeIntervalSince($0) } ?? 0
        recordStart = nil
        do {
            let url = try recorder.stop()
            Task { await sendVoice(url, held: held) }
        } catch {
            say("Couldn't save the recording: \(error.localizedDescription)")
        }
    }

    private func sendVoice(_ url: URL, held: Double) async {
        busy = true
        defer { busy = false }
        do {
            let dur = Recorder.duration(of: url) ?? 0
            let loud = Recorder.rms(of: url) ?? 0
            let data = try Data(contentsOf: url)
            try? FileManager.default.removeItem(at: url)
            guard dur >= 0.3 else {
                say(held >= 1 ? "Held it but nothing recorded — check the simulator audio input"
                    : String(format: "Too short (%.1fs) — press and hold to speak", dur))
                return
            }
            guard loud >= 0.001 else { say("No sound reached the mic — on the simulator, check the Mac mic permission and input"); return }
            let r = try await PipClient.voice(wav: data, screen: currentScreen,
                                              selectedWell: selectedWell?.id ?? "",
                                              openSheet: selectedWell == nil ? "" : "well_card")
            say("🎙 \(r.transcript)")
            handle(calls: r.tool_calls, messages: r.messages, reply: r.reply, ms: r.ms,
                   route: r.route, input: r.transcript, sources: r.sources,
                   robot: r.robot == true, dropped: r.dropped ?? 0,
                   ui: r.ui, speak: r.speak)
        } catch {
            say("Error: \(error.localizedDescription)")
        }
    }

    private func handle(calls: [ToolCall], messages: [String], reply: String, ms: [String: Int],
                        route: String?, input: String, sources: [Source],
                        robot: Bool, dropped: Int,
                        ui: [UIAction]?, speak: String?) {
        for msg in messages { say(msg) }
        if !reply.isEmpty {
            lastReply = reply
            say("Pip: \(reply)")
        }
        let uiOk = (ui ?? []).allSatisfy { applyUI($0) }
        if uiOk { sayAloud(speak) }
        if let show = calls.first(where: { $0.name == "show" }) {
            lab.lastShown = "plate \(show.args["target"]?.string ?? "")"
            lab.flash(parseShowTarget(show.args["target"]?.string))
        }
        if dropped > 0 { say("Ignored \(dropped) bad call(s)") }
        if robot { runRobot() }
        debug = DebugRun(
            input: input,
            calls: calls.map { ($0.name, "{" + $0.args.map { "\"\($0.key)\": \($0.value.compact)" }.sorted().joined(separator: ", ") + "}") },
            ms: ms,
            route: route,
            sources: sources.map(\.title)
        )
        say("⏱ " + msLine(ms))
    }

    private func runRobot() {
        robotState = .building
        Task {
            do {
                robotState = .done(try await PipClient.robot())
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
