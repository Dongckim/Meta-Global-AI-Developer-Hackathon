import AVFoundation
import SwiftUI

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
    @State private var ttsOn = SpeechBus.shared.enabled
    @State private var routeLine = ""
    private let screens = loadScreens()

    /// Screen the server reasons about. One sheet at a time; robot wins.
    private var currentScreen: String {
        if case .idle = robotState {} else { return "robot_preview" }
        return selectedWell == nil ? "main" : "well_card"
    }

    var body: some View {
        GeometryReader { g in
            let inner = g.size.width - 32
            let left = inner * 0.58
            HStack(spacing: 12) {
                VStack(spacing: 8) {
                    PlateView(lab: lab, selected: $selectedWell)
                    GroupBars(lab: lab)
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
        .sheet(isPresented: $showSettings) { VoiceSettingsView() }
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
