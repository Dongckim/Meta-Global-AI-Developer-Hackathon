import SwiftUI

struct DebugRun {
    var input: String
    var calls: [(name: String, args: String)]
    var ms: [String: Int]
    var route: String?
}

/// Reliable press-and-hold: tracks the button's own pressed state, no gesture fights.
struct HoldStyle: ButtonStyle {
    let onHold: (Bool) -> Void
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .onChange(of: configuration.isPressed) { _, pressed in onHold(pressed) }
    }
}

struct ContentView: View {
    @State private var lab = LabState()
    @State private var input = ""
    @State private var log: [String] = ["Pip ready — speak or type"]
    @State private var debug: DebugRun?
    @State private var recorder = Recorder()
    @State private var recording = false
    @State private var pressing = false
    @State private var recordStart: Date?
    @State private var busy = false

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            VStack(spacing: 12) {
                statusView
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 4) {
                            ForEach(log.indices, id: \.self) { i in
                                Text(log[i]).font(.body).textSelection(.enabled)
                            }
                            Color.clear.frame(height: 1).id("logBottom")
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .onChange(of: log.count) { _, _ in
                        proxy.scrollTo("logBottom", anchor: .bottom)
                    }
                }
                if let first = lab.pending.first {
                    confirmCard(question: first.question, detail: first.detail, rest: lab.pending.count - 1)
                }
                inputBar
            }
            .padding()
            if let d = debug {
                debugPanel(d)
                    .padding(.trailing)
                    .padding(.bottom, 84)
            }
        }
        .onAppear { lab.loadPlan() }
    }

    // MARK: - Subviews

    private var statusView: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Step \(lab.stepIndex + 1) of \(lab.steps.count): \(lab.steps[lab.stepIndex])")
                .font(.title2.bold())
            Text("Done \(lab.done.count) · Excluded \(lab.excluded.count) · Redo \(lab.redo.count) · Notes \(lab.notes.count) · Tips \(lab.tips.count)")
                .font(.callout).foregroundStyle(.secondary)
            if !lab.groups.isEmpty {
                Text(lab.groupProgress().map { "\($0.name) \($0.have)/\($0.need)" }.joined(separator: " · "))
                    .font(.callout)
            }
            if !lab.timers.isEmpty {
                Text("⏲ " + lab.timers.map { "\($0.label) \(fmtMinutes($0.minutes)) min" }.joined(separator: " · "))
                    .font(.callout)
            }
            if !lab.lastShown.isEmpty {
                Text("Viewing \(lab.lastShown)").font(.callout)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func confirmCard(question: String, detail: String?, rest: Int) -> some View {
        VStack(spacing: 8) {
            Text(question).font(.headline)
            if let detail, !detail.isEmpty { Text(detail).font(.callout).foregroundStyle(.secondary) }
            if rest > 0 { Text("\(rest) more waiting").font(.caption).foregroundStyle(.secondary) }
            HStack(spacing: 16) {
                Button("Yes") { answer(true) }
                    .buttonStyle(.borderedProminent)
                Button("No") { answer(false) }
                    .buttonStyle(.bordered)
            }
        }
        .frame(maxWidth: .infinity)
        .padding()
        .background(.yellow.opacity(0.15), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.yellow.opacity(0.5)))
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
            Text(msLine(d.ms))
                .font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: 320, alignment: .leading)
        .padding(10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - Logic

    private func say(_ s: String) {
        log.append(s)
        if log.count > 100 { log.removeFirst(log.count - 100) }
    }

    private func answer(_ ok: Bool) {
        if let msg = lab.confirmFirst(ok) {
            say(msg)
        } else {
            say("Canceled")
        }
    }

    private func submit() {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        input = ""
        say("> \(text)")
        if !lab.pending.isEmpty {
            if text.lowercased() == "yes" { answer(true) } else if text.lowercased() == "no" { answer(false) }
            else { say("Answer the card first (yes/no)") }
            return
        }
        if let action = Action.rule(text) {
            for msg in lab.enqueue([action]) { say(msg) }
            return
        }
        Task { await sendText(text) }
    }

    private func sendText(_ text: String) async {
        busy = true
        defer { busy = false }
        do {
            let r = try await PipClient.orchestrate(text: text, state: lab.takeState())
            handle(calls: r.tool_calls, reply: r.reply, ms: r.ms, route: r.route, input: text)
        } catch {
            lab.answerTarget = nil
            say("Error: \(error.localizedDescription)")
        }
    }

    private func startHold() {
        pressing = true
        guard !recording, !busy else { return }
        guard lab.pending.isEmpty else { say("Answer the card first (yes/no)"); return }
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
            let r = try await PipClient.voice(wav: data, state: lab.takeState())
            say("🎙 \(r.transcript)")
            handle(calls: r.tool_calls, reply: r.reply, ms: r.ms, route: r.route, input: r.transcript)
        } catch {
            lab.answerTarget = nil
            say("Error: \(error.localizedDescription)")
        }
    }

    private func handle(calls: [ToolCall], reply: String, ms: [String: Int], route: String?, input: String) {
        let actions = calls.compactMap { Action.fromTool(name: $0.name, args: $0.args) }
        let wasPending = lab.pending.count
        for msg in lab.enqueue(actions) { say(msg) }
        lab.answerTarget = nil // this round's timer labels are already applied
        lab.noteQuestion(reply: reply, target: targetOf(calls.map(\.args)))
        if let q = lab.pending.dropFirst(wasPending).first(where: \.fromCatch)?.question {
            say("Pip: \(q)")
        } else if !reply.isEmpty {
            say("Pip: \(reply)")
        }
        let dropped = calls.count - actions.count
        if dropped > 0 { say("Ignored \(dropped) bad call(s)") }
        debug = DebugRun(
            input: input,
            calls: calls.map { ($0.name, "{" + $0.args.map { "\"\($0.key)\": \($0.value.compact)" }.sorted().joined(separator: ", ") + "}") },
            ms: ms,
            route: route
        )
        say("⏱ " + msLine(ms))
    }
}

#Preview {
    ContentView()
}
