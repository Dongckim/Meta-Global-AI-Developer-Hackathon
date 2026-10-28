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
                Text("read only").font(.caption).foregroundStyle(.secondary)
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

// MARK: - Setup

struct SetupView: View {
    var routeLine: String
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
            VStack(alignment: .leading, spacing: 2) {
                Text("Bluetooth audio").font(.headline)
                Text(routeLine.isEmpty ? "—" : routeLine).font(.callout)
                Text("Pick glasses audio in Control Center, then re-check.")
                    .font(.caption).foregroundStyle(.secondary)
            }
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
        Task {
            mic = await Recorder.requestPermission() ? "Granted" : "Denied — enable in Settings"
        }
    }

    private func checkServer() {
        Task {
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
        Task {
            guard await Recorder.requestPermission() else {
                heard = "Microphone permission needed"
                return
            }
            do {
                try recorder.start()
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
        Task {
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
