import SwiftUI
import AVFoundation

struct VoiceSettingsView: View {
    @State private var voiceID: String =
        SpeechBus.shared.voice?.identifier ?? SpeechBus.enUSVoices().first?.identifier ?? ""
    @State private var rate: Double = SpeechBus.shared.rate
    @State private var wakeEnabled = VoiceSession.wakeEnabled
    @AppStorage("pip.setupDone") private var setupDone = true
    @Environment(\.dismiss) private var dismiss
    private let voices = SpeechBus.enUSVoices()

    var body: some View {
        NavigationStack {
            Form {
                Button("Open Setup") {
                    setupDone = false
                    dismiss()
                }
                Picker("Pip voice", selection: $voiceID) {
                    ForEach(voices, id: \.identifier) { v in
                        Text("\(v.name) (\(SpeechBus.qualityName(v.quality)))").tag(v.identifier)
                    }
                }
                HStack {
                    Text("Rate")
                    Slider(value: $rate, in: 0.3...0.7, step: 0.01)
                    Text(String(format: "%.2f", rate)).font(.caption.monospacedDigit())
                }
                Toggle("“Hey Pip” detection", isOn: $wakeEnabled)
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { dismiss() } }
            .onChange(of: voiceID) { _, id in SpeechBus.shared.voiceID = id }
            .onChange(of: rate) { _, r in SpeechBus.shared.rate = r }
            .onChange(of: wakeEnabled) { _, on in VoiceSession.wakeEnabled = on }
        }
    }
}
