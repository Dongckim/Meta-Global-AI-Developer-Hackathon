import SwiftUI
import AVFoundation

struct VoiceSettingsView: View {
    @State private var voiceID: String =
        SpeechBus.shared.voice?.identifier ?? SpeechBus.enUSVoices().first?.identifier ?? ""
    @State private var rate: Double = SpeechBus.shared.rate
    @State private var wakeEnabled = VoiceSession.wakeEnabled
    /// Opens the glasses & permissions (sync) screen.
    var onOpenSync: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    private let voices = SpeechBus.enUSVoices()

    var body: some View {
        NavigationStack {
            Form {
                Button("Glasses & permissions") { onOpenSync() }
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
