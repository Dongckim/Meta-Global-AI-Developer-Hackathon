import SwiftUI

struct DebugRun {
    var input: String
    var calls: [(name: String, args: String)]
    var ms: [(key: String, value: Int)]
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
    @State private var log: [String] = ["Pip 준비됨 — 말하거나 입력해"]
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
                if let first = lab.pending.first, let question = first.question {
                    confirmCard(question: question, detail: first.detail, rest: lab.pending.count - 1)
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
    }

    // MARK: - Subviews

    private var statusView: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(lab.stepIndex + 1)/\(lab.steps.count)단계 \(lab.steps[lab.stepIndex])")
                .font(.title2.bold())
            Text("완료 \(lab.done.count) · 제외 \(lab.excluded.count) · 다시 \(lab.redo.count) · 메모 \(lab.notes.count) · 팁 \(lab.tips.count)")
                .font(.callout).foregroundStyle(.secondary)
            if !lab.timers.isEmpty {
                Text("⏲ " + lab.timers.map { "\($0.label) \(fmtMinutes($0.minutes))분" }.joined(separator: " · "))
                    .font(.callout)
            }
            if !lab.lastShown.isEmpty {
                Text("🖥 \(lab.lastShown)").font(.callout)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func confirmCard(question: String, detail: String?, rest: Int) -> some View {
        VStack(spacing: 8) {
            Text(question).font(.headline)
            if let detail, !detail.isEmpty { Text(detail).font(.callout).foregroundStyle(.secondary) }
            if rest > 0 { Text("외 \(rest)개 대기").font(.caption).foregroundStyle(.secondary) }
            HStack(spacing: 16) {
                Button("응") { answer(true) }
                    .buttonStyle(.borderedProminent)
                Button("아니") { answer(false) }
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
            TextField("명령 입력", text: $input)
                .textFieldStyle(.roundedBorder)
                .onSubmit { submit() }
            if recording, let t0 = recordStart {
                TimelineView(.periodic(from: t0, by: 0.2)) { ctx in
                    Text(String(format: "%.1f초", ctx.date.timeIntervalSince(t0)))
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
            ForEach(d.calls.indices, id: \.self) { i in
                Text("\(d.calls[i].name)(\(d.calls[i].args))").font(.caption2.monospaced())
            }
            Text(d.ms.map { "\($0.key) \($0.value)" }.joined(separator: " · ") + " ms")
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
            say("취소함")
        }
    }

    private func submit() {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        input = ""
        say("> \(text)")
        if !lab.pending.isEmpty {
            if text == "응" { answer(true) } else if text == "아니" { answer(false) }
            else { say("먼저 카드에 답해줘 (응/아니)") }
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
            let r = try await PipClient.orchestrate(text: text, state: lab.serverState)
            handle(calls: r.tool_calls, reply: r.reply, ms: r.ms, input: text)
        } catch {
            say("오류: \(error.localizedDescription)")
        }
    }

    private func startHold() {
        pressing = true
        guard !recording, !busy else { return }
        guard lab.pending.isEmpty else { say("먼저 카드에 답해줘 (응/아니)"); return }
        Task {
            guard await Recorder.requestPermission() else { say("마이크 권한이 필요해"); return }
            guard pressing else { return } // released before the mic was ready
            do {
                try recorder.start()
                recordStart = Date()
                recording = true
            } catch {
                say("녹음 시작 실패: \(error.localizedDescription)")
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
            say("녹음 저장 실패: \(error.localizedDescription)")
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
                say(held >= 1 ? "눌렀는데 녹음이 안 됐어 — 시뮬레이터면 오디오 입력을 확인해"
                    : String(format: "너무 짧아 (%.1f초) — 꾹 누르고 말해줘", dur))
                return
            }
            guard loud >= 0.001 else { say("마이크에 아무 소리도 안 들어왔어 — 시뮬레이터면 Mac 마이크 권한·입력을 확인해"); return }
            let r = try await PipClient.voice(wav: data, state: lab.serverState)
            say("🎙 \(r.transcript)")
            handle(calls: r.tool_calls, reply: r.reply, ms: r.ms, input: r.transcript)
        } catch {
            say("오류: \(error.localizedDescription)")
        }
    }

    private func handle(calls: [ToolCall], reply: String, ms: [String: Int], input: String) {
        let actions = calls.compactMap { Action.fromTool(name: $0.name, args: $0.args) }
        for msg in lab.enqueue(actions) { say(msg) }
        if !reply.isEmpty { say("Pip: \(reply)") }
        let dropped = calls.count - actions.count
        if dropped > 0 { say("적용 못한 호출 \(dropped)개 무시") }
        debug = DebugRun(
            input: input,
            calls: calls.map { ($0.name, "{" + $0.args.map { "\"\($0.key)\": \($0.value.compact)" }.sorted().joined(separator: ", ") + "}") },
            ms: ms.sorted { $0.key < $1.key }.map { ($0.key, $0.value) }
        )
    }
}

#Preview {
    ContentView()
}
