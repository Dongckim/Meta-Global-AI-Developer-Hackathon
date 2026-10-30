import AVFoundation
import os
import SwiftUI

// MARK: - Pending cards (home, detail, and bench show the same cards)

struct CardPanel: View {
    var cards: [ServerCard]
    var onAnswer: (String, Bool) -> Void
    var onAnswerAll: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Persona.current.agentName).font(.caption.bold()).foregroundStyle(.secondary)
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

// MARK: - Protocol detail

struct GlassesAudioSection: View {
    /// Speaks through the voice session (same route switch and [audio] tts
    /// logs as a real reply); falls back to the bus without one.
    var voice: VoiceSession? = nil
    @State private var inputs: [AVAudioSessionPortDescription] = []
    @State private var routeText = "—"
    @State private var pick = Recorder.preferredInputUID
    @State private var a2dp = Recorder.a2dpEnabled
    @State private var outputMode = Recorder.outputMode
    @State private var sessionMode = Recorder.sessionMode
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
            #if DEBUG
            // Compare Milo's voice on the glasses; [audio] tts start/end and
            // "speech route" log lines show what each choice did.
            Picker("Output while speaking", selection: $outputMode) {
                Text("HFP only").tag("hfp_only")
                Text("A2DP while speaking").tag("a2dp_while_speaking")
            }
            .pickerStyle(.segmented)
            .onChange(of: outputMode) { _, v in Recorder.outputMode = v }
            Picker("Session mode", selection: $sessionMode) {
                Text("default").tag("default")
                Text("voiceChat").tag("voiceChat")
            }
            .pickerStyle(.segmented)
            .onChange(of: sessionMode) { _, v in
                Recorder.sessionMode = v
                try? Recorder.configureSession()
                refresh()
            }
            #endif
            TimelineView(.animation(minimumInterval: 1.0 / 15)) { _ in
                LevelBar(level: tester.currentPeak)
            }
            .frame(height: 10)
            HStack {
                Button(testing ? "Testing…" : "3s record → play") { loopbackTest() }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(testing)
                Button("Hear \(Persona.current.agentName)") {
                    let line = "Glasses audio check. Can you hear \(Persona.current.agentName)? "
                        + "Step four done, three wells left: D2, D6 and D7."
                    if let voice { voice.speak(line) } else { SpeechBus.shared.speak(line) }
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

// MARK: - Diagnostics (DEBUG: long-press the glasses icon on Glasses sync)

#if DEBUG
/// Developer checks that used to live under Setup's Diagnostics.
struct DiagnosticsView: View {
    var voice: VoiceSession? = nil
    /// The glasses (for the photo test); nil when DAT isn't set up.
    var dat: GlassesDAT? = nil
    @State private var photoTest = "Not run"
    @State private var photoThumb: UIImage?
    @Environment(\.dismiss) private var dismiss
    @State private var mic = "Not checked"
    @State private var server = "Not checked"
    @State private var heard = ""
    @State private var recorder = Recorder()
    @State private var recording = false
    @State private var busy = false
    @State private var command = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    row("Microphone", status: mic) {
                        Button("Check") { checkMic() }.buttonStyle(.bordered).controlSize(.small)
                    }
                    row("Server", status: server) {
                        Button("Check") { checkServer() }.buttonStyle(.bordered).controlSize(.small)
                    }
                    row(Persona.current.wakePhrase, status: voice?.wakeStatus ?? "Not checked") {
                        Button("Check") { checkWake() }.buttonStyle(.bordered).controlSize(.small)
                        #if DEBUG
                        Button("Measure ×10") { voice?.measureWake(count: 10) }
                            .buttonStyle(.bordered).controlSize(.small)
                        #endif
                    }
                    row("TTS voice", status: SpeechBus.shared.voiceInfo) { EmptyView() }
                    // test only (tap, not in the registry): is the glasses mic (HFP) what stops the photo?
                    row("Glasses photo test", status: photoTest) {
                        Button("Run") { runPhotoTest() }.buttonStyle(.bordered).controlSize(.small).disabled(busy)
                    }
                    if let photoThumb {
                        Image(uiImage: photoThumb).resizable().scaledToFit().frame(height: 120)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Text command").font(.headline)
                        TextField("Type a command (sent like speech)", text: $command)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit {
                                let t = command.trimmingCharacters(in: .whitespacesAndNewlines)
                                guard !t.isEmpty else { return }
                                command = ""
                                voice?.type(t)
                            }
                    }
                    GlassesAudioSection(voice: voice)
                    #if DEBUG
                    SampleRecorder()
                    #endif
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Voice check").font(.headline)
                        Text("Hold the mic and say “\(Persona.current.wakePhrase)”.").font(.callout)
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
                }
                .padding()
            }
            .navigationTitle("Diagnostics")
            .toolbar { Button("Done") { dismiss() } }
        }
    }

    private func row(_ title: String, status: String,
                     @ViewBuilder action: () -> some View) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(status).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            action()
        }
    }

    /// One glasses photo the way a photo question takes it: the stream
    /// (medium, low on HFP), then a photo from it, then the stream stops.
    private func runPhotoTest() {
        guard let dat else {
            photoTest = "No glasses (DAT isn't set up)"
            return
        }
        busy = true
        photoThumb = nil
        photoTest = "Running…"
        Task { @MainActor in
            Log.view.info("[look] test: start the glasses stream")
            dat.startLive()
            let t0 = Date()
            while dat.liveFrame == nil, Date().timeIntervalSince(t0) < 10 {
                try? await Task.sleep(for: .milliseconds(200))
            }
            Log.view.info("[look] test: first frame \(dat.liveFrame != nil ? "in" : "none", privacy: .public) after \(Int(Date().timeIntervalSince(t0) * 1000)) ms")
            let t1 = Date()
            do {
                let data = try await dat.takePhoto()
                let img = UIImage(data: data)
                let px = img.map { "\(Int($0.size.width * $0.scale))x\(Int($0.size.height * $0.scale))" } ?? "?"
                Log.view.info("[look] test: OK \(px, privacy: .public) px, \(data.count) bytes, \(Int(Date().timeIntervalSince(t1) * 1000)) ms")
                photoTest = "OK: \(px) px, \(data.count / 1024) KB"
                photoThumb = img
            } catch {
                Log.view.error("[look] test: failed: \(String(describing: error), privacy: .public)")
                photoTest = "Failed: \(error)"
            }
            dat.stopLive()
            busy = false
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

    private func checkWake() {
        Task { @MainActor in
            _ = await WakeListener.requestAccess()
            voice?.refresh()
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
                let r = try await PipClient.voice(wav: data)
                guard r.ignored != true else { return }
                heard = r.transcript.isEmpty ? "(nothing heard)" : r.transcript
                // the one speaking path (speaking state, live mic muted)
                if let voice { voice.speak(r.speak ?? r.reply) } else { SpeechBus.shared.speak(r.speak ?? r.reply) }
            } catch is PipClient.VoiceError {
                heard = "Too short — press and hold to speak"
            } catch {
                heard = "Error: \(error.localizedDescription)"
            }
        }
    }
}
#endif

#if DEBUG
/// Records a long 16 kHz mono 16-bit WAV from the current mic route (the
/// glasses when HFP is on) for the realtime ASR measurement, then shares it
/// (AirDrop to the Mac). Uses the existing Recorder on the one shared tap.
struct SampleRecorder: View {
    @State private var recorder = Recorder()
    @State private var recording = false
    @State private var started: Date?
    @State private var file: URL?
    @State private var note = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("ASR sample (debug)").font(.headline)
            Text("Tap Record, read the script, tap Stop, then Share the WAV.").font(.callout)
            HStack {
                Button(recording ? "Stop" : "Record") { recording ? stop() : start() }
                    .buttonStyle(.borderedProminent).tint(recording ? .red : .blue)
                if recording, let t0 = started {
                    TimelineView(.periodic(from: t0, by: 1)) { ctx in
                        Text(String(format: "%.0fs", ctx.date.timeIntervalSince(t0))).monospacedDigit()
                    }
                }
                if let file, !recording {
                    ShareLink(item: file) { Label("Share WAV", systemImage: "square.and.arrow.up") }
                }
            }
            if !note.isEmpty { Text(note).font(.caption).foregroundStyle(.secondary) }
        }
    }

    private func start() {
        Task { @MainActor in
            guard await Recorder.requestPermission() else { note = "Microphone permission needed"; return }
            do {
                try await recorder.start()
                recording = true
                started = Date()
                note = "route: \(Log.route())"
            } catch {
                note = "Recording failed: \(error.localizedDescription)"
            }
        }
    }

    private func stop() {
        recording = false
        do {
            let url = try recorder.stop()
            let name = "asr-sample-\(Int(Date().timeIntervalSince1970)).wav"
            let dest = url.deletingLastPathComponent().appending(path: name)
            try? FileManager.default.removeItem(at: dest)
            try FileManager.default.moveItem(at: url, to: dest)
            file = dest
            note = String(format: "%.1f s · 16 kHz mono 16-bit", Recorder.duration(of: dest) ?? 0)
        } catch {
            note = "Couldn't save: \(error.localizedDescription)"
        }
    }
}
#endif
