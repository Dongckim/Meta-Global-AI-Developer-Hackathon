import AVFoundation
import SwiftUI

func fmtDate(_ ts: Double?) -> String {
    guard let ts else { return "—" }
    return Date(timeIntervalSince1970: ts).formatted(date: .abbreviated, time: .shortened)
}

// MARK: - Pending cards (home, detail, and bench show the same cards)

struct CardPanel: View {
    var cards: [ServerCard]
    var onAnswer: (String, Bool) -> Void
    var onAnswerAll: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Pip").font(.caption.bold()).foregroundStyle(.secondary)
            if cards.count >= 2 {
                HStack {
                    Text("Checklist (\(cards.count))").font(.headline)
                    Spacer()
                    Button("Yes to all") { onAnswerAll() }
                        .buttonStyle(.borderedProminent).controlSize(.small)
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(cards.enumerated()), id: \.element.id) { idx, card in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(card.question).font(.callout).bold(idx == 0)
                                    if let detail = card.detail, !detail.isEmpty {
                                        Text(detail).font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                                Spacer()
                                Button("Yes") { onAnswer(card.id, true) }
                                    .buttonStyle(.borderedProminent).controlSize(.small)
                                Button("No") { onAnswer(card.id, false) }
                                    .buttonStyle(.bordered).controlSize(.small)
                            }
                            .padding(4)
                            .background(idx == 0 ? Color.accentColor.opacity(0.12) : .clear,
                                        in: RoundedRectangle(cornerRadius: 6))
                        }
                    }
                }
                .frame(maxHeight: 180)
            } else if let first = cards.first {
                Text(first.question).font(.headline)
                if let detail = first.detail, !detail.isEmpty {
                    Text(detail).font(.callout).foregroundStyle(.secondary)
                }
                HStack(spacing: 16) {
                    Button("Yes") { onAnswer(first.id, true) }
                        .buttonStyle(.borderedProminent)
                    Button("No") { onAnswer(first.id, false) }
                        .buttonStyle(.bordered)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - Home

struct HomeView: View {
    var protocols: [ProtocolSummary]
    var runs: [RunSummary]
    var cards: [ServerCard]
    var onImport: () -> Void
    var onReview: () -> Void
    var hasDraft: Bool
    var onProtocol: (String) -> Void
    var onRun: (String) -> Void
    var onAnswer: (String, Bool) -> Void
    var onAnswerAll: () -> Void
    var onSetup: () -> Void
    var onRefresh: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Pip").font(.title.bold())
                Spacer()
                Button { onSetup() } label: { Image(systemName: "gearshape") }
                    .buttonStyle(.bordered).controlSize(.small)
                Button { onRefresh() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.bordered).controlSize(.small)
            }
            if !cards.isEmpty {
                CardPanel(cards: cards, onAnswer: onAnswer, onAnswerAll: onAnswerAll)
            }
            HStack {
                Text("Protocols").font(.headline)
                Spacer()
                Button(hasDraft ? "Review" : "Import") { hasDraft ? onReview() : onImport() }
                    .buttonStyle(.borderedProminent).controlSize(.small)
            }
            Text("Demo protocols only — no real experiment data.")
                .font(.caption).foregroundStyle(.secondary)
            List(protocols) { p in
                Button { onProtocol(p.id) } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(p.name).font(.callout.bold())
                            Text("\(p.conditions) conditions · \(p.steps) steps")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(fmtDate(p.importedAt)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxHeight: 260)
            Text("Runs").font(.headline)
            List(runs) { r in
                Button { onRun(r.runId) } label: {
                    HStack {
                        Circle().fill(r.status == "active" ? .green : .secondary)
                            .frame(width: 8, height: 8)
                        VStack(alignment: .leading) {
                            Text(r.protocolName ?? r.runId).font(.callout.bold())
                            Text(runCounts(r)).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(fmtDate(r.startedAt)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Spacer()
        }
        .padding()
    }

    private func runCounts(_ r: RunSummary) -> String {
        guard let s = r.summary else { return r.status }
        let have = s.conditions.map(\.have).reduce(0, +)
        return "\(r.status) · \(have) wells · \(s.notes) notes · \(s.excluded) excluded"
    }
}

// MARK: - Protocol detail

struct ProtocolDetailView: View {
    var protocolId: String
    var cards: [ServerCard]
    var notice: String
    var onBack: () -> Void
    var onStart: (String) -> Void
    var onRun: (String) -> Void
    var onAnswer: (String, Bool) -> Void
    var onAnswerAll: () -> Void
    @State private var doc: ProtocolDoc?
    @State private var runs: [RunSummary] = []
    @State private var error = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button { onBack() } label: {
                    Image(systemName: "chevron.left").font(.headline)
                }
                .buttonStyle(.borderless)
                Text(doc?.name ?? "Protocol").font(.title2.bold())
                Spacer()
            }
            if !cards.isEmpty {
                CardPanel(cards: cards, onAnswer: onAnswer, onAnswerAll: onAnswerAll)
            }
            if !notice.isEmpty {
                Text(notice).font(.callout).foregroundStyle(.red)
            }
            if !error.isEmpty {
                Text(error).foregroundStyle(.red)
            } else if let doc {
                Text("\(doc.plan.conditions.count) conditions · \(doc.plan.steps.count) steps")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Start new run") { onStart(doc.id) }
                    .buttonStyle(.borderedProminent)
                List {
                    Section("Steps") {
                        ForEach(Array(doc.plan.steps.enumerated()), id: \.offset) { i, s in
                            VStack(alignment: .leading, spacing: 2) {
                                Text("[\(i + 1)] \(s.text)").font(.callout)
                                Text(stepLine(s)).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    Section("Runs") {
                        if runs.isEmpty {
                            Text("No runs yet").font(.callout).foregroundStyle(.secondary)
                        }
                        ForEach(runs) { r in
                            Button { onRun(r.runId) } label: {
                                HStack {
                                    Circle().fill(r.status == "active" ? .green : .secondary)
                                        .frame(width: 8, height: 8)
                                    Text(fmtDate(r.startedAt)).font(.callout)
                                    Spacer()
                                    Text(r.status).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            } else {
                ProgressView()
            }
            Spacer()
        }
        .padding()
        .task { await load() }
        .onChange(of: protocolId) { _, _ in Task { await load() } }
    }

    private func stepLine(_ s: PlanStep) -> String {
        var bits = [s.kind]
        if let m = s.minutes { bits.append(fmtNum(m) + " min") }
        if let v = s.pipette?.volume {
            bits.append(fmtNum(v) + " " + (s.pipette?.unit ?? "uL"))
        }
        return bits.joined(separator: " · ")
    }

    @MainActor
    private func load() async {
        error = ""
        do {
            async let d = PipClient.protocolDetail(id: protocolId)
            async let r = PipClient.runs(protocolId: protocolId)
            doc = try await d
            runs = try await r
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Past run (read-only)

struct PastRunView: View {
    var runId: String
    var onBack: () -> Void
    var onSource: (_ target: String, _ page: Int?, _ phrase: String?) -> Void
    @State private var doc: RunDoc?
    @State private var error = ""
    @State private var rlab = LabState()
    @State private var selected: WellSelect?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button { onBack() } label: {
                    Image(systemName: "chevron.left").font(.headline)
                }
                .buttonStyle(.borderless)
                Text(doc?.protocolName ?? "Past run").font(.title2.bold())
                Spacer()
                Text("VIEWING past run \(runId) (read-only)").font(.caption2).foregroundStyle(.orange)
                    .padding(4).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 6))
            }
            if !error.isEmpty {
                Text(error).foregroundStyle(.red)
            } else if doc != nil {
                GeometryReader { g in
                    let inner = g.size.width - 32
                    HStack(spacing: 12) {
                        VStack(spacing: 8) {
                            PlateView(lab: rlab, selected: $selected)
                            ConditionBars(lab: rlab)
                        }
                        .frame(width: inner * 0.58)
                        VStack(spacing: 8) {
                            StepCard(lab: rlab, onSource: openStepSource)
                            TimerList(lab: rlab)
                            MemoList(lab: rlab) { selected = WellSelect(id: $0) }
                            historySection
                            Button("Hand off (soon)") {}
                                .buttonStyle(.bordered).controlSize(.small)
                                .disabled(true)
                        }
                        .frame(width: inner * 0.42 - 12)
                    }
                    .padding()
                }
            } else {
                ProgressView()
                Spacer()
            }
        }
        .padding()
        .task { await load() }
        .onChange(of: runId) { _, _ in Task { await load() } }
        .sheet(item: $selected) { sel in
            WellSheet(well: sel.id, lab: rlab, readOnly: true) { _ in }
        }
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("History").font(.headline)
            if rlab.history.isEmpty {
                Text("No changes").font(.callout).foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(rlab.history.suffix(20)) { h in
                            Text("\(h.action) \(h.wells.joined(separator: ","))")
                                .font(.caption)
                        }
                    }
                }
                .frame(maxHeight: 120)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private func openStepSource() {
        guard let pid = doc?.protocolId, !pid.isEmpty,
              rlab.stepIndex < rlab.planSteps.count else { return }
        let s = rlab.planSteps[rlab.stepIndex]
        onSource("protocol:\(pid)#step:\(rlab.stepIndex + 1)", s.quotePage, s.quote)
    }

    @MainActor
    private func load() async {
        error = ""
        do {
            let d = try await PipClient.runDetail(id: runId)
            doc = d
            rlab.sync(d.record)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Glasses audio (iOS Bluetooth; no DAT dependency)

struct GlassesAudioSection: View {
    @State private var inputs: [AVAudioSessionPortDescription] = []
    @State private var routeText = "—"
    @State private var pick = Recorder.preferredInputUID
    @State private var a2dp = Recorder.a2dpEnabled
    @State private var testing = false
    @State private var testResult = ""
    @State private var player: AVAudioPlayer?
    @State private var tester = Recorder()

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Glasses audio").font(.headline)
            Text(routeText).font(.callout)
            Text(hfpLine).font(.callout).foregroundStyle(hfpFound ? .green : .secondary)
            Picker("Microphone", selection: $pick) {
                Text("Auto (prefer glasses)").tag("auto")
                ForEach(inputs, id: \.uid) { p in
                    Text("\(p.portName) (\(shortType(p.portType)))").tag(p.uid)
                }
            }
            .pickerStyle(.menu)
            .onChange(of: pick) { _, v in
                Recorder.preferredInputUID = v
                Recorder.ensurePreferredInput()
                refresh()
            }
            Toggle("A2DP output (better sound, mic may stay on iPad)", isOn: $a2dp)
                .font(.callout)
                .onChange(of: a2dp) { _, v in
                    Recorder.a2dpEnabled = v
                    try? Recorder.configureSession()
                    refresh()
                }
            TimelineView(.animation(minimumInterval: 1.0 / 15)) { _ in
                LevelBar(level: tester.currentPeak)
            }
            .frame(height: 10)
            HStack {
                Button(testing ? "Testing…" : "3s record → play") { loopbackTest() }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(testing)
                Button("Hear Pip") {
                    SpeechBus.shared.speak("Glasses audio check. Can you hear Pip?")
                }
                .buttonStyle(.bordered).controlSize(.small)
            }
            if !testResult.isEmpty {
                Text(testResult).font(.callout).foregroundStyle(.secondary)
            }
            Text("Pair glasses in Settings; pick their audio in Control Center.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .onAppear { refresh() }
        .onReceive(NotificationCenter.default.publisher(
            for: AVAudioSession.routeChangeNotification)) { _ in refresh() }
    }

    private var hfpFound: Bool { Recorder.hfpInput() != nil }

    private var hfpLine: String {
        if let h = Recorder.hfpInput() { return "Glasses mic: \(h.portName) ✓" }
        return "Glasses mic: not found"
    }

    private func shortType(_ t: AVAudioSession.Port) -> String {
        switch t {
        case .bluetoothHFP: return "HFP"
        case .bluetoothA2DP: return "A2DP"
        case .bluetoothLE: return "BLE"
        case .builtInMic: return "built-in"
        default: return "other"
        }
    }

    private func refresh() {
        let session = AVAudioSession.sharedInstance()
        inputs = session.availableInputs ?? []
        let ins = session.currentRoute.inputs
            .map { "\($0.portName) (\(shortType($0.portType)))" }.joined(separator: ", ")
        let outs = session.currentRoute.outputs
            .map { "\($0.portName) (\(shortType($0.portType)))" }.joined(separator: ", ")
        let khz = String(format: "%.1f", session.sampleRate / 1000)
        routeText = "in: \(ins.isEmpty ? "—" : ins) · out: \(outs.isEmpty ? "—" : outs) · \(khz) kHz"
    }

    private func loopbackTest() {
        guard !testing else { return }
        testing = true
        testResult = ""
        Task { @MainActor in
            defer { testing = false }
            guard await Recorder.requestPermission() else {
                testResult = "Mic permission needed"
                return
            }
            do {
                try await tester.start()
                try? await Task.sleep(for: .seconds(3))
                let url = try tester.stop()
                testResult = fileLine(url)
                try AVAudioSession.sharedInstance().setActive(true)
                player = try AVAudioPlayer(contentsOf: url)
                player?.play()
            } catch {
                testResult = "Failed: \(error.localizedDescription)"
            }
        }
    }

    /// The HFP→WAV proof: whatever the input rate, the file must be 16 kHz mono.
    private func fileLine(_ url: URL) -> String {
        guard let f = try? AVAudioFile(forReading: url) else { return "Unreadable file" }
        let fmt = f.fileFormat
        let ok = fmt.sampleRate == 16000 && fmt.channelCount == 1
        let dur = String(format: "%.1f", Recorder.duration(of: url) ?? 0)
        let rms = Recorder.rms(of: url).map { String(format: "%.2f", $0) } ?? "?"
        return "\(dur)s · \(Int(fmt.sampleRate)) Hz \(fmt.channelCount)ch \(ok ? "✓" : "⚠ expected 16 kHz mono") · rms \(rms)"
    }
}

struct LevelBar: View {
    var level: Float

    var body: some View {
        GeometryReader { g in
            RoundedRectangle(cornerRadius: 3).fill(.quaternary)
            RoundedRectangle(cornerRadius: 3)
                .fill(level > 0.6 ? .red : .green)
                .frame(width: g.size.width * CGFloat(min(1, level * 3)))
        }
    }
}

// MARK: - Setup

struct SetupView: View {
    var onDone: () -> Void
    @State private var mic = "Not checked"
    @State private var server = "Not checked"
    @State private var heard = ""
    @State private var recorder = Recorder()
    @State private var recording = false
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Setup").font(.title.bold())
            row("Microphone", status: mic) {
                Button("Check") { checkMic() }.buttonStyle(.bordered).controlSize(.small)
            }
            row("Server", status: server) {
                Button("Check") { checkServer() }.buttonStyle(.bordered).controlSize(.small)
            }
            GlassesAudioSection()
            VStack(alignment: .leading, spacing: 4) {
                Text("Voice check").font(.headline)
                Text("Hold the mic and say “Hey Pip”.").font(.callout)
                HStack {
                    Button(action: {}) {
                        Image(systemName: recording ? "mic.fill" : "mic")
                            .font(.title2).foregroundStyle(.white)
                            .padding(10).background(recording ? .red : .blue, in: Circle())
                    }
                    .buttonStyle(HoldStyle { pressed in pressed ? startHold() : endHold() })
                    .disabled(busy)
                    if !heard.isEmpty {
                        Text("“\(heard)”").font(.callout).foregroundStyle(.secondary)
                    }
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("Mode").font(.headline)
                HStack {
                    Text("Full").font(.callout.bold())
                        .padding(6).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 6))
                    Text("Private — coming soon").font(.callout).foregroundStyle(.secondary)
                        .padding(6).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 6))
                }
            }
            HStack {
                Text("DAT registration").font(.headline)
                Text("next").font(.caption).foregroundStyle(.secondary)
                    .padding(4).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 6))
            }
            Spacer()
            Button("Done") { onDone() }
                .buttonStyle(.borderedProminent)
        }
        .padding()
    }

    private func row(_ title: String, status: String, action: () -> some View) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(status).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            action()
        }
    }

    private func checkMic() {
        Task { @MainActor in
            mic = await Recorder.requestPermission() ? "Granted" : "Denied — enable in Settings"
        }
    }

    private func checkServer() {
        Task { @MainActor in
            do {
                let h = try await PipClient.health()
                server = h.ok ? "Connected (\(h.tools ?? 0) tools)" : "Unhealthy"
            } catch {
                server = "Unreachable: \(error.localizedDescription)"
            }
        }
    }

    private func startHold() {
        guard !recording, !busy else { return }
        Task { @MainActor in
            guard await Recorder.requestPermission() else {
                heard = "Microphone permission needed"
                return
            }
            do {
                try await recorder.start()
                recording = true
            } catch {
                heard = "Recording failed"
            }
        }
    }

    private func endHold() {
        guard recording else { return }
        recording = false
        let url: URL
        do {
            url = try recorder.stop()
        } catch {
            heard = "Couldn't save the recording"
            return
        }
        busy = true
        Task { @MainActor in
            defer { busy = false }
            do {
                let data = try Data(contentsOf: url)
                try? FileManager.default.removeItem(at: url)
                let r = try await PipClient.voice(wav: data, screen: "setup")
                heard = r.transcript.isEmpty ? "(nothing heard)" : r.transcript
                SpeechBus.shared.speak(r.speak ?? r.reply)
            } catch {
                heard = "Error: \(error.localizedDescription)"
            }
        }
    }
}
