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
    @State private var lastReply = ""
    @State private var recorder = Recorder()
    @State private var recording = false
    @State private var pressing = false
    @State private var recordStart: Date?
    @State private var busy = false
    @State private var selectedWell: WellSelect?
    @State private var memoFlash: SavedMemo?
    @State private var memoGen = 0
    @State private var robotState = RobotState.idle

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
                    inputBar
                }
                .frame(width: inner - left - 12)
            }
            .padding()
        }
        .sheet(item: $selectedWell) { sel in WellSheet(well: sel.id, lab: lab) { say($0) } }
        .sheet(isPresented: robotSheet) { robotPanel }
        .overlay(alignment: .top) {
            if let m = memoFlash {
                memoBanner(m)
                    .allowsHitTesting(false)
            }
        }
        .onAppear { connect() }
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
                    ForEach(lab.cards) { card in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(card.question).font(.callout)
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

    private func memoBanner(_ m: SavedMemo) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Note saved (\(m.target))").font(.caption.bold())
            Text(m.text).font(.body)
        }
        .frame(maxWidth: 420, alignment: .leading)
        .padding(10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
        .padding(.top, 12)
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
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
        case .timerWarning(let label):
            say("⏰ \(label): 5 minutes left")
        case .timerDone(let message):
            lastReply = message
            say("Pip: \(message)")
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
        if !lab.cards.isEmpty, isYesToAll(text) {
            answerAll()
            return
        }
        if !lab.cards.isEmpty, let ok = cardAnswer(for: text) {
            answer(ok)
            return
        }
        Task { await sendText(text) }
    }

    /// Card answers are yes/no/cancel only; everything else is a new command.
    private func cardAnswer(for text: String) -> Bool? {
        switch text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "yes": true
        case "no", "cancel": false
        default: nil
        }
    }

    private func isYesToAll(_ text: String) -> Bool {
        ["yes to all", "yes all", "approve all"].contains(
            text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())
    }

    private func sendText(_ text: String) async {
        busy = true
        defer { busy = false }
        do {
            let r = try await PipClient.orchestrate(text: text)
            handle(calls: r.tool_calls, messages: r.messages, reply: r.reply, ms: r.ms,
                   route: r.route, input: text, sources: r.sources,
                   savedMemo: r.saved_memo, robot: r.robot == true, dropped: r.dropped ?? 0)
        } catch {
            say("Error: \(error.localizedDescription)")
        }
    }

    private func startHold() {
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
            let r = try await PipClient.voice(wav: data)
            say("🎙 \(r.transcript)")
            if !lab.cards.isEmpty, isYesToAll(r.transcript) {
                answerAll()
                return
            }
            if !lab.cards.isEmpty, let ok = cardAnswer(for: r.transcript) {
                answer(ok)
                return
            }
            handle(calls: r.tool_calls, messages: r.messages, reply: r.reply, ms: r.ms,
                   route: r.route, input: r.transcript, sources: r.sources,
                   savedMemo: r.saved_memo, robot: r.robot == true, dropped: r.dropped ?? 0)
        } catch {
            say("Error: \(error.localizedDescription)")
        }
    }

    private func handle(calls: [ToolCall], messages: [String], reply: String, ms: [String: Int],
                        route: String?, input: String, sources: [Source],
                        savedMemo: SavedMemo?, robot: Bool, dropped: Int) {
        for msg in messages { say(msg) }
        if !reply.isEmpty {
            lastReply = reply
            say("Pip: \(reply)")
        }
        if let show = calls.first(where: { $0.name == "show" }) {
            lab.lastShown = "plate \(show.args["target"]?.string ?? "")"
            lab.flash(parseShowTarget(show.args["target"]?.string))
        }
        if let m = savedMemo { flashMemo(m) }
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

    private func flashMemo(_ m: SavedMemo) {
        memoFlash = m
        memoGen += 1
        let g = memoGen
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(2))
            if memoGen == g { memoFlash = nil }
        }
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
